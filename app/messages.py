from __future__ import annotations
import asyncio
from datetime import datetime
import discord
import os
import time
from core import config as core_config
from core import ai_models as core_ai_models
from commands import activity as commands_activity
from commands import ai_moderation as commands_ai_moderation
from commands import antispam as commands_antispam
from core import database as core_database
from core import signal as core_signal
from core import state as core_state
from vivian import ambient as vivian_ambient
from vivian import ambient_state as vivian_ambient_state
from vivian import conversation as vivian_conversation
from vivian import memory as vivian_memory
from vivian import memory_card as vivian_memory_card
from vivian import dialogue as vivian_dialogue
from vivian import chat_impact as vivian_chat_impact
from utils import helpers as utils_helpers
from app import message_observers


@core_config.bot.event
async def on_message(message):
    started = time.monotonic()
    if message.author.bot:
        return
    if not utils_helpers._claim_message_once(message.id):
        print(
            f"[Discord] Duplicate message ignored; message_id={message.id}; "
            f"instance={core_config.BOT_INSTANCE_ID}; pid={os.getpid()}",
            flush=True,
        )
        return
    reply_target = utils_helpers._vivian_reply_reference(message)
    directly_addressed = (core_config.bot.user is not None and core_config.bot.user in message.mentions) or reply_target is not None
    if directly_addressed:
        print(
            f"[AI] Message ingress; message_id={message.id}; "
            f"instance={core_config.BOT_INSTANCE_ID}; pid={os.getpid()}; "
            f"received_at={datetime.now(core_config.MOSCOW_TIMEZONE).isoformat(timespec='milliseconds')}",
            flush=True,
        )
    vivian_ambient_state._ambient_note_arrival(message)
    now = time.time()
    moderation_started = time.monotonic()
    if await commands_antispam._moderate_message_spam(message, now):
        return
    spam_ms = (time.monotonic() - moderation_started) * 1000
    rank_stats = None
    daily_stats = None
    activity_date = None
    if message.guild:
        core_signal.update_server_signal(message.guild.id, activity=0.05)
        content = message.content or ""
        rank_stats = core_state.RANK_ACTIVITY[(message.guild.id, message.author.id)]
        rank_stats["messages"] += 1
        rank_stats["word_messages"] += bool(content.strip())
        rank_stats["long_messages"] += len(content) > 100
        rank_stats["characters"] += len(content)
        activity_date = datetime.now(core_config.SUMMARY_TIMEZONE).date()
        daily_stats = core_state.DAILY_ACTIVITY[(activity_date, message.guild.id, message.author.id)]
        daily_stats["messages"] += 1
        daily_stats["profanity"] += len(core_config.PROFANITY_PATTERN.findall(content))
    # XP
    uid = message.author.id
    data = core_state.xp_data[uid]
    if now - core_state.xp_cooldown.get(uid, 0) > core_state.XP_COOLDOWN_SECONDS:
        core_state.xp_cooldown[uid] = now
        data["xp"] += core_state.XP_PER_MESSAGE
        # Level up every 500 xp
        new_level = data["xp"] // 500 + 1
        if new_level > data["level"]:
            data["level"] = new_level
            # можно отправить сообщение о левел-апе
    stats_snapshot = (
        message.guild.id if message.guild else None,
        uid,
        activity_date,
        dict(rank_stats) if rank_stats is not None else None,
        dict(daily_stats) if daily_stats is not None else None,
        dict(data),
    )

    observers, observer_timings = [], {}
    stats_started = False
    try:
        moderation_started = time.monotonic()
        if await commands_ai_moderation._moderate_ai_content(message, now):
            return
        moderation_ms = (time.monotonic() - moderation_started) * 1000
        # Both moderation passes retain priority. Accepted counters are copied
        # above, so newer gateway messages cannot mutate a pending snapshot.
        operations = (
            ("stats", lambda: core_database._persist_stats_snapshot(*stats_snapshot)),
            ("chat_impact", lambda: vivian_chat_impact.observe_approved_message(message)),
        )
        for label, operation in operations:
            if label == "stats":
                stats_started = True
            task = message_observers.start_observer(label, message.id, operation, observer_timings)
            if task is None:
                # Bounded backpressure never discards durable counters/events.
                await message_observers.run_observer(label, message.id, operation, observer_timings)
            else:
                observers.append(task)
        commands_activity._record_chat_pulse(message, now)
        memory_started = time.monotonic()
        try:
            # Privacy filtering and the current turn enter memory before
            # conversation preparation; only unrelated observers overlap it.
            await vivian_memory._remember_chat_message(message, directly_addressed=directly_addressed)
        except Exception as error:
            print(f"[AI Memory] Observer failed; message_id={message.id}; error={error!r}", flush=True)
        memory_ms = (time.monotonic() - memory_started) * 1000
        ambient_started = time.monotonic()
        try:
            await vivian_ambient._ambient_observe(message, directly_addressed=directly_addressed)
        except Exception as error:
            print(f"[Ambient] Observer failed; message_id={message.id}; error={error!r}", flush=True)
        ambient_ms = (time.monotonic() - ambient_started) * 1000

        # Direct mentions and replies to Vivian continue a personal conversation.
        ai_configured = core_ai_models.is_configured(message.guild.id if message.guild else 0, message.author.id)
        if directly_addressed:
            print(
                f"[AI] Mention received; message_id={message.id}; "
                f"instance={core_config.BOT_INSTANCE_ID}; pid={os.getpid()}; "
                f"AI configured={ai_configured}; "
                f"preprocess_ms={(time.monotonic() - started) * 1000:.0f}; "
                f"spam_ms={spam_ms:.0f}; moderation_ms={moderation_ms:.0f}; "
                f"memory_observe_ms={memory_ms:.0f}; ambient_ms={ambient_ms:.0f}",
                flush=True,
            )
        local_reply = (vivian_memory_card.MEMORY_QUESTION_PATTERN.search(message.content or "")
                       or vivian_dialogue.CREATOR_QUESTION_PATTERN.search(message.content or ""))
        if directly_addressed and (ai_configured or local_reply):
            history_key = (message.guild.id if message.guild else 0, message.channel.id, message.author.id)
            lock = core_state.AI_DIALOGUE_LOCKS.setdefault(history_key, asyncio.Lock())
            lock_started = time.monotonic()
            if lock.locked():
                print(f"[AI] Dialogue queued; message_id={message.id}", flush=True)
            async with lock:
                print(
                    f"[AI] Dialogue acquired; message_id={message.id}; "
                    f"lock_wait_ms={(time.monotonic() - lock_started) * 1000:.0f}", flush=True,
                )
                await vivian_conversation._handle_vivian_conversation(message, reply_target)
        elif directly_addressed:
            print("[AI] Selected model provider is not configured; AI replies are disabled.", flush=True)
            try:
                await utils_helpers._reply_once(message, "AI-канал не настроен.")
            except discord.DiscordException:
                pass
        await core_config.bot.process_commands(message)
    finally:
        if not stats_started:
            # Rejected AI-moderation candidates were counted before this
            # refactor too. Preserve that durability without storing chat text.
            await message_observers.run_observer(
                "stats", message.id,
                lambda: core_database._persist_stats_snapshot(*stats_snapshot), observer_timings,
            )
        await message_observers.finish_observers(observers)
        if directly_addressed:
            print(
                f"[AI] Message handled; message_id={message.id}; "
                f"handler_ms={(time.monotonic() - started) * 1000:.0f}; "
                f"stats_ms={observer_timings.get('stats', 0):.0f}; "
                f"chat_impact_ms={observer_timings.get('chat_impact', 0):.0f}", flush=True,
            )
