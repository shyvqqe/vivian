from __future__ import annotations

import asyncio
import logging
import sqlite3

import discord
from discord import app_commands

from core import config, exchange_store as store


exchange = app_commands.Group(name="exchange", description="Find members who can teach what you want to learn.", guild_only=True)
_log = logging.getLogger(__name__)


def _safe(text):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(text)))


async def _finish(interaction, text):
    await interaction.edit_original_response(content=text[:2000], allowed_mentions=discord.AllowedMentions.none())


async def _start(interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None or interaction.channel is None or interaction.user.bot:
        await _finish(interaction, "Use this command as a member of a server channel.")
        return False
    permissions = interaction.channel.permissions_for(interaction.user)
    if not permissions.view_channel:
        await _finish(interaction, "You cannot use this exchange channel.")
        return False
    return True


def _ids(interaction):
    return interaction.guild_id, interaction.channel_id, interaction.user.id


def _summary(profile):
    status = "active" if profile["active"] else "paused"
    return (f"Offers: {', '.join(_safe(tag) for tag in profile['offers'])}\n"
            f"Wants: {', '.join(_safe(tag) for tag in profile['wants'])}\nStatus: {status}")


async def _error(interaction, error):
    if isinstance(error, ValueError):
        return await _finish(interaction, str(error))
    _log.error("Exchange storage unavailable: %s", type(error).__name__)
    await _finish(interaction, "The exchange could not be saved or loaded. Try again later.")


@exchange.command(name="profile", description="Publish skills and identity to matching members in this channel.")
@app_commands.describe(offers="Skills you can teach, separated by commas (up to 8)",
                       wants="Skills you want to learn, separated by commas (up to 8)")
async def profile(interaction: discord.Interaction, offers: str, wants: str):
    if not await _start(interaction):
        return
    try:
        result = await store.save_profile(*_ids(interaction), offers, wants)
        await _finish(interaction, "Vivian · Skill exchange\n" + _summary(result)
                      + "\n\nYour skills and identity are now visible to members running /exchange matches in this channel. "
                      "Matches use the same skill tags, ignoring case. "
                      "Profiles stop matching after 90 days without an update. Use /exchange pause or /exchange remove at any time.")
    except (ValueError, sqlite3.Error, OSError) as error:
        await _error(interaction, error)


@exchange.command(name="show", description="Show your exchange profile for this channel.")
async def show(interaction: discord.Interaction):
    if not await _start(interaction):
        return
    try:
        result = await store.get_profile(*_ids(interaction))
        await _finish(interaction, "Vivian · Skill exchange\n" + _summary(result) if result
                      else "Create your profile with /exchange profile first.")
    except (ValueError, sqlite3.Error, OSError) as error:
        await _error(interaction, error)


@exchange.command(name="pause", description="Hide your profile from matching until you update it.")
async def pause(interaction: discord.Interaction):
    if not await _start(interaction):
        return
    try:
        changed = await store.pause_profile(*_ids(interaction))
        await _finish(interaction, "Your profile is paused. Update it with /exchange profile to resume."
                      if changed else "You do not have an exchange profile in this channel.")
    except (ValueError, sqlite3.Error, OSError) as error:
        await _error(interaction, error)


@exchange.command(name="remove", description="Permanently erase your exchange profile in this channel.")
async def remove(interaction: discord.Interaction):
    if not await _start(interaction):
        return
    try:
        changed = await store.remove_profile(*_ids(interaction))
        await _finish(interaction, "Your exchange profile was erased."
                      if changed else "You do not have an exchange profile in this channel.")
    except (ValueError, sqlite3.Error, OSError) as error:
        await _error(interaction, error)


async def _eligible_member(interaction, user_id):
    member = interaction.guild.get_member(user_id)
    if member is None:
        try:
            async with asyncio.timeout(2):
                member = await interaction.guild.fetch_member(user_id)
        except (discord.DiscordException, TimeoutError):
            return None
    if member.bot or member.id == interaction.user.id:
        return None
    if not interaction.channel.permissions_for(member).view_channel:
        return None
    if isinstance(interaction.channel, discord.Thread) and interaction.channel.is_private():
        if not member.guild_permissions.manage_threads:
            try:
                async with asyncio.timeout(2):
                    await interaction.channel.fetch_member(member.id)
            except (discord.DiscordException, TimeoutError):
                return None
    return member


@exchange.command(name="matches", description="Find reciprocal skill matches among members of this channel.")
async def matches(interaction: discord.Interaction):
    if not await _start(interaction):
        return
    try:
        candidates = await store.find_matches(*_ids(interaction))
        lines = ["Vivian · Reciprocal matches"]
        found = 0
        checked = 0
        async with asyncio.timeout(18):
            for offset in range(0, min(len(candidates), 30), 5):
                batch = candidates[offset:offset + 5]
                members = await asyncio.gather(*(_eligible_member(interaction, item["user_id"]) for item in batch))
                checked += len(batch)
                for candidate, member in zip(batch, members):
                    if member is None:
                        continue
                    give = ", ".join(_safe(tag) for tag in candidate["give"])
                    learn = ", ".join(_safe(tag) for tag in candidate["learn"])
                    name = _safe(member.display_name[:80])
                    entry = f"\n[{name}](https://discord.com/users/{member.id})\nYou teach: {give}\nYou learn: {learn}"
                    if len("\n".join(lines)) + len(entry) + 120 > 2000:
                        break
                    lines.append(entry)
                    found += 1
                    if found >= 5:
                        break
                if found >= 5:
                    break
        if not found:
            lines.append("No available reciprocal matches found. Try broader skill tags or update your profile.")
        if checked < len(candidates):
            lines.append("Showing a limited selection of currently accessible members.")
        await _finish(interaction, "\n".join(lines))
    except TimeoutError:
        await _finish(interaction, "Member access checks took too long. Try /exchange matches again.")
    except (ValueError, sqlite3.Error, OSError) as error:
        await _error(interaction, error)


config.bot.tree.add_command(exchange)
