from __future__ import annotations

import traceback

import discord

from core import config

# Importing these modules registers slash commands, events and views.
from app import errors, members, messages  # noqa: F401
from app import community, deployment_status, github_updates, room_panels, rooms, server  # noqa: F401
from commands import (  # noqa: F401
    activity, ai, ai_moderation, antispam, card, changelog, daily, feedback,
    general, help_panel, moderation, music, music_prefix, project, quests, rank,
    room, veyr, capsule, crossroads, exchange,
)
from core import database
from vivian import ambient, chat_impact, memory_store

bot = config.bot
_original_setup_hook = bot.setup_hook


def _start_loop(loop):
    try:
        if not loop.is_running():
            loop.start()
    except Exception:
        traceback.print_exc()


async def setup_hook():
    await _original_setup_hook()  # loads jishaku

    try:
        await database._open_persistent_storage()
    except Exception as error:
        print(f"[Storage] Failed to connect: {type(error).__name__}: {error}", flush=True)

    try:
        synced = await bot.tree.sync()
        bot.commands_synced = True
        print(f"[Commands] Synced {len(synced)} slash commands", flush=True)
    except Exception:
        bot.commands_synced = False
        traceback.print_exc()


bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    print(f"[Vivian] Logged in as {bot.user} (id={bot.user.id})", flush=True)
    for starter in (rooms.start, room_panels.start, community.start):
        try:
            starter()
        except Exception:
            traceback.print_exc()
    if config.GITHUB_UPDATES_ENABLED:
        _start_loop(github_updates.github_updates_loop)
    _start_loop(ambient.ambient_chat_loop)
    _start_loop(memory_store.ai_memory_flush_loop)
    _start_loop(chat_impact.retry_chat_impact_writes)
    _start_loop(daily.daily_summary_loop)
    print("[Vivian] Ready", flush=True)


if __name__ == "__main__":
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is required.")
    server.keep_alive()
    bot.run(config.TOKEN)
