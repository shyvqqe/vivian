from __future__ import annotations

import hashlib
import random
import re
import time

import discord

from app import modsuite_actions as actions
from core import config, guild_settings, hub_store, modsuite_store as store
from utils import hub_ui as ui


async def _defer(interaction):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)


async def _private(interaction, text, *, view=None, embed=None):
    payload = {"content": str(text)[:1900], "allowed_mentions": discord.AllowedMentions.none()}
    if view is not None:
        payload["view"] = view
    if embed is not None:
        payload["embed"] = embed
    if interaction.response.is_done():
        return await interaction.edit_original_response(**payload)
    return await interaction.response.send_message(**payload, ephemeral=True)


def case_embed(item):
    embed = ui.card(f"Vivian / {item['kind'].title()} case {item['id']}")
    embed.add_field(name="Member", value=f"`{item.get('user_id', 'unknown')}`")
    embed.add_field(name="Status", value=ui.safe(item["status"], 100))
    embed.add_field(name="Details", value=ui.safe(item.get("body") or item.get("reason") or "No details", 1000), inline=False)
    if item.get("message_url"):
        embed.add_field(name="Message", value=item["message_url"], inline=False)
    return embed


class AppealModal(discord.ui.Modal, title="Submit a ban appeal"):
    body = discord.ui.TextInput(label="Your appeal", min_length=10, max_length=1500, style=discord.TextStyle.paragraph)

    def __init__(self, guild_id, ban_id):
        super().__init__(timeout=300)
        self.guild_id, self.ban_id = guild_id, ban_id

    async def on_submit(self, interaction):
        await _defer(interaction)
        try:
            original = await store.case(self.guild_id, self.ban_id)
            if original["kind"] != "ban" or original["user_id"] != interaction.user.id:
                raise ValueError("Only the banned member can submit this appeal.")
            guild = config.bot.get_guild(self.guild_id)
            if guild is None:
                raise ValueError("Vivian is no longer connected to that server.")
            punishment = await store.punishment(guild.id, "ban", interaction.user.id)
            if not punishment or punishment["id"] != original["punishment_id"] or punishment.get("state") not in {"active", "retry"}:
                raise ValueError("This ban case is no longer active.")
            case_id = hashlib.blake2b(f"appeal:{self.ban_id}".encode(), digest_size=10).hexdigest()
            item = await store.create_case(guild.id, "appeal", {"user_id": interaction.user.id, "ban_id": self.ban_id,
                "body": str(self.body.value), "reporter_id": interaction.user.id}, case_id=case_id)
            settings = await guild_settings.get(guild.id)
            channel = guild.get_channel(int(settings.get("appeals_channel_id") or settings.get("modlog_channel_id") or 0))
            published = item.get("message_id") is not None
            if channel and not published:
                try:
                    message = await channel.send(embed=case_embed(item), view=case_controls(guild.id, item), allowed_mentions=discord.AllowedMentions.none())
                    await store.finish_case(guild.id, item["id"], channel_id=channel.id, message_id=message.id)
                    published = True
                except discord.DiscordException:
                    pass
            await _private(interaction, f"Appeal `{item['id']}` is saved. Moderators can review it in the appeal queue." + ("" if published else " Its moderation card could not be published yet."))
        except Exception as error:
            await _private(interaction, str(error) if isinstance(error, (ValueError, hub_store.Unavailable)) else "The appeal could not be saved. Try again shortly.")


class AppealButton(discord.ui.DynamicItem[discord.ui.Button], template=r"vivian:appeal:(?P<guild>\d{1,20}):(?P<case>[a-f0-9]{20})"):
    def __init__(self, guild_id, case_id):
        self.guild_id, self.case_id = int(guild_id), case_id
        super().__init__(discord.ui.Button(label="Appeal this ban", custom_id=f"vivian:appeal:{guild_id}:{case_id}", style=discord.ButtonStyle.secondary))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild"]), match["case"])

    async def callback(self, interaction):
        await interaction.response.send_modal(AppealModal(self.guild_id, self.case_id))


def appeal_notice(guild_id, case_id):
    view = discord.ui.View(timeout=None)
    view.add_item(AppealButton(guild_id, case_id))
    return view


async def _feedback(item, status):
    user_id = item.get("reporter_id") or item["user_id"]
    try:
        user = config.bot.get_user(user_id) or await config.bot.fetch_user(user_id)
        await user.send(f"Your {item['kind']} case `{item['id']}` was reviewed by a moderator. Outcome: {status}.", allowed_mentions=discord.AllowedMentions.none())
    except discord.DiscordException:
        pass


async def decide(interaction, guild_id, case_id, action):
    if interaction.guild is None or interaction.guild_id != guild_id:
        raise ValueError("Review this case in its original server.")
    item = await store.case(guild_id, case_id)
    if item["kind"] == "appeal":
        if action not in {"accept", "decline"}:
            raise ValueError("Invalid appeal action.")
        await actions.guard(interaction, permission="ban_members")
    elif item["kind"] == "report":
        if action not in {"delete", "timeout", "ban", "ignore"}:
            raise ValueError("Invalid report action.")
        permission = {"delete": "manage_messages", "timeout": "moderate_members", "ban": "ban_members", "ignore": "manage_messages"}[action]
        await actions.guard(interaction, permission=permission)
    else:
        raise ValueError("This case does not support these controls.")
    item = await store.claim_case(guild_id, case_id, interaction.user.id, action)
    guild = interaction.guild
    if item["kind"] == "appeal" and action == "accept":
        punishment = await store.punishment(guild_id, "ban", item["user_id"])
        if not punishment or punishment["id"] != item["ban_id"]:
            raise ValueError("This appeal refers to an older ban. Review the newer ban separately.")
        if punishment["state"] == "released":
            pass
        else:
            result = await actions.request_release(guild, "ban", item["user_id"])
            if result["state"] != "released":
                raise ValueError("A newer ban changed this case. No newer ban was removed.")
    elif item["kind"] == "report" and action != "ignore":
        target = guild.get_member(item["user_id"])
        existing = await store.punishment(guild_id, "ban" if action == "ban" else "timeout", item["user_id"]) if action != "delete" else None
        already_applied = bool(existing and existing.get("source_ref") == f"report:{case_id}" and existing.get("state") == "active")
        if not already_applied:
            if target is None:
                target = await guild.fetch_member(item["user_id"])
            if action == "delete":
                actor, target, me, _ = await actions.guard(interaction, target, "manage_messages")
                channel = guild.get_channel(item.get("channel_id", 0)) or config.bot.get_channel(item.get("channel_id", 0))
                if channel is None or getattr(getattr(channel, "guild", None), "id", None) != guild_id:
                    raise ValueError("The original message channel is unavailable.")
                if not channel.permissions_for(actor).manage_messages or not channel.permissions_for(me).manage_messages:
                    raise ValueError("Both you and Vivian need Manage Messages in the original channel.")
                try:
                    message = await channel.fetch_message(item["message_id"])
                    if message.author.id != item["user_id"]:
                        raise ValueError("The stored author does not match this message.")
                    await message.delete()
                    await store.audit(guild_id, "message_deleted", user_id=target.id, message_id=message.id, channel_id=channel.id, reversible=False)
                except discord.NotFound:
                    pass
            else:
                await actions.start_punishment(interaction, target, "ban" if action == "ban" else "timeout",
                    None if action == "ban" else 3600, f"Report {case_id}: {item['body']}"[:1000], source_ref=f"report:{case_id}")
    status = "accepted" if action == "accept" else "declined" if action == "decline" else "resolved"
    result = await store.finish_case(guild_id, case_id, status=status, decided_at=time.time(), outcome=action, lease_until=0)
    await store.audit(guild_id, "case_decided", case_id=case_id, actor_id=interaction.user.id, outcome=action)
    await actions.log(guild, f"Case `{case_id}` reviewed by `{interaction.user.id}`: {action}.")
    await _feedback(result, action)
    return result


class CaseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"vivian:case:(?P<guild>\d{1,20}):(?P<case>[a-f0-9]{20}):(?P<action>accept|decline|delete|timeout|ban|ignore)"):
    def __init__(self, guild_id, case_id, action):
        self.guild_id, self.case_id, self.action = int(guild_id), case_id, action
        super().__init__(discord.ui.Button(label=action.title(), custom_id=f"vivian:case:{guild_id}:{case_id}:{action}",
            style=discord.ButtonStyle.danger if action in {"ban", "delete", "decline"} else discord.ButtonStyle.secondary))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild"]), match["case"], match["action"])

    async def callback(self, interaction):
        await _defer(interaction)
        try:
            item = await decide(interaction, self.guild_id, self.case_id, self.action)
            await _private(interaction, f"Case `{item['id']}`: {item['outcome']}.")
        except Exception as error:
            await _private(interaction, str(error) if isinstance(error, (ValueError, hub_store.Unavailable)) else "The decision could not be confirmed. The stored decision can be retried shortly.")


def case_controls(guild_id, item):
    view = discord.ui.View(timeout=None)
    for action in (("accept", "decline") if item["kind"] == "appeal" else ("delete", "timeout", "ban", "ignore")):
        if action == "delete" and not item.get("message_id"):
            continue
        view.add_item(CaseButton(guild_id, item["id"], action))
    return view


class ChallengeModal(discord.ui.Modal, title="Server verification"):
    answer = discord.ui.TextInput(label="Answer", min_length=1, max_length=5)

    def __init__(self, guild_id, user_id):
        super().__init__(timeout=180)
        first, second = random.SystemRandom().randint(2, 12), random.SystemRandom().randint(2, 12)
        self.expected = str(first + second)
        self.answer.label = f"What is {first} + {second}?"
        self.guild_id, self.user_id = guild_id, user_id

    async def on_submit(self, interaction):
        await _defer(interaction)
        try:
            if interaction.guild_id != self.guild_id or interaction.user.id != self.user_id:
                raise ValueError("Open your own verification challenge in this server.")
            now = time.time()
            correct = self.answer.value.strip() == self.expected
            def attempt(value):
                attempts = [stamp for stamp in value.get("attempts", []) if stamp > now - 300]
                if len(attempts) >= 5:
                    raise ValueError("Too many attempts. Try again in five minutes.")
                if not correct:
                    attempts.append(now)
                value["attempts"] = attempts
            await hub_store.mutate(self.guild_id, store.NAMESPACE, f"challenge:{self.user_id}", attempt, default={})
            if not correct:
                raise ValueError("That answer is incorrect. Open Verify to try another challenge.")
            item = await actions.request_release(interaction.guild, "quarantine", self.user_id)
            await _private(interaction, "Verification completed. Your eligible roles have been restored." if item["state"] == "released" else "Your release is queued for moderator review.")
        except Exception as error:
            await _private(interaction, str(error) if isinstance(error, (ValueError, hub_store.Unavailable)) else "Verification could not be completed. Try again shortly.")


class QuarantineView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", custom_id="vivian:quarantine:verify", style=discord.ButtonStyle.primary)
    async def verify(self, interaction, button):
        if interaction.guild is None:
            return await interaction.response.send_message("Open this control in the server verification channel.", ephemeral=True)
        await interaction.response.send_modal(ChallengeModal(interaction.guild_id, interaction.user.id))


def quarantine_notice():
    return QuarantineView()


class RollbackView(discord.ui.View):
    def __init__(self, guild_id, owner_id, plan):
        super().__init__(timeout=120)
        self.guild_id, self.owner_id, self.plan = guild_id, owner_id, plan

    @discord.ui.button(label="Confirm listed unbans", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await _defer(interaction)
        try:
            if interaction.guild_id != self.guild_id or interaction.user.id != self.owner_id:
                raise ValueError("Only the administrator who opened this preview can confirm it.")
            await actions.guard(interaction, permission="ban_members", administrator=True)
            if self.confirm.disabled:
                raise ValueError("This preview has already been used.")
            self.confirm.disabled = True
            restored, skipped = [], []
            for candidate in self.plan[:20]:
                item = await store.punishment(self.guild_id, "ban", candidate["user_id"])
                if not item or item["id"] != candidate["case_id"] or item.get("state") not in {"active", "retry"}:
                    skipped.append(candidate["user_id"])
                    continue
                try:
                    result = await actions.request_release(interaction.guild, "ban", item["user_id"])
                    (restored if result["state"] == "released" else skipped).append(item["user_id"])
                except Exception:
                    skipped.append(item["user_id"])
            await store.audit(self.guild_id, "rollback", actor_id=interaction.user.id, restored=restored, skipped=skipped)
            await _private(interaction, f"Unbanned: {restored or 'none'}. Skipped changed or unavailable cases: {skipped or 'none'}. Deleted messages cannot be restored.")
            self.stop()
        except Exception as error:
            await _private(interaction, str(error) if isinstance(error, (ValueError, hub_store.Unavailable)) else "Rollback could not be completed.")


def register_views():
    if getattr(config.bot, "modsuite_views_registered", False):
        return
    config.bot.add_dynamic_items(AppealButton, CaseButton)
    config.bot.add_view(QuarantineView())
    config.bot.modsuite_views_registered = True
