
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import time

import discord

from commands import music_audio, music_panels, music_relay, music_search, music_links
from core import music_state
from utils import components_v2 as v2, helpers, music as music_utils
from visuals import panels


SOURCE_ALIASES = music_links.ALIASES


@dataclass(frozen=True)
class PlaybackResult:
    started: bool
    text: str
    track: dict | None = None


def _voice_channel(ctx):
    return getattr(getattr(ctx.author, "voice", None), "channel", None)


def _request_parts(query):
    return music_links.resolve_music_request(query)


def _first_search(query, source, fallback_source):
    try:
        tracks = music_relay._relay_search(query, source, 1)
    except music_relay.RelayError:
        if not fallback_source:
            raise
        tracks = []
    if not tracks and fallback_source:
        return music_relay._relay_search(query, fallback_source, 1)
    return tracks


def _card(ctx, text, *, title="Музыка Vivian", query=None, source=None, track=None, error=False):
    embed = helpers.vivian_embed(text, title=title, error=error)
    if query:
        safe_query = discord.utils.escape_markdown(query.replace("\n", " ")[:200])
        embed.add_field(name="ТВОЙ ЗАПРОС", value=safe_query, inline=False)
    if source:
        embed.add_field(name="АУДИО", value="SoundCloud" if source == "soundcloud" else "YouTube", inline=True)
    if track and track.get("requested_from"):
        embed.add_field(name="НАЙДЕНО ПО ССЫЛКЕ", value=str(track["requested_from"])[:100], inline=True)
    channel = _voice_channel(ctx)
    if channel is not None:
        embed.add_field(name="ГОЛОСОВОЙ", value=channel.mention, inline=True)
    if track:
        artist = discord.utils.escape_markdown(str(track.get("artist") or "Исполнитель неизвестен")[:200])
        duration = music_utils._track_duration_label(track.get("duration"))
        embed.add_field(name="ТРЕК", value=f"{artist} · {duration}", inline=False)
        thumbnail = track.get("thumbnail")
        if isinstance(thumbnail, str) and thumbnail.startswith("https://"):
            embed.set_thumbnail(url=thumbnail)
    embed.set_footer(text="!play — включить · /queue — плеер · !stop — выйти из голосового · VIVIAN")
    return embed


async def _send(ctx, text, *, view=None, **card_options):
    try:
        embed = _card(ctx, text, **card_options)
        visual = None
        if view is not None and panels._can_upload_panel_visual(ctx.guild, ctx.channel):
            visual = await panels._panel_visual_file("music")
        if visual is not None:
            panels._apply_panel_visual(embed, "music")
        if view is not None:
            view.visual_enabled = visual is not None
        return await v2.send_message(
            ctx, embed=embed, view=view, files=[visual] if visual else [],
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.DiscordException as error:
        print(f"[Music prefix] Reply failed; error={type(error).__name__}", flush=True)


async def _start(ctx, query, source, *, fallback_source=None, keep_channel=False, requested_from=None):
    channel = _voice_channel(ctx)
    if channel is None:
        return PlaybackResult(False, "сначала зайди в голосовой")
    if ctx.guild is None:
        return PlaybackResult(False, "Музыка доступна только на сервере.")
    if ctx.guild.me is not None:
        permissions = channel.permissions_for(ctx.guild.me)
        if not permissions.connect or not permissions.speak:
            return PlaybackResult(False, "Разреши боту Connect и Speak в голосовом канале.")
    try:
        # Resolve first; the existing backend records which transport succeeded.
        generation = music_state.MUSIC_GENERATION[ctx.guild.id] if keep_channel else None
        results = await music_audio._extract_in_executor(_first_search, query, source, fallback_source)
        if not results:
            return PlaybackResult(False, "По этому запросу ничего не найдено.")
        track = results[0]
        if requested_from:
            track["requested_from"] = requested_from
        if keep_channel:
            started = await music_audio.replace_current_track(
                ctx.guild, channel, track, requester=ctx.author, expected_generation=generation,
            )
        else:
            started = await music_audio.replace_current_track(ctx.guild, channel, track)
    except (ValueError, music_relay.RelayError) as error:
        return PlaybackResult(False, str(error))
    except Exception as error:
        print(f"[Music prefix] Start failed; guild_id={ctx.guild.id}; error={type(error).__name__}", flush=True)
        return PlaybackResult(False, "Не удалось включить музыку. Проверь источник и подключение к голосовому.")
    if not started:
        return PlaybackResult(False, "Воспроизведение не началось или уже заменено другой командой.")
    title = discord.utils.escape_markdown(str(track.get("title") or query)[:300])
    return PlaybackResult(True, f"играю: **{title}**", track)


class MusicSourceView(v2.ControllerView):
    def __init__(self, ctx, query):
        super().__init__(timeout=120)
        self.ctx, self.query = ctx, query
        self.busy = False
        self.message = None
        self.visual_enabled = False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await v2.send_response(interaction, "Открой свой выбор командой !play.", ephemeral=True)
            return False
        if self.busy:
            await v2.send_response(interaction, "Уже подключаю музыку.", ephemeral=True)
            return False
        return True

    async def choose(self, interaction, source):
        if interaction.user.id != self.ctx.author.id or self.busy:
            return
        self.busy = True
        await interaction.response.defer()
        for item in self.children:
            item.disabled = True
        try:
            embed = _card(self.ctx, "Ищу трек и подключаю голосовой канал…",
                          title="Подключаю музыку", query=self.query, source=source)
            if self.visual_enabled:
                panels._apply_panel_visual(embed, "music")
            await v2.edit_original(interaction, embed=embed, view=self)
            result = await _start(self.ctx, self.query, source)
            embed = _card(self.ctx, result.text, title="На волне" if result.started else "Музыка не включилась",
                          source=source, track=result.track, error=not result.started)
            if self.visual_enabled:
                panels._apply_panel_visual(embed, "music")
            await v2.edit_original(interaction, embed=embed, view=None,
                                   allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException as error:
            print(f"[Music prefix] Source response failed; error={type(error).__name__}", flush=True)
        finally:
            self.stop()

    @discord.ui.button(label="SoundCloud", style=discord.ButtonStyle.secondary)
    async def soundcloud(self, interaction, button):
        await self.choose(interaction, "soundcloud")

    @discord.ui.button(label="YouTube", style=discord.ButtonStyle.primary)
    async def youtube(self, interaction, button):
        await self.choose(interaction, "youtube")

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await v2.edit_response(interaction, embed=_card(
            self.ctx, "Запрос закрыт. Новый трек можно выбрать через `!play название`.", title="Выбор отменён",
        ), view=None)
        self.stop()

    async def on_timeout(self):
        if self.message is not None:
            try:
                await v2.edit_message(self.message, embed=_card(
                    self.ctx, "Выбор истёк. Напиши `!play название` ещё раз.",
                    title="Выбор источника завершён",
                ), view=None)
            except discord.DiscordException:
                pass


async def play_prefix_command(ctx, query):
    if _voice_channel(ctx) is None:
        await _send(ctx, "Сначала зайди в голосовой канал.", error=True, title="Нужен голосовой канал")
        return
    try:
        # Third-party link metadata performs HTTP I/O; never block Discord's event loop.
        query, source, requested_from = await music_audio._extract_in_executor(_request_parts, query)
    except ValueError as error:
        await _send(ctx, str(error), error=True, title="Какую песню включить?")
        return
    result = await _start(ctx, query, source, requested_from=requested_from)
    if result.track and requested_from:
        result_text = result.text + f"\nПо запросу из **{requested_from}**. Звук из **YouTube**."
    else:
        result_text = result.text
    await _send(ctx, result_text, title="На волне" if result.started else "Музыка не включилась",
                source=source, track=result.track, error=not result.started)


async def stop_prefix_command(ctx):
    if ctx.guild is None:
        await _send(ctx, "Музыка доступна только на сервере.", error=True)
        return
    try:
        await music_audio._stop_music_session(ctx.guild)
        _refresh_later(ctx.guild.id)
    except discord.DiscordException as error:
        print(f"[Music prefix] Stop failed; error={type(error).__name__}", flush=True)
        await _send(ctx, "Не удалось отключиться от голосового канала.", error=True)
        return
    await _send(ctx, "Воспроизведение остановлено. Вышла из голосового канала.", title="Сессия завершена")
from core import config

@config.bot.command(name="play")
async def play(ctx, *, query: str = ""):
    await play_prefix_command(ctx, query)

@config.bot.command(name="stop")
async def stop(ctx):
    await stop_prefix_command(ctx)


# Lightweight text controls.  The slash controls remain available unchanged.
# Panel refresh is scheduled after the acknowledgement so a Discord PATCH
# cannot delay a quick pause/skip/resume/volume reply.
def _refresh_later(guild_id: int) -> None:
    asyncio.create_task(music_panels._refresh_music_player(guild_id))


@config.bot.command(name="skip")
async def skip_prefix(ctx):
    if ctx.guild is None:
        return await _send(ctx, "Команда доступна только на сервере.", error=True)
    voice = ctx.guild.voice_client
    if not voice or not (voice.is_playing() or voice.is_paused()):
        return await _send(ctx, "Сейчас ничего не играет.", error=True)
    music_state.MUSIC_SKIP_PENDING.add(ctx.guild.id)
    voice.stop()
    await _send(ctx, "Пропускаю трек.")
    _refresh_later(ctx.guild.id)


@config.bot.command(name="pause")
async def pause_prefix(ctx):
    if ctx.guild is None:
        return await _send(ctx, "Команда доступна только на сервере.", error=True)
    voice = ctx.guild.voice_client
    if not voice or not voice.is_playing():
        return await _send(ctx, "Сейчас нечего приостанавливать.", error=True)
    guild_id = ctx.guild.id
    music_state.MUSIC_TRACK_ELAPSED[guild_id] = music_utils._music_position_seconds(guild_id)
    music_state.MUSIC_TRACK_STARTED_AT.pop(guild_id, None)
    voice.pause()
    await _send(ctx, "Воспроизведение приостановлено.")
    _refresh_later(guild_id)


@config.bot.command(name="resume")
async def resume_prefix(ctx):
    if ctx.guild is None:
        return await _send(ctx, "Команда доступна только на сервере.", error=True)
    voice = ctx.guild.voice_client
    if not voice or not voice.is_paused():
        return await _send(ctx, "Сейчас нет трека на паузе.", error=True)
    voice.resume()
    music_state.MUSIC_TRACK_STARTED_AT[ctx.guild.id] = time.monotonic()
    await _send(ctx, "Воспроизведение продолжено.")
    _refresh_later(ctx.guild.id)


@config.bot.command(name="volume")
async def volume_prefix(ctx, value: int):
    if ctx.guild is None or ctx.guild.voice_client is None:
        return await _send(ctx, "Vivian не подключена к голосовому каналу.", error=True)
    if not 0 <= value <= music_state.MAX_MUSIC_VOLUME:
        return await _send(ctx, f"Выбери громкость от 0 до {music_state.MAX_MUSIC_VOLUME}%.", error=True)
    music_state.MUSIC_VOLUME[ctx.guild.id] = value
    source = music_state.MUSIC_SOURCES.get(ctx.guild.id)
    if source is not None:
        source.volume = value / 100
    await _send(ctx, f"Громкость: **{value}%**.")
    _refresh_later(ctx.guild.id)


@config.bot.command(name="loop")
async def loop_prefix(ctx):
    if ctx.guild is None:
        return await _send(ctx, "Команда доступна только на сервере.", error=True)
    music_state.loop_status[ctx.guild.id] = not music_state.loop_status[ctx.guild.id]
    state = "включён" if music_state.loop_status[ctx.guild.id] else "выключен"
    await _send(ctx, f"Повтор {state}.")
    _refresh_later(ctx.guild.id)


@config.bot.command(name="queue")
async def queue_prefix(ctx):
    if ctx.guild is None:
        return await _send(ctx, "Очередь доступна только на сервере.", error=True)
    current = music_state.MUSIC_NOW_PLAYING.get(ctx.guild.id)
    waiting = len(music_state.MUSIC_QUEUES[ctx.guild.id])
    if current:
        title = discord.utils.escape_markdown(str(current.get("title") or "Неизвестный трек")[:180])
        await _send(ctx, f"Сейчас играет **{title}**. В очереди: **{waiting}**. Для панели используй `/queue`.")
    else:
        await _send(ctx, f"Сейчас ничего не играет. В очереди: **{waiting}**.")


@volume_prefix.error
async def _on_volume_prefix_error(ctx, error):
    from discord.ext import commands as discord_commands
    if isinstance(error, (discord_commands.MissingRequiredArgument, discord_commands.BadArgument)):
        await _send(ctx, "Используй `!volume 0..120`, например `!volume 80`.", error=True)
    else:
        await _send(ctx, "Громкость не изменилась. Проверь команду и права Vivian.", error=True)
