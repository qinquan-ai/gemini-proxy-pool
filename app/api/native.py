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
from app.runtime import DEFAULT_MODEL, HEDGE_INTERVAL_SECONDS, HEDGE_TIMEOUT_SECONDS, MAX_HEDGED_CONCURRENCY, MODE_11_TIMEOUT_SECONDS, app, get_http_client, key_pool, record_trace
from app.execution.classify import google_error_message, is_allowed_native_gemini_path, native_gemini_headers, native_google_error, usage_metadata_from_payload, usage_metadata_from_sse_tail
from app.scheduling.policy import build_route_candidates, extract_fallback_models, extract_route_mode, extract_target_key
from app.consumer.settlement import finalize_failed_response

@app.api_route(
    "/v1beta/{google_path:path}", methods=["GET", "POST", "DELETE", "PATCH"]
)
async def native_gemini_gateway(google_path: str, request: Request):
    if not is_allowed_native_gemini_path(google_path):
        return native_google_error("Unsupported native Gemini endpoint", 404)

    req_start_time = time.time()
    body = await request.body()
    is_stream = "streamGenerateContent" in google_path
    client = get_http_client(request)

    extracted_model = None
    if "models/" in google_path:
        m_part = google_path.split("models/", 1)[1]
        extracted_model = m_part.split(":", 1)[0].split("/", 1)[0]

    model_display = extracted_model or DEFAULT_MODEL

    fail_fast = (
        request.headers.get("x-retry-policy", "").lower() == "fail-fast"
        or request.headers.get("x-client-mode", "").lower() == "pipeline"
        or request.query_params.get("fail_fast", "").lower() in ("1", "true")
    )
    target_key = extract_target_key(request)

    if fail_fast:
        key_info = key_pool.acquire_key(model=extracted_model, target_key_name=target_key)
        if not key_info:
            return native_google_error("No healthy API key available", 503)
        api_key = key_info["key"]
        key_name = key_info["name"]
        finalized = False
        target = f"https://generativelanguage.googleapis.com/v1beta/{google_path}"
        try:
            upstream_req = client.build_request(
                request.method,
                target,
                params=list(request.query_params.multi_items()),
                headers=native_gemini_headers(request, api_key),
                content=body,
            )
            upstream = await client.send(upstream_req, stream=is_stream)
            if upstream.status_code >= 400:
                err_b = await upstream.aread()
                msg = google_error_message(upstream, err_b)
                finalize_failed_response(api_key, upstream, msg, model=extracted_model)
                finalized = True
                await upstream.aclose()
                return Response(
                    content=err_b,
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type"),
                )

            if is_stream:
                async def ff_stream():
                    usage_tail = b""
                    ff_finalized = False
                    try:
                        async for chunk in upstream.aiter_raw():
                            usage_tail = (usage_tail + chunk)[-262_144:]
                            yield chunk
                        usg = usage_metadata_from_sse_tail(usage_tail)
                        key_pool.mark_success(api_key, usg, model=extracted_model)
                        ff_finalized = True
                    finally:
                        await upstream.aclose()
                        if not ff_finalized:
                            key_pool.release(api_key, model=extracted_model)

                finalized = True
                return StreamingResponse(
                    ff_stream(),
                    status_code=200,
                    media_type=upstream.headers.get("content-type"),
                )
            else:
                try:
                    usg = usage_metadata_from_payload(upstream.json())
                except Exception:
                    usg = {}
                key_pool.mark_success(api_key, usg, model=extracted_model)
                finalized = True
                return Response(
                    content=upstream.content,
                    status_code=200,
                    media_type=upstream.headers.get("content-type"),
                )
        finally:
            if not finalized:
                key_pool.release(api_key, model=extracted_model)

    route_mode = extract_route_mode(request)
    fallbacks = extract_fallback_models(request)
    candidates = build_route_candidates(route_mode, model_display, target_key, fallbacks)
    if not candidates:
        return native_google_error(
            "No available candidate key/model matching routing policy", 503
        )

    def _build_target_url(cand_m: str) -> str:
        if extracted_model and extracted_model in google_path:
            p = google_path.replace(f"models/{extracted_model}", f"models/{cand_m}")
            return f"https://generativelanguage.googleapis.com/v1beta/{p}"
        return f"https://generativelanguage.googleapis.com/v1beta/{google_path}"

    if route_mode == "11":
        key_info, cand_model = candidates[0]
        api_key = key_info["key"]
        key_name = key_info["name"]
        attempt = 0
        last_error_message = ""
        last_status_code = 503

        while time.time() - req_start_time < MODE_11_TIMEOUT_SECONDS:
            if await request.is_disconnected():
                return native_google_error("Client cancelled request", 499)

            if not key_pool.can_consume_bool(key_info, cand_model, check_concurrency=False):
                last_error_message = f"Daily quota exhausted for model {cand_model} on key {key_name}"
                last_status_code = 429
                break

            acquired = key_pool.acquire_key(
                model=cand_model,
                target_key_name=key_name,
                allow_transient_cooling=True,
            )
            if not acquired:
                await asyncio.sleep(0.2)
                continue

            target_url = _build_target_url(cand_model)
            finalized = False
            try:
                upstream_req = client.build_request(
                    request.method,
                    target_url,
                    params=list(request.query_params.multi_items()),
                    headers=native_gemini_headers(request, api_key),
                    content=body,
                )
                upstream = await client.send(upstream_req, stream=is_stream)

                if upstream.status_code == 200:
                    if is_stream:
                        async def m11_stream():
                            usage_tail = b""
                            st_finalized = False
                            try:
                                async for chunk in upstream.aiter_raw():
                                    if await request.is_disconnected():
                                        break
                                    usage_tail = (usage_tail + chunk)[-262_144:]
                                    yield chunk
                                usg = usage_metadata_from_sse_tail(usage_tail)
                                key_pool.mark_success(api_key, usg, model=cand_model)
                                st_finalized = True
                            finally:
                                await upstream.aclose()
                                if not st_finalized:
                                    key_pool.release(api_key, model=cand_model)

                        finalized = True
                        return StreamingResponse(
                            m11_stream(),
                            status_code=200,
                            media_type=upstream.headers.get("content-type"),
                        )
                    else:
                        try:
                            usg = usage_metadata_from_payload(upstream.json())
                        except Exception:
                            usg = {}
                        key_pool.mark_success(api_key, usg, model=cand_model)
                        finalized = True
                        return Response(
                            content=upstream.content,
                            status_code=200,
                            media_type=upstream.headers.get("content-type"),
                        )

                err_b = await upstream.aread()
                await upstream.aclose()
                msg = google_error_message(upstream, err_b)
                last_error_message = msg
                last_status_code = upstream.status_code
                finalize_failed_response(api_key, upstream, msg, model=cand_model)
                finalized = True

                if upstream.status_code in (400, 401, 403, 404):
                    return Response(
                        content=err_b,
                        status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type"),
                    )

                backoff = min(1.0 * (2 ** min(attempt, 4)), 20.0)
                attempt += 1
                await asyncio.sleep(backoff)
            except (httpx.HTTPError, OSError) as exc:
                if not finalized:
                    key_pool.mark_failure(
                        api_key,
                        reason=str(exc),
                        cooldown_seconds=key_pool.transient_cooldown,
                        model=cand_model,
                    )
                    finalized = True
                last_error_message = f"Transport error: {exc}"
                attempt += 1
                await asyncio.sleep(1.0)
            finally:
                if not finalized:
                    key_pool.release(api_key, model=cand_model)

        return native_google_error(
            last_error_message or "Mode 11 retry timeout exceeded",
            status_code=last_status_code,
        )

    cand_idx = 0
    in_flight_tasks: dict[asyncio.Task, tuple[dict, str]] = {}
    last_error_msg = ""
    last_status = 503

    async def _execute_native_attempt(
        k_info: dict, m_name: str
    ) -> tuple[int, Response | StreamingResponse | None, str]:
        a_key = k_info["key"]
        finalized_inner = False
        t_url = _build_target_url(m_name)
        try:
            req_up = client.build_request(
                request.method,
                t_url,
                params=list(request.query_params.multi_items()),
                headers=native_gemini_headers(request, a_key),
                content=body,
            )
            up = await client.send(req_up, stream=is_stream)
            if up.status_code == 200:
                if is_stream:
                    async def nat_stream():
                        usage_tail = b""
                        nat_finalized = False
                        try:
                            async for chunk in up.aiter_raw():
                                if await request.is_disconnected():
                                    break
                                usage_tail = (usage_tail + chunk)[-262_144:]
                                yield chunk
                            usg = usage_metadata_from_sse_tail(usage_tail)
                            key_pool.mark_success(a_key, usg, model=m_name)
                            nat_finalized = True
                        finally:
                            await up.aclose()
                            if not nat_finalized:
                                key_pool.release(a_key, model=m_name)

                    finalized_inner = True
                    return (
                        200,
                        StreamingResponse(
                            nat_stream(),
                            status_code=200,
                            media_type=up.headers.get("content-type"),
                        ),
                        "",
                    )
                else:
                    try:
                        usg = usage_metadata_from_payload(up.json())
                    except Exception:
                        usg = {}
                    key_pool.mark_success(a_key, usg, model=m_name)
                    finalized_inner = True
                    record_trace(
                        "Google Native",
                        request.method,
                        f"/v1beta/{google_path}",
                        m_name,
                        k_info["name"],
                        200,
                        (time.time() - req_start_time) * 1000,
                        tokens=usg,
                        route_mode=route_mode,
                    )
                    return (
                        200,
                        Response(
                            content=up.content,
                            status_code=200,
                            media_type=up.headers.get("content-type"),
                        ),
                        "",
                    )

            err_b = await up.aread()
            await up.aclose()
            msg = google_error_message(up, err_b)
            finalize_failed_response(a_key, up, msg, model=m_name)
            finalized_inner = True
            record_trace(
                "Google Native",
                request.method,
                f"/v1beta/{google_path}",
                m_name,
                k_info["name"],
                up.status_code,
                (time.time() - req_start_time) * 1000,
                route_mode=route_mode,
                error=msg,
            )
            return up.status_code, None, msg
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
            return 500, None, str(ex)
        finally:
            if not finalized_inner:
                key_pool.release(a_key, model=m_name)

    try:
        while time.time() - req_start_time < HEDGE_TIMEOUT_SECONDS:
            if await request.is_disconnected():
                for t in in_flight_tasks:
                    t.cancel()
                return native_google_error("Client cancelled request", 499)

            while len(in_flight_tasks) < MAX_HEDGED_CONCURRENCY and cand_idx < len(candidates):
                k_cand, m_cand = candidates[cand_idx]
                cand_idx += 1
                if not key_pool.can_consume_bool(k_cand, m_cand, check_concurrency=True):
                    continue
                acq = key_pool.acquire_key(
                    model=m_cand,
                    target_key_name=k_cand["name"],
                    allow_transient_cooling=True,
                )
                if acq:
                    task = asyncio.create_task(_execute_native_attempt(acq, m_cand))
                    in_flight_tasks[task] = (acq, m_cand)
                    break

            if not in_flight_tasks:
                if cand_idx >= len(candidates):
                    if time.time() - req_start_time < HEDGE_TIMEOUT_SECONDS - 5.0:
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
                else max(0.5, HEDGE_TIMEOUT_SECONDS - (time.time() - req_start_time))
            )

            done, _ = await asyncio.wait(
                in_flight_tasks.keys(),
                timeout=wait_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for finished_task in done:
                f_key, f_model = in_flight_tasks.pop(finished_task)
                try:
                    code, resp_obj, err = finished_task.result()
                    if code == 200 and resp_obj is not None:
                        for other in in_flight_tasks:
                            other.cancel()
                        return resp_obj
                    last_status = code
                    last_error_msg = err
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    last_error_msg = str(e)
    finally:
        for t in in_flight_tasks:
            t.cancel()

    return native_google_error(
        last_error_msg or "All candidate keys/models unavailable",
        status_code=last_status,
    )
