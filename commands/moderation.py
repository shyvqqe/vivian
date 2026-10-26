from __future__ import annotations
from discord import app_commands
import discord
from core import config as core_config
from core import discord_api as core_discord_api
from utils import helpers as utils_helpers
from utils import command_responses as utils_command_responses
from utils import mod_logs as utils_mod_logs


def _result_card(interaction, title, description, *, target=None, reason=None):
    embed = utils_helpers.vivian_embed(description, title=title)
    if target is not None:
        embed.add_field(name="УЧАСТНИК", value=f"{target.mention}\n`{target.id}`", inline=True)
        embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="МОДЕРАТОР", value=interaction.user.mention, inline=True)
    embed.add_field(name="КАНАЛ", value=interaction.channel.mention, inline=True)
    if reason is not None:
        embed.add_field(name="ОСНОВАНИЕ", value=discord.utils.escape_markdown(reason.strip() or "Не указана")[:1024], inline=False)
    embed.set_footer(text="Действие выполнено · VIVIAN // MODERATION")
    return embed


async def _action_failed(interaction, error, description):
    print(
        f"[Moderation] Action failed; command={getattr(getattr(interaction, 'command', None), 'name', 'unknown')}; "
        f"error={type(error).__name__}; status={getattr(error, 'status', None)}; "
        f"code={getattr(error, 'code', None)}", flush=True,
    )
    if core_discord_api.is_rate_limit_error(error):
        description = "Discord временно ограничил запросы. Действие не повторялось."
    # This error belongs to the bot-token action, not the interaction webhook.
    return await utils_helpers.vivian_reply(
        interaction, description, title="Действие не выполнено", error=True, ephemeral=True,
    )


@core_config.bot.tree.command(name="ban", description="Заблокировать участнику доступ к серверу.")
@app_commands.guild_only()
@app_commands.describe(user="Кого заблокировать", reason="Основание для журнала модерации")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str = "Не указана"):
    if not await utils_command_responses.defer_command(interaction):
        return
    try:
        await user.ban(reason=reason)
    except Exception as e:
        return await _action_failed(interaction, e, "Блокировка не выполнена. Проверь права Vivian и положение ролей.")
    await utils_command_responses.edit_deferred(
        interaction,
        embed=_result_card(interaction, "Доступ заблокирован", "Участник больше не может присоединиться к серверу.",
                           target=user, reason=reason),
    )
    await utils_mod_logs.send_mod_log(interaction.guild, f"🔨 **Бан** {user} от {interaction.user.mention}. Причина: {reason}")


@core_config.bot.tree.command(name="kick", description="Удалить участника с сервера; он сможет присоединиться снова.")
@app_commands.guild_only()
@app_commands.describe(user="Кого удалить с сервера", reason="Основание для журнала модерации")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "Не указана"):
    if not await utils_command_responses.defer_command(interaction):
        return
    try:
        await user.kick(reason=reason)
    except Exception as e:
        return await _action_failed(interaction, e, "Удаление не выполнено. Проверь права Vivian и положение ролей.")
    await utils_command_responses.edit_deferred(
        interaction,
        embed=_result_card(interaction, "Участник удалён", "При наличии приглашения он сможет вернуться.",
                           target=user, reason=reason),
    )


@core_config.bot.tree.command(name="clear", description="Удалить недавние сообщения из текущего канала.")
@app_commands.guild_only()
@app_commands.describe(amount="Сколько последних сообщений удалить (от 1 до 100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not 1 <= amount <= 100:
        return await utils_helpers.vivian_reply(interaction, "Укажи число от 1 до 100.", error=True, ephemeral=True)
    if not await utils_command_responses.defer_command(interaction, ephemeral=True):
        return
    try:
        deleted = await interaction.channel.purge(limit=amount)
    except discord.DiscordException as error:
        return await _action_failed(interaction, error, "Не удалось очистить сообщения. Проверь права Vivian в канале.")
    await utils_command_responses.edit_deferred(
        interaction,
        embed=_result_card(interaction, "Канал очищен", f"Удалено сообщений: **{len(deleted)}**."),
    )


@core_config.bot.tree.command(name="slowmode", description="Задать интервал между сообщениями в текущем канале.")
@app_commands.guild_only()
@app_commands.describe(seconds="Интервал в секундах; 0 отключает замедление")
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    if not await utils_command_responses.defer_command(interaction):
        return
    try:
        await interaction.channel.edit(slowmode_delay=seconds)
    except discord.DiscordException as error:
        return await _action_failed(interaction, error, "Не удалось изменить интервал. Проверь права Vivian в канале.")
    message = f"Интервал установлен: **{seconds} сек.**" if seconds else "Замедление отключено."
    await utils_command_responses.edit_deferred(
        interaction, embed=_result_card(interaction, "Ритм канала обновлён", message))


@core_config.bot.tree.command(name="lock", description="Закрыть текущий канал для обычных участников.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    if not await utils_command_responses.defer_command(interaction):
        return
    try:
        await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
    except discord.DiscordException as error:
        return await _action_failed(interaction, error, "Не удалось закрыть канал. Проверь права Vivian.")
    await utils_command_responses.edit_deferred(
        interaction, embed=_result_card(interaction, "Канал закрыт", "Для роли @everyone отправка сообщений запрещена."))


@core_config.bot.tree.command(name="unlock", description="Снова разрешить сообщения в текущем канале.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    if not await utils_command_responses.defer_command(interaction):
        return
    try:
        await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=True)
    except discord.DiscordException as error:
        return await _action_failed(interaction, error, "Не удалось открыть канал. Проверь права Vivian.")
    await utils_command_responses.edit_deferred(
        interaction, embed=_result_card(interaction, "Канал открыт", "Для роли @everyone отправка сообщений разрешена."))
