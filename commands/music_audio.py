from __future__ import annotations
import asyncio
import discord
import random
import shutil
import subprocess
import time
from core import config as core_config
from commands import music_panels as commands_music_panels
from commands import music_search as commands_music_search
from commands import music_relay as commands_music_relay
from core import music_state as core_music_state


def _append_music_tracks(guild_id: int, tracks: list[dict]) -> None:
    core_music_state.MUSIC_QUEUES[guild_id].extend(tracks)
    if core_music_state.MUSIC_SHUFFLE_MODE[guild_id] and len(core_music_state.MUSIC_QUEUES[guild_id]) > 1:
        shuffled = list(core_music_state.MUSIC_QUEUES[guild_id])
        random.shuffle(shuffled)
        core_music_state.MUSIC_QUEUES[guild_id].clear()
        core_music_state.MUSIC_QUEUES[guild_id].extend(shuffled)


async def _queue_music_tracks(guild: discord.Guild | None, member: discord.Member, tracks: list[dict]) -> tuple[bool, str]:
    if guild is None:
        return False, "Музыка доступна только на сервере."
    voice_state = getattr(member, "voice", None)
    voice_channel = getattr(voice_state, "channel", None)
    if voice_channel is None:
        return False, "Сначала зайди в голосовой канал."
    if not tracks:
        return False, "Выбери хотя бы один трек."

    bot_member = guild.me
    if bot_member is not None:
        permissions = voice_channel.permissions_for(bot_member)
        missing = [
            name for name, allowed in (("Connect", permissions.connect), ("Speak", permissions.speak))
            if not allowed
        ]
        if missing:
            return False, f"Нет прав канала: {', '.join(missing)}. Разреши боту подключение и речь."

    guild_id = guild.id
    try:
        async with _music_connect_lock(guild_id):
            voice = guild.voice_client
            if voice is None:
                voice = await voice_channel.connect(timeout=45, reconnect=True)
            elif not voice.is_connected():
                core_music_state.MUSIC_CHANNELS[guild_id] = voice_channel
                _append_music_tracks(guild_id, tracks)
                _schedule_music_recovery(guild_id)
                asyncio.create_task(commands_music_panels._refresh_music_player(guild_id))
                return True, f"Связь восстанавливается. В очереди: {len(tracks)} трек(ов)."
            elif voice.channel != voice_channel:
                if voice.is_playing() or voice.is_paused():
                    return False, "Музыка уже воспроизводится в другом голосовом канале."
                await voice.move_to(voice_channel)

            core_music_state.MUSIC_CHANNELS[guild_id] = voice_channel
            was_active = bool(
                voice.is_playing()
                or voice.is_paused()
                or guild_id in core_music_state.MUSIC_STARTING
                or guild_id in core_music_state.MUSIC_RECOVERY_TASKS
            )
            _append_music_tracks(guild_id, tracks)
    except Exception as error:
        print(f"[Музыка] Ошибка подключения на сервере {guild_id}: {error!r}", flush=True)
        return False, f"Не удалось подключиться к голосовому каналу: {type(error).__name__}."

    if not was_active:
        if not await _play_next_track(guild_id):
            return False, "Не удалось начать воспроизведение. Проверь yt-dlp, ffmpeg и доступ к источнику."
        first_title = tracks[0].get("title", "трек")
        queued_after = len(tracks) - 1
        message = f"Запущено: **{first_title}**."
        if queued_after:
            message += f" Следом добавлено в очередь: {queued_after}."
        await commands_music_panels._refresh_music_player(guild_id)
        return True, message
    await commands_music_panels._refresh_music_player(guild_id)
    return True, f"В очередь добавлено треков: {len(tracks)}."


async def _ensure_ffmpeg() -> str:
    if core_music_state.FFMPEG_EXECUTABLE is None:
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            raise RuntimeError("ffmpeg не установлен в образе Render.")
        core_music_state.FFMPEG_EXECUTABLE = ffmpeg_path
    return core_music_state.FFMPEG_EXECUTABLE


async def _extract_in_executor(function, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, function, *args)


def _music_connect_lock(guild_id: int) -> asyncio.Lock:
    lock = core_music_state.MUSIC_CONNECT_LOCKS.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        core_music_state.MUSIC_CONNECT_LOCKS[guild_id] = lock
    return lock


def _reset_music_session(guild_id: int) -> int:
    """Invalidate pending resolves and old player callbacks before stopping voice."""
    core_music_state.MUSIC_GENERATION[guild_id] += 1
    core_music_state.MUSIC_QUEUES[guild_id].clear()
    for state in (core_music_state.MUSIC_NOW_PLAYING, core_music_state.MUSIC_SOURCES,
                  core_music_state.MUSIC_TRACK_STARTED_AT, core_music_state.MUSIC_CHANNELS,
                  core_music_state.MUSIC_STARTING_GENERATIONS):
        state.pop(guild_id, None)
    core_music_state.MUSIC_STARTING.discard(guild_id)
    core_music_state.MUSIC_SKIP_PENDING.discard(guild_id)
    core_music_state.MUSIC_TRACK_ELAPSED[guild_id] = 0.0
    core_music_state.loop_status[guild_id] = False
    recovery = core_music_state.MUSIC_RECOVERY_TASKS.pop(guild_id, None)
    if recovery and not recovery.done():
        recovery.cancel()
    return core_music_state.MUSIC_GENERATION[guild_id]


async def replace_current_track(guild, voice_channel, track: dict) -> bool:
    async with _music_connect_lock(guild.id):
        voice = guild.voice_client
        if voice is None:
            voice = await voice_channel.connect(timeout=45, reconnect=True)
        elif not voice.is_connected():
            await voice.disconnect(force=True)
            voice = await voice_channel.connect(timeout=45, reconnect=True)
        elif voice.channel != voice_channel:
            await voice.move_to(voice_channel)
        generation = _reset_music_session(guild.id)
        voice.stop()
        core_music_state.MUSIC_CHANNELS[guild.id] = voice_channel
        core_music_state.MUSIC_QUEUES[guild.id].append(track)
    started = await _play_next_track(guild.id)
    return bool(started and core_music_state.MUSIC_GENERATION[guild.id] == generation)


async def _stop_music_session(guild) -> bool:
    async with _music_connect_lock(guild.id):
        voice = guild.voice_client
        _reset_music_session(guild.id)
        if voice:
            voice.stop()
            await voice.disconnect(force=True)
        return voice is not None


def _schedule_music_recovery(guild_id: int):
    task = core_music_state.MUSIC_RECOVERY_TASKS.get(guild_id)
    if task is None or task.done():
        generation = core_music_state.MUSIC_GENERATION[guild_id]
        core_music_state.MUSIC_RECOVERY_TASKS[guild_id] = asyncio.create_task(_recover_music_after_drop(guild_id, generation))


async def _recover_music_after_drop(guild_id: int, generation: int | None = None):
    """Wait for discord.py voice reconnect; if it fails, make one clean rejoin."""
    current_task = asyncio.current_task()
    if generation is None:
        generation = core_music_state.MUSIC_GENERATION[guild_id]
    try:
        for _ in range(18):
            await asyncio.sleep(5)
            if (generation != core_music_state.MUSIC_GENERATION[guild_id]
                    or not core_music_state.MUSIC_QUEUES[guild_id]):
                return
            guild = core_config.bot.get_guild(guild_id)
            if guild is None:
                return
            voice = guild.voice_client
            if voice and voice.is_connected():
                if not voice.is_playing() and not voice.is_paused():
                    await _play_next_track(guild_id)
                return

        guild = core_config.bot.get_guild(guild_id)
        channel = core_music_state.MUSIC_CHANNELS.get(guild_id)
        if guild is None or channel is None or not core_music_state.MUSIC_QUEUES[guild_id]:
            return

        async with _music_connect_lock(guild_id):
            if generation != core_music_state.MUSIC_GENERATION[guild_id]:
                return
            voice = guild.voice_client
            if voice and voice.is_connected():
                recovered_voice = voice
            else:
                if voice:
                    try:
                        await voice.disconnect(force=True)
                    except discord.DiscordException as error:
                        print(f"[Музыка] Не удалось сбросить voice-сессию {guild_id}: {error!r}")
                if not core_music_state.MUSIC_QUEUES[guild_id]:
                    return
                recovered_voice = await channel.connect(timeout=45, reconnect=True)

        if recovered_voice and recovered_voice.is_connected():
            print(f"[Музыка] Голосовая сессия восстановлена на сервере {guild_id}")
            await _play_next_track(guild_id)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"[Музыка] Не удалось восстановить голосовую сессию {guild_id}: {error!r}")
    finally:
        if core_music_state.MUSIC_RECOVERY_TASKS.get(guild_id) is current_task:
            core_music_state.MUSIC_RECOVERY_TASKS.pop(guild_id, None)


async def _handle_track_end(guild_id: int, error, generation: int | None = None):
    if generation is not None and generation != core_music_state.MUSIC_GENERATION[guild_id]:
        return
    started_at = core_music_state.MUSIC_TRACK_STARTED_AT.pop(guild_id, None)
    elapsed = time.monotonic() - started_at if started_at is not None else None
    core_music_state.MUSIC_TRACK_ELAPSED[guild_id] = 0.0
    if guild_id in core_music_state.MUSIC_SKIP_PENDING:
        core_music_state.MUSIC_SKIP_PENDING.discard(guild_id)
        core_music_state.MUSIC_NOW_PLAYING.pop(guild_id, None)
        core_music_state.MUSIC_SOURCES.pop(guild_id, None)
        await _play_next_track(guild_id)
        return

    track = core_music_state.MUSIC_NOW_PLAYING.get(guild_id)
    core_music_state.MUSIC_SOURCES.pop(guild_id, None)
    # FFmpeg may report EOF (after(None)) even when the source returned HTTP 403.
    # Retry at most once for both the relay and local backends; an early failure
    # must not be reported to users as a successfully played full track.
    early_stream_end = commands_music_relay._is_premature_end(track, elapsed)
    if error or early_stream_end:
        if error:
            print(f"[Музыка] Ошибка воспроизведения на сервере {guild_id}: {type(error).__name__}")
        elif early_stream_end:
            print(f"[Музыка] {track.get('backend', 'unknown')} stream ended too early on server {guild_id}")
        core_music_state.MUSIC_NOW_PLAYING.pop(guild_id, None)
        if track and track.get("_veyr_retry_count", 0) < 1:
            retry_track = dict(track)
            retry_track["_veyr_retry_count"] = retry_track.get("_veyr_retry_count", 0) + 1
            if track.get("backend") == "relay":
                retry_track["backend"] = "local"
            core_music_state.MUSIC_QUEUES[guild_id].appendleft(retry_track)
        if core_music_state.MUSIC_QUEUES[guild_id]:
            guild = core_config.bot.get_guild(guild_id)
            voice = guild.voice_client if guild else None
            if voice and voice.is_connected():
                await _play_next_track(guild_id)
            else:
                _schedule_music_recovery(guild_id)
            await commands_music_panels._refresh_music_player(guild_id)
        else:
            await _play_next_track(guild_id)
        return

    await _play_next_track(guild_id)


async def _play_next_track(guild_id: int):
    generation = core_music_state.MUSIC_GENERATION[guild_id]
    if core_music_state.MUSIC_STARTING_GENERATIONS.get(guild_id) == generation:
        return False
    core_music_state.MUSIC_STARTING.add(guild_id)
    core_music_state.MUSIC_STARTING_GENERATIONS[guild_id] = generation
    owns_reservation = True
    playback_started = False
    retry = False
    source = None
    try:
        guild = core_config.bot.get_guild(guild_id)
        voice = guild.voice_client if guild else None
        if voice is None or not voice.is_connected():
            return False

        if core_music_state.loop_status[guild_id] and guild_id in core_music_state.MUSIC_NOW_PLAYING:
            track = core_music_state.MUSIC_NOW_PLAYING[guild_id]
        elif core_music_state.MUSIC_QUEUES[guild_id]:
            track = core_music_state.MUSIC_QUEUES[guild_id].popleft()
        else:
            core_music_state.MUSIC_NOW_PLAYING.pop(guild_id, None)
            core_music_state.MUSIC_SOURCES.pop(guild_id, None)
            core_music_state.MUSIC_TRACK_STARTED_AT.pop(guild_id, None)
            core_music_state.MUSIC_TRACK_ELAPSED[guild_id] = 0.0
            asyncio.create_task(_disconnect_when_idle(guild_id, voice, generation))
            await commands_music_panels._refresh_music_player(guild_id)
            return False

        # Prefer the home relay; use yt-dlp locally when the search used local fallback.
        resolved = await _extract_in_executor(
            commands_music_search._resolve_music_url,
            track.get("relay_query") or track["webpage_url"],
            track["title"],
            track.get("source"),
            track.get("backend"),
        )
        if generation != core_music_state.MUSIC_GENERATION[guild_id]:
            return False
        if not resolved:
            raise RuntimeError(f"{track.get('source', 'Источник')} не вернул аудиопоток.")
        fresh_track = dict(track)
        fresh_track.update({key: resolved[key] for key in ("stream_url", "relay_query", "source", "backend")})
        fresh_track["_veyr_retry_count"] = track.get("_veyr_retry_count", 0)
        ffmpeg_path = await _ensure_ffmpeg()
        if (generation != core_music_state.MUSIC_GENERATION[guild_id]
                or guild.voice_client is not voice or not voice.is_connected()):
            return False
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                fresh_track["stream_url"],
                executable=ffmpeg_path,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn",
                stderr=subprocess.DEVNULL,
            ),
            volume=min(core_music_state.MUSIC_VOLUME[guild_id], core_music_state.MAX_MUSIC_VOLUME) / 100,
        )
        core_music_state.MUSIC_NOW_PLAYING[guild_id] = fresh_track
        core_music_state.MUSIC_SOURCES[guild_id] = source
        core_music_state.MUSIC_TRACK_ELAPSED[guild_id] = 0.0
        core_music_state.MUSIC_TRACK_STARTED_AT[guild_id] = time.monotonic()

        def after_playback(error):
            try:
                asyncio.run_coroutine_threadsafe(_handle_track_end(guild_id, error, generation), core_config.bot.loop)
            except RuntimeError:
                pass

        voice.play(source, after=after_playback)
        playback_started = True
        # A short/failed stream may end while the panel awaits its HTTP edit.
        core_music_state.MUSIC_STARTING_GENERATIONS.pop(guild_id, None)
        core_music_state.MUSIC_STARTING.discard(guild_id)
        owns_reservation = False
        await commands_music_panels._refresh_music_player(guild_id)
        return True
    except Exception as error:
        if generation != core_music_state.MUSIC_GENERATION[guild_id]:
            return False
        if playback_started:
            # A panel failure cannot invalidate audio or the next track's state.
            print(f"[Музыка] Панель не обновилась на сервере {guild_id}: {type(error).__name__}")
            return True
        print(f"[Музыка] Не удалось воспроизвести трек на сервере {guild_id}: {type(error).__name__}")
        if source is not None and not voice.is_playing() and not voice.is_paused():
            source.cleanup()
        core_music_state.MUSIC_NOW_PLAYING.pop(guild_id, None)
        core_music_state.MUSIC_SOURCES.pop(guild_id, None)
        core_music_state.MUSIC_TRACK_STARTED_AT.pop(guild_id, None)
        core_music_state.MUSIC_TRACK_ELAPSED[guild_id] = 0.0
        retry = True
    finally:
        if owns_reservation and core_music_state.MUSIC_STARTING_GENERATIONS.get(guild_id) == generation:
            core_music_state.MUSIC_STARTING_GENERATIONS.pop(guild_id, None)
            core_music_state.MUSIC_STARTING.discard(guild_id)
    if retry:
        asyncio.create_task(_play_next_track(guild_id))
    return False


async def _disconnect_when_idle(guild_id: int, voice: discord.VoiceClient, generation: int | None = None):
    if generation is None:
        generation = core_music_state.MUSIC_GENERATION[guild_id]
    await asyncio.sleep(120)
    guild = core_config.bot.get_guild(guild_id)
    if not guild:
        return
    async with _music_connect_lock(guild_id):
        if (
            guild.voice_client is voice
            and generation == core_music_state.MUSIC_GENERATION[guild_id]
            and voice.is_connected()
            and not voice.is_playing()
            and not voice.is_paused()
            and not core_music_state.MUSIC_QUEUES[guild_id]
            and guild_id not in core_music_state.MUSIC_STARTING
        ):
            try:
                await voice.disconnect()
                core_music_state.MUSIC_CHANNELS.pop(guild_id, None)
            except discord.DiscordException as error:
                print(f"[Музыка] Ошибка отключения на сервере {guild_id}: {error!r}")
