from __future__ import annotations

import sqlite3
import time

import discord
from discord.ext import tasks
from discord import app_commands

from core import config as core_config, story_store as store
from utils import helpers as utils_helpers


class SeenButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vivian:story:seen:(?P<story_id>[0-9]+)",
):
    def __init__(self, story_id: int):
        super().__init__(
            discord.ui.Button(
                label="Посмотрел(а)", emoji="👁️", style=discord.ButtonStyle.secondary,
                custom_id=f"vivian:story:seen:{story_id}",
            )
        )
        self.story_id = story_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["story_id"]))

    async def callback(self, interaction: discord.Interaction):
        try:
            is_new = await store.record_view(self.story_id, interaction.user.id)
        except (sqlite3.Error, OSError):
            return await utils_helpers.vivian_reply(
                interaction, "Хранилище временно недоступно.", error=True, ephemeral=True)
        text = "Отмечено, что ты посмотрел(а)." if is_new else "Ты уже отмечал(а) просмотр."
        await utils_helpers.vivian_reply(interaction, text, ephemeral=True)


class ViewersButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vivian:story:viewers:(?P<story_id>[0-9]+)",
):
    def __init__(self, story_id: int, author_id: int):
        super().__init__(
            discord.ui.Button(
                label="Просмотры", emoji="📊", style=discord.ButtonStyle.secondary,
                custom_id=f"vivian:story:viewers:{story_id}",
            )
        )
        self.story_id = story_id
        self.author_id = author_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        story = await store.get_story(int(match["story_id"]))
        author_id = story["author_id"] if story else 0
        return cls(int(match["story_id"]), author_id)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await utils_helpers.vivian_reply(
                interaction, "Список просмотров виден только автору истории.", error=True, ephemeral=True)
        try:
            viewer_ids = await store.list_viewers(self.story_id)
        except (sqlite3.Error, OSError):
            return await utils_helpers.vivian_reply(
                interaction, "Хранилище временно недоступно.", error=True, ephemeral=True)
        text = "\n".join(f"<@{user_id}>" for user_id in viewer_ids) or "Пока никто не посмотрел."
        await utils_helpers.vivian_reply(
            interaction, text, title=f"Просмотры · {len(viewer_ids)}", ephemeral=True)


def register_views():
    if not getattr(core_config.bot, "story_dynamic_items_registered", False):
        core_config.bot.add_dynamic_items(SeenButton, ViewersButton)
        core_config.bot.story_dynamic_items_registered = True


@tasks.loop(minutes=5)
async def story_cleanup_loop():
    try:
        due = await store.due_expired(time.time())
    except (sqlite3.Error, OSError):
        return
    for item in due:
        guild = core_config.bot.get_guild(item["guild_id"])
        if guild is not None and item.get("message_id"):
            channel = guild.get_channel(item["channel_id"])
            if channel is not None:
                try:
                    message = await channel.fetch_message(item["message_id"])
                    await message.delete()
                except discord.DiscordException:
                    pass
        await store.mark_deleted(item["id"])


@story_cleanup_loop.before_loop
async def before_story_cleanup_loop():
    await core_config.bot.wait_until_ready()


def start():
    if not story_cleanup_loop.is_running():
        story_cleanup_loop.start()


@core_config.bot.tree.command(name="story_setup", description="Назначить канал для историй на 24 часа.")
@app_commands.guild_only()
@app_commands.describe(channel="Канал для публикации историй")
@app_commands.checks.has_permissions(manage_guild=True)
async def story_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    try:
        await store.set_channel(interaction.guild_id, channel.id)
    except (sqlite3.Error, OSError):
        return await utils_helpers.vivian_reply(
            interaction, "Хранилище временно недоступно. Попробуй позже.", error=True, ephemeral=True)
    await utils_helpers.vivian_reply(interaction, f"Истории теперь публикуются в {channel.mention}.", ephemeral=True)


@core_config.bot.tree.command(name="story_post", description="Опубликовать историю на 24 часа.")
@app_commands.guild_only()
@app_commands.describe(text="Текст истории (необязательно)", image="Картинка (необязательно)")
async def story_post(interaction: discord.Interaction, text: str = None, image: discord.Attachment = None):
    channel_id = await store.get_channel(interaction.guild_id)
    if channel_id is None:
        return await utils_helpers.vivian_reply(
            interaction, "Канал историй ещё не настроен. Попроси модератора выполнить /story_setup.",
            error=True, ephemeral=True)
    channel = interaction.guild.get_channel(channel_id)
    if channel is None:
        return await utils_helpers.vivian_reply(
            interaction, "Настроенный канал историй недоступен.", error=True, ephemeral=True)
    image_url = image.url if image is not None else None
    if image is not None and not (image.content_type or "").startswith("image/"):
        return await utils_helpers.vivian_reply(
            interaction, "Вложение должно быть изображением.", error=True, ephemeral=True)
    try:
        created = await store.create_story(interaction.guild_id, channel.id, interaction.user.id, text, image_url)
    except ValueError as error:
        return await utils_helpers.vivian_reply(interaction, str(error), error=True, ephemeral=True)
    except (sqlite3.Error, OSError):
        return await utils_helpers.vivian_reply(
            interaction, "Хранилище временно недоступно. Попробуй позже.", error=True, ephemeral=True)

    embed = utils_helpers.vivian_embed(text or "", title=f"История · {interaction.user.display_name}")
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text="Исчезнет через 24 часа · " + core_config.VIVIAN_FOOTER)
    view = discord.ui.View(timeout=None)
    view.add_item(SeenButton(created["id"]))
    view.add_item(ViewersButton(created["id"], interaction.user.id))
    try:
        message = await channel.send(embed=embed, view=view)
    except discord.DiscordException:
        return await utils_helpers.vivian_reply(
            interaction, "История сохранена, но опубликовать карточку не удалось.", error=True, ephemeral=True)
    await store.attach_message(created["id"], message.id)
    await utils_helpers.vivian_reply(interaction, "История опубликована на 24 часа.", ephemeral=True)
