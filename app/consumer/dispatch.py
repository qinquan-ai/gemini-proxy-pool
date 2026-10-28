"""候选推进循环：网关内唯一的重试与结算入口。

约定：
* ``attempt_fn`` 只发请求并回报结果，**不碰账本**。结算统一在这里做。
* 流式成功时 ``defer_settle=True``，lease 交给调用方，等流结束再结算。
* 重试严格沿候选数组前进，不回头、不重置下标、不对冲。
* 总时长受 ``spec.timeout_s`` 约束，到点就返回最后一次结果。
"""

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import quote
from dataclasses import dataclass, field
from typing import Any, Callable

from app.scheduling.policy import RouteSpec, build_candidates
from app.scheduling.limits import Lease, SlotRejection
from app.core.request_tag import request_tag

# 等待令牌时的检查粒度：短睡便于及时响应客户端断开
WAIT_SLICE_S = 0.5

# 每次上游调用的耗时流水（append-only，按天一个文件）；供排查与对比，不参与限速
TIMING_DIR = Path(__file__).resolve().parents[2] / "logs" / "timing"


def _log_timing(
    key_name: str,
    model: str,
    elapsed_ms: float,
    *,
    ok: bool,
    outcome: str,
    attempt: int,
) -> None:
    """追加一条调用耗时记录；写盘失败不影响主流程。"""
    try:
        TIMING_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tag": request_tag.get(),
            "key": key_name,
            "model": model,
            "elapsed_ms": int(elapsed_ms),
            "attempt": attempt,
            "ok": ok,
            "outcome": outcome,
        }
        path = TIMING_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


@dataclass
class Attempt:
    lease: Lease
    key_info: dict
    model: str

    @property
    def key_name(self) -> str:
        return self.key_info.get("name", "N/A")


@dataclass
class AttemptResult:
    ok: bool
    status: int = 0
    error: str = ""
    usage: dict = field(default_factory=dict)
    payload: Any = None
    outcome: str = "server_error"
    cost_ms: float | None = None
    retry_after_s: float | None = None
    cooldown_s: float | None = None
    terminal: bool = False
    defer_settle: bool = False
    reason: str = ""
    retry_after_ms: int = 0
    detail: list = field(default_factory=list)


def rpd_remaining(pool, model: str) -> int:
    total = 0
    for key_info in pool.keys:
        quota = pool._get_model_quota(key_info, model)
        total += max(0, int(quota.get("limit_rpd", 0)) - int(quota.get("used_today", 0)))
    return total


def slot_headers(
    spec: RouteSpec,
    *,
    model_used: str = "",
    key_used: str = "",
    result: AttemptResult | None = None,
    pool=None,
) -> dict[str, str]:
    headers = {"X-Timeout-Effective-Ms": str(int(spec.timeout_s * 1000))}
    if spec.timeout_note:
        headers["X-Timeout-Note"] = spec.timeout_note
    if model_used:
        headers["X-Model-Used"] = model_used
        if pool is not None:
            headers["X-Slot-RPD-Remaining"] = str(rpd_remaining(pool, model_used))
    if key_used:
        # Key 名可能是中文，HTTP 头只能 latin-1，这里统一 percent-encode
        headers["X-Key-Used"] = quote(key_used, safe="")
    if result is not None and not result.ok:
        if result.reason:
            headers["X-Slot-Reason"] = result.reason
        if result.retry_after_ms:
            headers["X-Slot-Retry-After-Ms"] = str(result.retry_after_ms)
    return headers


async def run_candidates(
    spec: RouteSpec,
    registry,
    pool,
    attempt_fn: Callable[[dict, str, Lease], Any],
    *,
    max_attempts: int | None = None,
) -> tuple[Attempt | None, AttemptResult]:
    """沿候选数组推进，返回最终成功或最后一次失败。

    成功且 ``defer_settle`` 时返回的 Attempt 携带未结算的 lease，
    调用方必须在流结束后调用 ``registry.settle``。
    """
    candidates = build_candidates(spec, pool)
    limit = max_attempts if max_attempts is not None else spec.max_attempts
    if not candidates:
        return None, AttemptResult(
            ok=False,
            status=503,
            error="No candidate key/model available",
            outcome="none",
            reason="no_candidate",
        )

    deadline = spec.deadline()
    attempts = 0
    last = AttemptResult(ok=False, status=503, error="No attempt was made")
    rejections: list[dict] = []

    for key_info, model in candidates:
        if attempts >= limit:
            break
        while True:
            now = time.time()
            if now >= deadline:
                last.reason = last.reason or "deadline_exceeded"
                last.detail = rejections
                return None, last

            grant = registry.acquire(key_info, model, now)
            if isinstance(grant, SlotRejection):
                rejections.append(
                    {
                        "key": grant.key_name,
                        "model": grant.model,
                        "reason": grant.reason,
                        "retry_after_ms": grant.retry_after_ms,
                    }
                )
                wait_s = grant.retry_after_ms / 1000.0
                if wait_s <= 0 or now + wait_s >= deadline:
                    break
                await asyncio.sleep(min(wait_s, WAIT_SLICE_S))
                continue

            lease = grant
            attempts += 1
            started = time.time()
            try:
                result = await attempt_fn(key_info, model, lease)
            except asyncio.CancelledError:
                registry.abort(lease)
                raise
            except Exception as exc:  # pragma: no cover - 兜底，正常路径由 attempt_fn 分类
                result = AttemptResult(
                    ok=False,
                    status=500,
                    error=str(exc),
                    outcome="server_error",
                    cost_ms=(time.time() - started) * 1000,
                )

            if result.cost_ms is None:
                result.cost_ms = (time.time() - started) * 1000

            if result.defer_settle:
                return Attempt(lease=lease, key_info=key_info, model=model), result

            registry.settle(
                lease,
                result.outcome,
                usage=result.usage if result.ok else None,
                cost_ms=result.cost_ms,
                retry_after_s=result.retry_after_s,
                cooldown_s=result.cooldown_s,
            )
            _log_timing(
                key_info.get("name", "N/A"),
                model,
                result.cost_ms or 0.0,
                ok=result.ok,
                outcome=result.outcome,
                attempt=attempts,
            )
            last = result
            last.detail = rejections

            if result.ok:
                return Attempt(lease=lease, key_info=key_info, model=model), result
            if result.terminal or spec.fail_fast:
                return None, result
            break  # 换下一个候选：下一个 Key，或数组里的下一个模型

    last.detail = rejections
    if not last.reason:
        blocked = [item for item in rejections if item["reason"]]
        last.reason = blocked[-1]["reason"] if blocked else "no_candidate"
        if blocked:
            last.retry_after_ms = blocked[-1]["retry_after_ms"]
    return None, last


def outcome_from_status(status: int) -> str:
    """把上游状态码映射到结算口径。"""
    if 200 <= status < 300:
        return "ok"
    if status == 429:
        return "rate_limited"
    if 400 <= status < 500:
        return "client_error"
    return "server_error"
