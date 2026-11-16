"""Offline tests for Vivian's public video downloader; no network required."""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from commands import video_download
from utils import video_downloader as mod


class VideoUrlTests(unittest.TestCase):
    def test_valid_video_urls(self):
        examples = {
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "YouTube",
            "https://m.youtube.com/shorts/dQw4w9WgXcQ": "YouTube Shorts",
            "https://youtu.be/dQw4w9WgXcQ": "YouTube",
            "https://www.tiktok.com/@user/video/7234567890123456789": "TikTok",
            "https://vm.tiktok.com/ZMTest123/": "TikTok",
            "https://vt.tiktok.com/ZSabcdefg/": "TikTok",
            "https://www.facebook.com/reel/10000000/": "Facebook",
            "https://www.facebook.com/watch/?v=123456": "Facebook",
            "https://www.facebook.com/share/r/abcDEFG123/": "Facebook",
            "https://fb.watch/xyz12345/": "Facebook",
            "https://www.instagram.com/reel/ABCDEFGHIJK/": "Instagram",
            "https://www.instagram.com/p/ABCDEFGHIJK/": "Instagram",
        }
        for url, expected in examples.items():
            with self.subTest(url=url):
                result = mod.validate_video_url(url)
                self.assertEqual(result.platform, expected)
                self.assertEqual(result.url, url)

    def test_reject_unsafe_or_unsupported_links(self):
        invalid = (
            "", "  https://youtu.be/dQw4w9WgXcQ", "http://youtu.be/dQw4w9WgXcQ",
            "https://youtube.com/playlist?list=PLABC",
            "https://youtube.com/redirect?q=https://127.0.0.1/a",
            "https://www.youtube.com/watch?v=short",
            "https://www.tiktok.com/@u",
            "https://example.com/video.mp4", "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
            "https://user:pass@youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com:8443/watch?v=dQw4w9WgXcQ",
            "https://127.0.0.1/watch?v=dQw4w9WgXcQ", "file:///etc/passwd",
            "https://youtu.be/dQw4w9WgXcQ\n--exec=evil",
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(mod.DownloadError):
                    mod.validate_video_url(url)

    def test_format_and_limits(self):
        for q in (360, 480, 720):
            self.assertIn(f"height<={q}", mod.format_selector(q))
        with self.assertRaises(mod.DownloadError):
            mod.format_selector(999)
        self.assertEqual(mod.upload_limit(8 * mod.MIB), 8 * mod.MIB)
        self.assertEqual(mod.upload_limit(30 * mod.MIB), 20 * mod.MIB)
        with mock.patch.dict("os.environ", {"VIVIAN_VIDEO_MAX_MB": "40"}):
            self.assertEqual(mod.upload_limit(60 * mod.MIB), 40 * mod.MIB)
            self.assertEqual(mod.upload_limit(12 * mod.MIB), 12 * mod.MIB)

    def test_error_sanitization(self):
        self.assertIn("аккаунт", mod._safe_message("ERROR: Sign in to confirm you are not a bot"))
        self.assertIn("частоту", mod._safe_message("HTTP Error 429"))
        self.assertNotIn("token", mod._safe_message("token=secret123"))
        self.assertIn("requirements.txt", mod._safe_message("No module named yt_dlp"))


class FakeProcess:
    def __init__(self, output_dir: Path, content: bytes, *, stderr=b"", returncode=0):
        self.output_dir = output_dir
        self.content = content
        self.exitcode = returncode
        self.returncode = None
        self.stderr = stderr
        self.terminated = False

    async def communicate(self):
        await asyncio.sleep(0)
        if self.content:
            (self.output_dir / "video.mp4").write_bytes(self.content)
        self.returncode = self.exitcode
        return b"", self.stderr

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class VideoDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_file_size(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            seen = {}
            async def fake_exec(*args, **kwargs):
                seen['args'] = args
                return FakeProcess(path, b"fake-mp4-bytes")
            with mock.patch.object(mod.asyncio, "create_subprocess_exec", new=fake_exec):
                result = await mod.fetch_video("https://youtu.be/dQw4w9WgXcQ", path, 2 * mod.MIB)
            self.assertEqual(result.size, len(b"fake-mp4-bytes"))
            self.assertEqual(result.filename, "Vivian_YouTube.mp4")
            self.assertIn("--max-filesize", seen['args'])
            self.assertEqual(seen['args'][-1], "https://youtu.be/dQw4w9WgXcQ")

    async def test_too_large_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            async def fake_exec(*a, **k):
                return FakeProcess(path, b"x" * 4096)
            with mock.patch.object(mod.asyncio, "create_subprocess_exec", new=fake_exec):
                with self.assertRaises(mod.DownloadTooLarge):
                    await mod._run_download(mod.VideoRequest("https://youtu.be/dQw4w9WgXcQ", "YouTube"), path, 200, 480)

    async def test_timeout_terminates_process(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            class SlowProcess(FakeProcess):
                async def communicate(self):
                    await asyncio.sleep(10)
                    self.returncode = 0
                    return b"", b""
            proc = SlowProcess(path, b"")
            async def fake_exec(*a, **k):
                return proc
            with mock.patch.object(mod.asyncio, "create_subprocess_exec", new=fake_exec), \
                    mock.patch.object(mod, "timeout_seconds", return_value=1):
                with self.assertRaisesRegex(mod.DownloadError, "долго"):
                    await mod._run_download(mod.VideoRequest("https://youtu.be/dQw4w9WgXcQ", "YouTube"), path, 1000, 480)
            self.assertTrue(proc.terminated)

    async def test_private_video_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            async def fake_exec(*a, **k):
                return FakeProcess(path, b"", stderr=b"You need to login", returncode=1)
            with mock.patch.object(mod.asyncio, "create_subprocess_exec", new=fake_exec):
                with self.assertRaisesRegex(mod.DownloadError, "аккаунт"):
                    await mod._run_download(mod.VideoRequest("https://youtu.be/dQw4w9WgXcQ", "YouTube"), path, 1000, 480)


class VideoCommandDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_slash_upload_is_one_public_v2_file_with_a_private_status_link(self):
        data = b"sample-mp4"
        output_path = None
        jump_url = "https://discord.com/channels/1/2/3"

        async def fetch(_url, directory, _limit, *, quality):
            nonlocal output_path
            self.assertEqual(quality, 480)
            output_path = directory / "test.mp4"
            output_path.write_bytes(data)
            return SimpleNamespace(path=output_path, filename="test.mp4", platform="YouTube", size=len(data))

        async def publish(**payload):
            self.assertNotIn("content", payload)
            self.assertEqual(payload["nonce"], "vd:42")
            view = payload["view"]
            self.assertTrue(view.has_components_v2())
            self.assertEqual(
                [item.media.url for item in view.walk_children() if isinstance(item, discord.ui.File)],
                ["attachment://test.mp4"],
            )
            self.assertEqual(payload["files"][0].fp.read(), data)
            return SimpleNamespace(jump_url=jump_url)

        channel = SimpleNamespace(send=mock.AsyncMock(side_effect=publish))
        interaction = SimpleNamespace(
            id=42, channel=channel, filesize_limit=5 * mod.MIB,
            app_permissions=discord.Permissions(send_messages=True, attach_files=True),
        )
        responses = video_download.command_responses
        with (mock.patch.object(mod, "fetch_video", side_effect=fetch),
              mock.patch.object(responses, "defer_command", new=mock.AsyncMock(return_value=True)) as defer,
              mock.patch.object(responses, "finish_deferred", new=mock.AsyncMock()) as finish):
            await video_download.download_video_slash.callback(interaction, "https://youtu.be/dQw4w9WgXcQ")
        defer.assert_awaited_once_with(interaction, ephemeral=True)
        channel.send.assert_awaited_once()
        finish.assert_awaited_once_with(interaction, f"[Перейти к видео]({jump_url})")
        self.assertIsNotNone(output_path)
        self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
