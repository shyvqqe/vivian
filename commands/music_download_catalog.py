"""Download original MP3s that Jamendo artists have opted to make available."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import io
import json
import logging
import os
import re

import httpx


API = "https://api.jamendo.com/v3.0/tracks/"
RESULT_LIMIT = 8
_MAX_CATALOG_BYTES = 256 * 1024
_REDIRECTS = {301, 302, 303, 307, 308}


class CatalogError(RuntimeError):
    pass


class FileTooLarge(CatalogError):
    pass


@dataclass(frozen=True, slots=True)
class Track:
    id: str
    title: str
    artist: str
    seconds: int
    downloadable: bool
    license_url: str = ""

    @property
    def page(self) -> str:
        return f"https://www.jamendo.com/track/{self.id}"


def client_id() -> str:
    return os.getenv("JAMENDO_CLIENT_ID", "").strip()


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(8, connect=4), follow_redirects=False, trust_env=False)


def _text(raw: object) -> str:
    return " ".join(raw.split())[:75] if isinstance(raw, str) else ""


def _track_id(raw: object) -> str:
    if type(raw) not in (str, int):
        return ""
    value = str(raw)
    return value if re.fullmatch(r"[1-9][0-9]{0,15}", value) else ""


def _license_url(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    match = re.fullmatch(
        r"https?://(?:www\.)?creativecommons\.org(/(?:licenses/[a-z-]+|publicdomain/(?:zero|mark))/[0-9]+\.[0-9]+/?)",
        raw,
    )
    return "https://creativecommons.org" + match[1] if match else ""


def _parse_tracks(payload: object) -> list[Track]:
    if not isinstance(payload, dict):
        raise CatalogError("Jamendo вернул неожиданный ответ.")
    headers = payload.get("headers")
    if not isinstance(headers, dict) or headers.get("status") != "success":
        raise CatalogError("Jamendo отклонил запрос. Проверь JAMENDO_CLIENT_ID и состояние сервиса.")
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise CatalogError("Jamendo вернул неожиданный список треков.")
    tracks = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        track_id = _track_id(row.get("id"))
        title, artist = _text(row.get("name")), _text(row.get("artist_name"))
        if not track_id or track_id in seen or not title or not artist:
            continue
        seen.add(track_id)
        seconds = row.get("duration")
        tracks.append(Track(
            track_id, title, artist, seconds if type(seconds) is int and 0 < seconds <= 86400 else 0,
            row.get("audiodownload_allowed") is True, _license_url(row.get("license_ccurl")),
        ))
        if len(tracks) == RESULT_LIMIT:
            break
    return tracks


async def search(client: httpx.AsyncClient, key: str, query: str) -> list[Track]:
    params = {
        "client_id": key, "format": "json", "type": "single albumtrack",
        "limit": RESULT_LIMIT, "include": "licenses", "search": query,
    }
    try:
        async with asyncio.timeout(12), client.stream("GET", API, params=params, follow_redirects=False) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes(16 * 1024):
                if len(body) + len(chunk) > _MAX_CATALOG_BYTES:
                    raise CatalogError("Jamendo вернул слишком большой ответ.")
                body.extend(chunk)
        return _parse_tracks(json.loads(body))
    except (httpx.HTTPError, TimeoutError, ValueError) as error:
        raise CatalogError("Поиск Jamendo временно недоступен.") from error


def _safe_download_url(url: httpx.URL) -> bool:
    host = url.host
    return (url.scheme == "https" and not url.userinfo and url.port in (None, 443) and (
        host in {"api.jamendo.com", "storage.jamendo.com", "mp3d.jamendo.com"}
        or re.fullmatch(r"prod-[0-9]+\.storage\.jamendo\.com", host) is not None
    ))


class _RedactJamendoQuery(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                value.copy_with(query=None) if isinstance(value, httpx.URL) and _safe_download_url(value) else value
                for value in record.args
            )
        return True


# Discord sets up root INFO logging, and HTTPX includes the entire URL by default.
logging.getLogger("httpx").addFilter(_RedactJamendoQuery())


def _has_mpeg_frame(prefix: bytes) -> bool:
    if len(prefix) < 4:
        return False
    # MPEG sync, a valid version, Layer III and a non-reserved bitrate/sample rate.
    return (prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0 and prefix[1] & 0x18 != 0x08
            and prefix[1] & 0x06 == 0x02 and prefix[2] >> 4 not in (0, 15) and prefix[2] & 0x0C != 0x0C)


def _has_mp3_audio(attachment: io.BytesIO) -> bool:
    attachment.seek(0)
    prefix = attachment.read(10)
    if not prefix.startswith(b"ID3"):
        return _has_mpeg_frame(prefix)
    if len(prefix) < 10 or prefix[3] not in (2, 3, 4) or any(byte & 128 for byte in prefix[6:10]):
        return False
    tag_size = 0
    for byte in prefix[6:10]:
        tag_size = (tag_size << 7) | byte
    has_footer = prefix[3] == 4 and prefix[5] & 0x10
    attachment.seek(10 + tag_size + (10 if has_footer else 0))
    return _has_mpeg_frame(attachment.read(4))


async def _download(client: httpx.AsyncClient, url: httpx.URL, limit: int) -> io.BytesIO:
    for _ in range(4):
        if not _safe_download_url(url):
            raise CatalogError("Jamendo вернул неподдерживаемый адрес файла.")
        # The CDN may label MP3s text/html. Validate the bytes, not that header.
        async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}, follow_redirects=False) as response:
            if response.status_code in _REDIRECTS and response.headers.get("location"):
                url = response.url.join(response.headers["location"])
                continue
            if response.status_code in (403, 404, 410):
                raise CatalogError("Источник больше не разрешает скачивание этого трека.")
            if response.status_code != 200:
                raise CatalogError("Jamendo не отдал MP3. Попробуй позже.")
            if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                raise CatalogError("Источник вернул неподдерживаемое кодирование файла.")
            length = response.headers.get("content-length", "")
            if length.isascii() and length.isdecimal() and (len(length) > 10 or int(length) > limit):
                raise FileTooLarge("MP3 превышает лимит вложений.")
            attachment = io.BytesIO()
            try:
                async for chunk in response.aiter_raw(64 * 1024):
                    if attachment.tell() + len(chunk) > limit:
                        raise FileTooLarge("MP3 превышает лимит вложений.")
                    attachment.write(chunk)
                    if attachment.tell() == len(chunk) and not (chunk.startswith(b"ID3") or _has_mpeg_frame(chunk)):
                        raise CatalogError("Источник не вернул MP3.")
                if not _has_mp3_audio(attachment):
                    raise CatalogError("Источник не вернул MP3.")
                attachment.seek(0)
                return attachment
            except BaseException:
                attachment.close()
                raise
    raise CatalogError("Источник вернул слишком много перенаправлений.")


async def download(client: httpx.AsyncClient, key: str, track: Track, limit: int) -> io.BytesIO:
    if not track.downloadable or not _track_id(track.id):
        raise CatalogError("Автор не разрешил скачивание этого трека.")
    url = httpx.URL(f"{API}file/", params={"client_id": key, "id": track.id, "action": "download"})
    try:
        async with asyncio.timeout(24):
            try:
                return await _download(client, url.copy_set_param("audioformat", "mp32"), limit)
            except FileTooLarge:
                return await _download(client, url.copy_set_param("audioformat", "mp31"), limit)
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError) as error:
        raise CatalogError("Источник не успел отдать MP3. Попробуй позже.") from error
