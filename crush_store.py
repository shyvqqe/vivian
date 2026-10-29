from __future__ import annotations

import time

from core import feature_storage

WEEKLY_LIMIT = 3
WEEK_SECONDS = 7 * 24 * 60 * 60


def _ids(*values):
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("A server and member are required.")
    return values


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS crushes (
        guild_id INTEGER NOT NULL,
        from_id INTEGER NOT NULL,
        to_id INTEGER NOT NULL,
        created_at REAL NOT NULL,
        notified INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, from_id, to_id)
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS crushes_weekly ON crushes(guild_id, from_id, created_at)"
    )


async def set_crush(guild_id: int, from_id: int, to_id: int, *, now: float | None = None) -> dict:
    """Record a secret crush. Returns {'mutual': bool, 'already_set': bool}.

    Nothing about a one-sided crush is ever exposed by this function's return value beyond the
    caller's own request; the other member is never told unless the pick is mutual.
    """
    guild_id, from_id, to_id = _ids(guild_id, from_id, to_id)
    if from_id == to_id:
        raise ValueError("Нельзя выбрать себя.")
    now = time.time() if now is None else now

    def work(connection):
        _schema(connection)
        existing = connection.execute(
            "SELECT 1 FROM crushes WHERE guild_id = ? AND from_id = ? AND to_id = ?",
            (guild_id, from_id, to_id),
        ).fetchone()
        if existing is not None:
            return {"mutual": False, "already_set": True}

        week_count = connection.execute(
            "SELECT COUNT(*) AS n FROM crushes WHERE guild_id = ? AND from_id = ? AND created_at >= ?",
            (guild_id, from_id, now - WEEK_SECONDS),
        ).fetchone()["n"]
        if week_count >= WEEKLY_LIMIT:
            raise ValueError(f"Лимит {WEEKLY_LIMIT} краша в неделю исчерпан. Попробуй позже.")

        connection.execute(
            "INSERT INTO crushes (guild_id, from_id, to_id, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, from_id, to_id, now),
        )
        reverse = connection.execute(
            "SELECT 1 FROM crushes WHERE guild_id = ? AND from_id = ? AND to_id = ?",
            (guild_id, to_id, from_id),
        ).fetchone()
        if reverse is None:
            return {"mutual": False, "already_set": False}

        connection.execute(
            "UPDATE crushes SET notified = 1 WHERE guild_id = ? AND "
            "((from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?))",
            (guild_id, from_id, to_id, to_id, from_id),
        )
        return {"mutual": True, "already_set": False}

    return await feature_storage.run(work)


async def remaining_this_week(guild_id: int, from_id: int, *, now: float | None = None) -> int:
    guild_id, from_id = _ids(guild_id, from_id)
    now = time.time() if now is None else now

    def work(connection):
        _schema(connection)
        used = connection.execute(
            "SELECT COUNT(*) AS n FROM crushes WHERE guild_id = ? AND from_id = ? AND created_at >= ?",
            (guild_id, from_id, now - WEEK_SECONDS),
        ).fetchone()["n"]
        return max(0, WEEKLY_LIMIT - used)

    return await feature_storage.run(work)
