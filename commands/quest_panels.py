"""Read-only progress snapshots for communication goals and existing rituals."""
from __future__ import annotations

from datetime import datetime, timedelta
import time

import discord

from core import config, rituals, state, veyr_state


DAILY_GOALS = (
    ("Первые реплики", 5, "Начни разговор и дай знать, как твой день."),
    ("В потоке общения", 15, "Подхвати темы и поделись тем, что тебе интересно."),
    ("Свой человек", 30, "Проведи день в разговорах с сообществом."),
)

MILESTONES = (
    ("Первое слово", "Сообщение с текстом", "word_messages", 1),
    ("Освоился", "Сообщения на сервере", "messages", 100),
    ("Завсегдатай", "Сообщения на сервере", "messages", 300),
    ("Старожил", "Сообщения на сервере", "messages", 1000),
    ("Есть что рассказать", "Сообщения длиннее 100 символов", "long_messages", 10),
    ("История в деталях", "Символы в сообщениях", "characters", 10000),
)

RITUAL_NAMES = {"CONVERGENCE": "Сближение", "AFTERIMAGE": "Послесвечение"}
ACTION_NAMES = {"SURVEY": "Наблюдать", "ANCHOR": "Закрепить", "LISTEN": "Прислушаться", "RELEASE": "Отпустить"}
STATE_NAMES = {
    "DORMANT": "Покой", "ACTIVE": "Активность", "UNSTABLE": "Перемены",
    "ECHO": "Эхо", "VOID": "Пустота", "ANOMALY": "Аномалия", "RITUAL": "Общее событие",
}
NOTICES = {
    "Residual interval. The pattern is not available.": "Событие восстанавливается. Время следующего открытия указано ниже.",
    "No response from the current signal.": "Для открытия события пока не выполнены условия сигнала.",
    "Trace already registered.": "Твоё действие уже учтено — повторять его не нужно.",
    "Trace registered.": "Твоё действие учтено. Остальные участники могут присоединиться со своих панелей.",
    "RITUAL COMPLETE": "Событие завершено! Запись добавлена в хронику сервера.",
}


def safe(value, maximum=100):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))[:maximum]


def number(value):
    return f"{max(0, int(value)):,}".replace(",", " ")


def bar(current, target, width=12):
    filled = max(0, min(width, int(max(0, current) / max(1, target) * width)))
    return f"{'▰' * filled}{'▱' * (width - filled)}"


def today_snapshot(guild_id, owner_id):
    now = datetime.now(config.SUMMARY_TIMEZONE)
    day = now.date()
    reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    messages = max(0, int(state.DAILY_ACTIVITY.get((day, guild_id, owner_id), {}).get("messages", 0)))
    xp = state.xp_data.get(owner_id, {"xp": 0, "level": 1})
    return {"messages": messages, "day": day, "reset": reset,
            "xp": max(0, int(xp.get("xp", 0))), "level": max(1, int(xp.get("level", 1)))}


def today_blocks(guild_id, owner_id):
    snap = today_snapshot(guild_id, owner_id)
    current = snap["messages"]
    blocks = []
    for title, target, hint in DAILY_GOALS:
        done = current >= target
        blocks.append(
            f"### {'✓' if done else '◇'} {title}\n{hint}\n"
            f"`{bar(current, target)}` **{number(min(current, target))} / {target}**"
            f" · {'цель достигнута' if done else f'ещё {target - current}'}"
        )
    return snap, blocks


def milestone_blocks(guild_id, owner_id):
    stats = state.RANK_ACTIVITY.get((guild_id, owner_id), {})
    blocks = []
    for title, description, metric, target in MILESTONES:
        current = max(0, int(stats.get(metric, 0)))
        done = current >= target
        blocks.append(f"**{'✓' if done else '◇'} {title}** · {description}\n"
                      f"`{bar(current, target, 10)}` **{number(min(current, target))} / {number(target)}**")
    return blocks


def ritual_snapshot(guild_id, ritual, owner_id=None):
    now = time.time()
    rules = rituals.RITUAL_RULES[ritual]
    session = veyr_state.RITUAL_SESSIONS[guild_id].get(ritual)
    active = bool(session and session["expires_at"] > now)
    last = veyr_state.RITUAL_LAST_COMPLETE[guild_id].get(ritual, 0.0)
    ready_at = last + rituals.RITUAL_COOLDOWN_SECONDS if last else 0
    signal = veyr_state.SERVER_SIGNAL[guild_id]["value"]
    low, high = rules["signal"]
    eligible = veyr_state.SERVER_STATE[guild_id] in rules["states"] and low <= signal <= high
    available = active or (ready_at <= now and eligible)
    participants = len(session["participants"]) if active else 0
    actions = session["actions"] if active else set()
    mine = session.get("users", {}).get(owner_id, set()) if active else set()
    status = "Окно открыто" if active else (
        "Восстанавливается" if ready_at > now else "Можно открыть" if eligible else "Ждёт подходящего сигнала")
    text = (f"### {RITUAL_NAMES[ritual]}\n**{status}**\n"
            f"Участники **{participants} / {rules['participants']}** · "
            f"Действия **{len(actions)} / {len(rules['actions'])}**\n")
    if active:
        deadline = datetime.fromtimestamp(session["expires_at"], config.MOSCOW_TIMEZONE)
        text += f"Закроется <t:{int(session['expires_at'])}:R> · **{deadline:%H:%M} МСК**\n"
    elif ready_at > now:
        text += f"Следующее открытие <t:{int(ready_at)}:R>\n"
    else:
        text += f"После открытия: **{rules['window'] // 60} минут** на совместные действия.\n"
    if last:
        text += f"Последнее завершение сервером: <t:{int(last)}:R>\n"
    condition = " / ".join(STATE_NAMES[item] for item in sorted(rules["states"]))
    text += (f"\nУсловия · **{condition}** · сигнал **{low:g}–{high:g}**\n"
             f"Результат · запись **{rules['record']}** в хронике сервера.")
    if mine:
        text += "\nТвой вклад · " + ", ".join(ACTION_NAMES[item] for item in rules["actions"] if item in mine)
    return {"text": text, "available": available, "mine": mine, "actions": rules["actions"]}


def communication_overview(guild_id):
    today = datetime.now(config.SUMMARY_TIMEZONE).date()
    records = [stats for (day, guild, _), stats in state.DAILY_ACTIVITY.items() if day == today and guild == guild_id]
    return sum(max(0, int(item.get("messages", 0))) for item in records)
