"""One status card, confirmed by the running bot revision, without Render secrets."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import re
import time

import discord
from discord.ext import tasks

from core import community_store as store, config, discord_api
from utils import components_v2 as v2


_TITLE = "Vivian // Версия онлайн"
_MARKER = "VIVIAN // DEPLOYMENT"
_locks = {}
_channel = None
_published = {}
_checked_at = {}
_status = {"state": "starting", "target": None}


def public_status():
    return dict(_status)


def _matches(sha):
    revision = config.DEPLOY_REVISION
    return bool(revision and sha and (sha.startswith(revision) or revision.startswith(sha)))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _evaluate(target):
    target = dict(target)
    if _matches(target.get("sha")) and config.bot.is_ready():
        target.update(state="live", confirmed_revision=config.DEPLOY_REVISION)
        target.setdefault("confirmed_at", _now())
    elif target.get("state") != "live":
        elapsed = max(0, time.time() - float(target.get("seen_at", time.time())))
        target["state"] = "unconfirmed" if elapsed >= 900 else "pending"
    return target


def build_card(target):
    sha = str(target.get("sha") or "")
    state = target.get("state", "unconfirmed")
    if state == "live":
        heading = "✓ Версия запущена"
        text = "Процесс Vivian подключён к Discord и подтвердил эту версию кода."
    elif state == "pending":
        heading = "◌ Ожидаем запуска версии"
        text = "Изменения уже в GitHub. Запуск этой версии бот пока не подтвердил."
    else:
        heading = "◌ Запуск пока не подтверждён"
        text = "Подтверждения новой версии ещё нет. Причину нужно смотреть в Render."
    lines = [f"**{heading}**", text]
    if re.fullmatch(r"[0-9a-f]{7,40}", sha):
        lines += ["", f"[Версия {sha[:7]}](https://github.com/{config.GITHUB_UPDATES_REPOSITORY}/commit/{sha})"]
    confirmed = target.get("confirmed_revision")
    if confirmed and confirmed != sha:
        lines.append(f"Последняя подтверждённая: `{confirmed[:7]}`")
    if target.get("confirmed_at") and state == "live":
        try:
            at = int(datetime.fromisoformat(target["confirmed_at"]).timestamp())
            lines.append(f"Подтверждено: <t:{at}:f>")
        except (TypeError, ValueError):
            pass
    lines += ["", "Стадии сборки и причины ошибок Render подключим отдельно."]
    embed = discord.Embed(title=_TITLE, description="\n".join(lines), color=config.VIVIAN_ACCENT)
    embed.set_footer(text=_MARKER)
    return embed


def _is_panel(message):
    from app.github_updates import _message_strings
    if config.bot.user is None or message.author.id != config.bot.user.id:
        return False
    strings = list(_message_strings(message))
    return (any(_MARKER in text for text in strings)
            and (f"## {_TITLE}" in strings or any(embed.title == _TITLE for embed in message.embeds)))


async def _find_message(channel, setting):
    if setting and setting.get("channel_id") == channel.id and setting.get("message_id"):
        try:
            message = await channel.fetch_message(setting["message_id"])
            if _is_panel(message):
                return message
        except discord.NotFound:
            pass
    async for message in channel.history(limit=100):
        if _is_panel(message):
            return message
    return None


async def _publish(channel, target):
    embed = build_card(target)
    signature = hashlib.sha256(str(embed.to_dict()).encode()).hexdigest()
    if (_published.get(channel.guild.id) == signature
            and time.monotonic() - _checked_at.get(channel.guild.id, 0) < 600):
        return
    setting = await store.get_setting(channel.guild.id, "deployment_panel")
    message = await _find_message(channel, setting)
    if message is None:
        prepared = v2.adapt({"embed": embed})
        generation = (setting or {}).get("message_id", "initial")
        identity = f"deployment:{channel.guild.id}:{channel.id}:{generation}"
        sent = await config.bot.http.request(
            discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id),
            json={"flags": 1 << 15, "components": prepared["view"].to_components(),
                  "nonce": hashlib.sha256(identity.encode()).hexdigest()[:24],
                  "enforce_nonce": True, "allowed_mentions": {"parse": []}},
        )
        message_id = int(sent["id"])
    else:
        message_id = message.id
        if not setting or setting.get("signature") != signature:
            await v2.edit_message(message, embed=embed, attachments=[])
    await store.set_setting(channel.guild.id, "deployment_panel",
                            {"channel_id": channel.id, "message_id": message_id, "signature": signature})
    _published[channel.guild.id] = signature
    _checked_at[channel.guild.id] = time.monotonic()


async def _save(channel, before, target):
    if target != before:
        await store.set_setting(channel.guild.id, "deployment_target", target)
    _status.update(state=target.get("state", "unconfirmed"), target=target.get("sha"))
    if target.get("item_id"):
        item = await store.get_item(channel.guild.id, target["item_id"])
        if item and item["status"] != target["state"]:
            await store.set_status(channel.guild.id, item["id"], target["state"], actor_id=config.bot.user.id)
    await _publish(channel, target)


async def observe_commit(channel, head):
    """GitHub tells us the desired version; only a matching running revision confirms it."""
    global _channel
    sha = str(head.get("sha") or "").lower()
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        return
    _channel = channel
    async with _locks.setdefault(channel.guild.id, asyncio.Lock()):
        before = await store.get_setting(channel.guild.id, "deployment_target")
        target = before
        if not target or target.get("sha") != sha:
            record = await store.create_item(
                channel.guild.id, "deploy", config.bot.user.id, f"Версия {sha[:7]}",
                seed_key=f"deploy:{sha}", details={"sha": sha},
            )
            target = {"sha": sha, "state": "pending", "seen_at": time.time(), "item_id": record["id"]}
            if before and before.get("confirmed_revision"):
                target["confirmed_revision"] = before["confirmed_revision"]
        await _save(channel, before, _evaluate(target))
    if not deployment_status_loop.is_running():
        deployment_status_loop.start()


async def bootstrap(channel):
    global _channel
    _channel = channel
    async with _locks.setdefault(channel.guild.id, asyncio.Lock()):
        before = await store.get_setting(channel.guild.id, "deployment_target")
        if before:
            await _save(channel, before, _evaluate(before))
        else:
            _status["state"] = "waiting_for_github"
    if not deployment_status_loop.is_running():
        deployment_status_loop.start()


async def current_target(guild_id):
    return await store.get_setting(guild_id, "deployment_target")


@tasks.loop(seconds=60)
async def deployment_status_loop():
    if _channel is None or discord_api.is_paused() or not config.bot.is_ready():
        return
    try:
        async with _locks.setdefault(_channel.guild.id, asyncio.Lock()):
            before = await store.get_setting(_channel.guild.id, "deployment_target")
            if before:
                await _save(_channel, before, _evaluate(before))
    except Exception as error:
        print(f"[Deployment] Confirmation deferred; error={type(error).__name__}", flush=True)


@deployment_status_loop.before_loop
async def _before_loop():
    await config.bot.wait_until_ready()
