from __future__ import annotations
import asyncio
import discord
from utils import components_v2 as v2
from core import config as core_config
from core import discord_api as core_discord_api
from commands import music_player as commands_music_player
from core import database as core_database
from core import music_state as core_music_state


async def _refresh_music_player(guild_id: int) -> None:
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


async def _persist_music_player_reference(guild_id: int, message: discord.Message) -> None:
    if core_database.db_pool is None or message.channel is None:
        return
    try:
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
    except Exception as error:
        print(f"[Music panel] Не удалось сохранить ссылку панели сервера {guild_id}: {error!r}", flush=True)


async def _delete_music_player_reference(guild_id: int) -> None:
    if core_database.db_pool is None:
        return
    try:
        async with core_database.db_pool.connection() as connection:
            await connection.execute(
                "DELETE FROM public.music_player_panels WHERE guild_id = %s",
                (guild_id,),
            )
    except Exception as error:
        print(f"[Music panel] Не удалось удалить ссылку панели сервера {guild_id}: {error!r}", flush=True)


async def _restore_music_player_panels() -> None:
    if core_database.db_pool is None or core_discord_api.is_paused():
        return
    try:
        async with core_database.db_pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT guild_id, channel_id, message_id FROM public.music_player_panels"
            )
            rows = await cursor.fetchall()
    except Exception as error:
        print(f"[Music panel] Не удалось прочитать сохранённые панели: {error!r}", flush=True)
        return

    for guild_id, channel_id, message_id in rows:
        if core_discord_api.is_paused():
            return
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
