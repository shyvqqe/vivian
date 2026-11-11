from __future__ import annotations
import discord
from core import config as core_config
from commands import veyr_panels as commands_veyr_panels
from commands import quest_panels
from core import rituals as core_rituals
from core import signal as core_signal
from core import veyr_state as core_veyr_state
from utils import helpers as utils_helpers
from utils import components_v2 as v2
from visuals import panels as visuals_panels


class VeyrSectionSelect(discord.ui.Select):
    def __init__(self, section="SYSTEM"):
        super().__init__(
            placeholder="Выбери раздел центра Vivian",
            min_values=1,
            max_values=1,
            row=0,
            options=[discord.SelectOption(label=label, value=value, default=value == section)
                     for value, label in commands_veyr_panels.VEYR_SECTIONS],
        )

    async def callback(self, interaction: discord.Interaction):
        view: VeyrControlView = self.view
        if interaction.user.id != view.owner_id:
            return await view.reject(interaction)
        section = self.values[0]
        core_signal.discover_layer(view.guild_id, section)
        view.show_section(section)
        try:
            await v2.edit_response(interaction, embed=view.make_embed(), view=view)
        except (discord.NotFound, discord.HTTPException):
            return


class VeyrStateSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Выбери состояние сервера",
            min_values=1,
            max_values=1,
            row=1,
            options=[discord.SelectOption(label=quest_panels.STATE_NAMES[value], value=value)
                     for value in core_veyr_state.VEYR_STATES],
        )

    async def callback(self, interaction: discord.Interaction):
        view: VeyrControlView = self.view
        if interaction.user.id != view.owner_id:
            return await view.reject(interaction)
        if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
            return await view.reject(interaction, "Менять состояние может участник с правом «Управлять сервером».")
        state = self.values[0]
        if state not in core_veyr_state.VEYR_STATES:
            return await view.reject(interaction)
        await core_signal.set_server_state(view.guild_id, state, f"operator:{interaction.user.id}")
        try:
            view.show_system_layer()
            await v2.edit_response(interaction, embed=view.make_embed(), view=view)
        except (discord.NotFound, discord.HTTPException):
            return


class VeyrStateLayerButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Состояние сервера", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: VeyrControlView = self.view
        if interaction.user.id != view.owner_id:
            return await view.reject(interaction)
        if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
            return await view.reject(interaction, "Менять состояние может участник с правом «Управлять сервером».")
        view.show_state_layer()
        try:
            await v2.edit_response(interaction, embed=view.make_embed(), view=view)
        except (discord.NotFound, discord.HTTPException):
            return


class VeyrReturnButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Назад в центр", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: VeyrControlView = self.view
        if interaction.user.id != view.owner_id:
            return await view.reject(interaction)
        if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
            return await view.reject(interaction, "Менять состояние может участник с правом «Управлять сервером».")
        view.show_system_layer()
        try:
            await v2.edit_response(interaction, embed=view.make_embed(), view=view)
        except (discord.NotFound, discord.HTTPException):
            return


def _veyr_state_layer(guild_id: int) -> discord.Embed:
    embed = discord.Embed(color=core_config.VIVIAN_ACCENT)
    utils_helpers.vivian_brand(embed, "Vivian // Управление сервером")
    embed.title = "Состояние сообщества"
    state = core_veyr_state.SERVER_STATE[guild_id]
    embed.description = (f"Сейчас · **{quest_panels.STATE_NAMES[state]}**\n\n"
                         "Выбери новое состояние. Оно влияет на условия совместных событий.")
    return embed


class VeyrControlView(v2.ControllerView):
    def __init__(self, guild_id: int, owner_id: int, can_manage_guild: bool):
        super().__init__(timeout=3600)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.can_manage_guild = can_manage_guild
        self.visual_enabled = False
        self.panel_message = None
        self.show_system_layer()

    def make_embed(self, notice: str = "") -> discord.Embed:
        if self.current_section == "STATE TRACE":
            embed = _veyr_state_layer(self.guild_id)
        else:
            embed = commands_veyr_panels._veyr_panel(self.guild_id, self.current_section, notice,
                                                     owner_id=self.owner_id)
        if self.current_section == "RITUALS":
            for item in self.children:
                if isinstance(item, core_rituals.RitualActionSelect):
                    item.disabled = not quest_panels.ritual_snapshot(
                        self.guild_id, item.ritual, self.owner_id)["available"]
        if self.visual_enabled:
            visuals_panels._apply_panel_visual(embed, "system")
        return embed

    def show_system_layer(self):
        self.show_section("SYSTEM")

    def show_section(self, section: str):
        self.current_section = section
        self.clear_items()
        self.add_item(VeyrSectionSelect(section))
        self.add_item(VeyrRefreshButton())
        self.add_item(VeyrQuestsButton())
        if section == "RITUALS":
            for row, (ritual, rules) in enumerate(core_rituals.RITUAL_RULES.items(), start=2):
                select = core_rituals.RitualActionSelect(ritual, rules["actions"], row=row)
                select.placeholder = f"{quest_panels.RITUAL_NAMES[ritual]} — твоё действие"
                select.options = [discord.SelectOption(label=quest_panels.ACTION_NAMES[action], value=action)
                                  for action in rules["actions"]]
                select.disabled = not quest_panels.ritual_snapshot(self.guild_id, ritual, self.owner_id)["available"]
                self.add_item(select)
        if self.can_manage_guild:
            self.add_item(VeyrStateLayerButton())

    def show_state_layer(self):
        self.current_section = "STATE TRACE"
        self.clear_items()
        self.add_item(VeyrStateSelect())
        self.add_item(VeyrReturnButton())

    async def interaction_check(self, interaction):
        if interaction.guild_id == self.guild_id and interaction.user.id == self.owner_id:
            return True
        await self.reject(interaction)
        return False

    async def reject(self, interaction: discord.Interaction,
                     message: str = "Это панель другого участника. Открой свою командой `/veyr`."):
        try:
            await utils_helpers.vivian_reply(interaction, message, error=True, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            return

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.panel_message is not None:
            try:
                await v2.edit_message(self.panel_message, view=self)
            except (discord.NotFound, discord.HTTPException):
                # Discord can expire an ephemeral message token before this view.
                pass


class VeyrRefreshButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Обновить", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: VeyrControlView = self.view
        if interaction.user.id != view.owner_id:
            return await view.reject(interaction)
        try:
            if view.current_section != "STATE TRACE":
                view.show_section(view.current_section)
            await v2.edit_response(interaction, embed=view.make_embed(), view=view)
        except (discord.NotFound, discord.HTTPException):
            return


class VeyrQuestsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Мои задания", style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction):
        view = self.view
        if not await view.interaction_check(interaction):
            return
        from commands.quests import QuestBoard
        board = QuestBoard(view.guild_id, interaction.user)
        result = await v2.edit_response(interaction, view=board, attachments=[])
        board.panel_message = getattr(result, "resource", result)
        if getattr(board.panel_message, "id", None) is None:
            board.panel_message = view.panel_message
        view.stop()
