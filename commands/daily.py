from __future__ import annotations
from datetime import datetime
from datetime import time as datetime_time
import discord
import hashlib
from discord.ext import tasks
from datetime import timedelta
from core import config as core_config
from core import state as core_state
from utils import helpers as utils_helpers
from utils import announcements


def _daily_summary_text(guild: discord.Guild, report_date):
    records = [
        (user_id, stats)
        for (day, guild_id, user_id), stats in core_state.DAILY_ACTIVITY.items()
        if day == report_date and guild_id == guild.id and stats["messages"] > 0
    ]
    if not records:
        return None

    total_messages = sum(stats["messages"] for _, stats in records)
    most_active_id, most_active = max(records, key=lambda item: item[1]["messages"])
    most_active_count = most_active["messages"]
    lines = [
        f"◆ **Vivian · Срез дня**  `{report_date:%d.%m}`",
        "",
        f"**Пик активности** — <@{most_active_id}> · "
        f"{most_active_count} {utils_helpers._ru_plural(most_active_count, 'сообщение', 'сообщения', 'сообщений')}",
    ]

    if len(records) > 1:
        quiet_id, quiet = min(records, key=lambda item: item[1]["messages"])
        quiet_count = quiet["messages"]
        lines.append(
            f"**Тихий сигнал** — <@{quiet_id}> · "
            f"{quiet_count} {utils_helpers._ru_plural(quiet_count, 'сообщение', 'сообщения', 'сообщений')}"
        )

    most_profanity_id, most_profanity = max(records, key=lambda item: item[1]["profanity"])
    if most_profanity["profanity"]:
        profanity_count = most_profanity["profanity"]
        lines.append(
            f"**Резкий словарь** — <@{most_profanity_id}> · "
            f"{profanity_count} {utils_helpers._ru_plural(profanity_count, 'слово', 'слова', 'слов')}"
        )

    lines.extend((
        "",
        f"*{total_messages} {utils_helpers._ru_plural(total_messages, 'сообщение', 'сообщения', 'сообщений')} · "
        f"{len(records)} {utils_helpers._ru_plural(len(records), 'участник', 'участника', 'участников')}*",
    ))
    return "\n".join(lines)


@tasks.loop(time=datetime_time(hour=0, minute=5, tzinfo=core_config.SUMMARY_TIMEZONE))
async def daily_summary_loop():
    report_date = datetime.now(core_config.SUMMARY_TIMEZONE).date() - timedelta(days=1)
    for guild in core_config.bot.guilds:
        summary_key = (guild.id, report_date)
        if summary_key in core_state.DAILY_SUMMARY_SENT:
            continue
        channel = guild.get_channel(core_config.DAILY_SUMMARY_CHANNEL_ID)
        summary = _daily_summary_text(guild, report_date)
        if not summary:
            core_state.DAILY_SUMMARY_SENT.add(summary_key)
            continue
        if channel is None:
            print(
                f"[Daily summary] Channel {core_config.DAILY_SUMMARY_CHANNEL_ID} not found "
                f"for guild {guild.id}",
                flush=True,
            )
            continue
        nonce_source = f"veyr-daily:{guild.id}:{report_date.isoformat()}".encode()
        nonce = hashlib.blake2b(nonce_source, digest_size=12).hexdigest()
        try:
            await announcements.send_once(
                channel,
                nonce=nonce,
                embed=utils_helpers.vivian_embed(summary, title="◆ Vivian // ИТОГИ ДНЯ"),
            )
            core_state.DAILY_SUMMARY_SENT.add(summary_key)
            for key in [key for key in core_state.DAILY_ACTIVITY if key[1] == guild.id and key[0] <= report_date]:
                core_state.DAILY_ACTIVITY.pop(key, None)
        except discord.HTTPException as error:
            print(f"[Daily summary] Failed for guild {guild.id}: {error!r}", flush=True)


@daily_summary_loop.before_loop
async def before_daily_summary_loop():
    await core_config.bot.wait_until_ready()
