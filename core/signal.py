from __future__ import annotations
import time
from core import veyr_state as core_veyr_state


def record_chronicle(guild_id: int, kind: str, detail: str, visible_at: float = 0.0) -> None:
    if kind not in {"DISCOVERY", "FIRST", "RARE", "LEGENDARY", "MYTHIC", "ANOMALY"}:
        return
    records = core_veyr_state.CHRONICLE[guild_id]
    records.append({"at": time.time(), "kind": kind, "detail": detail[:240], "visible_at": visible_at})
    if len(records) > core_veyr_state.CHRONICLE_LIMIT:
        del records[:-core_veyr_state.CHRONICLE_LIMIT]


def discover_layer(guild_id: int, section: str) -> None:
    if section in core_veyr_state.DISCOVERED_LAYERS[guild_id]:
        return
    core_veyr_state.DISCOVERED_LAYERS[guild_id].add(section)
    record_chronicle(guild_id, "DISCOVERY", f"{section} layer disclosed")


def _recent_echo_count(guild_id: int, now: float, window_seconds: int) -> int:
    return sum(1 for item in core_veyr_state.ECHO_MEMORY[guild_id] if now - item["at"] <= window_seconds)


def _change_server_state(guild_id: int, state: str, source: str) -> bool:
    previous = core_veyr_state.SERVER_STATE[guild_id]
    if previous == state:
        return False
    core_veyr_state.SERVER_STATE[guild_id] = state
    store_echo(guild_id, "state_change", f"{previous} → {state} ({source})")
    if guild_id not in core_veyr_state.STATE_CHANGED_BEFORE:
        core_veyr_state.STATE_CHANGED_BEFORE.add(guild_id)
        record_chronicle(guild_id, "FIRST", f"First state transition: {previous} → {state}")
    return True


def evaluate_anomaly(guild_id: int, now=None) -> bool:
    """Deterministic anomaly gate: state, signal band, UTC window, echo density, cooldown."""
    now = time.time() if now is None else now
    state = core_veyr_state.SERVER_STATE[guild_id]
    value = core_veyr_state.SERVER_SIGNAL[guild_id]["value"]
    utc_minute = time.gmtime(now).tm_min
    if state not in {"ECHO", "UNSTABLE"}:
        return False
    if not 18.0 <= value <= 80.0 or utc_minute not in {0, 1}:
        return False
    if _recent_echo_count(guild_id, now, 20 * 60) < 4:
        return False
    if now - core_veyr_state.ANOMALY_LAST_TRIGGER[guild_id] < 6 * 60 * 60:
        return False

    core_veyr_state.ANOMALY_LAST_TRIGGER[guild_id] = now
    hidden = state == "ECHO" or value < 35.0
    visible_at = now + 24 * 60 * 60 if hidden else 0.0
    record_chronicle(
        guild_id,
        "ANOMALY",
        f"Signal {value:.2f}; state {state}; echo density {_recent_echo_count(guild_id, now, 1200)}",
        visible_at=visible_at,
    )
    store_echo(guild_id, "anomaly", f"Anomaly registered ({'internal' if hidden else 'surface'})")
    if not hidden:
        _change_server_state(guild_id, "ANOMALY", "anomaly engine")
        core_veyr_state.ANOMALY_PUBLIC_NOTICE[guild_id] = now + 60 * 60
    return True


def update_server_signal(guild_id: int, activity: float = 0.0) -> float:
    """Apply quiet time drift and a small activity pulse to one server's signal."""
    now = time.time()
    signal = core_veyr_state.SERVER_SIGNAL[guild_id]
    elapsed_hours = max(0.0, now - signal["updated_at"]) / 3600
    # Activity fades toward baseline over time; this internal model is not shown.
    previous = signal["value"]
    value = previous * (0.98 ** elapsed_hours)
    signal["value"] = round(max(0.0, min(100.0, value + activity)), 2)
    signal["updated_at"] = now
    if signal["value"] - previous >= 14.0 and now - core_veyr_state.SIGNAL_RARE_LAST[guild_id] >= 6 * 60 * 60:
        core_veyr_state.SIGNAL_RARE_LAST[guild_id] = now
        record_chronicle(guild_id, "RARE", f"Signal shifted to {signal['value']:.2f}")
    evaluate_anomaly(guild_id, now)
    return signal["value"]


def store_echo(guild_id: int, kind: str, detail: str) -> None:
    echoes = core_veyr_state.ECHO_MEMORY[guild_id]
    echoes.append({"at": time.time(), "kind": kind, "detail": detail[:240]})
    if len(echoes) > core_veyr_state.SIGNAL_MEMORY_LIMIT:
        del echoes[:-core_veyr_state.SIGNAL_MEMORY_LIMIT]


async def set_server_state(guild_id: int, state: str, source: str) -> None:
    if _change_server_state(guild_id, state, source):
        evaluate_anomaly(guild_id)
