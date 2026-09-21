---
name: analyze-video
description: 全网公网视频链接 (抖音、YouTube、B站、网页) 或本地多媒体深度解析 Skill。基于 StudioKey 的 Gemini 多 Key 轮换池与零磁盘 RAM 流式切片机制，支持默认 general 通用镜头/叙事拆解及可选选品自动归档。
---

# StudioKey Video Analysis Workflow (通用多模态视频深度解析工作流)

本 Skill 用于调用 StudioKey 本地网关提供的 `studiokey-video` MCP 工具集（`analyze_video`、`submit_video_analysis`、`get_video_analysis`），对公网音视频（抖音、YouTube、B站、通用网页）或本地多媒体文件进行高精度多模态分析。

---

## 🎯 核心能力与机制设计

1. **全网嗅探对抗 (Universal Video Sniffer)**:
   - 针对主流视频平台反爬与 MSE `blob:` 播放机制，Playwright 后台静默抓取网络层真实 CDN 媒体流，彻底避免下载失败或验证码阻断。
2. **RAM vs ROM 双轨流转 (保护系统固态寿命)**:
   - **< 50MB (短视频/音视频片段)**: 全内存流式传输 (`io.BytesIO`)，直接推送到 Google Files API，本地零磁盘写入 (`0 ROM write`)。
   - **>= 50MB (长视频/高清原片)**: 自动平滑溢出至磁盘分块写入，并在任务处理完成后的 `finally` 中严格清理临时文件。
3. **云端即焚 (Google Files API Auto-Cleanup)**:
   - 分析一旦完成，底层 Python `finally` 块立即发送 `DELETE` 彻底销毁 Google 云端临时文件，不留云端残余。
4. **灵活多模态模式体系 (默认 General 通用模式)**:
   - **`general` (默认)**: 通用多模态事实与镜头拆解、音视频转录、叙事节奏与关键事件还原。
   - **`curation` (选品模式)**: 海外短剧/爆款选品漏斗与平台去重质检。传入 `archive_dir` 与 `save_threshold` (默认 7.0 分) 时自动归档优质素材与 JSON 报告。
   - **`remotion` / `vlog` / `vox` / `technical` / `transcript`**: 针对代码合成、特定叙事或纯转写的专业模式。

---

## 🛠️ MCP 工具调用规范

### 1. 同步长轮询分析 (`analyze_video`)
```python
result = await call_tool(
    "studiokey-video",
    "analyze_video",
    {
        "source": "https://v.douyin.com/xxxxxx/", # 支持抖音口令/短链、URL、YouTube、B站、本地文件
        "analysis_type": "general",                # 默认通用模式；可选: "curation", "remotion", "vlog", "transcript"
        "model": "gemini-3.5-flash-lite",          # 推荐优先使用 500 RPD 高配模型
        # 可选归档配置 (一般仅在选品模式使用):
        # "archive_dir": "D:/project_qin/Video_project/curation_pool",
        # "save_threshold": 7.0
    }
)
```

### 2. 异步投递与进度监控 (`submit_video_analysis` & `get_video_analysis`)
对于长视频、大批量或后台并发任务：
```python
job = await call_tool(
    "studiokey-video",
    "submit_video_analysis",
    {
        "source": "https://www.bilibili.com/video/BVxxxxxx",
        "analysis_type": "general",
    }
)
job_id = job["id"]

# 定期轮询任务进度
status = await call_tool("studiokey-video", "get_video_analysis", {"job_id": job_id})
# 阶段流转: "downloading" -> "uploading" -> "processing" -> "analyzing" -> "completed"
```

---

## ⚡ 高并发批量分析与配额健康校准 (CLI & 运维)

除了单视频 MCP 工具调用外，AI Agent 可推荐或调用 StudioKey CLI 进行超大规模吞吐与状态校验：

1. **9 并发极速批处理**:
   ```bash
   python -m app.cli batch --input "data/video_list.json" --concurrency 9 --interval 2.0
   ```
   - 自动按 3 账号 × 3 模型矩阵分配并发线程，受 2.0s 间隔保护，严格恪守 15 RPM 上限。

2. **Google AI Studio 真实配额快照与长图校准**:
   ```bash
   python -m app.cli snapshot
   ```
   - 免登持久化启动可视 Chrome，抓取 44 个模型实时 RPM/RPD，生成 6000+ 像素长截图与结构化 JSON。

---

## ⚠️ 避坑铁律

1. **默认使用 `general` 模式**：除非用户明确提出选品搬运或短剧筛选需求，否则始终默认保持通用分析。
2. **切勿在分析后手动遗留临时文件**：临时文件由网关 `VideoAnalysisService` 集中流转，无需手动清理。
3. **支持平台口令直接粘贴**：用户直接输入附带多余文本的平台分享口令时，网关正则解析器会自动提取短链并解析重定向。
