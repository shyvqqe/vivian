from __future__ import annotations
import discord
import re
from core import discord_api as core_discord_api


MOOD_REACTION_PATTERNS = {
    "thanks": re.compile(r"\b(спасибо|благодарю|спс|выручил[а]?|помогло)\b", re.IGNORECASE),
    "laugh": re.compile(r"\b(ахах\w*|хах\w*|лол|ору|ржу|смешно)\b", re.IGNORECASE),
    "sad": re.compile(r"\b(грустно|плохо|тяжело|обидно|одиноко|устал[а]?|страшно|тревожно|расстроен[а]?)\b", re.IGNORECASE),
    "frustrated": re.compile(r"\b(бесит|задолбал[о]?|достал[о]?|раздражает|ненавижу|всё бесит)\b", re.IGNORECASE),
    "excited": re.compile(r"\b(ура|круто|классно|супер|получилось|наконец-то|офигенно)\b", re.IGNORECASE),
}


MOOD_REACTION_EMOJI = {
    "thanks": "🙏",
    "laugh": "😂",
    "sad": "🫂",
    "frustrated": "😔",
    "excited": "✨",
}


MOOD_SERIOUS_PATTERN = re.compile(
    r"\b(?:не\s+шутк\w*|без\s+шуток|не\s+смешно|серь[её]зно|"
    r"это\s+важно|нужна\s+помощь)\b", re.IGNORECASE,
)


MOOD_PROBLEM_PATTERN = re.compile(
    r"\b(?:не\s+(?:работает|включается|запускается|загружается|отвечает|заряжается|помогло|получилось)|"
    r"сломал(?:ся|ась|ось)|сломан[ао]?|неисправност\w*|"
    r"лага(?:ет|ют)|тормоз(?:ит|ят)|вылетает|завис(?:ла|ло)?|ошибк\w*)\b",
    re.IGNORECASE,
)


MOOD_RESOLVED_PATTERN = re.compile(
    r"\b(?:помогло|получилось|заработал[ао]?|починил[а]?|исправил[а]?|решил[а]?)\b",
    re.IGNORECASE,
)


MOOD_NEGATION_PATTERN = re.compile(
    r"\b(?:не|ни|нет)\s+(?:(?:так|очень|особенно|особо|прям|прямо|только|уже|то|чтобы|"
    r"слишком|совсем|настолько|больше|вовсе|было|была|был|стало|становится)\s+){0,3}$",
    re.IGNORECASE,
)


def _mood_text(content: str) -> str:
    """Use the author's words, excluding quoted text, code, links and mentions."""
    content = re.sub(r"```[\s\S]*?(?:```|$)|`[^`\n]*`", " ", content)
    content = re.sub(r"(?m)^\s*>.*$", " ", content)
    content = re.sub(r"""«[^»]*»|“[^”]*”|"[^"\n]*"|'[^'\n]*'""", " ", content)
    content = re.sub(r"https?://\S+|<[@#][!&]?\d+>", " ", content)
    return re.sub(r"\s+", " ", content).strip()


def _mood_has_cue(text: str, pattern: re.Pattern) -> bool:
    for match in pattern.finditer(text):
        # Negation applies locally: 'не помогло', but not 'не работало, спасибо'.
        prefix = re.split(r"[.!?,;:\n]", text[:match.start()])[-1][-80:]
        if not MOOD_NEGATION_PATTERN.search(prefix):
            return True
    return False


def _mood_history_text(item: dict) -> str:
    content = item.get("content", "")
    if not isinstance(content, str):
        return ""
    # Stored history includes the display name, which is not a mood cue.
    if content.startswith("Собеседник:") and "\nСообщение:" in content:
        content = content.split("\nСообщение:", 1)[1]
    return _mood_text(content)


def _mood_is_serious(text: str) -> bool:
    return bool(MOOD_SERIOUS_PATTERN.search(text)) or _mood_has_cue(
        text, MOOD_PROBLEM_PATTERN,
    ) or any(
        _mood_has_cue(text, MOOD_REACTION_PATTERNS[mood])
        for mood in ("sad", "frustrated")
    )


def _mood_reaction_for(message: discord.Message, history: list[dict]) -> str | None:
    """React only when the current message clearly expresses a mood; never random."""
    current = _mood_text(message.content or "")
    current_moods = {
        mood for mood, pattern in MOOD_REACTION_PATTERNS.items()
        if _mood_has_cue(current, pattern)
    }
    current_serious = _mood_is_serious(current)
    if current_serious:
        current_moods.difference_update(("laugh", "excited"))

    if "laugh" in current_moods:
        # The caller has already appended the current user entry and the reply.
        # Inspect earlier user words only; an explicit resolution clears the veto.
        user_entries = [item for item in history if item.get("role") == "user"]
        resolved_now = _mood_has_cue(current, MOOD_RESOLVED_PATTERN)
        if not resolved_now:
            for item in reversed(user_entries[:-1][-2:]):
                context = _mood_history_text(item)
                if _mood_has_cue(context, MOOD_RESOLVED_PATTERN):
                    break
                if _mood_is_serious(context):
                    current_moods.discard("laugh")
                    break

    # A genuine sad cue wins over nervous laughter; gratitude can follow a fix.
    for mood in ("sad", "frustrated", "thanks", "excited", "laugh"):
        if mood in current_moods:
            return MOOD_REACTION_EMOJI[mood]
    return None


async def _add_mood_reaction(message: discord.Message, history: list[dict]) -> None:
    if core_discord_api.is_paused():
        return
    emoji = _mood_reaction_for(message, history)
    if emoji is None:
        return
    try:
        await message.add_reaction(emoji)
    except discord.DiscordException as error:
        if core_discord_api.is_rate_limit_error(error):
            return
        print(
            f"[AI] Mood reaction failed; message_id={message.id}; error={error!r}",
            flush=True,
        )
