"""YouTube/SoundCloud metadata and audio resolution.

A home relay is preferred when configured. If it is not configured, or a
search request fails, metadata is extracted locally with yt-dlp. Resolution
always runs in a worker thread via commands.music_audio._extract_in_executor.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
import time
import logging
import math
import os
import shutil
import string
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen


log = logging.getLogger(__name__)

# Deployment fingerprint: proves Render imported this version, not old relay-only code.
print("[Music backend] PLAYFIX-2026-11-11: fast search, bounded fallback", flush=True)


class RelayError(RuntimeError):
    """Music backend failure; never put the authenticated relay URL in here."""


_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
_SOUNDCLOUD_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"}
_MAX_RESPONSE_BYTES = 512 * 1024
_YOUTUBE_PUBLIC_MAX_BYTES = 2 * 1024 * 1024
_QUICK_SEARCH_TIMEOUT = 4
_RELAY_SEARCH_TIMEOUT = 7
_LOCAL_SEARCH_TIMEOUT = 12
_AUDIO_RESOLVE_TIMEOUT = 22


def _normalise_music_source(source: str | None = None) -> str:
    source = (source or "youtube").casefold()
    if source not in {"youtube", "soundcloud"}:
        raise ValueError("Выбери источник YouTube или SoundCloud.")
    return source


def _validated_music_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("Напиши название трека или ссылку.")
    query = query.strip()
    if not query:
        raise ValueError("Напиши название трека или ссылку.")
    if len(query) > 200:
        raise ValueError("Запрос должен быть не длиннее 200 символов.")
    if any(ord(char) < 32 or ord(char) == 127 for char in query):
        raise ValueError("Напиши запрос одной строкой.")
    try:
        parsed = urlsplit(query)
    except ValueError:
        raise ValueError("Поддерживаются ссылки YouTube и SoundCloud.") from None
    if parsed.scheme in {"http", "https"} or query.startswith("//") or "://" in query:
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError("Поддерживаются ссылки YouTube и SoundCloud.")
        try:
            if parsed.port not in {None, 80, 443}:
                raise ValueError("Поддерживаются ссылки YouTube и SoundCloud.")
        except ValueError:
            raise ValueError("Поддерживаются ссылки YouTube и SoundCloud.") from None
        if parsed.hostname.casefold() not in _YOUTUBE_HOSTS | _SOUNDCLOUD_HOSTS:
            raise ValueError("Поддерживаются ссылки YouTube и SoundCloud.")
        if (parsed.hostname.casefold() in _YOUTUBE_HOSTS and parsed.path.rstrip("/") == "/playlist") or (
                parsed.hostname.casefold() in _SOUNDCLOUD_HOSTS and "/sets/" in parsed.path):
            raise ValueError("Ссылки на плейлисты не поддерживаются.")
    return query


def _source_for_query(query: str, source: str) -> str:
    hostname = (urlsplit(query).hostname or "").lower()
    if hostname in _YOUTUBE_HOSTS:
        return "youtube"
    if hostname in _SOUNDCLOUD_HOSTS:
        return "soundcloud"
    return _normalise_music_source(source)


def _relay_credentials() -> tuple[str, str] | None:
    base = os.getenv("RELAY_URL", "").strip().rstrip("/")
    token = os.getenv("RELAY_TOKEN", "").strip()
    if not base and not token:
        return None
    # An incomplete pair must not leak half-authenticated requests.
    if not base or not token:
        log.warning("RELAY_URL and RELAY_TOKEN must be set together; using local music backend")
        return None
    try:
        parsed = urlsplit(base)
        _ = parsed.port
    except ValueError:
        log.warning("Invalid RELAY_URL; using local music backend")
        return None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        log.warning("Invalid RELAY_URL; using local music backend")
        return None
    return base, token


def _relay_url(endpoint: str, query: str, source: str = "youtube", limit: int | None = None) -> str:
    query = _validated_music_query(query)
    source = _source_for_query(query, source)
    credentials = _relay_credentials()
    if credentials is None:
        raise RelayError("Домашний relay не настроен.")
    base, token = credentials
    url = f"{base}/{endpoint}?token={quote(token, safe='')}&q={quote(query, safe='')}"
    if endpoint == "search" or source != "youtube":
        url += f"&source={source}"
    if limit is not None:
        url += f"&limit={min(max(int(limit), 1), 10)}"
    return url


def _yt_dlp_options() -> list[str]:
    # yt-dlp[default] supplies EJS scripts; JavaScript runtime is also required.
    options = ["--ignore-config", "--no-playlist"]
    if not shutil.which("deno") and shutil.which("node"):
        options += ["--js-runtimes", "node"]
    return options


def _run_ytdlp(args: list[str], timeout: int) -> bytes:
    command = [
        "yt-dlp", *_yt_dlp_options(), "--no-warnings", "--quiet",
        "--socket-timeout", "5", "--retries", "1", "--extractor-retries", "1",
        *args,
    ]
    try:
        process = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise RelayError("yt-dlp не установлен. Проверь requirements.txt и Dockerfile.") from None
    except subprocess.TimeoutExpired:
        raise RelayError("Источник музыки не ответил вовремя.") from None
    if process.returncode != 0:
        # Do not include yt-dlp stderr: it can contain signed media URLs.
        log.warning("Local yt-dlp failed (exit status %s)", process.returncode)
        raise RelayError("Не удалось получить музыку через yt-dlp. Проверь доступность источника.")
    return process.stdout


def _safe_track(entry: dict, fallback_source: str) -> dict | None:
    if not isinstance(entry, dict):
        return None
    candidate = None
    # SoundCloud flat entries may put a signed API endpoint into `url` while
    # leaving the public webpage in `original_url` or `permalink_url`.
    for field in ("webpage_url", "original_url", "permalink_url", "url"):
        value = entry.get(field)
        try:
            parsed = urlsplit(value) if isinstance(value, str) else None
        except ValueError:
            continue
        if (parsed is not None and parsed.scheme in {"http", "https"}
                and parsed.hostname in _YOUTUBE_HOSTS | _SOUNDCLOUD_HOSTS):
            candidate = value
            break
    if not candidate and fallback_source == "youtube":
        video_id = entry.get("id")
        if (isinstance(video_id, str) and len(video_id) == 11 and
                all(char in string.ascii_letters + string.digits + "_-" for char in video_id)):
            candidate = "https://www.youtube.com/watch?" + urlencode({"v": video_id})
    if not candidate:
        return None
    try:
        candidate = _validated_music_query(candidate)
    except ValueError:
        return None
    source = _source_for_query(candidate, fallback_source)
    parsed = urlsplit(candidate)
    if source == "youtube" and parsed.hostname not in {"youtu.be", "www.youtu.be"}:
        video_id = parse_qs(parsed.query).get("v", [None])[0]
        if video_id and len(video_id) == 11:
            candidate = "https://www.youtube.com/watch?" + urlencode({"v": video_id})
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    duration = entry.get("duration")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration < 0):
        duration = None
    thumbnail = entry.get("thumbnail")
    if not isinstance(thumbnail, str) or not thumbnail.startswith("https://"):
        thumbnail = None
    return {
        "title": title.strip()[:300],
        "webpage_url": candidate,
        "artist": str(entry.get("artist") or entry.get("uploader") or entry.get("channel") or "")[:200],
        "duration": duration,
        "source": "SoundCloud" if source == "soundcloud" else "YouTube",
        "thumbnail": thumbnail,
    }


def _normalise_tracks(entries: list, source: str, limit: int, backend: str) -> list[dict]:
    tracks = []
    seen = set()
    for entry in entries:
        track = _safe_track(entry, source)
        if track and track["webpage_url"] not in seen:
            track["backend"] = backend
            tracks.append(track)
            seen.add(track["webpage_url"])
        if len(tracks) >= limit:
            break
    return tracks


def _youtube_renderer_items(value, *, budget: int = 5000):
    """Visit the public YouTube search response without assuming a fixed layout."""
    stack = [value]
    while stack and budget:
        budget -= 1
        node = stack.pop()
        if isinstance(node, dict):
            video = node.get("videoRenderer")
            if isinstance(video, dict):
                yield video
                continue
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(reversed(node))


def _youtube_text(value) -> str:
    if not isinstance(value, dict):
        return ""
    runs = value.get("runs")
    if isinstance(runs, list):
        return "".join(str(part.get("text", "")) for part in runs if isinstance(part, dict)).strip()
    return str(value.get("simpleText") or "").strip()


def _youtube_duration(value) -> int | None:
    text = _youtube_text(value)
    parts = text.split(":")
    if not 1 < len(parts) <= 3 or not all(part.isascii() and part.isdigit() for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds if seconds > 0 else None


def _public_youtube_search(query: str, limit: int) -> list[dict]:
    """Fast, unauthenticated metadata lookup. yt-dlp remains the audio extractor.

    YouTube can change/block this endpoint; failures fall back to the supported
    yt-dlp backend. No user cookie, account token, or protected stream is used.
    """
    query = _validated_music_query(query)
    if urlsplit(query).scheme or _source_for_query(query, "youtube") != "youtube":
        return []
    payload = {
        "context": {"client": {"clientName": "WEB", "clientVersion": "2.20241126.01.00",
                               "hl": "ru", "gl": "US"}},
        "query": query,
    }
    request = Request(
        "https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json",
                 "Accept": "application/json", "Origin": "https://www.youtube.com"},
        method="POST",
    )
    with urlopen(request, timeout=_QUICK_SEARCH_TIMEOUT) as response:
        if "json" not in response.headers.get("Content-Type", "").lower():
            return []
        raw = response.read(_YOUTUBE_PUBLIC_MAX_BYTES + 1)
    if len(raw) > _YOUTUBE_PUBLIC_MAX_BYTES:
        return []
    data = json.loads(raw)
    candidates = []
    seen = set()
    for item in _youtube_renderer_items(data):
        video_id = item.get("videoId")
        if (not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                or video_id in seen):
            continue
        title = _youtube_text(item.get("title"))
        if not title:
            continue
        seen.add(video_id)
        thumbnail = None
        thumbs = (item.get("thumbnail") or {}).get("thumbnails") or []
        if isinstance(thumbs, list):
            for thumb in reversed(thumbs):
                if isinstance(thumb, dict) and str(thumb.get("url", "")).startswith("https://"):
                    thumbnail = thumb["url"]
                    break
        entry = {
            "id": video_id,
            "title": title,
            "channel": _youtube_text(item.get("ownerText")),
            "duration": _youtube_duration(item.get("lengthText")),
            "thumbnail": thumbnail,
        }
        candidates.append(entry)
        if len(candidates) >= limit:
            break
    return _normalise_tracks(candidates, "youtube", limit, "local")


def _direct_link_track(query: str, source: str) -> dict | None:
    """Queue supported direct *track* URLs without a redundant network search."""
    parsed = urlsplit(query)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if source == "youtube":
        video_id = None
        if host in {"youtu.be", "www.youtu.be"}:
            video_id = parsed.path.strip("/").split("/")[0]
        elif parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif parsed.path.startswith(("/shorts/", "/live/")):
            video_id = parsed.path.split("/")[2]
        if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return None
        return {
            "title": "YouTube видео", "webpage_url": "https://www.youtube.com/watch?v=" + video_id,
            "artist": "", "duration": None, "source": "YouTube", "thumbnail": None,
            "backend": "local", "relay_query": "https://www.youtube.com/watch?v=" + video_id,
        }
    if source == "soundcloud" and host in _SOUNDCLOUD_HOSTS:
        if len([part for part in parsed.path.split("/") if part]) < 2:
            return None
        return {"title": "SoundCloud трек", "webpage_url": query, "artist": "",
                "duration": None, "source": "SoundCloud", "thumbnail": None,
                "backend": "local", "relay_query": query}
    return None


def _local_search(query: str, source: str = "youtube", limit: int = 10) -> list[dict]:
    query = _validated_music_query(query)
    source = _source_for_query(query, source)
    limit = min(max(int(limit), 1), 10)
    is_url = urlsplit(query).scheme in {"http", "https"}
    target = query if is_url else ("scsearch" if source == "soundcloud" else "ytsearch") + f"{limit}:{query}"
    stdout = _run_ytdlp([
        "--dump-single-json", "--skip-download", "--flat-playlist", "--playlist-end", str(limit),
        "--", target,
    ], timeout=_LOCAL_SEARCH_TIMEOUT)
    if len(stdout) > _MAX_RESPONSE_BYTES:
        raise RelayError("Источник вернул слишком большой ответ.")
    try:
        payload = json.loads(stdout)
    except (ValueError, UnicodeError):
        raise RelayError("Источник вернул некорректный ответ.") from None
    if not isinstance(payload, dict):
        raise RelayError("Источник вернул некорректный ответ.")
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else [payload]
    return _normalise_tracks(entries, source, limit, "local")


def _relay_search_uncached(query: str, source: str, limit: int = 10) -> list[dict]:
    query = _validated_music_query(query)
    source = _source_for_query(query, source)
    limit = min(max(int(limit), 1), 10)
    # Direct track URLs already identify the recording; a search would add
    # latency and sometimes fail due to YouTube blocking search specifically.
    direct = _direct_link_track(query, source)
    if direct:
        return [direct]
    # Try a short public metadata search before spawning yt-dlp. Never send
    # SoundCloud queries to the YouTube endpoint.
    if source == "youtube" and urlsplit(query).scheme not in {"http", "https"}:
        try:
            fast_tracks = _public_youtube_search(query, limit)
            if fast_tracks:
                return fast_tracks
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeError, TypeError) as error:
            log.info("Fast YouTube metadata lookup unavailable (%s)", type(error).__name__)
    if _relay_credentials() is not None:
        try:
            request = Request(_relay_url("search", query, source, limit), headers={"Accept": "application/json"})
            with urlopen(request, timeout=_RELAY_SEARCH_TIMEOUT) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise RelayError("Relay вернул слишком большой ответ.")
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("tracks"), list):
                raise RelayError("Relay вернул некорректный список.")
            tracks = _normalise_tracks(data["tracks"], source, limit, "relay")
            if tracks:
                return tracks
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeError, RelayError) as exc:
            # Always drop the signed URL from errors/logs.
            log.warning("Music relay search failed (%s); using local backend", type(exc).__name__)
    return _local_search(query, source, limit)


@lru_cache(maxsize=256)
def _cached_music_search(query: str, source: str, limit: int, bucket: int) -> tuple[dict, ...]:
    """Reuse short-lived metadata results; never cache signed audio stream URLs."""
    return tuple(_relay_search_uncached(query, source, limit))


def _relay_search(query: str, source: str, limit: int = 10) -> list[dict]:
    query = _validated_music_query(query)
    source = _source_for_query(query, source)
    limit = min(max(int(limit), 1), 10)
    # 90s time buckets avoid serving stale catalog results for long periods.
    tracks = [dict(track) for track in _cached_music_search(query, source, limit, int(time.monotonic() // 90))]
    if not tracks:
        # A transient empty result must not be kept for the whole cache window.
        _cached_music_search.cache_clear()
    return tracks


def _local_audio_url(query: str, source: str) -> str:
    query = _validated_music_query(query)
    source = _source_for_query(query, source)
    is_url = urlsplit(query).scheme in {"http", "https"}
    target = query if is_url else ("scsearch1:" if source == "soundcloud" else "ytsearch1:") + query
    raw = _run_ytdlp(["--format", "bestaudio/best", "--get-url", "--playlist-end", "1", "--", target], timeout=_AUDIO_RESOLVE_TIMEOUT)
    # yt-dlp returns a short-lived media URL; only use its first valid line.
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith(("https://", "http://")):
            return line
    raise RelayError("yt-dlp не вернул аудиопоток.")


def _is_premature_end(track: dict | None, elapsed: float | None) -> bool:
    """Detect broken HTTP media streams that FFmpeg treats as clean EOF.

    No media URLs or private relay tokens are logged or returned.
    """
    if not track or elapsed is None or elapsed < 0 or elapsed >= 2.0:
        return False
    duration = track.get("duration")
    return not isinstance(duration, (int, float)) or duration > 8


