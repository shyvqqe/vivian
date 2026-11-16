from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
import re
import time

import discord

from core import config
from utils import helpers
from vivian import followups


_NAME = r"(?:vivian|вивиан|бот)"
_DIRECT_REQUEST = (
    r"(?:как\s+(?:ты|думаешь|считаешь)|(?:а\s+)?ты\b|что\s+(?:думаешь|скажешь|посоветуешь)|"
    r"объясни|подскажи|расскажи|поясни|ответь|скажи|помоги|можешь|проверь|сравни|сделай|покажи|"
    r"найди|почему|зачем|что|как|можно|can\s+you|could\s+you|what|why|how|help|explain)\b"
)
_NAME_CALL = re.compile(
    rf"^\s*(?:(?:а|привет|хай|эй|слушай|здравствуй|доброе утро|добрый день|добрый вечер|"
    rf"hello|hey|hi)[\s,.!—-]+)?{_NAME}(?:\s*$|\s*[,!:?…]|\.(?:\s|$)|[\s—–]+{_DIRECT_REQUEST})"
    rf"|,\s*{_NAME}(?:[.!?…\s]*$|\s*[,—–]|\s+{_DIRECT_REQUEST})"
    rf"|^\s*(?:вопрос|просьба)\s+к\s+(?:боту|ии|vivian|вивиан)\s*[:—–]",
    re.IGNORECASE,
)
_CONTINUATION = re.compile(
    rf"^\s*(?:(?:а|и|тогда|ок|ладно|ну)[\s,]+)*{_DIRECT_REQUEST}"
    r"|^\s*(?:то есть|имею в виду|в смысле|как так|можно подробнее|а если|"
    r"не совсем понял[аи]?|не понял[аи]?|у меня (?:так|не) получилось|"
    r"у меня (?:всё\s+ещ[её]|тоже) не работает)\b",
    re.IGNORECASE,
)
_FOLLOWUP_SECONDS = 90
_FOLLOWUP_INTERVAL = 8
_ACTIVE_CHANNEL_LIMIT = 512
_GLOBAL_TURN_LIMIT = 16
_CHANNEL_TURN_LIMIT = 3
_USER_TURN_LIMIT = 2
_active_chats: OrderedDict[int, tuple[int, int, float]] = OrderedDict()
_implicit_attempts: OrderedDict[int, float] = OrderedDict()
_pending_users: Counter[tuple[int, int]] = Counter()
_pending_channels: Counter[int] = Counter()
_pending_total = 0


def uses_bot_name(content: str) -> bool:
    return bool(_NAME_CALL.search(content or ""))


def _is_followup(content: str) -> bool:
    text = followups.normalize_short_followup(content.strip())
    return bool(_CONTINUATION.search(text))


def _bot_permissions(message):
    if message.guild is None or message.guild.me is None:
        return None
    return message.channel.permissions_for(message.guild.me)


async def _reply_target(message, permissions):
    reference = message.reference
    if message.type != discord.MessageType.reply or reference is None or reference.channel_id != message.channel.id:
        return None
    if permissions is not None and not permissions.read_message_history:
        return None
    resolved = helpers._vivian_reply_reference(message)
    if resolved is not None:
        return resolved
    if (reference.resolved is not None or reference.cached_message is not None
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
        return "dm", await _reply_target(message, None)
    if content.startswith(prefixes):
        return None, None

    bot_id = getattr(config.bot.user, "id", None)
    mentioned = bot_id is not None and any(user.id == bot_id for user in message.mentions)
    called = uses_bot_name(content)
    try:
        permissions = _bot_permissions(message)
    except discord.ClientException:
        return None, None
    if permissions is not None:
        can_send = (permissions.send_messages_in_threads if isinstance(message.channel, discord.Thread)
                    else permissions.send_messages)
        if not (permissions.view_channel and can_send):
            if mentioned or called:
                print(f"[AI] Cannot send in channel {message.channel.id}; check Discord permissions.", flush=True)
            return None, None

    reply_target = await _reply_target(message, permissions)
    if mentioned:
        return "mention", reply_target
    if reply_target is not None:
        return "reply", reply_target
    if called:
        return "name", None
    channel_id = message.channel.id
    active = _active_chats.get(channel_id)
    now = time.monotonic()
    if active and active[2] <= now:
        del _active_chats[channel_id]
        active = None
    continuing = (
        active is not None and active[0] == message.author.id and active[1] < message.id
        and not message.mentions and not message.role_mentions and not message.mention_everyone
        and message.reference is None and _is_followup(content)
    )
    return ("followup", None) if continuing else (None, None)


def reserve_turn(message, trigger: str) -> bool:
    """Limit queued model work before any await; explicit calls can queue one turn."""
    global _pending_total
    channel_id = message.channel.id
    key = channel_id, message.author.id
    if (_pending_total >= _GLOBAL_TURN_LIMIT or _pending_channels[channel_id] >= _CHANNEL_TURN_LIMIT
            or _pending_users[key] >= _USER_TURN_LIMIT):
        return False
    if trigger == "followup":
        now = time.monotonic()
        previous = _implicit_attempts.get(channel_id)
        if _pending_channels[channel_id] or (previous is not None and now - previous < _FOLLOWUP_INTERVAL):
            return False
        _implicit_attempts[channel_id] = now
        _implicit_attempts.move_to_end(channel_id)
        if len(_implicit_attempts) > _ACTIVE_CHANNEL_LIMIT:
            _implicit_attempts.popitem(last=False)
    _pending_users[key] += 1
    _pending_channels[channel_id] += 1
    _pending_total += 1
    return True


def release_turn(message) -> None:
    global _pending_total
    channel_id = message.channel.id
    key = channel_id, message.author.id
    _pending_users[key] -= 1
    _pending_channels[channel_id] -= 1
    _pending_total -= 1
    if not _pending_users[key]:
        del _pending_users[key]
    if not _pending_channels[channel_id]:
        del _pending_channels[channel_id]


def channel_is_busy(channel_id: int) -> bool:
    return bool(_pending_channels[channel_id])


def note_reply(message) -> None:
    if message.guild is None:
        return
    channel_id = message.channel.id
    previous = _active_chats.get(channel_id)
    if previous and previous[1] > message.id:
        return
    _active_chats[channel_id] = (message.author.id, message.id, time.monotonic() + _FOLLOWUP_SECONDS)
    _active_chats.move_to_end(channel_id)
    if len(_active_chats) > _ACTIVE_CHANNEL_LIMIT:
        _active_chats.popitem(last=False)
