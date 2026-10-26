from __future__ import annotations

import math
import sqlite3

import discord
from discord import app_commands

from core import capsule_store as store, config


capsule = app_commands.Group(name="capsule", description="Shared time capsules unlocked by time and participation.", guild_only=True)


def _safe(value):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))


async def _begin(interaction, *, publish=False):
    if (interaction.guild_id is None or interaction.channel_id is None
            or interaction.guild is None or interaction.channel is None):
        await interaction.response.send_message("Use this command in a server channel.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
        return False
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.user.bot or not interaction.permissions.view_channel:
        await _notice(interaction, "You cannot use capsules in this channel.")
        return False
    if publish:
        permissions = interaction.app_permissions
        can_send = (permissions.send_messages_in_threads if isinstance(interaction.channel, discord.Thread)
                    else permissions.send_messages)
        if not permissions.view_channel or not can_send or not permissions.embed_links:
            await _notice(interaction, "Vivian needs View Channel, Send Messages and Embed Links here to publish capsules.")
            return False
    return True


async def _notice(interaction, content):
    await interaction.edit_original_response(content=content, allowed_mentions=discord.AllowedMentions.none())


async def _operation(interaction, awaitable):
    try:
        return await awaitable
    except ValueError as error:
        await _notice(interaction, _safe(error))
    except (sqlite3.Error, OSError):
        await _notice(interaction, "Capsule storage is unavailable. Please try again later.")
    return None


@capsule.command(name="create", description="Create a capsule that requires both a deadline and a contributor quorum.")
@app_commands.describe(title="Capsule title", minutes="Delay in minutes", quorum="Unique contributors required")
async def create(interaction: discord.Interaction, title: app_commands.Range[str, 1, 120],
                 minutes: app_commands.Range[int, 1, 43200], quorum: app_commands.Range[int, 1, 50] = 2):
    if not await _begin(interaction, publish=True):
        return
    value = await _operation(interaction, store.create(interaction.guild_id, interaction.channel_id,
                                                     interaction.user.id, title, minutes, quorum))
    if value is not None:
        await _notice(interaction, f"Vivian · Capsule **#{value['id']}** created: {_safe(value['title'])}\n"
                      f"Opens <t:{value['opens_at']}:F> after **{quorum}** unique contributions.\n"
                      f"Members can privately add a note with `/capsule add id:{value['id']}`. "
                      "Use `/capsule open` in this channel to reveal it when ready. Notes are attributed at reveal.")


@capsule.command(name="add", description="Privately add or replace your note before a capsule is revealed.")
async def add(interaction: discord.Interaction, id: app_commands.Range[int, 1],
              note: app_commands.Range[str, 1, 1000]):
    if not await _begin(interaction):
        return
    value = await _operation(interaction, store.contribute(interaction.guild_id, interaction.channel_id,
                                                         id, interaction.user.id, note))
    if value is not None:
        await _notice(interaction, f"Your note is saved in capsule **#{id}**. It will be attributed to you when revealed "
                      f"in this channel. Contributors: **{value['contributors']}/{value['quorum']}**. "
                      "Submitting again replaces your note.")


@capsule.command(name="open", description="Reveal a ready capsule here, or privately check its remaining conditions.")
@app_commands.describe(page="Two complete notes per page")
async def open_command(interaction: discord.Interaction, id: app_commands.Range[int, 1],
                       page: app_commands.Range[int, 1, 25] = 1):
    if not await _begin(interaction, publish=True):
        return
    value = await _operation(interaction, store.open_capsule(interaction.guild_id, interaction.channel_id, id))
    if value is None:
        return
    if not value["ready"]:
        await _notice(interaction, f"Capsule **#{id}** is sealed until <t:{value['opens_at']}:F> and "
                      f"**{value['quorum']}** unique contributors. Current contributions: **{value['contributors']}**.")
        return
    pages = max(1, math.ceil(len(value["notes"]) / 2))
    if page > pages:
        await _notice(interaction, f"This capsule has **{pages}** pages. Choose a page from 1 to {pages}.")
        return
    embed = discord.Embed(title=f"Vivian · Capsule #{id}", description=_safe(value["title"]), color=0xD58BFF)
    for note in value["notes"][(page - 1) * 2:page * 2]:
        text = _safe(note["note"])
        for index in range(0, len(text), 1000):
            label = f"Member {note['user_id']}" + (" · continued" if index else "")
            embed.add_field(name=label, value=text[index:index + 1000], inline=False)
    embed.set_footer(text=f"Page {page}/{pages} · /capsule open id:{id} page:<number>")
    await _notice(interaction, f"Capsule **#{id}** is unlocked. Publishing page **{page}/{pages}** in this channel.")
    try:
        await interaction.followup.send(embed=embed, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        await _notice(interaction, "The capsule is unlocked, but Discord could not publish this page. Run `/capsule open` again.")
        return
    await _notice(interaction, f"Published capsule **#{id}**, page **{page}/{pages}**, in this channel.")


@capsule.command(name="list", description="Privately list the ten newest capsules in this channel.")
async def list_command(interaction: discord.Interaction):
    if not await _begin(interaction):
        return
    values = await _operation(interaction, store.list_capsules(interaction.guild_id, interaction.channel_id))
    if values is None:
        return
    lines = []
    for value in values:
        state = "cancelled" if value["cancelled_at"] is not None else "open" if value["revealed_at"] is not None else "sealed"
        lines.append(f"**#{value['id']}** · {_safe(value['title'])} · {state}\n"
                     f"<t:{value['opens_at']}:R> · {value['contributors']}/{value['quorum']} contributors")
    embed = discord.Embed(title="Vivian · Channel capsules", description="\n\n".join(lines) or "No capsules in this channel.",
                          color=0xD58BFF)
    await interaction.edit_original_response(embed=embed, allowed_mentions=discord.AllowedMentions.none())


@capsule.command(name="cancel", description="Cancel your sealed capsule and delete its stored notes.")
async def cancel(interaction: discord.Interaction, id: app_commands.Range[int, 1]):
    if not await _begin(interaction):
        return
    value = await _operation(interaction, store.cancel(
        interaction.guild_id, interaction.channel_id, id, interaction.user.id,
        moderator=bool(getattr(interaction.permissions, "manage_guild", False)),
    ))
    if value is not None:
        await _notice(interaction, f"Capsule **#{id}** cancelled. Its stored notes were deleted.")


config.bot.tree.add_command(capsule)
