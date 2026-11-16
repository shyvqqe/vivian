"""DeepSeek Chat Completions through the existing synchronous model interface."""
from __future__ import annotations

import atexit
import json
from threading import Lock
import time
from types import SimpleNamespace

import httpx

from core import config


FLASH_MODEL = "deepseek-flash"
PRO_MODEL = "deepseek-v4-pro"
_URL = "https://api.deepseek.com/chat/completions"
_RESPONSE_LIMIT = 512 * 1024
_CLIENT = None
_CLIENT_LOCK = Lock()


class DeepSeekUnavailable(RuntimeError):
    def __init__(self, *, status_code=None, reason=None):
        super().__init__(f"DeepSeek request failed; http={status_code}; reason={reason}")
        self.status_code = status_code
        self.reason = reason


def _get_client():
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.Client(follow_redirects=False)
            atexit.register(_CLIENT.close)
        return _CLIENT


def _request(payload, key, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeepSeekUnavailable(reason="timeout")
    with _get_client().stream(
        "POST", _URL, json=payload, headers={"Authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(remaining, connect=min(5.0, remaining), pool=min(5.0, remaining)),
    ) as response:
        if response.status_code != 200:
            raise DeepSeekUnavailable(status_code=response.status_code)
        body = bytearray()
        # The API may send blank lines while it waits in its inference queue.
        # Check the total budget on each chunk as well as HTTPX's idle timeout.
        for chunk in response.iter_bytes():
            if time.monotonic() >= deadline:
                raise DeepSeekUnavailable(reason="timeout")
            if len(body) + len(chunk) > _RESPONSE_LIMIT:
                raise DeepSeekUnavailable(reason="invalid_response")
            body.extend(chunk)
    return json.loads(body)


def create_completion(*, model, messages, **options):
    key = config.DEEPSEEK_API_KEY
    if not key:
        raise DeepSeekUnavailable(reason="missing_api_key")
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": options.get("max_completion_tokens", options.get("max_tokens", 700)),
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    if "temperature" in options:
        payload["temperature"] = options["temperature"]
    if options.get("json_mode"):
        payload["response_format"] = {"type": "json_object"}
    deadline = options.get("_deadline")
    if deadline is None:
        deadline = time.monotonic() + 20
    try:
        result = _request(payload, key, deadline)
    except httpx.TimeoutException:
        raise DeepSeekUnavailable(reason="timeout") from None
    except httpx.HTTPError:
        raise DeepSeekUnavailable(reason="network") from None
    except (ValueError, TypeError):
        raise DeepSeekUnavailable(reason="invalid_response") from None

    choices = result.get("choices") if isinstance(result, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        raise DeepSeekUnavailable(reason="invalid_response")
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=message.get("content")),
        finish_reason=choice.get("finish_reason"),
    )])
