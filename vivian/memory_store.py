from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from discord.ext import tasks
import time
from datetime import timedelta
from core import config as core_config
from core import database as core_database
from core import state as core_state
from vivian import memory as vivian_memory
from vivian import memory_state as vivian_memory_state
from vivian import memory_controls_store as controls_store


@asynccontextmanager
async def _memory_pipeline(connection):
    """Batch roundtrips, retaining the surrounding connection transaction."""
    pipeline = getattr(connection, "pipeline", None)
    try:
        from psycopg import capabilities
        supported = capabilities.has_pipeline()
    except ImportError:
        supported = False
    if pipeline is not None and supported:
        async with pipeline():
            yield
    else:
        yield


async def _open_ai_memory_storage() -> None:
    
    if core_database.db_pool is None or vivian_memory_state.AI_MEMORY_READY:
        return
    vivian_memory_state.AI_MEMORY_LAST_OPEN_ATTEMPT = time.time()
    try:
        async with asyncio.timeout(10), core_database.db_pool.connection() as connection:
            for statement in vivian_memory_state.AI_MEMORY_SCHEMA_SQL:
                await connection.execute(statement)
            await connection.execute(
                "DELETE FROM elis_private.chat_messages WHERE created_at < %s",
                (datetime.now(core_config.MOSCOW_TIMEZONE) - timedelta(days=vivian_memory_state.AI_MEMORY_DAYS),),
            )
            await connection.execute(
                "DELETE FROM elis_private.member_facts WHERE updated_at < %s",
                (datetime.now(core_config.MOSCOW_TIMEZONE) - timedelta(days=vivian_memory_state.AI_MEMORY_FACT_DAYS),),
            )
        vivian_memory_state.AI_MEMORY_READY = True
        # Any previously loaded in-memory scopes must merge their stored archive on reconnect.
        vivian_memory_state.AI_MEMORY_LOADED.clear()
        print("[AI Memory] Persistent conversation archive ready; 30 days, facts 90 days.", flush=True)
    except Exception as error:
        print(f"[AI Memory] Storage unavailable; using RAM: {error!r}", flush=True)


async def _ai_memory_load_unlocked(scope) -> None:
    vivian_memory._ai_memory_touch(scope)
    if scope in vivian_memory_state.AI_MEMORY_LOADED or not vivian_memory_state.AI_MEMORY_READY or core_database.db_pool is None:
        return
    if time.time() < vivian_memory_state.AI_MEMORY_LOAD_RETRY.get(scope, 0):
        return
    lock = vivian_memory_state.AI_MEMORY_LOAD_LOCKS.setdefault(scope, asyncio.Lock())
    async with lock:
        if scope in vivian_memory_state.AI_MEMORY_LOADED:
            return
        try:
            async with asyncio.timeout(4), core_database.db_pool.connection() as connection:
                async with _memory_pipeline(connection):
                    messages_cursor = await connection.execute(
                        """SELECT message_id, author_id, audience_id, role, display_name, content, created_at
                        FROM elis_private.chat_messages WHERE guild_id = %s AND channel_id = %s
                        AND created_at >= %s ORDER BY created_at DESC, message_id DESC LIMIT %s""",
                        (*scope, datetime.now(core_config.MOSCOW_TIMEZONE) - timedelta(days=vivian_memory_state.AI_MEMORY_DAYS), vivian_memory_state.AI_MEMORY_CACHE_LIMIT),
                    )
                    facts_cursor = await connection.execute(
                        """SELECT user_id, fact_key, content, source_message_id, updated_at
                        FROM elis_private.member_facts WHERE guild_id = %s AND channel_id = %s
                        AND updated_at >= %s""",
                        (*scope, datetime.now(core_config.MOSCOW_TIMEZONE) - timedelta(days=vivian_memory_state.AI_MEMORY_FACT_DAYS)),
                    )
                stored = [row async for row in messages_cursor]
                facts = [row async for row in facts_cursor]
            for mid, author, audience, role, name, content, created in reversed(stored):
                vivian_memory._ai_memory_cache_record(scope, {"message_id": mid, "author_id": author, "audience_id": audience,
                    "role": role, "name": name, "text": content, "timestamp": created.timestamp()})
            store = vivian_memory_state.AI_MEMORY_FACTS.setdefault(scope, {})
            for uid, key, content, source, updated in facts:
                entry = {"text": content, "source": source, "timestamp": updated.timestamp()}
                target = store.setdefault(uid, {})
                if key not in target or entry["timestamp"] > target[key]["timestamp"]:
                    target[key] = entry
            vivian_memory_state.AI_MEMORY_LOADED.add(scope)
        except Exception as error:
            vivian_memory_state.AI_MEMORY_LOAD_RETRY[scope] = time.time() + 60
            print(f"[AI Memory] Archive load failed; channel={scope[1]}; error={error!r}", flush=True)


async def _ai_memory_write_unlocked(scope, record, facts) -> bool:
    if not vivian_memory_state.AI_MEMORY_READY or core_database.db_pool is None:
        return False
    try:
        cleaned = False
        async with asyncio.timeout(3), core_database.db_pool.connection() as connection, _memory_pipeline(connection):
            await connection.execute(
                """INSERT INTO elis_private.chat_messages
                (guild_id, channel_id, message_id, author_id, audience_id, role, display_name, content, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (guild_id, channel_id, message_id) DO NOTHING""",
                (*scope, record["message_id"], record["author_id"], record["audience_id"], record["role"],
                 record["name"], record["text"], datetime.fromtimestamp(record["timestamp"], core_config.MOSCOW_TIMEZONE)),
            )
            for key, fact in facts.items():
                await connection.execute(
                    """INSERT INTO elis_private.member_facts
                    (guild_id, channel_id, user_id, fact_key, content, source_message_id, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (guild_id, channel_id, user_id, fact_key) DO UPDATE SET
                    content = EXCLUDED.content, source_message_id = EXCLUDED.source_message_id,
                    updated_at = EXCLUDED.updated_at
                    WHERE member_facts.updated_at <= EXCLUDED.updated_at
                    AND member_facts.source_message_id <= EXCLUDED.source_message_id""",
                    (*scope, record["author_id"], key, fact, record["message_id"],
                     datetime.fromtimestamp(record["timestamp"], core_config.MOSCOW_TIMEZONE)),
                )
            if facts:
                await connection.execute(
                    """DELETE FROM elis_private.member_facts WHERE guild_id = %s AND channel_id = %s
                    AND user_id = %s AND fact_key IN (SELECT fact_key FROM elis_private.member_facts
                    WHERE guild_id = %s AND channel_id = %s AND user_id = %s AND content <> ''
                    ORDER BY updated_at DESC, fact_key OFFSET %s)""",
                    (*scope, record["author_id"], *scope, record["author_id"], vivian_memory_state.AI_MEMORY_FACT_LIMIT),
                )
            if time.time() - vivian_memory_state.AI_MEMORY_CLEANED_AT.get(scope, 0) > 3600:
                await connection.execute(
                    """DELETE FROM elis_private.chat_messages WHERE guild_id = %s AND channel_id = %s
                    AND (created_at < %s OR message_id IN (SELECT message_id FROM elis_private.chat_messages
                    WHERE guild_id = %s AND channel_id = %s
                    ORDER BY created_at DESC, message_id DESC OFFSET %s))""",
                    (*scope, datetime.now(core_config.MOSCOW_TIMEZONE) - timedelta(days=vivian_memory_state.AI_MEMORY_DAYS),
                     *scope, vivian_memory_state.AI_MEMORY_CHANNEL_LIMIT),
                )
                await connection.execute(
                    "DELETE FROM elis_private.member_facts WHERE guild_id = %s AND channel_id = %s AND updated_at < %s",
                    (*scope, datetime.now(core_config.MOSCOW_TIMEZONE) - timedelta(days=vivian_memory_state.AI_MEMORY_FACT_DAYS)),
                )
                cleaned = True
        if cleaned:
            vivian_memory_state.AI_MEMORY_CLEANED_AT[scope] = time.time()
        return True
    except Exception as error:
        print(f"[AI Memory] Save failed; channel={scope[1]}; message_id={record['message_id']}; error={error!r}", flush=True)
        return False


async def _ai_memory_save(scope, record, facts) -> None:
    key = (*scope, record["message_id"])
    if core_config.DATABASE_URL or core_database.db_pool is not None:
        vivian_memory_state.AI_MEMORY_PENDING[key] = (scope, dict(record), dict(facts))
        while len(vivian_memory_state.AI_MEMORY_PENDING) > 1000:
            vivian_memory_state.AI_MEMORY_PENDING.pop(next(iter(vivian_memory_state.AI_MEMORY_PENDING)))
    if await _ai_memory_write(scope, record, facts):
        vivian_memory_state.AI_MEMORY_PENDING.pop(key, None)


async def _ai_memory_load(scope) -> None:
    await controls_store.guarded_memory_load(_ai_memory_load_unlocked, scope)


async def _ai_memory_write(scope, record, facts) -> bool:
    return await controls_store.guarded_memory_write(_ai_memory_write_unlocked, scope, record, facts)


@tasks.loop(seconds=60)
async def ai_memory_flush_loop():
    
    if core_database.db_pool is None:
        return
    if not vivian_memory_state.AI_MEMORY_READY and time.time() - vivian_memory_state.AI_MEMORY_LAST_OPEN_ATTEMPT >= 300:
        await _open_ai_memory_storage()
    if not vivian_memory_state.AI_MEMORY_READY:
        return
    for key, (scope, record, facts) in list(vivian_memory_state.AI_MEMORY_PENDING.items())[:20]:
        if await _ai_memory_write(scope, record, facts):
            vivian_memory_state.AI_MEMORY_PENDING.pop(key, None)
        else:
            break
    if time.time() - vivian_memory_state.AI_MEMORY_LAST_MAINTENANCE < 3600:
        return
    try:
        async with asyncio.timeout(5), core_database.db_pool.connection() as connection:
            await connection.execute(
                "DELETE FROM elis_private.chat_messages WHERE created_at < %s",
                (datetime.now(core_config.MOSCOW_TIMEZONE) - timedelta(days=vivian_memory_state.AI_MEMORY_DAYS),),
            )
            await connection.execute(
                "DELETE FROM elis_private.member_facts WHERE updated_at < %s",
                (datetime.now(core_config.MOSCOW_TIMEZONE) - timedelta(days=vivian_memory_state.AI_MEMORY_FACT_DAYS),),
            )
        vivian_memory_state.AI_MEMORY_LAST_MAINTENANCE = time.time()
    except Exception as error:
        print(f"[AI Memory] Maintenance deferred: {error!r}", flush=True)


@ai_memory_flush_loop.before_loop
async def before_ai_memory_flush_loop():
    await core_config.bot.wait_until_ready()


async def _ai_memory_read_turn(scope, user_id: int, current_id: int, tokens: set[str]) -> list[dict]:
    """Read personal turns and matching older text beyond the warm channel cache."""
    if not vivian_memory_state.AI_MEMORY_READY or core_database.db_pool is None:
        return []
    rows = []
    try:
        async with asyncio.timeout(3), core_database.db_pool.connection() as connection:
            cutoff = datetime.now(core_config.MOSCOW_TIMEZONE) - timedelta(days=vivian_memory_state.AI_MEMORY_DAYS)
            personal_sql = """SELECT message_id, author_id, audience_id, role, display_name, content, created_at
                FROM elis_private.chat_messages WHERE guild_id = %s AND channel_id = %s
                AND audience_id = %s AND audience_id <> 0 AND message_id < %s AND created_at >= %s
                ORDER BY created_at DESC, message_id DESC LIMIT %s"""
            params = (*scope, user_id, current_id, cutoff, core_state.MAX_HISTORY)
            if tokens:
                # Both bounded branches retain their filters and ordering; only
                # the network roundtrip is shared. Duplicates merge by ID later.
                matching_sql = """SELECT message_id, author_id, audience_id, role, display_name, content, created_at
                    FROM elis_private.chat_messages WHERE guild_id = %s AND channel_id = %s
                    AND message_id < %s AND created_at >= %s AND content ILIKE ANY(%s)
                    ORDER BY created_at DESC, message_id DESC LIMIT 48"""
                query = f"({personal_sql}) UNION ALL ({matching_sql})"
                params += (*scope, current_id, cutoff, [f"%{token}%" for token in sorted(tokens)[:8]])
            else:
                query = personal_sql
            cursor = await connection.execute(query, params)
            rows.extend([row async for row in cursor])
    except Exception as error:
        print(f"[AI Memory] Turn retrieval failed; channel={scope[1]}; error={error!r}", flush=True)
    return [{"message_id": mid, "author_id": author, "audience_id": audience, "role": role,
             "name": name, "text": content, "timestamp": created.timestamp()}
            for mid, author, audience, role, name, content, created in rows]
