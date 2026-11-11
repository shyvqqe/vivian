"""Atomic room ownership and creation tokens; Discord reconciliation is external."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation

from core import community_store, database, rooms_schema
from core.community_store import CommunityUnavailable


READY_POOL = None
SCHEMA_LOCK = None
_MAX_ID = 9223372036854775807
_MAX_ROOMS = 10000


def is_ready() -> bool:
    pool = database.db_pool
    return (pool is not None and pool is READY_POOL
            and not getattr(pool, "closed", False) and community_store.is_ready())


async def initialize() -> bool:
    global READY_POOL, SCHEMA_LOCK
    if is_ready():
        return True
    if not await community_store.initialize():
        return False
    pool = database.db_pool
    if SCHEMA_LOCK is None:
        SCHEMA_LOCK = asyncio.Lock()
    async with SCHEMA_LOCK:
        if is_ready():
            return True
        try:
            async with asyncio.timeout(10), pool.connection() as connection:
                for statement in rooms_schema.SQL:
                    await connection.execute(statement)
            READY_POOL = pool
            return True
        except Exception as error:
            READY_POOL = None
            print(f"[Rooms] Initialization unavailable; error={type(error).__name__}", flush=True)
            return False


@asynccontextmanager
async def _cursor():
    if not await initialize():
        raise CommunityUnavailable("База комнат сейчас недоступна. Попробуй позже.")
    # community_store supplies a transaction and the same fail-closed DB handling.
    async with community_store._cursor() as cursor:
        yield cursor


def _id(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _MAX_ID:
        raise ValueError(f"Некорректный {name}.")
    return value


def _token(value):
    if not isinstance(value, str):
        raise ValueError("Некорректное подтверждение комнаты.")
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError):
        raise ValueError("Некорректное подтверждение комнаты.") from None
    if value != normalized:
        raise ValueError("Некорректное подтверждение комнаты.")
    return value


async def reserve(guild_id, owner_id) -> dict:
    params = (_id(guild_id, "сервер"), _id(owner_id, "владелец"), str(uuid4()))
    async with _cursor() as cursor:
        await cursor.execute(
            """INSERT INTO esil_internal.private_voice_rooms (guild_id,owner_id,state,token)
            VALUES (%s,%s,'creating',%s) ON CONFLICT (guild_id,owner_id) DO NOTHING
            RETURNING *""", params,
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("У тебя уже есть комната или она сейчас создаётся.")
        return row


async def activate(guild_id, owner_id, token, channel_id) -> dict:
    channel_id = _id(channel_id, "канал")
    params = (channel_id, _id(guild_id, "сервер"),
              _id(owner_id, "владелец"), _token(token), channel_id)
    async with _cursor() as cursor:
        try:
            await cursor.execute(
                """UPDATE esil_internal.private_voice_rooms
                SET channel_id=%s,state='active',updated_at=now()
                WHERE guild_id=%s AND owner_id=%s AND token=%s
                AND (state='creating' OR (state IN ('active','deleting') AND channel_id=%s))
                RETURNING *""", params,
            )
        except UniqueViolation:
            raise ValueError("Этот голосовой канал уже закреплён за другой комнатой.") from None
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("Создание комнаты больше не актуально.")
        return row


async def release(guild_id, owner_id, token=None) -> None:
    params = (_id(guild_id, "сервер"), _id(owner_id, "владелец"))
    async with _cursor() as cursor:
        if token is None:
            await cursor.execute(
                "DELETE FROM esil_internal.private_voice_rooms WHERE guild_id=%s AND owner_id=%s",
                params,
            )
        else:
            await cursor.execute(
                """DELETE FROM esil_internal.private_voice_rooms
                WHERE guild_id=%s AND owner_id=%s AND token=%s""", (*params, _token(token)),
            )


async def by_owner(guild_id, owner_id) -> dict | None:
    params = (_id(guild_id, "сервер"), _id(owner_id, "владелец"))
    async with _cursor() as cursor:
        await cursor.execute(
            "SELECT * FROM esil_internal.private_voice_rooms WHERE guild_id=%s AND owner_id=%s", params,
        )
        return await cursor.fetchone()


async def by_channel(guild_id, channel_id) -> dict | None:
    params = (_id(guild_id, "сервер"), _id(channel_id, "канал"))
    async with _cursor() as cursor:
        await cursor.execute(
            "SELECT * FROM esil_internal.private_voice_rooms WHERE guild_id=%s AND channel_id=%s", params,
        )
        return await cursor.fetchone()


async def list_rooms(guild_id=None) -> list:
    params = (_MAX_ROOMS + 1,)
    where = ""
    if guild_id is not None:
        where = "WHERE guild_id=%s"
        params = (_id(guild_id, "сервер"), *params)
    async with _cursor() as cursor:
        await cursor.execute(
            f"""SELECT * FROM esil_internal.private_voice_rooms {where}
            ORDER BY guild_id,owner_id LIMIT %s""", params,
        )
        rows = await cursor.fetchall()
        if len(rows) > _MAX_ROOMS:
            raise CommunityUnavailable("Слишком много комнат для безопасного восстановления.")
        return rows


async def transfer(guild_id, channel_id, new_owner_id) -> dict:
    guild_id, channel_id, new_owner_id = (_id(guild_id, "сервер"), _id(channel_id, "канал"),
                                         _id(new_owner_id, "новый владелец"))
    async with _cursor() as cursor:
        try:
            await cursor.execute(
                """UPDATE esil_internal.private_voice_rooms SET owner_id=%s,updated_at=now()
                WHERE guild_id=%s AND channel_id=%s AND state='active' RETURNING *""",
                (new_owner_id, guild_id, channel_id),
            )
        except UniqueViolation:
            raise ValueError("У нового владельца уже есть комната или она сейчас создаётся.") from None
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("Действующая комната на этом сервере не найдена.")
        return row


async def mark_deleting(guild_id, channel_id) -> dict | None:
    params = (_id(guild_id, "сервер"), _id(channel_id, "канал"))
    async with _cursor() as cursor:
        await cursor.execute(
            """UPDATE esil_internal.private_voice_rooms SET state='deleting',updated_at=now()
            WHERE guild_id=%s AND channel_id=%s AND state IN ('active','deleting') RETURNING *""",
            params,
        )
        return await cursor.fetchone()
