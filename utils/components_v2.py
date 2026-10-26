"""Explicit V2 send/edit boundaries. Ordinary AI/nonce delivery is not intercepted."""
from __future__ import annotations

import discord

from utils import v2_delivery, v2_media
from utils.v2_layout import ControllerView, PanelLayout, layout_for
from utils.v2_content import OVERFLOW_NAME


def _spec(payload, source, message):
    previous = getattr(source, "_v2_spec", None)
    body_provided = any(key in payload for key in ("content", "embed", "embeds"))
    if not body_provided and previous is not None:
        return dict(previous)
    embed = payload.get("embed")
    if embed is None and payload.get("embeds"):
        embed = payload["embeds"][0]
    content = payload.get("content")
    if not body_provided and message is not None:
        if getattr(getattr(message, "flags", None), "components_v2", False):
            return {"message": message}
        content = getattr(message, "content", "")
        embeds = getattr(message, "embeds", ())
        embed = embeds[0] if embeds else None
    return {"content": content, "embed": embed.copy() if embed is not None else None}


def adapt(payload, *, message=None, editing=False):
    result = dict(payload)
    source = result.pop("view", None)
    preserve_card = isinstance(source, discord.ui.LayoutView)
    if isinstance(source, discord.ui.LayoutView):
        layout, extra = source, None
    else:
        spec = _spec(result, source, message)
        preserve_card = "message" in spec
        layout, extra = layout_for(source, spec)
    result.pop("content", None)
    result.pop("embed", None)
    result.pop("embeds", None)
    result["view"] = layout
    result.setdefault("allowed_mentions", discord.AllowedMentions.none())
    if editing:
        # Required when upgrading an old panel. V2 can never be downgraded to embeds.
        result.update(content=None, embeds=[])
        attachments = list(getattr(message, "attachments", ()) or ())
        if (message is not None and getattr(source, "_v2_message_id", None) == message.id):
            by_id = {item.id: item for item in getattr(source, "_v2_attachments", ())}
            by_id.update({item.id: item for item in attachments})
            attachments = list(by_id.values())
        if "attachments" in result or attachments is not None:
            attachments = result.get("attachments", attachments) or []
            result["attachments"] = [item for item in attachments
                                     if preserve_card or getattr(item, "filename", None) != OVERFLOW_NAME]
    if extra is not None:
        if editing:
            attachments = result.get("attachments")
            if attachments is None:
                attachments = getattr(message, "attachments", ())
            result["attachments"] = [item for item in attachments
                                     if getattr(item, "filename", None) != OVERFLOW_NAME] + [extra]
        else:
            files = list(result.pop("files", ()) or ())
            if result.get("file") is not None:
                files.append(result.pop("file"))
            result["files"] = files + [extra]
    media_files = list(result.get("attachments") or ()) + list(result.get("files") or ())
    if result.get("file") is not None:
        media_files.append(result["file"])
    v2_media.normalize(layout, media_files, message=message, source=source)
    return result


def _original_target(interaction):
    # A component may defer a NEW private response. interaction.message still
    # points to the public card in that case; its attachments belong elsewhere.
    cached = getattr(interaction, "extras", {}).get("vivian_v2_original")
    if cached is not None:
        return cached
    if interaction.response.type in (
            discord.InteractionResponseType.deferred_channel_message,
            discord.InteractionResponseType.channel_message):
        return getattr(interaction, "_original_response", None)
    return getattr(interaction, "message", None)


def _remember_original(interaction, result):
    message = getattr(result, "resource", result)
    if getattr(message, "id", None) is not None:
        interaction.extras["vivian_v2_original"] = message


async def edit_original(interaction, **payload):
    target = _original_target(interaction)
    async with v2_delivery.edit_lock(target, interaction):
        target = _original_target(interaction)
        state = interaction._state
        previous = v2_delivery.registered(target, payload.get("view"), state)
        prepared = adapt(payload, message=target, editing=True)
        try:
            result = await interaction.edit_original_response(**prepared)
        except BaseException:
            v2_delivery.restore(previous, target, state)
            raise
        _remember_original(interaction, result)
        return v2_delivery.commit(payload, prepared["view"], result,
                                  previous=previous, message=target, state=state,
                                  attachments=prepared.get("attachments") or ())


async def edit_response(interaction, **payload):
    target = getattr(interaction, "message", None)
    async with v2_delivery.edit_lock(target, interaction):
        state = interaction._state
        previous = v2_delivery.registered(target, payload.get("view"), state)
        prepared = adapt(payload, message=target, editing=True)
        try:
            result = await interaction.response.edit_message(**prepared)
        except BaseException:
            v2_delivery.restore(previous, target, state)
            raise
        _remember_original(interaction, result)
        return v2_delivery.commit(payload, prepared["view"], result,
                                  previous=previous, message=target, state=state,
                                  attachments=prepared.get("attachments") or ())


async def edit_message(message, **payload):
    async with v2_delivery.edit_lock(message):
        previous = v2_delivery.registered(message, payload.get("view"))
        prepared = adapt(payload, message=message, editing=True)
        try:
            result = await message.edit(**prepared)
        except BaseException:
            v2_delivery.restore(previous, message)
            raise
        return v2_delivery.commit(payload, prepared["view"], result,
                                  previous=previous, message=message,
                                  attachments=prepared.get("attachments") or ())


async def send_response(interaction, content=None, **payload):
    if content is not None:
        payload["content"] = content
    if "attachments" in payload:
        payload["files"] = payload.pop("attachments")
    prepared = adapt(payload)
    result = await interaction.response.send_message(**prepared)
    _remember_original(interaction, result)
    return v2_delivery.commit(payload, prepared["view"], result, state=interaction._state)


async def followup(interaction, content=None, **payload):
    if content is not None:
        payload["content"] = content
    if "attachments" in payload:
        payload["files"] = payload.pop("attachments")
    prepared = adapt(payload)
    result = await interaction.followup.send(**prepared)
    return v2_delivery.commit(payload, prepared["view"], result, state=interaction._state)


async def send_message(destination, content=None, **payload):
    if content is not None:
        payload["content"] = content
    if "attachments" in payload:
        payload["files"] = payload.pop("attachments")
    prepared = adapt(payload)
    result = await destination.send(**prepared)
    return v2_delivery.commit(payload, prepared["view"], result)
