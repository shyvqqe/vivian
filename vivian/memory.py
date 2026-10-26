from __future__ import annotations
from datetime import datetime
from collections import deque
import hashlib
import json
import re
import time
from core import config as core_config
from core import state as core_state
from vivian import ambient_state as vivian_ambient_state
from vivian import dialogue as vivian_dialogue
from vivian import memory_state as vivian_memory_state
from vivian import memory_store as vivian_memory_store
from vivian import memory_controls_store as controls_store


def _ai_memory_scope(message, require_history: bool = False):
    guild = getattr(message, "guild", None)
    channel = getattr(message, "channel", None)
    if guild is None or channel is None or guild.me is None:
        return None
    bot_permissions = channel.permissions_for(guild.me)
    if not bot_permissions.view_channel:
        return None
    if require_history:
        viewer = channel.permissions_for(message.author)
        if not (bot_permissions.read_message_history and viewer.view_channel and viewer.read_message_history):
            return None
    return (guild.id, channel.id)


def _ai_memory_touch(scope) -> None:
    vivian_memory_state.AI_MEMORY_LAST_USED[scope] = time.time()
    while len(vivian_memory_state.AI_MEMORY_LAST_USED) > vivian_memory_state.AI_MEMORY_MAX_SCOPES:
        victim = min(vivian_memory_state.AI_MEMORY_LAST_USED, key=vivian_memory_state.AI_MEMORY_LAST_USED.get)
        vivian_memory_state.AI_MEMORY_LAST_USED.pop(victim, None)
        vivian_memory_state.AI_MEMORY_CHANNELS.pop(victim, None)
        vivian_memory_state.AI_MEMORY_FACTS.pop(victim, None)
        vivian_memory_state.AI_MEMORY_LOADED.discard(victim)
        vivian_memory_state.AI_MEMORY_CLEANED_AT.pop(victim, None)
        vivian_memory_state.AI_MEMORY_LOAD_RETRY.pop(victim, None)
        vivian_memory_state.AI_MEMORY_LOAD_LOCKS.pop(victim, None)
        for key in [key for key in core_state.chat_history if key[:2] == victim]:
            core_state.chat_history.pop(key, None)
            if key in core_state.AI_DIALOGUE_LOCKS and not core_state.AI_DIALOGUE_LOCKS[key].locked():
                core_state.AI_DIALOGUE_LOCKS.pop(key, None)


def _ai_memory_cache_record(scope, record: dict) -> None:
    if record["message_id"] in controls_store.PURGED_SOURCE_IDS.get(scope, set()):
        return
    _ai_memory_touch(scope)
    records = vivian_memory_state.AI_MEMORY_CHANNELS.setdefault(scope, deque(maxlen=vivian_memory_state.AI_MEMORY_CACHE_LIMIT))
    merged = {item["message_id"]: item for item in records}
    merged[record["message_id"]] = record
    cutoff = time.time() - vivian_memory_state.AI_MEMORY_DAYS * 86400
    vivian_memory_state.AI_MEMORY_CHANNELS[scope] = deque(
        sorted((item for item in merged.values() if item["timestamp"] >= cutoff),
               key=lambda item: (item["timestamp"], item["message_id"]))[-vivian_memory_state.AI_MEMORY_CACHE_LIMIT:],
        maxlen=vivian_memory_state.AI_MEMORY_CACHE_LIMIT,
    )


def _ai_memory_fact_candidates(text: str) -> dict:
    """Keep explicit, attributable statements; never infer identity or mood from an avatar/name."""
    facts = {}
    text = re.sub(r"(?i)^\s*(?:vivian|вивиан)[\s,:]+", "", text)
    text = re.sub(r"(?i)^\s*запомни(?:,?\s+что|\s*:)?\s+", "", text)
    # Quoted words belong to their original speaker, not necessarily to this user.
    text = re.sub(r'```[\s\S]*?```|`[^`]*`|«[^»]*»|"[^"\n]*"|(?m:^>.*$)', '', text)
    for sentence in re.split(r"(?<=[.!?\n])\s*", text):
        sentence = sentence.strip()
        if not sentence or sentence.endswith("?") or len(sentence) > 300 or vivian_memory_state.AI_MEMORY_SECRET_PATTERN.search(sentence):
            continue
        sentence = sentence.rstrip(".! ")
        key = None
        if re.match(r"(?i)^(?:меня зовут|зови меня|обращайся ко мне как)\s+", sentence):
            key = "address_name"
        elif re.match(r"(?i)^(?:мои местоимения|я (?:девушка|парень|женщина|мужчина)\b|"
                      r"обращайся ко мне в (?:женском|мужском|нейтральном) роде)", sentence):
            key = "address_gender"
        elif re.match(r"(?i)^я (?:люблю|предпочитаю|играю|пользуюсь)\s+", sentence):
            key = "preference:" + hashlib.blake2b(sentence.lower().encode(), digest_size=6).hexdigest()
        elif re.match(r"(?i)^у меня\s+", sentence) and re.search(
            r"(?i)\b(?:rtx|gtx|radeon|ryzen|intel|iphone|айфон|android|андроид|samsung|"
            r"пиксель|pixel|ноутбук|процессор|видеокарт\w*|озу|оператив\w*|гб|gb)\b", sentence,
        ):
            key = "device:" + hashlib.blake2b(sentence.lower().encode(), digest_size=6).hexdigest()
        if key:
            facts[key] = sentence[:240]
    return facts


async def _remember_chat_message(message, directly_addressed: bool = False) -> None:
    scope = _ai_memory_scope(message)
    text = (message.content or "").strip()
    if scope is None or not text or text.startswith(("/", "!")) or vivian_memory_state.AI_MEMORY_SECRET_PATTERN.search(text):
        return
    if core_config.bot.user:
        text = text.replace(f"<@{core_config.bot.user.id}>", "Vivian").replace(f"<@!{core_config.bot.user.id}>", "Vivian")
    record = {"message_id": message.id, "author_id": message.author.id,
        "audience_id": message.author.id if directly_addressed else 0, "role": "user",
        "name": str(message.author.display_name)[:80], "text": text[:1800],
        "timestamp": min(message.created_at.timestamp(), time.time())}
    _ai_memory_cache_record(scope, record)
    facts = controls_store.filter_fact_candidates(scope, message.author.id, message.id,
                                                 _ai_memory_fact_candidates(text))
    personal = vivian_memory_state.AI_MEMORY_FACTS.setdefault(scope, {}).setdefault(message.author.id, {})
    for key, fact in facts.items():
        if key not in personal or personal[key]["timestamp"] <= record["timestamp"]:
            personal[key] = {"text": fact, "source": record["message_id"], "timestamp": record["timestamp"]}
    active = sorted(((key, value) for key, value in personal.items() if value.get("text")),
                    key=lambda item: (item[1]["timestamp"], item[0]), reverse=True)
    tombstones = {key: value for key, value in personal.items() if not value.get("text")
                  and time.time() - value["timestamp"] < vivian_memory_state.AI_MEMORY_FACT_DAYS * 86400}
    vivian_memory_state.AI_MEMORY_FACTS[scope][message.author.id] = {
        **dict(active[:vivian_memory_state.AI_MEMORY_FACT_LIMIT]), **tombstones}
    await vivian_memory_store._ai_memory_save(scope, record, facts)


async def _remember_vivian_message(channel, sent, content: str, audience_id: int = 0) -> None:
    guild = getattr(channel, "guild", None)
    mid = sent.get("id") if isinstance(sent, dict) else getattr(sent, "id", None)
    if guild is None or core_config.bot.user is None or mid is None or not content or vivian_memory_state.AI_MEMORY_SECRET_PATTERN.search(content):
        return
    scope = (guild.id, channel.id)
    record = {"message_id": int(mid), "author_id": core_config.bot.user.id, "audience_id": audience_id,
        "role": "assistant", "name": "Vivian", "text": content[:1800], "timestamp": time.time()}
    _ai_memory_cache_record(scope, record)
    await vivian_memory_store._ai_memory_save(scope, record, {})


def _ai_memory_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-zа-яё0-9]{3,}", text.lower())
    return {word[:6] if re.search(r"[а-яё]", word) else word for word in words
            if word not in vivian_memory_state.AI_MEMORY_STOP_WORDS}


def _ai_memory_prompt_record(item: dict, text_limit: int, *, include_source: bool = False) -> dict:
    """Archive dates remain data; normalize only the bot's own historical wording."""
    text, name = item["text"], item["name"]
    if item["role"] == "assistant":
        text = vivian_dialogue._normalize_vivian_identity(
            vivian_dialogue._strip_vivian_service_metadata(text), history=True,
        )
        name = "Vivian"
    record = {
        "speaker_id": str(item["author_id"]), "name": name, "role": item["role"],
        "text": text[:text_limit],
        "date": datetime.fromtimestamp(item["timestamp"], core_config.MOSCOW_TIMEZONE).isoformat(),
    }
    if include_source:
        record["message_id"] = str(item["message_id"])
    return record


async def _ai_memory_prepare(message):
    scope = _ai_memory_scope(message, require_history=True)
    key = (message.guild.id if message.guild else 0, message.channel.id, message.author.id)
    history = core_state.chat_history.setdefault(key, [])
    if message.guild is None:
        return history, ""
    if scope is None:
        # A member without history access must not receive an old channel archive.
        return [], ""
    await vivian_memory_store._ai_memory_load(scope)
    tokens = _ai_memory_tokens(message.content or "")
    stored = await vivian_memory_store._ai_memory_read_turn(scope, message.author.id, message.id, tokens)
    purged = controls_store.PURGED_SOURCE_IDS.get(scope, set())
    records_by_id = {item["message_id"]: item for item in stored if item["message_id"] not in purged}
    records_by_id.update({item["message_id"]: item for item in vivian_memory_state.AI_MEMORY_CHANNELS.get(scope, ())})
    records = sorted(records_by_id.values(), key=lambda item: (item["timestamp"], item["message_id"]))
    own = [item for item in records if item["audience_id"] == message.author.id and item["message_id"] < message.id]
    restored = [{"role": item["role"], "content": item["text"] if item["role"] == "assistant"
                 else f"Собеседник: {item['name']}\nСообщение: {item['text']}",
                 "_timestamp": item["timestamp"], "_message_id": item["message_id"]} for item in own[-core_state.MAX_HISTORY:]]
    history[:] = restored or [item for item in history if item.get("_message_id", 0) < message.id]
    facts = vivian_memory_state.AI_MEMORY_FACTS.get(scope, {}).get(message.author.id, {})
    facts = [item for item in facts.values()
             if item.get("text") and item["source"] <= message.id
             and time.time() - item["timestamp"] < vivian_memory_state.AI_MEMORY_FACT_DAYS * 86400]
    current_id = message.id
    # Only past messages from this exact channel; a newer concurrent turn is not the current user's context.
    past = [item for item in records if item["message_id"] < current_id]
    recent = past[-20:]
    recent_ids = {item["message_id"] for item in recent}
    ranked = sorted(((len(tokens & _ai_memory_tokens(item["text"])), item)
                     for item in past if item["message_id"] not in recent_ids),
        key=lambda entry: (entry[0], entry[1]["timestamp"]), reverse=True)
    relevant = [item for overlap, item in ranked if overlap][:8]
    payload = {
        "speaker_id": str(message.author.id),
        "speaker_facts": [{"text": item["text"], "date": datetime.fromtimestamp(item["timestamp"], core_config.MOSCOW_TIMEZONE).isoformat()}
                          for item in sorted(facts, key=lambda item: item["timestamp"], reverse=True)],
        "recent_channel": [_ai_memory_prompt_record(item, 300, include_source=True) for item in recent],
        "related_older_messages": [_ai_memory_prompt_record(item, 450, include_source=True)
                                  for item in sorted(relevant, key=lambda item: item["timestamp"])],
    }
    # Budget by structure: always emit valid JSON, without cutting a fact or an instruction mid-string.
    context = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while len(context) > 11000 and payload["recent_channel"]:
        payload["recent_channel"].pop(0)
        context = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return history, context


def _ai_memory_prompt_turns(history: list[dict]) -> list[tuple[dict, object]]:
    selected, used = [], 0
    for item in reversed(history):
        content = str(item.get("content", ""))[:1800]
        # Timestamps belong to storage/archive data. Adding them to assistant
        # examples teaches the model to reproduce those service headings.
        if item.get("role") == "assistant":
            content = vivian_dialogue._strip_vivian_service_metadata(content)
            content = vivian_dialogue._normalize_vivian_identity(content, history=True)
        if not content:
            continue
        if used + len(content) > vivian_memory_state.AI_MEMORY_PROMPT_CHARS:
            break
        selected.append(({"role": item["role"], "content": content}, item.get("_message_id")))
        used += len(content)
    return list(reversed(selected))


def _ai_memory_prompt_history(history: list[dict]) -> list[dict]:
    return [turn for turn, _ in _ai_memory_prompt_turns(history)]


def _ai_memory_pack_context(history: list[dict], memory_context: str) -> tuple[list[dict], str]:
    """Reference repeated source text once; preserve archive attribution, dates and facts.

    A matching ID is not enough: the selected, actually serialized dialogue must
    also contain the archive's exact text with the same role. Unknown/legacy
    archives and history without source IDs keep their original content.
    """
    selected = _ai_memory_prompt_turns(history)
    prompt_history = [turn for turn, _ in selected]
    if not memory_context:
        return prompt_history, memory_context
    try:
        archive = json.loads(memory_context)
    except (TypeError, ValueError):
        return prompt_history, memory_context
    if not isinstance(archive, dict):
        return prompt_history, memory_context

    def source_id(value):
        if type(value) not in (int, str):
            return None
        try:
            return str(int(value)) if int(value) > 0 else None
        except ValueError:
            return None

    represented = {}
    for index, (turn, mid) in enumerate(selected, 1):
        mid = source_id(mid)
        if mid is not None:
            represented.setdefault(mid, []).append((index, turn))
    changed = False
    for key in ("recent_channel", "related_older_messages"):
        records = archive.get(key)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("text"), str):
                continue
            text = record["text"]
            for index, turn in represented.get(source_id(record.get("message_id")), ()):
                if text and record.get("role") == turn["role"] and text in turn["content"]:
                    # Never remove the speaker/date or copy dates into assistant examples.
                    record.pop("text")
                    record["in_dialogue"] = index
                    changed = True
                    break
    if not changed:
        return prompt_history, memory_context
    archive["dialogue_reference"] = (
        "in_dialogue: номер реплики в следующей истории диалога, начиная с 1; текст уже в ней."
    )
    packed = json.dumps(archive, ensure_ascii=False, separators=(",", ":"))
    # Short replies such as "да" cost less than a reference plus its legend.
    return prompt_history, packed if len(packed) < len(memory_context) else memory_context


async def _ai_memory_ambient_excerpt(message, live_context: list[dict]) -> list[dict]:
    """Only relevant earlier public discussion; do not volunteer personal profile notes."""
    scope = _ai_memory_scope(message, require_history=True)
    if scope is None or not live_context:
        return []
    await vivian_memory_store._ai_memory_load(scope)
    tokens = _ai_memory_tokens(" ".join(item["text"] for item in live_context[-5:]))
    first_id = min(item["message_id"] for item in live_context)
    candidates = [item for item in vivian_memory_state.AI_MEMORY_CHANNELS.get(scope, ())
                  if item["message_id"] < first_id and tokens & _ai_memory_tokens(item["text"])
                  and not vivian_ambient_state.AMBIENT_SENSITIVE_PATTERN.search(item["text"])]
    ranked = sorted(candidates, key=lambda item: (
        len(tokens & _ai_memory_tokens(item["text"])), item["timestamp"]), reverse=True)[:6]
    return [_ai_memory_prompt_record(item, 280) for item in sorted(ranked, key=lambda item: item["timestamp"])]
