from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.components import _component_factory
from discord.ui.view import ViewStore

from core import config
from utils import components_v2 as v2, helpers, hub_ui, v2_delivery


class PersistentAnswer(discord.ui.DynamicItem[discord.ui.Button], template=r"test:v2:(?P<answer>\d+)"):
    def __init__(self, answer):
        self.answer = answer
        super().__init__(discord.ui.Button(label="Answer", custom_id=f"test:v2:{answer}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["answer"]))

    async def callback(self, interaction):
        await interaction.response.send_message(self.answer, ephemeral=True)


def _message(view):
    return SimpleNamespace(id=123, content="", embeds=[], attachments=[],
                           flags=discord.MessageFlags(components_v2=True),
                           components=[_component_factory(item) for item in view.to_components()])


class ComponentsV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_dynamic_buttons_and_global_factory_after_retiring_view(self):
        original = discord.ui.View(timeout=None)
        answer = PersistentAnswer(7)
        original.add_item(answer)
        original.add_item(discord.ui.Button(label="Ordinary"))
        layout = v2.adapt({"content": "Question", "view": original})["view"]
        store = ViewStore(config.bot._connection)
        store.add_view(layout, 123)
        converted = next(item for item in layout.walk_children() if isinstance(item, PersistentAnswer))
        self.assertIs(answer.view, original)
        self.assertIsNot(converted.item, answer.item)

        interaction = SimpleNamespace(message=_message(layout), data={},
                                      response=SimpleNamespace(send_message=AsyncMock()))
        match = answer.template.fullmatch(answer.custom_id)
        await store.schedule_dynamic_item_call(2, PersistentAnswer, interaction, answer.custom_id, match)
        interaction.response.send_message.assert_awaited_once_with(7, ephemeral=True)
        layout.stop()
        self.assertIs(store._dynamic_items[answer.template], PersistentAnswer)

    async def test_plain_view_controller_and_user_select_remain_active(self):
        calls = []

        class PlainView(discord.ui.View):
            @discord.ui.button(label="Run")
            async def run(self, interaction, item):
                calls.append((interaction, item.view))

        controller = PlainView(timeout=12)
        controller.add_item(discord.ui.UserSelect(row=1, custom_id="v2:users"))
        layout = v2.adapt({"content": "Controls", "view": controller})["view"]
        self.assertEqual(layout.timeout, 12)
        self.assertIn(5, [item.type.value for item in layout.walk_children()])
        state = SimpleNamespace(store_view=Mock())
        message = _message(layout)
        v2_delivery.commit({"view": controller}, layout, message, state=state)
        self.assertIs(controller.panel_message, message)
        clone = next(item for item in layout.walk_children() if isinstance(item, discord.ui.Button))
        interaction = SimpleNamespace(data={})
        await layout._scheduled_task(clone, interaction)
        self.assertEqual(calls, [(interaction, controller)])
        layout.stop()

    async def test_body_edits_keep_reconstructed_controls_until_explicitly_removed(self):
        original = discord.ui.View(timeout=None)
        original.add_item(PersistentAnswer(4))
        message = _message(v2.adapt({"content": "before", "view": original})["view"])
        edited = v2.adapt({"content": "after"}, editing=True, message=message)
        control = next(item for item in edited["view"].walk_children() if isinstance(item, discord.ui.Button))
        self.assertEqual(control.custom_id, "test:v2:4")
        self.assertFalse(control.is_dispatchable())
        self.assertIsNone(edited["content"])
        self.assertEqual(edited["embeds"], [])
        removed = v2.adapt({"content": "closed", "view": None}, editing=True, message=message)
        self.assertFalse(any(isinstance(item, discord.ui.Button) for item in removed["view"].walk_children()))

    async def test_multiple_embeds_keep_body_fields_and_media(self):
        first = discord.Embed(title="One", description="First").set_image(url="https://example.com/one.png")
        second = discord.Embed(title="Two", description="Second").add_field(name="Metric", value="42")
        second.set_image(url="https://example.com/two.png")
        result = v2.adapt({"embeds": [first, second]})
        items = list(result["view"].walk_children())
        text = "\n".join(item.content for item in items if isinstance(item, discord.ui.TextDisplay))
        self.assertTrue(all(fragment in text for fragment in ("One", "First", "Two", "Second", "Metric", "42")))
        gallery = next(item for item in items if isinstance(item, discord.ui.MediaGallery))
        self.assertEqual([item.media.url for item in gallery.items],
                         ["https://example.com/one.png", "https://example.com/two.png"])
        self.assertNotIn("embeds", result)

    async def test_each_upload_is_exposed_once_with_file_or_gallery(self):
        sound = discord.File(BytesIO(b"audio"), filename="track.mp3")
        poster = discord.File(BytesIO(b"image"), filename="poster.png")
        embed = discord.Embed(title="Media").set_image(url="attachment://poster.png")
        prepared = v2.adapt({"embed": embed, "files": [sound, poster]})
        self.assertEqual(prepared["files"], [sound, poster])
        controls = list(prepared["view"].walk_children())
        self.assertEqual([item.media.url for item in controls if isinstance(item, discord.ui.File)],
                         ["attachment://track.mp3"])
        self.assertEqual([entry.media.url for item in controls if isinstance(item, discord.ui.MediaGallery)
                          for entry in item.items], ["attachment://poster.png"])
        sound.close()
        poster.close()
        with self.assertRaisesRegex(ValueError, "poll"):
            v2.adapt({"content": "No mixed protocol", "poll": object()})

    async def test_nonce_publish_uses_v2_without_legacy_fields(self):
        with patch.object(config.bot.http, "request", AsyncMock(return_value={"id": "998"})) as request:
            await helpers._send_message_once(SimpleNamespace(id=5, guild=None), "Public card", "once",
                                             reference=SimpleNamespace(id=42), components_v2=True)
        body = request.await_args.kwargs["json"]
        self.assertEqual(body["flags"], 1 << 15)
        self.assertTrue(body["enforce_nonce"])
        self.assertEqual(body["nonce"], "once")
        self.assertEqual(body["message_reference"]["message_id"], "42")
        self.assertEqual(body["allowed_mentions"], {"parse": [], "replied_user": False})
        self.assertTrue(body["components"])
        self.assertNotIn("content", body)
        self.assertNotIn("embeds", body)

    async def test_hub_ui_preserves_deferred_response_and_followup_visibility(self):
        response = SimpleNamespace(is_done=lambda: False, type=None)
        interaction = SimpleNamespace(response=response)
        with patch.object(v2, "send_response", AsyncMock()) as send, patch.object(
                v2, "edit_original", AsyncMock()) as edit, patch.object(v2, "followup", AsyncMock()) as followup:
            await hub_ui.send(interaction, "private")
            self.assertTrue(send.await_args.kwargs["ephemeral"])
            response.is_done = lambda: True
            response.type = discord.InteractionResponseType.deferred_channel_message
            await hub_ui.send(interaction, "already deferred")
            self.assertNotIn("ephemeral", edit.await_args.kwargs)
            response.type = discord.InteractionResponseType.channel_message
            await hub_ui.send(interaction, "public followup", ephemeral=False)
            self.assertFalse(followup.await_args.kwargs["ephemeral"])
