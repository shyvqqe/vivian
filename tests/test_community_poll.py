from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from commands import community_poll


class PollTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        permissions = discord.Permissions(view_channel=True, send_messages=True, send_polls=True)
        channel = Mock(spec=discord.TextChannel)
        channel.id = 2
        channel.send = AsyncMock(return_value=SimpleNamespace(jump_url="https://discord.com/channels/1/2/4"))
        self.interaction = SimpleNamespace(
            guild_id=1,
            id=3,
            user=SimpleNamespace(id=5),
            channel=channel,
            permissions=permissions,
            app_permissions=permissions,
            response=SimpleNamespace(send_message=AsyncMock(return_value=None)),
            _state=None,
        )
        self.ack = AsyncMock(return_value=True)
        self.finish = AsyncMock()
        community_poll._recent_publications.clear()
        self.addCleanup(community_poll._recent_publications.clear)
        for name, mock in (("defer_command", self.ack), ("finish_deferred", self.finish)):
            patcher = patch.object(community_poll.command_responses, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_poll_data_matches_native_discord_api(self):
        poll = community_poll._build_poll("Во что играем, @everyone? ", " Minecraft |  Team   Fortress 2 ", 4, True)
        self.assertEqual(poll.question, "Во что играем, @\u200beveryone?")
        self.assertEqual([answer.text for answer in poll.answers], ["Minecraft", "Team Fortress 2"])
        self.assertEqual(poll.duration, timedelta(hours=4))
        self.assertTrue(poll.multiple)

    def test_invalid_inputs_are_rejected(self):
        for question, choices in (
            ("   ", "a | b"),
            ("Вопрос", "a"),
            ("Вопрос", "a | | b"),
            ("Вопрос", "a | A"),
            ("Вопрос", "a | " + "b" * 56),
            ("Вопрос", "a | b | c | d | e | f | g"),
        ):
            with self.subTest(question=question, choices=choices), self.assertRaises(ValueError):
                community_poll._build_poll(question, choices, 24, False)

    async def test_posts_one_silent_poll_and_acknowledges_privately_with_link(self):
        await community_poll.poll.callback(self.interaction, "Во что играем?", "Minecraft | Terraria")
        self.ack.assert_awaited_once_with(self.interaction, ephemeral=True)
        post = self.interaction.channel.send.await_args
        self.assertEqual(post.args, ("Опрос от <@5>",))
        self.assertEqual(post.kwargs["nonce"], "poll:3")
        self.assertTrue(post.kwargs["silent"])
        self.assertEqual(post.kwargs["allowed_mentions"].to_dict(), {"parse": []})
        self.assertIsInstance(post.kwargs["poll"], discord.Poll)
        self.assertNotIn("view", post.kwargs)
        self.finish.assert_awaited_once_with(self.interaction, "[Перейти к опросу](https://discord.com/channels/1/2/4)")

    async def test_thread_uses_thread_permissions_for_both_actors(self):
        thread = Mock(spec=discord.Thread)
        thread.id = 2
        thread.send = AsyncMock(return_value=SimpleNamespace(jump_url="https://discord.com/channels/1/2/4"))
        self.interaction.channel = thread
        self.interaction.permissions = discord.Permissions(
            view_channel=True, send_messages_in_threads=True, send_polls=True,
        )
        await community_poll.poll.callback(self.interaction, "Во что играем?", "Minecraft | Terraria")
        thread.send.assert_not_awaited()
        feedback = self.interaction.response.send_message.await_args.kwargs
        self.assertTrue(feedback["ephemeral"])
        card = feedback["view"]
        self.assertTrue(card.has_components_v2())
        self.assertTrue(any("Vivian нужны права" in item.content for item in card.walk_children()
                            if isinstance(item, discord.ui.TextDisplay)))
        self.interaction.app_permissions = self.interaction.permissions
        await community_poll.poll.callback(self.interaction, "Во что играем?", "Minecraft | Terraria")
        thread.send.assert_awaited_once()

    async def test_invalid_and_duplicate_calls_do_not_use_unrelated_channel_capacity(self):
        await community_poll.poll.callback(self.interaction, "Во что играем?", "Minecraft")
        self.interaction.channel.send.assert_not_awaited()
        self.assertFalse(community_poll._recent_publications)
        await community_poll.poll.callback(self.interaction, "Во что играем?", "Minecraft | Terraria")
        self.interaction.channel.send.assert_awaited_once()
        other_channel = Mock(spec=discord.TextChannel)
        other_channel.id = 3
        other_channel.send = AsyncMock(return_value=SimpleNamespace(jump_url="https://discord.com/channels/1/3/4"))
        self.interaction.channel = other_channel
        await community_poll.poll.callback(self.interaction, "Во что играем?", "Minecraft | Terraria")
        other_channel.send.assert_not_awaited()
        self.interaction.user.id = 7
        await community_poll.poll.callback(self.interaction, "Во что играем?", "Minecraft | Terraria")
        other_channel.send.assert_awaited_once()

    async def test_automod_error_has_no_duplicate_post(self):
        self.interaction.channel.send.side_effect = discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request"), {"code": 200000, "message": "Blocked"},
        )
        await community_poll.poll.callback(self.interaction, "Во что играем?", "Minecraft | Terraria")
        self.interaction.channel.send.assert_awaited_once()
        self.finish.assert_awaited_once_with(
            self.interaction, "Фильтры Discord отклонили опрос. Проверь текст вопроса и ответов.",
        )
