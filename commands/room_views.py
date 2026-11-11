"""Private voice-room wizard and native Components V2 controls."""
from __future__ import annotations

import discord

from app import rooms, room_actions as actions
from commands import room_ui as ui
from core import config


LOBBY_MARKER = ui.LOBBY_MARKER


class CreateView(ui.Panel):
    def __init__(self, guild_id, owner_id, *, members=(), name=None, note=""):
        super().__init__(guild_id, owner_id)
        self.members, self.name = list(members), name
        names = " · ".join(f"<@{member.id}>" for member in self.members) or "Только ты — друзей можно добавить позже."
        body = (f"**Название** · {ui.safe(name, 80) if name else 'Подберу по твоему нику'}\n\n"
                f"**Твой круг · {len(self.members)}/10**\n{names}\n\n"
                "Выбери людей через **@** в меню ниже и нажми «Создать».\n"
                "Комната скрыта от остальных. После создания перейди в неё по ссылке.")
        if note:
            body += f"\n\n**Уточнение**\n{note}"
        self.card("Собери свой круг", body,
                  [ui.PeopleSelect(self.select, placeholder="Кого пригласить в приватку?",
                                   selected=self.members)],
                  [ui.Button(f"Создать · {len(self.members)} друзей", self.create,
                             style=discord.ButtonStyle.primary, emoji="➕"),
                   ui.Button("Название", self.rename), ui.Button("Отмена", self.cancel)])

    async def select(self, interaction, values):
        members = [member for member in values if member.id != self.owner_id]
        view = CreateView(self.guild_id, self.owner_id, members=members, name=self.name)
        from utils import components_v2 as v2
        await v2.edit_response(interaction, view=view)

    async def rename(self, interaction):
        from commands.room_forms import RoomInputModal
        await interaction.response.send_modal(RoomInputModal(self, "create_name"))

    async def cancel(self, interaction):
        from utils import components_v2 as v2
        view = ui.Panel(self.guild_id, self.owner_id).card("Создание отменено", "Когда захочешь — открой `/room create` снова.")
        await v2.edit_response(interaction, view=view)

    async def create(self, interaction):
        await ui.acknowledge(interaction, update=True)
        try:
            row, channel = await rooms.create_room(interaction, self.members, self.name)
            note = "Твоя комната готова. Приглашённые уже получили доступ."
            if row.get("reused"):
                note = "У тебя уже есть комната. Выбранных друзей можно добавить кнопкой «Пригласить»."
            if row.get("move_failed"):
                note += " Перейди в неё сам: автоматически переместить тебя не удалось."
            await show_controls(interaction, row, channel, note)
        except Exception as error:
            await ui.show(interaction, CreateView(self.guild_id, self.owner_id, members=self.members,
                                                  name=self.name, note=ui.problem(error)))


class ControlsView(ui.Panel):
    def __init__(self, guild_id, owner_id, row, channel, note=""):
        super().__init__(guild_id, owner_id)
        self.row, self.channel = row, channel
        self.room_channel_id = channel.id
        owner = row["owner_id"] == owner_id
        permissions = channel.overwrites_for(channel.guild.default_role)
        locked = self.locked = row.get("ui_locked", permissions.connect is False)
        hidden = self.hidden = row.get("ui_hidden", permissions.view_channel is False)
        member_names = " · ".join(f"<@{member.id}>" for member in channel.members[:10]) or "Пока никого."
        body = (f"**<#{channel.id}>**\nВладелец · <@{row['owner_id']}>\n\n"
                f"**{'Закрыта' if locked else 'Открыта'}** · {'Скрыта' if hidden else 'Видна на сервере'}"
                f" · Лимит: **{channel.user_limit or 'без лимита'}**\n"
                f"**В комнате · {len(channel.members)}**\n{member_names}")
        if note:
            body += f"\n\n{note}"
        body += "\n\n-# Пустая комната удаляется через 60 секунд. Приглашения сохраняются при закрытии входа."
        if owner:
            rows = [
                [ui.Button("Название", self.rename), ui.Button("Лимит", self.limit),
                 ui.Button("Открыть вход" if locked else "Закрыть вход", self.toggle_lock),
                 ui.Button("Показать" if hidden else "Скрыть", self.toggle_visibility)],
                [ui.Button("Пригласить", self.invite, style=discord.ButtonStyle.primary),
                 ui.Button("Убрать участника", self.kick), ui.Button("Передать", self.transfer)],
                [ui.Button("Обновить", self.refresh),
                 ui.Button("Удалить комнату", self.confirm_delete, style=discord.ButtonStyle.danger)],
            ]
        else:
            body += "\n\nКогда владелец выйдет, участник комнаты сможет забрать управление."
            rows = [[ui.Button("Забрать управление", self.claim, style=discord.ButtonStyle.primary),
                     ui.Button("Обновить", self.refresh)]]
        self.card("Твоя тихая территория" if owner else "Комната сообщества", body, *rows)

    async def refresh(self, interaction):
        await ui.acknowledge(interaction, update=True)
        try:
            row, channel = await rooms.owned_room(interaction, require_owner=False)
            await show_controls(interaction, row, channel)
        except Exception as error:
            await ui.show(interaction, ui.Panel(self.guild_id, self.owner_id).card("Комната недоступна", ui.problem(error)))

    async def _toggle(self, interaction, callback, value, note):
        await ui.acknowledge(interaction, update=True)
        try:
            row, channel = await callback(interaction, value)
            row = {"ui_locked": self.locked, "ui_hidden": self.hidden, **row}
            await show_controls(interaction, row, channel, note)
        except Exception as error:
            await self.failure(interaction, error)

    async def failure(self, interaction, error):
        await ui.show(interaction, ControlsView(self.guild_id, self.owner_id, self.row, self.channel,
                                               ui.problem(error)))

    async def toggle_lock(self, interaction):
        locked = self.locked
        await self._toggle(interaction, actions.lock, not locked,
                           "Вход закрыт. Приглашённые сохранили доступ." if not locked else "Вход открыт для участников, которым видна комната.")

    async def toggle_visibility(self, interaction):
        hidden = self.hidden
        await self._toggle(interaction, actions.visibility, not hidden,
                           "Комната скрыта от остальных." if not hidden else "Комната видна на сервере. Настройка входа сохранена.")

    async def rename(self, interaction):
        from commands.room_forms import RoomInputModal
        await interaction.response.send_modal(RoomInputModal(self, "rename"))

    async def limit(self, interaction):
        from commands.room_forms import RoomInputModal
        await interaction.response.send_modal(RoomInputModal(self, "limit"))

    async def _people(self, interaction, mode):
        from commands.room_forms import MembersView
        from utils import components_v2 as v2
        await v2.edit_response(interaction, view=MembersView(self, mode))

    async def invite(self, interaction):
        await self._people(interaction, "invite")

    async def kick(self, interaction):
        await self._people(interaction, "kick")

    async def transfer(self, interaction):
        await self._people(interaction, "transfer")

    async def confirm_delete(self, interaction):
        from commands.room_forms import DeleteView
        from utils import components_v2 as v2
        await v2.edit_response(interaction, view=DeleteView(self))

    async def claim(self, interaction):
        await open_claim(interaction, update=True)


async def show_controls(interaction, row, channel, notice=""):
    return await ui.show(interaction, ControlsView(interaction.guild_id, interaction.user.id,
                                                   row, channel, notice))


async def open_create(interaction):
    if interaction.guild is None:
        return await ui.notice(interaction, "Создавать комнаты можно только на сервере.")
    await ui.show(interaction, CreateView(interaction.guild_id, interaction.user.id))


async def open_controls(interaction):
    await ui.acknowledge(interaction)
    try:
        row, channel = await rooms.owned_room(interaction, require_owner=False)
        await show_controls(interaction, row, channel)
    except Exception as error:
        await ui.notice(interaction, ui.problem(error))


async def open_claim(interaction, *, update=False):
    await ui.acknowledge(interaction, update=update)
    try:
        row, channel = await actions.claim(interaction)
        await show_controls(interaction, row, channel, "Теперь ты управляешь комнатой.")
    except Exception as error:
        if update:
            await ui.show(interaction, ui.Panel(interaction.guild_id, interaction.user.id).card(
                "Передача недоступна", ui.problem(error), [ui.Button("Обновить", open_controls)]))
        else:
            await ui.notice(interaction, ui.problem(error))


async def _help(interaction):
    view = ui.Panel(interaction.guild_id, interaction.user.id).card(
        "Как устроена приватка", "**01 · Выбери людей**\nВ меню можно найти участников по @ и выбрать до 10 человек.\n\n"
        "**02 · Создай комнату**\nVivian создаст скрытый канал. Если ты уже в голосовом, попробует перенести тебя; иначе зайди по ссылке.\n\n"
        "**03 · Управляй своим пространством**\nНазвание, лимит, доступ и приглашения — в личной панели `/room manage`.\n\n"
        "**04 · Пустая комната исчезает**\nЧерез 60 секунд без людей канал удаляется. Администраторы сервера сохраняют доступ.",
        [ui.Button("Создать комнату", open_create, style=discord.ButtonStyle.primary)])
    await ui.show(interaction, view)


def lobby_view():
    return ui.Panel(timeout=None).card(
        "Вместе, в своём пространстве", "Собери друзей и открой личный голосовой канал.\n"
        "Выбери участников через **@**, а остальное Vivian сделает за тебя.\n\n"
        "**Приватный вход** · **Своё название** · **Управление в один клик**",
        [ui.Button("Создать комнату", open_create, style=discord.ButtonStyle.primary,
                   custom_id="vivian_room_create", emoji="➕"),
         ui.Button("Моя комната", open_controls, custom_id="vivian_room_manage"),
         ui.Button("Как это работает", _help, custom_id="vivian_room_help")], footer=LOBBY_MARKER)


def register_views():
    if not getattr(config.bot, "room_lobby_registered", False):
        config.bot.add_view(lobby_view())
        config.bot.room_lobby_registered = True
