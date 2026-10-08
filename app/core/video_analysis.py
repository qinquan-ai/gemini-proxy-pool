import asyncio
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import httpx
import json_repair

logger = logging.getLogger(__name__)

# Clean defective no_proxy entries (e.g. ::1 on Windows causing httpx InvalidURL: Invalid port: ':1')
no_proxy_env = os.environ.get("no_proxy", "")
if "::1" in no_proxy_env:
    cleaned = re.sub(r',?::1(?:/\d+)?', '', no_proxy_env).strip(',')
    os.environ["no_proxy"] = cleaned

from app.core.key_manager import KeyManager
from app.core.video_sources import VideoSource, parse_video_source, validate_public_url
from app.core.video_downloader import (
    VideoDownloader,
    VideoDownloadError,
    validate_video_file,
    validate_video_bytes,
    find_browser_executable,
)


SUPPORTED_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/mpeg",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-flv",
    "video/webm",
    "video/x-ms-wmv",
    "video/3gpp",
}
ANALYSIS_TYPES = {"general", "curation", "remotion", "vox", "vlog", "technical", "transcript", "transflow_stage1"}

GENERAL_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "content_type": {"type": "string"},
        "language": {"type": "string"},
        "main_topics": {"type": "array", "items": {"type": "string"}},
        "visual_description": {"type": "string"},
        "audio_description": {"type": "string"},
        "timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "visual": {"type": "string"},
                    "audio": {"type": "string"},
                    "caption": {"type": "string"},
                },
                "required": ["start", "end", "visual"],
            },
        },
        "confidence_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "visual_description", "audio_description", "timeline"],
}

CURATION_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "plot_and_characters": {
            "type": "object",
            "properties": {
                "main_plot": {"type": "string"},
                "key_characters": {"type": "array", "items": {"type": "string"}},
                "conflict_and_twists": {"type": "string"},
                "emotional_hook": {"type": "string"},
            },
            "required": ["main_plot", "conflict_and_twists"],
        },
        "watermark_and_quality": {
            "type": "object",
            "properties": {
                "has_platform_watermark": {"type": "boolean"},
                "has_author_watermark": {"type": "boolean"},
                "watermark_details": {"type": "string"},
                "subtitle_obstruction": {"type": "string"},
                "visual_clarity": {"type": "string"},
                "audio_clarity": {"type": "string"},
                "flaws_or_policy_risks": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["has_platform_watermark", "has_author_watermark", "visual_clarity"],
        },
        "tiktok_potential": {
            "type": "object",
            "properties": {
                "opening_hook_score": {"type": "integer"},
                "hook_description": {"type": "string"},
                "climax_timestamp": {"type": "string"},
                "suggested_english_title": {"type": "string"},
                "recommended_tags": {"type": "array", "items": {"type": "string"}},
                "curation_score": {"type": "number"},
                "curation_recommendation": {"type": "string"},
            },
            "required": ["curation_score", "curation_recommendation"],
        },
        "timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "visual": {"type": "string"},
                    "audio": {"type": "string"},
                    "caption": {"type": "string"},
                },
                "required": ["start", "end", "visual"],
            },
        },
    },
    "required": [
        "summary",
        "plot_and_characters",
        "watermark_and_quality",
        "tiktok_potential",
        "timeline",
    ],
}

REMOTION_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "content_type": {"type": "string"},
        "language": {"type": "string"},
        "timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "visual": {"type": "string"},
                    "audio": {"type": "string"},
                    "caption": {"type": "string"},
                    "motion": {"type": "string"},
                    "transition": {"type": "string"},
                },
                "required": ["start", "end", "visual"],
            },
        },
        "visual_style": {
            "type": "object",
            "properties": {
                "palette": {"type": "array", "items": {"type": "string"}},
                "typography": {"type": "string"},
                "composition": {"type": "string"},
            },
        },
        "audio_style": {
            "type": "object",
            "properties": {
                "music": {"type": "string"},
                "sound_effects": {"type": "string"},
                "voiceover": {"type": "string"},
            },
        },
        "reusable_patterns": {"type": "array", "items": {"type": "string"}},
        "remotion_plan": {
            "type": "object",
            "properties": {
                "fps": {"type": "integer"},
                "dimensions": {"type": "string"},
                "compositions": {"type": "array", "items": {"type": "string"}},
                "components": {"type": "array", "items": {"type": "string"}},
                "assets": {"type": "array", "items": {"type": "string"}},
                "difficulties": {"type": "array", "items": {"type": "string"}},
            },
        },
        "confidence_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "timeline", "reusable_patterns", "remotion_plan"],
}

# TransFlow Stage 1 出海短视频重构专用结构化契约：四权分立（字幕带 / 画中字 / 对白Cue / 物理标记点 / 分镜）
TRANSFLOW_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "duration_s": {"type": "number"},
        "content_type": {"type": "string"},
        "chinese_subtitle_band": {
            "type": "object",
            "properties": {
                "detected": {"type": "boolean"},
                "ymin": {"type": "number"},
                "xmin": {"type": "number"},
                "ymax": {"type": "number"},
                "xmax": {"type": "number"},
                "confidence": {"type": "number"},
                "description": {"type": "string"},
            },
            "required": ["detected"],
        },
        "in_screen_titles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "start_ms": {"type": "integer"},
                    "end_ms": {"type": "integer"},
                    "zh_text": {"type": "string"},
                    "en_text": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ["title", "sticker_text", "floating_dialog", "scene_text"],
                    },
                    "bbox": {
                        "type": "object",
                        "properties": {
                            "ymin": {"type": "number"},
                            "xmin": {"type": "number"},
                            "ymax": {"type": "number"},
                            "xmax": {"type": "number"},
                        },
                        "required": ["ymin", "xmin", "ymax", "xmax"],
                    },
                },
                "required": ["id", "start_ms", "end_ms", "zh_text", "en_text", "category", "bbox"],
            },
        },
        "cues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "start_ms": {"type": "integer"},
                    "end_ms": {"type": "integer"},
                    "zh_text": {"type": "string"},
                    "en_text": {"type": "string"},  # 英文本土化翻译
                    "is_narrator": {"type": "boolean"},  # 影视解说旁白标记
                    "speaker": {"type": "string"},  # 短剧角色名
                    "confidence": {"type": "number"},
                },
                "required": ["id", "start_ms", "end_ms", "zh_text", "en_text"],
            },
        },
        "special_points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "point_type": {"type": "string"},
                    "start_s": {"type": "number"},
                    "end_s": {"type": "number"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "bbox": {
                        "type": "object",
                        "properties": {
                            "ymin": {"type": "number"},
                            "xmin": {"type": "number"},
                            "ymax": {"type": "number"},
                            "xmax": {"type": "number"},
                        },
                    },
                },
                "required": ["id", "point_type", "start_s", "end_s"],
            },
        },
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene_index": {"type": "integer"},
                    "start_s": {"type": "number"},
                    "end_s": {"type": "number"},
                    "visual_description": {"type": "string"},
                    "content_safety": {
                        "type": "string",
                        "enum": ["safe_for_flip", "has_face", "has_text", "unknown"]
                    },
                    "dedup_action": {
                        "type": "object",
                        "properties": {
                            "scale_factor": {
                                "type": "number",
                                "minimum": 1.035,
                                "maximum": 1.065,
                                "description": "空间微缩放系数，正脸镜头选小值(1.035-1.04)，环境镜头选大值(1.055-1.065)"
                            },
                            "stretch_y": {
                                "type": "number",
                                "minimum": 1.03,
                                "maximum": 1.08,
                                "description": "纵向拉伸系数"
                            },
                            "speed": {
                                "type": "number",
                                "minimum": 1.015,
                                "maximum": 1.035,
                                "description": "微变速系数"
                            },
                            "allow_flip": {
                                "type": "boolean",
                                "description": "是否允许水平翻转（检测到正脸/文字则为false）"
                            },
                            "reason": {
                                "type": "string",
                                "description": "AI判断依据：为什么选择这些参数"
                            }
                        },
                        "required": ["scale_factor", "stretch_y", "speed", "allow_flip", "reason"]
                    }
                },
                "required": ["scene_index", "start_s", "end_s", "visual_description", "content_safety", "dedup_action"]
            }
        },
    },
    "required": ["duration_s", "chinese_subtitle_band", "cues", "special_points", "scenes"],
}

# Alias for backwards compatibility
ANALYSIS_SCHEMA = REMOTION_ANALYSIS_SCHEMA

STYLE_INSTRUCTIONS = {
    "general": "Provide an objective, comprehensive audio-visual and narrative analysis of the content without assuming any code generation or Remotion intent.",
    "curation": "Analyze for short drama/video repurposing and curation (e.g. TikTok/Shorts). Focus on opening hook (0-3s), plot twists, character conflicts, watermark presence, visual/audio clarity, and provide a 1-10 curation score.",
    "remotion": "Prioritize timing, reusable React components, assets, typography, transitions, and Remotion implementation difficulty.",
    "vox": "Prioritize editorial structure, evidence reveals, maps, charts, kinetic typography, and VOX-style pacing.",
    "vlog": "Prioritize story beats, A-roll/B-roll, jump cuts, captions, music, ambience, and reusable creator effects.",
    "technical": "Prioritize teaching structure, screen recordings, code emphasis, diagrams, callouts, and information density.",
    "transcript": "Prioritize timestamped speech, speaker changes, on-screen text, and visible event synchronization.",
    "transflow_stage1": "Follow the caller-supplied TransFlow prompt exactly as the complete instruction set; do not add extra commentary.",
}


class VideoAnalysisError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class VideoJob:
    id: str
    source: str
    analysis_type: str
    model: str
    prompt: str | None = None
    source_type: str = "unknown"
    original_source: str | None = None
    resolved_source: str | None = None
    source_metadata: dict | None = None
    status: str = "queued"
    stage: str = "queued"
    progress: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    key_name: str | None = None
    file_uri: str | None = None
    result: dict | None = None
    error: str | None = None
    archive_dir: str | None = None
    save_threshold: float = 7.0
    saved_path: str | None = None

    def public_dict(self) -> dict:
        payload = asdict(self)
        payload["progress"] = round(self.progress, 3)
        return payload


async def _file_chunks(path: Path, chunk_size: int = 1024 * 1024):
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        while chunk := await asyncio.to_thread(handle.read, chunk_size):
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


def probe_video_duration(path: Path) -> float:
    """使用 ffprobe 快速获取视频时长 (秒)"""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception:
        pass
    return 0.0


def fast_compress_video(
    src: Path,
    dst: Path,
    max_width: int = 960,
    fps: int = 15,
    duration: float | None = None,
) -> bool:
    """快速压缩视频用于多模态视觉理解：960宽、15fps、音频64k。
    
    优先采用 NVIDIA GPU NVENC 硬件加速 (h264_nvenc)，
    若失败或环境不支持则平滑降级至 CPU libx264。
    超时时间根据视频时长自适应计算：max(300, int(duration * 0.5))。
    """
    if duration is None:
        duration = probe_video_duration(src)
    size_mb = src.stat().st_size / (1024 * 1024) if src.exists() else 0.0
    # 动态超时拟合公式：实测 NVENC 速率为 0.075 * duration，赋予 2 倍安全裕度与 IO 缓冲，彻底收敛超时上限
    timeout = max(45, int(2 * (0.08 * (duration or 0.0) + 0.1 * size_mb)))

    # 1. 优先尝试 GPU NVENC 硬件加速
    try:
        nvenc_cmd = [
            "ffmpeg", "-y", "-i", str(src),
            "-vf", f"scale='min({max_width},iw)':-2",
            "-r", str(fps),
            "-c:v", "h264_nvenc", "-preset", "p1", "-cq", "32",
            "-c:a", "aac", "-b:a", "64k",
            str(dst)
        ]
        res = subprocess.run(nvenc_cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            logger.info(f"[FAST_COMPRESS] NVENC GPU 硬件加速压缩成功 (timeout={timeout}s)")
            return True
        logger.warning(
            f"[FAST_COMPRESS] NVENC 退出码异常 ({res.returncode})，尝试降级至 CPU libx264: "
            f"{res.stderr[:200] if res.stderr else ''}"
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"[FAST_COMPRESS] NVENC 硬件编码超时 ({timeout}s)")
        if dst.exists():
            dst.unlink(missing_ok=True)
        return False
    except Exception as e:
        logger.warning(f"[FAST_COMPRESS] NVENC 不可用 ({e})，降级至 CPU libx264")

    # 2. 降级至 CPU libx264
    try:
        cpu_cmd = [
            "ffmpeg", "-y", "-i", str(src),
            "-vf", f"scale='min({max_width},iw)':-2",
            "-r", str(fps),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-threads", "0",
            "-c:a", "aac", "-b:a", "64k",
            str(dst)
        ]
        res = subprocess.run(cpu_cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            logger.info(f"[FAST_COMPRESS] CPU libx264 降级压缩成功 (timeout={timeout}s)")
            return True
        logger.warning(f"[FAST_COMPRESS] CPU 压缩失败: {res.stderr[:200] if res.stderr else ''}")
        return False
    except Exception as e:
        logger.warning(f"[FAST_COMPRESS] CPU 压缩异常: {e}")
        if dst.exists():
            dst.unlink(missing_ok=True)
        return False


class VideoAnalysisService:
    def __init__(
        self,
        key_pool: KeyManager,
        client: httpx.AsyncClient,
        job_dir: str | Path | None = None,
        download_dir: str | Path | None = None,
    ):
        self.key_pool = key_pool
        self.client = client
        self.jobs: dict[str, VideoJob] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.max_upload_bytes = int(
            os.getenv("VIDEO_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024))
        )
        self.poll_interval = float(os.getenv("VIDEO_POLL_INTERVAL_SECONDS", "5"))
        self.processing_timeout = float(os.getenv("VIDEO_PROCESSING_TIMEOUT_SECONDS", "900"))
        configured_job_dir = job_dir or os.getenv("VIDEO_JOB_DIR", "data/video_jobs")
        self.job_dir = Path(configured_job_dir).resolve()
        self.job_dir.mkdir(parents=True, exist_ok=True)
        configured_download_dir = download_dir or os.getenv(
            "VIDEO_DOWNLOAD_DIR", "data/video_downloads"
        )
        self.download_dir = Path(configured_download_dir).resolve()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.download_timeout = float(
            os.getenv("VIDEO_DOWNLOAD_TIMEOUT_SECONDS", "180")
        )
        self.cookies_from_browser = os.getenv(
            "VIDEO_YTDLP_COOKIES_FROM_BROWSER", ""
        ).strip()
        self.downloader = VideoDownloader(
            download_dir=self.download_dir,
            download_timeout=self.download_timeout,
            cookies_from_browser=self.cookies_from_browser,
            max_bytes=self.max_upload_bytes,
        )
        self._load_jobs()

    async def submit(
        self,
        source: str,
        analysis_type: str = "general",
        prompt: str | None = None,
        model: str = "gemini-3.5-flash-lite",
        archive_dir: str | None = None,
        save_threshold: float = 7.0,
        source_metadata: dict | None = None,
    ) -> dict:
        if analysis_type not in ANALYSIS_TYPES:
            raise VideoAnalysisError(f"analysis_type must be one of: {', '.join(sorted(ANALYSIS_TYPES))}")

        # 模型选型策略：
        # 1. 用户显式指定模型时 (如 CLI 传入 gemini-3.8-flash) 予以绝对尊重
        # 2. 未指定或默认时，日常任务首选 500 RPD 高吞吐模型 (gemini-3.5-flash-lite)，极大节约 20 RPD 配额
        # 3. 视频时长 >= 10分钟 (600秒) 的长视频多模态任务，自动升档至 20 RPD 智力旗舰 (gemini-3.8-flash)
        if not model or model == "gemini-3.5-flash-lite":
            duration = None
            if source_metadata and isinstance(source_metadata, dict):
                dur_raw = source_metadata.get("duration") or source_metadata.get("duration_s")
                if dur_raw is not None:
                    try:
                        duration = float(dur_raw)
                    except (ValueError, TypeError):
                        pass
            if duration is not None and duration >= 600.0:
                model = "gemini-3.8-flash"
                logger.info("[MODEL_DISPATCH] 视频时长 %.1fs >= 10min，长视频多模态分析自动升档至 20 RPD 智力旗舰: %s", duration, model)
            else:
                model = "gemini-3.5-flash-lite"

        source_info = self._validate_source(source)
        job = VideoJob(
            id=f"video_{uuid.uuid4().hex[:16]}",
            source=source_info.value,
            analysis_type=analysis_type,
            model=model,
            prompt=prompt,
            source_type=source_info.kind,
            original_source=source_info.original,
            resolved_source=source_info.value,
            archive_dir=archive_dir,
            save_threshold=save_threshold,
            source_metadata=source_metadata,
        )
        self.jobs[job.id] = job
        self._persist(job)
        self.tasks[job.id] = asyncio.create_task(self._run(job))
        return job.public_dict()

    def get(self, job_id: str) -> dict:
        if job_id not in self.jobs:
            raise VideoAnalysisError(f"Unknown video job: {job_id}")
        return self.jobs[job_id].public_dict()

    async def cancel(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise VideoAnalysisError(f"Unknown video job: {job_id}")
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return job.public_dict()

    async def shutdown(self):
        active = [task for task in self.tasks.values() if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    def _validate_source(self, source: str) -> VideoSource:
        try:
            source_info = parse_video_source(source)
        except ValueError as exc:
            raise VideoAnalysisError(str(exc)) from exc
        if source_info.kind != "local":
            return source_info
        path = Path(source_info.value)
        self._validate_downloaded_file(path)
        return source_info

    def _update(self, job: VideoJob, stage: str, progress: float):
        job.stage = stage
        job.progress = progress
        job.updated_at = time.time()
        self._persist(job)

    def _persist(self, job: VideoJob):
        destination = self.job_dir / f"{job.id}.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(job.public_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(destination)

    def _load_jobs(self):
        for path in self.job_dir.glob("video_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                job = VideoJob(**payload)
            except (OSError, ValueError, TypeError):
                continue
            if job.status in {"queued", "running"}:
                job.status = "failed"
                job.stage = "failed"
                job.error = "Gateway restarted before the job completed"
                job.updated_at = time.time()
                self._persist(job)
            self.jobs[job.id] = job

    async def _run(self, job: VideoJob):
        job.status = "running"
        self._persist(job)
        api_key: str | None = None
        key_finalized = False
        temporary_directory: Path | None = None
        file_name: str | None = None
        temp_compressed_path: Path | None = None
        try:
            mime_type = "video/mp4"
            source_path: Path | None = None
            video_bytes: bytes | None = None
            if job.source_type == "local":
                source_path = Path(job.source)
            elif job.source_type in {"douyin", "bilibili", "web"}:
                self._update(job, "downloading", 0.05)
                media_data, job.source_metadata, temporary_directory = (
                    await self._download_remote(job)
                )
                if isinstance(media_data, bytes):
                    video_bytes = media_data
                else:
                    source_path = media_data
                job.resolved_source = (
                    job.source_metadata.get("webpage_url") or job.source
                )
                self._persist(job)

            if source_path is not None:
                self._validate_downloaded_file(source_path)
                mime_type = mimetypes.guess_type(source_path.name)[0] or mime_type
            elif video_bytes is not None:
                self._validate_video_bytes(video_bytes)
                mime_type = "video/mp4"

            temp_compressed_path: Path | None = None
            if source_path is not None and source_path.is_file():
                file_size = source_path.stat().st_size
                duration = None
                if job.source_metadata and isinstance(job.source_metadata, dict):
                    raw_dur = job.source_metadata.get("duration") or job.source_metadata.get("duration_s")
                    if raw_dur is not None:
                        try:
                            duration = float(raw_dur)
                        except (ValueError, TypeError):
                            duration = None
                if duration is None:
                    duration = probe_video_duration(source_path)
                # 当视频超过 50MB 或超过 10 分钟 (600s) 时，进行视觉与OCR无损的快速下采样预压缩，大幅提速上传和 Gemini Active 预转码
                if file_size > 50 * 1024 * 1024 or duration > 600.0:
                    logger.info(
                        "[AUTO_COMPRESS] 视频文件较大 (size=%.1fMB, duration=%.1fs)，启动快速预压缩优化传输...",
                        file_size / (1024 * 1024),
                        duration,
                    )
                    self._update(job, "compressing", 0.10)
                    temp_dir = Path(tempfile.gettempdir())
                    c_path = temp_dir / f"compressed_{uuid.uuid4().hex[:8]}_{source_path.name}"
                    if await asyncio.to_thread(fast_compress_video, source_path, c_path, 960, 15, duration):
                        c_size = c_path.stat().st_size
                        logger.info(
                            "[AUTO_COMPRESS] 预压缩完成: %.1fMB -> %.1fMB (压缩率 %.1f%%)",
                            file_size / (1024 * 1024),
                            c_size / (1024 * 1024),
                            (1 - c_size / file_size) * 100,
                        )
                        temp_compressed_path = c_path

            upload_media = (
                temp_compressed_path
                if temp_compressed_path is not None
                else (source_path if source_path is not None else video_bytes)
            )
            if temp_compressed_path is not None:
                mime_type = "video/mp4"

            max_key_attempts = max(1, len(self.key_pool.keys))
            last_key_error: Exception | None = None

            for attempt in range(max_key_attempts):
                key_info = self.key_pool.acquire_key()
                if not key_info:
                    raise VideoAnalysisError("No healthy API key is available")
                api_key = key_info["key"]
                job.key_name = key_info["name"]
                key_finalized = False

                try:
                    file_to_upload = upload_media
                    if file_to_upload is not None:
                        self._update(job, "uploading", 0.15)
                        file_info = await self._upload(
                            file_to_upload, mime_type, api_key, display_name=f"{job.id}.mp4"
                        )
                        job.file_uri = file_info.get("uri")
                        file_name = file_info.get("name")
                        if not job.file_uri or not file_name:
                            raise VideoAnalysisError("Files API returned no file URI")
                        self._update(job, "processing", 0.35)
                        file_info = await self._wait_until_active(file_name, api_key, job)
                        job.file_uri = file_info.get("uri", job.file_uri)
                        mime_type = file_info.get("mimeType", mime_type)
                    else:
                        job.file_uri = job.source
                    # Model Fallback Tier 完整官方技术代际:
                    # 20 RPD 智力梯队: gemini-3.8-flash > 3.7-flash > 3.6-flash > 3.5-flash
                    # 500 RPD 轻量梯队: gemini-3.5-flash-lite > 3.1-flash-lite
                    fallback_models = [job.model]
                    if job.model in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"):
                        # 20 RPD 深度梯队：优先同代际降级，随后再回退至 500 RPD 轻量系列
                        flash_chain = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
                        lite_chain = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
                        fallback_models.extend([m for m in flash_chain if m != job.model])
                        fallback_models.extend(lite_chain)
                    elif job.model in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"):
                        # 500 RPD 轻量梯队：优先同代轻量互备，随后向上提档至 20 RPD 旗舰
                        lite_chain = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
                        flash_chain = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
                        fallback_models.extend([m for m in lite_chain if m != job.model])
                        fallback_models.extend(flash_chain)
                    else:
                        fallback_models.extend([
                            "gemini-3.8-flash",
                            "gemini-3.7-flash",
                            "gemini-3.6-flash",
                            "gemini-3.5-flash",
                            "gemini-3.5-flash-lite",
                            "gemini-3.1-flash-lite",
                        ])

                    # 保留原序去重
                    seen_models = set()
                    fallback_models = [
                        m for m in fallback_models
                        if not (m in seen_models or seen_models.add(m))
                    ]

                    analyze_error = None
                    for m_idx, m_name in enumerate(fallback_models):
                        job.model = m_name
                        try:
                            self._update(job, "analyzing", 0.55)
                            job.result = await self._analyze(job, mime_type, api_key)
                            analyze_error = None
                            break
                        except VideoAnalysisError as v_err:
                            err_str = str(v_err).lower()
                            should_failover = (
                                v_err.status_code in {429, 503}
                                or "high demand" in err_str
                                or "temporarily unavailable" in err_str
                                or "resource_exhausted" in err_str
                                or "quota" in err_str
                                or "rate limit" in err_str
                                or "attractor" in err_str
                                or "runaway" in err_str
                            )
                            if should_failover and m_idx < len(fallback_models) - 1:
                                next_m = fallback_models[m_idx + 1]
                                logger.warning(
                                    "[MODEL_FAILOVER] 模型 %s 遭遇异常 (%s)，自动降级切换至备选模型 %s...",
                                    m_name,
                                    v_err,
                                    next_m,
                                )
                                await asyncio.sleep(1.5)
                                continue
                            analyze_error = v_err
                            break
                    if analyze_error:
                        raise analyze_error

                    # Auto-archive funnel for curation or explicit archive_dir
                    if job.archive_dir:
                        try:
                            should_save = True
                            score = 0.0
                            if job.analysis_type == "curation" and job.result:
                                score = float(
                                    job.result.get("curation_score")
                                    or job.result.get("score")
                                    or 0.0
                                )
                                should_save = score >= job.save_threshold
                            if should_save:
                                target_dir = Path(job.archive_dir)
                                target_dir.mkdir(parents=True, exist_ok=True)
                                title = (job.source_metadata or {}).get("title") or job.id
                                clean_title = re.sub(r'[\\/*?:"<>|]', "", title)[:40]
                                prefix = f"[Score-{score:.1f}]" if job.analysis_type == "curation" else ""
                                dest_file = target_dir / f"{prefix}[{job.source_type}]_{clean_title}.mp4"
                                if video_bytes:
                                    dest_file.write_bytes(video_bytes)
                                elif source_path and source_path.is_file():
                                    shutil.copy2(source_path, dest_file)
                                report_file = dest_file.with_suffix(".json")
                                report_file.write_text(
                                    json.dumps(job.result, ensure_ascii=False, indent=2),
                                    encoding="utf-8",
                                )
                                job.saved_path = str(dest_file)
                                logger.info("Video successfully archived to %s", dest_file)
                        except Exception as archive_exc:
                            logger.warning("Failed to auto-archive video: %s", archive_exc)

                    job.status = "completed"
                    self._update(job, "completed", 1.0)
                    usage = (
                        (job.result or {}).get("gateway_metadata", {}).get("usage", {})
                    )
                    self.key_pool.mark_success(api_key, usage)
                    key_finalized = True
                    break
                except VideoAnalysisError as exc:
                    self._mark_failure(api_key, exc)
                    key_finalized = True
                    last_key_error = exc
                    if exc.status_code in {401, 403, 429, 503} and attempt < max_key_attempts - 1:
                        logger.warning(
                            "Key %s failed with %s, failing over to next key (attempt %d/%d)",
                            job.key_name,
                            exc,
                            attempt + 1,
                            max_key_attempts,
                        )
                        continue
                    raise
                except Exception:
                    self.key_pool.release(api_key)
                    key_finalized = True
                    raise
            else:
                if last_key_error:
                    raise last_key_error
        except asyncio.CancelledError:
            job.status = "cancelled"
            self._update(job, "cancelled", job.progress)
            if api_key:
                self.key_pool.release(api_key)
                key_finalized = True
            raise
        except VideoAnalysisError as exc:
            job.status, job.error = "failed", str(exc)[:1000]
            self._update(job, "failed", job.progress)
            if api_key:
                self._mark_failure(api_key, exc)
                key_finalized = True
        except (httpx.HTTPError, OSError, ValueError) as exc:
            job.status, job.error = "failed", f"Video transport error: {exc}"[:1000]
            self._update(job, "failed", job.progress)
            if api_key:
                self.key_pool.mark_failure(api_key, reason=job.error, cooldown_seconds=self.key_pool.transient_cooldown)
                key_finalized = True
        finally:
            if api_key and not key_finalized:
                self.key_pool.release(api_key)
            if temporary_directory:
                await asyncio.to_thread(
                    shutil.rmtree, temporary_directory, ignore_errors=True
                )
            if temp_compressed_path and temp_compressed_path.exists():
                try:
                    temp_compressed_path.unlink(missing_ok=True)
                except Exception:
                    pass
            if file_name and api_key:
                try:
                    await self.client.delete(
                        f"https://generativelanguage.googleapis.com/v1beta/{file_name.removeprefix('/')}",
                        headers={"x-goog-api-key": api_key},
                    )
                except Exception:
                    pass

    def _validate_downloaded_file(self, path: Path):
        try:
            validate_video_file(path, self.max_upload_bytes)
        except VideoDownloadError as exc:
            raise VideoAnalysisError(str(exc)) from exc

    def _validate_video_bytes(self, data: bytes):
        try:
            validate_video_bytes(data, self.max_upload_bytes)
        except VideoDownloadError as exc:
            raise VideoAnalysisError(str(exc)) from exc

    async def _download_remote(
        self, job: VideoJob
    ) -> tuple[Path | bytes, dict, Path | None]:
        workspace = self.download_dir / job.id
        workspace.mkdir(parents=True, exist_ok=False)
        try:
            media_data, metadata = await asyncio.wait_for(
                asyncio.to_thread(
                    self._download_remote_sync,
                    job.source,
                    job.source_type,
                    workspace,
                ),
                timeout=self.download_timeout,
            )
            return media_data, metadata, workspace
        except TimeoutError as exc:
            shutil.rmtree(workspace, ignore_errors=True)
            raise VideoAnalysisError(
                f"Timed out downloading the {job.source_type} video"
            ) from exc
        except Exception as exc:
            shutil.rmtree(workspace, ignore_errors=True)
            if isinstance(exc, VideoAnalysisError):
                raise
            raise VideoAnalysisError(
                f"Unable to download {job.source_type} video: {exc}"
            ) from exc

    def _download_remote_sync(
        self, url: str, source_type: str, workspace: Path
    ) -> tuple[Path | bytes, dict]:
        try:
            return self.downloader.download_media_stream(url, source_type, workspace)
        except VideoDownloadError as exc:
            raise VideoAnalysisError(str(exc)) from exc

    def _download_page_with_browser(
        self,
        url: str,
        source_type: str,
        workspace: Path,
        yt_dlp_error: Exception,
    ) -> tuple[Path | bytes, dict]:
        try:
            return self.downloader._download_page_with_browser(
                url, source_type, workspace, yt_dlp_error
            )
        except VideoDownloadError as exc:
            raise VideoAnalysisError(str(exc)) from exc

    def _fetch_browser_media(
        self,
        media_url: str,
        destination: Path,
        *,
        cookies: dict[str, str],
        referer: str,
        user_agent: str,
    ) -> Path | bytes:
        try:
            return self.downloader._fetch_browser_media(
                media_url,
                destination,
                cookies=cookies,
                referer=referer,
                user_agent=user_agent,
            )
        except VideoDownloadError as exc:
            raise VideoAnalysisError(str(exc)) from exc

    @staticmethod
    def _find_browser_executable() -> Path | None:
        return find_browser_executable()

    def _mark_failure(self, api_key: str, error: VideoAnalysisError):
        status = error.status_code
        if status == 401:
            self.key_pool.mark_failure(api_key, reason=str(error), disable=True)
        elif status in {403, 429}:
            self.key_pool.mark_failure(api_key, reason=str(error), cooldown_seconds=self.key_pool.default_rate_limit_cooldown, rate_limited=status == 429)
        elif status and status >= 500:
            self.key_pool.mark_failure(api_key, reason=str(error), cooldown_seconds=self.key_pool.transient_cooldown)
        else:
            self.key_pool.release(api_key)

    async def _upload(
        self,
        media: Path | bytes,
        mime_type: str,
        api_key: str,
        display_name: str | None = None,
    ) -> dict:
        if isinstance(media, bytes):
            size = len(media)
            file_name = display_name or "video.mp4"
            content = media
        elif media.stat().st_size <= 60 * 1024 * 1024:
            size = media.stat().st_size
            file_name = display_name or media.name
            content = media.read_bytes()
        else:
            size = media.stat().st_size
            file_name = display_name or media.name
            content = _file_chunks(media)

        size_mb = size / (1024 * 1024)
        logger.info(
            "video.upload.start file=%s size_bytes=%d size_mb=%.2f mime=%s",
            file_name, size, size_mb, mime_type,
        )

        start = await self.client.post(
            "https://generativelanguage.googleapis.com/upload/v1beta/files",
            headers={
                "x-goog-api-key": api_key,
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": mime_type,
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": file_name}},
            timeout=httpx.Timeout(60.0),
        )
        self._raise_for_google(start, "start video upload")
        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise VideoAnalysisError("Files API returned no upload URL")
        # 大文件上传需要更长的超时时间（默认 180s 太短）
        upload_timeout = httpx.Timeout(600.0)  # 10 分钟
        upload_start_ts = time.monotonic()
        uploaded = await self.client.post(
            upload_url,
            headers={
                "Content-Length": str(size),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            content=content,
            timeout=upload_timeout,
        )
        upload_elapsed = time.monotonic() - upload_start_ts
        self._raise_for_google(uploaded, "upload video")
        data = uploaded.json()
        file_meta = data.get("file", data)
        logger.info(
            "video.upload.done file=%s elapsed_seconds=%.2f throughput_mbps=%.2f",
            file_name,
            upload_elapsed,
            size_mb / upload_elapsed if upload_elapsed > 0 else 0.0,
        )
        return file_meta

    async def _wait_until_active(self, file_name: str, api_key: str, job: VideoJob) -> dict:
        deadline = time.monotonic() + self.processing_timeout
        url = f"https://generativelanguage.googleapis.com/v1beta/{file_name.removeprefix('/')}"
        polls = 0
        while time.monotonic() < deadline:
            response = await self.client.get(url, headers={"x-goog-api-key": api_key})
            self._raise_for_google(response, "poll video processing")
            file_info = response.json()
            state = str(file_info.get("state", "")).upper()
            if state == "ACTIVE":
                return file_info
            if state == "FAILED":
                raise VideoAnalysisError("Gemini failed to process the video")
            polls += 1
            self._update(job, "processing", min(0.5, 0.3 + polls * 0.01))
            await asyncio.sleep(self.poll_interval)
        raise VideoAnalysisError("Timed out waiting for Gemini video processing")

    async def _analyze(self, job: VideoJob, mime_type: str, api_key: str) -> dict:
        """
        分析视频（支持流式输出）
        
        流式输出会实时追加到 {job.id}_stream.log，用户可以看到 AI 正在生成的内容。
        最终仍然一次性解析完整 JSON。
        """
        if job.analysis_type == "remotion":
            schema = REMOTION_ANALYSIS_SCHEMA
        elif job.analysis_type == "curation":
            schema = CURATION_ANALYSIS_SCHEMA
        elif job.analysis_type == "transflow_stage1":
            schema = TRANSFLOW_ANALYSIS_SCHEMA
        else:
            schema = GENERAL_ANALYSIS_SCHEMA

        payload = {
            "contents": [{"role": "user", "parts": [
                {"fileData": {"fileUri": job.file_uri, "mimeType": mime_type}},
                {"text": self._prompt(job.analysis_type, job.prompt)},
            ]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0.7,
                "maxOutputTokens": 65536,  # 允许长视频输出大量字幕和分镜
            },
        }
        
        # 使用流式端点
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{job.model}:streamGenerateContent?alt=sse"
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        
        # 准备流式日志文件
        stream_log_file = self.job_dir / f"{job.id}_stream.log"
        chunks = []
        chunks_count = 0
        recent_window = ""
        last_chunk_text = ""
        repeat_chunk_count = 0
        
        try:
            async with self.client.stream("POST", url, headers=headers, json=payload, timeout=600.0) as response:
                response.raise_for_status()
                
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    
                    if line.startswith("data: "):
                        chunk_data = line[6:]  # 去掉 "data: " 前缀
                        
                        try:
                            chunk = json.loads(chunk_data)
                            candidates = chunk.get("candidates", [])
                            if not candidates:
                                continue
                            
                            content = candidates[0].get("content", {})
                            parts = content.get("parts", [])
                            finish_reason = candidates[0].get("finishReason")
                            
                            # 提取文本片段
                            for part in parts:
                                if "text" in part:
                                    text_chunk = part["text"]

                                    # Stream Runaway Anomaly Watchdog:
                                    # 1. 连续 0 坍缩吸引子检测 (跨 chunk 滑动窗口检测)
                                    recent_window = (recent_window + text_chunk)[-300:]
                                    if re.search(r'0{30,}', recent_window):
                                        logger.error(
                                            f"[STREAM_WATCHDOG] 捕获到连续 30+ 个 '0' 异常坍缩吸引子 "
                                            f"(job {job.id}, model {job.model})，主动切断连接触发故障转移"
                                        )
                                        raise VideoAnalysisError(
                                            f"Gemini streaming token attractor loop detected (runaway zeros) in {job.model}",
                                            status_code=429,
                                        )

                                    # 2. 相同 chunk 重复死循环检测
                                    stripped_chunk = text_chunk.strip()
                                    if stripped_chunk and stripped_chunk == last_chunk_text and len(stripped_chunk) > 3:
                                        repeat_chunk_count += 1
                                        if repeat_chunk_count >= 8:
                                            logger.error(
                                                f"[STREAM_WATCHDOG] 捕获到重复 chunk 连续出现 8 次 "
                                                f"(job {job.id}, model {job.model})，主动切断连接触发故障转移"
                                            )
                                            raise VideoAnalysisError(
                                                f"Gemini streaming repeating chunk attractor loop detected in {job.model}",
                                                status_code=429,
                                            )
                                    else:
                                        last_chunk_text = stripped_chunk
                                        repeat_chunk_count = 0

                                    chunks.append(text_chunk)
                                    chunks_count += 1
                                    
                                    # 实时写入流式日志
                                    with open(stream_log_file, "a", encoding="utf-8") as f:
                                        f.write(text_chunk)
                                    
                                    # 更新进度（analyzing: 0.55 → 0.95）
                                    # 假设平均 50-100 个 chunks，进度逐步增长
                                    progress = min(0.95, 0.55 + (chunks_count / 100) * 0.4)
                                    self._update(job, "analyzing", progress)
                            
                            # 如果完成，退出循环
                            if finish_reason:
                                logger.info(f"Stream finished for job {job.id}, reason: {finish_reason}, chunks: {chunks_count}")
                                break
                                
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse stream chunk for job {job.id}: {e}")
                            continue
            
            # 拼接完整文本
            full_text = "".join(chunks).strip()
            if not full_text:
                raise VideoAnalysisError("Gemini returned empty response via stream")
            
            # 去除可能的 markdown 代码块标记
            if full_text.startswith("```json"):
                full_text = full_text[7:].removesuffix("```").strip()
            
            # 解析 JSON
            try:
                result = json.loads(full_text)
            except ValueError as exc:
                # Gemini 长 JSON 容错：尝试修复常见格式错误
                try:
                    fixed_text = self._attempt_json_repair(full_text)
                    result = json.loads(fixed_text)
                    logger.warning(f"JSON repair succeeded for job {job.id}, original error: {exc}")
                except (ValueError, Exception) as repair_exc:
                    # 保存原始文本用于调试
                    debug_file = self.job_dir / f"{job.id}_debug_response.json"
                    debug_file.write_text(full_text, encoding="utf-8")
                    raise VideoAnalysisError(f"Gemini returned invalid structured analysis: {exc}") from exc
            
            # 添加元数据（注意：流式输出没有 usageMetadata，需要从最后一个 chunk 获取）
            result["gateway_metadata"] = {
                "job_id": job.id, 
                "model": job.model, 
                "analysis_type": job.analysis_type,
                "file_uri": job.file_uri, 
                "key_name": job.key_name, 
                "stream_chunks": chunks_count,
                "stream_log": str(stream_log_file),
            }
            
            return result
            
        except httpx.HTTPStatusError as e:
            try:
                await e.response.aread()
            except Exception:
                pass
            self._raise_for_google(e.response, "analyze video (stream)")
            raise  # 永远不会到达，_raise_for_google 总是抛出异常

    @staticmethod
    def _attempt_json_repair(text: str) -> str:
        """尝试修复 Gemini 返回的格式错误的 JSON（缺少逗号、多余逗号、未闭合括号等）"""
        try:
            # 使用 json_repair 库自动修复常见 JSON 格式错误
            repaired = json_repair.repair_json(text)
            return repaired
        except Exception as e:
            # 如果自动修复失败，尝试简单的正则修复
            logger.debug(f"json_repair failed: {e}, trying regex fallback")
            # 修复缺少逗号的情况：}{ -> },{  或  ][ -> ],[
            text = re.sub(r'}\s*{', '},{', text)
            text = re.sub(r']\s*\[', '],[', text)
            # 修复对象/数组末尾多余逗号：, } -> }  或  , ] -> ]
            text = re.sub(r',\s*}', '}', text)
            text = re.sub(r',\s*]', ']', text)
            return text

    @staticmethod
    def _prompt(analysis_type: str, custom_prompt: str | None) -> str:
        if analysis_type == "transflow_stage1" and custom_prompt:
            # TransFlow 侧已经准备好完整的结构化指令模板 (含时间戳/毫秒/坐标契约要求)，
            # 网关不再叠加通用指令，直接白盒透传，避免污染既定的 JSON 契约。
            return custom_prompt
        base = (
            "Analyze the complete video using visual and audio evidence. Use MM:SS timestamps. "
            "Separate observed facts from uncertain inference. Return JSON only. "
            + STYLE_INSTRUCTIONS[analysis_type]
        )
        return f"{base}\nAdditional requirements: {custom_prompt}" if custom_prompt else base

    @staticmethod
    def _raise_for_google(response: httpx.Response, action: str):
        if response.status_code < 400:
            return
        message = None
        try:
            raw = getattr(response, "_content", None)
            if raw:
                message = json.loads(raw.decode("utf-8", errors="ignore")).get("error", {}).get("message")
            elif hasattr(response, "text"):
                message = response.json().get("error", {}).get("message")
        except Exception:
            message = None
        raise VideoAnalysisError(f"Unable to {action}: {message or f'HTTP {response.status_code}'}", response.status_code)
