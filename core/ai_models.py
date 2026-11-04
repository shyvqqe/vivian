"""Personal text-model choices; optional OpenRouter requests stay on free IDs."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import time

from groq import APIConnectionError, APIStatusError, APITimeoutError
import httpx

from core import config, groq, gemini, groq_limits, openrouter


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    provider: str
    description: str


GEMINI_FAST_MODEL = "gemini-3.5-flash-lite"
GROQ_FALLBACK_TEXT = "openai/gpt-oss-20b"
GROQ_FALLBACK_VISION = "qwen/qwen3.8-27b"
OPENROUTER_DEFAULT = "google/gemma-4-31b-it:free"
OPENROUTER_ALTERNATE = "nvidia/nemotron-3.5-super-vl:free"
# Keep both stable Gemini IDs even when an environment override is configured:
# saved preferences must not disappear, and Discord select values must be unique.
_GEMINI_CATALOG = (
    Model(GEMINI_FAST_MODEL, "Gemini Flash-Lite", "Gemini", "Быстрые короткие ответы и изображения"),
    Model("gemini-3.8-flash", "Gemini Flash", "Gemini", "Разговор, подробный разбор и изображения"),
    Model(config.GEMINI_MODEL, "Gemini из настроек", "Gemini", "Модель, заданная в GEMINI_MODEL"),
)
# Checked against official model catalogs, 16 Oct 2026.
_GEMINI_UNIQUE = {}
for _model in _GEMINI_CATALOG:
    _GEMINI_UNIQUE.setdefault(_model.id, _model)
CATALOG = tuple(_GEMINI_UNIQUE.values()) + (
    Model(GROQ_FALLBACK_TEXT, "GPT-OSS 20B", "Groq", "Быстрый повседневный разговор"),
    Model("openai/gpt-oss-120b", "GPT-OSS 120B", "Groq", "Подробный разбор и сложные вопросы"),
    Model(GROQ_FALLBACK_VISION, "Qwen 3.8 27B", "Groq", "Диалог и работа с контекстом"),
    Model(OPENROUTER_DEFAULT, "Gemma 4 31B", "OpenRouter", "Бесплатная версия"),
    Model(OPENROUTER_ALTERNATE, "Nemotron 3.5 Super", "OpenRouter", "Бесплатная версия"),
)
MODELS = {model.id: model for model in CATALOG}
_SELECTIONS = OrderedDict()
_LOCAL_CHANGES = OrderedDict()
_MAX_SELECTIONS = 10000


class ModelUnavailable(RuntimeError):
    """A configured provider/model cannot currently complete the request."""

    def __init__(self, message, *, public_message=None, retryable=False):
        super().__init__(message)
        self.public_message = public_message
        self.retryable = retryable


def display_name(model_id):
    model = MODELS.get(model_id)
    return model.name if model else model_id


def _provider(model_id):
    model = MODELS.get(model_id)
    if model is not None:
        return model.provider
    if model_id in {groq.GROQ_MODEL, groq.GROQ_VISION_MODEL}:
        return "Groq"
    return None


def is_gemini_model(model_id):
    return _provider(model_id) == "Gemini"


def has_provider(model_id):
    provider = _provider(model_id)
    if provider == "Gemini":
        return bool(config.GEMINI_API_KEY)
    if provider == "OpenRouter":
        return bool(config.OPENROUTER_API_KEY)
    return provider == "Groq" and groq.groq_client is not None


def get_selection(guild_id, user_id):
    return _SELECTIONS.get((int(guild_id or 0), int(user_id)))


def get_default_model():
    return next((model for model in _defaults() if has_provider(model)), groq.GROQ_MODEL)


def _defaults(*, vision=False):
    groq_model = groq.GROQ_VISION_MODEL if vision else groq.GROQ_MODEL
    return config.GEMINI_MODEL, groq_model, OPENROUTER_DEFAULT


def get_text_model(guild_id, user_id):
    return get_selection(guild_id, user_id) or get_default_model()


def get_vision_model(guild_id, user_id):
    selected = get_text_model(guild_id, user_id)
    if _provider(selected) in {"Gemini", "OpenRouter"} or selected == GROQ_FALLBACK_VISION:
        return selected
    return groq.GROQ_VISION_MODEL


def fallback_models(guild_id, user_id, model, *, vision=False):
    """Offer alternatives for the automatic route, keeping explicit choices private."""
    if get_selection(guild_id, user_id) is not None:
        return ()
    defaults = _defaults(vision=vision)
    builtins = (GEMINI_FAST_MODEL, GROQ_FALLBACK_VISION if vision else GROQ_FALLBACK_TEXT, OPENROUTER_ALTERNATE)
    candidates = dict.fromkeys((*defaults, *builtins))
    return tuple(candidate for candidate in candidates if candidate != model and has_provider(candidate))[:2]


def is_configured(guild_id, user_id):
    return has_provider(get_text_model(guild_id, user_id))


def was_changed(guild_id, user_id):
    return (int(guild_id or 0), int(user_id)) in _LOCAL_CHANGES


def _mark_changed(guild_id, user_id):
    key = (int(guild_id or 0), int(user_id))
    _LOCAL_CHANGES[key] = True
    _LOCAL_CHANGES.move_to_end(key)
    while len(_LOCAL_CHANGES) > _MAX_SELECTIONS:
        _LOCAL_CHANGES.popitem(last=False)


def restore_selection(guild_id, user_id, model_id):
    if model_id not in MODELS:
        return False
    key = (int(guild_id or 0), int(user_id))
    _SELECTIONS[key] = model_id
    _SELECTIONS.move_to_end(key)
    while len(_SELECTIONS) > _MAX_SELECTIONS:
        _SELECTIONS.popitem(last=False)
    return True


def set_selection(guild_id, user_id, model_id):
    if model_id not in MODELS:
        raise ValueError("Неизвестная модель.")
    if not has_provider(model_id):
        raise ValueError("Для этой модели сначала нужен ключ её провайдера в настройках Render.")
    restore_selection(guild_id, user_id, model_id)
    _mark_changed(guild_id, user_id)


def reset_selection(guild_id, user_id):
    _SELECTIONS.pop((int(guild_id or 0), int(user_id)), None)
    _mark_changed(guild_id, user_id)


def _provider_failure(provider, status=None, *, reason=None):
    if status == 403 and provider == "OpenRouter":
        # OpenRouter also uses 403 for moderation and administrator guardrails.
        notice = "OpenRouter отклонил запрос. Проверь доступ к модели или переформулируй сообщение."
        retryable = False
    elif status in {401, 403}:
        notice = f"{provider} отклонил доступ. Проверь ключ и доступ к модели в настройках провайдера."
        retryable = True
    elif status == 402:
        notice = f"{provider} требует проверить квоту и оплату. Можно выбрать другую модель в /ai."
        retryable = True
    elif status == 404 or reason == "model_decommissioned":
        notice = f"Эта модель {provider} недоступна. Проверь настройку модели или выбери другую в /ai."
        retryable = True
    elif status == 429:
        notice = f"У {provider} сработал лимит запросов. Попробуй позже или выбери другую модель в /ai."
        retryable = True
    elif status == 408 or reason == "timeout":
        notice = f"{provider} слишком долго отвечает. Попробуй ещё раз чуть позже."
        retryable = True
    elif status == 498 or (status is not None and status >= 500) or reason == "network":
        notice = f"{provider} временно недоступен. Попробуй позже или выбери другую модель в /ai."
        retryable = True
    else:
        notice = f"{provider} не смог обработать запрос. Можно попробовать другую модель в /ai."
        retryable = False
    return ModelUnavailable(
        f"{provider} request failed; http={status}; reason={reason}",
        public_message=notice,
        retryable=retryable,
    )


def create_completion(*, model, messages, **options):
    """Synchronous call compatible with the existing executor-based AI pipeline."""
    provider = _provider(model)
    if provider is None:
        raise ModelUnavailable("Модель не входит в каталог Vivian.")
    if not has_provider(model):
        raise ModelUnavailable(
            f"{provider} is not configured.",
            public_message=f"Для {provider} нужен ключ API. Можно выбрать подключённую модель в /ai.",
            retryable=True,
        )
    if provider == "Gemini":
        try:
            return gemini.create_completion(model=model, messages=messages, **options)
        except gemini.GeminiUnavailable as error:
            raise ModelUnavailable(
                str(error), public_message=error.public_message, retryable=error.retryable,
            ) from None
    if provider == "Groq":
        deadline = options.pop("_deadline", None)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _provider_failure(provider, reason="timeout")
            options["timeout"] = httpx.Timeout(remaining, connect=min(5.0, remaining), pool=min(5.0, remaining))
        if model.startswith("qwen/qwen3.8-"):
            options.setdefault("reasoning_effort", "none")
            options.setdefault("reasoning_format", "hidden")
        elif model.startswith("openai/gpt-oss-"):
            options.setdefault("reasoning_effort", "low")
        try:
            groq_limits.check(model)
            return groq.groq_client.chat.completions.create(model=model, messages=messages, **options)
        except groq_limits.GroqLimited as error:
            raise ModelUnavailable(
                str(error), public_message=error.public_notice(display_name(model)),
                retryable=True,
            ) from None
        except APITimeoutError:
            raise _provider_failure(provider, reason="timeout") from None
        except APIConnectionError:
            raise _provider_failure(provider, reason="network") from None
        except APIStatusError as error:
            if error.status_code == 429:
                limited = groq_limits.limited(error, model)
                raise ModelUnavailable(
                    str(limited), public_message=limited.public_notice(display_name(model)),
                    retryable=True,
                ) from None
            details = error.body.get("error", error.body) if isinstance(error.body, dict) else None
            reason = (
                "model_decommissioned"
                if isinstance(details, dict) and details.get("code") == "model_decommissioned" else None
            )
            raise _provider_failure(provider, error.status_code, reason=reason) from None
    try:
        return openrouter.create_completion(model=model, messages=messages, **options)
    except openrouter.OpenRouterUnavailable as error:
        raise _provider_failure(provider, error.status_code, reason=error.reason) from None
