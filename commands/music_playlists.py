from __future__ import annotations
from psycopg.types.json import Jsonb
import discord
from utils import components_v2 as v2
from core import config as core_config
from commands import music_audio as commands_music_audio
from commands import music_select as commands_music_select
from core import database as core_database
from core import music_state as core_music_state
from utils import helpers as utils_helpers
from visuals import panels as visuals_panels


def _playlist_track_data(track: dict) -> dict:
    return {
        "title": str(track.get("title") or "Untitled")[:200],
        "webpage_url": str(track.get("webpage_url") or ""),
        "artist": str(track.get("artist") or "")[:120],
        "duration": track.get("duration") if isinstance(track.get("duration"), (int, float)) else None,
        "source": str(track.get("source") or "SoundCloud")[:40],
        "thumbnail": str(track.get("thumbnail") or "")[:1000],
    }


async def _save_music_playlist(guild_id: int, owner_id: int, name: str, tracks: list[dict]) -> bool:
    safe_name = name.strip()[:80]
    safe_tracks = [_playlist_track_data(track) for track in tracks[:10]]
    if not safe_name or not safe_tracks or any(not track["webpage_url"] for track in safe_tracks):
        raise ValueError("Укажи название и хотя бы один найденный трек.")

    playlist = {"name": safe_name, "tracks": safe_tracks}
    if core_database.db_pool is None:
        core_music_state.MUSIC_PLAYLISTS[(guild_id, owner_id)][safe_name.casefold()] = playlist
        return False

    try:
        async with core_database.db_pool.connection() as connection:
            await connection.execute(
                """INSERT INTO public.music_playlists (guild_id, owner_id, name, tracks)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (guild_id, owner_id, name) DO UPDATE SET
                       tracks = EXCLUDED.tracks,
                       updated_at = now()""",
                (guild_id, owner_id, safe_name, Jsonb(safe_tracks)),
            )
        core_music_state.MUSIC_PLAYLISTS[(guild_id, owner_id)][safe_name.casefold()] = playlist
        return True
    except Exception as error:
        print(f"[Music playlists] Save failed for guild {guild_id}: {error!r}", flush=True)
        core_music_state.MUSIC_PLAYLISTS[(guild_id, owner_id)][safe_name.casefold()] = playlist
        return False


async def _load_music_playlists(guild_id: int, owner_id: int) -> list[dict]:
    if core_database.db_pool is not None:
        try:
            async with core_database.db_pool.connection() as connection:
                cursor = await connection.execute(
                    """SELECT name, tracks FROM public.music_playlists
                       WHERE guild_id = %s AND owner_id = %s
                       ORDER BY updated_at DESC, name ASC LIMIT 25""",
                    (guild_id, owner_id),
                )
                rows = await cursor.fetchall()
            return [
                {"name": name, "tracks": tracks if isinstance(tracks, list) else []}
                for name, tracks in rows
            ]
        except Exception as error:
            print(f"[Music playlists] Load failed for guild {guild_id}: {error!r}", flush=True)
    return list(core_music_state.MUSIC_PLAYLISTS[(guild_id, owner_id)].values())[:25]


class MusicPlaylistNameModal(discord.ui.Modal, title="◆ Vivian // PLAYLIST"):
    playlist_name = discord.ui.TextInput(
        label="Название плейлиста",
        placeholder="Например, Ночной маршрут",
        max_length=80,
    )

    def __init__(self, search_view: MusicSearchView):
        super().__init__()
        self.search_view = search_view
        self.tracks = search_view.selected_tracks()

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.search_view.owner_id:
            return await utils_helpers.vivian_reply(interaction, "Эта панель связана с другим участником.", error=True, ephemeral=True)
        if interaction.guild is None:
            return await utils_helpers.vivian_reply(interaction, "Плейлисты доступны только на сервере.", error=True, ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        name = self.playlist_name.value.strip()
        try:
            persisted = await _save_music_playlist(
                interaction.guild.id,
                interaction.user.id,
                name,
                self.tracks,
            )
        except ValueError as error:
            return await utils_helpers.vivian_reply(interaction, str(error), title="◆ Vivian // ERROR", error=True, ephemeral=True)
        storage_note = "сохранён" if persisted else "сохранён в памяти до перезапуска"
        response = f"Плейлист **{name}** {storage_note} · треков: {len(self.tracks)}."
        await utils_helpers.vivian_reply(interaction, response, title="◆ Vivian // PLAYLIST", ephemeral=True)
        # The confirmation above is private; keep a personal playlist name off public search.
        self.search_view.notice = f"Плейлист {storage_note} · треков: {len(self.tracks)}."
        if self.search_view.panel_message is not None and not self.search_view.is_finished():
            try:
                await v2.edit_message(self.search_view.panel_message,
                    embed=self.search_view.make_embed(),
                    view=self.search_view,
                )
            except discord.HTTPException:
                pass


class MusicPlaylistSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=playlist["name"][:100],
                value=str(index),
                description=f"Треков: {len(playlist.get('tracks', []))}"[:100],
            )
            for index, playlist in enumerate(parent_view.playlists[:25])
        ]
        super().__init__(
            placeholder="Выбери плейлист",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.parent_view
        if interaction.user.id != view.owner_id:
            return await utils_helpers.vivian_reply(interaction, "Эта панель связана с другим участником.", error=True, ephemeral=True)
        view.selected_index = int(self.values[0])
        view.queue_playlist.disabled = False
        view.notice = None
        view.notice_error = False
        await v2.edit_response(interaction, embed=view.make_embed(), view=view)


class MusicPlaylistView(v2.ControllerView):
    def __init__(self, search_view: MusicSearchView, playlists: list[dict]):
        super().__init__(timeout=300)
        self.guild_id = search_view.guild_id
        self.owner_id = search_view.owner_id
        # Copy search data, never reuse a public/expired controller in this private panel.
        self.search_query = search_view.query
        self.search_tracks = [dict(track) for track in search_view.tracks]
        self.search_selected_indices = list(search_view.selected_indices)
        self.search_page = search_view.page
        self.search_notice = search_view.notice
        self.search_notice_error = search_view.notice_error
        self.visual_enabled = False
        self.playlists = playlists[:25]
        self.selected_index: int | None = None
        self.notice: str | None = None
        self.notice_error = False
        self.panel_message = None
        if self.playlists:
            self.add_item(MusicPlaylistSelect(self))
        else:
            self.queue_playlist.disabled = True

    def make_embed(self) -> discord.Embed:
        if self.playlists:
            lines = [
                f"**{index}. {playlist['name'][:80]}** · {len(playlist.get('tracks', []))} трек(ов)"
                for index, playlist in enumerate(self.playlists, 1)
            ]
            description = "Выбери сохранённый список и добавь его в очередь:\n\n" + "\n".join(lines)
        else:
            description = "Здесь пока нет сохранённых плейлистов. Выбери треки в поиске и создай первый."
        embed = discord.Embed(
            title="◆ Vivian // ПЛЕЙЛИСТЫ",
            description=description,
            color=core_config.VIVIAN_ERROR if self.notice_error else core_music_state.MUSIC_PANEL_COLOR,
        )
        utils_helpers.vivian_brand(embed)
        if self.notice:
            embed.add_field(name="Состояние", value=self.notice[:1000], inline=False)
        embed.set_footer(text="Списки видны только их автору.  ·  " + core_config.VIVIAN_FOOTER)
        if self.visual_enabled:
            visuals_panels._apply_panel_visual(embed, "music")
        return embed

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.panel_message is not None:
            try:
                await v2.edit_message(self.panel_message,
                    embed=utils_helpers.vivian_embed(
                        "Время выбора истекло. Открой новый поиск командой `/play`.",
                        title="◆ Vivian // ПЛЕЙЛИСТЫ ЗАКРЫТЫ",
                    ),
                    view=None,
                    attachments=[],
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="В очередь", style=discord.ButtonStyle.primary, row=1)
    async def queue_playlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await utils_helpers.vivian_reply(interaction, "Эта панель связана с другим участником.", error=True, ephemeral=True)
        if self.selected_index is None or not 0 <= self.selected_index < len(self.playlists):
            return await utils_helpers.vivian_reply(interaction, "Сначала выбери плейлист.", error=True, ephemeral=True)
        playlist = self.playlists[self.selected_index]
        await interaction.response.defer()
        ok, notice = await commands_music_audio._queue_music_tracks(interaction.guild, interaction.user, playlist["tracks"])
        self.notice = notice
        self.notice_error = not ok
        if ok:
            button.disabled = True
        try:
            await v2.edit_original(interaction, embed=self.make_embed(), view=self)
        except discord.HTTPException:
            return

    @discord.ui.button(label="Назад к поиску", style=discord.ButtonStyle.secondary, row=1)
    async def back_to_search(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await utils_helpers.vivian_reply(interaction, "Эта панель связана с другим участником.", error=True, ephemeral=True)
        search_view = commands_music_select.MusicSearchView(
            self.guild_id,
            self.owner_id,
            self.search_query,
            self.search_tracks,
        )
        search_view.selected_indices = list(self.search_selected_indices)
        search_view.visual_enabled = self.visual_enabled
        search_view.notice = self.search_notice
        search_view.notice_error = self.search_notice_error
        search_view.set_page(self.search_page)
        search_view.enqueue_selected.disabled = not search_view.selected_indices
        search_view.save_playlist.disabled = not search_view.selected_indices
        self.stop()
        await v2.edit_response(interaction, embed=search_view.make_embed(), view=search_view)
        search_view.panel_message = interaction.message

    @discord.ui.button(label="Закрыть", style=discord.ButtonStyle.secondary, row=1)
    async def close_playlists(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await utils_helpers.vivian_reply(interaction, "Эта панель связана с другим участником.", error=True, ephemeral=True)
        self.stop()
        await v2.edit_response(interaction,
            embed=utils_helpers.vivian_embed("Панель закрыта.", title="◆ Vivian // ЗАКРЫТО"),
            view=None,
            attachments=[],
        )
