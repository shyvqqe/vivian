"""Room owner actions. Each callback validates ownership again before changing Discord."""
from __future__ import annotations

import discord

from app import rooms
from core import rooms_store as store


def _target(guild, user):
    member = guild.get_member(user.id)
    if member is None or member.bot:
        raise ValueError("Выбери участника этого сервера, кроме ботов.")
    return member


def _manage(channel, *, move=False, overwrite=False):
    me = channel.guild.me
    rights = channel.permissions_for(me) if me else None
    if rights is None or not (rights.view_channel and rights.manage_channels):
        raise ValueError("Vivian нужны права видеть комнату и управлять каналами.")
    if move and not rights.move_members:
        raise ValueError("Vivian нужно право «Перемещать участников» в этой комнате.")
    if overwrite and not rights.manage_roles:
        raise ValueError("Vivian нужно право «Управлять ролями», чтобы менять доступ к комнате.")


async def rename(interaction, name):
    member = rooms.member_for(interaction)
    label = rooms.room_name(name, member)
    async with rooms.lock_for(member.guild.id):
        row, channel = await rooms.owned_room(interaction)
        _manage(channel)
        if channel.name != label:
            channel = await channel.edit(name=label, reason=f"Vivian: переименование владельцем {member.id}")
        return row, channel


async def limit(interaction, value):
    member = rooms.member_for(interaction)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 99:
        raise ValueError("Лимит: число от 0 до 99. Ноль — без ограничения.")
    async with rooms.lock_for(member.guild.id):
        row, channel = await rooms.owned_room(interaction)
        _manage(channel)
        if channel.user_limit != value:
            channel = await channel.edit(user_limit=value, reason=f"Vivian: лимит от владельца {member.id}")
        return row, channel


async def lock(interaction, locked):
    member = rooms.member_for(interaction)
    if not isinstance(locked, bool):
        raise ValueError("Некорректное состояние замка.")
    async with rooms.lock_for(member.guild.id):
        row, channel = await rooms.owned_room(interaction)
        _manage(channel, overwrite=True)
        overwrite = channel.overwrites_for(member.guild.default_role)
        overwrite.connect = not locked
        await channel.set_permissions(member.guild.default_role, overwrite=overwrite,
                                      reason=f"Vivian: доступ от владельца {member.id}")
        return {**row, "ui_locked": locked}, channel


async def visibility(interaction, hidden):
    member = rooms.member_for(interaction)
    if not isinstance(hidden, bool):
        raise ValueError("Некорректное состояние видимости.")
    async with rooms.lock_for(member.guild.id):
        row, channel = await rooms.owned_room(interaction)
        _manage(channel, overwrite=True)
        overwrite = channel.overwrites_for(member.guild.default_role)
        overwrite.view_channel = not hidden
        await channel.set_permissions(member.guild.default_role, overwrite=overwrite,
                                      reason=f"Vivian: видимость от владельца {member.id}")
        return {**row, "ui_hidden": hidden}, channel


async def invite(interaction, user):
    member = rooms.member_for(interaction)
    target = _target(member.guild, user)
    async with rooms.lock_for(member.guild.id):
        row, channel = await rooms.owned_room(interaction)
        _manage(channel, overwrite=True)
        overwrite = channel.overwrites_for(target)
        overwrite.view_channel = True
        overwrite.connect = True
        overwrite.speak = True
        await channel.set_permissions(target, overwrite=overwrite,
                                      reason=f"Vivian: приглашение от владельца {member.id}")
        return row, channel


async def kick(interaction, user):
    member = rooms.member_for(interaction)
    target = _target(member.guild, user)
    async with rooms.lock_for(member.guild.id):
        row, channel = await rooms.owned_room(interaction)
        connected = bool(target.voice and target.voice.channel == channel)
        _manage(channel, move=connected, overwrite=True)
        if target.id == row["owner_id"]:
            raise ValueError("Себя исключить нельзя. Можно передать владение или удалить комнату.")
        if target.guild_permissions.administrator or target.id == member.guild.owner_id:
            raise ValueError("Администраторы сервера могут обходить ограничения приватных комнат.")
        overwrite = channel.overwrites_for(target)
        overwrite.view_channel = False
        overwrite.connect = False
        await channel.set_permissions(target, overwrite=overwrite,
                                      reason=f"Vivian: исключение владельцем {member.id}")
        if target.voice and target.voice.channel == channel:
            await target.move_to(None, reason=f"Vivian: исключение из приватной комнаты владельцем {member.id}")
        return row, channel


async def _transfer(row, channel, target):
    if target.voice is None or target.voice.channel != channel:
        raise ValueError("Новый владелец должен находиться в этой комнате.")
    if target.id == row["owner_id"]:
        raise ValueError("Этот участник уже владелец комнаты.")
    other = await store.by_owner(channel.guild.id, target.id)
    if other is not None:
        raise ValueError("У этого участника уже есть своя комната или она создаётся.")
    _manage(channel, overwrite=True)
    previous = channel.overwrites_for(target)
    grant = discord.PermissionOverwrite.from_pair(*previous.pair())
    grant.view_channel, grant.connect, grant.speak = True, True, True
    await channel.set_permissions(target, overwrite=grant, reason="Vivian: смена владельца приватной комнаты")
    try:
        row = await store.transfer(channel.guild.id, channel.id, target.id)
    except Exception:
        # A timeout may arrive after COMMIT. Never revoke access from the
        # accepted owner just because the acknowledgement was lost.
        try:
            confirmed = await store.by_channel(channel.guild.id, channel.id)
        except Exception:
            confirmed = None
        if confirmed and confirmed["owner_id"] == target.id:
            return confirmed, channel
        if confirmed is not None:
            try:
                await channel.set_permissions(target, overwrite=previous,
                                              reason="Vivian: смена владельца не сохранена")
            except discord.DiscordException:
                pass
        raise
    return row, channel


async def transfer(interaction, user):
    member = rooms.member_for(interaction)
    target = _target(member.guild, user)
    async with rooms.lock_for(member.guild.id):
        row, channel = await rooms.owned_room(interaction)
        return await _transfer(row, channel, target)


async def claim(interaction):
    member = rooms.member_for(interaction)
    if member.voice is None or member.voice.channel is None:
        raise ValueError("Сначала зайди в комнату, которую хочешь забрать.")
    async with rooms.lock_for(member.guild.id):
        channel = member.voice.channel
        if not isinstance(channel, discord.VoiceChannel):
            raise ValueError("Забрать можно только приватную голосовую комнату Vivian.")
        expected = interaction.extras.get("room_channel_id")
        if expected is not None and expected != channel.id:
            raise ValueError("Ты уже в другой комнате. Открой её актуальную панель управления.")
        row = await store.by_channel(member.guild.id, channel.id)
        if row is None or row["state"] != "active":
            raise ValueError("Эта комната не принадлежит системе приватных комнат Vivian.")
        owner = member.guild.get_member(row["owner_id"])
        if owner and owner.voice and owner.voice.channel == channel:
            raise ValueError("Владелец ещё находится в комнате. Попроси его передать владение.")
        return await _transfer(row, channel, member)


async def delete(interaction):
    member = rooms.member_for(interaction)
    async with rooms.lock_for(member.guild.id):
        row, channel = await rooms.owned_room(interaction)
        _manage(channel)
        await store.mark_deleting(member.guild.id, channel.id)
        try:
            await channel.delete(reason=f"Vivian: удаление приватной комнаты владельцем {member.id}")
        except discord.NotFound:
            pass
        except discord.DiscordException:
            await store.activate(member.guild.id, row["owner_id"], row["token"], channel.id)
            raise
        await store.release(member.guild.id, row["owner_id"], row["token"])
        return "Комната удалена. Новую можно создать той же кнопкой."
