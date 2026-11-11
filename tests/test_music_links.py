"""Offline regression tests for supported music links and safe lookup behavior."""
import json
import unittest
from unittest.mock import patch

from commands import music_links, music_relay


class MusicLinksTests(unittest.TestCase):
    def test_normal_queries(self):
        self.assertEqual(music_links.parse_music_request("a-ha take on me"),
                         ("a-ha take on me", "youtube", None))
        self.assertEqual(music_links.parse_music_request("soundcloud artist song"),
                         ("artist song", "soundcloud", None))
        self.assertEqual(music_links.parse_music_request("vk Artist - Song"),
                         ("Artist - Song", "youtube", "VK Музыка"))
        self.assertEqual(music_links.parse_music_request("spotify Artist - Song"),
                         ("Artist - Song", "youtube", "Spotify"))

    def test_youtube_url(self):
        self.assertEqual(music_links.parse_music_request("https://youtu.be/ABCDEFGHI12"),
                         ("https://youtu.be/ABCDEFGHI12", "youtube", None))

    def test_spotify_oembed(self):
        with patch.object(music_links, "_fetch", return_value=json.dumps({"title": "A Nice Song"}).encode()):
            self.assertEqual(
                music_links.parse_music_request("https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp?si=abc"),
                ("A Nice Song", "youtube", "Spotify"),
            )

    def test_apple_music_track_lookup(self):
        result = {"results": [{"kind": "song", "trackId": 123456789, "artistName": "An Artist", "trackName": "Song"}]}
        with patch.object(music_links, "_fetch", return_value=json.dumps(result).encode()):
            self.assertEqual(
                music_links.parse_music_request("https://music.apple.com/us/album/some-album/123456789?i=123456789"),
                ("An Artist Song", "youtube", "Apple Music"),
            )

    def test_yandex_public_metadata(self):
        meta = b'<meta property="og:title" content="Singer - Music | Yandex Music">'
        with patch.object(music_links, "_fetch", return_value=meta):
            search, source, origin = music_links.parse_music_request("https://music.yandex.ru/album/123/track/456")
            self.assertEqual(search, "Singer - Music")
            self.assertEqual(source, "youtube")
            self.assertEqual(origin, "Яндекс Музыка")

    def test_vk_public_metadata(self):
        meta = '<meta property="og:title" content="Исполнитель — Песня — VK Музыка">'.encode()
        with patch.object(music_links, "_fetch", return_value=meta):
            search, source, origin = music_links.parse_music_request("https://vk.com/audio-123_456")
            self.assertEqual(search, "Исполнитель — Песня")
            self.assertEqual(source, "youtube")
            self.assertEqual(origin, "VK Музыка")

    def test_unsupported_or_spoofed_links_blocked(self):
        for url in (
            "http://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp",
            "https://open.spotify.com.evil.test/track/3n3Ppam7vgaVa1iaRUc9Lp",
            "https://127.0.0.1/audio",
            "https://open.spotify.com/playlist/3n3Ppam7vgaVa1iaRUc9Lp",
            "https://music.yandex.ru/album/123",
            "https://vk.com/club12345",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    music_links.parse_music_request(url)

    def test_matched_audio_does_not_pretend_to_stream_spotify(self):
        with patch.object(music_relay, "_relay_search", return_value=[{
            "title": "Demo", "source": "YouTube", "webpage_url": "https://youtu.be/ABCDEFGHI12"
        }]) as search:
            tracks = music_links.find_tracks("spotify Demo")
        self.assertEqual(tracks[0]["source"], "YouTube")
        self.assertEqual(tracks[0]["requested_from"], "Spotify")
        search.assert_called_once_with("Demo", "youtube", 1)

    def test_playlist_url_is_never_passed_as_a_track(self):
        bad = music_relay._safe_track({
            "title": "Playlist", "webpage_url": "https://www.youtube.com/playlist?list=PL12345",
        }, "youtube")
        self.assertIsNone(bad)
        good = music_relay._safe_track({
            "title": "Single", "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "id": "dQw4w9WgXcQ",
        }, "youtube")
        self.assertIsNotNone(good)
        self.assertEqual(good["source"], "YouTube")

    def test_metadata_search_is_cached_and_defensively_copied(self):
        music_relay._cached_music_search.cache_clear()
        track = {"title": "Demo", "source": "YouTube", "webpage_url": "https://youtu.be/ABCDEFGHI12"}
        with patch.object(music_relay, "_relay_search_uncached", return_value=[track]) as search:
            first = music_relay._relay_search("Demo unique test", "youtube", 1)
            first[0]["title"] = "tampered"
            second = music_relay._relay_search("Demo unique test", "youtube", 1)
        self.assertEqual(second[0]["title"], "Demo")
        search.assert_called_once()
        music_relay._cached_music_search.cache_clear()


if __name__ == "__main__":
    unittest.main()
