from __future__ import annotations

import asyncio
from datetime import datetime
import time
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks

from core import community_play, config, database, economy_store, guild_settings, hub_store, persona_store, voice_speech
from utils import helpers


_REGISTERED = False
_VOICE_READY = set()
_VOICE_JOBS = {}
_LAST_ERROR = {}


def _log(key, error):
    now = time.monotonic()
    if now - _LAST_ERROR.get(key, 0) >= 60:
        _LAST_ERROR[key] = now
        print(f"[Features] {key}; error={type(error).__name__}", flush=True)


def _public(channel, guild):
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id or guild.me is None:
        return False
    permissions = channel.permissions_for(guild.me)
    return permissions.view_channel and permissions.send_messages and channel.permissions_for(guild.default_role).view_channel


async def _activity(message, settings):
    if not any(settings.get(key) for key in ("events_enabled", "highlights_enabled", "server_style_enabled")):
        return
    now = time.time()

    def operation(value):
        recent = [item for item in value.get("recent", []) if item["at"] > now - 3600]
        if any(item["id"] == message.id for item in recent):
            return None
        recent.append({"id": message.id, "user": message.author.id, "channel": message.channel.id, "at": now})
        value["recent"] = recent[-300:]
        return [item for item in recent if item["channel"] == message.channel.id and item["at"] > now - 120]

    recent = await hub_store.mutate(message.guild.id, "hub", "activity", operation)
    if not recent or not settings.get("highlights_enabled") or len(recent) < 15 or len({item["user"] for item in recent}) < 5:
        return
    await highlight(message, settings, "A lively conversation is happening")


async def highlight(message, settings=None, title="A message is getting reactions"):
    settings = settings or await guild_settings.get(message.guild.id)
    if not settings.get("highlights_enabled"):
        return
    channel = message.guild.get_channel(int(settings.get("highlights_channel_id") or 0))
    if not _public(channel, message.guild) or not _public(message.channel, message.guild) or channel.id == message.channel.id:
        return
    hour = int(time.time() // 3600)
    key = f"highlight:{message.channel.id}:{hour}"

    def claim(value):
        if value.get("claimed"):
            return False
        value.update(claimed=True, created_at=time.time(), source=message.id, state="posting")
        return True

    if not await hub_store.mutate(message.guild.id, "hub", key, claim):
        return
    try:
        if not _public(message.channel, message.guild) or not _public(channel, message.guild):
            return
        await helpers._send_message_once(channel, f"**{title}**\n{message.jump_url}", community_play.nonce(key))
        await hub_store.put(message.guild.id, "hub", key, {"claimed": True, "state": "sent", "created_at": time.time()})
    except Exception as error:
        _log("highlight publication unconfirmed", error)


async def reaction_added(payload):
    guild = config.bot.get_guild(payload.guild_id) if payload.guild_id else None
    if guild is None:
        return
    try:
        settings = await guild_settings.get(guild.id)
        if not settings.get("highlights_enabled"):
            return
        channel = guild.get_channel(payload.channel_id)
        if not _public(channel, guild):
            return
        message = await channel.fetch_message(payload.message_id)
        if not message.author.bot and sum(reaction.count for reaction in message.reactions) >= 8:
            await highlight(message, settings)
    except Exception as error:
        _log("reaction highlight skipped", error)


async def message_edited(payload):
    if database.db_pool is None or not payload.guild_id:
        return
    data = getattr(payload, "data", {})
    if not any(key in data for key in ("content", "attachments")) or data.get("author", {}).get("bot"):
        return
    guild = config.bot.get_guild(payload.guild_id)
    channel = guild.get_channel_or_thread(payload.channel_id) if guild else None
    if channel is None or guild.me is None or not channel.permissions_for(guild.me).read_message_history:
        return
    try:
        from app import life, modsuite
        from commands import ai_moderation
        message = await channel.fetch_message(payload.message_id)
        if message.author.bot:
            return
        if await modsuite.observe_message(message) or await ai_moderation._moderate_ai_content(message, time.time()):
            await life.store.forget_message(guild.id, message.id)
            return
        await life.observe_message(message)
    except Exception as error:
        _log("message edit observation skipped", error)


async def observe_message(message):
    if message.guild is None or message.author.bot:
        return
    try:
        settings = await guild_settings.get(message.guild.id)
        for label, operation in (
            ("economy reward", lambda: economy_store.reward_message(message)),
            ("character observation", lambda: persona_store.observe(message)),
            ("activity observation", lambda: _activity(message, settings)),
        ):
            try:
                await operation()
            except Exception as error:
                _log(label, error)
        if settings.get("games_enabled"):
            won = await community_play.guess(message)
            if won:
                await _finish_game(message.guild, won, settings)
    except Exception as error:
        _log("message features skipped", error)


def _eligible_voice(member, channel):
    state = member.voice
    return bool(state and state.channel == channel and not state.deaf and not state.self_deaf
                and channel != member.guild.afk_channel
                and sum(not participant.bot for participant in channel.members) >= 2)


async def _voice_checkpoint(member, channel, settings, *, starting=False):
    key = f"voice:{member.id}"
    account_key = f"account:{member.id}"
    now = time.time()
    eligible = bool(channel and settings.get("voice_stats_enabled") and _eligible_voice(member, channel))

    def operation(records):
        previous = records[key]
        account = records[account_key] or economy_store._fresh_account()
        if previous and not starting and previous.get("eligible"):
            seconds = max(0, min(90, int(now - previous.get("checkpoint", now))))
            account["voice_seconds"] = int(account.get("voice_seconds", 0)) + seconds
            pending = int(account.get("voice_progress_seconds", 0)) + seconds
            account.setdefault("branches", {})["voice"] = int(account.get("branches", {}).get("voice", 0)) + pending // 60
            account["voice_progress_seconds"] = pending % 60
            records[account_key] = account
        records[key] = {"channel_id": channel.id, "checkpoint": now, "eligible": eligible} if channel else None

    await hub_store.mutate_many(member.guild.id, "economy", [key, account_key], operation)


async def voice_changed(member, before, after):
    if member.bot:
        return
    try:
        settings = await guild_settings.get(member.guild.id)
        if settings.get("voice_stats_enabled"):
            await _voice_checkpoint(member, after.channel, settings, starting=member.guild.id not in _VOICE_READY)
    except Exception as error:
        _log("voice checkpoint skipped", error)


async def _voice_tick(guild, settings):
    starting = guild.id not in _VOICE_READY
    present = set()
    for channel in guild.voice_channels:
        for member in channel.members:
            if not member.bot:
                present.add(member.id)
                await _voice_checkpoint(member, channel, settings, starting=starting)
    if starting:
        rows = await hub_store.scan(guild.id, "economy", prefix="voice:", limit=5000)
        for row in rows:
            user_id = int(row["key"].split(":", 1)[1])
            if user_id not in present:
                await hub_store.delete(guild.id, "economy", row["key"])
    _VOICE_READY.add(guild.id)


async def _comment(guild, channel, text):
    try:
        await voice_speech.play(guild, channel, text)
    except Exception as error:
        _log("voice commentary skipped", error)
    finally:
        _VOICE_JOBS.pop(guild.id, None)


async def _finish_game(guild, game, settings):
    key = f"game:{game['channel_id']}"
    current = await hub_store.get(guild.id, "community_play", key, {})
    if current.get("token") != game["token"] or current.get("state") != "won":
        return
    await community_play.game_reward(guild.id, current)
    current = await hub_store.get(guild.id, "community_play", key, {})
    if current.get("token") != game["token"] or not current.get("reward_done"):
        return
    if current.get("outcome_posted") or current.get("outcome_state") in {"posting", "uncertain"}:
        return
    channel = guild.get_channel(game["channel_id"])
    if channel is None or guild.me is None or not channel.permissions_for(guild.me).send_messages:
        return

    def claim(value):
        if (value.get("token") != game["token"] or value.get("state") != "won" or not value.get("reward_done")
                or value.get("outcome_posted") or value.get("outcome_state") in {"posting", "uncertain"}):
            return None
        value["outcome_state"] = "posting"
        return dict(value)

    claimed = await hub_store.mutate(guild.id, "community_play", key, claim)
    if claimed is None:
        return
    try:
        text = f"**Word hunt solved**\nMember `{claimed['winner_id']}` found **{claimed['answer']}**."
        if claimed.get("reward_amount"):
            text += f" Reward: {claimed['reward_amount']} {discord.utils.escape_mentions(str(claimed.get('reward_currency', 'sparks')))}."
        await helpers._send_message_once(channel, text, community_play.nonce("winner:" + claimed["token"]))
    except Exception as error:
        def uncertain(value):
            if value.get("token") == game["token"]:
                value["outcome_state"] = "retry" if isinstance(error, (discord.Forbidden, discord.NotFound)) else "uncertain"
        await hub_store.mutate(guild.id, "community_play", key, uncertain)
        _log("game result publication unconfirmed", error)
        return

    def posted(value):
        if value.get("token") == game["token"]:
            value["outcome_posted"] = True
            value["outcome_state"] = "sent"
    await hub_store.mutate(guild.id, "community_play", key, posted)
    voice_channel = guild.get_channel(int(settings.get("voice_channel_id") or 0))
    if (settings.get("voice_commentary_enabled") and settings.get("voice_tts_enabled")
            and isinstance(voice_channel, discord.VoiceChannel) and voice_channel.members and guild.id not in _VOICE_JOBS):
        _VOICE_JOBS[guild.id] = asyncio.create_task(_comment(guild, voice_channel, "The word hunt has a winner. Nicely solved!"))


async def process_guild(guild):
    settings = await guild_settings.get(guild.id)
    if settings.get("voice_stats_enabled"):
        await _voice_tick(guild, settings)
    else:
        _VOICE_READY.discard(guild.id)
    rows = await hub_store.scan(guild.id, "community_play", prefix="game:", limit=500)
    for row in rows:
        game = row["data"]
        try:
            if game.get("state") == "won":
                await _finish_game(guild, game, settings)
            elif game.get("state") in {"active", "preparing"} and game.get("expires_at", 0) <= time.time():
                def expire(value):
                    if (value.get("token") == game["token"] and value.get("state") in {"active", "preparing"}
                            and value.get("expires_at", 0) <= time.time()):
                        value["state"] = "expired"
                await hub_store.mutate(guild.id, "community_play", row["key"], expire)
        except Exception as error:
            _log("game job skipped", error)
    if settings.get("economy_enabled") and settings.get("seasons_enabled"):
        await economy_store.close_seasons(guild.id)
    if settings.get("events_enabled") and settings.get("games_enabled"):
        now = datetime.now(ZoneInfo(settings["timezone"]))
        activity = await hub_store.get(guild.id, "hub", "activity", {})
        active = {item["user"] for item in activity.get("recent", []) if item["at"] > time.time() - 3600}
        channel = guild.get_channel(int(settings.get("events_channel_id") or 0))
        if now.hour == 21 and len(active) >= 3 and _public(channel, guild):
            key = "autoevent:" + now.date().isoformat()

            def claim(value):
                if value.get("claimed"):
                    return False
                value.update(claimed=True, created_at=time.time())
                return True

            if await hub_store.mutate(guild.id, "hub", key, claim):
                from commands.community_play import publish_game
                await publish_game(guild, channel, config.bot.user.id, automatic=True)


@tasks.loop(seconds=60)
async def hub_loop():
    if not hub_store.is_ready():
        try:
            if database.db_pool is None and config.DATABASE_URL:
                await database._open_persistent_storage()
            if not await hub_store.initialize():
                return
        except Exception as error:
            _log("storage reconnect failed", error)
            return
    for guild in config.bot.guilds:
        try:
            await process_guild(guild)
        except Exception as error:
            _log(f"guild {guild.id} jobs skipped", error)


@hub_loop.before_loop
async def before_hub_loop():
    await config.bot.wait_until_ready()


def start():
    global _REGISTERED
    if not _REGISTERED:
        from commands.community_play import JoinGame
        config.bot.add_dynamic_items(JoinGame)
        config.bot.add_listener(voice_changed, "on_voice_state_update")
        config.bot.add_listener(reaction_added, "on_raw_reaction_add")
        config.bot.add_listener(message_edited, "on_raw_message_edit")
        _REGISTERED = True
    if not hub_loop.is_running():
        hub_loop.start()
