from __future__ import annotations

import math
import time

from core import feature_storage


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS vivian_capsules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        opens_at INTEGER NOT NULL,
        quorum INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        revealed_at INTEGER,
        cancelled_at INTEGER
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS vivian_capsule_notes (
        capsule_id INTEGER NOT NULL REFERENCES vivian_capsules(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        note TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (capsule_id, user_id)
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS vivian_capsules_channel
        ON vivian_capsules(guild_id, channel_id, id DESC)""")


def _identity(*values):
    if any(type(value) is not int or not 0 < value < 2**63 for value in values):
        raise ValueError("Invalid capsule or member identifier.")


def _clock(now):
    value = time.time() if now is None else now
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= 2**63 - 1 - 43200 * 60):
        raise ValueError("Invalid timestamp.")
    return int(value)


def _text(value, limit, label):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"{label} must contain 1 to {limit} characters.")
    return value.strip()


def _get(connection, guild_id, channel_id, capsule_id):
    row = connection.execute(
        "SELECT * FROM vivian_capsules WHERE id = ? AND guild_id = ? AND channel_id = ?",
        (capsule_id, guild_id, channel_id),
    ).fetchone()
    if row is None:
        raise ValueError("Capsule not found in this channel.")
    return dict(row)


def _summary(connection, capsule):
    capsule = dict(capsule)
    capsule["contributors"] = connection.execute(
        "SELECT count(*) FROM vivian_capsule_notes WHERE capsule_id = ?", (capsule["id"],)
    ).fetchone()[0]
    return capsule


async def create(guild_id, channel_id, owner_id, title, minutes, quorum, *, now=None):
    _identity(guild_id, channel_id, owner_id)
    title = _text(title, 120, "Title")
    if type(minutes) is not int or not 1 <= minutes <= 43200:
        raise ValueError("Choose a delay between 1 and 43200 minutes.")
    if type(quorum) is not int or not 1 <= quorum <= 50:
        raise ValueError("Choose a quorum between 1 and 50 members.")
    timestamp = _clock(now)

    def operation(connection):
        _schema(connection)
        count = connection.execute(
            "SELECT count(*) FROM vivian_capsules WHERE guild_id = ? AND channel_id = ? "
            "AND revealed_at IS NULL AND cancelled_at IS NULL", (guild_id, channel_id),
        ).fetchone()[0]
        if count >= 20:
            raise ValueError("This channel already has 20 sealed capsules. Open or cancel one first.")
        cursor = connection.execute(
            "INSERT INTO vivian_capsules (guild_id, channel_id, owner_id, title, opens_at, quorum, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, owner_id, title, timestamp + minutes * 60, quorum, timestamp),
        )
        return _summary(connection, _get(connection, guild_id, channel_id, cursor.lastrowid))

    return await feature_storage.run(operation)


async def contribute(guild_id, channel_id, capsule_id, user_id, note, *, now=None):
    _identity(guild_id, channel_id, capsule_id, user_id)
    note = _text(note, 1000, "Note")
    timestamp = _clock(now)

    def operation(connection):
        _schema(connection)
        capsule = _get(connection, guild_id, channel_id, capsule_id)
        if capsule["cancelled_at"] is not None:
            raise ValueError("This capsule was cancelled.")
        if capsule["revealed_at"] is not None:
            raise ValueError("This capsule is already open. Its notes are sealed.")
        exists = connection.execute(
            "SELECT 1 FROM vivian_capsule_notes WHERE capsule_id = ? AND user_id = ?",
            (capsule_id, user_id),
        ).fetchone()
        if exists is None and _summary(connection, capsule)["contributors"] >= 50:
            raise ValueError("This capsule already has 50 contributors.")
        connection.execute(
            "INSERT INTO vivian_capsule_notes (capsule_id, user_id, note, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(capsule_id, user_id) DO UPDATE SET note = excluded.note, updated_at = excluded.updated_at",
            (capsule_id, user_id, note, timestamp),
        )
        return _summary(connection, capsule)

    return await feature_storage.run(operation)


async def open_capsule(guild_id, channel_id, capsule_id, *, now=None):
    _identity(guild_id, channel_id, capsule_id)
    timestamp = _clock(now)

    def operation(connection):
        _schema(connection)
        capsule = _summary(connection, _get(connection, guild_id, channel_id, capsule_id))
        if capsule["cancelled_at"] is not None:
            raise ValueError("This capsule was cancelled.")
        capsule["notes"] = []
        capsule["ready"] = timestamp >= capsule["opens_at"] and capsule["contributors"] >= capsule["quorum"]
        if capsule["revealed_at"] is not None:
            capsule["ready"] = True
        if capsule["ready"]:
            if capsule["revealed_at"] is None:
                connection.execute("UPDATE vivian_capsules SET revealed_at = ? WHERE id = ?", (timestamp, capsule_id))
                capsule["revealed_at"] = timestamp
            capsule["notes"] = [dict(row) for row in connection.execute(
                "SELECT user_id, note, updated_at FROM vivian_capsule_notes "
                "WHERE capsule_id = ? ORDER BY user_id", (capsule_id,),
            ).fetchall()]
        return capsule

    return await feature_storage.run(operation)


async def list_capsules(guild_id, channel_id):
    _identity(guild_id, channel_id)

    def operation(connection):
        _schema(connection)
        rows = connection.execute(
            "SELECT * FROM vivian_capsules WHERE guild_id = ? AND channel_id = ? ORDER BY id DESC LIMIT 10",
            (guild_id, channel_id),
        ).fetchall()
        return [_summary(connection, row) for row in rows]

    return await feature_storage.run(operation)


async def cancel(guild_id, channel_id, capsule_id, actor_id, *, moderator=False, now=None):
    _identity(guild_id, channel_id, capsule_id, actor_id)
    timestamp = _clock(now)
    if type(moderator) is not bool:
        raise ValueError("Invalid moderator permission.")

    def operation(connection):
        _schema(connection)
        capsule = _get(connection, guild_id, channel_id, capsule_id)
        if capsule["owner_id"] != actor_id and not moderator:
            raise ValueError("Only the creator or a server manager can cancel this capsule.")
        if capsule["revealed_at"] is not None:
            raise ValueError("An open capsule cannot be cancelled.")
        connection.execute("DELETE FROM vivian_capsule_notes WHERE capsule_id = ?", (capsule_id,))
        connection.execute("UPDATE vivian_capsules SET cancelled_at = ? WHERE id = ?", (timestamp, capsule_id))
        return True

    return await feature_storage.run(operation)
