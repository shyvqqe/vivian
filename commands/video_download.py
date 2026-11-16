"""Public video downloads: TikTok, Facebook Reels, Instagram Reels, YouTube.

Slash: /download url [quality]     Prefix: !download url [360|480|720]
Only public videos that may be lawfully downloaded and shared are supported.
"""
from __future__ import annotations

from functools import partial
from pathlib import Path
import tempfile

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from utils import command_responses, components_v2 as v2, video_downloader as videos


async def _deliver(channel_send, *, url: str, quality: int, limit: int | None):
    """Shared download and upload path; TempDirectory lives through upload."""
    with tempfile.TemporaryDirectory(prefix="vivian-video-") as temp:
        video = await videos.fetch_video(url, Path(temp), limit, quality=quality)
        file = discord.File(video.path, filename=video.filename)
        try:
            return await channel_send(
                content=f"Видео: **{video.platform}** · {video.size / videos.MIB:.1f} МиБ",
                file=file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        finally:
            file.close()


@config.bot.tree.command(name="download", description="Скачать публичное видео TikTok, Facebook Reels, Instagram или YouTube.")
@app_commands.guild_only()
@app_commands.describe(url="HTTPS-ссылка на один публичный ролик", quality="Качество до 360, 480 или 720p")
@app_commands.choices(quality=[
    app_commands.Choice(name="360p (экономный размер)", value=360),
    app_commands.Choice(name="480p (по умолчанию)", value=480),
    app_commands.Choice(name="720p (может не пройти лимит Discord)", value=720),
])
@app_commands.checks.cooldown(1, 20, key=lambda interaction: (interaction.guild_id, interaction.user.id))
async def download_video_slash(interaction: discord.Interaction, url: str, quality: int = 480):
    try:
        videos.validate_video_url(url)
    except videos.DownloadError as exc:
        return await v2.send_response(interaction, str(exc), ephemeral=True)
    permissions = interaction.app_permissions
    channel = interaction.channel
    can_send = permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages
    if channel is None or not (can_send and permissions.attach_files):
        return await v2.send_response(
            interaction, "Vivian нужно право отправлять сообщения и прикреплять файлы в этом канале.", ephemeral=True,
        )
    if not await command_responses.defer_command(interaction, ephemeral=True):
        return
    try:
        message = await _deliver(
            partial(v2.send_message, channel, nonce=f"vd:{interaction.id}"), url=url, quality=quality,
            limit=getattr(interaction, "filesize_limit", None),
        )
    except videos.DownloadError as exc:
        await command_responses.finish_deferred(interaction, str(exc))
    except discord.DiscordException as exc:
        print(f"[Video download] Discord upload error: {type(exc).__name__}", flush=True)
        await command_responses.finish_deferred(
            interaction, "Discord не принял файл. Попробуй качество 360 или другой ролик.", error=exc,
        )
    except Exception as exc:
        print(f"[Video download] Unhandled error: {type(exc).__name__}", flush=True)
        await command_responses.finish_deferred(
            interaction, "Не удалось обработать ролик. Проверь установку yt-dlp и FFmpeg.",
        )
    else:
        await command_responses.finish_deferred(interaction, f"[Перейти к видео]({message.jump_url})")


@download_video_slash.error
async def download_video_slash_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"Подожди {error.retry_after:.0f} сек. перед следующей загрузкой."
    else:
        print(f"[Video download] Slash check error: {type(error).__name__}", flush=True)
        message = "Команда сейчас недоступна. Проверь права Vivian в канале."
    await command_responses.finish_deferred(interaction, message)


async def _prefix_reply(ctx: commands.Context, text: str):
    await v2.send_message(ctx, text, reference=ctx.message, mention_author=False)


@config.bot.command(name="download", aliases=["dl", "video"])
@commands.guild_only()
@commands.cooldown(1, 20, commands.BucketType.user)
@commands.bot_has_permissions(attach_files=True)
async def download_video_prefix(ctx: commands.Context, url: str, quality: int = 480):
    """!download https://www.tiktok.com/@user/video/123 480"""
    try:
        videos.validate_video_url(url)
        if quality not in (360, 480, 720):
            raise videos.DownloadError("Выбери качество 360, 480 или 720.")
        async with ctx.typing():
            await _deliver(
                partial(v2.send_message, ctx), url=url, quality=quality,
                limit=getattr(ctx.guild, "filesize_limit", None),
            )
    except videos.DownloadError as exc:
        await _prefix_reply(ctx, str(exc))
    except discord.HTTPException as exc:
        print(f"[Video download] Prefix upload error: {type(exc).__name__}", flush=True)
        await _prefix_reply(ctx, "Discord не принял файл. Попробуй качество 360.")
    except Exception as exc:
        print(f"[Video download] Prefix error: {type(exc).__name__}", flush=True)
        await _prefix_reply(ctx, "Не получилось скачать ролик. Проверь yt-dlp и доступность видео.")


@download_video_prefix.error
async def download_video_prefix_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandOnCooldown):
        message = f"Подожди {error.retry_after:.0f} сек. перед следующей загрузкой."
    elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        message = "Пример: `!download https://youtu.be/dQw4w9WgXcQ 480`"
    elif isinstance(error, commands.BotMissingPermissions):
        message = "Дай Vivian право «Прикреплять файлы» в канале."
    elif isinstance(error, commands.NoPrivateMessage):
        message = "Команда доступна только на сервере."
    else:
        print(f"[Video download] Prefix check error: {type(error).__name__}", flush=True)
        message = "Не удалось запустить скачивание."
    await _prefix_reply(ctx, message)
