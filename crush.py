from __future__ import annotations

import sqlite3

import discord
from discord import app_commands

from core import config as core_config, crush_store as store
from utils import helpers as utils_helpers


async def _notify_match(guild: discord.Guild, user_a: discord.abc.User, user_b: discord.abc.User):
    """Best-effort DM to both sides; a closed DM for one does not block the other."""
    embed = utils_helpers.vivian_embed(
        f"Вы оба выбрали друг друга на **{discord.utils.escape_markdown(guild.name)}**. Взаимно!",
        title="💘 Совпадение",
    )
    for member in (user_a, user_b):
        try:
            await member.send(embed=embed)
        except discord.DiscordException as error:
            print(f"[Crush] DM unavailable; user_id={member.id}; error={type(error).__name__}", flush=True)


@core_config.bot.tree.command(name="crush", description="Тайно выбрать краш. Узнаете только при взаимности.")
@app_commands.guild_only()
@app_commands.describe(user="Кого выбрать")
async def crush(interaction: discord.Interaction, user: discord.Member):
    if user.bot or user.id == interaction.user.id:
        return await utils_helpers.vivian_reply(
            interaction, "Нельзя выбрать себя или бота.", error=True, ephemeral=True)
    try:
        result = await store.set_crush(interaction.guild_id, interaction.user.id, user.id)
    except ValueError as error:
        return await utils_helpers.vivian_reply(interaction, str(error), error=True, ephemeral=True)
    except (sqlite3.Error, OSError):
        return await utils_helpers.vivian_reply(
            interaction, "Хранилище временно недоступно. Попробуй позже.", error=True, ephemeral=True)

    if result["already_set"]:
        text = "Ты уже выбрал(а) этого человека. Никто об этом не узнает, пока выбор не станет взаимным."
    elif result["mutual"]:
        text = "Выбор сохранён — и это взаимно! Проверь личные сообщения."
        await _notify_match(interaction.guild, interaction.user, user)
    else:
        text = "Выбор сохранён. Никто не узнает об этом, если только выбор не окажется взаимным."
    await utils_helpers.vivian_reply(interaction, text, ephemeral=True)


@core_config.bot.tree.command(name="crush_remaining", description="Сколько крашей на этой неделе осталось.")
@app_commands.guild_only()
async def crush_remaining(interaction: discord.Interaction):
    try:
        left = await store.remaining_this_week(interaction.guild_id, interaction.user.id)
    except (sqlite3.Error, OSError):
        return await utils_helpers.vivian_reply(
            interaction, "Хранилище временно недоступно. Попробуй позже.", error=True, ephemeral=True)
    await utils_helpers.vivian_reply(
        interaction, f"Осталось крашей на этой неделе: **{left}**.", ephemeral=True)
