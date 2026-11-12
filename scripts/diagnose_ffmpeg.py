"""Verify actual FFmpeg audio conversion without a Discord token or YouTube."""

from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.ffmpeg_runtime import find_ffmpeg, FFmpegUnavailable


def main() -> int:
    try:
        executable = find_ffmpeg()
    except FFmpegUnavailable as exc:
        print("FAIL:", exc)
        return 1
    print("FFmpeg:", executable)
    try:
        result = subprocess.run(
            [executable, "-hide_banner", "-nostdin", "-loglevel", "error",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
             "-t", "0.2", "-ac", "2", "-f", "s16le", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("FAIL: не удалось выполнить проверку аудио:", type(exc).__name__)
        return 1
    if result.returncode != 0 or not result.stdout:
        print("FAIL: FFmpeg запустился, но не выдал PCM-аудио.")
        print(result.stderr.decode("utf-8", "replace")[:400])
        return 1
    print("OK: FFmpeg создаёт PCM-аудио для Discord Voice.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
