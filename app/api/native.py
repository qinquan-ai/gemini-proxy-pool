"""Google 原生协议入口：/v1beta/{path}。

非流式与流式共用一条候选推进循环，区别只在读取响应体的方式：
流式成功时租约延到流结束，由 ``_stream_response`` 负责最终结算。
"""

import asyncio
import time

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from app.consumer.dispatch import (
    AttemptResult,
    outcome_from_status,
    run_candidates,
    slot_headers,
)
from app.execution.classify import (
    google_error_message,
    is_allowed_native_gemini_path,
    native_gemini_headers,
    native_google_error,
    retry_after_seconds,
    usage_metadata_from_payload,
    usage_metadata_from_sse_tail,
)
from app.runtime import (
    DEFAULT_MODEL,
    app,
    get_http_client,
    key_pool,
    record_trace,
    slot_registry,
)
from app.scheduling.policy import DeadlineBelowFloor, parse_route_spec

# 流式读取的单块最长间隔。首包之后不再用 TTFB 卡，避免误杀长思考。
STREAM_READ_GAP_S = 60.0


def _extract_model(google_path: str) -> str | None:
    if "models/" not in google_path:
        return None
    part = google_path.split("models/", 1)[1]
    return part.split(":", 1)[0].split("/", 1)[0]


def _target_url(google_path: str, requested_model: str | None, cand_model: str) -> str:
    if requested_model and requested_model in google_path:
        path = google_path.replace(f"models/{requested_model}", f"models/{cand_model}")
    else:
        path = google_path
    return f"https://generativelanguage.googleapis.com/v1beta/{path}"


@app.api_route(
    "/v1beta/{google_path:path}", methods=["GET", "POST", "DELETE", "PATCH"]
)
async def native_gemini_gateway(google_path: str, request: Request):
    if not is_allowed_native_gemini_path(google_path):
        return native_google_error("Unsupported native Gemini endpoint", 404)

    body = await request.body()
    is_stream = "streamGenerateContent" in google_path
    is_metered = "countTokens" not in google_path
    requested_model = _extract_model(google_path)
    model_display = requested_model or DEFAULT_MODEL
    client = get_http_client(request)
    start_time = time.time()

    try:
        spec = parse_route_spec(
            request,
            None,
            model=model_display,
            is_stream=is_stream,
            timeouts_for=slot_registry.timeouts_for,
        )
    except DeadlineBelowFloor as exc:
        return native_google_error(
            f"X-Request-Timeout-Ms={exc.requested_s * 1000:.0f} is below the {exc.model} floor "
            f"of {exc.floor_s * 1000:.0f} ms. Raise the timeout or switch to fail-fast.",
            400,
            headers={"X-Slot-Reason": "deadline_below_floor"},
            extra={"reason": "deadline_below_floor", "floor_ms": int(exc.floor_s * 1000)},
        )

    if not is_metered:
        return await _count_tokens(client, request, google_path, body, model_display, spec)

    ttfb = slot_registry.timeouts_for(spec.model).ttfb_s or spec.timeout_s

    async def attempt(key_info: dict, model: str, lease) -> AttemptResult:
        target = _target_url(google_path, requested_model, model)
        upstream_req = client.build_request(
            request.method,
            target,
            params=list(request.query_params.multi_items()),
            headers=native_gemini_headers(request, key_info["key"]),
            content=body,
        )
        t0 = time.time()
        try:
            if is_stream:
                upstream = await asyncio.wait_for(
                    client.send(upstream_req, stream=True),
                    timeout=min(ttfb, spec.timeout_s),
                )
            else:
                upstream = await client.send(
                    upstream_req, stream=False, timeout=httpx.Timeout(spec.timeout_s)
                )
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
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

        if upstream.status_code == 200:
            if is_stream:
                return AttemptResult(
                    ok=True,
                    status=200,
                    payload=upstream,
                    outcome="ok",
                    defer_settle=True,
                    cost_ms=(time.time() - t0) * 1000,
                )
            try:
                usage = usage_metadata_from_payload(upstream.json())
            except ValueError:
                usage = {}
            return AttemptResult(
                ok=True,
                status=200,
                payload=upstream,
                usage=usage,
                outcome="ok",
                cost_ms=(time.time() - t0) * 1000,
            )

        err_body = await upstream.aread()
        await upstream.aclose()
        message = google_error_message(upstream, err_body)
        status = upstream.status_code
        return AttemptResult(
            ok=False,
            status=status,
            error=message,
            outcome=outcome_from_status(status),
            payload=err_body,
            cost_ms=(time.time() - t0) * 1000,
            retry_after_s=retry_after_seconds(upstream) if status == 429 else None,
            terminal=status in (400, 401, 403, 404),
        )

    winner, result = await run_candidates(spec, slot_registry, key_pool, attempt)
    headers = slot_headers(
        spec,
        model_used=winner.model if winner else "",
        key_used=winner.key_name if winner else "",
        result=result,
        pool=key_pool,
    )

    if not result.ok:
        record_trace(
            "Google Native",
            request.method,
            f"/v1beta/{google_path}",
            winner.model if winner else spec.model,
            winner.key_name if winner else "N/A",
            result.status or 503,
            (time.time() - start_time) * 1000,
            error=result.error,
            route_mode=spec.label,
        )
        return native_google_error(
            result.error or "All candidate keys/models unavailable",
            result.status or 503,
            headers=headers,
            extra={"reason": result.reason} if result.reason else None,
        )

    if is_stream:
        return _stream_response(
            upstream=result.payload,
            request=request,
            lease=winner.lease,
            attempt=winner,
            spec=spec,
            headers=headers,
            start_time=start_time,
            google_path=google_path,
        )

    upstream: httpx.Response = result.payload
    record_trace(
        "Google Native",
        request.method,
        f"/v1beta/{google_path}",
        winner.model,
        winner.key_name,
        200,
        (time.time() - start_time) * 1000,
        tokens=result.usage,
        route_mode=spec.label,
    )
    return Response(
        content=upstream.content,
        status_code=200,
        media_type=upstream.headers.get("content-type"),
        headers=headers,
    )


def _stream_response(
    *,
    upstream: httpx.Response,
    request: Request,
    lease,
    attempt,
    spec,
    headers: dict,
    start_time: float,
    google_path: str,
) -> StreamingResponse:
    deadline = spec.deadline()

    async def body_stream():
        usage_tail = b""
        outcome = "timeout"
        try:
            iterator = upstream.aiter_raw()
            while True:
                if time.time() > deadline:
                    break
                try:
                    chunk = await asyncio.wait_for(
                        iterator.__anext__(), timeout=STREAM_READ_GAP_S
                    )
                except StopAsyncIteration:
                    outcome = "ok"
                    break
                except asyncio.TimeoutError:
                    break
                usage_tail = (usage_tail + chunk)[-262_144:]
                yield chunk
        finally:
            await upstream.aclose()
            usage = usage_metadata_from_sse_tail(usage_tail) if outcome == "ok" else None
            slot_registry.settle(
                lease,
                outcome,
                usage=usage,
                cost_ms=(time.time() - start_time) * 1000,
            )
            record_trace(
                "Google Native",
                request.method,
                f"/v1beta/{google_path}",
                attempt.model,
                attempt.key_name,
                200 if outcome == "ok" else 504,
                (time.time() - start_time) * 1000,
                tokens=usage or {},
                route_mode=spec.label,
                error="" if outcome == "ok" else "stream ended before completion",
            )

    return StreamingResponse(
        body_stream(),
        status_code=200,
        media_type=upstream.headers.get("content-type"),
        headers=headers,
    )


async def _count_tokens(
    client: httpx.AsyncClient,
    request: Request,
    google_path: str,
    body: bytes,
    model: str,
    spec,
):
    """countTokens 不生成内容，不占令牌、不记 RPD，直发第一个可用 Key。"""
    key_info = next((k for k in key_pool.keys if not k.get("disabled")), None)
    if key_info is None:
        return native_google_error("No healthy API key available", 503)
    target = _target_url(google_path, model, model)
    upstream_req = client.build_request(
        request.method,
        target,
        params=list(request.query_params.multi_items()),
        headers=native_gemini_headers(request, key_info["key"]),
        content=body,
    )
    try:
        upstream = await client.send(
            upstream_req, stream=False, timeout=httpx.Timeout(spec.timeout_s)
        )
    except (httpx.HTTPError, OSError) as exc:
        return native_google_error(f"Transport error: {exc}", 504)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=slot_headers(spec, model_used=model, pool=key_pool),
    )
