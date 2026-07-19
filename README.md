# Gemini Proxy Pool

本地优先的 OpenAI → Gemini 协议网关，提供多 Key 轮询、健康冷却、错误分类、流式输出、函数调用和可视化监控。

## 能力边界

- Key 轮询用于合法的多项目容量分配和故障切换。
- Gemini 配额按 Google Cloud Project 计算。同一 Project 下的多把 Key 不会获得多份额度。
- 当前支持文本、函数调用、base64 内联图片/音频，以及客户端主动提供的 Gemini File URI。
- 长视频上传与 Files API 任务队列尚未实现。

## 快速开始

```powershell
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

默认服务地址：

- 仪表盘：`http://127.0.0.1:8000`
- OpenAI 兼容接口：`http://127.0.0.1:8000/v1`
- 健康检查：`http://127.0.0.1:8000/healthz`
- API 文档：`http://127.0.0.1:8000/docs`

仪表盘采用零构建的组件化 UI：`styles/tokens.css` 维护主题令牌，
`ui/components.js` 提供可复用状态与指标组件，`dashboard.js` 只负责数据编排。
支持明暗主题、手动刷新、自动刷新暂停和移动端表格滚动。

## 配置四个独立项目 Key

```env
GEMINI_KEYS="账号1|AIzaSy...,账号2|AIzaSy...,账号3|AIzaSy...,账号4|AIzaSy..."
GEMINI_DEFAULT_MODEL="gemini-2.5-flash"
GEMINI_MODELS="gemini-2.5-flash,gemini-3-flash-preview"
```

只有当四把 Key 属于四个独立 Cloud Project 时，才可能对应四套项目配额。请遵守 Google 的服务条款和项目配额规则。

## 安全默认值

服务默认只绑定 `127.0.0.1`。如果需要监听局域网地址，必须先设置强随机 Token：

```env
PROXY_HOST="0.0.0.0"
PROXY_API_TOKEN="replace-with-a-long-random-token"
```

客户端随后发送：

```http
Authorization: Bearer replace-with-a-long-random-token
```

不要提交 `.env`，不要把真实 Key 放在 URL、日志、截图或前端代码中。

## 号池行为

- 健康 Key 使用线程安全的 round-robin 分配。
- `429` 优先采用 Google 返回的 `Retry-After` / `retryDelay`。
- `401` 会禁用无效 Key，直到服务重启。
- `403` 进入短期权限冷却，不永久误伤整把 Key。
- 网络错误和 Google `5xx` 使用短暂冷却并切换下一把 Key。
- 客户端 `400/404` 不会降低 Key 健康度。
- 全部 Key 冷却或禁用时返回 `503`，不会继续请求已耗尽 Key。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 可视化号池仪表盘 |
| `GET` | `/healthz` | 轻量健康检查 |
| `GET` | `/v1/status` | 号池、请求、失败、冷却与在途统计 |
| `GET` | `/v1/models` | 配置允许的模型列表 |
| `POST` | `/v1/chat/completions` | OpenAI 兼容对话接口 |
| `POST` | `/v1/responses` | OpenAI Responses 兼容接口（无服务端历史存储） |

`/v1/responses` 会复用同一套 Gemini 翻译、Key 轮询和故障切换逻辑。
客户端必须发送完整上下文；当前不支持通过 `previous_response_id` 在网关端恢复历史。
当前只转发函数工具；`web_search`、`file_search`、`computer` 等 OpenAI 托管工具不会伪装成已支持。

## 网关调用链

```text
Codex / OpenCode / Aider
        │ OpenAI Chat 或 Responses 请求
        ▼
FastAPI 鉴权与模型校验
        ▼
协议适配器（OpenAI → Gemini）
        ▼
KeyManager 轮询健康 Key
        ▼
Gemini generateContent / streamGenerateContent
        ▼
错误分类、冷却、自动切换下一 Key
        ▼
OpenAI Chat / Responses 格式返回客户端
```

它目前属于单厂商 Gemini 网关，不是完整的多厂商路由器。所有入口共享一个 Key 池，
因此不会因为增加客户端而绕过项目配额，只会获得统一的故障切换和协议兼容。

## CLI 接入

OpenCode 和 Aider 可直接使用 `/v1/chat/completions`。Codex 使用
`/v1/responses`，并且必须采用完整上下文模式。

Codex `config.toml` 示例：

```toml
model = "gemini-2.5-flash"
model_provider = "studiokey"

[model_providers.studiokey]
name = "StudioKey Gemini Gateway"
base_url = "http://127.0.0.1:8000/v1"
wire_api = "responses"
```

如果启用了 `PROXY_API_TOKEN`，再增加 `env_key = "STUDIOKEY_API_TOKEN"`，并把
Token 放进同名环境变量。不要把真实 Gemini Key 配进客户端；客户端只连接网关。

调用示例：

```powershell
$body = @{
  model = "gemini-2.5-flash"
  messages = @(@{role = "user"; content = "你好"})
  stream = $false
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Uri http://127.0.0.1:8000/v1/chat/completions `
  -Method Post `
  -ContentType application/json `
  -Body $body
```

## 客户端集成与配置指引

你可以非常方便地将本代理池集成到常用客户端中：

### 1. Cursor 配置
* 进入 Cursor 的 `Settings` -> `Models` 菜单。
* 在 `Override OpenAI Base URL` 一栏中填写：
  ```text
  http://localhost:8000/v1
  ```
* API Key 填写任意字符（如 `sk-gemini-pool`）。
* 在支持的模型列表中，确保添加并勾选了您在 `.env` 里开启的模型（如 `gemini-2.5-flash`、`gemini-3-flash-preview`）。

### 2. LobeChat 配置
* 进入 `设置` -> `语言模型` -> `OpenAI`。
* 开启**自定义接口地址**，并填写：
  ```text
  http://localhost:8000/v1
  ```
* API Key 填写任意字符（如 `sk-gemini`）。
* 保存后点击连接测试，并在模型列表中选择或添加目标模型。

### 3. NextChat (ChatGPT Next Web) 配置
* 打开设置，在 **API 接口地址** 中填写：
  ```text
  http://localhost:8000/v1
  ```
* **API Key** 填写任意字符。
* 自定义模型一栏中勾选并启用代理所支持的模型。

### 4. Cherry Studio 配置
* 添加自定义模型提供商（选择 `OneAPI` 或 `OpenAI` 兼容格式）。
* API 地址填入 `http://localhost:8000/v1`。
* 密钥填写任意字符，添加对应的模型名即可开始使用。

## 测试


离线单元测试不会调用 Google，也不会消耗 Key：

```powershell
$env:PYTHONDONTWRITEBYTECODE="1"
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

`test_pool.py` 是显式的在线冒烟测试，会真实消耗额度，不属于默认测试流程。

## 下一阶段

1. Files API 视频上传、处理轮询和同 Key 任务亲和
2. `analyze_video` MCP 工具
3. 每模型独立的 Key 健康与配额状态
4. SQLite 指标持久化和重启恢复

许可证：MIT
