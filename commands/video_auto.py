"""Automatic opt-in download buttons for public short-video links.

The message observer is intentionally lightweight: no network calls or media
fetching occur until the original message author presses a button.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
from collections import OrderedDict

import discord

from utils import video_downloader as videos

_PROMPT_COOLDOWN_SECONDS = 25
_MAX_RECENT = 2048
_recent: OrderedDict[tuple[int, int], float] = OrderedDict()


def enabled() -> bool:
    return os.getenv("VIVIAN_AUTO_VIDEO_PROMPTS", "true").strip().casefold() not in {
        "false", "0", "off", "no",
    }


def get_video_offer(message):
    """Identify a link without invoking the network or touching Discord state."""
    if not enabled() or getattr(message, "guild", None) is None:
        return None
    content = getattr(message, "content", "") or ""
    if content.lstrip().startswith(("!", "a.")):
        return None
    return videos.find_video_url(content)


class VideoOffer(discord.ui.View):
    def __init__(self, *, author_id: int, request: videos.VideoRequest):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.request = request
        self.offer_message = None
        self._busy = False
        self._done: set[str] = set()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Эти кнопки доступны автору ссылки. Отправь свою ссылку, чтобы скачать ролик.",
                ephemeral=True,
            )
            return False
        return True

    async def _perform(self, interaction: discord.Interaction, mode: str) -> None:
        if mode in self._done:
            return await interaction.response.send_message("Ты уже скачал этот формат.", ephemeral=True)
        if self._busy:
            return await interaction.response.send_message("Другая загрузка ещё выполняется.", ephemeral=True)
        if not interaction.app_permissions.attach_files:
            return await interaction.response.send_message(
                "Мне нужно право «Прикреплять файлы» в этом канале.", ephemeral=True,
            )
        self._busy = True
        try:
            # The file is visible only to the requester, not the whole channel.
            await interaction.response.defer(thinking=True, ephemeral=True)
            limit = getattr(interaction, "filesize_limit", None)
            if limit is None and interaction.guild is not None:
                limit = getattr(interaction.guild, "filesize_limit", None)
            with tempfile.TemporaryDirectory(prefix="vivian-link-") as temporary:
                destination = Path(temporary)
                if mode == "video":
                    result = await videos.fetch_video(self.request.url, destination, limit, quality=480)
                    caption = f"Видео · {result.platform} · {result.size / videos.MIB:.1f} МиБ"
                else:
                    result = await videos.fetch_cover_music(self.request.url, destination, limit)
                    caption = ("Картинка + музыка · " + result.platform + " · "
                               f"{result.size / videos.MIB:.1f} МиБ (MP4)")
                file = discord.File(result.path, filename=result.filename)
                try:
                    await interaction.followup.send(
                        content=caption,
                        file=file,
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                finally:
                    file.close()
            self._done.add(mode)
            for child in self.children:
                if isinstance(child, discord.ui.Button) and (
                    (mode == "video" and child.label == "Скачать видео") or
                    (mode == "cover" and child.label == "Картинка + музыка")
                ):
                    child.disabled = True
            if self.offer_message is not None:
                try:
                    await self.offer_message.edit(view=self)
                except discord.HTTPException:
                    pass
        except videos.DownloadError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send(
                "Discord не принял файл. Попробуй другое видео или более короткий ролик.",
                ephemeral=True,
            )
        except Exception as exc:
            print(f"[VideoAuto] {mode} failed ({type(exc).__name__})", flush=True)
            try:
                await interaction.followup.send(
                    "Не удалось обработать ролик. Проверь FFmpeg, yt-dlp и доступность видео.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        finally:
            self._busy = False

    @discord.ui.button(label="Скачать видео", style=discord.ButtonStyle.primary, emoji="🎬")
    async def video_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._perform(interaction, "video")

    @discord.ui.button(label="Картинка + музыка", style=discord.ButtonStyle.secondary, emoji="🎵")
    async def cover_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._perform(interaction, "cover")

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.offer_message is not None:
            try:
                await self.offer_message.edit(view=self)
            except discord.HTTPException:
                pass


async def maybe_offer_download(message) -> bool:
    """True iff an automatic offer was sent (after moderation approved message)."""
    request = get_video_offer(message)
    if request is None:
        return False
    me = getattr(message.guild, "me", None)
    if me is None:
        return False
    permissions = message.channel.permissions_for(me)
    can_send = (permissions.send_messages_in_threads if isinstance(message.channel, discord.Thread)
                else permissions.send_messages)
    if not permissions.view_channel or not can_send:
        return False
    key = (message.guild.id, message.author.id)
    now = time.monotonic()
    previous = _recent.get(key, 0)
    if now - previous < _PROMPT_COOLDOWN_SECONDS:
        # A recognized link must not go through the chat model accidentally.
        return True
    _recent[key] = now
    _recent.move_to_end(key)
    while len(_recent) > _MAX_RECENT:
        _recent.popitem(last=False)
    view = VideoOffer(author_id=message.author.id, request=request)
    description = (
        f"**{request.platform}** · Выбери, что скачать:\n"
        "🎬 **Видео** — оригинальный ролик.\n"
        "🎵 **Картинка + музыка** — статичная обложка со звуком в MP4 "
        "(для роликов до 3 минут).\n"
        "*Кнопки доступны автору ссылки в течение 3 минут.*"
    )
    try:
        if permissions.read_message_history:
            view.offer_message = await message.reply(
                description, view=view, mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            view.offer_message = await message.channel.send(
                description, view=view, allowed_mentions=discord.AllowedMentions.none(),
            )
        return True
    except discord.HTTPException as exc:
        _recent.pop(key, None)
        print(f"[VideoAuto] offer failed ({type(exc).__name__})", flush=True)
        return False
