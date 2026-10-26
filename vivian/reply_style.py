"""Compact chat cards; formatting never adds another Discord request or AI call."""
from __future__ import annotations

import re

import discord


CHAT_ACCENT = 0x83DDD2
AI_REPLY_MARKER = "Vivian · диалог"
_QUOTE = re.compile(r"^ {0,3}>{1,3}(?:[ \t]|$)")
_LIST = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")
_BOLD_LINE = re.compile(r"(?m)^\s*\*\*[^*\n]{2,}\*\*")


def _unquote(line, limit=None):
    removed = 0
    pattern = _QUOTE if limit is None else re.compile(r"^ {0,3}>[ \t]?")
    while limit is None or removed < limit:
        match = pattern.match(line)
        if match is None:
            break
        line = line[match.end():]
        removed += match.group(0).count(">")
    return line, removed


def _card_text(text):
    """Use the card's mint border for quotes; preserve literal code verbatim."""
    lines, quote_count, list_count = [], 0, 0
    fence, fence_quotes = None, 0
    for original in text.splitlines():
        if fence is not None:
            # Remove only the external block-quote wrapper around quoted code.
            # A literal '>' inside the code is never treated as another quote.
            line, _ = _unquote(original, fence_quotes)
            stripped = line.lstrip()
            marker = re.match(r"(`{3,}|~{3,})", stripped)
            if marker:
                run = marker.group(0)
                if (run[0] == fence[0] and len(run) >= len(fence)
                        and not stripped[len(run):].strip()):
                    fence, fence_quotes = None, 0
            lines.append(line)
            continue
        line, removed = _unquote(original)
        quote_count += bool(removed)
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            fence, fence_quotes = marker.group(0), removed
            lines.append(line)
            continue
        if _LIST.match(line):
            list_count += 1
        lines.append(line)
    return "\n".join(lines), quote_count, list_count


def _can_embed(message):
    guild = getattr(message, "guild", None)
    if guild is None:
        return True
    channel = getattr(message, "channel", None)
    me = getattr(guild, "me", None)
    permissions_for = getattr(channel, "permissions_for", None)
    if me is None or not callable(permissions_for):
        return False
    return bool(getattr(permissions_for(me), "embed_links", False))


def reply_payload(message, text, *, prefer_card=False):
    """Only structured explanations become cards; ordinary banter stays text."""
    rendered, quotes, lists = _card_text(text)
    structured = (quotes or lists >= 2 or len(_BOLD_LINE.findall(text)) >= 2
                  or (prefer_card and len(text) >= 160))
    if not structured or not rendered.strip() or not _can_embed(message):
        return {"content": text, "embed": None}
    embed = discord.Embed(description=rendered, color=CHAT_ACCENT)
    # A durable marker allows replies and unanswered-question recovery after restart.
    # Do not reuse the generic SYSTEM marker used by commands and creator cards.
    embed.set_footer(text=AI_REPLY_MARKER)
    return {"content": None, "embed": embed}


def extract_reply_text(message, bot_id):
    """Read only this bot's text or its marked dialogue cards, never UI metadata."""
    if bot_id is None or getattr(getattr(message, "author", None), "id", None) != bot_id:
        return ""
    content = getattr(message, "content", "")
    if isinstance(content, str) and content.strip():
        return content[:1900]
    parts = []
    for embed in getattr(message, "embeds", ()):
        if getattr(getattr(embed, "footer", None), "text", None) != AI_REPLY_MARKER:
            continue
        if isinstance(embed.description, str) and embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            for value in (field.name, field.value):
                if isinstance(value, str) and value:
                    parts.append(value)
    return "\n\n".join(parts)[:1900]
