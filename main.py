"""
AI Proxy Gateway - OpenAI -> Gemini Translator
Translates OpenAI-compatible API calls into Google Gemini API calls.
Supports streaming (SSE), non-streaming responses, and Automatic Account Key Pooling.
"""
import os
import json
import time
import uuid
import httpx
import traceback
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="AI Proxy Gateway (Key Pool Enabled)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_MODEL = "gemini-3-flash-preview"
COOLDOWN_SECONDS = 3600  # 1 hour cooldown for exhausted keys

# ----------------- Key Management System -----------------
class KeyManager:
    def __init__(self):
        self.keys = []
        self.current_index = 0
        
        # Format: "Name1|Key1,Name2|Key2"
        keys_str = os.getenv("GEMINI_KEYS", "")
        if keys_str:
            for item in keys_str.split(","):
                if "|" in item:
                    name, key = item.split("|", 1)
                    self._add_key(name.strip(), key.strip())
        else:
            # Fallback to legacy single key
            single_key = os.getenv("GEMINI_API_KEY", "")
            if single_key:
                self._add_key("Default", single_key.strip())

    def _add_key(self, name: str, key: str):
        self.keys.append({
            "name": name,
            "key": key,
            "req_count": 0,       # Total successful attempts
            "success_count": 0,   # Total 200 OKs
            "exhausted": False,
            "exhausted_time": 0
        })

    def get_next_key(self) -> dict:
        if not self.keys:
            return None

        start_index = self.current_index
        for _ in range(len(self.keys)):
            idx = self.current_index
            self.current_index = (self.current_index + 1) % len(self.keys)
            
            key_info = self.keys[idx]
            
            # Check if cooldown is over
            if key_info["exhausted"]:
                if time.time() - key_info["exhausted_time"] > COOLDOWN_SECONDS:
                    key_info["exhausted"] = False
                    print(f"[{time.strftime('%H:%M:%S')}] 🟢 Key Recovered from Cooldown: {key_info['name']}")
                else:
                    continue # Still exhausted

            # Selected a healthy key
            key_info["req_count"] += 1
            return key_info
            
        # If all exhausted, force rotation and try anyway (better than crashing instantly)
        print(f"[{time.strftime('%H:%M:%S')}] ⚠️ ALL KEYS EXHAUSTED! Forcing blind rotation...")
        self.current_index = (self.current_index + 1) % len(self.keys)
        return self.keys[self.current_index]

    def mark_exhausted(self, key_str: str):
        for k in self.keys:
            if k["key"] == key_str:
                k["exhausted"] = True
                k["exhausted_time"] = time.time()
                print(f"[{time.strftime('%H:%M:%S')}] 🔴 KEY 429 QUOTA HIT! Cooling down: {k['name']}")
                break
                
    def mark_success(self, key_str: str):
        for k in self.keys:
            if k["key"] == key_str:
                k["success_count"] += 1
                break

key_pool = KeyManager()
# ---------------------------------------------------------


def normalize_content(content) -> str:
    if content is None: return ""
    if isinstance(content, str): return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif "text" in item and "type" not in item:
                    text_parts.append(item.get("text", ""))
        return "\n".join(text_parts)
    return str(content)


def _sanitize_schema(schema: dict) -> dict:
    """Recursively strip OpenAI-only fields from JSON Schema objects.
    Gemini only accepts standard JSON Schema fields: type, description, properties, required, enum, items, etc.
    OpenAI adds non-standard fields like 'strict', 'additionalProperties' on function level, etc."""
    if not isinstance(schema, dict):
        return schema
    
    # Fields that Gemini's functionDeclarations accept
    ALLOWED_SCHEMA_KEYS = {"type", "description", "properties", "required", "enum", "items", "format", "nullable", "default"}
    cleaned = {}
    for key, value in schema.items():
        if key not in ALLOWED_SCHEMA_KEYS:
            continue  # Strip unknown fields like "strict", "additionalProperties"
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {k: _sanitize_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            cleaned[key] = _sanitize_schema(value)
        else:
            cleaned[key] = value
    return cleaned


def translate_tools(openai_tools: list) -> list:
    """Convert OpenAI tools array to Gemini functionDeclarations format.
    Strips OpenAI-only fields (like 'strict') that Gemini rejects with 400."""
    if not openai_tools:
        return []
        
    gemini_functions = []
    for t in openai_tools:
        if t.get("type") == "function" and "function" in t:
            fn = t["function"]
            cleaned_fn = {
                "name": fn.get("name", ""),
            }
            if fn.get("description"):
                cleaned_fn["description"] = fn["description"]
            if fn.get("parameters"):
                cleaned_fn["parameters"] = _sanitize_schema(fn["parameters"])
            gemini_functions.append(cleaned_fn)
            
    if not gemini_functions:
        return []
        
    return [{"functionDeclarations": gemini_functions}]


def openai_to_gemini(openai_messages: list) -> tuple:
    """Convert messages including tool_calls and tool responses."""
    contents = []
    system_instruction = None

    for msg in openai_messages:
        role = msg.get("role", "user")

        if role == "system":
            content_str = normalize_content(msg.get("content"))
            system_instruction = {"parts": [{"text": content_str}]}
            continue

        if role == "assistant" and msg.get("tool_calls"):
            parts = []
            if msg.get("content"):
                parts.append({"text": normalize_content(msg.get("content"))})
            for tc in msg.get("tool_calls", []):
                if tc.get("type") == "function":
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except:
                            args = {}
                    parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue

        if role == "tool":
            tool_name = msg.get("name", "unknown_tool")
            tool_content = msg.get("content", "")
            try:
                parsed_content = json.loads(tool_content) if isinstance(tool_content, str) else tool_content
            except:
                parsed_content = {"result": tool_content}

            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {"name": tool_name, "response": parsed_content}}]
            })
            continue

        gemini_role = "model" if role == "assistant" else "user"
        content_str = normalize_content(msg.get("content"))
        if content_str:
            contents.append({
                "role": gemini_role,
                "parts": [{"text": content_str}]
            })

    return contents, system_instruction


@app.get("/v1/models")
async def list_models():
    return JSONResponse(content={
        "object": "list",
        "data": [{
            "id": DEFAULT_MODEL,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "google",
        }]
    })


@app.get("/v1/status")
async def get_status():
    status_list = []
    for k in key_pool.keys:
        cooldown_remaining = max(0, int(COOLDOWN_SECONDS - (time.time() - k["exhausted_time"]))) if k["exhausted"] else 0
        status_list.append({
            "name": k["name"],
            "status": "Exhausted (429)" if k["exhausted"] else "Active",
            "req_count": k["req_count"],
            "success_count": k["success_count"],
            "cooldown_remaining_sec": cooldown_remaining,
            "key_prefix": f"{k['key'][:8]}...{k['key'][-4:]}" if k['key'] else "N/A"
        })
    return JSONResponse(content={"total_keys": len(key_pool.keys), "pool": status_list})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    openai_req = await request.json()
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())

    messages = openai_req.get("messages", [])
    contents, system_instruction = openai_to_gemini(messages)

    gemini_payload = {"contents": contents}
    if system_instruction:
        gemini_payload["systemInstruction"] = system_instruction
        
    # Inject Tools
    tools_arr = translate_tools(openai_req.get("tools", []))
    if tools_arr:
        gemini_payload["tools"] = tools_arr
        
    model = DEFAULT_MODEL
    base_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
    stream = openai_req.get("stream", False)

    print(f"\n{'='*60}")
    print(f"[{time.strftime('%H:%M:%S')}] Request: {len(messages)} msgs | Tools: {len(tools_arr)} | Stream: {stream}")

    if stream:
        return StreamingResponse(
            _handle_streaming_with_retry(base_url, gemini_payload, model, chat_id, created_ts), 
            media_type="text/event-stream"
        )
    else:
        return await _handle_non_streaming_with_retry(base_url, gemini_payload, model, chat_id, created_ts)


def build_error_chunk(chat_id, created_ts, model, message):
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created_ts,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": f"\n[网关错误: {message}]\n"}, "finish_reason": "stop"}]
    }


async def _handle_streaming_with_retry(base_url, gemini_payload, model, chat_id, created_ts):
    max_retries = max(1, len(key_pool.keys))
    
    for attempt in range(max_retries):
        key_info = key_pool.get_next_key()
        if not key_info:
            yield f"data: {json.dumps(build_error_chunk(chat_id, created_ts, model, '号池均已耗尽且无Key可用'))}\n\n"
            yield "data: [DONE]\n\n"
            return
            
        api_key = key_info["key"]
        url = f"{base_url}:streamGenerateContent?alt=sse&key={api_key}"
        
        try:
            async with httpx.AsyncClient() as client:
                print(f"[{time.strftime('%H:%M:%S')}] Attempt {attempt+1}/{max_retries} with Key: {key_info['name']}")
                async with client.stream("POST", url, json=gemini_payload, timeout=120.0) as response:
                    
                    if response.status_code == 429:
                        key_pool.mark_exhausted(api_key)
                        continue
                        
                    if response.status_code != 200:
                        error_body = await response.aread()
                        key_pool.mark_exhausted(api_key)
                        print(f"!!! HTTP {response.status_code} on {key_info['name']} !!!")
                        print(error_body)
                        # We yield the error if we are out of keys or if it's not a 429, but wait, usually if it's a 400 Bad Request, retrying won't help. 
                        # Let's break and return the error.
                        if response.status_code == 400:
                            yield f"data: {json.dumps(build_error_chunk(chat_id, created_ts, model, f'Bad Request: {error_body.decode()[:200]}'))}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        continue 
                        
                    key_pool.mark_success(api_key)
                    chunk_count = 0
                    
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue

                        raw = line[6:]
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        candidates = data.get("candidates", [])
                        if not candidates: continue

                        candidate = candidates[0]
                        parts = candidate.get("content", {}).get("parts", [])
                        finish_reason = candidate.get("finishReason")
                        
                        text_chunks = []
                        tool_calls = []
                        
                        for part in parts:
                            if "text" in part:
                                text_chunks.append(part["text"])
                            if "functionCall" in part:
                                func = part["functionCall"]
                                tool_calls.append({
                                    "index": 0,
                                    "id": f"call_{uuid.uuid4().hex[:8]}",
                                    "type": "function",
                                    "function": {
                                        "name": func.get("name", ""),
                                        "arguments": json.dumps(func.get("args", {}))
                                    }
                                })

                        # Send Text Delta
                        text = "".join(text_chunks)
                        if text:
                            chunk_count += 1
                            openai_chunk = {
                                "id": chat_id, "object": "chat.completion.chunk", "created": created_ts, "model": model,
                                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
                            }
                            yield f"data: {json.dumps(openai_chunk)}\n\n"
                            
                        # Send Tool Call Delta
                        if tool_calls:
                            chunk_count += 1
                            tool_chunk = {
                                "id": chat_id, "object": "chat.completion.chunk", "created": created_ts, "model": model,
                                "choices": [{"index": 0, "delta": {"tool_calls": tool_calls}, "finish_reason": "tool_calls"}]
                            }
                            yield f"data: {json.dumps(tool_chunk)}\n\n"

                        # Handle Block/Errors
                        if finish_reason and finish_reason not in ("STOP", "MAX_TOKENS"):
                            yield f"data: {json.dumps(build_error_chunk(chat_id, created_ts, model, f'安全拦截: {finish_reason}'))}\n\n"
                            # Do not break, wait for loop to finish natively

                    # Send Stop explicitly
                    yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created_ts, 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
                    yield "data: [DONE]\n\n"
                    return 

        except httpx.TimeoutException:
            print(f"[{time.strftime('%H:%M:%S')}] Timeout on Key: {key_info['name']}")
            continue
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Error: {e}")
            traceback.print_exc()
            continue

    yield f"data: {json.dumps(build_error_chunk(chat_id, created_ts, model, '号池全灭！请求全部失败。'))}\n\n"
    yield "data: [DONE]\n\n"


async def _handle_non_streaming_with_retry(base_url, gemini_payload, model, chat_id, created_ts):
    max_retries = max(1, len(key_pool.keys))
    
    for attempt in range(max_retries):
        key_info = key_pool.get_next_key()
        if not key_info:
            raise HTTPException(status_code=503, detail="No API Keys available in pool")
            
        api_key = key_info["key"]
        url = f"{base_url}:generateContent?key={api_key}"
        
        try:
            async with httpx.AsyncClient() as client:
                print(f"[{time.strftime('%H:%M:%S')}] Attempt {attempt+1}/{max_retries} with Key: {key_info['name']}")
                resp = await client.post(url, json=gemini_payload, timeout=120.0)
                
                if resp.status_code == 429:
                    key_pool.mark_exhausted(api_key)
                    continue
                    
                if resp.status_code != 200:
                    key_pool.mark_exhausted(api_key)
                    if resp.status_code == 400:
                        raise HTTPException(502, f"Gemini API 400 Error: {resp.text[:200]}")
                    continue

                key_pool.mark_success(api_key)
                data = resp.json()
                
                candidates = data.get("candidates", [])
                if not candidates:
                    raise Exception("No candidates returned from Gemini")
                    
                parts = candidates[0].get("content", {}).get("parts", [])
                
                text_chunks = []
                tool_calls = []
                for part in parts:
                    if "text" in part:
                        text_chunks.append(part["text"])
                    if "functionCall" in part:
                        func = part["functionCall"]
                        tool_calls.append({
                            "id": f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {
                                "name": func.get("name", ""),
                                "arguments": json.dumps(func.get("args", {}))
                            }
                        })
                
                message = {"role": "assistant"}
                text_content = "".join(text_chunks)
                if text_content:
                    message["content"] = text_content
                if tool_calls:
                    if "content" not in message:
                        message["content"] = None
                    message["tool_calls"] = tool_calls

                return JSONResponse(content={
                    "id": chat_id,
                    "object": "chat.completion",
                    "created": created_ts,
                    "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                })
        except httpx.TimeoutException:
            continue
            
    raise HTTPException(status_code=503, detail="号池全灭！所有 Key 均返回限流或超时。")

if __name__ == "__main__":
    import uvicorn
    print(f"Starting AI Proxy Gateway (Function Calling Enabled)...")
    print(f"  Loaded {len(key_pool.keys)} Keys in Pool")
    print(f"  Model: {DEFAULT_MODEL}")
    print(f"  Listening on: http://0.0.0.0:8000")
    print(f"  Status monitoring: http://localhost:8000/v1/status")
    print()
    uvicorn.run(app, host="0.0.0.0", port=8000)
