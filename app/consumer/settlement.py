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
from app.runtime import PERMISSION_COOLDOWN_SECONDS, key_pool
from app.execution.classify import retry_after_seconds

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
