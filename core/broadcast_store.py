"""Durable rundown state for SQLite and PostgreSQL."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import time

from core import feature_storage


MAX_SEGMENTS = 12
MAX_MINUTES_PER_SEGMENT = 120
MAX_LIFETIME = 12 * 3600
_RETENTION = 30 * 86400
_PENDING_SECONDS = 90
_GUILD_OPEN_LIMIT = 8
_CONTROL_INTERVAL = 2


class BroadcastUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class Segment:
    name: str
    planned_seconds: int
    actual_seconds: int | None = None


@dataclass(frozen=True)
class Broadcast:
    id: int
    guild_id: int
    channel_id: int
    host_id: int
    title: str
    segments: tuple[Segment, ...]
    created_at: int
    message_id: int | None = None
    current_index: int = 0
    anchor: int | None = None
    accumulated: int = 0
    finished_at: int | None = None
    last_action_id: int = 0
    updated_at: int = 0

    @property
    def expires_at(self) -> int:
        return self.created_at + MAX_LIFETIME

    def elapsed_at(self, now: int) -> int:
        advancing = max(0, min(now, self.expires_at) - self.anchor) if self.anchor is not None else 0
        return self.accumulated + advancing


def _schema(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS vivian_broadcast_sessions (
            guild_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            created_at BIGINT NOT NULL,
            finished_at BIGINT,
            message_id BIGINT,
            payload TEXT NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS vivian_broadcast_created
        ON vivian_broadcast_sessions (created_at)
    """)


def _load(connection, guild_id, channel_id):
    row = connection.execute(
        "SELECT payload FROM vivian_broadcast_sessions WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row[0])
    payload["segments"] = tuple(Segment(**item) for item in payload["segments"])
    return Broadcast(**payload)


def _save(connection, state):
    connection.execute("""
        INSERT INTO vivian_broadcast_sessions (guild_id, channel_id, created_at, finished_at, message_id, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (guild_id, channel_id) DO UPDATE SET
            created_at = excluded.created_at, finished_at = excluded.finished_at,
            message_id = excluded.message_id, payload = excluded.payload
    """, (state.guild_id, state.channel_id, state.created_at, state.finished_at, state.message_id,
          json.dumps(asdict(state), ensure_ascii=False, separators=(",", ":"))))
    return state


def segment(name: str, minutes: int) -> Segment:
    cleaned = " ".join(name.split())
    if not 1 <= len(cleaned) <= 90:
        raise BroadcastUnavailable("Укажи название сегмента длиной от 1 до 90 символов.")
    if not 1 <= minutes <= MAX_MINUTES_PER_SEGMENT:
        raise BroadcastUnavailable(f"На сегмент можно выделить от 1 до {MAX_MINUTES_PER_SEGMENT} минут.")
    return Segment(cleaned, minutes * 60)


def _validate_plan(segments):
    if not 1 <= len(segments) <= MAX_SEGMENTS:
        raise BroadcastUnavailable(f"В плане должно быть от 1 до {MAX_SEGMENTS} сегментов.")
    if sum(item.planned_seconds for item in segments) > MAX_LIFETIME:
        raise BroadcastUnavailable("Общая длительность плана не может превышать 12 часов.")


def _occupies_slot(state, now):
    return (
        state.finished_at is None and now < state.expires_at
        and (state.message_id is not None or now < state.created_at + _PENDING_SECONDS)
    )


async def create(
    broadcast_id: int, guild_id: int, channel_id: int, host_id: int, title: str, segments: tuple[Segment, ...],
    *, now: int | None = None,
) -> Broadcast:
    _validate_plan(segments)

    def write(connection):
        stamp = int(time.time()) if now is None else now
        _schema(connection)
        existing = _load(connection, guild_id, channel_id)
        if existing is not None:
            if existing.id == broadcast_id:
                return existing
            if existing.last_action_id >= broadcast_id:
                raise BroadcastUnavailable("Этот запуск устарел. Используй текущую панель канала.")
            if _occupies_slot(existing, stamp):
                raise BroadcastUnavailable("В этом канале уже идёт эфир. Заверши его через `/broadcast end`.")
        connection.execute("DELETE FROM vivian_broadcast_sessions WHERE created_at <= ?", (stamp - _RETENTION,))
        count = connection.execute("""
            SELECT COUNT(*) FROM vivian_broadcast_sessions
            WHERE guild_id = ? AND finished_at IS NULL AND created_at > ?
              AND (message_id IS NOT NULL OR created_at > ?)
        """, (guild_id, stamp - MAX_LIFETIME, stamp - _PENDING_SECONDS)).fetchone()[0]
        if count >= _GUILD_OPEN_LIMIT:
            raise BroadcastUnavailable("На сервере уже открыто 8 эфиров.")
        return _save(connection, Broadcast(
            broadcast_id, guild_id, channel_id, host_id, title, segments, stamp,
            anchor=stamp, last_action_id=broadcast_id, updated_at=stamp,
        ))

    return await feature_storage.run(write)


async def bind_message(guild_id: int, channel_id: int, broadcast_id: int, message_id: int) -> Broadcast:
    def write(connection):
        _schema(connection)
        state = _load(connection, guild_id, channel_id)
        if state is None or state.id != broadcast_id or state.message_id not in (None, message_id):
            raise BroadcastUnavailable("Эта панель относится к предыдущему эфиру.")
        return _save(connection, replace(state, message_id=message_id))

    return await feature_storage.run(write)


def _prepare(state, action, next_segment, now):
    if action == "segment":
        if next_segment is None:
            raise ValueError("segment has no data")
        segments = (*state.segments, next_segment)
        _validate_plan(segments)
        return replace(state, segments=segments)
    if action == "pause":
        if state.anchor is None:
            return replace(state, anchor=now)
        return replace(state, anchor=None, accumulated=state.elapsed_at(now))
    if action == "next" and state.current_index + 1 >= len(state.segments):
        raise BroadcastUnavailable("Сейчас последний сегмент. Заверши эфир через `/broadcast end`.")
    current = state.current_index
    closed = replace(state.segments[current], actual_seconds=state.elapsed_at(now))
    segments = (*state.segments[:current], closed, *state.segments[current + 1:])
    if action == "end":
        return replace(state, segments=segments, anchor=None, accumulated=0, finished_at=now)
    return replace(state, segments=segments, current_index=current + 1,
                   anchor=now if state.anchor is not None else None, accumulated=0)


async def change(
    guild_id: int, channel_id: int, actor_id: int, interaction_id: int, action: str, *, manager: bool,
    next_segment: Segment | None = None, board: tuple[int, int] | None = None, now: int | None = None,
) -> Broadcast:
    if action not in {"segment", "pause", "next", "end"}:
        raise ValueError("unknown broadcast action")

    def write(connection):
        _schema(connection)
        state = _load(connection, guild_id, channel_id)
        if state is None:
            raise BroadcastUnavailable("В этом канале нет эфира. Начни его через `/broadcast start`.")
        if actor_id != state.host_id and not manager:
            raise BroadcastUnavailable("Управлять эфиром может ведущий или участник с правом «Управлять событиями».")
        if board is not None and (board[0] != state.id or state.message_id not in (None, board[1])):
            raise BroadcastUnavailable("Эта панель относится к предыдущему эфиру.")
        if state.message_id is None:
            if board is None:
                raise BroadcastUnavailable("Карточка ещё публикуется. Если её нет, повтори запуск через 90 секунд.")
            state = replace(state, message_id=board[1])
        if interaction_id == state.last_action_id or (state.finished_at is not None and action == "end"):
            return state
        if interaction_id < state.last_action_id:
            raise BroadcastUnavailable("Это действие устарело. Посмотри текущую панель эфира.")
        if state.finished_at is not None:
            raise BroadcastUnavailable("Эфир уже завершён. Начни новый через `/broadcast start`.")
        stamp = int(time.time()) if now is None else now
        stamp = max(stamp, state.updated_at)
        if stamp >= state.expires_at:
            if action != "end":
                raise BroadcastUnavailable("Прошло 12 часов с начала. Закрой сводку через `/broadcast end`.")
            stamp = state.expires_at
        elif state.last_action_id != state.id and stamp - state.updated_at < _CONTROL_INTERVAL:
            raise BroadcastUnavailable("Панель обновилась только что. Повтори через пару секунд.")
        updated = _prepare(state, action, next_segment, stamp)
        return _save(connection, replace(updated, last_action_id=interaction_id, updated_at=stamp))

    return await feature_storage.run(write)
