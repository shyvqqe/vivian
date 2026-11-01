# Optional web control bridge. Validate Discord permissions before execution.
import asyncio
import os
import time

import psycopg
from commands import music_audio, music_prefix
from core import music_state


def _query(sql, args=(), fetch=False):
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(sql,args)
            return cur.fetchone() if fetch else None


async def _db(sql,args=(),fetch=False):
    return await asyncio.to_thread(_query,sql,args,fetch)


async def _execute(bot, job):
    job_id,guild_id,user_id,action,query=job
    guild=bot.get_guild(guild_id)
    if not guild:
        return 'rejected','Бот не состоит на этом сервере.'
    try:
        member=guild.get_member(user_id) or await guild.fetch_member(user_id)
    except Exception:
        return 'rejected','Участник не найден на сервере.'
    member_voice=getattr(getattr(member,'voice',None),'channel',None)
    if member_voice is None:
        return 'rejected','Сначала войди в голосовой канал сервера.'
    bot_voice=guild.voice_client
    if bot_voice and bot_voice.channel and bot_voice.channel.id!=member_voice.id:
        return 'rejected','Vivian находится в другом голосовом канале.'
    if action=='play':
        bot_member=guild.me
        permissions=member_voice.permissions_for(bot_member)
        if not permissions.connect or not permissions.speak:
            return 'rejected','Нет прав Connect/Speak.'
        from types import SimpleNamespace
        ctx=SimpleNamespace(author=member,guild=guild)
        result=await music_prefix._start(ctx,query,'youtube')
        return ('done' if result.started else 'rejected'), str(result.text)[:300]
    if not bot_voice or not (bot_voice.is_playing() or bot_voice.is_paused()):
        return 'rejected','Сейчас ничего не играет.'
    if action=='pause':
        if bot_voice.is_playing():
            from utils import music as music_utils
            music_state.MUSIC_TRACK_ELAPSED[guild_id]=music_utils._music_position_seconds(guild_id)
            bot_voice.pause()
    elif action=='resume':
        if bot_voice.is_paused():
            bot_voice.resume()
            music_state.MUSIC_TRACK_STARTED_AT[guild_id]=time.monotonic()
    elif action=='skip':
        music_state.MUSIC_SKIP_PENDING.add(guild_id)
        bot_voice.stop()
    elif action=='stop':
        bot_voice.stop()
        music_state.MUSIC_QUEUES[guild_id].clear()
        await bot_voice.disconnect(force=False)
    return 'done','Команда выполнена.'


async def _publish(bot):
    for guild in bot.guilds:
        track=music_state.MUSIC_NOW_PLAYING.get(guild.id) or {}
        voice=guild.voice_client
        playing=bool(voice and voice.is_playing())
        paused=bool(voice and voice.is_paused())
        await _db('''INSERT INTO vivian_web.music_status (guild_id,title,artist,duration_seconds,is_playing,is_paused,updated_at)
                     VALUES (%s,%s,%s,%s,%s,%s,now())
                     ON CONFLICT(guild_id) DO UPDATE SET title=excluded.title,artist=excluded.artist,
                     duration_seconds=excluded.duration_seconds,is_playing=excluded.is_playing,
                     is_paused=excluded.is_paused,updated_at=now()''',
                  (guild.id,str(track.get('title') or '')[:300],str(track.get('artist') or '')[:200],
                   track.get('duration') if isinstance(track.get('duration'),(int,float)) else None,playing,paused))


async def run(bot):
    if os.environ.get('WEB_MUSIC_BRIDGE_ENABLED','').lower()!='true':
        return
    while not bot.is_closed():
        try:
            # Claim only one pending job at a time; stale jobs are rejected.
            job=await _db('''UPDATE vivian_web.music_requests SET status='working'
                WHERE id=(SELECT id FROM vivian_web.music_requests
                    WHERE status='pending' AND created_at > now()-interval '2 minutes' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
                RETURNING id,guild_id,discord_id,action,query''',fetch=True)
            if job:
                try:
                        status,msg=await _execute(bot,job)
                except Exception:
                    status,msg='rejected','Не удалось обработать запрос.'
                await _db('''UPDATE vivian_web.music_requests SET status=%s,result=%s,processed_at=now() WHERE id=%s''',(status,msg,job[0]))
            await _publish(bot)
        except Exception as exc:
            print(f'[Web music bridge] {type(exc).__name__}: {exc}',flush=True)
        await asyncio.sleep(4)
