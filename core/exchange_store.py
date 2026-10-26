from __future__ import annotations

import json
import re
import time

from core import feature_storage


MAX_PROFILES = 1000
MAX_PROFILE_AGE = 90 * 24 * 60 * 60
_LINK = re.compile(r"(?:https?://|www\.|discord\.gg|@|[a-z0-9-]+\.(?:com|net|org|io|gg|ru|dev|app)\b)", re.I)
_TAG = re.compile(r"[\w +#.&()'-]+\Z", re.UNICODE)


def _ids(guild_id: int, channel_id: int, user_id: int) -> tuple[int, int, int]:
    values = (guild_id, channel_id, user_id)
    if any(type(value) is not int or not 0 < value < 2**63 for value in values):
        raise ValueError("A server, channel and member are required.")
    return values


def normalize_tags(value: str) -> list[str]:
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("Use 1 to 8 comma-separated skills, up to 32 characters each.")
    tags = []
    for item in value.split(","):
        tag = " ".join(item.strip().casefold().split())
        if not 1 <= len(tag) <= 32 or _LINK.search(tag) or not _TAG.fullmatch(tag):
            raise ValueError("Skills must contain 1 to 32 letters, numbers or simple punctuation, without links or mentions.")
        if tag not in tags:
            tags.append(tag)
    if not 1 <= len(tags) <= 8:
        raise ValueError("Use 1 to 8 different skills in each list.")
    return tags


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS exchange_profiles (
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        offers TEXT NOT NULL,
        wants TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (guild_id, channel_id, user_id)
    )""")


def _profile(row):
    if row is None:
        return None
    result = dict(row)
    result["offers"] = json.loads(result["offers"])
    result["wants"] = json.loads(result["wants"])
    result["active"] = bool(result["active"])
    return result


async def save_profile(guild_id: int, channel_id: int, user_id: int, offers: str, wants: str) -> dict:
    ids = _ids(guild_id, channel_id, user_id)
    offered, wanted = normalize_tags(offers), normalize_tags(wants)
    now = int(time.time())

    def operation(connection):
        _schema(connection)
        exists = connection.execute(
            "SELECT 1 FROM exchange_profiles WHERE guild_id=? AND channel_id=? AND user_id=?", ids
        ).fetchone()
        count = connection.execute(
            "SELECT COUNT(*) FROM exchange_profiles WHERE guild_id=? AND channel_id=?", ids[:2]
        ).fetchone()[0]
        if exists is None and count >= MAX_PROFILES:
            raise ValueError("This channel has reached its exchange profile limit.")
        connection.execute("""INSERT INTO exchange_profiles
            (guild_id, channel_id, user_id, offers, wants, active, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT (guild_id, channel_id, user_id) DO UPDATE SET
            offers=excluded.offers, wants=excluded.wants, active=1, updated_at=excluded.updated_at""",
            (*ids, json.dumps(offered), json.dumps(wanted), now))
        return _profile(connection.execute(
            "SELECT * FROM exchange_profiles WHERE guild_id=? AND channel_id=? AND user_id=?", ids
        ).fetchone())

    return await feature_storage.run(operation)


async def get_profile(guild_id: int, channel_id: int, user_id: int) -> dict | None:
    ids = _ids(guild_id, channel_id, user_id)

    def operation(connection):
        _schema(connection)
        return _profile(connection.execute(
            "SELECT * FROM exchange_profiles WHERE guild_id=? AND channel_id=? AND user_id=?", ids
        ).fetchone())

    return await feature_storage.run(operation)


async def remove_profile(guild_id: int, channel_id: int, user_id: int) -> bool:
    ids = _ids(guild_id, channel_id, user_id)

    def operation(connection):
        _schema(connection)
        return bool(connection.execute(
            "DELETE FROM exchange_profiles WHERE guild_id=? AND channel_id=? AND user_id=?", ids
        ).rowcount)

    return await feature_storage.run(operation)


async def pause_profile(guild_id: int, channel_id: int, user_id: int) -> bool:
    ids = _ids(guild_id, channel_id, user_id)

    def operation(connection):
        _schema(connection)
        return bool(connection.execute(
            "UPDATE exchange_profiles SET active=0 WHERE guild_id=? AND channel_id=? AND user_id=?", ids
        ).rowcount)

    return await feature_storage.run(operation)


async def find_matches(guild_id: int, channel_id: int, user_id: int) -> list[dict]:
    ids = _ids(guild_id, channel_id, user_id)
    cutoff = int(time.time()) - MAX_PROFILE_AGE

    def operation(connection):
        _schema(connection)
        own = _profile(connection.execute(
            "SELECT * FROM exchange_profiles WHERE guild_id=? AND channel_id=? AND user_id=?", ids
        ).fetchone())
        if own is None:
            raise ValueError("Create your profile with /exchange profile first.")
        if not own["active"] or own["updated_at"] < cutoff:
            raise ValueError("Update your profile with /exchange profile to enable matching.")
        rows = connection.execute("""SELECT * FROM exchange_profiles
            WHERE guild_id=? AND channel_id=? AND user_id<>? AND active=1 AND updated_at>=?""",
            (*ids, cutoff)).fetchall()
        results = []
        for row in rows:
            other = _profile(row)
            give = sorted(set(own["offers"]) & set(other["wants"]))
            learn = sorted(set(own["wants"]) & set(other["offers"]))
            if give and learn:
                results.append({
                    "user_id": other["user_id"], "give": give, "learn": learn,
                    "score": len(give) + len(learn),
                    "coverage": len(give) / len(own["offers"]) + len(learn) / len(own["wants"]),
                })
        results.sort(key=lambda item: (-item["score"], -item["coverage"], item["user_id"]))
        return results

    return await feature_storage.run(operation)
