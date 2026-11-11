from __future__ import annotations
from discord import app_commands
from datetime import datetime, timezone
import time
import discord
from core import config as core_config
from commands import activity as commands_activity
from commands import card as profile_cards, profile_data
from core import state as core_state
from core import chat_impact_state
from vivian import chat_impact
from vivian import memory_card as vivian_memory_card
from utils import helpers as utils_helpers
from utils import command_responses as utils_command_responses
from utils import v2_content


RANK_SECTIONS = (
    ("Общение", (
        ("👋", "Первое слово", "Первое текстовое сообщение", "word_messages", 1),
        ("💬", "Разговорчивый", "100 сообщений на сервере", "messages", 100),
        ("", "Свой голос", "300 сообщений на сервере", "messages", 300),
        ("", "Голос сообщества", "1 000 сообщений на сервере", "messages", 1000),
        ("", "Большая история", "10 000 сообщений на сервере", "messages", 10000),
        ("", "Есть что сказать", "10 текстов длиннее 100 символов", "long_messages", 10),
    )),
    ("Присутствие", (
        ("", "Первая неделя", "7 дней с момента вступления", "joined_days", 7),
        ("", "Свой человек", "30 дней с момента вступления", "joined_days", 30),
    )),
)


RANK_ACHIEVEMENT_TOTAL = sum(len(achievements) for _, achievements in RANK_SECTIONS)


def _rank_page_text(guild_id: int, member: discord.Member, page: int) -> str:
    section_name, achievements = RANK_SECTIONS[page]
    stats = dict(core_state.RANK_ACTIVITY.get((guild_id, member.id), {}))
    stats["joined_days"] = max(0, (datetime.now(timezone.utc) - member.joined_at).days) if member.joined_at else 0
    unlocked = [
        metric is not None and stats.get(metric, 0) >= threshold
        for _, _, _, metric, threshold in achievements
    ]
    unlocked_count = sum(unlocked)
    total_unlocked = sum(
        1
        for section_index, (_, section_achievements) in enumerate(RANK_SECTIONS)
        for _, _, _, metric, threshold in section_achievements
        if metric is not None and stats.get(metric, 0) >= threshold
    )
    filled = round(total_unlocked / RANK_ACHIEVEMENT_TOTAL * 12)
    lines = [
        f"◆ **Достижения — {member.mention}**",
        f"{'▰' * filled}{'▱' * (12 - filled)}  {total_unlocked} из {RANK_ACHIEVEMENT_TOTAL}",
        "",
        f"**{section_name}** — {unlocked_count} из {len(achievements)}",
        "",
    ]
    for is_unlocked, (_, title, description, _, _) in zip(unlocked, achievements):
        if is_unlocked:
            lines.append(f"◆ **{title}**")
            lines.append(f"    *{description}*")
        else:
            lines.append(f"◇ *{description}*")
    lines.extend(("", f"*Раздел {page + 1} из {len(RANK_SECTIONS)}*"))
    return "\n".join(lines)


def _member_trace_embed(guild_id: int, member: discord.Member, requester_id: int | None = None) -> discord.Embed:
    stats = core_state.RANK_ACTIVITY.get((guild_id, member.id), {})
    messages = max(0, stats.get("messages", 0))
    texts = max(0, stats.get("word_messages", 0))
    characters = max(0, stats.get("characters", 0))
    long_texts = min(texts, max(0, stats.get("long_messages", 0)))
    long_share = long_texts / texts if texts else 0
    today = commands_activity._activity_day_snapshot(guild_id)
    today_messages = next((record["messages"] for uid, record in today["records"] if uid == member.id), 0)
    share = today_messages / today["total"] if today["total"] else 0
    filled = min(12, round(share * 12))
    embed = utils_helpers.vivian_embed(f"{member.mention}\nСлед, который остаётся в разговорах.", title="След участника")
    utils_helpers.vivian_brand(embed, "Vivian / MEMBER TRACE")
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ВСЕГО СООБЩЕНИЙ", value=f"**{messages:,}**".replace(",", " "), inline=True)
    embed.add_field(name="СЕГОДНЯ", value=f"**{today_messages:,}**".replace(",", " "), inline=True)
    embed.add_field(name="ВКЛАД В СЕГОДНЯШНИЙ РАЗГОВОР",
                    value=f"`{'▰' * filled}{'▱' * (12 - filled)}`  **{share * 100:.1f}%**", inline=False)
    if requester_id is not None and chat_impact.can_view_chat(guild_id, requester_id):
        impact = chat_impact_state.member_totals(guild_id, member.id)
        embed.add_field(name="ИСКРА РАЗГОВОРА", value=f"**{impact['sparks']}**\nВозобновлений после тишины", inline=True)
        embed.add_field(name="ОТВЕТЫ НОВИЧКАМ", value=f"**{impact['newcomer_replies']}**\nПрямых ответов в первую неделю", inline=True)
    if texts:
        average = round(characters / texts)
        embed.add_field(name="СРЕДНЯЯ ДЛИНА ТЕКСТА", value=f"{average} символов", inline=True)
        embed.add_field(name="ДЛИННЕЕ 100 СИМВОЛОВ", value=f"{long_texts}  ·  {long_share * 100:.1f}%", inline=True)
    if texts < 20:
        rhythm = "Формат ещё складывается."
    elif long_share >= 0.5:
        rhythm = "Развёрнутый формат: чаще тексты длиннее 100 символов."
    elif long_share <= 0.15:
        rhythm = "Короткий формат: чаще компактные реплики."
    else:
        rhythm = "Смешанный формат: короткие реплики и развёрнутые сообщения."
    embed.add_field(name="ПОЧЕРК В ЧАТЕ", value=rhythm, inline=False)
    embed.set_footer(text="Вклад: все каналы сервера · Почерк: длина текста · Достижения на соседнем экране")
    return embed


class VeyrRankView(profile_cards.ProfileView):
    def __init__(self, profile, tab="card", page=0, *, expired=False):
        discord.ui.LayoutView.__init__(self, timeout=900)
        self.profile, self.tab, self.page = profile, tab, page
        self.guild_id, self.member, self.owner_id = profile.guild_id, profile.member, profile.owner_id
        if tab == "card":
            box = discord.ui.Container(accent_colour=profile.accent)
            if profile.image_url:
                box.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(
                    profile.image_url, description=f"{self.member.display_name}: LVL {profile.data['level']}")))
            else:
                box.add_item(discord.ui.Section(
                    discord.ui.TextDisplay(f"### Vivian // **{profile_cards.safe(self.member.display_name)}**"),
                    accessory=discord.ui.Thumbnail(self.member.display_avatar.url)))
                box.add_item(discord.ui.TextDisplay(self.details()))
        else:
            embed = (_member_trace_embed(self.guild_id, self.member, self.owner_id) if tab == "trace"
                     else utils_helpers.vivian_embed(_rank_page_text(self.guild_id, self.member, page), title="Достижения"))
            embed.colour = profile.accent
            box, _ = v2_content.card({"embed": embed})
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.ActionRow(
            profile_cards.ProfileButton("Карточка", self.card_tab, selected=tab == "card"),
            profile_cards.ProfileButton("След участника", self.trace_tab, selected=tab == "trace"),
            profile_cards.ProfileButton("Достижения", self.achievements_tab, selected=tab == "badges"),
            profile_cards.ProfileButton("Обновить", self.refresh)))
        pages = [
            profile_cards.ProfileButton("←", self.previous, disabled=tab != "badges" or page == 0),
            profile_cards.ProfileButton(f"{page + 1}/{len(RANK_SECTIONS)}" if tab == "badges" else "—", self.noop, disabled=True),
            profile_cards.ProfileButton("→", self.next, disabled=tab != "badges" or page >= len(RANK_SECTIONS) - 1),
            profile_cards.ProfileButton("Скачать PNG", self.download, disabled=profile.png is None),
        ]
        if self.member.id == self.owner_id:
            pages.append(profile_cards.ProfileButton("Моя память", self.memory_button))
        box.add_item(discord.ui.ActionRow(*pages))
        if expired:
            box.add_item(discord.ui.TextDisplay("-# Panel closed · /rank"))
        self.add_item(box)
        if expired:
            for item in self.walk_children():
                if isinstance(item, discord.ui.Button):
                    item.disabled = True

    async def trace_tab(self, interaction):
        await self.change(interaction, "trace")

    async def achievements_tab(self, interaction):
        await self.change(interaction, "badges")

    async def memory_button(self, interaction):
        if self.member.id != self.owner_id:
            return
        await vivian_memory_card.open_private_memory(
            interaction, owner_id=self.owner_id, guild_id=self.guild_id, channel_id=interaction.channel_id)


@core_config.bot.tree.command(name="rank", description="Открыть след участника, вклад в разговор и страницы достижений.")
@app_commands.describe(member="Чьи достижения показать; если не указать — твои")
@app_commands.checks.cooldown(1, 8, key=lambda interaction: interaction.user.id)
async def rank(interaction: discord.Interaction, member: discord.Member | None = None):
    if interaction.guild is None:
        return await utils_helpers.vivian_reply(
            interaction,
            "Профиль активности доступен только на сервере.",
            error=True,
            ephemeral=True,
        )
    target = member or interaction.user
    private = interaction.channel.id != core_config.CHAT_CHANNEL_ID
    if not await utils_command_responses.defer_command(interaction, ephemeral=private):
        return
    data, png = await profile_data.build(target, with_image=profile_cards._can_upload(interaction))
    profile = profile_cards.Profile(interaction.guild.id, interaction.user.id, target, data, png,
                                    last_refresh=time.monotonic(), command_name="rank", view_type=VeyrRankView)
    await profile_cards._publish(interaction, profile)
