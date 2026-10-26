from __future__ import annotations
from collections import defaultdict
from collections import deque
import time


warns = {}


xp_data = defaultdict(lambda: {"xp": 0, "level": 1})


xp_cooldown = {}


RANK_ACTIVITY = defaultdict(lambda: {
    "messages": 0,
    "word_messages": 0,
    "long_messages": 0,
    "characters": 0,
})


DAILY_ACTIVITY = defaultdict(lambda: {"messages": 0, "profanity": 0})


CHAT_PULSE_RECENT = defaultdict(lambda: deque(maxlen=600))


CHAT_PULSE_OBSERVING_SINCE = time.time()


DAILY_SUMMARY_SENT = set()


RECENT_BAN_EVENTS = {}


XP_PER_MESSAGE = 15


XP_COOLDOWN_SECONDS = 60


reaction_roles = {}


chat_history = {}


MAX_HISTORY = 120


AI_DIALOGUE_LOCKS = {}
