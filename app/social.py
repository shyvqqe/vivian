from __future__ import annotations

from discord.ext import tasks

from commands import social as commands_social
from commands.social_views import SocialAction
from core import config, hub_store


_registered = False
_guild_cursor = 0


def start():
    global _registered
    if not _registered:
        config.bot.add_dynamic_items(SocialAction)
        _registered = True
    if not notification_retry.is_running():
        notification_retry.start()


@tasks.loop(seconds=60)
async def notification_retry():
    global _guild_cursor
    remaining = 20
    guilds = list(config.bot.guilds)
    if not guilds:
        return
    start, visited = _guild_cursor % len(guilds), 0
    for offset in range(len(guilds)):
        if remaining <= 0:
            break
        guild = guilds[(start + offset) % len(guilds)]
        visited += 1
        try:
            await commands_social.deliver_outbox(guild, limit=min(remaining, 5))
            remaining -= 5
        except hub_store.Unavailable:
            return
        except Exception as error:
            print(f"[Social] Notification retry deferred: {type(error).__name__}", flush=True)
    _guild_cursor = (start + visited) % len(guilds)


@notification_retry.before_loop
async def before_notification_retry():
    await config.bot.wait_until_ready()
