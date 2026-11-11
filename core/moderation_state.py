from __future__ import annotations
import asyncio
from collections import defaultdict
from collections import deque


spam_tracker = defaultdict(deque)


spam_repeat_tracker = defaultdict(deque)


gif_spam_tracker = defaultdict(deque)


photo_burst_last = defaultdict(float)


spam_last_timeout = defaultdict(float)


moderation_warning_at = defaultdict(float)


moderation_action_locks = defaultdict(asyncio.Lock)


ai_moderation_last_check = defaultdict(float)


ai_moderation_guild_calls = defaultdict(deque)


ad_raid_tracker = defaultdict(deque)
