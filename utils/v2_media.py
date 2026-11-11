"""Normalize panel media against the exact uploads retained in this request.

New uploads use attachment:// names; existing image uploads use their CDN URLs.
Missing decorative media is omitted rather than making a button update fail.
"""
from __future__ import annotations

import discord


_PREFIX = "attachment://"


def _remote_url(value):
    return value if isinstance(value, str) and value.startswith(("https://", "http://")) else None


def _replace_children(parent, children):
    # Canonical panels contain a Container; rebuilding preserves sibling order.
    parent.clear_items()
    for child in children:
        parent.add_item(child)


def normalize(layout, attachments, *, message=None, source=None) -> None:
    """Modify a candidate LayoutView only; no downloads or Discord API calls.

    ``attachments`` is the final list for this target message, assembled by the
    sending boundary. Never borrow files from another message or controller.
    """
    files = {}
    retained = {}
    retained_ids = {}
    retained_urls = {}
    for attachment in attachments:
        filename = getattr(attachment, "filename", None)
        if not filename:
            continue
        if isinstance(attachment, discord.File):
            files[filename] = attachment
        elif isinstance(attachment, discord.Attachment):
            retained[filename] = attachment
            retained_ids[attachment.id] = filename
            retained_urls[attachment.url] = filename

    def resolve(url):
        if not isinstance(url, str):
            return None
        if not url.startswith(_PREFIX):
            return _remote_url(url)
        filename = url[len(_PREFIX):]
        if filename in files:
            return url
        attachment = retained.get(filename)
        return _remote_url(attachment.url) if attachment is not None else None

    def file_reference(item):
        # File components require attachment:// even when API response data has
        # expanded a previously accepted file reference into its CDN URL.
        media = item.media
        url = media.url
        if isinstance(url, str) and url.startswith(_PREFIX):
            filename = url[len(_PREFIX):]
        else:
            filename = retained_ids.get(getattr(media, "attachment_id", None))
            if filename is None:
                filename = retained_urls.get(url)
        if filename in files or filename in retained:
            return _PREFIX + filename
        return None

    def visit(parent):
        children = []
        changed = False
        for child in list(parent.children):
            if isinstance(child, discord.ui.MediaGallery):
                media_items = []
                for media_item in child.items:
                    url = resolve(media_item.media.url)
                    if url is not None:
                        media_items.append(discord.MediaGalleryItem(
                            url, description=media_item.description, spoiler=media_item.spoiler,
                        ))
                child.items = media_items
                if not media_items:
                    changed = True
                    continue
            elif isinstance(child, discord.ui.Section):
                accessory = child.accessory
                if isinstance(accessory, discord.ui.Thumbnail):
                    url = resolve(accessory.media.url)
                    if url is None:
                        children.extend(discord.ui.TextDisplay(text.content, id=text.id)
                                        for text in child.children)
                        changed = True
                        continue
                    accessory.media = url
            elif isinstance(child, discord.ui.File):
                url = file_reference(child)
                if url is None:
                    changed = True
                    continue
                child.media = url
            elif isinstance(child, discord.ui.Container):
                visit(child)
            children.append(child)
        if changed:
            _replace_children(parent, children)

    visit(layout)
