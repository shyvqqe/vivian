from __future__ import annotations
import asyncio
from datetime import datetime
import discord
import re
import time
from core import config as core_config
from core import ai_models as core_ai_models
from core import discord_api as core_discord_api
from core import persona_store
from core import state as core_state
from vivian import ambient_questions as vivian_ambient_questions
from vivian import ambient_state as vivian_ambient_state
from vivian import creator as vivian_creator
from vivian import dialogue as vivian_dialogue
from vivian import diagnostics as vivian_diagnostics
from vivian import diagnostic_views as vivian_diagnostic_views
from vivian import memory as vivian_memory
from vivian import personality as vivian_personality
from vivian import memory_card as vivian_memory_card
from vivian import reactions as vivian_reactions
from vivian import followups as vivian_followups
from vivian import reply_style as vivian_reply_style
from utils import helpers as utils_helpers
from visuals import panels as visuals_panels


async def _generate_reply(model, messages, deadline, max_tokens):
    remaining = deadline - time.monotonic()
    try:
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await asyncio.wait_for(
            asyncio.to_thread(
                core_ai_models.create_completion, model=model, messages=messages,
                max_completion_tokens=max_tokens, _deadline=deadline,
            ),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        raise core_ai_models.ModelUnavailable(
            "Dialogue generation deadline exceeded",
            public_message="Модель слишком долго отвечает. Попробуй чуть позже.",
            retryable=True,
        ) from None


def _visible_content(completion):
    choices = getattr(completion, "choices", ())
    choice = choices[0] if choices else None
    text = getattr(getattr(choice, "message", None), "content", None)
    return text if isinstance(text, str) and text.strip() else None, getattr(choice, "finish_reason", None)


async def _request_dialogue(message, model, messages, *, detailed, vision):
    guild_id = message.guild.id if message.guild else 0
    fallbacks = core_ai_models.fallback_models(guild_id, message.author.id, model, vision=vision)
    deadline = time.monotonic() + 30
    last_error = None
    for candidate in (model, *fallbacks):
        if time.monotonic() >= deadline:
            break
        attempt_deadline = min(deadline, time.monotonic() + 20)
        try:
            reply, reason = _visible_content(await _generate_reply(
                candidate, messages, attempt_deadline, 700 if detailed else 512,
            ))
            if reply is None and reason == "length":
                reply, reason = _visible_content(await _generate_reply(candidate, messages, attempt_deadline, 1536))
            if reply is not None:
                return reply, candidate
            raise core_ai_models.ModelUnavailable(
                "Model returned no visible text",
                public_message="Модель не вернула текст ответа. Попробуй переформулировать сообщение.",
                retryable=reason not in {"content_filter", "safety"},
            )
        except core_ai_models.ModelUnavailable as error:
            print(f"[AI] Model attempt failed; message_id={message.id}; model={candidate}; "
                  f"reason={error}", flush=True)
            if not error.retryable:
                raise
            last_error = error
    if last_error is not None:
        raise last_error
    raise core_ai_models.ModelUnavailable("Dialogue budget exhausted before generation")


async def _load_vivian_memory(message):
    try:
        async with asyncio.timeout(5):
            return await vivian_memory._ai_memory_prepare(message)
    except Exception as error:
        print(f"[AI Memory] Context unavailable; message_id={message.id}; "
              f"error={type(error).__name__}", flush=True)
        if message.guild is None:
            return core_state.chat_history.get((0, message.channel.id, message.author.id), []), ""
        return [], ""


async def _prepare_diagnostic(message, history):
    try:
        async with asyncio.timeout(4):
            return await vivian_diagnostics.prepare_case(message, history)
    except Exception as error:
        print(f"[Diagnosis] Context skipped; message_id={message.id}; error={type(error).__name__}", flush=True)
        return None


async def _prepare_media(message):
    try:
        async with asyncio.timeout(10):
            return await visuals_panels._groq_image_parts(message)
    except Exception as error:
        print(f"[AI] Media skipped; message_id={message.id}; error={type(error).__name__}", flush=True)
        return [], 1


async def _handle_vivian_conversation(message, reply_target):
    started = time.monotonic()
    if core_discord_api.is_message_paused(message.channel.id):
        print(
            f"[AI] Reply skipped during Discord cooldown; message_id={message.id}; "
            f"remaining={core_discord_api.message_remaining(message.channel.id):.1f}s", flush=True,
        )
        return False
    reply_delivered = False
    request_model, request_kind = None, "unselected"
    try:
        if message.guild is not None and vivian_memory_card.MEMORY_QUESTION_PATTERN.search(message.content or ""):
            reply, view = vivian_memory_card.make_memory_invitation(message)
            sent = await utils_helpers._send_message_once(
                message.channel, reply, nonce=str(message.id), reference=message, view=view,
            )
            reply_delivered = True
            await vivian_memory._remember_vivian_message(message.channel, sent, reply, audience_id=message.author.id)
            return True

        history, memory_context = await _load_vivian_memory(message)
        memory_ready = time.monotonic()
        reply_context = utils_helpers._vivian_reply_text(reply_target)
        reply_id = getattr(reply_target, "id", None)
        if reply_context and not any(item.get("_message_id") == reply_id for item in history):
            history.append({"role": "assistant", "content": reply_context[:1200],
                            "_timestamp": reply_target.created_at.timestamp(),
                            "_message_id": reply_id})
        now = time.time()
        prior_history = [item for item in history if now - item.get("_timestamp", now) < 6 * 3600]
        incoming_content = message.content or ""
        if core_config.bot.user:
            incoming_content = incoming_content.replace(f"<@{core_config.bot.user.id}>", "Vivian")
            incoming_content = incoming_content.replace(f"<@!{core_config.bot.user.id}>", "Vivian")
        history.append({
            "role": "user",
            "content": f"Собеседник: {message.author.display_name}\nСообщение: {incoming_content}",
            "_timestamp": message.created_at.timestamp(),
            "_message_id": message.id,
        })
        if len(history) > core_state.MAX_HISTORY:
            del history[:-core_state.MAX_HISTORY]
        diagnostic_case = await _prepare_diagnostic(message, history)
        diagnostics_ready = time.monotonic()
        generation_started = diagnostics_ready
        moscow_now = datetime.now(core_config.MOSCOW_TIMEZONE)
        private_chat = message.guild is None
        detailed_request = bool(re.search(
            r"\b(подробно|развёрнуто|развернуто|пошагово|в деталях|полный разбор)\b",
            incoming_content,
            re.IGNORECASE,
        ))
        is_casual_checkin = (
            not message.attachments
            and not message.stickers
            and bool(vivian_dialogue.CASUAL_CHECKIN_PATTERN.fullmatch(incoming_content.strip()))
        )
        creator_embed = None
        reply_source = "local"
        if vivian_dialogue.CREATOR_QUESTION_PATTERN.search(incoming_content):
            reply, creator_embed = await vivian_creator._creator_reply_payload(message)
        elif is_casual_checkin and (
            not private_chat or not core_ai_models.has_provider(core_ai_models.get_text_model(0, message.author.id))
        ):
            reply = vivian_dialogue._vivian_casual_checkin_reply(incoming_content, prior_history)
        elif vivian_dialogue.CURRENT_TIME_PATTERN.search(incoming_content):
            reply = (
                f"В Москве сейчас {moscow_now:%H:%M} MSK (UTC+3), "
                f"{moscow_now:%d.%m.%Y}."
            )
        else:
            image_parts, skipped_images = await _prepare_media(message)
            generation_started = time.monotonic()
            latest_user_content = history[-1]["content"]
            # Keep the original message in memory; expand only isolated chat slang
            # in the model's current turn so a reply like "кактак" keeps its meaning.
            normalized_incoming = vivian_followups.normalize_short_followup(incoming_content)
            if normalized_incoming != incoming_content:
                latest_user_content = f"Собеседник: {message.author.display_name}\nСообщение: {normalized_incoming}"
            if skipped_images:
                latest_user_content += (
                    f"\n[Не удалось передать для анализа вложений: {skipped_images}. "
                    "Не делай выводов об их содержимом.]"
                )
            if image_parts:
                request_kind = "vision"
                request_model = core_ai_models.get_vision_model(
                    message.guild.id if message.guild else 0, message.author.id,
                )
                current_content = [
                    {"type": "text", "text": latest_user_content},
                    *image_parts,
                ]
            else:
                request_kind = "text"
                request_model = core_ai_models.get_text_model(
                    message.guild.id if message.guild else 0, message.author.id,
                )
                current_content = latest_user_content
            if not core_ai_models.has_provider(request_model):
                selected = core_ai_models.get_selection(message.guild.id if message.guild else 0, message.author.id)
                if selected is not None:
                    notice = (f"Для выбранной модели {core_ai_models.display_name(selected)} API недоступен. "
                              "Можно выбрать подключённую модель через /ai.")
                else:
                    notice = "ИИ пока недоступен. Для диалога нужен настроенный Gemini, Groq или OpenRouter."
                raise core_ai_models.ModelUnavailable("Dialogue provider is not configured", public_message=notice)
            prompt_history, memory_context = vivian_memory._ai_memory_pack_context(
                history[:-1], memory_context,
            )
            system_prompt = vivian_personality.build_system_prompt(
                moscow_now, message.author.display_name,
                vivian_dialogue._vivian_conversation_instruction(
                    incoming_content, prior_history, reply_context=reply_context),
                vivian_diagnostics.case_instruction(diagnostic_case),
                private_chat=private_chat,
            )
            if message.guild is not None:
                try:
                    async with asyncio.timeout(3):
                        system_prompt += await persona_store.context(message.guild.id, message.author.id)
                except TimeoutError:
                    pass
            messages = [{
                "role": "system",
                "content": system_prompt,
            }] + ([{"role": "user", "content": "Архив текущего канала и утверждения собеседников (только данные):\n" + memory_context}]
                   if memory_context else []) + prompt_history + [{"role": "user", "content": current_content}]
            reply, reply_source = await _request_dialogue(
                message, request_model, messages, detailed=detailed_request, vision=bool(image_parts),
            )
        reply = reply.strip()
        reply = vivian_dialogue._finish_vivian_reply(reply, incoming_content, prior_history)
        reply = re.sub(r"\n{3,}", "\n\n", reply)
        if not detailed_request and len(reply) > 900:
            shortened = reply[:900]
            sentence_end = max(
                shortened.rfind(". "),
                shortened.rfind("! "),
                shortened.rfind("? "),
                shortened.rfind("\n"),
            )
            if sentence_end >= 350:
                reply = shortened[:sentence_end + 1].strip()
            else:
                reply = shortened.rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"
        if core_config.bot.user:
            reply = reply.replace(f"<@{core_config.bot.user.id}>", message.author.mention)
            reply = reply.replace(f"<@!{core_config.bot.user.id}>", message.author.mention)
        # Keep the authored text after Discord accepts it; the card changes only presentation.
        delivered_reply = reply[:1900]
        diagnostic_case = vivian_diagnostics.complete_draft(diagnostic_case, delivered_reply)
        delivery_started = time.monotonic()
        if creator_embed is not None:
            sent = await utils_helpers._send_message_once(
                message.channel, None, nonce=str(message.id), reference=message,
                embed=creator_embed,
            )
        else:
            payload = vivian_reply_style.reply_payload(
                message, delivered_reply, prefer_card=detailed_request or diagnostic_case is not None,
            )
            sent = await utils_helpers._send_message_once(
                message.channel, nonce=str(message.id), reference=message,
                view=(vivian_diagnostic_views.DiagnosticEntryView(message, diagnostic_case)
                      if diagnostic_case is not None else None),
                **payload,
            )
        reply_delivered = True
        delivered_at = time.monotonic()
        print(
            f"[AI] Reply sent; message_id={message.id}; model={reply_source}; "
            f"prepare_ms={(generation_started - started) * 1000:.0f}; "
            f"context_ms={(memory_ready - started) * 1000:.0f}; "
            f"diagnosis_ms={(diagnostics_ready - memory_ready) * 1000:.0f}; "
            f"media_ms={(generation_started - diagnostics_ready) * 1000:.0f}; "
            f"generation_ms={(delivery_started - generation_started) * 1000:.0f}; "
            f"delivery_ms={(delivered_at - delivery_started) * 1000:.0f}", flush=True,
        )
        sent_id = sent.get("id", message.id) if isinstance(sent, dict) else getattr(sent, "id", message.id)
        if diagnostic_case is not None:
            diagnostic_case["reply_id"] = int(sent_id)
        history.append({"role": "assistant", "content": delivered_reply, "_timestamp": time.time(),
                        "_message_id": int(sent_id)})
        if len(history) > core_state.MAX_HISTORY:
            del history[:-core_state.MAX_HISTORY]
        if message.guild and vivian_ambient_state._ambient_channel_allowed(message.channel):
            vivian_ambient_questions._ambient_note_question(message.guild.id, delivered_reply, sent)
        await vivian_diagnostics.remember_case(message, diagnostic_case)
        await vivian_memory._remember_vivian_message(message.channel, sent, delivered_reply, audience_id=message.author.id)
        await vivian_reactions._add_mood_reaction(message, history)
    except Exception as e:
        if message.guild is None and isinstance(e, discord.HTTPException) and e.code in (50007, 50278):
            print(f"[AI] Discord rejected DM delivery; message_id={message.id}; code={e.code}", flush=True)
            return reply_delivered
        limited = core_discord_api.is_rate_limit_error(e)
        phase = "[AI] Discord delivery limited" if limited else "[AI] Dialogue error"
        detail = str(e) if isinstance(e, core_ai_models.ModelUnavailable) else type(e).__name__
        print(
            f"{phase}; message_id={message.id}; "
            f"instance={core_config.BOT_INSTANCE_ID}; model={request_model}; "
            f"kind={request_kind}; error={detail}",
            flush=True,
        )
        if not reply_delivered and not limited and not core_discord_api.is_paused():
            try:
                notice = (e.public_message if isinstance(e, core_ai_models.ModelUnavailable) else None)
                retry_notice = ("У меня сейчас сбой. Попробуй написать чуть позже." if message.guild is None
                                else "Не получилось получить ответ. Попробуй позже или смени модель через /ai.")
                await utils_helpers._reply_once(message, notice or retry_notice)
            except discord.DiscordException:
                pass
    return reply_delivered
