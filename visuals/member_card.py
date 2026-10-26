"""Vivian profile with avatar-derived colours, real identity and counters."""
from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path
import colorsys
import unicodedata
import warnings

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from visuals.profile_palette import extract_profile_theme

WIDTH, HEIGHT = 1600, 800
CARD_ACCENT = 0x00FFF7  # Fallback only; each render derives its own colours.
_DEFAULT_ACCENT = (0, 255, 247)
_ROOT = Path(__file__).resolve().parents[1]
_BACKGROUND_FILE = _ROOT / "assets" / "profile" / "jane_doe_background.png"
_WHITE = (244, 247, 244)
_REPORTED_ERRORS = set()


@lru_cache(maxsize=48)
def _font(size: int, bold: bool = True):
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for candidate in (
        _ROOT / "assets" / "fonts" / filename,
        Path("/usr/share/fonts/truetype/dejavu") / filename,
        Path("/usr/share/fonts/dejavu") / filename,
        Path("C:/Windows/Fonts") / ("arialbd.ttf" if bold else "arial.ttf"),
        filename,
    ):
        try:
            return ImageFont.truetype(str(candidate), size=size)
        except OSError:
            continue
    raise RuntimeError("Add assets/fonts/DejaVuSans.ttf and DejaVuSans-Bold.ttf")


@lru_cache(maxsize=2048)
def _supported(char):
    if char.isspace():
        return True
    font = _font(16)
    glyph, missing = font.getmask(char), font.getmask("\uffff")
    return glyph.size != missing.size or bytes(glyph) != bytes(missing)


def _text(value, limit=100):
    value = unicodedata.normalize("NFKC", str(value or "")[:max(200, limit * 4)])
    return " ".join("".join(char for char in value
        if not unicodedata.category(char).startswith("C")
        and ord(char) <= 0xFFFF and char not in "\ufe0e\ufe0f" and _supported(char)).split())[:limit]


def _fit(draw, value, width, size, *, minimum=16, bold=True):
    for current in range(size, minimum - 1, -1):
        font = _font(current, bold)
        if draw.textlength(value, font=font) <= width:
            return value, font
    font = _font(minimum, bold)
    while value and draw.textlength(value + "…", font=font) > width:
        value = value[:-1]
    return value + "…", font


def _integer(value, default=0):
    try:
        return max(0, min(10**18, int(value)))
    except (ValueError, TypeError, OverflowError):
        return default


def _number(value):
    number = _integer(value, None)
    if number is None:
        return "—"
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f} млрд"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f} млн"
    return str(number)


def _mix(first, second, ratio):
    return tuple(round(a * (1 - ratio) + b * ratio) for a, b in zip(first, second))


def _luminance(rgb):
    channels = [value / 255 for value in rgb]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in channels]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def _palette(data, avatar_bytes):
    """All colours are local to this render, including concurrent worker calls."""
    theme = data.get("theme")
    top = theme.get("background_top") if isinstance(theme, dict) else None
    valid = (isinstance(theme, dict) and theme.get("source") == "avatar"
             and isinstance(top, (tuple, list)) and len(top) == 3
             and all(isinstance(value, (int, float)) and 0 <= value <= 255 for value in top))
    if not valid:
        theme = extract_profile_theme(avatar_bytes)
        top = theme.get("background_top")
    accent = _DEFAULT_ACCENT
    if theme.get("source") == "avatar" and isinstance(top, (tuple, list)) and len(top) == 3:
        hue, _, saturation = colorsys.rgb_to_hls(*(value / 255 for value in top))
        if saturation < 0.18:
            accent = (226, 226, 226)
        else:
            accent = tuple(round(value * 255) for value in colorsys.hsv_to_rgb(hue, 0.84, 1.0))
            # Blue/red accents need a little white to remain readable on ink.
            while _luminance(accent) < 0.24:
                accent = _mix(accent, (255, 255, 255), 0.08)
    ink = _mix((8, 10, 13), accent, 0.035)
    return {"accent": accent, "ink": ink, "footer": _mix(ink, accent, 0.025),
            "track": _mix(ink, accent, 0.09), "line": _mix(ink, accent, 0.28)}


@lru_cache(maxsize=2)
def _load_background(path, modified):
    """Cache only the static art. Each caller works on its own image copy."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        source_path = Path(path)
        if source_path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Background exceeds byte limit")
        with Image.open(source_path) as source:
            if source.width * source.height > 20_000_000:
                raise ValueError("Background exceeds pixel limit")
            source.verify()
        with Image.open(source_path) as source:
            fitted = ImageOps.fit(source.convert("RGB"), (WIDTH, HEIGHT),
                                  method=Image.Resampling.LANCZOS)
            return ImageOps.grayscale(fitted)


def _background(palette):
    try:
        stat = _BACKGROUND_FILE.stat()
        neutral = _load_background(str(_BACKGROUND_FILE), stat.st_mtime_ns)
        return ImageOps.colorize(neutral, palette["ink"], palette["accent"]).convert("RGBA")
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        kind = type(error).__name__
        if kind not in _REPORTED_ERRORS:
            print(f"[Profile card] Background unavailable; error={kind}", flush=True)
            _REPORTED_ERRORS.add(kind)
        return Image.new("RGBA", (WIDTH, HEIGHT), (*palette["ink"], 255))


def _avatar(avatar_bytes, palette, size=(240, 184)):
    """Return a real avatar or None; missing images never become initials."""
    avatar = None
    if isinstance(avatar_bytes, (bytes, bytearray)) and len(avatar_bytes) <= 8 * 1024 * 1024:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(avatar_bytes)) as source:
                    if source.width * source.height > 20_000_000:
                        raise ValueError("Avatar exceeds pixel limit")
                    source.verify()
                with Image.open(BytesIO(avatar_bytes)) as source:
                    cropped = ImageOps.fit(ImageOps.exif_transpose(source).convert("RGBA"),
                                           size, method=Image.Resampling.LANCZOS)
                    avatar = Image.new("RGBA", size, (*palette["ink"], 255))
                    avatar.alpha_composite(cropped)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            pass
    if avatar is None:
        return None
    mask = Image.new("L", (size[0] * 3, size[1] * 3))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] * 3 - 1, size[1] * 3 - 1), radius=30, fill=255)
    avatar.putalpha(mask.resize(size, Image.Resampling.LANCZOS))
    return avatar


def _wordmark(image, x, y, palette):
    font = _font(54)
    width = round(font.getlength("VIVIAN")) + 16
    height = font.getbbox("VIVIAN")[3] - font.getbbox("VIVIAN")[1] + 16
    layer = Image.new("RGBA", (width, height))
    ImageDraw.Draw(layer).text((6, 6), "VIVIAN", font=font,
                               fill=(*palette["accent"], 190), anchor="lt")
    skew = 0.18
    tilted = layer.transform((width + round(height * skew), height), Image.Transform.AFFINE,
                              (1, skew, -height * skew, 0, 1, 0), Image.Resampling.BICUBIC)
    bounds = tilted.getbbox()
    if bounds:
        image.alpha_composite(tilted.crop(bounds), (x, y))


def _nickname(image, value, palette, x=64):
    draw = ImageDraw.Draw(image)
    value, font = _fit(draw, value, 1164 - x, 182, minimum=60)
    # Align the visible glyphs, including their side bearings, to the logo.
    bounds = font.getbbox(value, anchor="lt")
    layer = Image.new("RGBA", (bounds[2] - bounds[0] + 24, bounds[3] - bounds[1] + 24))
    ImageDraw.Draw(layer).text((12 - bounds[0], 12 - bounds[1]), value,
                               font=font, fill=palette["accent"], anchor="lt")
    visible = layer.getbbox()
    if visible:
        image.alpha_composite(layer.crop(visible), (x, 172))


def _progress(image, ratio, palette):
    x, y, width, height = 70, 662, 1460, 52
    draw = ImageDraw.Draw(image)
    outline = _mix(palette["line"], palette["accent"], 0.25)
    empty = _mix(palette["track"], _WHITE, 0.045)
    draw.rounded_rectangle((x - 6, y - 6, x + width + 6, y + height + 6),
                           radius=32, fill=palette["ink"], outline=outline, width=3)
    draw.rounded_rectangle((x, y, x + width, y + height), radius=26, fill=empty)
    if ratio <= 0:
        return
    end = max(1, min(width, round(width * ratio)))
    mask = Image.new("L", (width, height))
    shape = ImageDraw.Draw(mask)
    shape.rounded_rectangle((0, 0, width - 1, height - 1), radius=26, fill=255)
    if end < width:
        shape.rectangle((end, 0, width, height), fill=0)
    fill = Image.new("RGBA", (width, height), (*palette["accent"], 255))
    ImageDraw.Draw(fill).rectangle((0, 0, width - 1, 8),
                                   fill=(*_mix(palette["accent"], _WHITE, 0.18), 255))
    glow = Image.new("RGBA", image.size)
    glow.paste(Image.new("RGBA", (width, height), (*palette["accent"], 125)), (x, y), mask)
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(10)))
    image.paste(fill, (x, y), mask)
    # The marker sits inside the real fill, including a one-pixel first XP.
    marker = Image.new("RGBA", (width, height))
    ImageDraw.Draw(marker).rectangle((max(0, end - 4), 4, end - 1, height - 5), fill=(245, 255, 250, 235))
    marker.putalpha(ImageChops.multiply(marker.getchannel("A"), mask))
    image.alpha_composite(marker, (x, y))


def _row_font(draw, left, right, left_width, right_width, size):
    """Fit the pair together so its two sides always use one font size."""
    for current in range(size, 23, -1):
        font = _font(current)
        if (draw.textlength(left, font=font) <= left_width
                and draw.textlength(right, font=font) <= right_width):
            return font
    return _font(24)


def _statistics(image, data, palette):
    draw = ImageDraw.Draw(image)
    count = _number(data.get("messages"))
    level = _integer(data.get("level"), None)
    level_text = str(level).zfill(2) if level is not None else "—"
    left, right = f"{count} СООБЩЕНИЙ", f"LVL {level_text}"
    font = _row_font(draw, left, right, 836, 450, 52)
    # Keep the pair together rather than pinning LVL to the opposite corner.
    right_edge = round(64 + draw.textlength(left, font=font) + 80
                       + draw.textlength(right, font=font))
    style = dict(font=font, stroke_width=2, stroke_fill=palette["ink"])
    # Full strings, identical baseline, font and stroke; colour is the only
    # difference between numbers and their labels.
    draw.text((64, 548), left, fill=_WHITE, anchor="ls", **style)
    draw.text((right_edge, 548), right, fill=_WHITE, anchor="rs", **style)
    draw.text((64, 548), count, fill=palette["accent"], anchor="ls", **style)
    draw.text((right_edge, 548), level_text, fill=palette["accent"], anchor="rs", **style)


def render_member_card(data: dict, avatar_bytes: bytes | None = None) -> bytes:
    """Produce a PNG in the existing profile worker with real caller data.

    The local pattern is tinted per avatar. Identity, messages, level and XP
    are drawn at runtime; no provider calls or hardcoded member numbers occur.
    """
    palette = _palette(data, avatar_bytes)
    image = _background(palette)
    draw = ImageDraw.Draw(image)
    # A clean edge fades into the art instead of forming a visible cut strip.
    edge = Image.new("RGBA", image.size)
    edge_draw = ImageDraw.Draw(edge)
    for x in range(64):
        alpha = 255 if x < 24 else round(255 * ((64 - x) / 40) ** 2)
        edge_draw.line((x, 0, x, HEIGHT), fill=(*palette["ink"], alpha))
    image.alpha_composite(edge)
    name = _text(data.get("display_name"), 100) or "Участник"
    username = _text(data.get("username"), 80).lstrip("@")
    handle = username or name.lstrip("@") or "Участник"
    xp = _integer(data.get("xp_into"))
    required = max(1, _integer(data.get("xp_required"), 1))
    ratio = min(1.0, xp / required)

    # The only portrait slot is the marked window in the top-right corner.
    avatar = _avatar(avatar_bytes, palette)
    if avatar is not None:
        frame = ImageDraw.Draw(image)
        frame.rounded_rectangle((1280, 56, 1536, 256), radius=16,
                                fill=palette["ink"], outline=palette["accent"], width=3)
        image.alpha_composite(avatar, (1288, 64))
    _wordmark(image, 64, 49, palette)
    _nickname(image, handle, palette, 64)
    draw = ImageDraw.Draw(image)

    _statistics(image, data, palette)

    # Keep the lower panel dark so the dynamic accent remains readable.
    draw.rectangle((0, 580, WIDTH, HEIGHT), fill=palette["footer"])
    draw.line((0, 613, WIDTH, 613), fill=palette["accent"], width=2)
    _progress(image, ratio, palette)
    draw = ImageDraw.Draw(image)
    value, percent = f"{_number(xp)} / {_number(required)} XP", f"{round(ratio * 100)}%"
    caption_font = _row_font(draw, value, percent, 1180, 200, 38)
    draw.text((64, 761), value, font=caption_font, fill=_WHITE, anchor="ls")
    draw.text((1536, 761), percent, font=caption_font, fill=palette["accent"], anchor="rs")

    output = BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()
