from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import re

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from core import database


_READY_POOL = None
_SCHEMA_LOCK = None
_MAX_BYTES = 512 * 1024
_SQL = (
    "CREATE SCHEMA IF NOT EXISTS vivian_private",
    "REVOKE ALL ON SCHEMA vivian_private FROM PUBLIC",
    """CREATE TABLE IF NOT EXISTS vivian_private.feature_records (
        guild_id BIGINT NOT NULL CHECK (guild_id > 0),
        namespace TEXT NOT NULL CHECK (char_length(namespace) BETWEEN 1 AND 64),
        key TEXT NOT NULL CHECK (char_length(key) BETWEEN 1 AND 180),
        payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (guild_id, namespace, key)
    )""",
    """CREATE INDEX IF NOT EXISTS feature_records_recent
        ON vivian_private.feature_records(guild_id, namespace, updated_at DESC, key)""",
    "ALTER TABLE vivian_private.feature_records ENABLE ROW LEVEL SECURITY",
    "REVOKE ALL ON vivian_private.feature_records FROM PUBLIC",
    """DO $vivian_features$ DECLARE role_name TEXT; BEGIN
        FOREACH role_name IN ARRAY ARRAY['anon','authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                EXECUTE format('REVOKE ALL ON SCHEMA vivian_private FROM %I', role_name);
                EXECUTE format('REVOKE ALL ON vivian_private.feature_records FROM %I', role_name);
            END IF;
        END LOOP;
    END $vivian_features$""",
)


class Unavailable(RuntimeError):
    pass


def identifier(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("A valid server or member identifier is required.")
    return value


def _namespace(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None:
        raise ValueError("Invalid feature namespace.")
    return value


def _key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 180 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid feature record identifier.")
    return value


def _document(value):
    if not isinstance(value, dict):
        raise ValueError("A record must be an object.")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > _MAX_BYTES:
            raise ValueError("This record has reached its storage limit.")
    except (TypeError, OverflowError, RecursionError):
        raise ValueError("This record cannot be stored.") from None
    return Jsonb(value)


def is_ready():
    pool = database.db_pool
    return pool is not None and pool is _READY_POOL and not getattr(pool, "closed", False)


async def initialize():
    global _READY_POOL, _SCHEMA_LOCK
    if is_ready():
        return True
    pool = database.db_pool
    if pool is None or getattr(pool, "closed", False):
        return False
    if _SCHEMA_LOCK is None:
        _SCHEMA_LOCK = asyncio.Lock()
    async with _SCHEMA_LOCK:
        if is_ready():
            return True
        try:
            async with asyncio.timeout(15), pool.connection() as connection:
                await connection.execute("SELECT pg_advisory_xact_lock(775716590728)")
                for statement in _SQL:
                    await connection.execute(statement)
            _READY_POOL = pool
            return True
        except Exception as error:
            print(f"[Features] Schema unavailable; error={type(error).__name__}", flush=True)
            _READY_POOL = None
            return False


@asynccontextmanager
async def cursor(guild_id=None):
    if guild_id is not None:
        identifier(guild_id)
    if not await initialize():
        raise Unavailable("Feature storage is unavailable. Configure DATABASE_URL and try again.")
    try:
        async with asyncio.timeout(15), database.db_pool.connection() as connection:
            await connection.execute("SET LOCAL lock_timeout = '7s'")
            await connection.execute("SET LOCAL statement_timeout = '10s'")
            if guild_id is not None:
                await connection.execute("SELECT pg_advisory_xact_lock(%s)", (guild_id,))
            async with connection.cursor(row_factory=dict_row) as current:
                yield current
    except (ValueError, Unavailable, PermissionError):
        raise
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"[Features] Transaction unavailable; error={type(error).__name__}", flush=True)
        raise Unavailable("The database did not confirm this action. Please try again.") from None


async def get(guild_id, namespace, key, default=None):
    identifier(guild_id)
    namespace, key = _namespace(namespace), _key(key)
    async with cursor() as current:
        await current.execute(
            "SELECT payload FROM vivian_private.feature_records WHERE guild_id=%s AND namespace=%s AND key=%s",
            (guild_id, namespace, key),
        )
        row = await current.fetchone()
        return deepcopy(row["payload"]) if row else deepcopy(default)


async def mutate_many(guild_id, namespace, keys, callback):
    identifier(guild_id)
    namespace = _namespace(namespace)
    if not isinstance(keys, (list, tuple)) or not 1 <= len(keys) <= 128:
        raise ValueError("Choose between 1 and 128 records.")
    keys = list(dict.fromkeys(_key(key) for key in keys))
    async with cursor(guild_id) as current:
        await current.execute(
            "SELECT key,payload FROM vivian_private.feature_records WHERE guild_id=%s AND namespace=%s AND key=ANY(%s) FOR UPDATE",
            (guild_id, namespace, keys),
        )
        rows = await current.fetchall()
        previous = {row["key"]: row["payload"] for row in rows}
        values = {key: deepcopy(previous.get(key)) for key in keys}
        result = callback(values)
        if asyncio.iscoroutine(result):
            result.close()
            raise ValueError("Storage callbacks must be synchronous.")
        if set(values) != set(keys):
            raise ValueError("A transaction cannot change its record keys.")
        encoded = {key: _document(value) for key, value in values.items() if value is not None}
        for key in keys:
            value = values[key]
            if value == previous.get(key):
                continue
            if value is None:
                await current.execute(
                    "DELETE FROM vivian_private.feature_records WHERE guild_id=%s AND namespace=%s AND key=%s",
                    (guild_id, namespace, key),
                )
            else:
                await current.execute(
                    """INSERT INTO vivian_private.feature_records(guild_id,namespace,key,payload)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(guild_id,namespace,key)
                    DO UPDATE SET payload=excluded.payload,updated_at=now()""",
                    (guild_id, namespace, key, encoded[key]),
                )
        return result


async def mutate(guild_id, namespace, key, callback, default=None):
    _key(key)

    def operation(values):
        if values[key] is None:
            values[key] = deepcopy(default) if default is not None else {}
        if not isinstance(values[key], dict):
            raise ValueError("A record must be an object.")
        return callback(values[key])

    return await mutate_many(guild_id, namespace, [key], operation)


async def put(guild_id, namespace, key, value):
    _document(value)

    def operation(values):
        values[key] = deepcopy(value)

    await mutate_many(guild_id, namespace, [key], operation)


async def delete(guild_id, namespace, key):
    def operation(values):
        existed = values[key] is not None
        values[key] = None
        return existed

    return await mutate_many(guild_id, namespace, [key], operation)


async def scan(guild_id, namespace, prefix="", limit=100, offset=0):
    identifier(guild_id)
    namespace = _namespace(namespace)
    if not isinstance(prefix, str) or len(prefix) > 180:
        raise ValueError("Invalid record prefix.")
    if type(limit) is not int or not 1 <= limit <= 5000 or type(offset) is not int or offset < 0:
        raise ValueError("Invalid pagination.")
    async with cursor() as current:
        await current.execute(
            """SELECT key,payload AS data,updated_at FROM vivian_private.feature_records
            WHERE guild_id=%s AND namespace=%s AND starts_with(key,%s)
            ORDER BY updated_at DESC,key LIMIT %s OFFSET %s""",
            (guild_id, namespace, prefix, limit, offset),
        )
        return await current.fetchall()


async def count(guild_id, namespace, prefix=""):
    identifier(guild_id)
    namespace = _namespace(namespace)
    if not isinstance(prefix, str) or len(prefix) > 180:
        raise ValueError("Invalid record prefix.")
    async with cursor() as current:
        await current.execute(
            "SELECT count(*) AS total FROM vivian_private.feature_records WHERE guild_id=%s AND namespace=%s AND starts_with(key,%s)",
            (guild_id, namespace, prefix),
        )
        return (await current.fetchone())["total"]
