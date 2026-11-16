"""Publish a native Components V2 premiere with durable attendance."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
import re
import sqlite3
import time
from urllib.parse import urlsplit

import discord
from discord import app_commands

from core import config, premiere_store


_LOG = logging.getLogger(__name__)
_NO_MENTIONS = discord.AllowedMentions.none()
_ACCENT = 0xB782FE
_CLOSED_ACCENT = 0x788194
_CANCELLED_ACCENT = 0xC04F70
_MIN_LEAD = 60
_MAX_LEAD = 30 * 86400
_TIME_HELP = "Укажи время как `45м`, `2ч` или `2026-12-10 21:00+02:00`."
_RELATIVE = re.compile(r"(?:через\s+)?([0-9]{1,5})\s*([мmчhдd])", re.IGNORECASE)
_UNITS = dict(zip("мmчhдd", (60, 60, 3600, 3600, 86400, 86400)))
_BUTTONS = {
    "going": ("Я приду", discord.ButtonStyle.success),
    "maybe": ("Возможно", discord.ButtonStyle.primary),
    "remove": ("Убрать ответ", discord.ButtonStyle.secondary),
    "refresh": ("Обновить", discord.ButtonStyle.secondary),
    "cancel_prompt": ("Отменить премьеру", discord.ButtonStyle.danger),
}
_VOTE_FEEDBACK = {
    "going": "Ты в списке участников.",
    "maybe": "Твой ответ «Возможно» сохранён.",
    "removed": "Твой ответ убран из списка.",
    "updated": "Панель обновляется.",
    "cancelled": "Премьера отменена.",
    "closed": "Запись на эту премьеру уже закрыта.",
}


def parse_start(value: str, now: int) -> int:
    value = value.strip()
    match = _RELATIVE.fullmatch(value)
    if match:
        delay, unit = match.groups()
        starts_at = now + int(delay) * _UNITS[unit.casefold()]
    else:
        try:
            stamp = datetime.fromisoformat(value.upper())
            if stamp.utcoffset() is None:
                raise ValueError
            starts_at = int(stamp.timestamp())
        except (ValueError, OverflowError, OSError):
            raise ValueError(_TIME_HELP) from None
    if not _MIN_LEAD <= starts_at - now <= _MAX_LEAD:
        raise ValueError("Запланируй начало в пределах от одной минуты до 30 дней.")
    return starts_at


def _event_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or "\\" in value
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError
        url.port
    except ValueError:
        raise ValueError("Для встречи можно указать только корректную HTTPS-ссылку.") from None
    return value


def _safe(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value, ignore_links=False))


def _make_view(box: discord.ui.Container, *, timeout: float | None = None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    view.add_item(box)
    return view


def premiere_card(event: premiere_store.Premiere, *, now: int | None = None) -> discord.ui.LayoutView:
    now = int(time.time()) if now is None else now
    if event.cancelled_at is not None:
        status, accent = "Отменена", _CANCELLED_ACCENT
    elif event.open_at(now):
        status, accent = "Запись до начала", _ACCENT
    else:
        status, accent = "Запись закрыта", _CLOSED_ACCENT
    box = discord.ui.Container(
        discord.ui.TextDisplay(f"-# Премьера Vivian · {status}\n## {_safe(event.title)}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            f"**Начало** · <t:{event.starts_at}:F> · <t:{event.starts_at}:R>\n"
            f"**Организатор** · <@{event.host_id}>"
        ),
        accent_colour=accent,
    )
    if event.details:
        box.add_item(discord.ui.TextDisplay(_safe(event.details)))
    box.add_item(discord.ui.Separator())
    box.add_item(discord.ui.TextDisplay(
        f"**Придут · {event.going}**     **Возможно · {event.maybe}**"
    ))
    if event.open_at(now):
        box.add_item(discord.ui.ActionRow(
            PremiereAction(event.id, "going"),
            PremiereAction(event.id, "maybe"),
            PremiereAction(event.id, "remove"),
        ))
    controls = [PremiereAction(event.id, "refresh")]
    if event.url and event.cancelled_at is None:
        controls.insert(0, discord.ui.Button(label="Ссылка встречи", url=event.url))
    if event.open_at(now):
        controls.append(PremiereAction(event.id, "cancel_prompt"))
    box.add_item(discord.ui.ActionRow(*controls))
    return _make_view(box)


async def _notice(interaction, title: str, message: str, control: discord.ui.Button | None = None):
    box = discord.ui.Container(discord.ui.TextDisplay(f"### {title}\n{message}"), accent_colour=_ACCENT)
    if control:
        box.add_item(discord.ui.ActionRow(control))
    view = _make_view(box, timeout=90 if control else 60)
    if interaction.response.is_done():
        await interaction.edit_original_response(view=view, allowed_mentions=_NO_MENTIONS)
    else:
        await interaction.response.send_message(view=view, ephemeral=True, allowed_mentions=_NO_MENTIONS)


def _from_vivian(interaction, message) -> bool:
    bot_user = interaction.client.user
    return bool(
        interaction.guild_id and interaction.channel_id and not interaction.user.bot
        and bot_user and message and message.author.id == bot_user.id
    )


@dataclass
class _CardRefresh:
    needed: bool = True
    task: asyncio.Task | None = None


_REFRESHES: dict[int, _CardRefresh] = {}


async def _coalesce_refreshes(event_id, guild_id, channel_id, message, refresh):
    try:
        while refresh.needed:
            await asyncio.sleep(0.75)
            refresh.needed = False
            try:
                latest, _ = await premiere_store.interact(
                    event_id, guild_id, channel_id, message.id, 0, "refresh",
                )
                async with asyncio.timeout(10):
                    await message.edit(view=premiere_card(latest), allowed_mentions=_NO_MENTIONS)
            except (premiere_store.PremiereUnavailable, sqlite3.Error, OSError, discord.DiscordException) as error:
                _LOG.info("Premiere card refresh failed: %s", type(error).__name__)
    finally:
        _REFRESHES.pop(event_id, None)


def _queue_refresh(event_id, guild_id, channel_id, message):
    refresh = _REFRESHES.get(event_id)
    if refresh:
        refresh.needed = True
        return
    refresh = _REFRESHES[event_id] = _CardRefresh()
    refresh.task = asyncio.create_task(_coalesce_refreshes(event_id, guild_id, channel_id, message, refresh))


async def _apply_action(interaction, event_id, action, message, *, update=False):
    if not _from_vivian(interaction, message):
        await _notice(interaction, "Премьера Vivian", "Открой исходную карточку премьеры.")
        return
    if update:
        await interaction.response.defer()
    else:
        await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        event, status = await premiere_store.interact(
            event_id, interaction.guild_id, interaction.channel_id, message.id, interaction.user.id, action,
        )
    except premiere_store.PremiereUnavailable as error:
        await _notice(interaction, "Премьера Vivian", str(error))
        return
    except (sqlite3.Error, OSError) as error:
        _LOG.info("Premiere registration unavailable: %s", type(error).__name__)
        await _notice(interaction, "Премьера Vivian", "Хранилище недоступно. Попробуй чуть позже.")
        return
    if status == "confirm":
        await _notice(
            interaction, "Отменить премьеру?",
            "Запись закроется, а участники увидят пометку об отмене на карточке.",
            _ConfirmCancellation(event_id, event.host_id, message),
        )
        return
    _queue_refresh(event_id, interaction.guild_id, interaction.channel_id, message)
    await _notice(interaction, "Премьера Vivian", _VOTE_FEEDBACK[status])


class PremiereAction(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vivian:premiere:(?P<event>[0-9]{1,20}):(?P<action>going|maybe|remove|refresh|cancel_prompt)",
):
    def __init__(self, event_id: int, action: str):
        self.event_id = event_id
        self.action = action
        label, style = _BUTTONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"vivian:premiere:{event_id}:{action}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["event"]), match["action"])

    async def callback(self, interaction: discord.Interaction):
        await _apply_action(interaction, self.event_id, self.action, interaction.message)


class _ConfirmCancellation(discord.ui.Button):
    def __init__(self, event_id, host_id, message):
        super().__init__(style=discord.ButtonStyle.danger, label="Подтвердить отмену")
        self.event_id, self.host_id, self.message = event_id, host_id, message

    async def callback(self, interaction):
        if interaction.user.id != self.host_id:
            await _notice(interaction, "Премьера Vivian", "Подтвердить отмену может её организатор.")
            return
        await _apply_action(interaction, self.event_id, "cancel", self.message, update=True)


def _send_allowed(permissions, *, thread):
    send = permissions.send_messages_in_threads if thread else permissions.send_messages
    return permissions.view_channel and send


@config.bot.tree.command(name="premiere", description="Запланировать премьеру с интерактивной записью участников.")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 45, key=lambda interaction: (interaction.guild_id, interaction.user.id))
@app_commands.describe(
    title="Название премьеры, эфира или совместного просмотра",
    start="Время начала. Примеры: 45м, 2ч или 2026-12-10 21:00+02:00",
    details="Что стоит знать участникам встречи",
    link="Необязательная HTTPS-ссылка для подключения",
)
async def premiere(
    interaction: discord.Interaction,
    title: app_commands.Range[str, 1, 100],
    start: app_commands.Range[str, 2, 40],
    details: app_commands.Range[str, 1, 600] = "",
    link: app_commands.Range[str, 1, 400] = "",
):
    channel = interaction.channel
    thread = isinstance(channel, discord.Thread)
    if not interaction.guild_id or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await _notice(interaction, "Премьера Vivian", "Запусти команду в текстовом канале или ветке сервера.")
        return
    permissions = interaction.permissions
    if not (permissions.create_events or permissions.manage_events) or not _send_allowed(permissions, thread=thread):
        await _notice(
            interaction, "Премьера Vivian",
            "Нужны права писать в этом канале и создавать события или управлять ими.",
        )
        return
    if not _send_allowed(interaction.app_permissions, thread=thread):
        await _notice(interaction, "Премьера Vivian", "Vivian нужны права отправки сообщений в этом канале.")
        return
    try:
        title = " ".join(title.split())
        if not title:
            raise ValueError("Введи название премьеры.")
        starts_at = parse_start(start, int(time.time()))
        url = _event_url(link)
    except ValueError as error:
        await _notice(interaction, "Премьера Vivian", str(error))
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    event = premiere_store.Premiere(
        interaction.id, interaction.guild_id, channel.id, interaction.user.id,
        title, details.strip(), url, starts_at,
    )
    try:
        event = await premiere_store.create(event)
    except premiere_store.PremiereUnavailable as error:
        await _notice(interaction, "Премьера Vivian", str(error))
        return
    except (sqlite3.Error, OSError) as error:
        _LOG.info("Premiere storage unavailable: %s", type(error).__name__)
        await _notice(interaction, "Премьера Vivian", "Хранилище недоступно. Попробуй чуть позже.")
        return

    try:
        async with asyncio.timeout(12):
            if event.message_id is not None:
                message = channel.get_partial_message(event.message_id)
            else:
                message = await channel.send(
                    view=premiere_card(event), allowed_mentions=_NO_MENTIONS,
                    silent=True, nonce=str(interaction.id),
                )
    except (OSError, discord.DiscordException) as error:
        _LOG.info("Premiere publication unconfirmed: %s", type(error).__name__)
        await _notice(
            interaction, "Премьера Vivian",
            "Не удалось подтвердить публикацию. Проверь канал перед повторным запуском.",
        )
        return

    try:
        await premiere_store.interact(event.id, event.guild_id, event.channel_id, message.id, interaction.user.id, "bind")
    except (premiere_store.PremiereUnavailable, sqlite3.Error, OSError) as error:
        # The first valid interaction on the published card can finish this binding.
        _LOG.info("Premiere card binding deferred: %s", type(error).__name__)
    await _notice(interaction, "Премьера опубликована", f"[Открыть карточку]({message.jump_url})")


config.bot.add_dynamic_items(PremiereAction)
