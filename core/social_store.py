from __future__ import annotations

from datetime import datetime, timezone
import time
import uuid

from core import hub_store


NS = "social"


def _ids(*values):
    if any(type(value) is not int or not 0 < value < 2**63 for value in values):
        raise ValueError("A valid server and member are required.")


def _pair(first, second):
    _ids(first, second)
    if first == second:
        raise ValueError("Choose another member.")
    return f"{min(first, second)}_{max(first, second)}"


def _member(values, user):
    key = f"member:{user}"
    if values.get(key) is None:
        values[key] = {"friends": [], "wall_privacy": "all"}
    return values[key]


def _text(value, maximum, empty=False):
    if not isinstance(value, str) or len(value.strip()) > maximum or (not empty and not value.strip()):
        raise ValueError(f"Use {'0' if empty else '1'} to {maximum} characters.")
    return value.strip()


def _friend(values, first, second):
    return second in _member(values, first).get("friends", [])


def _wall_access(values, owner, viewer):
    privacy = _member(values, owner).get("wall_privacy", "all")
    return owner == viewer or privacy == "all" or (privacy == "friends" and _friend(values, owner, viewer))


def _job(kind, recipient, other, now, **details):
    return {"kind": kind, "recipient": recipient, "other": other, "state": "pending", "attempts": 0,
            "created_at": now, "next_attempt": now, "expires_at": now + 3600, **details}


async def friend_request(guild, sender, target):
    _ids(guild, sender, target)
    pair = _pair(sender, target)
    key = f"friend:{pair}"
    now = int(time.time())

    def change(values):
        first, second = _member(values, sender), _member(values, target)
        if target in first.get("friends", []):
            raise ValueError("You are already friends.")
        previous = values.get(key)
        if previous and previous.get("state") == "pending":
            raise ValueError("A friend request is already pending. Use /friend requests to respond.")
        if len(first.get("friends", [])) >= 500 or len(second.get("friends", [])) >= 500:
            raise ValueError("A member has reached the 500-friend limit.")
        values[key] = {"sender": sender, "target": target, "state": "pending", "created_at": now}
        return dict(values[key], token=pair)

    return await hub_store.mutate_many(guild, NS, [key, f"member:{sender}", f"member:{target}"], change)


async def friend_respond(guild, actor, other, accept):
    _ids(guild, actor, other)
    key = f"friend:{_pair(actor, other)}"

    def change(values):
        request = values.get(key)
        if not request or request.get("state") != "pending" or request["target"] != actor:
            raise ValueError("No incoming friend request from that member.")
        sender = request["sender"]
        first, second = _member(values, sender), _member(values, actor)
        if accept:
            if len(first.get("friends", [])) >= 500 or len(second.get("friends", [])) >= 500:
                raise ValueError("A member has reached the 500-friend limit.")
            first["friends"] = sorted(set(first.get("friends", [])) | {actor})
            second["friends"] = sorted(set(second.get("friends", [])) | {sender})
            request.update(state="accepted", since=int(time.time()))
        else:
            request["state"] = "rejected"
        return dict(request)

    return await hub_store.mutate_many(guild, NS, [key, f"member:{actor}", f"member:{other}"], change)


async def friend_remove(guild, actor, other):
    _ids(guild, actor, other)
    key = f"friend:{_pair(actor, other)}"

    def change(values):
        for user, target in ((actor, other), (other, actor)):
            record = _member(values, user)
            record["friends"] = [item for item in record.get("friends", []) if item != target]
        values[key] = None
        return True

    return await hub_store.mutate_many(guild, NS, [key, f"member:{actor}", f"member:{other}"], change)


async def friends(guild, user):
    _ids(guild, user)
    record = await hub_store.get(guild, NS, f"member:{user}", {})
    return sorted(set(record.get("friends", [])))


async def mutual_friends(guild, first, second):
    _ids(guild, first, second)
    return sorted(set(await friends(guild, first)) & set(await friends(guild, second)))


async def friend_requests(guild, user, page=1):
    _ids(guild, user)
    rows, offset, selected = [], 0, []
    while True:
        rows = await hub_store.scan(guild, NS, prefix="friend:", limit=100, offset=offset)
        selected.extend(dict(item["data"], token=item["key"].split(":", 1)[1]) for item in rows
                        if item["data"].get("state") == "pending" and item["data"].get("target") == user)
        if len(rows) < 100:
            break
        offset += len(rows)
    selected.sort(key=lambda item: item["created_at"], reverse=True)
    return selected[(page - 1) * 5:page * 5], len(selected)


async def wall_privacy(guild, user, privacy):
    _ids(guild, user)
    if privacy not in {"all", "friends", "nobody"}:
        raise ValueError("Choose all, friends or nobody.")

    def change(values):
        _member(values, user)["wall_privacy"] = privacy
        return privacy

    return await hub_store.mutate_many(guild, NS, [f"member:{user}"], change)


async def wall_post(guild, author, owner, text):
    _ids(guild, author, owner)
    text = _text(text, 800)
    token = uuid.uuid4().hex[:16]
    key = f"wall:{token}"

    def change(values):
        if not _wall_access(values, owner, author):
            raise ValueError("This wall is private.")
        values[key] = {"token": token, "author": author, "owner": owner, "text": text,
                       "created_at": int(time.time()), "likes": [], "comments": [], "deleted": False}
        return dict(values[key])

    return await hub_store.mutate_many(guild, NS, [key, f"member:{owner}", f"member:{author}"], change)


async def _wall_change(guild, actor, token, action, text=None):
    _ids(guild, actor)
    if not isinstance(token, str) or not 1 <= len(token) <= 40 or not token.isalnum():
        raise ValueError("Invalid wall post.")
    key = f"wall:{token}"
    snapshot = await hub_store.get(guild, NS, key)
    if not snapshot or snapshot.get("deleted"):
        raise ValueError("This wall post is no longer available.")
    owner = snapshot["owner"]

    def change(values):
        post = values.get(key)
        if not post or post.get("deleted") or post["owner"] != owner or not _wall_access(values, owner, actor):
            raise ValueError("This wall post is unavailable or private.")
        if action == "like":
            likes = set(post.get("likes", []))
            if actor in likes:
                likes.remove(actor)
            elif len(likes) < 1000:
                likes.add(actor)
            else:
                raise ValueError("This post has reached its like limit.")
            post["likes"] = sorted(likes)
        elif action == "comment":
            if len(post.get("comments", [])) >= 100:
                raise ValueError("This post has reached its 100-comment limit.")
            post.setdefault("comments", []).append({"author": actor, "text": _text(text, 300), "created_at": int(time.time())})
        elif action == "delete":
            if actor not in {post["author"], owner}:
                raise ValueError("Only the post author or wall owner can delete this post.")
            values[key] = None
            return True
        return dict(post)

    return await hub_store.mutate_many(guild, NS, [key, f"member:{owner}", f"member:{actor}"], change)


async def wall_get(guild, actor, token):
    return await _wall_change(guild, actor, token, "get")


async def wall_like(guild, actor, token):
    return await _wall_change(guild, actor, token, "like")


async def wall_comment(guild, actor, token, text):
    return await _wall_change(guild, actor, token, "comment", text)


async def wall_delete(guild, actor, token):
    return await _wall_change(guild, actor, token, "delete")


async def wall_view(guild, actor, owner, page=1):
    _ids(guild, actor, owner)
    rows, selected, offset = [], [], 0
    while True:
        rows = await hub_store.scan(guild, NS, prefix="wall:", limit=100, offset=offset)
        selected.extend(item["data"] for item in rows if item["data"].get("owner") == owner and not item["data"].get("deleted"))
        if len(rows) < 100:
            break
        offset += len(rows)
    selected.sort(key=lambda item: (item["created_at"], item["token"]), reverse=True)
    keys = [f"member:{owner}", f"member:{actor}"] + [f"wall:{item['token']}" for item in selected[(page - 1) * 3:page * 3]]

    def change(values):
        if not _wall_access(values, owner, actor):
            raise ValueError("This wall is private.")
        posts = [dict(values[key]) for key in keys if key.startswith("wall:") and values.get(key) and not values[key].get("deleted")]
        return posts, len(selected)

    return await hub_store.mutate_many(guild, NS, keys, change)


async def status_set(guild, user, text, mood):
    _ids(guild, user)
    text, mood = _text(text, 120, empty=True), _text(mood, 32, empty=True)

    def change(values):
        _member(values, user).update(status_text=text, mood=mood)
        return True

    return await hub_store.mutate_many(guild, NS, [f"member:{user}"], change)


async def relationship_request(guild, sender, target):
    _ids(guild, sender, target)
    _pair(sender, target)
    token = uuid.uuid4().hex[:16]
    key = f"relationship:{token}"

    def change(values):
        for user in (sender, target):
            item = _member(values, user)
            if item.get("relationship_user_id") or item.get("relationship_request"):
                raise ValueError("A relationship or request is already active. Use /status single to clear yours.")
        values[key] = {"sender": sender, "target": target, "state": "pending", "created_at": int(time.time())}
        for user in (sender, target):
            _member(values, user)["relationship_request"] = token
        return dict(values[key], token=token)

    return await hub_store.mutate_many(guild, NS, [key, f"member:{sender}", f"member:{target}"], change)


async def relationship_respond(guild, actor, token, accept):
    _ids(guild, actor)
    key = f"relationship:{token}"
    snapshot = await hub_store.get(guild, NS, key)
    if not snapshot or snapshot.get("target") != actor:
        raise ValueError("This request belongs to another member or no longer exists.")
    sender = snapshot["sender"]

    def change(values):
        request = values.get(key)
        if not request or request.get("state") != "pending" or request.get("target") != actor:
            raise ValueError("This relationship request is no longer pending.")
        for user in (sender, actor):
            item = _member(values, user)
            if item.get("relationship_request") != token or item.get("relationship_user_id"):
                raise ValueError("This relationship request is no longer valid.")
        now = int(time.time())
        for user, partner in ((sender, actor), (actor, sender)):
            item = _member(values, user)
            item.pop("relationship_request", None)
            if accept:
                item.update(relationship_user_id=partner, relationship_since=now)
        request["state"] = "accepted" if accept else "rejected"
        return dict(request)

    return await hub_store.mutate_many(guild, NS, [key, f"member:{sender}", f"member:{actor}"], change)


async def relationship_single(guild, user):
    _ids(guild, user)
    snapshot = await hub_store.get(guild, NS, f"member:{user}", {})
    partner, token = snapshot.get("relationship_user_id"), snapshot.get("relationship_request")
    request = await hub_store.get(guild, NS, f"relationship:{token}") if token else None
    other = partner or (request["target"] if request and request["sender"] == user else request["sender"] if request else None)
    keys = [f"member:{user}"] + ([f"member:{other}"] if other else []) + ([f"relationship:{token}"] if token else [])

    def change(values):
        own = _member(values, user)
        if own.get("relationship_user_id") != partner or own.get("relationship_request") != token:
            raise ValueError("Your relationship changed. Please try again.")
        for field in ("relationship_user_id", "relationship_since", "relationship_request"):
            own.pop(field, None)
        if other:
            theirs = _member(values, other)
            if theirs.get("relationship_user_id") == user:
                theirs.pop("relationship_user_id", None)
                theirs.pop("relationship_since", None)
            if token and theirs.get("relationship_request") == token:
                theirs.pop("relationship_request", None)
        if token and values.get(f"relationship:{token}"):
            values[f"relationship:{token}"]["state"] = "cancelled"
        return True

    return await hub_store.mutate_many(guild, NS, keys, change)


def _week(now=None):
    date = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    year, week, _ = date.isocalendar()
    return f"{year}-{week:02d}"


async def crush_pick(guild, user, target):
    _ids(guild, user, target)
    pair = _pair(user, target)
    week, now = _week(), int(time.time())
    own_key, other_key, match_key = f"crush:{week}:{user}", f"crush:{week}:{target}", f"match:{week}:{pair}"
    job_keys = [f"outbox:crush:{week}:{pair}:{recipient}" for recipient in (user, target)]

    def change(values):
        if values.get(own_key) is None:
            values[own_key] = {"choices": [], "current": None}
        own = values[own_key]
        choices = own.setdefault("choices", [])
        if target not in choices:
            if len(choices) >= 3:
                raise ValueError("You have used your three distinct choices for this ISO week (UTC).")
            choices.append(target)
        own["current"] = target
        matched = bool(values.get(other_key) and values[other_key].get("current") == user)
        if matched and values.get(match_key) is None:
            values[match_key] = {"created_at": now, "members": sorted((user, target))}
            for recipient, other, key in zip((user, target), (target, user), job_keys):
                values[key] = _job("crush", recipient, other, now)
        return {"matched": matched, "used": len(choices), "week": week}

    return await hub_store.mutate_many(guild, NS, [own_key, other_key, match_key, *job_keys], change)


async def crush_show(guild, user):
    _ids(guild, user)
    result = await hub_store.get(guild, NS, f"crush:{_week()}:{user}", {"choices": [], "current": None})
    return {"choices": result.get("choices", []), "current": result.get("current"), "week": _week()}


async def crush_clear(guild, user):
    _ids(guild, user)

    def change(record):
        record.setdefault("choices", [])
        record["current"] = None
        return True

    return await hub_store.mutate(guild, NS, f"crush:{_week()}:{user}", change)


async def poke_send(guild, sender, target):
    _ids(guild, sender, target)
    _pair(sender, target)
    key, now = f"poke:{sender}_{target}", int(time.time())
    job_key = f"outbox:poke:{uuid.uuid4().hex[:16]}"

    def change(values):
        old = values.get(key)
        if old and now - old["last_at"] < 60:
            raise ValueError(f"Wait {60 - (now - old['last_at'])} seconds before poking this member again.")
        count = old.get("count", 0) + 1 if old else 1
        values[key] = {"sender": sender, "target": target, "count": count, "last_at": now}
        values[job_key] = _job("poke", target, sender, now)
        return {"count": count}

    return await hub_store.mutate_many(guild, NS, [key, job_key], change)


async def poke_stats(guild, first=None, second=None):
    _ids(guild)
    if first is not None and second is not None:
        _pair(first, second)
        sent = await hub_store.get(guild, NS, f"poke:{first}_{second}", {})
        received = await hub_store.get(guild, NS, f"poke:{second}_{first}", {})
        return {"sent": sent.get("count", 0), "received": received.get("count", 0)}
    rows, items, offset = [], [], 0
    while True:
        rows = await hub_store.scan(guild, NS, prefix="poke:", limit=100, offset=offset)
        items.extend(row["data"] for row in rows)
        if len(rows) < 100:
            break
        offset += len(rows)
    return sorted(items, key=lambda item: (-item["count"], item["sender"], item["target"]))[:10]


async def profile_summary(guild, user):
    _ids(guild, user)
    item = await hub_store.get(guild, NS, f"member:{user}", {})
    return {"friend_count": len(set(item.get("friends", []))), "status_text": item.get("status_text", ""),
            "mood": item.get("mood", ""), "relationship_user_id": item.get("relationship_user_id"),
            "relationship_since": item.get("relationship_since")}


async def public_friend_ids(guild, user):
    _ids(guild, user)
    async with hub_store.cursor() as current:
        await current.execute(
            """SELECT peer.key FROM vivian_private.feature_records AS consent
            JOIN vivian_private.feature_records AS owner
              ON owner.guild_id = consent.guild_id AND owner.namespace = 'social' AND owner.key = %s
            CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(owner.payload->'friends', '[]')) AS friend(id)
            JOIN vivian_private.feature_records AS peer
              ON peer.guild_id = consent.guild_id AND peer.namespace = 'profile_settings' AND peer.key = friend.id
            JOIN vivian_private.feature_records AS membership
              ON membership.guild_id = consent.guild_id AND membership.namespace = 'social'
              AND membership.key = 'member:' || friend.id
            WHERE consent.guild_id = %s AND consent.namespace = 'profile_settings' AND consent.key = %s
              AND consent.payload @> '{"public":true,"share_friends":true}'
              AND peer.payload @> '{"public":true,"share_friends":true}'
              AND membership.payload->'friends' @> jsonb_build_array(%s::bigint)
            ORDER BY peer.key LIMIT 12""",
            (f"member:{user}", guild, str(user), user),
        )
        return [int(row["key"]) for row in await current.fetchall()]


async def public_wall_posts(guild, user):
    _ids(guild, user)
    async with hub_store.cursor() as current:
        await current.execute(
            """SELECT post.payload FROM vivian_private.feature_records AS consent
            LEFT JOIN vivian_private.feature_records AS owner
              ON owner.guild_id = consent.guild_id AND owner.namespace = 'social' AND owner.key = %s
            JOIN vivian_private.feature_records AS post
              ON post.guild_id = consent.guild_id AND post.namespace = 'social' AND starts_with(post.key, 'wall:')
            JOIN vivian_private.feature_records AS author
              ON author.guild_id = consent.guild_id AND author.namespace = 'profile_settings'
              AND author.key = post.payload->>'author'
            WHERE consent.guild_id = %s AND consent.namespace = 'profile_settings' AND consent.key = %s
              AND consent.payload @> '{"public":true,"share_wall":true}'
              AND COALESCE(owner.payload->>'wall_privacy', 'all') = 'all'
              AND post.payload->>'owner' = %s AND post.payload->>'deleted' IS DISTINCT FROM 'true'
              AND author.payload @> '{"public":true,"share_wall":true}'
            ORDER BY post.payload->'created_at' DESC, post.key LIMIT 3""",
            (f"member:{user}", guild, str(user), str(user)),
        )
        posts = [row["payload"] for row in await current.fetchall()]
        comment_authors = sorted({str(comment["author"]) for post in posts for comment in post.get("comments", [])})
        visible_authors = set()
        if comment_authors:
            await current.execute(
                """SELECT key FROM vivian_private.feature_records
                WHERE guild_id = %s AND namespace = 'profile_settings' AND key = ANY(%s)
                  AND payload @> '{"public":true,"share_wall":true}'""",
                (guild, comment_authors),
            )
            visible_authors = {row["key"] for row in await current.fetchall()}

    return [{
        "author": post["author"], "text": post["text"], "created_at": post["created_at"], "likes": len(post.get("likes", [])),
        "comments": [
            {"author": comment["author"], "text": comment["text"], "created_at": comment["created_at"]}
            for comment in post.get("comments", []) if str(comment["author"]) in visible_authors
        ][-3:],
    } for post in posts]


async def pending_outbox(guild, limit=10):
    now = int(time.time())
    result, offset = [], 0
    while len(result) < limit:
        rows = await hub_store.scan(guild, NS, prefix="outbox:", limit=100, offset=offset)
        result.extend(dict(row["data"], key=row["key"]) for row in rows if row["data"].get("state") in {"pending", "delivering"}
                      and row["data"].get("next_attempt", 0) <= now and row["data"].get("expires_at", 0) > now)
        if len(rows) < 100:
            break
        offset += len(rows)
    return result[:limit]


async def claim_notification(guild, key):
    now = int(time.time())

    def change(job):
        if job.get("state") not in {"pending", "delivering"} or job.get("next_attempt", 0) > now or job.get("expires_at", 0) <= now:
            return None
        if job.get("attempts", 0) >= 4:
            job["state"] = "failed"
            return None
        job.update(state="delivering", attempts=job.get("attempts", 0) + 1, next_attempt=now + 60)
        return dict(job, key=key)

    return await hub_store.mutate(guild, NS, key, change)


async def finish_notification(guild, key, success):
    def change(job):
        job["state"] = "sent" if success else "failed" if job.get("attempts", 0) >= 4 else "pending"
        job["next_attempt"] = int(time.time()) + 60
        return True

    return await hub_store.mutate(guild, NS, key, change)
