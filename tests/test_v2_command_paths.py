from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from commands import life
from core import config
from utils import components_v2 as v2


class ConfessionControlTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, author_id=9, embeds=()):
        return SimpleNamespace(
            guild=object(),
            message=SimpleNamespace(author=SimpleNamespace(id=author_id), id=31, embeds=embeds),
            user=SimpleNamespace(bot=False),
            permissions=SimpleNamespace(view_channel=True),
            response=SimpleNamespace(send_modal=AsyncMock()),
        )

    async def test_v2_reply_uses_custom_id_without_an_embed(self):
        item = life.ConfessionReplyButton(42)
        interaction = self.interaction()
        with patch.object(config.bot._connection, "user", SimpleNamespace(id=9)):
            await item.callback(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual((modal.reply_to, modal.parent_message), (42, 31))

    async def test_legacy_embed_control_opens_the_same_modal(self):
        embed = discord.Embed(title="Confession #17").set_footer(text="Confession #17 · Publicly anonymous")
        interaction = self.interaction(embeds=[embed])
        with patch.object(config.bot._connection, "user", SimpleNamespace(id=9)):
            await life.AnonymousReplyView().reply.callback(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual((modal.reply_to, modal.parent_message), (17, 31))

    async def test_button_on_another_authors_message_is_rejected_privately(self):
        interaction = self.interaction(author_id=5)
        with (patch.object(config.bot._connection, "user", SimpleNamespace(id=9)),
              patch.object(v2, "send_response", AsyncMock()) as send):
            await life.ConfessionReplyButton(42).callback(interaction)
        self.assertTrue(send.await_args.kwargs["ephemeral"])
        interaction.response.send_modal.assert_not_awaited()
