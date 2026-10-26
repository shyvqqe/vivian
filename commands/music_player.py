from __future__ import annotations
import asyncio
import discord
from utils import components_v2 as v2
import random
import time
from core import config as core_config
from commands import music_panels as commands_music_panels
from core import music_state as core_music_state
from utils import helpers as utils_helpers
from utils import music as utils_music
from visuals import panels as visuals_panels


def _music_player_embed(guild_id: int) -> discord.Embed:
    guild = core_config.bot.get_guild(guild_id)
    voice = guild.voice_client if guild else None
    track = core_music_state.MUSIC_NOW_PLAYING.get(guild_id)
    queued = list(core_music_state.MUSIC_QUEUES[guild_id])

    if guild_id in core_music_state.MUSIC_SKIP_PENDING:
        playback_state = "SKIPPING"
    elif voice and voice.is_playing():
        playback_state = "PLAYING"
    elif voice and voice.is_paused():
        playback_state = "PAUSED"
    elif guild_id in core_music_state.MUSIC_STARTING or guild_id in core_music_state.MUSIC_RECOVERY_TASKS:
        playback_state = "BUFFERING"
    elif track:
        playback_state = "BUFFERING"
    else:
        playback_state = "IDLE"

    embed = discord.Embed(title="Музыкальная сессия", color=core_music_state.MUSIC_PANEL_COLOR)
    utils_helpers.vivian_brand(embed, "Vivian / AUDIO INTERFACE")
    embed.description = f"SESSION  ·  `{playback_state}`"
    if track:
        title = discord.utils.escape_markdown(str(track.get("title") or "Без названия")[:140])
        url = track.get("webpage_url")
        artist = discord.utils.escape_markdown(str(track.get("artist") or "Исполнитель неизвестен")[:100])
        source = str(track.get("source") or "SoundCloud")[:40]
        track_line = f"[{title}]({url})" if url else title
        duration = track.get("duration")
        position = utils_music._music_position_seconds(guild_id)
        if isinstance(duration, (int, float)) and duration > 0:
            segments = 18
            filled = min(segments, round(segments * position / duration))
            bar = "▰" * filled + "▱" * (segments - filled)
            progress = (
                f"`{utils_music._track_duration_label(position)}` {bar} "
                f"`{utils_music._track_duration_label(duration)}`"
            )
        else:
            progress = f"`{utils_music._track_duration_label(position)}` · длительность неизвестна"
        embed.add_field(
            name="СЕЙЧАС ИГРАЕТ",
            value=f"{track_line}\n{artist}  ·  {source}\n{progress}",
            inline=False,
        )
        thumbnail = track.get("thumbnail")
        if isinstance(thumbnail, str) and thumbnail.startswith(("https://", "http://")):
            embed.set_thumbnail(url=thumbnail)
    else:
        embed.add_field(
            name="СЕЙЧАС ИГРАЕТ",
            value="Тишина. Включи музыку через `!play название`.",
            inline=False,
        )

    if queued:
        queue_lines = []
        for index, queued_track in enumerate(queued[:5], 1):
            queued_title = str(queued_track.get("title") or "Без названия")[:80]
            queued_source = str(queued_track.get("source") or "SoundCloud")[:20]
            queue_lines.append(f"`{index:02}` {queued_title}  ·  {queued_source}")
        if len(queued) > 5:
            queue_lines.append(f"… ещё {len(queued) - 5}")
        queue_text = "\n".join(queue_lines)
    else:
        queue_text = "Следующих треков нет."
    embed.add_field(name=f"ОЧЕРЕДЬ  ·  {len(queued)}", value=queue_text[:1024], inline=False)

    loop_label = "вкл" if core_music_state.loop_status[guild_id] else "выкл"
    shuffle_label = "вкл" if core_music_state.MUSIC_SHUFFLE_MODE[guild_id] else "выкл"
    embed.add_field(
        name="СОСТОЯНИЕ",
        value=(
            f"`{playback_state}`  ·  повтор {loop_label}  ·  перемешивание {shuffle_label}"
            f"  ·  громкость {min(core_music_state.MUSIC_VOLUME[guild_id], core_music_state.MAX_MUSIC_VOLUME)}%"
        ),
        inline=False,
    )
    embed.set_footer(text="Кнопки управляют очередью сервера · Музыка: !play название")
    return embed


async def _music_panel_payload(guild_id: int, message=None, channel=None) -> dict:
    """Reuse uploads on timer edits; replace the visual only when playback changes."""
    embed = _music_player_embed(guild_id)
    guild = core_config.bot.get_guild(guild_id)
    voice = guild.voice_client if guild else None
    if voice and voice.is_playing() and core_music_state.MUSIC_NOW_PLAYING.get(guild_id):
        style = "player"
    elif voice and voice.is_paused() and core_music_state.MUSIC_NOW_PLAYING.get(guild_id):
        style = "paused"
    else:
        style = "idle"
    filename = visuals_panels.PANEL_VISUAL_FILENAMES[style]
    previous_attachments = list(getattr(message, "attachments", ()) or ())
    if any(attachment.filename == filename for attachment in previous_attachments):
        return {"embed": visuals_panels._apply_panel_visual(embed, style)}
    target_channel = message.channel if message is not None else channel
    visual = None
    if visuals_panels._can_upload_panel_visual(guild, target_channel):
        visual = await visuals_panels._panel_visual_file(style)
    payload = {"embed": embed}
    if visual is not None:
        visuals_panels._apply_panel_visual(embed, style)
    if message is None:
        payload["files"] = [visual] if visual else []
    else:
        kept = [attachment for attachment in previous_attachments
                if attachment.filename not in visuals_panels.PANEL_VISUAL_ATTACHMENT_NAMES]
        payload["attachments"] = kept + ([visual] if visual else [])
    return payload


async def _durable_music_message(message):
    # Public panels must use the bot token after the command token expires.
    if isinstance(message, (discord.WebhookMessage, discord.InteractionMessage)):
        try:
            return await message.channel.fetch_message(message.id)
        except discord.DiscordException:
            # Editing our own message works even without Read Message History.
            return message.channel.get_partial_message(message.id)
    return message


def _music_player_can_control(interaction: discord.Interaction, guild: discord.Guild) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    if getattr(permissions, "manage_guild", False):
        return True
    voice = guild.voice_client
    user_voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
    return voice is not None and voice.is_connected() and user_voice == voice.channel


class MusicPlayerView(v2.ControllerView):
    def __init__(self, guild_id: int | None = None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.sync_state()

    def sync_state(self) -> None:
        if self.guild_id is None:
            return
        guild = core_config.bot.get_guild(self.guild_id)
        voice = guild.voice_client if guild else None
        playing = bool(voice and voice.is_playing())
        paused = bool(voice and voice.is_paused())
        active = bool(core_music_state.MUSIC_NOW_PLAYING.get(self.guild_id))
        self.pause_resume.label = "▶ Продолжить" if paused else "Ⅱ Пауза"
        self.pause_resume.style = discord.ButtonStyle.success if paused else discord.ButtonStyle.primary
        self.pause_resume.disabled = not active or not (playing or paused)
        self.skip_track.disabled = not active or not (playing or paused)
        self.loop_track.label = "↻ Повтор: вкл" if core_music_state.loop_status[self.guild_id] else "↻ Повтор: выкл"
        self.loop_track.style = (
            discord.ButtonStyle.success if core_music_state.loop_status[self.guild_id] else discord.ButtonStyle.secondary
        )
        self.shuffle_queue.label = (
            "⇄ Перемешать: вкл" if core_music_state.MUSIC_SHUFFLE_MODE[self.guild_id] else "⇄ Перемешать: выкл"
        )
        self.shuffle_queue.style = (
            discord.ButtonStyle.success if core_music_state.MUSIC_SHUFFLE_MODE[self.guild_id] else discord.ButtonStyle.secondary
        )

    async def _prepare_control(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await utils_helpers.vivian_reply(interaction, "Панель музыки работает только на сервере.", error=True, ephemeral=True)
            return None
        if not _music_player_can_control(interaction, guild):
            await utils_helpers.vivian_reply(
                interaction,
                "Войди в тот же голосовой канал, что и Vivian; manage_guild позволяет управлять из любого канала.",
                error=True,
                ephemeral=True,
            )
            return None
        self.guild_id = guild.id
        core_music_state.MUSIC_PLAYER_MESSAGES[guild.id] = interaction.message
        core_music_state.MUSIC_PLAYER_VIEWS[guild.id] = self
        asyncio.create_task(commands_music_panels._persist_music_player_reference(guild.id, interaction.message))
        return guild

    async def _edit_panel(self, interaction: discord.Interaction, guild: discord.Guild) -> None:
        self.sync_state()
        try:
            payload = await _music_panel_payload(guild.id, interaction.message)
            response = await v2.edit_response(interaction, **payload, view=self)
            updated_message = getattr(response, "resource", None)
            if isinstance(updated_message, discord.Message):
                core_music_state.MUSIC_PLAYER_MESSAGES[guild.id] = await _durable_music_message(updated_message)
        except (discord.NotFound, discord.HTTPException):
            return

    @discord.ui.button(
        label="Ⅱ Пауза",
        style=discord.ButtonStyle.primary,
        custom_id="vivian_music_pause_resume",
        row=0,
    )
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = await self._prepare_control(interaction)
        if guild is None:
            return
        voice = guild.voice_client
        if voice and voice.is_playing():
            core_music_state.MUSIC_TRACK_ELAPSED[guild.id] = utils_music._music_position_seconds(guild.id)
            core_music_state.MUSIC_TRACK_STARTED_AT.pop(guild.id, None)
            voice.pause()
        elif voice and voice.is_paused():
            voice.resume()
            core_music_state.MUSIC_TRACK_STARTED_AT[guild.id] = time.monotonic()
        else:
            return await utils_helpers.vivian_reply(interaction, "Сейчас нечего приостанавливать.", error=True, ephemeral=True)
        await self._edit_panel(interaction, guild)

    @discord.ui.button(
        label="› Следующий",
        style=discord.ButtonStyle.secondary,
        custom_id="vivian_music_skip",
        row=0,
    )
    async def skip_track(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = await self._prepare_control(interaction)
        if guild is None:
            return
        voice = guild.voice_client
        if voice and (voice.is_playing() or voice.is_paused()):
            core_music_state.MUSIC_SKIP_PENDING.add(guild.id)
            voice.stop()
            await self._edit_panel(interaction, guild)
        else:
            return await utils_helpers.vivian_reply(interaction, "Сейчас нечего пропускать.", error=True, ephemeral=True)

    @discord.ui.button(
        label="↻ Повтор: выкл",
        style=discord.ButtonStyle.secondary,
        custom_id="vivian_music_loop",
        row=0,
    )
    async def loop_track(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = await self._prepare_control(interaction)
        if guild is None:
            return
        core_music_state.loop_status[guild.id] = not core_music_state.loop_status[guild.id]
        await self._edit_panel(interaction, guild)

    @discord.ui.button(
        label="⇄ Перемешать: выкл",
        style=discord.ButtonStyle.secondary,
        custom_id="vivian_music_shuffle",
        row=0,
    )
    async def shuffle_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = await self._prepare_control(interaction)
        if guild is None:
            return
        core_music_state.MUSIC_SHUFFLE_MODE[guild.id] = not core_music_state.MUSIC_SHUFFLE_MODE[guild.id]
        if core_music_state.MUSIC_SHUFFLE_MODE[guild.id] and len(core_music_state.MUSIC_QUEUES[guild.id]) > 1:
            shuffled = list(core_music_state.MUSIC_QUEUES[guild.id])
            random.shuffle(shuffled)
            core_music_state.MUSIC_QUEUES[guild.id].clear()
            core_music_state.MUSIC_QUEUES[guild.id].extend(shuffled)
        await self._edit_panel(interaction, guild)
