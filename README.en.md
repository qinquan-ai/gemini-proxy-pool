# Gemini Proxy Pool

A local-first OpenAI-to-Gemini gateway with multi-key round-robin routing, health cooldowns, error classification, streaming, function calling, multimodal inline data, and an operational dashboard.

## Scope

- Rotation is intended for legitimate multi-project capacity and failover.
- Gemini quotas are enforced per Google Cloud Project. Multiple keys from one project do not create multiple quotas.
- Text, function calls, base64 inline image/audio, and caller-provided Gemini File URIs are supported.
- Long-video upload and the Files API job queue are not implemented yet.
- Chat Completions and stateless Responses endpoints share the same key pool.

## Start

```powershell
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

Open `http://127.0.0.1:8000` for the dashboard and use `http://127.0.0.1:8000/v1` as the OpenAI-compatible base URL.

Available compatibility endpoints:

- `POST /v1/chat/completions`
- `POST /v1/responses` (stateless; send full history)

The Responses adapter supports function tools. OpenAI-hosted tools and
`previous_response_id` storage are intentionally rejected instead of being silently ignored.

Configure independent project keys:

```env
GEMINI_KEYS="account-1|AIzaSy...,account-2|AIzaSy...,account-3|AIzaSy...,account-4|AIzaSy..."
GEMINI_DEFAULT_MODEL="gemini-2.5-flash"
GEMINI_MODELS="gemini-2.5-flash,gemini-3-flash-preview"
```

The server binds to `127.0.0.1` by default. Set a strong `PROXY_API_TOKEN` before changing `PROXY_HOST` to `0.0.0.0`.

## Offline tests

```powershell
$env:PYTHONDONTWRITEBYTECODE="1"
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

`test_pool.py` is a live smoke test and consumes real quota.

## Next

1. Gemini Files API video jobs with per-job key affinity
2. An `analyze_video` MCP tool
3. Per-model key health and quota state
4. Persistent metrics

License: MIT
