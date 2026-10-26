from __future__ import annotations
import discord
from utils import components_v2 as v2
from core import config as core_config
from commands import music_audio as commands_music_audio
from commands import music_playlists as commands_music_playlists
from core import music_state as core_music_state
from utils import helpers as utils_helpers
from utils import music as utils_music
from visuals import panels as visuals_panels


class MusicTrackSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        self.page_indices = parent_view.page_indices()
        options = []
        for index in self.page_indices:
            track = parent_view.tracks[index]
            artist = (track.get("artist") or "SoundCloud").strip()
            source = track.get("source") or "SoundCloud"
            description = f"{source} · {artist} · {utils_music._track_duration_label(track.get('duration'))}"
            options.append(discord.SelectOption(
                label=(track.get("title") or "Без названия")[:100],
                value=str(index),
                description=description[:100],
                default=index in parent_view.selected_indices,
            ))
        super().__init__(
            placeholder=f"Страница {parent_view.page + 1}/{parent_view.page_count} · до 5 треков",
            min_values=0,
            max_values=max(1, min(5, len(options))),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.parent_view
        if interaction.user.id != view.owner_id:
            return await utils_helpers.vivian_reply(
                interaction,
                "Эта панель связана с участником, который её открыл.",
                error=True,
                ephemeral=True,
            )
        visible_indices = set(self.page_indices)
        selected_elsewhere = set(view.selected_indices) - visible_indices
        view.selected_indices = sorted(selected_elsewhere | {int(value) for value in self.values})
        view.enqueue_selected.disabled = not view.selected_indices
        view.enqueue_selected.label = "Проиграть выбор"
        view.save_playlist.disabled = not view.selected_indices
        view.notice = None
        view.notice_error = False
        await v2.edit_response(interaction, embed=view.make_embed(), view=view)


class MusicSearchView(v2.ControllerView):
    def __init__(self, guild_id: int, owner_id: int, query: str, tracks: list[dict]):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.query = query
        self.tracks = tracks[:10]
        self.page = 0
        self.selected_indices: list[int] = []
        self.notice: str | None = None
        self.notice_error = False
        self.panel_message = None
        self.visual_enabled = False
        self.busy = False
        self.track_select = MusicTrackSelect(self)
        self.add_item(self.track_select)
        self.previous_search_page.disabled = True
        self.page_indicator.label = f"{self.page + 1} / {self.page_count}"
        self.page_indicator.disabled = True
        self.next_search_page.disabled = self.page_count <= 1
        self.enqueue_selected.disabled = True
        self.save_playlist.disabled = True

    @property
    def page_count(self) -> int:
        return max(1, (len(self.tracks) + 4) // 5)

    def page_indices(self) -> list[int]:
        start = self.page * 5
        return list(range(start, min(start + 5, len(self.tracks))))

    def set_page(self, page: int) -> None:
        self.page = max(0, min(self.page_count - 1, page))
        self.remove_item(self.track_select)
        self.track_select = MusicTrackSelect(self)
        self.add_item(self.track_select)
        self.previous_search_page.disabled = self.page == 0
        self.next_search_page.disabled = self.page >= self.page_count - 1
        self.page_indicator.label = f"{self.page + 1} / {self.page_count}"

    async def change_page(self, interaction: discord.Interaction, page: int) -> None:
        if interaction.user.id != self.owner_id:
            return await utils_helpers.vivian_reply(
                interaction,
                "Эта панель связана с участником, который её открыл.",
                error=True,
                ephemeral=True,
            )
        self.set_page(page)
        try:
            await v2.edit_response(interaction, embed=self.make_embed(), view=self)
        except (discord.NotFound, discord.HTTPException):
            return

    def selected_tracks(self) -> list[dict]:
        return [self.tracks[index] for index in self.selected_indices if 0 <= index < len(self.tracks)]

    def make_embed(self) -> discord.Embed:
        page_indices = self.page_indices()
        selected_count = len(self.selected_indices)
        sources = " + ".join(dict.fromkeys(track.get("source") or "SoundCloud" for track in self.tracks))
        embed = discord.Embed(
            title="Поиск музыки",
            description=(
                f"**{discord.utils.escape_markdown(self.query[:100])}**\n"
                f"{sources}  ·  {len(self.tracks)} результатов  ·  выбрано {selected_count}"
            ),
            color=core_config.VIVIAN_ERROR if self.notice_error else core_music_state.MUSIC_PANEL_COLOR,
        )
        utils_helpers.vivian_brand(embed, "Vivian / MUSIC DISCOVERY")
        for index in page_indices:
            track = self.tracks[index]
            marker = "✓ " if index in self.selected_indices else ""
            title = discord.utils.escape_markdown(str(track.get("title") or "Без названия")[:85])
            artist = discord.utils.escape_markdown(str(track.get("artist") or "Исполнитель неизвестен")[:50])
            source = track.get("source") or "SoundCloud"
            embed.add_field(
                name=f"{index + 1:02}  {marker}{title}",
                value=f"{artist}  ·  `{utils_music._track_duration_label(track.get('duration'))}`  ·  {source}",
                inline=False,
            )
        if page_indices:
            cover_index = self.selected_indices[0] if self.selected_indices else page_indices[0]
            thumbnail = self.tracks[cover_index].get("thumbnail")
            if isinstance(thumbnail, str) and thumbnail.startswith(("https://", "http://")):
                embed.set_thumbnail(url=thumbnail)
        if self.notice:
            embed.add_field(name="СОСТОЯНИЕ", value=self.notice[:1000], inline=False)
        embed.set_footer(text=f"{self.page + 1} / {self.page_count}  ·  По 5 треков  ·  Выбор сохраняется между страницами")
        if self.visual_enabled:
            visuals_panels._apply_panel_visual(embed, "music")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await utils_helpers.vivian_reply(interaction, "Эта панель связана с другим участником.", error=True, ephemeral=True)
            return False
        if self.busy:
            await utils_helpers.vivian_reply(interaction, "Выбор уже передаётся в очередь.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="←", style=discord.ButtonStyle.secondary, row=1)
    async def previous_search_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_page(interaction, self.page - 1)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, row=1)
    async def page_indicator(self, interaction: discord.Interaction, button: discord.ui.Button):
        return

    @discord.ui.button(label="→", style=discord.ButtonStyle.secondary, row=1)
    async def next_search_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_page(interaction, self.page + 1)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.panel_message is not None:
            try:
                await v2.edit_message(self.panel_message,
                    embed=utils_helpers.vivian_embed(
                        "Время выбора истекло. Запусти `/play`, чтобы открыть новый поиск.",
                        title="◆ Vivian // ПОИСК ЗАКРЫТ",
                    ),
                    view=None,
                    attachments=[],
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Проиграть выбор", style=discord.ButtonStyle.primary, row=2)
    async def enqueue_selected(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await utils_helpers.vivian_reply(interaction, "Эта панель связана с другим участником.", error=True, ephemeral=True)
        selected = self.selected_tracks()
        if not selected:
            return await utils_helpers.vivian_reply(interaction, "Сначала выбери трек.", error=True, ephemeral=True)
        if self.busy:
            return await utils_helpers.vivian_reply(interaction, "Выбор уже передаётся в очередь.", ephemeral=True)
        self.busy = True
        previous_disabled = [(item, item.disabled) for item in self.children]
        for item in self.children:
            item.disabled = True
        button.label = "Подключаю…"
        self.notice = "Передаю выбранные треки в очередь."
        self.notice_error = False
        try:
            await v2.edit_response(interaction, embed=self.make_embed(), view=self)
        except (discord.NotFound, discord.HTTPException):
            self.busy = False
            button.label = "Проиграть выбор"
            for item, disabled in previous_disabled:
                item.disabled = disabled
            return
        ok = False
        try:
            ok, self.notice = await commands_music_audio._queue_music_tracks(interaction.guild, interaction.user, selected)
            self.notice_error = not ok
        except Exception as error:
            print(f"[Music search] Queue action failed; guild_id={self.guild_id}; error={error!r}", flush=True)
            self.notice = "Не удалось передать треки в очередь. Попробуй ещё раз."
            self.notice_error = True
        finally:
            self.busy = False
            for item, disabled in previous_disabled:
                item.disabled = disabled
            button.label = "Добавлено" if ok else "Проиграть выбор"
            button.disabled = ok or not self.selected_indices
        try:
            await v2.edit_original(interaction, embed=self.make_embed(), view=self)
        except discord.HTTPException:
            return

    @discord.ui.button(label="Создать плейлист", style=discord.ButtonStyle.primary, row=2)
    async def save_playlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await utils_helpers.vivian_reply(interaction, "Эта панель связана с другим участником.", error=True, ephemeral=True)
        if not self.selected_tracks():
            return await utils_helpers.vivian_reply(interaction, "Выбери треки, которые нужно сохранить.", error=True, ephemeral=True)
        if interaction.guild is None:
            return await utils_helpers.vivian_reply(interaction, "Плейлисты доступны только на сервере.", error=True, ephemeral=True)
        await interaction.response.send_modal(commands_music_playlists.MusicPlaylistNameModal(self))

    @discord.ui.button(label="Мои плейлисты", style=discord.ButtonStyle.secondary, row=2)
    async def open_playlists(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await utils_helpers.vivian_reply(interaction, "Эта панель связана с другим участником.", error=True, ephemeral=True)
        if interaction.guild is None:
            return await utils_helpers.vivian_reply(interaction, "Плейлисты доступны только на сервере.", error=True, ephemeral=True)
        # A personal collection must never replace the public discovery panel.
        await interaction.response.defer(thinking=True, ephemeral=True)
        playlists = await commands_music_playlists._load_music_playlists(interaction.guild.id, interaction.user.id)
        view = commands_music_playlists.MusicPlaylistView(self, playlists)
        visual = None
        try:
            if visuals_panels._can_upload_panel_visual(interaction.guild, interaction.channel):
                visual = await visuals_panels._panel_visual_file("music")
        except Exception as error:
            print(f"[Music playlists] Visual unavailable; error={type(error).__name__}", flush=True)
        view.visual_enabled = visual is not None
        try:
            view.panel_message = await v2.edit_original(
                interaction, embed=view.make_embed(), view=view,
                attachments=[visual] if visual else [],
            )
        except discord.HTTPException:
            view.stop()
            return

    @discord.ui.button(label="Закрыть", style=discord.ButtonStyle.secondary, row=2)
    async def close_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await utils_helpers.vivian_reply(interaction, "Эта панель связана с другим участником.", error=True, ephemeral=True)
        for item in self.children:
            item.disabled = True
        self.stop()
        await v2.edit_response(interaction,
            embed=utils_helpers.vivian_embed("Панель закрыта.", title="◆ Vivian // ЗАКРЫТО"),
            view=None,
            attachments=[],
        )
