"""Bounded V2 announcements with the existing Discord nonce guarantee."""
from __future__ import annotations

import discord

from core import config
from utils import components_v2 as v2


async def send_once(channel, *, embed, nonce):
    prepared = v2.adapt({"embed": embed})
    if prepared.get("files") or prepared.get("file"):
        for file in prepared.get("files", ()):
            file.close()
        raise ValueError("Announcement exceeds the inline card limit")
    return await config.bot.http.request(
        discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id),
        json={"flags": 1 << 15, "components": prepared["view"].to_components(),
              "nonce": str(nonce), "enforce_nonce": True,
              "allowed_mentions": {"parse": [], "replied_user": False}},
    )
