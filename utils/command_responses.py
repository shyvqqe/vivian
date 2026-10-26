"""Finish command acknowledgements through the interaction's own webhook."""
from __future__ import annotations

import discord

from core import discord_api
from visuals import panels
from utils import animated_responses, components_v2 as v2


PUBLIC_COMMANDS = frozenset({
    "help", "ping", "about", "rank", "activity", "veyr", "play", "queue",
    "skip", "pause", "resume", "stop", "loop", "volume", "ban", "kick",
    "clear", "slowmode", "lock", "unlock",
    "changelog", "deploy", "showcases",
    "card", "crad", "quests", "tasks",
})


def _public_command(interaction):
    # Buttons and modals can open private memory or show a personal error.
    if (getattr(interaction, "type", None) is not discord.InteractionType.application_command
            or getattr(interaction, "guild", None) is None):
        return None
    name = getattr(getattr(interaction, "command", None), "name", None)
    return name if name in PUBLIC_COMMANDS else None


def _extras(interaction):
    extras = getattr(interaction, "extras", None)
    if extras is None:
        extras = {}
        interaction.extras = extras
    return extras


def _log(interaction, phase, error=None):
    command = getattr(getattr(interaction, "command", None), "name", "unknown")
    detail = (f"; error={type(error).__name__}; status={getattr(error, 'status', None)}; "
              f"code={getattr(error, 'code', None)}") if error is not None else ""
    print(f"[Command UI] {phase}; command={command}; interaction_id={getattr(interaction, 'id', 'unknown')}{detail}", flush=True)


def _expired(interaction):
    check = getattr(interaction, "is_expired", None)
    return bool(check()) if callable(check) else False


def _completed(interaction):
    _extras(interaction)["vivian_response_completed"] = True


def is_thinking(interaction):
    return (getattr(interaction.response, "type", None) is discord.InteractionResponseType.deferred_channel_message
            and not getattr(interaction, "extras", {}).get("vivian_response_completed", False))


async def defer_command(interaction, *, ephemeral=False):
    if _expired(interaction):
        _log(interaction, "Expired before acknowledgement")
        return False
    if interaction.response.is_done():
        return True
    public_command = _public_command(interaction)
    if public_command == "clear":
        # A public ACK would itself be removed by channel.purge().
        ephemeral = True
        _extras(interaction)["vivian_publish_clear"] = True
    elif public_command and public_command != "queue":
        # /queue already publishes one shared player after its private ACK.
        ephemeral = False
    try:
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)
        _log(interaction, "Acknowledged")
        return True
    except discord.DiscordException as error:
        _log(interaction, "Acknowledgement failed", error)
        return False


def _text_payload(payload):
    embed = payload.get("embed")
    lines = ["Не получилось показать полную карточку."]
    if embed is not None:
        if embed.title:
            lines.append(f"**{embed.title}**")
        if embed.description:
            lines.append(embed.description)
        for field in embed.fields:
            lines.append(f"{field.name}: {field.value}")
    elif payload.get("content"):
        lines.append(str(payload["content"]))
    return {"content": "\n".join(lines)[:1900], "embed": None, "view": None, "attachments": [],
            "allowed_mentions": discord.AllowedMentions.none()}


async def _publish_clear_result(interaction, payload):
    """Publish once after purge has finished, using the interaction webhook."""
    extras = _extras(interaction)
    if extras.get("vivian_clear_public_attempted"):
        return None
    try:
        await v2.edit_original(interaction,
            content="Очистка завершена.", embed=None, view=None, attachments=[],
            allowed_mentions=discord.AllowedMentions.none(),
        )
        _completed(interaction)
        # Never repeat an uncertain POST, including an escaped 429.
        extras["vivian_clear_public_attempted"] = True
        payload = dict(payload)
        message = await v2.followup(interaction, **payload, ephemeral=False, wait=True)
        _log(interaction, "Completed publicly")
        return message
    except Exception as error:
        _log(interaction, "Public completion unavailable", error)
        return None


async def edit_deferred(interaction, **payload):
    if _expired(interaction):
        _log(interaction, "Expired before completion")
        return None
    payload = await animated_responses.decorate(interaction, payload)
    payload.setdefault("allowed_mentions", discord.AllowedMentions.none())
    embed = payload.get("embed")
    if (_public_command(interaction) == "clear"
            and _extras(interaction).get("vivian_publish_clear")
            and embed is not None and embed.color is not None
            and embed.color.value != 0xFF2A6D):
        return await _publish_clear_result(interaction, payload)
    try:
        message = await v2.edit_original(interaction, **payload)
        _completed(interaction)
        _log(interaction, "Completed")
        return message
    except discord.DiscordException as error:
        _log(interaction, "Completion failed", error)
        # The SDK already handles ordinary 429s. Do not amplify an escaped
        # webhook limit or send to an expired token through a new endpoint.
        if isinstance(error, discord.NotFound) or discord_api.is_rate_limit_error(error):
            return None
        if not isinstance(error, discord.HTTPException):
            return None
    except Exception as error:
        _log(interaction, "Invalid card payload", error)
    controller = payload.get("view")
    if isinstance(controller, v2.ControllerView):
        controller.stop()
    try:
        message = await v2.edit_original(interaction, **_text_payload(payload))
        _completed(interaction)
        _log(interaction, "Completed as text")
        return message
    except Exception as error:
        _log(interaction, "Text completion unavailable", error)
        return None


async def finish_deferred(interaction, description="Команда не завершилась. Попробуй ещё раз.", *, error=None):
    if error is not None:
        _log(interaction, "Command failed", error)
        if isinstance(error, discord.NotFound) or discord_api.is_rate_limit_error(error):
            return None
    if _expired(interaction):
        _log(interaction, "Cannot finish expired interaction")
        return None
    if is_thinking(interaction):
        return await edit_deferred(interaction, content=description[:1900], embed=None, view=None, attachments=[])
    try:
        if interaction.response.is_done():
            return await v2.followup(interaction, description[:1900], ephemeral=True,
                                     allowed_mentions=discord.AllowedMentions.none())
        result = await v2.send_response(interaction, description[:1900], ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())
        _completed(interaction)
        return result
    except discord.DiscordException as response_error:
        _log(interaction, "Error completion unavailable", response_error)
        return None


async def send_panel(interaction, *, embed=None, view=None, style=None):
    try:
        visual = (await panels._panel_visual_file(style)
                  if style and panels._can_upload_panel_visual(interaction.guild, interaction.channel) else None)
        if view is not None:
            view.visual_enabled = visual is not None
            embed = view.make_embed()
        elif visual is not None and embed is not None:
            panels._apply_panel_visual(embed, style)
        return await edit_deferred(interaction, embed=embed, view=view, attachments=[visual] if visual else [])
    except Exception as error:
        return await finish_deferred(interaction, error=error)
