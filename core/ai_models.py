"""Personal text-model choices; optional OpenRouter requests stay on free IDs."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from core import config, groq, gemini, groq_limits, openrouter


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    provider: str
    description: str


GEMINI_FAST_MODEL = "gemini-3.5-flash-lite"
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
    Model("openai/gpt-oss-20b", "GPT-OSS 20B", "Groq", "Быстрый повседневный разговор"),
    Model("openai/gpt-oss-120b", "GPT-OSS 120B", "Groq", "Подробный разбор и сложные вопросы"),
    Model("qwen/qwen3.8-27b", "Qwen 3.8 27B", "Groq", "Диалог и работа с контекстом"),
    Model("google/gemma-4-31b-it:free", "Gemma 4 31B", "OpenRouter", "Бесплатная версия"),
    Model("nvidia/nemotron-3.5-super-vl:free", "Nemotron 3.5 Super", "OpenRouter", "Бесплатная версия"),
)
MODELS = {model.id: model for model in CATALOG}
_SELECTIONS = OrderedDict()
_LOCAL_CHANGES = OrderedDict()
_MAX_SELECTIONS = 10000


class ModelUnavailable(RuntimeError):
    """A configured provider/model cannot currently complete the request."""

    def __init__(self, message, *, public_message=None):
        super().__init__(message)
        self.public_message = public_message


def display_name(model_id):
    model = MODELS.get(model_id)
    return model.name if model else model_id


def is_gemini_model(model_id):
    model = MODELS.get(model_id)
    return model is not None and model.provider == "Gemini"


def has_provider(model_id):
    model = MODELS.get(model_id)
    if model and model.provider == "Gemini":
        return bool(config.GEMINI_API_KEY)
    if model and model.provider == "OpenRouter":
        return bool(getattr(config, "OPENROUTER_API_KEY", None))
    return groq.groq_client is not None


def get_selection(guild_id, user_id):
    return _SELECTIONS.get((int(guild_id or 0), int(user_id)))


def get_default_model():
    return config.GEMINI_MODEL if config.GEMINI_API_KEY else groq.GROQ_MODEL


def get_text_model(guild_id, user_id):
    return get_selection(guild_id, user_id) or get_default_model()


def get_vision_model(guild_id, user_id):
    selected = get_text_model(guild_id, user_id)
    model = MODELS.get(selected)
    if model and model.provider == "Gemini":
        return selected
    if groq.groq_client is None and config.GEMINI_API_KEY:
        return config.GEMINI_MODEL
    return groq.GROQ_VISION_MODEL


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


def create_completion(*, model, messages, **options):
    """Synchronous call compatible with the existing executor-based AI pipeline."""
    selected = MODELS.get(model)
    if selected is None and model not in {groq.GROQ_MODEL, groq.GROQ_VISION_MODEL}:
        raise ModelUnavailable("Модель не входит в каталог Vivian.")
    if selected and selected.provider == "Gemini":
        try:
            return gemini.create_completion(model=model, messages=messages, **options)
        except gemini.GeminiUnavailable as error:
            raise ModelUnavailable(str(error), public_message=error.public_message) from None
    if selected is None or selected.provider == "Groq":
        if groq.groq_client is None:
            raise ModelUnavailable("Groq не настроен.")
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
            ) from None
        except Exception as error:
            if getattr(error, "status_code", None) != 429:
                raise
            limited = groq_limits.limited(error, model)
            raise ModelUnavailable(
                str(limited), public_message=limited.public_notice(display_name(model)),
            ) from None
    try:
        return openrouter.create_completion(model=model, messages=messages, **options)
    except openrouter.OpenRouterUnavailable as error:
        raise ModelUnavailable(str(error)) from None
