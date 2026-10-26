"""Owner-only, ephemeral presentation of explicitly remembered personal facts."""

from datetime import datetime
import re
import time

import discord

from core import config, database
from vivian import memory_state, memory_store
from utils import components_v2 as v2


MEMORY_QUESTION_PATTERN = re.compile(
    r"\b(?:что\s+(?:ты\s+)?(?:обо?\s+мне|про\s+меня)\s+(?:помнишь|знаешь)|"
    r"что\s+(?:ты\s+)?(?:помнишь|знаешь)\s+(?:обо?\s+мне|про\s+меня)|"
    r"(?:покажи|открой)\s+(?:мою\s+)?(?:личную\s+)?память|"
    r"какие\s+(?:факты|сведения)\s+(?:ты\s+)?(?:обо?\s+мне|про\s+меня))\b",
    re.IGNORECASE,
)


async def private_notice(interaction, text):
    try:
        if interaction.response.is_done():
            await v2.followup(interaction, text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await v2.send_response(interaction, text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    except (discord.NotFound, discord.HTTPException):
        return


async def authorize_private(interaction, owner_id, guild_id, channel_id):
    guild, channel = interaction.guild, interaction.channel
    if interaction.user.id != owner_id or guild is None or guild.id != guild_id or interaction.channel_id != channel_id:
        await private_notice(interaction, "Личная память доступна только владельцу этого экрана.")
        return False
    me = guild.me
    viewer = guild.get_member(owner_id) or interaction.user
    if me is None or channel is None:
        await private_notice(interaction, "Этот канал сейчас недоступен.")
        return False
    bot_rights, user_rights = channel.permissions_for(me), channel.permissions_for(viewer)
    if not (bot_rights.view_channel and bot_rights.read_message_history
            and user_rights.view_channel and user_rights.read_message_history):
        await private_notice(interaction, "Для этого экрана нужен доступ к истории текущего канала.")
        return False
    return True


async def fact_snapshot(scope, owner_id):
    await memory_store._ai_memory_load(scope)
    facts = memory_state.AI_MEMORY_FACTS.get(scope, {}).get(owner_id, {})
    cutoff = time.time() - memory_state.AI_MEMORY_FACT_DAYS * 86400
    return sorted(((key, dict(entry)) for key, entry in facts.items()
                   if entry.get("text") and entry["timestamp"] >= cutoff),
                  key=lambda item: (item[1]["timestamp"], item[0]), reverse=True)


def fact_card(view, interaction, notice=None):
    total = len(view.facts)
    lines = ["Только тебе. Сведения из текущего канала."]
    if notice:
        lines.append(notice)
    shown = view.facts[view.page * 5:(view.page + 1) * 5]
    if not shown:
        lines.append("Личных фактов пока не сохраняла.")
    embed = discord.Embed(title="Vivian // ЛИЧНАЯ ПАМЯТЬ", description="\n\n".join(lines), color=config.VIVIAN_ACCENT)
    plain = [embed.title, *lines]
    for index, (key, entry) in enumerate(shown, start=view.page * 5 + 1):
        text = discord.utils.escape_mentions(discord.utils.escape_markdown(entry["text"]))
        date = datetime.fromtimestamp(entry["timestamp"], config.MOSCOW_TIMEZONE)
        mark = " · выбран" if key == view.selected_key else ""
        embed.add_field(name=f"{index:02d}{mark} · {date:%d.%m.%Y}", value=text[:300], inline=False)
        plain.append(f"{index:02d}. {text}")
    mode = "Supabase" if database.db_pool is not None and memory_state.AI_MEMORY_READY else "RAM"
    embed.set_footer(text=f"{total} фактов · страница {view.page + 1}/{max(1, (total + 4) // 5)} · {mode} // VIVIAN")
    if not interaction.channel.permissions_for(interaction.guild.me).embed_links:
        return "\n\n".join(plain)[:1900], None
    return None, embed


async def open_private_memory(interaction, *, owner_id, guild_id, channel_id):
    from vivian.memory_controls import MemoryCardView
    if not await authorize_private(interaction, owner_id, guild_id, channel_id):
        return
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        view = MemoryCardView(owner_id, guild_id, channel_id)
        await view.refresh_facts()
        if not await authorize_private(interaction, owner_id, guild_id, channel_id):
            return
        content, embed = fact_card(view, interaction)
        view.panel_message = await v2.followup(
            interaction,
            content, embed=embed, view=view, ephemeral=True, wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.NotFound, discord.HTTPException):
        return


class MemoryInvitationView(discord.ui.View):
    def __init__(self, owner_id, guild_id, channel_id):
        super().__init__(timeout=600)
        self.owner_id, self.guild_id, self.channel_id = owner_id, guild_id, channel_id

    async def interaction_check(self, interaction):
        return await authorize_private(interaction, self.owner_id, self.guild_id, self.channel_id)

    @discord.ui.button(label="Моя память", style=discord.ButtonStyle.secondary)
    async def reveal(self, interaction, button):
        await open_private_memory(interaction, owner_id=self.owner_id,
                                  guild_id=self.guild_id, channel_id=self.channel_id)


def make_memory_invitation(message):
    if message.guild is None:
        return "Личная память доступна на сервере через твой профиль /rank.", None
    return ("Покажу сохранённые сведения лично тебе. Открой «Моя память».",
            MemoryInvitationView(message.author.id, message.guild.id, message.channel.id))
