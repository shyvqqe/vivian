"""Shareable, user-authored Discord Components V2 production cards."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO
import re
import time
from urllib.parse import urlsplit

import discord
from discord import app_commands

from core import config
from utils import components_v2 as v2


studio = app_commands.Group(name="studio", description="Студия карточек для релизов, съёмок и командной работы.", guild_only=True)
_SHOT = re.compile(r"([0-9]{1,3})\s*[сs]?\s*:\s*(.+)", re.IGNORECASE)
_BOOKMARKS = re.compile(r"[|\n]")
_MAX_REVEALS = 48
_CHANNEL_REVEALS = 3
_REVEAL_SECONDS = 15 * 60
_PANEL_CHAR_LIMIT = 3900


def _safe(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text, ignore_links=False))


def _entries(text):
    return [part.strip() for part in _BOOKMARKS.split(text) if part.strip()]


def _https(value):
    value = value.strip()
    if not value:
        return ""
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None
                or "\\" in value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError
        url.port
    except ValueError:
        raise ValueError("Для источника укажи корректную HTTPS-ссылку.") from None
    return value


def _can_publish(interaction):
    channel = interaction.channel
    thread = isinstance(channel, discord.Thread)
    if interaction.guild_id is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return False
    return all(
        permissions.view_channel and (
            permissions.send_messages_in_threads if thread else permissions.send_messages
        )
        for permissions in (interaction.permissions, interaction.app_permissions)
    )


def _panel(label, title, sections, *, color=0x8F86FF):
    header = f"-# Vivian Studio · {label}\n## {_safe(title)}"
    container = discord.ui.Container(
        discord.ui.TextDisplay(header), accent_colour=color,
    )
    remaining = _PANEL_CHAR_LIMIT - len(header)
    for heading, body in sections:
        if not body or remaining < 1:
            continue
        block = f"**{heading}**\n{body}" if heading else body
        if len(block) > remaining:
            block = block[:remaining - 1].rstrip("\\") + "…"
        remaining -= len(block)
        container.add_item(discord.ui.Separator(visible=False))
        container.add_item(discord.ui.TextDisplay(block))
    return container


def _view(container, *, timeout=180):
    view = discord.ui.LayoutView(timeout=timeout)
    view.add_item(container)
    return view


async def _notice(interaction, text):
    view = _view(_panel("Студия", "Vivian", [("", text)]))
    if interaction.response.is_done():
        await v2.edit_original(interaction, view=view)
    else:
        await v2.send_response(interaction, view=view, ephemeral=True)


async def _send(interaction, view, *, publish, file=None):
    if publish and not _can_publish(interaction):
        return await _notice(interaction, "Для общей карточки тебе и Vivian нужны права писать в этом канале.")
    uploads = {"file": file} if file is not None else {}
    return await v2.send_response(interaction, view=view, ephemeral=not publish, **uploads)


def _shots(text):
    entries = _entries(text)
    if not 1 <= len(entries) <= 8:
        raise ValueError("Добавь от одного до восьми кадров через |. Пример: 5: общий план | 12: крупный план.")
    result = []
    for shot in entries:
        match = _SHOT.fullmatch(shot)
        if match is None:
            raise ValueError("Перед каждым кадром укажи секунды и двоеточие. Пример: 8: вид со сцены.")
        seconds, caption = int(match[1]), match[2].strip()
        if not 1 <= seconds <= 600 or not 1 <= len(caption) <= 220:
            raise ValueError("У кадра должно быть от 1 до 600 секунд и до 220 символов описания.")
        result.append((seconds, caption))
    return tuple(result)


def _timestamp(seconds):
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


class Storyboard(discord.ui.LayoutView):
    def __init__(self, owner_id, title, shots):
        super().__init__(timeout=300)
        self.owner_id, self.title, self.shots = owner_id, title, shots
        self.page = 0
        self._lock = asyncio.Lock()
        self._build()

    def _build(self):
        self.clear_items()
        start = sum(seconds for seconds, _ in self.shots[:self.page])
        duration, text = self.shots[self.page]
        total = sum(seconds for seconds, _ in self.shots)
        box = _panel("Раскадровка", self.title, [
            (f"Кадр {self.page + 1} из {len(self.shots)}", _safe(text)),
            ("Таймкод", f"`{_timestamp(start)} → {_timestamp(start + duration)}` · всего `{_timestamp(total)}`"),
        ])
        if len(self.shots) > 1:
            previous = discord.ui.Button(label="Назад", disabled=self.page == 0,
                                         custom_id="vivian:studio:storyboard:previous")
            next_button = discord.ui.Button(label="Вперёд", style=discord.ButtonStyle.primary,
                                            disabled=self.page + 1 == len(self.shots),
                                            custom_id="vivian:studio:storyboard:next")
            previous.callback = self._previous
            next_button.callback = self._next
            box.add_item(discord.ui.ActionRow(previous, next_button))
        self.add_item(box)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await _notice(interaction, "Листать общую раскадровку может её автор.")
        return False

    async def _previous(self, interaction):
        await self._turn(interaction, -1)

    async def _next(self, interaction):
        await self._turn(interaction, 1)

    async def _turn(self, interaction, delta):
        await interaction.response.defer()
        async with self._lock:
            previous = self.page
            self.page = min(len(self.shots) - 1, max(0, previous + delta))
            if self.page == previous:
                return
            self._build()
            try:
                async with asyncio.timeout(8):
                    await v2.edit_original(interaction, view=self)
            except (discord.DiscordException, OSError):
                self.page = previous
                self._build()
                await v2.followup(interaction, "Не удалось обновить кадр. Попробуй нажать ещё раз.", ephemeral=True)


@studio.command(name="storyboard", description="Собрать интерактивную раскадровку с таймкодами и общим хронометражем.")
@app_commands.describe(shots="До 8 кадров через |. Пример: 5: общий план | 12: герой в кадре",
                       publish="Опубликовать раскадровку в текущем канале")
@app_commands.checks.cooldown(3, 30, key=lambda interaction: interaction.user.id)
async def storyboard(
    interaction: discord.Interaction, title: app_commands.Range[str, 1, 80],
    shots: app_commands.Range[str, 3, 1300], publish: bool = False,
):
    try:
        parsed = _shots(shots)
        if not title.strip():
            raise ValueError("Введи название раскадровки.")
    except ValueError as error:
        return await _notice(interaction, str(error))
    await _send(interaction, Storyboard(interaction.user.id, title.strip(), parsed), publish=publish)


@studio.command(name="release", description="Собрать релизную карточку с обновлениями, исправлениями и экспортом Markdown.")
@app_commands.describe(project="Название проекта", version="Версия или имя релиза", added="Что появилось",
                       fixed="Что исправлено", breaking="Что нужно изменить при обновлении",
                       export="Приложить release-notes.md", publish="Опубликовать в текущем канале")
@app_commands.checks.cooldown(3, 30, key=lambda interaction: interaction.user.id)
async def release(
    interaction: discord.Interaction, project: app_commands.Range[str, 1, 80],
    version: app_commands.Range[str, 1, 32], added: app_commands.Range[str, 1, 700],
    fixed: app_commands.Range[str, 1, 700] = "", breaking: app_commands.Range[str, 1, 600] = "",
    export: bool = False, publish: bool = False,
):
    title = " · ".join((" ".join(project.split()), " ".join(version.split())))
    sections = [("Что нового", added.strip()), ("Исправления", fixed.strip()), ("При обновлении", breaking.strip())]
    if not project.strip() or not version.strip() or not added.strip():
        return await _notice(interaction, "Заполни проект, версию и описание изменений.")
    box = _panel("Релиз", title, [(heading, _safe(body)) for heading, body in sections], color=0x5FBDA4)
    if not export:
        return await _send(interaction, _view(box), publish=publish)
    if not interaction.app_permissions.attach_files:
        return await _notice(interaction, "Для Markdown-файла Vivian нужно право прикреплять файлы. Карточку можно открыть без экспорта.")
    markdown = f"# {title}\n" + "".join(f"\n## {heading}\n\n{body}\n" for heading, body in sections if body)
    with closing(discord.File(BytesIO(markdown.encode("utf-8")), filename="release-notes.md")) as file:
        await _send(interaction, _view(box), publish=publish, file=file)


@studio.command(name="handoff", description="Передать проект или смену: что готово, следующий шаг и что его блокирует.")
@app_commands.describe(next_step="Конкретный следующий шаг", blocker="Что мешает или требует внимания",
                       owner="Кто подхватит следующий шаг", publish="Опубликовать в текущем канале")
@app_commands.checks.cooldown(3, 30, key=lambda interaction: interaction.user.id)
async def handoff(
    interaction: discord.Interaction, title: app_commands.Range[str, 1, 90],
    done: app_commands.Range[str, 1, 650], next_step: app_commands.Range[str, 1, 500],
    blocker: app_commands.Range[str, 1, 350] = "", owner: discord.Member | None = None, publish: bool = False,
):
    if not all(part.strip() for part in (title, done, next_step)):
        return await _notice(interaction, "Укажи название, что готово и следующий шаг.")
    recipient = f"\nПодхватит {owner.mention}" if owner else ""
    box = _panel("Передача работы", title.strip(), [
        ("Готово", _safe(done.strip())), ("Следующий шаг", _safe(next_step.strip()) + recipient),
        ("Зависимость или риск", _safe(blocker.strip())),
    ], color=0x76B7F4)
    await _send(interaction, _view(box), publish=publish)


@studio.command(name="credits", description="Собрать титры: участники проекта, их роли, лицензия и кнопка источника.")
@app_commands.describe(entries="До 8 строк «Роль: имя» через |. Пример: Монтаж: Ника | Звук: Алекс",
                       source="Необязательная HTTPS-ссылка на оригинал или страницу проекта",
                       publish="Опубликовать в текущем канале")
@app_commands.checks.cooldown(3, 30, key=lambda interaction: interaction.user.id)
async def credits(
    interaction: discord.Interaction, project: app_commands.Range[str, 1, 80],
    entries: app_commands.Range[str, 3, 1200], license: app_commands.Range[str, 1, 140] = "",
    source: app_commands.Range[str, 1, 400] = "", publish: bool = False,
):
    rows = [tuple(part.strip() for part in row.split(":", 1)) for row in _entries(entries)]
    if (not project.strip() or not 1 <= len(rows) <= 8
            or any(len(row) != 2 or not 1 <= len(row[0]) <= 45 or not 1 <= len(row[1]) <= 100 for row in rows)):
        return await _notice(interaction, "Укажи название проекта и до восьми строк через | в форме «Роль: имя».")
    try:
        url = _https(source)
    except ValueError as error:
        return await _notice(interaction, str(error))
    box = _panel("Титры", project.strip(), [
        ("Команда", "\n".join(f"**{_safe(role)}** · {_safe(person)}" for role, person in rows)),
        ("Лицензия", _safe(license.strip())),
    ], color=0xC68EF3)
    if url:
        box.add_item(discord.ui.ActionRow(discord.ui.Button(label="Источник", url=url)))
    await _send(interaction, _view(box), publish=publish)


@dataclass
class Reveal:
    guild_id: int
    channel_id: int
    owner_id: int
    title: str
    teaser: str
    secret: str
    opens_at: float
    expires_at: float
    opens_unix: int
    message_id: int | None = None
    cleanup: asyncio.TimerHandle | None = None


_REVEALS: dict[int, Reveal] = {}


def _forget_reveal(reveal_id):
    item = _REVEALS.pop(reveal_id, None)
    if item and item.cleanup:
        item.cleanup.cancel()


def _reveal_card(reveal_id, item, *, closed=False):
    expires_unix = item.opens_unix + int(item.expires_at - item.opens_at)
    detail = "Тизер отозван." if closed else (
        f"**Открытие** · <t:{item.opens_unix}:t>\n**Доступ до** · <t:{expires_unix}:t>"
    )
    box = _panel("Тизер", item.title, [
        ("", _safe(item.teaser)), ("", detail),
    ], color=0xEF9B7A)
    if not closed:
        box.add_item(discord.ui.ActionRow(RevealAction(reveal_id, "open"), RevealAction(reveal_id, "close")))
    return _view(box, timeout=None)


class RevealAction(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vivian:studio:reveal:(?P<id>[0-9]{1,20}):(?P<action>open|close)",
):
    def __init__(self, reveal_id, action):
        self.reveal_id, self.action = reveal_id, action
        super().__init__(discord.ui.Button(
            label="Раскрыть лично" if action == "open" else "Отозвать",
            style=discord.ButtonStyle.primary if action == "open" else discord.ButtonStyle.secondary,
            custom_id=f"vivian:studio:reveal:{reveal_id}:{action}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction):
        item = _REVEALS.get(self.reveal_id)
        now = time.monotonic()
        if (item is None or now >= item.expires_at or interaction.user.bot
                or interaction.guild_id != item.guild_id or interaction.channel_id != item.channel_id
                or item.message_id is None or getattr(interaction.message, "id", None) != item.message_id):
            return await _notice(interaction, "Тизер уже недоступен.")
        if self.action == "close":
            if interaction.user.id != item.owner_id:
                return await _notice(interaction, "Отозвать тизер может его автор.")
            _forget_reveal(self.reveal_id)
            await v2.edit_response(interaction, view=_reveal_card(self.reveal_id, item, closed=True))
            return
        if now < item.opens_at:
            return await _notice(interaction, f"Скрытый текст станет доступен <t:{item.opens_unix}:R>.")
        box = _panel("Раскрытый тизер", item.title, [("", _safe(item.secret))], color=0xEF9B7A)
        await v2.send_response(interaction, view=_view(box), ephemeral=True)


@studio.command(name="reveal", description="Опубликовать тизер, скрытый текст которого открывается лично по кнопке.")
@app_commands.describe(teaser="Текст публичной карточки", reveal="Скрытый текст, который увидит нажавший кнопку",
                       minutes="Задержка открытия, от 0 до 10 минут")
@app_commands.checks.cooldown(1, 30, key=lambda interaction: (interaction.guild_id, interaction.user.id))
async def reveal(
    interaction: discord.Interaction, title: app_commands.Range[str, 1, 80],
    teaser: app_commands.Range[str, 1, 350], reveal: app_commands.Range[str, 1, 750],
    minutes: app_commands.Range[int, 0, 10] = 0,
):
    if not _can_publish(interaction):
        return await _notice(interaction, "Тебе и Vivian нужны права писать в этом канале.")
    if not all(part.strip() for part in (title, teaser, reveal)):
        return await _notice(interaction, "Заполни заголовок, тизер и скрытый текст.")
    if (len(_REVEALS) >= _MAX_REVEALS
            or sum(item.channel_id == interaction.channel_id for item in _REVEALS.values()) >= _CHANNEL_REVEALS):
        return await _notice(interaction, "В канале уже есть открытые тизеры. Дождись, пока они закроются.")

    now = time.monotonic()
    delay = minutes * 60
    item = Reveal(
        interaction.guild_id, interaction.channel_id, interaction.user.id, title.strip(), teaser.strip(),
        reveal.strip(), now + delay, now + _REVEAL_SECONDS, int(time.time()) + delay,
    )
    _REVEALS[interaction.id] = item
    item.cleanup = asyncio.get_running_loop().call_later(_REVEAL_SECONDS, _forget_reveal, interaction.id)
    try:
        await interaction.response.defer(thinking=True, ephemeral=True)
        async with asyncio.timeout(10):
            message = await v2.send_message(
                interaction.channel, view=_reveal_card(interaction.id, item), silent=True, nonce=str(interaction.id),
            )
        item.message_id = message.id
    except (discord.DiscordException, OSError):
        _forget_reveal(interaction.id)
        return await _notice(interaction, "Не удалось подтвердить публикацию. Проверь канал перед повтором.")
    except asyncio.CancelledError:
        _forget_reveal(interaction.id)
        raise
    await _notice(interaction, f"[Открыть тизер]({message.jump_url}). Карточка действует 15 минут.")


config.bot.tree.add_command(studio)
config.bot.add_dynamic_items(RevealAction)
