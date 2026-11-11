from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from types import SimpleNamespace

import discord

from commands import music_audio, music_panels, music_prefix
from core import database, music_state
from utils import music as music_utils


log = logging.getLogger(__name__)
_ACTIONS = {"play", "pause", "resume", "skip", "stop"}
_EXPIRE = """UPDATE vivian_web.music_requests
    SET status='rejected', result='Request expired. Submit it again.', processed_at=now()
    WHERE (status='pending' AND created_at <= now()-interval '2 minutes')
       OR (status='working' AND COALESCE(processed_at,created_at) <= now()-interval '5 minutes')"""
_CLAIM = """UPDATE vivian_web.music_requests SET status='working',processed_at=now()
    WHERE id=(SELECT id FROM vivian_web.music_requests
        WHERE status='pending' AND created_at > now()-interval '2 minutes'
        ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
    RETURNING id,guild_id,discord_id,action,query"""
_COMPLETE = """UPDATE vivian_web.music_requests SET status=%s,result=%s,processed_at=now()
    WHERE id=%s AND status='working'"""
_STATUS = """INSERT INTO vivian_web.music_status (guild_id,title,artist,duration_seconds,is_playing,is_paused,updated_at)
    VALUES (%s,%s,%s,%s,%s,%s,now())
    ON CONFLICT(guild_id) DO UPDATE SET title=excluded.title,artist=excluded.artist,
    duration_seconds=excluded.duration_seconds,is_playing=excluded.is_playing,is_paused=excluded.is_paused,updated_at=now()"""


def enabled():
    return os.getenv("WEB_MUSIC_BRIDGE_ENABLED", "").strip().lower() == "true"


def start(bot):
    previous = getattr(bot, "_web_music_bridge_task", None)
    if not enabled() or (previous is not None and not previous.done()):
        return
    if database.db_pool is None:
        log.warning("Music bridge requires PostgreSQL storage")
        return
    bot._web_music_bridge_task = asyncio.create_task(run(bot), name="vivian-music-bridge")
    print("[Vivian] Music bridge task started", flush=True)


async def _db(query, args=(), *, fetch=False):
    pool = database.db_pool
    if pool is None:
        raise RuntimeError("Music bridge requires PostgreSQL storage")
    async with asyncio.timeout(8), pool.connection() as connection:
        cursor = await connection.execute(query, args)
        return await cursor.fetchone() if fetch else None


async def _execute(bot, job):
    _, guild_id, user_id, action, query = job
    if action not in _ACTIONS:
        return "rejected", "Unsupported music command."
    guild = bot.get_guild(guild_id)
    if not guild:
        return "rejected", "Бот не состоит на этом сервере."
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.DiscordException:
        return "rejected", "Участник не найден на сервере."
    if member.bot:
        return "rejected", "Only server members can control playback."
    member_voice = music_audio._member_channel(member)
    if member_voice is None:
        return "rejected", "Сначала войди в голосовой канал сервера."
    bot_voice = guild.voice_client
    if bot_voice and bot_voice.channel != member_voice:
        return "rejected", "Vivian находится в другом голосовом канале."
    if action == "play":
        if not isinstance(query, str):
            return "rejected", "Enter a track title or link."
        try:
            query, source, requested_from = await music_audio._extract_in_executor(
                music_prefix._request_parts, query,
            )
        except ValueError as error:
            return "rejected", str(error)
        result = await music_prefix._start(
            SimpleNamespace(author=member, guild=guild), query, source or "youtube",
            fallback_source=None, keep_channel=True, requested_from=requested_from,
        )
        return ("done" if result.started else "rejected"), result.text[:300]
    if action == "stop":
        stopped = await music_audio._stop_music_session(guild, requester=member)
        if not stopped:
            return "rejected", "Vivian is not in your voice channel."
    elif action == "pause" and bot_voice and bot_voice.is_playing():
        music_state.MUSIC_TRACK_ELAPSED[guild_id] = music_utils._music_position_seconds(guild_id)
        music_state.MUSIC_TRACK_STARTED_AT.pop(guild_id, None)
        bot_voice.pause()
    elif action == "resume" and bot_voice and bot_voice.is_paused():
        bot_voice.resume()
        music_state.MUSIC_TRACK_STARTED_AT[guild_id] = time.monotonic()
    elif action == "skip" and bot_voice and (bot_voice.is_playing() or bot_voice.is_paused()):
        music_state.MUSIC_SKIP_PENDING.add(guild_id)
        bot_voice.stop()
    else:
        return "rejected", "Playback is not in the required state for this command."
    try:
        await music_panels._refresh_music_player(guild_id)
    except discord.DiscordException as error:
        log.warning("Music panel refresh failed (%s)", type(error).__name__)
    return "done", "Команда выполнена."


async def _publish(bot):
    rows = []
    for guild in bot.guilds:
        track = music_state.MUSIC_NOW_PLAYING.get(guild.id) or {}
        voice = guild.voice_client
        duration = track.get("duration")
        if type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0:
            duration = None
        rows.append((guild.id, str(track.get("title") or "")[:300], str(track.get("artist") or "")[:200],
                     duration, bool(voice and voice.is_playing()), bool(voice and voice.is_paused())))
    if rows:
        async with asyncio.timeout(8), database.db_pool.connection() as connection, connection.cursor() as cursor:
            await cursor.executemany(_STATUS, rows)


async def _tick(bot):
    await _db(_EXPIRE)
    job = await _db(_CLAIM, fetch=True)
    if job:
        try:
            status, message = await _execute(bot, job)
        except Exception as error:
            log.warning("Music bridge command failed (%s)", type(error).__name__)
            status, message = "rejected", "Не удалось обработать запрос."
        await _db(_COMPLETE, (status, message, job[0]))
    await _publish(bot)


async def run(bot):
    ready = False
    while enabled() and not bot.is_closed():
        try:
            await _tick(bot)
            if not ready:
                print("[Web music bridge] Ready", flush=True)
                ready = True
        except Exception as error:
            log.warning("Music bridge storage unavailable (%s)", type(error).__name__)
        await asyncio.sleep(4)
