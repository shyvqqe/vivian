from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
import re
import time
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from core import hub_store as db


NAMESPACE = "life"
ARCHIVE = "life_archive"
RETENTION = 1095 * 86400


def local_now(settings, timestamp=None):
    try:
        zone = ZoneInfo(settings.get("timezone") or "UTC")
    except (ValueError, KeyError):
        zone = timezone.utc
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, zone)


def birthday_date(year, month, day):
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 3, 1)
    return date(year, month, day)


def next_birthday(month, day, today):
    candidate = birthday_date(today.year, month, day)
    return candidate if candidate >= today else birthday_date(today.year + 1, month, day)


async def rows(guild_id, prefix="", *, namespace=NAMESPACE, maximum=3000):
    result = []
    for offset in range(0, maximum, 100):
        batch = await db.scan(guild_id, namespace, prefix=prefix, limit=min(100, maximum - offset), offset=offset)
        result.extend(batch)
        if len(batch) < 100:
            break
    return result


async def preference(guild_id, user_id):
    return await db.get(guild_id, NAMESPACE, f"privacy:{user_id}", default={"archive": False})


async def consent(guild_id, user_id, enabled):
    await db.put(guild_id, NAMESPACE, f"privacy:{user_id}", {"archive": bool(enabled)})
    removed = 0
    if not enabled:
        async with db.cursor(guild_id) as current:
            await current.execute("""DELETE FROM vivian_private.feature_records
                WHERE guild_id=%s AND namespace=%s AND payload->>'author_id'=%s""",
                (guild_id, ARCHIVE, str(user_id)))
            removed = current.rowcount
    return removed


async def archive_message(guild_id, record):
    if record["created_at"] < time.time() - RETENTION or not record.get("content", "").strip():
        return False
    async with db.cursor(guild_id) as current:
        await current.execute("""SELECT payload FROM vivian_private.feature_records
            WHERE guild_id=%s AND namespace=%s AND key=%s""",
            (guild_id, NAMESPACE, f"privacy:{record['author_id']}"))
        saved = await current.fetchone()
        if not saved or not saved["payload"].get("archive"):
            return False
        await current.execute("""INSERT INTO vivian_private.feature_records(guild_id,namespace,key,payload)
            VALUES(%s,%s,%s,%s) ON CONFLICT(guild_id,namespace,key)
            DO UPDATE SET payload=excluded.payload,updated_at=now()""",
            (guild_id, ARCHIVE, str(record["message_id"]), Jsonb(dict(record, content=record["content"][:2000]))))
    return True


async def forget_message(guild_id, message_id):
    await db.delete(guild_id, ARCHIVE, str(message_id))


async def archive_records(guild_id):
    return [row["data"] for row in await rows(guild_id, namespace=ARCHIVE)
            if row["data"].get("created_at", 0) >= time.time() - RETENTION]


async def memories_records(guild_id, settings, today):
    zone = str(local_now(settings).tzinfo)
    async with db.cursor() as current:
        await current.execute("""SELECT payload AS data FROM vivian_private.feature_records
            WHERE guild_id=%s AND namespace=%s AND (payload->>'created_at')::double precision >= %s
            AND EXTRACT(MONTH FROM to_timestamp((payload->>'created_at')::double precision) AT TIME ZONE %s)=%s
            AND EXTRACT(DAY FROM to_timestamp((payload->>'created_at')::double precision) AT TIME ZONE %s)=%s
            AND EXTRACT(YEAR FROM to_timestamp((payload->>'created_at')::double precision) AT TIME ZONE %s)<%s
            ORDER BY COALESCE((payload->>'score')::integer,0) DESC,updated_at DESC LIMIT 50""",
            (guild_id, ARCHIVE, time.time() - RETENTION, zone, today.month, zone, today.day, zone, today.year))
        return [row["data"] for row in await current.fetchall()]


async def search_records(guild_id, query):
    terms = sorted(set(re.findall(r"[\w-]{2,}", query.casefold())))[:20]
    if not terms:
        raise ValueError("Use at least one word with two or more characters.")
    async with db.cursor() as current:
        await current.execute("""SELECT payload AS data,
            (SELECT count(*) FROM unnest(%s::text[]) AS term WHERE position(term in lower(payload->>'content'))>0) AS score
            FROM vivian_private.feature_records WHERE guild_id=%s AND namespace=%s
            AND (payload->>'created_at')::double precision >= %s
            AND EXISTS(SELECT 1 FROM unnest(%s::text[]) AS term WHERE position(term in lower(payload->>'content'))>0)
            ORDER BY score DESC,updated_at DESC LIMIT 30""", (terms, guild_id, ARCHIVE, time.time() - RETENTION, terms))
        return [row["data"] for row in await current.fetchall()]


async def due_records(guild_id, kind, *, now=None):
    if kind not in {"story", "birthday-role"}:
        raise ValueError("Invalid scheduled record type.")
    now = time.time() if now is None else now
    async with db.cursor() as current:
        await current.execute("""SELECT key,payload AS data FROM vivian_private.feature_records
            WHERE guild_id=%s AND namespace=%s AND starts_with(key,%s)
            AND (payload->>'expires_at')::double precision <= %s
            AND CASE WHEN %s='story' THEN COALESCE(payload->>'status','') NOT IN ('deleted','failed')
                     ELSE COALESCE((payload->>'owned')::boolean,false) END
            ORDER BY (payload->>'expires_at')::double precision LIMIT 100""",
            (guild_id, NAMESPACE, kind + ":", now, kind))
        return await current.fetchall()


async def prune(guild_id):
    cutoff = time.time() - RETENTION
    async with db.cursor(guild_id) as current:
        await current.execute("""DELETE FROM vivian_private.feature_records WHERE guild_id=%s AND namespace=%s
            AND (payload->>'created_at')::double precision < %s""", (guild_id, ARCHIVE, cutoff))


async def job_due(guild_id, key, *, now=None):
    now = time.time() if now is None else now
    value = await db.get(guild_id, NAMESPACE, f"job:{key}", default={})
    return not (value.get("done") or value.get("uncertain") or value.get("delivery_started") or value.get("retry_at", 0) > now
                or value.get("lease_until", 0) > now or value.get("attempts", 0) >= 6)


async def reserve_confession(guild_id, author_id, channel_id, *, reply_to=None):
    def increment(document):
        document["value"] = int(document.get("value", 0)) + 1
        return document["value"]
    number = await db.mutate(guild_id, NAMESPACE, "confession-counter", increment, default={})
    value = {"number": number, "author_id": author_id, "channel_id": channel_id,
             "message_id": None, "thread_id": None, "reply_to": reply_to, "created_at": int(time.time())}
    await db.put(guild_id, NAMESPACE, f"confession:{number:020}", value)
    return value


async def confession(guild_id, number):
    return await db.get(guild_id, NAMESPACE, f"confession:{number:020}")


async def save_confession(guild_id, item):
    await db.put(guild_id, NAMESPACE, f"confession:{item['number']:020}", item)


async def reserve_vibe(guild_id, user_id, token, *, now=None):
    now = time.time() if now is None else now
    def reserve(document):
        if document.get("until", 0) > now:
            raise ValueError(f"Vibecheck is available again <t:{int(document['until'])}:R>.")
        document.update(token=str(token), until=now + 90)
        return True
    return await db.mutate(guild_id, NAMESPACE, f"vibe:{user_id}", reserve, default={})


async def finish_vibe(guild_id, user_id, token, *, success):
    def finish(document):
        if document.get("token") == str(token):
            document["until"] = time.time() + 3600 if success else 0
    await db.mutate(guild_id, NAMESPACE, f"vibe:{user_id}", finish, default={})


async def set_birthday(guild_id, user_id, day, month, year=None):
    if type(day) is not int or type(month) is not int:
        raise ValueError("Enter a valid day and month.")
    try:
        date(2000 if year is None else year, month, day)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid birthday.") from None
    today = datetime.now(timezone.utc).date()
    if year is not None and (type(year) is not int or not today.year - 120 <= year <= today.year
                             or date(year, month, day) > today):
        raise ValueError("Enter a valid birth year, or leave the year blank.")
    value = {"user_id": user_id, "day": day, "month": month, "year": year}
    await db.put(guild_id, NAMESPACE, f"birthday:{user_id}", value)
    return value


async def birthdays(guild_id, today):
    values = [row["data"] for row in await rows(guild_id, "birthday:", maximum=10000)]
    return sorted(values, key=lambda item: (next_birthday(item["month"], item["day"], today), item["user_id"]))


async def save_story(guild_id, story_id, data):
    await db.put(guild_id, NAMESPACE, f"story:{story_id}", data)


async def reserve_story(guild_id, data):
    async with db.cursor(guild_id) as current:
        await current.execute("""SELECT count(*) FILTER (WHERE payload->>'author_id'=%s) AS own,
            count(*) AS total FROM vivian_private.feature_records
            WHERE guild_id=%s AND namespace=%s AND starts_with(key,'story:')
            AND payload->>'status' IN ('active','pending') AND (payload->>'expires_at')::double precision>%s""",
            (str(data["author_id"]), guild_id, NAMESPACE, time.time()))
        counts = await current.fetchone()
        if counts["own"] >= 5 or counts["total"] >= 1000:
            raise ValueError("Story capacity reached: at most 5 active stories per member and 1000 per server.")
        await current.execute("""INSERT INTO vivian_private.feature_records(guild_id,namespace,key,payload)
            VALUES(%s,%s,%s,%s)""", (guild_id, NAMESPACE, f"story:{data['id']}", Jsonb(data)))


async def get_story(guild_id, story_id):
    return await db.get(guild_id, NAMESPACE, f"story:{story_id}")


async def viewed(guild_id, story_id, user_id, *, now=None):
    now = time.time() if now is None else now
    def update(document):
        if not document or document.get("expires_at", 0) <= now or document.get("status") != "active":
            raise ValueError("This story has expired or is unavailable.")
        viewers = document.setdefault("viewers", {})
        if str(user_id) not in viewers and len(viewers) >= 1000:
            raise ValueError("This story has reached its voluntary viewer limit.")
        viewers.setdefault(str(user_id), int(now))
        return len(viewers)
    return await db.mutate(guild_id, NAMESPACE, f"story:{story_id}", update, default={})


async def claim_job(guild_id, key, *, now=None):
    now = time.time() if now is None else now
    def claim(document):
        if document.get("done") or document.get("uncertain") or document.get("delivery_started") or document.get("retry_at", 0) > now:
            return False
        if document.get("lease_until", 0) > now or document.get("attempts", 0) >= 6:
            return False
        document.update(lease_until=now + 120, attempts=document.get("attempts", 0) + 1)
        return True
    return await db.mutate(guild_id, NAMESPACE, f"job:{key}", claim, default={})


async def finish_job(guild_id, key, *, success=False, uncertain=False):
    def finish(document):
        document.update(done=success, uncertain=uncertain, lease_until=0, delivery_started=False,
                        retry_at=time.time() + min(3600, 60 * 2 ** document.get("attempts", 0)))
    await db.mutate(guild_id, NAMESPACE, f"job:{key}", finish, default={})


async def mark_delivery(guild_id, key):
    def mark(document):
        document["delivery_started"] = True
    await db.mutate(guild_id, NAMESPACE, f"job:{key}", mark, default={})


def search(records, query, maximum=30):
    terms = set(re.findall(r"[\w-]{2,}", query.casefold()))
    if not terms:
        raise ValueError("Use at least one word with two or more characters.")
    scored = []
    for record in records:
        content = record.get("content", "").casefold()
        score = sum(content.count(term) for term in terms)
        if score:
            scored.append((score, record.get("created_at", 0), record))
    return [record for _, _, record in sorted(scored, key=lambda item: (-item[0], -item[1]))[:maximum]]
