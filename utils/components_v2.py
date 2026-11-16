"""Explicit V2 send/edit boundaries. Ordinary AI/nonce delivery is not intercepted."""
from __future__ import annotations

import discord

from utils import v2_delivery, v2_media
from utils.v2_layout import ControllerView, PanelLayout, layout_for, retained_controller
from utils.v2_content import OVERFLOW_NAME


def _spec(payload, source, message):
    previous = getattr(source, "_v2_spec", None)
    body_provided = any(key in payload for key in ("content", "embed", "embeds"))
    if not body_provided and previous is not None:
        return dict(previous)
    embeds = (payload["embed"],) if payload.get("embed") is not None else payload.get("embeds") or ()
    content = payload.get("content")
    if not body_provided and message is not None:
        if getattr(getattr(message, "flags", None), "components_v2", False):
            return {"message": message}
        content = getattr(message, "content", "")
        embeds = getattr(message, "embeds", ())
    return {"content": content, "embeds": tuple(embed.copy() for embed in embeds)}


def _attachments(result, message, source, *, editing):
    if "attachments" in result:
        retained = result.pop("attachments") or ()
    elif editing and message is not None:
        cached = getattr(source, "_v2_attachments", ()) if getattr(source, "_v2_message_id", None) == message.id else ()
        current = getattr(message, "attachments", ()) or ()
        retained = {item.id: item for item in (*cached, *current)}.values()
    else:
        retained = ()
    files = list(result.pop("files", ()) or ())
    single = result.pop("file", None)
    if single is not None:
        files.append(single)
    return [*retained, *files]


def adapt(payload, *, message=None, editing=False, previous=None):
    result = dict(payload)
    for key in ("poll", "stickers"):
        if result.pop(key, None):
            raise ValueError(f"Discord Components V2 does not support {key} in the same message.")
    source = result.pop("view", None)
    if editing and "view" not in payload and message is not None:
        source = retained_controller(message, previous)
    attachments = _attachments(result, message, source, editing=editing)
    if isinstance(source, discord.ui.LayoutView):
        layout, extra = source, None
    else:
        spec = _spec(result, source, message)
        if "message" not in spec:
            attachments = [item for item in attachments
                           if getattr(item, "filename", None) != OVERFLOW_NAME]
        layout, extra = layout_for(source, spec, file_slots=len(attachments))
    for key in ("content", "embed", "embeds"):
        result.pop(key, None)
    result["view"] = layout
    result.setdefault("allowed_mentions", discord.AllowedMentions.none())
    if extra is not None:
        attachments.append(extra)
    v2_media.normalize(layout, attachments, message=message, source=source)
    v2_media.expose_files(layout, attachments)
    if editing:
        # Clear legacy fields when promoting a message. The V2 flag is irreversible.
        result.update(content=None, embeds=[], attachments=attachments)
    elif attachments:
        result["files"] = attachments
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
        prepared = adapt(payload, message=target, editing=True, previous=previous)
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
        prepared = adapt(payload, message=target, editing=True, previous=previous)
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
        prepared = adapt(payload, message=message, editing=True, previous=previous)
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
    prepared = adapt(payload)
    result = await interaction.response.send_message(**prepared)
    _remember_original(interaction, result)
    return v2_delivery.commit(payload, prepared["view"], result, state=interaction._state)


async def followup(interaction, content=None, **payload):
    if content is not None:
        payload["content"] = content
    prepared = adapt(payload)
    result = await interaction.followup.send(**prepared)
    return v2_delivery.commit(payload, prepared["view"], result, state=interaction._state)


async def send_message(destination, content=None, **payload):
    if content is not None:
        payload["content"] = content
    prepared = adapt(payload)
    result = await destination.send(**prepared)
    return v2_delivery.commit(payload, prepared["view"], result)
