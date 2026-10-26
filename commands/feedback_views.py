"""Private feedback forms and persistent, database-bound proposal voting."""
from __future__ import annotations

from io import BytesIO
import re

import discord

from core import community_store as store, config
from commands import feedback_support as support
from utils import components_v2 as v2


_TITLE = re.compile(r"(?:^|\n)## Идея #([1-9][0-9]{0,18})(?:\n|$)")


def _proposal_id(message):
    def scan(node):
        if node.get("type") == 10:
            found = _TITLE.search(node.get("content", ""))
            if found:
                return int(found.group(1))
        for child in node.get("components", ()):
            found = scan(child)
            if found is not None:
                return found
        return None
    for component in message.components:
        found = scan(component.to_dict())
        if found is not None:
            return found
    return None


class SuggestionVoteView(v2.ControllerView):
    def __init__(self, counts=None, status="open"):
        super().__init__(timeout=None)
        counts = counts or {"up": 0, "down": 0}
        self.up.label = f"За · {counts['up']}"
        self.down.label = f"Против · {counts['down']}"
        self.up.disabled = self.down.disabled = status not in support.OPEN_VOTES

    async def _vote(self, interaction, value):
        if not await support.acknowledge(interaction):
            return
        problem = support.access_error(interaction)
        if problem:
            return await support.private_reply(interaction, problem)
        message = interaction.message
        if message is None or config.bot.user is None or message.author.id != config.bot.user.id:
            return await support.private_reply(interaction, "Это не действующая карточка предложения Vivian.")
        item_id = _proposal_id(message)
        if item_id is None or item_id >= 2 ** 63:
            return await support.private_reply(interaction, "Не получилось определить номер предложения.")
        try:
            # This lock is separate from the V2 delivery lock acquired inside edit_message.
            async with support.lock_for("item", interaction.guild_id, item_id):
                item = await store.get_item(interaction.guild_id, item_id)
                if (item is None or item["kind"] != "suggestion"
                        or item.get("message_id") != message.id
                        or item.get("channel_id") != interaction.channel_id):
                    return await support.private_reply(interaction, "Эта карточка не привязана к предложению в базе.")
                problem = support.access_error(interaction)
                if problem:
                    return await support.private_reply(interaction, problem)
                if item["status"] not in support.OPEN_VOTES:
                    return await support.private_reply(interaction, "Голосование по этому предложению закрыто.")
                counts = await store.vote(interaction.guild_id, item_id, interaction.user.id, value)
                notice = "Голос учтён." if counts["mine"] else "Твой голос снят."
                try:
                    await v2.edit_message(message, embed=support.feedback_embed(item, counts=counts),
                                          view=SuggestionVoteView(counts, item["status"]))
                except discord.DiscordException as error:
                    print(f"[Feedback] Vote card update unavailable; id={item_id}; error={type(error).__name__}", flush=True)
                    notice += " Карточку сейчас обновить не удалось; голос сохранён в базе."
                await support.private_reply(interaction, notice)
        except store.CommunityUnavailable:
            await support.private_reply(interaction, "Хранилище временно недоступно. Голос не подтверждён.")
        except ValueError:
            await support.private_reply(interaction, "Предложение недоступно или голосование уже закрыто.")
        except Exception as error:
            print(f"[Feedback] Vote completion unavailable; id={item_id}; error={type(error).__name__}", flush=True)
            await support.private_reply(interaction, f"Не удалось подтвердить обновление карточки. Проверь `/report id:{item_id}`.")

    @discord.ui.button(label="За", style=discord.ButtonStyle.primary, custom_id="vivian_suggestion_up")
    async def up(self, interaction, button):
        await self._vote(interaction, 1)

    @discord.ui.button(label="Против", style=discord.ButtonStyle.secondary, custom_id="vivian_suggestion_down")
    async def down(self, interaction, button):
        await self._vote(interaction, -1)


def register_views():
    if not getattr(config.bot, "community_vote_view_registered", False):
        config.bot.add_view(SuggestionVoteView())
        config.bot.community_vote_view_registered = True


async def _submit(interaction, *, kind, owner_id, guild_id, title, body, details=None, attachment=None):
    if not await support.acknowledge(interaction):
        return
    if interaction.user.id != owner_id or interaction.guild_id != guild_id:
        return await support.private_reply(interaction, "Эта форма принадлежит другому участнику или серверу.")
    problem = support.access_error(interaction)
    if problem:
        return await support.private_reply(interaction, problem)
    item = None
    uploaded = None
    try:
        async with support.lock_for("submit", guild_id, owner_id):
            remaining = support.cooldown_remaining(guild_id, owner_id)
            if remaining:
                return await support.private_reply(interaction, f"Следующее обращение можно отправить через {remaining} с.")
            if not store.is_ready():
                raise store.CommunityUnavailable("Community storage is unavailable")
            channel = support.target_channel(interaction.guild, kind, member=support.member_for(interaction), image=attachment is not None)
            data, extension = await support.download_image(attachment, interaction.guild)
            # Recheck channel privacy and caller access after downloading the upload.
            problem = support.access_error(interaction)
            if problem:
                return await support.private_reply(interaction, problem)
            channel = support.target_channel(interaction.guild, kind, member=support.member_for(interaction), image=data is not None)
            item = await store.create_item(guild_id, kind, owner_id, title.strip(), body.strip(),
                                           details=details or {}, seed_key=f"interaction:{interaction.id}")
            support.note_submission(guild_id, owner_id)
            if item.get("message_id"):
                return await support.private_reply(interaction, f"Обращение #{item['id']} уже сохранено. Посмотреть: `/report id:{item['id']}`.")
            image_url = None
            if data is not None:
                uploaded = discord.File(BytesIO(data), filename=f"bug-{item['id']}.{extension}")
                image_url = uploaded.uri
            counts = await store.vote_counts(guild_id, item["id"]) if kind == "suggestion" else None
            view = SuggestionVoteView(counts, item["status"]) if counts is not None else None
            message = await v2.send_message(channel, embed=support.feedback_embed(item, counts=counts, image_url=image_url),
                                            view=view, files=[uploaded] if uploaded else [])
            await store.bind_message(guild_id, item["id"], channel.id, message.id)
            if kind == "bug":
                receipt = f"Ошибка #{item['id']} сохранена и отправлена команде сервера в закрытый канал."
            else:
                receipt = f"Предложение #{item['id']} опубликовано: {message.jump_url}"
            await support.private_reply(interaction, receipt + f"\nСтатус: `/report id:{item['id']}`.")
    except ValueError as error:
        await support.private_reply(interaction, str(error))
    except store.CommunityUnavailable:
        text = (f"Обращение #{item['id']} сохранено, но публикация карточки не подтверждена."
                if item else "Хранилище временно недоступно. Сохранение обращения не подтверждено.")
        await support.private_reply(interaction, text)
    except Exception as error:
        print(f"[Feedback] Submission unavailable; kind={kind}; id={item['id'] if item else 'new'}; error={type(error).__name__}", flush=True)
        text = (f"Обращение #{item['id']} сохранено, но отправку карточки завершить не удалось. Используй `/report id:{item['id']}`."
                if item else "Не получилось завершить обращение. Попробуй позже.")
        await support.private_reply(interaction, text)
    finally:
        if uploaded is not None:
            uploaded.close()


class BugModal(discord.ui.Modal, title="Сообщить об ошибке Vivian"):
    summary = discord.ui.TextInput(label="Коротко об ошибке", max_length=100)
    command = discord.ui.TextInput(label="Команда или действие", placeholder="Например: /play, выбор трека", max_length=100, required=False)
    description = discord.ui.TextInput(label="Что случилось и что ожидалось", style=discord.TextStyle.paragraph, max_length=1500)
    reproduction = discord.ui.TextInput(label="Как повторить", style=discord.TextStyle.paragraph, max_length=800, required=False)

    def __init__(self, owner_id, guild_id, attachment=None):
        super().__init__(timeout=300)
        self.owner_id, self.guild_id, self.attachment = owner_id, guild_id, attachment

    async def on_submit(self, interaction):
        await _submit(interaction, kind="bug", owner_id=self.owner_id, guild_id=self.guild_id,
                      title=self.summary.value, body=self.description.value, attachment=self.attachment,
                      details={"command": self.command.value, "reproduction": self.reproduction.value,
                               "screenshot": self.attachment is not None})

    async def on_error(self, interaction, error):
        print(f"[Feedback] Bug form unavailable; error={type(error).__name__}", flush=True)
        await support.private_reply(interaction, "Не получилось обработать форму ошибки.")


class SuggestModal(discord.ui.Modal, title="Предложить улучшение Vivian"):
    summary = discord.ui.TextInput(label="Название идеи", max_length=100)
    description = discord.ui.TextInput(label="Что предлагаешь и зачем", style=discord.TextStyle.paragraph, max_length=1500)

    def __init__(self, owner_id, guild_id):
        super().__init__(timeout=300)
        self.owner_id, self.guild_id = owner_id, guild_id

    async def on_submit(self, interaction):
        await _submit(interaction, kind="suggestion", owner_id=self.owner_id, guild_id=self.guild_id,
                      title=self.summary.value, body=self.description.value)

    async def on_error(self, interaction, error):
        print(f"[Feedback] Suggestion form unavailable; error={type(error).__name__}", flush=True)
        await support.private_reply(interaction, "Не получилось обработать форму предложения.")
