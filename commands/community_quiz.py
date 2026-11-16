"""A short, shared Discord quiz with round-scoped answer buttons."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import random

import discord
from discord import app_commands

from core import config, database, discord_api, guild_settings, hub_store
from utils import components_v2 as v2


_LOG = logging.getLogger(__name__)
_ROUND_SECONDS = 20
_REVEAL_SECONDS = 4
_EDIT_TIMEOUT = 8
_MAX_QUIZZES = 16
_MAX_PLAYERS = 100
_NO_MENTIONS = discord.AllowedMentions.none()

# Each entry contains the question, the correct answer, and three alternatives.
_QUESTION_BANK = (
    ("Природа и наука", (
        ("Какая планета ближе всего к Солнцу?", "Меркурий", "Венера", "Марс", "Земля"),
        ("Какой химический элемент обозначается символом Au?", "Золото", "Серебро", "Медь", "Алюминий"),
        ("Сколько сердец у осьминога?", "Три", "Одно", "Два", "Четыре"),
        ("Как называется переход вещества из твёрдого состояния сразу в газ?", "Сублимация", "Кристаллизация", "Конденсация", "Плавление"),
        ("Как называется самое маленькое простое число?", "Два", "Ноль", "Один", "Три"),
        ("В какой части клетки человека находится большая часть её ДНК?", "Ядро", "Митохондрия", "Рибосома", "Клеточная мембрана"),
    )),
    ("Культура и игры", (
        ("Кто написал «Алису в Стране чудес»?", "Льюис Кэрролл", "Марк Твен", "Чарльз Диккенс", "Редьярд Киплинг"),
        ("Какой инструмент обычно имеет 88 клавиш?", "Фортепиано", "Скрипка", "Гитара", "Аккордеон"),
        ("Какая шахматная фигура ходит буквой «Г»?", "Конь", "Слон", "Ладья", "Король"),
        ("Кто написал картину «Звёздная ночь»?", "Винсент ван Гог", "Клод Моне", "Сальвадор Дали", "Пабло Пикассо"),
        ("В какой игре игроки складывают падающие фигуры в горизонтальные линии?", "Тетрис", "Сапёр", "Понг", "Пакман"),
        ("В каком виде спорта выполняют тройной аксель?", "Фигурное катание", "Фехтование", "Плавание", "Конькобежный спорт"),
    )),
    ("Путешествия", (
        ("Какая река протекает через Париж?", "Сена", "Темза", "Тибр", "Дунай"),
        ("На каком материке находится пустыня Атакама?", "Южная Америка", "Африка", "Азия", "Австралия"),
        ("Как называется столица Канады?", "Оттава", "Торонто", "Ванкувер", "Монреаль"),
        ("В какой стране находится древний город Мачу-Пикчу?", "Перу", "Мексика", "Чили", "Колумбия"),
        ("Какой океан самый большой по площади?", "Тихий", "Атлантический", "Индийский", "Северный Ледовитый"),
        ("В какой столице стоит Колизей?", "Рим", "Афины", "Мадрид", "Лиссабон"),
    )),
)
_ROUND_COUNT = len(_QUESTION_BANK)


@dataclass(frozen=True)
class Question:
    category: str
    prompt: str
    choices: tuple[str, ...]
    correct: str


def _pick_questions():
    questions = []
    for category, entries in _QUESTION_BANK:
        prompt, correct, *alternatives = random.choice(entries)
        choices = [correct, *alternatives]
        random.shuffle(choices)
        questions.append(Question(category, prompt, tuple(choices), correct))
    random.shuffle(questions)
    return tuple(questions)


@dataclass
class Player:
    name: str
    score: int = 0


@dataclass
class QuizSession:
    quiz_id: int
    questions: tuple[Question, ...]
    message_id: int | None = None
    round_index: int = -1
    deadline: float = 0.0
    players: dict[int, Player] = field(default_factory=dict)
    answered: set[int] = field(default_factory=set)
    correct_count: int = 0

    def open_round(self, round_index: int, now: float) -> None:
        self.round_index = round_index
        self.deadline = now + _ROUND_SECONDS
        self.answered.clear()
        self.correct_count = 0

    def answer(self, user: discord.Member | discord.User, round_index: int, choice: int, now: float) -> str:
        if round_index != self.round_index or not 0 <= round_index < len(self.questions) or now >= self.deadline:
            return "Этот раунд уже закрыт."
        if user.id in self.answered:
            return "Твой ответ в этом раунде уже принят."
        if user.id not in self.players:
            if len(self.players) >= _MAX_PLAYERS:
                return f"В этом квизе уже участвуют {_MAX_PLAYERS} игроков."
            self.players[user.id] = Player(user.display_name)

        question = self.questions[round_index]
        self.answered.add(user.id)
        if question.choices[choice] == question.correct:
            self.players[user.id].score += 1
            self.correct_count += 1
        return "Ответ принят. Правильный вариант появится после раунда."

    def results(self) -> str:
        if not self.players:
            return "В этот раз никто не ответил. Следующий квиз можно запустить командой `/quiz`."
        ordered = sorted(self.players.values(), key=lambda player: (-player.score, player.name.casefold()))
        lines = [f"Участников в игре — **{len(ordered)}**", ""]
        place, previous_score = 0, None
        for index, player in enumerate(ordered[:10], 1):
            if player.score != previous_score:
                place, previous_score = index, player.score
            cleaned = " ".join(player.name.split())[:40]
            name = discord.utils.escape_mentions(discord.utils.escape_markdown(cleaned, ignore_links=False))
            lines.append(f"**{place}.** {name} · **{player.score}/{len(self.questions)}**")
        return "\n".join(lines)


_GAMES: dict[int, QuizSession] = {}


class QuizAnswer(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"vivian:quiz:(?P<quiz>[0-9]{1,20}):(?P<round>[0-2]):(?P<choice>[0-3])"):
    def __init__(self, quiz_id: int, round_index: int, choice: int, label: str):
        self.quiz_id, self.round_index, self.choice = quiz_id, round_index, choice
        super().__init__(discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"vivian:quiz:{quiz_id}:{round_index}:{choice}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["quiz"]), int(match["round"]), int(match["choice"]), item.label)

    async def callback(self, interaction: discord.Interaction):
        session = _GAMES.get(interaction.channel_id)
        if (session is None or session.quiz_id != self.quiz_id or session.message_id is None
                or session.message_id != getattr(interaction.message, "id", None)):
            feedback = "Этот квиз уже закончился. Запусти новый командой `/quiz`."
        else:
            feedback = session.answer(interaction.user, self.round_index, self.choice, asyncio.get_running_loop().time())
        await v2.send_response(interaction, feedback, ephemeral=True, allowed_mentions=_NO_MENTIONS)


def _can_post(channel, permissions: discord.Permissions) -> bool:
    if isinstance(channel, discord.Thread):
        return permissions.view_channel and permissions.send_messages_in_threads
    return isinstance(channel, discord.TextChannel) and permissions.view_channel and permissions.send_messages


async def _edit(interaction, title: str, body: str, answers: tuple[QuizAnswer, ...] = ()):
    card = discord.ui.Container(
        discord.ui.TextDisplay(f"## {title}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(body),
        accent_color=config.VIVIAN_ACCENT,
    )
    if answers:
        card.add_item(discord.ui.ActionRow(*answers))
    layout = discord.ui.LayoutView(timeout=None)
    layout.add_item(card)
    async with asyncio.timeout(_EDIT_TIMEOUT):
        return await v2.edit_original(interaction, view=layout, allowed_mentions=_NO_MENTIONS)


async def _play(interaction, session: QuizSession):
    clock = asyncio.get_running_loop().time
    for index, question in enumerate(session.questions):
        answers = tuple(
            QuizAnswer(session.quiz_id, index, choice, option)
            for choice, option in enumerate(question.choices)
        )
        title = f"Квиз Vivian · {index + 1}/{len(session.questions)}"
        body = f"{question.category}\n\n**{question.prompt}**\n\nНа ответ — {_ROUND_SECONDS} секунд."
        message = await _edit(interaction, title, body, answers)
        session.message_id = message.id
        session.open_round(index, clock())
        await asyncio.sleep(_ROUND_SECONDS)
        session.deadline = 0.0

        result = (f"**{question.prompt}**\nПравильный ответ — **{question.correct}**.\n\n"
                  f"Правильных ответов — **{session.correct_count}** из **{len(session.answered)}**.")
        await _edit(interaction, title, result)
        await asyncio.sleep(_REVEAL_SECONDS)

    await _edit(interaction, "Итоги квиза Vivian", session.results())


async def _private(interaction, message):
    await v2.send_response(interaction, message, ephemeral=True, allowed_mentions=_NO_MENTIONS)


@config.bot.tree.command(name="quiz", description="Запустить квиз на три вопроса с общей таблицей результатов.")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 90, key=lambda interaction: (interaction.guild_id, interaction.user.id))
async def quiz(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    if channel_id is None or not _can_post(interaction.channel, interaction.permissions):
        return await _private(interaction, "Запусти квиз в текстовом канале или ветке, где можешь писать.")
    if not _can_post(interaction.channel, interaction.app_permissions):
        return await _private(interaction, "Vivian нужны права отправки сообщений в этом канале или ветке.")
    if channel_id in _GAMES:
        return await _private(interaction, "В этом канале уже идёт квиз. Присоединяйся к текущему раунду.")
    if len(_GAMES) >= _MAX_QUIZZES:
        return await _private(interaction, "Сейчас идёт слишком много квизов. Попробуй через минуту.")

    session = QuizSession(interaction.id, _pick_questions())
    _GAMES[channel_id] = session
    try:
        if database.db_pool is not None:
            try:
                async with asyncio.timeout(1.2):
                    settings = await guild_settings.get(interaction.guild_id)
            except (hub_store.Unavailable, TimeoutError):
                return await _private(interaction, "Настройки игр сейчас недоступны. Попробуй позже.")
            if not settings.get("games_enabled"):
                return await _private(interaction, "Игры отключены в настройках сервера.")

        async with asyncio.timeout(_EDIT_TIMEOUT):
            await interaction.response.defer(thinking=True)
        await _play(interaction, session)
    except (discord.DiscordException, TimeoutError) as error:
        _LOG.info("Quiz delivery stopped: %s", type(error).__name__)
        if not isinstance(error, discord.NotFound) and not discord_api.is_rate_limit_error(error):
            try:
                if interaction.response.is_done():
                    await _edit(interaction, "Квиз Vivian", "Квиз завершился раньше времени. Запусти новый командой `/quiz`.")
                else:
                    await _private(interaction, "Квиз не удалось запустить. Попробуй чуть позже.")
            except (discord.DiscordException, TimeoutError):
                pass
    finally:
        _GAMES.pop(channel_id, None)


# Old buttons can show a private expiry notice even after a restart.
config.bot.add_dynamic_items(QuizAnswer)
