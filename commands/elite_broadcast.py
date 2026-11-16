"""Persistent live rundown and timing controls for Discord hosts."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from weakref import WeakValueDictionary

import discord
from discord import app_commands

from core import broadcast_store as store, config


broadcast = app_commands.Group(
    name="broadcast", description="Режиссёрская панель эфира с таймером и хронометражем", guild_only=True,
)
_LOG = logging.getLogger(__name__)
_NO_MENTIONS = discord.AllowedMentions.none()
_ACCENT = 0xE4B561
_PAUSED_ACCENT = 0xAB91E0
_CLOSED_ACCENT = 0x69B99E
_DISCORD_TIMEOUT = 8
_CHANNEL_LOCKS: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def parse_rundown(text: str) -> tuple[store.Segment, ...]:
    parts = text.split("|")
    if not 1 <= len(parts) <= store.MAX_SEGMENTS:
        raise store.BroadcastUnavailable(f"В плане должно быть от 1 до {store.MAX_SEGMENTS} сегментов.")
    segments = []
    for part in parts:
        name, separator, raw_minutes = part.rpartition("=")
        raw_minutes = raw_minutes.strip()
        if not separator or not raw_minutes.isascii() or not raw_minutes.isdecimal() or len(raw_minutes) > 3:
            raise store.BroadcastUnavailable("Запиши план в формате «Вступление=2 | Основная часть=20 | Вопросы=5».")
        segments.append(store.segment(name, int(raw_minutes)))
    return tuple(segments)


def _safe(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text, ignore_links=False))


def _clock(seconds: int) -> str:
    hours, remaining = divmod(seconds, 3600)
    minutes, remainder = divmod(remaining, 60)
    return f"{hours}:{minutes:02d}:{remainder:02d}" if hours else f"{minutes:02d}:{remainder:02d}"


def _difference(seconds: int) -> str:
    if seconds == 0:
        return "по плану"
    return f"{'+' if seconds > 0 else '−'}{_clock(abs(seconds))}"


def _view(*items, accent, timeout=None):
    layout = discord.ui.LayoutView(timeout=timeout)
    layout.add_item(discord.ui.Container(*items, accent_colour=accent))
    return layout


def _segment_line(state, index, item):
    title = f"**{index + 1:02}. {_safe(item.name)}**"
    planned = _clock(item.planned_seconds)
    if item.actual_seconds is not None:
        actual = _clock(item.actual_seconds)
        delta = _difference(item.actual_seconds - item.planned_seconds)
        return f"✓ {title} · {actual} / {planned} · {delta}"
    if state.finished_at is not None:
        return f"– {title} · {planned} · пропущен"
    symbol = "▶" if index == state.current_index else "·"
    return f"{symbol} {title} · {planned}"


def broadcast_card(state: store.Broadcast, *, now: int | None = None) -> discord.ui.LayoutView:
    now = int(time.time()) if now is None else now
    if state.finished_at is not None:
        label, accent = "Эфир завершён", _CLOSED_ACCENT
        performed = [item for item in state.segments if item.actual_seconds is not None]
        actual = sum(item.actual_seconds for item in performed)
        plan = sum(item.planned_seconds for item in performed)
        details = (
            f"**Активное время** · {_clock(actual)}  ·  **Отклонение** · {_difference(actual - plan)}\n"
            f"Сегментов проведено: **{len(performed)} из {len(state.segments)}** · Завершён <t:{state.finished_at}:R>"
        )
    else:
        expired = now >= state.expires_at
        paused = state.anchor is None
        label, accent = ("Лимит времени", _PAUSED_ACCENT) if expired else (
            ("Пауза", _PAUSED_ACCENT) if paused else ("В эфире", _ACCENT)
        )
        current = state.segments[state.current_index]
        elapsed = state.elapsed_at(now)
        remaining = current.planned_seconds - elapsed
        details = (f"**Сейчас** · {_safe(current.name)}\n"
                   f"**Прошло** · {_clock(elapsed)}  ·  **План** · {_clock(current.planned_seconds)}")
        if expired:
            details += "\nЗакрой сводку через /broadcast end."
        elif not paused:
            deadline = state.anchor + current.planned_seconds - state.accumulated
            details += f"\nПлановый рубеж <t:{deadline}:R>"
        elif remaining >= 0:
            details += f"\nВремя по плану на паузе · **{_clock(remaining)}**"
        else:
            details += f"\nПревышение плана · **{_clock(-remaining)}**"
    items = [
        discord.ui.TextDisplay(f"-# Пульт Vivian · {label}\n## {_safe(state.title)}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(details),
        discord.ui.Separator(),
    ]
    lines = [_segment_line(state, index, item) for index, item in enumerate(state.segments)]
    for offset in range(0, len(lines), 6):
        heading = "**Порядок эфира**\n" if offset == 0 else ""
        items.append(discord.ui.TextDisplay(heading + "\n".join(lines[offset:offset + 6])))
    if state.finished_at is None and now < state.expires_at:
        items.append(discord.ui.ActionRow(
            BroadcastAction(
                state.id, "next", "Следующий сегмент",
                disabled=state.current_index + 1 == len(state.segments),
            ),
            BroadcastAction(state.id, "pause", "Продолжить" if state.anchor is None else "Пауза"),
        ))
        items.append(discord.ui.TextDisplay(
            f"-# Ведущий <@{state.host_id}> · /broadcast segment добавить · /broadcast end подвести итоги"
        ))
    return _view(*items, accent=accent)


async def _notice(interaction, title, text):
    view = _view(discord.ui.TextDisplay(f"### {title}\n{text}"), accent=_ACCENT, timeout=60)
    if interaction.response.is_done():
        await interaction.edit_original_response(view=view, allowed_mentions=_NO_MENTIONS)
    else:
        await interaction.response.send_message(view=view, ephemeral=True, allowed_mentions=_NO_MENTIONS)


def _can_send(permissions, *, thread):
    return permissions.view_channel and (
        permissions.send_messages_in_threads if thread else permissions.send_messages
    )


async def _valid_channel(interaction, *, start=False):
    channel = interaction.channel
    thread = isinstance(channel, discord.Thread)
    if interaction.guild_id is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await _notice(interaction, "Эфир Vivian", "Открой команду в текстовом канале или ветке сервера.")
        return False
    permissions = interaction.permissions
    if not _can_send(permissions, thread=thread):
        await _notice(interaction, "Эфир Vivian", "Нужны права просматривать канал и отправлять в него сообщения.")
        return False
    if start and not (permissions.create_events or permissions.manage_events):
        await _notice(interaction, "Эфир Vivian", "Чтобы начать эфир, нужно право «Создавать события» или «Управлять событиями».")
        return False
    if not _can_send(interaction.app_permissions, thread=thread):
        await _notice(interaction, "Эфир Vivian", "Vivian нужны права отправки сообщений в этом канале.")
        return False
    return True


async def _storage_notice(interaction, error):
    if isinstance(error, store.BroadcastUnavailable):
        await _notice(interaction, "Эфир Vivian", str(error))
    else:
        _LOG.info("Broadcast storage unavailable: %s", type(error).__name__)
        await _notice(interaction, "Эфир Vivian", "Хранилище эфира сейчас недоступно. Попробуй чуть позже.")


def _channel_lock(channel_id):
    return _CHANNEL_LOCKS.setdefault(channel_id, asyncio.Lock())


async def _change(interaction, action, *, next_segment=None, board=None):
    if not await _valid_channel(interaction):
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    channel = interaction.channel
    async with _channel_lock(channel.id):
        try:
            state = await store.change(
                interaction.guild_id, channel.id, interaction.user.id, interaction.id, action,
                manager=interaction.permissions.manage_events, next_segment=next_segment, board=board,
            )
        except (store.BroadcastUnavailable, sqlite3.Error, OSError) as error:
            return await _storage_notice(interaction, error)
        message = channel.get_partial_message(state.message_id)
        try:
            async with asyncio.timeout(_DISCORD_TIMEOUT):
                await message.edit(view=broadcast_card(state), allowed_mentions=_NO_MENTIONS)
        except discord.NotFound:
            await _notice(interaction, "Эфир Vivian", "Действие сохранено. Исходная карточка удалена, команды эфира доступны.")
            return
        except (discord.DiscordException, OSError) as error:
            _LOG.info("Broadcast panel edit unavailable: %s", type(error).__name__)
            await _notice(interaction, "Эфир Vivian", "Действие сохранено. Discord пока не обновил исходную карточку.")
            return
    if action == "pause":
        title = "Эфир на паузе" if state.anchor is None else "Эфир продолжен"
    else:
        title = {"next": "Сегмент переключён", "segment": "Сегмент добавлен", "end": "Итоги эфира готовы"}[action]
    await _notice(interaction, title, f"[Открыть панель]({message.jump_url})")


class BroadcastAction(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vivian:broadcast:(?P<broadcast>[0-9]{1,20}):(?P<action>next|pause)",
):
    def __init__(self, broadcast_id, action, label, *, disabled=False):
        self.broadcast_id, self.action = broadcast_id, action
        super().__init__(discord.ui.Button(
            label=label, style=discord.ButtonStyle.primary if action == "next" else discord.ButtonStyle.secondary,
            custom_id=f"vivian:broadcast:{broadcast_id}:{action}", disabled=disabled,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["broadcast"]), match["action"], item.label, disabled=item.disabled)

    async def callback(self, interaction: discord.Interaction):
        bot_user = interaction.client.user
        message = interaction.message
        if message is None or bot_user is None or message.author.id != bot_user.id or interaction.user.bot:
            await _notice(interaction, "Эфир Vivian", "Открой исходную карточку эфира.")
            return
        await _change(interaction, self.action, board=(self.broadcast_id, message.id))


@broadcast.command(name="start", description="Опубликовать пульт эфира с готовым расписанием")
@app_commands.checks.cooldown(1, 10, key=lambda interaction: (interaction.guild_id, interaction.user.id))
@app_commands.describe(title="Имя эфира", rundown="Сегменты в минутах. Пример: Вступление=2 | Основная часть=20")
async def start(interaction: discord.Interaction, title: app_commands.Range[str, 1, 100],
                rundown: app_commands.Range[str, 3, 1200]):
    if not await _valid_channel(interaction, start=True):
        return
    try:
        title = " ".join(title.split())
        if not title:
            raise store.BroadcastUnavailable("Введи название эфира.")
        segments = parse_rundown(rundown)
    except store.BroadcastUnavailable as error:
        return await _storage_notice(interaction, error)
    await interaction.response.defer(thinking=True, ephemeral=True)
    channel = interaction.channel
    async with _channel_lock(channel.id):
        try:
            state = await store.create(
                interaction.id, interaction.guild_id, channel.id, interaction.user.id, title, segments,
            )
        except (store.BroadcastUnavailable, sqlite3.Error, OSError) as error:
            return await _storage_notice(interaction, error)
        if state.message_id is not None:
            message = channel.get_partial_message(state.message_id)
        else:
            try:
                async with asyncio.timeout(_DISCORD_TIMEOUT):
                    message = await channel.send(view=broadcast_card(state), silent=True,
                                                 nonce=str(state.id), allowed_mentions=_NO_MENTIONS)
            except (discord.DiscordException, OSError) as error:
                _LOG.info("Broadcast publication unconfirmed: %s", type(error).__name__)
                await _notice(
                    interaction, "Публикация не подтверждена",
                    "Проверь канал. Если карточка не появилась, повтори запуск через 90 секунд.",
                )
                return
            try:
                await store.bind_message(state.guild_id, state.channel_id, state.id, message.id)
            except (store.BroadcastUnavailable, sqlite3.Error, OSError) as error:
                _LOG.info("Broadcast message binding deferred: %s", type(error).__name__)
                await _notice(interaction, "Пульт опубликован",
                              f"[Открыть панель]({message.jump_url}) и нажать кнопку, чтобы подтвердить её привязку.")
                return
    await _notice(interaction, "Пульт эфира открыт", f"[Перейти к расписанию]({message.jump_url})")


@broadcast.command(name="segment", description="Добавить сегмент в конец текущего расписания")
@app_commands.describe(title="Название сегмента", minutes="Длительность в минутах")
async def add_segment(interaction: discord.Interaction, title: app_commands.Range[str, 1, 90],
                      minutes: app_commands.Range[int, 1, store.MAX_MINUTES_PER_SEGMENT]):
    try:
        next_segment = store.segment(title, minutes)
    except store.BroadcastUnavailable as error:
        return await _storage_notice(interaction, error)
    await _change(interaction, "segment", next_segment=next_segment)


@broadcast.command(name="next", description="Записать время текущего сегмента и перейти к следующему")
async def next_command(interaction: discord.Interaction):
    await _change(interaction, "next")


@broadcast.command(name="pause", description="Поставить таймер на паузу или продолжить текущий сегмент")
async def pause_command(interaction: discord.Interaction):
    await _change(interaction, "pause")


@broadcast.command(name="end", description="Завершить эфир и показать фактическое время каждого сегмента")
async def end_command(interaction: discord.Interaction):
    await _change(interaction, "end")


config.bot.tree.add_command(broadcast)
config.bot.add_dynamic_items(BroadcastAction)
