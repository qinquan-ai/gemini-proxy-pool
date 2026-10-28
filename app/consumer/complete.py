"""OpenAI 兼容非流式响应：chat/completions 与 responses 共用。"""

import json
import time

import httpx
from fastapi.responses import JSONResponse

from app.consumer.dispatch import (
    AttemptResult,
    outcome_from_status,
    run_candidates,
    slot_headers,
)
from app.execution.classify import (
    extract_response_parts,
    google_error_message,
    openai_error,
    retry_after_seconds,
    usage_metadata_from_payload,
)
from app.runtime import logger, record_trace


def _upstream_url(model: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _result_from_response(resp: httpx.Response, cost_ms: float) -> AttemptResult:
    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError as exc:
            return AttemptResult(
                ok=False,
                status=502,
                error=f"Malformed upstream JSON: {exc}",
                outcome="server_error",
                cost_ms=cost_ms,
            )
        return AttemptResult(
            ok=True,
            status=200,
            payload=data,
            usage=usage_metadata_from_payload(data),
            outcome="ok",
            cost_ms=cost_ms,
        )
    message = google_error_message(resp)
    status = resp.status_code
    return AttemptResult(
        ok=False,
        status=status,
        error=message,
        outcome=outcome_from_status(status),
        cost_ms=cost_ms,
        retry_after_s=retry_after_seconds(resp) if status == 429 else None,
        terminal=status in (400, 401, 403, 404),
    )


async def complete_with_retry(
    client: httpx.AsyncClient,
    payload: dict,
    spec,
    chat_id: str,
    created_at: int,
    registry=None,
    pool=None,
):
    from app.runtime import key_pool, slot_registry

    registry = registry or slot_registry
    pool = pool or key_pool
    start_time = time.time()

    async def attempt(key_info: dict, model: str, lease) -> AttemptResult:
        t0 = time.time()
        try:
            resp = await client.post(
                _upstream_url(model),
                headers={"x-goog-api-key": key_info["key"]},
                json=payload,
                timeout=httpx.Timeout(spec.timeout_s),
            )
        except httpx.TimeoutException as exc:
            return AttemptResult(
                ok=False,
                status=504,
                error=f"Upstream timeout: {exc}",
                outcome="timeout",
                cost_ms=(time.time() - t0) * 1000,
            )
        except (httpx.HTTPError, OSError) as exc:
            return AttemptResult(
                ok=False,
                status=504,
                error=f"Transport error: {exc}",
                outcome="server_error",
                cost_ms=(time.time() - t0) * 1000,
            )
        return _result_from_response(resp, (time.time() - t0) * 1000)

    winner, result = await run_candidates(spec, registry, pool, attempt)
    headers = slot_headers(
        spec,
        model_used=winner.model if winner else "",
        key_used=winner.key_name if winner else "",
        result=result,
        pool=pool,
    )

    if not result.ok or result.payload is None:
        record_trace(
            "OpenAI Compat",
            "POST",
            "/v1/chat/completions",
            winner.model if winner else spec.model,
            winner.key_name if winner else "N/A",
            result.status or 503,
            (time.time() - start_time) * 1000,
            error=result.error,
            route_mode=spec.label,
        )
        return openai_error(
            result.error or "All candidate keys/models unavailable",
            result.status or 503,
            result.reason or "service_unavailable",
            headers=headers,
        )

    data = result.payload
    text_chunks, tool_calls, finish_reason = extract_response_parts(data)
    usage = result.usage
    record_trace(
        "OpenAI Compat",
        "POST",
        "/v1/chat/completions",
        winner.model,
        winner.key_name,
        200,
        (time.time() - start_time) * 1000,
        tokens=usage,
        route_mode=spec.label,
    )
    logger.info(
        "chat.completion ok model=%s key=%s attempts_used",
        winner.model,
        winner.key_name,
    )

    message = {"role": "assistant", "content": "".join(text_chunks) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return JSONResponse(
        headers=headers,
        content={
            "id": chat_id,
            "object": "chat.completion",
            "created": created_at,
            "model": winner.model,
            "system_fingerprint": winner.key_name,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get(
                    "totalTokenCount",
                    int(usage.get("promptTokenCount") or 0)
                    + int(usage.get("candidatesTokenCount") or 0),
                ),
            },
        },
    )


def chat_completion_error(message: str, status: int = 503, headers: dict | None = None):
    return openai_error(message, status, "service_unavailable", headers=headers or {})


def parse_chat_completion(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))
