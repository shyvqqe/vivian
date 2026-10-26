"""Private fact changes, serialized with archive reads and delayed writes."""

import asyncio
import copy
from collections import deque
from datetime import datetime
import time

from core import config, database, state
from vivian import ambient_state, memory_state


CONTROL_LOCKS = {}
CONTROL_FACTS = {}
PURGED_SOURCE_IDS = {}


class MemoryUnavailable(RuntimeError):
    pass


def control_lock(scope):
    return CONTROL_LOCKS.setdefault(scope, asyncio.Lock())


def merge_control_fact(scope, uid, key, entry):
    target = CONTROL_FACTS.setdefault(scope, {}).setdefault(uid, {})
    current = target.get(key)
    if current is None or (entry["source"], entry["timestamp"]) >= (current["source"], current["timestamp"]):
        target[key] = dict(entry)


def filter_fact_candidates(scope, uid, source_id, facts):
    controls = CONTROL_FACTS.get(scope, {}).get(uid, {})
    return {key: value for key, value in facts.items()
            if key not in controls or source_id > controls[key]["source"]
            or (source_id == controls[key]["source"] and controls[key]["text"] == value)}


async def guarded_memory_write(writer, scope, record, facts):
    async with control_lock(scope):
        if record["message_id"] in PURGED_SOURCE_IDS.get(scope, set()):
            return True  # Retire a stale pending archive job without resurrecting its source.
        from vivian.memory import _ai_memory_fact_candidates
        candidates = _ai_memory_fact_candidates(record["text"]) if record["role"] == "user" else {}
        fact_keys = set(facts) | set(candidates)
        if fact_keys and memory_state.AI_MEMORY_READY and database.db_pool is not None:
            try:
                async with asyncio.timeout(3), database.db_pool.connection() as connection:
                    cursor = await connection.execute(
                        """SELECT fact_key, content, source_message_id, updated_at
                        FROM elis_private.member_facts WHERE guild_id = %s AND channel_id = %s
                        AND user_id = %s AND fact_key = ANY(%s)""",
                        (*scope, record["author_id"], list(fact_keys)),
                    )
                    for key, text, source, updated in [row async for row in cursor]:
                        entry = {"text": text, "source": source, "timestamp": updated.timestamp()}
                        merge_control_fact(scope, record["author_id"], key, entry)
                        personal = memory_state.AI_MEMORY_FACTS.setdefault(scope, {}).setdefault(record["author_id"], {})
                        current = personal.get(key)
                        if current is None or source > current["source"]:
                            personal[key] = entry
            except Exception as error:
                print(f"[AI Memory] Fact guard read deferred; channel={scope[1]}; error={error!r}", flush=True)
                return False
        accepted = filter_fact_candidates(scope, record["author_id"], record["message_id"], facts)
        current_candidates = filter_fact_candidates(scope, record["author_id"], record["message_id"], candidates)
        if candidates.keys() - current_candidates.keys():
            _purge_sources(scope, record["author_id"], {record["message_id"]})
            return True  # Includes an old source replay after restart, not just pending RAM jobs.
        return await writer(scope, record, accepted)


async def guarded_memory_load(loader, scope):
    async with control_lock(scope):
        await loader(scope)
        for uid, facts in memory_state.AI_MEMORY_FACTS.get(scope, {}).items():
            for key, entry in facts.items():
                merge_control_fact(scope, uid, key, entry)


def _source_ids(records, uid, keys, before_id, facts):
    from vivian.memory import _ai_memory_fact_candidates
    sources = {entry["source"] for key, entry in facts.items() if key in keys}
    for item in records:
        if item["author_id"] == uid and item["role"] == "user" and item["message_id"] <= before_id:
            if keys & _ai_memory_fact_candidates(item["text"]).keys():
                sources.add(item["message_id"])
    # The reply to a forgotten assertion must not reintroduce it as personal history.
    ordered = sorted(records, key=lambda item: item["message_id"])
    attached_to_source = False
    for item in ordered:
        if item["role"] == "user" and item["author_id"] == uid:
            attached_to_source = item["message_id"] in sources
        elif attached_to_source and item["role"] == "assistant" and item["audience_id"] == uid:
            sources.add(item["message_id"])
    return sources


def _purge_sources(scope, uid, sources):
    PURGED_SOURCE_IDS.setdefault(scope, set()).update(sources)
    cache = memory_state.AI_MEMORY_CHANNELS.get(scope)
    if cache is not None:
        memory_state.AI_MEMORY_CHANNELS[scope] = deque(
            (item for item in cache if item["message_id"] not in sources), maxlen=cache.maxlen)
    for pending_key, (pending_scope, record, _) in list(memory_state.AI_MEMORY_PENDING.items()):
        if pending_scope == scope and record["message_id"] in sources:
            memory_state.AI_MEMORY_PENDING.pop(pending_key, None)
    for history_key, history in list(state.chat_history.items()):
        if history_key[:2] == scope:
            history[:] = [item for item in history if item.get("_message_id") not in sources]
    if scope[1] == ambient_state.AMBIENT_CHAT_CHANNEL_ID:
        context = ambient_state.AMBIENT_CONTEXT.get(scope[0])
        if context is not None:
            ambient_state.AMBIENT_CONTEXT[scope[0]] = deque(
                (item for item in context if item["message_id"] not in sources), maxlen=context.maxlen)
        # Cancel a draft already built from a forgotten source without sending a notification.
        ambient = ambient_state.AMBIENT_STATE.get(scope[0])
        if ambient is not None:
            ambient["pause_until"] = max(ambient.get("pause_until", 0), time.time() + 30)


def _sanitized_case(case, sources, action_id):
    if not case:
        return None
    result = copy.deepcopy(case)
    if result.get("origin") in sources:
        result.update(issue="Источник удалён владельцем.", facts=[], tried=[], status="SOLVED")
    else:
        for group in ("facts", "tried"):
            result[group] = [item for item in result.get(group, ()) if item.get("source") not in sources]
    result["next"] = ""
    result["revision"] = max(action_id, int(result.get("revision", 0)) + 1)
    result["updated"] = time.time()
    return result


async def change_personal_facts(scope, uid, action_id, *, key=None, replacement=None, clear=False):
    """Explicit owner action. Blank durable rows are 90-day tombstones, not public facts."""
    from vivian import memory_store
    from vivian import diagnostic_store
    from psycopg.types.json import Jsonb
    await memory_store._ai_memory_load(scope)
    dialogue_lock = state.AI_DIALOGUE_LOCKS.setdefault((*scope, uid), asyncio.Lock())
    case_key = (*scope, uid)
    async with dialogue_lock, control_lock(scope), diagnostic_store.case_lock(case_key):
        personal = memory_state.AI_MEMORY_FACTS.setdefault(scope, {}).setdefault(uid, {})
        active = {name: dict(entry) for name, entry in personal.items() if entry.get("text")}
        keys = set(active) if clear else ({key} if key in active else set())
        keys = {name for name in keys if active[name]["source"] <= action_id}
        if not keys:
            return 0
        records = list(memory_state.AI_MEMORY_CHANNELS.get(scope, ()))
        now = time.time()
        persistent = bool(config.DATABASE_URL or database.db_pool is not None)
        if persistent and (database.db_pool is None or not memory_state.AI_MEMORY_READY):
            raise MemoryUnavailable("Хранилище временно недоступно. Изменение не сохранено.")
        case = diagnostic_store.CASES.get(case_key)
        if persistent:
            try:
                if not await diagnostic_store._ready():
                    raise MemoryUnavailable("Личная диагностика временно недоступна.")
                async with asyncio.timeout(8), database.db_pool.connection() as connection:
                    case_cursor = await connection.execute(
                        "SELECT payload FROM elis_private.diagnostic_cases "
                        "WHERE guild_id=%s AND channel_id=%s AND user_id=%s", case_key,
                    )
                    case_row = await case_cursor.fetchone()
                    if case_row and isinstance(case_row[0], dict):
                        if not case or int(case_row[0].get("revision", 0)) >= int(case.get("revision", 0)):
                            case = case_row[0]
                    cursor = await connection.execute(
                        """SELECT message_id, author_id, audience_id, role, content
                        FROM elis_private.chat_messages WHERE guild_id = %s AND channel_id = %s
                        AND message_id <= %s AND (author_id = %s OR audience_id = %s)
                        ORDER BY message_id LIMIT 2000""", (*scope, action_id, uid, uid),
                    )
                    records.extend({"message_id": mid, "author_id": author, "audience_id": audience,
                                    "role": role, "text": text} for mid, author, audience, role, text in [row async for row in cursor])
                    sources = _source_ids(records, uid, keys, action_id, active)
                    if sources:
                        await connection.execute(
                            """DELETE FROM elis_private.chat_messages
                            WHERE guild_id = %s AND channel_id = %s AND message_id = ANY(%s)""",
                            (*scope, list(sources)),
                        )
                    for name in keys:
                        await connection.execute(
                            """INSERT INTO elis_private.member_facts
                            (guild_id, channel_id, user_id, fact_key, content, source_message_id, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (guild_id, channel_id, user_id, fact_key) DO UPDATE SET
                            content = EXCLUDED.content, source_message_id = EXCLUDED.source_message_id,
                            updated_at = EXCLUDED.updated_at
                            WHERE member_facts.source_message_id <= EXCLUDED.source_message_id""",
                            (*scope, uid, name, replacement if name == key and replacement else "", action_id,
                             datetime.fromtimestamp(now, config.MOSCOW_TIMEZONE)),
                        )
                    sanitized_case = _sanitized_case(case, sources, action_id)
                    if sanitized_case is not None:
                        await connection.execute(
                            """INSERT INTO elis_private.diagnostic_cases (guild_id, channel_id, user_id, payload)
                            VALUES (%s,%s,%s,%s) ON CONFLICT (guild_id, channel_id, user_id) DO UPDATE SET
                            payload=EXCLUDED.payload, updated_at=now()
                            WHERE (diagnostic_cases.payload->>'revision')::bigint <= (EXCLUDED.payload->>'revision')::bigint""",
                            (*case_key, Jsonb(sanitized_case)),
                        )
            except Exception as error:
                raise MemoryUnavailable("Хранилище временно недоступно. Изменение не сохранено.") from error
        else:
            sources = _source_ids(records, uid, keys, action_id, active)
            sanitized_case = _sanitized_case(case, sources, action_id)
        # RAM changes only after the durable transaction succeeds (or explicit RAM-only mode).
        _purge_sources(scope, uid, sources)
        if sanitized_case is not None:
            diagnostic_store.cache_case(case_key, sanitized_case)
        for name in keys:
            entry = {"text": replacement if name == key and replacement else "",
                     "source": action_id, "timestamp": now}
            personal[name] = entry
            merge_control_fact(scope, uid, name, entry)
            for pending_scope, record, pending_facts in memory_state.AI_MEMORY_PENDING.values():
                if pending_scope == scope and record["author_id"] == uid and record["message_id"] <= action_id:
                    pending_facts.pop(name, None)
        return len(keys)
