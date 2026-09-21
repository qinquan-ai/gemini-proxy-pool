import io
import mimetypes
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx

from app.core.video_sources import parse_video_source, validate_public_url

# Clean defective no_proxy entries (e.g. ::1 on Windows causing httpx InvalidURL: Invalid port: ':1')
no_proxy_env = os.environ.get("no_proxy", "")
if "::1" in no_proxy_env:
    cleaned = re.sub(r',?::1(?:/\d+)?', '', no_proxy_env).strip(',')
    os.environ["no_proxy"] = cleaned

SUPPORTED_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-matroska",
    "video/x-msvideo",
    "video/x-flv",
    "video/mpeg",
}


class VideoDownloadError(Exception):
    """Raised when video downloading or sniffing fails."""
    pass


def validate_video_file(path: Path, max_bytes: int = 2 * 1024 * 1024 * 1024) -> None:
    if not path.is_file():
        raise VideoDownloadError("Video downloader returned no media file")
    if path.stat().st_size > max_bytes:
        raise VideoDownloadError("Video exceeds the configured download limit")
    mime_type = mimetypes.guess_type(path.name)[0]
    if mime_type not in SUPPORTED_VIDEO_MIME_TYPES:
        raise VideoDownloadError(f"Unsupported video MIME type: {mime_type}")
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
        raise VideoDownloadError(
            "The source is not a real video file; it may be an HTML verification page"
        )


def validate_video_bytes(data: bytes, max_bytes: int = 2 * 1024 * 1024 * 1024) -> None:
    if not data:
        raise VideoDownloadError("Video downloader returned empty media data")
    if len(data) > max_bytes:
        raise VideoDownloadError("Video exceeds the configured download limit")
    header = data[:32]
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
        raise VideoDownloadError(
            "The downloaded stream is not a real video file; it may be an HTML verification page"
        )


def find_browser_executable() -> Path | None:
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


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|\r\n\t]', "_", name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:80]


class VideoDownloader:
    """Universal video downloader & sniffer supporting yt-dlp, Playwright sniffing, and dual-track streaming."""

    def __init__(
        self,
        download_dir: str | Path | None = None,
        download_timeout: float = 180.0,
        cookies_from_browser: str = "",
        max_bytes: int = 2 * 1024 * 1024 * 1024,
    ):
        configured_download_dir = download_dir or os.getenv(
            "VIDEO_DOWNLOAD_DIR", "data/video_downloads"
        )
        self.download_dir = Path(configured_download_dir).resolve()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.download_timeout = download_timeout
        self.cookies_from_browser = cookies_from_browser or os.getenv(
            "VIDEO_YTDLP_COOKIES_FROM_BROWSER", ""
        ).strip()
        self.max_bytes = max_bytes

    def download_media_stream(
        self,
        url: str,
        source_type: str,
        workspace: Path,
    ) -> tuple[Path | bytes, dict]:
        """Download video into workspace or RAM buffer using yt-dlp with Playwright browser fallback."""
        try:
            validate_public_url(url)
        except ValueError as exc:
            raise VideoDownloadError(str(exc)) from exc

        try:
            from yt_dlp import YoutubeDL
        except ImportError as exc:
            raise VideoDownloadError(
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
            "max_filesize": self.max_bytes,
        }
        if self.cookies_from_browser:
            options["cookiesfrombrowser"] = (self.cookies_from_browser,)

        info = None
        prepared_path = None
        try:
            with YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=True)
                prepared_path = Path(downloader.prepare_filename(info))
        except Exception as exc:
            if self.cookies_from_browser:
                try:
                    options_no_cookies = dict(options)
                    options_no_cookies.pop("cookiesfrombrowser", None)
                    with YoutubeDL(options_no_cookies) as downloader:
                        info = downloader.extract_info(url, download=True)
                        prepared_path = Path(downloader.prepare_filename(info))
                except Exception as inner_exc:
                    exc = inner_exc

            if info is None:
                if source_type in {"douyin", "web"}:
                    return self._download_page_with_browser(
                        url, source_type, workspace, exc
                    )
                raise VideoDownloadError(
                    f"{source_type} returned no downloadable public stream. "
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
            raise VideoDownloadError(
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

        # RAM vs ROM Dual-track check: if <= 50MB, read into RAM buffer and unlink file
        if path.stat().st_size <= 50 * 1024 * 1024:
            media_bytes = path.read_bytes()
            try:
                path.unlink()
            except OSError:
                pass
            return media_bytes, metadata
        return path, metadata

    def _download_page_with_browser(
        self,
        url: str,
        source_type: str,
        workspace: Path,
        yt_dlp_error: Exception,
    ) -> tuple[Path | bytes, dict]:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise VideoDownloadError(
                "The page requires browser extraction and Playwright is not installed"
            ) from exc

        executable = find_browser_executable()
        if not executable:
            raise VideoDownloadError(
                "The page requires browser extraction and no supported local Chrome/Edge executable was found"
            ) from yt_dlp_error

        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        )
        captured_media: list[tuple[str, str, int]] = []
        best_dom: dict | None = None
        title = ""
        final_url = url
        cookies: dict[str, str] = {}

        TIKTOK_PROFILE = Path(r"D:\project_qin\Video_project\TikTok\tiktok_browser\data\browser_profile")
        profile_dir = TIKTOK_PROFILE if TIKTOK_PROFILE.exists() else (workspace.parent / "data" / "browser_profile")
        profile_dir.mkdir(parents=True, exist_ok=True)

        headless_env = os.getenv("VIDEO_BROWSER_HEADLESS", "false").strip().lower()
        headless = headless_env in {"true", "1", "yes"}

        try:
            with sync_playwright() as playwright:
                args = [
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ]
                kwargs = {
                    "user_data_dir": str(profile_dir),
                    "headless": headless,
                    "args": args,
                    "locale": "zh-CN",
                    "user_agent": user_agent,
                }
                if executable:
                    kwargs["executable_path"] = str(executable)

                context = playwright.chromium.launch_persistent_context(**kwargs)
                try:
                    page = context.pages[0] if context.pages else context.new_page()

                    # Anti-detect script
                    page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                        window.navigator.chrome = { runtime: {} };
                    """)

                    def on_response(res):
                        try:
                            res_url = res.url
                            ct = (res.headers.get("content-type") or "").lower()
                            if "uuu_265.mp4" in res_url or res_url.endswith(".js") or res_url.endswith(".css"):
                                return
                            if "media-video" in res_url:
                                captured_media.append((res_url, ct, 10))
                            elif "douyinvod.com" in res_url:
                                captured_media.append((res_url, ct, 8))
                            elif ct.startswith("video/"):
                                captured_media.append((res_url, ct, 5))
                        except Exception:
                            pass

                    page.on("response", on_response)
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)

                    try:
                        page.wait_for_function(
                            """
                            () => [...document.querySelectorAll('video')].some(
                              (v) => (v.currentSrc || v.src) && Number.isFinite(v.duration) && v.duration > 3
                            )
                            """,
                            timeout=30_000,
                        )
                    except PlaywrightTimeoutError:
                        time.sleep(2)

                    # Performance API sniffing
                    perf_urls = page.evaluate(
                        """
                        () => performance.getEntriesByType('resource')
                            .map(e => e.name)
                            .filter(u => (u.includes('douyinvod.com') || u.includes('media-video') || u.includes('.mp4')) && !u.includes('douyinstatic.com'))
                        """
                    )
                    for pu in perf_urls:
                        prio = 9 if "media-video" in pu else 7
                        captured_media.append((pu, "video/mp4", prio))

                    # Inspect DOM video tags
                    dom_videos = page.evaluate(
                        """
                        () => [...document.querySelectorAll('video')].map(v => ({
                            src: v.currentSrc || v.src || '',
                            duration: v.duration || 0,
                            width: v.videoWidth || 0,
                            height: v.videoHeight || 0
                        }))
                        """
                    )
                    for dv in dom_videos:
                        s = dv.get("src", "")
                        if s and not s.startswith("blob:") and (s.startswith("http://") or s.startswith("https://")):
                            if not best_dom or dv.get("duration", 0) > best_dom.get("duration", 0):
                                best_dom = dv

                    title = page.title()
                    final_url = page.url
                    cookies = {
                        cookie["name"]: cookie["value"]
                        for cookie in context.cookies()
                    }
                finally:
                    context.close()
        except PlaywrightTimeoutError as exc:
            raise VideoDownloadError(
                "The page loaded but timed out waiting for playable video stream"
            ) from exc

        target_media_url = None
        if captured_media:
            captured_media.sort(key=lambda item: item[2], reverse=True)
            target_media_url = captured_media[0][0]
        elif best_dom and best_dom.get("src"):
            target_media_url = best_dom["src"]

        if not target_media_url:
            raise VideoDownloadError(
                "The page loaded but no playable public or sniffed video stream was found"
            )

        metadata = {
            "title": title,
            "duration": (best_dom or {}).get("duration"),
            "width": (best_dom or {}).get("width"),
            "height": (best_dom or {}).get("height"),
            "webpage_url": final_url,
            "extractor": f"{source_type}-browser-sniff",
            "source_type": source_type,
            "sniffed_media_url": target_media_url[:120],
        }

        destination = workspace / f"{source_type}.mp4"
        media_result = self._fetch_browser_media(
            target_media_url,
            destination,
            cookies=cookies,
            referer=final_url,
            user_agent=user_agent,
        )
        return media_result, metadata

    def _fetch_browser_media(
        self,
        media_url: str,
        destination: Path,
        *,
        cookies: dict[str, str],
        referer: str,
        user_agent: str,
    ) -> Path | bytes:
        current_url = media_url
        timeout = httpx.Timeout(60, read=self.download_timeout)
        headers = {"User-Agent": user_agent}
        if "douyin" in referer or "douyinvod" in media_url:
            headers["Referer"] = "https://www.douyin.com/"
        else:
            headers["Referer"] = referer

        with httpx.Client(
            follow_redirects=False,
            timeout=timeout,
            headers=headers,
            trust_env=False,
        ) as client:
            for _ in range(6):
                try:
                    validate_public_url(current_url)
                except ValueError as exc:
                    raise VideoDownloadError(str(exc)) from exc
                with client.stream("GET", current_url, cookies=cookies) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise VideoDownloadError(
                                "Video media redirect has no destination"
                            )
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status_code >= 400:
                        raise VideoDownloadError(
                            f"Video media stream returned HTTP {response.status_code}"
                        )
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and not (
                        content_type.startswith("video/")
                        or "octet-stream" in content_type
                        or "application/mp4" in content_type
                    ):
                        raise VideoDownloadError(
                            f"Video media stream returned unexpected content type: {content_type}"
                        )

                    # RAM vs ROM Dual-track streaming
                    ram_threshold = 50 * 1024 * 1024
                    downloaded = 0
                    buffer = io.BytesIO()
                    file_handle = None

                    try:
                        for chunk in response.iter_bytes(1024 * 1024):
                            downloaded += len(chunk)
                            if downloaded > self.max_bytes:
                                raise VideoDownloadError(
                                    "Video exceeds the configured download limit"
                                )
                            if file_handle is not None:
                                file_handle.write(chunk)
                            else:
                                buffer.write(chunk)
                                if downloaded > ram_threshold:
                                    file_handle = destination.open("wb")
                                    file_handle.write(buffer.getvalue())
                                    buffer = None
                    finally:
                        if file_handle is not None:
                            file_handle.close()

                    if file_handle is not None:
                        validate_video_file(destination, self.max_bytes)
                        return destination
                    else:
                        data = buffer.getvalue()
                        validate_video_bytes(data, self.max_bytes)
                        return data

        raise VideoDownloadError("Video media stream exceeded the redirect limit")

    def download(
        self,
        source: str,
        output_dir: str | Path = ".",
        filename: str | None = None,
    ) -> tuple[Path, dict]:
        """Download video directly to output_dir and return (saved_file_path, metadata)."""
        video_source = parse_video_source(source)
        if video_source.kind == "local":
            src_path = Path(video_source.value)
            validate_video_file(src_path, self.max_bytes)
            return src_path, {"title": src_path.stem, "source_type": "local"}

        out_dir = Path(output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        # Temporary workspace for downloading / buffering
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            media_result, metadata = self.download_media_stream(
                video_source.value, video_source.kind, workspace
            )

            # Determine appropriate filename
            if not filename:
                vid = metadata.get("id") or ""
                if not vid and metadata.get("webpage_url"):
                    m = re.search(r'/video/(\d+)', metadata["webpage_url"])
                    if m:
                        vid = m.group(1)

                raw_title = metadata.get("title") or ""
                clean_title = re.sub(r'[\s\-_]*抖音$', '', raw_title).strip()
                if not clean_title or "" in clean_title:
                    clean_title = f"{video_source.kind}_{vid or int(time.time())}"
                clean_title = sanitize_filename(clean_title)

                if vid:
                    target_name = f"{vid}_{clean_title}.mp4"
                else:
                    target_name = f"{clean_title}.mp4"
            else:
                target_name = filename if filename.endswith(".mp4") else f"{filename}.mp4"

            dest_file = out_dir / target_name

            if isinstance(media_result, bytes):
                dest_file.write_bytes(media_result)
            elif isinstance(media_result, Path):
                shutil.copy2(media_result, dest_file)
            else:
                raise VideoDownloadError(f"Unexpected media result type: {type(media_result)}")

            validate_video_file(dest_file, self.max_bytes)
            return dest_file, metadata
