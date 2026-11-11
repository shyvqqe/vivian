"""Durable activity snapshots for the bot's single gateway process."""
from __future__ import annotations

from datetime import date
import json

from core import feature_storage, state


TABLES = (
    (
        "member_activity", ("guild_id", "user_id"),
        ("messages", "word_messages", "long_messages", "characters"),
        """CREATE TABLE IF NOT EXISTS member_activity (
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            messages BIGINT NOT NULL DEFAULT 0,
            word_messages BIGINT NOT NULL DEFAULT 0,
            long_messages BIGINT NOT NULL DEFAULT 0,
            characters BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )""",
    ),
    (
        "daily_activity", ("activity_date", "guild_id", "user_id"),
        ("messages", "profanity"),
        """CREATE TABLE IF NOT EXISTS daily_activity (
            activity_date DATE NOT NULL,
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            messages BIGINT NOT NULL DEFAULT 0,
            profanity BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (activity_date, guild_id, user_id)
        )""",
    ),
    (
        "user_xp", ("user_id",), ("xp", "level"),
        """CREATE TABLE IF NOT EXISTS user_xp (
            user_id BIGINT PRIMARY KEY,
            xp BIGINT NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 1
        )""",
    ),
)


def _upserts(placeholder, highest):
    queries = []
    for table, keys, counters, _ in TABLES:
        columns = keys + counters
        placeholders = ", ".join([placeholder] * len(columns))
        updates = ", ".join(
            f"{column} = {highest}({table}.{column}, EXCLUDED.{column})" for column in counters
        )
        queries.append(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}"
        )
    return tuple(queries)


POSTGRES_UPSERTS = _upserts("%s", "GREATEST")
SQLITE_UPSERTS = _upserts("?", "max")


def snapshot_rows(guild_id, user_id, activity_date, rank_stats, daily_stats, user_xp):
    rank, daily = None, None
    if guild_id is not None and rank_stats is not None and daily_stats is not None:
        rank = (guild_id, user_id, *(rank_stats[key] for key in TABLES[0][2]))
        daily = (activity_date.isoformat(), guild_id, user_id, *(daily_stats[key] for key in TABLES[1][2]))
    return rank, daily, (user_id, user_xp["xp"], user_xp["level"])


def select_queries(first_day, placeholder):
    for table, keys, counters, _ in TABLES:
        query = f"SELECT {', '.join(keys + counters)} FROM {table}"
        if table == "daily_activity":
            yield f"{query} WHERE activity_date >= {placeholder}", (first_day.isoformat(),)
        else:
            yield query, ()


def restore(rows):
    rank, daily, xp = rows
    for guild_id, user_id, *counters in rank:
        state.RANK_ACTIVITY[guild_id, user_id] = dict(zip(TABLES[0][2], counters))
    for day, guild_id, user_id, *counters in daily:
        if isinstance(day, str):
            day = date.fromisoformat(day)
        state.DAILY_ACTIVITY[day, guild_id, user_id] = dict(zip(TABLES[1][2], counters))
    for user_id, points, level in xp:
        state.xp_data[user_id] = {"xp": points, "level": level}


def init_sqlite(connection, first_day, cutoff):
    for _, _, _, sql in TABLES:
        connection.execute(sql)
    connection.execute("DELETE FROM daily_activity WHERE activity_date < ?", (cutoff.isoformat(),))
    return tuple(connection.execute(query, params).fetchall() for query, params in select_queries(first_day, "?"))


def save_sqlite(connection, rows):
    for query, row in zip(SQLITE_UPSERTS, rows):
        if row is not None:
            connection.execute(query, row)


async def defer(rows):
    def write(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS stats_pending (
            id INTEGER PRIMARY KEY,
            snapshot TEXT NOT NULL
        )""")
        connection.execute("INSERT INTO stats_pending (snapshot) VALUES (?)", (json.dumps(rows),))

    await feature_storage.run_local(write)


async def pending_batch():
    def read(connection):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stats_pending'"
        ).fetchone()
        if not exists:
            return []
        return [
            (row[0], json.loads(row[1]))
            for row in connection.execute("SELECT id, snapshot FROM stats_pending ORDER BY id LIMIT 100")
        ]

    if not feature_storage.database_path().is_file():
        return []
    return await feature_storage.run_local(read)


async def acknowledge(last_id):
    await feature_storage.run_local(
        lambda connection: connection.execute("DELETE FROM stats_pending WHERE id <= ?", (last_id,))
    )
