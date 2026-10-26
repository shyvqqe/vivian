from __future__ import annotations
from collections import defaultdict
import time


VEYR_STATES = (
    "DORMANT",
    "ACTIVE",
    "UNSTABLE",
    "ECHO",
    "VOID",
    "ANOMALY",
    "RITUAL",
)


SERVER_SIGNAL = defaultdict(lambda: {"value": 0.0, "updated_at": time.time()})


SERVER_STATE = defaultdict(lambda: "DORMANT")


ECHO_MEMORY = defaultdict(list)


SIGNAL_MEMORY_LIMIT = 200


CHRONICLE = defaultdict(list)


CHRONICLE_LIMIT = 100


DISCOVERED_LAYERS = defaultdict(set)


STATE_CHANGED_BEFORE = set()


SIGNAL_RARE_LAST = defaultdict(float)


ANOMALY_LAST_TRIGGER = defaultdict(float)


ANOMALY_PUBLIC_NOTICE = defaultdict(float)


RITUAL_SESSIONS = defaultdict(dict)


RITUAL_LAST_COMPLETE = defaultdict(dict)
