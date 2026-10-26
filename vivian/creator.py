"""Creator presentation; the conversational caller owns sending and memory."""

import asyncio

import discord

from core import config as core_config


CREATOR_NAME = "zev1x"
CREATOR_DESCRIPTION = (
    "Он собрал меня для своего Discord-сервера: хотелось не только команд, "
    "но и собеседницу со своим характером. Так появилась Vivian."
)
CREATOR_FALLBACK = f"Мой создатель — {CREATOR_NAME}. {CREATOR_DESCRIPTION}"


def _creator_match_rank(member):
    if getattr(member, "bot", False):
        return None
    for rank, attribute in enumerate(("nick", "display_name", "name", "global_name")):
        value = getattr(member, attribute, None)
        if isinstance(value, str) and value.strip().casefold() == CREATOR_NAME:
            return rank
    return None


def _choose_creator(members):
    matches = []
    for member in members:
        rank = _creator_match_rank(member)
        if rank is not None:
            matches.append((rank, int(getattr(member, "id", 0)), member))
    return min(matches, key=lambda item: item[:2])[2] if matches else None


async def _find_creator_member(guild):
    if guild is None:
        return None
    cached = _choose_creator(getattr(guild, "members", ()))
    if cached is not None:
        return cached
    if not getattr(getattr(core_config.bot, "intents", None), "members", False):
        return None
    try:
        members = await asyncio.wait_for(
            guild.query_members(query=CREATOR_NAME, limit=25, cache=True), timeout=3
        )
    except (discord.DiscordException, asyncio.TimeoutError, AttributeError):
        return None
    return _choose_creator(members)


def _creator_embed_allowed(message):
    guild = getattr(message, "guild", None)
    me = getattr(guild, "me", None)
    permissions_for = getattr(getattr(message, "channel", None), "permissions_for", None)
    if me is None or permissions_for is None:
        return False
    return bool(getattr(permissions_for(me), "embed_links", False))


async def _creator_reply_payload(message):
    """Return a memory-safe text plus an optional Discord card, never send here."""
    if not _creator_embed_allowed(message):
        return CREATOR_FALLBACK, None
    member = await _find_creator_member(getattr(message, "guild", None))
    if member is None:
        return CREATOR_FALLBACK, None
    embed = discord.Embed(
        title="Мой создатель — zev1x",
        description=f"{member.mention}\n\n{CREATOR_DESCRIPTION}",
        color=core_config.VIVIAN_ACCENT,
    )
    avatar_url = getattr(getattr(member, "display_avatar", None), "url", None)
    if avatar_url:
        embed.set_thumbnail(url=str(avatar_url))
    requester = str(getattr(message.author, "display_name", "участник"))[:80]
    embed.set_footer(text=f"Запросил @{requester} // VIVIAN // SYSTEM")
    return CREATOR_FALLBACK, embed
