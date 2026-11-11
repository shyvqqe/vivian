from __future__ import annotations

from copy import deepcopy
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from core.guild_settings import DEFAULTS


_LIMITS = {
    "birthday_hour": (0, 23), "weekly_hour": (0, 23), "weekly_day": (0, 6),
    "monthly_day": (1, 28), "monthly_hour": (0, 23),
    "moderate_min_account_age_hours": (0, 8760), "moderate_raid_join_count": (3, 100),
    "moderate_raid_window_seconds": (5, 300), "jail_bail": (0, 1000000),
    "daily_currency_cap": (1, 1000), "daily_reward": (1, 1000),
}


def _public_channel(guild, channel):
    return isinstance(channel, discord.TextChannel) and channel.guild.id == guild.id


async def validate_patch(guild, patch):
    if not isinstance(patch, dict) or not 1 <= len(patch) <= 30:
        raise ValueError("Choose between 1 and 30 settings.")
    result = {}
    for key, value in patch.items():
        if key not in DEFAULTS:
            raise ValueError(f"Unknown setting: {str(key)[:80]}")
        if type(DEFAULTS[key]) is bool:
            if type(value) is not bool:
                raise ValueError(f"{key} must be true or false.")
        elif key.endswith("_channel_id"):
            if value is not None:
                if type(value) is not int:
                    raise ValueError("A channel identifier is required.")
                channel = guild.get_channel(value)
                if key == "voice_channel_id":
                    if not isinstance(channel, discord.VoiceChannel):
                        raise ValueError("Choose a voice channel on this server.")
                elif not _public_channel(guild, channel):
                    raise ValueError("Choose a text channel on this server.")
                if guild.me is None or not channel.permissions_for(guild.me).view_channel:
                    raise ValueError("Vivian cannot view this channel.")
                if key in {"anonymous_log_channel_id", "appeals_channel_id", "modlog_channel_id"}:
                    if channel.permissions_for(guild.default_role).view_channel:
                        raise ValueError("Moderator logs must use a channel hidden from @everyone.")
                    for target, overwrite in getattr(channel, "overwrites", {}).items():
                        if overwrite.view_channel is not True or getattr(target, "id", None) == guild.me.id:
                            continue
                        tags = getattr(target, "tags", None)
                        if tags and getattr(tags, "bot_id", None) == guild.me.id:
                            continue
                        allowed = getattr(target, "guild_permissions", None) or getattr(target, "permissions", None)
                        if not allowed or not (allowed.administrator or allowed.manage_guild or allowed.moderate_members):
                            raise ValueError("Moderator logs cannot grant access to non-moderator roles or members.")
        elif key.endswith("_role_id") or key == "autorole_id":
            if value is not None:
                role = guild.get_role(value) if type(value) is int else None
                if role is None or role.is_default() or role.managed or guild.me is None or role >= guild.me.top_role:
                    raise ValueError("Choose a role below Vivian's highest role.")
                permissions = role.permissions
                if any(getattr(permissions, name, False) for name in (
                    "administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members", "kick_members",
                    "moderate_members", "manage_webhooks", "manage_messages",
                )):
                    raise ValueError("This feature cannot grant a moderation role.")
        elif key == "archive_channel_ids":
            if not isinstance(value, list) or len(value) > 20 or any(type(item) is not int for item in value):
                raise ValueError("Choose up to 20 archive channels.")
            value = list(dict.fromkeys(value))
            for channel_id in value:
                channel = guild.get_channel(channel_id)
                if not _public_channel(guild, channel) or not channel.permissions_for(guild.default_role).view_channel:
                    raise ValueError("Archive channels must be public text channels on this server.")
                permissions = channel.permissions_for(guild.me) if guild.me else None
                if not permissions or not permissions.view_channel or not permissions.read_message_history:
                    raise ValueError("Vivian needs access to archive channel history.")
        elif key == "timezone":
            try:
                if not isinstance(value, str) or len(value) > 80:
                    raise ValueError
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                raise ValueError("Use an IANA timezone such as Europe/Riga.") from None
        elif key in _LIMITS:
            low, high = _LIMITS[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}.")
        elif key == "moderate_warning_ladder":
            if not isinstance(value, list) or not 1 <= len(value) <= 10:
                raise ValueError("Set 1 to 10 warning steps.")
            seen = set()
            for step in value:
                if not isinstance(step, dict) or set(step) != {"count", "action", "seconds"}:
                    raise ValueError("Each step needs count, action and seconds.")
                if type(step["count"]) is not int or not 1 <= step["count"] <= 100 or step["count"] in seen:
                    raise ValueError("Warning counts must be unique integers from 1 to 100.")
                seen.add(step["count"])
                if step["action"] not in {"timeout", "ban"} or type(step["seconds"]) is not int:
                    raise ValueError("Choose timeout or ban for each step.")
                if not 0 <= step["seconds"] <= 31536000 or (step["action"] == "timeout" and not step["seconds"]):
                    raise ValueError("Invalid warning duration.")
            value = sorted(value, key=lambda step: step["count"])
        elif isinstance(DEFAULTS[key], str):
            limit = 1000 if key == "server_style" else 32
            if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
                raise ValueError(f"{key} must contain at most {limit} characters.")
        else:
            raise ValueError(f"This setting is not editable: {key}")
        result[key] = deepcopy(value)
    return result
