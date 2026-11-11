"""Public *metadata* adapters for external catalogs; audio stays YouTube/SoundCloud.

Compatible with the music_catalog.prepare/search interface. These functions never
claim to stream Spotify, Apple Music, VK or Yandex audio. A lookup failure does
not turn a private/unknown catalog URL into a fake YouTube track.
"""
from __future__ import annotations

from functools import lru_cache
import os
from urllib.parse import urlencode
import json
import re
from typing import Optional

from commands import music_links

SOURCES = {
    "youtube": "YouTube", "soundcloud": "SoundCloud", "spotify": "Spotify",
    "apple": "Apple Music", "yandex": "Яндекс Музыка", "vk": "VK Музыка",
}
PREFIXES = music_links.ALIASES


def prepare(raw: str) -> tuple[str, Optional[str]]:
    """Validate source/URL and return (query, catalog name or None). No I/O."""
    if not isinstance(raw, str) or not 1 <= len(raw.strip()) <= 400:
        raise ValueError("Напиши название песни или HTTPS-ссылку (до 400 символов).")
    value = raw.strip()
    if value.casefold() in PREFIXES:
        raise ValueError("После названия сервиса укажи трек или ссылку.")
    prefix, sep, rest = value.partition(" ")
    source = PREFIXES.get(prefix.casefold()) if sep else None
    if source:
        value = rest.strip()
        if not value:
            raise ValueError("После названия сервиса укажи трек или ссылку.")
    if value.startswith(("https://", "http://")):
        detected, _ = music_links._validated_url(value)
        if source and source != detected:
            raise ValueError("Название сервиса не совпадает со ссылкой.")
        return value, detected
    if "://" in value or value.startswith("//"):
        raise ValueError("Некорректная ссылка на музыкальный сервис.")
    return value, source


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:200]


def _bridged(source: str, title: str, artist: str = "", url: str = "") -> dict:
    title, artist = _clean(title), _clean(artist)
    return {
        "source": SOURCES[source], "source_id": source,
        "title": title, "artist": artist, "bridged": True,
        "relay_query": f"{artist} - {title}" if artist else title,
        "stream_url": None, "url": url,
    }


def _get(url: str, headers=None, timeout=5) -> bytes:
    """Shared bounded HTTP getter (URLs must be fixed API endpoints)."""
    # music_links uses a restricted redirect handler, size cap and timeout.
    return music_links._fetch(url, max_bytes=196608, timeout=timeout, headers=headers)


def _json(url: str, headers=None, timeout=5) -> dict:
    return json.loads(_get(url, headers=headers, timeout=timeout).decode("utf-8", "replace"))


@lru_cache(maxsize=256)
def _apple_search(query: str, limit: int) -> tuple[tuple[str, str, str], ...]:
    country = os.getenv("APPLE_MUSIC_COUNTRY", "RU").upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        country = "RU"
    url = "https://itunes.apple.com/search?" + urlencode({
        "term": query, "entity": "song", "limit": min(max(limit, 1), 10), "country": country,
    })
    payload = _json(url)
    if not isinstance(payload, dict):
        return ()
    results = []
    seen = set()
    for record in payload.get("results", []):
        if not isinstance(record, dict) or record.get("kind") not in {None, "song"}:
            continue
        title, artist = _clean(record.get("trackName")), _clean(record.get("artistName"))
        if not title or not artist or (artist.casefold(), title.casefold()) in seen:
            continue
        seen.add((artist.casefold(), title.casefold()))
        url_value = str(record.get("trackViewUrl") or "")
        if not url_value.startswith("https://music.apple.com/"):
            url_value = ""
        results.append((title, artist, url_value))
        if len(results) >= limit:
            break
    return tuple(results)


def search(query: str, source: Optional[str] = None, limit: int = 2) -> list[dict]:
    """Search for catalog metadata. Non-playable results are explicitly bridged.

    Plain Spotify/VK/Yandex searches don't impersonate catalog APIs. Direct
    catalog links require readable public metadata or give a useful error.
    """
    value, detected = prepare(query)
    src = detected or (source or "youtube").casefold()
    if src not in SOURCES:
        raise ValueError("Неизвестный музыкальный сервис.")
    limit = min(max(int(limit), 1), 10)
    if value.startswith("https://"):
        if src in {"youtube", "soundcloud"}:
            return [_bridged(src, value, url=value)]
        # The existing parser handles strict URL validation and errors.
        # Preserve artist data where the official Spotify/Apple APIs expose it.
        if src == "spotify":
            title, artist = music_links._spotify_details(value)
            return [_bridged(src, title, artist, value)]
        if src == "apple":
            title, artist = music_links._apple_details(value)
            return [_bridged(src, title, artist, value)]
        title, _, _ = music_links.parse_music_request(value)
        return [_bridged(src, title, url=value)]
    if src == "apple":
        try:
            rows = _apple_search(value, limit)
        except (ValueError, OSError, music_links.MetadataError):
            rows = ()
        return ([_bridged(src, title, artist, url) for title, artist, url in rows]
                or [_bridged(src, value)])
    return [_bridged(src, value)]
