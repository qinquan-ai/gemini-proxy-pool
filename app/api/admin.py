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
from app.runtime import AVAILABLE_MODELS, DEFAULT_MODEL, PROXY_API_TOKEN, REQUEST_TRACES, STATIC_DIR, app, key_pool, logger, require_proxy_auth
from app.execution.classify import google_error_message
from app.scheduling.policy import normalize_model

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
