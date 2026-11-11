from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import time
from zoneinfo import ZoneInfo

from core import guild_settings, hub_store


BRANCHES = ("speaker", "helper", "memologist", "night_owl", "voice")


def _amount(amount):
    if type(amount) is not int or not 1 <= amount <= 1_000_000_000:
        raise ValueError("Amount must be between 1 and 1000000000.")
    return amount


def _transaction_key(reference):
    if not isinstance(reference, str) or not 1 <= len(reference) <= 300:
        raise ValueError("A transaction reference is required.")
    return "tx:" + hashlib.sha256(reference.encode()).hexdigest()


def _account(user_id):
    hub_store.identifier(user_id)
    return f"account:{user_id}"


def _fresh_account():
    return {"balance": 0, "branches": {}, "season_points": {}, "badges": [], "earned": {}}


async def balance(guild_id, user_id):
    data = await hub_store.get(guild_id, "economy", _account(user_id), _fresh_account())
    return max(0, int(data.get("balance", 0)))


get_balance = balance


async def transaction(guild_id, reference):
    return await hub_store.get(guild_id, "economy", _transaction_key(reference), None)


async def _change(guild_id, user_id, amount, reference, direction):
    _amount(amount)
    key, transaction = _account(user_id), _transaction_key(reference)

    def operation(records):
        account = records[key] or _fresh_account()
        if records[transaction] is not None:
            old = records[transaction]
            if old.get("user_id") != user_id or old.get("amount") != amount or old.get("direction") != direction:
                raise ValueError("This transaction reference has already been used.")
            return int(account["balance"])
        new_balance = int(account.get("balance", 0)) + amount * direction
        if new_balance < 0:
            raise ValueError("You do not have enough currency.")
        if new_balance > 10**15:
            raise ValueError("This account has reached its balance limit.")
        account["balance"] = new_balance
        records[key] = account
        records[transaction] = {"user_id": user_id, "amount": amount, "direction": direction, "created_at": time.time()}
        return new_balance

    return await hub_store.mutate_many(guild_id, "economy", [key, transaction], operation)


async def spend(guild_id, user_id, amount, reference):
    return await _change(guild_id, user_id, amount, reference, -1)


async def credit(guild_id, user_id, amount, reference):
    return await _change(guild_id, user_id, amount, reference, 1)


def _local_date(settings):
    return datetime.now(ZoneInfo(settings.get("timezone", "Europe/Riga"))).date().isoformat()


async def daily(guild_id, user_id):
    settings = await guild_settings.get(guild_id)
    if not settings.get("economy_enabled"):
        raise ValueError("The server economy is disabled.")
    day = _local_date(settings)
    amount = _amount(int(settings.get("daily_reward", 25)))
    key = _account(user_id)

    def operation(account):
        if account.get("daily_date") == day:
            raise ValueError("Your daily reward has already been claimed today.")
        if int(account.get("balance", 0)) + amount > 10**15:
            raise ValueError("This account has reached its balance limit.")
        account["daily_date"] = day
        account["balance"] = int(account.get("balance", 0)) + amount
        return {"amount": amount, "balance": account["balance"]}

    return await hub_store.mutate(guild_id, "economy", key, operation, default=_fresh_account())


async def reward_message(message):
    settings = await guild_settings.get(message.guild.id)
    if not settings.get("economy_enabled") or message.author.bot:
        return
    content = (message.content or "").strip()
    if len(content) < 12 or len(set(content.casefold().split())) < 3:
        return
    digest = hashlib.sha256(content.casefold().encode()).hexdigest()
    now = time.time()
    local = datetime.now(ZoneInfo(settings["timezone"]))
    day, season = local.date().isoformat(), local.strftime("%Y-%m")
    key = _account(message.author.id)

    def operation(account):
        if now - float(account.get("last_earned_at", 0)) < 60 or digest == account.get("last_content"):
            return
        if int(account.get("balance", 0)) >= 10**15:
            return
        earned = account.setdefault("earned", {})
        if earned.get("day") != day:
            earned.clear()
            earned.update(day=day, amount=0)
        if int(earned["amount"]) >= int(settings.get("daily_currency_cap", 100)):
            return
        account["last_earned_at"], account["last_content"] = now, digest
        earned["amount"] += 1
        account["balance"] = int(account.get("balance", 0)) + 1
        branches = account.setdefault("branches", {})
        branches["speaker"] = int(branches.get("speaker", 0)) + 1
        if getattr(message, "reference", None) is not None and len(content) >= 60:
            branches["helper"] = int(branches.get("helper", 0)) + 2
        if message.attachments or "tenor.com/" in content or "giphy.com/" in content:
            branches["memologist"] = int(branches.get("memologist", 0)) + 1
        if local.hour < 6:
            branches["night_owl"] = int(branches.get("night_owl", 0)) + 1
        account.setdefault("season_points", {})[season] = int(account.get("season_points", {}).get(season, 0)) + 1
        account["season_points"] = dict(sorted(account["season_points"].items())[-24:])

    await hub_store.mutate(message.guild.id, "economy", key, operation, default=_fresh_account())


async def reward_voice(guild_id, user_id, seconds):
    seconds = max(0, min(int(seconds), 3600))
    if seconds < 60:
        return

    def operation(account):
        branches = account.setdefault("branches", {})
        branches["voice"] = int(branches.get("voice", 0)) + seconds // 60
        account["voice_seconds"] = int(account.get("voice_seconds", 0)) + seconds

    await hub_store.mutate(guild_id, "economy", _account(user_id), operation, default=_fresh_account())


async def profile_summary(guild_id, user_id):
    settings = await guild_settings.get(guild_id)
    account = await hub_store.get(guild_id, "economy", _account(user_id), _fresh_account())
    branches = {key: max(0, int(account.get("branches", {}).get(key, 0))) for key in BRANCHES}
    badges = [f"{key}:{value // 100}" for key, value in branches.items() if value >= 100]
    result = {"balance": max(0, int(account.get("balance", 0))), "currency": settings.get("currency_name", "sparks"),
            "branches": branches, "badges": badges + list(account.get("badges", []))[-12:],
            "voice_seconds": int(account.get("voice_seconds", 0))}
    if settings.get("seasons_enabled"):
        from core.season_style import theme_for
        month = datetime.now(ZoneInfo(settings["timezone"])).strftime("%Y-%m")
        result["season"] = theme_for(month, settings.get("season_theme", "auto"))
    return result


async def close_seasons(guild_id):
    settings = await guild_settings.get(guild_id)
    current = datetime.now(ZoneInfo(settings["timezone"])).strftime("%Y-%m")
    async with hub_store.cursor() as cursor:
        await cursor.execute("""SELECT key FROM vivian_private.feature_records
            WHERE guild_id=%s AND namespace='economy' AND starts_with(key,'account:')
            AND EXISTS(SELECT 1 FROM jsonb_object_keys(coalesce(payload->'season_points','{}'::jsonb)) AS season
                WHERE season < %s AND NOT (coalesce(payload->'closed_seasons','[]'::jsonb) ? season))
            ORDER BY key LIMIT 100""", (guild_id, current))
        rows = await cursor.fetchall()
    for row in rows:
        def operation(account):
            awarded = account.setdefault("closed_seasons", [])
            for season, points in account.get("season_points", {}).items():
                if season < current and season not in awarded:
                    if int(points) >= 50:
                        account.setdefault("badges", []).append(f"season:{season}")
                    awarded.append(season)
            account["closed_seasons"] = awarded[-24:]
            account["badges"] = account.get("badges", [])[-36:]
        await hub_store.mutate(guild_id, "economy", row["key"], operation)
    return len(rows)


async def leaderboard(guild_id, limit=10, offset=0, branch=None):
    hub_store.identifier(guild_id)
    if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or not 0 <= offset <= 1000000:
        raise ValueError("Invalid leaderboard page.")
    if branch is not None and branch not in BRANCHES:
        raise ValueError("Unknown progress branch.")
    field = "payload->'branches'->>%s" if branch else "payload->>'balance'"
    params = (branch,) if branch else ()
    score = f"CASE WHEN ({field}) ~ '^[0-9]{{1,16}}$' THEN ({field})::bigint ELSE 0 END"
    params = params + params
    async with hub_store.cursor() as cursor:
        await cursor.execute(f"""SELECT split_part(key,':',2)::bigint AS user_id,
            coalesce(payload->'branches','{{}}'::jsonb) AS branches,
            coalesce((payload->>'balance')::bigint,0) AS balance, {score} AS score
            FROM vivian_private.feature_records
            WHERE guild_id=%s AND namespace='economy' AND key ~ '^account:[0-9]{{1,19}}$'
            ORDER BY score DESC,user_id LIMIT %s OFFSET %s""", (*params, guild_id, limit, offset))
        return await cursor.fetchall()
