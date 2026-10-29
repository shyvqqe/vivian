from __future__ import annotations

import time

from core import feature_storage

MAX_TEXT = 100


def _ids(*values):
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("A server and member are required.")
    return values


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS relationships (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        partner_id INTEGER NOT NULL,
        since REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS relationship_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        from_id INTEGER NOT NULL,
        to_id INTEGER NOT NULL,
        message_id INTEGER,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','declined','canceled')),
        created_at REAL NOT NULL
    )""")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS relationship_requests_pending "
        "ON relationship_requests(guild_id, from_id, to_id) WHERE status = 'pending'"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS relationship_requests_message ON relationship_requests(message_id)"
    )
    connection.execute("""CREATE TABLE IF NOT EXISTS status_profiles (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        text TEXT,
        mood TEXT,
        updated_at REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )""")


def _is_single(connection, guild_id, user_id):
    row = connection.execute(
        "SELECT 1 FROM relationships WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    ).fetchone()
    return row is None


async def propose(guild_id: int, from_id: int, to_id: int) -> int:
    guild_id, from_id, to_id = _ids(guild_id, from_id, to_id)
    if from_id == to_id:
        raise ValueError("Нельзя предложить отношения самому себе.")

    def work(connection):
        _schema(connection)
        if not _is_single(connection, guild_id, from_id):
            raise ValueError("Ты уже в отношениях. Сначала используй /status_single.")
        if not _is_single(connection, guild_id, to_id):
            raise ValueError("Этот участник уже в отношениях.")
        existing = connection.execute(
            "SELECT id FROM relationship_requests WHERE guild_id = ? AND from_id = ? AND to_id = ? AND status = 'pending'",
            (guild_id, from_id, to_id),
        ).fetchone()
        if existing is not None:
            raise ValueError("Предложение уже отправлено и ожидает ответа.")
        cursor = connection.execute(
            "INSERT INTO relationship_requests (guild_id, from_id, to_id, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (guild_id, from_id, to_id, time.time()),
        )
        return cursor.lastrowid

    return await feature_storage.run(work)


async def attach_message(guild_id: int, request_id: int, message_id: int) -> None:
    guild_id, request_id, message_id = _ids(guild_id, request_id, message_id)

    def work(connection):
        _schema(connection)
        connection.execute(
            "UPDATE relationship_requests SET message_id = ? WHERE id = ? AND guild_id = ?",
            (message_id, request_id, guild_id),
        )

    await feature_storage.run(work)


async def resolve_by_message(guild_id: int, message_id: int, responder_id: int, accept: bool) -> dict:
    guild_id, message_id, responder_id = _ids(guild_id, message_id, responder_id)

    def work(connection):
        _schema(connection)
        row = connection.execute(
            "SELECT * FROM relationship_requests WHERE guild_id = ? AND message_id = ? AND status = 'pending'",
            (guild_id, message_id),
        ).fetchone()
        if row is None:
            raise ValueError("Это предложение больше не активно.")
        item = dict(row)
        if responder_id != item["to_id"]:
            raise PermissionError("Ответить может только приглашённый участник.")
        now = time.time()
        if accept and (not _is_single(connection, guild_id, item["from_id"])
                       or not _is_single(connection, guild_id, item["to_id"])):
            connection.execute(
                "UPDATE relationship_requests SET status = 'declined' WHERE id = ?", (item["id"],)
            )
            raise ValueError("Один из вас уже успел завести отношения с кем-то другим.")
        connection.execute(
            "UPDATE relationship_requests SET status = ? WHERE id = ?",
            ("accepted" if accept else "declined", item["id"]),
        )
        if accept:
            connection.execute(
                "INSERT INTO relationships (guild_id, user_id, partner_id, since) VALUES (?, ?, ?, ?)",
                (guild_id, item["from_id"], item["to_id"], now),
            )
            connection.execute(
                "INSERT INTO relationships (guild_id, user_id, partner_id, since) VALUES (?, ?, ?, ?)",
                (guild_id, item["to_id"], item["from_id"], now),
            )
        return {"from_id": item["from_id"], "to_id": item["to_id"]}

    return await feature_storage.run(work)


async def single(guild_id: int, user_id: int) -> int | None:
    """Ends the relationship for both sides. Returns the ex-partner's id, if any."""
    guild_id, user_id = _ids(guild_id, user_id)

    def work(connection):
        _schema(connection)
        row = connection.execute(
            "SELECT partner_id FROM relationships WHERE guild_id = ? AND user_id = ?", (guild_id, user_id),
        ).fetchone()
        if row is None:
            return None
        partner_id = row["partner_id"]
        connection.execute(
            "DELETE FROM relationships WHERE guild_id = ? AND user_id IN (?, ?)",
            (guild_id, user_id, partner_id),
        )
        return partner_id

    return await feature_storage.run(work)


async def get_relationship(guild_id: int, user_id: int) -> dict | None:
    guild_id, user_id = _ids(guild_id, user_id)

    def work(connection):
        _schema(connection)
        row = connection.execute(
            "SELECT partner_id, since FROM relationships WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    return await feature_storage.run(work)


async def set_profile(guild_id: int, user_id: int, text: str | None, mood: str | None) -> None:
    guild_id, user_id = _ids(guild_id, user_id)
    if text is not None and len(text) > MAX_TEXT:
        raise ValueError(f"Статус не длиннее {MAX_TEXT} символов.")
    if mood is not None and len(mood) > 16:
        raise ValueError("Настроение — это короткий эмодзи или пара символов.")

    def work(connection):
        _schema(connection)
        existing = connection.execute(
            "SELECT text, mood FROM status_profiles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        new_text = text if text is not None else (existing["text"] if existing else None)
        new_mood = mood if mood is not None else (existing["mood"] if existing else None)
        connection.execute(
            "INSERT INTO status_profiles (guild_id, user_id, text, mood, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET text = excluded.text, mood = excluded.mood, "
            "updated_at = excluded.updated_at",
            (guild_id, user_id, new_text, new_mood, time.time()),
        )

    await feature_storage.run(work)


async def get_profile(guild_id: int, user_id: int) -> dict | None:
    guild_id, user_id = _ids(guild_id, user_id)

    def work(connection):
        _schema(connection)
        row = connection.execute(
            "SELECT text, mood FROM status_profiles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    return await feature_storage.run(work)
