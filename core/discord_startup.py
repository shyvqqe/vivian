"""Keep the login HTTP session alive while Discord asks us to wait."""
from __future__ import annotations

from asyncio import sleep as _sleep
from datetime import datetime, timedelta, timezone

from core import discord_api

LOGIN_PATHS = frozenset({"/users/@me", "/oauth2/applications/@me"})


def _retry_at(seconds):
    try:
        moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, ValueError):
        return "see-pause-seconds"


async def login_request(request, route, remaining):
    """Retry only a caller-selected login GET, without restarting the client.

    The REST guard records the server's complete delay before this runs.
    Cancellation and every non-rate-limit failure propagate to discord.py.
    """
    waited = False
    while True:
        try:
            result = await request()
        except Exception as error:
            if not discord_api.is_rate_limit_error(error):
                raise
            # A zero remainder can mean that another request owns the probe.
            # Keep that case from spinning; never shorten a server deadline.
            delay = max(1.0, remaining())
            print(
                f"[Discord Startup] Login waiting; pause={delay:.1f}s; "
                f"retry_at_utc={_retry_at(delay)}; route={route.method} {route.path}; "
                "process stays alive; Discord is not connected yet.", flush=True,
            )
            waited = True
            await _sleep(delay)
        else:
            if waited:
                print(
                    f"[Discord Startup] Login request recovered; route={route.method} {route.path}",
                    flush=True,
                )
            return result
