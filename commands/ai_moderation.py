from __future__ import annotations
import asyncio
import discord
import hashlib
import json
import re
from core import config as core_config
from commands import antispam as commands_antispam
from core import ai_models as core_ai_models
from core import moderation_state as core_moderation_state


def _track_ad_raid_message(message: discord.Message, now: float) -> list[discord.Message]:
    """Group identical, already-classified ads posted by several accounts."""
    content = re.sub(r"<@!?\d+>", " @user ", (message.content or "").casefold())
    signature = hashlib.sha256(re.sub(r"\s+", " ", content).strip().encode("utf-8")).hexdigest()
    key = (message.guild.id, signature)
    recent = core_moderation_state.ad_raid_tracker[key]
    while recent and now - recent[0][0] >= core_config.AD_RAID_INTERVAL:
        recent.popleft()
    recent.append((now, message.author.id, message))
    if len(core_moderation_state.ad_raid_tracker) > 500:
        for stale_key, stale_messages in list(core_moderation_state.ad_raid_tracker.items()):
            if not stale_messages or now - stale_messages[-1][0] >= core_config.AD_RAID_INTERVAL:
                core_moderation_state.ad_raid_tracker.pop(stale_key, None)

    distinct_accounts = {}
    for _, user_id, item in recent:
        distinct_accounts[user_id] = item
    if len(distinct_accounts) < core_config.AD_RAID_LIMIT:
        return []
    core_moderation_state.ad_raid_tracker.pop(key, None)
    return list(distinct_accounts.values())


async def _classify_ai_moderation(message: discord.Message, now: float) -> str | None:
    """Classify only link/promo or direct-abuse candidates; fail open on errors."""
    model = core_ai_models.get_default_model()
    if not core_ai_models.has_provider(model) or message.guild is None or not (message.content or "").strip():
        return None
    permissions = getattr(message.author, "guild_permissions", None)
    if permissions and (permissions.administrator or permissions.manage_messages):
        return None

    content = message.content.strip()
    ad_candidate = bool(core_config.AI_AD_CANDIDATE_PATTERN.search(content))
    abuse_candidate = bool(core_config.AI_ABUSE_CANDIDATE_PATTERN.search(content)) or bool(
        (message.mentions or message.reference) and core_config.PROFANITY_PATTERN.search(content)
    )
    if not ad_candidate and not abuse_candidate:
        return None

    user_key = (message.guild.id, message.author.id)
    if now - core_moderation_state.ai_moderation_last_check[user_key] < core_config.AI_MODERATION_COOLDOWN:
        return None
    guild_calls = core_moderation_state.ai_moderation_guild_calls[message.guild.id]
    while guild_calls and now - guild_calls[0] >= core_config.AI_MODERATION_GUILD_INTERVAL:
        guild_calls.popleft()
    if len(guild_calls) >= core_config.AI_MODERATION_GUILD_LIMIT:
        return None
    core_moderation_state.ai_moderation_last_check[user_key] = now
    guild_calls.append(now)

    system_prompt = (
        "Ты — строгий классификатор модерации Discord. Верни только JSON: "
        '{"category":"none|advertisement|targeted_harassment","confidence":0.0}. '
        "advertisement ставь только для навязчивого продвижения, рекламы, набора на сервер, "
        "реферальных ссылок или очевидного мошеннического предложения; обычная ссылка, "
        "обсуждение покупки или ответ на запрос не являются рекламой. targeted_harassment — "
        "прямое оскорбление, травля или угроза конкретному участнику. Не помечай спор, шутку, "
        "цитату, ругань без направленной атаки, короткие ответы вроде «да/нет» или критику идеи. "
        "Если ссылка является ответом на просьбу или обсуждается по теме, выбирай none. "
        "При сомнении выбирай none. Используй только текст и явно переданный контекст."
    )
    try:
        reference = getattr(message.reference, "resolved", None) if message.reference else None
        reference_content = getattr(reference, "content", "")
        classification_input = f"Сообщение: {content[:1400]}"
        if isinstance(reference_content, str) and reference_content.strip():
            classification_input += f"\nКонтекст ответа: {reference_content[:400]}"
        loop = asyncio.get_running_loop()
        options = {"max_completion_tokens": 384}
        if model.lower().startswith("openai/gpt-oss-"):
            options["reasoning_effort"] = "low"
        completion = await loop.run_in_executor(
            None,
            lambda: core_ai_models.create_completion(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": classification_input},
                ],
                **options,
            ),
        )
        raw = completion.choices[0].message.content
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            json_match = re.search(r"\{[^{}]*\}", raw)
            if json_match is None:
                return None
            result = json.loads(json_match.group(0))
        category = result.get("category")
        confidence = float(result.get("confidence", 0.0))
        if not 0.92 <= confidence <= 1.0 or category not in {"advertisement", "targeted_harassment"}:
            return None
        return category
    except Exception as error:
        print(
            f"[AI Moderation] Classification unavailable; message_id={message.id}; "
            f"guild_id={message.guild.id}; error={type(error).__name__}: {error}",
            flush=True,
        )
        return None


async def _moderate_ai_content(message: discord.Message, now: float) -> bool:
    category = await _classify_ai_moderation(message, now)
    if category is None:
        return False

    raid_messages = _track_ad_raid_message(message, now) if category == "advertisement" else []
    if category == "advertisement":
        reason = "нежелательная реклама"
        if raid_messages:
            reason = f"рекламный рейд: одинаковый текст от {len(raid_messages)} аккаунтов"
            messages = raid_messages
        else:
            messages = [message]
        strike_category = "ai_advertisement"
    else:
        reason = "направленная травля или оскорбление"
        messages = [message]
        strike_category = "ai_harassment"

    await commands_antispam._record_moderation_incident(
        message,
        strike_category,
        reason,
        messages,
        now,
    )
    return True
