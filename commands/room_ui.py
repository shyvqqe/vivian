"""Small native Components V2 building blocks for private rooms."""
from __future__ import annotations

import discord

from core import config
from utils import components_v2 as v2


LOBBY_MARKER = "VIVIAN // PRIVATE ROOMS"


def safe(value, limit=300):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value or "")))[:limit]


class Button(discord.ui.Button):
    def __init__(self, label, callback, *, style=discord.ButtonStyle.secondary,
                 custom_id=None, emoji=None, disabled=False):
        kwargs = {"label": label, "style": style, "disabled": disabled, "emoji": emoji}
        if custom_id is not None:
            kwargs["custom_id"] = custom_id
        super().__init__(**kwargs)
        self._press = callback

    async def callback(self, interaction):
        await self._press(interaction)


class PeopleSelect(discord.ui.UserSelect):
    def __init__(self, callback, *, placeholder, maximum=10, selected=()):
        super().__init__(placeholder=placeholder, min_values=0, max_values=maximum,
                         default_values=list(selected))
        self._select = callback

    async def callback(self, interaction):
        await self._select(interaction, list(self.values))


class Panel(discord.ui.LayoutView):
    def __init__(self, guild_id=None, owner_id=None, *, timeout=600):
        super().__init__(timeout=timeout)
        self.guild_id, self.owner_id = guild_id, owner_id

    def card(self, title, body, *rows, footer=None, color=None):
        header = discord.ui.TextDisplay(f"### Vivian // Приватные комнаты\n## {title}")
        avatar = getattr(config.bot.user, "display_avatar", None)
        if avatar is not None:
            header = discord.ui.Section(header, accessory=discord.ui.Thumbnail(avatar.url))
        box = discord.ui.Container(header, discord.ui.Separator(),
                                   discord.ui.TextDisplay(body),
                                   accent_colour=config.VIVIAN_ACCENT if color is None else color)
        if rows:
            box.add_item(discord.ui.Separator())
            for row in rows:
                box.add_item(discord.ui.ActionRow(*row))
        box.add_item(discord.ui.TextDisplay(
            f"-# {footer or 'Личная панель на 10 минут · /room manage'} · Новый интерфейс Components V2"))
        self.add_item(box)
        return self

    async def interaction_check(self, interaction):
        if interaction.guild is None or (self.guild_id is not None and interaction.guild_id != self.guild_id):
            await notice(interaction, "Открой панель приватных комнат на своём сервере.")
            return False
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await notice(interaction, "Это личная панель другого участника. Открой свою через `/room manage`.")
            return False
        expected_channel = getattr(self, "room_channel_id", None)
        if expected_channel is not None:
            interaction.extras["room_channel_id"] = expected_channel
        return True

    async def on_error(self, interaction, error, item):
        print(f"[Rooms UI] Action failed; error={type(error).__name__}", flush=True)
        try:
            await notice(interaction, problem(error))
        except discord.DiscordException:
            pass


def problem(error):
    if isinstance(error, ValueError):
        return safe(error, 700)
    if isinstance(error, discord.Forbidden):
        return ("У Vivian не хватает прав на это действие. Нужны доступ к категории и «Управлять каналами»; "
                "для изменения доступа — «Управлять ролями», для переноса людей — «Перемещать участников».")
    if isinstance(error, discord.NotFound):
        return "Комната уже удалена. Открой `/room create`, чтобы создать новую."
    return "Не удалось подтвердить действие. Попробуй `/room manage` чуть позже."


async def show(interaction, view):
    if interaction.response.is_done():
        result = await v2.edit_original(interaction, view=view, attachments=[])
    else:
        result = await v2.send_response(interaction, view=view, ephemeral=True)
    interaction.extras["vivian_response_completed"] = True
    return result


async def notice(interaction, text):
    panel = Panel(interaction.guild_id, interaction.user.id).card("Небольшое уточнение", text)
    if not interaction.response.is_done():
        return await show(interaction, panel)
    if interaction.response.type is discord.InteractionResponseType.deferred_channel_message:
        return await show(interaction, panel)
    result = await v2.followup(interaction, view=panel, ephemeral=True)
    interaction.extras["vivian_response_completed"] = True
    return result


async def acknowledge(interaction, *, update=False):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=not update)
