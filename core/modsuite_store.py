from __future__ import annotations

import copy
import re
import time
import uuid

from core import hub_store


NAMESPACE = "moderation"
_RUNNING = {"applying", "active", "releasing", "retry"}
_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "с": 1, "сек": 1,
          "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "м": 60, "мин": 60,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "ч": 3600, "час": 3600,
          "d": 86400, "day": 86400, "days": 86400, "д": 86400, "дн": 86400,
          "w": 604800, "week": 604800, "weeks": 604800, "н": 604800, "нед": 604800}
_DURATION = re.compile(r"(\d+)\s*([a-zа-я]+)", re.I)


def duration(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
        raise ValueError("Use a duration such as 30m, 2d 3h, or 2д 3ч.")
    value = value.strip().lower()
    result = cursor = 0
    for match in _DURATION.finditer(value):
        if value[cursor:match.start()].strip() or match.group(2) not in _UNITS:
            raise ValueError("Use a duration such as 30m, 2d 3h, or 2д 3ч.")
        result += int(match.group(1)) * _UNITS[match.group(2)]
        cursor = match.end()
    if value[cursor:].strip() or not 1 <= result <= 365 * 86400:
        raise ValueError("Duration must be between one second and 365 days.")
    return result


def text(value, limit=1000):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= limit
            or any(ord(char) < 32 and char not in "\n\t" for char in value)):
        raise ValueError(f"Provide 1 to {limit} characters.")
    return value.strip()


def identifier(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Invalid server or member identifier.")
    return value


async def get(guild_id, key, default=None):
    return await hub_store.get(identifier(guild_id), NAMESPACE, key, default=default)


async def put(guild_id, key, value):
    await hub_store.put(identifier(guild_id), NAMESPACE, key, value)
    return value


async def rows(guild_id, prefix, limit=100, offset=0):
    return await hub_store.scan(identifier(guild_id), NAMESPACE, prefix=prefix, limit=limit, offset=offset)


async def member_history(guild_id, user_id):
    return await get(guild_id, f"member:{identifier(user_id)}", {"warnings": [], "history": [], "notes": []})


async def history(guild_id, user_id, entry):
    entry = dict(entry)
    if isinstance(entry.get("reason"), str):
        entry["reason"] = entry["reason"][:400]
    def change(value):
        entries = value.setdefault("history", [])
        if entry.get("id") and any(item.get("id") == entry["id"] for item in entries):
            return copy.deepcopy(value)
        entries.append({**entry, "at": time.time()})
        value["history"] = entries[-60:]
        return copy.deepcopy(value)
    return await hub_store.mutate(identifier(guild_id), NAMESPACE, f"member:{identifier(user_id)}", change, default={})


async def warning(guild_id, user_id, actor_id, reason, reference):
    reason = text(reason)
    def change(value):
        warnings = value.setdefault("warnings", [])
        for item in warnings:
            if item["reference"] == reference:
                return {"count": value.get("warning_count", len(warnings)), "new": False, "item": copy.deepcopy(item)}
        item = {"id": uuid.uuid4().hex[:20], "actor_id": actor_id, "reason": reason,
                "at": time.time(), "reference": reference}
        warnings.append(item)
        value["warning_count"] = value.get("warning_count", len(warnings) - 1) + 1
        value["warnings"] = warnings[-50:]
        entries = value.setdefault("history", [])
        entries.append({"kind": "warning", **item, "reason": item["reason"][:400]})
        value["history"] = entries[-60:]
        return {"count": value["warning_count"], "new": True, "item": copy.deepcopy(item)}
    return await hub_store.mutate(identifier(guild_id), NAMESPACE, f"member:{identifier(user_id)}", change, default={})


async def note(guild_id, user_id, actor_id, body):
    body = text(body)
    def change(value):
        notes = value.setdefault("notes", [])
        notes.append({"actor_id": actor_id, "body": body, "at": time.time()})
        value["notes"] = notes[-40:]
        return copy.deepcopy(value["notes"][-1])
    return await hub_store.mutate(identifier(guild_id), NAMESPACE, f"member:{identifier(user_id)}", change, default={})


async def create_punishment(guild_id, kind, user_id, actor_id, reason, *, seconds=None, extra=None):
    identifier(user_id)
    reason = text(reason)
    now = time.time()
    def change(value):
        if value.get("state") in _RUNNING:
            if extra and extra.get("source_ref") and value.get("source_ref") == extra["source_ref"]:
                return copy.deepcopy(value)
            raise ValueError(f"This member already has an active {kind} case.")
        value.clear()
        value.update({"id": uuid.uuid4().hex[:20], "kind": kind, "user_id": user_id,
                      "actor_id": actor_id, "reason": reason, "created_at": now,
                      "ends_at": now + seconds if seconds is not None else None,
                      "state": "applying", "next_at": now, "lease_until": 0, "attempts": 0,
                      **(extra or {})})
        return copy.deepcopy(value)
    key = f"punishment:{kind}:{user_id}"
    if kind in {"jail", "quarantine"}:
        other = f"punishment:{'jail' if kind == 'quarantine' else 'quarantine'}:{user_id}"
        def isolate(values):
            if values[other] and values[other].get("state") in _RUNNING:
                raise ValueError("Release the existing isolation case before applying a different isolation role.")
            if values[key] is None:
                values[key] = {}
            return change(values[key])
        return await hub_store.mutate_many(identifier(guild_id), NAMESPACE, [key, other], isolate)
    return await hub_store.mutate(identifier(guild_id), NAMESPACE, key, change, default={})


async def punishment(guild_id, kind, user_id):
    return await get(guild_id, f"punishment:{kind}:{identifier(user_id)}")


async def update_punishment(guild_id, item, **patch):
    def change(value):
        if value.get("id") != item["id"]:
            return None
        value.update(patch)
        if value.get("phase") == "release" and patch.get("state") == "active":
            value.update(state="releasing", next_at=time.time(), lease_until=0)
        return copy.deepcopy(value)
    return await hub_store.mutate(identifier(guild_id), NAMESPACE, f"punishment:{item['kind']}:{item['user_id']}", change, default={})


async def queue_release(guild_id, kind, user_id):
    def change(value):
        if value.get("state") not in _RUNNING:
            raise ValueError("No active Vivian case was found for this member.")
        value.update(state="releasing", phase="release", next_at=time.time())
        return copy.deepcopy(value)
    return await hub_store.mutate(identifier(guild_id), NAMESPACE, f"punishment:{kind}:{identifier(user_id)}", change, default={})


async def claim_punishment(guild_id, kind, user_id):
    now = time.time()
    def change(value):
        if value.get("state") not in _RUNNING or value.get("lease_until", 0) > now or value.get("next_at", 0) > now:
            return None
        value["lease_until"] = now + 90
        value["attempts"] = value.get("attempts", 0) + 1
        return copy.deepcopy(value)
    return await hub_store.mutate(identifier(guild_id), NAMESPACE, f"punishment:{kind}:{identifier(user_id)}", change, default={})


async def audit(guild_id, kind, **data):
    item = {"id": uuid.uuid4().hex[:20], "kind": kind, "at": time.time(), **data}
    await put(guild_id, f"audit:{int(item['at'] * 1000):016}:{item['id']}", item)
    return item


async def create_case(guild_id, kind, data, *, case_id=None):
    case_id = case_id or uuid.uuid4().hex[:20]
    if not re.fullmatch(r"[a-f0-9]{20}", case_id):
        raise ValueError("Invalid case identifier.")
    def change(value):
        if value:
            return copy.deepcopy(value)
        value.update({"id": case_id, "kind": kind, "status": "pending", "created_at": time.time(), **data})
        return copy.deepcopy(value)
    return await hub_store.mutate(identifier(guild_id), NAMESPACE, f"case:{case_id}", change, default={})


async def case(guild_id, case_id):
    if not isinstance(case_id, str) or not re.fullmatch(r"[a-f0-9]{20}", case_id):
        raise ValueError("Invalid case identifier.")
    value = await get(guild_id, f"case:{case_id}")
    if not value:
        raise ValueError("Case not found.")
    return value


async def claim_case(guild_id, case_id, actor_id, action):
    now = time.time()
    def change(value):
        if not value:
            raise ValueError("Case not found.")
        if value.get("status") not in {"pending", "processing"}:
            raise ValueError("This case has already been decided.")
        if value.get("status") == "processing" and value.get("lease_until", 0) > now:
            raise ValueError("Another moderator is processing this case.")
        if value.get("status") == "processing" and value.get("decision") != action:
            raise ValueError("Retry the existing case decision before choosing another action.")
        value.update(status="processing", decision=action, decision_actor=actor_id, lease_until=now + 90)
        return copy.deepcopy(value)
    return await hub_store.mutate(identifier(guild_id), NAMESPACE, f"case:{case_id}", change, default={})


async def finish_case(guild_id, case_id, **patch):
    def change(value):
        if not value:
            raise ValueError("Case not found.")
        value.update(patch)
        return copy.deepcopy(value)
    return await hub_store.mutate(identifier(guild_id), NAMESPACE, f"case:{case_id}", change, default={})


async def report_limit(guild_id, reporter_id):
    now = time.time()
    def change(value):
        recent = [stamp for stamp in value.get("recent", []) if stamp > now - 600]
        if len(recent) >= 5:
            raise ValueError("You can submit up to five reports every ten minutes.")
        recent.append(now)
        value["recent"] = recent
    await hub_store.mutate(identifier(guild_id), NAMESPACE, f"report_limit:{identifier(reporter_id)}", change, default={})
