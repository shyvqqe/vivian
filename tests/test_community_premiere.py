from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from commands import community_premiere as command
from core import config, premiere_store as store


class PremiereInputTests(unittest.TestCase):
    def test_relative_days_hours_and_explicit_time_zone(self):
        now = int(datetime(2026, 12, 1, 18, tzinfo=timezone.utc).timestamp())
        self.assertEqual(command.parse_start("Через 30м", now), now + 1800)
        self.assertEqual(command.parse_start("2h", now), now + 7200)
        self.assertEqual(command.parse_start("2д", now), now + 2 * 86400)
        self.assertEqual(command.parse_start("2026-12-01 21:00+02:00", now), now + 3600)
        for value in ("2026-12-01 21:00", "2026-13-01 21:00Z", "0м", "31д"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                command.parse_start(value, now)

    def test_validates_https_without_credentials_or_control_characters(self):
        self.assertEqual(command._event_url("https://example.com/live?v=1"), "https://example.com/live?v=1")
        for value in ("javascript:alert(1)", "http://example.com", "https:///live",
                      "https://user:pw@example.com/", "https://exa\nmple.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                command._event_url(value)


class PremiereTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict("os.environ", {"VIVIAN_DATA_DIR": directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        local = patch.object(config, "DATABASE_URL", "")
        local.start()
        self.addCleanup(local.stop)
        self.event = store.Premiere(100, 10, 20, 30, "Тест @everyone", "Описание", "https://example.com", 200)

    async def action(self, user, action, *, message=40, channel=20, now=150):
        return await store.interact(100, 10, channel, message, user, action, now=now)

    async def test_concurrent_votes_are_counted_once_and_can_be_changed(self):
        await store.create(self.event, now=100)
        votes = [self.action(user, "going" if user % 2 else "maybe") for user in range(1, 11)]
        await asyncio.gather(*votes)
        state, _ = await self.action(1, "going")
        self.assertEqual((state.message_id, state.going, state.maybe), (40, 5, 5))
        state, _ = await self.action(1, "maybe")
        self.assertEqual((state.going, state.maybe), (4, 6))
        state, _ = await self.action(1, "remove")
        self.assertEqual((state.going, state.maybe), (4, 5))

    async def test_message_scope_and_host_confirmation_gate_cancellation(self):
        await store.create(self.event, now=100)
        await self.action(30, "bind")
        with self.assertRaisesRegex(store.PremiereUnavailable, "исходную карточку"):
            await self.action(1, "going", message=41)
        with self.assertRaisesRegex(store.PremiereUnavailable, "нет сохранённой"):
            await self.action(1, "going", channel=21)
        with self.assertRaisesRegex(store.PremiereUnavailable, "её организатор"):
            await self.action(1, "cancel_prompt")
        with self.assertRaisesRegex(store.PremiereUnavailable, "её организатор"):
            await self.action(1, "cancel")
        state, notice = await self.action(30, "cancel_prompt")
        self.assertIsNone(state.cancelled_at)
        self.assertEqual(notice, "confirm")
        state, notice = await self.action(30, "cancel")
        self.assertEqual((state.cancelled_at, notice), (150, "cancelled"))
        state, notice = await self.action(4, "going")
        self.assertEqual((state.going, notice), (0, "closed"))

    async def test_registration_ends_at_start_even_with_old_buttons(self):
        await store.create(self.event, now=100)
        await self.action(1, "going", now=199)
        state, notice = await self.action(2, "going", now=200)
        self.assertEqual((state.going, notice), (1, "closed"))
        _, notice = await self.action(30, "cancel", now=200)
        self.assertEqual(notice, "closed")

    async def test_rapid_votes_are_reflected_by_one_public_edit(self):
        await store.create(self.event, now=100)
        await self.action(1, "going")
        message = SimpleNamespace(id=40, edit=AsyncMock())
        command._queue_refresh(100, 10, 20, message)
        await self.action(2, "maybe")
        command._queue_refresh(100, 10, 20, message)
        await command._REFRESHES[100].task
        message.edit.assert_awaited_once()
        view = message.edit.await_args.kwargs["view"]
        display = "\n".join(child.content for child in view.walk_children()
                            if isinstance(child, discord.ui.TextDisplay))
        self.assertIn("Придут · 1", display)
        self.assertIn("Возможно · 1", display)

    async def test_native_card_uses_safe_markup_and_changes_controls_when_closed(self):
        current = command.premiere_card(self.event, now=150)
        payload = current.to_components()
        self.assertTrue(current.has_components_v2())
        self.assertTrue(current.is_persistent())
        self.assertTrue(all(item["type"] == 17 for item in payload))
        display = "\n".join(child.content for child in current.walk_children()
                            if isinstance(child, discord.ui.TextDisplay))
        self.assertNotIn("@everyone", display)
        self.assertIn("<t:200:F>", display)
        self.assertIn("<t:200:R>", display)
        self.assertEqual(
            {child.action for child in current.walk_children() if isinstance(child, command.PremiereAction)},
            {"going", "maybe", "remove", "refresh", "cancel_prompt"},
        )
        closed = command.premiere_card(self.event, now=200)
        self.assertEqual({child.action for child in closed.walk_children()
                          if isinstance(child, command.PremiereAction)}, {"refresh"})

    async def test_command_sends_public_native_layout_and_returns_private_card(self):
        channel = MagicMock(spec=discord.TextChannel, id=20)
        channel.send = AsyncMock(return_value=SimpleNamespace(id=40, jump_url="https://discord.com/channels/10/20/40"))
        permissions = discord.Permissions(view_channel=True, send_messages=True, create_events=True)
        interaction = SimpleNamespace(
            id=100, guild_id=10, channel=channel, user=SimpleNamespace(id=30),
            permissions=permissions, app_permissions=permissions,
            response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
            edit_original_response=AsyncMock(),
        )
        with patch.object(command.time, "time", return_value=100):
            await command.premiere.callback(interaction, "Ночной просмотр", "45м")
        payload = channel.send.await_args.kwargs
        self.assertTrue(payload["view"].has_components_v2())
        self.assertTrue(payload["silent"])
        self.assertFalse(payload["allowed_mentions"].everyone)
        self.assertTrue(interaction.response.defer.await_args.kwargs["ephemeral"])
        self.assertTrue(interaction.edit_original_response.await_args.kwargs["view"].has_components_v2())
        state, _ = await self.action(30, "refresh")
        self.assertEqual((state.message_id, state.starts_at), (40, 2800))
