from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from commands import elite_studio as studio
from utils import components_v2 as v2


def contents(view):
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


class StudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_storyboard_uses_cumulative_timestamps_and_owner_guard(self):
        shots = studio._shots("5с: общий план | 12: герой в кадре\n 8: титры")
        card = studio.Storyboard(1, "Съёмка", shots)
        self.assertIn("00:00 → 00:05", contents(card))
        self.assertIn("всего `00:25`", contents(card))
        card.page = 1
        card._build()
        self.assertIn("00:05 → 00:17", contents(card))
        with patch.object(studio, "_notice", AsyncMock()) as notice:
            other = SimpleNamespace(user=SimpleNamespace(id=2))
            self.assertFalse(await card.interaction_check(other))
            notice.assert_awaited_once()
        for bad in ("без длительности", "0: кадр", "601: кадр", "8:    "):
            with self.assertRaises(ValueError):
                studio._shots(bad)

    async def test_release_export_keeps_explicit_sections_and_registers_v2_file(self):
        async def inspect(interaction, view, *, publish, file):
            self.assertFalse(publish)
            self.assertEqual(file.filename, "release-notes.md")
            exported = file.fp.read().decode()
            self.assertIn("## Исправления\n\nПлеер", exported)
            self.assertIn("## При обновлении\n\nНужна новая конфигурация", exported)
            self.assertIn("Релиз", contents(view))
            prepared = v2.adapt({"view": view, "file": file})
            self.assertTrue(any(isinstance(item, discord.ui.File) for item in prepared["view"].walk_children()))

        interaction = SimpleNamespace(app_permissions=SimpleNamespace(attach_files=True))
        with patch.object(studio, "_send", side_effect=inspect):
            await studio.release.callback(interaction, "Atlas", "2.0", "Архив", "Плеер",
                                          "Нужна новая конфигурация", True)

    async def test_release_with_maximum_escaped_markup_fits_discord(self):
        with patch.object(studio, "_send", AsyncMock()) as send:
            await studio.release.callback(SimpleNamespace(), "*" * 80, "*" * 32, "*" * 700, "*" * 700, "*" * 600)
        view = send.await_args.args[1]
        self.assertLessEqual(view.content_length(), 4000)
        self.assertTrue(contents(view).endswith("…"))

    async def test_rejected_storyboard_edit_keeps_current_frame(self):
        view = studio.Storyboard(1, "Съёмка", studio._shots("5: Первый | 5: Второй | 5: Третий"))
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()))
        with (patch.object(v2, "edit_original", AsyncMock(side_effect=[TimeoutError(), None])),
              patch.object(v2, "followup", AsyncMock()) as notice):
            await view._turn(interaction, 1)
            self.assertEqual(view.page, 0)
            await view._turn(interaction, 1)
            self.assertEqual(view.page, 1)
            notice.assert_awaited_once()

    async def test_reveal_respects_time_message_scope_and_owner_withdrawal(self):
        item = studio.Reveal(10, 20, 1, "Тизер", "Видно всем", "Личный итог", 150, 300, 999, 30)
        item.cleanup = SimpleNamespace(cancel=lambda: None)
        interaction = SimpleNamespace(
            guild_id=10, channel_id=20, message=SimpleNamespace(id=30),
            user=SimpleNamespace(id=2, bot=False),
        )
        clock = Mock(return_value=100)
        with (patch.dict(studio._REVEALS, {55: item}), patch.object(studio, "_notice", AsyncMock()) as notice,
              patch.object(v2, "send_response", AsyncMock()) as private, patch.object(v2, "edit_response", AsyncMock()) as edit,
              patch.object(studio, "time", SimpleNamespace(monotonic=clock))):
            public = studio._reveal_card(55, item)
            self.assertNotIn(item.secret, str(public.to_components()))
            await studio.RevealAction(55, "open").callback(interaction)
            private.assert_not_awaited()
            clock.return_value = 200
            interaction.message.id = 99
            await studio.RevealAction(55, "open").callback(interaction)
            private.assert_not_awaited()
            interaction.message.id = 30
            await studio.RevealAction(55, "open").callback(interaction)
            self.assertTrue(private.await_args.kwargs["ephemeral"])
            self.assertIn(item.secret, contents(private.await_args.kwargs["view"]))
            await studio.RevealAction(55, "close").callback(interaction)
            edit.assert_not_awaited()
            interaction.user.id = 1
            await studio.RevealAction(55, "close").callback(interaction)
            self.assertNotIn(55, studio._REVEALS)
            self.assertNotIn(item.secret, contents(edit.await_args.kwargs["view"]))
            self.assertEqual(notice.await_count, 3)

    def test_sources_accept_safe_https_only(self):
        self.assertEqual(studio._https("https://example.org/source"), "https://example.org/source")
        for bad in ("http://example.org", "https://x@example.org", "https://example.org:99999", "https://example.org/a b"):
            with self.assertRaises(ValueError):
                studio._https(bad)
