from __future__ import annotations
from datetime import datetime
import discord
import hashlib
import re
import time
from core import config as core_config
from core import discord_api as core_discord_api
from vivian import ambient_state as vivian_ambient_state
from vivian import memory as vivian_memory
from utils import helpers as utils_helpers


def _ambient_question_marker(day) -> str:
    return f"Вопрос дня · {day:%d.%m.%Y}"


def _ambient_is_question(content: str | None) -> bool:
    content = content or ""
    return "?" in content or bool(re.search(r"Вопрос дня · \d{2}\.\d{2}\.\d{4}", content))


def _ambient_note_question(guild_id: int, content: str, sent) -> None:
    """Track delivered questions without storing their text in the silence guard."""
    if not _ambient_is_question(content):
        return
    state = vivian_ambient_state._ambient_state_for(guild_id)
    raw_id = sent.get("id") if isinstance(sent, dict) else getattr(sent, "id", None)
    question_id = int(raw_id) if raw_id is not None else None
    state["last_question_id"] = question_id
    state["last_question_time"] = (
        discord.utils.snowflake_time(question_id).timestamp() if question_id is not None else time.time()
    )
    state["unanswered_question"] = bool(
        question_id is None or state["latest_message_id"] is None
        or state["latest_message_id"] < question_id
    )


async def _ambient_read_channel_history(channel, now: float, today) -> None:
    if core_discord_api.is_paused():
        return
    state = vivian_ambient_state._ambient_state_for(channel.guild.id)
    member = channel.guild.me
    if member is None or not channel.permissions_for(member).read_message_history:
        state["history_ready"] = True
        state["quiet_day"] = today.isoformat()
        return
    try:
        recent = [item async for item in channel.history(limit=200)]
    except discord.DiscordException as error:
        if core_discord_api.is_rate_limit_error(error):
            return
        print(f"[Ambient AI] History unavailable; channel={channel.id}; error={error!r}", flush=True)
        state["history_ready"] = True
        state["quiet_day"] = today.isoformat()
        return
    newest_human = next((item for item in recent if not item.author.bot), None)
    if newest_human is not None:
        state["last_human"] = max(
            newest_human.created_at.timestamp(),
            state["last_human"] if state["latest_message_id"] is not None else 0.0,
        )
        state["latest_human_created_at"] = max(
            state["latest_human_created_at"], newest_human.created_at.timestamp(),
        )
    latest_question = next((
        item for item in recent
        if core_config.bot.user and item.author.id == core_config.bot.user.id
        and _ambient_is_question(utils_helpers._vivian_reply_text(item))
    ), None)
    if latest_question is not None:
        question_time = latest_question.created_at.timestamp()
        if question_time >= state["last_question_time"]:
            state["last_question_time"] = question_time
            state["last_question_id"] = getattr(latest_question, "id", None)
    if state["last_question_time"]:
        state["unanswered_question"] = state["latest_human_created_at"] <= state["last_question_time"]
    elif len(recent) == 200 and newest_human is None:
        # Bot-only history can hide an older unanswered question beyond the page.
        state["unanswered_question"] = True
    else:
        state["unanswered_question"] = False
    marker = _ambient_question_marker(today)
    if any(core_config.bot.user and item.author.id == core_config.bot.user.id
           and marker in utils_helpers._vivian_reply_text(item) for item in recent):
        state["quiet_day"] = today.isoformat()
    elif len(recent) == 200 and recent[-1].created_at.astimezone(core_config.MOSCOW_TIMEZONE).date() == today:
        # The day's history was truncated: suppress instead of risking a duplicate after restart.
        state["quiet_day"] = today.isoformat()
    state["history_ready"] = True


def _ambient_quiet_eligible(state: dict, now: float, moscow_now: datetime) -> bool:
    return bool(
        state["history_ready"]
        and not state["unanswered_question"]
        and 12 <= moscow_now.hour < 21
        and now - state["last_human"] >= vivian_ambient_state.AMBIENT_QUIET_SECONDS
        and now >= state["pause_until"]
        and now - state["last_attempt"] >= vivian_ambient_state.AMBIENT_REPLY_COOLDOWN
        and state["quiet_day"] != moscow_now.date().isoformat()
    )


async def _ambient_check_quiet_channel(channel, now: float, moscow_now: datetime) -> None:
    if core_discord_api.is_paused() or not vivian_ambient_state._ambient_channel_allowed(channel):
        return
    state = vivian_ambient_state._ambient_state_for(channel.guild.id)
    today = moscow_now.date()
    if not state["history_ready"]:
        await _ambient_read_channel_history(channel, now, today)
    if core_discord_api.is_paused():
        return
    if not _ambient_quiet_eligible(state, now, moscow_now):
        return
    # Verify today's marker again before sending, including after a day rollover.
    await _ambient_read_channel_history(channel, now, today)
    if core_discord_api.is_paused():
        return
    if not _ambient_quiet_eligible(state, now, moscow_now):
        return
    state["quiet_day"] = today.isoformat()
    state["last_attempt"] = now
    question_index = (today.toordinal() + channel.guild.id) % len(vivian_ambient_state.AMBIENT_DAILY_QUESTIONS)
    content = vivian_ambient_state.AMBIENT_DAILY_QUESTIONS[question_index] + "\n\n" + _ambient_question_marker(today)
    nonce = hashlib.blake2b(
        f"vivian-quiet:{channel.guild.id}:{today.isoformat()}".encode(), digest_size=12,
    ).hexdigest()
    try:
        if not core_discord_api.is_paused() and vivian_ambient_state._ambient_channel_allowed(channel):
            sent = await utils_helpers._send_message_once(channel, content, nonce=nonce)
            _ambient_note_question(channel.guild.id, content, sent)
            await vivian_memory._remember_vivian_message(channel, sent, content)
    except discord.DiscordException as error:
        if core_discord_api.is_rate_limit_error(error):
            return
        print(f"[Ambient AI] Daily question skipped; channel={channel.id}; error={error!r}", flush=True)
