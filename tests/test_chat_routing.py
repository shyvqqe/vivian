from collections import deque
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from app import chat_routing as routing
from vivian import ambient, ambient_state, memory, memory_controls_store
from core import state


def message(message_id, author_id=10, content="", **fields):
    data = dict(
        id=message_id, author=SimpleNamespace(id=author_id),
        channel=SimpleNamespace(id=20), guild=SimpleNamespace(id=1, me=None),
        content=content, type=discord.MessageType.default, mentions=[], role_mentions=[],
        mention_everyone=False, attachments=[], stickers=[], reference=None,
    )
    return SimpleNamespace(**{**data, **fields})


class TurnRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot_user = patch.object(routing.config.bot._connection, "user", SimpleNamespace(id=42))
        self.bot_user.start()
        self.addCleanup(self.bot_user.stop)
        for cache in (routing._active_chats, routing._implicit_attempts,
                      routing._pending_users, routing._pending_channels):
            cache.clear()
            self.addCleanup(cache.clear)
        routing._pending_total = 0

    async def test_name_phrase_and_incidental_mentions(self):
        for text in ("Vivian, можешь объяснить?", "Слушай, Вивиан, вопрос", "Как думаешь, Вивиан?",
                     "Вопрос к боту: какие аргументы?", "вивиан скажи сколько времени",
                     "Hey Vivian, what is that?", "Vivian, сравни варианты"):
            with self.subTest(text=text):
                self.assertEqual(await routing.select_turn(message(100, content=text)), ("name", None))
        for text in ("мы обсуждали Vivian", "Vivian говорит, что было иначе", "Sol, помоги",
                     "бот вчера не работал", "Соловей, привет", "> Vivian, помоги"):
            self.assertEqual(await routing.select_turn(message(101, content=text)), (None, None))

    async def test_reply_with_ping_disabled_fetches_reference(self):
        target = message(101, author_id=42)
        current = message(
            102, content="А это точно?", type=discord.MessageType.reply,
            channel=SimpleNamespace(id=20, fetch_message=AsyncMock(return_value=target)),
            reference=SimpleNamespace(channel_id=20, message_id=101, resolved=None, cached_message=None),
        )
        self.assertEqual(await routing.select_turn(current), ("reply", target))
        current.channel.fetch_message.assert_awaited_once_with(101)

    async def test_address_priority_and_interleaved_followup(self):
        routing.note_reply(message(200))
        self.assertEqual(await routing.select_turn(message(201, author_id=11, content="а почему?")), (None, None))
        self.assertEqual(await routing.select_turn(message(202, content="спасибо")), (None, None))
        self.assertEqual(await routing.select_turn(message(203, content="всм")), ("followup", None))
        self.assertEqual(await routing.select_turn(message(199, content="всм")), (None, None))
        mention = SimpleNamespace(id=42)
        self.assertEqual(await routing.select_turn(message(204, content="<@42>", mentions=[mention])),
                         ("mention", None))
        self.assertEqual(await routing.select_turn(message(205, content="!play example")), (None, None))
        routing.note_reply(message(210, author_id=13))
        routing.note_reply(message(207))
        self.assertEqual(await routing.select_turn(message(211, content="всм")), (None, None))

    def test_followup_cooldown_never_crowds_out_direct_requests(self):
        current = message(222, content="почему?")
        self.assertTrue(routing.reserve_turn(current, "followup"))
        self.assertFalse(routing.reserve_turn(current, "followup"))
        self.assertTrue(routing.reserve_turn(current, "mention"))
        self.assertFalse(routing.reserve_turn(current, "mention"))
        routing.release_turn(current)
        routing.release_turn(current)
        self.assertFalse(routing.reserve_turn(current, "followup"))
        self.assertTrue(routing.reserve_turn(current, "reply"))
        routing.release_turn(current)
        self.assertFalse(routing.channel_is_busy(20))


class AmbientTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.context = [
            dict(message_id=number, author_id=10 + number % 2, channel_id=20, timestamp=self.now - (8 - number),
                 text=text)
            for number, text in enumerate((
                "Для музыкального канала предложили свой плеер с очередью.",
                "Клиент сейчас путает музыкальную очередь при перезапуске.",
                "Можно сохранять очередь. Есть ли вариант проще?",
                "Не согласен: перезапуск происходит редко, важнее быстрота очереди.",
            ), 1)
        ]
        for attr, data in (
            ("AMBIENT_CONFIG", {1: {"channel_id": 20}}),
            ("AMBIENT_CONTEXT", {1: deque(self.context, maxlen=18)}),
            ("AMBIENT_STATE", {}),
        ):
            patcher = patch.object(ambient_state, attr, data)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = ambient_state._ambient_state_for(1)
        self.state["recent_arrivals"].extend({**item, "approved": True} for item in self.context)
        self.state["latest_message_id"] = 4

    def test_short_discussion_can_try_once_without_delivery_cooldown(self):
        day = "2026-11-15"
        self.assertEqual(ambient_state._ambient_claim_reply(1, self.now, day), self.context)
        self.assertIsNone(ambient_state._ambient_claim_reply(1, self.now + 1, day))
        self.state["last_sent"] = self.now
        self.assertIsNone(ambient_state._ambient_claim_reply(1, self.now + 130, day))

    def test_small_followup_preserves_draft_but_topic_shift_or_direct_address_cancels_it(self):
        current = message(4)
        followup = {**self.context[-1], "message_id": 5, "author_id": 10, "timestamp": self.now,
                    "approved": True, "text": "Да, очередь и должна быть быстрой."}
        self.state["recent_arrivals"].append(followup)
        ambient_state.AMBIENT_CONTEXT[1].append(followup)
        self.assertTrue(ambient._draft_still_relevant(current, self.context, self.now))
        self.state["last_direct_id"] = 5
        self.assertFalse(ambient._draft_still_relevant(current, self.context, self.now))
        self.state["last_direct_id"] = None
        followup["text"] = "Кто-нибудь видел вчерашний матч по волейболу между столичными клубами?"
        self.assertFalse(ambient._draft_still_relevant(current, self.context, self.now))


class MemoryTests(unittest.TestCase):
    def test_cached_fallback_excludes_future_purged_and_secret_turns(self):
        now = time.time()
        current = message(55)
        turns = [{"_message_id": mid, "_timestamp": now, "content": text} for mid, text in
                 ((50, "контекст"), (51, "пароль скрыт"), (52, "забыто"), (57, "из будущего"))]
        with patch.dict(state.chat_history, {(1, 20, 10): turns}), patch.dict(
            memory_controls_store.PURGED_SOURCE_IDS, {(1, 20): {52}},
        ):
            self.assertEqual(memory._cached_prior_history((1, 20), current), [turns[0]])
        self.assertEqual(memory._ai_memory_fact_candidates("Сол, зови меня Лин."), {"address_name": "зови меня Лин"})
        self.assertEqual(memory._ai_memory_fact_candidates("Вивиан, зови меня Лин."), {"address_name": "зови меня Лин"})
