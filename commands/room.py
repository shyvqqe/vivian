"""Slash entry points; the room workflow itself is entirely Components V2."""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands

from app import rooms, room_panels
from commands import room_ui, room_views
from core import community_store, config, rooms_store


room = app_commands.Group(name="room", description="Приватные комнаты Vivian · создание и управление", guild_only=True)


async def _notice(interaction, text):
    view = room_ui.Panel(interaction.guild_id, interaction.user.id).card("Настройка комнат", text[:2200])
    return await room_ui.show(interaction, view)


def _staff(member):
    return bool(member and (getattr(getattr(member, "guild_permissions", None), "manage_guild", False)
                or any(role.id == config.STAFF_ROLE_ID for role in getattr(member, "roles", ()))))


async def _current_staff(interaction):
    if interaction.guild is None:
        raise ValueError("Настройка комнат доступна на сервере.")
    async with asyncio.timeout(6):
        member = await interaction.guild.fetch_member(interaction.user.id)
    if not _staff(member):
        raise ValueError("Настраивать общую панель может администрация: «Управлять сервером» или роль staff.")
    return member


@room.command(name="create", description="Выбрать участников по @ и создать приватную голосовую комнату.")
async def create(interaction: discord.Interaction):
    await room_views.open_create(interaction)


@room.command(name="manage", description="Открыть личную панель управления своей приватной комнатой.")
async def manage(interaction: discord.Interaction):
    await room_views.open_controls(interaction)


@room.command(name="claim", description="Забрать управление комнатой, если владелец вышел из неё.")
async def claim(interaction: discord.Interaction):
    await room_views.open_claim(interaction)


@room.command(name="setup", description="Разместить общую панель приватных комнат · для администрации.")
@app_commands.describe(channel="Текстовый канал панели; по умолчанию текущий",
                       category="Категория комнат; если не выбрана, Vivian создаст отдельную")
async def setup(interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                category: discord.CategoryChannel | None = None):
    if not _staff(interaction.user) or interaction.guild is None:
        return await _notice(interaction, "Общую панель настраивает администрация сервера.")
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except discord.DiscordException:
        return
    saved = False
    try:
        await _current_staff(interaction)
        if not await rooms_store.initialize():
            raise ValueError("Хранилище комнат сейчас недоступно. Настройка не сохранена.")
        guild = interaction.guild
        target = channel or interaction.channel
        room_panels.check_channel(target, guild)
        if category is not None:
            if category.guild.id != guild.id:
                raise ValueError("Категория должна находиться на этом сервере.")
            permissions = category.permissions_for(guild.me) if guild.me else None
            if permissions is None or not (permissions.view_channel and permissions.manage_channels and permissions.manage_roles):
                raise ValueError("В категории Vivian нужны права видеть каналы, управлять каналами и разрешениями.")
        async with rooms.lock_for(guild.id):
            value = await rooms.settings(guild.id)
            await _current_staff(interaction)
            value = {**value, "enabled": True}
            if category is not None:
                value["category_id"] = category.id
            await community_store.set_setting(guild.id, "rooms_config", value)
            saved = True
            value, selected_category = await rooms.ensure_settings(guild)
            await _current_staff(interaction)
            message = await room_panels.publish(guild, target, value)
        await _notice(interaction,
            f"Панель готова: [открыть]({message.jump_url})\n"
            f"Категория: **{discord.utils.escape_mentions(discord.utils.escape_markdown(selected_category.name))}**\n\n"
            "Можно нажать «Создать комнату» или вызвать `/room create`, выбрать участников по `@` и подтвердить.")
    except (ValueError, community_store.CommunityUnavailable) as error:
        suffix = "\nНастройки категории сохранены; публикация панели ещё не подтверждена." if saved else ""
        await _notice(interaction, discord.utils.escape_mentions(str(error)) + suffix)
    except Exception as error:
        print(f"[Rooms] Setup unavailable; error={type(error).__name__}", flush=True)
        text = ("Настройки сохранены, но публикацию панели завершить не удалось. Повтори `/room setup`."
                if saved else "Настроить панель сейчас не получилось. Проверь права Vivian и попробуй позже.")
        await _notice(interaction, text)


config.bot.tree.add_command(room)
