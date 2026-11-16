"""Five shared, replayable puzzles rendered with Discord Components V2."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time

import discord
from discord import app_commands

from core import config, database, guild_settings, hub_store
from core import puzzle_games as games
from utils import components_v2 as v2


_LOG = logging.getLogger(__name__)
_NO_MENTIONS = discord.AllowedMentions.none()
_ACCENT = 0xA382FF
_TTL = 300
_MAX_SESSIONS = 16
_MAX_GUESSES = 24
_MAX_SWITCHES = 40
_PUZZLE_ENDED = "Эта головоломка уже закрыта. Открой новую через `/puzzle`."
_PLACEHOLDERS = {
    "code": "Например, 1264",
    "sequence": "Например, 23",
    "cipher": "Исходная фраза по-русски",
    "grid": "Например, #### / .... / #..# / .##.",
}


@dataclass
class PuzzleSession:
    id: int
    guild_id: int
    channel_id: int
    owner_id: int
    challenge: games.Challenge
    expires_at: int
    deadline: float
    lit: int
    message: discord.Message | None = None
    attempts: int = 0
    last_play: dict[int, float] = field(default_factory=dict)
    guesses: set[str] = field(default_factory=set)
    code_progress: list[tuple[str, int, int]] = field(default_factory=list)
    status: str = "active"
    winner_id: int | None = None
    revision: int = 0
    expiry: asyncio.Task | None = None
    refresh: asyncio.Task | None = None

    @property
    def limit(self):
        return _MAX_SWITCHES if self.challenge.mode == "lights" else _MAX_GUESSES

    def play(self, user_id: int, action: str, value: str, now: float) -> str:
        if self.status != "active" or now >= self.deadline:
            return _PUZZLE_ENDED
        puzzle = self.challenge
        if puzzle.mode == "lights" and action == "cell":
            choice, cooldown = int(value), 0.75
            if not 0 <= choice < 9:
                return "Эта клетка недоступна."
        elif puzzle.mode != "lights" and action == "guess":
            try:
                guess = games.canonical_answer(puzzle.mode, value)
            except ValueError as error:
                return str(error)
            if guess in self.guesses:
                return "Такой ответ уже проверяли. Попробуй другой."
            cooldown = 2.5
        else:
            return "Этот способ ответа здесь не подходит."

        previous = self.last_play.get(user_id)
        if previous is not None and now - previous < cooldown:
            return "Подожди немного перед следующим ходом."
        self.last_play[user_id] = now
        self.attempts += 1

        if action == "cell":
            self.lit ^= games.light_mask(choice)
            success = self.lit == 0
            feedback = f"Переключено поле {games.light_label(choice)}."
        else:
            self.guesses.add(guess)
            success = guess == puzzle.answer
            feedback = "Пока не сходится. Проверь правило и попробуй снова."
            if puzzle.mode == "code":
                exact, moved = games.code_score(puzzle.answer, guess)
                self.code_progress.append((guess, exact, moved))
                feedback = f"На своих местах — **{exact}**. Угадано на других местах — **{moved}**."

        if success:
            self.status, self.winner_id = "won", user_id
            return "Решено! Правило и ответ появятся на общей карточке."
        if self.attempts >= self.limit:
            self.status = "exhausted"
            return "Попытки закончились. Решение появится на общей карточке."
        return feedback


_SESSIONS: dict[int, PuzzleSession] = {}
_RENDERS: set[asyncio.Task] = set()
_MAX_PENDING_RENDERS = 32


class PuzzleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vivian:puzzle:(?P<session>[0-9]{1,20}):(?P<action>guess|end|c[0-8])",
):
    def __init__(self, session_id: int, action: str, *, lit: bool = False):
        self.session_id, self.action = session_id, action
        if action == "guess":
            label, style = "Проверить ответ", discord.ButtonStyle.primary
        elif action == "end":
            label, style = "Завершить", discord.ButtonStyle.secondary
        else:
            label = games.light_label(int(action[1:])) + (" · вкл" if lit else " · выкл")
            style = discord.ButtonStyle.success if lit else discord.ButtonStyle.secondary
        item = discord.ui.Button(label=label, style=style, custom_id=f"vivian:puzzle:{session_id}:{action}")
        super().__init__(item)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["session"]), match["action"])

    async def callback(self, interaction: discord.Interaction):
        session = _current(interaction, self.session_id, getattr(interaction.message, "id", None))
        if session is None:
            return await _private(interaction, _PUZZLE_ENDED)
        if self.action == "guess":
            if session.challenge.mode == "lights":
                return await _private(interaction, "Используй кнопки переключателей.")
            return await interaction.response.send_modal(AnswerModal(session))
        if self.action == "end":
            if interaction.user.id != session.owner_id and not interaction.permissions.manage_messages:
                return await _private(interaction, "Завершить игру может ведущий или модератор канала.")
            session.status = "ended"
            _state_changed(session)
            return await _private(interaction, "Головоломка закрыта. Решение появится на панели.")
        return await _apply(interaction, session, "cell", self.action[1:])


class AnswerModal(discord.ui.Modal, title="Проверить ответ"):
    def __init__(self, session: PuzzleSession):
        super().__init__(timeout=90)
        self.session_id = session.id
        self.message_id = session.message.id
        mode = session.challenge.mode
        self.answer = discord.ui.TextInput(
            style=discord.TextStyle.paragraph if mode == "grid" else discord.TextStyle.short,
            placeholder=_PLACEHOLDERS[mode],
            max_length=100,
        )
        self.add_item(discord.ui.Label(text="Твой ответ", component=self.answer))

    async def on_submit(self, interaction: discord.Interaction):
        session = _current(interaction, self.session_id, self.message_id)
        if session is None:
            return await _private(interaction, _PUZZLE_ENDED)
        return await _apply(interaction, session, "guess", self.answer.value)


def puzzle_card(session: PuzzleSession) -> discord.ui.LayoutView:
    puzzle = session.challenge
    open_ = session.status == "active"
    color = _ACCENT if open_ else 0x54BEA3 if session.status == "won" else 0x6F7D98
    box = discord.ui.Container(
        discord.ui.TextDisplay(f"-# Головоломки Vivian\n## {puzzle.title}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(puzzle.prompt),
        discord.ui.TextDisplay(puzzle.instructions),
        accent_colour=color,
    )
    if open_:
        if puzzle.mode == "lights":
            for start in range(0, 9, 3):
                box.add_item(discord.ui.ActionRow(*(
                    PuzzleButton(session.id, f"c{cell}", lit=bool(session.lit & (1 << cell)))
                    for cell in range(start, start + 3)
                )))
            controls = [PuzzleButton(session.id, "end")]
        else:
            controls = [PuzzleButton(session.id, "guess"), PuzzleButton(session.id, "end")]
        if session.code_progress:
            lines = [f"`{guess}` · {exact} на месте · {moved} не на месте"
                     for guess, exact, moved in session.code_progress]
            box.add_item(discord.ui.TextDisplay("**Попытки**\n" + "\n".join(lines)))
        box.add_item(discord.ui.TextDisplay(
            f"-# До конца <t:{session.expires_at}:R> · Ходов {session.attempts}/{session.limit}"
        ))
        box.add_item(discord.ui.ActionRow(*controls))
    else:
        status_line = {
            "won": f"Решил <@{session.winner_id}> · ходов {session.attempts}",
            "expired": "Время вышло",
            "exhausted": "Попытки закончились",
            "ended": "Игра завершена ведущим или модератором",
        }[session.status]
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(f"**{status_line}**\n{games.answer_block(puzzle)}\n{puzzle.explanation}"))
    layout = discord.ui.LayoutView(timeout=None)
    layout.add_item(box)
    return layout


def _current(interaction, session_id: int, message_id: int | None) -> PuzzleSession | None:
    session = _SESSIONS.get(interaction.channel_id)
    if (session is None or session.id != session_id or session.guild_id != interaction.guild_id
            or session.message is None or session.message.id != message_id or interaction.user.bot):
        return None
    if asyncio.get_running_loop().time() >= session.deadline:
        session.status = "expired"
        _state_changed(session)
        return None
    return session


async def _private(interaction, text: str):
    if interaction.response.is_done():
        return await v2.edit_original(interaction, content=text)
    return await v2.send_response(interaction, text, ephemeral=True)


async def _apply(interaction, session: PuzzleSession, action: str, value: str):
    attempts = session.attempts
    feedback = session.play(interaction.user.id, action, value, asyncio.get_running_loop().time())
    if session.attempts != attempts:
        _state_changed(session)
    return await _private(interaction, feedback)


async def _update_board(session: PuzzleSession):
    while True:
        await asyncio.sleep(0.25)
        revision = session.revision
        try:
            async with asyncio.timeout(8):
                await v2.edit_message(session.message, view=puzzle_card(session), allowed_mentions=_NO_MENTIONS)
        except (discord.DiscordException, OSError) as error:
            _LOG.info("Puzzle board update unavailable: %s", type(error).__name__)
        if revision == session.revision:
            return


def _state_changed(session: PuzzleSession):
    session.revision += 1
    if session.status != "active":
        if _SESSIONS.get(session.channel_id) is session:
            del _SESSIONS[session.channel_id]
        if session.expiry is not None and session.expiry is not asyncio.current_task():
            session.expiry.cancel()
    if session.refresh is not None and not session.refresh.done():
        return
    session.refresh = asyncio.create_task(_update_board(session), name=f"puzzle-board:{session.id}")
    _RENDERS.add(session.refresh)
    session.refresh.add_done_callback(_RENDERS.discard)


async def _expire(session: PuzzleSession):
    await asyncio.sleep(max(0, session.deadline - asyncio.get_running_loop().time()))
    if _SESSIONS.get(session.channel_id) is session:
        session.status = "expired"
        _state_changed(session)


def _can_post(channel, permissions) -> bool:
    allowed_channel = isinstance(channel, (discord.TextChannel, discord.Thread))
    can_send = permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages
    return allowed_channel and permissions.view_channel and can_send


async def _start(interaction: discord.Interaction, mode: games.Mode):
    channel = interaction.channel
    if not interaction.guild_id or not _can_post(channel, interaction.permissions):
        return await _private(interaction, "Запусти головоломку в канале сервера, где можешь писать.")
    if not _can_post(channel, interaction.app_permissions):
        return await _private(interaction, "Vivian нужны права видеть канал и отправлять сообщения.")
    if channel.id in _SESSIONS:
        return await _private(interaction, "В этом канале уже идёт головоломка.")
    if len(_SESSIONS) >= _MAX_SESSIONS or len(_RENDERS) >= _MAX_PENDING_RENDERS:
        return await _private(interaction, "Сейчас много активных головоломок. Попробуй немного позже.")

    clock = asyncio.get_running_loop().time
    challenge = games.generate(mode)
    session = PuzzleSession(
        interaction.id, interaction.guild_id, channel.id, interaction.user.id,
        challenge, int(time.time()) + _TTL, clock() + _TTL, challenge.lights,
    )
    _SESSIONS[channel.id] = session
    try:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if database.db_pool is not None:
            try:
                async with asyncio.timeout(4):
                    settings = await guild_settings.get(interaction.guild_id)
            except (hub_store.Unavailable, TimeoutError):
                return await _private(interaction, "Настройки игр сейчас недоступны. Попробуй позже.")
            if not settings.get("games_enabled"):
                return await _private(interaction, "На этом сервере игры выключены.")
        try:
            async with asyncio.timeout(12):
                session.message = await v2.send_message(
                    channel, view=puzzle_card(session), nonce=str(interaction.id),
                    allowed_mentions=_NO_MENTIONS, silent=True,
                )
        except (discord.DiscordException, OSError):
            return await _private(interaction, "Публикация не подтвердилась. Проверь канал перед повтором.")
        session.expiry = asyncio.create_task(_expire(session), name=f"puzzle-expiry:{session.id}")
        return await _private(interaction, f"[Открыть головоломку]({session.message.jump_url})")
    finally:
        if session.message is None and _SESSIONS.get(channel.id) is session:
            del _SESSIONS[channel.id]


puzzle = app_commands.Group(name="puzzle", description="Совместные головоломки Vivian для чата", guild_only=True)


def _cooldown_key(interaction):
    return interaction.guild_id, interaction.user.id


_start_cooldown = app_commands.checks.cooldown(1, 25, key=_cooldown_key)


@puzzle.command(name="code", description="Угадать четырёхзначный код с подсказками о совпадениях.")
@_start_cooldown
async def code(interaction: discord.Interaction):
    await _start(interaction, "code")


@puzzle.command(name="sequence", description="Восстановить следующее число по скрытому правилу ряда.")
@_start_cooldown
async def sequence(interaction: discord.Interaction):
    await _start(interaction, "sequence")


@puzzle.command(name="cipher", description="Расшифровать русскую фразу, сдвинутую по алфавиту.")
@_start_cooldown
async def cipher(interaction: discord.Interaction):
    await _start(interaction, "cipher")


@puzzle.command(name="grid", description="Восстановить клетки нонограммы 4 × 4 по строкам и столбцам.")
@_start_cooldown
async def grid(interaction: discord.Interaction):
    await _start(interaction, "grid")


@puzzle.command(name="lights", description="Погасить игровое поле 3 × 3 кнопками, переключающими соседей.")
@_start_cooldown
async def lights(interaction: discord.Interaction):
    await _start(interaction, "lights")


config.bot.tree.add_command(puzzle)
config.bot.add_dynamic_items(PuzzleButton)
