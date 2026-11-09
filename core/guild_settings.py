from __future__ import annotations

from copy import deepcopy

from core import hub_store


DEFAULTS = {
    "social_enabled": True, "ambient_enabled": True,
    "confessions_enabled": False, "vibecheck_enabled": False,
    "memories_enabled": False, "birthdays_enabled": False, "stories_enabled": False,
    "timeline_enabled": False, "ask_enabled": False, "journal_enabled": False,
    "economy_enabled": False, "events_enabled": False, "voice_stats_enabled": False,
    "server_style_enabled": False, "highlights_enabled": False,
    "games_enabled": True, "voice_tts_enabled": False,
    "seasons_enabled": False, "welcome_enabled": True, "autorole_enabled": True,
    "ai_moderation_enabled": True,
    "public_profiles_enabled": False, "weekly_enabled": False,
    "social_channel_id": None, "confessions_channel_id": None, "conversation_channel_id": None,
    "welcome_channel_id": None, "autorole_id": None,
    "anonymous_log_channel_id": None, "memories_channel_id": None,
    "birthday_channel_id": None, "birthday_role_id": None,
    "stories_channel_id": None, "journal_channel_id": None,
    "modlog_channel_id": None, "appeals_channel_id": None,
    "member_event_channel_id": None,
    "jail_role_id": None, "jail_channel_id": None,
    "quarantine_role_id": None, "quarantine_channel_id": None,
    "events_channel_id": None, "highlights_channel_id": None,
    "voice_channel_id": None, "voice_commentary_enabled": False,
    "archive_channel_ids": [], "timezone": "Europe/Riga",
    "birthday_hour": 9, "weekly_day": 6, "weekly_hour": 18,
    "monthly_day": 1, "monthly_hour": 18,
    "moderate_warning_ladder": [
        {"count": 3, "action": "timeout", "seconds": 3600},
        {"count": 5, "action": "timeout", "seconds": 86400},
        {"count": 7, "action": "ban", "seconds": 0},
    ],
    "moderate_quarantine_enabled": False, "moderate_min_account_age_hours": 24,
    "moderate_require_avatar": False, "moderate_raid_auto": False,
    "moderate_raid_join_count": 6, "moderate_raid_window_seconds": 15,
    "moderate_raid_link_filter": True, "moderate_link_filter": False,
    "jail_bail": 0, "currency_name": "sparks", "daily_currency_cap": 100,
    "daily_reward": 25, "season_theme": "auto", "server_style": "",
}


async def get(guild_id):
    saved = await hub_store.get(guild_id, "settings", "guild", {})
    return {**deepcopy(DEFAULTS), **saved}


async def update(guild_id, patch):
    if not isinstance(patch, dict) or any(not isinstance(key, str) or len(key) > 100 for key in patch):
        raise ValueError("Invalid settings update.")

    def operation(data):
        data.update(deepcopy(patch))
        return {**deepcopy(DEFAULTS), **deepcopy(data)}

    return await hub_store.mutate(guild_id, "settings", "guild", operation)
