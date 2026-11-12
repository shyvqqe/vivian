"""Offline tests for automatic video offers and cover-with-audio rendering."""
from __future__ import annotations

import ast
import asyncio
from collections import OrderedDict
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from utils import video_downloader as videos

ROOT = Path(__file__).resolve().parents[1]


def isolated_function(name: str, namespace: dict):
    tree = ast.parse((ROOT / "commands/video_auto.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "video_auto.py", "exec"), namespace)
    return namespace[name]


class VideoDetectionTests(unittest.TestCase):
    def test_link_in_message_and_punctuation(self):
        examples = [
            ("вот https://www.tiktok.com/@user/video/1234567890123456789!", "TikTok"),
            ("https://www.instagram.com/reel/abcdefg/?igsh=abc)", "Instagram"),
            ("https://www.facebook.com/reel/12345", "Facebook"),
            ("shorts https://www.youtube.com/shorts/dQw4w9WgXcQ?si=abc", "YouTube Shorts"),
            ("https://youtu.be/dQw4w9WgXcQ", "YouTube"),
            ("https://not-real.example/x https://youtu.be/dQw4w9WgXcQ", "YouTube"),
        ]
        for text, source in examples:
            with self.subTest(text=text):
                self.assertEqual(videos.find_video_url(text).platform, source)

    def test_rejects_spoofed_and_non_video_urls(self):
        for text in (
            "https://youtube.com.bad.example/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/playlist?list=PL123",
            "https://127.0.0.1/private",
            "Просто текст без ссылок",
        ):
            with self.subTest(text=text):
                self.assertIsNone(videos.find_video_url(text))

    def test_offer_disabled_and_skip_commands(self):
        def get(text, *, guild=True, flag=True):
            namespace = {"enabled": lambda: flag, "videos": videos}
            method = isolated_function("get_video_offer", namespace)
            return method(SimpleNamespace(content=text, guild=(object() if guild else None)))
        url = "https://youtu.be/dQw4w9WgXcQ"
        self.assertIsNotNone(get(f"видео {url}"))
        self.assertIsNone(get(f"!download {url}"))
        self.assertIsNone(get(f"a.jsk {url}"))
        self.assertIsNone(get(url, guild=False))
        self.assertIsNone(get(url, flag=False))


class OfferDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompts_only_once_after_permission_checks(self):
        request = videos.VideoRequest("https://youtu.be/dQw4w9WgXcQ", "YouTube")
        fake_message = SimpleNamespace()
        fake_message.guild = SimpleNamespace(id=100, me=object())
        fake_message.author = SimpleNamespace(id=101)
        fake_message.reply = AsyncMock(return_value=SimpleNamespace())
        permissions = SimpleNamespace(send_messages=True, view_channel=True,
                                      read_message_history=True, send_messages_in_threads=True)
        fake_message.channel = SimpleNamespace(permissions_for=lambda user: permissions)
        class FakeDiscord:
            Thread = type("Thread", (), {})
            HTTPException = Exception
            class AllowedMentions:
                @staticmethod
                def none(): return None
        class FakeView:
            def __init__(self, *, author_id, request):
                self.author_id, self.request = author_id, request
                self.offer_message = None
        namespace = {
            "get_video_offer": lambda m: request,
            "discord": FakeDiscord,
            "VideoOffer": FakeView,
            "_recent": OrderedDict(),
            "_MAX_RECENT": 2048,
            "_PROMPT_COOLDOWN_SECONDS": 25,
            "time": SimpleNamespace(monotonic=lambda: 100),
        }
        offer = isolated_function("maybe_offer_download", namespace)
        self.assertTrue(await offer(fake_message))
        self.assertEqual(fake_message.reply.await_count, 1)
        self.assertTrue(await offer(fake_message))
        self.assertEqual(fake_message.reply.await_count, 1)
        permissions.send_messages = False
        self.assertFalse(await offer(fake_message))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
class CoverEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_still_cover_mp4_has_video_and_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            generated = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=c=blue:s=360x640:r=10", "-f", "lavfi",
                "-i", "sine=frequency=440:sample_rate=44100", "-t", "1.2",
                "-c:v", "mpeg4", "-q:v", "3", "-c:a", "aac", "-shortest", str(source),
            ], capture_output=True)
            self.assertEqual(generated.returncode, 0, generated.stderr.decode(errors="replace"))
            result = await videos._render_cover_music(source, Path(directory), 5 * videos.MIB)
            self.assertTrue(result.is_file())
            self.assertGreater(result.stat().st_size, 100)
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                "-of", "csv=p=0", str(result),
            ], capture_output=True, text=True)
            self.assertIn("audio", probe.stdout)
            self.assertIn("video", probe.stdout)


if __name__ == "__main__":
    unittest.main()
