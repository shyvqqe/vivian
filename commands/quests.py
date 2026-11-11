"""Native Components V2 board: real communication goals and collective events."""
from __future__ import annotations

import asyncio
from collections import defaultdict

import discord

from commands import quest_panels as panels
from core import config, rituals, signal, state, veyr_state
from utils import command_responses, components_v2 as v2, helpers


_RITUAL_LOCKS = defaultdict(asyncio.Lock)
SECTIONS = (("today", "Сегодня", "Три цели дня и твой прогресс"),
            ("path", "Путь общения", "Долгие цели из реальных достижений"),
            ("events", "Общие события", "Совместные действия и хроника сервера"))


class BoardButton(discord.ui.Button):
    def __init__(self, label, callback, *, primary=False):
        super().__init__(label=label, style=discord.ButtonStyle.primary if primary else discord.ButtonStyle.secondary)
        self._press = callback

    async def callback(self, interaction):
        await self._press(interaction)


class BoardSelect(discord.ui.Select):
    def __init__(self, section):
        super().__init__(placeholder="Выбери раздел заданий", min_values=1, max_values=1,
                         options=[discord.SelectOption(label=label, value=value, description=description,
                                                       default=section == value)
                                  for value, label, description in SECTIONS])

    async def callback(self, interaction):
        await self.view.show(interaction, self.values[0])


class RitualSelect(discord.ui.Select):
    def __init__(self, guild_id, owner_id, ritual):
        self.ritual = ritual
        snap = panels.ritual_snapshot(guild_id, ritual, owner_id)
        super().__init__(placeholder=f"{panels.RITUAL_NAMES[ritual]} — твоё действие",
                         min_values=1, max_values=1, disabled=not snap["available"],
                         options=[discord.SelectOption(label=panels.ACTION_NAMES[action], value=action,
                                                       description="Твоё действие уже учтено" if action in snap["mine"]
                                                       else "Добавить свой вклад в общее событие")
                                  for action in snap["actions"]])

    async def callback(self, interaction):
        view = self.view
        if not await view.interaction_check(interaction):
            return
        await interaction.response.defer()
        async with _RITUAL_LOCKS[(view.guild_id, self.ritual)]:
            action = self.values[0]
            if action not in rituals.RITUAL_RULES[self.ritual]["actions"]:
                return await helpers.vivian_reply(interaction, "Выбери действие из меню события.", ephemeral=True)
            result = await rituals.contribute_ritual(view.guild_id, self.ritual, action, interaction.user.id)
            await view.show(interaction, "events", panels.NOTICES.get(result, "Состояние события обновлено."))


class QuestBoard(discord.ui.LayoutView):
    def __init__(self, guild_id, member, *, section="today", notice="", expired=False):
        super().__init__(timeout=900)
        self.guild_id, self.member, self.owner_id = guild_id, member, member.id
        self.section, self.panel_message = section, None
        signal.update_server_signal(guild_id)
        header = discord.ui.TextDisplay(f"### Vivian // Общение и события\n## Твой маршрут на сегодня\n"
                                       f"**{panels.safe(member.display_name)}** · {member.mention}")
        header = discord.ui.Section(header, accessory=discord.ui.Thumbnail(member.display_avatar.url))
        box = discord.ui.Container(header, discord.ui.Separator(), accent_colour=config.VIVIAN_ACCENT)
        if notice:
            box.add_item(discord.ui.TextDisplay(f"**{notice}**"))
            box.add_item(discord.ui.Separator())
        if section == "path":
            box.add_item(discord.ui.TextDisplay("### Путь общения\nВехи из твоей активности на этом сервере. Срока нет."))
            for block in panels.milestone_blocks(guild_id, self.owner_id):
                box.add_item(discord.ui.TextDisplay(block))
        elif section == "events":
            self._events(box)
        else:
            self._today(box)
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.ActionRow(BoardSelect(section)))
        box.add_item(discord.ui.ActionRow(BoardButton("Обновить прогресс", self.refresh, primary=True),
                                         BoardButton("Как это работает", self.explain)))
        box.add_item(discord.ui.TextDisplay(
            "-# Панель закрыта для чужих нажатий · "
            + ("Время панели истекло — открой /quests снова." if expired else "Обновляется по кнопке · 15 минут")
            + " · Components V2"))
        self.add_item(box)
        if expired:
            for item in self.walk_children():
                if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                    item.disabled = True

    def _today(self, box):
        snap, blocks = panels.today_blocks(self.guild_id, self.owner_id)
        complete = sum(snap["messages"] >= target for _, target, _ in panels.DAILY_GOALS)
        box.add_item(discord.ui.TextDisplay(
            f"**{snap['day']:%d.%m} · {complete} из {len(blocks)} целей**\n"
            f"Твои сообщения сегодня: **{panels.number(snap['messages'])}** · "
            f"Обнуление <t:{int(snap['reset'].timestamp())}:R> · **00:00 МСК**"))
        for block in blocks:
            box.add_item(discord.ui.TextDisplay(block))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(
            f"**Твой опыт · {panels.number(snap['xp'])} XP · уровень {snap['level']}**\n"
            f"За обычное общение: **+{state.XP_PER_MESSAGE} XP** за сообщение, "
            f"не чаще раза в **{state.XP_COOLDOWN_SECONDS} с**.\n"
            "-# Цели — ориентиры прогресса без отдельного XP-бонуса. Спешить и флудить не нужно."))

    def _events(self, box):
        current = veyr_state.SERVER_SIGNAL[self.guild_id]["value"]
        status = panels.STATE_NAMES[veyr_state.SERVER_STATE[self.guild_id]]
        box.add_item(discord.ui.TextDisplay(f"**Сервер сейчас · {status} · сигнал {current:.2f} / 100**\n"
                                           "События проходят вместе. Каждый участник открывает свою `/quests`."))
        for ritual in rituals.RITUAL_RULES:
            snap = panels.ritual_snapshot(self.guild_id, ritual, self.owner_id)
            box.add_item(discord.ui.Separator())
            box.add_item(discord.ui.TextDisplay(snap["text"]))
            box.add_item(discord.ui.ActionRow(RitualSelect(self.guild_id, self.owner_id, ritual)))

    async def interaction_check(self, interaction):
        if interaction.guild_id == self.guild_id and interaction.user.id == self.owner_id:
            return True
        await helpers.vivian_reply(interaction, "Открой свою доску командой `/quests` — здесь прогресс другого участника.",
                                 ephemeral=True)
        return False

    async def show(self, interaction, section=None, notice=""):
        view = QuestBoard(self.guild_id, self.member, section=section or self.section, notice=notice)
        if interaction.response.is_done():
            message = await v2.edit_original(interaction, view=view, attachments=[])
        else:
            message = await v2.edit_response(interaction, view=view, attachments=[])
        view.panel_message = message if getattr(message, "id", None) else self.panel_message
        self.stop()

    async def refresh(self, interaction):
        await self.show(interaction)

    async def explain(self, interaction):
        await helpers.vivian_reply(
            interaction,
            "**Сегодня** — 5, 15 и 30 сообщений за текущий день по МСК во всех каналах этого сервера.\n\n"
            "**Путь общения** — долгие цели из накопленных сообщений, текстов и символов.\n\n"
            f"**Опыт** — автоматически +{state.XP_PER_MESSAGE} XP за сообщение с интервалом "
            f"{state.XP_COOLDOWN_SECONDS} с. XP общий для бота; цели и активность привязаны к серверу.\n\n"
            "**Общие события** — совместные действия. Результат завершения — запись в хронике; XP за событие не выдаётся. "
            "Состояние события хранится до перезапуска бота.\n\n"
            "Кнопка обновления показывает текущие данные. Пустые сообщения и спам не помогут разговору.",
            title="Vivian // Как устроены цели", ephemeral=True,
        )

    async def on_timeout(self):
        if self.panel_message is not None:
            try:
                view = QuestBoard(self.guild_id, self.member, section=self.section, expired=True)
                await v2.edit_message(self.panel_message, view=view, attachments=[])
                view.stop()
            except discord.DiscordException:
                pass

    async def on_error(self, interaction, error, item):
        print(f"[Quests UI] Action unavailable; error={type(error).__name__}", flush=True)
        try:
            await helpers.vivian_reply(interaction, "Не удалось обновить доску. Открой `/quests` ещё раз.",
                                     error=True, ephemeral=True)
        except discord.DiscordException:
            pass


async def open_quests(interaction):
    if interaction.guild is None:
        return await helpers.vivian_reply(interaction, "Задания за общение доступны на сервере.",
                                        error=True, ephemeral=True)
    if not await command_responses.defer_command(interaction):
        return
    view = QuestBoard(interaction.guild.id, interaction.user)
    message = await command_responses.edit_deferred(interaction, view=view, attachments=[])
    if message is not None:
        # Keep the webhook-backed InteractionMessage: profile buttons can open
        # this board privately, and a bot-REST partial cannot edit that message.
        view.panel_message = message


@config.bot.tree.command(name="quests", description="Цели за общение: прогресс дня, путь участника и общие события.")
async def quests(interaction: discord.Interaction):
    await open_quests(interaction)


@config.bot.tree.command(name="tasks", description="Открыть задания за общение и совместные события Vivian.")
async def tasks(interaction: discord.Interaction):
    await open_quests(interaction)
