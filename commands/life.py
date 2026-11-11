from __future__ import annotations

import asyncio
from datetime import date, timedelta
from io import BytesIO
import logging
import re
import time

import discord
from discord import app_commands

from app import life as runtime
from core import config, guild_settings, life_models as models, life_store as store


_log = logging.getLogger(__name__)
memories = app_commands.Group(name="memories", description="Opt-in messages and verified memories from earlier years.", guild_only=True)
birthday = app_commands.Group(name="birthday", description="Your birthday and upcoming community birthdays.", guild_only=True)
story = app_commands.Group(name="story", description="24-hour stories and voluntary viewer acknowledgements.", guild_only=True)
timeline = app_commands.Group(name="timeline", description="Newspapers grounded in accessible, opted-in saved messages.", guild_only=True)


async def _ack(interaction):
    if (interaction.guild is None or interaction.channel is None or interaction.user.bot
            or not interaction.permissions.view_channel):
        await interaction.response.send_message("Use this command as a member in an accessible server channel.",
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return False
    await interaction.response.defer(ephemeral=True, thinking=True)
    return True


async def _reply(interaction, text=None, *, embed=None):
    await interaction.edit_original_response(content=text, embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _failure(interaction, error):
    if isinstance(error, ValueError):
        text = str(error)[:1800]
    elif isinstance(error, store.db.Unavailable):
        text = "Persistent storage is unavailable. Nothing can be published or saved right now."
    else:
        _log.warning("Community action unavailable: %s", type(error).__name__)
        text = "This action could not be completed. Please try again later."
    await _reply(interaction, text)


async def _settings(interaction, feature):
    value = await guild_settings.get(interaction.guild_id)
    if not value.get(feature + "_enabled"):
        raise ValueError("This feature is disabled. A server manager can configure it with /setup.")
    return value


class ConfessionModal(discord.ui.Modal, title="Anonymous confession"):
    text = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, min_length=1,
                               max_length=1800, placeholder="No insults, threats or identifying details. Moderators can identify abuse.")

    def __init__(self, *, reply_to=None, parent_message=None):
        super().__init__(timeout=300)
        self.reply_to = reply_to
        self.parent_message = parent_message

    async def on_submit(self, interaction):
        if not await _ack(interaction):
            return
        try:
            settings = await _settings(interaction, "confessions")
            destination = await runtime.channel_for(interaction.guild, settings.get("confessions_channel_id"))
            log = await runtime.channel_for(interaction.guild, settings.get("anonymous_log_channel_id"))
            if not runtime.publishable(destination, interaction.guild) or not await runtime.readable(destination, interaction.user):
                raise ValueError("The confession channel is unavailable or inaccessible to you.")
            if not runtime.private_log(log, interaction.guild):
                raise ValueError("A private moderator-only anonymous log must be configured before posting.")
            content = str(self.text).strip()
            if not await models.moderate_confession(content):
                raise ValueError("This submission could not pass the safety check. Insults, threats and identifying details are not accepted; the moderation provider must be available.")
            parent = None
            if self.reply_to:
                parent = await store.confession(interaction.guild_id, self.reply_to)
                if (not parent or parent.get("message_id") != self.parent_message
                        or parent.get("channel_id") != destination.id):
                    raise ValueError("The original confession is unavailable.")
                root_channel = destination
                original = await root_channel.fetch_message(parent["message_id"])
                destination = original.thread or interaction.guild.get_thread(original.id)
                if destination is None:
                    permissions = root_channel.permissions_for(interaction.guild.me)
                    if not permissions.create_public_threads or not permissions.send_messages_in_threads:
                        raise ValueError("Vivian needs public thread permissions to post anonymous replies.")
                    destination = await original.create_thread(name=f"Confession #{self.reply_to} replies", auto_archive_duration=1440)
                if not runtime.publishable(destination, interaction.guild):
                    raise ValueError("The reply thread is unavailable.")
            item = await store.reserve_confession(interaction.guild_id, interaction.user.id, destination.id,
                                                  reply_to=self.reply_to)
            embed = discord.Embed(title=f"Confession #{item['number']}", description=runtime.safe(content, 3500),
                                  color=config.VIVIAN_ACCENT)
            embed.set_footer(text=f"Confession #{item['number']} · Publicly anonymous; abuse is accountable to moderators")
            view = AnonymousReplyView() if parent is None else None
            message = await destination.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none(), nonce=str(interaction.id))
            try:
                item.update(message_id=message.id, thread_id=destination.id if parent else None)
                await store.save_confession(interaction.guild_id, item)
                log = await interaction.guild.fetch_channel(log.id)
                if not runtime.private_log(log, interaction.guild):
                    raise ValueError("The moderator log is no longer private; the anonymous post was withdrawn.")
                await log.send(f"Confession #{item['number']} · author ID {interaction.user.id}\n{message.jump_url}",
                               allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                try:
                    await message.delete()
                except discord.DiscordException:
                    pass
                raise
            await _reply(interaction, f"Confession #{item['number']} was posted anonymously. Moderator accountability is stored privately.")
        except Exception as error:
            await _failure(interaction, error)


class AnonymousReplyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Reply anonymously", style=discord.ButtonStyle.secondary, custom_id="vivian:confession:reply")
    async def reply(self, interaction, button):
        message = interaction.message
        bot_id = getattr(config.bot.user, "id", None)
        footer = message.embeds[0].footer.text if message and message.embeds else ""
        match = re.match(r"Confession #(\d+) ·", footer or "")
        if (interaction.guild is None or not message or message.author.id != bot_id or not match
                or interaction.user.bot or not interaction.permissions.view_channel):
            await interaction.response.send_message("This confession is unavailable.", ephemeral=True)
            return
        await interaction.response.send_modal(ConfessionModal(reply_to=int(match[1]), parent_message=message.id))


class StoryViewedView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Mark viewed (voluntary)", style=discord.ButtonStyle.secondary, custom_id="vivian:story:viewed")
    async def viewed(self, interaction, button):
        if not await _ack(interaction):
            return
        try:
            await _settings(interaction, "stories")
            mapping = await store.db.get(interaction.guild_id, store.NAMESPACE, f"story-message:{interaction.message.id}")
            if not mapping:
                raise ValueError("This story is unavailable.")
            item = await store.get_story(interaction.guild_id, mapping["id"])
            if not item or item.get("message_id") != interaction.message.id or item.get("channel_id") != interaction.channel_id:
                raise ValueError("This story is unavailable in this channel.")
            await store.viewed(interaction.guild_id, item["id"], interaction.user.id)
            await _reply(interaction, "Your voluntary acknowledgement was saved. The story author can see that you clicked; ordinary views are not tracked.")
        except Exception as error:
            await _failure(interaction, error)


class LegacyStoryButton(discord.ui.DynamicItem[discord.ui.Button],
                        template=r"vivian:story:(?P<action>seen|viewers):(?P<story_id>[0-9]{1,20})"):
    def __init__(self, item, action, story_id):
        super().__init__(item)
        self.action = action
        self.story_id = f"legacy-{story_id}"

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(item, match["action"], match["story_id"])

    async def callback(self, interaction):
        if not await _ack(interaction):
            return
        try:
            await _settings(interaction, "stories")
            item = await store.get_story(interaction.guild_id, self.story_id)
            if (not item or not interaction.message or item.get("message_id") != interaction.message.id
                    or item.get("channel_id") != interaction.channel_id or item.get("status") != "active"
                    or item.get("expires_at", 0) <= time.time()):
                raise ValueError("This story has expired or is unavailable.")
            if self.action == "seen":
                await store.viewed(interaction.guild_id, self.story_id, interaction.user.id)
                await _reply(interaction, "Your voluntary acknowledgement was saved. Only the author can view it.")
            else:
                await _show_story_viewers(interaction, item, page=1)
        except Exception as error:
            await _failure(interaction, error)


def register_views():
    if not getattr(config.bot, "life_views_registered", False):
        config.bot.add_view(AnonymousReplyView())
        config.bot.add_view(StoryViewedView())
        config.bot.add_dynamic_items(LegacyStoryButton)
        config.bot.life_views_registered = True


@config.bot.tree.command(name="confess", description="Open a publicly anonymous, moderated confession form.")
@app_commands.guild_only()
async def confess(interaction: discord.Interaction):
    if interaction.user.bot or interaction.guild is None or not interaction.permissions.view_channel:
        return await interaction.response.send_message("Use this in an accessible server channel.", ephemeral=True)
    await interaction.response.send_modal(ConfessionModal())


async def own_recent_messages(guild, member, settings):
    messages = []
    for channel_id in settings.get("archive_channel_ids", [])[:10]:
        channel = await runtime.channel_for(guild, channel_id)
        if not await runtime.readable(channel, member) or not await runtime.readable(channel, guild.me):
            continue
        async for message in channel.history(limit=200):
            if message.author.id == member.id and not message.author.bot and message.content.strip():
                messages.append((message.created_at.timestamp(), message.content[:350]))
    return [text for _, text in sorted(messages, key=lambda item: item[0], reverse=True)[:50]][::-1]


@config.bot.tree.command(name="vibecheck", description="Rate the style of only your own recent messages with explicit provider consent.")
@app_commands.guild_only()
@app_commands.describe(consent="Allow sending up to 50 of your own recent text messages to Groq for this request")
async def vibecheck(interaction: discord.Interaction, consent: bool = False):
    if not await _ack(interaction):
        return
    reserved = False
    try:
        settings = await _settings(interaction, "vibecheck")
        if not consent:
            raise ValueError("Run /vibecheck consent:true to send only your own recent messages from readable configured channels to Groq. This is entertainment, not a personality assessment.")
        await store.reserve_vibe(interaction.guild_id, interaction.user.id, interaction.id)
        reserved = True
        async with asyncio.timeout(20):
            messages = await own_recent_messages(interaction.guild, interaction.user, settings)
        if not messages:
            raise ValueError("No recent messages of yours were found in the configured readable channels.")
        result = await models.vibe(messages)
        await store.finish_vibe(interaction.guild_id, interaction.user.id, interaction.id, success=True)
        reserved = False
        embed = discord.Embed(title=f"Writing vibe · {result['rating']}/10", color=config.VIVIAN_ACCENT,
                              description=f"**{runtime.safe(result['archetype'], 160)}**\n{runtime.safe(result['joke'], 400)}")
        embed.set_footer(text=f"Entertainment only · based on {len(messages)} of your own messages · available again in one hour")
        await _reply(interaction, embed=embed)
    except Exception as error:
        if reserved:
            try:
                await store.finish_vibe(interaction.guild_id, interaction.user.id, interaction.id, success=False)
            except Exception:
                pass
        await _failure(interaction, error)


@memories.command(name="consent", description="Allow or stop saving your future messages in configured journal channels.")
@app_commands.describe(enabled="Allow retaining my future messages for up to 3 years, search, public memories and optional provider summaries")
async def memory_consent(interaction: discord.Interaction, enabled: bool):
    if not await _ack(interaction):
        return
    try:
        removed = await store.consent(interaction.guild_id, interaction.user.id, enabled)
        await _reply(interaction, "Opt-in enabled. Only future messages in explicitly configured journal channels may be saved, for at most three years. Accessible quotes may appear in search, memories, newspapers and optional provider summaries. You can withdraw anytime."
                     if enabled else f"Opt-in withdrawn. Removed {removed} saved messages; messages you write later will not be archived.")
    except Exception as error:
        await _failure(interaction, error)


@memories.command(name="now", description="Show verified, accessible memories from this calendar date in earlier years.")
async def memories_now(interaction: discord.Interaction):
    if not await _ack(interaction):
        return
    try:
        settings = await _settings(interaction, "memories")
        quotes = await runtime.memory_candidates(interaction.guild, settings, store.local_now(settings).date())
        await _reply(interaction, embed=runtime.memory_embed(quotes))
    except Exception as error:
        await _failure(interaction, error)


@birthday.command(name="set", description="Opt in to a public birthday celebration; birth year stays private.")
async def birthday_set(interaction: discord.Interaction, day: app_commands.Range[int, 1, 31],
                       month: app_commands.Range[int, 1, 12], year: app_commands.Range[int, 1900, 2200] | None = None):
    if not await _ack(interaction):
        return
    try:
        await _settings(interaction, "birthdays")
        await store.set_birthday(interaction.guild_id, interaction.user.id, day, month, year)
        await _reply(interaction, f"Birthday saved: {day:02d}/{month:02d}. The server may announce this date publicly; your year and age are never published. February 29 is celebrated on March 1 in non-leap years.")
    except Exception as error:
        await _failure(interaction, error)


@birthday.command(name="remove", description="Erase your stored birthday and stop future celebrations.")
async def birthday_remove(interaction: discord.Interaction):
    if not await _ack(interaction):
        return
    try:
        await store.db.delete(interaction.guild_id, store.NAMESPACE, f"birthday:{interaction.user.id}")
        await _reply(interaction, "Your birthday was erased. Any temporary role already granted will expire after its original 24 hours.")
    except Exception as error:
        await _failure(interaction, error)


@birthday.command(name="list", description="List upcoming birthdays without birth years or ages.")
async def birthday_list(interaction: discord.Interaction, page: app_commands.Range[int, 1, 500] = 1):
    if not await _ack(interaction):
        return
    try:
        settings = await _settings(interaction, "birthdays")
        today = store.local_now(settings).date()
        items = [item for item in await store.birthdays(interaction.guild_id, today)
                 if interaction.guild.get_member(item["user_id"]) is not None]
        lines = []
        for item in items[(page - 1) * 15:page * 15]:
            member = interaction.guild.get_member(item["user_id"])
            lines.append(f"{runtime.safe(member.display_name, 80)} · {store.next_birthday(item['month'], item['day'], today):%d %b}")
        await _reply(interaction, "Upcoming birthdays\n" + ("\n".join(lines) or "No birthdays on this page.")
                     + f"\nPage {page}/{max(1, (len(items) + 14) // 15)}")
    except Exception as error:
        await _failure(interaction, error)


async def attachment_file(attachment, limit):
    supported = {"image/png", "image/jpeg", "image/gif", "image/webp", "video/mp4", "video/webm"}
    if attachment.content_type not in supported or not 0 < attachment.size <= min(limit, 8 * 1024 * 1024):
        raise ValueError("Use a PNG, JPEG, GIF, WebP, MP4 or WebM attachment no larger than 8 MB.")
    async with asyncio.timeout(10):
        data = await attachment.read()
    if len(data) > min(limit, 8 * 1024 * 1024):
        raise ValueError("The attachment exceeds the upload limit.")
    signatures = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"), "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        "video/mp4": data[4:8] == b"ftyp", "video/webm": data.startswith(b"\x1aE\xdf\xa3"),
    }
    if not signatures[attachment.content_type]:
        raise ValueError("The attachment content does not match its declared media type.")
    extension = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "video/mp4": "mp4", "video/webm": "webm"}[attachment.content_type]
    return discord.File(BytesIO(data), filename=f"story.{extension}")


@story.command(name="post", description="Publish your text or media for 24 hours; viewer clicks are voluntary.")
async def story_post(interaction: discord.Interaction, text: app_commands.Range[str, 1, 1800] | None = None,
                     attachment: discord.Attachment | None = None):
    if not await _ack(interaction):
        return
    file = None
    try:
        settings = await _settings(interaction, "stories")
        if not text and attachment is None:
            raise ValueError("Include text or a media attachment.")
        channel = await runtime.channel_for(interaction.guild, settings.get("stories_channel_id"))
        if not runtime.publishable(channel, interaction.guild) or not await runtime.readable(channel, interaction.user):
            raise ValueError("The story channel is unavailable or inaccessible to you.")
        if attachment:
            if not channel.permissions_for(interaction.guild.me).attach_files:
                raise ValueError("Vivian needs Attach Files in the story channel.")
            file = await attachment_file(attachment, interaction.guild.filesize_limit)
        item = {"id": str(interaction.id), "author_id": interaction.user.id, "channel_id": channel.id,
                "message_id": None, "created_at": int(time.time()), "expires_at": int(time.time() + 86400),
                "status": "pending", "viewers": {}}
        await store.reserve_story(interaction.guild_id, item)
        embed = discord.Embed(title=f"Story · {runtime.safe(interaction.user.display_name, 80)}",
                              description=runtime.safe(text or "", 3500), color=config.VIVIAN_ACCENT)
        embed.set_footer(text=f"Story {item['id']} · expires in 24 hours · only voluntary viewer clicks are recorded")
        message = await channel.send(embed=embed, file=file if file else discord.utils.MISSING,
                                     view=StoryViewedView(), allowed_mentions=discord.AllowedMentions.none(), nonce=item["id"])
        try:
            item.update(message_id=message.id, status="active")
            await store.save_story(interaction.guild_id, item["id"], item)
            await store.db.put(interaction.guild_id, store.NAMESPACE, f"story-message:{message.id}", {"id": item["id"]})
        except Exception:
            try:
                await message.delete()
            except discord.DiscordException:
                pass
            raise
        await _reply(interaction, f"Story {item['id']} published: {message.jump_url}\nIt expires <t:{item['expires_at']}:R>. Media was uploaded as a new bot attachment.")
    except Exception as error:
        await _failure(interaction, error)
    finally:
        if file:
            file.close()


@story.command(name="viewers", description="Privately list voluntary viewer clicks on your own story.")
async def story_viewers(interaction: discord.Interaction, story_id: str, page: app_commands.Range[int, 1, 100] = 1):
    if not await _ack(interaction):
        return
    try:
        item = await store.get_story(interaction.guild_id, story_id)
        await _show_story_viewers(interaction, item, page)
    except Exception as error:
        await _failure(interaction, error)


async def _show_story_viewers(interaction, item, page):
    if not item or item.get("author_id") != interaction.user.id:
        raise ValueError("Only the author can inspect a story's voluntary viewer list.")
    viewers = sorted(item.get("viewers", {}).items(), key=lambda item: item[1])
    lines = []
    for user_id, timestamp in viewers[(page - 1) * 15:page * 15]:
        member = interaction.guild.get_member(int(user_id))
        lines.append(f"{runtime.safe(member.display_name, 80) if member else 'Former member'} · <t:{int(timestamp)}:R>")
    await _reply(interaction, "Voluntary viewer clicks\n" + ("\n".join(lines) or "No acknowledgements on this page.")
                 + "\nOrdinary views are not tracked." + f"\nPage {page}/{max(1, (len(viewers) + 14) // 15)}")


@timeline.command(name="month", description="Build a verified monthly newspaper from opted-in saved messages.")
async def timeline_month(interaction: discord.Interaction, month: str | None = None):
    if not await _ack(interaction):
        return
    try:
        settings = await _settings(interaction, "timeline")
        if month and not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("Use a month in YYYY-MM format.")
        first = date.fromisoformat(month + "-01") if month else store.local_now(settings).date().replace(day=1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        await _reply(interaction, embed=await runtime.newspaper(interaction.guild, settings, first, last, "Vivian · Monthly Timeline"))
    except Exception as error:
        await _failure(interaction, error)


@timeline.command(name="week", description="Build a weekly gazette from the last seven calendar days.")
async def timeline_week(interaction: discord.Interaction):
    if not await _ack(interaction):
        return
    try:
        settings = await _settings(interaction, "timeline")
        last = store.local_now(settings).date() + timedelta(days=1)
        await _reply(interaction, embed=await runtime.newspaper(interaction.guild, settings, last - timedelta(days=7), last, "Vivian · Weekly Gazette"))
    except Exception as error:
        await _failure(interaction, error)


@config.bot.tree.command(name="ask", description="Search opted-in saved messages with verified accessible quotes.")
@app_commands.guild_only()
@app_commands.describe(summary="Also send these accessible quotes to Groq for a grounded summary")
async def ask(interaction: discord.Interaction, question: app_commands.Range[str, 2, 300], summary: bool = False):
    if not await _ack(interaction):
        return
    try:
        settings = await _settings(interaction, "ask")
        candidates = await store.search_records(interaction.guild_id, question)
        quotes = []
        for candidate in candidates:
            quote = await runtime.current_quote(interaction.guild, candidate, member=interaction.user, settings=settings)
            if quote and store.search([quote], question):
                quotes.append(quote)
            if len(quotes) >= 5:
                break
        if not quotes:
            raise ValueError("No matching, currently accessible saved messages were found. Only explicitly opted-in future messages can be searched.")
        embed = discord.Embed(title="Vivian · Verified search", color=config.VIVIAN_ACCENT)
        if summary:
            answer, citations = await models.summarize(question, [quote["content"] for quote in quotes])
            embed.description = runtime.safe(answer, 1200) + "\nSources: " + ", ".join(f"[{index}]({quotes[index - 1]['link']})" for index in citations)
        for index, quote in enumerate(quotes, 1):
            embed.add_field(name=f"Source {index}", value=runtime.safe(quote["content"], 550) + f"\n[Original]({quote['link']})", inline=False)
        embed.set_footer(text="Original messages were re-fetched and permissions checked for this request.")
        await _reply(interaction, embed=embed)
    except Exception as error:
        await _failure(interaction, error)


for group in (memories, birthday, story, timeline):
    config.bot.tree.add_command(group)
