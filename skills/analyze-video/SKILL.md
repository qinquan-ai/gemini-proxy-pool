---
name: analyze-video
description: Analyze public video links (Douyin, YouTube, Bilibili, Web) or local media using StudioKey's Gemini multi-key pool with automated curation archiving and zero-disk RAM streaming. Defaults to general video analysis.
---

# StudioKey Video Analysis Workflow

Analyze public video links (Douyin, YouTube, Bilibili, Web) or local media files using StudioKey's local `studiokey-video` MCP tools (`analyze_video`, `submit_video_analysis`, `get_video_analysis`).

---

## 🎯 Core Capabilities

1. **Universal Network Media Sniffer**:
   - Playwright-backed background media extraction bypassing MSE `blob:` mechanisms and anti-scraping blocks to grab direct CDN streams.
2. **RAM vs. Disk Dual-Tier Pipeline (SSD-Safe)**:
   - **< 50MB (Short clips)**: 100% in-memory streaming (`io.BytesIO`) pushed directly to Google Files API (`0 ROM write`).
   - **>= 50MB (Long videos / HD)**: Seamless overflow to chunked disk streaming, rigorously cleaned up in `finally` blocks.
3. **Auto-Cleanup (Ephemeral Cloud Storage)**:
   - Immediately deletes remote Google Files API temporary files upon analysis completion.
4. **Flexible Multimodal Modes (Default: General)**:
   - **`general` (Default)**: Multimodal scene breakdown, transcript extraction, narrative rhythm, and factual analysis.
   - **`curation` (Optional)**: Short video curation funnel with deduplication and quality assessment. When `archive_dir` and `save_threshold` (default 7.0) are passed, qualifies and saves high-scoring media alongside JSON reports.
   - **`remotion` / `vlog` / `vox` / `technical` / `transcript`**: Specialized modes for programmatic code synthesis, narrative breakdowns, or transcripts.

---

## 🛠️ MCP Tool Invocation

### 1. Synchronous Analysis (`analyze_video`)
```python
result = await call_tool(
    "studiokey-video",
    "analyze_video",
    {
        "source": "https://www.youtube.com/watch?v=xxxxxx", # Supports URLs, share tokens, local files
        "analysis_type": "general",                          # Default is "general"; optional: "curation", "vlog", "remotion"
        "model": "gemini-3.5-flash-lite",                    # Recommended high-quota tier (500 RPD)
        # Optional archiving (primarily for curation mode):
        # "archive_dir": "D:/project_qin/Video_project/curation_pool",
        # "save_threshold": 7.0
    }
)
```

### 2. Asynchronous Job Submission (`submit_video_analysis` & `get_video_analysis`)
For long videos or batch queue processing:
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

# Poll status
status = await call_tool("studiokey-video", "get_video_analysis", {"job_id": job_id})
# Progress stages: "downloading" -> "uploading" -> "processing" -> "analyzing" -> "completed"
```

---

## ⚡ CLI & Operations

- **9-Worker Batch Engine**:
  ```bash
  python -m app.cli batch --input "data/video_list.json" --concurrency 9 --interval 2.0
  ```
- **Quota Snapshot Inspection**:
  ```bash
  python -m app.cli snapshot
  ```

---

## ⚠️ Guidelines

1. **Default Mode**: Always default to `general` unless the user explicitly specifies curation or viral short video funnels.
2. **Never leave temporary files**: Handled transparently by `VideoAnalysisService`.
3. **Share Tokens**: Raw clipboard share tokens (e.g. Douyin mobile share strings) are automatically parsed by regex into canonical URLs.
