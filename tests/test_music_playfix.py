"""No-network regression tests for the fast /play search pipeline."""
import json
import unittest
from unittest.mock import patch

from commands import music_relay


class _HTTPResponse:
    headers = {"Content-Type": "application/json; charset=UTF-8"}

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, amount):
        return self.payload[:amount]


class MusicPlayFixTests(unittest.TestCase):
    def setUp(self):
        music_relay._cached_music_search.cache_clear()

    def tearDown(self):
        music_relay._cached_music_search.cache_clear()

    def test_public_youtube_search_parses_song_and_duration(self):
        payload = {"contents": {"sectionListRenderer": {"contents": [{"videoRenderer": {
            "videoId": "xKpzH5bxYsk",
            "title": {"runs": [{"text": "Виктор Цой - Группа Крови"}]},
            "ownerText": {"runs": [{"text": "КИНО"}]},
            "lengthText": {"simpleText": "4:45"},
            "thumbnail": {"thumbnails": [{"url": "https://i.ytimg.com/vi/xKpzH5bxYsk/default.jpg"}]},
        }}]}}}
        with patch.object(music_relay, "urlopen", return_value=_HTTPResponse(payload)) as request:
            tracks = music_relay._public_youtube_search("кино группа крови", 1)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["title"], "Виктор Цой - Группа Крови")
        self.assertEqual(tracks[0]["artist"], "КИНО")
        self.assertEqual(tracks[0]["duration"], 285)
        self.assertEqual(tracks[0]["webpage_url"], "https://www.youtube.com/watch?v=xKpzH5bxYsk")
        self.assertEqual(tracks[0]["backend"], "local")
        self.assertEqual(request.call_args.kwargs["timeout"], 4)

    def test_public_search_ignores_invalid_videos(self):
        payload = {"contents": [{"videoRenderer": {"videoId": "invalid", "title": {"simpleText": "Not a video"}}},
                                {"channelRenderer": {"title": {"simpleText": "Channel"}}}]}
        with patch.object(music_relay, "urlopen", return_value=_HTTPResponse(payload)):
            self.assertEqual(music_relay._public_youtube_search("channel", 1), [])

    def test_direct_youtube_link_never_triggers_network_search(self):
        url = "https://youtu.be/dQw4w9WgXcQ?t=15"
        with patch.object(music_relay, "_public_youtube_search") as public, patch.object(
                music_relay, "_local_search") as local, patch.object(
                music_relay, "_relay_credentials", return_value=None):
            found = music_relay._relay_search(url, "youtube", 1)
        self.assertEqual(found[0]["webpage_url"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(found[0]["relay_query"], found[0]["webpage_url"])
        public.assert_not_called()
        local.assert_not_called()

    def test_public_search_success_skips_slow_ytdlp_search(self):
        sample = {"title": "Test track", "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "backend": "local"}
        with patch.object(music_relay, "_public_youtube_search", return_value=[sample]), patch.object(
                music_relay, "_local_search") as local, patch.object(
                music_relay, "_relay_credentials", return_value=None):
            self.assertEqual(music_relay._relay_search("test track", "youtube", 1), [sample])
        local.assert_not_called()

    def test_fast_lookup_failure_falls_back_to_yt_dlp(self):
        sample = {"title": "Fallback", "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
        with patch.object(music_relay, "_public_youtube_search", side_effect=TimeoutError), patch.object(
                music_relay, "_local_search", return_value=[sample]) as local, patch.object(
                music_relay, "_relay_credentials", return_value=None):
            found = music_relay._relay_search("some song", "youtube", 1)
        self.assertEqual(found, [sample])
        local.assert_called_once_with("some song", "youtube", 1)

    def test_missing_results_are_not_cached_for_90_seconds(self):
        with patch.object(music_relay, "_relay_search_uncached", return_value=[]) as backend:
            self.assertEqual(music_relay._relay_search("no hit", "youtube", 1), [])
            self.assertEqual(music_relay._relay_search("no hit", "youtube", 1), [])
        self.assertEqual(backend.call_count, 2)

    def test_rejected_playlist_or_untrusted_link(self):
        for query in ("https://youtube.com/playlist?list=PLtest", "https://example.org/watch?v=dQw4w9WgXcQ"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                music_relay._relay_search(query, "youtube", 1)

    def test_local_fallback_and_audio_have_shorter_timeouts(self):
        payload = {"entries": [{"id": "xKpzH5bxYsk", "title": "Song"}]}
        with patch.object(music_relay, "_run_ytdlp", return_value=json.dumps(payload).encode()) as runner:
            self.assertEqual(len(music_relay._local_search("song", "youtube", 1)), 1)
        self.assertEqual(runner.call_args.kwargs["timeout"], 12)
        with patch.object(music_relay, "_run_ytdlp", return_value=b"https://media.example/audio\n") as runner:
            self.assertEqual(music_relay._local_audio_url("https://youtu.be/dQw4w9WgXcQ", "youtube"),
                             "https://media.example/audio")
        self.assertEqual(runner.call_args.kwargs["timeout"], 22)


if __name__ == "__main__":
    unittest.main()
