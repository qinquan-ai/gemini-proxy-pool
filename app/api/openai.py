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
from app.runtime import app, get_http_client, logger, require_proxy_auth
from app.execution.classify import openai_error
from app.consumer.complete import complete_with_retry
from app.scheduling.policy import extract_fallback_models, extract_route_mode, extract_target_key, normalize_model
from app.producer.request_spec import build_gemini_payload
from app.consumer.responses_stream import responses_stream_with_retry
from app.execution.streaming import stream_with_retry

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
