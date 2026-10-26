"""Readable Vivian control-centre cards; progress comes from existing counters."""
from __future__ import annotations

import time

import discord

from commands import quest_panels as quests
from core import config, rituals, signal, state, veyr_state
from utils import helpers


VEYR_SECTIONS = (
    ("SYSTEM", "Центр сервера"), ("IDENTITY", "Сообщество"),
    ("MUSIC", "Музыка"), ("SIGNAL", "Пульс сигнала"),
    ("EVENTS", "События"), ("ARTIFACTS", "Достижения"),
    ("QUESTS", "Цели за общение"), ("CHRONICLE", "Хроника"),
    ("RITUALS", "Общие события"), ("PROFILE", "Мой профиль"),
)
SECTION_NAMES = dict(VEYR_SECTIONS)
EVENT_NAMES = {"DISCOVERY": "Открытие", "FIRST": "Впервые", "RARE": "Редкое событие",
               "LEGENDARY": "Легендарное", "MYTHIC": "Мифическое", "ANOMALY": "Аномалия"}


def _signal_readout(value: float) -> str:
    return f"{quests.bar(value, 100)}  {value:.2f} / 100"


def _veyr_panel(guild_id: int, section: str = "SYSTEM", notice: str = "", owner_id=None) -> discord.Embed:
    value = signal.update_server_signal(guild_id)
    current = veyr_state.SERVER_STATE[guild_id]
    current_name = quests.STATE_NAMES[current]
    guild = config.bot.get_guild(guild_id)
    embed = helpers.vivian_embed(title=f"Vivian // {SECTION_NAMES.get(section, 'Центр сервера')}")
    helpers.vivian_brand(embed, "Vivian / COMMUNITY")
    if section == "SYSTEM":
        embed.description = "Один экран для жизни сообщества. Выбери раздел или открой свою доску целей."
        if veyr_state.ANOMALY_PUBLIC_NOTICE[guild_id] > time.time():
            embed.description = "**◇ На сервере замечена аномалия.**\n\n" + embed.description
        embed.add_field(name="СИГНАЛ СЕРВЕРА", value=f"`{_signal_readout(value)}`", inline=False)
        embed.add_field(name="СОСТОЯНИЕ", value=current_name, inline=True)
        embed.add_field(name="СООБЩЕНИЙ СЕГОДНЯ", value=quests.number(quests.communication_overview(guild_id)), inline=True)
        embed.add_field(name="ТВОЙ МАРШРУТ", value="`/quests` — цели дня и совместные события\n"
                        "`/rank` — вклад в разговор и достижения", inline=False)
    elif section == "CHRONICLE":
        records = [item for item in veyr_state.CHRONICLE[guild_id] if item["visible_at"] <= time.time()]
        embed.description = "События, которые оставили след в жизни сервера."
        if records:
            for item in reversed(records[-5:]):
                kind = EVENT_NAMES.get(item["kind"], "Событие")
                embed.add_field(name=kind, value=f"{quests.safe(item['detail'], 240)}\n<t:{int(item['at'])}:R>", inline=False)
        else:
            embed.add_field(name="ПОКА ТИХО", value="Новые события появятся здесь, когда сервер оставит первый след.", inline=False)
    elif section == "RITUALS":
        embed.description = "Совместные действия для участников. Каждый выбирает вклад со своей панели."
        if notice:
            embed.description += "\n\n**" + quests.NOTICES.get(notice, quests.safe(notice, 250)) + "**"
        for ritual in rituals.RITUAL_RULES:
            snap = quests.ritual_snapshot(guild_id, ritual, owner_id)
            embed.add_field(name=quests.RITUAL_NAMES[ritual], value=snap["text"].split("\n", 1)[1], inline=False)
    elif section in ("SIGNAL", "EVENTS"):
        embed.description = f"**{current_name}** · `{_signal_readout(value)}`\nНаблюдения из жизни сервера."
        echoes = veyr_state.ECHO_MEMORY[guild_id]
        if echoes:
            records = echoes[-(3 if section == "SIGNAL" else 6):]
            for item in reversed(records):
                embed.add_field(name=quests.safe(item["kind"], 100),
                                value=f"{quests.safe(item['detail'], 220)}\n<t:{int(item['at'])}:R>", inline=False)
        else:
            embed.add_field(name="НАБЛЮДЕНИЯ", value="Сигнал пока не оставил записей.", inline=False)
    elif section == "QUESTS":
        embed.description = "Твои цели за общение теперь собраны в отдельной интерактивной доске."
        embed.add_field(name="СЕГОДНЯ", value="**5 / 15 / 30 сообщений** · срок до 00:00 МСК", inline=False)
        embed.add_field(name="ПУТЬ ОБЩЕНИЯ", value="Накопленные сообщения, длинные тексты и символы.", inline=False)
        embed.add_field(name="ОТКРЫТЬ", value="Нажми **Мои задания** ниже или используй `/quests`.", inline=False)
    elif section == "PROFILE":
        stats = state.RANK_ACTIVITY.get((guild_id, owner_id), {})
        embed.description = f"<@{owner_id}>\nТвой вклад в разговоры этого сервера." if owner_id else "Профиль участника."
        embed.add_field(name="СООБЩЕНИЯ", value=quests.number(stats.get("messages", 0)), inline=True)
        embed.add_field(name="СИМВОЛЫ", value=quests.number(stats.get("characters", 0)), inline=True)
        embed.add_field(name="ПОДРОБНЕЕ", value="`/rank` — статистика и страницы достижений.", inline=False)
    elif section == "IDENTITY":
        embed.description = "Люди, которые делают сервер живым."
        if guild is not None:
            embed.add_field(name="СЕРВЕР", value=quests.safe(guild.name), inline=False)
            if guild.member_count is not None:
                embed.add_field(name="УЧАСТНИКИ", value=quests.number(guild.member_count), inline=True)
            if guild.icon is not None:
                embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="АКТИВНОСТЬ", value="`/activity` — общая статистика и пульс разговора.", inline=False)
    elif section == "MUSIC":
        embed.description = "Музыка для общего голосового канала."
        embed.add_field(name="ЗАПУСТИТЬ ТРЕК", value="Зайди в голосовой и напиши `!play название`.", inline=False)
        embed.add_field(name="УПРАВЛЕНИЕ", value="`/queue` · `/pause` · `/resume` · `/skip` · `/stop`", inline=False)
        embed.add_field(name="СЛЕДУЮЩИЙ ШАГ", value="Новый `/play` с YouTube находится в разработке.", inline=False)
    elif section == "ARTIFACTS":
        embed.description = "Твои достижения растут вместе с участием в разговорах."
        embed.add_field(name="ПРОГРЕСС", value="`/rank` → **Достижения**\n`/quests` → **Путь общения**", inline=False)
    embed.set_footer(text=f"{SECTION_NAMES.get(section, section)} · {current_name} · Components V2")
    return embed
