"""Fast prefix moderation aliases backed by Vivian's durable slash moderation engine.

The actions never bypass the fresh role/permission checks of modsuite_actions.
"""
from __future__ import annotations

from types import SimpleNamespace

import discord
from discord.ext import commands

from app import modsuite_actions as actions
from core import config, modsuite_store as store
from utils import mod_logs


def _request(ctx):
    # modsuite_actions accepts an interaction-like object with user/guild/id.
    # Message ID provides a stable idempotency key for retries of one command.
    return SimpleNamespace(guild=ctx.guild, user=ctx.author, id=ctx.message.id)


async def _reply(ctx, text: str) -> None:
    await ctx.reply(text[:1850], mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none())


async def _failure(ctx, error: Exception) -> None:
    if isinstance(error, (ValueError, commands.CommandError)):
        return await _reply(ctx, f"Не удалось выполнить команду: {error}")
    print(f"[Moderation prefix] Error: {type(error).__name__}", flush=True)
    return await _reply(ctx, "Действие не выполнено. Проверь права и журнал модерации Vivian.")


def _status(case: dict) -> str:
    state = str(case.get("state") or "unknown")
    if state == "active":
        detail = "Наказание применено."
    elif state == "released":
        detail = "Наказание снято."
    elif state in {"applying", "retry", "releasing"}:
        detail = "Заявка сохранена; система модерации выполнит повторную попытку."
    else:
        detail = "Проверь журнал модерации для подробностей."
    return f"Статус: `{state}`. {detail}"


@config.bot.command(name="mute")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, duration: str = "10m", *, reason: str = "Не указана"):
    """!mute @member 10m [reason]"""
    try:
        seconds = store.duration(duration)
        case = await actions.start_punishment(_request(ctx), member, "timeout", seconds, reason)
        await _reply(ctx, f"Мут для {member.display_name} на `{duration}`. {_status(case)}")
    except Exception as exc:
        await _failure(ctx, exc)


@config.bot.command(name="unmute")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    """!unmute @member (only Vivian-owned timeouts)."""
    try:
        await actions.guard(_request(ctx), member, "moderate_members")
        case = await actions.request_release(ctx.guild, "timeout", member.id)
        await _reply(ctx, f"Снятие мута для {member.display_name}. {_status(case)}")
    except Exception as exc:
        await _failure(ctx, exc)


@config.bot.command(name="ban")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason: str = "Не указана"):
    """!ban @member [reason]"""
    try:
        case = await actions.start_punishment(_request(ctx), member, "ban", None, reason)
        await _reply(ctx, f"Блокировка {member.display_name}. {_status(case)}")
    except Exception as exc:
        await _failure(ctx, exc)


@config.bot.command(name="kick")
@commands.guild_only()
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason: str = "Не указана"):
    """!kick @member [reason]"""
    try:
        await actions.guard(_request(ctx), member, "kick_members")
        await member.kick(reason=store.text(reason))
        await _reply(ctx, f"{member.display_name} удалён с сервера.")
        try:
            await mod_logs.send_mod_log(ctx.guild, f"**Кик** `{member.id}` от `{ctx.author.id}`. {reason[:400]}")
        except discord.DiscordException:
            pass  # Logging failure cannot turn a successful kick into an error.
    except Exception as exc:
        await _failure(ctx, exc)


@config.bot.command(name="warn")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def warn(ctx, member: discord.Member, *, reason: str):
    """!warn @member reason; uses the existing warning ladder."""
    try:
        result = await actions.add_warning(_request(ctx), member, reason)
        escalation = result.get("escalation")
        message = f"Предупреждение {member.display_name}: всего **{result['count']}**."
        if escalation:
            message += f" Автоматическое наказание: `{escalation.get('state', 'unknown')}`."
        await _reply(ctx, message)
    except Exception as exc:
        await _failure(ctx, exc)


for _command in (mute, unmute, ban, kick, warn):
    @_command.error
    async def _on_prefix_moderation_error(ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            return await _reply(ctx, "Недостаточно аргументов. Пример: `!mute @user 10m Спам` или `!warn @user Причина`.")
        if isinstance(error, commands.BadArgument):
            return await _reply(ctx, "Не найден участник. Укажи @пользователя или ID участника сервера.")
        if isinstance(error, commands.MissingPermissions):
            return await _reply(ctx, "Недостаточно прав модератора.")
        if isinstance(error, commands.BotMissingPermissions):
            return await _reply(ctx, "Vivian не хватает прав для модерации этого участника.")
        if isinstance(error, commands.NoPrivateMessage):
            return await _reply(ctx, "Эта команда работает только на сервере.")
        await _failure(ctx, error)
