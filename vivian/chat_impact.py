"""Called only after both moderation passes approved a human chat message."""
from __future__ import annotations
import asyncio
from datetime import timezone
import discord
from discord.ext import tasks
from core import config
from core import discord_api
from core import chat_impact_state as state
from core import chat_impact_store as store

OBSERVED_GUILDS = set()
REPLY_FETCH_LIMIT = 64
REPLY_FETCH_TIMEOUT = 2.0
REPLY_FETCH_TASKS = {}


async def _fetch_reply_target(channel, mid):
    try:
        return await channel.fetch_message(mid)
    except discord.DiscordException:
        return None
    except Exception as error:
        print(f"[Chat impact] Reply lookup failed; error={type(error).__name__}", flush=True)
        return None


def _reply_fetch_finished(key, task):
    if REPLY_FETCH_TASKS.get(key) is task:
        REPLY_FETCH_TASKS.pop(key, None)
    # A caller may have stopped waiting. Retrieve the exception so a failed
    # background GET never becomes an unhandled task exception.
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def can_view_chat(guild_id: int, user_id: int) -> bool:
    guild = config.bot.get_guild(guild_id)
    channel = guild.get_channel(config.CHAT_CHANNEL_ID) if guild else None
    member = guild.get_member(user_id) if guild else None
    if channel is None or member is None:
        return False
    permissions = channel.permissions_for(member)
    return bool(permissions.view_channel and permissions.read_message_history)


async def _reply_target(message):
    reference = getattr(message, "reference", None)
    if reference is None or getattr(reference, "type", discord.MessageReferenceType.reply) != discord.MessageReferenceType.reply:
        return None
    if getattr(reference, "channel_id", None) != config.CHAT_CHANNEL_ID:
        return None
    resolved = getattr(reference, "resolved", None)
    if isinstance(resolved, discord.DeletedReferencedMessage):
        return None
    if resolved is not None:
        return resolved
    mid = getattr(reference, "message_id", None)
    bot_member = message.guild.me
    if not mid or bot_member is None:
        return None
    permissions = message.channel.permissions_for(bot_member)
    if not permissions.view_channel or not permissions.read_message_history:
        return None
    key = (message.channel.id, mid)
    task = REPLY_FETCH_TASKS.get(key)
    if task is None:
        route = discord.http.Route(
            "GET", "/channels/{channel_id}/messages/{message_id}",
            channel_id=message.channel.id, message_id=mid,
        )
        if (discord_api.is_paused() or discord_api.is_route_paused(route)
                or len(REPLY_FETCH_TASKS) >= REPLY_FETCH_LIMIT):
            return None
        task = asyncio.create_task(_fetch_reply_target(message.channel, mid))
        REPLY_FETCH_TASKS[key] = task
        task.add_done_callback(lambda done, key=key: _reply_fetch_finished(key, done))
    try:
        # Cancelling discord.py during its global Retry-After sleep can leave
        # the SDK's global gate closed. Bound this caller, not the owned GET.
        async with asyncio.timeout(REPLY_FETCH_TIMEOUT):
            return await asyncio.shield(task)
    except (discord.DiscordException, TimeoutError):
        return None


async def observe_approved_message(message) -> None:
    guild = message.guild
    if guild is None or message.author.bot or message.channel.id != config.CHAT_CHANNEL_ID:
        return
    bot_member = guild.me
    if bot_member is None or not message.channel.permissions_for(bot_member).view_channel:
        return
    if len(state.PENDING) >= state.PENDING_LIMIT:
        await store.flush_pending()
    target = await _reply_target(message)
    timestamp = message.created_at.timestamp()
    async with state.LOCKS[guild.id]:
        if guild.id not in OBSERVED_GUILDS:
            # Offline time is not evidence of silence. An unfinished five-minute
            # candidate may resume, but the first fresh message cannot seed one.
            state.window_for(guild.id)["last_at"] = None
            OBSERVED_GUILDS.add(guild.id)
        state.observe_activity(guild.id, message.id, message.author.id, timestamp)
        if target is not None and getattr(target, "guild", None) is not None:
            target_author = getattr(target, "author", None)
            target_channel = getattr(target, "channel", None)
            if (target.guild.id == guild.id and getattr(target_channel, "id", None) == config.CHAT_CHANNEL_ID
                    and target_author is not None and not target_author.bot and target_author.id != message.author.id
                    and target.id < message.id):
                newcomer = guild.get_member(target_author.id)
                joined = getattr(newcomer, "joined_at", None)
                if joined is not None:
                    joined = joined.replace(tzinfo=timezone.utc) if joined.tzinfo is None else joined
                    age = timestamp - joined.timestamp()
                    if 0 <= age <= state.NEWCOMER_SECONDS and target.created_at.timestamp() >= joined.timestamp():
                        state.add_event(guild.id, "newcomer", message.author.id, target.id, timestamp)
        await store.flush_pending()


@tasks.loop(seconds=60)
async def retry_chat_impact_writes():
    await store.flush_pending()


@retry_chat_impact_writes.before_loop
async def before_retry_chat_impact_writes():
    await config.bot.wait_until_ready()
