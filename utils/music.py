from __future__ import annotations
import time
from core import music_state as core_music_state


def _track_duration_label(duration) -> str:
    if not isinstance(duration, (int, float)) or duration < 0:
        return "длительность неизвестна"
    seconds = int(duration)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def _music_position_seconds(guild_id: int) -> float:
    position = core_music_state.MUSIC_TRACK_ELAPSED.get(guild_id, 0.0)
    started_at = core_music_state.MUSIC_TRACK_STARTED_AT.get(guild_id)
    if started_at is not None:
        position += max(0.0, time.monotonic() - started_at)
    track = core_music_state.MUSIC_NOW_PLAYING.get(guild_id) or {}
    duration = track.get("duration")
    if isinstance(duration, (int, float)) and duration > 0:
        position = min(position, float(duration))
    return position
