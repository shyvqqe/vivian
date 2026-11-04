from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import os

from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import AsyncConnectionPool

from core import config as core_config, feature_storage, stats_storage


db_pool = None
_backend = None
_retry_task = None
_stats_deferred = False

_MUSIC_TABLES = (
    """CREATE TABLE IF NOT EXISTS public.music_playlists (
        guild_id BIGINT NOT NULL,
        owner_id BIGINT NOT NULL,
        name TEXT NOT NULL,
        tracks JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (guild_id, owner_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS public.music_player_panels (
        guild_id BIGINT PRIMARY KEY,
        channel_id BIGINT NOT NULL,
        message_id BIGINT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
)


def public_status() -> dict:
    return {"backend": _backend, "stats_writes_deferred": _stats_deferred}


def is_ready() -> bool:
    return _backend is not None


async def _write_postgres(connection, rows):
    for sql, row in zip(stats_storage.POSTGRES_UPSERTS, rows):
        if row is not None:
            await connection.execute(sql, row)


async def _flush_pending(pool) -> bool:
    batch = await stats_storage.pending_batch()
    if not batch:
        return False
    async with asyncio.timeout(10), pool.connection() as connection, connection.pipeline():
        # Acquire row locks in the same table order as normal message writes.
        for index, sql in enumerate(stats_storage.POSTGRES_UPSERTS):
            for _, rows in batch:
                if rows[index] is not None:
                    await connection.execute(sql, rows[index])
    # UPSERT keeps the largest snapshot, so replay after an interrupted
    # acknowledgement is safe even if newer counters have already committed.
    await stats_storage.acknowledge(batch[-1][0])
    return True


async def _retry_pending(pool):
    global _stats_deferred
    while True:
        await asyncio.sleep(30)
        try:
            while await _flush_pending(pool):
                pass
            _stats_deferred = False
        except Exception as error:
            print(f"[Storage] Deferred stats still pending ({type(error).__name__}).", flush=True)


async def _open_persistent_storage() -> None:
    """Initialize storage and restore counters before accepting gateway events."""
    global db_pool, _backend, _retry_task
    if _backend is not None:
        return

    today = datetime.now(core_config.SUMMARY_TIMEZONE).date()
    first_day, cutoff = today - timedelta(days=2), today - timedelta(days=90)
    if not core_config.DATABASE_URL:
        if os.getenv("RENDER", "").lower() == "true":
            raise RuntimeError("Set DATABASE_URL to a writable PostgreSQL connection string in Render.")
        await feature_storage.initialize()
        rows = await feature_storage.run_local(
            lambda connection: stats_storage.init_sqlite(connection, first_day, cutoff)
        )
        stats_storage.restore(rows)
        _backend = "sqlite"
        print("[Storage] SQLite ready; activity and XP restored.", flush=True)
        return

    connection_params = conninfo_to_dict(core_config.DATABASE_URL)
    pool = AsyncConnectionPool(
        conninfo=core_config.DATABASE_URL,
        min_size=1,
        max_size=3,
        kwargs={"sslmode": connection_params.get("sslmode", "require"), "prepare_threshold": None},
        timeout=10,
        open=False,
    )
    try:
        await pool.open(wait=True, timeout=10)
        async with asyncio.timeout(10), pool.connection() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(5674981742901)")
            for table, _, _, sql in stats_storage.TABLES:
                await connection.execute(sql)
                await connection.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            for sql in _MUSIC_TABLES:
                await connection.execute(sql)
        while await _flush_pending(pool):
            pass
        async with asyncio.timeout(10), pool.connection() as connection:
            await connection.execute("DELETE FROM daily_activity WHERE activity_date < %s", (cutoff,))
            rows = []
            for sql, params in stats_storage.select_queries(first_day, "%s"):
                cursor = await connection.execute(sql, params)
                rows.append(await cursor.fetchall())
        await feature_storage.initialize()
        stats_storage.restore(rows)
    except BaseException:
        await pool.close()
        await feature_storage.close()
        raise

    db_pool = pool
    _backend = "postgres"
    _retry_task = asyncio.create_task(_retry_pending(pool), name="stats-storage-retry")
    print("[Storage] PostgreSQL ready; activity and XP restored.", flush=True)

    try:
        from core import chat_impact_store
        await chat_impact_store.initialize(pool)
    except Exception as error:
        print(f"[Storage] Chat impact initialization deferred ({type(error).__name__}).", flush=True)


async def _persist_stats_snapshot(
    guild_id: int | None,
    user_id: int,
    activity_date,
    rank_stats: dict | None,
    daily_stats: dict | None,
    user_xp: dict,
) -> None:
    global _stats_deferred
    rows = stats_storage.snapshot_rows(guild_id, user_id, activity_date, rank_stats, daily_stats, user_xp)
    if _backend == "sqlite":
        await feature_storage.run_local(lambda connection: stats_storage.save_sqlite(connection, rows))
        return
    pool = db_pool
    if pool is None:
        raise RuntimeError("Activity storage is not initialized.")
    try:
        async with asyncio.timeout(5), pool.connection() as connection:
            await _write_postgres(connection, rows)
    except Exception as error:
        await stats_storage.defer(rows)
        _stats_deferred = True
        print(f"[Storage] Activity saved for retry ({type(error).__name__}).", flush=True)


async def close() -> None:
    global db_pool, _backend, _retry_task, _stats_deferred
    if _retry_task is not None:
        _retry_task.cancel()
        await asyncio.gather(_retry_task, return_exceptions=True)
        _retry_task = None
    if db_pool is not None:
        try:
            async with asyncio.timeout(5):
                while await _flush_pending(db_pool):
                    pass
        except Exception as error:
            print(f"[Storage] Deferred stats retained on disk ({type(error).__name__}).", flush=True)
        await db_pool.close()
        db_pool = None
    await feature_storage.close()
    _backend = None
    _stats_deferred = False
