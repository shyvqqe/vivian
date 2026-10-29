from __future__ import annotations

import time

from core import feature_storage

RETENTION_DAYS = 365 * 5


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS memory_messages (
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        content TEXT NOT NULL,
        message_date TEXT NOT NULL,
        reaction_count INTEGER NOT NULL DEFAULT 0,
        posted_as_memory INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, message_id)
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS memory_messages_date ON memory_messages(guild_id, message_date, reaction_count DESC)"
    )


async def record_message(guild_id: int, channel_id: int, message_id: int, author_id: int,
                         content: str, message_date: str) -> None:
    if not content.strip():
        return

    def work(connection):
        _schema(connection)
        connection.execute(
            "INSERT OR IGNORE INTO memory_messages "
            "(guild_id, channel_id, message_id, author_id, content, message_date) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, message_id, author_id, content[:500], message_date),
        )
        connection.execute(
            "DELETE FROM memory_messages WHERE guild_id = ? AND message_date < date('now', ?)",
            (guild_id, f"-{RETENTION_DAYS} days"),
        )

    await feature_storage.run(work)


async def bump_reactions(guild_id: int, message_id: int, delta: int) -> None:
    def work(connection):
        _schema(connection)
        connection.execute(
            "UPDATE memory_messages SET reaction_count = MAX(0, reaction_count + ?) "
            "WHERE guild_id = ? AND message_id = ?",
            (delta, guild_id, message_id),
        )

    await feature_storage.run(work)


async def best_for_month_day(guild_id: int, month_day: str, exclude_date: str) -> dict | None:
    """Best-liked message whose date shares this month-day (e.g. '10-29') in any earlier year."""

    def work(connection):
        _schema(connection)
        row = connection.execute(
            "SELECT * FROM memory_messages WHERE guild_id = ? AND strftime('%m-%d', message_date) = ? "
            "AND message_date != ? ORDER BY reaction_count DESC, message_id ASC LIMIT 1",
            (guild_id, month_day, exclude_date),
        ).fetchone()
        return dict(row) if row else None

    return await feature_storage.run(work)
