from __future__ import annotations

import importlib
import asyncio
import sys
import traceback

import discord

from core import config
from core import database, hub_store
from app import dashboard, hub, legacy_life, legacy_social, life, modsuite, shop, social

try:
    from vivian import ambient as vivian_ambient
except Exception as e:
    print(f"[Vivian] ambient not loaded: {e}", flush=True)
    traceback.print_exc()
    vivian_ambient = None

try:
    from vivian import chat_impact as vivian_chat_impact
except Exception as e:
    print(f"[Vivian] chat_impact not loaded: {e}", flush=True)
    traceback.print_exc()
    vivian_chat_impact = None

try:
    from vivian import memory_store as vivian_memory_store
except Exception as e:
    print(f"[Vivian] memory_store not loaded: {e}", flush=True)
    traceback.print_exc()
    vivian_memory_store = None

ambient = vivian_ambient
chat_impact = vivian_chat_impact
memory_store = vivian_memory_store

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
    "moderation_prefix",
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
    "community_play",
    "economy",
    "setup",
    "life",
    "modsuite",
    "social",
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
    if n not in {"life", "modsuite", "social"}:
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
        if config.DATABASE_URL:
            raise RuntimeError(
                f"PostgreSQL initialization failed ({type(error).__name__}). "
                "Check DATABASE_URL, network access and database permissions."
            ) from None
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError(
            f"SQLite initialization failed ({type(error).__name__}). "
            "Check VIVIAN_DATA_DIR and write permissions."
        ) from None

    if await hub_store.initialize():
        await legacy_life.migrate()
        await legacy_social.migrate()
    legacy_social.register_views(bot)
    if memory_store is not None:
        await memory_store.initialize_postgres()

    try:
        from core import ai_models, ai_preferences
        await ai_preferences.load_preferences()
        default_model = ai_models.get_default_model()
        if ai_models.has_provider(default_model):
            print(f"[AI] Automatic model configured: {default_model}.", flush=True)
        else:
            print("[AI] No provider configured. Set GROQ_API_KEY, GEMINI_API_KEY or OPENROUTER_API_KEY.", flush=True)
    except Exception as error:
        print(f"[AI] Startup check failed ({type(error).__name__}).", flush=True)

    try:
        synced = await bot.tree.sync()
        bot.commands_synced = True
        print(f"[Commands] Synced {len(synced)} slash commands", flush=True)
    except Exception:
        bot.commands_synced = False
        traceback.print_exc()


bot.setup_hook = setup_hook


def _stored_event(handler):
    async def guarded(payload):
        if database.db_pool is None:
            return
        try:
            await handler(payload)
        except Exception as error:
            print(f"[Archive] Event deferred; event={handler.__name__}; error={type(error).__name__}", flush=True)
    return guarded


bot.add_listener(_stored_event(life.reaction_added), "on_raw_reaction_add")
bot.add_listener(_stored_event(life.reaction_removed), "on_raw_reaction_remove")
bot.add_listener(_stored_event(life.message_deleted), "on_raw_message_delete")
bot.add_listener(_stored_event(life.bulk_deleted), "on_raw_bulk_message_delete")


@bot.event
async def on_ready():
    print(f"[Vivian] Logged in as {bot.user} (id={bot.user.id})", flush=True)

    starters = []
    for mod, attr in (
        (rooms, "start"),
        (room_panels, "start"),
        (community, "start"),
        (social, "start"),
        (modsuite, "start"),
        (life, "start"),
        (shop, "start"),
        (hub, "start"),
        (dashboard, "start"),
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

    if ambient and hasattr(ambient, "ambient_chat_loop"):
        _start_loop(ambient.ambient_chat_loop)
    if memory_store and hasattr(memory_store, "ai_memory_flush_loop"):
        _start_loop(memory_store.ai_memory_flush_loop)
    if chat_impact and hasattr(chat_impact, "retry_chat_impact_writes"):
        _start_loop(chat_impact.retry_chat_impact_writes)

    daily_mod = commands.get("daily")
    if daily_mod and hasattr(daily_mod, "daily_summary_loop"):
        _start_loop(daily_mod.daily_summary_loop)

    from vivian import music_web_bridge
    music_web_bridge.start(bot)

    print("[Vivian] Ready", flush=True)


async def _run_on_windows():
    async with bot:
        await bot.start(config.TOKEN)


if __name__ == "__main__":
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is required.")
    if server and hasattr(server, "keep_alive"):
        server.keep_alive()
    if sys.platform == "win32" and config.DATABASE_URL:
        # Psycopg async connections require a selector loop on Windows.
        discord.utils.setup_logging()
        try:
            asyncio.run(_run_on_windows(), loop_factory=asyncio.SelectorEventLoop)
        except KeyboardInterrupt:
            pass
    else:
        bot.run(config.TOKEN)
