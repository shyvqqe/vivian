from __future__ import annotations
from discord import app_commands
import asyncio
import discord
from utils import components_v2 as v2
import time
from core import config as core_config
from core import discord_api as core_discord_api
from commands import music_audio as commands_music_audio
from commands import music_panels as commands_music_panels
from commands import music_player as commands_music_player
from commands import music_relay as commands_music_relay
from commands import music_search as commands_music_search
from commands import music_select as commands_music_select
from core import music_state as core_music_state
from utils import helpers as utils_helpers
from utils import command_responses as utils_command_responses
from utils import music as utils_music

MUSIC_COOLDOWN_NOTICE = "Discord временно ограничил запросы. Повтори команду позже."


@core_config.bot.tree.command(name="play", description="Новый поиск YouTube в разработке. Для музыки используй !play название.")
@app_commands.describe(query="Название трека; пока музыка доступна через !play")
async def play(interaction: discord.Interaction, query: str):
    example = " ".join(query.replace("`", "").split())[:200] or "название трека"
    await utils_helpers.vivian_reply(
        interaction,
        "Готовлю новый поиск и плеер YouTube.\n\n"
        f"Пока включай музыку обычной командой:\n`!play {example}`\n\n"
        "Выбор источника и управление музыкой остаются в `!play` и `/queue`.",
        title="/play · Команда в разработке",
    )


async def _play_search(interaction: discord.Interaction, query: str, source: str = "youtube"):
    """Existing discovery flow retained while the new /play is being developed."""
    if not await utils_command_responses.defer_command(interaction, ephemeral=True):
        return

    if core_discord_api.is_paused():
        return await utils_command_responses.finish_deferred(interaction, MUSIC_COOLDOWN_NOTICE)
    if interaction.guild is None:
        return await utils_helpers.vivian_reply(interaction, "Музыка доступна только на сервере.", error=True, ephemeral=True)
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await utils_helpers.vivian_reply(interaction, "Сначала зайди в голосовой канал.", error=True, ephemeral=True)
    voice_channel = interaction.user.voice.channel

    bot_member = interaction.guild.me
    if bot_member is not None:
        permissions = voice_channel.permissions_for(bot_member)
        missing = [
            name for name, allowed in (("Connect", permissions.connect), ("Speak", permissions.speak))
            if not allowed
        ]
        if missing:
            return await utils_helpers.vivian_reply(
                interaction,
                f"Не хватает прав канала: {', '.join(missing)}. Разреши подключение и речь в {voice_channel.mention}.",
                error=True,
                ephemeral=True,
            )

    search_query = query.strip()
    if not search_query or len(search_query) > 200:
        return await utils_helpers.vivian_reply(
            interaction, "Запрос должен содержать от 1 до 200 символов.", error=True, ephemeral=True)
    try:
        if commands_music_search._is_soundcloud_url(search_query) or commands_music_search._is_youtube_url(search_query):
            platform = "soundcloud" if commands_music_search._is_soundcloud_url(search_query) else "youtube"
            tracks = await commands_music_audio._extract_in_executor(
                commands_music_relay._relay_search, search_query, platform, 1,
            )
        elif search_query.startswith(("http://", "https://")):
            raise ValueError("Поддерживаются прямые ссылки SoundCloud и YouTube.")
        else:
            tracks = await commands_music_audio._extract_in_executor(
                commands_music_search._search_music_platform, search_query, source, 10,
            )
    except ValueError as error:
        return await utils_helpers.vivian_reply(interaction, str(error), error=True, ephemeral=True)
    except Exception as error:
        if core_discord_api.is_rate_limit_error(error) or core_discord_api.is_paused():
            return await utils_command_responses.finish_deferred(interaction, MUSIC_COOLDOWN_NOTICE)
        print(f"[Music search] Ошибка поиска {search_query!r}: {error!r}", flush=True)
        return await utils_helpers.vivian_reply(
            interaction,
            "Поиск треков временно не ответил. Попробуй позже.",
            title="◆ Vivian // ERROR",
            error=True,
            ephemeral=True,
        )
    if core_discord_api.is_paused():
        return await utils_command_responses.finish_deferred(interaction, MUSIC_COOLDOWN_NOTICE)
    if not tracks:
        return await utils_helpers.vivian_reply(interaction, "По этому запросу ничего не найдено.", error=True, ephemeral=True)

    view = commands_music_select.MusicSearchView(interaction.guild.id, interaction.user.id, query.strip(), tracks)
    view.panel_message = await utils_command_responses.send_panel(interaction, view=view, style="music")


@core_config.bot.tree.command(name="queue", description="Открыть живую панель трека, очереди и управления музыкой.")
async def queue_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        return await utils_helpers.vivian_reply(interaction, "Очередь доступна только на сервере.", error=True, ephemeral=True)
    guild_id = interaction.guild.id
    if not await utils_command_responses.defer_command(interaction, ephemeral=True):
        return
    if core_discord_api.is_paused():
        return await utils_command_responses.finish_deferred(interaction, MUSIC_COOLDOWN_NOTICE)
    view = commands_music_player.MusicPlayerView(guild_id)
    existing_message = core_music_state.MUSIC_PLAYER_MESSAGES.get(guild_id)
    if existing_message is not None:
        try:
            payload = await commands_music_player._music_panel_payload(guild_id, existing_message)
            updated_message = await v2.edit_message(existing_message, **payload, view=view)
        except discord.NotFound:
            core_music_state.MUSIC_PLAYER_MESSAGES.pop(guild_id, None)
            core_music_state.MUSIC_PLAYER_VIEWS.pop(guild_id, None)
        except discord.DiscordException as error:
            if core_discord_api.is_rate_limit_error(error) or core_discord_api.is_paused():
                return await utils_command_responses.finish_deferred(interaction, MUSIC_COOLDOWN_NOTICE)
            if not isinstance(error, discord.HTTPException):
                raise
            print(f"[Music panel] Не удалось переиспользовать панель сервера {guild_id}: {error!r}", flush=True)
        else:
            core_music_state.MUSIC_PLAYER_MESSAGES[guild_id] = updated_message
            core_music_state.MUSIC_PLAYER_VIEWS[guild_id] = view
            asyncio.create_task(commands_music_panels._persist_music_player_reference(guild_id, updated_message))
            return await utils_command_responses.edit_deferred(
                interaction,
                embed=utils_helpers.vivian_embed("Открытая панель обновлена.", title="◆ Vivian // MUSIC"),
                view=None,
            )

    if core_discord_api.is_paused():
        return await utils_command_responses.finish_deferred(interaction, MUSIC_COOLDOWN_NOTICE)
    # Finish the private deferred original before publishing a public followup.
    # Discord otherwise treats the first POST as an edit and preserves privacy.
    ready = await utils_command_responses.edit_deferred(
        interaction, content="Открываю музыкальную панель в канале.", embed=None, view=None, attachments=[])
    if ready is None:
        return
    try:
        payload = await commands_music_player._music_panel_payload(guild_id, channel=interaction.channel)
        panel_message = await v2.followup(interaction,
            **payload,
            view=view,
            wait=True,
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.DiscordException as error:
        print(f"[Music panel] Не удалось открыть панель сервера {guild_id}: {error!r}", flush=True)
        return await utils_command_responses.finish_deferred(
            interaction, "Панель музыки сейчас недоступна.", error=error)
    core_music_state.MUSIC_PLAYER_MESSAGES[guild_id] = await commands_music_player._durable_music_message(panel_message)
    core_music_state.MUSIC_PLAYER_VIEWS[guild_id] = view
    asyncio.create_task(
        commands_music_panels._persist_music_player_reference(guild_id, core_music_state.MUSIC_PLAYER_MESSAGES[guild_id])
    )
    await utils_command_responses.edit_deferred(
        interaction,
        content=None,
        embed=utils_helpers.vivian_embed("Панель музыки открыта в канале.", title="◆ Vivian // MUSIC"),
        view=None,
    )


@core_config.bot.tree.command(name="skip", description="Перейти к следующему треку в очереди.")
async def skip(interaction: discord.Interaction):
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice and (voice.is_playing() or voice.is_paused()):
        try:
            await utils_helpers.vivian_reply(interaction, "Трек пропущен.", title="◆ Vivian // MUSIC", ephemeral=True)
        except discord.HTTPException as error:
            print(f"[Музыка] Не удалось подтвердить /skip: {error!r}")
        core_music_state.MUSIC_SKIP_PENDING.add(interaction.guild.id)
        voice.stop()
        await commands_music_panels._refresh_music_player(interaction.guild.id)
    else:
        await utils_helpers.vivian_reply(interaction, "Сейчас ничего не играет.", error=True, ephemeral=True)


@core_config.bot.tree.command(name="pause", description="Приостановить текущий трек.")
async def pause(interaction: discord.Interaction):
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice and voice.is_playing():
        guild_id = interaction.guild.id
        core_music_state.MUSIC_TRACK_ELAPSED[guild_id] = utils_music._music_position_seconds(guild_id)
        core_music_state.MUSIC_TRACK_STARTED_AT.pop(guild_id, None)
        voice.pause()
        await utils_helpers.vivian_reply(interaction, "Воспроизведение приостановлено.", title="◆ Vivian // MUSIC", ephemeral=True)
        await commands_music_panels._refresh_music_player(guild_id)
    else:
        await utils_helpers.vivian_reply(interaction, "Сейчас нечего приостанавливать.", error=True, ephemeral=True)


@core_config.bot.tree.command(name="resume", description="Продолжить приостановленный трек.")
async def resume(interaction: discord.Interaction):
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice and voice.is_paused():
        voice.resume()
        core_music_state.MUSIC_TRACK_STARTED_AT[interaction.guild.id] = time.monotonic()
        await utils_helpers.vivian_reply(interaction, "Воспроизведение продолжено.", title="◆ Vivian // MUSIC", ephemeral=True)
        await commands_music_panels._refresh_music_player(interaction.guild.id)
    else:
        await utils_helpers.vivian_reply(interaction, "Нет приостановленного трека.", error=True, ephemeral=True)


@core_config.bot.tree.command(name="stop", description="Остановить очередь и отключить Vivian от голосового канала.")
async def stop(interaction: discord.Interaction):
    if interaction.guild is None:
        return await utils_helpers.vivian_reply(interaction, "Vivian не подключена к голосовому каналу.", error=True, ephemeral=True)
    guild_id = interaction.guild.id
    voice = await commands_music_audio._stop_music_session(interaction.guild)
    await commands_music_panels._refresh_music_player(guild_id)
    if voice:
        await utils_helpers.vivian_reply(interaction, "Воспроизведение остановлено.", title="◆ Vivian // MUSIC", ephemeral=True)
    else:
        await utils_helpers.vivian_reply(interaction, "Vivian не подключена к голосовому каналу.", error=True, ephemeral=True)


@core_config.bot.tree.command(name="loop", description="Включить или выключить повтор текущего трека.")
async def loop_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        return await utils_helpers.vivian_reply(interaction, "Повтор доступен только на сервере.", error=True, ephemeral=True)
    guild_id = interaction.guild.id
    core_music_state.loop_status[guild_id] = not core_music_state.loop_status[guild_id]
    state = "включён" if core_music_state.loop_status[guild_id] else "выключен"
    await utils_helpers.vivian_reply(interaction, f"Повтор {state}.", title="◆ Vivian // MUSIC", ephemeral=True)
    await commands_music_panels._refresh_music_player(guild_id)


@core_config.bot.tree.command(name="volume", description="Изменить громкость плеера в диапазоне от 0 до 120%.")
@app_commands.describe(value="Громкость в процентах (0–120)")
async def volume(interaction: discord.Interaction, value: int):
    if interaction.guild is None or interaction.guild.voice_client is None:
        return await utils_helpers.vivian_reply(interaction, "Vivian не подключена к голосовому каналу.", error=True, ephemeral=True)
    if not 0 <= value <= core_music_state.MAX_MUSIC_VOLUME:
        return await utils_helpers.vivian_reply(
            interaction,
            f"Допустимый диапазон: 0–{core_music_state.MAX_MUSIC_VOLUME}.",
            error=True,
            ephemeral=True,
        )
    guild_id = interaction.guild.id
    core_music_state.MUSIC_VOLUME[guild_id] = value
    source = core_music_state.MUSIC_SOURCES.get(guild_id)
    if source:
        source.volume = value / 100
    await utils_helpers.vivian_reply(interaction, f"Громкость установлена: **{value}%**.", title="◆ Vivian // MUSIC", ephemeral=True)
    await commands_music_panels._refresh_music_player(guild_id)
