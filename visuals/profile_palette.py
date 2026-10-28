from __future__ import annotations

import colorsys
import hashlib
from io import BytesIO
import math
import warnings

from PIL import Image, ImageOps


_MAX_BYTES = 8 * 1024 * 1024
_MAX_PIXELS = 20_000_000
_HUE_BINS = 48


def _rgb(hue: float, lightness: float, saturation: float) -> tuple[int, int, int]:
    return tuple(round(channel * 255) for channel in colorsys.hls_to_rgb(
        hue % 1.0, max(0.0, min(1.0, lightness)), max(0.0, min(1.0, saturation))
    ))


def _luminance(rgb: tuple[int, int, int]) -> float:
    channels = [channel / 255 for channel in rgb]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
              for channel in channels]
    return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def _contrast(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    bright, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (bright + 0.05) / (dark + 0.05)


def _theme(hue: float, saturation: float, source: str) -> dict:
    saturation = 0.0 if saturation < 0.08 else max(0.34, min(0.95, saturation * 0.82 + 0.18))
    hue = hue % 1.0 if saturation else 0.0
    panel = _rgb(hue, 0.115, saturation * 0.72)
    panel_secondary = _rgb(hue, 0.155, saturation * 0.68)
    accent_lightness = 0.61
    accent = _rgb(hue, accent_lightness, saturation)
    while min(_contrast(accent, panel), _contrast(accent, panel_secondary)) < 4.5 and accent_lightness < 0.90:
        accent_lightness += 0.01
        accent = _rgb(hue, accent_lightness, saturation)
    secondary_lightness = max(0.46, accent_lightness - 0.08)
    secondary = _rgb(hue, secondary_lightness, saturation * 0.96)
    while min(_contrast(secondary, panel), _contrast(secondary, panel_secondary)) < 4.5 and secondary_lightness < 0.90:
        secondary_lightness += 0.01
        secondary = _rgb(hue, secondary_lightness, saturation * 0.96)
    text = _rgb(hue, 0.95, saturation * 0.56)
    muted = _rgb(hue, 0.77, saturation * 0.58)
    return {
        "background_top": _rgb(hue, 0.045, saturation * 0.72),
        "background_bottom": _rgb(hue, 0.026, saturation * 0.64),
        "glow": _rgb(hue, 0.30, saturation * 0.90),
        "panel": panel,
        "panel_secondary": panel_secondary,
        "text": text,
        "muted": muted,
        "accent": accent,
        "accent_secondary": secondary,
        "border": _rgb(hue, 0.38, saturation * 0.76),
        "track": _rgb(hue, 0.085, saturation * 0.65),
        "shadow": _rgb(hue, 0.025, saturation * 0.58),
        "highlight": _rgb(hue, 0.85, saturation * 0.74),
        "accent_hex": (accent[0] << 16) | (accent[1] << 8) | accent[2],
        "source": source,
        "hue": hue,
        "saturation": saturation,
    }


def _fallback(fallback_key: str) -> dict:
    key = str(fallback_key or "")[:256].encode("utf-8", errors="ignore")
    index = hashlib.blake2b(key, digest_size=2).digest()
    hue = int.from_bytes(index, "big") / 65536
    return _theme(hue, 0.42, "fallback")


def _pixels(avatar_bytes: bytes | None) -> list | None:
    if not isinstance(avatar_bytes, (bytes, bytearray, memoryview)):
        return None
    byte_count = avatar_bytes.nbytes if isinstance(avatar_bytes, memoryview) else len(avatar_bytes)
    if not byte_count or byte_count > _MAX_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(avatar_bytes)) as source:
                if source.width < 1 or source.height < 1 or source.width * source.height > _MAX_PIXELS:
                    return None
                source.seek(0)
                source.draft("RGB", (192, 192))
                image = ImageOps.exif_transpose(source).convert("RGBA")
                image.thumbnail((96, 96), Image.Resampling.LANCZOS)
                width, height = image.size
                center_x, center_y = (width - 1) / 2, (height - 1) / 2
                radius = max(1.0, math.hypot(center_x, center_y))
                pixels = []
                decoded = image.tobytes()
                for offset in range(0, len(decoded), 4):
                    red, green, blue, alpha = decoded[offset:offset + 4]
                    if alpha < 24:
                        continue
                    index = offset // 4
                    x, y = index % width, index // width
                    centrality = 1.0 - min(1.0, math.hypot(x - center_x, y - center_y) / radius)
                    pixels.append(((red, green, blue), alpha / 255, 0.78 + centrality * 0.22))
                return pixels
    except Exception:
        return None


def _dominant(pixels: list) -> tuple[float, float]:
    total = sum(opacity for _, opacity, _ in pixels)
    buckets = [[] for _ in range(_HUE_BINS)]
    coverage = [0.0] * _HUE_BINS
    scores = [0.0] * _HUE_BINS
    averages = [0.0, 0.0, 0.0]
    for rgb, opacity, centrality in pixels:
        for index, value in enumerate(rgb):
            averages[index] += value * opacity
        hue, saturation, brightness = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
        if saturation < 0.12 or brightness < 0.12:
            continue
        index = int(hue * _HUE_BINS) % _HUE_BINS
        weight = opacity * centrality * saturation ** 1.7 * (0.45 + 0.55 * brightness)
        coverage[index] += opacity
        scores[index] += weight
        buckets[index].append((hue, saturation, weight))
    if sum(coverage) < total * 0.025:
        average = [channel / max(0.001, total) / 255 for channel in averages]
        hue, saturation, _ = colorsys.rgb_to_hsv(*average)
        return (hue, saturation) if saturation >= 0.08 else (0.0, 0.0)

    def neighbourhood(values, index):
        return values[index] + 0.60 * (values[(index - 1) % _HUE_BINS] + values[(index + 1) % _HUE_BINS])

    candidates = [index for index in range(_HUE_BINS) if neighbourhood(coverage, index) >= total * 0.02]
    if not candidates:
        return 0.0, 0.0
    chosen = max(candidates, key=lambda index: neighbourhood(scores, index))
    x = y = saturation_sum = weight_sum = 0.0
    for offset in (-1, 0, 1):
        index = (chosen + offset) % _HUE_BINS
        factor = 1.0 if offset == 0 else 0.60
        for hue, saturation, weight in buckets[index]:
            weight *= factor
            x += math.cos(hue * math.tau) * weight
            y += math.sin(hue * math.tau) * weight
            saturation_sum += saturation * weight
            weight_sum += weight
    if weight_sum <= 0:
        return 0.0, 0.0
    return (math.atan2(y, x) / math.tau) % 1.0, saturation_sum / weight_sum


def extract_profile_theme(avatar_bytes: bytes | None, *, fallback_key: str = "") -> dict:
    try:
        pixels = _pixels(avatar_bytes)
        if not pixels:
            return _fallback(fallback_key)
        hue, saturation = _dominant(pixels)
        return _theme(hue, saturation, "avatar")
    except Exception:
        return _fallback("")
