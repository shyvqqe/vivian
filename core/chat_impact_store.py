"""Private, idempotent storage for approved chat interaction metadata."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import time
from core import config, database
from core import chat_impact_state as state

READY = False
LAST_OPEN_ATTEMPT = 0.0
LAST_CLEANUP = 0.0
BATCH_SIZE = 20
STORE_LOCK = asyncio.Lock()
SCHEMA_SQL = (
    "CREATE SCHEMA IF NOT EXISTS elis_private",
    "REVOKE ALL ON SCHEMA elis_private FROM PUBLIC, anon, authenticated",
    """CREATE TABLE IF NOT EXISTS elis_private.chat_impact_totals (
        guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
        sparks BIGINT NOT NULL DEFAULT 0, newcomer_replies BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id))""",
    """CREATE TABLE IF NOT EXISTS elis_private.chat_impact_events (
        guild_id BIGINT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('spark', 'newcomer')),
        actor_id BIGINT NOT NULL, target_id BIGINT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (guild_id, kind, actor_id, target_id))""",
    """CREATE INDEX IF NOT EXISTS chat_impact_events_day_idx
        ON elis_private.chat_impact_events (guild_id, occurred_at)""",
    """CREATE TABLE IF NOT EXISTS elis_private.chat_impact_windows (
        guild_id BIGINT PRIMARY KEY, last_id BIGINT NOT NULL, last_at TIMESTAMPTZ,
        candidate_id BIGINT, candidate_author BIGINT, candidate_at TIMESTAMPTZ,
        responders BIGINT[] NOT NULL DEFAULT '{}')""",
    "ALTER TABLE elis_private.chat_impact_totals ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE elis_private.chat_impact_events ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE elis_private.chat_impact_windows ENABLE ROW LEVEL SECURITY",
    """REVOKE ALL ON elis_private.chat_impact_totals,
        elis_private.chat_impact_events, elis_private.chat_impact_windows
        FROM PUBLIC, anon, authenticated""",
)
EVENT_SQL = """WITH inserted AS (
    INSERT INTO elis_private.chat_impact_events (guild_id, kind, actor_id, target_id, occurred_at)
    VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING guild_id, kind, actor_id
) INSERT INTO elis_private.chat_impact_totals (guild_id, user_id, sparks, newcomer_replies)
SELECT guild_id, actor_id, (kind = 'spark')::int, (kind = 'newcomer')::int FROM inserted
ON CONFLICT (guild_id, user_id) DO UPDATE SET
    sparks = chat_impact_totals.sparks + EXCLUDED.sparks,
    newcomer_replies = chat_impact_totals.newcomer_replies + EXCLUDED.newcomer_replies"""
WINDOW_SQL = """INSERT INTO elis_private.chat_impact_windows
    (guild_id, last_id, last_at, candidate_id, candidate_author, candidate_at, responders)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (guild_id) DO UPDATE SET last_id = EXCLUDED.last_id, last_at = EXCLUDED.last_at,
        candidate_id = EXCLUDED.candidate_id, candidate_author = EXCLUDED.candidate_author,
        candidate_at = EXCLUDED.candidate_at, responders = EXCLUDED.responders
    WHERE chat_impact_windows.last_id <= EXCLUDED.last_id"""


def _datetime(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc) if timestamp is not None else None


def event_parameters(event):
    return (event["guild_id"], event["kind"], event["actor_id"], event["target_id"], _datetime(event["at"]))


def window_parameters(guild_id, window):
    candidate = window["candidate"] or {}
    return (guild_id, window["last_id"], _datetime(window["last_at"]), candidate.get("message_id"),
            candidate.get("author_id"), _datetime(candidate.get("at")), sorted(candidate.get("responders", ())))


async def _load_snapshots(connection, load_seen=False):
    totals_cursor = await connection.execute("SELECT guild_id, user_id, sparks, newcomer_replies FROM elis_private.chat_impact_totals")
    totals = {(gid, uid): {"sparks": sparks, "newcomer_replies": replies} async for gid, uid, sparks, replies in totals_cursor}
    first_day = datetime.now(config.MOSCOW_TIMEZONE).date() - timedelta(days=2)
    daily_cursor = await connection.execute(
        """SELECT (occurred_at AT TIME ZONE 'Europe/Moscow')::date, guild_id, actor_id,
            COUNT(*) FILTER (WHERE kind = 'spark'), COUNT(*) FILTER (WHERE kind = 'newcomer')
            FROM elis_private.chat_impact_events WHERE occurred_at >= %s
            GROUP BY 1, 2, 3""", (datetime.combine(first_day, datetime.min.time(), config.MOSCOW_TIMEZONE),))
    daily = {(str(day), gid, uid): {"sparks": sparks, "newcomer_replies": replies}
             async for day, gid, uid, sparks, replies in daily_cursor}
    windows_cursor = await connection.execute(
        "SELECT guild_id, last_id, last_at, candidate_id, candidate_author, candidate_at, responders FROM elis_private.chat_impact_windows")
    windows = [row async for row in windows_cursor]
    seen = []
    if load_seen:
        cursor = await connection.execute(
            "SELECT guild_id, kind, actor_id, target_id FROM elis_private.chat_impact_events "
            "ORDER BY occurred_at DESC LIMIT %s", (state.SEEN_LIMIT,))
        seen = [tuple(row) async for row in cursor]
    return totals, daily, windows, seen


def _merge_snapshots(totals, daily, windows, seen):
    state.TOTALS.clear()
    state.TOTALS.update(totals)
    state.DAILY.clear()
    state.DAILY.update(daily)
    # Uncommitted events remain visible in RAM and are retried idempotently.
    for event in state.PENDING.values():
        state.apply_event(event)
    for key in reversed(seen):
        state.mark_seen(key)
    for gid, last_id, last_at, cid, author, at, responders in windows:
        if gid not in state.WINDOWS or state.WINDOWS[gid]["last_id"] < last_id:
            state.WINDOWS[gid] = {"last_id": last_id, "last_at": last_at.timestamp() if last_at else None,
                "candidate": {"message_id": cid, "author_id": author, "at": at.timestamp(), "responders": set(responders)} if cid and at else None}
    state.prune_daily()


async def _store_pending(connection):
    pending = dict(list(state.PENDING.items())[:BATCH_SIZE])
    dirty = {gid: window_parameters(gid, state.WINDOWS[gid]) for gid in state.DIRTY_WINDOWS}
    for event in pending.values():
        await connection.execute(EVENT_SQL, event_parameters(event))
    for parameters in dirty.values():
        await connection.execute(WINDOW_SQL, parameters)
    cleanup = time.time() if time.time() - LAST_CLEANUP >= 60 * 60 else 0.0
    if cleanup:
        await connection.execute("DELETE FROM elis_private.chat_impact_events WHERE occurred_at < %s",
                                 (datetime.now(timezone.utc) - timedelta(days=state.RETENTION_DAYS),))
    return pending, dirty, cleanup


def _acknowledge(pending, dirty, cleanup):
    global LAST_CLEANUP
    for key, event in pending.items():
        if state.PENDING.get(key) == event:
            state.PENDING.pop(key, None)
    for gid, parameters in dirty.items():
        if window_parameters(gid, state.WINDOWS[gid]) == parameters:
            state.DIRTY_WINDOWS.discard(gid)
    if cleanup:
        LAST_CLEANUP = cleanup
    state.prune_daily()


async def initialize(pool=None) -> bool:
    global READY, LAST_OPEN_ATTEMPT
    pool = pool or database.db_pool
    if pool is None:
        return False
    async with STORE_LOCK:
        if READY:
            return True
        LAST_OPEN_ATTEMPT = time.time()
        try:
            async with asyncio.timeout(8), pool.connection() as connection:
                for sql in SCHEMA_SQL:
                    await connection.execute(sql)
                pending, dirty, cleanup = await _store_pending(connection)
                snapshots = await _load_snapshots(connection, load_seen=True)
            _acknowledge(pending, dirty, cleanup)
            _merge_snapshots(*snapshots)
            READY = True
            print("[Chat impact] Private conversation counters restored.", flush=True)
            return True
        except Exception as error:
            print(f"[Chat impact] Storage unavailable; using RAM: {error!r}", flush=True)
            return False


async def flush_pending() -> None:
    state.prune_daily()
    if database.db_pool is None:
        return
    if not READY:
        if time.time() - LAST_OPEN_ATTEMPT >= 60:
            await initialize()
        return
    if not state.PENDING and not state.DIRTY_WINDOWS and time.time() - LAST_CLEANUP < 60 * 60:
        return
    async with STORE_LOCK:
        try:
            async with asyncio.timeout(3), database.db_pool.connection() as connection:
                pending, dirty, cleanup = await _store_pending(connection)
                # Reload only when a counter changed; normal chat activity saves just its window.
                snapshots = await _load_snapshots(connection) if pending else None
            _acknowledge(pending, dirty, cleanup)
            if snapshots is not None:
                _merge_snapshots(*snapshots)
        except Exception as error:
            print(f"[Chat impact] Deferred counter write: {error!r}", flush=True)
