from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands

from core import config as core_config, status_store as store
from utils import helpers as utils_helpers


def _safe_name(member: discord.abc.User) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(member)))


class RelationshipRequestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _respond(self, interaction: discord.Interaction, accept: bool):
        if interaction.guild_id is None or interaction.message is None:
            return
        try:
            result = await store.resolve_by_message(
                interaction.guild_id, interaction.message.id, interaction.user.id, accept,
            )
        except PermissionError as error:
            return await utils_helpers.vivian_reply(interaction, str(error), error=True, ephemeral=True)
        except ValueError as error:
            return await utils_helpers.vivian_reply(interaction, str(error), error=True, ephemeral=True)
        except (sqlite3.Error, OSError):
            return await utils_helpers.vivian_reply(
                interaction, "Хранилище временно недоступно.", error=True, ephemeral=True)

        from_mention, to_mention = f"<@{result['from_id']}>", f"<@{result['to_id']}>"
        if accept:
            embed = utils_helpers.vivian_embed(
                f"{from_mention} и {to_mention} теперь в отношениях. 💞", title="Предложение принято")
        else:
            embed = utils_helpers.vivian_embed(
                f"{to_mention} отклонил(а) предложение {from_mention}.", title="Предложение отклонено")
        for child in self.children:
            child.disabled = True
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            try:
                await interaction.message.edit(embed=embed, view=self)
            except discord.DiscordException:
                pass

    @discord.ui.button(label="Да", style=discord.ButtonStyle.success, custom_id="vivian_status_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._respond(interaction, True)

    @discord.ui.button(label="Нет", style=discord.ButtonStyle.secondary, custom_id="vivian_status_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._respond(interaction, False)


def register_views():
    if not getattr(core_config.bot, "status_request_view_registered", False):
        core_config.bot.add_view(RelationshipRequestView())
        core_config.bot.status_request_view_registered = True


@core_config.bot.tree.command(name="status_relationship", description="Предложить отношения участнику.")
@app_commands.guild_only()
@app_commands.describe(user="Кому предложить")
async def status_relationship(interaction: discord.Interaction, user: discord.Member):
    if user.bot or user.id == interaction.user.id:
        return await utils_helpers.vivian_reply(
            interaction, "Нельзя предложить отношения себе или боту.", error=True, ephemeral=True)
    try:
        request_id = await store.propose(interaction.guild_id, interaction.user.id, user.id)
    except ValueError as error:
        return await utils_helpers.vivian_reply(interaction, str(error), error=True, ephemeral=True)
    except (sqlite3.Error, OSError):
        return await utils_helpers.vivian_reply(
            interaction, "Хранилище временно недоступно. Попробуй позже.", error=True, ephemeral=True)

    embed = utils_helpers.vivian_embed(
        f"{interaction.user.mention} предлагает отношения {user.mention}.", title="Предложение отношений")
    try:
        message = await interaction.channel.send(
            content=user.mention, embed=embed, view=RelationshipRequestView(),
            allowed_mentions=discord.AllowedMentions(users=[user]),
        )
    except discord.DiscordException:
        return await utils_helpers.vivian_reply(
            interaction, "Не удалось отправить карточку предложения.", error=True, ephemeral=True)
    await store.attach_message(interaction.guild_id, request_id, message.id)
    await utils_helpers.vivian_reply(interaction, "Предложение отправлено.", ephemeral=True)


@core_config.bot.tree.command(name="status_single", description="Завершить отношения.")
@app_commands.guild_only()
async def status_single(interaction: discord.Interaction):
    try:
        ex_partner_id = await store.single(interaction.guild_id, interaction.user.id)
    except (sqlite3.Error, OSError):
        return await utils_helpers.vivian_reply(
            interaction, "Хранилище временно недоступно. Попробуй позже.", error=True, ephemeral=True)
    if ex_partner_id is None:
        return await utils_helpers.vivian_reply(interaction, "Ты и так не в отношениях.", ephemeral=True)
    await utils_helpers.vivian_reply(interaction, f"Теперь ты свободен(на) от <@{ex_partner_id}>.", ephemeral=True)


@core_config.bot.tree.command(name="status_set", description="Задать свободный статус и настроение.")
@app_commands.guild_only()
@app_commands.describe(text="Текст статуса (необязательно)", mood="Эмодзи настроения (необязательно)")
async def status_set(interaction: discord.Interaction, text: str = None, mood: str = None):
    if text is None and mood is None:
        return await utils_helpers.vivian_reply(
            interaction, "Укажи текст статуса, настроение или оба параметра.", error=True, ephemeral=True)
    try:
        await store.set_profile(interaction.guild_id, interaction.user.id, text, mood)
    except (ValueError, sqlite3.Error, OSError) as error:
        return await utils_helpers.vivian_reply(interaction, str(error), error=True, ephemeral=True)
    await utils_helpers.vivian_reply(interaction, "Статус обновлён.", ephemeral=True)


@core_config.bot.tree.command(name="status_view", description="Показать статус и отношения участника.")
@app_commands.guild_only()
@app_commands.describe(user="Чей статус показать (по умолчанию твой)")
async def status_view(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    try:
        relationship = await store.get_relationship(interaction.guild_id, target.id)
        profile = await store.get_profile(interaction.guild_id, target.id)
    except (sqlite3.Error, OSError):
        return await utils_helpers.vivian_reply(
            interaction, "Хранилище временно недоступно. Попробуй позже.", error=True, ephemeral=True)

    lines = []
    if relationship:
        since = datetime.fromtimestamp(relationship["since"], tz=timezone.utc).strftime("%d.%m.%Y")
        lines.append(f"**В отношениях** с <@{relationship['partner_id']}> · с {since}")
    else:
        lines.append("**Свободен(на)**")
    if profile and (profile.get("text") or profile.get("mood")):
        mood = f"{profile['mood']} " if profile.get("mood") else ""
        lines.append(f"{mood}{discord.utils.escape_markdown(profile.get('text') or '')}".strip())
    await utils_helpers.vivian_reply(
        interaction, "\n".join(lines), title=f"Статус · {_safe_name(target)}", ephemeral=True)
