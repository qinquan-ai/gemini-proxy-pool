import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app.core.key_manager import KeyManager
from app.core.video_analysis import VideoAnalysisError, VideoAnalysisService
from app.core.video_sources import parse_video_source, validate_public_url


VALID_MP4_HEADER = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 24


def _analysis_response():
    result = {
        "summary": "test",
        "timeline": [],
        "reusable_patterns": [],
        "remotion_plan": {"fps": 30, "components": []},
    }
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"text": json.dumps(result)}]}}
            ],
            "usageMetadata": {"totalTokenCount": 10},
        },
    )


class VideoAnalysisTests(unittest.IsolatedAsyncioTestCase):
    def test_douyin_share_text_extracts_the_public_short_link(self):
        source = parse_video_source(
            "1.58 foq:/ 教程 https://v.douyin.com/FWM0O0W99R8/ "
            "复制此链接，打开Dou音搜索"
        )
        self.assertEqual(source.kind, "douyin")
        self.assertEqual(source.value, "https://v.douyin.com/FWM0O0W99R8/")

    def test_bilibili_share_text_extracts_the_video_link(self):
        source = parse_video_source(
            "这个视频不错 https://www.bilibili.com/video/BV1xx411c7mD/ 看看"
        )
        self.assertEqual(source.kind, "bilibili")
        self.assertIn("BV1xx411c7mD", source.value)

    def test_arbitrary_public_page_uses_generic_web_resolver(self):
        source = parse_video_source("https://media.example.com/tutorial")
        self.assertEqual(source.kind, "web")

    def test_private_and_credentialed_urls_are_rejected(self):
        for url in (
            "http://127.0.0.1/video.mp4",
            "http://localhost/video.mp4",
            "https://user:password@example.com/video.mp4",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_video_source(url)

    def test_dns_resolution_rejects_private_target(self):
        with mock.patch(
            "app.core.video_sources.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("192.168.1.8", 0))],
        ):
            with self.assertRaisesRegex(ValueError, "private or local"):
                validate_public_url("https://video.example.test/watch")

    def test_synthetic_dns_requires_explicit_opt_in(self):
        fake_dns = [(None, None, None, None, ("198.18.0.186", 0))]
        with mock.patch(
            "app.core.video_sources.socket.getaddrinfo", return_value=fake_dns
        ):
            with mock.patch.dict("os.environ", {}, clear=False):
                os.environ.pop("VIDEO_ALLOW_SYNTHETIC_DNS", None)
                with self.assertRaisesRegex(ValueError, "private or local"):
                    validate_public_url("https://video.example.test/watch")
            with mock.patch.dict(
                "os.environ", {"VIDEO_ALLOW_SYNTHETIC_DNS": "true"}
            ):
                validate_public_url("https://video.example.test/watch")

    async def test_youtube_job_completes_in_background(self):
        async def handler(request: httpx.Request):
            self.assertIn(":generateContent", request.url.path)
            return _analysis_response()

        with tempfile.TemporaryDirectory() as job_dir:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                service = VideoAnalysisService(
                    KeyManager("test|fake-key"), client, job_dir=job_dir,
                    download_dir=Path(job_dir) / "downloads"
                )
                submitted = await service.submit(
                    "https://www.youtube.com/watch?v=test", analysis_type="remotion"
                )
                await service.tasks[submitted["id"]]
                completed = service.get(submitted["id"])
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(completed["result"]["summary"], "test")

                restored = VideoAnalysisService(
                    KeyManager("test|fake-key"), client, job_dir=job_dir,
                    download_dir=Path(job_dir) / "downloads-restored"
                ).get(submitted["id"])
                self.assertEqual(restored["status"], "completed")
                self.assertEqual(restored["result"]["summary"], "test")

    async def test_local_file_upload_poll_and_analysis(self):
        calls = []

        async def handler(request: httpx.Request):
            calls.append((request.method, str(request.url)))
            if request.url.host == "upload.test":
                return httpx.Response(
                    200,
                    json={
                        "file": {
                            "name": "files/test",
                            "uri": "https://files.test/video",
                            "mimeType": "video/mp4",
                        }
                    },
                )
            if request.url.path == "/upload/v1beta/files":
                return httpx.Response(
                    200, headers={"x-goog-upload-url": "https://upload.test/final"}
                )
            if request.method == "GET" and request.url.path.endswith("/files/test"):
                return httpx.Response(
                    200,
                    json={
                        "name": "files/test",
                        "uri": "https://files.test/video",
                        "mimeType": "video/mp4",
                        "state": "ACTIVE",
                    },
                )
            return _analysis_response()

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as job_dir:
            video = Path(directory) / "clip.mp4"
            video.write_bytes(VALID_MP4_HEADER)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                service = VideoAnalysisService(
                    KeyManager("test|fake-key"), client, job_dir=job_dir,
                    download_dir=Path(job_dir) / "downloads"
                )
                submitted = await service.submit(str(video))
                await service.tasks[submitted["id"]]
                completed = service.get(submitted["id"])
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(completed["file_uri"], "https://files.test/video")
                self.assertEqual(len(calls), 5)
                self.assertEqual(calls[-1][0], "DELETE")
                self.assertTrue(calls[-1][1].endswith("/files/test"))

    async def test_arbitrary_http_url_is_queued_for_web_resolution(self):
        with tempfile.TemporaryDirectory() as job_dir:
            async with httpx.AsyncClient() as client:
                service = VideoAnalysisService(
                    KeyManager("test|fake-key"), client, job_dir=job_dir,
                    download_dir=Path(job_dir) / "downloads"
                )
                submitted = await service.submit("https://example.com/video.mp4")
                self.assertEqual(submitted["source_type"], "web")
                await service.cancel(submitted["id"])

    async def test_html_renamed_to_mp4_is_rejected_before_upload(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as job_dir:
            video = Path(directory) / "verification.mp4"
            video.write_text("<html>captcha</html>", encoding="utf-8")
            async with httpx.AsyncClient() as client:
                service = VideoAnalysisService(
                    KeyManager("test|fake-key"), client, job_dir=job_dir,
                    download_dir=Path(job_dir) / "downloads"
                )
                with self.assertRaisesRegex(VideoAnalysisError, "not a real video"):
                    await service.submit(str(video))

    async def test_ram_streaming_bytes_upload_and_zero_disk(self):
        calls = []

        async def handler(request: httpx.Request):
            calls.append((request.method, str(request.url)))
            if request.url.host == "upload.test":
                return httpx.Response(
                    200,
                    json={
                        "file": {
                            "name": "files/ram_test",
                            "uri": "https://files.test/ram_video",
                            "mimeType": "video/mp4",
                        }
                    },
                )
            if request.url.path == "/upload/v1beta/files":
                return httpx.Response(
                    200, headers={"x-goog-upload-url": "https://upload.test/final"}
                )
            if request.method == "GET" and request.url.path.endswith("/files/ram_test"):
                return httpx.Response(
                    200,
                    json={
                        "name": "files/ram_test",
                        "uri": "https://files.test/ram_video",
                        "mimeType": "video/mp4",
                        "state": "ACTIVE",
                    },
                )
            return _analysis_response()

        with tempfile.TemporaryDirectory() as job_dir:
            downloads = Path(job_dir) / "downloads"
            downloads.mkdir()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                service = VideoAnalysisService(
                    KeyManager("test|fake-key"), client, job_dir=job_dir,
                    download_dir=downloads
                )
                # Mock _download_remote_sync to return bytes (in-RAM stream)
                with mock.patch.object(
                    service,
                    "_download_remote_sync",
                    return_value=(VALID_MP4_HEADER, {"title": "RAM Video", "webpage_url": "https://example.com/v"}),
                ):
                    submitted = await service.submit("https://example.com/video.mp4")
                    await service.tasks[submitted["id"]]
                    completed = service.get(submitted["id"])
                    self.assertEqual(completed["status"], "completed")
                    self.assertEqual(completed["file_uri"], "https://files.test/ram_video")
                    # Assert no stray video files left in downloads
                    job_workspace = downloads / submitted["id"]
                    self.assertFalse(job_workspace.exists())

    async def test_auto_archive_curation_threshold(self):
        high_score_result = {
            "summary": "Epic Drama",
            "curation_score": 8.5,
            "plot_and_characters": {"main_plot": "A", "conflict_and_twists": "B"},
            "watermark_and_quality": {"has_platform_watermark": False, "has_author_watermark": False, "visual_clarity": "1080p"},
            "tiktok_potential": {"opening_hook_score": 9, "curation_score": 8.5},
        }

        async def handler(request: httpx.Request):
            if request.url.host == "upload.test":
                return httpx.Response(
                    200,
                    json={"file": {"name": "files/test", "uri": "https://files.test/v", "mimeType": "video/mp4"}},
                )
            if request.url.path == "/upload/v1beta/files":
                return httpx.Response(200, headers={"x-goog-upload-url": "https://upload.test/final"})
            if request.method == "GET" and request.url.path.endswith("/files/test"):
                return httpx.Response(200, json={"name": "files/test", "uri": "https://files.test/v", "state": "ACTIVE"})
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": json.dumps(high_score_result)}]}}]},
            )

        with tempfile.TemporaryDirectory() as archive_dir, tempfile.TemporaryDirectory() as job_dir:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                service = VideoAnalysisService(
                    KeyManager("test|fake-key"), client, job_dir=job_dir,
                    download_dir=Path(job_dir) / "downloads"
                )
                with mock.patch.object(
                    service,
                    "_download_remote_sync",
                    return_value=(VALID_MP4_HEADER, {"title": "Test Drama", "webpage_url": "https://example.com/drama"}),
                ):
                    submitted = await service.submit(
                        "https://example.com/drama",
                        analysis_type="curation",
                        archive_dir=archive_dir,
                        save_threshold=7.0,
                    )
                    await service.tasks[submitted["id"]]
                    completed = service.get(submitted["id"])
                    self.assertEqual(completed["status"], "completed")
                    self.assertIsNotNone(completed.get("saved_path"))
                    saved_video = Path(completed["saved_path"])
                    self.assertTrue(saved_video.is_file())
                    self.assertTrue(saved_video.name.startswith("[Score-8.5]"))
                    self.assertTrue(saved_video.with_suffix(".json").is_file())


if __name__ == "__main__":
    unittest.main()
