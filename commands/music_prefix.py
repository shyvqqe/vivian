
from __future__ import annotations

from dataclasses import dataclass

import discord

from commands import music_audio, music_panels, music_relay, music_search
from utils import components_v2 as v2, helpers, music as music_utils
from visuals import panels


SOURCE_ALIASES = {
    "youtube": "youtube", "yt": "youtube", "ютуб": "youtube",
    "soundcloud": "soundcloud", "soundclode": "soundcloud", "sc": "soundcloud",
}


@dataclass(frozen=True)
class PlaybackResult:
    started: bool
    text: str
    track: dict | None = None


def _voice_channel(ctx):
    return getattr(getattr(ctx.author, "voice", None), "channel", None)


def _card(ctx, text, *, title="Музыка Vivian", query=None, source=None, track=None, error=False):
    embed = helpers.vivian_embed(text, title=title, error=error)
    if query:
        safe_query = discord.utils.escape_markdown(query.replace("\n", " ")[:200])
        embed.add_field(name="ТВОЙ ЗАПРОС", value=safe_query, inline=False)
    if source:
        embed.add_field(name="ИСТОЧНИК", value="SoundCloud" if source == "soundcloud" else "YouTube", inline=True)
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
        if panels._can_upload_panel_visual(ctx.guild, ctx.channel):
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


async def _start(ctx, query, source):
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
        results = await music_audio._extract_in_executor(music_relay._relay_search, query, source, 1)
        if not results:
            return PlaybackResult(False, "По этому запросу ничего не найдено.")
        track = results[0]
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
        await _send(ctx, "сначала зайди в голосовой", error=True, title="Нужен голосовой канал")
        return
    query = query.strip()
    first, separator, rest = query.partition(" ")
    source = SOURCE_ALIASES.get(first.casefold())
    if source:
        query = rest.strip()
    if not query or len(query) > 200:
        await _send(ctx, "Напиши запрос от 1 до 200 символов: `!play название песни`",
                    error=True, title="Какую песню включить?")
        return
    if music_search._is_youtube_url(query):
        source = "youtube"
    elif music_search._is_soundcloud_url(query):
        source = "soundcloud"
    if source:
        result = await _start(ctx, query, source)
        await _send(ctx, result.text, title="На волне" if result.started else "Музыка не включилась",
                    source=source, track=result.track, error=not result.started)
        return
    view = MusicSourceView(ctx, query)
    view.message = await _send(
        ctx, "Выбери платформу ниже. Найденный трек заменит текущую музыку.",
        title="Один трек · два источника", query=query, view=view,
    )
    if view.message is None:
        view.stop()


async def stop_prefix_command(ctx):
    if ctx.guild is None:
        await _send(ctx, "Музыка доступна только на сервере.", error=True)
        return
    try:
        await music_audio._stop_music_session(ctx.guild)
        await music_panels._refresh_music_player(ctx.guild.id)
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