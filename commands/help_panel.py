"""Compact help pages built from the registered commands."""
from __future__ import annotations

import discord
from discord import app_commands

from core import config
from utils import helpers
from utils import components_v2 as v2
from visuals import panels


def command_entries():
    entries = []
    for command in sorted(config.bot.tree.get_commands(), key=lambda item: item.name.casefold()):
        if isinstance(command, app_commands.Group):
            for child in sorted(command.commands, key=lambda item: item.name.casefold()):
                entries.append((f"/{command.name} {child.name}", child.description))
        else:
            entries.append((f"/{command.name}", command.description))
    entries.extend([
        ("!play [youtube|soundcloud] запрос", "Текущая музыка: без источника предложит кнопки выбора. Новый /play в разработке."),
        ("!stop", "Остановить музыку и выйти из голосового канала."),
    ])
    return entries


class HelpView(v2.ControllerView):
    def __init__(self, owner_id, guild_id, visual_enabled=False):
        super().__init__(timeout=600)
        self.owner_id, self.guild_id = owner_id, guild_id
        self.visual_enabled = visual_enabled
        self.page = 0
        self.entries = command_entries()
        self.panel_message = None
        self.sync_controls()

    def sync_controls(self):
        self.pages = max(1, (len(self.entries) + 4) // 5)
        self.page = max(0, min(self.page, self.pages - 1))
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page == self.pages - 1

    def make_embed(self):
        embed = helpers.vivian_embed(title="Команды Vivian", description=f"`SYSTEM / HELP` · {len(self.entries)} команд")
        for name, description in self.entries[self.page * 5:self.page * 5 + 5]:
            embed.add_field(name=name, value=description[:200], inline=False)
        embed.set_footer(text=f"{self.page + 1} / {self.pages} · VIVIAN // SYSTEM")
        if self.visual_enabled:
            panels._apply_panel_visual(embed, "system")
        return embed

    async def interaction_check(self, interaction):
        guild_id = interaction.guild.id if interaction.guild else 0
        if interaction.user.id == self.owner_id and guild_id == self.guild_id:
            return True
        try:
            await helpers.vivian_reply(interaction, "Открой свою панель командой `/help`.", ephemeral=True)
        except discord.HTTPException:
            pass
        return False

    async def update(self, interaction):
        self.sync_controls()
        try:
            await v2.edit_response(interaction, embed=self.make_embed(), view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="←", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page -= 1
        await self.update(interaction)

    @discord.ui.button(label="→", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page += 1
        await self.update(interaction)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.panel_message is not None:
            try:
                await v2.edit_message(self.panel_message, view=self)
            except discord.HTTPException:
                pass
