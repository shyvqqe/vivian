"""Offline contract tests for Grok/Meta-style catalog API and live bot adapters."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from commands import music_catalog, music_links, music_relay


class CatalogTests(unittest.TestCase):
    def tearDown(self):
        music_links._spotify_details.cache_clear()
        music_links._apple_details.cache_clear()
        music_links._yandex_metadata.cache_clear()
        music_links._vk_metadata.cache_clear()
        music_catalog._apple_search.cache_clear()

    def test_prepare_understands_cyrillic_and_latin_prefixes(self):
        samples = {
            "blunt": ("blunt", None),
            "spotify blunt": ("blunt", "spotify"),
            "спотифай blunt": ("blunt", "spotify"),
            "яндекс кислород": ("кислород", "yandex"),
            "вк Кино - Группа крови": ("Кино - Группа крови", "vk"),
            "эпл hello": ("hello", "apple"),
            "ютуб песня": ("песня", "youtube"),
            "саундклауд песня": ("песня", "soundcloud"),
            "https://music.yandex.ru/album/1/track/2":
                ("https://music.yandex.ru/album/1/track/2", "yandex"),
            "https://music.apple.com/ru/album/example/123?i=456":
                ("https://music.apple.com/ru/album/example/123?i=456", "apple"),
        }
        for raw, expected in samples.items():
            with self.subTest(raw=raw):
                self.assertEqual(music_catalog.prepare(raw), expected)

    def test_reject_url_spoofing_and_source_mismatch(self):
        for raw in (
            "https://open.spotify.com.evil.invalid/track/3n3Ppam7vgaVa1iaRUc9Lp",
            "http://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp",
            "vk https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp",
            "https://127.0.0.1/a", "https://example.com/a", "", "spotify ",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                music_catalog.prepare(raw)

    def test_spotify_artist_from_public_oembed_metadata(self):
        target = "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6"
        payload = {"title": "Speak To Me - 2011 Remastered Version", "author_name": "Pink Floyd"}
        with patch.object(music_links, "_fetch", return_value=json.dumps(payload).encode()) as fetch:
            self.assertEqual(music_links.parse_music_request(target),
                             ("Pink Floyd - Speak To Me - 2011 Remastered Version", "youtube", "Spotify"))
            tracks = music_catalog.search(target, "spotify", 1)
            self.assertEqual(tracks[0]["artist"], "Pink Floyd")
            self.assertEqual(tracks[0]["title"], payload["title"])
            self.assertEqual(tracks[0]["relay_query"], "Pink Floyd - " + payload["title"])
            self.assertTrue(tracks[0]["bridged"])
            self.assertIsNone(tracks[0]["stream_url"])
            self.assertEqual(fetch.call_count, 1)  # cached across slash/prefix adapters

    def test_spotify_url_that_has_no_public_artist_is_honest(self):
        target = "https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp"
        with patch.object(music_links, "_fetch", return_value=b'{"title":"Song"}'):
            self.assertEqual(music_catalog.search(target, "spotify")[0]["relay_query"], "Song")

    def test_apple_song_id_lookup_includes_artist(self):
        url = "https://music.apple.com/ru/album/example/123456789?i=123456789"
        record = {"results": [{"kind": "song", "trackId": 123456789,
                               "artistName": "Кино", "trackName": "Группа крови"}]}
        with patch.object(music_links, "_fetch", return_value=json.dumps(record).encode()):
            track = music_catalog.search(url, "apple", 2)[0]
            self.assertEqual((track["title"], track["artist"]), ("Группа крови", "Кино"))
            self.assertEqual(track["relay_query"], "Кино - Группа крови")

    def test_apple_public_text_search_deduplicates_and_enriches_prefix(self):
        record = {"results": [
            {"kind": "song", "artistName": "Кино", "trackName": "Группа крови", "trackViewUrl": "https://music.apple.com/ru/song/123"},
            {"kind": "song", "artistName": "Кино", "trackName": "Группа крови"},
            {"kind": "song", "artistName": "Земфира", "trackName": "Искала"},
        ]}
        with patch.object(music_catalog, "_json", return_value=record) as api:
            self.assertEqual(len(music_catalog.search("кино", "apple", 10)), 2)
            self.assertEqual(music_links.resolve_music_request("apple кино"),
                             ("Кино - Группа крови", "youtube", "Apple Music"))
            self.assertEqual(api.call_count, 2)  # different query/limit cache keys

    def test_apple_service_failure_uses_search_text_without_fake_metadata(self):
        with patch.object(music_catalog, "_json", side_effect=OSError("unavailable")):
            self.assertEqual(music_links.resolve_music_request("apple артист песня"),
                             ("артист песня", "youtube", "Apple Music"))

    def test_catalog_urls_require_real_metadata(self):
        with patch.object(music_links, "_fetch", return_value="<html><title>Яндекс Музыка</title></html>".encode()):
            with self.assertRaises(ValueError):
                music_catalog.search("https://music.yandex.ru/album/10/track/20", "yandex")
        with patch.object(music_links, "_fetch", return_value=b"<html><title>VK</title></html>"):
            with self.assertRaises(ValueError):
                music_catalog.search("https://vk.com/audio-123_456", "vk")

    def test_bridged_queries_are_not_direct_audio(self):
        for source in ("youtube", "soundcloud", "spotify", "vk", "yandex"):
            with self.subTest(source=source):
                track = music_catalog.search("artist song", source, 3)[0]
                self.assertIsNone(track["stream_url"])
                self.assertEqual(track["relay_query"], "artist song")
                self.assertTrue(track["bridged"])

    def test_apple_enriched_request_uses_youtube_backend(self):
        record = {"results": [{"kind":"song", "artistName":"Кино", "trackName":"Группа крови"}]}
        with patch.object(music_catalog, "_json", return_value=record), patch.object(
            music_relay, "_relay_search", return_value=[{"title":"Demo", "source":"YouTube"}]
        ) as search:
            found = music_links.find_tracks("apple кино", 1)
            search.assert_called_once_with("Кино - Группа крови", "youtube", 1)
            self.assertEqual(found[0]["requested_from"], "Apple Music")


if __name__ == "__main__":
    unittest.main()
