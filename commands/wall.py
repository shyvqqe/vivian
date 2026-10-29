from __future__ import annotations

import sqlite3

import discord
from discord import app_commands

from core import config as core_config, friends_store, wall_store as store
from utils import helpers as utils_helpers


def _safe_name(member: discord.abc.User) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(member)))


def _post_embed(post: dict, like_count: int) -> discord.Embed:
    embed = utils_helpers.vivian_embed(post["text"], title="Запись на стене")
    embed.add_field(name="АВТОР", value=f"<@{post['author_id']}>", inline=True)
    embed.add_field(name="ЛАЙКИ", value=str(like_count), inline=True)
    return embed


class CommentModal(discord.ui.Modal, title="Комментарий"):
    text = discord.ui.TextInput(label="Текст комментария", style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, post_id: int):
        super().__init__()
        self.post_id = post_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            post = await store.get_post(self.post_id)
            if post is None:
                return await interaction.edit_original_response(content="Пост больше не существует.")
            await store.add_comment(self.post_id, interaction.user.id, self.text.value)
        except ValueError as error:
            return await interaction.edit_original_response(content=str(error))
        except (sqlite3.Error, OSError):
            return await interaction.edit_original_response(content="Хранилище временно недоступно.")

        destination = None
        if post.get("thread_id"):
            destination = interaction.guild.get_channel(post["thread_id"])
            if destination is None:
                try:
                    destination = await interaction.guild.fetch_channel(post["thread_id"])
                except discord.DiscordException:
                    destination = None
        if destination is not None:
            embed = utils_helpers.vivian_embed(self.text.value, title="Новый комментарий")
            embed.add_field(name="АВТОР", value=interaction.user.mention)
            try:
                await destination.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.DiscordException:
                pass
        await interaction.edit_original_response(content="Комментарий добавлен.")


class LikeButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vivian:wall:like:(?P<post_id>[0-9]+)",
):
    def __init__(self, post_id: int):
        super().__init__(
            discord.ui.Button(
                label="Лайк", emoji="❤️", style=discord.ButtonStyle.secondary,
                custom_id=f"vivian:wall:like:{post_id}",
            )
        )
        self.post_id = post_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["post_id"]))

    async def callback(self, interaction: discord.Interaction):
        try:
            result = await store.toggle_like(self.post_id, interaction.user.id)
            post = await store.get_post(self.post_id)
        except (sqlite3.Error, OSError):
            return await utils_helpers.vivian_reply(
                interaction, "Хранилище временно недоступно.", error=True, ephemeral=True)
        if post is not None and interaction.message is not None:
            try:
                await interaction.message.edit(embed=_post_embed(post, result["count"]))
            except discord.DiscordException:
                pass
        text = "Лайк поставлен." if result["liked"] else "Лайк убран."
        await utils_helpers.vivian_reply(interaction, text, ephemeral=True)


class CommentButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vivian:wall:comment:(?P<post_id>[0-9]+)",
):
    def __init__(self, post_id: int):
        super().__init__(
            discord.ui.Button(
                label="Комментировать", emoji="💬", style=discord.ButtonStyle.secondary,
                custom_id=f"vivian:wall:comment:{post_id}",
            )
        )
        self.post_id = post_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["post_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CommentModal(self.post_id))


def register_views():
    if not getattr(core_config.bot, "wall_dynamic_items_registered", False):
        core_config.bot.add_dynamic_items(LikeButton, CommentButton)
        core_config.bot.wall_dynamic_items_registered = True


async def _check_access(interaction, owner: discord.Member, *, for_posting: bool) -> bool:
    privacy = await store.get_privacy(interaction.guild_id, owner.id)
    is_friend = await friends_store.are_friends(interaction.guild_id, interaction.user.id, owner.id)
    allowed = (store.can_post if for_posting else store.can_view)(privacy, interaction.user.id, owner.id, is_friend)
    if not allowed:
        reason = "постить на эту стену" if for_posting else "смотреть эту стену"
        await utils_helpers.vivian_reply(
            interaction, f"У тебя нет доступа, чтобы {reason}.", error=True, ephemeral=True)
    return allowed


@core_config.bot.tree.command(name="wall_post", description="Оставить запись на стене участника.")
@app_commands.guild_only()
@app_commands.describe(user="Чья это стена", text="Текст поста")
async def wall_post(interaction: discord.Interaction, user: discord.Member, text: str):
    if not await _check_access(interaction, user, for_posting=True):
        return
    try:
        created = await store.create_post(interaction.guild_id, user.id, interaction.user.id, text)
    except ValueError as error:
        return await utils_helpers.vivian_reply(interaction, str(error), error=True, ephemeral=True)
    except (sqlite3.Error, OSError):
        return await utils_helpers.vivian_reply(
            interaction, "Хранилище временно недоступно. Попробуй позже.", error=True, ephemeral=True)

    embed = _post_embed({"text": text, "author_id": interaction.user.id}, 0)
    view = discord.ui.View(timeout=None)
    view.add_item(LikeButton(created["id"]))
    view.add_item(CommentButton(created["id"]))
    try:
        message = await interaction.channel.send(
            content=f"Новая запись на стене {user.mention}", embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions(users=[user]),
        )
        thread_id = None
        try:
            thread = await message.create_thread(name="Комментарии")
            thread_id = thread.id
        except discord.DiscordException:
            pass
        await store.attach_message(created["id"], interaction.channel.id, message.id, thread_id)
    except discord.DiscordException:
        return await utils_helpers.vivian_reply(
            interaction, "Пост сохранён, но карточку отправить не удалось.", error=True, ephemeral=True)
    await utils_helpers.vivian_reply(interaction, "Пост опубликован.", ephemeral=True)


@core_config.bot.tree.command(name="wall", description="Показать стену участника.")
@app_commands.guild_only()
@app_commands.describe(user="Чью стену показать (по умолчанию твою)", page="Страница")
async def wall(interaction: discord.Interaction, user: discord.Member = None,
               page: app_commands.Range[int, 1, 999] = 1):
    target = user or interaction.user
    if not await _check_access(interaction, target, for_posting=False):
        return
    try:
        result = await store.list_posts(interaction.guild_id, target.id, page)
    except (sqlite3.Error, OSError):
        return await utils_helpers.vivian_reply(
            interaction, "Хранилище временно недоступно. Попробуй позже.", error=True, ephemeral=True)
    if not result["items"]:
        return await utils_helpers.vivian_reply(
            interaction, f"На стене {_safe_name(target)} пока пусто.", ephemeral=True)
    lines = []
    for post in result["items"]:
        link = ""
        if post.get("channel_id") and post.get("message_id"):
            link = f" · [перейти](https://discord.com/channels/{interaction.guild_id}/{post['channel_id']}/{post['message_id']})"
        lines.append(f"`#{post['id']}` <@{post['author_id']}>: {post['text'][:200]}{link}")
    embed = utils_helpers.vivian_embed(
        "\n\n".join(lines), title=f"Стена · {_safe_name(target)} · {result['total']}",
    )
    embed.set_footer(text=f"Страница {result['page']} из {result['pages']} · {core_config.VIVIAN_FOOTER}")
    await utils_helpers.vivian_reply(interaction, embed.description, title=embed.title, ephemeral=True)


@core_config.bot.tree.command(name="wall_privacy", description="Настроить, кто может видеть и постить на твою стену.")
@app_commands.guild_only()
@app_commands.describe(setting="все, только друзья или никто")
@app_commands.choices(setting=[
    app_commands.Choice(name="Все", value="all"),
    app_commands.Choice(name="Друзья", value="friends"),
    app_commands.Choice(name="Никто", value="none"),
])
async def wall_privacy(interaction: discord.Interaction, setting: app_commands.Choice[str]):
    try:
        await store.set_privacy(interaction.guild_id, interaction.user.id, setting.value)
    except (ValueError, sqlite3.Error, OSError) as error:
        return await utils_helpers.vivian_reply(interaction, str(error), error=True, ephemeral=True)
    await utils_helpers.vivian_reply(interaction, f"Приватность стены: **{setting.name}**.", ephemeral=True)


@core_config.bot.tree.command(name="wall_delete", description="Удалить пост со своей стены (или свой пост с чужой).")
@app_commands.guild_only()
@app_commands.describe(post_id="Номер поста (виден в /wall)")
async def wall_delete(interaction: discord.Interaction, post_id: int):
    try:
        row = await store.delete_post(interaction.guild_id, post_id, interaction.user.id, is_owner_override=False)
    except PermissionError as error:
        return await utils_helpers.vivian_reply(interaction, str(error), error=True, ephemeral=True)
    except ValueError as error:
        return await utils_helpers.vivian_reply(interaction, str(error), error=True, ephemeral=True)
    except (sqlite3.Error, OSError):
        return await utils_helpers.vivian_reply(
            interaction, "Хранилище временно недоступно. Попробуй позже.", error=True, ephemeral=True)
    if row.get("channel_id") and row.get("message_id"):
        channel = interaction.guild.get_channel(row["channel_id"])
        if channel is not None:
            try:
                message = await channel.fetch_message(row["message_id"])
                await message.delete()
            except discord.DiscordException:
                pass
    await utils_helpers.vivian_reply(interaction, "Пост удалён.", ephemeral=True)
