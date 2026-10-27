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
from app.runtime import MAX_RETRY_AFTER_SECONDS, NATIVE_GEMINI_PATH_PATTERN, key_pool

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
