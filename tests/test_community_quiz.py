import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from commands import community_quiz as quiz


def _user(user_id, name="Игрок"):
    return SimpleNamespace(id=user_id, display_name=name)


def _session():
    return quiz.QuizSession(123, quiz._pick_questions())


def _card_text(view):
    return " ".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


class QuizScoringTests(unittest.TestCase):
    def test_question_bank_has_a_distinct_answer_in_each_category(self):
        for category, questions in quiz._QUESTION_BANK:
            self.assertTrue(category)
            for prompt, *choices in questions:
                self.assertLess(len(prompt), 500)
                self.assertEqual(len({choice.casefold() for choice in choices}), 4)
                self.assertTrue(all(0 < len(choice) <= 80 for choice in choices))
        questions = quiz._pick_questions()
        self.assertEqual(len({question.category for question in questions}), 3)
        self.assertTrue(all(question.correct in question.choices for question in questions))

    def test_one_answer_per_player_and_closed_rounds_stay_closed(self):
        session = _session()
        current = session.questions[0]
        correct = current.choices.index(current.correct)
        wrong = (correct + 1) % len(current.choices)
        session.open_round(0, now=100)

        self.assertIn("Ответ принят", session.answer(_user(13), 0, wrong, now=101))
        self.assertIn("уже принят", session.answer(_user(13), 0, correct, now=102))
        self.assertIn("Ответ принят", session.answer(_user(99), 0, correct, now=103))
        self.assertIn("уже закрыт", session.answer(_user(100), 0, correct, now=session.deadline))
        self.assertEqual(session.correct_count, 1)
        self.assertEqual(len(session.answered), 2)
        self.assertEqual(session.players[13].score, 0)
        self.assertEqual(session.players[99].score, 1)
        self.assertNotIn(100, session.players)

        session.open_round(1, now=130)
        self.assertIn("уже закрыт", session.answer(_user(99), 0, correct, now=131))
        self.assertEqual(session.answered, set())
        self.assertEqual(session.players[99].score, 1)
        current = session.questions[1]
        self.assertIn("Ответ принят", session.answer(_user(99), 1, current.choices.index(current.correct), now=131))
        self.assertEqual(session.players[99].score, 2)

    def test_scoreboard_groups_ties_and_disarms_mentions_and_names(self):
        session = _session()
        session.players = {1: quiz.Player("@everyone", 2), 2: quiz.Player("**Борис**", 2),
                           3: quiz.Player("Анна", 1)}
        content = session.results()
        self.assertEqual(content.count("**1.**"), 2)
        self.assertIn("**3.** Анна", content)
        self.assertNotIn("@everyone", content)
        self.assertIn(r"\*\*Борис\*\*", content)

    def test_posting_uses_the_thread_permission_for_threads(self):
        thread = MagicMock(spec=discord.Thread)
        channel = MagicMock(spec=discord.TextChannel)
        base = discord.Permissions(view_channel=True, send_messages=True)
        self.assertTrue(quiz._can_post(channel, base))
        self.assertFalse(quiz._can_post(thread, base))
        base.send_messages_in_threads = True
        self.assertTrue(quiz._can_post(thread, base))


class QuizInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_buttons_in_expired_games_and_old_rounds_get_private_feedback(self):
        session = _session()
        session.message_id = 456
        session.open_round(1, now=asyncio.get_running_loop().time())
        button = quiz.QuizAnswer(123, 0, 0, "Тест")
        response = SimpleNamespace(send_message=AsyncMock(return_value=None))
        interaction = SimpleNamespace(channel_id=42, message=SimpleNamespace(id=456), user=_user(1),
                                      response=response, _state=None)

        with patch.dict(quiz._GAMES, {42: session}, clear=True):
            await button.callback(interaction)
        feedback = response.send_message.await_args.kwargs["view"]
        self.assertTrue(feedback.has_components_v2())
        self.assertIn("раунд уже закрыт", _card_text(feedback))
        self.assertTrue(response.send_message.await_args.kwargs["ephemeral"])
        self.assertEqual(session.players, {})

        with patch.dict(quiz._GAMES, {}, clear=True):
            await button.callback(interaction)
        self.assertIn("квиз уже закончился", _card_text(response.send_message.await_args.kwargs["view"]))

    async def test_game_edits_one_message_and_removes_controls_after_each_round(self):
        session = _session()
        interaction = SimpleNamespace(
            app_permissions=discord.Permissions.none(),
            edit_original_response=AsyncMock(return_value=SimpleNamespace(id=456, attachments=[])),
            response=SimpleNamespace(type=None), extras={}, _state=None,
        )
        with patch.object(quiz.asyncio, "sleep", new_callable=AsyncMock) as wait:
            await quiz._play(interaction, session)
        edits = [call.kwargs for call in interaction.edit_original_response.await_args_list]
        self.assertEqual(len(edits), 7)
        self.assertEqual(wait.await_count, 6)
        for round_index, edit_index in enumerate((0, 2, 4)):
            view = edits[edit_index]["view"]
            self.assertTrue(view.has_components_v2())
            self.assertIn(f"{round_index + 1}/3", _card_text(view))
            buttons = [item for item in view.walk_children() if isinstance(item, quiz.QuizAnswer)]
            self.assertEqual(len(buttons), 4)
            self.assertTrue(all(item.round_index == round_index for item in buttons))
            self.assertFalse(edits[edit_index + 1]["view"].is_dispatchable())
        self.assertIn("Итоги квиза Vivian", _card_text(edits[-1]["view"]))
        self.assertFalse(edits[-1]["view"].is_dispatchable())
        self.assertFalse(edits[-1]["allowed_mentions"].everyone)

    async def test_failed_game_releases_reserved_channel(self):
        permissions = discord.Permissions(view_channel=True, send_messages=True)
        interaction = SimpleNamespace(
            id=123, channel=MagicMock(spec=discord.TextChannel), channel_id=42,
            permissions=permissions, app_permissions=permissions,
            response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True, type=None),
            edit_original_response=AsyncMock(return_value=None), _state=None,
        )
        with patch.object(quiz.database, "db_pool", None), patch.dict(quiz._GAMES, {}, clear=True), patch.object(
            quiz, "_play", AsyncMock(side_effect=TimeoutError),
        ):
            await quiz.quiz.callback(interaction)
            self.assertNotIn(42, quiz._GAMES)
        self.assertIn("завершился раньше времени",
                      _card_text(interaction.edit_original_response.await_args.kwargs["view"]))
