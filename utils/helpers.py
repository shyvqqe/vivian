from __future__ import annotations
import discord
from core import config as core_config
from utils import command_responses
from utils import animated_responses, components_v2 as v2
from utils import delivery_state as utils_delivery_state


def vivian_brand(embed: discord.Embed, author: str = "◆ Vivian // SYSTEM") -> discord.Embed:
    if core_config.bot.user is not None:
        embed.set_author(name=author, icon_url=core_config.bot.user.display_avatar.url)
    else:
        embed.set_author(name=author)
    embed.set_footer(text=core_config.VIVIAN_FOOTER)
    return embed


def vivian_embed(
    description: str = "",
    *,
    title: str = "◆ Vivian",
    error: bool = False,
    color: discord.Color | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color or (core_config.VIVIAN_ERROR if error else core_config.VIVIAN_ACCENT),
    )
    return vivian_brand(embed)


async def vivian_reply(
    interaction: discord.Interaction,
    description: str,
    *,
    title: str = "◆ Vivian // SYSTEM",
    error: bool = False,
    ephemeral: bool = False,
):
    embed = vivian_embed(description, title=title, error=error)
    if command_responses.is_thinking(interaction):
        return await command_responses.edit_deferred(interaction, embed=embed, view=None)
    if interaction.response.is_done():
        payload = await animated_responses.decorate(interaction, {"embed": embed})
        return await v2.followup(interaction, **payload, ephemeral=ephemeral,
                                 allowed_mentions=discord.AllowedMentions.none())
    if not await command_responses.defer_command(interaction, ephemeral=ephemeral):
        return None
    return await command_responses.edit_deferred(interaction, embed=embed, view=None)


def _claim_message_once(message_id: int) -> bool:
    if message_id in utils_delivery_state.processed_message_ids:
        return False
    utils_delivery_state.processed_message_ids.add(message_id)
    utils_delivery_state.processed_message_order.append(message_id)
    if len(utils_delivery_state.processed_message_order) > core_config.PROCESSED_MESSAGE_LIMIT:
        utils_delivery_state.processed_message_ids.discard(utils_delivery_state.processed_message_order.popleft())
    return True


async def _send_message_once(
    channel,
    content: str | None,
    nonce: str,
    reference=None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
):
    route = discord.http.Route(
        "POST",
        "/channels/{channel_id}/messages",
        channel_id=channel.id,
    )
    payload = {
        "nonce": str(nonce),
        "enforce_nonce": True,
        "allowed_mentions": {"parse": [], "replied_user": False},
    }
    if content is not None:
        payload["content"] = content
    if embed is not None:
        payload["embeds"] = [embed.to_dict()]
    if reference is not None:
        payload["message_reference"] = {"message_id": str(reference.id)}
    if view is not None:
        payload["components"] = view.to_components()
    sent = await core_config.bot.http.request(route, json=payload)
    if view is not None:
        # Raw REST delivery does not register callbacks as Messageable.send does.
        # A registration error after accepted delivery must never send a duplicate.
        try:
            message_id = int(sent["id"])
            core_config.bot._connection.store_view(view, message_id)
            view.panel_message = channel.get_partial_message(message_id)
        except Exception as error:
            print(f"[UI] View registration failed; nonce={nonce}; error={error!r}", flush=True)
    return sent


async def _reply_once(message, content: str):
    return await _send_message_once(
        message.channel,
        content,
        nonce=str(message.id),
        reference=message,
    )


def _vivian_reply_reference(message):
    reference = getattr(message, "reference", None)
    resolved = getattr(reference, "resolved", None)
    author = getattr(resolved, "author", None)
    if core_config.bot.user is not None and getattr(author, "id", None) == core_config.bot.user.id:
        return resolved
    return None


def _vivian_reply_text(message):
    from vivian import reply_style
    bot_id = getattr(core_config.bot.user, "id", None)
    return reply_style.extract_reply_text(message, bot_id)


def _ru_plural(number: int, one: str, few: str, many: str) -> str:
    remainder_100 = number % 100
    remainder_10 = number % 10
    if 11 <= remainder_100 <= 14:
        return many
    if remainder_10 == 1:
        return one
    if 2 <= remainder_10 <= 4:
        return few
    return many
