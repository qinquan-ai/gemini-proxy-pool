# 🦞 Gemini Proxy Pool（Gemini 代理号池）

> 轻量级 OpenAI → Gemini 协议翻译代理，支持多密钥自动轮换、429 限流自动降级、函数调用双向翻译。

## ✨ 核心能力

| 能力 | 说明 |
|------|------|
| **协议翻译** | 将 OpenAI 格式无缝转换为 Gemini 格式，任何兼容 OpenAI 的客户端都能直接调用 Gemini |
| **多密钥号池** | 多把 API Key 轮询负载均衡，最大化免费额度利用率 |
| **429 自动降级** | 检测到被限流的 Key 后，自动标记冷却、切换下一把健康 Key 重试，对客户端完全无感 |
| **函数调用桥** | OpenAI `tools` ↔ Gemini `functionDeclarations` 双向翻译，自动清洗不兼容字段 |
| **流式响应** | 完整的 SSE (Server-Sent Events) 实时逐字输出支持 |
| **健康面板** | `/v1/status` 端点实时查看号池健康度、每把 Key 的请求计数和冷却倒计时 |

## 🏗️ 系统架构

```
┌─────────────┐     OpenAI 格式      ┌──────────────────┐     Gemini 格式      ┌─────────────┐
│  任意客户端   │ ──────────────────▶  │  Gemini Proxy    │ ──────────────────▶  │  Google API  │
│  (OpenClaw,  │                      │    Pool           │    密钥轮换          │  (Gemini 3   │
│   Cursor,    │ ◀──────────────────  │                  │ ◀──────────────────  │   Flash)     │
│   飞书Bot)   │     OpenAI 格式      │  localhost:8000  │     Gemini 格式      │              │
└─────────────┘                       └──────────────────┘                      └─────────────┘
                                              │
                                      ┌───────┴───────┐
                                      │  KeyManager   │
                                      │  ┌─密钥1 ✅──┐ │
                                      │  ├─密钥2 ✅──┤ │
                                      │  ├─密钥3 ⏳──┤ │  ← 429 冷却中
                                      │  └─密钥4 ✅──┘ │
                                      └───────────────┘
```

## 🚀 快速上手

### 1. 安装

```bash
git clone https://github.com/qinquan-ai/gemini-proxy-pool.git
cd gemini-proxy-pool
pip install fastapi uvicorn httpx python-dotenv
```

### 2. 配置密钥

创建 `.env` 文件（已被 `.gitignore` 保护，不会泄露）：

```env
GEMINI_KEYS="主号|AIzaSy...,备用号|AIzaSy...,家庭号|AIzaSy..."
```

格式：`名称|密钥` 逗号分隔。建议每把 Key 来自不同 Google 账号，以最大化独立额度。

### 3. 启动

```bash
python main.py
```

```
Starting AI Proxy Gateway (Function Calling Enabled)...
  Loaded 3 Keys in Pool
  Model: gemini-3-flash-preview
  Listening on: http://0.0.0.0:8000
  Status monitoring: http://localhost:8000/v1/status
```

### 4. 使用

将任何 OpenAI 兼容客户端的 Base URL 指向 `http://localhost:8000/v1`：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3-flash-preview","messages":[{"role":"user","content":"你好！"}]}'
```

## 📊 号池监控

浏览器访问 `http://localhost:8000/v1/status`：

```json
{
  "total_keys": 4,
  "pool": [
    { "name": "初号机", "status": "Active",          "success_count": 142 },
    { "name": "母号",   "status": "Active",          "success_count": 138 },
    { "name": "子号1",  "status": "Exhausted (429)", "cooldown_remaining_sec": 2847 },
    { "name": "子号2",  "status": "Active",          "success_count": 141 }
  ]
}
```

## 🔧 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/chat/completions` | OpenAI 兼容的对话补全接口 |
| `GET`  | `/v1/models` | 查询可用模型列表 |
| `GET`  | `/v1/status` | 号池健康状态面板 |

## 📜 许可证

MIT
