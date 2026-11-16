"""Durable events and RSVP records shared by the SQLite and PostgreSQL runtimes."""
from __future__ import annotations

from dataclasses import dataclass
import time

from core import feature_storage


@dataclass(frozen=True)
class Premiere:
    id: int
    guild_id: int
    channel_id: int
    host_id: int
    title: str
    details: str
    url: str
    starts_at: int
    message_id: int | None = None
    cancelled_at: int | None = None
    going: int = 0
    maybe: int = 0

    def open_at(self, now: int) -> bool:
        return self.cancelled_at is None and self.starts_at > now


_COLUMNS = (
    "id, guild_id, channel_id, host_id, title, details, url, starts_at, message_id, cancelled_at"
)
_MAX_UPCOMING = 4
_PUBLISH_GRACE = 90
_RETENTION = 30 * 86400


class PremiereUnavailable(ValueError):
    pass


def _schema(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS vivian_premieres (
            id BIGINT PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            host_id BIGINT NOT NULL,
            title TEXT NOT NULL,
            details TEXT NOT NULL,
            url TEXT NOT NULL,
            starts_at BIGINT NOT NULL,
            created_at BIGINT NOT NULL,
            message_id BIGINT,
            cancelled_at BIGINT
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS vivian_premieres_channel
        ON vivian_premieres (guild_id, channel_id, starts_at)
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS vivian_premiere_votes (
            premiere_id BIGINT NOT NULL REFERENCES vivian_premieres(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            choice TEXT NOT NULL CHECK (choice IN ('going', 'maybe')),
            PRIMARY KEY (premiere_id, user_id)
        )
    """)


def _get(connection, event_id):
    row = connection.execute(
        f"SELECT {_COLUMNS} FROM vivian_premieres WHERE id = ?", (event_id,),
    ).fetchone()
    if row is None:
        return None
    counts = connection.execute("""
        SELECT COUNT(CASE WHEN choice = 'going' THEN 1 END),
               COUNT(CASE WHEN choice = 'maybe' THEN 1 END)
        FROM vivian_premiere_votes WHERE premiere_id = ?
    """, (event_id,)).fetchone()
    return Premiere(*(row[index] for index in range(10)), counts[0], counts[1])


async def create(event: Premiere, *, now: int | None = None) -> Premiere:
    if now is None:
        now = int(time.time())

    def write(connection):
        _schema(connection)
        previous = _get(connection, event.id)
        if previous is not None:
            return previous
        connection.execute("""
            DELETE FROM vivian_premieres
            WHERE guild_id = ? AND (starts_at <= ? OR cancelled_at <= ?)
        """, (event.guild_id, now - _RETENTION, now - _RETENTION))
        total = connection.execute("""
            SELECT COUNT(*) FROM vivian_premieres
            WHERE guild_id = ? AND channel_id = ? AND starts_at > ? AND cancelled_at IS NULL
                AND (message_id IS NOT NULL OR created_at > ?)
        """, (event.guild_id, event.channel_id, now, now - _PUBLISH_GRACE)).fetchone()[0]
        if total >= _MAX_UPCOMING:
            raise PremiereUnavailable(f"В этом канале уже запланировано {_MAX_UPCOMING} премьер.")
        connection.execute("""
            INSERT INTO vivian_premieres (id, guild_id, channel_id, host_id, title, details, url, starts_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (event.id, event.guild_id, event.channel_id, event.host_id, event.title,
              event.details, event.url, event.starts_at, now))
        return event

    return await feature_storage.run(write)


async def interact(
    event_id: int, guild_id: int, channel_id: int, message_id: int, user_id: int, action: str,
    *, now: int | None = None,
) -> tuple[Premiere, str]:
    def write(connection):
        _schema(connection)
        event = _get(connection, event_id)
        if event is None or (event.guild_id, event.channel_id) != (guild_id, channel_id):
            raise PremiereUnavailable("У этой карточки нет сохранённой премьеры.")
        if event.message_id is not None and event.message_id != message_id:
            raise PremiereUnavailable("Открой исходную карточку премьеры.")
        if event.message_id is None:
            # A click from the bot's own card can confirm a message after an interrupted launch.
            connection.execute("UPDATE vivian_premieres SET message_id = ? WHERE id = ?", (message_id, event_id))
        if action in {"cancel", "cancel_prompt"} and user_id != event.host_id:
            raise PremiereUnavailable("Отменить премьеру может её организатор.")
        current_time = int(time.time()) if now is None else now
        if action in {"bind", "refresh"}:
            status = "updated"
        elif not event.open_at(current_time):
            status = "closed"
        elif action == "cancel_prompt":
            status = "confirm"
        elif action == "cancel":
            connection.execute("UPDATE vivian_premieres SET cancelled_at = ? WHERE id = ?", (current_time, event_id))
            status = "cancelled"
        elif action == "remove":
            connection.execute("DELETE FROM vivian_premiere_votes WHERE premiere_id = ? AND user_id = ?",
                               (event_id, user_id))
            status = "removed"
        elif action in {"going", "maybe"}:
            connection.execute("""
                INSERT INTO vivian_premiere_votes (premiere_id, user_id, choice) VALUES (?, ?, ?)
                ON CONFLICT (premiere_id, user_id) DO UPDATE SET choice = excluded.choice
            """, (event_id, user_id, action))
            status = action
        else:
            raise ValueError("Unknown premiere action")
        return _get(connection, event_id), status

    return await feature_storage.run(write)
