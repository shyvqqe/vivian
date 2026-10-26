from __future__ import annotations
from discord import app_commands
import math
import discord
from core import config as core_config
from core import discord_api as core_discord_api
from utils import helpers as utils_helpers
from utils import command_responses


@core_config.bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if core_discord_api.is_rate_limit_error(original):
        response = getattr(original, "response", None)
        url = str(getattr(response, "url", ""))
        known_bot_rest = isinstance(original, (core_discord_api.DiscordAPIPaused, discord.RateLimited)) or (
            "/channels/" in url or "/guilds/" in url or "/users/" in url
        )
        print(
            f"[Command] Rate limit during command; "
            f"command={getattr(interaction.command, 'name', 'unknown')}; "
            f"error={type(original).__name__}", flush=True,
        )
        return await command_responses.finish_deferred(
            interaction, "Discord временно ограничил действие. Попробуй команду после паузы.",
            error=None if known_bot_rest else original,
        )
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"Эта команда только что использовалась. Повтори через {max(1, math.ceil(error.retry_after))} с."
    elif isinstance(error, app_commands.MissingPermissions):
        message = "Для этой команды не хватает прав участника."
    elif isinstance(error, app_commands.BotMissingPermissions):
        message = "У Vivian не хватает прав для выполнения этой команды."
    elif isinstance(error, app_commands.CheckFailure):
        message = "Команда недоступна в текущем контексте. Проверь права и место запуска."
    else:
        original = getattr(error, "original", error)
        print(
            f"[Command] Failed; command={getattr(interaction.command, 'name', 'unknown')}; "
            f"error={original!r}",
            flush=True,
        )
        message = "Команда не завершилась. Попробуй ещё раз чуть позже."
    try:
        if command_responses.is_thinking(interaction):
            await command_responses.finish_deferred(interaction, message)
        else:
            await utils_helpers.vivian_reply(interaction, message, title="◆ Vivian // ACCESS TRACE", error=True, ephemeral=True)
    except discord.DiscordException as response_error:
        print(f"[Command] Could not deliver themed error reply: {response_error!r}", flush=True)
