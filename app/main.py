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

load_dotenv()

# Configure standard logger to match uvicorn
logger = logging.getLogger("uvicorn.error")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)

DEFAULT_MODEL = os.getenv("GEMINI_DEFAULT_MODEL", "gemini-3.5-flash-lite")
AVAILABLE_MODELS = [
    model.strip()
    for model in os.getenv("GEMINI_MODELS", DEFAULT_MODEL).split(",")
    if model.strip()
]
PROXY_API_TOKEN = os.getenv("PROXY_API_TOKEN", "").strip()
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "180"))
MAX_RETRY_AFTER_SECONDS = int(os.getenv("MAX_RETRY_AFTER_SECONDS", "86400"))
PERMISSION_COOLDOWN_SECONDS = int(
    os.getenv("PERMISSION_COOLDOWN_SECONDS", "300")
)
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
NATIVE_GEMINI_PATH_PATTERN = re.compile(
    r"^models(?:/[A-Za-z0-9._-]+(?::(?:generateContent|streamGenerateContent|countTokens))?)?$"
)

# 请求调用审计流水缓冲 (保留最近 60 笔调用轨迹供 Web 控制台实时审计)
MAX_TRACES = 60
REQUEST_TRACES: deque[dict] = deque(maxlen=MAX_TRACES)


def record_trace(
    protocol: str,
    method: str,
    endpoint: str,
    model: str,
    key_name: str,
    status_code: int,
    latency_ms: float,
    tokens: dict | None = None,
    fail_fast: bool = False,
    route_mode: str = "",
    error: str = "",
):
    tok = tokens or {}
    total_tok = int(
        tok.get("totalTokenCount")
        or tok.get("total_tokens")
        or (int(tok.get("promptTokenCount") or 0) + int(tok.get("candidatesTokenCount") or 0))
    )
    effective_mode = route_mode or ("Fail-Fast" if fail_fast else "01")
    trace_entry = {
        "id": f"tr_{uuid.uuid4().hex[:8]}",
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "time": datetime.now().strftime("%H:%M:%S"),
        "protocol": protocol,
        "method": method,
        "endpoint": endpoint,
        "model": model,
        "key_name": key_name or "N/A",
        "status_code": status_code,
        "latency_ms": round(latency_ms, 1),
        "input_tokens": int(tok.get("promptTokenCount") or 0),
        "output_tokens": int(tok.get("candidatesTokenCount") or 0),
        "thought_tokens": int(tok.get("thoughtsTokenCount") or 0),
        "total_tokens": total_tok,
        "route_mode": effective_mode,
        "fail_fast": bool(fail_fast),
        "error": error[:160] if error else "",
    }
    REQUEST_TRACES.appendleft(trace_entry)


def _sanitize_no_proxy():
    """Sanitize NO_PROXY environment variables to strip out IPv6 entries like ::1 that crash httpx's URLPattern parser."""
    for var in ("NO_PROXY", "no_proxy"):
        val = os.environ.get(var)
        if val:
            clean_entries = [
                item.strip()
                for item in val.split(",")
                if item.strip() and ":" not in item
            ]
            os.environ[var] = ",".join(clean_entries)

_sanitize_no_proxy()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
    )
    print()
    logger.info("=" * 60)
    logger.info("🚀 GEMINI PROXY GATEWAY ACTIVE (High-Throughput Pool)")
    logger.info("👉 Web Dashboard:    http://localhost:8000")
    logger.info("👉 Native Gemini:    http://localhost:8000/v1beta")
    logger.info("👉 OpenAI Compat:    http://localhost:8000/v1")
    logger.info("👉 Health Status:    http://localhost:8000/v1/status")
    logger.info("=" * 60)
    print()
    yield
    await app.state.http_client.aclose()


app = FastAPI(title="Gemini Proxy Pool", version="0.8.0", lifespan=lifespan)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


def get_http_client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "http_client", None)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
        )
        request.app.state.http_client = client
    return client

allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "PROXY_CORS_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000,tauri://localhost",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

key_pool = KeyManager()


def require_proxy_auth(request: Request):
    if not PROXY_API_TOKEN:
        return
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token, PROXY_API_TOKEN
    ):
        raise HTTPException(status_code=401, detail="Invalid proxy token")


def extract_target_key(request: Request) -> str | None:
    raw = request.headers.get("x-target-key") or request.query_params.get("target_key")
    if not raw:
        return None
    try:
        return unquote(raw).strip()
    except Exception:
        return raw.strip()


MODE_11_TIMEOUT_SECONDS = float(os.getenv("MODE_11_TIMEOUT_SECONDS", "300"))
HEDGE_TIMEOUT_SECONDS = float(os.getenv("HEDGE_TIMEOUT_SECONDS", "120"))
HEDGE_INTERVAL_SECONDS = float(os.getenv("HEDGE_INTERVAL_SECONDS", "3.0"))
MAX_HEDGED_CONCURRENCY = int(os.getenv("MAX_HEDGED_CONCURRENCY", "3"))


def extract_route_mode(request: Request, body_dict: dict | None = None) -> str:
    raw = request.headers.get("x-route-mode") or request.headers.get("x-routing-mode")
    if raw in ("11", "10", "01", "00"):
        return raw
    raw = request.query_params.get("route_mode") or request.query_params.get("mode")
    if raw in ("11", "10", "01", "00"):
        return raw
    if body_dict and isinstance(body_dict, dict):
        raw = body_dict.get("route_mode") or body_dict.get("routing_mode")
        if raw in ("11", "10", "01", "00"):
            return str(raw)
    target_key = extract_target_key(request)
    if target_key:
        return "11"
    return "01"


def extract_fallback_models(request: Request, body_dict: dict | None = None) -> list[str]:
    raw = request.headers.get("x-fallback-models") or request.query_params.get("fallback_models")
    if not raw and body_dict and isinstance(body_dict, dict):
        raw = body_dict.get("fallback_models")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(m).strip() for m in raw if str(m).strip()]
    return [m.strip() for m in str(raw).split(",") if m.strip()]


def build_route_candidates(
    mode: str,
    target_model: str,
    target_key_name: str | None = None,
    custom_fallbacks: list[str] | None = None,
) -> list[tuple[dict, str]]:
    candidates: list[tuple[dict, str]] = []
    if mode == "11":
        if target_key_name:
            k = key_pool.find_key_by_name(target_key_name)
            if k and not k.get("disabled"):
                candidates.append((k, target_model))
        else:
            for k in key_pool.keys:
                if not k.get("disabled") and key_pool.can_consume_bool(k, target_model, check_concurrency=False):
                    candidates.append((k, target_model))
                    break
        return candidates

    if mode == "10":
        key_info = None
        if target_key_name:
            key_info = key_pool.find_key_by_name(target_key_name)
        if not key_info:
            for k in key_pool.keys:
                if not k.get("disabled"):
                    key_info = k
                    break
        if key_info and not key_info.get("disabled"):
            ranked = key_pool.get_ranked_models(target_model, key_info, custom_fallbacks=custom_fallbacks)
            for item in ranked:
                candidates.append((key_info, item["model"]))
        return candidates

    if mode == "01":
        available = [
            k for k in key_pool.keys
            if not k.get("disabled") and key_pool.can_consume_bool(k, target_model, check_concurrency=False)
        ]
        available.sort(key=lambda k: (
            k.get("model_in_flight", {}).get(target_model, 0),
            key_pool._get_model_quota(k, target_model).get("success_today", 0)
        ))
        for k in available:
            candidates.append((k, target_model))
        return candidates

    if mode == "00":
        all_models = key_pool.get_ranked_models(target_model, None, custom_fallbacks=custom_fallbacks)
        for item in all_models:
            m = item["model"]
            keys_for_m = [
                k for k in key_pool.keys
                if not k.get("disabled") and key_pool.can_consume_bool(k, m, check_concurrency=False)
            ]
            keys_for_m.sort(key=lambda k: (
                k.get("model_in_flight", {}).get(m, 0),
                key_pool._get_model_quota(k, m).get("success_today", 0)
            ))
            for k in keys_for_m:
                candidates.append((k, m))
        return candidates

    return candidates


def normalize_model(requested_model: str | None) -> str:
    raw_model = requested_model or DEFAULT_MODEL
    segments = raw_model.split("/")
    if (
        len(segments) > 2
        or any(not segment for segment in segments)
        or any(not MODEL_PATTERN.fullmatch(segment) for segment in segments)
    ):
        raise HTTPException(status_code=400, detail="Invalid model identifier")
    return segments[-1]


def build_generation_config(openai_request: dict) -> dict:
    mapping = {
        "temperature": "temperature",
        "top_p": "topP",
        "max_tokens": "maxOutputTokens",
        "max_completion_tokens": "maxOutputTokens",
        "stop": "stopSequences",
    }
    config = {}
    for source, target in mapping.items():
        if source in openai_request and openai_request[source] is not None:
            config[target] = openai_request[source]
    if isinstance(config.get("stopSequences"), str):
        config["stopSequences"] = [config["stopSequences"]]
    return config


def build_gemini_payload(openai_request: dict) -> dict:
    contents, system_instruction = openai_to_gemini(
        openai_request.get("messages", [])
    )
    if not contents:
        raise HTTPException(status_code=400, detail="At least one message is required")

    payload = {"contents": contents}
    if system_instruction:
        payload["systemInstruction"] = system_instruction

    tools = translate_tools(openai_request.get("tools", []))
    if tools:
        payload["tools"] = tools

    generation_config = build_generation_config(openai_request)
    if generation_config:
        payload["generationConfig"] = generation_config
    return payload


def retry_after_seconds(response: httpx.Response) -> int:
    header = response.headers.get("retry-after", "").strip()
    if header.isdigit():
        return min(MAX_RETRY_AFTER_SECONDS, max(1, int(header)))

    try:
        details = response.json().get("error", {}).get("details", [])
        for detail in details:
            retry_delay = str(detail.get("retryDelay", ""))
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)s", retry_delay)
            if match:
                return min(
                    MAX_RETRY_AFTER_SECONDS,
                    max(1, int(float(match.group(1)) + 0.999)),
                )
    except (ValueError, TypeError, AttributeError):
        pass
    return key_pool.default_rate_limit_cooldown


def google_error_message(response: httpx.Response, body: bytes | None = None) -> str:
    try:
        payload = json.loads((body or response.content).decode("utf-8"))
        message = payload.get("error", {}).get("message")
        if message:
            return str(message)[:500]
    except (ValueError, UnicodeDecodeError, AttributeError):
        pass
    return f"Google API returned HTTP {response.status_code}"


def openai_error(message: str, status_code: int, error_type: str):
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type}},
    )


def finalize_failed_response(
    api_key: str,
    response: httpx.Response,
    message: str,
    model: str | None = None,
) -> str:
    status = response.status_code
    if status == 429:
        cooldown = retry_after_seconds(response)
        key_pool.mark_failure(
            api_key,
            reason=message,
            cooldown_seconds=cooldown,
            rate_limited=True,
            model=model,
        )
        return "retry"
    if status == 401:
        key_pool.mark_failure(api_key, reason=message, disable=True, model=model)
        return "retry"
    if status == 403:
        key_pool.mark_failure(
            api_key,
            reason=message,
            cooldown_seconds=PERMISSION_COOLDOWN_SECONDS,
            model=model,
        )
        return "retry"
    if status in (408, 409, 425, 500, 502, 503, 504):
        key_pool.mark_failure(
            api_key,
            reason=message,
            cooldown_seconds=key_pool.transient_cooldown,
            model=model,
        )
        return "retry"

    # Request/model errors are not evidence that the key is unhealthy.
    key_pool.release(api_key, model=model)
    return "return"


def extract_response_parts(data: dict) -> tuple[list[str], list[dict], str]:
    candidates = data.get("candidates", [])
    if not candidates:
        return [], [], "stop"

    candidate = candidates[0]
    text_chunks = []
    tool_calls = []
    for part in candidate.get("content", {}).get("parts", []):
        if "text" in part:
            text_chunks.append(part["text"])
        if "thought" in part:
            thought = part["thought"]
            thought_text = thought if isinstance(thought, str) else json.dumps(thought)
            text_chunks.append(
                f"<!--THOUGHT_BEGIN-->{thought_text}<!--THOUGHT_END-->"
            )
        if "functionCall" in part:
            function_call = part["functionCall"]
            call_id = f"call_{uuid.uuid4().hex[:8]}"
            thought_signature_store.remember(
                call_id,
                function_call.get("name", ""),
                function_call.get("args", {}),
                part.get("thoughtSignature", ""),
            )
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": function_call.get("name", ""),
                        "arguments": json.dumps(function_call.get("args", {})),
                    },
                }
            )

    finish_reason = "tool_calls" if tool_calls else "stop"
    return text_chunks, tool_calls, finish_reason


def usage_metadata_from_payload(data: dict) -> dict:
    usage = data.get("usageMetadata", {}) if isinstance(data, dict) else {}
    return usage if isinstance(usage, dict) else {}


def usage_metadata_from_sse_tail(content: bytes) -> dict:
    latest = {}
    for line in content.decode("utf-8", errors="ignore").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except ValueError:
            continue
        usage = usage_metadata_from_payload(payload)
        if usage:
            latest = usage
    return latest


def is_allowed_native_gemini_path(path: str) -> bool:
    return bool(NATIVE_GEMINI_PATH_PATTERN.fullmatch(path))


def native_gemini_headers(request: Request, api_key: str) -> dict[str, str]:
    headers = {"x-goog-api-key": api_key}
    for name in ("accept", "content-type", "user-agent", "x-goog-api-client"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


def native_google_error(message: str, status_code: int = 503) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": status_code,
                "message": message,
                "status": "UNAVAILABLE" if status_code == 503 else "UNKNOWN",
            }
        },
    )


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


@app.get("/", response_class=FileResponse)
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/healthz")
async def healthz():
    status = key_pool.get_status()
    return {
        "status": "ok" if status["available_keys"] else "degraded",
        "version": app.version,
        "available_keys": status["available_keys"],
        "total_keys": status["total_keys"],
        "default_model": DEFAULT_MODEL,
        "recent_traces_count": len(REQUEST_TRACES),
    }


_OFFICIAL_MODELS_CACHE = {"models": [], "cached_at": 0.0}


@app.get("/v1/models")
async def list_models(request: Request):
    require_proxy_auth(request)
    now = time.time()
    # Cache for 10 minutes
    if not _OFFICIAL_MODELS_CACHE["models"] or (now - _OFFICIAL_MODELS_CACHE["cached_at"] > 600):
        key_info = key_pool.acquire_key()
        if key_info:
            api_key = key_info["key"]
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}")
                    if resp.status_code == 200:
                        raw_models = resp.json().get("models", [])
                        official_ids = [m["name"].replace("models/", "") for m in raw_models]
                        _OFFICIAL_MODELS_CACHE["models"] = official_ids
                        _OFFICIAL_MODELS_CACHE["cached_at"] = now
            except Exception as e:
                logger.warning("Failed to fetch official models from Google, falling back to local list: %s", e)
            finally:
                key_pool.release(api_key)

    model_ids = list(dict.fromkeys((_OFFICIAL_MODELS_CACHE["models"] or []) + AVAILABLE_MODELS))
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "owned_by": "google"}
            for model in model_ids
        ],
    }


@app.get("/v1/status")
async def get_status():
    status = key_pool.get_status()
    quota_status = key_pool.get_quota_status()
    status["default_model"] = DEFAULT_MODEL
    status["models"] = AVAILABLE_MODELS
    status["auth_enabled"] = bool(PROXY_API_TOKEN)
    status["quota_summary"] = quota_status.get("summary_by_model", {})
    status["quota_reset_in_hours"] = quota_status.get("next_reset_in_hours")
    status["recent_traces"] = list(REQUEST_TRACES)[:30]
    status["total_traces"] = len(REQUEST_TRACES)
    return JSONResponse(content=status)


@app.get("/v1/quota")
async def get_quota():
    """获取模型配额状态摘要"""
    return JSONResponse(content=key_pool.get_quota_status())


@app.get("/v1/traces")
async def get_traces():
    """获取网关最近的请求审计流水。"""
    return JSONResponse(content={"traces": list(REQUEST_TRACES)})


@app.post("/v1/keys/{key_name}/unfreeze")
async def unfreeze_key(key_name: str, request: Request):
    require_proxy_auth(request)
    success = key_pool.unfreeze_key(key_name)
    if not success:
        raise HTTPException(status_code=404, detail=f"Key '{key_name}' not found")
    logger.info("Key '%s' manually unfrozen via dashboard API", key_name)
    return {"status": "ok", "message": f"Key '{key_name}' 已成功解冻"}


@app.post("/v1/keys/{key_name}/toggle")
async def toggle_key(key_name: str, request: Request):
    require_proxy_auth(request)
    disabled = key_pool.toggle_key(key_name)
    if disabled is None:
        raise HTTPException(status_code=404, detail=f"Key '{key_name}' not found")
    action_str = "禁用" if disabled else "启用"
    logger.info("Key '%s' manually %s via dashboard API", key_name, action_str)
    return {
        "status": "ok",
        "disabled": disabled,
        "message": f"Key '{key_name}' 已{action_str}",
    }


@app.post("/v1/keys/{key_name}/ping")
async def ping_key(key_name: str, request: Request):
    require_proxy_auth(request)
    key_info = key_pool.find_key_by_name(key_name)
    if not key_info:
        raise HTTPException(status_code=404, detail=f"Key '{key_name}' not found")

    api_key = key_info["key"]
    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            )
            latency_ms = round((time.time() - start_time) * 1000, 1)
            if resp.status_code == 200:
                models_health = {}
                deprioritized = []
                now_ts = time.time()
                for m_name in CANONICAL_MODELS:
                    q = key_pool._get_model_quota(key_info, m_name)
                    limit_rpd = q.get("limit_rpd", 20)
                    used_today = q.get("success_today", 0)
                    ratio = (used_today / limit_rpd) if limit_rpd > 0 else 0.0
                    is_deprioritized = ratio > 0.6 and (limit_rpd - used_today) > 0
                    can_use, reason = key_pool.can_consume(key_info, m_name, check_concurrency=False, now=now_ts)
                    status = (
                        "Disabled"
                        if key_info.get("disabled")
                        else (
                            "RateLimited"
                            if q.get("cooldown_until", 0.0) > now_ts
                            else ("Exhausted" if used_today >= limit_rpd else "Active")
                        )
                    )
                    models_health[m_name] = {
                        "can_consume": can_use,
                        "reason": reason,
                        "used_today": used_today,
                        "limit_rpd": limit_rpd,
                        "usage_ratio": round(ratio, 3),
                        "status": status,
                        "is_deprioritized": is_deprioritized,
                    }
                    if is_deprioritized:
                        deprioritized.append(m_name)

                return {
                    "status": "ok",
                    "status_code": 200,
                    "latency_ms": latency_ms,
                    "message": f"连通性正常 ({latency_ms}ms)",
                    "auth_valid": True,
                    "models_health": models_health,
                    "deprioritized_models": deprioritized,
                }
            err_msg = google_error_message(resp)
            return {
                "status": "error",
                "status_code": resp.status_code,
                "latency_ms": latency_ms,
                "message": err_msg,
                "auth_valid": False,
            }
    except Exception as exc:
        latency_ms = round((time.time() - start_time) * 1000, 1)
        return {
            "status": "error",
            "status_code": 500,
            "latency_ms": latency_ms,
            "message": f"网络异常: {exc}",
            "auth_valid": False,
        }


class BatchVerifyRequest(BaseModel):
    model: str | None = None
    models: list[str] | None = None
    keys: list[str] | None = None
    probe_type: str = "ping"


class DeleteBatchesRequest(BaseModel):
    batch_ids: list[str]


@app.post("/v1/keys/verify-batch")
async def verify_batch(req_data: BatchVerifyRequest, request: Request):
    require_proxy_auth(request)
    target_models = []
    if req_data.models:
        target_models = [normalize_model(m) for m in req_data.models if m]
    elif req_data.model:
        target_models = [normalize_model(req_data.model)]
    if not target_models:
        target_models = ["gemini-3.8-flash"]

    probe_type = req_data.probe_type or "ping"

    selected_keys = []
    if req_data.keys:
        for k_name in req_data.keys:
            k = key_pool.find_key_by_name(k_name)
            if k:
                selected_keys.append(k)
    else:
        selected_keys = [k for k in key_pool.keys if not k.get("disabled")]

    if not selected_keys:
        raise HTTPException(status_code=400, detail="未选择任何有效 Key")

    batch_id = f"batch_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    batch_start = time.time()

    async def _test_single(key_info: dict, m_target: str) -> dict:
        api_key = key_info["key"]
        k_name = key_info["name"]
        t0 = time.time()
        now_ts = time.time()
        can_consume, reason = key_pool.can_consume(key_info, m_target, check_concurrency=False, now=now_ts)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if probe_type == "generate":
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_target}:generateContent?key={api_key}"
                    payload = {
                        "contents": [{"parts": [{"text": "Hi"}]}],
                        "generationConfig": {"maxOutputTokens": 1},
                    }
                    resp = await client.post(url, json=payload)
                else:
                    # Health 探针 (GET models/{model}，验证权限且 0 配额消耗)
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_target}?key={api_key}"
                    resp = await client.get(url)

                lat = round((time.time() - t0) * 1000, 1)
                if resp.status_code == 200:
                    return {
                        "key_name": k_name,
                        "key_prefix": key_pool._mask_key(api_key),
                        "model": m_target,
                        "probe_type": probe_type,
                        "status": "Success",
                        "status_code": 200,
                        "latency_ms": lat,
                        "can_consume": can_consume,
                        "reason": reason,
                        "message": "生成连通测试正常 (HTTP 200)" if probe_type == "generate" else "模型支持且接口健康 (0额度消耗)",
                    }
                err_msg = google_error_message(resp)
                if resp.status_code == 503:
                    st = "HighDemand"
                elif resp.status_code == 429:
                    st = "RateLimited"
                elif resp.status_code in (401, 403):
                    st = "AuthError"
                else:
                    st = "Failed"
                return {
                    "key_name": k_name,
                    "key_prefix": key_pool._mask_key(api_key),
                    "model": m_target,
                    "probe_type": probe_type,
                    "status": st,
                    "status_code": resp.status_code,
                    "latency_ms": lat,
                    "can_consume": can_consume,
                    "reason": reason,
                    "message": err_msg,
                }
        except Exception as e:
            lat = round((time.time() - t0) * 1000, 1)
            return {
                "key_name": k_name,
                "key_prefix": key_pool._mask_key(api_key),
                "model": m_target,
                "probe_type": probe_type,
                "status": "Failed",
                "status_code": 500,
                "latency_ms": lat,
                "can_consume": False,
                "reason": "Exception",
                "message": f"连接异常: {str(e)}",
            }

    tasks = [
        _test_single(k, m)
        for k in selected_keys
        for m in target_models
    ]
    records = await asyncio.gather(*tasks)

    success_cnt = sum(1 for r in records if r["status"] == "Success")
    cooling_cnt = sum(1 for r in records if r["status"] in ("HighDemand", "RateLimited"))
    failed_cnt = sum(1 for r in records if r["status"] in ("Failed", "AuthError"))

    models_display = ", ".join(target_models) if len(target_models) <= 3 else f"{len(target_models)} 款模型"
    batch_record = {
        "batch_id": batch_id,
        "verified_at": int(batch_start),
        "model": models_display,
        "probe_type": probe_type,
        "total": len(records),
        "success_count": success_cnt,
        "cooling_count": cooling_cnt,
        "failed_count": failed_cnt,
        "records": records,
    }
    key_pool.add_verification_batch(batch_record)
    return batch_record


@app.get("/v1/keys/verify-history")
async def get_verify_history():
    return {
        "status": "ok",
        "batches": key_pool.get_verification_batches(),
    }


@app.delete("/v1/keys/verify-history")
async def delete_verify_batches(req_data: DeleteBatchesRequest, request: Request):
    require_proxy_auth(request)
    key_pool.delete_verification_batches(req_data.batch_ids)
    return {"status": "ok", "deleted": req_data.batch_ids}


@app.delete("/v1/keys/verify-history/{batch_id}")
async def delete_single_verify_batch(batch_id: str, request: Request):
    require_proxy_auth(request)
    key_pool.delete_verification_batches([batch_id])
    return {"status": "ok", "deleted": [batch_id]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    require_proxy_auth(request)
    openai_request = await request.json()
    model = normalize_model(openai_request.get("model"))
    payload = build_gemini_payload(openai_request)
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_at = int(time.time())
    base_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}"

    target_key = extract_target_key(request)
    route_mode = extract_route_mode(request, openai_request)
    fallbacks = extract_fallback_models(request, openai_request)

    logger.info(
        "Request model=%s mode=%s target_key=%s messages=%d tools=%d stream=%s",
        model,
        route_mode,
        target_key or "auto",
        len(openai_request.get("messages", [])),
        len(openai_request.get("tools", [])),
        bool(openai_request.get("stream")),
    )

    client = get_http_client(request)

    if openai_request.get("stream", False):
        return StreamingResponse(
            stream_with_retry(
                client,
                base_url,
                payload,
                model,
                chat_id,
                created_at,
                target_key=target_key,
                request=request,
                route_mode=route_mode,
                custom_fallbacks=fallbacks,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return await complete_with_retry(
        client,
        base_url,
        payload,
        model,
        chat_id,
        created_at,
        target_key=target_key,
        request=request,
        route_mode=route_mode,
        custom_fallbacks=fallbacks,
    )


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
    base_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}"

    target_key = extract_target_key(request)
    route_mode = extract_route_mode(request, response_request)
    fallbacks = extract_fallback_models(request, response_request)
    client = get_http_client(request)

    if response_request.get("stream", False):
        return StreamingResponse(
            responses_stream_with_retry(
                client,
                base_url,
                payload,
                model,
                response_id,
                created_at,
                response_request,
                target_key=target_key,
                request=request,
                route_mode=route_mode,
                custom_fallbacks=fallbacks,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    chat_response = await complete_with_retry(
        client,
        base_url,
        payload,
        model,
        f"chatcmpl-{uuid.uuid4().hex[:12]}",
        created_at,
        target_key=target_key,
        request=request,
        route_mode=route_mode,
        custom_fallbacks=fallbacks,
    )
    if chat_response.status_code >= 400:
        return chat_response
    chat_completion = json.loads(chat_response.body.decode("utf-8"))
    return JSONResponse(
        content=chat_completion_to_response(
            chat_completion, response_request, response_id, created_at
        )
    )


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


async def responses_stream_with_retry(
    client: httpx.AsyncClient,
    base_url: str,
    payload: dict,
    model: str,
    response_id: str,
    created_at: int,
    response_request: dict,
    target_key: str | None = None,
    request: Request | None = None,
    route_mode: str = "01",
    custom_fallbacks: list[str] | None = None,
):
    sequence = 0
    initial_response = response_envelope(
        response_id,
        model,
        response_request,
        status="in_progress",
        output=[],
        created_at=created_at,
    )
    yield sse_event("response.created", sequence, response=initial_response)
    sequence += 1
    yield sse_event("response.in_progress", sequence, response=initial_response)
    sequence += 1

    message_item = None
    text_chunks = []
    function_items = []
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    async for chunk in stream_with_retry(
        client,
        base_url,
        payload,
        model,
        chat_id,
        created_at,
        target_key=target_key,
        request=request,
        route_mode=route_mode,
        custom_fallbacks=custom_fallbacks,
    ):
        for line in chunk.splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                chat_chunk = json.loads(line[6:])
            except ValueError:
                continue
            choice = (chat_chunk.get("choices") or [{}])[0]
            delta = choice.get("delta", {})

            text_delta = delta.get("content")
            if text_delta:
                if message_item is None:
                    message_item = {
                        "id": f"msg_{uuid.uuid4().hex[:24]}",
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    }
                    yield sse_event(
                        "response.output_item.added",
                        sequence,
                        output_index=0,
                        item=message_item,
                    )
                    sequence += 1
                    yield sse_event(
                        "response.content_part.added",
                        sequence,
                        item_id=message_item["id"],
                        output_index=0,
                        content_index=0,
                        part={
                            "type": "output_text",
                            "text": "",
                            "annotations": [],
                            "logprobs": [],
                        },
                    )
                    sequence += 1
                text_chunks.append(text_delta)
                yield sse_event(
                    "response.output_text.delta",
                    sequence,
                    item_id=message_item["id"],
                    output_index=0,
                    content_index=0,
                    delta=text_delta,
                    logprobs=[],
                )
                sequence += 1

            for tool_call in delta.get("tool_calls", []) or []:
                function = tool_call.get("function", {})
                function_item = {
                    "id": f"fc_{uuid.uuid4().hex[:24]}",
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": tool_call.get("id")
                    or f"call_{uuid.uuid4().hex[:16]}",
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments", "{}"),
                }
                output_index = (1 if message_item else 0) + len(function_items)
                function_items.append(function_item)
                yield sse_event(
                    "response.output_item.added",
                    sequence,
                    output_index=output_index,
                    item=function_item,
                )
                sequence += 1
                yield sse_event(
                    "response.function_call_arguments.delta",
                    sequence,
                    item_id=function_item["id"],
                    output_index=output_index,
                    delta=function_item["arguments"],
                )
                sequence += 1

    output = []
    if message_item is not None:
        text = "".join(text_chunks)
        message_item = {
            **message_item,
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": text,
                    "annotations": [],
                    "logprobs": [],
                }
            ],
        }
        output.append(message_item)
        yield sse_event(
            "response.output_text.done",
            sequence,
            item_id=message_item["id"],
            output_index=0,
            content_index=0,
            text=text,
            logprobs=[],
        )
        sequence += 1
        yield sse_event(
            "response.content_part.done",
            sequence,
            item_id=message_item["id"],
            output_index=0,
            content_index=0,
            part=message_item["content"][0],
        )
        sequence += 1
        yield sse_event(
            "response.output_item.done",
            sequence,
            output_index=0,
            item=message_item,
        )
        sequence += 1

    for index, function_item in enumerate(function_items, start=len(output)):
        function_item = {**function_item, "status": "completed"}
        output.append(function_item)
        yield sse_event(
            "response.function_call_arguments.done",
            sequence,
            item_id=function_item["id"],
            output_index=index,
            arguments=function_item["arguments"],
        )
        sequence += 1
        yield sse_event(
            "response.output_item.done",
            sequence,
            output_index=index,
            item=function_item,
        )
        sequence += 1

    completed_response = response_envelope(
        response_id,
        model,
        response_request,
        status="completed",
        output=output,
        usage={
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,
        },
        created_at=created_at,
    )
    yield sse_event(
        "response.completed", sequence, response=completed_response
    )


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
