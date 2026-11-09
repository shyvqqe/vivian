from __future__ import annotations

from collections import defaultdict

import discord
from psycopg.types.json import Jsonb

from core import hub_store, social_store


_MARKER = "legacy_social_v1"
_READ_GUILDS = """WITH candidates AS (SELECT guild_id FROM vivian_features.wall_privacy
    UNION SELECT guild_id FROM vivian_features.wall_posts WHERE deleted=0
    UNION SELECT guild_id FROM vivian_features.status_profiles
    UNION SELECT guild_id FROM vivian_features.relationships
    UNION SELECT guild_id FROM vivian_features.relationship_requests WHERE status='pending'
    UNION SELECT guild_id FROM vivian_features.crushes)
    SELECT guild_id FROM candidates c WHERE c.guild_id > 0 AND NOT EXISTS (
        SELECT 1 FROM vivian_private.feature_records f
        WHERE f.guild_id=c.guild_id AND f.namespace='migration' AND f.key=%s
    ) ORDER BY guild_id"""
_INSERT = """INSERT INTO vivian_private.feature_records (guild_id,namespace,key,payload)
    VALUES (%s,%s,%s,%s) ON CONFLICT (guild_id,namespace,key) DO NOTHING"""


async def _rows(current, query, params=()):
    await current.execute(query, params)
    return await current.fetchall()


async def _wall_records(current, guild, members, records):
    privacy = await _rows(current, "SELECT user_id,privacy FROM vivian_features.wall_privacy WHERE guild_id=%s", (guild,))
    for row in privacy:
        members[row["user_id"]].setdefault("wall_privacy", "nobody" if row["privacy"] == "none" else row["privacy"])
    posts = await _rows(current, """SELECT id,author_id,owner_id,text,created_at,channel_id,message_id,thread_id
        FROM vivian_features.wall_posts WHERE guild_id=%s AND deleted=0""", (guild,))
    if not posts:
        return
    comments = await _rows(current, """SELECT c.id,c.post_id,c.author_id,c.text,c.created_at
        FROM vivian_features.wall_comments c JOIN vivian_features.wall_posts p ON p.id=c.post_id
        WHERE p.guild_id=%s AND p.deleted=0 ORDER BY c.created_at,c.id""", (guild,))
    likes = await _rows(current, """SELECT l.post_id,l.user_id FROM vivian_features.wall_likes l
        JOIN vivian_features.wall_posts p ON p.id=l.post_id WHERE p.guild_id=%s AND p.deleted=0
        ORDER BY l.user_id""", (guild,))
    post_comments, post_likes = defaultdict(list), defaultdict(list)
    for row in comments:
        post_comments[row["post_id"]].append({"author": row["author_id"], "text": row["text"],
                                              "created_at": row["created_at"], "legacy_id": row["id"]})
    for row in likes:
        post_likes[row["post_id"]].append(row["user_id"])
    for post in posts:
        token = str(post["id"])
        records[f"wall:{token}"] = {
            "token": token, "owner": post["owner_id"], "author": post["author_id"],
            "text": post["text"], "created_at": post["created_at"], "deleted": False,
            "likes": post_likes[post["id"]], "comments": post_comments[post["id"]],
            "legacy_channel_id": post["channel_id"], "legacy_message_id": post["message_id"],
            "legacy_thread_id": post["thread_id"],
        }


async def _status_records(current, guild, members, records):
    profiles = await _rows(current, "SELECT user_id,text,mood FROM vivian_features.status_profiles WHERE guild_id=%s", (guild,))
    for row in profiles:
        member = members[row["user_id"]]
        member.setdefault("status_text", row["text"] or "")
        member.setdefault("mood", row["mood"] or "")

    relationships = await _rows(current, "SELECT user_id,partner_id,since FROM vivian_features.relationships WHERE guild_id=%s", (guild,))
    links = {row["user_id"]: row for row in relationships}
    for first, row in links.items():
        second = row["partner_id"]
        if first >= second or links.get(second, {}).get("partner_id") != first:
            continue
        pair = members[first], members[second]
        if not any("relationship_user_id" in member or "relationship_request" in member for member in pair):
            pair[0].update(relationship_user_id=second, relationship_since=row["since"])
            pair[1].update(relationship_user_id=first, relationship_since=links[second]["since"])

    requests = await _rows(current, """SELECT id,from_id,to_id,created_at,message_id FROM vivian_features.relationship_requests
        WHERE guild_id=%s AND status='pending' ORDER BY (message_id IS NULL),created_at DESC,id DESC""", (guild,))
    for request in requests:
        sender, target = request["from_id"], request["to_id"]
        token = str(request["id"])
        pair = members[sender], members[target]
        if sender == target or any("relationship_user_id" in member or "relationship_request" in member for member in pair):
            continue
        for member in pair:
            member["relationship_request"] = token
        records[f"relationship:{token}"] = {"sender": sender, "target": target, "state": "pending",
                                             "created_at": request["created_at"]}
        if request["message_id"] is not None:
            records[f"legacy_relationship_message:{request['message_id']}"] = {"token": token}


async def _crush_records(current, guild, records):
    picks = await _rows(current, """SELECT from_id,to_id,created_at,notified FROM vivian_features.crushes
        WHERE guild_id=%s ORDER BY created_at,from_id,to_id""", (guild,))
    for row in picks:
        sender, target, created_at = row["from_id"], row["to_id"], row["created_at"]
        if sender == target:
            continue
        week = social_store._week(created_at)
        own = records.setdefault(f"crush:{week}:{sender}", {"choices": [], "current": None})
        if target not in own["choices"]:
            own["choices"].append(target)
        own["current"] = target
        if row["notified"]:
            first, second = sorted((sender, target))
            records[f"match:{week}:{first}_{second}"] = {"created_at": created_at, "members": [first, second]}


async def migrate_guild(guild):
    async with hub_store.cursor(guild) as current:
        await current.execute("LOCK TABLE vivian_private.feature_records IN ROW EXCLUSIVE MODE")
        marker = await _rows(current, """SELECT key FROM vivian_private.feature_records
            WHERE guild_id=%s AND namespace='migration' AND key=%s""", (guild, _MARKER))
        if marker:
            return False
        # Serialize the snapshot with transactions from the previous release.
        await current.execute("SELECT pg_advisory_xact_lock(721304880981522349)")
        member_rows = await _rows(current, """SELECT key,payload FROM vivian_private.feature_records
            WHERE guild_id=%s AND namespace='social' AND starts_with(key,'member:')""", (guild,))
        members = defaultdict(dict, {int(row["key"].split(":", 1)[1]): row["payload"] for row in member_rows})
        records = {}
        await _wall_records(current, guild, members, records)
        await _status_records(current, guild, members, records)
        await _crush_records(current, guild, records)
        if records:
            await current.executemany(_INSERT, ((guild, "social", key, Jsonb(value)) for key, value in records.items()))
        if members:
            await current.executemany("""INSERT INTO vivian_private.feature_records (guild_id,namespace,key,payload)
                VALUES (%s,'social',%s,%s) ON CONFLICT (guild_id,namespace,key)
                DO UPDATE SET payload=excluded.payload,updated_at=now()
                WHERE feature_records.payload IS DISTINCT FROM excluded.payload""",
                ((guild, f"member:{user}", Jsonb(member)) for user, member in members.items()))
        await current.execute(_INSERT, (guild, "migration", _MARKER, Jsonb({"complete": True})))
        return True


async def migrate():
    if not hub_store.is_ready():
        return 0
    async with hub_store.cursor() as current:
        guilds = await _rows(current, _READ_GUILDS, (_MARKER,))
    migrated = 0
    for row in guilds:
        migrated += await migrate_guild(row["guild_id"])
    return migrated


class _LegacyWallAction(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"vivian:wall:(?P<action>like|comment):(?P<post>[0-9]+)"):
    def __init__(self, item, action, post):
        super().__init__(item)
        self.action, self.post = action, post

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(item, match["action"], match["post"])

    async def callback(self, interaction):
        from commands.social import handle_action

        if interaction.guild_id is not None:
            await handle_action(interaction, f"wall{self.action}", interaction.guild_id, self.post)


class _LegacyRelationshipRequest(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _respond(self, interaction, accept):
        from commands import social

        await social._begin(interaction)
        try:
            guild, actor = await social._context(interaction)
            message = interaction.message
            mapping = await hub_store.get(guild.id, social_store.NS, f"legacy_relationship_message:{message.id}") if message else None
            request = await hub_store.get(guild.id, social_store.NS, f"relationship:{mapping['token']}") if mapping else None
            if not request:
                raise ValueError("This relationship request is unavailable.")
            await social._member(guild, request["sender"])
            await social_store.relationship_respond(guild.id, actor.id, mapping["token"], accept)
        except (ValueError, hub_store.Unavailable) as error:
            return await social._error(interaction, error)
        await social._finish(interaction, "Relationship accepted." if accept else "Relationship request rejected.")

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="vivian_status_accept")
    async def accept(self, interaction, button):
        await self._respond(interaction, True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.secondary, custom_id="vivian_status_decline")
    async def decline(self, interaction, button):
        await self._respond(interaction, False)


def register_views(bot):
    bot.add_dynamic_items(_LegacyWallAction)
    bot.add_view(_LegacyRelationshipRequest())
