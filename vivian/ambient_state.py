from __future__ import annotations
from collections import deque
import discord
import re
import time
from core import config as core_config
from vivian import dialogue as vivian_dialogue
from utils import helpers as utils_helpers


AMBIENT_CHAT_CHANNEL_ID = core_config.CHAT_CHANNEL_ID


AMBIENT_CONTEXT_TTL = 10 * 60


AMBIENT_REPLY_COOLDOWN = 15 * 60


AMBIENT_DAILY_REPLY_LIMIT = 3


AMBIENT_QUIET_SECONDS = 3 * 60 * 60


AMBIENT_STARTED_AT = time.time()


AMBIENT_CONTEXT = {}


AMBIENT_STATE = {}


AMBIENT_SENSITIVE_PATTERN = re.compile(
    r"\b(?:парол\w*|api[ _-]?key|токен\w*|домашний\s+адрес|"
    r"номер\s+(?:телефона|карты)|травл\w*|угрож\w*|суицид\w*|"
    r"самоубий\w*|насили\w*|похорон\w*)\b",
    re.IGNORECASE,
)


AMBIENT_STOP_PATTERN = re.compile(
    r"\b(?:не\s+вмешивайся|не\s+подключайся|без\s+бота|"
    r"(?:vivian|вивиан)[\s,!:]*молчи)\b",
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
    if channel.id != AMBIENT_CHAT_CHANNEL_ID or channel.guild is None:
        return False
    member = channel.guild.me
    if member is None:
        return False
    permissions = channel.permissions_for(member)
    return bool(permissions.view_channel and permissions.send_messages)


def _ambient_state_for(guild_id: int) -> dict:
    return AMBIENT_STATE.setdefault(guild_id, {
        "last_human": AMBIENT_STARTED_AT,
        "last_attempt": AMBIENT_STARTED_AT,
        "pause_until": 0.0,
        "reply_day": "",
        "reply_attempts": 0,
        "quiet_day": "",
        "history_ready": False,
        "latest_message_id": None,
        "last_stop_message_id": None,
        "recent_arrivals": deque(maxlen=5),
        "latest_human_created_at": 0.0,
        "last_question_id": None,
        "last_question_time": 0.0,
        "unanswered_question": False,
    })


def _ambient_clean_text(content: str) -> str:
    content = re.sub(r"<@!?\d+>|<@&\d+>|<#\d+>", "", content or "")
    return re.sub(r"\s+", " ", content).strip()[:450]


def _ambient_note_arrival(message) -> None:
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
            sorted(arrivals, key=lambda item: item["message_id"])[-5:], maxlen=5,
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
    directly_addressed = bool(
        core_config.bot.user is not None and any(
            getattr(member, "id", None) == core_config.bot.user.id for member in message.mentions
        )
    ) or utils_helpers._vivian_reply_reference(message) is not None
    if directly_addressed:
        state["last_attempt"] = now
    raw = message.content or ""
    if core_config.bot.user is not None:
        raw = re.sub(rf"<@!?{core_config.bot.user.id}>", "Vivian", raw)
    text = _ambient_clean_text(raw)
    stop_requested = bool(AMBIENT_STOP_PATTERN.search(text)) or bool(
        directly_addressed and re.fullmatch(r"молчи[.!\s]*", text, re.IGNORECASE)
    )
    stop_id = state["last_stop_message_id"]
    if stop_requested and (stop_id is None or message.id > stop_id):
        state["pause_until"] = now + 60 * 60
        state["last_stop_message_id"] = message.id
        AMBIENT_CONTEXT.pop(message.guild.id, None)


def _ambient_context_for(guild_id: int, now: float) -> list[dict]:
    context = AMBIENT_CONTEXT.setdefault(guild_id, deque(maxlen=18))
    while context and now - context[0]["timestamp"] > AMBIENT_CONTEXT_TTL:
        context.popleft()
    return list(context)


def _ambient_has_recent_dialogue(state: dict) -> bool:
    """The five newest human actions count, including short texts and attachments."""
    arrivals = state["recent_arrivals"]
    return bool(
        len(arrivals) >= 2
        and all(item["approved"] for item in arrivals)
        and len({item["author_id"] for item in arrivals}) >= 2
    )


def _ambient_claim_reply(guild_id: int, now: float, day: str) -> list[dict] | None:
    state = _ambient_state_for(guild_id)
    if state["reply_day"] != day:
        state["reply_day"] = day
        state["reply_attempts"] = 0
    if now < state["pause_until"] or now - state["last_attempt"] < AMBIENT_REPLY_COOLDOWN:
        return None
    if state["reply_attempts"] >= AMBIENT_DAILY_REPLY_LIMIT:
        return None
    if not _ambient_has_recent_dialogue(state):
        return None
    context = _ambient_context_for(guild_id, now)
    if len(context) < 4 or len({item["author_id"] for item in context}) < 2:
        return None
    if sum(len(item["text"]) for item in context) < 180:
        return None
    if any(AMBIENT_SENSITIVE_PATTERN.search(item["text"]) for item in context):
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
