from __future__ import annotations

import asyncio
from dataclasses import replace
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from commands import elite_broadcast as command
from core import broadcast_store as store, config


class RundownInputTests(unittest.TestCase):
    def test_rundown_parser_accepts_minute_values_and_normalizes_whitespace(self):
        self.assertEqual(
            command.parse_rundown(" Вступление\n шоу=2 | Игра = 10 "),
            (store.Segment("Вступление шоу", 120), store.Segment("Игра", 600)),
        )
        for bad in ("", "|", "=4", "Название", "Название=0", "Название=121",
                    "Название=3.5", "Название=-4", "Название=٥", "Название=5 |"):
            with self.subTest(bad=bad), self.assertRaises(store.BroadcastUnavailable):
                command.parse_rundown(bad)


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict("os.environ", {"VIVIAN_DATA_DIR": directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        local = patch.object(config, "DATABASE_URL", "")
        local.start()
        self.addCleanup(local.stop)
        self.plan = command.parse_rundown("@everyone=2 | Игра=3 | Финал=1")

    async def create(self, run_id=100, *, now=100, bind=True):
        result = await store.create(run_id, 10, 20, 30, "Пульт @here", self.plan, now=now)
        return await store.bind_message(10, 20, run_id, 40) if bind else result

    async def action(self, request, name, *, now, user=30, manager=False, board=None, next_segment=None):
        return await store.change(10, 20, user, request, name, now=now, manager=manager, board=board,
                                  next_segment=next_segment)

    async def test_racing_starts_and_retry_use_only_one_slot(self):
        started = await asyncio.gather(self.create(101, bind=False), self.create(102, bind=False),
                                       return_exceptions=True)
        self.assertEqual(sum(isinstance(item, store.Broadcast) for item in started), 1)
        state = next(item for item in started if isinstance(item, store.Broadcast))
        self.assertEqual((await self.create(state.id, bind=False)).id, state.id)
        with self.assertRaisesRegex(store.BroadcastUnavailable, "ещё публикуется"):
            await self.action(103, "pause", now=120)
        recovered = await self.create(110, now=191, bind=False)
        self.assertEqual(recovered.id, 110)
        self.assertIsNone(recovered.message_id)

    async def test_pause_resume_and_end_audit_count_active_time_only(self):
        await self.create()
        pause = await self.action(101, "pause", now=130)
        self.assertIsNone(pause.anchor)
        self.assertEqual(pause.elapsed_at(200), 30)
        repeated = await self.action(101, "pause", now=200)
        self.assertEqual(repeated, pause)
        resumed = await self.action(102, "pause", now=210)
        self.assertEqual((resumed.anchor, resumed.elapsed_at(218)), (210, 38))
        changed = await self.action(103, "next", now=240)
        self.assertEqual((changed.current_index, changed.segments[0].actual_seconds), (1, 60))
        ended = await self.action(104, "end", now=280)
        self.assertEqual([item.actual_seconds for item in ended.segments], [60, 40, None])
        self.assertEqual(ended.finished_at, 280)
        self.assertEqual(await self.action(105, "end", now=290), ended)

    async def test_scope_permissions_and_near_simultaneous_next_suppression(self):
        await self.create()
        with self.assertRaisesRegex(store.BroadcastUnavailable, "Управлять событиями"):
            await self.action(101, "next", user=99, now=150)
        for board in ((99, 40), (100, 41)):
            with self.subTest(board=board), self.assertRaisesRegex(store.BroadcastUnavailable, "предыдущему"):
                await self.action(102, "next", board=board, now=150)
        changed = await self.action(103, "next", manager=True, user=99, board=(100, 40), now=150)
        self.assertEqual(changed.current_index, 1)
        with self.assertRaisesRegex(store.BroadcastUnavailable, "только что"):
            await self.action(104, "next", board=(100, 40), now=151)
        with self.assertRaisesRegex(store.BroadcastUnavailable, "устарело"):
            await self.action(102, "next", now=155)

    async def test_stored_state_expires_and_can_be_finalized_at_the_limit(self):
        original = await self.create()
        expiry = original.expires_at
        with self.assertRaisesRegex(store.BroadcastUnavailable, "12 часов"):
            await self.action(101, "next", now=expiry + 5)
        ended = await self.action(102, "end", now=expiry + 6)
        self.assertEqual((ended.finished_at, ended.segments[0].actual_seconds), (expiry, store.MAX_LIFETIME))
        replacement = await self.create(200, bind=False, now=expiry + 7)
        self.assertEqual(replacement.id, 200)
        with self.assertRaisesRegex(store.BroadcastUnavailable, "предыдущему"):
            await self.action(201, "next", now=expiry + 8, board=(100, 40))

    async def test_plan_and_view_limits_and_dynamic_buttons(self):
        state = await self.create()
        view = command.broadcast_card(state, now=120)
        self.assertTrue(view.has_components_v2())
        self.assertTrue(view.is_persistent())
        controls = [child for child in view.walk_children() if isinstance(child, command.BroadcastAction)]
        self.assertEqual({child.action for child in controls}, {"next", "pause"})
        display = "\n".join(child.content for child in view.walk_children() if isinstance(child, discord.ui.TextDisplay))
        self.assertNotIn("@here", display)
        self.assertNotIn("@everyone", display)
        self.assertIn("<t:220:R>", display)
        last = replace(state, current_index=len(state.segments) - 1)
        menu = command.broadcast_card(last, now=120)
        self.assertTrue(next(item for item in menu.walk_children()
                             if isinstance(item, command.BroadcastAction) and item.action == "next").item.disabled)
        with self.assertRaisesRegex(store.BroadcastUnavailable, "12 часов"):
            await store.create(200, 10, 22, 30, "Длинный", (store.segment("Блок", 120),) * 7, now=130)

    async def test_command_sends_one_card_and_edits_in_place(self):
        shown = SimpleNamespace(id=40, jump_url="https://discord.com/channels/10/20/40", edit=AsyncMock())
        channel = MagicMock(spec=discord.TextChannel, id=20)
        channel.send = AsyncMock(return_value=shown)
        channel.get_partial_message.return_value = shown
        permissions = discord.Permissions(view_channel=True, send_messages=True, create_events=True)

        def interaction(request):
            return SimpleNamespace(
                id=request, guild_id=10, channel=channel, user=SimpleNamespace(id=30),
                permissions=permissions, app_permissions=permissions, response=SimpleNamespace(
                    defer=AsyncMock(), is_done=lambda: True,
                ), edit_original_response=AsyncMock(),
            )

        initial = interaction(100)
        with patch.object(store.time, "time", return_value=100):
            await command.start.callback(initial, "Пульт", "Вступление=2 | Вопросы=5")
        with patch.object(store.time, "time", return_value=110):
            await command.next_command.callback(interaction(101))
        with patch.object(store.time, "time", return_value=115):
            await command.end_command.callback(interaction(102))
        channel.send.assert_awaited_once()
        self.assertTrue(channel.send.await_args.kwargs["view"].has_components_v2())
        self.assertFalse(channel.send.await_args.kwargs["allowed_mentions"].everyone)
        self.assertTrue(initial.response.defer.await_args.kwargs["ephemeral"])
        self.assertEqual(shown.edit.await_count, 2)
        final = shown.edit.await_args.kwargs["view"]
        self.assertFalse(any(isinstance(child, command.BroadcastAction) for child in final.walk_children()))
