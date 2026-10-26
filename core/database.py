from __future__ import annotations
from psycopg_pool import AsyncConnectionPool
from datetime import datetime
from datetime import timedelta
from core import config as core_config
from core import state as core_state


DATABASE_URL = core_config.DATABASE_URL
db_pool = None


async def _open_persistent_storage() -> None:
    """Connect to Supabase Postgres and restore durable rank/activity counters."""
    global db_pool
    if not core_config.DATABASE_URL:
        print("[Storage] DATABASE_URL is missing; stats remain in memory only.", flush=True)
        return
    if db_pool is not None:
        return

    pool = AsyncConnectionPool(
        conninfo=core_config.DATABASE_URL,
        min_size=1,
        max_size=3,
        kwargs={"sslmode": "require"},
        timeout=10,
        open=False,
    )
    try:
        await pool.open(wait=True)
        async with pool.connection() as connection:
            await connection.execute(
                """CREATE TABLE IF NOT EXISTS member_activity (
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    messages BIGINT NOT NULL DEFAULT 0,
                    word_messages BIGINT NOT NULL DEFAULT 0,
                    long_messages BIGINT NOT NULL DEFAULT 0,
                    characters BIGINT NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )"""
            )
            await connection.execute(
                """CREATE TABLE IF NOT EXISTS daily_activity (
                    activity_date DATE NOT NULL,
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    messages BIGINT NOT NULL DEFAULT 0,
                    profanity BIGINT NOT NULL DEFAULT 0,
                    PRIMARY KEY (activity_date, guild_id, user_id)
                )"""
            )
            await connection.execute(
                """CREATE TABLE IF NOT EXISTS user_xp (
                    user_id BIGINT PRIMARY KEY,
                    xp BIGINT NOT NULL DEFAULT 0,
                    level INTEGER NOT NULL DEFAULT 1
                )"""
            )
            await connection.execute(
                """CREATE TABLE IF NOT EXISTS public.music_playlists (
                    guild_id BIGINT NOT NULL,
                    owner_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    tracks JSONB NOT NULL DEFAULT '[]'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (guild_id, owner_id, name)
                )"""
            )
            await connection.execute(
                """CREATE TABLE IF NOT EXISTS public.music_player_panels (
                    guild_id BIGINT PRIMARY KEY,
                    channel_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )"""
            )
            await connection.execute(
                "DELETE FROM daily_activity WHERE activity_date < %s",
                (datetime.now(core_config.SUMMARY_TIMEZONE).date() - timedelta(days=90),),
            )

            cursor = await connection.execute(
                "SELECT guild_id, user_id, messages, word_messages, long_messages, characters "
                "FROM member_activity"
            )
            async for row in cursor:
                guild_id, user_id, messages, word_messages, long_messages, characters = row
                core_state.RANK_ACTIVITY[(guild_id, user_id)] = {
                    "messages": messages,
                    "word_messages": word_messages,
                    "long_messages": long_messages,
                    "characters": characters,
                }

            first_day = datetime.now(core_config.SUMMARY_TIMEZONE).date() - timedelta(days=2)
            cursor = await connection.execute(
                "SELECT activity_date, guild_id, user_id, messages, profanity "
                "FROM daily_activity WHERE activity_date >= %s",
                (first_day,),
            )
            async for row in cursor:
                activity_date, guild_id, user_id, messages, profanity = row
                core_state.DAILY_ACTIVITY[(activity_date, guild_id, user_id)] = {
                    "messages": messages,
                    "profanity": profanity,
                }

            cursor = await connection.execute("SELECT user_id, xp, level FROM user_xp")
            async for user_id, xp, level in cursor:
                core_state.xp_data[user_id] = {"xp": xp, "level": level}

        db_pool = pool
        from core import chat_impact_store
        await chat_impact_store.initialize(pool)
        print("[Storage] Supabase Postgres connected; activity and XP restored.", flush=True)
    except Exception:
        await pool.close()
        raise


async def _persist_stats_snapshot(
    guild_id: int | None,
    user_id: int,
    activity_date,
    rank_stats: dict | None,
    daily_stats: dict | None,
    user_xp: dict,
) -> None:
    if db_pool is None:
        return
    try:
        async with db_pool.connection() as connection:
            if guild_id is not None and rank_stats is not None and daily_stats is not None:
                await connection.execute(
                    """INSERT INTO member_activity
                        (guild_id, user_id, messages, word_messages, long_messages, characters)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (guild_id, user_id) DO UPDATE SET
                        messages = GREATEST(member_activity.messages, EXCLUDED.messages),
                        word_messages = GREATEST(member_activity.word_messages, EXCLUDED.word_messages),
                        long_messages = GREATEST(member_activity.long_messages, EXCLUDED.long_messages),
                        characters = GREATEST(member_activity.characters, EXCLUDED.characters)""",
                    (
                        guild_id, user_id, rank_stats["messages"],
                        rank_stats["word_messages"], rank_stats["long_messages"],
                        rank_stats["characters"],
                    ),
                )
                await connection.execute(
                    """INSERT INTO daily_activity
                        (activity_date, guild_id, user_id, messages, profanity)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (activity_date, guild_id, user_id) DO UPDATE SET
                        messages = GREATEST(daily_activity.messages, EXCLUDED.messages),
                        profanity = GREATEST(daily_activity.profanity, EXCLUDED.profanity)""",
                    (
                        activity_date, guild_id, user_id,
                        daily_stats["messages"], daily_stats["profanity"],
                    ),
                )
            await connection.execute(
                """INSERT INTO user_xp (user_id, xp, level)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    xp = GREATEST(user_xp.xp, EXCLUDED.xp),
                    level = GREATEST(user_xp.level, EXCLUDED.level)""",
                (user_id, user_xp["xp"], user_xp["level"]),
            )
    except Exception as error:
        print(
            f"[Storage] Failed to persist stats for user {user_id}: {error!r}",
            flush=True,
        )
