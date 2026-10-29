from __future__ import annotations

import time

from core import feature_storage

STORY_DURATION_SECONDS = 24 * 60 * 60


def _ids(*values):
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("A server and member are required.")
    return values


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS story_settings (
        guild_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS stories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        message_id INTEGER,
        text TEXT,
        image_url TEXT,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS stories_expiry ON stories(expires_at, deleted)"
    )
    connection.execute("""CREATE TABLE IF NOT EXISTS story_views (
        story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        viewed_at REAL NOT NULL,
        PRIMARY KEY (story_id, user_id)
    )""")


async def set_channel(guild_id: int, channel_id: int) -> None:
    guild_id, channel_id = _ids(guild_id, channel_id)

    def work(connection):
        _schema(connection)
        connection.execute(
            "INSERT INTO story_settings (guild_id, channel_id) VALUES (?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE SET channel_id = excluded.channel_id",
            (guild_id, channel_id),
        )

    await feature_storage.run(work)


async def get_channel(guild_id: int) -> int | None:
    guild_id, = _ids(guild_id)

    def work(connection):
        _schema(connection)
        row = connection.execute(
            "SELECT channel_id FROM story_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return row["channel_id"] if row else None

    return await feature_storage.run(work)


async def create_story(guild_id: int, channel_id: int, author_id: int, text: str | None,
                       image_url: str | None, *, now: float | None = None) -> dict:
    guild_id, channel_id, author_id = _ids(guild_id, channel_id, author_id)
    if not text and not image_url:
        raise ValueError("Добавь текст или картинку.")
    now = time.time() if now is None else now

    def work(connection):
        _schema(connection)
        cursor = connection.execute(
            "INSERT INTO stories (guild_id, channel_id, author_id, text, image_url, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, author_id, text, image_url, now, now + STORY_DURATION_SECONDS),
        )
        return {"id": cursor.lastrowid, "expires_at": now + STORY_DURATION_SECONDS}

    return await feature_storage.run(work)


async def attach_message(story_id: int, message_id: int) -> None:
    def work(connection):
        _schema(connection)
        connection.execute("UPDATE stories SET message_id = ? WHERE id = ?", (message_id, story_id))

    await feature_storage.run(work)


async def record_view(story_id: int, user_id: int) -> bool:
    """Returns True if this is a new view (not already recorded)."""

    def work(connection):
        _schema(connection)
        existing = connection.execute(
            "SELECT 1 FROM story_views WHERE story_id = ? AND user_id = ?", (story_id, user_id)
        ).fetchone()
        if existing is not None:
            return False
        connection.execute(
            "INSERT INTO story_views (story_id, user_id, viewed_at) VALUES (?, ?, ?)",
            (story_id, user_id, time.time()),
        )
        return True

    return await feature_storage.run(work)


async def get_story(story_id: int) -> dict | None:
    def work(connection):
        _schema(connection)
        row = connection.execute("SELECT * FROM stories WHERE id = ? AND deleted = 0", (story_id,)).fetchone()
        return dict(row) if row else None

    return await feature_storage.run(work)


async def list_viewers(story_id: int, limit: int = 50) -> list[int]:
    def work(connection):
        _schema(connection)
        rows = connection.execute(
            "SELECT user_id FROM story_views WHERE story_id = ? ORDER BY viewed_at ASC LIMIT ?",
            (story_id, limit),
        ).fetchall()
        return [row["user_id"] for row in rows]

    return await feature_storage.run(work)


async def due_expired(now: float) -> list[dict]:
    def work(connection):
        _schema(connection)
        rows = connection.execute(
            "SELECT id, guild_id, channel_id, message_id FROM stories WHERE expires_at <= ? AND deleted = 0",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    return await feature_storage.run(work)


async def mark_deleted(story_id: int) -> None:
    def work(connection):
        _schema(connection)
        connection.execute("UPDATE stories SET deleted = 1 WHERE id = ?", (story_id,))

    await feature_storage.run(work)
