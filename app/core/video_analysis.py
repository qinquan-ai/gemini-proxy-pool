import asyncio
import json
import mimetypes
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import httpx

from app.core.key_manager import KeyManager
from app.core.video_sources import VideoSource, parse_video_source, validate_public_url


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
ANALYSIS_TYPES = {"remotion", "vox", "vlog", "technical", "transcript", "general"}

ANALYSIS_SCHEMA = {
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

STYLE_INSTRUCTIONS = {
    "remotion": "Prioritize timing, reusable React components, assets, typography, transitions, and implementation difficulty.",
    "vox": "Prioritize editorial structure, evidence reveals, maps, charts, kinetic typography, and VOX-style pacing.",
    "vlog": "Prioritize story beats, A-roll/B-roll, jump cuts, captions, music, ambience, and reusable creator effects.",
    "technical": "Prioritize teaching structure, screen recordings, code emphasis, diagrams, callouts, and information density.",
    "transcript": "Prioritize timestamped speech, speaker changes, on-screen text, and visible event synchronization.",
    "general": "Provide a balanced audio-visual and narrative analysis.",
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
        self._load_jobs()

    async def submit(self, source: str, analysis_type: str = "remotion", prompt: str | None = None, model: str = "gemini-3-flash-preview") -> dict:
        if analysis_type not in ANALYSIS_TYPES:
            raise VideoAnalysisError(f"analysis_type must be one of: {', '.join(sorted(ANALYSIS_TYPES))}")
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
        try:
            mime_type = "video/mp4"
            source_path: Path | None = None
            if job.source_type == "local":
                source_path = Path(job.source)
            elif job.source_type in {"douyin", "bilibili", "web"}:
                self._update(job, "downloading", 0.05)
                source_path, job.source_metadata, temporary_directory = (
                    await self._download_remote(job)
                )
                job.resolved_source = (
                    job.source_metadata.get("webpage_url") or job.source
                )
                self._persist(job)

            key_info = self.key_pool.acquire_key()
            if not key_info:
                raise VideoAnalysisError("No healthy API key is available")
            api_key = key_info["key"]
            job.key_name = key_info["name"]

            if source_path is not None:
                self._validate_downloaded_file(source_path)
                mime_type = mimetypes.guess_type(source_path.name)[0] or mime_type
                self._update(job, "uploading", 0.15)
                file_info = await self._upload(source_path, mime_type, api_key)
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
            self._update(job, "analyzing", 0.55)
            job.result = await self._analyze(job, mime_type, api_key)
            job.status = "completed"
            self._update(job, "completed", 1.0)
            usage = (
                (job.result or {}).get("gateway_metadata", {}).get("usage", {})
            )
            self.key_pool.mark_success(api_key, usage)
            key_finalized = True
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

    def _validate_downloaded_file(self, path: Path):
        if not path.is_file():
            raise VideoAnalysisError("Video downloader returned no media file")
        if path.stat().st_size > self.max_upload_bytes:
            raise VideoAnalysisError("Video exceeds the configured upload limit")
        mime_type = mimetypes.guess_type(path.name)[0]
        if mime_type not in SUPPORTED_VIDEO_MIME_TYPES:
            raise VideoAnalysisError(f"Unsupported video MIME type: {mime_type}")
        with path.open("rb") as handle:
            header = handle.read(32)
        signatures = (
            len(header) >= 12 and header[4:8] == b"ftyp",
            header.startswith(b"\x1a\x45\xdf\xa3"),
            header.startswith(b"FLV"),
            header.startswith(b"\x00\x00\x01\xba"),
            header.startswith(b"\x00\x00\x01\xb3"),
            header.startswith(b"0&\xb2u\x8ef\xcf\x11\xa6\xd9\x00\xaa\x00b\xcel"),
            header.startswith(b"RIFF") and header[8:12] == b"AVI ",
        )
        if not any(signatures):
            raise VideoAnalysisError(
                "The source is not a real video file; it may be an HTML verification page"
            )

    async def _download_remote(self, job: VideoJob) -> tuple[Path, dict, Path]:
        workspace = self.download_dir / job.id
        workspace.mkdir(parents=True, exist_ok=False)
        try:
            path, metadata = await asyncio.wait_for(
                asyncio.to_thread(
                    self._download_remote_sync,
                    job.source,
                    job.source_type,
                    workspace,
                ),
                timeout=self.download_timeout,
            )
            return path, metadata, workspace
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
    ) -> tuple[Path, dict]:
        try:
            validate_public_url(url)
        except ValueError as exc:
            raise VideoAnalysisError(str(exc)) from exc

        try:
            from yt_dlp import YoutubeDL
            from yt_dlp.utils import DownloadError
        except ImportError as exc:
            raise VideoAnalysisError(
                "Remote video support requires yt-dlp; install project requirements"
            ) from exc

        options = {
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": str(workspace / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 2,
            "fragment_retries": 2,
            "socket_timeout": 30,
            "max_filesize": self.max_upload_bytes,
        }
        if self.cookies_from_browser:
            options["cookiesfrombrowser"] = (self.cookies_from_browser,)

        try:
            with YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=True)
                prepared_path = Path(downloader.prepare_filename(info))
        except DownloadError as exc:
            if source_type in {"douyin", "web"}:
                return self._download_page_with_browser(
                    url, source_type, workspace, exc
                )
            raise VideoAnalysisError(
                "Bilibili returned no downloadable public stream. "
                "The video may require login cookies, region access, or ffmpeg."
            ) from exc

        candidates = [prepared_path] if prepared_path.is_file() else []
        candidates.extend(
            path
            for path in workspace.iterdir()
            if path.is_file()
            and mimetypes.guess_type(path.name)[0] in SUPPORTED_VIDEO_MIME_TYPES
        )
        if not candidates:
            raise VideoAnalysisError(
                f"{source_type} returned no downloadable video stream"
            )
        path = max(candidates, key=lambda candidate: candidate.stat().st_size)
        metadata = {
            key: info.get(key)
            for key in (
                "id",
                "title",
                "uploader",
                "duration",
                "width",
                "height",
                "webpage_url",
                "extractor",
            )
            if info.get(key) is not None
        }
        metadata["source_type"] = source_type
        return path, metadata

    def _download_page_with_browser(
        self,
        url: str,
        source_type: str,
        workspace: Path,
        yt_dlp_error: Exception,
    ) -> tuple[Path, dict]:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise VideoAnalysisError(
                "The page requires browser extraction and Playwright is not installed"
            ) from exc

        executable = self._find_browser_executable()
        if not executable:
            raise VideoAnalysisError(
                "The page requires browser extraction and no supported local Chrome/Edge executable was found"
            ) from yt_dlp_error

        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        )
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=str(executable),
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                try:
                    context = browser.new_context(
                        locale="zh-CN",
                        viewport={"width": 1280, "height": 720},
                        user_agent=user_agent,
                    )
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_function(
                        """
                        () => [...document.querySelectorAll('video')].some(
                          (video) => video.currentSrc &&
                            Number.isFinite(video.duration) && video.duration > 5
                        )
                        """,
                        timeout=30_000,
                    )
                    video = page.locator("video").evaluate_all(
                        """
                        (videos) => videos
                          .map((video) => ({
                            src: video.currentSrc || video.src,
                            duration: video.duration,
                            width: video.videoWidth,
                            height: video.videoHeight,
                          }))
                          .filter((video) => video.src && video.duration > 5)
                          .sort((left, right) => right.duration - left.duration)[0]
                        """
                    )
                    cookies = {
                        cookie["name"]: cookie["value"]
                        for cookie in context.cookies()
                    }
                    metadata = {
                        "title": page.title(),
                        "duration": video["duration"],
                        "width": video["width"],
                        "height": video["height"],
                        "webpage_url": page.url,
                        "extractor": f"{source_type}-browser",
                        "source_type": source_type,
                    }
                    media_url = video["src"]
                    referer = page.url
                finally:
                    browser.close()
        except PlaywrightTimeoutError as exc:
            raise VideoAnalysisError(
                "The page loaded but no playable public video stream was found"
            ) from exc

        destination = workspace / f"{source_type}.mp4"
        self._download_browser_media(
            media_url,
            destination,
            cookies=cookies,
            referer=referer,
            user_agent=user_agent,
        )
        return destination, metadata

    def _download_browser_media(
        self,
        media_url: str,
        destination: Path,
        *,
        cookies: dict[str, str],
        referer: str,
        user_agent: str,
    ) -> None:
        current_url = media_url
        timeout = httpx.Timeout(60, read=self.download_timeout)
        with httpx.Client(
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Referer": referer},
        ) as client:
            for _ in range(6):
                try:
                    validate_public_url(current_url)
                except ValueError as exc:
                    raise VideoAnalysisError(str(exc)) from exc
                with client.stream("GET", current_url, cookies=cookies) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise VideoAnalysisError(
                                "Video media redirect has no destination"
                            )
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status_code >= 400:
                        raise VideoAnalysisError(
                            f"Video media stream returned HTTP {response.status_code}"
                        )
                    content_type = response.headers.get("content-type", "").lower()
                    if not content_type.startswith("video/"):
                        raise VideoAnalysisError(
                            "Video media stream returned "
                            f"{content_type or 'unknown content'}"
                        )
                    declared_size = int(response.headers.get("content-length") or 0)
                    if declared_size > self.max_upload_bytes:
                        raise VideoAnalysisError(
                            "Video exceeds the configured upload limit"
                        )
                    downloaded = 0
                    with destination.open("wb") as handle:
                        for chunk in response.iter_bytes(1024 * 1024):
                            downloaded += len(chunk)
                            if downloaded > self.max_upload_bytes:
                                raise VideoAnalysisError(
                                    "Video exceeds the configured upload limit"
                                )
                            handle.write(chunk)
                    return
        raise VideoAnalysisError("Video media stream exceeded the redirect limit")

    @staticmethod
    def _find_browser_executable() -> Path | None:
        configured = os.getenv("VIDEO_BROWSER_EXECUTABLE", "").strip()
        candidates = [
            configured,
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        return next(
            (Path(candidate) for candidate in candidates if candidate and Path(candidate).is_file()),
            None,
        )

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

    async def _upload(self, path: Path, mime_type: str, api_key: str) -> dict:
        size = path.stat().st_size
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
            json={"file": {"display_name": path.name}},
        )
        self._raise_for_google(start, "start video upload")
        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise VideoAnalysisError("Files API returned no upload URL")
        uploaded = await self.client.post(
            upload_url,
            headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"},
            content=_file_chunks(path),
        )
        self._raise_for_google(uploaded, "upload video")
        data = uploaded.json()
        return data.get("file", data)

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
        payload = {
            "contents": [{"role": "user", "parts": [
                {"fileData": {"fileUri": job.file_uri, "mimeType": mime_type}},
                {"text": self._prompt(job.analysis_type, job.prompt)},
            ]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": ANALYSIS_SCHEMA, "temperature": 0.2},
        }
        response = await self.client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{job.model}:generateContent",
            headers={"x-goog-api-key": api_key}, json=payload,
        )
        self._raise_for_google(response, "analyze video")
        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise VideoAnalysisError("Gemini returned no video analysis candidate")
        text = "".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", [])).strip()
        if text.startswith("```json"):
            text = text[7:].removesuffix("```").strip()
        try:
            result = json.loads(text)
        except ValueError as exc:
            raise VideoAnalysisError(f"Gemini returned invalid structured analysis: {exc}") from exc
        result["gateway_metadata"] = {
            "job_id": job.id, "model": job.model, "analysis_type": job.analysis_type,
            "file_uri": job.file_uri, "key_name": job.key_name, "usage": data.get("usageMetadata", {}),
        }
        return result

    @staticmethod
    def _prompt(analysis_type: str, custom_prompt: str | None) -> str:
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
        try:
            message = response.json().get("error", {}).get("message")
        except ValueError:
            message = None
        raise VideoAnalysisError(f"Unable to {action}: {message or f'HTTP {response.status_code}'}", response.status_code)
