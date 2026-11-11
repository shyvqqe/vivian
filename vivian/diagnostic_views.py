"""Private diagnostic panels; feedback changes a case, never calls AI on its own."""
from __future__ import annotations

import time

import discord

from core import config
from vivian import diagnostic_store as store
from utils import helpers, components_v2 as v2


def _allowed(interaction, key):
    guild = interaction.guild
    if guild is None or (guild.id, interaction.channel_id, interaction.user.id) != key:
        return False
    channel = interaction.channel
    if channel is None or guild.me is None:
        return False
    member_permissions = channel.permissions_for(interaction.user)
    bot_permissions = channel.permissions_for(guild.me)
    return bool(member_permissions.view_channel and member_permissions.read_message_history
                and bot_permissions.view_channel and bot_permissions.read_message_history)


def _quote_lines(items):
    return "\n".join("• " + discord.utils.escape_markdown(item["text"]) for item in items)[:1000]


def case_embed(case):
    embed = helpers.vivian_embed(title="Ход диагностики")
    helpers.vivian_brand(embed, "Vivian / DIAGNOSTICS")
    status = {"OPEN": "РАЗБИРАЕМСЯ", "FAILED": "ШАГ НЕ ПОМОГ", "SOLVED": "РЕШЕНО"}
    embed.description = f"`{status.get(case['status'], 'РАЗБИРАЕМСЯ')}`\n" + discord.utils.escape_markdown(case["issue"])
    embed.add_field(name="ИЗВЕСТНО ИЗ ТВОИХ СЛОВ", value=_quote_lines(case["facts"][-4:])
                    or "Характеристики ещё не уточнены.", inline=False)
    embed.add_field(name="УЖЕ ПРОВЕРЕНО", value=_quote_lines(case["tried"][-3:])
                    or "Конкретные проверки ещё не названы.", inline=False)
    next_step = case.get("next") or "Нужны детали проблемы. Ответь Vivian в переписке."
    if case["status"] == "SOLVED":
        next_step = "Проблема отмечена как решённая."
    elif case["status"] == "FAILED":
        next_step = "Опиши результат в ответ на сообщение Vivian. Она учтёт, что этот шаг не помог."
    embed.add_field(name="СЛЕДУЮЩИЙ ШАГ", value=next_step[:1000], inline=False)
    embed.set_footer(text="Только тебе · Цитаты, проверки и следующий шаг // VIVIAN")
    return embed


def _case_payload(interaction, case):
    embed = case_embed(case)
    if getattr(interaction.channel.permissions_for(interaction.guild.me), "embed_links", False):
        return {"content": None, "embed": embed}
    text = "\n\n".join([embed.title, embed.description,
                           *(f"{field.name}\n{field.value}" for field in embed.fields)])
    return {"content": text[:1900], "embed": None}


class DiagnosticView(v2.ControllerView):
    def __init__(self, key, origin, step, status="OPEN"):
        super().__init__(timeout=900)
        self.key, self.origin, self.step = key, origin, step
        self.panel_message = None
        self.helped.disabled = self.not_helped.disabled = status != "OPEN"

    async def interaction_check(self, interaction):
        if _allowed(interaction, self.key):
            return True
        try:
            await v2.send_response(interaction, "Эта карточка доступна только её собеседнику.", ephemeral=True,
                                   allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        return False

    async def _feedback(self, interaction, solved):
        if not _allowed(interaction, self.key):
            return
        try:
            await interaction.response.defer()
            async with store.case_lock(self.key):
                case = await store.load_case(self.key)
                if not _allowed(interaction, self.key):
                    return
                if (case is None or case["origin"] != self.origin
                        or case.get("step") != self.step or case["status"] != "OPEN"):
                    await v2.followup(interaction, "Этот шаг уже изменился. Открой свежую карточку.", ephemeral=True)
                    return
                case["revision"] = max(case["revision"], interaction.id)
                case["updated"] = time.time()
                case["status"] = "SOLVED" if solved else "FAILED"
                if not solved:
                    case["tried"].append({"text": "Последний шаг Vivian не помог (твоя отметка).",
                                          "source": self.step})
                    case["tried"] = case["tried"][-5:]
                durable = await store.save_case(self.key, case)
            self.helped.disabled = self.not_helped.disabled = True
            await v2.edit_original(interaction, **_case_payload(interaction, case), view=self,
                                   allowed_mentions=discord.AllowedMentions.none())
            if not durable:
                await v2.followup(interaction, "Отметка учтена в этой сессии; база сейчас недоступна.", ephemeral=True)
        except discord.HTTPException:
            return

    @discord.ui.button(label="Помогло", style=discord.ButtonStyle.primary)
    async def helped(self, interaction, button):
        await self._feedback(interaction, True)

    @discord.ui.button(label="Не помогло", style=discord.ButtonStyle.secondary)
    async def not_helped(self, interaction, button):
        await self._feedback(interaction, False)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.panel_message:
            try:
                await v2.edit_message(self.panel_message, view=self)
            except discord.HTTPException:
                pass


class DiagnosticEntryView(discord.ui.View):
    def __init__(self, message, case):
        super().__init__(timeout=3600)
        self.key, self.origin = store.case_key(message), case["origin"]
        self.panel_message = None

    async def interaction_check(self, interaction):
        if _allowed(interaction, self.key):
            return True
        try:
            await helpers.vivian_reply(interaction, "Ход этого разбора доступен только его собеседнику.", ephemeral=True)
        except discord.HTTPException:
            pass
        return False

    @discord.ui.button(label="Ход диагностики", style=discord.ButtonStyle.secondary)
    async def open_case(self, interaction, button):
        if not _allowed(interaction, self.key):
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with store.case_lock(self.key):
                case = await store.load_case(self.key)
            if not _allowed(interaction, self.key):
                return
            if case is None or case["origin"] != self.origin:
                await v2.edit_original(interaction, content="Этот разбор завершён или заменён. Ответь Vivian для нового.")
                return
            view = DiagnosticView(self.key, self.origin, case.get("step"), case["status"])
            view.panel_message = await v2.edit_original(interaction, **_case_payload(interaction, case), view=view,
                                                       allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.panel_message:
            try:
                await self.panel_message.edit(view=self)
            except discord.HTTPException:
                pass
