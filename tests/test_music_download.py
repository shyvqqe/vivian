"""Offline contracts for /music discovery, MP3 delivery and failure handling."""
from __future__ import annotations

import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
import httpx

from commands import music_download
from commands import music_download_catalog as catalog


_MPEG = b"\xff\xfb\x90\x00" + b"\x00" * 413
_TRACK = catalog.Track("123", "Название", "Исполнитель", 160, True)


class ResponseBody(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_both_release_types_and_checks_author_permission(self):
        def search_response(request):
            self.assertEqual(str(request.url).split("?")[0], catalog.API)
            self.assertEqual(request.url.params["search"], "artist & track")
            self.assertEqual(request.url.params["type"], "single albumtrack")
            row = {"id": "123", "name": "First", "artist_name": "Band", "duration": 160,
                   "license_ccurl": "http://creativecommons.org/licenses/by-nc/4.0/",
                   "audiodownload_allowed": True}
            return httpx.Response(200, json={"headers": {"status": "success"}, "results": [
                row, row, dict(row, id=124, audiodownload_allowed="true"),
                dict(row, id="../3"), dict(row, id=125, audiodownload_allowed=False),
            ]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(search_response)) as client:
            tracks = await catalog.search(client, "example-id", "artist & track")
        self.assertEqual([(track.id, track.downloadable) for track in tracks], [("123", True), ("124", False), ("125", False)])
        self.assertEqual(tracks[0].license_url, "https://creativecommons.org/licenses/by-nc/4.0/")

    async def test_api_error_does_not_expose_raw_provider_error(self):
        payload = {"headers": {"status": "failed", "error_message": "example-id"}, "results": []}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
            with self.assertRaises(catalog.CatalogError) as context, self.assertLogs("httpx", level="INFO") as logs:
                await catalog.search(client, "example-id", "track")
        self.assertNotIn("example-id", str(context.exception))
        self.assertNotIn("example-id", "\n".join(logs.output))

    async def test_download_rechecks_permission_and_falls_back_to_smaller_mp3(self):
        requests = []
        tagged = b"ID3\x04\x00\x00\x00\x00\x00\x03tag" + _MPEG

        def fetch(request):
            requests.append(request.url)
            if request.url.host == "api.jamendo.com":
                self.assertEqual(request.url.params["id"], "123")
                self.assertEqual(request.url.params["action"], "download")
                quality = request.url.params["audioformat"]
                return httpx.Response(302, headers={"Location": f"https://prod-1.storage.jamendo.com/{quality}/"})
            self.assertEqual(request.url.query, b"")
            if request.url.path == "/mp32/":
                return httpx.Response(200, headers={"Content-Length": "1000"}, stream=ResponseBody(b""))
            return httpx.Response(200, headers={"Content-Type": "text/html"}, stream=ResponseBody(tagged))

        async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
            with await catalog.download(client, "example-id", _TRACK, 600) as attachment:
                self.assertEqual(attachment.read(), tagged)
        self.assertEqual(len(requests), 4)

    async def test_download_refuses_permissionless_track_and_unsafe_redirect(self):
        requests = []

        def fetch(request):
            requests.append(request.url)
            return httpx.Response(302, headers={"Location": "https://prod-1.storage.jamendo.com.evil.test/file"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
            with self.assertRaises(catalog.CatalogError):
                await catalog.download(client, "example-id", catalog.Track("1", "A", "B", 0, False), 600)
            self.assertEqual(requests, [])
            with self.assertRaises(catalog.CatalogError):
                await catalog.download(client, "example-id", _TRACK, 600)
        self.assertEqual(len(requests), 1)

    async def test_download_rejects_removed_non_mp3_and_oversize_responses(self):
        cases = (
            (lambda _: httpx.Response(404), catalog.CatalogError, "больше"),
            (lambda _: httpx.Response(200, stream=ResponseBody(b"<html>unavailable</html>")), catalog.CatalogError, "MP3"),
            (lambda _: httpx.Response(200, stream=ResponseBody(b"ID3\x04" + b"\x00" * 6)), catalog.CatalogError, "MP3"),
            (lambda _: httpx.Response(200, stream=ResponseBody(_MPEG * 3)), catalog.FileTooLarge, "лимит"),
        )
        for handler, error, expected in cases:
            with self.subTest(expected=expected):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaisesRegex(error, expected):
                        await catalog.download(client, "example-id", _TRACK, 800)


class DownloadViewTests(unittest.IsolatedAsyncioTestCase):
    def test_mp3_menu_retains_result_numbers_and_excludes_link_only_tracks(self):
        linked = catalog.Track("500", "Another", "Band", 0, False)
        view = music_download.MusicDownloadView(1, [linked, _TRACK])
        self.assertEqual(view.selector.max_values, 1)
        self.assertEqual([(option.label, option.value) for option in view.selector.options],
                         [("2. Исполнитель — Название", "123")])

    async def test_one_failure_keeps_other_selected_tracks_deliverable(self):
        broken = catalog.Track("100", "Broken", "Band", 0, True)
        view = music_download.MusicDownloadView(1, [broken, _TRACK])
        view.selector._values = ["100", "123"]
        ack = AsyncMock(return_value=None)
        edit = AsyncMock(return_value=None)
        followup = AsyncMock(return_value=None)
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=ack, type=None), edit_original_response=edit,
            followup=SimpleNamespace(send=followup), filesize_limit=1000, _state=None,
        )
        data = io.BytesIO(_MPEG)
        with (patch.object(catalog, "client_id", return_value="example-id"),
              patch.object(catalog, "download", new=AsyncMock(side_effect=[catalog.FileTooLarge("too large"), data]))):
            await view._download_selected(interaction)
        ack.assert_awaited_once()
        self.assertTrue(ack.await_args.kwargs["ephemeral"])
        status = ack.await_args.kwargs["view"]
        self.assertTrue(status.has_components_v2())
        self.assertIn("Готовлю MP3…", " ".join(item.content for item in status.walk_children()
                                             if isinstance(item, discord.ui.TextDisplay)))
        followup.assert_awaited_once()
        payload = followup.await_args.kwargs
        attached = payload.get("file") or payload["files"][0]
        self.assertTrue(attached.filename.endswith(".mp3"))
        self.assertTrue(payload["ephemeral"])
        self.assertIn(f"attachment://{attached.filename}",
                      [item.media.url for item in payload["view"].walk_children() if isinstance(item, discord.ui.File)])
        result = " ".join(item.content for item in edit.await_args.kwargs["view"].walk_children()
                          if isinstance(item, discord.ui.TextDisplay))
        self.assertIn("1 из 2", result)
        self.assertIn("лимит", result)
        self.assertEqual(view.delivered, {"123"})
        self.assertTrue(data.closed)
