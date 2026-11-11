from __future__ import annotations
import discord
from core import config as core_config
from commands import veyr_views as commands_veyr_views
from core import signal as core_signal
from utils import helpers as utils_helpers
from utils import command_responses as utils_command_responses


@core_config.bot.tree.command(name="veyr", description="Центр Vivian: пульс сервера, общие события, задания и хроника.")
async def veyr_control_center(interaction: discord.Interaction):
    if interaction.guild is None:
        return await utils_helpers.vivian_reply(
            interaction,
            "Центр Vivian доступен на сервере. Там видны его активность, события и задания.",
            error=True,
            ephemeral=True,
        )
    if not await utils_command_responses.defer_command(interaction, ephemeral=True):
        return
    core_signal.discover_layer(interaction.guild.id, "SYSTEM")
    view = commands_veyr_views.VeyrControlView(
        interaction.guild.id,
        interaction.user.id,
        interaction.user.guild_permissions.manage_guild,
    )
    view.panel_message = await utils_command_responses.send_panel(interaction, view=view, style="system")
