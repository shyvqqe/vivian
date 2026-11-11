from __future__ import annotations

import asyncio

import discord

from core import config, hub_store


def safe(value, limit=1000):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value or "")))[:limit]


def card(title, description=""):
    return discord.Embed(title=str(title)[:256], description=str(description)[:4000], colour=config.VIVIAN_ACCENT)


def manager(interaction):
    return bool(interaction.guild and getattr(interaction.permissions, "manage_guild", False))


async def defer(interaction, *, ephemeral=True):
    if interaction.guild is None or interaction.channel is None or interaction.user.bot:
        await send(interaction, "Use this command in a server channel.", ephemeral=True)
        return False
    if not getattr(interaction.permissions, "view_channel", False):
        await send(interaction, "You cannot access this channel.", ephemeral=True)
        return False
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
    return True


async def send(interaction, text=None, *, embed=None, view=None, ephemeral=True):
    payload = {"allowed_mentions": discord.AllowedMentions.none()}
    if text is not None:
        payload["content"] = str(text)[:2000]
    if embed is not None:
        payload["embed"] = embed
    if view is not None:
        payload["view"] = view
    if not interaction.response.is_done():
        return await interaction.response.send_message(**payload, ephemeral=ephemeral)
    if interaction.response.type is discord.InteractionResponseType.deferred_channel_message:
        return await interaction.edit_original_response(**payload)
    return await interaction.followup.send(**payload, ephemeral=ephemeral)


async def error(interaction, exception):
    if isinstance(exception, (ValueError, PermissionError, hub_store.Unavailable)):
        return await send(interaction, str(exception))
    print(f"[Features] Action unavailable; error={type(exception).__name__}", flush=True)
    return await send(interaction, "This action could not be completed. Try again shortly.")


async def member(guild, user_id):
    try:
        async with asyncio.timeout(5):
            current = await guild.fetch_member(int(user_id))
        return current if not current.bot else None
    except (discord.DiscordException, TimeoutError, ValueError, TypeError):
        return None
