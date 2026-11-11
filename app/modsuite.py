from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid

import discord
from discord.ext import tasks

from app import modsuite_actions as actions
from core import config, guild_settings, hub_store, modsuite_store as store


_log = logging.getLogger(__name__)
_LINK = re.compile(r"(?:https?://|discord(?:app)?\.com/invite/|discord\.gg/)", re.I)


def start():
    from commands.modsuite_views import register_views
    register_views()
    if not expiry_loop.is_running():
        expiry_loop.start()


async def process_guild(guild):
    offset = 0
    while offset < 2000:
        batch = await store.rows(guild.id, "punishment:", limit=100, offset=offset)
        for row in batch:
            item = row["data"]
            if item.get("state") not in {"applying", "active", "retry", "releasing"} or item.get("next_at", 0) > time.time():
                continue
            try:
                await actions.run_case(guild, item["kind"], item["user_id"])
            except Exception as error:
                _log.warning("Moderation case deferred: guild=%s case=%s error=%s", guild.id, item.get("id"), type(error).__name__)
        if len(batch) < 100:
            break
        offset += len(batch)
    raid = await store.get(guild.id, "raid")
    if raid and raid.get("active") and raid.get("mode") == "automatic" and raid.get("ends_at", 0) <= time.time():
        raid.update(active=False, phase="expired", ended_at=time.time())
        await store.put(guild.id, "raid", raid)
    if raid and raid.get("phase") == "restoring":
        await _restore_raid(guild, raid)


@tasks.loop(seconds=30)
async def expiry_loop():
    for guild in list(config.bot.guilds):
        try:
            await process_guild(guild)
        except Exception as error:
            _log.warning("Moderation scheduler unavailable: guild=%s error=%s", guild.id, type(error).__name__)


@expiry_loop.before_loop
async def _before_expiry():
    await config.bot.wait_until_ready()


async def enable_raid(interaction):
    await actions.guard(interaction, permission="manage_guild", administrator=True)
    guild = interaction.guild
    current_guild = await config.bot.fetch_guild(guild.id)
    def prepare(value):
        if value.get("active"):
            raise ValueError("Raid mode is already active. Disable the existing session first.")
        value.clear()
        value.update(id=uuid.uuid4().hex[:20], active=True, phase="applying", mode="manual",
            actor_id=interaction.user.id, started_at=time.time(),
            previous_disabled="INVITES_DISABLED" in current_guild.features, applied_disabled=True)
        return dict(value)
    raid = await hub_store.mutate(guild.id, store.NAMESPACE, "raid", prepare, default={})
    try:
        if not raid["previous_disabled"]:
            await guild.edit(invites_disabled=True, reason=f"Vivian raid session {raid['id']}")
        raid["phase"] = "active"
        await store.put(guild.id, "raid", raid)
        await store.audit(guild.id, "raid_enabled", raid_id=raid["id"], actor_id=interaction.user.id)
        await actions.log(guild, f"Raid mode `{raid['id']}` enabled. New suspicious joins are quarantined when a safe quarantine configuration exists. Existing timed incident pauses were not edited.")
        return raid
    except Exception:
        raid["phase"] = "restoring"
        await store.put(guild.id, "raid", raid)
        raise


async def _restore_raid(guild, raid):
    await actions._bot_check(guild, "manage_guild")
    current_guild = await config.bot.fetch_guild(guild.id)
    disabled = "INVITES_DISABLED" in current_guild.features
    if not raid["previous_disabled"] and disabled:
        await guild.edit(invites_disabled=False, reason=f"Vivian raid session {raid['id']} restore")
    raid = {**raid, "active": False, "phase": "restored", "ended_at": time.time()}
    await store.put(guild.id, "raid", raid)
    await store.audit(guild.id, "raid_disabled", raid_id=raid["id"])
    await actions.log(guild, f"Raid mode `{raid['id']}` disabled. Vivian's invite flag was restored to its previous value; unrelated incident pause timestamps were preserved.")
    return raid


async def disable_raid(interaction):
    await actions.guard(interaction, permission="manage_guild", administrator=True)
    raid = await store.get(interaction.guild_id, "raid")
    if not raid or not raid.get("active"):
        raise ValueError("No active raid session was found.")
    if raid.get("mode") == "automatic":
        raid.update(active=False, phase="restored", ended_at=time.time())
        await store.put(interaction.guild_id, "raid", raid)
        return raid
    raid["phase"] = "restoring"
    await store.put(interaction.guild_id, "raid", raid)
    return await _restore_raid(interaction.guild, raid)


async def observe_message(message):
    if message.guild is None or message.author.bot:
        return False
    deleted = False
    try:
        settings = await guild_settings.get(message.guild.id)
        me = message.guild.me
        permissions = message.channel.permissions_for(me) if me else None
        author_permissions = getattr(message.author, "guild_permissions", None)
        if not permissions or not permissions.manage_messages or author_permissions and (author_permissions.administrator or author_permissions.manage_messages):
            return False
        shadow = await store.punishment(message.guild.id, "shadowmute", message.author.id)
        raid = await store.get(message.guild.id, "raid", {})
        shadow_active = bool(shadow and shadow.get("state") == "active" and (shadow.get("ends_at") or 0) > time.time())
        active_raid = raid.get("active") and (raid.get("ends_at") is None or raid["ends_at"] > time.time())
        link_filter = settings.get("moderate_link_filter") or active_raid and settings.get("moderate_raid_link_filter")
        if not shadow_active and not (link_filter and _LINK.search(message.content or "")):
            return False
        log_channel = message.guild.get_channel(int(settings.get("modlog_channel_id") or 0))
        if log_channel is None:
            return False
        await message.delete()
        deleted = True
        await store.audit(message.guild.id, "message_deleted", user_id=message.author.id, channel_id=message.channel.id,
            message_id=message.id, reason="automatic deletion" if shadow_active else "configured link filter",
            raid_id=raid.get("id") if raid.get("active") else None, reversible=False)
        await actions.log(message.guild, f"Message `{message.id}` from `{message.author.id}` in `{message.channel.id}` deleted by the configured {'automatic deletion mode' if shadow_active else 'link filter'}. Deleted messages cannot be restored.")
        return True
    except Exception as error:
        _log.warning("Moderation observer skipped: error=%s", type(error).__name__)
        return deleted


async def member_join(member):
    if member.bot:
        return
    try:
        for kind in ("jail", "quarantine"):
            active = await store.punishment(member.guild.id, kind, member.id)
            if active and active.get("state") in {"active", "retry", "applying"} and (active.get("ends_at") is None or active["ends_at"] > time.time()):
                role, _, _, _ = await actions.validate_isolation(member.guild, kind)
                if role.id == active.get("role_id"):
                    await member.edit(roles=[role], reason=actions.audit_reason(active))
                return
        settings = await guild_settings.get(member.guild.id)
        now = time.time()
        def arrival(value):
            stamps = [stamp for stamp in value.get("stamps", []) if stamp > now - min(120, max(5, int(settings.get("moderate_raid_window_seconds", 15))))]
            stamps.append(now)
            value["stamps"] = stamps[-100:]
            return len(value["stamps"])
        count = await hub_store.mutate(member.guild.id, store.NAMESPACE, "joins", arrival, default={})
        raid = await store.get(member.guild.id, "raid", {})
        spike = bool(settings.get("moderate_raid_auto") and count >= min(50, max(3, int(settings.get("moderate_raid_join_count", 6)))))
        if spike and not raid.get("active"):
            raid = {"id": uuid.uuid4().hex[:20], "mode": "automatic", "active": True, "phase": "active", "started_at": now, "ends_at": now + 1800}
            await store.put(member.guild.id, "raid", raid)
            await actions.log(member.guild, f"A configured join spike activated quarantine session `{raid['id']}` for 30 minutes. No members were kicked and no invite settings were changed.")
        active_raid = raid.get("active") and (raid.get("ends_at") is None or raid["ends_at"] > now)
        account_age = max(0, now - member.created_at.timestamp())
        minimum = max(0, min(8760, int(settings.get("moderate_min_account_age_hours", 24)))) * 3600
        avatar_missing = member.avatar is None and getattr(member, "guild_avatar", None) is None
        suspicious = settings.get("moderate_quarantine_enabled") and (account_age < minimum or settings.get("moderate_require_avatar") and avatar_missing)
        if active_raid or suspicious:
            await actions.automatic_quarantine(member, "Configured join quarantine: join spike or account requirements", raid_id=raid.get("id") if active_raid else None)
    except Exception as error:
        _log.warning("Join quarantine skipped: guild=%s member=%s error=%s", member.guild.id, member.id, type(error).__name__)
