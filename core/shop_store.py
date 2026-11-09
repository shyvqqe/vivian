from __future__ import annotations

import copy
import re
import time
import uuid

from core import hub_store


NAMESPACE = "shop"
_PENDING = {"pending", "charging", "delivery_pending", "refund_pending", "retry"}
_FINAL = {"delivered", "refunded", "already_owned", "canceled"}


def item_key(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", value) is None:
        raise ValueError("Use a lowercase item ID with 1 to 32 letters, digits, underscores or hyphens.")
    return value


def _identifier(value):
    return hub_store.identifier(value)


def _purchase_id(value):
    value = str(value)
    if re.fullmatch(r"[0-9]{1,20}", value) is None or int(value) <= 0:
        raise ValueError("Invalid purchase identifier.")
    return value


async def catalog(guild_id):
    value = await hub_store.get(guild_id, NAMESPACE, "catalog", {"items": {}})
    return list(value.get("items", {}).values())


async def add_item(guild_id, key, name, price, role_id, description=""):
    key = item_key(key)
    if type(price) is not int or not 1 <= price <= 1_000_000_000:
        raise ValueError("Price must be between 1 and 1000000000.")
    _identifier(role_id)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
        raise ValueError("Item names must contain 1 to 64 characters.")
    if not isinstance(description, str) or len(description) > 200:
        raise ValueError("Item descriptions must contain at most 200 characters.")
    def change(value):
        items = value.setdefault("items", {})
        if key not in items and len(items) >= 100:
            raise ValueError("This server already has 100 shop items.")
        items[key] = {"key": key, "name": name.strip(), "price": price, "role_id": role_id,
                      "description": description.strip(), "updated_at": time.time()}
        return copy.deepcopy(items[key])
    return await hub_store.mutate(guild_id, NAMESPACE, "catalog", change, default={"items": {}})


async def remove_item(guild_id, key):
    key = item_key(key)
    def change(value):
        return value.setdefault("items", {}).pop(key, None) is not None
    return await hub_store.mutate(guild_id, NAMESPACE, "catalog", change, default={"items": {}})


async def purchase(guild_id, purchase_id):
    purchase_id = _purchase_id(purchase_id)
    return await hub_store.get(guild_id, NAMESPACE, f"purchase:{purchase_id}")


async def prepare(guild_id, user_id, key, purchase_id, *, expected_role_id=None, expected_price=None):
    _identifier(user_id)
    key, purchase_id = item_key(key), _purchase_id(purchase_id)
    record_key, user_key = f"purchase:{purchase_id}", f"user:{user_id}"
    now = time.time()
    def change(values):
        existing = values[record_key]
        if existing:
            if existing["user_id"] != user_id or existing["item_key"] != key:
                raise ValueError("This purchase identifier is already bound to another request.")
            return copy.deepcopy(existing)
        items = (values["catalog"] or {}).get("items", {})
        item = items.get(key)
        if not item:
            raise ValueError("That item is not available in this server's shop.")
        if (expected_role_id is not None and item["role_id"] != expected_role_id
                or expected_price is not None and item["price"] != expected_price):
            raise ValueError("This shop item changed while the purchase was being opened. Review the new price and try again.")
        user = values[user_key] or {"pending": {}, "last_purchase_at": 0}
        if now - user.get("last_purchase_at", 0) < 10:
            raise ValueError("Wait ten seconds between new purchases.")
        pending = user.setdefault("pending", {})
        if str(item["role_id"]) in pending:
            raise ValueError("A purchase for this role is already being processed.")
        if len(pending) >= 5:
            raise ValueError("You already have five pending purchases. Wait for them to finish.")
        queue = values["pending"] or {"ids": []}
        if len(queue["ids"]) >= 1000:
            raise ValueError("This server's shop queue is full. Try again later.")
        record = {"id": purchase_id, "user_id": user_id, "item_key": key, "item_name": item["name"],
            "role_id": item["role_id"], "price": item["price"], "reference": f"shop:{purchase_id}",
            "state": "pending", "created_at": now, "next_at": now, "attempts": 0,
            "lease_until": 0, "lease_token": None, "charged": False, "charge_started": False}
        user.update(last_purchase_at=now)
        pending[str(item["role_id"])] = purchase_id
        queue["ids"].append(purchase_id)
        values[user_key], values["pending"], values[record_key] = user, queue, record
        return copy.deepcopy(record)
    return await hub_store.mutate_many(guild_id, NAMESPACE, ["catalog", "pending", user_key, record_key], change)


async def claim(guild_id, purchase_id):
    purchase_id = _purchase_id(purchase_id)
    now = time.time()
    def change(value):
        if value.get("state") not in _PENDING or value.get("lease_until", 0) > now or value.get("next_at", 0) > now:
            return None
        value.update(lease_until=now + 90, lease_token=uuid.uuid4().hex[:20], attempts=value.get("attempts", 0) + 1)
        return copy.deepcopy(value)
    return await hub_store.mutate(guild_id, NAMESPACE, f"purchase:{purchase_id}", change, default={})


async def update(guild_id, item, **patch):
    if any(key not in {"state", "charge_started", "charged", "next_at", "lease_until", "last_error", "refund_reason", "delivered_at"} for key in patch):
        raise ValueError("Invalid purchase update.")
    def change(value):
        if value.get("lease_token") != item.get("lease_token") or value.get("user_id") != item["user_id"]:
            raise ValueError("This purchase worker no longer holds its lease.")
        value.update(patch)
        return copy.deepcopy(value)
    return await hub_store.mutate(guild_id, NAMESPACE, f"purchase:{_purchase_id(item['id'])}", change, default={})


async def finish(guild_id, item, state, **details):
    if state not in _FINAL:
        raise ValueError("Invalid final purchase state.")
    record_key, user_key = f"purchase:{_purchase_id(item['id'])}", f"user:{item['user_id']}"
    def change(values):
        value = values[record_key]
        if not value or value.get("user_id") != item["user_id"] or value.get("lease_token") != item.get("lease_token"):
            raise ValueError("This purchase worker no longer holds its lease.")
        if value["state"] in _FINAL:
            return copy.deepcopy(value)
        value.update(state=state, finished_at=time.time(), lease_until=0, **details)
        user = values[user_key] or {"pending": {}}
        if user.get("pending", {}).get(str(item["role_id"])) == item["id"]:
            user["pending"].pop(str(item["role_id"]))
        queue = values["pending"] or {"ids": []}
        queue["ids"] = [identifier for identifier in queue["ids"] if identifier != item["id"]]
        values[user_key], values["pending"] = user, queue
        return copy.deepcopy(value)
    return await hub_store.mutate_many(guild_id, NAMESPACE, [record_key, user_key, "pending"], change)


async def pending(guild_id, limit=50):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid purchase batch size.")
    queue = await hub_store.get(guild_id, NAMESPACE, "pending", {"ids": []})
    now = time.time()
    result = []
    for purchase_id in queue["ids"][:1000]:
        item = await purchase(guild_id, purchase_id)
        if item and item.get("state") in _PENDING and item.get("next_at", 0) <= now and item.get("lease_until", 0) <= now:
            result.append(item)
            if len(result) >= limit:
                break
    return result
