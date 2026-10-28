"""HTTP 参数解析与候选构造。

旧的 4 个 ``x-route-mode`` 值（11/10/01/00）实际是两条轴的组合，P1 直接暴露这两条轴：

    X-Lock-Key: true|false              true = 只用一个 Key
    X-Allow-Model-Switch: true|false    true = 沿 X-Fallback-Models 换模型

候选严格按调用方给的数组顺序 first-fit：不排序、不打分、不回头。
选 Key 仍按 in_flight → used_today 排序，因为 Key 之间才真正等价，适合摊负载。
"""

import os
import time
from dataclasses import dataclass, field
from urllib.parse import unquote

from fastapi import HTTPException, Request

from app.runtime import DEFAULT_MODEL, MODEL_PATTERN

RETRY_POLICIES = ("family", "fail-fast")


class DeadlineBelowFloor(Exception):
    """调用方报的预算低于该档下限。应在准入层直接拒，不占槽不记账。"""

    def __init__(self, model: str, floor_s: float, requested_s: float):
        super().__init__(
            f"timeout {requested_s:.1f}s below floor {floor_s:.1f}s for model {model}"
        )
        self.model = model
        self.floor_s = floor_s
        self.requested_s = requested_s


@dataclass
class RouteSpec:
    """一次调用的路由契约，由请求头解析而来，后续所有层只读它。"""

    model: str
    models: list[str]
    lock_key: bool
    allow_model_switch: bool
    target_key: str | None
    timeout_s: float
    retry_policy: str
    is_stream: bool
    batch_id: str = ""
    timeout_note: str = ""
    received_at: float = field(default_factory=time.time)

    @property
    def fail_fast(self) -> bool:
        return self.retry_policy == "fail-fast"

    @property
    def label(self) -> str:
        if self.fail_fast:
            return "fail-fast"
        if self.lock_key and self.allow_model_switch:
            return "lock-key+switch"
        if self.lock_key:
            return "lock-key"
        if self.allow_model_switch:
            return "family"
        return "single"

    @property
    def max_attempts(self) -> int:
        """网关内最多尝试几个候选。

        下限取「每个模型两次」，否则 3 个 Key 全 5xx 时会把预算耗在同一个模型上，
        数组后面的模型根本没机会。fail-fast 只发一次。
        """
        if self.fail_fast:
            return 1
        try:
            configured = int(os.getenv("RETRY_MAX_PER_REQUEST", "3"))
        except ValueError:
            configured = 3
        return max(1, configured, len(self.models) * 2)

    def deadline(self) -> float:
        return self.received_at + self.timeout_s


def extract_target_key(request: Request) -> str | None:
    raw = request.headers.get("x-target-key") or request.query_params.get("target_key")
    if not raw:
        return None
    try:
        return unquote(raw).strip()
    except Exception:
        return raw.strip()


def extract_fallback_models(request: Request, body_dict: dict | None = None) -> list[str]:
    raw = request.headers.get("x-fallback-models") or request.query_params.get(
        "fallback_models"
    )
    if not raw and body_dict and isinstance(body_dict, dict):
        raw = body_dict.get("fallback_models")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(m).strip() for m in raw if str(m).strip()]
    return [m.strip() for m in str(raw).split(",") if m.strip()]


def _extract_bool(
    request: Request, header: str, body_dict: dict | None, body_key: str
) -> bool | None:
    raw = request.headers.get(header)
    if raw is None:
        raw = request.query_params.get(body_key)
    if raw is None and body_dict and isinstance(body_dict, dict):
        raw = body_dict.get(body_key)
    if raw is None:
        return None
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _extract_retry_policy(request: Request, body_dict: dict | None) -> str:
    raw = (
        request.headers.get("x-retry-policy")
        or request.query_params.get("retry_policy")
        or (body_dict or {}).get("retry_policy")
        or "family"
    )
    value = str(raw).strip().lower().replace("_", "-")
    return value if value in RETRY_POLICIES else "family"


def _extract_timeout_ms(request: Request, body_dict: dict | None) -> float | None:
    raw = (
        request.headers.get("x-request-timeout-ms")
        or request.query_params.get("request_timeout_ms")
        or (body_dict or {}).get("request_timeout_ms")
    )
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value / 1000.0 if value > 0 else None


def parse_route_spec(
    request: Request,
    body_dict: dict | None,
    *,
    model: str,
    is_stream: bool,
    timeouts_for,
) -> RouteSpec:
    """解析并裁决路由契约。超时低于该档下限时抛 DeadlineBelowFloor。"""
    fallbacks = extract_fallback_models(request, body_dict)
    allow_switch = _extract_bool(request, "x-allow-model-switch", body_dict, "allow_model_switch")
    if allow_switch is None:
        allow_switch = bool(fallbacks)
    lock_key = _extract_bool(request, "x-lock-key", body_dict, "lock_key") or False

    target_key = extract_target_key(request)
    if lock_key and not target_key:
        lock_key = True  # 由候选层挑第一个可用 Key

    profile = timeouts_for(model)
    requested = _extract_timeout_ms(request, body_dict)
    effective, note = profile.clamp(requested)
    if effective is None:
        raise DeadlineBelowFloor(model, profile.floor_s, requested or 0.0)

    models: list[str] = []
    for item in fallbacks:
        normalized = normalize_model(item, strict=False)
        if normalized and normalized not in models:
            models.append(normalized)
    if model and model not in models:
        models.insert(0, model)

    return RouteSpec(
        model=model,
        models=models,
        lock_key=bool(lock_key),
        allow_model_switch=bool(allow_switch),
        target_key=target_key,
        timeout_s=effective,
        retry_policy=_extract_retry_policy(request, body_dict),
        is_stream=is_stream,
        batch_id=(request.headers.get("x-batch-id") or "").strip()[:64],
        timeout_note=note or "",
    )


def build_candidates(spec: RouteSpec, pool) -> list[tuple[dict, str]]:
    """按 first-fit 顺序拼候选。

    lock_key=false: 每个模型配所有可用 Key，Key 内按负载排序。
    lock_key=true : 只用一个 Key，沿数组换模型。
    """
    models = list(spec.models) if spec.allow_model_switch else [spec.model]
    if not models:
        models = [spec.model]

    candidates: list[tuple[dict, str]] = []
    locked_key = None
    if spec.lock_key:
        locked_key = _pick_locked_key(spec, pool, models[0])

    for model in models:
        if spec.lock_key:
            if locked_key is not None and _usable(locked_key, model, pool):
                candidates.append((locked_key, model))
            continue
        keys = [k for k in pool.keys if not k.get("disabled") and _usable(k, model, pool)]
        keys.sort(
            key=lambda k: (
                (k.get("model_in_flight") or {}).get(model, 0),
                pool._get_model_quota(k, model).get("used_today", 0),
            )
        )
        for key in keys:
            candidates.append((key, model))
    return candidates


def _usable(key_info: dict, model: str, pool) -> bool:
    ok, _ = pool.can_consume(key_info, model, check_concurrency=False)
    return ok


def _pick_locked_key(spec: RouteSpec, pool, model: str) -> dict | None:
    if spec.target_key:
        return pool.find_key_by_name(spec.target_key)
    ranked = [k for k in pool.keys if not k.get("disabled") and _usable(k, model, pool)]
    ranked.sort(
        key=lambda k: (
            (k.get("model_in_flight") or {}).get(model, 0),
            pool._get_model_quota(k, model).get("used_today", 0),
        )
    )
    return ranked[0] if ranked else None


def normalize_model(requested_model: str | None, strict: bool = True) -> str:
    raw_model = requested_model or DEFAULT_MODEL
    segments = raw_model.split("/")
    if (
        len(segments) > 2
        or any(not segment for segment in segments)
        or any(not MODEL_PATTERN.fullmatch(segment) for segment in segments)
    ):
        if strict:
            raise HTTPException(status_code=400, detail="Invalid model identifier")
        return ""
    return segments[-1]
