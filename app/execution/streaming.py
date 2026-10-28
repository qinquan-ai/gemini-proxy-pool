"""OpenAI 兼容流式响应（chat/completions stream=true）。"""

import asyncio
import json
import time

import httpx

from app.consumer.dispatch import AttemptResult, outcome_from_status, run_candidates
from app.execution.classify import (
    extract_response_parts,
    google_error_message,
    retry_after_seconds,
    usage_metadata_from_payload,
)
from app.runtime import key_pool, logger, slot_registry

STREAM_READ_GAP_S = 60.0


def _stream_url(model: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:streamGenerateContent?alt=sse"
    )


async def stream_with_retry(
    client: httpx.AsyncClient,
    payload: dict,
    spec,
    chat_id: str,
    created_at: int,
    registry=None,
    pool=None,
):
    registry = registry or slot_registry
    pool = pool or key_pool
    start_time = time.time()
    ttfb = registry.timeouts_for(spec.model).ttfb_s or spec.timeout_s

    async def attempt(key_info: dict, model: str, lease) -> AttemptResult:
        req_up = client.build_request(
            "POST",
            _stream_url(model),
            headers={"x-goog-api-key": key_info["key"]},
            json=payload,
        )
        t0 = time.time()
        try:
            upstream = await asyncio.wait_for(
                client.send(req_up, stream=True),
                timeout=min(ttfb, spec.timeout_s),
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
            return AttemptResult(
                ok=True,
                status=200,
                payload=upstream,
                outcome="ok",
                defer_settle=True,
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
            cost_ms=(time.time() - t0) * 1000,
            retry_after_s=retry_after_seconds(upstream) if status == 429 else None,
            terminal=status in (400, 401, 403, 404),
        )

    winner, result = await run_candidates(spec, registry, pool, attempt)
    if not result.ok or result.payload is None:
        yield _sse_error(
            chat_id,
            created_at,
            winner.model if winner else spec.model,
            result.error or "All candidate stream connections failed",
        )
        yield "data: [DONE]\n\n"
        return

    upstream: httpx.Response = result.payload
    deadline = spec.deadline()
    usage: dict = {}
    outcome = "timeout"
    try:
        iterator = upstream.aiter_lines()
        while True:
            if time.time() > deadline:
                break
            try:
                line = await asyncio.wait_for(
                    iterator.__anext__(), timeout=STREAM_READ_GAP_S
                )
            except StopAsyncIteration:
                outcome = "ok"
                break
            except asyncio.TimeoutError:
                break
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except ValueError:
                continue
            chunk_usage = usage_metadata_from_payload(data)
            if chunk_usage:
                usage = chunk_usage
            text_chunks, tool_calls, _ = extract_response_parts(data)
            if text_chunks:
                yield _sse_chunk(
                    chat_id,
                    created_at,
                    winner.model,
                    {"content": "".join(text_chunks)},
                )
            if tool_calls:
                yield _sse_chunk(
                    chat_id,
                    created_at,
                    winner.model,
                    {"tool_calls": tool_calls},
                    "tool_calls",
                )
    finally:
        await upstream.aclose()
        registry.settle(
            winner.lease,
            outcome,
            usage=usage if outcome == "ok" else None,
            cost_ms=(time.time() - start_time) * 1000,
        )
        if outcome != "ok":
            logger.warning(
                "Stream for %s/%s ended before completion",
                winner.key_name,
                winner.model,
            )

    if outcome == "ok":
        yield _sse_chunk(chat_id, created_at, winner.model, {}, "stop")
    yield "data: [DONE]\n\n"


def _sse_chunk(
    chat_id: str,
    created_at: int,
    model: str,
    delta: dict,
    finish_reason: str | None = None,
) -> str:
    payload = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created_at,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_error(chat_id: str, created_at: int, model: str, message: str) -> str:
    return _sse_chunk(
        chat_id,
        created_at,
        model,
        {"content": f"\n[Gateway Error: {message}]\n"},
        "stop",
    )
