from __future__ import annotations
from discord import app_commands
from datetime import datetime, timezone
import discord
from core import config as core_config
from commands import activity as commands_activity
from core import state as core_state
from core import chat_impact_state
from vivian import chat_impact
from vivian import memory_card as vivian_memory_card
from utils import helpers as utils_helpers
from utils import command_responses as utils_command_responses
from utils import components_v2 as v2
from visuals import panels as visuals_panels


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


class VeyrRankView(v2.ControllerView):
    def __init__(self, guild_id: int, member: discord.Member, owner_id: int):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.member = member
        self.owner_id = owner_id
        self.page = 0
        self.mode = "TRACE"
        self.visual_enabled = False
        self.panel_message = None
        self.sync_controls()
        if self.member.id != self.owner_id:
            self.remove_item(self.memory_button)

    def sync_controls(self):
        trace = self.mode == "TRACE"
        self.trace_tab.style = discord.ButtonStyle.primary if trace else discord.ButtonStyle.secondary
        self.achievements_tab.style = discord.ButtonStyle.secondary if trace else discord.ButtonStyle.primary
        self.previous_page.disabled = trace or self.page == 0
        self.next_page.disabled = trace or self.page == len(RANK_SECTIONS) - 1
        self.page_indicator.label = "СЛЕД" if trace else f"{self.page + 1} / {len(RANK_SECTIONS)}"
        self.page_indicator.disabled = True

    def make_embed(self):
        embed = (_member_trace_embed(self.guild_id, self.member, self.owner_id) if self.mode == "TRACE"
                 else utils_helpers.vivian_embed(_rank_page_text(self.guild_id, self.member, self.page), title="Достижения"))
        if self.visual_enabled:
            visuals_panels._apply_panel_visual(embed, "profile")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.guild.id != self.guild_id or interaction.user.id != self.owner_id:
            try:
                await utils_helpers.vivian_reply(interaction, "Открой свой экран командой `/rank`.", ephemeral=True)
            except (discord.NotFound, discord.HTTPException):
                pass
            return False
        return True

    async def update_panel(self, interaction: discord.Interaction):
        self.sync_controls()
        try:
            await v2.edit_response(interaction, embed=self.make_embed(), view=self,
                                                    allowed_mentions=discord.AllowedMentions.none())
        except (discord.NotFound, discord.HTTPException):
            pass

    async def _change_page(self, interaction: discord.Interaction, delta: int):
        if interaction.user.id != self.owner_id:
            return await utils_helpers.vivian_reply(
                interaction,
                "Этот экран связан с участником, который его открыл.",
                error=True,
                ephemeral=True,
            )
        self.page = max(0, min(len(RANK_SECTIONS) - 1, self.page + delta))
        await self.update_panel(interaction)

    @discord.ui.button(label="След участника", style=discord.ButtonStyle.primary, row=0)
    async def trace_tab(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode = "TRACE"
        await self.update_panel(interaction)

    @discord.ui.button(label="Достижения", style=discord.ButtonStyle.secondary, row=0)
    async def achievements_tab(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode = "ACHIEVEMENTS"
        await self.update_panel(interaction)

    @discord.ui.button(label="Моя память", style=discord.ButtonStyle.secondary, row=0)
    async def memory_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.member.id != self.owner_id:
            return
        await vivian_memory_card.open_private_memory(
            interaction, owner_id=self.owner_id, guild_id=self.guild_id, channel_id=interaction.channel_id)

    @discord.ui.button(label="Карточка", style=discord.ButtonStyle.secondary, row=0)
    async def profile_card(self, interaction: discord.Interaction, button: discord.ui.Button):
        from commands.card import open_card
        await open_card(interaction, self.member)

    @discord.ui.button(label="←", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._change_page(interaction, -1)

    @discord.ui.button(label="СЛЕД", style=discord.ButtonStyle.secondary, row=1)
    async def page_indicator(self, interaction: discord.Interaction, button: discord.ui.Button):
        return

    @discord.ui.button(label="→", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._change_page(interaction, 1)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.panel_message is not None:
            try:
                await v2.edit_message(self.panel_message, view=self)
            except discord.HTTPException:
                pass


@core_config.bot.tree.command(name="rank", description="Открыть след участника, вклад в разговор и страницы достижений.")
@app_commands.describe(member="Чьи достижения показать; если не указать — твои")
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
    view = VeyrRankView(interaction.guild.id, target, interaction.user.id)
    message = await utils_command_responses.send_panel(interaction, view=view, style="profile")
    if message is not None:
        view.panel_message = message if private else interaction.channel.get_partial_message(message.id)
