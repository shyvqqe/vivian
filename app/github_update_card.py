"""Format a source update without claiming that a deployment has succeeded."""
from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import urlsplit

import discord

from core import config
from app.change_summary import summarize


REPOSITORY = "https://github.com/shyvqqe/vivian"
_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_EMAIL = re.compile(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+")
_COMPARE_PATH = re.compile(r"/shyvqqe/vivian/compare/[0-9a-fA-F]{7,40}\.\.\.[0-9a-fA-F]{7,40}\Z")
_FILE_LIMIT = 10


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _safe(value, limit):
    text = " ".join(str(value or "").split())
    text = text[:limit - 1] + "…" if len(text) > limit else text
    text = discord.utils.escape_markdown(text, ignore_links=False)
    return discord.utils.escape_mentions(text)


def _commit_url(head):
    sha = head.get("sha")
    if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
        raise ValueError("GitHub update requires a full 40-character commit SHA")
    return sha.lower(), f"{REPOSITORY}/commit/{sha.lower()}"


def _comparison_url(comparison):
    url = _mapping(comparison).get("html_url")
    if not isinstance(url, str) or any(char.isspace() for char in url):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (parsed.scheme == "https" and parsed.netloc == "github.com"
            and _COMPARE_PATH.fullmatch(parsed.path) is not None
            and not parsed.query and not parsed.fragment):
        return url
    return None


def _file_lines(comparison):
    records = _mapping(comparison).get("files")
    if not isinstance(records, list):
        return ["Список файлов пока недоступен."]
    files = [item for item in records if isinstance(item, dict) and item.get("filename")]
    if not files:
        return ["GitHub не передал изменённые файлы."]
    statuses = {
        "added": "Добавлен", "modified": "Изменён", "removed": "Удалён",
        "renamed": "Переименован", "copied": "Скопирован", "changed": "Изменён",
    }
    lines = [f"**Файлы · {len(files)}**"]
    for item in files[:_FILE_LIMIT]:
        status = statuses.get(item.get("status"), "Изменён")
        lines.append(f"• {status}: {_safe(item['filename'], 100)}")
    if len(files) > _FILE_LIMIT:
        lines.append(f"… ещё {len(files) - _FILE_LIMIT} файлов")
    return lines


def build_update(head: dict, comparison: dict | None) -> discord.Embed:
    """Return a bounded Embed; its first link is the recovery marker."""
    sha, commit_url = _commit_url(head)
    commit = _mapping(head.get("commit"))
    author = _mapping(head.get("author"))
    commit_author = _mapping(commit.get("author"))
    name = _EMAIL.sub("", str(commit_author.get("name") or "")).strip(" <>")
    login = str(author.get("login") or "")
    identity = _safe(name or login or "Не указан", 70)
    if login and name and login.casefold() != name.casefold():
        identity += f" ({_safe(login, 40)})"
    lines = [
        f"[Коммит {sha[:7]}]({commit_url})",
        "", "**Что изменилось**",
        *[f"• {_safe(note, 300)}" for note in summarize(head, comparison)],
        "", f"**Автор:** {identity}", "**Ветка:** main", "",
        *_file_lines(comparison if comparison is not None else head),
    ]
    compare_url = _comparison_url(comparison)
    if compare_url:
        lines.extend(("", f"[Посмотреть сравнение]({compare_url})"))
    embed = discord.Embed(
        title="Vivian // GitHub", description="\n".join(lines)[:3300],
        color=config.VIVIAN_ACCENT,
    )
    embed.set_footer(text="Изменения исходного кода · main")
    avatar = author.get("avatar_url")
    if isinstance(avatar, str) and avatar.startswith("https://") and not any(c.isspace() for c in avatar):
        try:
            parsed = urlsplit(avatar)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.netloc and len(avatar) <= 2048:
            embed.set_thumbnail(url=avatar)
    date = commit_author.get("date")
    if isinstance(date, str):
        try:
            embed.timestamp = datetime.fromisoformat(date.replace("Z", "+00:00"))
        except ValueError:
            pass
    return embed


def build_view(head: dict, comparison: dict | None) -> discord.ui.View:
    """Link-only buttons need neither callbacks nor persistent registration."""
    _, commit_url = _commit_url(head)
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Открыть коммит", url=commit_url))
    compare_url = _comparison_url(comparison)
    if compare_url:
        view.add_item(discord.ui.Button(label="Сравнить изменения", url=compare_url))
    return view
