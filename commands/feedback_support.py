"""Shared permission, media and presentation rules for community feedback."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from io import BytesIO
import time
import warnings
from weakref import WeakValueDictionary

import discord
from PIL import Image

from core import config
from utils import components_v2 as v2


STATUSES = {
    "bug": {"open": "Открыта", "in_progress": "В работе", "fixed": "Исправлена", "closed": "Закрыта"},
    "suggestion": {"open": "Обсуждается", "accepted": "Принята", "in_progress": "В работе",
                   "done": "Реализована", "rejected": "Отклонена"},
}
OPEN_VOTES = frozenset({"open", "accepted", "in_progress"})
IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_locks = WeakValueDictionary()
_submitted = OrderedDict()
_image_gate = asyncio.Semaphore(2)


def lock_for(*key):
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def member_for(interaction):
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        return None
    return guild.get_member(interaction.user.id) or interaction.user


def is_staff(interaction):
    member = member_for(interaction)
    return bool(member and (member.guild_permissions.manage_guild
                or any(role.id == config.STAFF_ROLE_ID for role in member.roles)))


def access_error(interaction):
    member = member_for(interaction)
    channel = interaction.channel
    if member is None or getattr(getattr(channel, "guild", None), "id", None) != interaction.guild_id:
        return "Эта функция доступна участникам сервера в его каналах."
    if not channel.permissions_for(member).view_channel:
        return "У тебя нет доступа к этому каналу."
    return None


def private_staff_channel(channel, guild):
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
        return False
    if channel.permissions_for(guild.default_role).view_channel:
        return False
    bot_roles = set(guild.me.roles) if guild.me else set()
    for role in guild.roles:
        staff_role = (role.id == config.STAFF_ROLE_ID or role.permissions.manage_guild
                      or role.permissions.administrator or (role.is_bot_managed() and role in bot_roles))
        if not staff_role and channel.permissions_for(role).view_channel:
            return False
    for target, overwrite in channel.overwrites.items():
        if overwrite.view_channel is not True:
            continue
        if isinstance(target, discord.Role):
            allowed = (target.id == config.STAFF_ROLE_ID or target.permissions.manage_guild
                       or target.permissions.administrator
                       or (target.is_bot_managed() and target in bot_roles))
        else:
            allowed = (getattr(target, "id", None) == getattr(guild.me, "id", None)
                       or getattr(getattr(target, "guild_permissions", None), "manage_guild", False)
                       or any(role.id == config.STAFF_ROLE_ID for role in getattr(target, "roles", ())))
        if not allowed:
            return False
    return True


def target_channel(guild, kind, *, member=None, image=False):
    channel_id = config.MOD_LOG_CHANNEL_ID if kind == "bug" else config.GITHUB_UPDATES_CHANNEL_ID
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError("Канал для этой функции не настроен на этом сервере.")
    if kind == "bug" and not private_staff_channel(channel, guild):
        raise ValueError("Канал ошибок должен быть закрыт от обычных участников и доступен только команде сервера.")
    if kind == "suggestion" and member is not None and not channel.permissions_for(member).view_channel:
        raise ValueError("У тебя нет доступа к каналу предложений.")
    perms = channel.permissions_for(guild.me) if guild.me else None
    if not perms or not (perms.view_channel and perms.send_messages and perms.read_message_history and perms.embed_links):
        raise ValueError("Vivian нужны права видеть канал, читать историю, отправлять сообщения и встраивать ссылки.")
    if image and not perms.attach_files:
        raise ValueError("В закрытом канале ошибок Vivian нужно право прикреплять файлы.")
    return channel


async def acknowledge(interaction):
    try:
        await interaction.response.defer(thinking=True, ephemeral=True)
        return True
    except discord.DiscordException:
        return False


async def private_reply(interaction, text=None, *, embed=None):
    payload = {"content": text, "embed": embed, "view": None}
    try:
        if interaction.response.is_done():
            return await v2.edit_original(interaction, **payload)
        return await v2.send_response(interaction, **payload, ephemeral=True)
    except discord.DiscordException as error:
        print(f"[Feedback] Private response unavailable; error={type(error).__name__}", flush=True)
        return None


def cooldown_remaining(guild_id, user_id):
    return max(0, int(120 - (time.monotonic() - _submitted.get((guild_id, user_id), -1000))) + 1)


def note_submission(guild_id, user_id):
    key = (guild_id, user_id)
    _submitted[key] = time.monotonic()
    _submitted.move_to_end(key)
    while len(_submitted) > 10000:
        _submitted.popitem(last=False)


def validate_image(attachment, guild):
    if attachment is None:
        return
    limit = min(MAX_IMAGE_BYTES, guild.filesize_limit)
    mime = (attachment.content_type or "").split(";", 1)[0].lower()
    if mime not in IMAGE_MIMES or attachment.size <= 0 or attachment.size > limit:
        raise ValueError(f"Скриншот: только PNG, JPEG, WebP или GIF, до {limit // (1024 * 1024)} МБ.")


def _image_format(data):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as image:
            extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}.get(image.format)
            if extension is None or image.width * image.height > 20_000_000:
                raise ValueError("Неподдерживаемое изображение или слишком большое разрешение.")
            image.verify()
            return extension


async def download_image(attachment, guild):
    if attachment is None:
        return None, None
    validate_image(attachment, guild)
    try:
        async with asyncio.timeout(12), _image_gate:
            data = await attachment.read()
            if not data or len(data) > min(MAX_IMAGE_BYTES, guild.filesize_limit):
                raise ValueError("Изображение слишком большое или пустое.")
            extension = await asyncio.to_thread(_image_format, data)
            return data, extension
    except TimeoutError as error:
        raise ValueError("Скриншот не загрузился вовремя. Попробуй прикрепить его ещё раз.") from error
    except (discord.DiscordException, OSError, Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        raise ValueError("Не получилось прочитать скриншот. Прикрепи обычный PNG, JPEG, WebP или GIF.") from error


def safe(value, limit=2000):
    text = str(value or "")[:limit]
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text, ignore_links=False))


def feedback_embed(item, *, counts=None, image_url=None):
    kind = item["kind"]
    status = STATUSES.get(kind, {}).get(item["status"], item["status"])
    title = f"Ошибка #{item['id']}" if kind == "bug" else f"Идея #{item['id']}"
    lines = [f"**{safe(item['title'], 100)}**", safe(item.get("body"), 1800), "",
             f"Статус: **{safe(status, 50)}**", f"Автор: {int(item['author_id'])}"]
    if kind == "bug":
        details = item.get("details") or {}
        if details.get("command"):
            lines.extend(("", "**Команда**", safe(details["command"], 100)))
        if details.get("reproduction"):
            lines.extend(("", "**Как повторить**", safe(details["reproduction"], 800)))
    elif counts is not None:
        lines.extend(("", f"За: **{counts['up']}** · Против: **{counts['down']}**"))
    embed = discord.Embed(title=title, description="\n".join(lines)[:3500], color=config.VIVIAN_ACCENT)
    embed.set_footer(text="Личный отчёт · Vivian" if kind == "bug" else "Предложения сообщества · Vivian")
    if image_url:
        embed.set_image(url=image_url)
    return embed
