from __future__ import annotations

import sqlite3
import time
from datetime import datetime, time as datetime_time

import discord
from discord.ext import tasks
from discord import app_commands

from core import birthday_store as store, config as core_config
from utils import helpers as utils_helpers

ROLE_NAME = "🎂 Именинник"
ROLE_DURATION_SECONDS = 24 * 60 * 60
MONTH_NAMES = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
               "июля", "августа", "сентября", "октября", "ноября", "декабря"]


async def _get_or_create_role(guild: discord.Guild) -> discord.Role | None:
    role = discord.utils.get(guild.roles, name=ROLE_NAME)
    if role is not None:
        return role
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return None
    try:
        return await guild.create_role(
            name=ROLE_NAME, color=core_config.VIVIAN_ACCENT, reason="Автосоздание роли именинника Vivian",
        )
    except discord.DiscordException:
        return None


@tasks.loop(time=datetime_time(hour=9, minute=0, tzinfo=core_config.SUMMARY_TIMEZONE))
async def birthday_congratulate_loop():
    now = datetime.now(core_config.SUMMARY_TIMEZONE)
    for guild in core_config.bot.guilds:
        try:
            user_ids = await store.for_month_day(guild.id, now.month, now.day)
        except (sqlite3.Error, OSError):
            continue
        if not user_ids:
            continue
        channel = guild.get_channel(core_config.CHAT_CHANNEL_ID)
        role = await _get_or_create_role(guild)
        mentions = []
        for user_id in user_ids:
            member = guild.get_member(user_id)
            if member is None:
                continue
            mentions.append(member.mention)
            if role is not None:
                try:
                    await member.add_roles(role, reason="День рождения")
                    await store.set_active_role(guild.id, user_id, role.id, time.time() + ROLE_DURATION_SECONDS)
                except discord.DiscordException:
                    pass
        if mentions and channel is not None:
            embed = utils_helpers.vivian_embed(
                " ".join(mentions) + "\nПоздравляем с днём рождения! 🎉", title="Сегодня день рождения",
            )
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.DiscordException:
                pass


@tasks.loop(minutes=15)
async def birthday_role_cleanup_loop():
    try:
        due = await store.due_active_roles(time.time())
    except (sqlite3.Error, OSError):
        return
    for item in due:
        guild = core_config.bot.get_guild(item["guild_id"])
        if guild is not None:
            member = guild.get_member(item["user_id"])
            role = guild.get_role(item["role_id"])
            if member is not None and role is not None:
                try:
                    await member.remove_roles(role, reason="Срок роли именинника истёк")
                except discord.DiscordException:
                    pass
        await store.clear_active_role(item["guild_id"], item["user_id"])


@birthday_congratulate_loop.before_loop
async def before_birthday_congratulate():
    await core_config.bot.wait_until_ready()


@birthday_role_cleanup_loop.before_loop
async def before_birthday_cleanup():
    await core_config.bot.wait_until_ready()


def start():
    if not birthday_congratulate_loop.is_running():
        birthday_congratulate_loop.start()
    if not birthday_role_cleanup_loop.is_running():
        birthday_role_cleanup_loop.start()


@core_config.bot.tree.command(name="birthday_set", description="Сохранить дату дня рождения.")
@app_commands.guild_only()
@app_commands.describe(day="День (1-31)", month="Месяц (1-12)", year="Год рождения (необязательно)")
async def birthday_set(interaction: discord.Interaction, day: app_commands.Range[int, 1, 31],
                       month: app_commands.Range[int, 1, 12], year: app_commands.Range[int, 1900, 2100] = None):
    try:
        await store.set_birthday(interaction.guild_id, interaction.user.id, day, month, year)
    except (ValueError, sqlite3.Error, OSError) as error:
        return await utils_helpers.vivian_reply(interaction, str(error), error=True, ephemeral=True)
    await utils_helpers.vivian_reply(
        interaction, f"День рождения сохранён: {day} {MONTH_NAMES[month]}.", ephemeral=True)


@core_config.bot.tree.command(name="birthday_list", description="Ближайшие дни рождения на сервере.")
@app_commands.guild_only()
async def birthday_list(interaction: discord.Interaction):
    try:
        rows = await store.upcoming(interaction.guild_id)
    except (sqlite3.Error, OSError):
        return await utils_helpers.vivian_reply(
            interaction, "Хранилище временно недоступно. Попробуй позже.", error=True, ephemeral=True)
    if not rows:
        return await utils_helpers.vivian_reply(interaction, "Пока никто не указал день рождения.", ephemeral=True)
    lines = [f"<@{row['user_id']}> — {row['day']} {MONTH_NAMES[row['month']]}" for row in rows]
    await utils_helpers.vivian_reply(interaction, "\n".join(lines), title="Ближайшие дни рождения", ephemeral=True)
