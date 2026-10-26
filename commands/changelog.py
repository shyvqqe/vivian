"""Public release journal and the actual deployment confirmation."""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands

from app import deployment_status
from core import community_store as store, config
from utils import command_responses as responses, components_v2 as v2, helpers


def _safe(value, limit=1500):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)[:limit]))


class ChangelogView(v2.ControllerView):
    def __init__(self, owner_id, guild_id):
        super().__init__(timeout=600)
        self.owner_id, self.guild_id = owner_id, guild_id
        self.page, self.total, self.record = 0, 0, None
        self.panel_message = None
        self.lock = asyncio.Lock()

    async def load(self):
        records, self.total = await store.list_items(self.guild_id, "release", limit=1, offset=self.page)
        if not records and self.total:
            self.page = self.total - 1
            records, self.total = await store.list_items(self.guild_id, "release", limit=1, offset=self.page)
        self.record = records[0] if records else None
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page + 1 >= self.total

    def make_embed(self):
        if self.record is None:
            return helpers.vivian_embed(
                "Журнал появится после первого обработанного обновления GitHub.", title="Журнал изменений Vivian")
        record, details = self.record, self.record.get("details") or {}
        lines = ["**Что изменилось**", *[f"• {_safe(line, 300)}" for line in record["body"].splitlines()[:7]]]
        sha = str(details.get("sha") or "")
        if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
            lines += ["", f"[Коммит {sha[:7]}](https://github.com/{config.GITHUB_UPDATES_REPOSITORY}/commit/{sha})"]
        lines += [f"Автор: {_safe(details.get('author', 'Не указан'), 80)}",
                  f"Изменено файлов: {int(details.get('file_count', 0))}", "",
                  "Запись в журнале подтверждает изменение кода. Статус запуска — в `/deploy`."]
        embed = helpers.vivian_embed("\n".join(lines), title="Журнал изменений Vivian")
        embed.set_footer(text=f"{self.page + 1} / {max(1, self.total)} · VIVIAN // CHANGELOG")
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id == self.guild_id and interaction.user.id == self.owner_id:
            return True
        await helpers.vivian_reply(interaction, "Открой свою страницу командой `/changelog`.", ephemeral=True)
        return False

    async def move(self, interaction, delta):
        await interaction.response.defer()
        async with self.lock:
            previous = self.page
            self.page = max(0, min(max(0, self.total - 1), self.page + delta))
            try:
                await self.load()
                await v2.edit_original(interaction, embed=self.make_embed(), view=self)
            except (store.CommunityUnavailable, discord.DiscordException):
                self.page = previous
                await helpers.vivian_reply(interaction, "Не получилось обновить страницу. Попробуй позже.", ephemeral=True)

    @discord.ui.button(label="← Новее", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        await self.move(interaction, -1)

    @discord.ui.button(label="Раньше →", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        await self.move(interaction, 1)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.panel_message:
            try:
                await v2.edit_message(self.panel_message, view=self)
            except discord.DiscordException:
                pass


@config.bot.tree.command(name="changelog", description="Понятный журнал изменений Vivian с листанием версий.")
@app_commands.guild_only()
async def changelog(interaction: discord.Interaction):
    if not await responses.defer_command(interaction):
        return
    view = ChangelogView(interaction.user.id, interaction.guild_id)
    try:
        await view.load()
    except store.CommunityUnavailable:
        return await helpers.vivian_reply(interaction, "Журнал сейчас недоступен. Попробуй позже.", error=True)
    view.panel_message = await responses.edit_deferred(interaction, embed=view.make_embed(), view=view)


@config.bot.tree.command(name="deploy", description="Посмотреть, подтвердила ли Vivian запуск новой версии.")
@app_commands.guild_only()
async def deploy(interaction: discord.Interaction):
    if not await responses.defer_command(interaction):
        return
    try:
        target = await deployment_status.current_target(interaction.guild_id)
    except store.CommunityUnavailable:
        return await helpers.vivian_reply(interaction, "Подтверждение версии сейчас недоступно.", error=True)
    if not target:
        return await helpers.vivian_reply(interaction, "Подтверждение версий доступно на сервере проекта Vivian.")
    await responses.edit_deferred(interaction, embed=deployment_status.build_card(target))
