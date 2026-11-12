"""Offline FFmpeg discovery regression tests; run with unittest discover."""
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import ffmpeg_runtime as ff


class FFmpegRuntimeTests(unittest.TestCase):
    def tearDown(self):
        ff.find_ffmpeg.cache_clear()

    def test_available_binary(self):
        self.assertTrue(Path(ff.find_ffmpeg()).is_file())

    def test_configured_binary_takes_priority(self):
        ff.find_ffmpeg.cache_clear()
        with mock.patch.dict(os.environ, {"FFMPEG_PATH": sys.executable}):
            # A python executable is not FFmpeg even though it exists.
            self.assertNotEqual(ff.find_ffmpeg(), sys.executable)

    def test_uses_bundled_when_system_missing(self):
        original_which = shutil.which
        with mock.patch.dict(os.environ, {"FFMPEG_PATH": ""}):
            with mock.patch.object(ff.shutil, "which", side_effect=lambda name: None if name == "ffmpeg" else original_which(name)):
                ff.find_ffmpeg.cache_clear()
                result = ff.find_ffmpeg()
        self.assertTrue(Path(result).is_file())
        self.assertIn("ffmpeg", Path(result).name.lower())

    def test_missing_dependency_raises_helpful_error(self):
        with mock.patch.object(ff, "_candidates", return_value=iter(())):
            with self.assertRaises(ff.FFmpegUnavailable) as err:
                ff.find_ffmpeg()
        self.assertIn("requirements.txt", str(err.exception))

    def test_checks_binary_execution_not_only_presence(self):
        self.assertFalse(ff._is_working(sys.executable))

    def test_cached_binary(self):
        ff.find_ffmpeg.cache_clear()
        first = ff.find_ffmpeg()
        with mock.patch.object(ff, "_candidates", side_effect=AssertionError("unexpected second scan")):
            self.assertEqual(ff.find_ffmpeg(), first)


if __name__ == "__main__":
    unittest.main()
