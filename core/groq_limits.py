"""Share Groq model cooldowns across users without retrying rate-limited calls."""
from __future__ import annotations

import math
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock


_LOCK = Lock()
_COOLDOWNS = OrderedDict()
_MAX_MODELS = 128
_FALLBACK_SECONDS = 30.0
_BODY_DELAY = re.compile(
    r"please\s+try\s+again\s+in\s+"
    r"(?:(?P<hours>\d+(?:\.\d+)?)h\s*)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)m(?!s)\s*)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)(?P<unit>ms|s))",
    re.IGNORECASE,
)


class GroqLimited(RuntimeError):
    """A safe delivery error, containing no provider body or account identifiers."""

    def __init__(self, retry_seconds, source):
        self.retry_seconds = max(1, math.ceil(retry_seconds))
        self.source = source
        self.public_message = self.public_notice()
        super().__init__(
            f"Groq HTTP 429; pause_s={self.retry_seconds}; source={source}"
        )

    def public_notice(self, model_name=None):
        label = f"{model_name} (Groq)" if model_name else "Groq"
        if self.source == "local-fallback":
            wait = f"Сервис не указал срок; бот делает паузу ещё на {self.retry_seconds} с."
        else:
            wait = f"Сервис указал паузу: ещё {self.retry_seconds} с."
        return f"{label}: достигнут лимит. {wait} Другую модель можно выбрать в /ai."


def _positive_seconds(value):
    try:
        seconds = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _header_delay(error):
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        raw = headers.get("retry-after")
    if not isinstance(raw, (str, int, float)):
        return None
    seconds = _positive_seconds(raw)
    if seconds is not None:
        return seconds
    if not isinstance(raw, str):
        return None
    try:
        date = parsedate_to_datetime(raw)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return _positive_seconds((date - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError, IndexError):
        return None


def _body_delay(error):
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        response = getattr(error, "response", None)
        try:
            body = response.json() if response is not None else None
        except (ValueError, TypeError, AttributeError):
            return None
    if not isinstance(body, dict):
        return None
    details = body.get("error", body)
    message = details.get("message") if isinstance(details, dict) else None
    if not isinstance(message, str):
        return None
    delays = []
    for match in _BODY_DELAY.finditer(message[:65536]):
        try:
            seconds = (
                float(match["hours"] or 0) * 3600
                + float(match["minutes"] or 0) * 60
                + float(match["seconds"]) / (1000 if match["unit"].lower() == "ms" else 1)
            )
        except (ValueError, OverflowError):
            continue
        valid = _positive_seconds(seconds)
        if valid is not None:
            delays.append(valid)
    return max(delays) if delays else None


def check(model):
    """Raise before a Groq request when this model still has a recorded cooldown."""
    with _LOCK:
        now = time.monotonic()
        entry = _COOLDOWNS.get(model)
        if entry is None:
            return
        deadline, source = entry
        if deadline <= now:
            del _COOLDOWNS[model]
            return
        _COOLDOWNS.move_to_end(model)
        error = GroqLimited(deadline - now, source)
    raise error


def limited(error, model):
    """Record one HTTP 429 and return a safe exception; never sleep or resend it."""
    hints = [
        (seconds, source)
        for seconds, source in (
            (_header_delay(error), "retry-after"),
            (_body_delay(error), "error-message"),
        )
        if seconds is not None
    ]
    delay, source = max(hints) if hints else (_FALLBACK_SECONDS, "local-fallback")
    with _LOCK:
        now = time.monotonic()
        deadline = now + delay
        previous = _COOLDOWNS.get(model)
        if previous is not None and previous[0] > deadline:
            deadline, source = previous
        _COOLDOWNS[model] = (deadline, source)
        _COOLDOWNS.move_to_end(model)
        while len(_COOLDOWNS) > _MAX_MODELS:
            _COOLDOWNS.popitem(last=False)
        safe_error = GroqLimited(deadline - now, source)
    print(f"[AI Provider] {safe_error}", flush=True)
    return safe_error
