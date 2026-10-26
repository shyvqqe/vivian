from __future__ import annotations
from collections import defaultdict
from collections import deque
from core import config as core_config


loop_status = defaultdict(bool)


MUSIC_QUEUES = defaultdict(deque)


MUSIC_NOW_PLAYING = {}


MUSIC_SOURCES = {}


MUSIC_STARTING = set()


MUSIC_GENERATION = defaultdict(int)


MUSIC_STARTING_GENERATIONS = {}


MUSIC_CONNECT_LOCKS = {}


MUSIC_CHANNELS = {}


MUSIC_RECOVERY_TASKS = {}


MUSIC_SKIP_PENDING = set()


MUSIC_VOLUME = defaultdict(lambda: 100)


MUSIC_SHUFFLE_MODE = defaultdict(bool)


MUSIC_TRACK_STARTED_AT = {}


MUSIC_TRACK_ELAPSED = defaultdict(float)


MUSIC_PLAYER_MESSAGES = {}


MUSIC_PLAYER_VIEWS = {}


MUSIC_PLAYER_REFRESH_TASK = None


MAX_MUSIC_VOLUME = 120


MUSIC_PLAYLISTS = defaultdict(dict)


FFMPEG_EXECUTABLE = None


MUSIC_PANEL_COLOR = core_config.VIVIAN_ACCENT
