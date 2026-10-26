"""Private fact navigation, correction modal and explicit deletion confirmation."""

import discord

from vivian import memory_card, memory_controls_store, memory_state
from utils import components_v2 as v2


class FactSelect(discord.ui.Select):
    def __init__(self, view):
        shown = view.facts[view.page * 5:(view.page + 1) * 5]
        options = [discord.SelectOption(label=entry["text"][:80], value=key,
                                       default=key == view.selected_key) for key, entry in shown]
        super().__init__(placeholder="Выбери сохранённый факт", options=options or [
            discord.SelectOption(label="Личных фактов пока нет", value="none")],
            disabled=not bool(options), min_values=1, max_values=1, row=0)

    async def callback(self, interaction):
        if not await self.view.authorize(interaction):
            return
        self.view.selected_key = self.values[0]
        await self.view.update_panel(interaction)


class MemoryCardView(v2.ControllerView):
    def __init__(self, owner_id, guild_id, channel_id):
        super().__init__(timeout=600)
        self.owner_id, self.guild_id, self.channel_id = owner_id, guild_id, channel_id
        self.page, self.facts, self.selected_key, self.panel_message = 0, [], None, None

    async def authorize(self, interaction):
        return await memory_card.authorize_private(interaction, self.owner_id, self.guild_id, self.channel_id)

    async def interaction_check(self, interaction):
        return await self.authorize(interaction)

    async def refresh_facts(self):
        self.facts = await memory_card.fact_snapshot((self.guild_id, self.channel_id), self.owner_id)
        self.page = min(self.page, max(0, (len(self.facts) - 1) // 5))
        if self.selected_key not in {key for key, _ in self.facts}:
            self.selected_key = None
        for child in list(self.children):
            if isinstance(child, FactSelect):
                self.remove_item(child)
        self.add_item(FactSelect(self))
        self.previous.disabled = self.page == 0
        self.next.disabled = (self.page + 1) * 5 >= len(self.facts)
        self.correct.disabled = self.selected_key is None
        self.forget.disabled = self.selected_key is None
        self.forget_all.disabled = not bool(self.facts)

    async def update_panel(self, interaction, notice=None):
        if not await self.authorize(interaction):
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
            await self.refresh_facts()
            if not await self.authorize(interaction):
                return
            content, embed = memory_card.fact_card(self, interaction, notice)
            self.panel_message = await v2.edit_original(interaction,
                content=content, embed=embed, view=self,
                allowed_mentions=discord.AllowedMentions.none())
        except (discord.NotFound, discord.HTTPException):
            return

    async def apply_change(self, interaction, *, key=None, replacement=None, clear=False):
        if not await self.authorize(interaction):
            return False
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
            previous_panel = self.panel_message
            changed = await memory_controls_store.change_personal_facts(
                (self.guild_id, self.channel_id), self.owner_id, interaction.id,
                key=key, replacement=replacement, clear=clear,
            )
            notice = "Сведения обновила." if replacement else "Выбранные факты забыла."
            if not changed:
                notice = "Этот факт уже изменён или удалён."
            await self.update_panel(interaction, notice)
            if previous_panel is not None and previous_panel is not self.panel_message:
                # A clear confirmation is a separate private message; remove stale facts there too.
                content, embed = memory_card.fact_card(self, interaction, notice)
                try:
                    await v2.edit_message(previous_panel, content=content, embed=embed, view=self,
                                          allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
            return True
        except memory_controls_store.MemoryUnavailable as error:
            await memory_card.private_notice(interaction, str(error))
        except (discord.NotFound, discord.HTTPException):
            pass
        return False

    @discord.ui.button(label="←", row=1)
    async def previous(self, interaction, button):
        if await self.authorize(interaction):
            self.page = max(0, self.page - 1)
            await self.update_panel(interaction)

    @discord.ui.button(label="Обновить", row=1)
    async def refresh(self, interaction, button):
        await self.update_panel(interaction)

    @discord.ui.button(label="→", row=1)
    async def next(self, interaction, button):
        if await self.authorize(interaction):
            self.page += 1
            await self.update_panel(interaction)

    @discord.ui.button(label="Уточнить", style=discord.ButtonStyle.primary, row=2)
    async def correct(self, interaction, button):
        if not await self.authorize(interaction):
            return
        entry = dict(self.facts).get(self.selected_key)
        if entry is None:
            await self.update_panel(interaction)
            return
        try:
            await interaction.response.send_modal(FactCorrectionModal(self, self.selected_key, entry["text"]))
        except (discord.NotFound, discord.HTTPException):
            return

    @discord.ui.button(label="Забыть факт", row=2)
    async def forget(self, interaction, button):
        if not await self.authorize(interaction):
            return
        if self.selected_key is not None:
            await self.apply_change(interaction, key=self.selected_key)

    @discord.ui.button(label="Забыть все факты", style=discord.ButtonStyle.danger, row=2)
    async def forget_all(self, interaction, button):
        if not await self.authorize(interaction):
            return
        try:
            await v2.send_response(
                interaction,
                "Забыть все твои сохранённые факты и их источники из памяти этого канала?",
                ephemeral=True, view=ForgetConfirmationView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.HTTPException):
            return

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.panel_message is not None:
            try:
                await v2.edit_message(self.panel_message, view=self)
            except discord.HTTPException:
                pass


class FactCorrectionModal(discord.ui.Modal, title="Vivian // Уточнить факт"):
    def __init__(self, view, key, text):
        super().__init__(timeout=300)
        self.owner_view, self.fact_key = view, key
        self.corrected = discord.ui.TextInput(label="Как правильно?", default=text[:240],
                                              style=discord.TextStyle.paragraph, min_length=2, max_length=240)
        self.add_item(self.corrected)

    async def on_submit(self, interaction):
        if not await self.owner_view.authorize(interaction):
            return
        text = " ".join(str(self.corrected.value).split())
        if len(text) < 2 or memory_state.AI_MEMORY_SECRET_PATTERN.search(text):
            await memory_card.private_notice(interaction, "Запиши короткий факт без паролей, токенов и ключей.")
            return
        await self.owner_view.apply_change(interaction, key=self.fact_key, replacement=text)


class ForgetConfirmationView(v2.ControllerView):
    def __init__(self, owner_view):
        super().__init__(timeout=120)
        self.owner_view = owner_view

    async def interaction_check(self, interaction):
        return await self.owner_view.authorize(interaction)

    @discord.ui.button(label="Подтвердить", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if not await self.owner_view.authorize(interaction):
            return
        if await self.owner_view.apply_change(interaction, clear=True):
            self.stop()

    @discord.ui.button(label="Отмена")
    async def cancel(self, interaction, button):
        if not await self.owner_view.authorize(interaction):
            return
        try:
            await v2.edit_response(interaction, content="Сохранённые факты оставила.", view=None)
            self.stop()
        except (discord.NotFound, discord.HTTPException):
            return
