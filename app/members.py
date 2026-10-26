from __future__ import annotations
import asyncio
import discord
import time
from core import config as core_config
from core import signal as core_signal
from core import state as core_state
from utils import mod_logs as utils_mod_logs
from utils import helpers as utils_helpers
from utils import components_v2 as v2
from visuals import panels as visuals_panels


async def _welcome_member(member):
    channel = member.guild.get_channel(core_config.WELCOME_CHANNEL_ID)
    if channel is None:
        channel = await core_config.bot.fetch_channel(core_config.WELCOME_CHANNEL_ID)
    # The configured channel belongs to one server; do not greet in another one.
    if getattr(getattr(channel, "guild", None), "id", None) != member.guild.id:
        print(f"[Welcome] Channel belongs to another server; guild={member.guild.id}", flush=True)
        return
    me = getattr(member.guild, "me", None)
    permissions = channel.permissions_for(me) if me is not None else None
    if permissions is not None and not permissions.send_messages:
        print(f"[Welcome] Sending is unavailable; guild={member.guild.id}", flush=True)
        return
    greeting = f"Привет, {member.mention}! Я Vivian. Рада, что ты с нами. Осваивайся и расскажи немного о себе."
    # Only the new member can be notified, even if a nickname contains mentions.
    mentions = discord.AllowedMentions(users=[member], roles=False, everyone=False, replied_user=False)
    if permissions is None or not permissions.embed_links:
        await v2.send_message(channel, greeting, allowed_mentions=mentions)
        return
    embed = utils_helpers.vivian_embed(
        f"{member.mention}, добро пожаловать!\nЯ Vivian. Рада, что ты с нами. Как тебя лучше называть?",
        title="Рада знакомству",
    )
    avatar = getattr(getattr(member, "display_avatar", None), "url", None)
    if avatar:
        embed.set_thumbnail(url=avatar)
    visual = None
    if permissions.attach_files:
        visual = await visuals_panels._panel_visual_file("system")
        if visual is not None:
            visuals_panels._apply_panel_visual(embed, "system")
    try:
        payload = {"embed": embed, "allowed_mentions": mentions, "content": member.mention}
        if visual is not None:
            payload["file"] = visual
        await v2.send_message(channel, **payload)
    finally:
        if visual is not None:
            visual.close()


@core_config.bot.event
async def on_member_join(member):
    core_state.RECENT_BAN_EVENTS.pop((member.guild.id, member.id), None)
    core_signal.update_server_signal(member.guild.id, activity=2.0)
    core_signal.store_echo(member.guild.id, "member_join", f"Участник присоединился: {member.id}")
    # Autorole
    try:
        role = member.guild.get_role(core_config.AUTOROLE_ID)
        if role:
            await member.add_roles(role, reason="Autorole")
    except discord.DiscordException as error:
        print(f"[Welcome] Не удалось выдать роль участнику {member.id}: {error!r}", flush=True)
    # Welcome
    try:
        await _welcome_member(member)
    except discord.DiscordException as error:
        print(f"[Welcome] Не удалось поприветствовать участника {member.id}: {error!r}", flush=True)


@core_config.bot.event
async def on_member_ban(guild, user):
    now = time.monotonic()
    for event_key, event_time in list(core_state.RECENT_BAN_EVENTS.items()):
        if now - event_time > 30:
            core_state.RECENT_BAN_EVENTS.pop(event_key, None)
    core_state.RECENT_BAN_EVENTS[(guild.id, user.id)] = now
    await utils_mod_logs.send_member_event(
        guild,
        f"**УЧАСТНИК ЗАБЛОКИРОВАН**\n{discord.utils.escape_markdown(str(user))} · `{user.id}`",
    )


@core_config.bot.event
async def on_member_remove(member):
    event_key = (member.guild.id, member.id)
    # Discord can emit both ban and member-remove events for the same ban.
    await asyncio.sleep(1.0)
    banned_at = core_state.RECENT_BAN_EVENTS.pop(event_key, None)
    if banned_at is not None and time.monotonic() - banned_at < 30:
        return
    await utils_mod_logs.send_member_event(
        member.guild,
        f"**УЧАСТНИК ВЫБЫЛ С СЕРВЕРА**\n{discord.utils.escape_markdown(str(member))} · `{member.id}`",
    )
