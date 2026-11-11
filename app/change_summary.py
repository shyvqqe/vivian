"""Readable summaries from explicit release notes or the actual changed paths."""
from __future__ import annotations

import re


_GENERIC = re.compile(r"^(add files via upload|update|updates|changes|initial commit)\.?$", re.I)
_AREAS = (
    ("Музыка: изменены поиск или управление плеером.", ("commands/music", "utils/music", "relay")),
    ("Интерфейс: обновлены карточки или компоненты команд.", ("visuals/", "utils/v2_", "utils/components_v2")),
    ("Общение: изменены модули диалогов или моделей ИИ.", ("vivian/", "core/gemini", "core/groq", "core/ai_", "core/openrouter")),
    ("Проект: обновлены обращения, предложения или план развития.", ("commands/feedback", "commands/project", "core/community")),
    ("Обновления: изменены уведомления, журнал или подтверждение версии.", ("app/github_", "app/deployment", "app/release_", "app/change_", "commands/changelog")),
    ("Хранение: изменена работа с базой данных.", ("core/database", "vivian/memory")),
)


def files_for(head, comparison=None):
    source = comparison if isinstance(comparison, dict) else head
    return [item for item in source.get("files", ()) if isinstance(item, dict) and item.get("filename")]


def summarize(head, comparison=None):
    """Never infer a feature or a successful deploy solely from filenames."""
    message = str((head.get("commit") or {}).get("message") or "").strip()
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    bullets = [re.sub(r"^[-*•]\s+", "", line)[:300]
               for line in lines[1:] if re.match(r"^[-*•]\s+", line)]
    if bullets:
        return bullets[:7]
    notes = []
    if lines and not _GENERIC.fullmatch(lines[0]):
        notes.append(lines[0][:250])
    paths = [str(item["filename"]).lower() for item in files_for(head, comparison)]
    for description, prefixes in _AREAS:
        if any(path.startswith(prefixes) for path in paths):
            notes.append(description)
    if not notes:
        notes.append("Обновлены файлы проекта. Подробности доступны в коммите.")
    return notes[:7]
