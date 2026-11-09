from __future__ import annotations

import os
import time

import discord
from discord import app_commands

from core import community_play as store, config, economy_store, guild_settings, hub_store, voice_speech
from utils import helpers, hub_ui as ui


game = app_commands.Group(name="game", description="Cooperative word hunts with persistent controls", guild_only=True)
voice = app_commands.Group(name="voice", description="Voice participation and optional speech", guild_only=True)
profile = app_commands.Group(name="profile", description="Choose whether to publish your web profile", guild_only=True)
persona = app_commands.Group(name="persona", description="Server character and memory settings", guild_only=True)


class JoinGame(discord.ui.DynamicItem[discord.ui.Button], template=r"vivian:game:(?P<guild>[0-9]+):(?P<channel>[0-9]+):(?P<token>[a-f0-9]{12})"):
    def __init__(self, guild_id, channel_id, token):
        self.guild_id, self.channel_id, self.token = guild_id, channel_id, token
        super().__init__(discord.ui.Button(label="Join word hunt", style=discord.ButtonStyle.primary,
                                          custom_id=f"vivian:game:{guild_id}:{channel_id}:{token}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild"]), int(match["channel"]), match["token"])

    async def callback(self, interaction):
        if not await ui.defer(interaction):
            return
        try:
            if interaction.guild_id != self.guild_id or interaction.channel_id != self.channel_id:
                raise ValueError("This hunt belongs to another channel.")
            settings = await guild_settings.get(self.guild_id)
            if not settings.get("games_enabled"):
                raise ValueError("Games are disabled on this server.")
            current = await hub_store.get(self.guild_id, "community_play", f"game:{self.channel_id}", {})
            if getattr(interaction.message, "id", None) != current.get("message_id"):
                raise ValueError("This hunt control is no longer active.")
            count = await store.join(self.guild_id, self.channel_id, interaction.user.id, self.token)
            await ui.send(interaction, f"Joined. Players: {count}. Type your answer in this channel; one guess every 5 seconds.")
        except Exception as error:
            await ui.error(interaction, error)


async def publish_game(guild, channel, owner_id, *, automatic=False):
    permissions = channel.permissions_for(guild.me) if guild.me else None
    if not permissions or not permissions.view_channel or not permissions.send_messages:
        raise ValueError("Vivian cannot post a game in this channel.")
    settings = await guild_settings.get(guild.id)
    if not settings.get("games_enabled"):
        raise ValueError("Games are disabled on this server.")
    item = await store.start_game(guild.id, channel.id, owner_id, automatic=automatic)
    view = discord.ui.View(timeout=None)
    view.add_item(JoinGame(guild.id, channel.id, item["token"]))
    text = f"**Vivian / Word hunt**\n{item['question']}\n\n" + ("Everyone can guess." if automatic else "Join below, then type your answer.") + " First correct answer wins. One guess every 5 seconds.\nCloses in one hour."
    try:
        sent = await helpers._send_message_once(channel, text, store.nonce("start:" + item["token"]), view=view)
    except Exception:
        def failed(value):
            if value.get("token") == item["token"]:
                value["state"] = "uncertain"
        await hub_store.mutate(guild.id, "community_play", f"game:{channel.id}", failed)
        raise ValueError("Game publication was not confirmed. Check this channel before starting another hunt.") from None

    def confirmed(value):
        if value.get("token") == item["token"]:
            value.update(state="active", message_id=int(sent["id"]))
    await hub_store.mutate(guild.id, "community_play", f"game:{channel.id}", confirmed)
    return item


@game.command(name="start", description="Start a word hunt in this channel")
@app_commands.checks.cooldown(1, 60, key=lambda interaction: interaction.guild_id)
async def start(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        if not isinstance(interaction.channel, discord.TextChannel) or not interaction.permissions.send_messages:
            raise ValueError("Start a word hunt in a text channel where you can send messages.")
        await publish_game(interaction.guild, interaction.channel, interaction.user.id)
        await ui.send(interaction, "The word hunt is ready in this channel.")
    except Exception as error:
        await ui.error(interaction, error)


@game.command(name="join", description="Join the current word hunt")
async def join(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        current = await hub_store.get(interaction.guild_id, "community_play", f"game:{interaction.channel_id}", {})
        count = await store.join(interaction.guild_id, interaction.channel_id, interaction.user.id, current.get("token", ""))
        await ui.send(interaction, f"Joined. Players: {count}.")
    except Exception as error:
        await ui.error(interaction, error)


@game.command(name="stop", description="Close a word hunt you host")
async def stop(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        await store.stop_game(interaction.guild_id, interaction.channel_id, interaction.user.id, manager=ui.manager(interaction))
        await ui.send(interaction, "Word hunt stopped.")
    except Exception as error:
        await ui.error(interaction, error)


@voice.command(name="stats", description="Show your saved participation time and voice branch")
async def voice_stats(interaction: discord.Interaction, member: discord.Member | None = None):
    if not await ui.defer(interaction):
        return
    try:
        target = member or interaction.user
        data = await economy_store.profile_summary(interaction.guild_id, target.id)
        seconds = int(data["voice_seconds"])
        await ui.send(interaction, f"{ui.safe(target.display_name)} · {seconds // 3600}h {seconds // 60 % 60}m\nVoice branch: {data['branches']['voice']} points. Counts eligible time with at least two human participants; deafened and AFK time is excluded.")
    except Exception as error:
        await ui.error(interaction, error)


@voice.command(name="speak", description="Read a short text in your current voice channel")
@app_commands.checks.cooldown(1, 60, key=lambda interaction: interaction.user.id)
async def speak(interaction: discord.Interaction, text: app_commands.Range[str, 1, 300]):
    if not await ui.defer(interaction):
        return
    try:
        settings = await guild_settings.get(interaction.guild_id)
        if not settings.get("voice_tts_enabled"):
            raise ValueError("Voice speech is disabled on this server.")
        state = interaction.user.voice
        channel = state.channel if state else None
        if not isinstance(channel, discord.VoiceChannel):
            raise ValueError("Join a voice channel first.")
        await ui.send(interaction, "Preparing speech…")
        await voice_speech.play(interaction.guild, channel, text,
                               still_allowed=lambda: bool(interaction.user.voice and interaction.user.voice.channel == channel))
        await ui.send(interaction, "Speech finished.")
    except Exception as error:
        await ui.error(interaction, error)


@profile.command(name="publish", description="Choose whether your profile can be viewed on the public website")
async def publish_profile(interaction: discord.Interaction, enabled: bool):
    if not await ui.defer(interaction):
        return
    try:
        def change(value):
            value["public"] = enabled
            if not enabled:
                value.update(share_friends=False, share_wall=False)

        await hub_store.mutate(interaction.guild_id, "profile_settings", str(interaction.user.id), change)
        text = ("You opted in to publishing your display name, avatar, statistics, badges and status. The server must also enable public profiles. Use /profile sharing to choose whether to share friends or public wall activity."
                if enabled else "Your web profile is now private.")
        await ui.send(interaction, text)
    except Exception as error:
        await ui.error(interaction, error)


@profile.command(name="sharing", description="Choose whether to share your friends and public wall activity on the website")
@app_commands.describe(friends="Show friends who also opted in", wall="Share your posts and comments on public walls")
async def profile_sharing(interaction: discord.Interaction, friends: bool | None = None, wall: bool | None = None):
    if not await ui.defer(interaction):
        return
    try:
        key = str(interaction.user.id)
        if friends is None and wall is None:
            privacy = await hub_store.get(interaction.guild_id, "profile_settings", key, {})
        else:
            def change(value):
                if (friends is True or wall is True) and value.get("public") is not True:
                    raise ValueError("Enable your public profile with /profile publish first.")
                if friends is not None:
                    value["share_friends"] = friends
                if wall is not None:
                    value["share_wall"] = wall
                return dict(value)

            privacy = await hub_store.mutate(interaction.guild_id, "profile_settings", key, change)
        friend_state = "on" if privacy.get("share_friends") is True else "off"
        wall_state = "on" if privacy.get("share_wall") is True else "off"
        await ui.send(interaction, f"Share friends: **{friend_state}**. Share public wall activity: **{wall_state}**.\n"
                      "Friends must also share their lists. Wall posts and comments require a public wall and the author's own opt-in.")
    except Exception as error:
        await ui.error(interaction, error)


@profile.command(name="link", description="Show your public profile link when sharing is enabled")
async def profile_link(interaction: discord.Interaction):
    if not await ui.defer(interaction):
        return
    try:
        settings = await guild_settings.get(interaction.guild_id)
        privacy = await hub_store.get(interaction.guild_id, "profile_settings", str(interaction.user.id), {})
        if not settings.get("public_profiles_enabled") or not privacy.get("public"):
            raise ValueError("Enable your profile with /profile publish and ask a manager to enable public profiles.")
        website = os.getenv("PUBLIC_SITE_URL", "https://esil.life").rstrip("/")
        await ui.send(interaction, f"{website}/profile.html?guild={interaction.guild_id}&member={interaction.user.id}")
    except Exception as error:
        await ui.error(interaction, error)


@persona.command(name="style", description="Set the server character style")
async def style(interaction: discord.Interaction, text: app_commands.Range[str, 0, 1000]):
    if not await ui.defer(interaction):
        return
    try:
        if not ui.manager(interaction):
            raise ValueError("Manage Server permission is required.")
        await guild_settings.update(interaction.guild_id, {"server_style": text, "server_style_enabled": True})
        await ui.send(interaction, "Server character style saved. Observed vocabulary uses only member-opted-in archive messages.")
    except Exception as error:
        await ui.error(interaction, error)


for command in (game, voice, profile, persona):
    config.bot.tree.add_command(command)
