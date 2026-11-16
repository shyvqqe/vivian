from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from commands import elite_puzzle as puzzle
from core import puzzle_games as games


class PuzzleRulesTests(unittest.TestCase):
    def test_code_scores_duplicate_digits_at_most_once(self):
        self.assertEqual(games.code_score("1123", "1322"), (2, 1))
        self.assertEqual(games.code_score("1111", "1234"), (1, 0))
        self.assertEqual(games.code_score("2121", "1212"), (0, 4))

    def test_cipher_round_trip_uses_yo_and_alphabet_wrap(self):
        self.assertEqual(games.rotate("еёя", 1), "ёжа")
        example = "сова ждёт на старой башне"
        self.assertEqual(games.rotate(games.rotate(example, 32), -32), example)
        self.assertEqual(games.canonical_answer("cipher", "СОВА,  ждёт!"), "сова ждёт")

    def test_nonograms_have_exactly_one_solution(self):
        self.assertEqual(games.run_clues((1, 0, 1, 1)), (1, 2))
        for seed in range(40):
            challenge = games.generate("grid", random.Random(seed))
            board = tuple(tuple(int(char == "#") for char in challenge.answer[index:index + 4])
                          for index in range(0, 16, 4))
            self.assertEqual(games.grid_solution_count(*games.grid_clues(board)), 1)
        self.assertEqual(games.canonical_answer("grid", "0110 / 0000 / 1001 / 1111"), ".##.....#..#####")
        with self.assertRaises(ValueError):
            games.canonical_answer("grid", "#..")

    def test_every_generated_light_panel_can_be_solved(self):
        labels = {games.light_label(index): index for index in range(9)}
        self.assertEqual(games.light_mask(0).bit_count(), 3)
        self.assertEqual(games.light_mask(4).bit_count(), 5)
        for seed in range(40):
            challenge = games.generate("lights", random.Random(seed))
            state = challenge.lights
            self.assertNotEqual(state, 0)
            for label in challenge.answer.split():
                state ^= games.light_mask(labels[label])
            self.assertEqual(state, 0)


def make_session(mode="code", *, deadline=500.0, session_id=41):
    challenge = games.generate(mode, random.Random(12)) if mode != "code" else games.Challenge(
        "code", "Код", "____", "Инструкция", "1123", "Правило.",
    )
    session = puzzle.PuzzleSession(
        session_id, 8, 9, 10, challenge, 42000, deadline, challenge.lights,
        message=SimpleNamespace(id=11),
    )
    return session


class PuzzleSessionTests(unittest.TestCase):
    def test_attempts_are_bounded_and_feedback_does_not_reveal_answer(self):
        state = make_session()
        clue = state.play(101, "guess", "1622", 40.0)
        self.assertNotIn(state.challenge.answer, clue)
        self.assertIn("**2**", clue)
        self.assertEqual(state.attempts, 1)
        self.assertIn("уже", state.play(102, "guess", "16 22", 41.0))
        self.assertIn("Подожди", state.play(101, "guess", "1234", 41.0))
        self.assertIn("цифры", state.play(102, "guess", "2368", 41.0))
        self.assertEqual(state.attempts, 1)
        self.assertIn("Решено", state.play(101, "guess", "1123", 43.0))
        self.assertEqual((state.winner_id, state.status), (101, "won"))
        self.assertIn("закрыта", state.play(102, "guess", "1111", 44.0))

    def test_board_expires_and_limits_attempts(self):
        state = make_session()
        self.assertIn("закрыта", state.play(1, "guess", "1123", 500.0))
        state.attempts = state.limit - 1
        self.assertIn("закончились", state.play(101, "guess", "1234", 50.0))
        self.assertEqual(state.status, "exhausted")

    def test_v2_panel_shows_switches_and_only_reveals_answer_when_closed(self):
        state = make_session("lights")
        opened = puzzle.puzzle_card(state)
        self.assertEqual(opened.to_components()[0]["type"], 17)
        self.assertEqual(sum(isinstance(item, puzzle.PuzzleButton) for item in opened.walk_children()), 10)
        self.assertFalse(any(state.challenge.answer in getattr(item, "content", "") for item in opened.walk_children()))
        state.status = "ended"
        closed = puzzle.puzzle_card(state)
        self.assertFalse(any(isinstance(item, puzzle.PuzzleButton) for item in closed.walk_children()))
        self.assertTrue(any(state.challenge.answer in getattr(item, "content", "") for item in closed.walk_children()))


class PuzzleInteractionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_control_does_not_open_another_sessions_modal(self):
        state = make_session(deadline=asyncio.get_running_loop().time() + 30, session_id=42)
        interaction = SimpleNamespace(
            channel_id=9, guild_id=8, user=SimpleNamespace(id=100, bot=False),
            message=SimpleNamespace(id=11), response=SimpleNamespace(send_modal=AsyncMock()),
        )
        with (patch.dict(puzzle._SESSIONS, {9: state}),
              patch.object(puzzle, "_private", AsyncMock()) as notice):
            await puzzle.PuzzleButton(41, "guess").callback(interaction)
            notice.assert_awaited_once()
            interaction.response.send_modal.assert_not_awaited()
            await puzzle.PuzzleButton(42, "guess").callback(interaction)
            interaction.response.send_modal.assert_awaited_once()
            prompt = interaction.response.send_modal.await_args.args[0]
            self.assertIsInstance(prompt.children[0], discord.ui.Label)

    async def test_command_publishes_one_silent_v2_card_with_private_receipt(self):
        self.assertIs(puzzle.code.checks[0], puzzle.lights.checks[0])
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 9
        permissions = discord.Permissions(view_channel=True, send_messages=True)
        posted = SimpleNamespace(id=11, jump_url="https://discord.com/channels/8/9/11")
        interaction = SimpleNamespace(
            channel=channel, guild_id=8, id=42, user=SimpleNamespace(id=10),
            permissions=permissions, app_permissions=permissions,
            response=SimpleNamespace(defer=AsyncMock()),
        )
        with (patch.dict(puzzle._SESSIONS, {}, clear=True),
              patch.object(puzzle.database, "db_pool", None),
              patch.object(puzzle.v2, "send_message", AsyncMock(return_value=posted)) as send,
              patch.object(puzzle, "_private", AsyncMock()) as notice):
            await puzzle.code.callback(interaction)
            try:
                send.assert_awaited_once()
                self.assertIsInstance(send.await_args.kwargs["view"], discord.ui.LayoutView)
                self.assertTrue(send.await_args.kwargs["silent"])
                self.assertIn(posted.jump_url, notice.await_args.args[1])
                self.assertTrue(interaction.response.defer.await_args.kwargs["ephemeral"])
                await puzzle.code.callback(interaction)
                send.assert_awaited_once()
                self.assertIn("уже идёт", notice.await_args.args[1])
            finally:
                task = puzzle._SESSIONS[9].expiry
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
