"""Commit candidate layouts only after Discord accepts the edit."""
from __future__ import annotations

import asyncio
from weakref import WeakValueDictionary

import discord

from utils.v2_layout import ControllerView, PanelLayout


_locks = WeakValueDictionary()


def edit_lock(message, interaction=None):
    message_id = getattr(message, "id", None)
    key = ("message", message_id) if message_id is not None else (
        "interaction", getattr(interaction, "id", id(interaction)),
    )
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def registered(message, source=None, state=None):
    if message is None:
        return None
    state = state or getattr(message, "_state", None)
    store = getattr(state, "_view_store", None)
    previous = getattr(store, "_synced_message_views", {}).get(message.id)
    if previous is None and getattr(source, "_v2_message_id", None) == message.id:
        previous = getattr(source, "_v2_layout", None)
    return previous


def restore(previous, message, state=None):
    # discord.py removes message tracking before HTTP. Dispatch must stay alive
    # if that HTTP request fails; this does not make another Discord request.
    if previous is None or previous.is_finished() or message is None:
        return
    state = state or getattr(message, "_state", None)
    if state is not None:
        state.store_view(previous, message.id)


def commit(payload, layout, result, *, previous=None, message=None, state=None, attachments=()):
    actual = getattr(result, "resource", result)
    if getattr(actual, "id", None) is None:
        actual = message
    if actual is None:
        return result
    state = state or getattr(actual, "_state", None)
    source = payload.get("view")
    if isinstance(source, ControllerView):
        source._v2_layout = layout
        source._v2_spec = dict(layout.spec)
        source.panel_message = actual
        source._v2_message_id = actual.id
        # Some interaction resources omit uploads. Accepted retained attachment
        # IDs are still valid; an explicit empty list correctly leaves none.
        source._v2_attachments = list(getattr(actual, "attachments", ()) or ()) or [
            item for item in attachments if isinstance(item, discord.Attachment)
        ]
    if isinstance(previous, discord.ui.LayoutView) and previous is not layout:
        # stop() can remove entries that the SDK just registered for the new
        # view. Re-register immediately, synchronously, after retiring the old timer.
        previous.stop()
    if state is not None and not layout.is_finished() and layout.is_dispatchable():
        state.store_view(layout, actual.id)
    return result
