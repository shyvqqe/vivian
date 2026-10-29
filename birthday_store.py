from __future__ import annotations

from core import feature_storage


def _ids(*values):
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("A server and member are required.")
    return values


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS birthdays (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        day INTEGER NOT NULL CHECK (day BETWEEN 1 AND 31),
        month INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
        year INTEGER,
        PRIMARY KEY (guild_id, user_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS birthday_active_roles (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        role_id INTEGER NOT NULL,
        expires_at REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS birthdays_month_day ON birthdays(guild_id, month, day)"
    )


async def set_birthday(guild_id: int, user_id: int, day: int, month: int, year: int | None) -> None:
    guild_id, user_id = _ids(guild_id, user_id)
    if not 1 <= day <= 31 or not 1 <= month <= 12:
        raise ValueError("Укажи корректные день и месяц.")
    if year is not None and not 1900 <= year <= 2100:
        raise ValueError("Укажи корректный год или оставь поле пустым.")

    def work(connection):
        _schema(connection)
        connection.execute(
            "INSERT INTO birthdays (guild_id, user_id, day, month, year) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET day = excluded.day, month = excluded.month, "
            "year = excluded.year",
            (guild_id, user_id, day, month, year),
        )

    await feature_storage.run(work)


async def get_birthday(guild_id: int, user_id: int) -> dict | None:
    guild_id, user_id = _ids(guild_id, user_id)

    def work(connection):
        _schema(connection)
        row = connection.execute(
            "SELECT day, month, year FROM birthdays WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    return await feature_storage.run(work)


async def for_month_day(guild_id: int, month: int, day: int) -> list[int]:
    guild_id, = _ids(guild_id)

    def work(connection):
        _schema(connection)
        rows = connection.execute(
            "SELECT user_id FROM birthdays WHERE guild_id = ? AND month = ? AND day = ?",
            (guild_id, month, day),
        ).fetchall()
        return [row["user_id"] for row in rows]

    return await feature_storage.run(work)


async def upcoming(guild_id: int, limit: int = 10) -> list[dict]:
    """Ordered by closeness; SQLite handles the day-of-year wraparound in plain SQL."""
    guild_id, = _ids(guild_id)

    def work(connection):
        _schema(connection)
        rows = connection.execute(
            """
            SELECT user_id, day, month, year,
                (CASE
                    WHEN (month * 100 + day) >= CAST(strftime('%m%d', 'now') AS INTEGER)
                    THEN (month * 100 + day) - CAST(strftime('%m%d', 'now') AS INTEGER)
                    ELSE (month * 100 + day) - CAST(strftime('%m%d', 'now') AS INTEGER) + 1231
                END) AS distance
            FROM birthdays WHERE guild_id = ? ORDER BY distance ASC LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    return await feature_storage.run(work)


async def set_active_role(guild_id: int, user_id: int, role_id: int, expires_at: float) -> None:
    guild_id, user_id, role_id = _ids(guild_id, user_id, role_id)

    def work(connection):
        _schema(connection)
        connection.execute(
            "INSERT INTO birthday_active_roles (guild_id, user_id, role_id, expires_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET role_id = excluded.role_id, "
            "expires_at = excluded.expires_at",
            (guild_id, user_id, role_id, expires_at),
        )

    await feature_storage.run(work)


async def due_active_roles(now: float) -> list[dict]:
    def work(connection):
        _schema(connection)
        rows = connection.execute(
            "SELECT guild_id, user_id, role_id FROM birthday_active_roles WHERE expires_at <= ?", (now,)
        ).fetchall()
        return [dict(row) for row in rows]

    return await feature_storage.run(work)


async def clear_active_role(guild_id: int, user_id: int) -> None:
    def work(connection):
        _schema(connection)
        connection.execute(
            "DELETE FROM birthday_active_roles WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )

    await feature_storage.run(work)
