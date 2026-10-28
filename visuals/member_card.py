from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path
import math
import unicodedata
import warnings

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from visuals.profile_palette import extract_profile_theme


WIDTH, HEIGHT = 1600, 768
CARD_ACCENT = 0xD58BFF
_ROOT = Path(__file__).resolve().parents[1]
_THEME_KEYS = (
    "background_top", "background_bottom", "glow", "panel", "text", "muted",
    "accent", "accent_secondary", "border", "panel_secondary", "track", "shadow", "highlight",
)


@lru_cache(maxsize=48)
def _font(size: int, bold: bool = False, serif: bool = False):
    filename = "CormorantGaramond.ttf" if serif else "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = [
        _ROOT / "assets" / "fonts" / filename,
        Path("/usr/share/fonts/truetype/dejavu") / filename,
        Path("/usr/share/fonts/dejavu") / filename,
    ]
    if serif:
        candidates.extend([
            _ROOT / "assets" / "fonts" / "DejaVuSans.ttf",
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
        ])
    for candidate in candidates:
        try:
            font = ImageFont.truetype(str(candidate), size=size)
            if candidate.name == "CormorantGaramond.ttf":
                try:
                    font.set_variation_by_axes([500])
                except (OSError, ValueError, AttributeError):
                    pass
            return font
        except OSError:
            continue
    return ImageFont.load_default(size=size)


@lru_cache(maxsize=2048)
def _supported(char, serif=False):
    if char.isspace():
        return True
    font = _font(20, serif=serif)
    glyph, missing = font.getmask(char), font.getmask("\uffff")
    return glyph.size != missing.size or bytes(glyph) != bytes(missing)


def _text(value, limit=100):
    value = unicodedata.normalize("NFKC", str(value or "")[:max(200, limit * 4)])
    return " ".join("".join(
        char for char in value
        if not unicodedata.category(char).startswith("C")
        and char not in "\ufe0e\ufe0f" and _supported(char)
    ).split())[:limit]


def _fit(draw, value, width, size, *, minimum=32, bold=False, serif=False):
    if serif and any(not _supported(char, True) for char in value):
        serif = False
    font = _font(minimum, bold, serif)
    if draw.textlength(value, font=font) > width:
        while value and draw.textlength(value + "…", font=font) > width:
            value = value[:-1]
        return value + "…", font
    low, high = minimum, size
    while low <= high:
        current = (low + high) // 2
        candidate = _font(current, bold, serif)
        if draw.textlength(value, font=candidate) <= width:
            font = candidate
            low = current + 1
        else:
            high = current - 1
    return value, font


def _integer(value, default=0):
    try:
        return max(0, min(10**18, int(value)))
    except (ValueError, TypeError, OverflowError):
        return default


def _number(value):
    number = _integer(value, None)
    if number is None:
        return "—"
    if number >= 1_000_000_000_000:
        return f"{number / 1_000_000_000_000:.1f}T"
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f}B"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    return f"{number:,}".replace(",", " ")


def _mix(first, second, ratio):
    return tuple(round(a * (1 - ratio) + b * ratio) for a, b in zip(first, second))


def _palette(data, avatar_bytes):
    theme = data.get("theme")
    valid = isinstance(theme, dict) and theme.get("source") in {"avatar", "fallback"}
    if valid:
        valid = all(
            isinstance(theme.get(key), (tuple, list)) and len(theme[key]) == 3
            and all(type(channel) is int and 0 <= channel <= 255 for channel in theme[key])
            for key in _THEME_KEYS
        )
    if not valid:
        theme = extract_profile_theme(avatar_bytes, fallback_key=str(data.get("user_id", "")))
    return {key: tuple(theme[key]) for key in _THEME_KEYS}


def _gradient(size, first, second, *, horizontal=False):
    mask = Image.linear_gradient("L")
    if horizontal:
        mask = mask.transpose(Image.Transpose.ROTATE_90)
    return ImageOps.colorize(mask.resize(size, Image.Resampling.BICUBIC), first, second).convert("RGBA")


@lru_cache(maxsize=1)
def _glow_mask():
    width, height = 400, 192
    values = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            nx, ny = x / width, y / height
            center = math.exp(-(((nx - 0.48) / 0.52) ** 2 + ((ny - 0.38) / 0.68) ** 2) * 2.4)
            corner = math.exp(-(((nx - 0.91) / 0.22) ** 2 + ((ny - 0.09) / 0.35) ** 2) * 2.5)
            values[y * width + x] = round(min(1, center * 0.12 + corner * 0.07) * 255)
    return Image.frombytes("L", (width, height), bytes(values)).resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)


def _background(palette):
    image = _gradient((WIDTH, HEIGHT), palette["background_top"], palette["background_bottom"])
    glow = Image.new("RGBA", image.size, (*palette["glow"], 255))
    glow.putalpha(_glow_mask())
    image.alpha_composite(glow)
    return image


def _avatar(avatar_bytes, palette, size=(256, 192)):
    if not isinstance(avatar_bytes, (bytes, bytearray, memoryview)):
        return None
    if not avatar_bytes or memoryview(avatar_bytes).nbytes > 8 * 1024 * 1024:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(avatar_bytes)) as source:
                if source.width * source.height > 20_000_000:
                    return None
                source.seek(0)
                cropped = ImageOps.fit(
                    ImageOps.exif_transpose(source).convert("RGBA"), size,
                    method=Image.Resampling.LANCZOS,
                )
        avatar = Image.new("RGBA", size, (*palette["background_top"], 255))
        avatar.alpha_composite(cropped)
        mask = Image.new("L", (size[0] * 3, size[1] * 3))
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] * 3 - 1, size[1] * 3 - 1), radius=22, fill=255)
        avatar.putalpha(mask.resize(size, Image.Resampling.LANCZOS))
        return avatar
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def _portrait(image, avatar_bytes, palette):
    avatar = _avatar(avatar_bytes, palette)
    if avatar is None:
        return
    bounds = (1284, 38, 1556, 246)
    mask = Image.new("L", image.size)
    ImageDraw.Draw(mask).rounded_rectangle(bounds, radius=15, outline=160, width=2)
    glow = Image.new("RGBA", image.size, (*palette["accent"], 255))
    glow.putalpha(mask.filter(ImageFilter.GaussianBlur(12)))
    image.alpha_composite(glow)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(bounds, radius=15, fill=palette["shadow"])
    outline = Image.new("RGBA", image.size)
    gradient = _gradient((273, 209), palette["accent_secondary"], palette["accent"], horizontal=True)
    stroke = Image.new("L", (273, 209))
    ImageDraw.Draw(stroke).rounded_rectangle((0, 0, 272, 208), radius=15, outline=220, width=2)
    outline.paste(gradient, bounds[:2], stroke)
    image.alpha_composite(outline)
    image.alpha_composite(avatar, (1292, 46))


def _nickname(image, value, palette):
    value, font = _fit(ImageDraw.Draw(image), value, 1160, 260, minimum=52, serif=True)
    bounds = font.getbbox(value, anchor="lt")
    width = max(1, bounds[2] - bounds[0] + 4)
    height = max(1, bounds[3] - bounds[1] + 4)
    mask = Image.new("L", (width, height))
    ImageDraw.Draw(mask).text((2 - bounds[0], 2 - bounds[1]), value, font=font, fill=255, anchor="lt")
    visible = mask.getbbox()
    if visible is None:
        return
    mask = mask.crop(visible)
    color = _gradient(mask.size, palette["accent"], _mix(palette["accent_secondary"], palette["border"], 0.52), horizontal=True)
    color.putalpha(mask)
    y = 155 + max(0, (175 - mask.height) // 2)
    image.alpha_composite(color, (60, y))


def _statistics(image, data, palette):
    draw = ImageDraw.Draw(image)
    count = _number(data.get("messages"))
    level = _integer(data.get("level"), None)
    level_text = str(level).zfill(2) if level is not None else "—"
    left, right = f"{count} СООБЩЕНИЙ", f"LVL {level_text}"
    font = _font(56)
    for size in range(56, 23, -1):
        font = _font(size)
        if draw.textlength(left, font=font) + draw.textlength(right, font=font) + 80 <= 1480:
            break
    x, baseline = 60, 535
    draw.text((x, baseline), count, font=font, fill=palette["accent"], anchor="ls")
    x += draw.textlength(count, font=font)
    draw.text((x, baseline), " СООБЩЕНИЙ", font=font, fill=palette["text"], anchor="ls")
    x = 60 + draw.textlength(left, font=font) + 80
    draw.text((x, baseline), "LVL ", font=font, fill=palette["text"], anchor="ls")
    x += draw.textlength("LVL ", font=font)
    draw.text((x, baseline), level_text, font=font, fill=palette["accent"], anchor="ls")


def _progress(image, ratio, palette):
    x, y, width, height = 63, 643, 1488, 42
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((58, 638, 1555, 690), radius=28, fill=palette["shadow"], outline=palette["border"], width=2)
    draw.rounded_rectangle((x, y, x + width - 1, y + height - 1), radius=21, fill=palette["track"])
    if ratio <= 0:
        return
    end = max(1, min(width, round(width * ratio)))
    mask = Image.new("L", (width, height))
    shape = ImageDraw.Draw(mask)
    shape.rounded_rectangle((0, 0, width - 1, height - 1), radius=21, fill=255)
    if end < width:
        shape.rectangle((end, 0, width, height), fill=0)
    fill = _gradient((width, height), _mix(palette["accent_secondary"], palette["border"], 0.55), palette["accent"], horizontal=True)
    fill.putalpha(mask)
    glow = Image.new("RGBA", image.size)
    glow.paste(Image.new("RGBA", (width, height), (*palette["accent"], 75)), (x, y), mask)
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(10)))
    image.alpha_composite(fill, (x, y))
    marker = Image.new("RGBA", (width, height))
    ImageDraw.Draw(marker).rectangle((max(0, end - 2), 3, end - 1, height - 4), fill=(*palette["highlight"], 200))
    marker.putalpha(ImageChops.multiply(marker.getchannel("A"), mask))
    image.alpha_composite(marker, (x, y))


def render_member_card(data: dict, avatar_bytes: bytes | None = None) -> bytes:
    palette = _palette(data, avatar_bytes)
    image = _background(palette)
    name = _text(data.get("display_name"), 100) or _text(data.get("username"), 100) or "Vivian"
    xp = _integer(data.get("xp_into"))
    required = max(1, _integer(data.get("xp_required"), 1))
    ratio = min(1.0, xp / required)
    _portrait(image, avatar_bytes, palette)
    _nickname(image, name, palette)
    _statistics(image, data, palette)
    footer = _gradient((WIDTH, HEIGHT - 601), palette["background_top"], palette["shadow"])
    image.alpha_composite(footer, (0, 601))
    line = _gradient((WIDTH, 1), palette["border"], palette["accent_secondary"], horizontal=True)
    image.alpha_composite(line, (0, 600))
    _progress(image, ratio, palette)
    draw = ImageDraw.Draw(image)
    value, percent = f"{_number(xp)} / {_number(required)} XP", f"{round(ratio * 100)}%"
    value, font = _fit(draw, value, 1180, 40, minimum=22)
    draw.text((60, 744), value, font=font, fill=palette["text"], anchor="ls")
    percent_font = _font(40)
    draw.text((1550, 744), percent, font=percent_font, fill=palette["accent"], anchor="rs")
    output = BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()
