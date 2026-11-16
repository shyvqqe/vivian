from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import re
import time
from typing import Literal
from urllib.parse import quote

import httpx


_API = "https://www.virustotal.com/api/v3"
_LOG = logging.getLogger(__name__)
_CACHE_SIZE = 1024
_POLL_DELAYS = (8, 15, 25, 40, 45)
_ERROR = "VirusTotal временно не отвечает."
_QUOTA = "Достигнут лимит запросов VirusTotal."
_AUTH = "VirusTotal отклонил ключ или запрос. Проверьте настройки бота."
_PENDING = "VirusTotal ещё анализирует файл."
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class FileReport:
    status: Literal["complete", "pending", "unknown", "unavailable"]
    digest: str | None = None
    analysis_id: str | None = None
    message: str = ""
    malicious: int = 0
    suspicious: int = 0
    examined: int = 0
    skipped: int = 0

    @property
    def url(self) -> str | None:
        if self.digest and self.status in ("complete", "pending"):
            return f"https://www.virustotal.com/gui/file/{self.digest}"
        return None


class _Unavailable(Exception):
    pass


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _report(digest: str, stats) -> FileReport:
    stats = _dict(stats)

    def count(key):
        value = stats.get(key)
        return value if type(value) is int and 0 <= value <= 10000 else 0

    malicious, suspicious = count("malicious"), count("suspicious")
    examined = malicious + suspicious + count("undetected") + count("harmless")
    skipped = sum(count(key) for key in ("type-unsupported", "failure", "timeout", "confirmed-timeout"))
    return FileReport("complete", digest, malicious=malicious, suspicious=suspicious,
                      examined=examined, skipped=skipped)


def _retry_after(value: str | None) -> float:
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                moment = parsedate_to_datetime(value)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                seconds = (moment - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = 0.0
        if math.isfinite(seconds) and seconds > 0:
            return min(seconds, 86400)
    return 65.0


class VirusTotal:
    def __init__(self, api_key: str, *, per_minute: int, per_day: int):
        self._api_key = api_key
        self._per_minute = per_minute
        self._per_day = per_day
        self._minute: deque[float] = deque()
        self._day = None
        self._daily_requests = 0
        self._blocked_until = 0.0
        self._blocked_message = _QUOTA
        self._cache: OrderedDict[str, tuple[float, FileReport]] = OrderedDict()
        self._active: dict[str, asyncio.Future[FileReport]] = {}

    async def _reserve(self) -> None:
        while True:
            now = time.monotonic()
            if now < self._blocked_until:
                raise _Unavailable(self._blocked_message)
            today = datetime.now(timezone.utc).date()
            if today != self._day:
                self._day = today
                self._daily_requests = 0
            if self._daily_requests >= self._per_day:
                raise _Unavailable(_QUOTA)
            while self._minute and now - self._minute[0] >= 60:
                self._minute.popleft()
            if len(self._minute) < self._per_minute:
                self._minute.append(now)
                self._daily_requests += 1
                return
            await asyncio.sleep(self._minute[0] + 60.1 - now)

    async def _request(self, client, method, path, *, allow_missing=False, **kwargs) -> dict | None:
        await self._reserve()
        try:
            async with asyncio.timeout(25):
                response = await client.request(
                    method, f"{_API}/{path}", follow_redirects=False,
                    headers={"x-apikey": self._api_key, "accept": "application/json"}, **kwargs,
                )
        except (httpx.HTTPError, TimeoutError) as error:
            raise _Unavailable(_ERROR) from error
        if response.status_code == 404 and allow_missing:
            return None
        if response.status_code == 400 and allow_missing and path.startswith("analyses/"):
            try:
                if _dict(_dict(response.json()).get("error")).get("code") == "NotAvailableYet":
                    return None
            except ValueError:
                pass
        if response.status_code in (401, 403, 429):
            limited = response.status_code == 429
            self._blocked_message = _QUOTA if limited else _AUTH
            delay = _retry_after(response.headers.get("Retry-After")) if limited else 300
            self._blocked_until = time.monotonic() + delay
            _LOG.warning("VirusTotal HTTP %s, paused for %.0fs", response.status_code, delay)
            raise _Unavailable(self._blocked_message)
        if not response.is_success:
            _LOG.warning("VirusTotal HTTP %s", response.status_code)
            raise _Unavailable(_ERROR)
        try:
            result = _dict(response.json())
        except ValueError as error:
            raise _Unavailable(_ERROR) from error
        if not _dict(result.get("data")):
            raise _Unavailable(_ERROR)
        return result

    def _cached(self, digest: str) -> FileReport | None:
        entry = self._cache.get(digest)
        if entry is None:
            return None
        expires, result = entry
        if expires <= time.monotonic():
            del self._cache[digest]
            return None
        self._cache.move_to_end(digest)
        return result

    def _remember(self, result: FileReport) -> FileReport:
        ttl = {"complete": 21600, "pending": 900, "unknown": 300}.get(result.status)
        if ttl and result.digest:
            self._cache[result.digest] = (time.monotonic() + ttl, result)
            self._cache.move_to_end(result.digest)
            if len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)
        return result

    async def inspect(self, client, digest: str, body: bytes, *, may_upload, suffix: str = "") -> FileReport:
        if not _SHA256.fullmatch(digest):
            raise ValueError("Expected a lowercase SHA-256 digest")
        filename = f"attachment{suffix}" if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else "attachment"
        cached = self._cached(digest)
        if cached is not None and (cached.status != "unknown" or not may_upload()):
            return cached
        result = await self._share(digest, lambda: self._lookup(client, digest, body, filename, may_upload))
        # A simultaneous lookup from a different channel may have been hash-only.
        if result.status == "unknown" and may_upload():
            return await self._share(digest, lambda: self._lookup(client, digest, body, filename, may_upload))
        return result

    async def _share(self, key, work):
        pending = self._active.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        pending = asyncio.get_running_loop().create_future()
        self._active[key] = pending
        try:
            result = await work()
            pending.set_result(result)
            return result
        finally:
            if not pending.done():
                pending.set_result(FileReport("unavailable", key, message=_ERROR))
            del self._active[key]

    async def _lookup(self, client, digest, body, filename, may_upload) -> FileReport:
        try:
            response = await self._request(client, "GET", f"files/{digest}", allow_missing=True)
            if response is not None:
                data = response["data"]
                if data.get("id") != digest:
                    raise _Unavailable(_ERROR)
                stats = _dict(data.get("attributes")).get("last_analysis_stats")
                if not stats:
                    return self._remember(FileReport("pending", digest, message=_PENDING))
                return self._remember(_report(digest, stats))
            if not may_upload():
                return self._remember(FileReport("unknown", digest, message="Для этого SHA-256 нет отчёта."))
            response = await self._request(client, "POST", "files", files={
                "file": (filename, body, "application/octet-stream"),
            })
            analysis_id = response["data"].get("id")
            if not isinstance(analysis_id, str) or not re.fullmatch(r"[A-Za-z0-9_+/=-]{1,256}", analysis_id):
                raise _Unavailable(_ERROR)
            return self._remember(FileReport("pending", digest, analysis_id, _PENDING))
        except _Unavailable as error:
            return FileReport("unavailable", digest, message=str(error))

    async def follow(self, client, report: FileReport) -> FileReport:
        if report.status != "pending" or not report.digest:
            return report
        cached = self._cached(report.digest)
        if cached is not None and cached.status == "complete":
            return cached
        return await self._share(report.digest, lambda: self._poll(client, report))

    async def _poll(self, client, report: FileReport) -> FileReport:
        try:
            async with asyncio.timeout(210):
                for delay in _POLL_DELAYS:
                    await asyncio.sleep(delay)
                    if report.analysis_id:
                        escaped = quote(report.analysis_id, safe="")
                        response = await self._request(client, "GET", f"analyses/{escaped}", allow_missing=True)
                        data = _dict((response or {}).get("data"))
                        attributes = _dict(data.get("attributes"))
                        if data.get("id") == report.analysis_id and attributes.get("status") == "completed":
                            meta = _dict(_dict(response.get("meta")).get("file_info"))
                            if meta.get("sha256", report.digest) != report.digest:
                                raise _Unavailable(_ERROR)
                            return self._remember(_report(report.digest, attributes.get("stats")))
                    else:
                        response = await self._request(client, "GET", f"files/{report.digest}", allow_missing=True)
                        data = _dict((response or {}).get("data"))
                        stats = _dict(data.get("attributes")).get("last_analysis_stats")
                        if data.get("id") == report.digest and stats:
                            return self._remember(_report(report.digest, stats))
        except _Unavailable as error:
            return replace(report, message=f"{_PENDING} {error}")
        except TimeoutError:
            pass
        return replace(report, message="Анализ продолжается. Результат появится в отчёте VirusTotal.")
