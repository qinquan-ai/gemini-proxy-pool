# Gemini Proxy Pool

本地优先的 OpenAI → Gemini 协议网关，提供多 Key 轮询、健康冷却、错误分类、流式输出、函数调用和可视化监控。

## 能力边界

- Key 轮询用于合法的多项目容量分配和故障切换。
- Gemini 配额按 Google Cloud Project 计算。同一 Project 下的多把 Key 不会获得多份额度。
- 请求、成功/失败、429、冷却和 Gemini 返回的 Token 用量会按 Key 指纹持久化到 `data/key_pool_state.json`；状态文件不保存真实 Key。
- 支持文本、函数调用、base64 内联图片/音频，以及客户端主动提供的 Gemini File URI。
- 视频 MCP 支持 YouTube、抖音分享文案/短链、哔哩哔哩、公开网页和本地视频，任务结果持久化保存。

## 快速开始

```powershell
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

默认服务地址：

- 仪表盘：`http://127.0.0.1:8008`
- OpenAI 兼容接口：`http://127.0.0.1:8008/v1`
- 健康检查：`http://127.0.0.1:8008/healthz`
- API 文档：`http://127.0.0.1:8008/docs`

仪表盘采用零构建的组件化 UI：`styles/tokens.css` 维护主题令牌，
`ui/components.js` 提供可复用状态与指标组件，`dashboard.js` 只负责数据编排。
支持明暗主题、手动刷新、自动刷新暂停和移动端表格滚动。

## 配置四个独立项目 Key 与模型推荐

```env
GEMINI_KEYS="账号1|AIzaSy...,账号2|AIzaSy...,账号3|AIzaSy...,账号4|AIzaSy..."
# 推荐高频/批量默认模型（每日 500 次配额天花板）：
GEMINI_DEFAULT_MODEL="gemini-3.5-flash-lite"
GEMINI_MODELS="gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash"
```

只有当四把 Key 属于四个独立 Cloud Project 时，才可能对应四套项目配额。请遵守 Google 的服务条款和项目配额规则。

### 免费额度分级与选型指南（重要）

Google AI Studio 免费层对不同模型配额差异巨大，选错模型极易触发 429。详见 [全模型官方配额清单](docs/gemini_rate_limits_status.md)：

1. **第一梯队·额度天花板（强烈推荐作为默认模型）**：
   - **`gemini-3.5-flash-lite`** / **`gemini-3.1-flash-lite`**：**500 RPD (每日 500 次)**，15 RPM，支持多模态与音频，高频对话与批量任务首选。
2. **第二梯队·旗舰大模型（长文本与复杂多模态）**：
   - **`gemini-3.8-flash`** / **`gemini-3.7-flash`** / **`gemini-3.6-flash`** / **`gemini-3.5-flash`**：**20 RPD (每日仅 20 次)**，5 RPM，额度较为紧俏，建议留给深度视频分析或关键复杂推理。（注：`gemini-2.5-flash` 官方已下线不可用）
3. **第三梯队·超大并发纯文本**：
   - **`gemma-4-31b-it`** / **`gemma-4-26b-a4b-it`**：**14,400 RPD (每日 1.44 万次)**，30 RPM，轻量文本任务无限畅享。

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
| `POST` | `/v1beta/models/{model}:generateContent` | Gemini 原生协议网关，用于 Gemini CLI 主模型 |
| `POST` | `/v1beta/models/{model}:streamGenerateContent` | Gemini 原生流式协议网关 |
| `POST` | `/mcp` | Streamable HTTP MCP（视频分析工具） |

`/v1/responses` 会复用同一套 Gemini 翻译、Key 轮询和故障切换逻辑。
客户端必须发送完整上下文；当前不支持通过 `previous_response_id` 在网关端恢复历史。
当前只转发函数工具；`web_search`、`file_search`、`computer` 等 OpenAI 托管工具不会伪装成已支持。
Gemini 3 工具调用的加密 thought signature 会由网关按 call ID 短期保存在内存并自动回填，
兼容无法透传该非标准字段的 OpenAI-compatible 客户端。
Gemini CLI 0.51 可保留 `gemini-api-key` 认证模式，并设置
`GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8008`，把内置主模型请求送入同一
4-Key 池；StudioKey 会忽略客户端占位 Key 并使用池中 Key。普通 CLI 不接受其内部的
`gateway` 认证类型，直接选择该类型会报 `Invalid auth method selected.`。
原生协议路径无需 OpenAI 格式转换。

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
base_url = "http://127.0.0.1:8008/v1"
wire_api = "responses"
```

如果启用了 `PROXY_API_TOKEN`，再增加 `env_key = "STUDIOKEY_API_TOKEN"`，并把
Token 放进同名环境变量。不要把真实 Gemini Key 配进客户端；客户端只连接网关。

## 视频分析 MCP

MCP 地址：`http://127.0.0.1:8008/mcp/`

提供四个工具：

- `submit_video_analysis`：后台提交 YouTube URL、抖音/B站链接或分享文案、公开网页、本地视频、Gemini File URI 或 `gs://` URI。
- `get_video_analysis`：查询上传、处理、分析进度并获取结构化结果。
- `cancel_video_analysis`：取消运行中的 Job。
- `analyze_video`：阻塞等待并通过 MCP progress 报告阶段，适合短视频。

分析模式包括 `remotion`、`vox`、`vlog`、`technical`、`transcript` 和 `general`。
本地视频使用 Files API，上传、处理轮询和分析保持同一 Key 亲和。Job 与完成结果持久化到
`data/video_jobs/`；服务重启后可继续查询已完成任务，重启时仍在运行的任务会明确标记失败。

抖音链接采用在线来源适配：先尝试 `yt-dlp`，需要新鲜 Cookie 时自动启动隔离的无头
Chrome 打开页面并取得真实媒体流。下载文件只存在于 `data/video_downloads/` 的任务临时目录，
成功、失败或取消后都会清理。上传前会校验媒体文件头，验证码 HTML 不会被误传到 Files API。

B站与公开网页复用同一远程解析器。B站的分离音视频流由 `ffmpeg` 合并；公开网页先使用
`yt-dlp`，失败后再从隔离浏览器的 `<video>` 元素取得直接媒体流。仅允许公网 HTTP(S)
地址，并对初始域名、浏览器媒体地址及每次重定向执行内网/本机地址拦截。登录、会员、
地区限制、验证码和 DRM 内容不会绕过站点权限。

默认工作流是提交后继续处理代码库与素材规划，再调用查询工具，避免长连接和高频轮询。
这与实验性的 MCP Tasks 扩展语义一致，但 `submit/get/cancel` 可兼容尚未实现 Tasks 扩展的客户端。

### 登录自动启动

安装当前用户的登录启动任务：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
```

任务会启动唯一的 `127.0.0.1:8008` 网关；如果健康检查发现已有实例则不会重复启动。
移除自动启动使用 `scripts/uninstall_autostart.ps1`。

调用示例：

```powershell
$body = @{
  model = "gemini-2.5-flash"
  messages = @(@{role = "user"; content = "你好"})
  stream = $false
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Uri http://127.0.0.1:8008/v1/chat/completions `
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
  http://localhost:8008/v1
  ```
* API Key 填写任意字符（如 `sk-gemini-pool`）。
* 在支持的模型列表中，确保添加并勾选了您在 `.env` 里开启的模型（如 `gemini-2.5-flash`、`gemini-3-flash-preview`）。

### 2. LobeChat 配置
* 进入 `设置` -> `语言模型` -> `OpenAI`。
* 开启**自定义接口地址**，并填写：
  ```text
  http://localhost:8008/v1
  ```
* API Key 填写任意字符（如 `sk-gemini`）。
* 保存后点击连接测试，并在模型列表中选择或添加目标模型。

### 3. NextChat (ChatGPT Next Web) 配置
* 打开设置，在 **API 接口地址** 中填写：
  ```text
  http://localhost:8008/v1
  ```
* **API Key** 填写任意字符。
* 自定义模型一栏中勾选并启用代理所支持的模型。

### 4. Cherry Studio 配置
* 添加自定义模型提供商（选择 `OneAPI` 或 `OpenAI` 兼容格式）。
* API 地址填入 `http://localhost:8008/v1`。
* 密钥填写任意字符，添加对应的模型名即可开始使用。

## 高级运维与自动化 CLI 工具

StudioKey 提供了一套专用的 CLI 工具集，支持可视化配额看板校准、微信式超清长截图和高并发批量视频分析：

### 1. AI Studio 真实配额看板与微信式无缝长截图 (`snapshot`)

自动启动独立可视 Chrome，复用免登 Profile 导航至 Google AI Studio 配额看板，抓取 44 个模型的真实指标并无缝拼接长图：

```powershell
python -m app.cli snapshot
```

- **数据作用与核心价值**：
  1. **基准校准参考 (Reference Benchmark)**：AI Studio 数据通常有 15 分钟延迟，但它是 Google 云端令牌桶最权威的基准，用于校验 StudioKey 本地滑动窗口 Rate Limiter 的准确性。
  2. **硬指标避让机制 (Hard Quota Avoidance)**：导出的结构化 JSON（`logs/snapshots/ai_studio_quota_*.json`）可直接被代理池流控器读取。当检测到某个模型（例如 `Gemini 2.5 Flash` 达到 80% 警告线）时，调度器自动降低该模型权重，无缝把请求分流至配额充沛的 `Gemini 3 Flash` 或 `Gemini 3.5 Flash`。
  3. **AI Agent 容量规划 (Agent Capacity Planning)**：AI Agent 批量调度前读取此快照，动态规划并发上限，避免盲目超频导致账号被禁。
- **产物文件**：
  - `logs/snapshots/ai_studio_quota_<timestamp>_long.png`：无缝拼接的 6000+ 像素超高清微信式长截图。
  - `logs/snapshots/ai_studio_quota_<timestamp>.json`：44 个模型结构化 RPM/RPD 指标。
  - `logs/snapshots/ai_studio_network_rpc_<timestamp>.json`：拦截到的后台 Quota RPC 原生响应包。

### 2. 9 并发极速视频深度分析引擎 (`batch`)

采用“多账号 × 多模型”矩阵调度，以 9 并发全速吞吐分析短视频素材：

```powershell
python -m app.cli batch --input "data/douyin_search.json" --concurrency 9 --interval 2.0
```

- **参数说明**：
  - `--input`：包含视频 URL 或 Aweme 列表的 JSON 文件。
  - `--concurrency`：并发工作线程数（默认 9 并发，对应 3 个账号 × 3 个主力模型）。
  - `--interval`：单账号请求保护间隔（默认 2.0 秒，严格恪守 15 RPM 上限）。

---

## 测试

离线单元测试不会调用 Google，也不会消耗 Key：

```powershell
$env:PYTHONDONTWRITEBYTECODE="1"
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

`test_pool.py` 是显式的在线冒烟测试，会真实消耗额度，不属于默认测试流程。

许可证：MIT
