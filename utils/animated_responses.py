"""Attach a cached GIF once; Discord plays its frames without REST updates."""
from __future__ import annotations

from visuals import panels

MUSIC_COMMANDS = {"play", "queue", "skip", "pause", "resume", "stop", "loop", "volume"}


async def decorate(interaction, payload):
    embed = payload.get("embed")
    if (embed is None or embed.image.url or any(
            key in payload for key in ("attachments", "files", "file"))):
        return payload
    command = getattr(getattr(interaction, "command", None), "name", "")
    style = ("music" if command in MUSIC_COMMANDS else
             {"rank": "profile", "activity": "activity"}.get(command, "system"))
    try:
        if not panels._can_upload_panel_visual(getattr(interaction, "guild", None),
                                               getattr(interaction, "channel", None)):
            return payload
        visual = await panels._panel_visual_file(style)
        if visual is not None:
            # Do not mutate a view's shared embed while decorating a private reply.
            embed = embed.copy()
            panels._apply_panel_visual(embed, style)
            payload = {**payload, "embed": embed, "attachments": [visual]}
    except Exception as error:
        print(f"[Command UI] Animation unavailable; error={type(error).__name__}", flush=True)
    return payload
