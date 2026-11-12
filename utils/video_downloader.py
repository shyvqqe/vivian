"""Bounded public-video downloads for Discord attachments.

Only explicitly approved public video pages are accepted.  yt-dlp is invoked
without shell expansion, account cookies or authentication.  A subprocess is
used so a stalled extractor can actually be killed (unlike to_thread timeouts).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import os
import sys
import time
from urllib.parse import parse_qs, urlsplit

MIB = 1024 * 1024
DEFAULT_MAX_BYTES = 20 * MIB
MAX_CONFIGURED_BYTES = 100 * MIB
DEFAULT_TIMEOUT_SECONDS = 95
MAX_TIMEOUT_SECONDS = 180
CONCURRENT_DOWNLOADS = 2
_DOWNLOAD_SLOTS = asyncio.Semaphore(CONCURRENT_DOWNLOADS)


class DownloadError(Exception):
    """A safe, user-facing download failure."""


class DownloadBusy(DownloadError):
    pass


class DownloadTooLarge(DownloadError):
    pass


@dataclass(frozen=True)
class VideoRequest:
    url: str
    platform: str


@dataclass(frozen=True)
class DownloadedVideo:
    path: Path
    platform: str
    size: int
    filename: str


def _positive_int_env(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default
    return max(1, min(value, maximum))


def upload_limit(discord_limit: int | None = None) -> int:
    """Never assume the server allows bigger uploads than Discord says."""
    configured = _positive_int_env("VIVIAN_VIDEO_MAX_MB", 20, 100) * MIB
    if isinstance(discord_limit, int) and discord_limit > 0:
        return min(configured, discord_limit)
    return configured


def timeout_seconds() -> int:
    return _positive_int_env("VIVIAN_VIDEO_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)


def validate_video_url(raw: str) -> VideoRequest:
    """Block SSRF-style URLs, playlists and unsupported sites before yt-dlp."""
    if not isinstance(raw, str) or not raw or len(raw) > 2048:
        raise DownloadError("Нужна ссылка на одно публичное видео длиной не более 2048 символов.")
    value = raw.strip()
    if value != raw or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise DownloadError("Вставь одну ссылку без лишнего текста.")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError as exc:
        raise DownloadError("Некорректная ссылка на видео.") from exc
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password or port is not None:
        raise DownloadError("Принимаются только обычные HTTPS-ссылки на видео.")
    path = parts.path.lower()
    query = parse_qs(parts.query)

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if path == "/watch" and query.get("v") and len(query["v"][0]) == 11:
            return VideoRequest(value, "YouTube")
        if path.startswith("/shorts/") and len(path.split("/")[2]) == 11:
            return VideoRequest(value, "YouTube Shorts")
    elif host == "youtu.be":
        if len(path.lstrip("/").split("/")[0]) == 11:
            return VideoRequest(value, "YouTube")
    elif host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
        if ("/video/" in path and path.startswith("/@")) or path.startswith("/t/"):
            return VideoRequest(value, "TikTok")
    elif host in {"vm.tiktok.com", "vt.tiktok.com"}:
        if len(path.strip("/")) >= 4:
            return VideoRequest(value, "TikTok")
    elif host in {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}:
        if path.startswith(("/reel/", "/share/r/", "/videos/", "/watch/")) or (path == "/watch" and query.get("v")):
            return VideoRequest(value, "Facebook")
    elif host == "fb.watch":
        if path.strip("/"):
            return VideoRequest(value, "Facebook")
    elif host in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
        if path.startswith(("/reel/", "/reels/", "/p/")):
            return VideoRequest(value, "Instagram")

    raise DownloadError(
        "Поддерживаются ссылки на отдельные видео TikTok, Facebook Reels, "
        "Instagram Reels, YouTube и Shorts. Плейлисты и другие сайты не принимаются."
    )


def format_selector(height: int = 480) -> str:
    """Prefer a single audio+video file; avoid merge/transcode time."""
    if height not in {360, 480, 720}:
        raise DownloadError("Качество должно быть 360, 480 или 720.")
    # `best` in yt-dlp means a combined audio/video format (no merging).
    return (f"best[ext=mp4][height<={height}]/"
            f"best[height<={height}]/best[ext=mp4]/best")


def _total_directory_size(directory: Path) -> int:
    total = 0
    for root, _, files in os.walk(directory):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except (OSError, FileNotFoundError):
                pass
    return total


def _safe_message(stderr: str) -> str:
    message = stderr.lower()
    if "no module named yt_dlp" in message:
        return "На сервере не установлен yt-dlp. Установи зависимости из requirements.txt."
    if "unsupported url" in message or "no suitable extractor" in message:
        return "Эту ссылку не удалось обработать. Проверь, что она ведёт на один публичный ролик."
    if any(word in message for word in ("login", "sign in", "private", "cookies", "not available")):
        return "Ролик закрыт, недоступен в регионе или требует входа в аккаунт."
    if "requested format is not available" in message:
        return "Для ролика нет подходящего видео со звуком. Попробуй другое качество."
    if "http error 429" in message or "too many requests" in message:
        return "Источник ограничил частоту запросов. Попробуй позже."
    if "max-filesize" in message or "maximum file size" in message or "larger than max" in message:
        return "Видео слишком большое для отправки в Discord. Попробуй качество 360."
    return "Не удалось скачать ролик: источник не ответил или ограничил доступ."


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _run_download(request: VideoRequest, output_dir: Path, max_bytes: int, height: int) -> DownloadedVideo:
    limit = max(1, int(max_bytes))
    args = (
        sys.executable, "-m", "yt_dlp",
        "--ignore-config", "--no-cache-dir", "--no-playlist", "--quiet", "--no-warnings",
        "--no-mtime", "--no-part", "--retries", "1", "--fragment-retries", "1",
        "--extractor-retries", "1", "--socket-timeout", "8",
        "--max-filesize", str(limit),
        "--format", format_selector(height),
        "--output", str(output_dir / "video.%(ext)s"),
        request.url,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(output_dir),
        )
    except (OSError, FileNotFoundError) as exc:
        raise DownloadError("Не удалось запустить yt-dlp на сервере.") from exc

    completed = asyncio.create_task(process.communicate())
    deadline = time.monotonic() + timeout_seconds()
    try:
        while not completed.done():
            if _total_directory_size(output_dir) > limit:
                raise DownloadTooLarge("Видео слишком большое для Discord. Попробуй качество 360.")
            if time.monotonic() > deadline:
                raise DownloadError("Источник отвечает слишком долго. Попробуй позже или другую ссылку.")
            await asyncio.sleep(0.25)
        _, stderr = await completed
        if process.returncode != 0:
            # Don't echo raw yt-dlp output: it may contain private URL parameters.
            raise DownloadError(_safe_message(stderr.decode("utf-8", "replace")[-1800:]))
    finally:
        if process.returncode is None:
            await _stop_process(process)
        if not completed.done():
            completed.cancel()
            await asyncio.gather(completed, return_exceptions=True)

    allowed_ext = {".mp4", ".webm", ".mov", ".mkv"}
    files = [p for p in output_dir.iterdir() if p.is_file() and p.suffix.lower() in allowed_ext]
    if len(files) != 1:
        raise DownloadError("Источник не вернул один готовый видеофайл.")
    file = files[0]
    size = file.stat().st_size
    if size == 0:
        raise DownloadError("Источник прислал пустой файл.")
    if size > limit:
        raise DownloadTooLarge("Видео превышает лимит вложений Discord. Попробуй качество 360.")
    return DownloadedVideo(path=file, platform=request.platform, size=size,
                           filename=f"Vivian_{request.platform.replace(' ', '_')}{file.suffix.lower()}")


async def fetch_video(url: str, destination: Path, discord_limit: int | None, quality: int = 480) -> DownloadedVideo:
    """Bounded download; the caller owns `destination` and its cleanup."""
    request = validate_video_url(url)
    limit = upload_limit(discord_limit)
    if limit < MIB:
        raise DownloadError("В этом канале слишком маленький лимит вложений.")
    if _DOWNLOAD_SLOTS.locked():
        raise DownloadBusy("Уже скачиваются два видео. Попробуй чуть позже.")
    async with _DOWNLOAD_SLOTS:
        return await _run_download(request, destination, limit, quality)

# Auto-offer accepts normal chat text while keeping the strict URL allowlist
# above as the sole authority for downloads.
import re

_LINK_PATTERN = re.compile(r"https://[^\s<>]+", re.IGNORECASE)
_COVER_SLOTS = asyncio.Semaphore(1)
COVER_AUDIO_MAX_SECONDS = 180


def find_video_url(message_text: str) -> VideoRequest | None:
    """Return the first supported public-video URL embedded in a message.

    No requests are made here: untrusted text cannot trigger network I/O until
    its author explicitly chooses a download button.
    """
    if not isinstance(message_text, str) or not message_text:
        return None
    for match in list(_LINK_PATTERN.finditer(message_text))[:8]:
        candidate = match.group(0).rstrip(".,!?;:)]}\"'»”")
        try:
            return validate_video_url(candidate)
        except DownloadError:
            continue
    return None


async def _run_ffmpeg(arguments: list[str], *, timeout: float = 60.0) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, FileNotFoundError) as exc:
        raise DownloadError("Для этого формата на сервере нужны FFmpeg и FFprobe.") from exc
    completed = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(completed), timeout=timeout)
    except asyncio.TimeoutError as exc:
        await _stop_process(process)
        completed.cancel()
        await asyncio.gather(completed, return_exceptions=True)
        raise DownloadError("Обработка ролика заняла слишком много времени.") from exc
    except asyncio.CancelledError:
        await _stop_process(process)
        completed.cancel()
        await asyncio.gather(completed, return_exceptions=True)
        raise
    if process.returncode != 0:
        # FFmpeg reports expected media errors; do not expose raw URLs or paths.
        if b"matches no streams" in stderr or b"does not contain any stream" in stderr:
            raise DownloadError("У ролика нет отдельной доступной звуковой дорожки.")
        raise DownloadError("Не удалось выделить обложку или музыку из этого ролика.")
    return stdout


async def _render_cover_music(source: Path, destination: Path, max_bytes: int) -> Path:
    duration_text = await _run_ffmpeg([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(source),
    ], timeout=12)
    try:
        duration = float(duration_text.decode("utf-8").strip())
    except (ValueError, UnicodeError) as exc:
        raise DownloadError("Не удалось определить длительность видео.") from exc
    if not (0.1 < duration <= COVER_AUDIO_MAX_SECONDS):
        raise DownloadError(
            f"Для варианта «Картинка + музыка» подходят ролики до {COVER_AUDIO_MAX_SECONDS} секунд."
        )
    cover = destination / "cover.jpg"
    output = destination / "cover_music.mp4"
    await _run_ffmpeg([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-frames:v", "1", "-q:v", "4", str(cover),
    ], timeout=20)
    if not cover.is_file() or cover.stat().st_size < 10:
        raise DownloadError("Не удалось получить обложку этого ролика.")
    await _run_ffmpeg([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-loop", "1", "-framerate", "2", "-i", str(cover),
        "-i", str(source),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,"
               "pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
        "-r", "2", "-c:a", "aac", "-b:a", "96k",
        "-shortest", "-movflags", "+faststart", str(output),
    ], timeout=max(30, min(90, duration / 2 + 15)))
    if not output.is_file() or output.stat().st_size < 100:
        raise DownloadError("Не удалось создать видео с обложкой и музыкой.")
    if output.stat().st_size > max_bytes:
        raise DownloadTooLarge("Картинка с музыкой превышает лимит вложений Discord.")
    return output


async def fetch_cover_music(url: str, destination: Path, discord_limit: int | None) -> DownloadedVideo:
    """Create a playable MP4 of a still from the public reel and its own audio.

    Never fetches DRM media or user credentials. Length is limited to 180 s.
    Upload ceiling applies to the *result* (source download is bounded too).
    """
    request = validate_video_url(url)
    limit = upload_limit(discord_limit)
    source_budget = min(max(limit * 3, 20 * MIB), 80 * MIB)
    if _COVER_SLOTS.locked():
        raise DownloadBusy("Сейчас создаётся другая картинка с музыкой. Попробуй позже.")
    async with _COVER_SLOTS:
        if _DOWNLOAD_SLOTS.locked():
            raise DownloadBusy("Сервер занят загрузками, попробуй позже.")
        async with _DOWNLOAD_SLOTS:
            source = await _run_download(request, destination, source_budget, height=360)
        result = await _render_cover_music(source.path, destination, limit)
        return DownloadedVideo(
            path=result, platform=request.platform, size=result.stat().st_size,
            filename="Vivian_Cover_and_Music.mp4",
        )
