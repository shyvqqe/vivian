"""Short, bounded in-memory cache of public Discord avatars for profile cards."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import time

import discord


_CACHE = OrderedDict()
_CACHE_BYTES = 0
_BYTE_LIMIT = 16 * 1024 * 1024
_ITEM_LIMIT = 96
_AVATAR_LIMIT = 2 * 1024 * 1024
_TTL = 300


def _prune(now):
    global _CACHE_BYTES
    for key, (expires, data) in tuple(_CACHE.items()):
        if expires <= now:
            _CACHE.pop(key)
            _CACHE_BYTES -= len(data)
    while _CACHE and (len(_CACHE) > _ITEM_LIMIT or _CACHE_BYTES > _BYTE_LIMIT):
        _, (_, data) = _CACHE.popitem(last=False)
        _CACHE_BYTES -= len(data)


async def read_avatar(member):
    """A changed avatar URL naturally invalidates the previous cached image."""
    global _CACHE_BYTES
    asset = member.display_avatar.with_size(512).with_static_format("png")
    key = str(asset.url)
    now = time.monotonic()
    _prune(now)
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key][1]
    try:
        async with asyncio.timeout(3):
            data = await asset.read()
    except (discord.DiscordException, TimeoutError, OSError):
        return None
    if not data or len(data) > _AVATAR_LIMIT:
        return None
    # Concurrent requests can finish for the same avatar; replace, don't add
    # its byte count twice. No await occurs while the cache is being changed.
    previous = _CACHE.pop(key, None)
    if previous:
        _CACHE_BYTES -= len(previous[1])
    _CACHE[key] = (time.monotonic() + _TTL, data)
    _CACHE_BYTES += len(data)
    _prune(time.monotonic())
    return data
