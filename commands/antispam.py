from __future__ import annotations
import discord
import os
import re
from datetime import timedelta
from core import config as core_config
from core import discord_api as core_discord_api
from core import moderation_state as core_moderation_state
from core import signal as core_signal
from utils import mod_logs as utils_mod_logs
from utils import components_v2 as v2


def _message_contains_gif(message: discord.Message) -> bool:
    if message.stickers:
        return True

    for attachment in message.attachments:
        filename = attachment.filename.lower()
        content_type = (attachment.content_type or "").lower()
        if content_type == "image/gif" or filename.endswith((".gif", ".gifv")):
            return True

    content = (message.content or "").lower()
    if any(marker in content for marker in (
        "tenor.com/view/", "media.tenor.com/", "giphy.com/gifs/", "media.giphy.com/media/", ".gifv", ".gif"
    )):
        return True

    for embed in message.embeds:
        provider = (embed.provider.name or "").lower() if embed.provider else ""
        if "gif" in (embed.type or "").lower() or provider in {"tenor", "giphy"}:
            return True
    return False


def _is_forwarded_message(message: discord.Message) -> bool:
    message_type = getattr(message.type, "name", "")
    reference_type = getattr(getattr(message, "reference", None), "type", None)
    reference_type_name = getattr(reference_type, "name", "")
    return (
        str(message_type).casefold() == "forward"
        or str(reference_type_name).casefold() == "forward"
        or bool(getattr(message, "message_snapshots", None))
    )


def _is_photo_message(message: discord.Message) -> bool:
    return any(
        (attachment.content_type or "").lower().startswith("image/")
        or os.path.splitext(attachment.filename)[1].lower() in {".png", ".jpg", ".jpeg", ".webp"}
        for attachment in message.attachments
    )


def _normalized_spam_text(message: discord.Message) -> str:
    content = re.sub(r"<@!?\d+>", " @user ", (message.content or "").casefold())
    return re.sub(r"\s+", " ", content).strip()


async def _delete_moderation_messages(messages: list[discord.Message]) -> bool:
    if core_discord_api.is_paused():
        return False
    unique_messages = {item.id: item for item in messages if item is not None}
    items = list(unique_messages.values())
    if not items:
        return False
    if len(items) > 1:
        channel = items[0].channel
        if all(item.channel.id == channel.id for item in items) and hasattr(channel, "delete_messages"):
            try:
                await channel.delete_messages(items, reason="Automatic moderation")
                return True
            except discord.DiscordException as error:
                if core_discord_api.is_rate_limit_error(error) or core_discord_api.is_paused():
                    return False
                if not isinstance(error, discord.HTTPException):
                    raise
                if error.status != 404:
                    print(f"[Автомодерация] Пакетное удаление не удалось, удаляю по одному: {error!r}", flush=True)
    if core_discord_api.is_paused():
        return False
    deletion_failed = False
    for item in items:
        if core_discord_api.is_paused():
            return False
        try:
            await item.delete()
        except discord.NotFound:
            continue
        except discord.Forbidden:
            print("[Автомодерация] Нет права удалять сообщения.", flush=True)
            return False
        except discord.DiscordException as error:
            if core_discord_api.is_rate_limit_error(error) or core_discord_api.is_paused():
                return False
            if not isinstance(error, discord.HTTPException):
                raise
            print(f"[Автомодерация] Ошибка удаления сообщения: {error!r}", flush=True)
            deletion_failed = True
    return not deletion_failed


async def _record_moderation_incident(
    message: discord.Message,
    category: str,
    reason: str,
    messages: list[discord.Message],
    now: float,
) -> None:
    lock = core_moderation_state.moderation_action_locks[(message.guild.id, message.author.id)]
    async with lock:
        await _record_moderation_incident_locked(message, category, reason, messages, now)


async def _record_moderation_incident_locked(
    message: discord.Message,
    category: str,
    reason: str,
    messages: list[discord.Message],
    now: float,
) -> None:
    if core_discord_api.is_paused():
        return
    guild = message.guild
    member = message.author
    warning_key = (guild.id, member.id, category)
    previous_warning = core_moderation_state.moderation_warning_at.get(warning_key, 0.0)
    had_warning = warning_key in core_moderation_state.moderation_warning_at

    def restore_warning() -> None:
        if had_warning:
            core_moderation_state.moderation_warning_at[warning_key] = previous_warning
        else:
            core_moderation_state.moderation_warning_at.pop(warning_key, None)

    repeated = bool(previous_warning and now - previous_warning < core_config.MODERATION_REPEAT_WINDOW)
    timeout_minutes = None
    if repeated:
        previous_timeout = core_moderation_state.spam_last_timeout[(guild.id, member.id)]
        timeout_minutes = (
            core_config.SPAM_REPEAT_MUTE_MINUTES
            if previous_timeout and now - previous_timeout < core_config.SPAM_REPEAT_WINDOW
            else core_config.SPAM_MUTE_MINUTES
        )
    core_moderation_state.moderation_warning_at[warning_key] = now

    try:
        await utils_mod_logs.send_spam_incident_log(guild, member, reason, timeout_minutes, messages)
    except discord.DiscordException as error:
        if core_discord_api.is_rate_limit_error(error):
            restore_warning()
            return
        raise
    if core_discord_api.is_paused():
        restore_warning()
        return
    deleted = await _delete_moderation_messages(messages)
    if core_discord_api.is_paused():
        restore_warning()
        return

    if timeout_minutes is not None:
        try:
            await member.timeout(
                discord.utils.utcnow() + timedelta(minutes=timeout_minutes),
                reason=f"Vivian moderation: {reason}",
            )
            core_moderation_state.spam_last_timeout[(guild.id, member.id)] = now
            core_signal.store_echo(guild.id, "moderation_timeout", f"Timeout участника {member.id}: {category}")
            notice = f"{member.mention}, повторное нарушение. Таймаут: {timeout_minutes} минут."
        except discord.Forbidden:
            print(f"[Автомодерация] Нет права выдать таймаут участнику {member.id}.", flush=True)
            delete_status = "сообщения удалены" if deleted else "удалить сообщение не удалось"
            notice = f"{member.mention}, {delete_status}. Повторное нарушение передано модераторам."
        except discord.DiscordException as error:
            if core_discord_api.is_rate_limit_error(error):
                restore_warning()
                return
            if not isinstance(error, discord.HTTPException):
                raise
            print(f"[Автомодерация] Не удалось выдать таймаут: {error!r}", flush=True)
            delete_status = "сообщения удалены" if deleted else "удалить сообщение не удалось"
            notice = f"{member.mention}, {delete_status}. Повторное нарушение передано модераторам."
    else:
        notice = (
            f"{member.mention}, {'сообщения удалены' if deleted else 'нарушение обнаружено, но удалить сообщение не удалось'}. "
            "Это предупреждение; "
            "таймаут последует только при повторе."
        )

    if core_discord_api.is_paused():
        restore_warning()
        return
    try:
        await v2.send_message(message.channel, notice,
            delete_after=15,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.DiscordException as error:
        if core_discord_api.is_rate_limit_error(error):
            restore_warning()
            return
        if not isinstance(error, discord.HTTPException):
            raise
        print(f"[Автомодерация] Не удалось отправить уведомление: {error!r}", flush=True)


async def _moderate_message_spam(message: discord.Message, now: float) -> bool:
    guild = message.guild
    if guild is None:
        return False

    member = message.author
    permissions = getattr(member, "guild_permissions", None)
    if permissions and (permissions.administrator or permissions.manage_messages):
        return False

    key = (guild.id, member.id)
    recent_messages = core_moderation_state.spam_tracker[key]
    repeated_messages = core_moderation_state.spam_repeat_tracker[key]
    recent_gifs = core_moderation_state.gif_spam_tracker[key]
    while recent_messages and now - recent_messages[0][0] >= core_config.SPAM_INTERVAL:
        recent_messages.popleft()
    while repeated_messages and now - repeated_messages[0][0] >= core_config.SPAM_REPEAT_INTERVAL:
        repeated_messages.popleft()
    while recent_gifs and now - recent_gifs[0][0] >= core_config.GIF_SPAM_INTERVAL:
        recent_gifs.popleft()

    # One Discord album is one event; a quick run of separate photo posts is grouped too.
    is_photo = _is_photo_message(message)
    photo_continuation = is_photo and now - core_moderation_state.photo_burst_last[key] < 5.0
    if is_photo:
        core_moderation_state.photo_burst_last[key] = now
    exempt_from_message_flood = _is_forwarded_message(message) or photo_continuation
    if not exempt_from_message_flood:
        recent_messages.append((now, message))
        if not is_photo:
            normalized = _normalized_spam_text(message)
            if normalized:
                repeated_messages.append((now, normalized, message))
    is_sticker_or_gif = _message_contains_gif(message)
    if is_sticker_or_gif:
        recent_gifs.append((now, message))

    message_flood = len(recent_messages) >= core_config.SPAM_LIMIT
    repeated_text = _normalized_spam_text(message)
    repeated_group = [item for _, content, item in repeated_messages if content == repeated_text]
    repeated_flood = len(repeated_group) >= core_config.SPAM_REPEAT_LIMIT
    sticker_flood = len(recent_gifs) >= core_config.GIF_SPAM_LIMIT
    if not message_flood and not repeated_flood and not sticker_flood:
        return False

    if sticker_flood:
        reason, messages_to_delete = "лавина стикеров или GIF", [item for _, item in recent_gifs]
        recent_gifs.clear()
    elif repeated_flood:
        reason, messages_to_delete = "повторяющиеся сообщения", repeated_group
        repeated_messages.clear()
    else:
        reason, messages_to_delete = "лавина сообщений", [
            item for _, item in recent_messages if not _is_photo_message(item)
        ]
    recent_messages.clear()
    repeated_messages.clear()
    recent_gifs.clear()

    await _record_moderation_incident(
        message,
        "spam",
        reason,
        list({item.id: item for item in messages_to_delete}.values()),
        now,
    )
    return True
