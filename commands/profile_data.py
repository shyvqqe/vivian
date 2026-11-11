"""Public profile data from the existing counters; no new rewards or private memory."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import discord

from core import config, database, state
from commands.profile_avatars import read_avatar
from visuals.member_card import render_member_card
from visuals.profile_palette import extract_profile_theme


_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vivian-profile")
_GATE = asyncio.Semaphore(2)
_RENDER_TIMEOUT = 8
_SUMMARY_TIMEOUT = 3


def achievements(member, stats):
    joined_days = max(0, (datetime.now(timezone.utc) - member.joined_at).days) if member.joined_at else None
    definitions = (
        ("Первое слово", "Первое текстовое сообщение", stats.get("word_messages", 0), 1),
        ("Разговорчивый", "100 сообщений на сервере", stats.get("messages", 0), 100),
        ("Свой голос", "300 сообщений на сервере", stats.get("messages", 0), 300),
        ("Голос сообщества", "1 000 сообщений на сервере", stats.get("messages", 0), 1000),
        ("Большая история", "10 000 сообщений на сервере", stats.get("messages", 0), 10000),
        ("Есть что сказать", "10 текстов длиннее 100 символов", stats.get("long_messages", 0), 10),
        ("Первая неделя", "7 дней с момента вступления", joined_days, 7),
        ("Свой человек", "30 дней с момента вступления", joined_days, 30),
    )
    return [{"title": title, "description": description, "value": value, "target": target,
             "done": value is not None and value >= target}
            for title, description, value, target in definitions]


def snapshot(member):
    guild = member.guild
    stats = dict(state.RANK_ACTIVITY.get((guild.id, member.id), {}))
    total_xp = max(0, int(state.xp_data.get(member.id, {}).get("xp", 0)))
    messages = max(0, int(stats.get("messages", 0)))
    # The existing XP system is global, while message ranking is guild-scoped.
    known = [(uid, record) for (gid, uid), record in state.RANK_ACTIVITY.items()
             if gid == guild.id and guild.get_member(uid) is not None
             and not guild.get_member(uid).bot]
    rank = 1 + sum(record.get("messages", 0) > messages for _, record in known) if messages else None
    today = datetime.now(config.MOSCOW_TIMEZONE).date()
    today_stats = dict(state.DAILY_ACTIVITY.get((today, guild.id, member.id), {}))
    unlocked = achievements(member, stats)
    badges = [item["title"] for item in reversed(unlocked) if item["done"]][:3]
    role = member.top_role
    return {
        "display_name": member.display_name, "username": member.name, "guild_name": guild.name,
        "user_id": member.id, "role_name": role.name if not role.is_default() else "",
        "level": total_xp // 500 + 1, "xp_into": total_xp % 500, "xp_required": 500,
        "total_xp": total_xp, "messages": messages, "rank": rank,
        "joined_days": max(0, (datetime.now(timezone.utc) - member.joined_at).days) if member.joined_at else None,
        "badges": badges, "achievements": unlocked, "stats": stats,
        "today_messages": max(0, int(today_stats.get("messages", 0))),
        "observed_members": len(known), "updated_at": int(discord.utils.utcnow().timestamp()),
        "storage_ready": database.is_ready(),
    }


def _prepare(data, avatar, with_image):
    data = dict(data, theme=extract_profile_theme(avatar, fallback_key=str(data["user_id"])))
    image = None
    if with_image:
        try:
            image = render_member_card(data, avatar)
        except Exception as error:
            print(f"[Profile] Image unavailable; error={type(error).__name__}", flush=True)
    return data, image


def _count(value):
    try:
        return max(0, min(10**18, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _consume_summary(task):
    if not task.cancelled():
        task.exception()


async def _summaries(member, data):
    from core import economy_store, social_store
    tasks = [asyncio.create_task(social_store.profile_summary(member.guild.id, member.id)),
             asyncio.create_task(economy_store.profile_summary(member.guild.id, member.id))]
    try:
        done, pending = await asyncio.wait(tasks, timeout=_SUMMARY_TIMEOUT)
    except BaseException:
        for task in tasks:
            task.cancel()
            task.add_done_callback(_consume_summary)
        raise
    for task in pending:
        task.cancel()
        task.add_done_callback(_consume_summary)
    results = []
    for task in tasks:
        if task in done and not task.cancelled() and task.exception() is None:
            results.append(task.result())
        else:
            results.append(None)
    result = dict(data)
    social, economy = results
    if isinstance(social, dict):
        partner_id = social.get("relationship_user_id")
        partner = member.guild.get_member(partner_id) if type(partner_id) is int and partner_id > 0 and partner_id != member.id else None
        if partner is not None and (getattr(partner, "bot", False)
                or getattr(partner, "id", None) != partner_id
                or getattr(getattr(partner, "guild", None), "id", member.guild.id) != member.guild.id):
            partner = None
        result["social"] = {
            "friend_count": min(500, _count(social.get("friend_count"))),
            "status_text": str(social.get("status_text") or "")[:120],
            "mood": str(social.get("mood") or "")[:32],
            "relationship_name": str(partner.display_name)[:80] if partner is not None else "",
            "relationship_user_id": partner.id if partner is not None else None,
            "relationship_since": _count(social.get("relationship_since")) if partner is not None else None,
        }
    if isinstance(economy, dict):
        badges = economy.get("badges")
        result["economy"] = {
            "balance": _count(economy.get("balance")), "currency": str(economy.get("currency") or "sparks")[:40],
            "badges": [str(badge)[:80] for badge in badges[-3:]] if isinstance(badges, list) else [],
            "voice_seconds": _count(economy.get("voice_seconds")),
        }
        season = economy.get("season")
        if isinstance(season, dict):
            result["season"] = {key: str(season.get(key, ""))[:40] for key in ("id", "label", "month")}
    return result


async def build(member, *, with_image=True):
    data = snapshot(member)
    data = await _summaries(member, data)
    gate = _GATE
    pending = None
    try:
        await asyncio.wait_for(gate.acquire(), timeout=_RENDER_TIMEOUT)
    except TimeoutError:
        return data, None
    try:
        avatar = await read_avatar(member)
        try:
            pending = asyncio.get_running_loop().run_in_executor(_WORKERS, _prepare, data, avatar, with_image)
            pending.add_done_callback(lambda completed: _release_render_slot(completed, gate))
            return await asyncio.wait_for(asyncio.shield(pending), timeout=_RENDER_TIMEOUT)
        except Exception as error:
            print(f"[Profile] Image unavailable; error={type(error).__name__}", flush=True)
            return data, None
    finally:
        if pending is None:
            gate.release()


def _release_render_slot(pending, gate):
    try:
        pending.exception()
    except asyncio.CancelledError:
        pass
    finally:
        gate.release()
