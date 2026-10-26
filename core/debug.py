"""Load the standard, owner-only Jishaku extension during Bot.setup_hook."""
from __future__ import annotations

import asyncio
from discord.ext import commands


async def load_debug_extension(bot: commands.Bot) -> bool:
    """Loading is local; Jishaku retains its own bot.is_owner permission check."""
    lock = getattr(bot, "_vivian_debug_setup_lock", None)
    if lock is None:
        lock = bot._vivian_debug_setup_lock = asyncio.Lock()
    async with lock:
        if "jishaku" in bot.extensions:
            return True
        try:
            await bot.load_extension("jishaku")
        except commands.ExtensionAlreadyLoaded:
            return True
        except Exception as error:
            # Exception text may contain developer input; never print it here.
            print(f"[Debug] Jishaku unavailable; error={type(error).__name__}", flush=True)
            return False
        print("[Debug] Jishaku loaded; access=bot-owner", flush=True)
        return True
