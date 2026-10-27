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
from app.runtime import DEFAULT_MODEL, MODEL_PATTERN, key_pool

def extract_target_key(request: Request) -> str | None:
    raw = request.headers.get("x-target-key") or request.query_params.get("target_key")
    if not raw:
        return None
    try:
        return unquote(raw).strip()
    except Exception:
        return raw.strip()


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
