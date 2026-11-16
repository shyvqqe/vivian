"""Private /music search and artist-authorized downloads."""
from __future__ import annotations

import asyncio
from contextlib import closing
import re
import time
from urllib.parse import quote

import discord
from discord import app_commands

from commands import music_catalog
from commands import music_download_catalog as catalog
from core import config
from utils import command_responses, components_v2 as v2


_SEARCH_SLOTS = asyncio.Semaphore(4)
_DOWNLOAD_SLOTS = asyncio.Semaphore(2)
_ACTIVE_DOWNLOADS: set[int] = set()
_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_SELECTION = 3
_NO_MENTIONS = discord.AllowedMentions.none()


def _track_label(artist: str, title: str) -> str:
    return discord.utils.escape_markdown(f"{artist} — {title}", ignore_links=False)


def _track_link(track: catalog.Track) -> str:
    return f"[Jamendo]({track.page})"


def _results_embed(tracks: list[catalog.Track], apple_rows: list[dict], notice: str) -> discord.Embed:
    lines = []
    for number, track in enumerate(tracks, 1):
        length = f" · {track.seconds // 60}:{track.seconds % 60:02d}" if track.seconds else ""
        status = "MP3" if track.downloadable else "только ссылка"
        lines.append(f"**{number}.** {_track_label(track.artist, track.title)}{length}\n"
                     f"↳ {_track_link(track)} · {status}")
    for number, row in enumerate(apple_rows, len(tracks) + 1):
        label = _track_label(row["artist"][:75], row["title"][:75])
        url = quote(row["url"], safe=":/?&=%._-~")
        lines.append(f"**{number}.** {label}\n↳ [Apple Music]({url}) · только ссылка")
    embed = discord.Embed(title="Результаты поиска музыки", description="\n".join(lines), color=0x806BFF)
    if any(track.downloadable for track in tracks):
        embed.set_footer(text="Выбери до трёх треков с пометкой MP3. Меню действует 3 минуты.")
    elif notice:
        embed.set_footer(text=notice)
    return embed


async def _apple_links(query: str) -> list[dict]:
    try:
        async with asyncio.timeout(7):
            rows = await asyncio.to_thread(music_catalog.search, query, "apple", 5)
    except (TimeoutError, OSError, TypeError, ValueError):
        return []
    return [row for row in rows if row["url"] and len(row["url"]) <= 320]


def _filename(track: catalog.Track) -> str:
    name = re.sub(r"[^\w -]+", "", track.title)[:65].strip(" _-")
    return f"jamendo-{track.id}-{name}.mp3" if name else f"jamendo-{track.id}.mp3"


async def _send_track(interaction: discord.Interaction, client, key: str, track: catalog.Track) -> None:
    file_limit = min(interaction.filesize_limit, _MAX_FILE_BYTES)
    data = await catalog.download(client, key, track, file_limit)
    try:
        source = _track_link(track)
        if track.license_url:
            source += f" · [Лицензия]({track.license_url})"
        with closing(discord.File(data, filename=_filename(track))) as attachment:
            async with asyncio.timeout(25):
                await v2.followup(
                    interaction, f"**{_track_label(track.artist, track.title)}**\n{source}",
                    file=attachment, ephemeral=True, allowed_mentions=_NO_MENTIONS,
                )
    finally:
        data.close()


class MusicDownloadView(v2.ControllerView):
    def __init__(self, owner_id: int, tracks: list[catalog.Track]):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.tracks = {track.id: track for track in tracks if track.downloadable}
        self.delivered: set[str] = set()
        self.last_request = -float("inf")
        self.panel_message: discord.InteractionMessage | None = None
        self.selector = discord.ui.Select(
            placeholder="Скачать MP3 по номеру",
            max_values=min(_MAX_SELECTION, len(self.tracks)),
            options=[discord.SelectOption(label=f"{index}. {track.artist} — {track.title}"[:100], value=track.id)
                     for index, track in enumerate(tracks, 1) if track.downloadable],
        )
        self.selector.callback = self._download_selected
        self.add_item(self.selector)

    async def on_timeout(self) -> None:
        if self.panel_message is not None:
            self.selector.disabled = True
            try:
                await v2.edit_message(self.panel_message, view=self)
            except discord.DiscordException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await v2.send_response(interaction, "Открой свой список через `/music`.", ephemeral=True)
        return False

    async def _download_selected(self, interaction: discord.Interaction) -> None:
        selected = tuple(self.selector.values)
        if not 1 <= len(selected) <= _MAX_SELECTION or any(value not in self.tracks for value in selected):
            await v2.send_response(interaction, "Выбери треки из списка.", ephemeral=True)
            return
        tracks = [track for track_id, track in self.tracks.items()
                  if track_id in selected and track_id not in self.delivered]
        if not tracks:
            await v2.send_response(interaction, "Эти MP3 уже отправлены выше.", ephemeral=True)
            return
        key = catalog.client_id()
        if not key:
            await v2.send_response(interaction, "Скачивание Jamendo сейчас отключено.", ephemeral=True)
            return
        if (self.owner_id in _ACTIVE_DOWNLOADS or _DOWNLOAD_SLOTS.locked()
                or time.monotonic() - self.last_request < 6):
            await v2.send_response(interaction, "Скачивание занято. Попробуй через несколько секунд.", ephemeral=True)
            return
        async with _DOWNLOAD_SLOTS:
            _ACTIVE_DOWNLOADS.add(self.owner_id)
            self.last_request = time.monotonic()
            try:
                await v2.send_response(interaction, "Готовлю MP3…", ephemeral=True)
                successes = 0
                failures = []
                async with catalog.new_client() as client:
                    for track in tracks:
                        try:
                            await _send_track(interaction, client, key, track)
                        except catalog.FileTooLarge:
                            issue = "трек превышает лимит вложений."
                        except catalog.CatalogError as error:
                            issue = str(error)
                        except (discord.DiscordException, TimeoutError):
                            issue = "не удалось подтвердить отправку в Discord."
                        else:
                            successes += 1
                            self.delivered.add(track.id)
                            continue
                        failures.append(f"• {_track_label(track.artist, track.title)} · {issue} {_track_link(track)}")
                content = f"Отправлено MP3: {successes} из {len(tracks)}."
                if failures:
                    content += "\n" + "\n".join(failures)
                await v2.edit_original(interaction, content=content, allowed_mentions=_NO_MENTIONS)
            finally:
                _ACTIVE_DOWNLOADS.discard(self.owner_id)


@config.bot.tree.command(name="music", description="Найти треки и скачать MP3, которые разрешил скачать автор.")
@app_commands.describe(query="Исполнитель или название трека, например: zxcursed blessed")
@app_commands.checks.cooldown(1, 8, key=lambda interaction: interaction.user.id)
async def music(interaction: discord.Interaction, query: app_commands.Range[str, 1, 160]):
    query = " ".join(query.split())
    if not query or "://" in query or query.startswith("//"):
        await command_responses.finish_deferred(interaction, "Введи название песни или имя исполнителя.")
        return
    if _SEARCH_SLOTS.locked():
        await command_responses.finish_deferred(interaction, "Поиск занят. Попробуй через несколько секунд.")
        return
    async with _SEARCH_SLOTS:
        if not await command_responses.defer_command(interaction, ephemeral=True):
            return
        tracks = []
        key = catalog.client_id()
        notice = "Для скачивания MP3 владелец бота может настроить JAMENDO_CLIENT_ID."
        if key:
            try:
                async with catalog.new_client() as client:
                    tracks = await catalog.search(client, key, query)
                notice = "В результатах нет треков с разрешённым скачиванием MP3."
            except catalog.CatalogError as error:
                notice = str(error)
        has_downloads = any(track.downloadable for track in tracks)
        apple_rows = [] if has_downloads else await _apple_links(query)
        if not tracks and not apple_rows:
            await command_responses.finish_deferred(interaction, f"По запросу нет результатов. {notice}")
            return
        if apple_rows:
            tracks = tracks[:catalog.RESULT_LIMIT - len(apple_rows)]
        view = MusicDownloadView(interaction.user.id, tracks) if has_downloads else None
        panel = await command_responses.edit_deferred(interaction, embed=_results_embed(tracks, apple_rows, notice), view=view)
        if view is not None:
            view.panel_message = panel
