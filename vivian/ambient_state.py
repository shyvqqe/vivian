from __future__ import annotations
from collections import deque
import asyncio
from datetime import datetime
import discord
import re
import time
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo
from app import chat_routing
from core import config as core_config
from core import database, guild_settings, hub_store
from vivian import dialogue as vivian_dialogue
from vivian.memory_state import AI_MEMORY_SECRET_PATTERN
from utils import helpers as utils_helpers


AMBIENT_CHAT_CHANNEL_ID = core_config.CONVERSATION_CHANNEL_ID


AMBIENT_CONTEXT_TTL = 10 * 60


AMBIENT_REPLY_COOLDOWN = 15 * 60
AMBIENT_ATTEMPT_COOLDOWN = 120
AMBIENT_DAILY_ATTEMPT_LIMIT = 12
AMBIENT_SETTINGS_TTL = 30


AMBIENT_DAILY_REPLY_LIMIT = 3


AMBIENT_QUIET_SECONDS = 3 * 60 * 60


AMBIENT_STARTED_AT = time.time()


AMBIENT_CONTEXT = {}


AMBIENT_STATE = {}
AMBIENT_CONFIG = {}
_CONFIG_LOCKS = WeakValueDictionary()


AMBIENT_SENSITIVE_PATTERN = re.compile(
    r"\b(?:парол\w*|api[ _-]?key|токен\w*|домашний\s+адрес|"
    r"номер\s+(?:телефона|карты)|травл\w*|угрож\w*|суицид\w*|"
    r"самоубий\w*|насили\w*|похорон\w*)\b",
    re.IGNORECASE,
)


AMBIENT_STOP_PATTERN = re.compile(
    r"\b(?:не\s+вмешивайся|не\s+подключайся|без\s+бота|"
    r"(?:vivian|вивиан|бот)[\s,!:]*молчи)\b",
    re.IGNORECASE,
)


AMBIENT_DAILY_QUESTIONS = (
    "Какая маленькая вещь в последнее время неожиданно подняла тебе настроение?",
    "Какой трек сейчас у тебя на повторе? Можно без ссылки, просто название.",
    "Если бы сегодня появился свободный час без дел, на что бы ты его потратил?",
    "Что ты недавно попробовал впервые и захотел повторить?",
    "Есть игра, фильм или книга, к которым хочется вернуться? Что в них цепляет?",
    "Какой привычный предмет у тебя оказался гораздо полезнее, чем ожидалось?",
    "Какой совет ты когда-то пропустил мимо ушей, а потом оценил?",
    "Какое место в твоём городе ты бы показал человеку, который приехал впервые?",
    "Есть маленький навык, которому ты сейчас хотел бы научиться?",
    "Что у тебя обычно спасает вечер, когда день получился так себе?",
    "Какой звук или запах сразу напоминает тебе о хорошем моменте?",
    "Какую обычную вещь люди, по-твоему, зря недооценивают?",
    "Что ты недавно узнал и захотел кому-нибудь рассказать?",
    "Какой случайный разговор тебе запомнился? Без имён, если это личное.",
)


def _ambient_channel_allowed(channel) -> bool:
    if not isinstance(channel, discord.TextChannel):
        return False
    if channel.guild is None:
        return False
    setting = AMBIENT_CONFIG.get(channel.guild.id)
    if setting is None:
        legacy = core_config.bot.get_channel(AMBIENT_CHAT_CHANNEL_ID)
        if (_persistent_storage() or legacy is None or legacy.guild.id != channel.guild.id
                or channel.id != AMBIENT_CHAT_CHANNEL_ID):
            return False
        setting = {"enabled": True, "channel_id": AMBIENT_CHAT_CHANNEL_ID, "legacy": True}
    if not setting.get("enabled") or setting.get("unavailable") or setting.get("channel_id") != channel.id:
        return False
    member = channel.guild.me
    if member is None:
        return False
    permissions = channel.permissions_for(member)
    if not (permissions.view_channel and permissions.send_messages):
        return False
    return bool(permissions.read_message_history and channel.permissions_for(channel.guild.default_role).view_channel)


def _persistent_storage():
    return bool(core_config.DATABASE_URL) or database.db_pool is not None


def _cached_channel(guild, setting):
    channel = guild.get_channel(setting["channel_id"]) if setting["channel_id"] else None
    return channel if channel is not None and _ambient_channel_allowed(channel) else None


async def refresh_configuration(guild, *, force=False):
    lock = _CONFIG_LOCKS.setdefault(guild.id, asyncio.Lock())
    async with lock:
        setting = AMBIENT_CONFIG.get(guild.id)
        if not force and setting and setting.get("checked_at", 0) + AMBIENT_SETTINGS_TTL > time.monotonic():
            return _cached_channel(guild, setting)
        return await _refresh_configuration(guild)


async def _refresh_configuration(guild):
    setting = {"enabled": False, "channel_id": None, "legacy": False,
               "timezone": str(core_config.MOSCOW_TIMEZONE), "persistent": _persistent_storage()}
    previous = AMBIENT_CONFIG.get(guild.id)
    legacy = guild.get_channel(AMBIENT_CHAT_CHANNEL_ID)
    legacy = legacy if isinstance(legacy, discord.TextChannel) and legacy.guild.id == guild.id else None
    if setting["persistent"]:
        try:
            async with asyncio.timeout(3):
                values = await guild_settings.get(guild.id)
            enabled, selected = values.get("ambient_enabled", True), values.get("conversation_channel_id")
            setting["timezone"] = values.get("timezone") or setting["timezone"]
            if enabled is True:
                if type(selected) is int and selected > 0:
                    channel = guild.get_channel(selected)
                    if isinstance(channel, discord.TextChannel) and channel.guild.id == guild.id:
                        setting.update(enabled=True, channel_id=channel.id)
                elif selected is None and legacy is not None:
                    setting.update(enabled=True, channel_id=legacy.id, legacy=True)
        except Exception:
            AMBIENT_CONFIG[guild.id] = {
                **(previous or setting), "unavailable": True,
                "checked_at": time.monotonic() - AMBIENT_SETTINGS_TTL + 5,
            }
            return None
    elif legacy is not None:
        setting.update(enabled=True, channel_id=legacy.id, legacy=True)
    if previous is not None and (previous.get("channel_id"), previous.get("enabled")) != (
        setting["channel_id"], setting["enabled"],
    ):
        AMBIENT_CONTEXT.pop(guild.id, None)
        AMBIENT_STATE.pop(guild.id, None)
    setting["checked_at"] = time.monotonic()
    AMBIENT_CONFIG[guild.id] = setting
    return _cached_channel(guild, setting)


def _ambient_local_time(guild_id, now):
    timezone = AMBIENT_CONFIG.get(guild_id, {}).get("timezone")
    try:
        selected = ZoneInfo(timezone) if isinstance(timezone, str) else core_config.MOSCOW_TIMEZONE
    except (ValueError, KeyError):
        selected = core_config.MOSCOW_TIMEZONE
    return datetime.fromtimestamp(now, selected)


async def _ambient_reserve(guild_id, channel_id, now, day, *, quiet=False):
    setting = AMBIENT_CONFIG.get(guild_id, {})
    if not setting.get("enabled") or setting.get("channel_id") != channel_id:
        return False

    def reserve(value):
        count = int(value.get("count", 0)) if value.get("day") == day else 0
        if (count >= AMBIENT_DAILY_REPLY_LIMIT or now - float(value.get("last_at", 0)) < AMBIENT_REPLY_COOLDOWN
                or (quiet and value.get("quiet_day") == day)):
            return False
        value.update(day=day, count=count + 1, last_at=now)
        if quiet:
            value["quiet_day"] = day
        return True

    if _persistent_storage():
        try:
            return await hub_store.mutate(guild_id, "ambient", "quota", reserve)
        except Exception:
            return False
    if not setting.get("legacy"):
        return False
    value = _ambient_state_for(guild_id).setdefault("delivery_quota", {})
    return reserve(value)


def _ambient_state_for(guild_id: int) -> dict:
    return AMBIENT_STATE.setdefault(guild_id, {
        "last_human": AMBIENT_STARTED_AT,
        "last_attempt": 0.0,
        "last_sent": 0.0,
        "last_direct_id": None,
        "pause_until": 0.0,
        "reply_day": "",
        "reply_attempts": 0,
        "quiet_day": "",
        "history_ready": False,
        "latest_message_id": None,
        "last_stop_message_id": None,
        "recent_arrivals": deque(maxlen=8),
        "latest_human_created_at": 0.0,
        "last_question_id": None,
        "last_question_time": 0.0,
        "unanswered_question": False,
    })


def _ambient_clean_text(content: str) -> str:
    content = re.sub(r"<@!?\d+>|<@&\d+>|<#\d+>", "", content or "")
    return re.sub(r"\s+", " ", content).strip()[:450]


def ambient_stop_requested(content: str, *, directly_addressed=False) -> bool:
    if core_config.bot.user is not None:
        content = re.sub(rf"<@!?{core_config.bot.user.id}>", "Vivian", content)
    text = _ambient_clean_text(content)
    return bool(AMBIENT_STOP_PATTERN.search(text) or (
        directly_addressed and re.fullmatch(r"молчи[.!\s]*", text, re.IGNORECASE)
    ))


def _ambient_note_arrival(message, *, directly_addressed=False) -> None:
    """Record activity before moderation awaits; no unreviewed text enters model context."""
    if message.guild is None or message.author.bot or not _ambient_channel_allowed(message.channel):
        return
    state = _ambient_state_for(message.guild.id)
    arrivals = list(state["recent_arrivals"])
    if not any(item["message_id"] == message.id for item in arrivals):
        arrivals.append({
            "message_id": message.id,
            "author_id": message.author.id,
            "approved": False,
        })
        state["recent_arrivals"] = deque(
            sorted(arrivals, key=lambda item: item["message_id"])[-8:], maxlen=8,
        )
    latest_id = state["latest_message_id"]
    if latest_id is not None and message.id < latest_id:
        return
    now = time.time()
    if latest_id is None or message.id > latest_id:
        state["last_human"] = now
        state["latest_message_id"] = message.id
        state["latest_human_created_at"] = message.created_at.timestamp()
        if state["last_question_id"] is not None and message.id > state["last_question_id"]:
            state["unanswered_question"] = False
    directly_addressed = directly_addressed or bool(
        core_config.bot.user is not None and any(
            getattr(member, "id", None) == core_config.bot.user.id for member in message.mentions
        )
    ) or utils_helpers._vivian_reply_reference(message) is not None or chat_routing.uses_bot_name(message.content)
    if directly_addressed:
        _ambient_note_direct(state, message.id, now)
    stop_requested = ambient_stop_requested(message.content or "", directly_addressed=directly_addressed)
    stop_id = state["last_stop_message_id"]
    if stop_requested and (stop_id is None or message.id > stop_id):
        state["pause_until"] = now + 60 * 60
        state["last_stop_message_id"] = message.id
        AMBIENT_CONTEXT.pop(message.guild.id, None)


def _ambient_note_direct(state: dict, message_id: int, now: float) -> None:
    if state["last_direct_id"] is None or state["last_direct_id"] < message_id:
        state["last_direct_id"] = message_id
        state["last_attempt"] = now


def _ambient_context_for(guild_id: int, now: float) -> list[dict]:
    context = AMBIENT_CONTEXT.setdefault(guild_id, deque(maxlen=18))
    while context and now - context[0]["timestamp"] > AMBIENT_CONTEXT_TTL:
        context.popleft()
    channel_id = AMBIENT_CONFIG.get(guild_id, {}).get("channel_id")
    return [item for item in context if item.get("channel_id") == channel_id]


def _ambient_has_recent_dialogue(state: dict) -> bool:
    arrivals = list(state["recent_arrivals"])[-4:]
    return bool(
        len(arrivals) >= 2
        and all(item["approved"] and not item.get("sensitive") for item in arrivals)
        and len({item["author_id"] for item in arrivals}) >= 2
    )


_DISCUSSION_CUES = re.compile(
    r"\?|\b(?:не соглас[еа]н\w*|с другой стороны|спорн\w*|"
    r"а если|в ч[её]м разница|как считаете|какой вариант|почему|"
    r"disagree|what if|on the other hand|which option|why)\b",
    re.IGNORECASE,
)


def _ambient_claim_reply(guild_id: int, now: float, day: str) -> list[dict] | None:
    state = _ambient_state_for(guild_id)
    if state["reply_day"] != day:
        state["reply_day"] = day
        state["reply_attempts"] = 0
    if (now < state["pause_until"] or now - state["last_attempt"] < AMBIENT_ATTEMPT_COOLDOWN
            or now - state["last_sent"] < AMBIENT_REPLY_COOLDOWN):
        return None
    if state["reply_attempts"] >= AMBIENT_DAILY_ATTEMPT_LIMIT:
        return None
    if not _ambient_has_recent_dialogue(state):
        return None
    context = [item for item in _ambient_context_for(guild_id, now) if now - item["timestamp"] <= 150][-10:]
    speakers = len({item["author_id"] for item in context})
    if len(context) < 4 or speakers < 2:
        return None
    if sum(len(item["text"]) for item in context) < 180:
        return None
    if any(AMBIENT_SENSITIVE_PATTERN.search(item["text"]) or AI_MEMORY_SECRET_PATTERN.search(item["text"])
           for item in context):
        return None
    if speakers < 3 and len(context) < 6 and not any(
        _DISCUSSION_CUES.search(item["text"]) for item in context[-4:]
    ):
        return None
    # Claim before network I/O so simultaneous messages cannot start two replies.
    state["last_attempt"] = now
    state["reply_attempts"] += 1
    return context


def _ambient_finish_reply(raw: str | None, finish_reason: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if raw.lower().strip("`\"'[] ") == "silent" or raw.upper().startswith("SILENT"):
        return None
    if "<think>" in raw and "</think>" not in raw:
        return None
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    if re.search(r"https?://|www\.|discord\.gg/", raw, re.IGNORECASE):
        return None
    raw = re.sub(r"<@!?\d+>|<@&\d+>|@everyone|@here", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" `")
    if not raw:
        return None
    sentences = re.findall(r".*?[.!?…](?=\s|$)|.+$", raw)
    if finish_reason == "length" and sentences and not re.search(r"[.!?…][\"»)]?$", sentences[-1]):
        sentences.pop()
    result = " ".join(sentence.strip() for sentence in sentences[:2]).strip()
    if len(result) > 400:
        complete = list(re.finditer(r"[.!?…](?=\s|$)", result[:400]))
        if not complete:
            return None
        result = result[:complete[-1].end()].strip()
    return vivian_dialogue._use_vivian_feminine_self_reference(result) if result else None
