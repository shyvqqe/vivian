"""Metadata-only conversation signals; no message text or inferred personality."""
from __future__ import annotations
from collections import defaultdict, deque
from datetime import datetime, timedelta
import asyncio
from core import config

QUIET_SECONDS = 15 * 60
SPARK_WINDOW_SECONDS = 5 * 60
NEWCOMER_SECONDS = 7 * 24 * 60 * 60
SEEN_LIMIT = 20000
PENDING_LIMIT = 5000
RETENTION_DAYS = 90
WINDOWS = {}
TOTALS = defaultdict(lambda: {"sparks": 0, "newcomer_replies": 0})
DAILY = defaultdict(lambda: {"sparks": 0, "newcomer_replies": 0})
LOCKS = defaultdict(asyncio.Lock)
SEEN = set()
SEEN_ORDER = deque()
PENDING = {}
DIRTY_WINDOWS = set()


def window_for(guild_id: int) -> dict:
    return WINDOWS.setdefault(guild_id, {"last_id": 0, "last_at": None, "candidate": None})


def day_key(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, config.MOSCOW_TIMEZONE).date().isoformat()


def mark_seen(key) -> bool:
    if key in SEEN:
        return False
    SEEN.add(key)
    SEEN_ORDER.append(key)
    while len(SEEN_ORDER) > SEEN_LIMIT:
        SEEN.discard(SEEN_ORDER.popleft())
    return True


def apply_event(event: dict) -> None:
    metric = "sparks" if event["kind"] == "spark" else "newcomer_replies"
    TOTALS[(event["guild_id"], event["actor_id"])][metric] += 1
    DAILY[(day_key(event["at"]), event["guild_id"], event["actor_id"])][metric] += 1


def add_event(guild_id: int, kind: str, actor_id: int, target_id: int, at: float) -> bool:
    key = (guild_id, kind, actor_id, target_id)
    if len(PENDING) >= PENDING_LIMIT and key not in PENDING:
        # Keep already accepted writes; a prolonged outage cannot grow RAM forever.
        return False
    if key in PENDING or not mark_seen(key):
        return False
    event = {"guild_id": guild_id, "kind": kind, "actor_id": actor_id, "target_id": target_id, "at": at}
    PENDING[key] = event
    apply_event(event)
    return True


def prune_daily() -> None:
    cutoff = (datetime.now(config.MOSCOW_TIMEZONE).date() - timedelta(days=RETENTION_DAYS)).isoformat()
    for key in list(DAILY):
        if key[0] < cutoff:
            DAILY.pop(key, None)


def observe_activity(guild_id: int, message_id: int, author_id: int, timestamp: float) -> None:
    window = window_for(guild_id)
    # Late moderation completions cannot move time backwards or reopen an old window.
    if message_id <= window["last_id"] or (window["last_at"] is not None and timestamp < window["last_at"]):
        return
    candidate = window["candidate"]
    if candidate and timestamp - candidate["at"] > SPARK_WINDOW_SECONDS:
        candidate = window["candidate"] = None
    if candidate and author_id != candidate["author_id"]:
        candidate["responders"].add(author_id)
        if len(candidate["responders"]) >= 2:
            add_event(guild_id, "spark", candidate["author_id"], candidate["message_id"], timestamp)
            window["candidate"] = None
    previous_at = window["last_at"]
    if previous_at is not None and timestamp - previous_at >= QUIET_SECONDS:
        window["candidate"] = {"message_id": message_id, "author_id": author_id, "at": timestamp, "responders": set()}
    window["last_id"], window["last_at"] = message_id, timestamp
    DIRTY_WINDOWS.add(guild_id)


def member_totals(guild_id: int, user_id: int) -> dict:
    return dict(TOTALS.get((guild_id, user_id), {"sparks": 0, "newcomer_replies": 0}))


def today_totals(guild_id: int, day: str) -> dict:
    rows = [(uid, dict(counts)) for (key_day, gid, uid), counts in DAILY.items() if gid == guild_id and key_day == day]
    leaders = sorted(((uid, counts["sparks"]) for uid, counts in rows if counts["sparks"]), key=lambda item: (-item[1], item[0]))
    return {"sparks": sum(counts["sparks"] for _, counts in rows),
            "newcomer_replies": sum(counts["newcomer_replies"] for _, counts in rows), "leaders": leaders[:3]}
