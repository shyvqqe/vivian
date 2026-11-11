from __future__ import annotations
from datetime import datetime
import discord
import time
from core import config as core_config
from core import state as core_state
from core import chat_impact_state
from vivian import chat_impact
from utils import helpers as utils_helpers
from utils import command_responses as utils_command_responses
from utils import components_v2 as v2
from visuals import panels as visuals_panels


def _activity_day_snapshot(guild_id: int, day=None) -> dict:
    day = day or datetime.now(core_config.SUMMARY_TIMEZONE).date()
    records = [(uid, dict(stats)) for (record_day, gid, uid), stats in core_state.DAILY_ACTIVITY.items()
               if gid == guild_id and record_day == day and stats.get("messages", 0) > 0]
    records.sort(key=lambda item: (-item[1]["messages"], item[0]))
    return {"day": day, "records": records,
            "total": sum(stats["messages"] for _, stats in records),
            "people": len(records)}


def _record_chat_pulse(message, now: float) -> None:
    if message.guild is None or message.channel.id != core_config.CHAT_CHANNEL_ID or message.author.bot:
        return
    records = core_state.CHAT_PULSE_RECENT[message.guild.id]
    records.append((now, message.author.id))
    while records and records[0][0] < now - 30 * 60:
        records.popleft()


def _chat_pulse_snapshot(guild_id: int, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    records = core_state.CHAT_PULSE_RECENT.get(guild_id, ())
    recent = sorted((item for item in records if item[0] >= now - 30 * 60), key=lambda item: item[0])
    switches = sum(first[1] != second[1] for first, second in zip(recent, recent[1:]))
    bins = [0] * 6
    for timestamp, _ in recent:
        index = max(0, min(5, int((timestamp - (now - 30 * 60)) // 300)))
        bins[index] += 1
    scale = max(bins, default=0)
    glyphs = "▁▂▃▄▅▆▇█"
    sparkline = "".join(glyphs[round(count / scale * 7)] if scale else "▁" for count in bins)
    return {"messages": len(recent), "people": len({uid for _, uid in recent}),
            "switches": switches, "bins": bins, "sparkline": sparkline,
            "since": max(core_state.CHAT_PULSE_OBSERVING_SINCE, now - 30 * 60),
            "limited": len(records) == 600 and bool(recent)}


def _activity_panel(guild_id: int, section: str = "TODAY", page: int = 0, requester_id: int | None = None) -> discord.Embed:
    snapshot = _activity_day_snapshot(guild_id)
    day, records, total = snapshot["day"], snapshot["records"], snapshot["total"]
    embed = utils_helpers.vivian_embed(title="Пульс сообщества")
    utils_helpers.vivian_brand(embed, "Vivian / CHAT PULSE")
    can_view = requester_id is not None and chat_impact.can_view_chat(guild_id, requester_id)
    if section == "PULSE":
        if not can_view:
            embed.description = "Этот срез доступен участникам, которые видят chat и его историю."
            return embed
        pulse = _chat_pulse_snapshot(guild_id)
        embed.description = f"<#{core_config.CHAT_CHANNEL_ID}>  ·  последние 30 минут"
        embed.add_field(name="РИТМ", value=f"`{pulse['sparkline']}`\nКаждый участок: 5 минут", inline=False)
        embed.add_field(name="СООБЩЕНИЯ", value=str(pulse["messages"]), inline=True)
        embed.add_field(name="ГОЛОСА", value=str(pulse["people"]), inline=True)
        embed.add_field(name="СМЕНА СОБЕСЕДНИКА", value=str(pulse["switches"]), inline=False)
        if pulse["messages"] == 0:
            embed.add_field(name="НАБЛЮДЕНИЕ", value="Чат пока держит тишину.", inline=False)
        embed.set_footer(text=("Последние 600 сообщений · " if pulse["limited"] else "")
                         + "Наблюдение с " + datetime.fromtimestamp(pulse["since"], core_config.SUMMARY_TIMEZONE).strftime("%H:%M МСК"))
    elif section == "PEOPLE":
        pages = max(1, (len(records) + 4) // 5)
        page = max(0, min(pages - 1, page))
        embed.description = f"Участники дня  ·  {day:%d.%m} МСК"
        for index, (uid, stats) in enumerate(records[page * 5:page * 5 + 5], page * 5 + 1):
            share = stats["messages"] / total * 100 if total else 0
            embed.add_field(name=f"{index:02}  /  ВКЛАД В РАЗГОВОР",
                            value=f"<@{uid}>  ·  **{stats['messages']}** сообщений  ·  {share:.1f}%", inline=False)
        if not records:
            embed.description += "\nСегодняшний след ещё не сформировался."
        embed.set_footer(text=f"{page + 1} / {pages}  ·  Доля от всех сообщений сервера за день")
    else:
        embed.description = f"Сегодня  ·  {day:%d.%m.%Y} МСК"
        embed.add_field(name="СООБЩЕНИЯ", value=f"**{total:,}**".replace(",", " "), inline=True)
        embed.add_field(name="УЧАСТНИКИ", value=f"**{snapshot['people']}**", inline=True)
        if records:
            uid, stats = records[0]
            embed.add_field(name="ГЛАВНЫЙ ГОЛОС ДНЯ", value=f"<@{uid}>  ·  {stats['messages']} сообщений", inline=False)
            core = sum(stats["messages"] for _, stats in records[:3]) / total * 100
            embed.add_field(name="КАК РАСПРЕДЕЛЁН РАЗГОВОР",
                            value=f"Первые {min(3, len(records))} участника: **{core:.1f}%** сообщений.\n"
                                  f"Остальные: **{max(0, 100 - core):.1f}%**.", inline=False)
        else:
            embed.add_field(name="СЛЕД ДНЯ", value="Сегодняшний след ещё не сформировался.", inline=False)
        embed.set_footer(text="Сегодня: все каналы сервера · Пульс: только chat")
    if section != "PEOPLE" and can_view:
        impact = chat_impact_state.today_totals(guild_id, day.isoformat())
        embed.add_field(name="ДИАЛОГ СЕГОДНЯ / CHAT",
                        value=f"Искр разговора: **{impact['sparks']}**\nОтветов новичкам: **{impact['newcomer_replies']}**", inline=False)
        if impact["leaders"]:
            embed.add_field(name="КТО ВОЗОБНОВЛЯЛ РАЗГОВОР",
                            value="\n".join(f"<@{uid}>  ·  {count}" for uid, count in impact["leaders"]), inline=False)
    return embed


class ActivityLayerSelect(discord.ui.Select):
    def __init__(self, section: str, can_view_chat: bool = True):
        super().__init__(placeholder="Срез активности", row=0, options=[
            discord.SelectOption(label=label, value=value, default=section == value)
            for value, label in (("TODAY", "Сегодня на сервере"), ("PULSE", "Пульс chat"), ("PEOPLE", "Участники дня"))
            if value != "PULSE" or can_view_chat
        ])

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        view.section = self.values[0]
        view.page = 0
        await view.update_panel(interaction)


class ActivityView(v2.ControllerView):
    def __init__(self, guild_id: int, owner_id: int):
        super().__init__(timeout=900)
        self.guild_id, self.owner_id = guild_id, owner_id
        self.section, self.page = "TODAY", 0
        self.visual_enabled = False
        self.panel_message = None
        self.sync_controls()

    def sync_controls(self):
        for item in list(self.children):
            if isinstance(item, ActivityLayerSelect):
                self.remove_item(item)
        self.add_item(ActivityLayerSelect(self.section, chat_impact.can_view_chat(self.guild_id, self.owner_id)))
        pages = max(1, (_activity_day_snapshot(self.guild_id)["people"] + 4) // 5)
        self.page = max(0, min(pages - 1, self.page))
        self.previous_page.disabled = self.section != "PEOPLE" or self.page == 0
        self.next_page.disabled = self.section != "PEOPLE" or self.page >= pages - 1

    def make_embed(self):
        embed = _activity_panel(self.guild_id, self.section, self.page, self.owner_id)
        if self.visual_enabled:
            visuals_panels._apply_panel_visual(embed, "activity")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.guild.id != self.guild_id or interaction.user.id != self.owner_id:
            try:
                await utils_helpers.vivian_reply(interaction, "Открой свой срез командой `/activity`.", ephemeral=True)
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

    @discord.ui.button(label="←", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self.update_panel(interaction)

    @discord.ui.button(label="Обновить", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_panel(interaction)

    @discord.ui.button(label="→", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self.update_panel(interaction)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.panel_message is not None:
            try:
                await v2.edit_message(self.panel_message, view=self)
            except discord.HTTPException:
                pass


@core_config.bot.tree.command(name="activity", description="Открыть пульс чата, вклад участников и сегодняшний срез сервера.")
async def activity_summary(interaction: discord.Interaction):
    if interaction.guild is None:
        return await utils_helpers.vivian_reply(
            interaction,
            "Итоги активности доступны только на сервере.",
            error=True,
            ephemeral=True,
        )
    private = interaction.channel.id != core_config.CHAT_CHANNEL_ID
    if not await utils_command_responses.defer_command(interaction, ephemeral=private):
        return
    view = ActivityView(interaction.guild.id, interaction.user.id)
    message = await utils_command_responses.send_panel(interaction, view=view, style="activity")
    if message is not None:
        view.panel_message = message if private else interaction.channel.get_partial_message(message.id)
