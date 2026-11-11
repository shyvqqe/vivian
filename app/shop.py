from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord.ext import tasks

from core import config, economy_store, guild_settings, shop_store as store


_log = logging.getLogger(__name__)
_GATE = asyncio.Semaphore(3)


class UnsafeReward(ValueError):
    pass


class DeliveryDeferred(RuntimeError):
    pass


def _role_ids(member):
    values = getattr(member, "_roles", None)
    return set(values) if values is not None else {role.id for role in member.roles}


def _authority(member, roles, owner_id):
    selected = [role for role in roles if role.is_default() or role.id in _role_ids(member)]
    value = 0
    for role in selected:
        value |= role.permissions.value
    permissions = discord.Permissions(value)
    if member.id == owner_id or permissions.administrator:
        permissions = discord.Permissions.all()
    return permissions, max((role.position for role in selected), default=0)


async def reward_context(guild, user_id, role_id, *, manager_id=None):
    async with asyncio.timeout(12):
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            raise UnsafeReward("The purchasing member has left this server.") from None
        me = await guild.fetch_member(config.bot.user.id)
        roles = await guild.fetch_roles()
        if member.bot:
            raise UnsafeReward("Bot accounts cannot purchase role rewards.")
        role = next((item for item in roles if item.id == role_id), None)
        if role is None or role.is_default() or role.managed or role.permissions.value != 0:
            raise UnsafeReward("The reward role was deleted or is no longer a permission-free cosmetic role.")
        permissions, top = _authority(me, roles, guild.owner_id)
        if not permissions.manage_roles:
            raise DeliveryDeferred("Vivian currently lacks Manage Roles. Delivery will retry.")
        if role.position >= top:
            raise UnsafeReward("The reward role is no longer below Vivian's highest role.")
        if manager_id is not None:
            manager = member if manager_id == user_id else await guild.fetch_member(manager_id)
            permissions, actor_top = _authority(manager, roles, guild.owner_id)
            if not permissions.manage_guild or not permissions.manage_roles:
                raise ValueError("You need current Manage Server and Manage Roles permissions.")
            if manager.id != guild.owner_id and role.position >= actor_top:
                raise ValueError("Shop reward roles must be below your highest role.")
    return member, role


async def debit_record(guild_id, item):
    transaction = await economy_store.transaction(guild_id, item["reference"])
    if transaction is None:
        return False
    if (transaction.get("user_id") != item["user_id"] or transaction.get("amount") != item["price"]
            or transaction.get("direction") != -1):
        raise DeliveryDeferred("The purchase transaction reference does not match this record.")
    return True


async def refund(guild, item, reason):
    charged = await debit_record(guild.id, item)
    if not charged:
        return await store.finish(guild.id, item, "canceled", outcome=reason, charged=False)
    item = await store.update(guild.id, item, state="refund_pending", charged=True, refund_reason=reason)
    await economy_store.credit(guild.id, item["user_id"], item["price"], f"refund:{item['id']}")
    return await store.finish(guild.id, item, "refunded", outcome=reason, charged=True)


async def _deliver(guild, item):
    if item.get("refund_reason"):
        return await refund(guild, item, item["refund_reason"])
    try:
        member, role = await reward_context(guild, item["user_id"], item["role_id"])
    except UnsafeReward as error:
        return await refund(guild, item, str(error))
    charged = await debit_record(guild.id, item)
    if role.id in _role_ids(member):
        return await store.finish(guild.id, item, "delivered" if charged else "already_owned", charged=charged,
                                  outcome="The member already has this reward role.")
    if not charged:
        item = await store.update(guild.id, item, state="charging", charge_started=True)
        try:
            await economy_store.spend(guild.id, item["user_id"], item["price"], item["reference"])
        except ValueError:
            charged = await debit_record(guild.id, item)
            if not charged:
                return await store.finish(guild.id, item, "canceled", outcome="Insufficient balance; no currency was charged.", charged=False)
            raise
    item = await store.update(guild.id, item, state="delivery_pending", charged=True)
    try:
        member, role = await reward_context(guild, item["user_id"], item["role_id"])
    except UnsafeReward as error:
        return await refund(guild, item, str(error))
    if role.id not in _role_ids(member):
        try:
            await member.add_roles(role, reason=f"Vivian shop purchase {item['id']}", atomic=True)
        except discord.HTTPException:
            current = await guild.fetch_member(item["user_id"])
            if role.id not in _role_ids(current):
                raise DeliveryDeferred("Discord has not confirmed the role grant. The same purchase will retry.") from None
    current = await guild.fetch_member(item["user_id"])
    if role.id not in _role_ids(current):
        raise DeliveryDeferred("Discord has not yet confirmed the reward role.")
    return await store.finish(guild.id, item, "delivered", charged=True, delivered_at=time.time(), outcome="Role delivered.")


async def process_purchase(guild, purchase_id):
    async with _GATE:
        item = await store.claim(guild.id, purchase_id)
        if item is None:
            return await store.purchase(guild.id, purchase_id)
        try:
            async with asyncio.timeout(60):
                return await _deliver(guild, item)
        except Exception as error:
            try:
                return await store.update(guild.id, item, state="retry", lease_until=0,
                    next_at=time.time() + min(3600, 15 * 2 ** min(item.get("attempts", 1), 7)),
                    last_error=str(error)[:200] if isinstance(error, (ValueError, DeliveryDeferred)) else type(error).__name__)
            except Exception as storage_error:
                _log.warning("Shop purchase confirmation deferred: guild=%s purchase=%s error=%s", guild.id, purchase_id, type(storage_error).__name__)
                raise


async def buy(interaction, item_key):
    settings = await guild_settings.get(interaction.guild_id)
    if not settings.get("economy_enabled"):
        raise ValueError("The server economy is disabled.")
    key = store.item_key(item_key)
    items = await store.catalog(interaction.guild_id)
    item = next((item for item in items if item["key"] == key), None)
    if item is None:
        raise ValueError("That item is not available in this server's shop.")
    member, role = await reward_context(interaction.guild, interaction.user.id, item["role_id"])
    if role.id in _role_ids(member):
        raise ValueError("You already own this role. No currency was charged.")
    prepared = await store.prepare(interaction.guild_id, interaction.user.id, key, interaction.id,
                                   expected_role_id=item["role_id"], expected_price=item["price"])
    return await process_purchase(interaction.guild, prepared["id"])


async def process_guild(guild):
    items = await store.pending(guild.id, limit=30)
    if items:
        await asyncio.gather(*(process_purchase(guild, item["id"]) for item in items), return_exceptions=True)


def start():
    if not reconciliation_loop.is_running():
        reconciliation_loop.start()


@tasks.loop(seconds=45)
async def reconciliation_loop():
    for guild in list(config.bot.guilds):
        try:
            await process_guild(guild)
        except Exception as error:
            _log.warning("Shop queue unavailable: guild=%s error=%s", guild.id, type(error).__name__)


@reconciliation_loop.before_loop
async def _before_reconciliation():
    await config.bot.wait_until_ready()
