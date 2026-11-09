from __future__ import annotations

import asyncio
from collections import Counter
from datetime import timedelta
import logging
import time

import discord
from discord.ext import tasks

from core import config, guild_settings, life_store as store


_log = logging.getLogger(__name__)
_lock = asyncio.Lock()


def safe(value, length=1500):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))[:length]


async def channel_for(guild, channel_id):
    if not channel_id:
        return None
    channel = guild.get_channel_or_thread(int(channel_id))
    if channel is None:
        try:
            channel = await guild.fetch_channel(int(channel_id))
        except discord.DiscordException:
            return None
    return channel if getattr(getattr(channel, "guild", None), "id", None) == guild.id else None


async def readable(channel, member):
    if channel is None or member is None:
        return False
    permissions = channel.permissions_for(member)
    if not permissions.view_channel or not permissions.read_message_history:
        return False
    if isinstance(channel, discord.Thread) and channel.is_private() and not permissions.manage_threads:
        try:
            await channel.fetch_member(member.id)
        except discord.DiscordException:
            return False
    return True


def publishable(channel, guild):
    if channel is None or guild.me is None:
        return False
    permissions = channel.permissions_for(guild.me)
    send = permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages
    return bool(permissions.view_channel and send and permissions.embed_links)


def private_log(channel, guild):
    if channel is None or isinstance(channel, discord.Thread) or not publishable(channel, guild):
        return False
    if channel.permissions_for(guild.default_role).view_channel:
        return False
    for target, overwrite in channel.overwrites.items():
        if overwrite.view_channel is not True:
            continue
        permissions = getattr(target, "guild_permissions", None)
        if permissions is None:
            permissions = getattr(target, "permissions", None)
        if getattr(target, "id", None) == getattr(guild.me, "id", None):
            continue
        tags = getattr(target, "tags", None)
        if tags and getattr(tags, "bot_id", None) == guild.me.id:
            continue
        if not permissions or not (permissions.administrator or permissions.manage_guild or permissions.moderate_members):
            return False
    return True


async def current_quote(guild, record, *, member=None, public=False, settings=None):
    settings = settings or await guild_settings.get(guild.id)
    if (not settings.get("journal_enabled") or record.get("channel_id") not in settings.get("archive_channel_ids", [])
            or not (await store.preference(guild.id, record.get("author_id"))).get("archive")):
        return None
    channel = await channel_for(guild, record.get("channel_id"))
    if not await readable(channel, guild.me):
        return None
    if public:
        if isinstance(channel, discord.Thread) and channel.is_private():
            return None
        if not channel.permissions_for(guild.default_role).view_channel:
            return None
    elif not await readable(channel, member):
        return None
    try:
        message = await channel.fetch_message(record["message_id"])
    except discord.NotFound:
        await store.forget_message(guild.id, record["message_id"])
        return None
    except discord.DiscordException:
        return None
    if message.author.id != record["author_id"] or message.author.bot or not message.content.strip():
        return None
    if guild.get_member(message.author.id) is None:
        return None
    return dict(record, content=message.content[:2000], link=message.jump_url,
                score=sum(reaction.count for reaction in message.reactions))


async def observe_message(message):
    if message.guild is None or message.author.bot or not message.content.strip():
        return
    settings = await guild_settings.get(message.guild.id)
    if not settings.get("journal_enabled") or message.channel.id not in settings.get("archive_channel_ids", []):
        return
    await store.archive_message(message.guild.id, {
        "message_id": message.id, "channel_id": message.channel.id, "author_id": message.author.id,
        "created_at": int(message.created_at.timestamp()), "content": message.content[:2000],
        "score": sum(reaction.count for reaction in message.reactions),
    })


async def message_deleted(payload):
    if payload.guild_id:
        await store.forget_message(payload.guild_id, payload.message_id)


async def bulk_deleted(payload):
    if payload.guild_id:
        for message_id in payload.message_ids:
            await store.forget_message(payload.guild_id, message_id)


async def _reaction(payload):
    if not payload.guild_id:
        return
    record = await store.db.get(payload.guild_id, store.ARCHIVE, str(payload.message_id))
    if record is None:
        return
    guild = config.bot.get_guild(payload.guild_id)
    if guild is None:
        return
    channel = await channel_for(guild, payload.channel_id)
    if not await readable(channel, guild.me):
        return
    try:
        await observe_message(await channel.fetch_message(payload.message_id))
    except discord.NotFound:
        await store.forget_message(payload.guild_id, payload.message_id)
    except discord.DiscordException:
        pass


reaction_added = _reaction
reaction_removed = _reaction


async def memory_candidates(guild, settings, today):
    records = await store.memories_records(guild.id, settings, today)
    records.sort(key=lambda item: (-item.get("score", 0), -item["created_at"]))
    valid = []
    for record in records[:30]:
        quote = await current_quote(guild, record, public=True, settings=settings)
        if quote:
            valid.append(quote)
    return sorted(valid, key=lambda item: (-item["score"], -item["created_at"]))[:3]


def memory_embed(quotes):
    embed = discord.Embed(title="Vivian · On this day", color=config.VIVIAN_ACCENT)
    for quote in quotes:
        embed.add_field(name=f"{quote['score']} reactions · earlier years",
                        value=safe(quote["content"], 800) + f"\n[Original message]({quote['link']})", inline=False)
    if not quotes:
        embed.description = "No eligible memories have been saved for this calendar date yet. Future messages require each author's /memories consent."
    return embed


async def newspaper(guild, settings, first, last, title):
    records = [record for record in await store.archive_records(guild.id)
               if first <= store.local_now(settings, record["created_at"]).date() < last]
    valid = []
    for record in sorted(records, key=lambda item: (-item.get("score", 0), -item["created_at"]))[:100]:
        quote = await current_quote(guild, record, public=True, settings=settings)
        if quote:
            valid.append(quote)
    counts = Counter(record["author_id"] for record in valid)
    embed = discord.Embed(title=title, color=config.VIVIAN_ACCENT,
                          description=f"{first.isoformat()} — {(last - timedelta(days=1)).isoformat()}\n"
                          f"{len(valid)} accessible saved messages inspected. This is an opt-in sample, not a complete server history.")
    leaders = []
    for user_id, count in counts.most_common(3):
        member = guild.get_member(user_id)
        if member:
            leaders.append(f"{safe(member.display_name, 60)} · {count} saved messages")
    embed.add_field(name="Most active in the saved sample", value="\n".join(leaders) or "No eligible saved messages.", inline=False)
    joined = sum(first <= member.joined_at.astimezone(store.local_now(settings).tzinfo).date() < last
                 for member in guild.members if not member.bot and member.joined_at)
    embed.add_field(name="New members", value=str(joined) + " currently present members joined during this period.", inline=False)
    for quote in sorted(valid, key=lambda item: -item["score"])[:3]:
        embed.add_field(name=f"{quote['score']} reactions", value=safe(quote["content"], 600) + f"\n[Read]({quote['link']})", inline=False)
    return embed


async def _send_job(guild, key, channel, embed):
    if not publishable(channel, guild) or not await store.claim_job(guild.id, key):
        return
    await store.mark_delivery(guild.id, key)
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(), nonce=key[-25:])
    except (discord.Forbidden, discord.NotFound):
        await store.finish_job(guild.id, key)
    except Exception:
        await store.finish_job(guild.id, key, uncertain=True)
    else:
        await store.finish_job(guild.id, key, success=True)


async def _expire_stories(guild):
    now = time.time()
    for row in await store.due_records(guild.id, "story", now=now):
        item = row["data"]
        if item.get("expires_at", now + 1) > now or item.get("status") in {"deleted", "failed"}:
            continue
        key = f"story-expiry:{row['key']}"
        if not await store.claim_job(guild.id, key):
            continue
        try:
            channel = await channel_for(guild, item.get("channel_id"))
            if not item.get("message_id"):
                item.update(status="failed", viewers={})
                await store.save_story(guild.id, item["id"], item)
                await store.finish_job(guild.id, key, success=True)
                continue
            if channel is None:
                raise ValueError("Story channel or message unavailable")
            await channel.get_partial_message(item["message_id"]).delete()
        except discord.NotFound:
            pass
        except Exception:
            await store.finish_job(guild.id, key)
            continue
        item.update(status="deleted", viewers={})
        await store.save_story(guild.id, item["id"], item)
        if item.get("message_id"):
            await store.db.delete(guild.id, store.NAMESPACE, f"story-message:{item['message_id']}")
        await store.finish_job(guild.id, key, success=True)


async def _role_cleanup(guild):
    for row in await store.due_records(guild.id, "birthday-role"):
        lease = row["data"]
        if lease.get("expires_at", 0) > time.time() or not lease.get("owned"):
            continue
        key = f"role-cleanup:{row['key']}"
        if not await store.claim_job(guild.id, key):
            continue
        try:
            member = await guild.fetch_member(lease["user_id"])
            role = guild.get_role(lease["role_id"])
            if role and role in member.roles:
                await member.remove_roles(role, reason="Vivian birthday role expired")
        except discord.NotFound:
            pass
        except Exception:
            await store.finish_job(guild.id, key)
            continue
        await store.db.delete(guild.id, store.NAMESPACE, row["key"])
        await store.finish_job(guild.id, key, success=True)


async def _birthday_jobs(guild, settings, now):
    if not settings.get("birthdays_enabled") or now.hour < settings.get("birthday_hour", 10):
        return
    channel = await channel_for(guild, settings.get("birthday_channel_id"))
    if not publishable(channel, guild):
        return
    for item in await store.birthdays(guild.id, now.date()):
        if store.birthday_date(now.year, item["month"], item["day"]) != now.date():
            continue
        member = guild.get_member(item["user_id"])
        if member is None or member.bot:
            continue
        key = f"birthday:{item['user_id']}:{now.date().isoformat()}"
        embed = discord.Embed(title="Happy birthday!", description=f"Happy birthday, {safe(member.display_name, 100)}!", color=config.VIVIAN_ACCENT)
        await _send_job(guild, key, channel, embed)
        role = guild.get_role(settings.get("birthday_role_id") or 0)
        role_key = f"birthday-role:{member.id}:{now.date().isoformat()}"
        if (role and role not in member.roles and guild.me.guild_permissions.manage_roles
                and role < guild.me.top_role and not role.managed and not role.is_default()
                and not await store.db.get(guild.id, store.NAMESPACE, role_key)):
            lease = {"user_id": member.id, "role_id": role.id, "owned": True, "expires_at": int(time.time() + 86400)}
            await store.db.put(guild.id, store.NAMESPACE, role_key, lease)
            try:
                await member.add_roles(role, reason="Vivian birthday for 24 hours")
            except discord.DiscordException:
                await store.db.delete(guild.id, store.NAMESPACE, role_key)


async def process_guild(guild):
    settings = await guild_settings.get(guild.id)
    now = store.local_now(settings)
    await _expire_stories(guild)
    await _role_cleanup(guild)
    await _birthday_jobs(guild, settings, now)
    if now.hour < 10:
        return
    if settings.get("memories_enabled") and settings.get("journal_enabled"):
        key = f"memories:{now.date().isoformat()}"
        if await store.job_due(guild.id, key):
            quotes = await memory_candidates(guild, settings, now.date())
            if not quotes and await store.claim_job(guild.id, key):
                await store.finish_job(guild.id, key, success=True)
        else:
            quotes = []
        if quotes:
            await _send_job(guild, f"memories:{now.date().isoformat()}",
                            await channel_for(guild, settings.get("memories_channel_id")), memory_embed(quotes))
    if settings.get("timeline_enabled") and settings.get("journal_enabled"):
        channel = await channel_for(guild, settings.get("journal_channel_id"))
        if (now.weekday() == settings.get("weekly_day", 6) and now.hour >= settings.get("weekly_hour", 18)
                and await store.job_due(guild.id, f"gazette:{now.date().isoformat()}")):
            embed = await newspaper(guild, settings, now.date() - timedelta(days=6), now.date() + timedelta(days=1), "Vivian · Sunday Gazette")
            await _send_job(guild, f"gazette:{now.date().isoformat()}", channel, embed)
        if now.day == settings.get("monthly_day", 1) and now.hour >= settings.get("monthly_hour", 18):
            previous = now.date().replace(day=1) - timedelta(days=1)
            if await store.job_due(guild.id, f"timeline:{previous:%Y-%m}"):
                embed = await newspaper(guild, settings, previous.replace(day=1), now.date().replace(day=1), "Vivian · Monthly Timeline")
                await _send_job(guild, f"timeline:{previous:%Y-%m}", channel, embed)
    if await store.claim_job(guild.id, f"archive-prune:{now.date().isoformat()}"):
        await store.prune(guild.id)
        await store.finish_job(guild.id, f"archive-prune:{now.date().isoformat()}", success=True)


@tasks.loop(minutes=1)
async def life_loop():
    if _lock.locked():
        return
    async with _lock:
        for guild in config.bot.guilds:
            try:
                await process_guild(guild)
            except Exception as error:
                _log.warning("Community life job unavailable: %s", type(error).__name__)


@life_loop.before_loop
async def before_life_loop():
    await config.bot.wait_until_ready()


def start():
    from commands.life import register_views
    register_views()
    if not life_loop.is_running():
        life_loop.start()
