"""Public GitHub changes sent to one configured Discord updates channel."""
from __future__ import annotations

import hashlib
import re
import time

import discord
from discord.ext import tasks

from app import github_update_card
from app import deployment_status, release_journal
from app.github_update_client import GitHubUnavailable, GitHubUpdatesClient
from core import community_store as store, config, discord_api
from utils import components_v2


_status = {"state": "starting", "last_commit": None}
_client = None
_cursor = None
_history_loaded = False
_retry_at = 0.0
_journal_bootstrapped = False
_commit_link = re.compile(
    rf"https://github\.com/{re.escape(config.GITHUB_UPDATES_REPOSITORY)}/commit/([0-9a-f]{{40}})",
    re.IGNORECASE,
)


def public_status():
    if not config.GITHUB_UPDATES_ENABLED:
        return {"state": "disabled", "last_commit": None}
    return dict(_status)


def _message_strings(message):
    yield message.content or ""
    for embed in message.embeds:
        yield embed.description or ""
        yield embed.url or ""
    stack = [component.to_dict() for component in getattr(message, "components", ())]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            stack.extend(reversed(list(value.values())))
        elif isinstance(value, list):
            stack.extend(reversed(value))


async def _last_announced(channel):
    # The channel is the durable cursor: redeploying Render cannot erase it.
    async for message in channel.history(limit=100):
        if message.author.id != config.bot.user.id:
            continue
        strings = list(_message_strings(message))
        if ("## Vivian // GitHub" not in strings
                and not any(embed.title == "Vivian // GitHub" for embed in message.embeds)):
            continue
        for text in strings:
            match = _commit_link.search(text)
            if match:
                return match.group(1).lower()
    return None


async def _updates_channel():
    channel = config.bot.get_channel(config.GITHUB_UPDATES_CHANNEL_ID)
    if channel is None:
        channel = await config.bot.fetch_channel(config.GITHUB_UPDATES_CHANNEL_ID)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        _status["state"] = "invalid_channel"
        return None
    me = channel.guild.me
    if me is None:
        return None
    rights = channel.permissions_for(me)
    can_send = rights.send_messages_in_threads if isinstance(channel, discord.Thread) else rights.send_messages
    if not (rights.view_channel and rights.read_message_history and can_send):
        _status["state"] = "missing_permissions"
        print("[GitHub updates] Need View Channel, Read Message History and Send Messages in updates channel.", flush=True)
        return None
    return channel


async def _publish(channel, update):
    head, comparison = update["head"], update.get("comparison")
    embed = github_update_card.build_update(head, comparison)
    controls = github_update_card.build_view(head, comparison)
    layout = components_v2.adapt({"embed": embed, "view": controls})["view"]
    identity = f"{config.GITHUB_UPDATES_REPOSITORY}:{channel.id}:{head['sha']}"
    nonce = "gh-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:21]
    # A single create request with Discord's idempotency guard also covers
    # overlapping Render instances and uncertain delivery during a reconnect.
    sent = await config.bot.http.request(
        discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id),
        json={
            "flags": 1 << 15,
            "components": layout.to_components(),
            "nonce": nonce,
            "enforce_nonce": True,
            "allowed_mentions": {"parse": [], "replied_user": False},
        },
    )
    print(f"[GitHub updates] Published; commit={head['sha'][:12]}; message_id={sent['id']}", flush=True)


async def _poll_once():
    global _client, _cursor, _history_loaded, _journal_bootstrapped
    if discord_api.is_paused():
        _status["state"] = "discord_paused"
        return
    channel = await _updates_channel()
    if channel is None:
        return
    if not _history_loaded:
        _cursor = await _last_announced(channel)
        _status["last_commit"] = _cursor
        _history_loaded = True
    if _client is None:
        _client = GitHubUpdatesClient(config.GITHUB_UPDATES_REPOSITORY, config.GITHUB_UPDATES_BRANCH)
    # Fetch the current head once on boot even when its source card already
    # exists: persistence and deployment confirmation must survive restarts.
    update = await _client.poll(_cursor)
    if update is None and not _journal_bootstrapped:
        update = await _client.poll(None)
    if update is None:
        _status["state"] = "watching"
        return
    _journal_bootstrapped = False
    if await store.initialize():
        try:
            await release_journal.record(channel.guild.id, update)
            await deployment_status.observe_commit(channel, update["head"])
            _journal_bootstrapped = True
        except Exception as error:
            print(f"[GitHub updates] Project records deferred; error={type(error).__name__}", flush=True)
    # Re-read before publishing: another instance or a previous uncertain POST
    # may already have announced this revision. Never repeat old history on boot.
    latest = await _last_announced(channel)
    head = update["head"]["sha"].lower()
    if latest == head:
        _cursor = head
    elif latest is not None and latest != _cursor:
        _cursor = latest
        _status.update(state="watching", last_commit=_cursor)
        return
    else:
        await _publish(channel, update)
        _cursor = head
    _status.update(state="watching", last_commit=_cursor)


@tasks.loop(seconds=config.GITHUB_UPDATES_INTERVAL)
async def github_updates_loop():
    global _retry_at
    if time.monotonic() < _retry_at:
        return
    try:
        await _poll_once()
    except GitHubUnavailable as error:
        _status["state"] = "github_waiting"
        _retry_at = time.monotonic() + error.retry_after
    except (discord.Forbidden, discord.NotFound):
        _status["state"] = "channel_unavailable"
        _retry_at = time.monotonic() + 600
        print("[GitHub updates] Updates channel is unavailable or permissions are missing.", flush=True)
    except Exception as error:
        _status["state"] = "discord_paused" if discord_api.is_rate_limit_error(error) else "retrying"
        _retry_at = time.monotonic() + 120
        print(f"[GitHub updates] Deferred; error={type(error).__name__}", flush=True)


@github_updates_loop.before_loop
async def _before_updates():
    await config.bot.wait_until_ready()
    print(f"[GitHub updates] Watching {config.GITHUB_UPDATES_REPOSITORY}; channel={config.GITHUB_UPDATES_CHANNEL_ID}", flush=True)


@github_updates_loop.after_loop
async def _after_updates():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
