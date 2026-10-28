import hmac
import logging
import os
import re
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.key_manager import KeyManager
from app.scheduling.limits import SlotRegistry

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
    logger.info("👉 Web Dashboard:    http://localhost:8008")
    logger.info("👉 Native Gemini:    http://localhost:8008/v1beta")
    logger.info("👉 OpenAI Compat:    http://localhost:8008/v1")
    logger.info("👉 Health Status:    http://localhost:8008/v1/status")
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
        "http://127.0.0.1:8008,http://localhost:8008,tauri://localhost",
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


slot_registry = SlotRegistry(key_pool)


def require_proxy_auth(request: Request):
    if not PROXY_API_TOKEN:
        return
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token, PROXY_API_TOKEN
    ):
        raise HTTPException(status_code=401, detail="Invalid proxy token")


# 每次请求的预算由 RouteSpec.timeout_s 决定（超时三档见 scheduling/quota.py），
# 这里只保留 httpx 客户端的兜底值。
