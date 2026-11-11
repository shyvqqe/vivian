"""Retain bounded auxiliary work while a direct conversation is being answered."""
from __future__ import annotations
import asyncio
import time


OBSERVER_TASK_LIMIT = 32
_TASKS: set[asyncio.Task] = set()
# The shared Postgres pool has three connections. Auxiliary writes may use at
# most two concurrently, leaving capacity for direct conversation memory.
_OBSERVER_SLOTS = asyncio.Semaphore(2)


async def run_observer(label, message_id, operation, timings):
    started = time.monotonic()
    try:
        async with _OBSERVER_SLOTS:
            await operation()
    except Exception as error:
        print(
            f"[Message Observer] Failed; message_id={message_id}; "
            f"observer={label}; error={type(error).__name__}", flush=True,
        )
    finally:
        timings[label] = (time.monotonic() - started) * 1000


def _finished(task):
    _TASKS.discard(task)
    # The gateway handler may be cancelled during shutdown. Keep a strong
    # reference until completion and always retrieve the observer's exception.
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def start_observer(label, message_id, operation, timings):
    """Return None at capacity: the caller must await that work inline."""
    if len(_TASKS) >= OBSERVER_TASK_LIMIT:
        return None
    task = asyncio.create_task(
        run_observer(label, message_id, operation, timings),
        name=f"message-observer:{label}:{message_id}",
    )
    _TASKS.add(task)
    task.add_done_callback(_finished)
    return task


async def finish_observers(tasks):
    if tasks:
        # A cancelled message handler must not cancel an in-flight DB commit
        # or Discord lookup. The bounded registry owns them until completion.
        await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
