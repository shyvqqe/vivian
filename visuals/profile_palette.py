"""Derive a restrained glass-card palette locally from a member's avatar.

No image or user identifier leaves the process. Colour extraction deliberately
uses sizeable colour regions rather than a single, potentially noisy pixel.
"""
from __future__ import annotations

import colorsys
import hashlib
from io import BytesIO
import math
import warnings

from PIL import Image, ImageOps

_MAX_BYTES = 8 * 1024 * 1024
_MAX_PIXELS = 20_000_000
_HUE_BINS = 24
_COMPOSITE = (40, 40, 44, 255)


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
    # A darker glass base keeps small Cyrillic labels readable. The surrounding
    # brighter gradient and pale accent provide the soft profile-colour effect.
    saturation = max(0.025, min(0.50, saturation))
    panel_lightness = 0.34
    panel = _rgb(hue, panel_lightness, saturation * 0.64)
    text = _rgb(hue, 0.98, 0.10)
    muted = _rgb(hue, 0.86, min(0.18, saturation * 0.4))
    while _contrast(muted, panel) < 5.0 and panel_lightness > 0.20:
        panel_lightness -= 0.01
        panel = _rgb(hue, panel_lightness, saturation * 0.64)
    accent = _rgb(hue, 0.87, min(0.58, saturation * 1.25))
    return {
        "background_top": _rgb(hue, 0.64, saturation),
        "background_bottom": _rgb(hue, 0.29, saturation * 0.95),
        "glow": _rgb(hue, 0.78, min(0.62, saturation * 1.20)),
        "panel": panel,
        "text": text,
        "muted": muted,
        "accent": accent,
        "accent_hex": (accent[0] << 16) | (accent[1] << 8) | accent[2],
        "source": source,
    }


def _fallback(fallback_key: str) -> dict:
    # A small stable family, rather than a random hue on each refresh. These
    # intentionally remain subdued when there is no usable avatar to inspect.
    key = str(fallback_key or "")[:256].encode("utf-8", errors="ignore")
    index = hashlib.blake2b(key, digest_size=1).digest()[0] % 4 if key else 0
    hue = (0.75, 0.61, 0.91, 0.07)[index]
    return _theme(hue, 0.13, "fallback")


def _pixels(avatar_bytes: bytes | None) -> list | None:
    if not isinstance(avatar_bytes, (bytes, bytearray, memoryview)):
        return None
    if not avatar_bytes or len(avatar_bytes) > _MAX_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(avatar_bytes)) as image:
                if image.width < 1 or image.height < 1 or image.width * image.height > _MAX_PIXELS:
                    return None
                image.seek(0)
                image = ImageOps.exif_transpose(image).convert("RGBA")
                image.thumbnail((96, 96), Image.Resampling.LANCZOS)
                alpha = list(image.getchannel("A").getdata())
                composite = Image.alpha_composite(Image.new("RGBA", image.size, _COMPOSITE), image)
                return [(rgb[:3], opacity / 255) for rgb, opacity in zip(composite.getdata(), alpha)
                        if opacity >= 38]
    except Exception:
        # A corrupt upload, unusual image plugin, or oversized decoded image
        # must not prevent the rest of a public profile from being displayed.
        return None


def _dominant(pixels: list) -> tuple[float, float]:
    coverage = [0.0] * _HUE_BINS
    scores = [0.0] * _HUE_BINS
    saturation_sum = [0.0] * _HUE_BINS
    total = sum(opacity for _, opacity in pixels)
    neutral_channels = [0.0, 0.0, 0.0]
    for rgb, opacity in pixels:
        for index, value in enumerate(rgb):
            neutral_channels[index] += value * opacity
        hue, saturation, brightness = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
        if saturation < 0.22 or brightness < 0.10:
            continue
        index = int(hue * _HUE_BINS) % _HUE_BINS
        coverage[index] += opacity
        # Saturation helps recognise dyed hair, artwork, and coloured scenery;
        # the bounded multiplier cannot promote a few neon pixels to a theme.
        weight = opacity * (0.45 + 0.55 * saturation) * (0.65 + 0.35 * brightness)
        scores[index] += weight
        saturation_sum[index] += saturation * opacity

    coloured = sum(coverage)
    if coloured < total * 0.075:
        average = [channel / max(1.0, total) / 255 for channel in neutral_channels]
        hue, saturation, _ = colorsys.rgb_to_hsv(*average)
        return (hue if saturation >= 0.035 else 0.70), min(0.11, max(0.035, saturation * 0.4))

    def neighbourhood(values: list, index: int) -> float:
        return values[index] + 0.65 * (values[(index - 1) % _HUE_BINS]
                                      + values[(index + 1) % _HUE_BINS])

    # Three adjacent bins smooth quantisation and avoid choosing a boundary
    # between two almost identical blues or peaches as separate colours.
    candidates = [index for index in range(_HUE_BINS)
                  if neighbourhood(coverage, index) >= total * 0.055]
    if not candidates:
        return 0.70, 0.07
    chosen = max(candidates, key=lambda index: neighbourhood(scores, index))
    chosen_hue = (chosen + 0.5) / _HUE_BINS

    # Portrait skin is often the largest warm region. If there is a substantial
    # strongly coloured region elsewhere, let that identify the profile too.
    if 0.015 <= chosen_hue <= 0.16:
        alternatives = [index for index in candidates
                        if not 0.015 <= (index + 0.5) / _HUE_BINS <= 0.16
                        and neighbourhood(coverage, index) >= total * 0.12
                        and neighbourhood(saturation_sum, index)
                        / max(0.001, neighbourhood(coverage, index)) >= 0.48]
        if alternatives:
            alternative = max(alternatives, key=lambda index: neighbourhood(scores, index))
            if neighbourhood(scores, alternative) >= neighbourhood(scores, chosen) * 0.62:
                chosen = alternative

    x = y = saturation_total = weight_total = 0.0
    for offset in (-1, 0, 1):
        index = (chosen + offset) % _HUE_BINS
        weight = scores[index] * (1.0 if offset == 0 else 0.65)
        angle = ((index + 0.5) / _HUE_BINS) * math.tau
        x += math.cos(angle) * weight
        y += math.sin(angle) * weight
        saturation_total += saturation_sum[index] * (1.0 if offset == 0 else 0.65)
        weight_total += coverage[index] * (1.0 if offset == 0 else 0.65)
    hue = (math.atan2(y, x) / math.tau) % 1.0
    observed_saturation = saturation_total / max(0.001, weight_total)
    return hue, 0.24 + min(0.24, observed_saturation * 0.28)


def extract_profile_theme(avatar_bytes: bytes | None, *, fallback_key: str = "") -> dict:
    """Return safe RGB colours and a Discord accent integer; never raise.

    ``panel`` is a finished darker glass base; ``text`` and ``muted`` have at
    least 5:1 contrast on it. Transparent avatars are composited on neutral
    charcoal and transparent margins do not dominate colour selection.
    """
    try:
        pixels = _pixels(avatar_bytes)
        if not pixels:
            return _fallback(fallback_key)
        hue, saturation = _dominant(pixels)
        return _theme(hue, saturation, "avatar")
    except Exception:
        # The no-argument fallback is independent of malformed optional input.
        return _fallback("")
