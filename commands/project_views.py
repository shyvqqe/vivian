"""Public roadmap navigation and guarded project management forms."""
from __future__ import annotations

import asyncio
import discord

from core import community_store as store, config
from utils import components_v2 as v2, helpers


STATUSES = {"planned": "В планах", "in_progress": "В работе", "done": "Готово"}
ROADMAP_TITLE = "План Vivian"
ROADMAP_MARKER = "VIVIAN // ROADMAP"
PAGE_SIZE = 6


def safe(text, limit=180):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(text or "")))[:limit]


def is_staff(member):
    return bool(getattr(getattr(member, "guild_permissions", None), "manage_guild", False)
                or any(role.id == config.STAFF_ROLE_ID for role in getattr(member, "roles", ())))


async def notice(interaction, text):
    if not interaction.response.is_done():
        result = await v2.send_response(interaction, text, ephemeral=True)
    elif interaction.response.type is discord.InteractionResponseType.deferred_channel_message:
        result = await v2.edit_original(interaction, content=text, view=None, attachments=[])
    else:
        result = await v2.followup(interaction, text, ephemeral=True)
    interaction.extras["vivian_response_completed"] = True
    return result


async def staff_guard(interaction, *, fresh=False):
    guild = interaction.guild
    member = guild.get_member(interaction.user.id) if guild is not None else None
    if fresh and guild is not None:
        try:
            async with asyncio.timeout(5):
                member = await guild.fetch_member(interaction.user.id)
        except (discord.DiscordException, TimeoutError):
            await notice(interaction, "Не удалось проверить текущие права. Действие не выполнено.")
            return False
    elif member is None:
        member = interaction.user
    if guild is not None and is_staff(member):
        return True
    await notice(interaction, "Это действие доступно администрации: «Управлять сервером» или роль staff.")
    return False


async def require_storage():
    if not store.is_ready() and not await store.initialize():
        raise RuntimeError("Project storage is unavailable")


class RoadmapAddModal(discord.ui.Modal, title="Vivian // Новый пункт плана"):
    item_title = discord.ui.TextInput(label="Название", max_length=100)
    description = discord.ui.TextInput(label="Что хотим сделать?", style=discord.TextStyle.paragraph,
                                        required=False, max_length=1000)
    status = discord.ui.TextInput(label="Статус: planned / in_progress / done", default="planned", max_length=20)

    def __init__(self, status="planned"):
        super().__init__(timeout=300)
        self.status.default = status

    async def on_submit(self, interaction):
        if not await staff_guard(interaction):
            return
        status = str(self.status.value).strip().lower()
        title = str(self.item_title.value).strip()
        if status not in STATUSES or not title:
            return await notice(interaction, "Укажи название и статус planned, in_progress или done.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await staff_guard(interaction):
            return
        try:
            await require_storage()
            if not await staff_guard(interaction, fresh=True):
                return
            item = await store.create_item(interaction.guild.id, "roadmap", interaction.user.id,
                                           title, str(self.description.value).strip(), status=status,
                                           seed_key=f"roadmap-add:{interaction.id}")
        except Exception as error:
            print(f"[Project] Roadmap save unavailable; error={type(error).__name__}", flush=True)
            return await notice(interaction, "Не удалось подтвердить сохранение пункта. База сейчас недоступна.")
        from commands import project
        try:
            panel = await project.refresh_roadmap(interaction.guild.id)
            suffix = f" [Открыть план]({panel.jump_url})." if panel is not None else ""
        except Exception as error:
            print(f"[Project] Saved roadmap needs refresh; error={type(error).__name__}", flush=True)
            suffix = " Панель обновится после следующего /roadmap."
        await notice(interaction, f"Пункт **#{item['id']}** сохранён: {safe(title)}.{suffix}")


class RoadmapView(v2.ControllerView):
    def __init__(self, guild_id, records, total, page=0):
        super().__init__(timeout=None)
        self.guild_id, self.records, self.total, self.page = guild_id, records, total, page
        self.panel_message = None
        self.pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.previous.disabled = page == 0
        self.next.disabled = page >= self.pages - 1

    def make_embed(self):
        embed = helpers.vivian_embed(title=ROADMAP_TITLE, description=(
            "Что развивается в Vivian и что уже готово.\n"
            "Статусы обновляет команда проекта; план может меняться."
        ))
        for status, label in STATUSES.items():
            entries = []
            for item in self.records:
                if item.get("status") == status:
                    line = f"**#{item['id']} · {safe(item['title'], 140)}**"
                    if item.get("body"):
                        line += f"\n{safe(item['body'], 140)}"
                    entries.append(line)
            embed.add_field(name=label, value="\n\n".join(entries) or "На этой странице пунктов нет.", inline=False)
        embed.set_footer(text=f"{ROADMAP_MARKER} · {self.page + 1}/{self.pages} · {self.total} пунктов")
        return embed

    async def interaction_check(self, interaction):
        if (interaction.guild is not None and interaction.guild.id == self.guild_id
                and interaction.channel_id == config.GITHUB_UPDATES_CHANNEL_ID):
            return True
        await notice(interaction, "Открой актуальную панель через /roadmap на своём сервере.")
        return False

    async def change_page(self, interaction, delta=0):
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer()
        from commands import project
        try:
            await project.refresh_roadmap(self.guild_id, page=max(0, self.page + delta))
        except Exception as error:
            print(f"[Project] Roadmap refresh unavailable; error={type(error).__name__}", flush=True)
            await notice(interaction, "Не удалось обновить план. Попробуй немного позже.")

    @discord.ui.button(label="←", custom_id="vivian_roadmap_previous", row=0)
    async def previous(self, interaction, button):
        await self.change_page(interaction, -1)

    @discord.ui.button(label="Обновить", custom_id="vivian_roadmap_refresh", row=0)
    async def refresh(self, interaction, button):
        await self.change_page(interaction)

    @discord.ui.button(label="→", custom_id="vivian_roadmap_next", row=0)
    async def next(self, interaction, button):
        await self.change_page(interaction, 1)

    @discord.ui.button(label="Добавить пункт", custom_id="vivian_roadmap_add", style=discord.ButtonStyle.primary, row=0)
    async def add(self, interaction, button):
        if await self.interaction_check(interaction) and await staff_guard(interaction):
            await interaction.response.send_modal(RoadmapAddModal())


class ShowcasesView(v2.ControllerView):
    def __init__(self, guild_id, owner_id, records, total, page=0):
        super().__init__(timeout=600)
        self.guild_id, self.owner_id = guild_id, owner_id
        self.records, self.total, self.page = records, total, page
        self.panel_message = None
        self.pages = max(1, (total + 4) // 5)
        self.previous.disabled = page == 0
        self.next.disabled = page >= self.pages - 1

    def make_embed(self):
        lines = []
        for item in self.records:
            if item.get("status") == "published" and item.get("channel_id") and item.get("message_id"):
                url = f"https://discord.com/channels/{self.guild_id}/{int(item['channel_id'])}/{int(item['message_id'])}"
                lines.append(f"**#{item['id']} · {safe(item['title'])}**\n[Посмотреть до и после]({url})")
        embed = helpers.vivian_embed("\n\n".join(lines) or "На этой странице опубликованных карточек пока нет.",
                                  title="Vivian // До и после")
        embed.set_footer(text=f"Только настоящие изображения команды · {self.page + 1}/{self.pages}")
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild is not None and interaction.guild.id == self.guild_id and interaction.user.id == self.owner_id:
            return True
        await notice(interaction, "Открой свой список через /showcases.")
        return False

    async def change_page(self, interaction, delta):
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer()
        try:
            await require_storage()
            page = max(0, min(self.pages - 1, self.page + delta))
            records, total = await store.list_items(self.guild_id, "showcase", limit=5, offset=page * 5)
            view = ShowcasesView(self.guild_id, self.owner_id, records, total, page)
            await v2.edit_original(interaction, embed=view.make_embed(), view=view)
        except Exception as error:
            print(f"[Project] Showcases unavailable; error={type(error).__name__}", flush=True)
            await notice(interaction, "Список сейчас недоступен. Сохранённые карточки не изменены.")

    @discord.ui.button(label="←")
    async def previous(self, interaction, button):
        await self.change_page(interaction, -1)

    @discord.ui.button(label="→")
    async def next(self, interaction, button):
        await self.change_page(interaction, 1)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.panel_message is not None:
            try:
                await v2.edit_message(self.panel_message, view=self)
            except discord.DiscordException:
                pass
