"""Public information cards; each reply and button update uses Components V2."""
from __future__ import annotations

import math
import time

import discord

from core import config, discord_api
from commands import help_panel
from utils import command_responses as responses, components_v2 as v2, helpers
from visuals import panels


_STARTED_AT = time.monotonic()


def _uptime():
    minutes = int(max(0, time.monotonic() - _STARTED_AT)) // 60
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return f"{days} д {hours} ч {minutes} мин" if days else f"{hours} ч {minutes} мин"


def _avatar(embed):
    avatar = getattr(getattr(config.bot, "user", None), "display_avatar", None)
    if avatar is not None:
        embed.set_thumbnail(url=str(avatar.url))


class InformationView(v2.ControllerView):
    def __init__(self, interaction):
        super().__init__(timeout=600)
        self.owner_id = interaction.user.id
        self.guild_id = interaction.guild.id if interaction.guild else 0
        self.visual_enabled = False
        self.panel_message = None
        self.last_action = 0.0

    async def interaction_check(self, interaction):
        guild_id = interaction.guild.id if interaction.guild else 0
        if interaction.user.id != self.owner_id or guild_id != self.guild_id:
            await helpers.vivian_reply(interaction, "Открой свою карточку той же командой.", ephemeral=True)
            return False
        if time.monotonic() - self.last_action < 2:
            await helpers.vivian_reply(interaction, "Подожди пару секунд перед обновлением.", ephemeral=True)
            return False
        self.last_action = time.monotonic()
        return True

    async def update(self, interaction):
        await v2.edit_response(interaction, embed=self.make_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.panel_message is not None:
            try:
                await v2.edit_message(self.panel_message, view=self)
            except discord.DiscordException:
                pass


class PingView(InformationView):
    def __init__(self, interaction, acknowledgement_ms):
        super().__init__(interaction)
        self.acknowledgement_ms = acknowledgement_ms
        self.measured_at = discord.utils.utcnow()

    def make_embed(self):
        latency = getattr(config.bot, "latency", None)
        gateway = (f"{round(latency * 1000)} мс" if isinstance(latency, (int, float))
                   and math.isfinite(latency) and latency >= 0 else "Измеряю…")
        embed = helpers.vivian_embed("Связь с Discord и время подтверждения этой команды.", title="Pong · связь Vivian")
        embed.add_field(name="GATEWAY", value=f"**{gateway}**", inline=True)
        embed.add_field(name="ОТКЛИК КОМАНДЫ", value=f"**{self.acknowledgement_ms} мс**", inline=True)
        embed.add_field(name="РАБОТА ПРОЦЕССА", value=_uptime(), inline=True)
        if discord_api.is_paused():
            embed.add_field(name="REST API · ПАУЗА", value=(
                "Discord временно ограничивает обычные запросы. "
                f"Осталось около {math.ceil(discord_api.remaining() / 60)} мин."
            ), inline=False)
        else:
            embed.add_field(name="СОЕДИНЕНИЕ", value="Активно. Команды принимаются.", inline=False)
        embed.add_field(name="ОБНОВЛЕНО", value=discord.utils.format_dt(self.measured_at, "R"), inline=False)
        embed.set_footer(text="Задержка Discord · скорость ответа ИИ измеряется отдельно · VIVIAN")
        _avatar(embed)
        if self.visual_enabled:
            panels._apply_panel_visual(embed, "system")
        return embed

    @discord.ui.button(label="Обновить связь", emoji="🔄", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction, button):
        started = time.monotonic()
        await interaction.response.defer()
        self.acknowledgement_ms = round((time.monotonic() - started) * 1000)
        self.measured_at = discord.utils.utcnow()
        await responses.edit_deferred(interaction, embed=self.make_embed(), view=self)


class AboutView(InformationView):
    def __init__(self, interaction):
        super().__init__(interaction)
        self.page = "about"
        self.sync_controls()

    def sync_controls(self):
        self.overview.style = (discord.ButtonStyle.primary if self.page == "about"
                               else discord.ButtonStyle.secondary)
        self.features.style = (discord.ButtonStyle.primary if self.page == "features"
                               else discord.ButtonStyle.secondary)

    def make_embed(self):
        embed = helpers.vivian_embed(title="Vivian · рядом с твоим сервером", description=(
            "Собеседница, музыка, статистика и уютные приватки.\nСоздатель — **zev1x**."
        ))
        _avatar(embed)
        if self.page == "about":
            embed.add_field(name="СОСТОЯНИЕ", value="ONLINE" if config.bot.is_ready() else "Подключаюсь…", inline=True)
            embed.add_field(name="БЕЗ ПЕРЕЗАПУСКА", value=_uptime(), inline=True)
            count = len(help_panel.command_entries())
            embed.add_field(name="КОМАНДЫ", value=f"**{count}** · весь список в `/help`", inline=True)
            embed.add_field(name="ТВОЯ VIVIAN", value=(
                "Модель для своих разговоров выбирай в `/ai`. "
                "Память и личные настройки доступны только владельцу."
            ), inline=False)
            embed.add_field(name="НОВОСТИ ПРОЕКТА", value="`/changelog` · `/roadmap` · `/deploy`", inline=False)
        else:
            for name, value in (
                ("ОБЩЕНИЕ", "Контекст беседы, память и разбор изображений. Модель — `/ai`."),
                ("ТВОЯ КАРТОЧКА", "`/card` или `/crad` — аватарка, уровень, прогресс и статистика участника."),
                ("УЧАСТНИКИ", "Уровни и достижения — `/rank`; пульс сообщества — `/activity`."),
                ("ЗАДАНИЯ ЗА ОБЩЕНИЕ", "`/quests` или `/tasks` — цели на день и твой прогресс в чате."),
                ("МУЗЫКА", "Сейчас — `!play название`, `!stop` и `/queue`. Новый `/play` в разработке."),
                ("ПРИВАТНЫЕ КОМНАТЫ", "`/room create` — выбери участников по @ и создай свою голосовую."),
                ("ПОМОЩЬ ПРОЕКТУ", "Идеи — `/suggest`; ошибки — `/bug`; оформление до / после — `/showcases`."),
                ("МОДЕРАЦИЯ", "Блокировка, очистка и управление каналом; доступ определяют права Discord."),
            ):
                embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text="О боте / Возможности · VIVIAN // SYSTEM")
        if self.visual_enabled:
            panels._apply_panel_visual(embed, "system")
        return embed

    @discord.ui.button(label="О боте", style=discord.ButtonStyle.primary)
    async def overview(self, interaction, button):
        self.page = "about"
        self.sync_controls()
        await self.update(interaction)

    @discord.ui.button(label="Возможности", style=discord.ButtonStyle.secondary)
    async def features(self, interaction, button):
        self.page = "features"
        self.sync_controls()
        await self.update(interaction)


@config.bot.tree.command(name="help", description="Просмотреть команды Vivian и краткие описания их функций.")
async def help_command(interaction: discord.Interaction):
    if not await responses.defer_command(interaction, ephemeral=True):
        return
    view = help_panel.HelpView(interaction.user.id, interaction.guild.id if interaction.guild else 0, False)
    view.panel_message = await responses.send_panel(interaction, view=view, style="system")


@config.bot.tree.command(name="ping", description="Показать задержку соединения Vivian с Discord.")
async def ping(interaction: discord.Interaction):
    started = time.monotonic()
    if not await responses.defer_command(interaction, ephemeral=True):
        return
    view = PingView(interaction, round((time.monotonic() - started) * 1000))
    view.panel_message = await responses.send_panel(interaction, view=view, style="system")


@config.bot.tree.command(name="about", description="О Vivian, её возможностях и текущем состоянии.")
async def about(interaction: discord.Interaction):
    if not await responses.defer_command(interaction, ephemeral=True):
        return
    view = AboutView(interaction)
    view.panel_message = await responses.send_panel(interaction, view=view, style="system")
