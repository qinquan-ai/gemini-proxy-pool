# Gemini Proxy Pool

A local-first OpenAI-to-Gemini gateway with multi-key round-robin routing, health cooldowns, error classification, streaming, function calling, multimodal inline data, and an operational dashboard.

## Scope

- Rotation is intended for legitimate multi-project capacity and failover.
- Gemini quotas are enforced per Google Cloud Project. Multiple keys from one project do not create multiple quotas.
- Text, function calls, base64 inline image/audio, and caller-provided Gemini File URIs are supported.
- The video MCP accepts YouTube, Douyin share text/short links, Bilibili, public video pages, and local files, with persisted background jobs.
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
- `POST /mcp` (Streamable HTTP video-analysis MCP)

The Responses adapter supports function tools. OpenAI-hosted tools and
`previous_response_id` storage are intentionally rejected instead of being silently ignored.

The MCP exposes `submit_video_analysis`, `get_video_analysis`,
`cancel_video_analysis`, and `analyze_video`. Douyin pages are resolved through
yt-dlp with an isolated headless-Chrome fallback, then verified before Files API
upload. Job records and completed results are persisted under `data/video_jobs/`;
temporary media is cleaned after success, failure, or cancellation.

Configure independent project keys:

```env
GEMINI_KEYS="account-1|AIzaSy...,account-2|AIzaSy...,account-3|AIzaSy...,account-4|AIzaSy..."
GEMINI_DEFAULT_MODEL="gemini-3-flash-preview"
GEMINI_MODELS="gemini-3-flash-preview"
```

The server binds to `127.0.0.1` by default. Set a strong `PROXY_API_TOKEN` before changing `PROXY_HOST` to `0.0.0.0`.

## Offline tests

```powershell
$env:PYTHONDONTWRITEBYTECODE="1"
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

`test_pool.py` is a live smoke test and consumes real quota.

## Next

1. Per-model key health and quota state
2. Persistent metrics
3. Native MCP Tasks when client support is broadly available

License: MIT
