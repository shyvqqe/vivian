"""Start the optional project features without interrupting Discord commands."""
from __future__ import annotations

import asyncio

import discord
from discord.ext import tasks

from app import deployment_status
from commands import feedback, project
from core import community_store as store, config, discord_api


_ready = False
_lock = None
_status = {"state": "starting"}


def public_status():
    return {**_status, "storage_ready": store.is_ready(),
            "feedback_controls_ready": bool(getattr(config.bot, "community_vote_view_registered", False)),
            "registered_commands": len(config.bot.tree.get_commands()),
            "commands_synced": bool(getattr(config.bot, "commands_synced", False)),
            "deployment": deployment_status.public_status()}


def start():
    feedback.register_views()
    if not community_bootstrap_loop.is_running():
        community_bootstrap_loop.start()


async def _bootstrap():
    global _ready, _lock
    if _ready or discord_api.is_paused() or not config.bot.is_ready():
        return
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _ready:
            return
        if not await store.initialize():
            _status["state"] = "storage_unavailable"
            return
        channel = config.bot.get_channel(config.GITHUB_UPDATES_CHANNEL_ID)
        if channel is None:
            channel = await config.bot.fetch_channel(config.GITHUB_UPDATES_CHANNEL_ID)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            _status["state"] = "invalid_channel"
            return
        channel = await project.project_channel(channel.guild.id)
        if channel is None:
            _status["state"] = "channel_unavailable"
            return
        await project.bootstrap(channel.guild)
        await deployment_status.bootstrap(channel)
        _ready = True
        _status["state"] = "ready"
        print("[Community] Roadmap, feedback and version confirmation ready.", flush=True)


@tasks.loop(seconds=120)
async def community_bootstrap_loop():
    try:
        await _bootstrap()
    except Exception as error:
        _status["state"] = "retrying"
        print(f"[Community] Bootstrap deferred; error={type(error).__name__}", flush=True)


@community_bootstrap_loop.before_loop
async def _before_bootstrap():
    await config.bot.wait_until_ready()
