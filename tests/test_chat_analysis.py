from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from commands import chat_analysis


class AnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_stays_in_current_channel_and_excludes_personal_or_secret_text(self):
        now = discord.utils.utcnow()
        person = SimpleNamespace(bot=False, display_name="Тест")
        rows = [
            SimpleNamespace(id=number, author=person, content=text, created_at=now, webhook_id=None)
            for number, text in (
                (120, "Принимаем вариант Б."), (119, "Мой пароль"),
                (118, "Vivian, зови меня Профиль."), (117, "Эту реплику забыли"),
                (116, "Начинаем сравнение вариантов."),
            )
        ]
        rows[-1].created_at -= timedelta(hours=2)
        old = SimpleNamespace(id=111, author=person, content="Старая запись.",
                              created_at=now - timedelta(days=2), webhook_id=None)

        async def history(*, limit, before):
            self.assertEqual(limit, 16)
            self.assertEqual(before.id, 150)
            for row in (*rows, old):
                yield row

        interaction = SimpleNamespace(
            id=150, guild=SimpleNamespace(id=5, me=SimpleNamespace(id=99)),
            channel=SimpleNamespace(id=8, history=history),
        )
        with patch.dict(chat_analysis.PURGED_SOURCE_IDS, {(5, 8): {117}}):
            records = await chat_analysis._recent_messages(interaction, 8)
        self.assertEqual([row["id"] for row in records], ["116", "120"])

    async def test_private_thread_membership_must_be_current(self):
        thread = unittest.mock.MagicMock(spec=discord.Thread)
        thread.type = discord.ChannelType.private_thread
        thread.fetch_member = AsyncMock(return_value=object())
        interaction = SimpleNamespace(channel=thread, user=SimpleNamespace(id=12))
        with patch.object(chat_analysis, "_can_read", return_value=True):
            self.assertTrue(await chat_analysis._still_can_read(interaction))
            thread.fetch_member.side_effect = TimeoutError()
            self.assertFalse(await chat_analysis._still_can_read(interaction))
        thread.fetch_member.assert_awaited_with(12)

    def test_citations_are_valid_and_links_survive_field_limit(self):
        records = [{"id": "9" * 20}, {"id": "8" * 20}]
        result = {
            "1": [{"text": "*" * 220 + " [chat](https://example.com)", "sources": ["7" * 20, *[r["id"] for r in records]]}] * 3,
            "2": [{"text": "Мнимое решение.", "sources": ["fake"]}],
            "3": [],
        }
        card = chat_analysis._analysis_embed(2**63, 2**63, "summary", records, result)
        first = card.fields[0].value
        self.assertEqual(first.count("https://discord.com/channels/"), 6)
        self.assertNotIn("https://example.com", first)
        self.assertNotIn("7" * 20, first)
        self.assertLessEqual(len(first), 1024)
        self.assertTrue(all(line.endswith(")") for line in first.splitlines()))
        self.assertNotIn("Мнимое решение", card.fields[1].value)

    def test_parse_requires_all_sections(self):
        self.assertIsNone(chat_analysis._parse_answer('{"1": []}'))
        self.assertIsNone(chat_analysis._parse_answer("not JSON"))
        self.assertEqual(chat_analysis._parse_answer('```json\n{"1":[],"2":[],"3":[]}\n```'),
                         {"1": [], "2": [], "3": []})
