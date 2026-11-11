"""Conditional public GitHub polling with bounded caches and server backoff."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from email.utils import parsedate_to_datetime
import logging
import math
import re
import time
from urllib.parse import quote

import httpx


_LOG = logging.getLogger(__name__)


class GitHubUnavailable(RuntimeError):
    def __init__(self, message: str, *, retry_after: float):
        super().__init__(message)
        self.retry_after = max(0, math.ceil(retry_after))


class GitHubUpdatesClient:
    def __init__(self, repository: str, branch: str):
        repository = repository.strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("GitHub repository must be owner/repository")
        if not branch.strip():
            raise ValueError("GitHub branch must not be empty")
        self.repository, self.branch = repository, branch.strip()
        self._base = f"https://api.github.com/repos/{repository}"
        self._client = httpx.AsyncClient(timeout=10.0, follow_redirects=False, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Vivian-GitHub-Updates",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self._lock = asyncio.Lock()
        self._etag: str | None = None
        self._heads: list[dict] = []
        self._heads_loaded = False
        self._summaries: OrderedDict[tuple[str | None, str], dict] = OrderedDict()
        self._not_before = 0.0
        self._failures = 0

    @staticmethod
    def _seconds(value, *, epoch=False):
        try:
            number = float(value)
            if not math.isfinite(number):
                return 0.0
            return max(0.0, number - time.time() if epoch else number)
        except (TypeError, ValueError, OverflowError):
            if epoch or not isinstance(value, str):
                return 0.0
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return 0.0

    def _unavailable(self, reason: str, response=None, *, minimum=60.0):
        self._failures += 1
        delay = max(minimum, min(900.0, 60.0 * 2 ** min(self._failures - 1, 4)))
        if response is not None and response.status_code in (403, 429):
            delay = max(delay,
                        self._seconds(response.headers.get("Retry-After")),
                        self._seconds(response.headers.get("X-RateLimit-Reset"), epoch=True))
        self._not_before = max(self._not_before, time.monotonic() + delay)
        _LOG.warning("GitHub updates unavailable; reason=%s; retry_after=%ss",
                     reason, math.ceil(delay))
        return GitHubUnavailable(f"GitHub updates unavailable ({reason})",
                                 retry_after=self._not_before - time.monotonic())

    async def _get(self, path: str, *, params=None, headers=None):
        try:
            response = await self._client.get(self._base + path, params=params, headers=headers)
        except httpx.HTTPError as error:
            raise self._unavailable(type(error).__name__) from None
        if response.status_code not in (200, 304):
            raise self._unavailable(f"HTTP {response.status_code}", response,
                                    minimum=300.0 if response.status_code == 404 else 60.0)
        return response

    def _json(self, response, expected):
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            raise self._unavailable("invalid JSON") from None
        if not isinstance(data, expected):
            raise self._unavailable("unexpected response shape")
        return data

    def _success(self):
        self._failures = 0
        self._not_before = 0.0

    async def poll(self, cursor: str | None) -> dict | None:
        async with self._lock:
            remaining = self._not_before - time.monotonic()
            if remaining > 0:
                raise GitHubUnavailable("GitHub updates are backing off", retry_after=remaining)
            response = await self._get("/commits", params={"sha": self.branch, "per_page": 10},
                                       headers={"If-None-Match": self._etag} if self._etag else None)
            if response.status_code == 200:
                heads = self._json(response, list)
                if any(not isinstance(item, dict) or not isinstance(item.get("sha"), str)
                       or not re.fullmatch(r"[0-9a-fA-F]{40,64}", item["sha"]) for item in heads):
                    raise self._unavailable("invalid commit list")
                self._heads = heads
                self._heads_loaded = True
                self._etag = response.headers.get("ETag")
            elif not self._heads_loaded:
                raise self._unavailable("conditional response without cached commits")
            if not self._heads:
                self._success()
                return None
            head = self._heads[0]
            head_sha = head["sha"]
            if cursor == head_sha:
                self._success()
                return None
            key = (cursor, head_sha)
            if key in self._summaries:
                self._summaries.move_to_end(key)
                self._success()
                return self._summaries[key]
            if cursor:
                path = f"/compare/{quote(cursor, safe='')}...{head_sha}"
                comparison = self._json(await self._get(path), dict)
                if not isinstance(comparison.get("files"), list) or not isinstance(comparison.get("commits"), list):
                    raise self._unavailable("invalid comparison")
            else:
                head = self._json(await self._get(f"/commits/{head_sha}"), dict)
                if head.get("sha") != head_sha or not isinstance(head.get("files"), list):
                    raise self._unavailable("invalid commit details")
                comparison = None
            summary = {"head": head, "comparison": comparison}
            self._summaries[key] = summary
            while len(self._summaries) > 16:
                self._summaries.popitem(last=False)
            self._success()
            return summary

    async def aclose(self):
        await self._client.aclose()
