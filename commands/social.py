from __future__ import annotations

import asyncio
import hashlib
import math

import discord
from discord import app_commands

from commands import social_views as views
from core import config, hub_store, social_store as store


friend = app_commands.Group(name="friend", description="Consensual server friendships.", guild_only=True)
wall = app_commands.Group(name="wall", description="Member walls with privacy controls.", guild_only=True)
status = app_commands.Group(name="status", description="Personal status and consensual relationships.", guild_only=True)
crush = app_commands.Group(name="crush", description="Private weekly selections, revealed only on a mutual match.", guild_only=True)
poke = app_commands.Group(name="poke", description="Send a poke and see server poke statistics.", guild_only=True)


def safe(value):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))


def link(member):
    return f"[{safe(member.display_name)}](https://discord.com/users/{member.id})"


async def _finish(interaction, text=None, **payload):
    if text is not None:
        payload["content"] = str(text)[:2000]
    payload["allowed_mentions"] = discord.AllowedMentions.none()
    return await interaction.edit_original_response(**payload)


async def _member(guild, user_id):
    if guild is None:
        raise ValueError("This server is unavailable.")
    result = guild.get_member(user_id)
    if result is None:
        try:
            async with asyncio.timeout(4):
                result = await guild.fetch_member(user_id)
        except (discord.DiscordException, TimeoutError):
            raise ValueError("This member is no longer available on this server.") from None
    if result.bot:
        raise ValueError("Choose a server member, rather than a bot.")
    return result


async def _context(interaction, guild_id=None):
    guild_id = guild_id or interaction.guild_id
    if interaction.guild_id is not None and interaction.guild_id != guild_id:
        raise ValueError("This control belongs to another server.")
    guild = interaction.guild if interaction.guild_id == guild_id else config.bot.get_guild(guild_id)
    actor = await _member(guild, interaction.user.id)
    settings = await hub_store.get(guild_id, "settings", "guild", {})
    if settings.get("social_enabled", True) is False:
        raise ValueError("Social features are disabled on this server.")
    return guild, actor


async def _begin(interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)


async def _error(interaction, error):
    if isinstance(error, ValueError):
        return await _finish(interaction, safe(error))
    return await _finish(interaction, "Social storage is unavailable. Your action was not confirmed; please try again later.")


async def _request_dm(member, text, view):
    try:
        await member.send(text, view=view, allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.DiscordException:
        return False


@friend.command(name="add", description="Send a friend request that the other member must accept.")
@app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
async def friend_add(interaction: discord.Interaction, member: discord.Member):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        target = await _member(guild, member.id)
        request = await store.friend_request(guild.id, actor.id, target.id)
        delivered = await _request_dm(target, f"{safe(actor.display_name)} sent you a friend request on **{safe(guild.name)}**.",
                                      views.request_view(guild.id, request["token"]))
        await _finish(interaction, "Friend request sent." if delivered else "Request saved. DMs are unavailable; the recipient can use /friend requests.")
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@friend.command(name="accept", description="Accept an incoming friend request.")
async def friend_accept(interaction: discord.Interaction, member: discord.Member):
    await _friend_response(interaction, member.id, True)


@friend.command(name="reject", description="Privately reject an incoming friend request.")
async def friend_reject(interaction: discord.Interaction, member: discord.Member):
    await _friend_response(interaction, member.id, False)


async def _friend_response(interaction, other, accept, guild_id=None):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction, guild_id)
        await _member(guild, other)
        await store.friend_respond(guild.id, actor.id, other, accept)
        await _finish(interaction, "You are now friends." if accept else "Friend request rejected.")
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@friend.command(name="requests", description="Privately review your incoming friend requests.")
async def friend_requests(interaction: discord.Interaction, page: app_commands.Range[int, 1, 1000] = 1):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        requests, total = await store.friend_requests(guild.id, actor.id, page)
        lines, controls = [f"Friend requests · page {page}/{max(1, math.ceil(total / 5))}"], discord.ui.View(timeout=None)
        for row, request in enumerate(requests):
            try:
                sender = await _member(guild, request["sender"])
            except ValueError:
                continue
            lines.append(f"{link(sender)} · /friend accept or /friend reject")
            for action, label in (("friendaccept", f"Accept {sender.display_name[:40]}"), ("friendreject", "Reject")):
                control = views.SocialAction(action, guild.id, request["token"], label)
                control.row = row
                controls.add_item(control)
        await _finish(interaction, "\n".join(lines) if len(lines) > 1 else "No incoming requests on this page.", view=controls)
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@friend.command(name="list", description="Privately list your friends who are still on this server.")
async def friend_list(interaction: discord.Interaction, page: app_commands.Range[int, 1, 100] = 1):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        ids = await store.friends(guild.id, actor.id)
        lines = []
        for user in ids[(page - 1) * 10:page * 10]:
            try:
                lines.append(link(await _member(guild, user)))
            except ValueError:
                continue
        await _finish(interaction, f"Friends · page {page}/{max(1, math.ceil(len(ids) / 10))}\n" + ("\n".join(lines) or "No active friends on this page."))
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@friend.command(name="mutual", description="Find your mutual friends with another server member.")
async def friend_mutual(interaction: discord.Interaction, member: discord.Member, page: app_commands.Range[int, 1, 100] = 1):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        await _member(guild, member.id)
        ids = await store.mutual_friends(guild.id, actor.id, member.id)
        lines = []
        for user in ids[(page - 1) * 10:page * 10]:
            try:
                lines.append(link(await _member(guild, user)))
            except ValueError:
                continue
        await _finish(interaction, f"Mutual friends · page {page}/{max(1, math.ceil(len(ids) / 10))}\n" + ("\n".join(lines) or "No mutual friends on this page."))
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@friend.command(name="remove", description="Remove a friend silently, without notifying them.")
async def friend_remove(interaction: discord.Interaction, member: discord.Member):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        await store.friend_remove(guild.id, actor.id, member.id)
        await _finish(interaction, "Friendship removed. No notification was sent.")
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


async def _wall_embeds(guild, posts):
    embeds = []
    for post in posts:
        try:
            author = link(await _member(guild, post["author"]))
        except ValueError:
            author = "Former member"
        embed = discord.Embed(title=f"Wall post · {post['token']}", description=safe(post["text"]), color=0xD58BFF)
        embed.add_field(name="Author", value=author, inline=False)
        embed.set_footer(text=f"{len(post.get('likes', []))} likes · {len(post.get('comments', []))} comments · /wall comments for all")
        embeds.append(embed)
    return embeds


@wall.command(name="view", description="Read a member wall, subject to its current privacy setting.")
async def wall_show(interaction: discord.Interaction, member: discord.Member | None = None,
                    page: app_commands.Range[int, 1, 10000] = 1):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        owner = await _member(guild, member.id if member else actor.id)
        posts, total = await store.wall_view(guild.id, actor.id, owner.id, page)
        await _finish(interaction, f"{safe(owner.display_name)} · wall · page {page}/{max(1, math.ceil(total / 3))}",
                      embeds=await _wall_embeds(guild, posts), view=views.wall_view(guild.id, posts))
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@wall.command(name="post", description="Write on a member wall, or open the message form.")
@app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
async def wall_post(interaction: discord.Interaction, member: discord.Member, text: app_commands.Range[str, 1, 800] | None = None):
    if text is None:
        return await interaction.response.send_modal(views.WallPostModal(interaction.guild_id, member.id))
    await submit_wall_post(interaction, interaction.guild_id, member.id, text)


async def submit_wall_post(interaction, guild_id, owner_id, text):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction, guild_id)
        await _member(guild, owner_id)
        post = await store.wall_post(guild.id, actor.id, owner_id, text)
        await _finish(interaction, "Wall post saved.", embeds=await _wall_embeds(guild, [post]), view=views.wall_view(guild.id, [post]))
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@wall.command(name="comments", description="Read comments while enforcing the wall's current privacy.")
async def wall_comments(interaction: discord.Interaction, post: str, page: app_commands.Range[int, 1, 20] = 1):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        item = await store.wall_get(guild.id, actor.id, post)
        await _member(guild, item["owner"])
        comments = item.get("comments", [])
        embed = discord.Embed(title=f"Comments · {post} · page {page}/{max(1, math.ceil(len(comments) / 5))}", color=0xD58BFF)
        for comment in comments[(page - 1) * 5:page * 5]:
            try:
                name = safe((await _member(guild, comment["author"])).display_name)
            except ValueError:
                name = "Former member"
            embed.add_field(name=name[:200], value=safe(comment["text"]), inline=False)
        if not embed.fields:
            embed.description = "No comments on this page."
        await _finish(interaction, embed=embed, view=views.wall_view(guild.id, [item]))
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@wall.command(name="privacy", description="Control who can view, post, like and comment on your wall.")
@app_commands.choices(value=[app_commands.Choice(name="All server members", value="all"),
                            app_commands.Choice(name="Friends", value="friends"), app_commands.Choice(name="Only me", value="nobody")])
async def wall_privacy(interaction: discord.Interaction, value: str):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        await store.wall_privacy(guild.id, actor.id, value)
        await _finish(interaction, "Wall privacy updated. Existing controls will enforce this setting immediately.")
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


async def submit_wall_comment(interaction, guild_id, token, text):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction, guild_id)
        post = await store.wall_get(guild.id, actor.id, token)
        await _member(guild, post["owner"])
        post = await store.wall_comment(guild.id, actor.id, token, text)
        await _finish(interaction, "Comment saved.", embeds=await _wall_embeds(guild, [post]), view=views.wall_view(guild.id, [post]))
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@status.command(name="set", description="Set a short profile status and emoji mood; empty values clear them.")
async def status_set(interaction: discord.Interaction, text: app_commands.Range[str, 0, 120] = "", mood: app_commands.Range[str, 0, 32] = ""):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        await store.status_set(guild.id, actor.id, text, mood)
        await _finish(interaction, "Profile status saved.")
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@status.command(name="relationship", description="Request a relationship that requires the other member's consent.")
async def status_relationship(interaction: discord.Interaction, member: discord.Member):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        target = await _member(guild, member.id)
        request = await store.relationship_request(guild.id, actor.id, target.id)
        delivered = await _request_dm(target, f"{safe(actor.display_name)} requested a relationship on **{safe(guild.name)}**.",
                                      views.request_view(guild.id, request["token"], relationship=True))
        await _finish(interaction, "Relationship request sent." if delivered else "Request saved. The recipient can respond using /status pending.")
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@status.command(name="pending", description="Privately view and respond to your pending relationship request.")
async def status_pending(interaction: discord.Interaction):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        own = await hub_store.get(guild.id, store.NS, f"member:{actor.id}", {})
        token = own.get("relationship_request")
        request = await hub_store.get(guild.id, store.NS, f"relationship:{token}") if token else None
        if not request or request.get("state") != "pending":
            return await _finish(interaction, "No pending relationship request.")
        other = request["sender"] if request["target"] == actor.id else request["target"]
        target = await _member(guild, other)
        controls = views.request_view(guild.id, token, relationship=True) if request["target"] == actor.id else None
        await _finish(interaction, f"Relationship request with {link(target)}. Use /status single to cancel.", view=controls)
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@status.command(name="single", description="Clear your relationship or pending request for both members.")
async def status_single(interaction: discord.Interaction):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        await store.relationship_single(guild.id, actor.id)
        await _finish(interaction, "Your relationship and pending request were cleared.")
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@crush.command(name="pick", description="Privately choose a member; at most three distinct choices each ISO week.")
async def crush_pick(interaction: discord.Interaction, member: discord.Member):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        await _member(guild, member.id)
        result = await store.crush_pick(guild.id, actor.id, member.id)
        await _finish(interaction, f"Private selection saved · {result['used']}/3 distinct choices used this week. "
                      "A mutual selection sends a private notification to both members.")
        await deliver_outbox(guild, limit=2)
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@crush.command(name="show", description="Show only your own private selection and weekly choices.")
async def crush_show(interaction: discord.Interaction):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        item = await store.crush_show(guild.id, actor.id)
        lines = [f"Your choices · ISO week {item['week']} (UTC) · {len(item['choices'])}/3"]
        for target in item["choices"]:
            try:
                text = link(await _member(guild, target))
            except ValueError:
                text = "Former member"
            lines.append(("Current: " if target == item.get("current") else "Previous: ") + text)
        await _finish(interaction, "\n".join(lines) + ("\nNo current selection." if item.get("current") is None else ""))
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@crush.command(name="clear", description="Clear your private selection without refunding a distinct weekly choice.")
async def crush_clear(interaction: discord.Interaction):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        await store.crush_clear(guild.id, actor.id)
        await _finish(interaction, "Private selection cleared. Distinct weekly choices remain counted.")
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@poke.command(name="send", description="Poke another member, at most once a minute for that directed pair.")
async def poke_send(interaction: discord.Interaction, member: discord.Member):
    await _send_poke(interaction, member.id)


async def _send_poke(interaction, target_id, guild_id=None):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction, guild_id)
        await _member(guild, target_id)
        await store.poke_send(guild.id, actor.id, target_id)
        await _finish(interaction, "Poke saved. Its notification will be delivered to the configured social channel or by DM.")
        await deliver_outbox(guild, limit=2)
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


@poke.command(name="stats", description="See your directed pair stats or this server's top poke pairs.")
async def poke_stats(interaction: discord.Interaction, member: discord.Member | None = None):
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction)
        if member:
            await _member(guild, member.id)
            item = await store.poke_stats(guild.id, actor.id, member.id)
            return await _finish(interaction, f"Pokes with {link(member)}\nSent: {item['sent']} · Received: {item['received']}")
        rows = await store.poke_stats(guild.id)
        lines = ["Server poke pairs"]
        for item in rows:
            try:
                first, second = await _member(guild, item["sender"]), await _member(guild, item["target"])
                lines.append(f"{link(first)} → {link(second)} · {item['count']}")
            except ValueError:
                continue
        await _finish(interaction, embed=discord.Embed(title="Poke leaderboard", description="\n".join(lines), color=0xD58BFF))
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


async def handle_action(interaction, action, guild_id, token):
    if action in {"friendaccept", "friendreject"}:
        try:
            first, second = map(int, token.split("_"))
            other = second if interaction.user.id == first else first
        except (ValueError, TypeError):
            await _begin(interaction)
            return await _finish(interaction, "Invalid friend request control.")
        return await _friend_response(interaction, other, action == "friendaccept", guild_id)
    if action == "poke":
        try:
            sender, recipient = map(int, token.split("_"))
            if interaction.user.id != recipient:
                raise ValueError("This return poke belongs to another member.")
        except (ValueError, TypeError) as error:
            await _begin(interaction)
            return await _finish(interaction, safe(error))
        return await _send_poke(interaction, sender, guild_id)
    if action == "wallcomment":
        try:
            guild, actor = await _context(interaction, guild_id)
            post = await store.wall_get(guild.id, actor.id, token)
            await _member(guild, post["owner"])
        except (ValueError, hub_store.Unavailable) as error:
            await _begin(interaction)
            return await _error(interaction, error)
        return await interaction.response.send_modal(views.WallCommentModal(guild_id, token))
    await _begin(interaction)
    try:
        guild, actor = await _context(interaction, guild_id)
        if action in {"relationaccept", "relationreject"}:
            request = await hub_store.get(guild.id, store.NS, f"relationship:{token}")
            if not request:
                raise ValueError("This relationship request is unavailable.")
            await _member(guild, request["sender"])
            await _member(guild, request["target"])
            await store.relationship_respond(guild.id, actor.id, token, action == "relationaccept")
            return await _finish(interaction, "Relationship accepted." if action == "relationaccept" else "Relationship request rejected.")
        if action in {"walllike", "walldelete"}:
            post = await store.wall_get(guild.id, actor.id, token)
            await _member(guild, post["owner"])
            if action == "walldelete":
                await store.wall_delete(guild.id, actor.id, token)
                return await _finish(interaction, "Wall post deleted.")
            post = await store.wall_like(guild.id, actor.id, token)
            return await _finish(interaction, "Like updated.", embeds=await _wall_embeds(guild, [post]), view=views.wall_view(guild.id, [post]))
        await _finish(interaction, "This social control is unavailable.")
    except (ValueError, hub_store.Unavailable) as error:
        await _error(interaction, error)


async def deliver_outbox(guild, limit=5):
    from core import guild_settings
    from utils import helpers
    settings = await hub_store.get(guild.id, "settings", "guild", {})
    if settings.get("social_enabled", True) is False:
        return 0
    jobs = await store.pending_outbox(guild.id, limit=limit)
    delivered = 0
    for pending in jobs:
        job = await store.claim_notification(guild.id, pending["key"])
        if not job:
            continue
        success = False
        try:
            recipient, other = await _member(guild, job["recipient"]), await _member(guild, job["other"])
            controls = None
            if job["kind"] == "crush":
                text = f"Mutual match on **{safe(guild.name)}**: you and {safe(other.display_name)} chose each other."
                channel = await recipient.create_dm()
            else:
                text = f"{safe(other.display_name)} poked {safe(recipient.display_name)} on **{safe(guild.name)}**."
                controls = views.poke_view(guild.id, other.id, recipient.id)
                settings = await guild_settings.get(guild.id)
                channel = guild.get_channel(settings.get("social_channel_id") or 0)
                if (not isinstance(channel, discord.TextChannel) or not channel.permissions_for(recipient).view_channel
                        or not channel.permissions_for(other).view_channel or not channel.permissions_for(guild.me).send_messages):
                    channel = await recipient.create_dm()
            nonce = str(int.from_bytes(hashlib.sha256(f"{guild.id}:{job['key']}".encode()).digest()[:8], "big"))
            await helpers._send_message_once(channel, text, nonce, view=controls)
            success, delivered = True, delivered + 1
        except (discord.DiscordException, ValueError, TimeoutError):
            pass
        await store.finish_notification(guild.id, job["key"], success)
    return delivered


for group in (friend, wall, status, crush, poke):
    config.bot.tree.add_command(group)
