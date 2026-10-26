"""One durable public roadmap panel, with staff-only mutations."""
from __future__ import annotations

import asyncio
import hashlib

import discord
from discord import app_commands

from core import community_store as store, config
from commands import project_views as views
from utils import components_v2 as v2, v2_delivery


_locks = {}
_views = {}
_SEEDS = (
    ("ui-redesign", "Новый интерфейс Vivian", "Переход команд на Components V2 и обновление карточек.", "in_progress"),
    ("youtube-play", "YouTube в /play", "Довести поиск, выбор трека и воспроизведение в голосовом канале.", "planned"),
    ("prefix-music", "Музыка через !play", "Команда запуска музыки и выбор источника уже добавлены.", "done"),
    ("github-updates", "Обновления из GitHub", "Карточки коммитов и изменённых файлов в канале проекта.", "done"),
    ("public-release", "Публичный выпуск Vivian", "Подготовить понятные команды и описание возможностей.", "in_progress"),
    ("profile-activity", "Новые карточки профиля и активности", "Сделать статистику участников и пульс чата нагляднее.", "planned"),
)


async def project_channel(guild_id):
    guild = config.bot.get_guild(guild_id)
    if guild is None:
        return None
    channel = config.bot.get_channel(config.GITHUB_UPDATES_CHANNEL_ID)
    if channel is None:
        channel = await config.bot.fetch_channel(config.GITHUB_UPDATES_CHANNEL_ID)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)) or channel.guild.id != guild_id:
        return None
    if guild.me is None:
        raise RuntimeError("Bot member is unavailable")
    rights = channel.permissions_for(guild.me)
    sending = rights.send_messages_in_threads if isinstance(channel, discord.Thread) else rights.send_messages
    if not (rights.view_channel and rights.read_message_history and sending):
        raise RuntimeError("Roadmap channel permissions are unavailable")
    return channel


def _is_roadmap(message):
    if config.bot.user is None or message.author.id != config.bot.user.id:
        return False
    if any(embed.title == views.ROADMAP_TITLE and views.ROADMAP_MARKER in (embed.footer.text or "")
           for embed in message.embeds):
        return True
    stack = [component.to_dict() for component in getattr(message, "components", ())]
    texts = []
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if value.get("type") == 10:
                texts.append(str(value.get("content", "")))
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return (any(f"## {views.ROADMAP_TITLE}" in text.splitlines() for text in texts)
            and any(views.ROADMAP_MARKER in text for text in texts))


async def _find_panel(channel, setting):
    if setting and int(setting.get("channel_id", 0)) == channel.id and setting.get("message_id"):
        try:
            message = await channel.fetch_message(int(setting["message_id"]))
            if _is_roadmap(message):
                return message
        except discord.NotFound:
            pass
    # Recover a successful POST if persisting its message ID failed previously.
    async for message in channel.history(limit=100):
        if _is_roadmap(message):
            return message
    return None


async def _publish_locked(guild_id, *, page=None):
    await views.require_storage()
    channel = await project_channel(guild_id)
    if channel is None:
        return None
    setting = await store.get_setting(guild_id, "roadmap_panel")
    message = await _find_panel(channel, setting)
    if page is None:
        page = getattr(_views.get(guild_id), "page", 0)
    records, total = await store.list_items(guild_id, "roadmap", limit=views.PAGE_SIZE,
                                           offset=max(0, page) * views.PAGE_SIZE)
    pages = max(1, (total + views.PAGE_SIZE - 1) // views.PAGE_SIZE)
    if page >= pages:
        page = pages - 1
        records, total = await store.list_items(guild_id, "roadmap", limit=views.PAGE_SIZE,
                                               offset=page * views.PAGE_SIZE)
    view = views.RoadmapView(guild_id, records, total, page)
    payload = {"embed": view.make_embed(), "view": view}
    if message is not None:
        message = await v2.edit_message(message, **payload, attachments=[])
    else:
        prepared = v2.adapt(payload)
        generation = (setting or {}).get("message_id", "initial")
        identity = f"roadmap:{guild_id}:{channel.id}:{generation}"
        nonce = hashlib.blake2b(identity.encode(), digest_size=12).hexdigest()
        sent = await config.bot.http.request(
            discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id),
            json={"flags": 1 << 15, "components": prepared["view"].to_components(),
                  "nonce": nonce, "enforce_nonce": True, "allowed_mentions": {"parse": []}},
        )
        message = discord.Message(state=config.bot._connection, channel=channel, data=sent)
        v2_delivery.commit(payload, prepared["view"], message, state=config.bot._connection)
    _views[guild_id] = view
    if setting != {"channel_id": channel.id, "message_id": message.id}:
        await store.set_setting(guild_id, "roadmap_panel", {"channel_id": channel.id, "message_id": message.id})
    if channel.permissions_for(channel.guild.me).manage_messages and not message.pinned:
        try:
            await message.pin(reason="Vivian public project roadmap")
        except discord.DiscordException:
            pass
    return message


async def publish_roadmap(guild_id):
    async with _locks.setdefault(guild_id, asyncio.Lock()):
        return await _publish_locked(guild_id)


async def refresh_roadmap(guild_id, *, page=None):
    async with _locks.setdefault(guild_id, asyncio.Lock()):
        return await _publish_locked(guild_id, page=page)


async def bootstrap(guild):
    if not await store.initialize() or await project_channel(guild.id) is None:
        return
    async with _locks.setdefault(guild.id, asyncio.Lock()):
        for seed_key, title, body, status in _SEEDS:
            await store.create_item(guild.id, "roadmap", config.bot.user.id, title, body,
                                    status=status, seed_key=f"roadmap:{seed_key}")
        await _publish_locked(guild.id, page=0)


@config.bot.tree.command(name="roadmap", description="Открыть публичный план развития Vivian.")
@app_commands.guild_only()
async def roadmap(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        panel = await publish_roadmap(interaction.guild.id)
        text = (f"[Открыть общий план Vivian]({panel.jump_url})" if panel is not None
                else "Публичный план находится на сервере проекта Vivian.")
    except Exception as error:
        print(f"[Project] Roadmap unavailable; error={type(error).__name__}", flush=True)
        text = "План сейчас недоступен. Сохранённые пункты не изменены."
    await views.notice(interaction, text)


@config.bot.tree.command(name="roadmap_add", description="Добавить пункт в план проекта · для команды сервера.")
@app_commands.guild_only()
@app_commands.choices(status=[app_commands.Choice(name=label, value=value) for value, label in views.STATUSES.items()])
async def roadmap_add(interaction: discord.Interaction, status: str = "planned"):
    if await views.staff_guard(interaction):
        await interaction.response.send_modal(views.RoadmapAddModal(status))


@config.bot.tree.command(name="project_status", description="Изменить статус пункта плана · для команды сервера.")
@app_commands.guild_only()
@app_commands.rename(item_id="id")
@app_commands.choices(status=[app_commands.Choice(name=label, value=value) for value, label in views.STATUSES.items()])
async def project_status(interaction: discord.Interaction, item_id: int, status: str):
    if not await views.staff_guard(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not await views.staff_guard(interaction):
        return
    try:
        await views.require_storage()
        item = await store.get_item(interaction.guild.id, item_id)
        if item is None or item.get("kind") != "roadmap":
            return await views.notice(interaction, "Такого пункта плана на этом сервере нет.")
        if status not in views.STATUSES:
            return await views.notice(interaction, "Выбери предусмотренный статус пункта.")
        if not await views.staff_guard(interaction, fresh=True):
            return
        result = await store.set_status(interaction.guild.id, item_id, status, actor_id=interaction.user.id)
        if not result:
            return await views.notice(interaction, "Не удалось подтвердить изменение статуса.")
    except Exception as error:
        print(f"[Project] Status save unavailable; error={type(error).__name__}", flush=True)
        return await views.notice(interaction, "База сейчас недоступна. Изменение статуса не подтверждено.")
    try:
        panel = await refresh_roadmap(interaction.guild.id)
        suffix = " Общая панель обновлена." if panel is not None else " На этом сервере нет публичной панели проекта."
    except Exception as error:
        print(f"[Project] Status saved; refresh unavailable; error={type(error).__name__}", flush=True)
        suffix = " Панель можно обновить через /roadmap."
    await views.notice(interaction, f"Пункт **#{item_id}**: **{views.STATUSES[status]}**.{suffix}")


# Register the separate showcase commands when the project module is loaded.
from commands import project_showcase as _project_showcase  # noqa: E402,F401
