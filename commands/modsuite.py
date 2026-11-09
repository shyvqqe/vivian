from __future__ import annotations

import difflib
import hashlib
import time

import discord
from discord import app_commands

from app import modsuite as runtime, modsuite_actions as actions
from commands import modsuite_views as views
from core import config, guild_settings, hub_models, hub_store, modsuite_store as store
from utils import hub_ui as ui


add_warning = actions.add_warning
jail = app_commands.Group(name="jail", description="Configured role isolation with safe role restoration", guild_only=True)
quarantine = app_commands.Group(name="quarantine", description="Configured account quarantine and private verification", guild_only=True)
shadowmute = app_commands.Group(name="shadowmute", description="Transparent automatic message deletion", guild_only=True)
raidmode = app_commands.Group(name="raidmode", description="Bounded raid controls and exact invite-flag restoration", guild_only=True)
modnote = app_commands.Group(name="modnote", description="Private moderator notes", guild_only=True)
appeal = app_commands.Group(name="appeal", description="Review durable ban appeals", guild_only=True)


async def _result(interaction, item):
    ends = f" until <t:{int(item['ends_at'])}:F>" if item.get("ends_at") else ""
    await ui.send(interaction, f"Case `{item['id']}`: **{item['kind']}** for `{item['user_id']}` is **{item['state']}**{ends}." + (" Pending cases are retried from durable storage." if item["state"] not in {"active", "released"} else ""))


@config.bot.tree.command(name="tempban", description="Ban a lower-ranked member for a durable parsed duration")
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def tempban(interaction: discord.Interaction, user: discord.Member, duration: str, reason: app_commands.Range[str, 1, 1000] = "Temporary ban"):
    if not await ui.defer(interaction):
        return
    try:
        item = await actions.start_punishment(interaction, user, "ban", store.duration(duration), reason)
        await _result(interaction, item)
    except Exception as error:
        await ui.error(interaction, error)


@config.bot.tree.command(name="tempmute", description="Apply a durable timeout; durations over 28 days renew safely")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def tempmute(interaction: discord.Interaction, user: discord.Member, duration: str, reason: app_commands.Range[str, 1, 1000] = "Temporary timeout"):
    if not await ui.defer(interaction):
        return
    try:
        await _result(interaction, await actions.start_punishment(interaction, user, "timeout", store.duration(duration), reason))
    except Exception as error:
        await ui.error(interaction, error)


@config.bot.tree.command(name="warn", description="Save a warning and apply the configured permission-checked ladder")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 1, 1000]):
    if not await ui.defer(interaction):
        return
    try:
        result = await add_warning(interaction, user, reason)
        escalation = result.get("escalation")
        text = f"Warning saved for `{user.id}`. Active warning count: **{result['count']}**."
        if escalation:
            text += f" Ladder result: {ui.safe(escalation.get('state'), 100)}. {ui.safe(escalation.get('reason') or '', 500)}"
        await ui.send(interaction, text)
    except Exception as error:
        await ui.error(interaction, error)


@jail.command(name="add", description="Isolate a member using a previously validated role and channel")
@app_commands.checks.has_permissions(manage_roles=True)
async def jail_add(interaction: discord.Interaction, user: discord.Member, duration: str, reason: app_commands.Range[str, 1, 1000] = "Moderator isolation"):
    if not await ui.defer(interaction):
        return
    try:
        await _result(interaction, await actions.start_punishment(interaction, user, "jail", store.duration(duration), reason))
    except Exception as error:
        await ui.error(interaction, error)


@jail.command(name="release", description="Release a Vivian jail case and restore eligible unchanged roles")
@app_commands.checks.has_permissions(manage_roles=True)
async def jail_release(interaction: discord.Interaction, user: discord.Member):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, user, "manage_roles")
        await _result(interaction, await actions.request_release(interaction.guild, "jail", user.id))
    except Exception as error:
        await ui.error(interaction, error)


@jail.command(name="check", description="Validate configured jail restrictions without changing permissions")
@app_commands.checks.has_permissions(manage_guild=True)
async def jail_check(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, permission="manage_guild")
        role, channel, _, _ = await actions.validate_isolation(interaction.guild, "jail")
        await ui.send(interaction, f"Jail role `{role.id}` and channel `{channel.id}` passed validation. No channel permissions were edited.")
    except Exception as error:
        await ui.error(interaction, error)


@jail.command(name="bail", description="Pay the administrator-configured bail for your own active jail case")
async def jail_bail(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        settings = await guild_settings.get(interaction.guild_id)
        amount = settings.get("jail_bail", 0)
        if type(amount) is not int or not 1 <= amount <= 10**9:
            raise ValueError("Paid bail is not enabled on this server.")
        item = await store.punishment(interaction.guild_id, "jail", interaction.user.id)
        if not item or item.get("state") not in {"active", "retry", "releasing"}:
            raise ValueError("You do not have an active Vivian jail case.")
        await actions._bot_check(interaction.guild, "manage_roles")
        from core import economy_store
        balance = await economy_store.spend(interaction.guild_id, interaction.user.id, amount, f"jail-bail:{item['id']}")
        saved = await actions.request_release(interaction.guild, "jail", interaction.user.id)
        await ui.send(interaction, f"Bail paid: {amount}. Balance: {balance}. Case state: {saved['state']}. Eligible roles are restored by the durable release job.")
    except Exception as error:
        await ui.error(interaction, error)


@quarantine.command(name="add", description="Place a lower-ranked member in the configured verification quarantine")
@app_commands.checks.has_permissions(manage_roles=True)
async def quarantine_add(interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 1, 1000] = "Verification required"):
    if not await ui.defer(interaction):
        return
    try:
        await _result(interaction, await actions.start_punishment(interaction, user, "quarantine", None, reason))
    except Exception as error:
        await ui.error(interaction, error)


@quarantine.command(name="release", description="Release a configured quarantine case after moderator review")
@app_commands.checks.has_permissions(manage_roles=True)
async def quarantine_release(interaction: discord.Interaction, user: discord.Member):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, user, "manage_roles")
        await _result(interaction, await actions.request_release(interaction.guild, "quarantine", user.id))
    except Exception as error:
        await ui.error(interaction, error)


@quarantine.command(name="check", description="Validate configured quarantine restrictions without granting permissions")
@app_commands.checks.has_permissions(manage_guild=True)
async def quarantine_check(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, permission="manage_guild")
        role, channel, _, _ = await actions.validate_isolation(interaction.guild, "quarantine")
        await ui.send(interaction, f"Quarantine role `{role.id}` and channel `{channel.id}` passed validation. Verification uses a private arithmetic challenge, not an external captcha.")
    except Exception as error:
        await ui.error(interaction, error)


@shadowmute.command(name="add", description="Enable disclosed automatic deletion of this member's messages")
@app_commands.checks.has_permissions(administrator=True)
async def shadowmute_add(interaction: discord.Interaction, user: discord.Member, duration: str, reason: app_commands.Range[str, 1, 1000] = "Automatic message deletion"):
    if not await ui.defer(interaction):
        return
    try:
        item = await actions.start_punishment(interaction, user, "shadowmute", store.duration(duration), reason)
        await ui.send(interaction, f"Automatic deletion case `{item['id']}` is {item['state']}. Discord cannot make a member's messages visible only to that member. This mode deletes messages and writes moderation logs; the member receives a disclosure when DMs are available.")
    except Exception as error:
        await ui.error(interaction, error)


@shadowmute.command(name="remove", description="Stop the recorded automatic message deletion mode")
@app_commands.checks.has_permissions(administrator=True)
async def shadowmute_remove(interaction: discord.Interaction, user: discord.Member):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, user, "manage_messages", administrator=True)
        await _result(interaction, await actions.request_release(interaction.guild, "shadowmute", user.id))
    except Exception as error:
        await ui.error(interaction, error)


@raidmode.command(name="enable", description="Pause the invite flag and activate configured raid quarantine controls")
@app_commands.checks.has_permissions(administrator=True)
async def raid_enable(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        raid = await runtime.enable_raid(interaction)
        await ui.send(interaction, f"Raid session `{raid['id']}` enabled. Invite-flag changes are snapshotted; unrelated incident timestamps remain unchanged. Quarantine still requires a validated restriction role and channel.")
    except Exception as error:
        await ui.error(interaction, error)


@raidmode.command(name="disable", description="Restore the previous invite flag from the active raid snapshot")
@app_commands.checks.has_permissions(administrator=True)
async def raid_disable(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        raid = await runtime.disable_raid(interaction)
        await ui.send(interaction, f"Raid session `{raid['id']}` disabled. Previous invite-flag state restored.")
    except Exception as error:
        await ui.error(interaction, error)


@raidmode.command(name="status", description="Inspect the current durable raid session")
@app_commands.checks.has_permissions(manage_guild=True)
async def raid_status(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, permission="manage_guild")
        raid = await store.get(interaction.guild_id, "raid", {})
        await ui.send(interaction, f"Session: `{raid.get('id', 'none')}`. Active: {bool(raid.get('active'))}. Phase: {raid.get('phase', 'none')}. Mode: {raid.get('mode', 'none')}.")
    except Exception as error:
        await ui.error(interaction, error)


@config.bot.tree.command(name="judge", description="Request a Gemini moderation recommendation without applying punishment")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def judge(interaction: discord.Interaction, text: app_commands.Range[str, 1, 6000], context: app_commands.Range[str, 1, 6000] = "No additional context"):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, permission="moderate_members")
        result = await hub_models.moderation(text, context)
        embed = ui.card("Vivian / Moderation review", ui.safe(result["reason"], 1000))
        embed.add_field(name="Classification", value=ui.safe(result["label"], 100))
        embed.add_field(name="Recommendation", value=ui.safe(result["recommendation"], 500), inline=False)
        embed.set_footer(text="Recommendation only. A moderator must review the context; no punishment was applied.")
        await ui.send(interaction, embed=embed)
    except Exception as error:
        await ui.error(interaction, error)


@modnote.command(name="add", description="Save a private moderator note on a server member")
@app_commands.checks.has_permissions(moderate_members=True)
async def note_add(interaction: discord.Interaction, user: discord.Member, text: app_commands.Range[str, 1, 1000]):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, permission="moderate_members")
        await store.note(interaction.guild_id, user.id, interaction.user.id, text)
        await ui.send(interaction, "Private moderator note saved.")
    except Exception as error:
        await ui.error(interaction, error)


@modnote.command(name="list", description="Read the most recent private moderator notes")
@app_commands.checks.has_permissions(moderate_members=True)
async def note_list(interaction: discord.Interaction, user: discord.Member):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, permission="moderate_members")
        record = await store.member_history(interaction.guild_id, user.id)
        lines = [f"<t:{int(item['at'])}:d> · `{item['actor_id']}` · {ui.safe(item['body'], 400)}" for item in record.get("notes", [])[-8:]]
        await ui.send(interaction, embed=ui.card(f"Moderator notes / {user.id}", "\n".join(lines) or "No notes."))
    except Exception as error:
        await ui.error(interaction, error)


@config.bot.tree.command(name="userinfo", description="Inspect member details and durable moderator history privately")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def userinfo(interaction: discord.Interaction, user: discord.User):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, permission="moderate_members")
        record = await store.member_history(interaction.guild_id, user.id)
        embed = ui.card(f"Vivian / Member {user.id}", f"Name: {ui.safe(user.name, 100)}\nAccount created: <t:{int(user.created_at.timestamp())}:F>\nWarnings: {record.get('warning_count', len(record.get('warnings', [])))} · Private notes: {len(record.get('notes', []))}")
        lines = [f"<t:{int(item['at'])}:d> {ui.safe(item.get('kind', 'event'), 40)} · {ui.safe(item.get('reason', ''), 180)}" for item in record.get("history", [])[-10:]]
        embed.add_field(name="Moderation history", value=("\n".join(lines) or "No recorded history.")[:1000], inline=False)
        await ui.send(interaction, embed=embed)
    except Exception as error:
        await ui.error(interaction, error)


async def submit_report(interaction, target, reason, message=None):
    if interaction.guild is None or interaction.user.bot or target.id == interaction.user.id:
        raise ValueError("Report another member in a server channel.")
    await store.report_limit(interaction.guild_id, interaction.user.id)
    settings = await guild_settings.get(interaction.guild_id)
    channel = interaction.guild.get_channel(int(settings.get("modlog_channel_id") or 0))
    if channel is None:
        raise ValueError("This server has not configured a moderation report channel.")
    case_id = hashlib.blake2b(f"report:{interaction.id}".encode(), digest_size=10).hexdigest()
    item = await store.create_case(interaction.guild_id, "report", {"user_id": target.id,
        "reporter_id": interaction.user.id, "body": store.text(reason, 1500),
        "channel_id": message.channel.id if message else interaction.channel_id,
        "message_id": message.id if message else None, "message_url": message.jump_url if message else None}, case_id=case_id)
    if not item.get("moderation_message_id"):
        try:
            posted = await channel.send(embed=views.case_embed(item), view=views.case_controls(interaction.guild_id, item), allowed_mentions=discord.AllowedMentions.none())
            await store.finish_case(interaction.guild_id, item["id"], moderation_channel_id=channel.id, moderation_message_id=posted.id)
        except discord.DiscordException:
            pass
    return item


@config.bot.tree.command(name="report", description="Send a private member report to the configured moderation team")
@app_commands.guild_only()
async def report(interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 1, 1500]):
    if not await ui.defer(interaction):
        return
    try:
        item = await submit_report(interaction, user, reason)
        await ui.send(interaction, f"Report `{item['id']}` saved privately. You receive feedback after an explicit moderator decision.")
    except Exception as error:
        await ui.error(interaction, error)


@app_commands.context_menu(name="Report message")
@app_commands.guild_only()
async def report_message(interaction: discord.Interaction, message: discord.Message):
    if not await ui.defer(interaction):
        return
    try:
        if message.guild is None or message.guild.id != interaction.guild_id:
            raise ValueError("Report a message from this server.")
        item = await submit_report(interaction, message.author, f"Reported message: {message.content[:1300] or '[attachment or embed]'}", message)
        await ui.send(interaction, f"Message report `{item['id']}` saved privately. A moderator will review it.")
    except Exception as error:
        await ui.error(interaction, error)


@appeal.command(name="queue", description="List the most recent pending ban appeals")
@app_commands.checks.has_permissions(ban_members=True)
async def appeal_queue(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, permission="ban_members")
        entries = await store.rows(interaction.guild_id, "case:", limit=500)
        pending = [entry["data"] for entry in entries if entry["data"].get("kind") == "appeal" and entry["data"].get("status") in {"pending", "processing"}]
        lines = [f"`{item['id']}` · member `{item['user_id']}` · {ui.safe(item.get('body', ''), 150)}" for item in pending[:15]]
        await ui.send(interaction, embed=ui.card("Vivian / Appeal queue", "\n".join(lines) or "No pending appeals in the most recent 500 cases. Use /appeal review for a known case ID."))
    except Exception as error:
        await ui.error(interaction, error)


@appeal.command(name="review", description="Open persistent moderator controls for a stored appeal or report")
@app_commands.checks.has_permissions(ban_members=True)
async def appeal_review(interaction: discord.Interaction, case_id: app_commands.Range[str, 20, 20]):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, permission="ban_members")
        item = await store.case(interaction.guild_id, case_id)
        if item.get("kind") not in {"appeal", "report"}:
            raise ValueError("Open a report or an appeal case for review.")
        await ui.send(interaction, embed=views.case_embed(item), view=views.case_controls(interaction.guild_id, item))
    except Exception as error:
        await ui.error(interaction, error)


@config.bot.tree.command(name="scan", description="Review account and recorded-ban similarities as non-conclusive heuristics")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def scan(interaction: discord.Interaction, user: discord.Member):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, permission="moderate_members")
        entries = await store.rows(interaction.guild_id, "punishment:ban:", limit=1000)
        avatar_hash = user.avatar.key if user.avatar else None
        candidates = []
        for entry in entries:
            item = entry["data"]
            if item.get("user_id") == user.id:
                continue
            name = str(item.get("identity_name") or "")
            similarity = difflib.SequenceMatcher(None, user.name.casefold(), name.casefold()).ratio() if name else 0
            exact_avatar = bool(avatar_hash and item.get("avatar_hash") == avatar_hash)
            if similarity >= 0.85 or exact_avatar:
                candidates.append(f"Recorded case `{item['id']}` · `{item['user_id']}` · name similarity {similarity:.0%} · exact avatar asset match: {exact_avatar}")
        age = max(0, int((time.time() - user.created_at.timestamp()) / 86400))
        text = f"Account age: {age} day(s). Custom avatar: {user.avatar is not None}.\n\n" + ("\n".join(candidates[:10]) or "No threshold matches in the most recent 1000 recorded Vivian ban cases.")
        embed = ui.card("Vivian / Account review", text)
        embed.set_footer(text="Heuristics only. Similar names or identical avatar assets do not establish identity. No punishment was applied.")
        await ui.send(interaction, embed=embed)
    except Exception as error:
        await ui.error(interaction, error)


@config.bot.tree.command(name="rollback", description="Preview up to 20 recorded Vivian raid bans before confirming their release")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def rollback(interaction: discord.Interaction, raid_id: app_commands.Range[str, 20, 20]):
    if not await ui.defer(interaction):
        return
    try:
        await actions.guard(interaction, permission="ban_members", administrator=True)
        entries = await store.rows(interaction.guild_id, "audit:", limit=1000)
        plan, seen = [], set()
        for entry in entries:
            item = entry["data"]
            if item.get("kind") == "ban" and item.get("raid_id") == raid_id and item.get("case_id") not in seen:
                seen.add(item["case_id"])
                plan.append(item)
                if len(plan) >= 20:
                    break
        if not plan:
            raise ValueError("No bot-recorded raid bans were found in the bounded audit history. Arbitrary Discord actions cannot be rolled back.")
        lines = [f"Unban member `{item['user_id']}` only while case `{item['case_id']}` still owns the ban." for item in plan]
        await ui.send(interaction, embed=ui.card("Vivian / Explicit rollback preview", "\n".join(lines) + "\n\nOnly these listed bans are eligible. Newer or unrelated changes are skipped. Deleted messages cannot be restored."),
                      view=views.RollbackView(interaction.guild_id, interaction.user.id, plan))
    except Exception as error:
        await ui.error(interaction, error)


for group in (jail, quarantine, shadowmute, raidmode, modnote, appeal):
    config.bot.tree.add_command(group)
config.bot.tree.add_command(report_message)
