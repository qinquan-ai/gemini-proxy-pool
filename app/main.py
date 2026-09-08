import hmac
import json
import logging
import os
import re
import time
import uuid
import traceback
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from app.core.key_manager import KeyManager
from app.core.responses_adapter import (
    chat_completion_to_response,
    response_envelope,
    responses_to_chat_request,
    sse_event,
)
from app.core.thought_signatures import thought_signature_store
from app.core.translator import openai_to_gemini, translate_tools
from app.core.video_analysis import VideoAnalysisService
from app.mcp_server import configure_video_service, mcp

load_dotenv()

# Configure standard logger to match uvicorn
logger = logging.getLogger("uvicorn.error")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)

DEFAULT_MODEL = os.getenv("GEMINI_DEFAULT_MODEL", "gemini-3-flash-preview")
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
    # Print clean local access instructions on startup
    print()
    logger.info("=" * 60)
    logger.info("🚀 GEMINI PROXY POOL ACTIVE")
    logger.info("👉 Local Dashboard: http://localhost:8000")
    logger.info("👉 Health Status:   http://localhost:8000/v1/status")
    logger.info("👉 Video MCP:      http://localhost:8000/mcp/")
    logger.info("=" * 60)
    print()
    app.state.video_service = VideoAnalysisService(key_pool, app.state.http_client)
    configure_video_service(app.state.video_service)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(mcp.session_manager.run())
        yield
    await app.state.video_service.shutdown()
    await app.state.http_client.aclose()


app = FastAPI(title="Gemini Proxy Pool", version="0.7.2", lifespan=lifespan)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
app.mount("/mcp", mcp.streamable_http_app())

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
    key_pool.release(api_key)
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

    body = await request.body()
    target = f"https://generativelanguage.googleapis.com/v1beta/{google_path}"
    is_stream = "streamGenerateContent" in google_path

    extracted_model = None
    if "models/" in google_path:
        m_part = google_path.split("models/", 1)[1]
        extracted_model = m_part.split(":", 1)[0].split("/", 1)[0]

    for attempt in range(max(1, len(key_pool.keys))):
        key_info = key_pool.acquire_key(model=extracted_model)
        if not key_info:
            break
        api_key = key_info["key"]
        finalized = False
        try:
            upstream_request = request.app.state.http_client.build_request(
                request.method,
                target,
                params=list(request.query_params.multi_items()),
                headers=native_gemini_headers(request, api_key),
                content=body,
            )
            if is_stream:
                upstream = await request.app.state.http_client.send(
                    upstream_request, stream=True
                )
                if upstream.status_code >= 400:
                    error_body = await upstream.aread()
                    message = google_error_message(upstream, error_body)
                    action = finalize_failed_response(api_key, upstream, message, model=extracted_model)
                    finalized = True
                    await upstream.aclose()
                    if action == "retry":
                        continue
                    return Response(
                        content=error_body,
                        status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type"),
                    )

                async def stream_body():
                    stream_finalized = False
                    usage_tail = b""
                    try:
                        async for chunk in upstream.aiter_raw():
                            usage_tail = (usage_tail + chunk)[-262_144:]
                            yield chunk
                        key_pool.mark_success(
                            api_key, usage_metadata_from_sse_tail(usage_tail), model=extracted_model
                        )
                        stream_finalized = True
                    except (httpx.HTTPError, OSError) as exc:
                        key_pool.mark_failure(
                            api_key,
                            reason=f"Native stream transport error: {exc}",
                            cooldown_seconds=key_pool.transient_cooldown,
                            model=extracted_model,
                        )
                        stream_finalized = True
                        raise
                    finally:
                        await upstream.aclose()
                        if not stream_finalized:
                            key_pool.release(api_key)

                finalized = True
                return StreamingResponse(
                    stream_body(),
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type"),
                )

            upstream = await request.app.state.http_client.send(upstream_request)
            if upstream.status_code >= 400:
                message = google_error_message(upstream)
                action = finalize_failed_response(api_key, upstream, message, model=extracted_model)
                finalized = True
                if action == "retry":
                    continue
                return Response(
                    content=upstream.content,
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type"),
                )
            try:
                usage = usage_metadata_from_payload(upstream.json())
            except ValueError:
                usage = {}
            key_pool.mark_success(api_key, usage, model=extracted_model)
            finalized = True
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type"),
            )
        except (httpx.HTTPError, OSError) as exc:
            if not finalized:
                key_pool.mark_failure(
                    api_key,
                    reason=f"Native gateway transport error: {exc}",
                    cooldown_seconds=key_pool.transient_cooldown,
                    model=extracted_model,
                )
                finalized = True
            logger.warning(
                "Native Gemini attempt %d failed for %s: %s",
                attempt + 1,
                key_info["name"],
                exc,
            )
        finally:
            if not finalized:
                key_pool.release(api_key)

    return native_google_error("No healthy API key is available")


@app.get("/", response_class=FileResponse)
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/healthz")
async def healthz():
    status = key_pool.get_status()
    video_service = getattr(app.state, "video_service", None)
    return {
        "status": "ok" if status["available_keys"] else "degraded",
        "version": app.version,
        "available_keys": status["available_keys"],
        "total_keys": status["total_keys"],
        "default_model": DEFAULT_MODEL,
        "mcp_endpoint": "/mcp/",
        "video_jobs": len(video_service.jobs) if video_service else 0,
    }


@app.get("/v1/models")
async def list_models(request: Request):
    require_proxy_auth(request)
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "owned_by": "google"}
            for model in AVAILABLE_MODELS
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
    video_service = getattr(app.state, "video_service", None)
    jobs = list(video_service.jobs.values()) if video_service else []
    status["mcp_endpoint"] = "/mcp/"
    status["video_jobs"] = {
        "total": len(jobs),
        "running": sum(job.status in {"queued", "running"} for job in jobs),
        "completed": sum(job.status == "completed" for job in jobs),
        "failed": sum(job.status == "failed" for job in jobs),
    }
    status["supported_video_sources"] = [
        "YouTube",
        "抖音",
        "哔哩哔哩",
        "公开网页",
        "本地文件",
    ]
    status["recent_video_jobs"] = [
        {
            "id": job.id,
            "status": job.status,
            "stage": job.stage,
            "progress": round(job.progress, 3),
            "analysis_type": job.analysis_type,
            "source_type": job.source_type,
            "title": (job.source_metadata or {}).get("title"),
            "duration": (job.source_metadata or {}).get("duration"),
            "model": job.model,
            "key_name": job.key_name,
            "error": job.error,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }
        for job in sorted(jobs, key=lambda item: item.updated_at, reverse=True)[:8]
    ]
    return JSONResponse(content=status)


@app.get("/v1/quota")
@app.get("/stats/quota")
async def get_quota_details():
    return JSONResponse(content=key_pool.get_quota_status())


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    require_proxy_auth(request)
    openai_request = await request.json()
    model = normalize_model(openai_request.get("model"))
    payload = build_gemini_payload(openai_request)
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_at = int(time.time())
    base_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}"

    logger.info(
        "Request model=%s messages=%d tools=%d stream=%s",
        model,
        len(openai_request.get("messages", [])),
        len(openai_request.get("tools", [])),
        bool(openai_request.get("stream")),
    )

    if openai_request.get("stream", False):
        return StreamingResponse(
            stream_with_retry(
                request.app.state.http_client,
                base_url,
                payload,
                model,
                chat_id,
                created_at,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return await complete_with_retry(
        request.app.state.http_client,
        base_url,
        payload,
        model,
        chat_id,
        created_at,
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

    if response_request.get("stream", False):
        return StreamingResponse(
            responses_stream_with_retry(
                request.app.state.http_client,
                base_url,
                payload,
                model,
                response_id,
                created_at,
                response_request,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    chat_response = await complete_with_retry(
        request.app.state.http_client,
        base_url,
        payload,
        model,
        f"chatcmpl-{uuid.uuid4().hex[:12]}",
        created_at,
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
):
    for attempt in range(max(1, len(key_pool.keys))):
        key_info = key_pool.acquire_key(model=model)
        if not key_info:
            break

        api_key = key_info["key"]
        finalized = False
        usage = {}
        try:
            url = f"{base_url}:streamGenerateContent?alt=sse"
            async with client.stream(
                "POST", url, headers={"x-goog-api-key": api_key}, json=payload
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    message = google_error_message(response, body)
                    action = finalize_failed_response(api_key, response, message, model=model)
                    finalized = True
                    if action == "retry":
                        logger.warning(
                            "Attempt %d failed for %s: %s",
                            attempt + 1,
                            key_info["name"],
                            message,
                        )
                        continue
                    yield _sse_error(chat_id, created_at, model, message)
                    yield "data: [DONE]\n\n"
                    return

                async for line in response.aiter_lines():
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
                            model,
                            {"content": "".join(text_chunks)},
                        )
                    if tool_calls:
                        yield _sse_chunk(
                            chat_id,
                            created_at,
                            model,
                            {"tool_calls": tool_calls},
                            "tool_calls",
                        )

                key_pool.mark_success(api_key, usage, model=model)
                finalized = True
                yield _sse_chunk(chat_id, created_at, model, {}, "stop")
                yield "data: [DONE]\n\n"
                return
        except (httpx.HTTPError, OSError) as exc:
            if not finalized:
                key_pool.mark_failure(
                    api_key,
                    reason=f"Transport error: {exc}",
                    cooldown_seconds=key_pool.transient_cooldown,
                    model=model,
                )
                finalized = True
            logger.warning("Streaming transport error for %s: %s", key_info["name"], exc)
        finally:
            if not finalized:
                key_pool.release(api_key)

    yield _sse_error(chat_id, created_at, model, "No healthy API key is available")
    yield "data: [DONE]\n\n"


async def responses_stream_with_retry(
    client: httpx.AsyncClient,
    base_url: str,
    payload: dict,
    model: str,
    response_id: str,
    created_at: int,
    response_request: dict,
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
        client, base_url, payload, model, chat_id, created_at
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
):
    for attempt in range(max(1, len(key_pool.keys))):
        key_info = key_pool.acquire_key(model=model)
        if not key_info:
            break

        api_key = key_info["key"]
        finalized = False
        try:
            response = await client.post(
                f"{base_url}:generateContent",
                headers={"x-goog-api-key": api_key},
                json=payload,
            )
            if response.status_code != 200:
                message = google_error_message(response)
                action = finalize_failed_response(api_key, response, message, model=model)
                finalized = True
                if action == "retry":
                    logger.warning(
                        "Attempt %d failed for %s: %s",
                        attempt + 1,
                        key_info["name"],
                        message,
                    )
                    continue
                return openai_error(message, response.status_code, "invalid_request_error")

            data = response.json()
            text_chunks, tool_calls, finish_reason = extract_response_parts(data)
            usage = usage_metadata_from_payload(data)
            key_pool.mark_success(api_key, usage, model=model)
            finalized = True

            message = {"role": "assistant", "content": "".join(text_chunks) or None}
            if tool_calls:
                message["tool_calls"] = tool_calls

            return JSONResponse(
                content={
                    "id": chat_id,
                    "object": "chat.completion",
                    "created": created_at,
                    "model": model,
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
        except (httpx.HTTPError, OSError, ValueError) as exc:
            if not finalized:
                key_pool.mark_failure(
                    api_key,
                    reason=f"Transport error: {exc}",
                    cooldown_seconds=key_pool.transient_cooldown,
                    model=model,
                )
                finalized = True
            logger.warning("Transport error for %s: %s", key_info["name"], exc)
        finally:
            if not finalized:
                key_pool.release(api_key)

    return openai_error(
        "No healthy API key is available", 503, "service_unavailable"
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
