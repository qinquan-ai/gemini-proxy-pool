# 🦞 Gemini Proxy Pool

> Lightweight OpenAI-to-Gemini API proxy with automatic multi-key rotation, reactive 429 fallback, and function calling translation.

## ✨ Features

| Feature | Description |
|---------|-------------|
| **Protocol Translation** | Seamlessly converts OpenAI API format to Google Gemini, enabling any OpenAI-compatible client to use Gemini models |
| **Multi-Key Pool** | Round-robin load balancing across multiple Gemini API keys to maximize free-tier quota |
| **Reactive 429 Fallback** | Automatically detects rate-limited keys, marks them for cooldown, and retries with the next healthy key — completely transparent to the client |
| **Function Calling Bridge** | Bi-directional translation of OpenAI `tools` ↔ Gemini `functionDeclarations`, with schema sanitization to strip incompatible fields |
| **Streaming (SSE)** | Full Server-Sent Events support for real-time token streaming |
| **Health Dashboard** | `/v1/status` endpoint for monitoring pool health, per-key request counts, and cooldown timers |

## 🏗️ Architecture

```
┌─────────────┐     OpenAI Format     ┌──────────────────┐     Gemini Format     ┌─────────────┐
│  Any Client  │ ──────────────────▶  │  Gemini Proxy    │ ──────────────────▶   │  Google API  │
│  (OpenClaw,  │                      │    Pool           │    Key Rotation       │  (Gemini 3   │
│   Cursor,    │ ◀──────────────────  │                  │ ◀──────────────────   │   Flash)     │
│   ChatBot)   │     OpenAI Format     │  localhost:8000  │     Gemini Format     │              │
└─────────────┘                       └──────────────────┘                       └─────────────┘
                                              │
                                      ┌───────┴───────┐
                                      │  KeyManager   │
                                      │  ┌─Key 1 ✅─┐ │
                                      │  ├─Key 2 ✅─┤ │
                                      │  ├─Key 3 ⏳─┤ │  ← 429 Cooldown
                                      │  └─Key 4 ✅─┘ │
                                      └───────────────┘
```

## 🚀 Quick Start

### 1. Install

```bash
git clone https://github.com/qinquan-ai/gemini-proxy-pool.git
cd gemini-proxy-pool
pip install fastapi uvicorn httpx python-dotenv
```

### 2. Configure Keys

```bash
cp .env.example .env
# Edit .env and fill in your Gemini API keys
```

### 3. Run

```bash
python main.py
```

### 4. Use

Point any OpenAI-compatible client to `http://localhost:8000/v1`:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3-flash-preview","messages":[{"role":"user","content":"Hello!"}]}'
```

## 📊 Monitoring

Visit `http://localhost:8000/v1/status`:

```json
{
  "total_keys": 4,
  "pool": [
    { "name": "Primary",   "status": "Active",          "success_count": 142 },
    { "name": "Secondary", "status": "Exhausted (429)", "cooldown_remaining_sec": 2847 }
  ]
}
```

## 🔧 API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat endpoint |
| `GET`  | `/v1/models` | List available models |
| `GET`  | `/v1/status` | Key pool health dashboard |

## 📜 License

MIT
