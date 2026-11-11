"""Durable private voice rooms; never remove a channel without its saved record."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
import time
from weakref import WeakValueDictionary

import discord
from discord.ext import tasks

from core import community_store, config, discord_api, rooms_store as store


EMPTY_GRACE = 60
MAX_ROOMS = 20
_locks = WeakValueDictionary()
_empty_tasks = {}
_empty_since = {}
_pending_channels = {}
_status = {"state": "starting", "tracked_rooms": 0}


def lock_for(guild_id):
    lock = _locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[guild_id] = lock
    return lock


def public_status():
    return {**_status, "storage_ready": store.is_ready(), "pending_recovery": len(_pending_channels)}


def member_for(interaction):
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        raise ValueError("Комнаты доступны только участникам сервера.")
    member = guild.get_member(interaction.user.id)
    if member is None:
        raise ValueError("Не удалось подтвердить, что ты всё ещё на сервере.")
    source = interaction.channel
    if source is None or not source.permissions_for(member).view_channel:
        raise ValueError("У тебя нет доступа к этому каналу.")
    return member


async def settings(guild_id):
    return await community_store.get_setting(guild_id, "rooms_config") or {}


def _bot_rights(guild, channel=None):
    me = guild.me
    rights = channel.permissions_for(me) if channel and me else getattr(me, "guild_permissions", None)
    if rights is None or not (rights.view_channel and rights.manage_channels and rights.manage_roles):
        raise ValueError("Vivian нужны права «Видеть каналы», «Управлять каналами» и «Управлять ролями» для комнат.")
    return me


def _overwrites(guild, owner=None, members=(), *, source=None):
    result = {guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False)}
    result[guild.me] = discord.PermissionOverwrite(
        view_channel=True, connect=True, speak=True, manage_channels=True, manage_roles=True,
    )
    rights = source.permissions_for(guild.me) if source else guild.me.guild_permissions
    if rights.move_members:
        result[guild.me].move_members = True
    for member in ([owner] if owner else []) + list(members):
        result[member] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
    return result


async def ensure_settings(guild):
    """Caller holds the guild lock. Provision once instead of guessing an existing category."""
    value = await settings(guild.id)
    if value.get("enabled") is False:
        raise ValueError("Приватные комнаты отключены администрацией сервера.")
    category = guild.get_channel(value.get("category_id", 0))
    if isinstance(category, discord.CategoryChannel):
        _bot_rights(guild, category)
        return value, category
    _bot_rights(guild)
    category = await guild.create_category(
        "Vivian · Приватные комнаты", overwrites=_overwrites(guild),
        reason="Vivian: настройка приватных комнат по запросу участника",
    )
    value = {**value, "category_id": category.id, "enabled": True}
    try:
        await community_store.set_setting(guild.id, "rooms_config", value)
    except BaseException:
        if not category.channels:
            try:
                await category.delete(reason="Vivian: не удалось сохранить настройку комнат")
            except discord.DiscordException:
                pass
        raise
    return value, category


def room_name(name, member):
    value = name if name is not None else f"◇ {member.display_name}"
    value = re.sub(r"[\r\n\x00-\x1f\x7f]", " ", value).strip()
    if not value or len(value) > 80:
        raise ValueError("Название комнаты должно содержать от 1 до 80 символов.")
    return value


async def owned_room(interaction, require_owner=True):
    member = member_for(interaction)
    row = await store.by_owner(member.guild.id, member.id)
    if row is None and not require_owner and member.voice and member.voice.channel:
        row = await store.by_channel(member.guild.id, member.voice.channel.id)
    if row is None:
        raise ValueError("У тебя пока нет своей комнаты. Нажми «Создать комнату».")
    expected = interaction.extras.get("room_channel_id")
    if expected is not None and expected != row["channel_id"]:
        raise ValueError("Эта панель относится к старой комнате. Открой управление своей текущей комнатой.")
    if require_owner and row["owner_id"] != member.id:
        raise ValueError("Этой комнатой может управлять только её владелец.")
    channel = member.guild.get_channel(row["channel_id"] or 0)
    if not isinstance(channel, discord.VoiceChannel):
        if row["channel_id"]:
            await store.release(member.guild.id, row["owner_id"], row["token"])
        raise ValueError("Комната уже удалена. Можно создать новую.")
    if row["state"] != "active":
        raise ValueError("Операция с комнатой ещё завершается. Попробуй чуть позже.")
    return row, channel


async def _move_owner(channel, member, row):
    if member.voice is None or member.voice.channel is None:
        return {**row, "move_failed": True}
    if member.voice and member.voice.channel != channel:
        if not channel.permissions_for(member.guild.me).move_members:
            return {**row, "move_failed": True}
        try:
            await member.move_to(channel, reason="Vivian: переход в свою приватную комнату")
        except discord.DiscordException:
            return {**row, "move_failed": True}
    return row


async def create_room(interaction, members=None, name=None):
    member = member_for(interaction)
    label = room_name(name, member)
    selected = {}
    for user in members or ():
        target = member.guild.get_member(user.id)
        if target is None or target.bot:
            raise ValueError("Приглашать можно только участников этого сервера, кроме ботов.")
        if target.id != member.id:
            selected[target.id] = target
    if len(selected) > 10:
        raise ValueError("За один раз можно пригласить до 10 участников.")
    async with lock_for(member.guild.id):
        previous = await store.by_owner(member.guild.id, member.id)
        if previous:
            existing = member.guild.get_channel(previous["channel_id"] or 0)
            if isinstance(existing, discord.VoiceChannel) and previous["state"] == "active":
                return await _move_owner(existing, member, {**previous, "reused": True}), existing
            if previous["channel_id"] and existing is None:
                await store.release(member.guild.id, member.id, previous["token"])
            else:
                raise ValueError("Твоя комната уже создаётся или удаляется. Подожди немного.")
        if len(await store.list_rooms(member.guild.id)) >= MAX_ROOMS:
            raise ValueError("Сейчас заняты все 20 приватных комнат. Попробуй после освобождения одной.")
        row = await store.reserve(member.guild.id, member.id)
        channel = None
        try:
            _, category = await ensure_settings(member.guild)
            channel = await member.guild.create_voice_channel(
                label, category=category,
                overwrites=_overwrites(member.guild, member, selected.values(), source=category),
                user_limit=0, reason=f"Vivian: приватная комната; owner={member.id}; reservation={row['token']}",
            )
            _pending_channels[(member.guild.id, member.id)] = (row["token"], channel.id)
            row = await store.activate(member.guild.id, member.id, row["token"], channel.id)
            _pending_channels.pop((member.guild.id, member.id), None)
        except BaseException as error:
            deleted = channel is None
            if channel is not None:
                try:
                    await channel.delete(reason="Vivian: создание комнаты не удалось завершить")
                    deleted = True
                except discord.DiscordException:
                    # Preserve a known channel after an uncertain database commit;
                    # maintenance can reconcile it, never erase its only record.
                    try:
                        await store.activate(member.guild.id, member.id, row["token"], channel.id)
                        _pending_channels.pop((member.guild.id, member.id), None)
                    except Exception:
                        pass
            if deleted:
                await store.release(member.guild.id, member.id, row["token"])
                _pending_channels.pop((member.guild.id, member.id), None)
            elif (member.guild.id, member.id) in _pending_channels:
                print(f"[Rooms] Creation needs reconciliation; guild_id={member.guild.id}; channel_id={channel.id}", flush=True)
                if not isinstance(error, asyncio.CancelledError):
                    raise community_store.CommunityUnavailable(
                        f"Комната {channel.mention} создана, но сохранение пока не подтверждено. "
                        "Повторную комнату не создаю; попробуй открыть управление чуть позже."
                    ) from None
            raise
        row = await _move_owner(channel, member, row)
        if not channel.members:
            schedule_empty(channel.id, member.guild.id)
        return row, channel


def schedule_empty(channel_id, guild_id):
    previous = _empty_tasks.get(channel_id)
    if previous and not previous.done():
        return
    _empty_since.setdefault(channel_id, time.monotonic())
    task = asyncio.create_task(cleanup_empty(channel_id, guild_id))
    _empty_tasks[channel_id] = task
    task.add_done_callback(lambda done: _empty_tasks.pop(channel_id, None)
                           if _empty_tasks.get(channel_id) is done else None)


async def cleanup_empty(channel_id, guild_id, delay=EMPTY_GRACE):
    try:
        await asyncio.sleep(max(0, delay))
        # A join cancels only the sleeping task. Once a remote mutation starts,
        # cancellation must not leave a persisted room stuck in deleting state.
        if _empty_tasks.get(channel_id) is asyncio.current_task():
            _empty_tasks.pop(channel_id, None)
        if discord_api.is_paused():
            return
        async with lock_for(guild_id):
            row = await store.by_channel(guild_id, channel_id)
            if row is None:
                _empty_since.pop(channel_id, None)
                return
            guild = config.bot.get_guild(guild_id)
            if guild is None:
                _empty_since.pop(channel_id, None)
                return
            channel = guild.get_channel(channel_id)
            if channel is None:
                await store.release(guild_id, row["owner_id"], row["token"])
                _empty_since.pop(channel_id, None)
                return
            if not isinstance(channel, discord.VoiceChannel) or channel.members:
                return
            empty_since = _empty_since.get(channel_id)
            if empty_since is None or time.monotonic() - empty_since < max(0, delay):
                return
            await store.mark_deleting(guild_id, channel_id)
            if channel.members:
                await store.activate(guild_id, row["owner_id"], row["token"], channel.id)
                return
            try:
                await channel.delete(reason="Vivian: приватная комната пуста 60 секунд")
            except discord.NotFound:
                pass
            except discord.DiscordException:
                await store.activate(guild_id, row["owner_id"], row["token"], channel.id)
                raise
            await store.release(guild_id, row["owner_id"], row["token"])
            _empty_since.pop(channel_id, None)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"[Rooms] Empty cleanup deferred; error={type(error).__name__}", flush=True)


async def on_voice_state_update(member, before, after):
    if before.channel and before.channel != after.channel and not before.channel.members:
        schedule_empty(before.channel.id, member.guild.id)
    if after.channel:
        _empty_since.pop(after.channel.id, None)
        task = _empty_tasks.pop(after.channel.id, None)
        if task:
            task.cancel()


async def _maintain():
    if discord_api.is_paused() or not await store.initialize():
        _status["state"] = "waiting"
        return
    for (guild_id, owner_id), (token, channel_id) in list(_pending_channels.items()):
        async with lock_for(guild_id):
            row = await store.by_owner(guild_id, owner_id)
            if row is None or row["token"] != token:
                continue
            guild = config.bot.get_guild(guild_id)
            if guild is None:
                continue
            channel = guild.get_channel(channel_id)
            if channel is None:
                await store.release(guild_id, owner_id, token)
            elif isinstance(channel, discord.VoiceChannel):
                await store.activate(guild_id, owner_id, token, channel.id)
            else:
                continue
            _pending_channels.pop((guild_id, owner_id), None)
    rows = await store.list_rooms()
    _status.update(state="pending_recovery" if _pending_channels else "ready", tracked_rooms=len(rows))
    for snapshot in rows:
        async with lock_for(snapshot["guild_id"]):
            row = await store.by_owner(snapshot["guild_id"], snapshot["owner_id"])
            if row is None or row["token"] != snapshot["token"]:
                continue
            guild = config.bot.get_guild(row["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(row["channel_id"] or 0)
            if row["channel_id"] and channel is None:
                await store.release(guild.id, row["owner_id"], row["token"])
            elif isinstance(channel, discord.VoiceChannel) and not channel.members:
                schedule_empty(channel.id, guild.id)
            elif isinstance(channel, discord.VoiceChannel) and row["state"] == "deleting":
                await store.activate(guild.id, row["owner_id"], row["token"], channel.id)
            elif not row["channel_id"] and row["state"] == "creating":
                if (guild.id, row["owner_id"]) in _pending_channels:
                    continue
                stamp = row["updated_at"]
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - stamp).total_seconds() > 300:
                    await store.release(guild.id, row["owner_id"], row["token"])


@tasks.loop(seconds=60)
async def rooms_maintenance():
    try:
        await _maintain()
    except Exception as error:
        _status["state"] = "retrying"
        print(f"[Rooms] Maintenance deferred; error={type(error).__name__}", flush=True)


@rooms_maintenance.before_loop
async def _before_rooms():
    await config.bot.wait_until_ready()


def start():
    if not getattr(config.bot, "private_rooms_listener_registered", False):
        config.bot.add_listener(on_voice_state_update, "on_voice_state_update")
        config.bot.private_rooms_listener_registered = True
    if not rooms_maintenance.is_running():
        rooms_maintenance.start()
