from __future__ import annotations
from difflib import SequenceMatcher
import re
from urllib.parse import urlparse
from commands import music_relay as commands_music_relay


def _is_soundcloud_url(query: str) -> bool:
    host = (urlparse(query).hostname or "").lower()
    return host == "soundcloud.com" or host.endswith(".soundcloud.com")


def _is_youtube_url(query: str) -> bool:
    host = (urlparse(query).hostname or "").lower()
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def _entries_from_info(info):
    if not info:
        return []
    entries = info.get("entries")
    if entries is not None:
        return [entry for entry in entries if entry]
    return [info]


def _normalise_track_text(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (value or "").casefold()).split())


def _soundcloud_title_score(query: str, entry: dict) -> float:
    query_text = _normalise_track_text(query)
    title_text = _normalise_track_text(entry.get("title", ""))
    artist_text = _normalise_track_text(
        entry.get("artist") or entry.get("uploader") or entry.get("creator") or ""
    )
    if not query_text or not title_text:
        return 0.0
    if query_text == title_text:
        return 2.0
    if query_text in title_text:
        return 1.0 + len(query_text) / len(title_text)
    if title_text in query_text:
        return 1.0 + len(title_text) / len(query_text)

    query_words = set(query_text.split())
    title_words = set(title_text.split())
    artist_words = set(artist_text.split())
    title_overlap = len(query_words & title_words) / len(query_words)
    artist_overlap = len(query_words & artist_words) / len(query_words)
    similarity = SequenceMatcher(None, query_text, title_text).ratio()
    return 0.68 * title_overlap + 0.17 * artist_overlap + 0.15 * similarity


def _resolve_music_url(url: str, fallback_title: str = "", source_hint: str | None = None,
                       backend_hint: str | None = None):
    """Runs in an executor so local yt-dlp does not block the Discord event loop."""
    track = _make_relay_track(url, source_hint or "youtube")
    track["title"] = fallback_title or track["title"]
    track["backend"] = ("local" if backend_hint == "local"
                        or commands_music_relay._relay_credentials() is None else "relay")
    track["stream_url"] = commands_music_relay._relay_audio_url(
        track["relay_query"],
        "soundcloud" if track["source"] == "SoundCloud" else "youtube",
        prefer_local=backend_hint == "local",
    )
    return track


def _make_relay_track(query: str, source: str = "youtube") -> dict:
    query = commands_music_relay._validated_music_query(query)
    source = commands_music_relay._normalise_music_source(source)
    if _is_soundcloud_url(query):
        source = "soundcloud"
    elif _is_youtube_url(query):
        source = "youtube"
    return {
        "title": query,
        "webpage_url": query if _is_soundcloud_url(query) or _is_youtube_url(query) else "",
        "relay_query": query,
        "stream_url": None,  # Resolved asynchronously only when playback starts.
        "backend": "auto",
        "artist": "",
        "duration": None,
        "source": "SoundCloud" if source == "soundcloud" else "YouTube",
        "thumbnail": None,
    }


def _resolve_soundcloud_url(url: str, fallback_title: str = ""):
    """Backward-compatible resolver for saved SoundCloud track entries."""
    return _resolve_music_url(url, fallback_title, source_hint="SoundCloud")


def _search_music_platform(query: str, platform: str, limit: int = 10):
    entries = commands_music_relay._relay_search(query, platform, limit)
    if not entries:
        return []

    ranked = sorted(entries, key=lambda entry: _soundcloud_title_score(query, entry), reverse=True)
    tracks = []
    seen_urls = set()
    for entry in ranked:
        if _soundcloud_title_score(query, entry) < 0.15:
            break
        track_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
        if not track_url or track_url in seen_urls:
            continue
        seen_urls.add(track_url)
        tracks.append({
            "title": entry.get("title") or query,
            "webpage_url": track_url,
            "artist": entry.get("artist") or entry.get("uploader") or entry.get("creator") or "",
            "duration": entry.get("duration"),
            "source": entry["source"],
            "thumbnail": entry.get("thumbnail"),
            "backend": entry.get("backend", "auto"),
        })
        if len(tracks) >= min(max(limit, 1), 10):
            break
    return tracks


def _merge_music_search_results(query: str, platform_results: list[list[dict]], limit: int = 10) -> list[dict]:
    candidates = [track for results in platform_results for track in results]
    candidates.sort(
        key=lambda track: (
            _soundcloud_title_score(query, track),
            track.get("source") == "SoundCloud",
        ),
        reverse=True,
    )
    selected = []
    seen_urls = set()
    seen_tracks = set()
    for track in candidates:
        url = track.get("webpage_url")
        if not url or url in seen_urls:
            continue
        duplicate_key = (
            _normalise_track_text(track.get("title", "")),
            _normalise_track_text(track.get("artist", "")),
        )
        if duplicate_key[0] and duplicate_key in seen_tracks:
            continue
        seen_urls.add(url)
        seen_tracks.add(duplicate_key)
        selected.append(track)
        if len(selected) >= min(max(limit, 1), 10):
            break
    return selected
