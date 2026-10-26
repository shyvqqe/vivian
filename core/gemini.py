"""Gemini's native REST API; existing dialogue and delivery stay provider neutral."""
from __future__ import annotations

import atexit
import math
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from types import SimpleNamespace

import httpx

from core import config
from core.gemini_errors import GeminiUnavailable, response_error, blocked_error

_CLIENT = None
_CLIENT_LOCK = Lock()
_IMAGE_DATA = re.compile(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)")


def _get_client():
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0, pool=5.0),
                                   follow_redirects=False)
            atexit.register(_CLIENT.close)
        return _CLIENT


def _retry_delay(response):
    """Honor server delays without logging its body or waiting indefinitely."""
    delays = [1.0 + random.uniform(0.0, 0.3)]
    value = response.headers.get("Retry-After")
    if value is not None:
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(value)
                seconds = (date - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return None
        if not math.isfinite(seconds) or seconds < 0:
            return None
        delays.append(seconds)
    try:
        body = response.json()
        details = (body.get("error") or {}).get("details") or []
        for detail in details:
            if detail.get("@type") == "type.googleapis.com/google.rpc.RetryInfo":
                raw = detail.get("retryDelay", "")
                if not isinstance(raw, str) or not re.fullmatch(r"\d+(?:\.\d+)?s", raw):
                    return None
                seconds = float(raw[:-1])
                if not math.isfinite(seconds):
                    return None
                delays.append(seconds)
    except (ValueError, TypeError, AttributeError):
        pass
    return max(delays)


def _post(model, key, payload, deadline):
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpx.ReadTimeout("Gemini request budget exhausted")
        response = _get_client().post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"}, json=payload,
            timeout=httpx.Timeout(remaining, connect=min(5.0, remaining), pool=min(5.0, remaining)),
        )
        if attempt or response.status_code != 503:
            return response
        delay = _retry_delay(response)
        if delay is None or delay + 2.0 >= deadline - time.monotonic():
            return response
        response.close()
        print(f"[AI Provider] Gemini HTTP 503; retry=1; delay_s={delay:.1f}", flush=True)
        # Gemini runs in the executor. This pause never blocks Discord's loop.
        time.sleep(delay)


def _parts(content):
    if isinstance(content, str):
        return [{"text": content}] if content.strip() else []
    result = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            raise GeminiUnavailable("Неподдерживаемый формат содержимого Gemini.")
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            result.append({"text": part["text"]})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            image = _IMAGE_DATA.fullmatch(url) if isinstance(url, str) else None
            if image is None:
                raise GeminiUnavailable("Gemini ожидает подготовленное изображение.")
            result.append({"inlineData": {"mimeType": image.group(1), "data": image.group(2)}})
    return result


def _payload(messages, model, options):
    contents, instructions = [], []
    for message in messages:
        role = message.get("role")
        parts = _parts(message.get("content"))
        if not parts:
            continue
        if role == "system":
            instructions.extend(part for part in parts if "text" in part)
        elif role in {"user", "assistant"}:
            contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
    if not contents:
        raise GeminiUnavailable("Для Gemini нет текста запроса.")
    # Gemini's budget includes reasoning: leave room for a visible short answer.
    limit = max(2048, min(int(options.get("max_completion_tokens", 512)) * 4, 8192))
    generation = {"maxOutputTokens": limit, "candidateCount": 1}
    if model.startswith("gemini-3"):
        generation["thinkingConfig"] = {
            "thinkingLevel": "MINIMAL" if "flash-lite" in model else "LOW",
            "includeThoughts": False,
        }
    payload = {"contents": contents, "generationConfig": generation}
    if instructions:
        payload["systemInstruction"] = {"parts": instructions}
    return payload


def create_completion(*, model, messages, **options):
    key = config.GEMINI_API_KEY
    if not key:
        raise GeminiUnavailable("Gemini не настроен: отсутствует GEMINI_API_KEY.")
    if not re.fullmatch(r"gemini-[a-zA-Z0-9.-]{1,180}", model):
        raise GeminiUnavailable("Некорректное имя модели Gemini.")
    payload = _payload(messages, model, options)
    try:
        deadline = options.get("_deadline", time.monotonic() + 20.0)
        response = _post(model, key, payload, deadline)
        if response.status_code != 200:
            raise response_error(response)
        result = response.json()
        if not isinstance(result, dict) or result.get("error"):
            raise GeminiUnavailable("Gemini не вернул готовый ответ.")
        feedback = result.get("promptFeedback") or {}
        if feedback.get("blockReason") not in (None, "", "BLOCK_REASON_UNSPECIFIED"):
            raise blocked_error(feedback.get("blockReason"))
        candidates = result.get("candidates") or []
        if not candidates:
            raise GeminiUnavailable("Gemini не вернул текст ответа.")
        candidate = candidates[0]
        finish_reason = candidate.get("finishReason")
        if finish_reason not in (None, "STOP", "MAX_TOKENS", "FINISH_REASON_UNSPECIFIED"):
            raise blocked_error(finish_reason)
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part["text"] for part in parts
                       if isinstance(part.get("text"), str) and not part.get("thought"))
        finish = "length" if candidate.get("finishReason") == "MAX_TOKENS" else "stop"
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=text), finish_reason=finish,
        )])
    except GeminiUnavailable:
        raise
    except httpx.TimeoutException:
        raise GeminiUnavailable(
            "Gemini request failed; reason=TIMEOUT",
            public_message="Gemini слишком долго отвечает. Попробуй ещё раз чуть позже.",
        ) from None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError):
        raise GeminiUnavailable("Не удалось получить ответ Gemini.") from None
