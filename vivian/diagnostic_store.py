"""One private, bounded diagnostic case per member and channel."""
from __future__ import annotations

import asyncio
import copy
import time
from collections import OrderedDict

from psycopg.types.json import Jsonb

from core import database

CASES = OrderedDict()
LOCKS = {}
READY_POOL = None
SCHEMA_LOCK = None
RETENTION_SECONDS = 30 * 86400
MAX_CASES = 256
SCHEMA_SQL = (
    "CREATE SCHEMA IF NOT EXISTS elis_private",
    "REVOKE ALL ON SCHEMA elis_private FROM PUBLIC",
    """CREATE TABLE IF NOT EXISTS elis_private.diagnostic_cases (
        guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
        payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (guild_id, channel_id, user_id)
    )""",
    "ALTER TABLE elis_private.diagnostic_cases ENABLE ROW LEVEL SECURITY",
    "REVOKE ALL ON elis_private.diagnostic_cases FROM PUBLIC",
    """DO $vivian_diagnostics$ DECLARE api_role TEXT; BEGIN
        FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
                EXECUTE format('REVOKE ALL ON SCHEMA elis_private FROM %I', api_role);
                EXECUTE format('REVOKE ALL ON elis_private.diagnostic_cases FROM %I', api_role);
            END IF;
        END LOOP;
    END $vivian_diagnostics$""",
)


def case_key(message):
    return (message.guild.id, message.channel.id, message.author.id)


def case_lock(key):
    if key not in LOCKS:
        if len(LOCKS) >= MAX_CASES * 2:
            for old, lock in list(LOCKS.items()):
                if not lock.locked() and old not in CASES:
                    LOCKS.pop(old, None)
        LOCKS[key] = asyncio.Lock()
    return LOCKS[key]


def cache_case(key, case):
    CASES[key] = copy.deepcopy(case)
    CASES.move_to_end(key)
    while len(CASES) > MAX_CASES:
        CASES.popitem(last=False)


async def _ready():
    global READY_POOL, SCHEMA_LOCK
    pool = database.db_pool
    if pool is None:
        return False
    if READY_POOL is pool:
        return True
    if SCHEMA_LOCK is None:
        SCHEMA_LOCK = asyncio.Lock()
    async with SCHEMA_LOCK:
        if READY_POOL is pool:
            return True
        try:
            async with asyncio.timeout(4), pool.connection() as connection:
                for sql in SCHEMA_SQL:
                    await connection.execute(sql)
                await connection.execute(
                    "DELETE FROM elis_private.diagnostic_cases WHERE updated_at < now() - interval '30 days'"
                )
            READY_POOL = pool
        except Exception as error:
            print(f"[Diagnosis] Storage unavailable: {error!r}", flush=True)
            return False
    return True


async def load_case(key):
    case = CASES.get(key)
    if case is not None:
        if time.time() - case.get("updated", 0) <= RETENTION_SECONDS:
            CASES.move_to_end(key)
            return copy.deepcopy(case)
        CASES.pop(key, None)
    if not await _ready():
        return None
    try:
        async with asyncio.timeout(3), database.db_pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT payload FROM elis_private.diagnostic_cases WHERE guild_id=%s AND channel_id=%s "
                "AND user_id=%s AND updated_at >= now() - interval '30 days'", key,
            )
            row = await cursor.fetchone()
        if row and isinstance(row[0], dict):
            cache_case(key, row[0])
            return copy.deepcopy(row[0])
    except Exception as error:
        print(f"[Diagnosis] Read failed: {error!r}", flush=True)
    return None


async def save_case(key, case):
    cache_case(key, case)
    if not await _ready():
        return False
    try:
        async with asyncio.timeout(3), database.db_pool.connection() as connection:
            await connection.execute(
                """INSERT INTO elis_private.diagnostic_cases (guild_id, channel_id, user_id, payload)
                VALUES (%s,%s,%s,%s) ON CONFLICT (guild_id, channel_id, user_id) DO UPDATE SET
                payload=EXCLUDED.payload, updated_at=now()
                WHERE (diagnostic_cases.payload->>'revision')::bigint <= (EXCLUDED.payload->>'revision')::bigint""",
                (*key, Jsonb(case)),
            )
        return True
    except Exception as error:
        print(f"[Diagnosis] Save failed: {error!r}", flush=True)
        return False
