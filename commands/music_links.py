"""Convert public music track links into search terms for playable sources.

Spotify/Apple/VK/Yandex provide track *metadata*, not audio to this bot.
Playback is resolved independently from YouTube/SoundCloud by music_relay.
No cookies, account sessions, DRM decryption or private API credentials required.
"""
from __future__ import annotations

import html
from html.parser import HTMLParser
from functools import lru_cache
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ALIASES = {
    "yt": "youtube", "youtube": "youtube", "ютуб": "youtube",
    "sc": "soundcloud", "soundcloud": "soundcloud", "саундклауд": "soundcloud",
    "vk": "vk", "вк": "vk", "вконтакте": "vk", "vkontakte": "vk",
    "yandex": "yandex", "yandexmusic": "yandex", "яндекс": "yandex", "ям": "yandex",
    "ямузыка": "yandex", "я.музыка": "yandex",
    "spotify": "spotify", "спотифай": "spotify", "споти": "spotify",
    "apple": "apple", "applemusic": "apple", "эпл": "apple", "айтьюнс": "apple",
}
PLATFORM_NAMES = {
    "youtube": "YouTube", "soundcloud": "SoundCloud", "vk": "VK Музыка",
    "yandex": "Яндекс Музыка", "spotify": "Spotify", "apple": "Apple Music",
}
_ALLOWED_HOSTS = {
    "youtube.com": "youtube", "www.youtube.com": "youtube", "m.youtube.com": "youtube",
    "music.youtube.com": "youtube", "youtu.be": "youtube", "www.youtu.be": "youtube",
    "soundcloud.com": "soundcloud", "www.soundcloud.com": "soundcloud",
    "m.soundcloud.com": "soundcloud", "on.soundcloud.com": "soundcloud",
    "open.spotify.com": "spotify", "spotify.link": "spotify",
    "music.apple.com": "apple", "itunes.apple.com": "apple",
    "music.yandex.ru": "yandex", "music.yandex.com": "yandex",
    "music.yandex.kz": "yandex", "music.yandex.by": "yandex",
    "vk.com": "vk", "m.vk.com": "vk", "music.vk.com": "vk",
    "vk.ru": "vk", "music.vk.ru": "vk",
}


class MetadataError(ValueError):
    """A track cannot be identified from its public page."""


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.props = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        attrs = dict(attrs)
        key = attrs.get("property") or attrs.get("name")
        if key in {"og:title", "og:description", "twitter:title", "description"}:
            self.props[key] = html.unescape(attrs.get("content") or "")


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
            raise MetadataError("Небезопасное перенаправление музыкальной ссылки.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(_SafeRedirect())


def _fetch(url: str, *, max_bytes: int = 196608, timeout: float = 5, headers=None) -> bytes:
    _validated_url(url)  # The initial URL must be safe, not only its redirects.
    request_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; VivianMusicBot/1.0)",
        "Accept": "application/json,text/html",
    }
    if isinstance(headers, dict):
        for name in ("User-Agent", "Accept", "Referer", "X-Requested-With"):
            if name in headers and isinstance(headers[name], str):
                request_headers[name] = headers[name][:256]
    req = Request(url, headers=request_headers)
    try:
        with _OPENER.open(req, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise MetadataError("Сервис не отдал публичные данные трека. Напиши исполнителя и название песни.") from exc
    if len(data) > max_bytes:
        raise MetadataError("Страница сервиса слишком большая. Напиши название песни.")
    return data


def _validated_url(url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise MetadataError("Некорректная ссылка на трек.") from exc
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or port not in {None, 443} or host not in _ALLOWED_HOSTS):
        raise MetadataError("Поддерживаются HTTPS-ссылки YouTube, SoundCloud, VK, Яндекс, Spotify и Apple Music.")
    return _ALLOWED_HOSTS[host], host


def _text(value: str) -> str:
    text = html.unescape(re.sub(r"\s+", " ", value or "")).strip(" -–—|\n")
    text = re.sub(r"\s*(?:[|—–-]\s*)?(?:слушать онлайн|слушать песню)\b.*$", "", text, flags=re.I)
    text = re.sub(r"\s*[|—–-]\s*(?:Spotify|Apple Music|Яндекс Музыка|Yandex Music|VK Музыка|ВКонтакте)\s*$", "", text, flags=re.I)
    return text.strip()[:180]


def _html_metadata(url: str) -> str:
    parser = _MetaParser()
    parser.feed(_fetch(url, max_bytes=524288).decode("utf-8", errors="replace"))
    title = _text(parser.props.get("og:title") or parser.props.get("twitter:title") or "")
    if not title or title.casefold() in {"vk", "вконтакте", "vk музыка", "яндекс музыка", "yandex music"}:
        raise MetadataError("Не удалось прочитать публичные данные трека. Пришли название и исполнителя текстом.")
    # Public og:description often contains the artist; only use short, clearly delimited entries.
    description = _text(parser.props.get("og:description") or "")
    if (description and len(description) < 85 and not any(char in title for char in "—–-")
            and not description.lower().startswith(("слуш", "listen", "музык"))):
        title = f"{title} {description}"
    return title[:180]


@lru_cache(maxsize=256)
def _spotify_details(url: str) -> tuple[str, str]:
    """Extract public title and artist where provided by Spotify oEmbed."""
    path = urlsplit(url).path
    if urlsplit(url).hostname == "spotify.link":
        # Follow only redirects to the explicitly allowed music hosts.
        try:
            with _OPENER.open(Request(url, headers={"User-Agent": "VivianMusicBot/1.0"}), timeout=5) as response:
                url = response.geturl()
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise MetadataError("Не получилось раскрыть короткую ссылку Spotify.") from exc
        if _validated_url(url)[0] != "spotify":
            raise MetadataError("Короткая ссылка ведёт не на Spotify.")
        path = urlsplit(url).path
    match = re.search(r"/(?:intl-[a-z]{2}/)?track/([A-Za-z0-9]{22})(?:/|$)", path)
    if not match:
        raise MetadataError("Spotify: поддерживаются ссылки на отдельные треки, не плейлисты и альбомы.")
    public_url = "https://open.spotify.com/track/" + match.group(1)
    try:
        payload = json.loads(_fetch("https://open.spotify.com/oembed?" + urlencode({"url": public_url})))
    except (ValueError, MetadataError) as exc:
        raise MetadataError("Spotify не отдал метаданные трека. Введи исполнителя и название песни.") from exc
    title = _text(str(payload.get("title") or ""))
    artist = _text(str(payload.get("author_name") or ""))
    if not title:
        raise MetadataError("Spotify не вернул название трека.")
    if artist and title.casefold().startswith(artist.casefold() + " - "):
        title = title[len(artist) + 3:].strip()
    return title, artist


def _spotify_metadata(url: str) -> str:
    title, artist = _spotify_details(url)
    return _text(f"{artist} - {title}") if artist else title


@lru_cache(maxsize=256)
def _apple_details(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    path = parts.path
    song_id = parse_qs(parts.query).get("i", [None])[0]
    if not song_id:
        match = re.search(r"/song/(?:[^/?]+/)?(\d{5,20})(?:/|$)", path)
        song_id = match.group(1) if match else None
    if song_id and song_id.isdigit():
        try:
            payload = json.loads(_fetch("https://itunes.apple.com/lookup?" + urlencode({"id": song_id, "entity": "song"})))
        except (ValueError, MetadataError):
            payload = {}  # Try public page metadata when iTunes is unavailable.
        for record in payload.get("results", []):
            if record.get("kind") == "song" and str(record.get("trackId")) == song_id:
                title = _text(str(record.get("trackName") or ""))
                artist = _text(str(record.get("artistName") or ""))
                if title:
                    return title, artist
    if "/song/" not in path and not song_id:
        raise MetadataError("Apple Music: отправь ссылку на песню (или альбом с параметром ?i=ID).")
    return _html_metadata(url), ""


def _apple_metadata(url: str) -> str:
    title, artist = _apple_details(url)
    return _text(f"{artist} {title}") if artist else title


@lru_cache(maxsize=256)
def _yandex_metadata(url: str) -> str:
    if not re.search(r"/track/\d+", urlsplit(url).path):
        raise MetadataError("Яндекс Музыка: нужна ссылка на конкретный трек, не альбом или плейлист.")
    return _html_metadata(url)


@lru_cache(maxsize=256)
def _vk_metadata(url: str) -> str:
    path = urlsplit(url).path
    if not (re.search(r"/audio-?\d+_\d+", path) or "/track/" in path):
        raise MetadataError("VK Музыка: отправь публичную ссылку на конкретный трек или название исполнителя и песни.")
    return _html_metadata(url)


def parse_music_request(query: str) -> tuple[str, str, str | None]:
    """Return (playable-search-term-or-url, playback-source, original-platform).

    For protected catalogs only PUBLIC metadata is used, and the matched audio
    comes from the independently searchable YouTube catalog.
    """
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 400:
        raise ValueError("Напиши название песни или ссылку (не более 400 символов).")
    query = query.strip()
    first, space, rest = query.partition(" ")
    tagged = ALIASES.get(first.casefold()) if space else None
    if tagged:
        query = rest.strip()
    if not query:
        raise ValueError("После названия сервиса укажи песню или ссылку.")
    if query.startswith(("http://", "https://")):
        platform, _ = _validated_url(query)
        if tagged and tagged != platform:
            raise ValueError("Указанный сервис не совпадает с сервисом ссылки.")
        if platform in {"youtube", "soundcloud"}:
            from commands.music_relay import _validated_music_query
            return _validated_music_query(query), platform, None
        metadata = {
            "spotify": _spotify_metadata, "apple": _apple_metadata,
            "yandex": _yandex_metadata, "vk": _vk_metadata,
        }[platform](query)
        return metadata, "youtube", PLATFORM_NAMES[platform]
    if "://" in query or query.startswith("//"):
        raise ValueError("Некорректная музыкальная ссылка.")
    if len(query) > 200:
        raise ValueError("Название песни должно быть не длиннее 200 символов.")
    if tagged in {"youtube", "soundcloud"}:
        return query, tagged, None
    return query, "youtube", PLATFORM_NAMES[tagged] if tagged else None


def resolve_music_request(query: str) -> tuple[str, str, str | None]:
    """Resolve public catalog metadata for commands without blocking the event loop.

    Call from a worker, never directly in an asyncio interaction callback.
    For Apple text queries, the public iTunes index often provides a better
    artist/title pairing than a generic search. An outage falls back to the
    user's original text, not an invented recording.
    """
    term, source, origin = parse_music_request(query)
    if origin == "Apple Music" and not query.strip().startswith(("https://", "http://")):
        raw = query.strip().split(None, 1)
        if len(raw) == 2 and ALIASES.get(raw[0].casefold()) == "apple":
            from commands import music_catalog
            hits = music_catalog.search(term, "apple", 1)
            best = hits[0] if hits else None
            if best and best.get("artist") and best.get("title"):
                term = best["relay_query"]
    return term, source, origin


def find_tracks(query: str, limit: int = 1) -> list[dict]:
    from commands import music_relay
    search, source, requested_from = resolve_music_request(query)
    results = music_relay._relay_search(search, source, limit)
    if requested_from:
        for result in results:
            result["requested_from"] = requested_from
    return results
