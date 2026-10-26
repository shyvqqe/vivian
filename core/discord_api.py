"""Circuit breaker for Discord 429 responses that escape discord.py's retry logic."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time

import discord

MAX_ROUTE_GUARDS = 128
_BODY_UNREAD = object()


class DiscordAPIPaused(discord.DiscordException):
    def __init__(self, retry_after: float):
        self.retry_after = max(0.0, retry_after)
        super().__init__(f"Discord REST paused; retry_after={self.retry_after:.1f}s")


def is_rate_limit_error(error: BaseException) -> bool:
    return (
        isinstance(error, (DiscordAPIPaused, discord.RateLimited))
        or isinstance(error, discord.HTTPException) and error.status == 429
    )


def _positive_seconds(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _header(headers, name: str):
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


async def _cached_json(error: BaseException):
    response = getattr(error, "response", None)
    if response is None or not hasattr(response, "json"):
        return None
    # HTTPClient has already consumed the response. Do not start another read.
    cached = getattr(response, "_body", _BODY_UNREAD)
    if cached is None or isinstance(cached, (bytes, str)) and len(cached) > 65536:
        return None
    content_type = _header(getattr(response, "headers", {}), "Content-Type")
    if content_type and "json" not in str(content_type).lower():
        return None
    try:
        body = await response.json()
        return body if isinstance(body, dict) else None
    except Exception:
        return None


async def _retry_after(error: BaseException, body=_BODY_UNREAD) -> float | None:
    retry = _positive_seconds(getattr(error, "retry_after", None))
    if retry is not None:
        return retry
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {})
    value = _header(headers, "Retry-After")
    retry = _positive_seconds(value)
    if retry is not None:
        return retry
    if isinstance(value, str):
        try:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            retry = _positive_seconds((moment - datetime.now(timezone.utc)).total_seconds())
            if retry is not None:
                return retry
        except (TypeError, ValueError, OverflowError):
            pass
    retry = _positive_seconds(_header(headers, "X-RateLimit-Reset-After"))
    if retry is not None:
        return retry
    if body is _BODY_UNREAD:
        body = await _cached_json(error)
    return _positive_seconds(body.get("retry_after")) if isinstance(body, dict) else None


def _rate_limit_scope(error: BaseException, body) -> str:
    headers = getattr(getattr(error, "response", None), "headers", {})
    scope = str(_header(headers, "X-RateLimit-Scope") or "").lower()
    global_header = str(_header(headers, "X-RateLimit-Global") or "").lower()
    if (scope == "global" or global_header in {"true", "1"}
            or isinstance(body, dict) and body.get("global") is True
            or "global rate limit" in str(getattr(error, "text", "")).lower()):
        return "global"
    if scope in {"user", "shared"}:
        return scope
    if (isinstance(body, dict) and body.get("global") is False
            or isinstance(error, discord.RateLimited)):
        return "route"
    return "unknown"


def _route_key(route) -> tuple[str, str]:
    return route.key, route.major_parameters


class _Circuit:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.blocked_until: float | None = None
        self.probing = False
        self.failures = 0

    def remaining(self) -> float:
        return max(0.0, (self.blocked_until or 0.0) - self.clock())

    def is_paused(self) -> bool:
        return self.probing or self.remaining() > 0

    def _reserve(self) -> bool:
        if self.blocked_until is None:
            return False
        if self.is_paused():
            raise DiscordAPIPaused(self.remaining())
        self.probing = True
        return True

    def _recovered(self, label="global"):
        self.blocked_until = None
        self.failures = 0
        print(f"[Discord REST] Response without 429; {label} cooldown cleared.", flush=True)


class DiscordRESTGuard(_Circuit):
    def __init__(self, clock=time.monotonic):
        super().__init__(clock)
        self._route_guards: OrderedDict[tuple[str, str], _Circuit] = OrderedDict()

    def is_route_paused(self, route) -> bool:
        circuit = self._route_guards.get(_route_key(route))
        return self.is_paused() or circuit is not None and circuit.is_paused()

    def route_remaining(self, route) -> float:
        circuit = self._route_guards.get(_route_key(route))
        return max(self.remaining(), circuit.remaining() if circuit else 0.0)

    def _route_circuit(self, route) -> _Circuit | None:
        key = _route_key(route)
        circuit = self._route_guards.get(key)
        if circuit is not None:
            return circuit
        if len(self._route_guards) >= MAX_ROUTE_GUARDS:
            for stale_key, stale in list(self._route_guards.items()):
                if not stale.is_paused():
                    self._route_guards.pop(stale_key, None)
        if len(self._route_guards) >= MAX_ROUTE_GUARDS:
            return None  # Never evict an actively blocked or probing route.
        circuit = _Circuit(self.clock)
        self._route_guards[key] = circuit
        return circuit

    async def request(self, original, route, *args, **kwargs):
        # Interaction ACKs/webhooks have their own limits and a separate adapter.
        # Never queue their initial response behind a bot-token REST cooldown.
        if route.path.startswith(("/interactions/", "/webhooks/")):
            return await original(route, *args, **kwargs)
        probes = []
        try:
            if self._reserve():
                probes.append((self, self.failures, "global"))
            local = self._route_guards.get(_route_key(route))
            if local is not None and local._reserve():
                probes.append((local, local.failures, "route"))
            result = await original(route, *args, **kwargs)
        except BaseException as error:
            if is_rate_limit_error(error) and not isinstance(error, DiscordAPIPaused):
                body = await _cached_json(error)
                scope = _rate_limit_scope(error, body)
                target = self if scope in {"global", "unknown"} else self._route_circuit(route)
                circuit_scope = "global" if target is self or target is None else "route"
                if target is None:
                    target = self  # The bounded route map is full of active circuits.
                target.failures += 1
                retry = await _retry_after(error, body)
                source = "server" if retry is not None else "local-backoff"
                if retry is None:
                    retry = min(900.0, 60.0 * 2 ** min(target.failures - 1, 4))
                target.blocked_until = max(target.blocked_until or 0.0, self.clock() + retry)
                print(
                    f"[Discord REST] 429; pause={target.remaining():.1f}s; source={source}; "
                    f"scope={scope}; circuit={circuit_scope}; route={route.method} {route.path}", flush=True,
                )
            elif not isinstance(error, DiscordAPIPaused) and isinstance(error, discord.HTTPException):
                # A non-429 response means REST is reachable again (e.g. missing permission).
                self._finish_probes(probes)
            raise
        else:
            self._finish_probes(probes)
            return result
        finally:
            for circuit, _, _ in probes:
                circuit.probing = False

    def _finish_probes(self, probes):
        for circuit, failures_before, label in probes:
            if circuit.failures == failures_before:
                circuit._recovered(label)
                if circuit is not self:
                    for key, value in list(self._route_guards.items()):
                        if value is circuit:
                            self._route_guards.pop(key, None)


_guard = DiscordRESTGuard()


def is_paused() -> bool:
    return _guard.is_paused()


def remaining() -> float:
    return _guard.remaining()


def is_route_paused(route) -> bool:
    return _guard.is_route_paused(route)


def _message_route(channel_id):
    return discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel_id)


def is_message_paused(channel_id: int) -> bool:
    return is_route_paused(_message_route(channel_id))


def message_remaining(channel_id: int) -> float:
    return _guard.route_remaining(_message_route(channel_id))


def install(http):
    if getattr(http, "_vivian_rate_limit_guard", False):
        return
    from core import discord_startup

    original = http.request

    async def guarded_request(route, *args, **kwargs):
        if route.method == "GET" and route.path in discord_startup.LOGIN_PATHS:
            return await discord_startup.login_request(
                lambda: _guard.request(original, route, *args, **kwargs),
                route,
                lambda: _guard.route_remaining(route),
            )
        return await _guard.request(original, route, *args, **kwargs)

    http.request = guarded_request
    http._vivian_rate_limit_guard = True
