"""Image profile and accessible statistics, all inside native Components V2."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from io import BytesIO
import time

import discord
from discord import app_commands

from commands import profile_data
from core import config
from utils import command_responses as responses, components_v2 as v2, helpers


def safe(value, limit=180):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value or "")))[:limit]


def number(value):
    return f"{int(value):,}".replace(",", " ")


@dataclass
class Profile:
    guild_id: int
    owner_id: int
    member: discord.Member
    data: dict
    png: bytes | None
    image_url: str | None = None
    last_refresh: float = 0
    message: object = None
    current_view: object = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def filename(self):
        return f"vivian-profile-{self.member.id}.png"

    @property
    def accent(self):
        value = self.data.get("theme", {}).get("accent_hex")
        return discord.Colour(value) if isinstance(value, int) and 0 <= value <= 0xFFFFFF else config.VIVIAN_ACCENT


class ProfileButton(discord.ui.Button):
    def __init__(self, label, callback, *, selected=False, disabled=False):
        super().__init__(label=label, disabled=disabled,
                         style=discord.ButtonStyle.primary if selected else discord.ButtonStyle.secondary)
        self.action = callback

    async def callback(self, interaction):
        await self.action(interaction)


class ProfileView(discord.ui.LayoutView):
    def __init__(self, profile, tab="card", page=0, *, expired=False):
        super().__init__(timeout=600)
        self.profile, self.tab, self.page = profile, tab, page
        data, member = profile.data, profile.member
        box = discord.ui.Container(accent_colour=profile.accent)
        box.add_item(discord.ui.Section(
            discord.ui.TextDisplay(f"### Vivian // Профиль участника\n**{safe(member.display_name)}** · {member.mention}"),
            accessory=discord.ui.Thumbnail(member.display_avatar.url)))
        box.add_item(discord.ui.Separator())
        # Keep the same identity card and attachment on every tab.
        if profile.image_url:
            box.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(profile.image_url,
                         description=f"Профиль {member.display_name}: уровень {data['level']}, {data['messages']} сообщений")))
        if tab == "card":
            if not profile.image_url:
                box.add_item(discord.ui.TextDisplay(self.details()))
            palette_source = "Цвета подобраны по аватарке" if data.get("theme", {}).get("source") == "avatar" else "Базовая палитра профиля"
            box.add_item(discord.ui.TextDisplay(f"-# {palette_source}"))
        elif tab == "stats":
            box.add_item(discord.ui.TextDisplay(self.details()))
        else:
            items = data["achievements"]
            done = sum(item["done"] for item in items)
            lines = [f"### Достижения · {done}/{len(items)}"]
            for item in items[page * 4:page * 4 + 4]:
                value = number(item["value"]) if item["value"] is not None else "неизвестно"
                lines += [f"{'✓' if item['done'] else '◇'} **{safe(item['title'])}**",
                          f"{safe(item['description'])} · {value}/{number(item['target'])}", ""]
            box.add_item(discord.ui.TextDisplay("\n".join(lines)))
            box.add_item(discord.ui.ActionRow(
                ProfileButton("←", self.previous, disabled=page == 0),
                ProfileButton(f"{page + 1}/2", self.noop, disabled=True),
                ProfileButton("→", self.next, disabled=page >= 1)))
        box.add_item(discord.ui.TextDisplay(
            f"-# XP общий для Vivian · Сообщения и место — на этом сервере · Обновлено <t:{data['updated_at']}:t>"))
        if not data.get("storage_ready"):
            box.add_item(discord.ui.TextDisplay("-# Хранилище статистики сейчас недоступно: показаны доступные данные этой сессии."))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.ActionRow(
            ProfileButton("Карточка", self.card_tab, selected=tab == "card"),
            ProfileButton("Статистика", self.stats_tab, selected=tab == "stats"),
            ProfileButton("Достижения", self.badges_tab, selected=tab == "badges")))
        box.add_item(discord.ui.ActionRow(
            ProfileButton("Обновить", self.refresh),
            ProfileButton("Скачать PNG", self.download, disabled=profile.png is None),
            ProfileButton("Мои задания", self.quests)))
        footer = "Панель закрыта · Открой /card для свежих данных" if expired else "Твоя история в сообществе · VIVIAN"
        box.add_item(discord.ui.TextDisplay(f"-# {footer}"))
        self.add_item(box)
        if expired:
            for item in self.walk_children():
                if isinstance(item, discord.ui.Button):
                    item.disabled = True

    def details(self):
        data = self.profile.data
        rank = f"#{data['rank']}" if data["rank"] else "появится после первого сообщения"
        days = number(data["joined_days"]) if data["joined_days"] is not None else "нет данных"
        texts = max(0, int(data["stats"].get("word_messages", 0)))
        average = round(data["stats"].get("characters", 0) / texts) if texts else 0
        return (f"### Статистика · уровень {data['level']}\n**Прогресс уровня · {number(data['xp_into'])}/{number(data['xp_required'])} XP**\n"
                f"Всего опыта: **{number(data['total_xp'])}** · XP общий для Vivian\n\n"
                f"**Сообщения на сервере** · {number(data['messages'])}\n"
                f"**Сегодня, МСК** · {number(data['today_messages'])}\n"
                f"**Место по сообщениям** · {rank}\n"
                f"**Средняя длина текста** · {average} символов\n"
                f"**Дней с момента вступления** · {days}")

    async def interaction_check(self, interaction):
        profile = self.profile
        if interaction.guild_id == profile.guild_id and interaction.user.id == profile.owner_id:
            return True
        await helpers.vivian_reply(interaction, "Открой свою карточку командой `/card`.", ephemeral=True)
        return False

    async def change(self, interaction, tab, page=0):
        await interaction.response.defer()
        async with self.profile.lock:
            view = ProfileView(self.profile, tab, page)
            message = await v2.edit_original(interaction, view=view)
            self.profile.message, self.profile.current_view = message, view
            self.stop()

    async def card_tab(self, interaction):
        await self.change(interaction, "card")

    async def stats_tab(self, interaction):
        await self.change(interaction, "stats")

    async def badges_tab(self, interaction):
        await self.change(interaction, "badges")

    async def previous(self, interaction):
        await self.change(interaction, "badges", max(0, self.page - 1))

    async def next(self, interaction):
        await self.change(interaction, "badges", min(1, self.page + 1))

    async def noop(self, interaction):
        await interaction.response.defer()

    async def refresh(self, interaction):
        await interaction.response.defer()
        async with self.profile.lock:
            if time.monotonic() - self.profile.last_refresh < 8:
                return await helpers.vivian_reply(interaction, "Карточка только что обновилась. Подожди несколько секунд.", ephemeral=True)
            member = interaction.guild.get_member(self.profile.member.id)
            if member is None:
                return await helpers.vivian_reply(interaction, "Этот участник уже покинул сервер.", ephemeral=True)
            self.profile.last_refresh = time.monotonic()
            data, png = await profile_data.build(member, with_image=_can_upload(interaction))
            profile = self.profile
            previous = profile.data, profile.png, profile.member, profile.image_url
            profile.data, profile.png, profile.member = data, png, member
            try:
                message = await _publish(interaction, profile, self.tab, self.page)
                if message is None:
                    profile.data, profile.png, profile.member, profile.image_url = previous
                else:
                    self.stop()
            except BaseException:
                profile.data, profile.png, profile.member, profile.image_url = previous
                raise

    async def download(self, interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        profile = self.profile
        if profile.png is None:
            return await helpers.vivian_reply(interaction, "Изображение пока недоступно.", ephemeral=True)
        layout = discord.ui.LayoutView(timeout=None)
        layout.add_item(discord.ui.Container(discord.ui.TextDisplay("### Твоя карточка Vivian"),
                         discord.ui.File(f"attachment://{profile.filename}"), accent_colour=profile.accent))
        file = discord.File(BytesIO(profile.png), filename=profile.filename)
        try:
            await v2.edit_original(interaction, view=layout, attachments=[file])
        finally:
            file.close()

    async def quests(self, interaction):
        from commands.quests import open_quests
        await interaction.response.defer(thinking=True, ephemeral=True)
        await open_quests(interaction)

    async def on_error(self, interaction, error, item):
        print(f"[Profile UI] Action unavailable; error={type(error).__name__}", flush=True)
        try:
            await helpers.vivian_reply(interaction, "Не получилось обновить карточку. Открой `/card` ещё раз.", ephemeral=True)
        except discord.DiscordException:
            pass

    async def on_timeout(self):
        async with self.profile.lock:
            if self.profile.message is None or self.profile.current_view is not self:
                return
            expired = ProfileView(self.profile, self.tab, self.page, expired=True)
            try:
                await v2.edit_message(self.profile.message, view=expired)
                self.profile.current_view = expired
            except discord.DiscordException:
                pass
            finally:
                expired.stop()


def _can_upload(interaction):
    me, channel = interaction.guild.me if interaction.guild else None, interaction.channel
    return bool(me and channel and channel.permissions_for(me).attach_files)


async def _publish(interaction, profile, tab="card", page=0):
    file = discord.File(BytesIO(profile.png), filename=profile.filename) if profile.png else None
    if file:
        profile.image_url = file.uri
    else:
        profile.image_url = None
    view = ProfileView(profile, tab, page)
    try:
        message = await responses.edit_deferred(interaction, view=view, attachments=[file] if file else [])
        if message:
            profile.message = message
            profile.current_view = view
        if message and file:
            profile.image_url = next((item.url for item in message.attachments if item.filename == profile.filename), None)
        return message
    finally:
        if file:
            file.close()


async def open_card(interaction, member=None):
    if interaction.guild is None:
        return await helpers.vivian_reply(interaction, "Карточки участников доступны на сервере.", ephemeral=True, error=True)
    if not await responses.defer_command(interaction, ephemeral=True):
        return
    target = member or interaction.user
    data, png = await profile_data.build(target, with_image=_can_upload(interaction))
    profile = Profile(interaction.guild_id, interaction.user.id, target, data, png, last_refresh=time.monotonic())
    await _publish(interaction, profile)


@config.bot.tree.command(name="card", description="Карточка участника: аватарка, уровень, XP, статистика и достижения.")
@app_commands.describe(member="Чья карточка; по умолчанию твоя")
@app_commands.checks.cooldown(1, 8, key=lambda interaction: interaction.user.id)
async def card(interaction: discord.Interaction, member: discord.Member | None = None):
    await open_card(interaction, member)


@config.bot.tree.command(name="crad", description="Карточка участника — удобный алиас /card.")
@app_commands.describe(member="Чья карточка; по умолчанию твоя")
@app_commands.checks.cooldown(1, 8, key=lambda interaction: interaction.user.id)
async def crad(interaction: discord.Interaction, member: discord.Member | None = None):
    await open_card(interaction, member)
