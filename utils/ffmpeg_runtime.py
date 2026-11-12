"""Locate a working FFmpeg binary for Discord voice playback.

Prefer a configured/system FFmpeg; fall back to imageio-ffmpeg's bundled
executable on Python-only hosting where ``apt install ffmpeg`` is unavailable.
Keep this module independent from Discord so it can be tested at deployment.
"""

from __future__ import annotations

from functools import lru_cache
import os
import shutil
import subprocess


class FFmpegUnavailable(RuntimeError):
    """No working FFmpeg executable is available in this process."""


_INSTALL_HINT = (
    "FFmpeg недоступен на хостинге. Установи зависимости из requirements.txt "
    "(включая imageio-ffmpeg), затем полностью пересобери и перезапусти бота. "
    "Если FFmpeg установлен отдельно, укажи путь в FFMPEG_PATH."
)


def _candidates():
    """Use only local executables, not URLs or shell commands."""
    configured = (os.environ.get("FFMPEG_PATH") or "").strip().strip('"')
    if configured:
        yield configured
    system = shutil.which("ffmpeg")
    if system:
        yield system
    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled:
            yield bundled
    except Exception:
        # This optional provider can fail on an unsupported platform.
        pass


def _is_working(candidate: str) -> bool:
    """FFmpeg can exist on disk but fail to execute (missing shared libraries)."""
    path = shutil.which(candidate)
    if path is None or not os.path.isfile(path):
        return False
    try:
        check = subprocess.run(
            [path, "-version"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            timeout=4, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return check.returncode == 0


@lru_cache(maxsize=1)
def find_ffmpeg() -> str:
    """Return the absolute binary path, or a user-actionable error.

    Cache only successful calls; missing dependencies can be installed and
    retried without restarting the Python interpreter in tests/development.
    """
    seen: set[str] = set()
    for candidate in _candidates():
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if _is_working(candidate):
            return os.path.abspath(shutil.which(candidate) or candidate)
    raise FFmpegUnavailable(_INSTALL_HINT)
