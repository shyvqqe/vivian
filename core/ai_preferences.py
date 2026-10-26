"""Personal model choices stored privately through the existing Postgres pool."""
from __future__ import annotations

import asyncio
from weakref import WeakValueDictionary

from core import database

_ready_pool = None
_loaded_pool = None
_schema_lock = None
_selection_locks = WeakValueDictionary()


def selection_lock(guild_id, user_id):
    """Serialize a person's changes across panels without retaining idle locks."""
    key = (int(guild_id), int(user_id))
    lock = _selection_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _selection_locks[key] = lock
    return lock


SCHEMA_SQL = (
    "CREATE SCHEMA IF NOT EXISTS vivian_private",
    "REVOKE ALL ON SCHEMA vivian_private FROM PUBLIC",
    """CREATE TABLE IF NOT EXISTS vivian_private.ai_preferences (
        guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
        model_id TEXT NOT NULL CHECK (length(model_id) BETWEEN 1 AND 200),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (guild_id, user_id)
    )""",
    "ALTER TABLE vivian_private.ai_preferences ENABLE ROW LEVEL SECURITY",
    "REVOKE ALL ON vivian_private.ai_preferences FROM PUBLIC",
    """DO $vivian_preferences$ DECLARE api_role TEXT; BEGIN
        FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
                EXECUTE format('REVOKE ALL ON SCHEMA vivian_private FROM %I', api_role);
                EXECUTE format('REVOKE ALL ON vivian_private.ai_preferences FROM %I', api_role);
            END IF;
        END LOOP;
    END $vivian_preferences$""",
)


async def _ready():
    global _ready_pool, _schema_lock
    pool = database.db_pool
    if pool is None:
        return None
    if _ready_pool is pool:
        return pool
    if _schema_lock is None:
        _schema_lock = asyncio.Lock()
    async with _schema_lock:
        if _ready_pool is pool:
            return pool
        try:
            async with asyncio.timeout(4), pool.connection() as connection:
                for sql in SCHEMA_SQL:
                    await connection.execute(sql)
        except Exception as error:
            print(f"[AI Preferences] Storage unavailable; error={type(error).__name__}", flush=True)
            return None
        _ready_pool = pool
    return pool


async def save_preference(guild_id, user_id, model_id):
    from core import ai_models
    if model_id is not None and model_id not in ai_models.MODELS:
        return False
    pool = await _ready()
    if pool is None:
        return False
    try:
        async with asyncio.timeout(4), pool.connection() as connection:
            if model_id is None:
                await connection.execute(
                    "DELETE FROM vivian_private.ai_preferences WHERE guild_id=%s AND user_id=%s",
                    (guild_id, user_id),
                )
            else:
                await connection.execute(
                    """INSERT INTO vivian_private.ai_preferences (guild_id, user_id, model_id)
                    VALUES (%s, %s, %s) ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET model_id=EXCLUDED.model_id, updated_at=now()""",
                    (guild_id, user_id, model_id),
                )
        return True
    except Exception as error:
        print(f"[AI Preferences] Save unavailable; error={type(error).__name__}", flush=True)
        return False


async def load_preferences():
    global _loaded_pool
    from core import ai_models
    pool = await _ready()
    if pool is None or _loaded_pool is pool:
        return
    try:
        async with asyncio.timeout(4), pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT guild_id, user_id, model_id FROM vivian_private.ai_preferences "
                "ORDER BY updated_at DESC LIMIT 10000"
            )
            rows = await cursor.fetchall()
        for guild_id, user_id, model_id in reversed(rows):
            async with selection_lock(guild_id, user_id):
                if not ai_models.was_changed(guild_id, user_id):
                    ai_models.restore_selection(guild_id, user_id, model_id)
        _loaded_pool = pool
    except Exception as error:
        print(f"[AI Preferences] Restore unavailable; error={type(error).__name__}", flush=True)
