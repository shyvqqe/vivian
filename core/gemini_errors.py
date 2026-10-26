"""Classify native Gemini errors without retaining provider text or credentials."""
from __future__ import annotations


_DEFAULT_NOTICE = "Gemini сейчас не смогла ответить. Попробуй позже или выбери другую модель в /ai."
_STATUSES = frozenset({
    "INVALID_ARGUMENT", "FAILED_PRECONDITION", "UNAUTHENTICATED", "PERMISSION_DENIED",
    "NOT_FOUND", "RESOURCE_EXHAUSTED", "INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED",
    "CANCELLED", "ABORTED", "UNKNOWN", "UNIMPLEMENTED", "OUT_OF_RANGE",
})
_KEY_REASONS = frozenset({"API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_NOT_FOUND"})
_REASONS = _KEY_REASONS | frozenset({
    "API_KEY_SERVICE_BLOCKED", "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED",
    "API_KEY_ANDROID_APP_BLOCKED", "API_KEY_IOS_APP_BLOCKED", "SERVICE_DISABLED",
    "SERVICE_NOT_ACTIVATED", "CONSUMER_INVALID", "BILLING_DISABLED", "BILLING_NOT_ACTIVE",
    "RATE_LIMIT_EXCEEDED", "QUOTA_EXCEEDED", "ACCESS_TOKEN_EXPIRED", "CREDENTIALS_MISSING",
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "SECURITY_POLICY_VIOLATED",
})
_BLOCK_REASONS = frozenset({
    "SAFETY", "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT", "IMAGE_SAFETY", "RECITATION",
    "LANGUAGE", "SPII", "IMAGE_PROHIBITED_CONTENT", "IMAGE_OTHER", "IMAGE_RECITATION",
    "PUP_LIMITED_DISABLED", "ESCALATION",
})


class GeminiUnavailable(RuntimeError):
    """Only a fixed diagnostic and a fixed, safe Discord notice are retained."""

    def __init__(self, message, *, public_message=None):
        super().__init__(message)
        self.public_message = public_message or _DEFAULT_NOTICE


def _metadata(response):
    """Read only native Google error enums; never return raw message or details."""
    try:
        payload = response.json()
    except (ValueError, TypeError, AttributeError):
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    status = error.get("status")
    status = status if isinstance(status, str) and status in _STATUSES else "UNKNOWN"
    reasons = set()
    details = error.get("details")
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        if detail.get("@type") != "type.googleapis.com/google.rpc.ErrorInfo":
            continue
        reason = detail.get("reason")
        if isinstance(reason, str) and reason in _REASONS:
            reasons.add(reason)
    # Used only to recognize fixed documented failures; it is not retained.
    message = error.get("message")
    message = " ".join(message.lower().split()) if isinstance(message, str) else ""
    return status, reasons, message


def response_error(response):
    """Return a safe error for a failed generateContent HTTP response; no retry."""
    code = getattr(response, "status_code", 0)
    code = code if type(code) is int and 100 <= code <= 599 else 0
    status, reasons, message = _metadata(response)
    key_bad = bool(reasons & _KEY_REASONS) or any(marker in message for marker in (
        "api key not valid", "api key is invalid", "api key expired", "api key has expired",
        "api key was reported as leaked", "api key has been reported as leaked",
    ))
    region = any(marker in message for marker in (
        "user location is not supported", "location is not supported for the api",
        "free tier is not available in your country", "free tier is not available in your region",
    ))
    billing = bool(reasons & {"BILLING_DISABLED", "BILLING_NOT_ACTIVE"}) or any(
        marker in message for marker in (
            "please enable billing", "billing is not enabled", "billing must be enabled",
            "billing account is disabled", "billing account is not active", "prepay credit",
        )
    )
    if key_bad or code == 401 or status == "UNAUTHENTICATED":
        kind = "key_invalid"
        notice = "Gemini отклонила ключ API. Проверь GEMINI_API_KEY в Render и статус ключа в Google AI Studio."
    elif code in {400, 403} and region:
        kind = "region_restricted"
        notice = "Gemini ограничила доступ для этого региона или бесплатного тарифа. Выбери другую модель в /ai."
    elif code == 402 or (code in {400, 403} and billing):
        kind = "billing_required"
        notice = "Gemini требует проверить доступ и оплату в проекте Google AI Studio. Можно выбрать другую модель в /ai."
    elif code == 403 or status == "PERMISSION_DENIED":
        kind = "permission_denied"
        notice = "Gemini запретила доступ. Проверь ограничения ключа и доступ к API в проекте Google AI Studio."
    elif code == 404 or status == "NOT_FOUND":
        kind = "model_not_found"
        notice = "Эта модель Gemini сейчас недоступна проекту. Проверь GEMINI_MODEL или выбери другую модель в /ai."
    elif code == 429 or status == "RESOURCE_EXHAUSTED":
        kind = "quota_limited"
        notice = "У Gemini сработал лимит запросов или закончилась квота. Попробуй позже либо выбери другую модель в /ai."
    elif code == 400 and status == "FAILED_PRECONDITION":
        kind = "prerequisite_missing"
        notice = "Gemini требует проверить настройки доступа проекта в Google AI Studio. Можно выбрать другую модель в /ai."
    elif code == 400:
        kind = "invalid_request"
        notice = "Gemini не приняла параметры запроса. Нужно проверить интеграцию; пока можно выбрать другую модель в /ai."
    elif code == 408 or code >= 500 or status in {"UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL"}:
        kind = "temporary_unavailable"
        notice = "Gemini сейчас недоступна или не успела ответить. Попробуй позже либо выбери другую модель в /ai."
    else:
        kind = "provider_error"
        notice = _DEFAULT_NOTICE
    reason = ",".join(sorted(reasons)) or "UNKNOWN"
    return GeminiUnavailable(
        f"Gemini failure; kind={kind}; http={code}; status={status}; reason={reason}",
        public_message=notice,
    )


def blocked_error(reason=None):
    """A filtered generation must not trigger an identical automatic retry."""
    reason = reason if isinstance(reason, str) and reason in _BLOCK_REASONS else "UNKNOWN"
    return GeminiUnavailable(
        f"Gemini failure; kind=content_blocked; reason={reason}",
        public_message="Gemini ограничила ответ на этот запрос. Попробуй переформулировать его.",
    )
