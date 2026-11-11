from __future__ import annotations
import asyncio
import time
import discord
from utils import components_v2 as v2
from core import config as core_config
from core import discord_api as core_discord_api
from commands import music_player as commands_music_player
from core import database as core_database
from core import feature_storage
from core import music_state as core_music_state


_restore_lock = asyncio.Lock()
# A rapid sequence of play/skip/queue interactions should produce one panel
# update, not several simultaneous Discord PATCH requests.
_panel_locks: dict[int, asyncio.Lock] = {}
_panel_last_edit: dict[int, float] = {}
_panel_delayed_tasks: dict[int, asyncio.Task] = {}
_PANEL_MIN_EDIT_INTERVAL = 1.0


async def _local_panels(query, params=(), *, fetch=False):
    def work(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS music_player_panels (
            guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL
        )""")
        cursor = connection.execute(query, params)
        return cursor.fetchall() if fetch else None
    return await feature_storage.run_local(work)


async def _update_music_player(guild_id: int) -> None:
    if core_discord_api.is_paused():
        return
    message = core_music_state.MUSIC_PLAYER_MESSAGES.get(guild_id)
    if message is None:
        return
    view = core_music_state.MUSIC_PLAYER_VIEWS.get(guild_id)
    if view is None:
        view = commands_music_player.MusicPlayerView(guild_id)
        core_music_state.MUSIC_PLAYER_VIEWS[guild_id] = view
    try:
        view.sync_state()
        payload = await commands_music_player._music_panel_payload(guild_id, message)
        core_music_state.MUSIC_PLAYER_MESSAGES[guild_id] = await v2.edit_message(message,
            **payload,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.NotFound:
        core_music_state.MUSIC_PLAYER_MESSAGES.pop(guild_id, None)
        core_music_state.MUSIC_PLAYER_VIEWS.pop(guild_id, None)
        await _delete_music_player_reference(guild_id)
    except discord.Forbidden as error:
        core_music_state.MUSIC_PLAYER_MESSAGES.pop(guild_id, None)
        core_music_state.MUSIC_PLAYER_VIEWS.pop(guild_id, None)
        await _delete_music_player_reference(guild_id)
        print(f"[Music panel] Недостаточно прав для обновления панели сервера {guild_id}: {error!r}", flush=True)
    except discord.DiscordException as error:
        if core_discord_api.is_rate_limit_error(error):
            return
        print(f"[Music panel] Не удалось обновить сообщение сервера {guild_id}: {error!r}", flush=True)
    except Exception as error:
        print(f"[Music panel] Ошибка обновления сервера {guild_id}: {error!r}", flush=True)


async def _refresh_music_player(guild_id: int) -> None:
    """Coalesce bursts of panel edits while keeping command responses fast."""
    if core_discord_api.is_paused() or guild_id not in core_music_state.MUSIC_PLAYER_MESSAGES:
        return
    last = _panel_last_edit.get(guild_id, 0.0)
    remaining = _PANEL_MIN_EDIT_INTERVAL - (time.monotonic() - last)
    if remaining > 0:
        pending = _panel_delayed_tasks.get(guild_id)
        if pending is None or pending.done():
            async def delayed():
                try:
                    await asyncio.sleep(remaining)
                    if guild_id not in core_music_state.MUSIC_PLAYER_MESSAGES or core_discord_api.is_paused():
                        return
                    lock = _panel_locks.setdefault(guild_id, asyncio.Lock())
                    async with lock:
                        _panel_last_edit[guild_id] = time.monotonic()
                        await _update_music_player(guild_id)
                finally:
                    if _panel_delayed_tasks.get(guild_id) is asyncio.current_task():
                        _panel_delayed_tasks.pop(guild_id, None)
            _panel_delayed_tasks[guild_id] = asyncio.create_task(delayed())
        return
    lock = _panel_locks.setdefault(guild_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        # Timestamp before awaiting the remote edit so subsequent calls avoid
        # another PATCH even when the network is slow.
        _panel_last_edit[guild_id] = time.monotonic()
        await _update_music_player(guild_id)


async def _persist_music_player_reference(guild_id: int, message: discord.Message) -> bool:
    if message.channel is None:
        return False
    try:
        if not core_config.DATABASE_URL:
            await _local_panels(
                """INSERT INTO music_player_panels (guild_id, channel_id, message_id) VALUES (?, ?, ?)
                   ON CONFLICT (guild_id) DO UPDATE SET
                       channel_id = EXCLUDED.channel_id, message_id = EXCLUDED.message_id""",
                (guild_id, message.channel.id, message.id),
            )
            return True
        async with core_database.db_pool.connection() as connection:
            await connection.execute(
                """INSERT INTO public.music_player_panels (guild_id, channel_id, message_id)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (guild_id) DO UPDATE SET
                       channel_id = EXCLUDED.channel_id,
                       message_id = EXCLUDED.message_id,
                       updated_at = now()""",
                (guild_id, message.channel.id, message.id),
            )
        return True
    except Exception as error:
        print(f"[Music panel] Не удалось сохранить ссылку панели сервера {guild_id}: {type(error).__name__}", flush=True)
        return False


async def _delete_music_player_reference(guild_id: int) -> None:
    try:
        if not core_config.DATABASE_URL:
            await _local_panels("DELETE FROM music_player_panels WHERE guild_id = ?", (guild_id,))
            return
        async with core_database.db_pool.connection() as connection:
            await connection.execute(
                "DELETE FROM public.music_player_panels WHERE guild_id = %s",
                (guild_id,),
            )
    except Exception as error:
        print(f"[Music panel] Не удалось удалить ссылку панели сервера {guild_id}: {type(error).__name__}", flush=True)


async def _restore_music_player_panels() -> None:
    if core_discord_api.is_paused():
        return
    try:
        if not core_config.DATABASE_URL:
            rows = await _local_panels("SELECT guild_id, channel_id, message_id FROM music_player_panels", fetch=True)
        else:
            async with core_database.db_pool.connection() as connection:
                cursor = await connection.execute(
                    "SELECT guild_id, channel_id, message_id FROM public.music_player_panels"
                )
                rows = await cursor.fetchall()
    except Exception as error:
        print(f"[Music panel] Не удалось прочитать сохранённые панели: {type(error).__name__}", flush=True)
        return

    for guild_id, channel_id, message_id in rows:
        if core_discord_api.is_paused():
            return
        if guild_id in core_music_state.MUSIC_PLAYER_MESSAGES:
            continue
        try:
            channel = core_config.bot.get_channel(channel_id) or await core_config.bot.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
            view = commands_music_player.MusicPlayerView(guild_id)
            core_music_state.MUSIC_PLAYER_VIEWS[guild_id] = view
            payload = await commands_music_player._music_panel_payload(guild_id, message)
            core_music_state.MUSIC_PLAYER_MESSAGES[guild_id] = await v2.edit_message(message,
                **payload,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            await _delete_music_player_reference(guild_id)
        except (discord.DiscordException, AttributeError) as error:
            if core_discord_api.is_rate_limit_error(error):
                return
            print(f"[Music panel] Не удалось восстановить панель сервера {guild_id}: {error!r}", flush=True)
        except Exception as error:
            print(f"[Music panel] Ошибка восстановления панели сервера {guild_id}: {error!r}", flush=True)


@core_config.bot.listen("on_ready")
async def restore_saved_music_panels():
    async with _restore_lock:
        await _restore_music_player_panels()


async def _music_player_refresh_loop() -> None:
    await core_config.bot.wait_until_ready()
    while not core_config.bot.is_closed():
        await asyncio.sleep(15)
        if core_discord_api.is_paused():
            continue
        for guild_id in list(core_music_state.MUSIC_PLAYER_MESSAGES):
            guild = core_config.bot.get_guild(guild_id)
            voice = guild.voice_client if guild else None
            if voice and voice.is_playing() and core_music_state.MUSIC_NOW_PLAYING.get(guild_id):
                try:
                    await _refresh_music_player(guild_id)
                except Exception as error:
                    print(f"[Music panel] Ошибка обновления таймера сервера {guild_id}: {error!r}", flush=True)
