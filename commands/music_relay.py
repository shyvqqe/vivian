"""YouTube/SoundCloud metadata and audio resolution.

A home relay is preferred when configured. If it is not configured, or a
search request fails, metadata is extracted locally with yt-dlp. Resolution
always runs in a worker thread via commands.music_audio._extract_in_executor.
"""
from __future__ import annotations

import json
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
print("[Music backend] HOTFIX-2026-10-17-v2 loaded; local yt-dlp fallback enabled", flush=True)


class RelayError(RuntimeError):
    """Music backend failure; never put the authenticated relay URL in here."""


_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
_SOUNDCLOUD_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"}
_MAX_RESPONSE_BYTES = 512 * 1024


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
    parsed = urlsplit(base)
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
    command = ["yt-dlp", *_yt_dlp_options(), "--no-warnings", "--quiet", *args]
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


def _local_search(query: str, source: str = "youtube", limit: int = 10) -> list[dict]:
    query = _validated_music_query(query)
    source = _source_for_query(query, source)
    limit = min(max(int(limit), 1), 10)
    is_url = urlsplit(query).scheme in {"http", "https"}
    target = query if is_url else ("scsearch" if source == "soundcloud" else "ytsearch") + f"{limit}:{query}"
    stdout = _run_ytdlp([
        "--dump-single-json", "--skip-download", "--flat-playlist", "--playlist-end", str(limit),
        "--", target,
    ], timeout=30)
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


def _relay_search(query: str, source: str, limit: int = 10) -> list[dict]:
    query = _validated_music_query(query)
    source = _source_for_query(query, source)
    limit = min(max(int(limit), 1), 10)
    if _relay_credentials() is not None:
        request = Request(_relay_url("search", query, source, limit), headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=25) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise RelayError("Relay вернул слишком большой ответ.")
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("tracks"), list):
                raise RelayError("Relay вернул некорректный список.")
            return _normalise_tracks(data["tracks"], source, limit, "relay")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeError, RelayError) as exc:
            # Always drop the signed URL from errors/logs.
            log.warning("Music relay search failed (%s); using local backend", type(exc).__name__)
    return _local_search(query, source, limit)


def _local_audio_url(query: str, source: str) -> str:
    query = _validated_music_query(query)
    source = _source_for_query(query, source)
    is_url = urlsplit(query).scheme in {"http", "https"}
    target = query if is_url else ("scsearch1:" if source == "soundcloud" else "ytsearch1:") + query
    raw = _run_ytdlp(["--format", "bestaudio/best", "--get-url", "--playlist-end", "1", "--", target], timeout=35)
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


def _relay_audio_url(query: str, source: str = "youtube", *, prefer_local: bool = False) -> str:
    query = _validated_music_query(query)
    source = _source_for_query(query, source)
    if not prefer_local and _relay_credentials() is not None:
        return _relay_url("audio", query, source)
    return _local_audio_url(query, source)
