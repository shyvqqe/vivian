from __future__ import annotations

import asyncio
from collections import OrderedDict
import re
import time

import discord

from core import config
from utils import helpers


_NAME_CALL = re.compile(
    r"^\s*(?:(?:привет|хай|эй|здравствуй|доброе утро|добрый день|добрый вечер|hello|hey|hi)[\s,.!—-]+)?"
    r"(?:vivian|вивиан)(?=$|[\s,.:;!?…—–-])|,\s*(?:vivian|вивиан)[.!?…\s]*$",
    re.IGNORECASE,
)
_FOLLOWUP_SECONDS = 90
_ACTIVE_CHANNEL_LIMIT = 512
_active_chats: OrderedDict[int, tuple[int, float]] = OrderedDict()


def _bot_permissions(message):
    if message.guild is None or message.guild.me is None:
        return None
    return message.channel.permissions_for(message.guild.me)


async def _reply_target(message, permissions, *, fetch):
    reference = message.reference
    if message.type != discord.MessageType.reply or reference is None or reference.channel_id != message.channel.id:
        return None
    if permissions is not None and not permissions.read_message_history:
        return None
    resolved = helpers._vivian_reply_reference(message)
    if resolved is not None:
        return resolved
    if (not fetch or reference.resolved is not None or reference.cached_message is not None
            or reference.message_id is None):
        return None
    try:
        async with asyncio.timeout(2.5):
            target = await message.channel.fetch_message(reference.message_id)
    except (discord.DiscordException, OSError, TimeoutError):
        return None
    return target if target.author.id == getattr(config.bot.user, "id", None) else None


async def select_turn(message) -> tuple[str | None, discord.Message | None]:
    content = message.content or ""
    if (message.type not in (discord.MessageType.default, discord.MessageType.reply)
            or not (content.strip() or message.attachments or message.stickers)):
        return None, None

    prefix = config.command_prefix(config.bot, message)
    prefixes = (prefix,) if isinstance(prefix, str) else tuple(prefix)
    if message.guild is None:
        if message.channel.type != discord.ChannelType.private:
            return None, None
        # A private message is an invitation to chat. Only registered prefix
        # commands take a separate path, so exclamations still reach Vivian.
        if content.startswith(prefixes) and (await config.bot.get_context(message)).valid:
            return None, None
        return "dm", await _reply_target(message, None, fetch=True)
    if content.startswith(prefixes):
        return None, None

    bot_id = getattr(config.bot.user, "id", None)
    mentioned = bot_id is not None and any(user.id == bot_id for user in message.mentions)
    called = _NAME_CALL.search(content) is not None
    permissions = _bot_permissions(message)
    if permissions is not None:
        can_send = (permissions.send_messages_in_threads if isinstance(message.channel, discord.Thread)
                    else permissions.send_messages)
        if not (permissions.view_channel and can_send):
            if mentioned or called:
                print(f"[AI] Cannot send in channel {message.channel.id}; check Discord permissions.", flush=True)
            return None, None

    reply_target = await _reply_target(message, permissions, fetch=not (mentioned or called))
    active = _active_chats.get(message.channel.id)
    continuing = (active is not None and active[0] == message.author.id and active[1] > time.monotonic()
                  and not message.mentions and message.reference is None)
    if active is not None and not continuing:
        _active_chats.pop(message.channel.id, None)
    if mentioned:
        return "mention", reply_target
    if reply_target is not None:
        return "reply", reply_target
    if called:
        return "name", None
    return ("followup", None) if continuing else (None, None)


def note_reply(message) -> None:
    if message.guild is None:
        return
    channel_id = message.channel.id
    _active_chats[channel_id] = (message.author.id, time.monotonic() + _FOLLOWUP_SECONDS)
    _active_chats.move_to_end(channel_id)
    if len(_active_chats) > _ACTIVE_CHANNEL_LIMIT:
        _active_chats.popitem(last=False)
