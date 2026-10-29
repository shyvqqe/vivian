from __future__ import annotations

import importlib
import traceback

import discord

from core import config
from core import database
from vivian import ambient, chat_impact, memory_store

APP_MODULES = [
    "errors",
    "members",
    "messages",
    "community",
    "deployment_status",
    "github_updates",
    "room_panels",
    "rooms",
    "server",
]

COMMAND_MODULES = [
    "activity",
    "ai",
    "ai_moderation",
    "antispam",
    "card",
    "changelog",
    "daily",
    "feedback",
    "general",
    "help_panel",
    "moderation",
    "music",
    "music_prefix",
    "project",
    "quests",
    "rank",
    "room",
    "veyr",
    "capsule",
    "crossroads",
    "exchange",
    "friends",
    "poke",
    "confess",
    "crush",
    "wall",
    "status",
    "vibecheck",
    "memories",
    "birthday",
    "story",
]

app = {}
for name in APP_MODULES:
    try:
        app[name] = importlib.import_module(f"app.{name}")
    except ModuleNotFoundError:
        print(f"[App] {name} not found, skipping", flush=True)
    except Exception:
        print(f"[App] {name} failed", flush=True)
        traceback.print_exc()

commands = {}
for name in COMMAND_MODULES:
    try:
        commands[name] = importlib.import_module(f"commands.{name}")
    except ModuleNotFoundError:
        print(f"[Commands] {name} not found, skipping", flush=True)
    except Exception:
        print(f"[Commands] {name} failed", flush=True)
        traceback.print_exc()

errors = app.get("errors")
members = app.get("members")
messages = app.get("messages")
community = app.get("community")
deployment_status = app.get("deployment_status")
github_updates = app.get("github_updates")
room_panels = app.get("room_panels")
rooms = app.get("rooms")
server = app.get("server")

for n, m in commands.items():
    globals()[n] = m

bot = config.bot
_original_setup_hook = bot.setup_hook


def _start_loop(loop):
    try:
        if not loop.is_running():
            loop.start()
    except Exception:
        traceback.print_exc()


async def setup_hook():
    await _original_setup_hook()

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

    starters = []
    for mod, attr in (
        (rooms, "start"),
        (room_panels, "start"),
        (community, "start"),
        (commands.get("friends"), "register_views"),
        (commands.get("poke"), "register_views"),
        (commands.get("confess"), "register_views"),
        (commands.get("wall"), "register_views"),
        (commands.get("status"), "register_views"),
        (commands.get("story"), "register_views"),
        (commands.get("memories"), "start"),
        (commands.get("birthday"), "start"),
    ):
        if mod and hasattr(mod, attr):
            starters.append(getattr(mod, attr))

    for starter in starters:
        try:
            starter()
        except Exception:
            traceback.print_exc()

    if config.GITHUB_UPDATES_ENABLED:
        if github_updates and hasattr(github_updates, "github_updates_loop"):
            _start_loop(github_updates.github_updates_loop)

    if hasattr(ambient, "ambient_chat_loop"):
        _start_loop(ambient.ambient_chat_loop)
    if hasattr(memory_store, "ai_memory_flush_loop"):
        _start_loop(memory_store.ai_memory_flush_loop)
    if hasattr(chat_impact, "retry_chat_impact_writes"):
        _start_loop(chat_impact.retry_chat_impact_writes)

    daily_mod = commands.get("daily")
    if daily_mod and hasattr(daily_mod, "daily_summary_loop"):
        _start_loop(daily_mod.daily_summary_loop)

    print("[Vivian] Ready", flush=True)


if __name__ == "__main__":
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is required.")
    if server and hasattr(server, "keep_alive"):
        server.keep_alive()
    bot.run(config.TOKEN)
