from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import feature_storage

RETENTION_DAYS = 365 * 5


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS memory_messages (
        guild_id BIGINT NOT NULL,
        channel_id BIGINT NOT NULL,
        message_id BIGINT NOT NULL,
        author_id BIGINT NOT NULL,
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
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=RETENTION_DAYS)

    def work(connection):
        _schema(connection)
        connection.execute(
            "INSERT INTO memory_messages "
            "(guild_id, channel_id, message_id, author_id, content, message_date) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, message_id) DO NOTHING",
            (guild_id, channel_id, message_id, author_id, content[:500], message_date),
        )
        connection.execute(
            "DELETE FROM memory_messages WHERE guild_id = ? AND message_date < ?",
            (guild_id, cutoff.isoformat()),
        )

    await feature_storage.run(work)


async def bump_reactions(guild_id: int, message_id: int, delta: int) -> None:
    def work(connection):
        _schema(connection)
        connection.execute(
            "UPDATE memory_messages SET reaction_count = "
            "CASE WHEN reaction_count + ? < 0 THEN 0 ELSE reaction_count + ? END "
            "WHERE guild_id = ? AND message_id = ?",
            (delta, delta, guild_id, message_id),
        )

    await feature_storage.run(work)


async def best_for_month_day(guild_id: int, channel_id: int, month_day: str, exclude_date: str) -> dict | None:
    """Find the best-liked message from this channel on the same date in an earlier year."""

    def work(connection):
        _schema(connection)
        row = connection.execute(
            "SELECT * FROM memory_messages WHERE guild_id = ? AND channel_id = ? AND substr(message_date, 6, 5) = ? "
            "AND message_date != ? ORDER BY reaction_count DESC, message_id ASC LIMIT 1",
            (guild_id, channel_id, month_day, exclude_date),
        ).fetchone()
        return dict(row) if row else None

    return await feature_storage.run(work)
