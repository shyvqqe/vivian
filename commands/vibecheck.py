from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict

import discord
from discord import app_commands

from core import ai_models as core_ai_models, config as core_config
from utils import helpers as utils_helpers

COOLDOWN_SECONDS = 60 * 60
HISTORY_SCAN_LIMIT = 300
MESSAGE_LIMIT = 50

_last_check: dict[tuple[int, int], float] = defaultdict(float)


async def _collect_messages(channel: discord.abc.Messageable, user_id: int) -> list[str]:
    texts = []
    async for message in channel.history(limit=HISTORY_SCAN_LIMIT):
        if message.author.id == user_id and (message.content or "").strip():
            texts.append(message.content.strip())
            if len(texts) >= MESSAGE_LIMIT:
                break
    texts.reverse()
    return texts


@core_config.bot.tree.command(name="vibecheck", description="ИИ оценит твой вайб по недавним сообщениям в этом канале.")
@app_commands.guild_only()
async def vibecheck(interaction: discord.Interaction):
    model = core_ai_models.get_default_model()
    if not core_ai_models.has_provider(model):
        return await utils_helpers.vivian_reply(
            interaction, "AI-провайдер сейчас не настроен.", error=True, ephemeral=True)

    key = (interaction.guild_id, interaction.user.id)
    now = time.time()
    remaining = COOLDOWN_SECONDS - (now - _last_check[key])
    if remaining > 0:
        minutes = int(remaining // 60) + 1
        return await utils_helpers.vivian_reply(
            interaction, f"Можно проверить вайб снова через ~{minutes} мин.", error=True, ephemeral=True)

    await interaction.response.defer(ephemeral=False, thinking=True)
    try:
        messages = await _collect_messages(interaction.channel, interaction.user.id)
    except discord.DiscordException:
        return await interaction.edit_original_response(content="Не удалось прочитать историю канала.")
    if len(messages) < 5:
        return await interaction.edit_original_response(
            content="Маловато сообщений в этом канале для честной оценки. Попробуй там, где ты чаще пишешь.")

    _last_check[key] = now
    system_prompt = (
        "Ты — остроумный, но не злой классификатор вайба для Discord. Тебе дают до 50 последних "
        "сообщений одного пользователя из одного канала. Верни только JSON: "
        '{"rating": 0-100, "archetype": "строка до 24 символов", "joke": "одна короткая шутка"}. '
        "Будь дружелюбным, не оскорбляй и не делай выводов о реальной личности человека, "
        "это просто развлечение. Пиши по-русски."
    )
    try:
        loop = asyncio.get_running_loop()
        completion = await loop.run_in_executor(
            None,
            lambda: core_ai_models.create_completion(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "\n".join(messages)[:4000]},
                ],
                max_completion_tokens=256,
            ),
        )
        raw = completion.choices[0].message.content
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match is None:
                raise ValueError("no json")
            result = json.loads(match.group(0))
        rating = max(0, min(100, int(result.get("rating", 0))))
        archetype = str(result.get("archetype", "Загадка"))[:24]
        joke = str(result.get("joke", ""))[:200]
    except Exception as error:
        print(f"[Vibecheck] Unavailable; user_id={interaction.user.id}; error={type(error).__name__}", flush=True)
        return await interaction.edit_original_response(content="Не удалось оценить вайб. Попробуй позже.")

    embed = utils_helpers.vivian_embed(joke, title=f"Вайб-чек · {interaction.user.display_name}")
    embed.add_field(name="РЕЙТИНГ", value=f"{rating}/100", inline=True)
    embed.add_field(name="АРХЕТИП", value=discord.utils.escape_markdown(archetype), inline=True)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text=f"По сообщениям в этом канале · {core_config.VIVIAN_FOOTER}")
    await interaction.edit_original_response(content=None, embed=embed)
