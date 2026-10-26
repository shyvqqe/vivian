"""Owner-bound room forms, participant selection and deletion confirmation."""
from __future__ import annotations

import discord

from app import room_actions as actions
from commands import room_ui as ui
from utils import components_v2 as v2


class RoomInputModal(discord.ui.Modal):
    def __init__(self, source, mode):
        super().__init__(title="Vivian // Лимит участников" if mode == "limit" else "Vivian // Название комнаты", timeout=300)
        self.source, self.mode = source, mode
        if mode == "limit":
            self.value = discord.ui.TextInput(label="От 0 до 99 · 0 — без лимита", max_length=2,
                                               default=str(source.channel.user_limit))
        else:
            name = source.name if mode == "create_name" else source.channel.name
            self.value = discord.ui.TextInput(label="Как назовём твоё пространство?", max_length=80,
                                               default=name or "", placeholder="Например: Тихий вечер")
        self.add_item(self.value)

    async def on_submit(self, interaction):
        if not await self.source.interaction_check(interaction):
            return
        await ui.acknowledge(interaction, update=True)
        try:
            from commands import room_views
            if self.mode == "create_name":
                name = str(self.value.value).strip()
                if not name:
                    raise ValueError("Придумай название длиной от 1 до 80 символов.")
                view = room_views.CreateView(self.source.guild_id, self.source.owner_id,
                                              members=self.source.members, name=name)
                return await ui.show(interaction, view)
            if self.mode == "limit":
                value = str(self.value.value).strip()
                if not value.isascii() or not value.isdigit() or not 0 <= int(value) <= 99:
                    raise ValueError("Лимит — число от 0 до 99. Ноль означает без лимита.")
                row, channel = await actions.limit(interaction, int(value))
                note = f"Лимит обновлён: **{channel.user_limit or 'без лимита'}**."
            else:
                row, channel = await actions.rename(interaction, str(self.value.value).strip())
                note = "Название обновлено."
            row = {"ui_locked": self.source.locked, "ui_hidden": self.source.hidden, **row}
            await room_views.show_controls(interaction, row, channel, note)
        except Exception as error:
            if hasattr(self.source, "failure"):
                await self.source.failure(interaction, error)
            else:
                await ui.show(interaction, room_views.CreateView(
                    self.source.guild_id, self.source.owner_id, members=self.source.members,
                    name=self.source.name, note=ui.problem(error)))

    async def on_error(self, interaction, error):
        print(f"[Rooms UI] Form failed; error={type(error).__name__}", flush=True)
        await ui.notice(interaction, ui.problem(error))


class MembersView(ui.Panel):
    def __init__(self, source, mode, members=(), note=""):
        super().__init__(source.guild_id, source.owner_id)
        self.source, self.mode, self.members = source, mode, list(members)
        self.room_channel_id = source.channel.id
        title, action = {"invite": ("Кого позовём?", "Пригласить"),
                         "kick": ("Кому закрыть доступ?", "Убрать из комнаты"),
                         "transfer": ("Кому доверим комнату?", "Передать управление")}[mode]
        people = " · ".join(f"<@{member.id}>" for member in self.members) or "Пока никто не выбран."
        description = {"invite": "Участники получат доступ к скрытой комнате, даже когда вход закрыт.",
                       "kick": "Участники потеряют доступ. Если они сейчас в комнате, Vivian их отключит. Администратора ограничить нельзя.",
                       "transfer": "Выбери одного участника в этой комнате. Он получит все кнопки владельца."}[mode]
        body = f"<#{self.room_channel_id}>\n\n{description}\n\n**Выбраны**\n{people}"
        if note:
            body += f"\n\n{note}"
        self.card(title, body,
                  [ui.PeopleSelect(self.select, placeholder="Выбери участника через @",
                                   maximum=1 if mode == "transfer" else 10, selected=self.members)],
                  [ui.Button(action, self.apply, style=discord.ButtonStyle.primary,
                             disabled=not self.members), ui.Button("Назад", self.back)])

    async def select(self, interaction, values):
        await v2.edit_response(interaction, view=MembersView(self.source, self.mode, values))

    async def back(self, interaction):
        await self.source.refresh(interaction)

    async def apply(self, interaction):
        await ui.acknowledge(interaction, update=True)
        if not self.members:
            return await ui.show(interaction, MembersView(self.source, self.mode, note="Сначала выбери участника."))
        callback = {"invite": actions.invite, "kick": actions.kick, "transfer": actions.transfer}[self.mode]
        completed, result, failure = 0, None, None
        for member in self.members:
            try:
                result = await callback(interaction, member)
                completed += 1
            except Exception as error:
                failure = error
                break
        if result is None:
            return await ui.show(interaction, MembersView(self.source, self.mode, self.members,
                                                          ui.problem(failure)))
        from commands.room_views import show_controls
        note = "Управление передано выбранному участнику." if self.mode == "transfer" else (
            f"Приглашено: **{completed}**." if self.mode == "invite" else f"Доступ закрыт: **{completed}**.")
        if failure is not None:
            note += f" Остальные действия не выполнены. {ui.problem(failure)}"
        row, channel = result
        row = {"ui_locked": self.source.locked, "ui_hidden": self.source.hidden, **row}
        await show_controls(interaction, row, channel, notice=note)


class DeleteView(ui.Panel):
    def __init__(self, source):
        super().__init__(source.guild_id, source.owner_id)
        self.source, self.room_channel_id = source, source.channel.id
        self.card("Удалить комнату?", f"<#{source.channel.id}> будет удалена.\n"
                  "Все участники отключатся, приглашения этой комнаты перестанут действовать.",
                  [ui.Button("Да, удалить", self.confirm, style=discord.ButtonStyle.danger),
                   ui.Button("Оставить комнату", self.cancel)])

    async def confirm(self, interaction):
        await ui.acknowledge(interaction, update=True)
        try:
            result = await actions.delete(interaction)
            view = ui.Panel(self.guild_id, self.owner_id).card("Комната удалена", ui.safe(result, 700))
            await ui.show(interaction, view)
        except Exception as error:
            await self.source.failure(interaction, error)

    async def cancel(self, interaction):
        await self.source.refresh(interaction)
