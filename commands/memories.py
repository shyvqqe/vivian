from __future__ import annotations

import sqlite3
from datetime import datetime, time as datetime_time

import discord
from discord.ext import tasks
from discord import app_commands

from core import config as core_config, memories_store as store

MEMORIES_CHANNEL_ID = core_config.CHAT_CHANNEL_ID


def _today_parts(guild_id: int | None = None):
    now = datetime.now(core_config.SUMMARY_TIMEZONE)
    return now.strftime("%Y-%m-%d"), now.strftime("%m-%d")


async def _post_memory(guild: discord.Guild, channel: discord.abc.Messageable) -> bool:
    today_iso, month_day = _today_parts()
    try:
        best = await store.best_for_month_day(guild.id, month_day, today_iso)
    except (sqlite3.Error, OSError):
        return False
    if best is None:
        return False
    year = best["message_date"][:4]
    link = f"https://discord.com/channels/{guild.id}/{best['channel_id']}/{best['message_id']}"
    embed = discord.Embed(
        title=f"В этот день, {year} год",
        description=discord.utils.escape_markdown(best["content"])[:1000],
        color=core_config.VIVIAN_ACCENT,
    )
    embed.add_field(name="АВТОР", value=f"<@{best['author_id']}>", inline=True)
    embed.add_field(name="РЕАКЦИЙ БЫЛО", value=str(best["reaction_count"]), inline=True)
    embed.add_field(name="ОРИГИНАЛ", value=f"[Перейти к сообщению]({link})", inline=False)
    embed.set_footer(text=core_config.VIVIAN_FOOTER)
    try:
        await channel.send(embed=embed)
        return True
    except discord.DiscordException:
        return False


@tasks.loop(time=datetime_time(hour=12, minute=0, tzinfo=core_config.SUMMARY_TIMEZONE))
async def memories_loop():
    for guild in core_config.bot.guilds:
        channel = guild.get_channel(MEMORIES_CHANNEL_ID)
        if channel is None:
            continue
        try:
            await _post_memory(guild, channel)
        except Exception as error:
            print(f"[Memories] Failed for guild {guild.id}: {type(error).__name__}", flush=True)


@memories_loop.before_loop
async def before_memories_loop():
    await core_config.bot.wait_until_ready()


def start():
    if not memories_loop.is_running():
        memories_loop.start()


@core_config.bot.listen("on_message")
async def memories_on_message(message: discord.Message):
    if message.guild is None or message.author.bot:
        return
    message_date = message.created_at.astimezone(core_config.SUMMARY_TIMEZONE).strftime("%Y-%m-%d")
    try:
        await store.record_message(
            message.guild.id, message.channel.id, message.id, message.author.id,
            message.content or "", message_date,
        )
    except (sqlite3.Error, OSError):
        pass


@core_config.bot.listen("on_raw_reaction_add")
async def memories_on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    try:
        await store.bump_reactions(payload.guild_id, payload.message_id, 1)
    except (sqlite3.Error, OSError):
        pass


@core_config.bot.listen("on_raw_reaction_remove")
async def memories_on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    try:
        await store.bump_reactions(payload.guild_id, payload.message_id, -1)
    except (sqlite3.Error, OSError):
        pass


@core_config.bot.tree.command(name="memories_now", description="Опубликовать воспоминание дня прямо сейчас.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def memories_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    channel = interaction.guild.get_channel(MEMORIES_CHANNEL_ID) or interaction.channel
    posted = await _post_memory(interaction.guild, channel)
    message = "Воспоминание опубликовано." if posted else "За эту дату в прошлые годы ничего не нашлось."
    await interaction.edit_original_response(content=message)
