from __future__ import annotations
from concurrent.futures import Future, ThreadPoolExecutor
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler
from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont
from PIL import ImageOps
from urllib.request import Request
from urllib.error import URLError
from PIL import UnidentifiedImageError
import asyncio
import base64
from urllib.request import build_opener
import discord
from functools import lru_cache
import math
import os
from threading import Lock
from urllib.parse import urlparse


PANEL_VISUAL_FILENAMES = {
    "system": "vivian_system.gif",
    "music": "vivian_music.gif",
    "player": "vivian_player.gif",
    "paused": "vivian_paused.gif",
    "idle": "vivian_idle.gif",
    "activity": "vivian_activity.gif",
    "profile": "vivian_profile.gif",
}

# Remove older uploads when a saved music panel changes playback state.
PANEL_VISUAL_ATTACHMENT_NAMES = frozenset(PANEL_VISUAL_FILENAMES.values()) | {
    "vivian_system.gif", "vivian_music.gif", "vivian_player.gif", "vivian_paused.png",
    "vivian_idle.png", "vivian_activity.gif", "vivian_profile.gif",
}

PANEL_VISUAL_TIMEOUT = 2.0
_PANEL_VISUAL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vivian-panels")
_PANEL_VISUAL_PENDING: dict[str, Future] = {}
_PANEL_VISUAL_CACHE: dict[str, bytes] = {}
_PANEL_VISUAL_LOCK = Lock()


@lru_cache(maxsize=7)
def _render_panel_visual(style: str) -> bytes:
    """Small decorative loops; playback timing remains in the real player readout."""
    filename = PANEL_VISUAL_FILENAMES[style]
    animated = filename.endswith(".gif")
    labels = {
        "system": ("SYSTEM INTERFACE", "SIGNAL / SURFACE"),
        "music": ("MUSIC DISCOVERY", "SEARCH / SELECT"),
        "player": ("AUDIO INTERFACE", "PLAYING"),
        "paused": ("AUDIO INTERFACE", "PAUSED"),
        "idle": ("AUDIO INTERFACE", "STANDBY"),
        "activity": ("CHAT PULSE", "VOICES / RHYTHM"),
        "profile": ("MEMBER TRACE", "WORDS / PRESENCE"),
    }
    palette_colors = [
        (14, 10, 20), (24, 17, 35), (47, 31, 63), (78, 49, 102),
        (133, 81, 173), (180, 103, 214), (213, 139, 255),
        (243, 190, 239), (249, 242, 255),
    ]
    palette = [component for color in palette_colors for component in color]
    palette += [0] * (768 - len(palette))
    title_font = ImageFont.load_default(size=42)
    small_font = ImageFont.load_default(size=14)
    status_font = ImageFont.load_default(size=12)
    frames = []
    frame_count = 24 if animated else 1
    for index in range(frame_count):
        phase = index / frame_count * math.tau
        frame = Image.new("P", (720, 144), color=0)
        frame.putpalette(palette)
        draw = ImageDraw.Draw(frame)
        draw.rounded_rectangle((1, 1, 718, 142), radius=16, fill=1, outline=3, width=1)
        draw.rounded_rectangle((18, 28, 21, 115), radius=2, fill=6)
        draw.text((40, 25), "Vivian", font=title_font, fill=8)
        draw.text((42, 81), labels[style][0], font=small_font, fill=7)
        draw.text((42, 110), labels[style][1], font=status_font, fill=5)
        draw.line((288, 27, 288, 117), fill=2)
        if style == "activity":
            draw.line((321, 76, 684, 76), fill=2)
            points = []
            for column in range(121):
                x = 324 + column * 3
                y = 76 - int(29 * math.sin(column * 0.19) * math.exp(-((column % 40 - 20) / 9) ** 2))
                points.append((x, y))
            draw.line(points, fill=4, width=2)
            cursor = index * 5 % len(points)
            draw.line(points[max(0, cursor - 12):cursor + 1], fill=6, width=3)
            x, y = points[cursor]
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=7)
            for node in range(6):
                x = 340 + node * 62
                color = 6 if node == index // 4 else 3
                draw.ellipse((x, 117, x + 4, 121), fill=color)
        elif style == "profile":
            cx, cy = 505, 78
            for radius in (18, 35, 51):
                draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=3)
            draw.line((324, cy, 685, cy), fill=2)
            draw.line((cx, 24, cx, 130), fill=2)
            for node, radius in enumerate((18, 35, 51)):
                angle = phase * (1 if node != 1 else -1) + node * 2.1
                x = cx + int(radius * math.cos(angle))
                y = cy + int(radius * math.sin(angle))
                draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=6 if node == 2 else 5)
            draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=7)
            for offset in range(4):
                draw.line((335, 57 + offset * 13, 369 + offset * 8, 57 + offset * 13), fill=4)
                draw.line((630 - offset * 8, 57 + offset * 13, 664, 57 + offset * 13), fill=4)
        else:
            draw.line((321, 73, 684, 73), fill=2)
            for column in range(37):
                x = 326 + column * 9
                if style == "system":
                    height = 6 + int(22 * abs(math.sin(column * 0.23 + phase)))
                elif style == "idle":
                    height = 3
                else:
                    height = 5 + int(32 * abs(math.sin(column * 0.37 + phase))
                                     * (0.55 + 0.45 * abs(math.sin(column * 0.13 - phase))))
                color = 6 if animated and (column - index * 2) % 37 < 5 else 4
                draw.rounded_rectangle((x, 73 - height, x + 3, 73 + height), radius=1, fill=color)
        if animated and style not in ("activity", "profile"):
            scan_x = 320 + int(index / (frame_count - 1) * 366)
            draw.line((scan_x, 26, scan_x, 116), fill=3)
            draw.ellipse((scan_x - 2, 122, scan_x + 2, 126), fill=6)
        draw.text((324, 17), "VIVIAN / INTERFACE", font=status_font, fill=5)
        frames.append(frame)
    buffer = BytesIO()
    if animated:
        frames[0].save(
            buffer, format="GIF", save_all=True, append_images=frames[1:],
            duration=100, loop=0, disposal=2, optimize=False,
        )
    else:
        frames[0].save(buffer, format="PNG")
    return buffer.getvalue()


def _render_panel_job(style: str) -> tuple[bytes | None, Exception | None]:
    # A late failure is a result, not an exception in a cancelled shield (Python 3.14).
    try:
        return _render_panel_visual(style), None
    except Exception as error:
        return None, error


def _finish_panel_visual(style: str, job: Future) -> None:
    # A timed-out caller leaves the bounded render running so the next call can use it.
    try:
        data, _ = job.result()
    except Exception:
        data = None
    with _PANEL_VISUAL_LOCK:
        if data is not None:
            _PANEL_VISUAL_CACHE[style] = data
        if _PANEL_VISUAL_PENDING.get(style) is job:
            _PANEL_VISUAL_PENDING.pop(style, None)


def _observe_panel_waiter(waiter: asyncio.Future) -> None:
    # Consume late failures even if wait_for has already returned without a picture.
    if not waiter.cancelled():
        waiter.exception()


async def _panel_visual_file(style: str) -> discord.File | None:
    # Decorative work has its own two workers and cannot hold a command indefinitely.
    try:
        filename = PANEL_VISUAL_FILENAMES[style]
        with _PANEL_VISUAL_LOCK:
            data = _PANEL_VISUAL_CACHE.get(style)
            job = _PANEL_VISUAL_PENDING.get(style)
            submitted = data is None and job is None
            if submitted:
                job = _PANEL_VISUAL_EXECUTOR.submit(_render_panel_job, style)
                _PANEL_VISUAL_PENDING[style] = job
        if submitted:
            job.add_done_callback(lambda completed: _finish_panel_visual(style, completed))
        if data is None:
            waiter = asyncio.wrap_future(job)
            waiter.add_done_callback(_observe_panel_waiter)
            data, error = await asyncio.wait_for(asyncio.shield(waiter), timeout=PANEL_VISUAL_TIMEOUT)
            if error is not None:
                raise error
        return discord.File(BytesIO(data), filename=filename)
    except asyncio.TimeoutError:
        print(f"[UI] Visual deferred; style={style}; budget={PANEL_VISUAL_TIMEOUT:g}s", flush=True)
        return None
    except Exception as error:
        print(f"[UI] Visual unavailable; style={style}; error={error!r}", flush=True)
        return None


def _apply_panel_visual(embed: discord.Embed, style: str) -> discord.Embed:
    embed.set_image(url=f"attachment://{PANEL_VISUAL_FILENAMES[style]}")
    return embed


def _can_upload_panel_visual(guild, channel) -> bool:
    member = getattr(guild, "me", None)
    permissions_for = getattr(channel, "permissions_for", None)
    if member is None or permissions_for is None:
        return False
    return bool(permissions_for(member).attach_files)


MAX_GROQ_IMAGE_BYTES = 20 * 1024 * 1024


MAX_GROQ_IMAGE_DATA_BYTES = 4 * 1024 * 1024


MAX_GROQ_IMAGES = 3


GROQ_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".apng"}


GROQ_MEDIA_HOSTS = {
    "cdn.discordapp.com",
    "media.discordapp.net",
    "images-ext-1.discordapp.net",
    "images-ext-2.discordapp.net",
    "images-ext-3.discordapp.net",
    "images-ext-4.discordapp.net",
    "media.tenor.com",
    "c.tenor.com",
    "media.giphy.com",
    "i.giphy.com",
}


def _trusted_media_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in GROQ_MEDIA_HOSTS


def _download_embed_image(url: str) -> bytes | None:
    """Fetch a bounded image preview from an allowlisted Discord/GIF CDN."""
    if not _trusted_media_url(url):
        return None

    class TrustedRedirects(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not _trusted_media_url(newurl):
                return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    request = Request(url, headers={"User-Agent": "Vivian Discord media preview", "Accept": "image/*"})
    opener = build_opener(TrustedRedirects())
    with opener.open(request, timeout=8) as response:
        if not _trusted_media_url(response.geturl()):
            return None
        if not response.headers.get_content_type().startswith("image/"):
            return None
        image_bytes = response.read(MAX_GROQ_IMAGE_BYTES + 1)
        return image_bytes if len(image_bytes) <= MAX_GROQ_IMAGE_BYTES else None


def _image_data_url(image_bytes: bytes) -> str | None:
    """Normalize still images and animated media's first frame for Groq Vision."""
    try:
        with Image.open(BytesIO(image_bytes)) as opened:
            if opened.width * opened.height > 30_000_000:
                return None
            opened.seek(0)
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")

            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            mime_type = "image/png"
            if output.tell() > MAX_GROQ_IMAGE_DATA_BYTES:
                if image.mode == "RGBA":
                    background = Image.new("RGB", image.size, (255, 255, 255))
                    background.paste(image, mask=image.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
                output = BytesIO()
                image.save(output, format="JPEG", quality=88, optimize=True)
                mime_type = "image/jpeg"
            if output.tell() > MAX_GROQ_IMAGE_DATA_BYTES:
                return None
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
            return f"data:{mime_type};base64,{encoded}"
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


async def _groq_image_parts(message: discord.Message) -> tuple[list[dict], int]:
    """Read at most three Discord images/stickers/GIF previews; animation uses its first frame."""
    parts = []
    skipped = 0

    async def add_image_bytes(raw: bytes) -> None:
        nonlocal skipped
        if len(raw) > MAX_GROQ_IMAGE_BYTES:
            skipped += 1
            return
        image_url = await asyncio.to_thread(_image_data_url, raw)
        if image_url is None:
            skipped += 1
            return
        parts.append({"type": "image_url", "image_url": {"url": image_url}})

    for attachment in message.attachments:
        extension = os.path.splitext(attachment.filename)[1].lower()
        content_type = (attachment.content_type or "").lower().split(";", 1)[0]
        if not content_type.startswith("image/") and extension not in GROQ_IMAGE_EXTENSIONS:
            continue
        if len(parts) >= MAX_GROQ_IMAGES or attachment.size > MAX_GROQ_IMAGE_BYTES:
            skipped += 1
            continue
        try:
            await add_image_bytes(await attachment.read())
        except (discord.HTTPException, asyncio.TimeoutError, OSError) as error:
            print(f"[AI] Could not read image attachment {attachment.id}: {error!r}", flush=True)
            skipped += 1

    for sticker in message.stickers:
        if len(parts) >= MAX_GROQ_IMAGES:
            skipped += 1
            continue
        asset = getattr(sticker, "url", None)
        if asset is None or os.path.splitext(urlparse(str(asset)).path)[1].lower() not in GROQ_IMAGE_EXTENSIONS:
            skipped += 1
            continue
        try:
            raw = await asyncio.wait_for(asset.read(), timeout=10)
            await add_image_bytes(raw)
        except (discord.HTTPException, asyncio.TimeoutError, OSError) as error:
            print(f"[AI] Could not read sticker {getattr(sticker, 'id', 'unknown')}: {error!r}", flush=True)
            skipped += 1

    for embed in message.embeds:
        if len(parts) >= MAX_GROQ_IMAGES:
            skipped += 1
            continue
        image_urls = [
            getattr(getattr(embed, "image", None), "url", None),
            getattr(getattr(embed, "thumbnail", None), "url", None),
        ]
        image_urls = [url for url in image_urls if isinstance(url, str) and url]
        if not image_urls:
            continue
        loaded = False
        for image_url in image_urls:
            try:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(_download_embed_image, image_url),
                    timeout=10,
                )
            except (HTTPError, URLError, asyncio.TimeoutError, OSError, ValueError) as error:
                print(f"[AI] Could not read embedded image: {error!r}", flush=True)
                continue
            if raw is None:
                continue
            before = len(parts)
            await add_image_bytes(raw)
            loaded = len(parts) > before
            if loaded:
                break
        if not loaded:
            skipped += 1

    return parts, skipped
