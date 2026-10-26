"""Bug reports stay private; ideas have public, persistent voting."""
from __future__ import annotations

import discord
from discord import app_commands

from core import community_store as store, config
from commands import feedback_support as support
from commands.feedback_views import BugModal, SuggestModal, SuggestionVoteView, register_views
from utils import components_v2 as v2


async def _open_form(interaction, kind, screenshot=None):
    problem = support.access_error(interaction)
    if problem:
        return await support.private_reply(interaction, problem)
    remaining = support.cooldown_remaining(interaction.guild_id, interaction.user.id)
    if remaining:
        return await support.private_reply(interaction, f"Следующее обращение можно отправить через {remaining} с.")
    if not store.is_ready():
        return await support.private_reply(interaction, "Хранилище сейчас недоступно. Форму можно будет отправить позже.")
    try:
        support.validate_image(screenshot, interaction.guild)
        support.target_channel(interaction.guild, kind, member=support.member_for(interaction), image=screenshot is not None)
    except ValueError as error:
        return await support.private_reply(interaction, str(error))
    form = (BugModal(interaction.user.id, interaction.guild_id, screenshot) if kind == "bug"
            else SuggestModal(interaction.user.id, interaction.guild_id))
    await interaction.response.send_modal(form)


@config.bot.tree.command(name="bug", description="Лично сообщить команде Vivian об ошибке, можно со скриншотом.")
@app_commands.guild_only()
@app_commands.describe(screenshot="Необязательно: PNG, JPEG, WebP или GIF до 8 МБ, только для команды сервера.")
async def bug(interaction: discord.Interaction, screenshot: discord.Attachment | None = None):
    await _open_form(interaction, "bug", screenshot)


@config.bot.tree.command(name="suggest", description="Предложить улучшение Vivian и открыть голосование участников.")
@app_commands.guild_only()
async def suggest(interaction: discord.Interaction):
    await _open_form(interaction, "suggestion")


async def _bound_message(guild, item):
    channel_id, message_id = item.get("channel_id"), item.get("message_id")
    if not channel_id or not message_id:
        return None
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
        raise ValueError("Канал сохранённой карточки больше недоступен.")
    if item["kind"] == "bug" and not support.private_staff_channel(channel, guild):
        raise ValueError("Карточка ошибки должна оставаться в закрытом канале команды сервера.")
    message = await channel.fetch_message(message_id)
    if config.bot.user is None or message.author.id != config.bot.user.id:
        raise ValueError("Сохранённое сообщение не является карточкой Vivian.")
    return message


def _image_url(message):
    if message is None:
        return None
    for attachment in message.attachments:
        if (attachment.content_type or "").split(";", 1)[0].lower() in support.IMAGE_MIMES:
            return attachment.url
    return None


@config.bot.tree.command(name="report", description="Посмотреть своё обращение или статус публичного предложения по номеру.")
@app_commands.guild_only()
@app_commands.rename(item_id="id")
@app_commands.describe(item_id="Номер ошибки или предложения из квитанции.")
async def report(interaction: discord.Interaction, item_id: int):
    if not await support.acknowledge(interaction):
        return
    problem = support.access_error(interaction)
    if problem:
        return await support.private_reply(interaction, problem)
    if not 0 < item_id < 2 ** 63:
        return await support.private_reply(interaction, "Укажи корректный номер обращения.")
    try:
        item = await store.get_item(interaction.guild_id, item_id)
        if item is None or item["kind"] not in support.STATUSES:
            return await support.private_reply(interaction, "Обращение не найдено на этом сервере.")
        if item["kind"] == "bug" and item["author_id"] != interaction.user.id and not support.is_staff(interaction):
            return await support.private_reply(interaction, "Личный отчёт доступен только его автору и команде сервера.")
        if item["kind"] == "suggestion" and item["author_id"] != interaction.user.id and not support.is_staff(interaction):
            channel = interaction.guild.get_channel(item.get("channel_id")) if item.get("channel_id") else None
            if channel is None or not channel.permissions_for(support.member_for(interaction)).view_channel:
                return await support.private_reply(interaction, "У тебя нет доступа к каналу этого предложения.")
        counts = await store.vote_counts(interaction.guild_id, item_id, interaction.user.id) if item["kind"] == "suggestion" else None
        message = None
        if item["kind"] == "bug" and (item.get("details") or {}).get("screenshot"):
            try:
                message = await _bound_message(interaction.guild, item)
            except (discord.DiscordException, ValueError):
                pass
        problem = support.access_error(interaction)
        if problem:
            return await support.private_reply(interaction, problem)
        if item["kind"] == "bug" and item["author_id"] != interaction.user.id and not support.is_staff(interaction):
            return await support.private_reply(interaction, "Доступ к личному отчёту больше не разрешён.")
        await support.private_reply(interaction, embed=support.feedback_embed(item, counts=counts, image_url=_image_url(message)))
    except store.CommunityUnavailable:
        await support.private_reply(interaction, "Хранилище сейчас недоступно. Попробуй позже.")
    except ValueError:
        await support.private_reply(interaction, "Не получилось прочитать обращение.")


_STATUS_CHOICES = [app_commands.Choice(name=label, value=value) for value, label in (
    ("open", "Открыта / обсуждается"), ("in_progress", "В работе"), ("fixed", "Ошибка исправлена"),
    ("closed", "Ошибка закрыта"), ("accepted", "Идея принята"),
    ("done", "Идея реализована"), ("rejected", "Идея отклонена"),
)]


@config.bot.tree.command(name="feedback_status", description="Команда сервера: изменить статус ошибки или предложения.")
@app_commands.guild_only()
@app_commands.rename(item_id="id")
@app_commands.describe(item_id="Номер ошибки или предложения.", status="Статус для выбранного типа обращения.")
@app_commands.choices(status=_STATUS_CHOICES)
async def feedback_status(interaction: discord.Interaction, item_id: int, status: str):
    if not await support.acknowledge(interaction):
        return
    problem = support.access_error(interaction)
    if problem or not support.is_staff(interaction):
        return await support.private_reply(interaction, problem or "Нужны manage_guild или роль команды сервера.")
    if not 0 < item_id < 2 ** 63:
        return await support.private_reply(interaction, "Укажи корректный номер обращения.")
    try:
        async with support.lock_for("item", interaction.guild_id, item_id):
            item = await store.get_item(interaction.guild_id, item_id)
            if item is None or item["kind"] not in support.STATUSES:
                return await support.private_reply(interaction, "Ошибка или предложение не найдены на этом сервере.")
            if status not in support.STATUSES[item["kind"]]:
                return await support.private_reply(interaction, "Этот статус не подходит к типу обращения.")
            if support.access_error(interaction) or not support.is_staff(interaction):
                return await support.private_reply(interaction, "Право изменять статус больше не разрешено.")
            item = await store.set_status(interaction.guild_id, item_id, status, actor_id=interaction.user.id)
            notice = f"Статус обращения #{item_id}: {support.STATUSES[item['kind']][status]}. Сохранён в базе."
            try:
                message = await _bound_message(interaction.guild, item)
                if message is None:
                    notice += " У обращения пока нет опубликованной карточки."
                else:
                    counts = await store.vote_counts(interaction.guild_id, item_id) if item["kind"] == "suggestion" else None
                    view = SuggestionVoteView(counts, status) if counts is not None else None
                    await v2.edit_message(message, embed=support.feedback_embed(item, counts=counts, image_url=_image_url(message)), view=view)
            except (discord.DiscordException, ValueError, store.CommunityUnavailable) as error:
                print(f"[Feedback] Status card update unavailable; id={item_id}; error={type(error).__name__}", flush=True)
                notice += " Карточку обновить не удалось."
            await support.private_reply(interaction, notice)
    except store.CommunityUnavailable:
        await support.private_reply(interaction, "Хранилище недоступно. Изменение статуса не подтверждено.")
    except ValueError:
        await support.private_reply(interaction, "Не получилось изменить статус обращения.")
