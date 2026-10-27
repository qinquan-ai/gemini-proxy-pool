import asyncio
import hmac
import json
import logging
import os
import re
import time
import uuid
import traceback
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel

from app.core.key_manager import KeyManager, CANONICAL_MODELS
from app.core.responses_adapter import (
    chat_completion_to_response,
    response_envelope,
    responses_to_chat_request,
    sse_event,
)
from app.core.thought_signatures import thought_signature_store
from app.core.translator import openai_to_gemini, translate_tools
from app.runtime import HEDGE_INTERVAL_SECONDS, HEDGE_TIMEOUT_SECONDS, MAX_HEDGED_CONCURRENCY, MODE_11_TIMEOUT_SECONDS, key_pool, logger
from app.execution.classify import extract_response_parts, google_error_message, usage_metadata_from_payload
from app.scheduling.policy import build_route_candidates
from app.consumer.settlement import finalize_failed_response

async def stream_with_retry(
    client: httpx.AsyncClient,
    base_url: str,
    payload: dict,
    model: str,
    chat_id: str,
    created_at: int,
    target_key: str | None = None,
    request: Request | None = None,
    route_mode: str = "01",
    custom_fallbacks: list[str] | None = None,
):
    start_time = time.time()
    candidates = build_route_candidates(route_mode, model, target_key, custom_fallbacks)
    if not candidates:
        yield _sse_error(chat_id, created_at, model, "No healthy candidate key/model available")
        yield "data: [DONE]\n\n"
        return

    winning_response: httpx.Response | None = None
    winning_key: dict | None = None
    winning_model: str = model

    if route_mode == "11":
        key_info, cand_model = candidates[0]
        api_key = key_info["key"]
        key_name = key_info["name"]
        winning_model = cand_model
        attempt = 0
        last_error = ""

        while time.time() - start_time < MODE_11_TIMEOUT_SECONDS:
            if request and await request.is_disconnected():
                logger.info("Mode 11 stream: client disconnected.")
                return

            if not key_pool.can_consume_bool(key_info, cand_model, check_concurrency=False):
                last_error = f"Daily quota exhausted for model {cand_model} on key {key_name}"
                break

            acquired = key_pool.acquire_key(model=cand_model, target_key_name=key_name, allow_transient_cooling=True)
            if not acquired:
                await asyncio.sleep(0.2)
                continue

            finalized = False
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{cand_model}:streamGenerateContent?alt=sse"
                req_up = client.build_request("POST", url, headers={"x-goog-api-key": api_key}, json=payload)
                upstream = await client.send(req_up, stream=True)
                if upstream.status_code == 200:
                    winning_response = upstream
                    winning_key = acquired
                    break

                body = await upstream.aread()
                await upstream.aclose()
                msg = google_error_message(upstream, body)
                last_error = msg
                finalize_failed_response(api_key, upstream, msg, model=cand_model)
                finalized = True

                if upstream.status_code in (400, 401, 403, 404):
                    break

                backoff = min(1.0 * (2 ** min(attempt, 4)), 20.0)
                attempt += 1
                await asyncio.sleep(backoff)
            except (httpx.HTTPError, OSError) as exc:
                if not finalized:
                    key_pool.mark_failure(api_key, reason=str(exc), cooldown_seconds=key_pool.transient_cooldown, model=cand_model)
                    finalized = True
                last_error = f"Transport error: {exc}"
                attempt += 1
                await asyncio.sleep(1.0)
            finally:
                if not finalized and winning_response is None:
                    key_pool.release(api_key, model=cand_model)

        if not winning_response:
            yield _sse_error(chat_id, created_at, cand_model, last_error or "Mode 11 stream retry timeout exceeded")
            yield "data: [DONE]\n\n"
            return

    else:
        cand_idx = 0
        in_flight_tasks: dict[asyncio.Task, tuple[dict, str]] = {}
        last_error = ""

        async def _open_stream_attempt(k_info: dict, m_name: str) -> tuple[int, httpx.Response | None, str]:
            a_key = k_info["key"]
            finalized_inner = False
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:streamGenerateContent?alt=sse"
                req_up = client.build_request("POST", url, headers={"x-goog-api-key": a_key}, json=payload)
                up = await client.send(req_up, stream=True)
                if up.status_code == 200:
                    return 200, up, ""
                b = await up.aread()
                await up.aclose()
                msg = google_error_message(up, b)
                finalize_failed_response(a_key, up, msg, model=m_name)
                finalized_inner = True
                return up.status_code, None, msg
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                if not finalized_inner:
                    key_pool.mark_failure(a_key, reason=str(ex), cooldown_seconds=key_pool.transient_cooldown, model=m_name)
                    finalized_inner = True
                return 500, None, str(ex)
            finally:
                if not finalized_inner:
                    pass

        try:
            while time.time() - start_time < HEDGE_TIMEOUT_SECONDS:
                if request and await request.is_disconnected():
                    for t in in_flight_tasks:
                        t.cancel()
                    return

                while len(in_flight_tasks) < MAX_HEDGED_CONCURRENCY and cand_idx < len(candidates):
                    k_cand, m_cand = candidates[cand_idx]
                    cand_idx += 1
                    if not key_pool.can_consume_bool(k_cand, m_cand, check_concurrency=True):
                        continue
                    acq = key_pool.acquire_key(model=m_cand, target_key_name=k_cand["name"], allow_transient_cooling=True)
                    if acq:
                        t = asyncio.create_task(_open_stream_attempt(acq, m_cand))
                        in_flight_tasks[t] = (acq, m_cand)
                        break

                if not in_flight_tasks:
                    if cand_idx >= len(candidates):
                        if time.time() - start_time < HEDGE_TIMEOUT_SECONDS - 5.0:
                            await asyncio.sleep(1.0)
                            cand_idx = 0
                            continue
                        break
                    await asyncio.sleep(0.1)
                    continue

                can_hedge_more = (len(in_flight_tasks) < MAX_HEDGED_CONCURRENCY and cand_idx < len(candidates))
                wait_step = HEDGE_INTERVAL_SECONDS if can_hedge_more else max(0.5, HEDGE_TIMEOUT_SECONDS - (time.time() - start_time))

                done, _ = await asyncio.wait(
                    in_flight_tasks.keys(),
                    timeout=wait_step,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for f_task in done:
                    f_key, f_model = in_flight_tasks.pop(f_task)
                    try:
                        code, up_stream, err = f_task.result()
                        if code == 200 and up_stream:
                            winning_response = up_stream
                            winning_key = f_key
                            winning_model = f_model
                            for other in in_flight_tasks:
                                other.cancel()
                            break
                        else:
                            last_error = err
                            key_pool.release(f_key["key"], model=f_model)
                    except asyncio.CancelledError:
                        key_pool.release(f_key["key"], model=f_model)
                    except Exception as e:
                        last_error = str(e)
                        key_pool.release(f_key["key"], model=f_model)

                if winning_response:
                    break
        finally:
            for t in list(in_flight_tasks.keys()):
                t.cancel()
                k_t, m_t = in_flight_tasks.pop(t)
                key_pool.release(k_t["key"], model=m_t)

        if not winning_response:
            yield _sse_error(chat_id, created_at, model, last_error or "All candidate stream connections failed")
            yield "data: [DONE]\n\n"
            return

    w_api_key = winning_key["key"]
    stream_finalized = False
    usage = {}
    try:
        async for line in winning_response.aiter_lines():
            if request and await request.is_disconnected():
                logger.info("Client disconnected during stream. Cancelling upstream.")
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
                    winning_model,
                    {"content": "".join(text_chunks)},
                )
            if tool_calls:
                yield _sse_chunk(
                    chat_id,
                    created_at,
                    winning_model,
                    {"tool_calls": tool_calls},
                    "tool_calls",
                )

        key_pool.mark_success(w_api_key, usage, model=winning_model)
        stream_finalized = True
        yield _sse_chunk(chat_id, created_at, winning_model, {}, "stop")
        yield "data: [DONE]\n\n"
    except Exception as exc:
        if not stream_finalized:
            key_pool.mark_failure(w_api_key, reason=f"Streaming transport error: {exc}", cooldown_seconds=key_pool.transient_cooldown, model=winning_model)
            stream_finalized = True
        logger.warning("Streaming error for %s: %s", winning_key["name"], exc)
    finally:
        await winning_response.aclose()
        if not stream_finalized:
            key_pool.release(w_api_key, model=winning_model)


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
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ],
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
