from __future__ import annotations

import time

from discord.ext import tasks

from core import feature_storage


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS ai_memories (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        fact TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id, fact)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS ai_conversations (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS ai_conversations_time ON ai_conversations(guild_id, user_id, created_at)")


async def save_fact(guild_id: int, user_id: int, fact: str):
    def work(connection):
        _schema(connection)
        connection.execute(
            "INSERT OR IGNORE INTO ai_memories (guild_id, user_id, fact, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, fact[:500], time.time()),
        )
    try:
        await feature_storage.run(work)
    except Exception:
        pass


async def get_facts(guild_id: int, user_id: int):
    def work(connection):
        _schema(connection)
        rows = connection.execute(
            "SELECT fact FROM ai_memories WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT 20",
            (guild_id, user_id),
        ).fetchall()
        return [r["fact"] for r in rows]
    try:
        return await feature_storage.run(work)
    except Exception:
        return []


@tasks.loop(minutes=30)
async def ai_memory_flush_loop():
    pass


@ai_memory_flush_loop.before_loop
async def before_ai_memory_flush():
    from core import config as core_config
    await core_config.bot.wait_until_ready()
