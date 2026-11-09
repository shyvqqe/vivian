from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from itertools import islice
import time

from discord.ext import tasks

from core import config, database, feature_storage, state as core_state
from vivian import memory_controls_store as controls, memory_state as state


_SCHEMA_LOCK = asyncio.Lock()
_READY_POOL = None
_MESSAGE_COLUMNS = "message_id, author_id, audience_id, role, display_name, content, created_at"
_PENDING_LIMIT = 2000
_RETRY_BATCH = 100


def uses_local_storage():
    return not config.DATABASE_URL and database.db_pool is None


def is_ready():
    pool = database.db_pool
    return state.AI_MEMORY_READY and (uses_local_storage() or (
        pool is not None and pool is _READY_POOL and not pool.closed
    ))


def local_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS ai_chat_messages (
        guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL, audience_id INTEGER NOT NULL, role TEXT NOT NULL,
        display_name TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY (guild_id, channel_id, message_id)
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS ai_chat_messages_recent
        ON ai_chat_messages(guild_id, channel_id, created_at DESC, message_id DESC)""")
    connection.execute("""CREATE INDEX IF NOT EXISTS ai_chat_messages_personal
        ON ai_chat_messages(guild_id, channel_id, audience_id, message_id DESC)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS ai_member_facts (
        guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        fact_key TEXT NOT NULL, content TEXT NOT NULL, source_message_id INTEGER NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (guild_id, channel_id, user_id, fact_key)
    )""")


async def _postgres_pool():
    global _READY_POOL
    pool = database.db_pool
    if pool is None or pool.closed:
        state.AI_MEMORY_READY = False
        return None
    async with _SCHEMA_LOCK:
        if _READY_POOL is pool:
            state.AI_MEMORY_READY = True
            return pool
        if state.AI_MEMORY_LAST_OPEN_ATTEMPT and time.monotonic() - state.AI_MEMORY_LAST_OPEN_ATTEMPT < 15:
            return None
        state.AI_MEMORY_LAST_OPEN_ATTEMPT = time.monotonic()
        state.AI_MEMORY_READY = False
        try:
            async with asyncio.timeout(4), pool.connection() as connection:
                for sql in state.AI_MEMORY_SCHEMA_SQL:
                    await connection.execute(sql)
        except Exception as error:
            print(f"[AI Memory] Schema unavailable; error={type(error).__name__}", flush=True)
            return None
        _READY_POOL = pool
        state.AI_MEMORY_READY = True
        return pool


def _cutoff(days):
    return time.time() - days * 86400


def _pg_timestamp(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc)


def _record(row):
    mid, author, audience, role, name, content, created = row
    return {
        "message_id": mid, "author_id": author, "audience_id": audience,
        "role": role, "name": name, "text": content,
        "timestamp": created.timestamp() if isinstance(created, datetime) else created,
    }


def _merge_loaded(scope, rows, fact_rows):
    from vivian.memory import _ai_memory_touch

    _ai_memory_touch(scope)
    records = {item["message_id"]: item for item in map(_record, rows)}
    records.update({item["message_id"]: item for item in state.AI_MEMORY_CHANNELS.get(scope, ())})
    purged = controls.PURGED_SOURCE_IDS.get(scope, set())
    cutoff = _cutoff(state.AI_MEMORY_DAYS)
    recent = sorted(
        (item for mid, item in records.items() if mid not in purged and item["timestamp"] >= cutoff),
        key=lambda item: (item["timestamp"], item["message_id"]),
    )
    state.AI_MEMORY_CHANNELS[scope] = deque(recent[-state.AI_MEMORY_CACHE_LIMIT:], maxlen=state.AI_MEMORY_CACHE_LIMIT)
    fact_cache = state.AI_MEMORY_FACTS.setdefault(scope, {})
    for uid, key, text, source, updated in fact_rows:
        timestamp = updated.timestamp() if isinstance(updated, datetime) else updated
        personal = fact_cache.setdefault(uid, {})
        existing = personal.get(key)
        if existing is None or source >= existing["source"]:
            personal[key] = {"text": text, "source": source, "timestamp": timestamp}
    for uid, personal in fact_cache.items():
        active = sorted(((key, item) for key, item in personal.items() if item["text"]),
                        key=lambda entry: (entry[1]["timestamp"], entry[0]), reverse=True)
        fact_cache[uid] = {
            **dict(active[:state.AI_MEMORY_FACT_LIMIT]),
            **{key: item for key, item in personal.items() if not item["text"]},
        }


def _load_local(connection, scope):
    local_schema(connection)
    messages = connection.execute(
        f"SELECT {_MESSAGE_COLUMNS} FROM ai_chat_messages WHERE guild_id = ? AND channel_id = ? "
        "AND created_at >= ? ORDER BY created_at DESC, message_id DESC LIMIT ?",
        (*scope, _cutoff(state.AI_MEMORY_DAYS), state.AI_MEMORY_CACHE_LIMIT),
    ).fetchall()
    facts = connection.execute(
        "SELECT user_id, fact_key, content, source_message_id, updated_at FROM ai_member_facts "
        "WHERE guild_id = ? AND channel_id = ? AND updated_at >= ?",
        (*scope, _cutoff(state.AI_MEMORY_FACT_DAYS)),
    ).fetchall()
    return messages, facts


async def _load(scope):
    if scope in state.AI_MEMORY_LOADED or time.monotonic() < state.AI_MEMORY_LOAD_RETRY.get(scope, 0):
        return
    try:
        if uses_local_storage():
            messages, facts = await feature_storage.run(lambda connection: _load_local(connection, scope))
        else:
            pool = await _postgres_pool()
            if pool is None:
                return
            async with asyncio.timeout(3), pool.connection() as connection:
                messages = await (await connection.execute(
                    f"SELECT {_MESSAGE_COLUMNS} FROM elis_private.chat_messages WHERE guild_id = %s AND channel_id = %s "
                    "AND created_at >= %s ORDER BY created_at DESC, message_id DESC LIMIT %s",
                    (*scope, _pg_timestamp(_cutoff(state.AI_MEMORY_DAYS)), state.AI_MEMORY_CACHE_LIMIT),
                )).fetchall()
                facts = await (await connection.execute(
                    "SELECT user_id, fact_key, content, source_message_id, updated_at FROM elis_private.member_facts "
                    "WHERE guild_id = %s AND channel_id = %s AND updated_at >= %s",
                    (*scope, _pg_timestamp(_cutoff(state.AI_MEMORY_FACT_DAYS))),
                )).fetchall()
        _merge_loaded(scope, messages, facts)
        state.AI_MEMORY_LOADED.add(scope)
        state.AI_MEMORY_LOAD_RETRY.pop(scope, None)
        state.AI_MEMORY_READY = True
    except Exception as error:
        state.AI_MEMORY_LOAD_RETRY[scope] = time.monotonic() + 15
        print(f"[AI Memory] Read deferred; channel={scope[1]}; error={type(error).__name__}", flush=True)


async def _ai_memory_load(scope):
    await controls.guarded_memory_load(_load, scope)


async def _ai_memory_read_turn(scope, uid, before_id):
    # The bounded channel cache supplies recent context. Query personal turns separately
    # so busy public channels cannot evict a user's conversation after a restart.
    if scope not in state.AI_MEMORY_LOADED:
        return []
    async with controls.control_lock(scope):
        try:
            cutoff = _cutoff(state.AI_MEMORY_DAYS)
            if uses_local_storage():
                def work(connection):
                    return connection.execute(
                        f"SELECT {_MESSAGE_COLUMNS} FROM ai_chat_messages "
                        "WHERE guild_id = ? AND channel_id = ? AND audience_id = ? "
                        "AND message_id < ? AND created_at >= ? ORDER BY message_id DESC LIMIT ?",
                        (*scope, uid, before_id, cutoff, core_state.MAX_HISTORY),
                    ).fetchall()
                rows = await feature_storage.run(work)
            else:
                pool = await _postgres_pool()
                if pool is None:
                    return []
                async with asyncio.timeout(3), pool.connection() as connection:
                    rows = await (await connection.execute(
                        f"SELECT {_MESSAGE_COLUMNS} FROM elis_private.chat_messages "
                        "WHERE guild_id = %s AND channel_id = %s AND audience_id = %s "
                        "AND message_id < %s AND created_at >= %s ORDER BY message_id DESC LIMIT %s",
                        (*scope, uid, before_id, _pg_timestamp(cutoff), core_state.MAX_HISTORY),
                    )).fetchall()
            return [_record(row) for row in rows]
        except Exception as error:
            print(f"[AI Memory] Turn read deferred; channel={scope[1]}; error={type(error).__name__}", flush=True)
            return []


def _message_values(scope, record, stamp):
    return (*scope, record["message_id"], record["author_id"], record["audience_id"], record["role"],
            record["name"], record["text"], stamp)


def _fact_values(scope, record, facts, stamp):
    return [(*scope, record["author_id"], key, text, record["message_id"], stamp) for key, text in facts.items()]


def _source_was_replaced(record, candidates, rows):
    mid = record["message_id"]
    return any(key in candidates and (source > mid or (source == mid and text != candidates[key]))
               for key, text, source, _ in rows)


def _write_local(connection, scope, record, facts, candidates):
    local_schema(connection)
    keys = set(candidates) | set(facts)
    rows = []
    if keys:
        placeholders = ", ".join("?" for _ in keys)
        rows = connection.execute(
            "SELECT fact_key, content, source_message_id, updated_at FROM ai_member_facts "
            f"WHERE guild_id = ? AND channel_id = ? AND user_id = ? AND fact_key IN ({placeholders})",
            (*scope, record["author_id"], *keys),
        ).fetchall()
        if _source_was_replaced(record, candidates, rows):
            return False, rows
    stamp = record["timestamp"]
    connection.execute(
        """INSERT INTO ai_chat_messages
        (guild_id, channel_id, message_id, author_id, audience_id, role, display_name, content, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id, channel_id, message_id) DO NOTHING""",
        _message_values(scope, record, stamp),
    )
    if facts:
        connection.executemany(
            """INSERT INTO ai_member_facts
            (guild_id, channel_id, user_id, fact_key, content, source_message_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id, channel_id, user_id, fact_key) DO UPDATE SET
            content = excluded.content, source_message_id = excluded.source_message_id,
            updated_at = excluded.updated_at WHERE ai_member_facts.source_message_id < excluded.source_message_id""",
            _fact_values(scope, record, facts, stamp),
        )
    return True, rows


async def _write(scope, record, facts, candidates):
    if uses_local_storage():
        result = await feature_storage.run(lambda connection: _write_local(connection, scope, record, facts, candidates))
    else:
        pool = await _postgres_pool()
        if pool is None:
            return None
        async with asyncio.timeout(3), pool.connection() as connection:
            # Forget and edit actions take the same channel lock before removing
            # sources. Recheck tombstones in this transaction before any insert.
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (scope[1],))
            keys = list(set(candidates) | set(facts))
            rows = []
            if keys:
                rows = await (await connection.execute(
                    """SELECT fact_key, content, source_message_id, updated_at FROM elis_private.member_facts
                    WHERE guild_id = %s AND channel_id = %s AND user_id = %s AND fact_key = ANY(%s)""",
                    (*scope, record["author_id"], keys),
                )).fetchall()
                if _source_was_replaced(record, candidates, rows):
                    return False, rows
            stamp = _pg_timestamp(record["timestamp"])
            await connection.execute(
                """INSERT INTO elis_private.chat_messages
                (guild_id, channel_id, message_id, author_id, audience_id, role, display_name, content, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (guild_id, channel_id, message_id) DO NOTHING""",
                _message_values(scope, record, stamp),
            )
            if facts:
                async with connection.cursor() as cursor:
                    await cursor.executemany(
                        """INSERT INTO elis_private.member_facts
                        (guild_id, channel_id, user_id, fact_key, content, source_message_id, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (guild_id, channel_id, user_id, fact_key) DO UPDATE SET
                        content = EXCLUDED.content, source_message_id = EXCLUDED.source_message_id,
                        updated_at = EXCLUDED.updated_at WHERE member_facts.source_message_id < EXCLUDED.source_message_id""",
                        _fact_values(scope, record, facts, stamp),
                    )
        result = True, rows
    state.AI_MEMORY_READY = True
    return result


async def _ai_memory_save(scope, record, facts):
    pending = state.AI_MEMORY_PENDING
    key = (*scope, record["message_id"])
    if record["timestamp"] < _cutoff(state.AI_MEMORY_DAYS):
        pending.pop(key, None)
        return
    if key not in pending:
        if len(pending) >= _PENDING_LIMIT:
            pending.pop(next(iter(pending)))
            print("[AI Memory] Retry queue full; oldest pending message discarded.", flush=True)
        pending[key] = (scope, record, dict(facts))
    try:
        # SQLite runs in a worker thread and retains its privacy lock through commit.
        timeout = None if uses_local_storage() else 4
        async with asyncio.timeout(timeout):
            if await controls.guarded_memory_write(_write, scope, record, facts):
                pending.pop(key, None)
    except Exception as error:
        print(f"[AI Memory] Write queued; channel={scope[1]}; error={type(error).__name__}", flush=True)


def _maintain_local(connection, messages_before, facts_before):
    local_schema(connection)
    connection.execute("DELETE FROM ai_chat_messages WHERE created_at < ?", (messages_before,))
    connection.execute("DELETE FROM ai_member_facts WHERE updated_at < ?", (facts_before,))
    connection.execute(
        """DELETE FROM ai_chat_messages WHERE rowid IN (
        SELECT rowid FROM (
            SELECT rowid, row_number() OVER (
                PARTITION BY guild_id, channel_id ORDER BY created_at DESC, message_id DESC
            ) AS position FROM ai_chat_messages
        ) WHERE position > ?)""", (state.AI_MEMORY_CHANNEL_LIMIT,),
    )


async def _maintain():
    messages_before = _cutoff(state.AI_MEMORY_DAYS)
    facts_before = _cutoff(state.AI_MEMORY_FACT_DAYS)
    if uses_local_storage():
        await feature_storage.run(lambda connection: _maintain_local(connection, messages_before, facts_before))
    else:
        pool = await _postgres_pool()
        if pool is None:
            return
        async with asyncio.timeout(8), pool.connection() as connection:
            await connection.execute("DELETE FROM elis_private.chat_messages WHERE created_at < %s",
                                     (_pg_timestamp(messages_before),))
            await connection.execute("DELETE FROM elis_private.member_facts WHERE updated_at < %s",
                                     (_pg_timestamp(facts_before),))
            await connection.execute(
                """DELETE FROM elis_private.chat_messages AS archive USING (
                    SELECT guild_id, channel_id, message_id, row_number() OVER (
                        PARTITION BY guild_id, channel_id ORDER BY created_at DESC, message_id DESC
                    ) AS position FROM elis_private.chat_messages
                ) AS ranked WHERE archive.guild_id = ranked.guild_id AND archive.channel_id = ranked.channel_id
                AND archive.message_id = ranked.message_id AND ranked.position > %s""",
                (state.AI_MEMORY_CHANNEL_LIMIT,),
            )
    state.AI_MEMORY_LAST_MAINTENANCE = time.monotonic()
    state.AI_MEMORY_READY = True


@tasks.loop(seconds=15)
async def ai_memory_flush_loop():
    from core import ai_preferences

    await ai_preferences.load_preferences()
    pending = state.AI_MEMORY_PENDING
    for key in list(islice(pending, _RETRY_BATCH)):
        item = pending.get(key)
        if item is None:
            continue
        await _ai_memory_save(*item)
        if key in pending:
            pending[key] = pending.pop(key)
            break
    if time.monotonic() - state.AI_MEMORY_LAST_MAINTENANCE >= 1800 or not state.AI_MEMORY_LAST_MAINTENANCE:
        try:
            await _maintain()
        except Exception as error:
            print(f"[AI Memory] Maintenance deferred; error={type(error).__name__}", flush=True)


@ai_memory_flush_loop.before_loop
async def before_ai_memory_flush():
    await config.bot.wait_until_ready()


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS ai_memories (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        fact TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id, fact)
    )""")


async def save_fact(guild_id: int, user_id: int, fact: str):
    def work(connection):
        _schema(connection)
        connection.execute(
            "INSERT OR IGNORE INTO ai_memories (guild_id, user_id, fact, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, fact[:500], time.time()),
        )
    try:
        await feature_storage.run_local(work)
    except Exception as error:
        print(f"[AI Memory] Legacy fact write failed; error={type(error).__name__}", flush=True)


async def get_facts(guild_id: int, user_id: int):
    def work(connection):
        _schema(connection)
        rows = connection.execute(
            "SELECT fact FROM ai_memories WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT 20",
            (guild_id, user_id),
        ).fetchall()
        return [row["fact"] for row in rows]
    try:
        return await feature_storage.run_local(work)
    except Exception as error:
        print(f"[AI Memory] Legacy fact read failed; error={type(error).__name__}", flush=True)
        return []

