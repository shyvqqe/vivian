"""Persist and restore one public Components V2 lobby per configured guild."""
from __future__ import annotations

import hashlib

import discord
from discord.ext import tasks

from core import community_store, config
from commands import room_views
from utils import components_v2 as v2, v2_delivery


_restored = set()


def _is_lobby(message):
    if config.bot.user is None or message.author.id != config.bot.user.id:
        return False
    stack = [item.to_dict() for item in message.components]
    marker = button = False
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == 10 and room_views.LOBBY_MARKER in str(node.get("content", "")):
                marker = True
            if node.get("custom_id") == "vivian_room_create":
                button = True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return marker and button


async def _find(channel, value):
    if value.get("panel_channel_id") == channel.id and value.get("panel_message_id"):
        try:
            message = await channel.fetch_message(int(value["panel_message_id"]))
            if _is_lobby(message):
                return message
        except discord.NotFound:
            pass
    async for message in channel.history(limit=100):
        if _is_lobby(message):
            return message
    return None


def check_channel(channel, guild):
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
        raise ValueError("Выбери текстовый канал этого сервера для общей панели.")
    rights = channel.permissions_for(guild.me) if guild.me else None
    if rights is None or not (rights.view_channel and rights.send_messages and rights.read_message_history):
        raise ValueError("В канале панели Vivian нужны права видеть канал, читать историю и отправлять сообщения.")


async def publish(guild, channel, value):
    """Caller serializes configuration changes using app.rooms.lock_for(guild.id)."""
    check_channel(channel, guild)
    message = await _find(channel, value)
    layout = room_views.lobby_view()
    if message is not None:
        message = await v2.edit_message(message, view=layout, attachments=[])
    else:
        generation = value.get("panel_message_id", "initial")
        identity = f"private-rooms:{guild.id}:{channel.id}:{generation}"
        sent = await config.bot.http.request(
            discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id),
            json={"flags": 1 << 15, "components": layout.to_components(),
                  "nonce": hashlib.sha256(identity.encode()).hexdigest()[:24], "enforce_nonce": True,
                  "allowed_mentions": {"parse": []}},
        )
        message = discord.Message(state=config.bot._connection, channel=channel, data=sent)
        v2_delivery.commit({"view": layout}, layout, message, state=config.bot._connection)
    saved = {**value, "panel_channel_id": channel.id, "panel_message_id": message.id}
    await community_store.set_setting(guild.id, "rooms_config", saved)
    return message


async def restore(guild):
    from app import rooms
    async with rooms.lock_for(guild.id):
        value = await rooms.settings(guild.id)
        if not value.get("panel_channel_id"):
            return
        channel = guild.get_channel(value["panel_channel_id"])
        if channel is None:
            return
        await publish(guild, channel, value)


@tasks.loop(seconds=120)
async def restore_room_panels():
    from core import discord_api, rooms_store
    if discord_api.is_paused() or not await rooms_store.initialize():
        return
    for guild in config.bot.guilds:
        if guild.id in _restored:
            continue
        try:
            await restore(guild)
            _restored.add(guild.id)
        except Exception as error:
            print(f"[Rooms] Lobby restoration deferred; error={type(error).__name__}", flush=True)


@restore_room_panels.before_loop
async def _before_restore():
    await config.bot.wait_until_ready()


def start():
    room_views.register_views()
    if not restore_room_panels.is_running():
        restore_room_panels.start()
