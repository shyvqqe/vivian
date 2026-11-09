from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core.season_style import theme_for


SAFE_REGIONS = ((48, 30, 674, 108), (1090, 26, 1246, 126), (8, 696, 46, 760), (1568, 696, 1596, 760))
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


@lru_cache(maxsize=1)
def _font():
    path = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(str(path), size=17)
    except OSError:
        return ImageFont.load_default(size=17)


def _color(palette, key, fallback):
    value = palette.get(key, fallback)
    if not isinstance(value, (tuple, list)) or len(value) != 3 or any(type(channel) is not int or not 0 <= channel <= 255 for channel in value):
        return fallback
    return tuple(value)


def _motif(draw, identifier, center, radius, accent, border):
    x, y = center
    def point(a, b):
        return round(x + a * radius), round(y + b * radius)
    def line(points, color=border, width=1):
        draw.line([point(*position) for position in points], fill=color, width=width)
    def ellipse(box, color=border, width=1, fill=None):
        first, second = point(box[0], box[1]), point(box[2], box[3])
        draw.ellipse((*first, *second), outline=color, width=width, fill=fill)
    if identifier == "frost":
        for index in range(6):
            angle = index * math.tau / 6
            dx, dy = math.cos(angle), math.sin(angle)
            line([(0, 0), (dx, dy)], accent)
            for side in (-1, 1):
                branch = angle + side * 0.62
                line([(dx * 0.55, dy * 0.55), (dx * 0.55 - math.cos(branch) * 0.32, dy * 0.55 - math.sin(branch) * 0.32)], accent)
    elif identifier == "neon":
        for offset in (-0.64, 0.0, 0.64):
            line([(offset - 0.25, -0.72), (offset + 0.25, 0), (offset - 0.25, 0.72)], accent, 2)
    elif identifier == "forest":
        for height in (-0.65, -0.15, 0.35):
            line([(-0.7, height + 0.4), (0, height - 0.4), (0.7, height + 0.4)], accent)
        line([(0, -0.9), (0, 1.0)], border, 2)
    elif identifier == "rain":
        for offset in (-0.65, 0, 0.65):
            line([(offset + 0.2, -0.8), (offset - 0.1, 0.15)], accent, 2)
            ellipse((offset - 0.19, 0.3, offset + 0.01, 0.5), accent)
    elif identifier == "bloom":
        for index in range(5):
            angle = index * math.tau / 5 - math.pi / 2
            px, py = math.cos(angle) * 0.5, math.sin(angle) * 0.5
            ellipse((px - 0.34, py - 0.34, px + 0.34, py + 0.34), border)
        ellipse((-0.15, -0.15, 0.15, 0.15), accent, fill=accent)
    elif identifier == "solar":
        ellipse((-0.48, -0.48, 0.48, 0.48), accent)
        for index in range(8):
            angle = index * math.tau / 8
            line([(math.cos(angle) * 0.68, math.sin(angle) * 0.68), (math.cos(angle), math.sin(angle))], accent)
    elif identifier == "tropical":
        line([(0, -0.45), (0.12, 0.4), (0.32, 1)], border, 2)
        for tip in ((-1, -0.1), (-0.8, -0.8), (-0.1, -1), (0.8, -0.8), (1, -0.15)):
            line([tip, (0, -0.45), (tip[0] * 0.8, tip[1] + 0.32)], accent)
    elif identifier == "dusk":
        for height, width in ((0.15, 0.92), (0.5, 0.65), (0.82, 0.32)):
            line([(-width, height), (width, height)], border)
        draw.arc((*point(-0.55, -0.65), *point(0.55, 0.45)), 180, 360, fill=accent, width=2)
    elif identifier == "orbit":
        ellipse((-0.45, -0.45, 0.45, 0.45), accent)
        ellipse((-1, -0.24, 1, 0.24), border)
        ellipse((0.72, -0.4, 0.88, -0.24), accent, fill=accent)
    elif identifier == "halloween":
        ellipse((-0.82, -0.58, 0.82, 0.78), accent)
        ellipse((-0.45, -0.58, 0.45, 0.78), border)
        line([(0, -0.58), (0.15, -0.98), (0.42, -0.98)], accent, 2)
        line([(-0.55, 0.16), (-0.2, 0.38), (0, 0.16), (0.2, 0.38), (0.55, 0.16)], accent)
    elif identifier == "cyberpunk":
        line([(-1, -0.75), (-0.4, -0.75), (-0.4, -0.12), (0.25, -0.12), (0.25, 0.7), (1, 0.7)], accent, 2)
        line([(-1, 0.55), (-0.65, 0.55), (-0.65, 0.12), (0.65, 0.12), (0.65, -0.7), (1, -0.7)], border)
        for px, py in ((-1, -0.75), (1, 0.7), (1, -0.7)):
            ellipse((px - 0.1, py - 0.1, px + 0.1, py + 0.1), accent, fill=accent)
    else:
        line([(-1, 0.45), (-0.35, -0.7), (0.3, 0.1), (1, -0.65)], border)
        for px, py, size in ((-1, 0.45, 0.17), (-0.35, -0.7, 0.24), (0.3, 0.1, 0.17), (1, -0.65, 0.22)):
            draw.polygon([point(px, py - size), point(px + size * 0.65, py), point(px, py + size), point(px - size * 0.65, py)], fill=accent)


def apply_season(image, palette, season):
    if not isinstance(image, Image.Image) or image.mode not in {"RGB", "RGBA"} or not isinstance(palette, dict) or not isinstance(season, dict) or not season.get("id"):
        return image
    accent = _color(palette, "accent", None)
    if accent is None:
        return image
    border = _color(palette, "border", accent)
    panel = _color(palette, "panel", border)
    muted = _color(palette, "muted", accent)
    theme = theme_for(season.get("month"), season.get("id"))
    month = theme["month"]
    number = int(month[-2:])
    label = f"{_MONTHS[number - 1]} {month[:4]}" if len(month) == 7 else _MONTHS[number - 1]
    label += f"  /  {theme['label'].upper()}"
    overlay = Image.new("RGBA", (1600, 768))
    draw = ImageDraw.Draw(overlay)
    font = _font()
    width = min(560, round(draw.textlength(label, font=font)) + 80)
    draw.rounded_rectangle((60, 42, 60 + width, 86), radius=18, fill=(*panel, 125), outline=(*border, 155), width=1)
    _motif(draw, theme["id"], (83, 64), 11, (*accent, 200), (*border, 190))
    draw.text((108, 64), label, font=font, fill=(*muted, 235), anchor="lm")
    _motif(draw, theme["id"], (1168, 75), 36, (*accent, 115), (*border, 125))
    _motif(draw, theme["id"], (27, 728), 12, (*accent, 90), (*border, 105))
    _motif(draw, theme["id"], (1582, 728), 9, (*accent, 90), (*border, 105))
    if image.size != overlay.size:
        overlay = overlay.resize(image.size, Image.Resampling.LANCZOS)
    result = Image.alpha_composite(image.convert("RGBA"), overlay)
    for left, top, right, bottom in SAFE_REGIONS:
        bounds = (round(left * image.width / 1600), round(top * image.height / 768),
                  round(right * image.width / 1600), round(bottom * image.height / 768))
        if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
            patch = result.crop(bounds)
            image.paste(patch if image.mode == "RGBA" else patch.convert("RGB"), bounds[:2])
    return image
