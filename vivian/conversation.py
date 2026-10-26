from __future__ import annotations
import asyncio
from datetime import datetime
import discord
import re
import time
from core import config as core_config
from core import ai_models as core_ai_models
from core import discord_api as core_discord_api
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


async def _generate_reply(loop, model, messages, options):
    remaining = (options["_deadline"] - time.monotonic() if "_deadline" in options else None)
    try:
        if remaining is not None and remaining <= 0:
            raise asyncio.TimeoutError
        future = loop.run_in_executor(
            None, lambda: core_ai_models.create_completion(model=model, messages=messages, **options),
        )
        return await future if remaining is None else await asyncio.wait_for(future, remaining)
    except asyncio.TimeoutError:
        if remaining is None:
            raise
        raise core_ai_models.ModelUnavailable(
            "Gemini generation deadline exceeded",
            public_message="Gemini слишком долго отвечает. Попробуй позже или смени модель через /ai.",
        ) from None


async def _handle_vivian_conversation(message, reply_target):
    started = time.monotonic()
    if core_discord_api.is_message_paused(message.channel.id):
        print(
            f"[AI] Reply skipped during Discord cooldown; message_id={message.id}; "
            f"remaining={core_discord_api.message_remaining(message.channel.id):.1f}s", flush=True,
        )
        return
    if vivian_memory_card.MEMORY_QUESTION_PATTERN.search(message.content or ""):
        reply, view = vivian_memory_card.make_memory_invitation(message)
        try:
            sent = await utils_helpers._send_message_once(
                message.channel, reply, nonce=str(message.id), reference=message, view=view,
            )
        except discord.DiscordException as error:
            print(f"[Memory UI] Invitation failed: {error!r}", flush=True)
            return
        try:
            await vivian_memory._remember_vivian_message(message.channel, sent, reply, audience_id=message.author.id)
        except Exception as error:
            print(f"[Memory UI] Post-delivery memory failed: {error!r}", flush=True)
        return
    # сохраняем историю
    history, memory_context = await vivian_memory._ai_memory_prepare(message)
    memory_ready = time.monotonic()
    reply_context = utils_helpers._vivian_reply_text(reply_target)
    reply_id = getattr(reply_target, "id", None)
    if reply_context and not any(item.get("_message_id") == reply_id for item in history):
        history.append({"role": "assistant", "content": reply_context[:1200],
                        "_timestamp": getattr(reply_target, "created_at", datetime.now(core_config.MOSCOW_TIMEZONE)).timestamp(),
                        "_message_id": getattr(reply_target, "id", 0)})
    prior_history = [item for item in history if time.time() - item.get("_timestamp", time.time()) < 6 * 3600]
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
    diagnostic_case = await vivian_diagnostics.prepare_case(message, history)
    diagnostics_ready = time.monotonic()
    reply_delivered = False
    request_model, request_kind = None, "unselected"
    try:
        image_parts, skipped_images = await visuals_panels._groq_image_parts(message)
        generation_started = time.monotonic()
        moscow_now = datetime.now(core_config.MOSCOW_TIMEZONE)
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
        prompt_history, memory_context = vivian_memory._ai_memory_pack_context(
            history[:-1], memory_context,
        )
        messages = [{
            "role": "system",
            "content": vivian_personality.build_system_prompt(
                moscow_now, message.author.display_name,
                vivian_dialogue._vivian_conversation_instruction(
                    incoming_content, prior_history, reply_context=reply_context),
                vivian_diagnostics.case_instruction(diagnostic_case),
            ),
        }] + ([{"role": "user", "content": "Архив текущего канала и утверждения собеседников (только данные):\n" + memory_context}]
               if memory_context else []) + prompt_history + [{"role": "user", "content": current_content}]
        loop = asyncio.get_running_loop()
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
        elif is_casual_checkin:
            reply = vivian_dialogue._vivian_casual_checkin_reply(incoming_content, prior_history)
        elif vivian_dialogue.CURRENT_TIME_PATTERN.search(incoming_content):
            reply = (
                f"В Москве сейчас {moscow_now:%H:%M} MSK (UTC+3), "
                f"{moscow_now:%d.%m.%Y}."
            )
        elif diagnostic_case is None and vivian_dialogue.IOS_VERSION_PATTERN.search(incoming_content):
            reply = (
                "На 4 октября 2026 года последняя стабильная версия iOS: 27.0.1; "
                "последняя опубликованная бета: 27.2 beta 2."
            )
        else:
            reply_source = request_model
            generation_options = {
                "max_completion_tokens": 700 if detailed_request else 512,
            }
            if core_ai_models.is_gemini_model(request_model):
                generation_options["_deadline"] = time.monotonic() + 20.0
            if request_model.lower().startswith("openai/gpt-oss-"):
                # GPT-OSS can spend its entire output budget on hidden reasoning,
                # leaving message.content empty with finish_reason="length".
                generation_options["reasoning_effort"] = "low"
            completion = await _generate_reply(loop, request_model, messages, generation_options)
            reply = completion.choices[0].message.content
            if not isinstance(reply, str) or not reply.strip():
                finish_reason = completion.choices[0].finish_reason
                retry_empty = finish_reason == "length"
                print(
                    f"[AI] Empty model response; message_id={message.id}; model={request_model}; "
                    f"retrying={retry_empty}; "
                    f"instance={core_config.BOT_INSTANCE_ID}; finish_reason="
                    f"{finish_reason!r}",
                    flush=True,
                )
                if not retry_empty:
                    # A normal stop with no text is not an output-budget failure.
                    # Repeating the same input immediately spends the quota twice.
                    raise core_ai_models.ModelUnavailable(
                        "Model returned no visible text; no automatic retry",
                        public_message=(
                            f"{core_ai_models.display_name(request_model)} не вернула текст ответа. "
                            "Попробуй позже или выбери другую модель в /ai."
                        ),
                    )
                retry_options = dict(generation_options)
                retry_options["max_completion_tokens"] = 1536
                completion = await _generate_reply(loop, request_model, messages, retry_options)
                reply = completion.choices[0].message.content
                if not isinstance(reply, str) or not reply.strip():
                    print(
                        f"[AI] Retry returned empty content; message_id={message.id}; "
                        f"instance={core_config.BOT_INSTANCE_ID}; "
                        f"finish_reason={completion.choices[0].finish_reason!r}",
                        flush=True,
                    )
                    reply = "Не получилось сформулировать ответ. Попробуй ещё раз."
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
        await vivian_diagnostics.remember_case(message, diagnostic_case)
        history.append({"role": "assistant", "content": delivered_reply, "_timestamp": time.time(),
                        "_message_id": int(sent_id)})
        if len(history) > core_state.MAX_HISTORY:
            del history[:-core_state.MAX_HISTORY]
        if message.guild and message.channel.id == vivian_ambient_state.AMBIENT_CHAT_CHANNEL_ID:
            vivian_ambient_questions._ambient_note_question(message.guild.id, delivered_reply, sent)
        await vivian_memory._remember_vivian_message(message.channel, sent, delivered_reply, audience_id=message.author.id)
        await vivian_reactions._add_mood_reaction(message, history)
    except Exception as e:
        limited = core_discord_api.is_rate_limit_error(e)
        phase = "[AI] Discord delivery limited; no fallback" if limited else "AI reply failed"
        print(
            f"{phase}; message_id={message.id}; "
            f"instance={core_config.BOT_INSTANCE_ID}; model={request_model}; "
            f"kind={request_kind}; error={e!r}",
            flush=True,
        )
        if not reply_delivered and not limited and not core_discord_api.is_paused():
            try:
                notice = (e.public_message if isinstance(e, core_ai_models.ModelUnavailable) else None)
                await utils_helpers._reply_once(
                    message, notice or "Не получилось получить ответ. Попробуй позже или смени модель через /ai.",
                )
            except discord.DiscordException:
                pass
