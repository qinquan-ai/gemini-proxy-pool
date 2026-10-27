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
from app.runtime import HEDGE_INTERVAL_SECONDS, HEDGE_TIMEOUT_SECONDS, MAX_HEDGED_CONCURRENCY, MODE_11_TIMEOUT_SECONDS, key_pool, logger, record_trace
from app.execution.classify import extract_response_parts, google_error_message, openai_error, usage_metadata_from_payload
from app.scheduling.policy import build_route_candidates
from app.consumer.settlement import finalize_failed_response

async def complete_with_retry(
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
        return openai_error(
            "No available candidate key/model matching routing policy",
            503,
            "service_unavailable",
        )

    # 1. Mode 11: Single Point Persistent Retry up to MODE_11_TIMEOUT_SECONDS
    if route_mode == "11":
        fail_fast = (
            (request is not None and request.headers.get("x-retry-policy", "").lower() == "fail-fast")
            or (request is not None and request.headers.get("x-client-mode", "").lower() == "pipeline")
            or (request is not None and request.query_params.get("fail_fast", "").lower() in ("1", "true"))
        )
        key_info, cand_model = candidates[0]
        api_key = key_info["key"]
        key_name = key_info["name"]
        attempt = 0
        last_error_message = ""
        last_status_code = 503

        while time.time() - start_time < MODE_11_TIMEOUT_SECONDS:
            if request and await request.is_disconnected():
                logger.info("Mode 11: client disconnected. Aborting request.")
                record_trace(
                    "OpenAI Compat",
                    "POST",
                    "/v1/chat/completions",
                    cand_model,
                    key_name,
                    499,
                    (time.time() - start_time) * 1000,
                    error="Client disconnected",
                )
                return openai_error("Client cancelled request", 499, "client_closed_request")

            if not key_pool.can_consume_bool(key_info, cand_model, check_concurrency=False):
                last_error_message = f"Daily quota exhausted for model {cand_model} on key {key_name}"
                last_status_code = 429
                if fail_fast:
                    return openai_error(last_error_message, 429, "quota_exhausted")
                break

            acquired = key_pool.acquire_key(
                model=cand_model,
                target_key_name=key_name,
                allow_transient_cooling=True,
            )
            if not acquired:
                if fail_fast:
                    return openai_error(f"Key {key_name} currently in cooldown or concurrency limit", 429, "key_busy")
                await asyncio.sleep(0.2)
                continue

            finalized = False
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{cand_model}:generateContent"
                response = await client.post(
                    url,
                    headers={"x-goog-api-key": api_key},
                    json=payload,
                )
                latency_ms = (time.time() - start_time) * 1000
                if response.status_code == 200:
                    data = response.json()
                    text_chunks, tool_calls, finish_reason = extract_response_parts(data)
                    usage = usage_metadata_from_payload(data)
                    key_pool.mark_success(api_key, usage, model=cand_model)
                    finalized = True
                    record_trace(
                        "OpenAI Compat",
                        "POST",
                        "/v1/chat/completions",
                        cand_model,
                        key_name,
                        200,
                        latency_ms,
                        tokens=usage,
                    )
                    message = {"role": "assistant", "content": "".join(text_chunks) or None}
                    if tool_calls:
                        message["tool_calls"] = tool_calls
                    return JSONResponse(
                        content={
                            "id": chat_id,
                            "object": "chat.completion",
                            "created": created_at,
                            "model": cand_model,
                            "system_fingerprint": key_name,
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
                                "total_tokens": usage.get("totalTokenCount", 0),
                            },
                        }
                    )

                msg = google_error_message(response)
                last_error_message = msg
                last_status_code = response.status_code
                finalize_failed_response(api_key, response, msg, model=cand_model)
                finalized = True
                record_trace(
                    "OpenAI Compat",
                    "POST",
                    "/v1/chat/completions",
                    cand_model,
                    key_name,
                    response.status_code,
                    latency_ms,
                    error=msg,
                )

                if response.status_code in (400, 401, 403, 404):
                    return openai_error(msg, response.status_code, "invalid_request_error")

                if fail_fast:
                    return openai_error(msg, response.status_code, "upstream_error")

                backoff = min(1.0 * (2 ** min(attempt, 4)), 20.0)
                remaining = max(0.0, MODE_11_TIMEOUT_SECONDS - (time.time() - start_time))
                logger.warning(
                    "Mode 11 [%s/%s] attempt %d failed (%d). Backing off %.1fs (%.1fs remaining)...",
                    key_name,
                    cand_model,
                    attempt + 1,
                    response.status_code,
                    backoff,
                    remaining,
                )
                attempt += 1
                await asyncio.sleep(backoff)
            except (httpx.HTTPError, OSError) as exc:
                latency_ms = (time.time() - start_time) * 1000
                if not finalized:
                    key_pool.mark_failure(
                        api_key,
                        reason=str(exc),
                        cooldown_seconds=key_pool.transient_cooldown,
                        model=cand_model,
                    )
                    finalized = True
                last_error_message = f"Transport error: {exc}"
                last_status_code = 504
                record_trace(
                    "OpenAI Compat",
                    "POST",
                    "/v1/chat/completions",
                    cand_model,
                    key_name,
                    504,
                    latency_ms,
                    error=str(exc),
                )
                if fail_fast:
                    return openai_error(f"Transport error: {exc}", 504, "transport_error")
                attempt += 1
                await asyncio.sleep(1.0)
            finally:
                if not finalized:
                    key_pool.release(api_key, model=cand_model)

        total_ms = (time.time() - start_time) * 1000
        return openai_error(
            last_error_message or "Mode 11 retry timeout exceeded",
            last_status_code,
            "service_unavailable",
        )

    # 2. Modes 10, 01, 00: Hedged Concurrency Racing
    cand_idx = 0
    in_flight_tasks: dict[asyncio.Task, tuple[dict, str]] = {}
    last_error_msg = ""
    last_status = 503

    async def _execute_attempt(k_info: dict, m_name: str) -> tuple[int, dict | None, str, dict]:
        a_key = k_info["key"]
        k_name = k_info["name"]
        finalized_inner = False
        try:
            u_url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent"
            t0 = time.time()
            resp = await client.post(
                u_url,
                headers={"x-goog-api-key": a_key},
                json=payload,
            )
            lat_ms = (time.time() - t0) * 1000
            if resp.status_code == 200:
                d = resp.json()
                usg = usage_metadata_from_payload(d)
                key_pool.mark_success(a_key, usg, model=m_name)
                finalized_inner = True
                record_trace(
                    "OpenAI Compat",
                    "POST",
                    "/v1/chat/completions",
                    m_name,
                    k_name,
                    200,
                    lat_ms,
                    tokens=usg,
                    route_mode=route_mode,
                )
                return 200, d, "", usg
            err_text = google_error_message(resp)
            finalize_failed_response(a_key, resp, err_text, model=m_name)
            finalized_inner = True
            record_trace(
                "OpenAI Compat",
                "POST",
                "/v1/chat/completions",
                m_name,
                k_name,
                resp.status_code,
                lat_ms,
                route_mode=route_mode,
                error=err_text,
            )
            return resp.status_code, None, err_text, {}
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            if not finalized_inner:
                key_pool.mark_failure(
                    a_key,
                    reason=str(ex),
                    cooldown_seconds=key_pool.transient_cooldown,
                    model=m_name,
                )
                finalized_inner = True
            return 500, None, str(ex), {}
        finally:
            if not finalized_inner:
                key_pool.release(a_key, model=m_name)

    try:
        while time.time() - start_time < HEDGE_TIMEOUT_SECONDS:
            if request and await request.is_disconnected():
                for t in in_flight_tasks:
                    t.cancel()
                record_trace(
                    "OpenAI Compat",
                    "POST",
                    "/v1/chat/completions",
                    model,
                    "N/A",
                    499,
                    (time.time() - start_time) * 1000,
                    error="Client disconnected",
                )
                return openai_error("Client cancelled request", 499, "client_closed_request")

            while len(in_flight_tasks) < MAX_HEDGED_CONCURRENCY and cand_idx < len(candidates):
                k_candidate, m_candidate = candidates[cand_idx]
                cand_idx += 1
                if not key_pool.can_consume_bool(k_candidate, m_candidate, check_concurrency=True):
                    continue
                acq = key_pool.acquire_key(
                    model=m_candidate,
                    target_key_name=k_candidate["name"],
                    allow_transient_cooling=True,
                )
                if acq:
                    task = asyncio.create_task(_execute_attempt(acq, m_candidate))
                    in_flight_tasks[task] = (acq, m_candidate)
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

            can_hedge_more = (
                len(in_flight_tasks) < MAX_HEDGED_CONCURRENCY and cand_idx < len(candidates)
            )
            wait_timeout = (
                HEDGE_INTERVAL_SECONDS
                if can_hedge_more
                else max(0.5, HEDGE_TIMEOUT_SECONDS - (time.time() - start_time))
            )

            done, _ = await asyncio.wait(
                in_flight_tasks.keys(),
                timeout=wait_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for finished_task in done:
                f_key, f_model = in_flight_tasks.pop(finished_task)
                try:
                    code, data, err, usage = finished_task.result()
                    if code == 200 and data is not None:
                        for other in in_flight_tasks:
                            other.cancel()
                        text_chunks, tool_calls, finish_reason = extract_response_parts(data)
                        message = {"role": "assistant", "content": "".join(text_chunks) or None}
                        if tool_calls:
                            message["tool_calls"] = tool_calls
                        return JSONResponse(
                            content={
                                "id": chat_id,
                                "object": "chat.completion",
                                "created": created_at,
                                "model": f_model,
                                "system_fingerprint": f_key["name"],
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
                                    "total_tokens": usage.get("totalTokenCount", 0),
                                },
                            }
                        )
                    last_status = code
                    last_error_msg = err
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    last_error_msg = str(e)
    finally:
        for t in in_flight_tasks:
            t.cancel()

    return openai_error(
        last_error_msg or "All candidate keys/models unavailable",
        last_status,
        "service_unavailable",
    )
