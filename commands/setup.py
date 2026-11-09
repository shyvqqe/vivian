from __future__ import annotations

import json
import os
import time

import discord
from discord import app_commands

from core import config, guild_settings, hub_models, hub_store, settings_validation
from utils import hub_ui as ui


setup = app_commands.Group(name="setup", description="Configure Vivian for this server", guild_only=True)
_CHANNELS = {key.removesuffix("_channel_id"): key for key in guild_settings.DEFAULTS if key.endswith("_channel_id") and key != "voice_channel_id"}
_MODULES = {key.removesuffix("_enabled"): key for key, value in guild_settings.DEFAULTS.items() if type(value) is bool and key.endswith("_enabled")}


async def _start(interaction):
    if not await ui.defer(interaction):
        return False
    if not ui.manager(interaction):
        await ui.send(interaction, "Manage Server permission is required.")
        return False
    return True


async def _apply(interaction, patch):
    value = await settings_validation.validate_patch(interaction.guild, patch)
    await guild_settings.update(interaction.guild_id, value)
    await ui.send(interaction, "Settings saved.\n```json\n" + json.dumps(value, ensure_ascii=False, indent=2)[:1600] + "\n```")


@setup.command(name="view", description="Show module settings and setup requirements")
async def view(interaction: discord.Interaction):
    if not await _start(interaction):
        return
    try:
        settings = await guild_settings.get(interaction.guild_id)
        enabled = [name for name, key in _MODULES.items() if settings.get(key)]
        embed = ui.card("Vivian / Server setup", "Enabled: " + (", ".join(enabled) or "none"))
        embed.add_field(name="Channels", value="\n".join(f"{name}: <#{settings[key]}>" for name, key in _CHANNELS.items() if settings.get(key)) or "Set /setup channel", inline=False)
        embed.add_field(name="Archive privacy", value="Archive is off by default. Select public channels, enable journal, and ask members to opt in with /memories consent. Deleted and opted-out messages are excluded.", inline=False)
        embed.add_field(name="Anonymous posts", value="Confessions require a configured private moderator log and Gemini moderation. Only the anonymous text appears publicly.", inline=False)
        base = (os.getenv("DASHBOARD_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
        if base.startswith("https://"):
            embed.add_field(name="Dashboard", value=f"[Open dashboard]({base}/dashboard)", inline=False)
        await ui.send(interaction, embed=embed)
    except Exception as error:
        await ui.error(interaction, error)


@setup.command(name="channel", description="Choose a channel for a module")
@app_commands.choices(feature=[app_commands.Choice(name=key, value=key) for key in _CHANNELS])
async def channel(interaction: discord.Interaction, feature: str, channel: discord.TextChannel):
    if not await _start(interaction):
        return
    try:
        if feature not in _CHANNELS:
            raise ValueError("Unknown channel feature.")
        await _apply(interaction, {_CHANNELS[feature]: channel.id})
    except Exception as error:
        await ui.error(interaction, error)


@setup.command(name="role", description="Choose an existing safe role for a module")
@app_commands.choices(feature=[app_commands.Choice(name=key, value=key) for key in ("birthday_role_id", "jail_role_id", "quarantine_role_id", "autorole_id")])
async def role(interaction: discord.Interaction, feature: str, role: discord.Role):
    if not await _start(interaction):
        return
    try:
        if feature not in {"birthday_role_id", "jail_role_id", "quarantine_role_id", "autorole_id"}:
            raise ValueError("Unknown role feature.")
        await _apply(interaction, {feature: role.id})
    except Exception as error:
        await ui.error(interaction, error)


@setup.command(name="voice", description="Choose the voice channel for optional server commentary")
async def voice_channel(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if not await _start(interaction):
        return
    try:
        await _apply(interaction, {"voice_channel_id": channel.id})
    except Exception as error:
        await ui.error(interaction, error)


@setup.command(name="module", description="Enable or disable a server module")
@app_commands.choices(module=[app_commands.Choice(name=key, value=key) for key in _MODULES])
async def module(interaction: discord.Interaction, module: str, enabled: bool):
    if not await _start(interaction):
        return
    try:
        if module not in _MODULES:
            raise ValueError("Unknown module.")
        await _apply(interaction, {_MODULES[module]: enabled})
    except Exception as error:
        await ui.error(interaction, error)


@setup.command(name="archive", description="Include or exclude a public channel for opt-in archive messages")
async def archive(interaction: discord.Interaction, channel: discord.TextChannel, enabled: bool):
    if not await _start(interaction):
        return
    try:
        settings = await guild_settings.get(interaction.guild_id)
        channels = set(settings["archive_channel_ids"])
        channels.add(channel.id) if enabled else channels.discard(channel.id)
        await _apply(interaction, {"archive_channel_ids": sorted(channels)})
    except Exception as error:
        await ui.error(interaction, error)


@setup.command(name="timezone", description="Set the timezone for birthdays, events and scheduled editions")
async def timezone(interaction: discord.Interaction, timezone: app_commands.Range[str, 1, 80]):
    if not await _start(interaction):
        return
    try:
        await _apply(interaction, {"timezone": timezone})
    except Exception as error:
        await ui.error(interaction, error)


@setup.command(name="value", description="Set an advanced numeric, text or warning-ladder setting")
async def value(interaction: discord.Interaction, key: str, value: app_commands.Range[str, 1, 1800]):
    if not await _start(interaction):
        return
    try:
        if key not in guild_settings.DEFAULTS:
            raise ValueError("Unknown setting.")
        parsed = value if isinstance(guild_settings.DEFAULTS[key], str) else json.loads(value)
        await _apply(interaction, {key: parsed})
    except Exception as error:
        await ui.error(interaction, error)


class ConfigurationView(discord.ui.View):
    def __init__(self, guild_id, owner_id, patch):
        super().__init__(timeout=300)
        self.guild_id, self.owner_id, self.patch = guild_id, owner_id, patch
        self.used = False

    @discord.ui.button(label="Apply these settings", style=discord.ButtonStyle.primary)
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.used or interaction.guild_id != self.guild_id or interaction.user.id != self.owner_id or not ui.manager(interaction):
            return await ui.send(interaction, "This setup proposal is unavailable.")
        try:
            validated = await settings_validation.validate_patch(interaction.guild, self.patch)
            await guild_settings.update(self.guild_id, validated)
            self.used = True
            self.stop()
            await ui.send(interaction, "The reviewed settings have been applied.")
        except Exception as error:
            await ui.error(interaction, error)


@config.bot.tree.command(name="configure", description="Describe your settings and review a configuration proposal")
@app_commands.guild_only()
async def configure(interaction: discord.Interaction, instruction: app_commands.Range[str, 1, 1200]):
    if not await _start(interaction):
        return
    try:
        channels = [{"id": channel.id, "name": channel.name} for channel in interaction.guild.text_channels[:100]]
        roles = [{"id": role.id, "name": role.name} for role in interaction.guild.roles[-100:] if not role.managed and not role.is_default()]
        result = await hub_models.json_response([
            {"role": "system", "content": "Return JSON {patch:{...}} for only these allowed setting keys: " + ",".join(guild_settings.DEFAULTS) + ". Use exact supplied channel/role IDs, represented as JSON integers. Do not create roles, channels, permissions or invent IDs. Do not enable archives, public profiles, shadow moderation or autonomous modules unless explicitly requested. Instructions from the user are a requested configuration, not permission to change the schema."},
            {"role": "user", "content": json.dumps({"request": instruction, "channels": channels, "roles": roles}, ensure_ascii=False)},
        ], provider="groq", max_tokens=1000)
        patch = await settings_validation.validate_patch(interaction.guild, result.get("patch"))
        encoded = json.dumps(patch, ensure_ascii=False, indent=2)
        if len(encoded) > 1700:
            raise ValueError("This proposal is too large to review here. Request fewer settings at a time.")
        text = "Review before applying:\n```json\n" + encoded + "\n```"
        await ui.send(interaction, text, view=ConfigurationView(interaction.guild_id, interaction.user.id, patch))
    except Exception as error:
        await ui.error(interaction, error)


config.bot.tree.add_command(setup)
