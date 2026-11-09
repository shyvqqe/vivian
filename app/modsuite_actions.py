from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import time

import discord

from core import config, guild_settings, modsuite_store as store


_PRIVILEGED = ("administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members", "kick_members")


def marker(item):
    return f"[vivian:{item['id']}]"


def audit_reason(item):
    return f"{item['reason'][:430]} {marker(item)}"


def _ids(member):
    values = getattr(member, "_roles", None)
    return set(values) if values is not None else {role.id for role in member.roles}


def _authority(member, roles, owner_id):
    selected = [role for role in roles if role.is_default() or role.id in _ids(member)]
    permissions = 0
    for role in selected:
        permissions |= role.permissions.value
    permissions = discord.Permissions.all() if member.id == owner_id or discord.Permissions(permissions).administrator else discord.Permissions(permissions)
    highest = max((role.position for role in selected), default=0)
    return permissions, highest


async def guard(interaction, target=None, permission="moderate_members", *, administrator=False):
    guild = interaction.guild
    if guild is None:
        raise ValueError("This action requires a server.")
    async with asyncio.timeout(12):
        actor = await guild.fetch_member(interaction.user.id)
        me = await guild.fetch_member(config.bot.user.id)
        roles = await guild.fetch_roles()
        target = await guild.fetch_member(target.id) if target is not None else None
    actor_permissions, actor_top = _authority(actor, roles, guild.owner_id)
    bot_permissions, bot_top = _authority(me, roles, guild.owner_id)
    if administrator and not actor_permissions.administrator:
        raise ValueError("Only a current server administrator can use this action.")
    if not getattr(actor_permissions, permission, False):
        raise ValueError(f"You need the {permission.replace('_', ' ')} permission.")
    if not getattr(bot_permissions, permission, False):
        raise ValueError(f"Vivian needs the {permission.replace('_', ' ')} permission.")
    if target is not None:
        target_permissions, target_top = _authority(target, roles, guild.owner_id)
        if target.id in {guild.owner_id, actor.id, me.id}:
            raise ValueError("This member cannot be targeted by this action.")
        if target_top >= bot_top or actor.id != guild.owner_id and target_top >= actor_top:
            raise ValueError("The target must be below both your highest role and Vivian's highest role.")
        if permission == "moderate_members" and target_permissions.administrator:
            raise ValueError("Discord does not allow timeouts on administrators.")
    return actor, target, me, roles


async def log(guild, text):
    settings = await guild_settings.get(guild.id)
    channel = guild.get_channel(int(settings.get("modlog_channel_id") or 0))
    if channel is None:
        return False
    try:
        await channel.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.DiscordException:
        return False


async def _member(guild, user_id):
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


async def _bot_check(guild, permission):
    me = await guild.fetch_member(config.bot.user.id)
    roles = await guild.fetch_roles()
    permissions, top = _authority(me, roles, guild.owner_id)
    if not getattr(permissions, permission, False):
        raise ValueError(f"Vivian needs {permission.replace('_', ' ')} to finish this case.")
    return me, roles, top


async def validate_isolation(guild, kind):
    settings = await guild_settings.get(guild.id)
    role = guild.get_role(int(settings.get(f"{kind}_role_id") or 0))
    channel = guild.get_channel(int(settings.get(f"{kind}_channel_id") or 0))
    if role is None or channel is None or role.is_default() or role.managed:
        raise ValueError(f"Configure a dedicated {kind} role and channel first.")
    me, roles, top = await _bot_check(guild, "manage_roles")
    role = next((item for item in roles if item.id == role.id), None)
    if role is None or role.position >= top or any(getattr(role.permissions, name) for name in _PRIVILEGED):
        raise ValueError("The restriction role must be below Vivian and have no privileged permissions.")
    if len(guild.channels) > 500:
        raise ValueError("This server has too many channels to validate safely.")
    own = channel.overwrites_for(role)
    everyone = channel.overwrites_for(guild.default_role)
    if own.view_channel is not True or own.send_messages is not True or everyone.view_channel is not False:
        raise ValueError("The restriction channel must deny @everyone visibility and explicitly allow the restriction role to view and send.")
    for other in guild.channels:
        if other.id != channel.id and other.overwrites_for(role).view_channel is not False:
            raise ValueError(f"The restriction role needs an explicit view-channel deny in channel {other.id}. No permissions were changed.")
    return role, channel, roles, top


async def start_punishment(interaction, target, kind, seconds, reason, *, raid_id=None, source_ref=None):
    required = {"ban": "ban_members", "timeout": "moderate_members", "jail": "manage_roles",
                "quarantine": "manage_roles", "shadowmute": "manage_messages"}[kind]
    actor, target, me, roles = await guard(interaction, target, required, administrator=kind == "shadowmute")
    if raid_id is None:
        raid = await store.get(interaction.guild.id, "raid", {})
        if raid.get("active") and (raid.get("ends_at") is None or raid["ends_at"] > time.time()):
            raid_id = raid.get("id")
    extra = {"raid_id": raid_id, "source_ref": source_ref or str(interaction.id),
             "identity_name": target.name, "avatar_hash": target.avatar.key if target.avatar else None}
    existing = await store.punishment(interaction.guild.id, kind, target.id)
    if existing and existing.get("state") in {"applying", "active", "retry", "releasing"} and existing.get("source_ref") == extra["source_ref"]:
        await run_case(interaction.guild, kind, target.id)
        return await store.punishment(interaction.guild.id, kind, target.id)
    if kind == "timeout" and target.timed_out_until and target.timed_out_until > discord.utils.utcnow():
        raise ValueError("This member already has a Discord timeout. Resolve that case before creating another timed case.")
    if kind == "shadowmute":
        settings = await guild_settings.get(interaction.guild.id)
        if not interaction.guild.get_channel(int(settings.get("modlog_channel_id") or 0)):
            raise ValueError("Configure a moderation log channel before enabling automatic message deletion.")
    if kind in {"jail", "quarantine"}:
        role, channel, fresh_roles, top = await validate_isolation(interaction.guild, kind)
        member_role_ids = _ids(target)
        selected = [item for item in fresh_roles if item.id in member_role_ids and not item.is_default()]
        if any(item.id == role.id for item in selected):
            raise ValueError("This member already has the restriction role without a matching active case. Review their roles first.")
        if any(item.managed or item.position >= top for item in selected) or len(selected) > 100:
            raise ValueError("This member has roles Vivian cannot safely remove and later restore.")
        extra.update(role_id=role.id, channel_id=channel.id,
                     roles=[{"id": item.id, "permissions": item.permissions.value} for item in selected],
                     expected_roles=[role.id])
    item = await store.create_punishment(interaction.guild.id, kind, target.id, actor.id, reason, seconds=seconds, extra=extra)
    await run_case(interaction.guild, item["kind"], target.id)
    saved = await store.punishment(interaction.guild.id, kind, target.id)
    return saved


async def automatic_quarantine(member, reason, *, raid_id=None):
    role, channel, roles, top = await validate_isolation(member.guild, "quarantine")
    target_roles = [item for item in roles if item.id in _ids(member) and not item.is_default()]
    permissions, target_top = _authority(member, roles, member.guild.owner_id)
    if member.id == member.guild.owner_id or target_top >= top or permissions.administrator or any(item.managed or item.position >= top for item in target_roles):
        return None
    existing = await store.punishment(member.guild.id, "quarantine", member.id)
    if existing and existing.get("state") in {"applying", "active", "retry", "releasing"}:
        return existing
    item = await store.create_punishment(member.guild.id, "quarantine", member.id, config.bot.user.id, reason,
        extra={"role_id": role.id, "channel_id": channel.id, "roles": [{"id": item.id, "permissions": item.permissions.value} for item in target_roles],
               "expected_roles": [role.id], "raid_id": raid_id})
    await run_case(member.guild, "quarantine", member.id)
    return item


async def _apply(guild, item):
    kind, user_id = item["kind"], item["user_id"]
    now = time.time()
    if kind == "ban":
        await _bot_check(guild, "ban_members")
        try:
            entry = await guild.fetch_ban(discord.Object(user_id))
        except discord.NotFound:
            entry = None
        if entry is not None and marker(item) not in (entry.reason or ""):
            await store.update_punishment(guild.id, item, state="superseded", lease_until=0, next_at=now + 86400)
            return
        if entry is None:
            member = await _member(guild, user_id)
            if member is not None:
                _, roles, top = await _bot_check(guild, "ban_members")
                _, target_top = _authority(member, roles, guild.owner_id)
                if user_id == guild.owner_id or target_top >= top:
                    raise ValueError("The target is no longer below Vivian's highest role.")
            await guild.ban(discord.Object(user_id), reason=audit_reason(item), delete_message_seconds=0)
        await store.update_punishment(guild.id, item, state="active", lease_until=0,
                                      next_at=item["ends_at"] or now + 86400)
        await store.history(guild.id, user_id, {"id": item["id"], "kind": kind, "reason": item["reason"], "actor_id": item["actor_id"]})
        await store.audit(guild.id, "ban", user_id=user_id, case_id=item["id"], raid_id=item.get("raid_id"), reason_marker=marker(item))
        ban_case = await store.create_case(guild.id, "ban", {"user_id": user_id, "reason": item["reason"], "punishment_id": item["id"]}, case_id=item["id"])
        if not item.get("ban_notice_attempted"):
            await store.update_punishment(guild.id, item, ban_notice_attempted=True)
            try:
                from commands.modsuite_views import appeal_notice
                user = config.bot.get_user(user_id) or await config.bot.fetch_user(user_id)
                await user.send(f"You were banned from {guild.name}. Reason: {item['reason'][:800]}", view=appeal_notice(guild.id, ban_case["id"]), allowed_mentions=discord.AllowedMentions.none())
            except discord.DiscordException:
                pass
    elif kind == "timeout":
        _, roles, top = await _bot_check(guild, "moderate_members")
        member = await _member(guild, user_id)
        if member is None:
            await store.update_punishment(guild.id, item, state="active", lease_until=0, next_at=min(item["ends_at"], now + 300))
            return
        permissions, target_top = _authority(member, roles, guild.owner_id)
        if permissions.administrator or target_top >= top:
            raise ValueError("The target cannot currently be timed out by Vivian.")
        current = member.timed_out_until.timestamp() if member.timed_out_until else None
        expected = item.get("last_until")
        owned_times = [stamp for stamp in (expected, item.get("previous_until")) if stamp is not None]
        if current and current > now and (not owned_times or all(abs(current - stamp) > 3 for stamp in owned_times)):
            await store.update_punishment(guild.id, item, state="superseded", lease_until=0)
            return
        until = min(item["ends_at"], now + 27 * 86400)
        item = await store.update_punishment(guild.id, item, previous_until=current, last_until=until)
        await member.timeout(datetime.fromtimestamp(until, timezone.utc), reason=audit_reason(item))
        await store.update_punishment(guild.id, item, state="active", lease_until=0, next_at=min(item["ends_at"], max(now + 30, until - 2 * 86400)))
        await store.history(guild.id, user_id, {"id": item["id"], "kind": kind, "reason": item["reason"], "actor_id": item["actor_id"]})
    elif kind in {"jail", "quarantine"}:
        role, channel, roles, top = await validate_isolation(guild, kind)
        if role.id != item["role_id"]:
            raise ValueError("The configured restriction role changed; a moderator must review this case.")
        member = await _member(guild, user_id)
        if member is None:
            await store.update_punishment(guild.id, item, state="active", lease_until=0, next_at=min(item["ends_at"] or now + 300, now + 300))
            return
        current = {identifier for identifier in _ids(member) if identifier != guild.id}
        expected = set(item["expected_roles"])
        original = {entry["id"] for entry in item["roles"]}
        if current != expected and current != original:
            raise ValueError("Member roles changed after this case was created; automatic replacement was stopped.")
        if current != expected:
            await member.edit(roles=[role], reason=audit_reason(item))
        await store.update_punishment(guild.id, item, state="active", lease_until=0, next_at=item["ends_at"] or now + 86400)
        await store.history(guild.id, user_id, {"id": item["id"], "kind": kind, "reason": item["reason"], "actor_id": item["actor_id"]})
        if kind == "quarantine":
            from commands.modsuite_views import quarantine_notice
            await channel.send(f"<@{user_id}>: use Verify to complete the private server challenge.", view=quarantine_notice(), allowed_mentions=discord.AllowedMentions(users=[discord.Object(user_id)]))
    elif kind == "shadowmute":
        member = await _member(guild, user_id)
        await store.update_punishment(guild.id, item, state="active", lease_until=0, next_at=item["ends_at"])
        if member:
            try:
                await member.send(f"Moderators enabled automatic deletion of your messages in {guild.name} until <t:{int(item['ends_at'])}:F>. Your messages are not secretly hidden: Discord messages are deleted.", allowed_mentions=discord.AllowedMentions.none())
            except discord.DiscordException:
                pass
    await log(guild, f"Case `{item['id']}`: {kind} applied to `{user_id}`. Reason: {item['reason'][:700]}")


async def _release(guild, item):
    kind, user_id = item["kind"], item["user_id"]
    skipped = []
    if kind == "ban":
        await _bot_check(guild, "ban_members")
        try:
            entry = await guild.fetch_ban(discord.Object(user_id))
        except discord.NotFound:
            entry = None
        if entry is not None:
            if marker(item) not in (entry.reason or ""):
                await store.update_punishment(guild.id, item, state="superseded", lease_until=0)
                return
            await guild.unban(discord.Object(user_id), reason=f"Vivian case {item['id']} release")
    elif kind == "timeout":
        await _bot_check(guild, "moderate_members")
        member = await _member(guild, user_id)
        if member and member.timed_out_until and member.timed_out_until > discord.utils.utcnow():
            expected = item.get("last_until")
            if expected is None or abs(member.timed_out_until.timestamp() - expected) > 3:
                await store.update_punishment(guild.id, item, state="superseded", lease_until=0)
                return
            await member.timeout(None, reason=f"Vivian case {item['id']} release")
    elif kind in {"jail", "quarantine"}:
        me, roles, top = await _bot_check(guild, "manage_roles")
        member = await _member(guild, user_id)
        if member:
            selected = [role for role in member.roles if not role.is_default() and role.id != item["role_id"]]
            current = {role.id for role in selected}
            for snapshot in item.get("roles", []):
                role = next((role for role in roles if role.id == snapshot["id"]), None)
                if role is None or role.managed or role.position >= top or role.permissions.value != snapshot["permissions"]:
                    skipped.append(snapshot["id"])
                elif role.id not in current:
                    selected.append(role)
            await member.edit(roles=selected, reason=f"Vivian case {item['id']} release")
    await store.update_punishment(guild.id, item, state="released", lease_until=0, released_at=time.time(), skipped_roles=skipped)
    await store.history(guild.id, user_id, {"id": item["id"] + ":release", "kind": f"{kind}_release", "skipped_roles": skipped})
    await log(guild, f"Case `{item['id']}`: {kind} released for `{user_id}`. Skipped changed or unavailable roles: {skipped or 'none'}.")


async def run_case(guild, kind, user_id):
    item = await store.claim_punishment(guild.id, kind, user_id)
    if item is None:
        return None
    try:
        async with asyncio.timeout(65):
            now = time.time()
            if item.get("phase") == "release" or item["ends_at"] is not None and item["ends_at"] <= now:
                await _release(guild, item)
            elif item["state"] in {"applying", "retry"} or kind == "timeout":
                await _apply(guild, item)
            else:
                await store.update_punishment(guild.id, item, lease_until=0, next_at=item["ends_at"] or now + 86400)
    except Exception as error:
        await store.update_punishment(guild.id, item, state="retry", lease_until=0,
            next_at=time.time() + min(3600, 30 * 2 ** min(item.get("attempts", 1), 6)), last_error=type(error).__name__)
        raise
    return await store.punishment(guild.id, kind, user_id)


async def request_release(guild, kind, user_id):
    await store.queue_release(guild.id, kind, user_id)
    await run_case(guild, kind, user_id)
    return await store.punishment(guild.id, kind, user_id)


async def add_warning(interaction, target, reason):
    actor, target, _, _ = await guard(interaction, target, "moderate_members")
    result = await store.warning(interaction.guild.id, target.id, actor.id, reason, str(interaction.id))
    settings = await guild_settings.get(interaction.guild.id)
    ladder = settings.get("moderate_warning_ladder") or [{"count": 3, "action": "timeout", "seconds": 3600}, {"count": 5, "action": "timeout", "seconds": 86400}, {"count": 7, "action": "ban"}]
    applied = None
    if result["new"]:
        for step in ladder[:10]:
            if step.get("count") == result["count"] and step.get("action") in {"timeout", "ban"}:
                try:
                    seconds = int(step.get("seconds", 3600)) if step["action"] == "timeout" else None
                    if seconds is not None and not 1 <= seconds <= 365 * 86400:
                        raise ValueError("The configured warning duration is invalid.")
                    applied = await start_punishment(interaction, target, step["action"], seconds,
                        f"Warning ladder: {result['count']} warnings. {reason}"[:1000])
                except ValueError as error:
                    applied = {"state": "not_applied", "reason": str(error)}
                break
        await log(interaction.guild, f"Warning {result['count']} for `{target.id}` by `{actor.id}`. {reason[:800]}")
    return {**result, "escalation": applied}
