"""Isolated tests for the asynchronous /play acknowledgement, without Discord credentials."""
from __future__ import annotations

import ast
import asyncio
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


SOURCE = Path(__file__).resolve().parents[1] / "commands" / "music_audio.py"
FUNCTIONS = {"_queue_music_tracks", "_report_delayed_start"}


def _isolated_functions(namespace):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in FUNCTIONS]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *selected], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class AsyncPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_play_returns_loading_instead_of_waiting_for_audio_and_reports_failure(self):
        guild_id = 1807
        voice_channel = SimpleNamespace(mention="#voice")
        member = SimpleNamespace(voice=SimpleNamespace(channel=voice_channel))
        voice = SimpleNamespace(channel=voice_channel, is_connected=lambda: True,
                                is_playing=lambda: False, is_paused=lambda: False)
        guild = SimpleNamespace(id=guild_id, me=None, voice_client=voice)
        report_channel = SimpleNamespace(send=AsyncMock())
        state = SimpleNamespace(
            MUSIC_QUEUES=defaultdict(deque), MUSIC_SHUFFLE_MODE=defaultdict(bool),
            MUSIC_CONNECT_LOCKS={}, MUSIC_STARTING=set(), MUSIC_RECOVERY_TASKS={},
            MUSIC_CHANNELS={}, MUSIC_GENERATION=defaultdict(int), MUSIC_LAST_ERRORS={},
        )
        refresh = AsyncMock()
        panels = SimpleNamespace(_refresh_music_player=refresh)
        local_lock = asyncio.Lock()

        def queue_tracks(server_id, tracks):
            state.MUSIC_QUEUES[server_id].extend(tracks)

        async def slow_audio(server_id):
            await asyncio.sleep(2.65)
            state.MUSIC_LAST_ERRORS[server_id] = "Тест: yt-dlp не вернул аудио."
            return False

        ns = _isolated_functions({
            "asyncio": asyncio, "core_music_state": state,
            "commands_music_panels": panels, "_append_music_tracks": queue_tracks,
            "_music_connect_lock": lambda _: local_lock, "_play_next_track": slow_audio,
            "_ensure_ffmpeg": AsyncMock(return_value="/usr/bin/ffmpeg"),
            "ffmpeg_runtime": SimpleNamespace(FFmpegUnavailable=RuntimeError),
            "discord": SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda: None),
                                       DiscordException=Exception),
        })
        ok, message = await ns["_queue_music_tracks"](
            guild, member, [{"title": "Test Song"}], quick_response=True, notify_channel=report_channel,
        )
        self.assertTrue(ok)
        self.assertIn("Загружаю аудио", message)
        self.assertEqual(len(state.MUSIC_QUEUES[guild_id]), 1)
        await asyncio.sleep(0.25)  # audio startup completes independently
        report_channel.send.assert_awaited_once()
        self.assertIn("yt-dlp не вернул", report_channel.send.await_args.args[0])

    async def test_missing_ffmpeg_does_not_connect_to_voice(self):
        voice_channel = SimpleNamespace(connect=AsyncMock())
        member = SimpleNamespace(voice=SimpleNamespace(channel=voice_channel))
        guild = SimpleNamespace(id=42, me=None, voice_client=None)
        state = SimpleNamespace(MUSIC_QUEUES=defaultdict(deque))
        async def no_ffmpeg():
            raise RuntimeError("FFmpeg недоступен: установи requirements.txt")
        ns = _isolated_functions({
            "asyncio": asyncio, "core_music_state": state,
            "_ensure_ffmpeg": no_ffmpeg,
            "ffmpeg_runtime": SimpleNamespace(FFmpegUnavailable=RuntimeError),
        })
        ok, notice = await ns["_queue_music_tracks"](guild, member, [{"title": "Test"}])
        self.assertFalse(ok)
        self.assertIn("FFmpeg", notice)
        voice_channel.connect.assert_not_called()
        self.assertFalse(state.MUSIC_QUEUES[42])

    async def test_stale_audio_failure_does_not_notify_after_new_session(self):
        state = SimpleNamespace(MUSIC_GENERATION=defaultdict(int), MUSIC_LAST_ERRORS={9: "Old failure"})
        state.MUSIC_GENERATION[9] = 2
        guild = SimpleNamespace(id=9, voice_client=None)
        channel = SimpleNamespace(send=AsyncMock())
        ns = _isolated_functions({
            "asyncio": asyncio, "core_music_state": state,
            "discord": SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda: None),
                                       DiscordException=Exception),
        })
        done = asyncio.create_task(asyncio.sleep(0, result=False))
        await ns["_report_delayed_start"](guild, done, 1, channel)
        channel.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
