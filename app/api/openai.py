"""OpenAI 兼容入口：/v1/chat/completions 与 /v1/responses。"""

import json
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.request_tag import request_tag

from app.consumer.complete import complete_with_retry
from app.consumer.dispatch import slot_headers
from app.consumer.responses_stream import responses_stream_with_retry
from app.core.responses_adapter import (
    chat_completion_to_response,
    responses_to_chat_request,
)
from app.execution.classify import openai_error
from app.execution.streaming import stream_with_retry
from app.producer.request_spec import build_gemini_payload
from app.runtime import (
    app,
    get_http_client,
    key_pool,
    logger,
    require_proxy_auth,
    slot_registry,
)
from app.scheduling.policy import DeadlineBelowFloor, normalize_model, parse_route_spec


def _deadline_error(request: Request, model: str, exc: DeadlineBelowFloor):
    headers = {
        "X-Slot-Reason": "deadline_below_floor",
        "X-Timeout-Floor-Ms": str(int(exc.floor_s * 1000)),
    }
    return openai_error(
        f"X-Request-Timeout-Ms={exc.requested_s * 1000:.0f} is below the floor of "
        f"{exc.floor_s * 1000:.0f} ms for {model}. Raise the timeout or use X-Retry-Policy: fail-fast.",
        400,
        "invalid_request_error",
        headers=headers,
        extra={
            "reason": "deadline_below_floor",
            "floor_ms": int(exc.floor_s * 1000),
            "requested_ms": int(exc.requested_s * 1000),
        },
    )


def _route(request: Request, body: dict, model: str, is_stream: bool):
    return parse_route_spec(
        request,
        body,
        model=model,
        is_stream=is_stream,
        timeouts_for=slot_registry.timeouts_for,
    )


def _stream_headers(spec, model: str) -> dict[str, str]:
    base = slot_headers(spec, model_used=model, pool=key_pool)
    base["Cache-Control"] = "no-cache"
    base["X-Accel-Buffering"] = "no"
    return base


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    require_proxy_auth(request)
    # 只用于耗时对账（logs/timing/*.jsonl 的 tag 列），不参与调度
    request_tag.set(request.headers.get("x-request-tag", ""))
    openai_request = await request.json()
    model = normalize_model(openai_request.get("model"))
    payload = build_gemini_payload(openai_request)
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_at = int(time.time())
    is_stream = bool(openai_request.get("stream", False))

    try:
        spec = _route(request, openai_request, model, is_stream)
    except DeadlineBelowFloor as exc:
        return _deadline_error(request, model, exc)

    logger.info(
        "Request model=%s route=%s timeout=%.0fs models=%s messages=%d tools=%d stream=%s batch=%s",
        model,
        spec.label,
        spec.timeout_s,
        ",".join(spec.models),
        len(openai_request.get("messages", [])),
        len(openai_request.get("tools", [])),
        is_stream,
        spec.batch_id or "-",
    )

    client = get_http_client(request)

    if is_stream:
        return StreamingResponse(
            stream_with_retry(client, payload, spec, chat_id, created_at),
            media_type="text/event-stream",
            headers=_stream_headers(spec, model),
        )

    return await complete_with_retry(client, payload, spec, chat_id, created_at)


@app.post("/v1/responses")
async def responses(request: Request):
    require_proxy_auth(request)
    response_request = await request.json()
    if response_request.get("store"):
        return openai_error(
            "store=true is not supported by this stateless gateway",
            400,
            "invalid_request_error",
        )
    if response_request.get("previous_response_id"):
        return openai_error(
            "previous_response_id is not supported; send the full input history",
            400,
            "invalid_request_error",
        )
    unsupported_tools = sorted(
        {
            str(tool.get("type", "unknown"))
            for tool in response_request.get("tools", [])
            if not isinstance(tool, dict) or tool.get("type") != "function"
        }
    )
    if unsupported_tools:
        return openai_error(
            "Unsupported Responses tool types: " + ", ".join(unsupported_tools),
            400,
            "invalid_request_error",
        )

    chat_request = responses_to_chat_request(response_request)
    model = normalize_model(chat_request.get("model"))
    payload = build_gemini_payload(chat_request)
    response_id = f"resp_{uuid.uuid4().hex[:24]}"
    created_at = int(time.time())
    is_stream = bool(response_request.get("stream", False))

    try:
        spec = _route(request, response_request, model, is_stream)
    except DeadlineBelowFloor as exc:
        return _deadline_error(request, model, exc)

    client = get_http_client(request)
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if is_stream:
        return StreamingResponse(
            responses_stream_with_retry(
                client,
                payload,
                spec,
                response_id,
                created_at,
                response_request,
                chat_id,
            ),
            media_type="text/event-stream",
            headers=_stream_headers(spec, model),
        )

    chat_response = await complete_with_retry(
        client, payload, spec, chat_id, created_at
    )
    if chat_response.status_code >= 400:
        return chat_response
    chat_completion = json.loads(chat_response.body.decode("utf-8"))
    result = JSONResponse(
        content=chat_completion_to_response(
            chat_completion, response_request, response_id, created_at
        )
    )
    for name, value in slot_headers(spec, model_used=chat_completion.get("model", model), pool=key_pool).items():
        result.headers[name] = value
    return result
