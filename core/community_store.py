"""Guild-scoped community records. A failed database never becomes a RAM save."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from core import community_schema as schema
from core import database


READY_POOL = None
SCHEMA_LOCK = None
_MAX_ID = 9223372036854775807
_COUNTS_SQL = """SELECT
    count(*) FILTER (WHERE v.value=1)::integer AS up,
    count(*) FILTER (WHERE v.value=-1)::integer AS down,
    coalesce(max(v.value) FILTER (WHERE v.user_id=%s),0)::integer AS mine
    FROM esil_internal.community_items AS i
    LEFT JOIN esil_internal.community_votes AS v
      ON v.guild_id=i.guild_id AND v.item_id=i.id
    WHERE i.guild_id=%s AND i.id=%s AND i.kind='suggestion'
    GROUP BY i.guild_id,i.id"""


class CommunityUnavailable(RuntimeError):
    """The requested operation could not be committed or read durably."""


def is_ready() -> bool:
    pool = database.db_pool
    return pool is not None and pool is READY_POOL and not getattr(pool, "closed", False)


async def initialize() -> bool:
    global READY_POOL, SCHEMA_LOCK
    if is_ready():
        return True
    pool = database.db_pool
    if pool is None or getattr(pool, "closed", False):
        return False
    if SCHEMA_LOCK is None:
        SCHEMA_LOCK = asyncio.Lock()
    async with SCHEMA_LOCK:
        if is_ready():
            return True
        try:
            async with asyncio.timeout(10), pool.connection() as connection:
                for statement in schema.SQL:
                    await connection.execute(statement)
            READY_POOL = pool
            return True
        except Exception as error:
            READY_POOL = None
            print(f"[Community] Initialization unavailable; error={type(error).__name__}", flush=True)
            return False


@asynccontextmanager
async def _cursor():
    global READY_POOL
    if not await initialize():
        raise CommunityUnavailable("База сообщества сейчас недоступна. Ничего не сохранила.")
    pool = database.db_pool
    try:
        async with asyncio.timeout(8), pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                yield cursor
    except (CommunityUnavailable, ValueError):
        raise
    except Exception as error:
        if READY_POOL is pool:
            READY_POOL = None
        print(f"[Community] Storage unavailable; error={type(error).__name__}", flush=True)
        raise CommunityUnavailable("Не удалось подтвердить операцию в базе. Попробуй позже.") from None


def _id(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _MAX_ID:
        raise ValueError(f"Некорректный {name}.")
    return value


def _kind(kind):
    if not isinstance(kind, str) or kind not in schema.STATUSES:
        raise ValueError("Неизвестный тип записи.")
    return kind


def _text(value, name, limit, *, empty=False):
    if not isinstance(value, str):
        raise ValueError(f"{name}: нужен текст.")
    value = value.strip()
    if len(value) > limit or (not value and not empty):
        raise ValueError(f"{name}: допустимо от {0 if empty else 1} до {limit} символов.")
    return value


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("Сведения должны быть JSON-объектом.")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Сведения должны содержать только JSON-значения.") from None
    if len(encoded.encode("utf-8")) > 262144:
        raise ValueError("Слишком большой объём дополнительных сведений.")
    return Jsonb(value)


async def create_item(guild_id, kind, author_id, title, body="", *, details=None,
                      status=None, seed_key=None) -> dict:
    guild_id, author_id = _id(guild_id, "сервер"), _id(author_id, "автор")
    kind = _kind(kind)
    title, body = _text(title, "Заголовок", 200), _text(body, "Описание", 6000, empty=True)
    status = schema.DEFAULT_STATUS[kind] if status is None else status
    if status not in schema.STATUSES[kind]:
        raise ValueError("Этот статус не подходит для выбранного типа записи.")
    if seed_key is not None:
        seed_key = _text(seed_key, "Идентификатор исходной записи", 160)
    payload = _object({} if details is None else details)
    async with _cursor() as cursor:
        await cursor.execute(
            """INSERT INTO esil_internal.community_items
            (guild_id,kind,author_id,title,body,status,details,seed_key,updated_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (guild_id,kind,seed_key) DO NOTHING RETURNING *""",
            (guild_id, kind, author_id, title, body, status, payload, seed_key, author_id),
        )
        row = await cursor.fetchone()
        if row is None and seed_key is not None:
            await cursor.execute(
                "SELECT * FROM esil_internal.community_items WHERE guild_id=%s AND kind=%s AND seed_key=%s",
                (guild_id, kind, seed_key),
            )
            row = await cursor.fetchone()
        if row is None:
            raise CommunityUnavailable("Не удалось подтвердить сохранение записи.")
    return row


async def get_item(guild_id, item_id) -> dict | None:
    params = (_id(guild_id, "сервер"), _id(item_id, "запись"))
    async with _cursor() as cursor:
        await cursor.execute(
            "SELECT * FROM esil_internal.community_items WHERE guild_id=%s AND id=%s", params,
        )
        return await cursor.fetchone()


async def list_items(guild_id, kind, *, limit=10, offset=0) -> tuple[list, int]:
    guild_id, kind = _id(guild_id, "сервер"), _kind(kind)
    if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
            or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
        raise ValueError("Некорректная страница списка.")
    async with _cursor() as cursor:
        await cursor.execute(
            "SELECT count(*) AS total FROM esil_internal.community_items WHERE guild_id=%s AND kind=%s",
            (guild_id, kind),
        )
        total = (await cursor.fetchone())["total"]
        await cursor.execute(
            """SELECT * FROM esil_internal.community_items WHERE guild_id=%s AND kind=%s
            ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s""", (guild_id, kind, limit, offset),
        )
        return await cursor.fetchall(), total


async def set_status(guild_id, item_id, status, *, actor_id) -> dict:
    guild_id, item_id, actor_id = (_id(guild_id, "сервер"), _id(item_id, "запись"),
                                 _id(actor_id, "участник"))
    if not isinstance(status, str) or not any(status in values for values in schema.STATUSES.values()):
        raise ValueError("Неизвестный статус.")
    async with _cursor() as cursor:
        await cursor.execute(
            f"""UPDATE esil_internal.community_items SET status=%s,updated_by=%s,updated_at=now()
            WHERE guild_id=%s AND id=%s AND %s=ANY ({schema.STATUS_CASE_SQL}) RETURNING *""",
            (status, actor_id, guild_id, item_id, status),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("Запись не найдена на этом сервере или статус для неё недопустим.")
    return row


async def bind_message(guild_id, item_id, channel_id, message_id) -> dict:
    params = (_id(channel_id, "канал"), _id(message_id, "сообщение"),
              _id(guild_id, "сервер"), _id(item_id, "запись"))
    async with _cursor() as cursor:
        await cursor.execute(
            """UPDATE esil_internal.community_items SET channel_id=%s,message_id=%s,updated_at=now()
            WHERE guild_id=%s AND id=%s RETURNING *""", params,
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("Запись на этом сервере не найдена.")
    return row


async def vote(guild_id, item_id, user_id, value) -> dict:
    guild_id, item_id, user_id = (_id(guild_id, "сервер"), _id(item_id, "предложение"),
                                _id(user_id, "участник"))
    if isinstance(value, bool) or not isinstance(value, int) or value not in (-1, 1):
        raise ValueError("Голос должен быть +1 или -1.")
    async with _cursor() as cursor:
        # Serialize eligibility with status changes and other votes for this item.
        await cursor.execute(
            """SELECT id FROM esil_internal.community_items WHERE guild_id=%s AND id=%s
            AND kind='suggestion' AND status IN ('open','accepted','in_progress') FOR UPDATE""",
            (guild_id, item_id),
        )
        if await cursor.fetchone() is None:
            raise ValueError("Голосование закрыто или предложение на этом сервере не найдено.")
        await cursor.execute(
            """INSERT INTO esil_internal.community_votes (guild_id,item_id,user_id,value)
            VALUES (%s,%s,%s,%s) ON CONFLICT (guild_id,item_id,user_id) DO UPDATE
            SET value=CASE WHEN community_votes.value=EXCLUDED.value THEN 0 ELSE EXCLUDED.value END,
                updated_at=now()""", (guild_id, item_id, user_id, value),
        )
        await cursor.execute(_COUNTS_SQL, (user_id, guild_id, item_id))
        return await cursor.fetchone()


async def vote_counts(guild_id, item_id, user_id=None) -> dict:
    guild_id, item_id = _id(guild_id, "сервер"), _id(item_id, "предложение")
    if user_id is not None:
        user_id = _id(user_id, "участник")
    async with _cursor() as cursor:
        await cursor.execute(_COUNTS_SQL, (user_id, guild_id, item_id))
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("Предложение на этом сервере не найдено.")
        return row


async def get_setting(guild_id, key) -> dict | None:
    params = (_id(guild_id, "сервер"), _text(key, "Настройка", 80))
    async with _cursor() as cursor:
        await cursor.execute(
            "SELECT value FROM esil_internal.community_settings WHERE guild_id=%s AND key=%s", params,
        )
        row = await cursor.fetchone()
        return row["value"] if row is not None else None


async def set_setting(guild_id, key, value: dict) -> dict:
    params = (_id(guild_id, "сервер"), _text(key, "Настройка", 80), _object(value))
    async with _cursor() as cursor:
        await cursor.execute(
            """INSERT INTO esil_internal.community_settings (guild_id,key,value) VALUES (%s,%s,%s)
            ON CONFLICT (guild_id,key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()
            RETURNING value""", params,
        )
        return (await cursor.fetchone())["value"]
