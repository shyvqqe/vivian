"""Render existing panel data as native Discord Components V2."""
from __future__ import annotations

from io import BytesIO

import discord


OVERFLOW_NAME = "vivian-panel.txt"


def _url(value):
    return value if isinstance(value, str) and value.startswith(("https://", "http://", "attachment://")) else None


def _existing_card(message):
    """Copy this panel's structure without flattening its text or media."""
    layout = discord.ui.LayoutView.from_message(message, timeout=None)
    if len(layout.children) != 1 or not isinstance(layout.children[0], discord.ui.Container):
        raise ValueError("Expected one Vivian panel Container for a Components V2 update")
    container = layout.children[0]
    layout.remove_item(container)
    for item in list(container.children):
        if isinstance(item, discord.ui.ActionRow):
            container.remove_item(item)
    return container, None


def card(spec, *, body_blocks=4):
    if spec.get("message") is not None:
        return _existing_card(spec["message"])
    embed = spec.get("embed")
    content = spec.get("content") or ""
    header, body, footer, image, thumbnail = "", content, "", None, None
    sections = [content] if content else []
    accent = 0xD58BFF
    if embed is not None:
        author = getattr(embed.author, "name", None)
        title = embed.title or ""
        header = "\n".join(part for part in (
            f"-# {author}" if author else "", f"## {title}" if title else "",
        ) if part)
        fields = [f"**{field.name}**\n{field.value}" for field in embed.fields]
        body = "\n\n".join(part for part in (content, embed.description, *fields) if part)
        sections = [part for part in (content, embed.description, *fields) if part]
        footer = getattr(embed.footer, "text", None) or ""
        image = _url(embed.image.url)
        thumbnail = _url(embed.thumbnail.url) or _url(getattr(embed.author, "icon_url", None))
        if embed.color is not None:
            accent = embed.color.value
    if not header and not body:
        body = "Панель закрыта."
        sections = [body]
    if not header:
        header = "## Vivian"
    complete = "\n\n".join(part for part in (header, body, footer) if part)
    extra = None
    if len(complete) > 3900:
        extra = discord.File(BytesIO(complete.encode("utf-8")), filename=OVERFLOW_NAME)
        header, footer = header[:450], footer[:150]
        limit = 3700 - len(header) - len(footer)
        body = body[:max(0, limit)] + "\n\nПолный текст — в приложенном файле."
        sections = [body]
    items = []
    if header:
        heading = discord.ui.TextDisplay(header)
        items.append(discord.ui.Section(heading, accessory=discord.ui.Thumbnail(thumbnail))
                     if thumbnail else heading)
    elif thumbnail:
        items.append(discord.ui.Section(discord.ui.TextDisplay("### Vivian"),
                                        accessory=discord.ui.Thumbnail(thumbnail)))
    if header and body:
        items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
    if body:
        # Keep descriptions and metric groups readable without exceeding the
        # total component budget after the controller's rows are attached.
        count = max(1, min(body_blocks, len(sections)))
        size = max(1, (len(sections) + count - 1) // count)
        items.extend(discord.ui.TextDisplay("\n\n".join(sections[index:index + size]))
                     for index in range(0, len(sections), size))
    if image:
        items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(image)))
    if extra is not None:
        items.append(discord.ui.File(f"attachment://{OVERFLOW_NAME}"))
    if footer:
        if body_blocks > 1:
            items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        items.append(discord.ui.TextDisplay(f"-# {footer}"))
    return discord.ui.Container(*items, accent_color=accent), extra
