"""Native Discord polls with private acknowledgements."""
from __future__ import annotations

from collections import OrderedDict
from datetime import timedelta
import math
import time

import discord
from discord import app_commands

from core import config, discord_api
from utils import command_responses, components_v2 as v2


_PUBLICATION_COOLDOWN = 30
_MAX_COOLDOWN_ENTRIES = 2048
_recent_publications = OrderedDict()


def _poll_text(text: str) -> str:
    return discord.utils.escape_mentions(" ".join(text.split()))


def _build_poll(question: str, answers: str, hours: int, multiple: bool) -> discord.Poll:
    question = _poll_text(question)
    if not 1 <= len(question) <= 300:
        raise ValueError("Вопрос должен содержать от 1 до 300 символов.")
    choices = [_poll_text(answer) for answer in answers.split("|")]
    if not 2 <= len(choices) <= 6 or any(not answer for answer in choices):
        raise ValueError("Укажи от 2 до 6 вариантов через |. Например, Minecraft | Terraria.")
    if any(len(answer) > 55 for answer in choices):
        raise ValueError("Вариант ответа может содержать до 55 символов.")
    if len({answer.casefold() for answer in choices}) != len(choices):
        raise ValueError("Варианты ответа должны различаться.")
    poll = discord.Poll(question, timedelta(hours=hours), multiple=multiple)
    for answer in choices:
        poll.add_answer(text=answer)
    return poll


def _can_post(permissions: discord.Permissions, *, thread: bool) -> bool:
    can_send = permissions.send_messages_in_threads if thread else permissions.send_messages
    return permissions.view_channel and can_send and permissions.send_polls


def _claim_publication(guild_id: int, channel_id: int, user_id: int) -> int:
    now = time.monotonic()
    while _recent_publications and next(iter(_recent_publications.values())) <= now:
        _recent_publications.popitem(last=False)
    keys = ((guild_id, user_id), (channel_id,))
    ready_at = max(_recent_publications.get(key, now) for key in keys)
    if ready_at > now:
        return math.ceil(ready_at - now)
    if len(_recent_publications) + len(keys) > _MAX_COOLDOWN_ENTRIES:
        return math.ceil(next(iter(_recent_publications.values())) - now)
    for key in keys:
        _recent_publications[key] = now + _PUBLICATION_COOLDOWN
    return 0


def _publication_error(error: Exception) -> str:
    if discord_api.is_rate_limit_error(error):
        return "Discord временно ограничил отправку. Попробуй создать опрос позже."
    if isinstance(error, discord.HTTPException):
        if error.code in {200000, 240000}:
            return "Фильтры Discord отклонили опрос. Проверь текст вопроса и ответов."
        if error.code in {160005, 520002}:
            return "Discord не разрешает опросы в этом канале или закрытой ветке."
        if isinstance(error, discord.Forbidden):
            return "У Vivian больше нет доступа или прав на отправку опроса в этот канал."
    return "Не удалось подтвердить публикацию опроса. Проверь канал перед повторной попыткой."


@config.bot.tree.command(name="poll", description="Запустить голосование Discord в текущем канале.")
@app_commands.guild_only()
@app_commands.describe(
    question="Вопрос для голосования",
    answers="От 2 до 6 вариантов через |. Например, Minecraft | Terraria",
    hours="Сколько часов принимать голоса (от 1 до 168, по умолчанию 24)",
    multiple="Разрешить выбирать несколько ответов",
)
async def poll(
    interaction: discord.Interaction,
    question: app_commands.Range[str, 1, 300],
    answers: app_commands.Range[str, 3, 360],
    hours: app_commands.Range[int, 1, 168] = 24,
    multiple: bool = False,
):
    channel = interaction.channel
    thread = isinstance(channel, discord.Thread)
    notice = None
    if interaction.guild_id is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        notice = "Опрос можно запустить в текстовом канале сервера или в ветке."
    elif not _can_post(interaction.permissions, thread=thread):
        notice = "Для этой команды нужны права на отправку сообщений и создание опросов в этом канале."
    elif not _can_post(interaction.app_permissions, thread=thread):
        notice = "Vivian нужны права на отправку сообщений и создание опросов в этом канале."
    if notice is not None:
        await v2.send_response(interaction, notice, ephemeral=True)
        return
    try:
        native_poll = _build_poll(question, answers, hours, multiple)
    except ValueError as error:
        await v2.send_response(interaction, str(error), ephemeral=True)
        return

    if not await command_responses.defer_command(interaction, ephemeral=True):
        return
    wait_seconds = _claim_publication(interaction.guild_id, channel.id, interaction.user.id)
    if wait_seconds:
        await command_responses.finish_deferred(
            interaction, f"Следующий опрос можно создать через {wait_seconds} с.",
        )
        return
    try:
        message = await channel.send(
            f"Опрос от <@{interaction.user.id}>",
            poll=native_poll,
            allowed_mentions=discord.AllowedMentions.none(),
            silent=True,
            nonce=f"poll:{interaction.id}",
        )
    except (discord.DiscordException, OSError) as error:
        await command_responses.finish_deferred(interaction, _publication_error(error))
    else:
        await command_responses.finish_deferred(interaction, f"[Перейти к опросу]({message.jump_url})")
