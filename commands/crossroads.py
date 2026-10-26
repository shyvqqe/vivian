from __future__ import annotations

import logging
import time

import discord
from discord import app_commands

from core import config, crossroads_store as store


crossroads = app_commands.Group(name="crossroads", description="Sealed decisions with a scheduled outcome review", guild_only=True)
_log = logging.getLogger(__name__)


def _safe(value):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


def _staff(interaction):
    return bool(getattr(interaction.permissions, "manage_guild", False))


async def _ack(interaction, *, publish=False):
    if (interaction.guild is None or interaction.guild_id is None or interaction.channel_id is None
            or interaction.channel is None or interaction.user.bot):
        await interaction.response.send_message("Use this command as a member in a server channel.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
        return False
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not interaction.permissions.view_channel:
        await _reply(interaction, text="You cannot use decisions in this channel.")
        return False
    if publish:
        permissions = interaction.app_permissions
        can_send = (permissions.send_messages_in_threads if isinstance(interaction.channel, discord.Thread)
                    else permissions.send_messages)
        if not permissions.view_channel or not can_send or not permissions.embed_links:
            await _reply(interaction, text="Vivian needs View Channel, Send Messages and Embed Links here to publish results.")
            return False
    return True


async def _reply(interaction, *, text=None, embed=None):
    return await interaction.edit_original_response(content=text, embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _failed(interaction, error):
    if isinstance(error, ValueError):
        return await _reply(interaction, text=str(error))
    _log.error("Crossroads operation failed: %s", type(error).__name__)
    return await _reply(interaction, text="This decision could not be saved or loaded. Try again shortly.")


def _ballot_embed(item):
    lines = [f"**{index}.** {_safe(option)}" for index, option in enumerate(item["options"], 1)]
    embed = discord.Embed(title=f"Vivian / Crossroads #{item['id']}", color=config.VIVIAN_ACCENT,
                          description=_safe(item["question"]) + "\n\n" + "\n".join(lines))
    embed.add_field(name="Expected outcome", value=_safe(item["expectation"]), inline=False)
    embed.add_field(name="Ballots open", value=f"<t:{int(item['closes_at'])}:F>")
    embed.add_field(name="Outcome review", value=f"<t:{int(item['review_at'])}:F>")
    embed.set_footer(text=f"Use /crossroads vote decision_id:{item['id']} choice:1 confidence:3 in this channel.")
    return embed


@crossroads.command(name="create", description="Create a sealed ballot and set a later outcome review")
@app_commands.describe(question="The decision to make", options="2 to 6 options separated by |", expectation="What result will tell you the decision worked", minutes="Minutes until ballots are revealed", review_hours="Hours after voting closes before the outcome can be recorded")
async def create(interaction: discord.Interaction, question: app_commands.Range[str, 1, 200],
                 options: app_commands.Range[str, 3, 605], expectation: app_commands.Range[str, 1, 300],
                 minutes: app_commands.Range[int, 1, 43200] = 60,
                 review_hours: app_commands.Range[int, 1, 2160] = 24):
    if not await _ack(interaction):
        return
    try:
        item = await store.create(interaction.guild_id, interaction.channel_id, interaction.user.id,
                                  question, options.split("|"), expectation, minutes, review_hours)
        await _reply(interaction, embed=_ballot_embed(item), text=f"Share #{item['id']} with participants. They can view the options with /crossroads ballot.")
    except Exception as error:
        await _failed(interaction, error)


@crossroads.command(name="ballot", description="View a decision and its options without revealing any votes")
async def ballot(interaction: discord.Interaction, decision_id: app_commands.Range[int, 1]):
    if not await _ack(interaction):
        return
    try:
        item = await store.ballot(interaction.guild_id, interaction.channel_id, decision_id)
        await _reply(interaction, embed=_ballot_embed(item))
    except Exception as error:
        await _failed(interaction, error)


@crossroads.command(name="vote", description="Cast or replace a private ballot before the deadline")
async def vote(interaction: discord.Interaction, decision_id: app_commands.Range[int, 1],
               choice: app_commands.Range[int, 1, 6], confidence: app_commands.Range[int, 1, 5] = 3):
    if not await _ack(interaction):
        return
    try:
        selected = await store.vote(interaction.guild_id, interaction.channel_id, decision_id,
                                    interaction.user.id, choice, confidence)
        await _reply(interaction, text=f"Ballot saved for #{decision_id}: {_safe(selected)}. Confidence: {confidence}/5. Results stay sealed until the deadline.")
    except Exception as error:
        await _failed(interaction, error)


@crossroads.command(name="results", description="Publish anonymous results and the outcome after the deadline")
async def results(interaction: discord.Interaction, decision_id: app_commands.Range[int, 1]):
    if not await _ack(interaction, publish=True):
        return
    try:
        item = await store.results(interaction.guild_id, interaction.channel_id, decision_id)
        lines = []
        for row in item["tally"]:
            confidence = f"{row['confidence']:.1f}/5" if row["confidence"] is not None else "no votes"
            lines.append(f"**{_safe(row['option'])}** — {row['votes']} vote(s), confidence {confidence}")
        embed = discord.Embed(title=f"Vivian / Crossroads #{decision_id}", color=config.VIVIAN_ACCENT,
                              description=_safe(item["question"]) + "\n\n" + "\n".join(lines))
        embed.add_field(name="Expected outcome", value=_safe(item["expectation"]), inline=False)
        observed = _safe(item["outcome"]) if item["outcome"] else f"Review opens <t:{int(item['review_at'])}:F>."
        for offset in range(0, len(observed), 1000):
            embed.add_field(name="Observed outcome" if offset == 0 else "Observed outcome (continued)",
                            value=observed[offset:offset + 1000], inline=False)
        embed.set_footer(text="Each member has one equal vote. Individual ballots remain private.")
    except Exception as error:
        await _failed(interaction, error)
        return
    await _reply(interaction, text="Preparing channel results…")
    try:
        await interaction.followup.send(embed=embed, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())
    except discord.DiscordException as error:
        _log.error("Crossroads results publication failed: %s", type(error).__name__)
        await _reply(interaction, text="The results are available, but Discord could not confirm publication in this channel. "
                     "Check the channel before retrying /crossroads results.")
        return
    await _reply(interaction, text="Results published in this channel.")


@crossroads.command(name="outcome", description="Record the observed result once the review date arrives")
async def outcome(interaction: discord.Interaction, decision_id: app_commands.Range[int, 1],
                  observed: app_commands.Range[str, 1, 1000]):
    if not await _ack(interaction):
        return
    try:
        await store.record_outcome(interaction.guild_id, interaction.channel_id, decision_id,
                                   interaction.user.id, observed, staff=_staff(interaction))
        await _reply(interaction, text=f"Outcome saved. Use /crossroads results decision_id:{decision_id} to publish the comparison.")
    except Exception as error:
        await _failed(interaction, error)


@crossroads.command(name="history", description="List recent channel decisions and pending outcome reviews")
async def history(interaction: discord.Interaction):
    if not await _ack(interaction):
        return
    try:
        items = await store.history(interaction.guild_id, interaction.channel_id)
        lines = []
        for item in items:
            if item["closes_at"] > time.time():
                state = f"sealed until <t:{int(item['closes_at'])}:R>"
            elif item["outcome"] is not None:
                state = "outcome recorded"
            else:
                state = f"review <t:{int(item['review_at'])}:R>"
            lines.append(f"**#{item['id']}** {_safe(item['question'])[:200]} · {state}")
        embed = discord.Embed(title="Vivian / Crossroads", color=config.VIVIAN_ACCENT,
                              description="\n".join(lines) or "No decisions in this channel yet.")
        await _reply(interaction, embed=embed)
    except Exception as error:
        await _failed(interaction, error)


@crossroads.command(name="cancel", description="Cancel your open decision and erase its ballots")
async def cancel(interaction: discord.Interaction, decision_id: app_commands.Range[int, 1]):
    if not await _ack(interaction):
        return
    try:
        await store.cancel(interaction.guild_id, interaction.channel_id, decision_id,
                           interaction.user.id, staff=_staff(interaction))
        await _reply(interaction, text="Decision canceled and ballots erased.")
    except Exception as error:
        await _failed(interaction, error)


config.bot.tree.add_command(crossroads)
