"""OpenRouter free-model requests with a reusable, thread-safe HTTP pool."""
from __future__ import annotations

import atexit
from threading import Lock
from types import SimpleNamespace

import httpx

from core import config


_CLIENT = None
_CLIENT_LOCK = Lock()


class OpenRouterUnavailable(RuntimeError):
    """A safe provider error containing no response body or credentials."""


def _get_client():
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.Client(
                timeout=httpx.Timeout(45.0, connect=10.0),
                follow_redirects=False,
            )
            atexit.register(_CLIENT.close)
        return _CLIENT


def create_completion(*, model, messages, **options):
    """Return one nonstreaming completion; never retry or substitute a model."""
    if not isinstance(model, str) or not model.endswith(":free"):
        raise OpenRouterUnavailable("Разрешены только бесплатные варианты OpenRouter.")
    key = config.OPENROUTER_API_KEY
    if not key:
        raise OpenRouterUnavailable("OpenRouter не настроен.")
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": options.get("max_completion_tokens", options.get("max_tokens", 700)),
        "stream": False,
        "reasoning": {"enabled": False, "exclude": True},
        "provider": {"max_price": {"prompt": 0, "completion": 0}},
    }
    if "temperature" in options:
        payload["temperature"] = options["temperature"]
    try:
        # The initialization lock is released before network I/O. HTTPX's sync
        # client shares its connection pool safely across executor threads.
        response = _get_client().post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}", "X-OpenRouter-Title": "Vivian"},
        )
        if response.status_code != 200:
            raise OpenRouterUnavailable(
                f"OpenRouter временно недоступен (HTTP {response.status_code})."
            )
        result = response.json()
    except OpenRouterUnavailable:
        raise
    except httpx.TimeoutException:
        raise OpenRouterUnavailable("OpenRouter слишком долго отвечает.") from None
    except (httpx.HTTPError, ValueError, TypeError):
        raise OpenRouterUnavailable("Не удалось получить ответ OpenRouter.") from None
    if not isinstance(result, dict):
        raise OpenRouterUnavailable("OpenRouter не вернул готовый ответ.")
    choices = result.get("choices")
    if (
        result.get("error")
        or not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], dict)
        or choices[0].get("error")
        or not isinstance(choices[0].get("message"), dict)
    ):
        raise OpenRouterUnavailable("OpenRouter не вернул готовый ответ.")
    choice = choices[0]
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=choice["message"].get("content")),
        finish_reason=choice.get("finish_reason"),
    )])
