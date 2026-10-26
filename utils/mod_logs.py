from __future__ import annotations
import discord
from core import config as core_config
from utils import helpers as utils_helpers
from utils import components_v2 as v2


async def send_mod_log(guild: discord.Guild, message: str):
    channel = guild.get_channel(core_config.MOD_LOG_CHANNEL_ID)
    if channel:
        try:
            await v2.send_message(channel,
                embed=utils_helpers.vivian_embed(message, title="◆ Vivian // MODERATION TRACE"),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException as error:
            print(f"[Мод-лог] Не удалось отправить запись на сервере {guild.id}: {error!r}")


async def send_member_event(guild: discord.Guild, message: str):
    channel = guild.get_channel(core_config.MEMBER_EVENT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await core_config.bot.fetch_channel(core_config.MEMBER_EVENT_CHANNEL_ID)
        except discord.DiscordException as error:
            print(
                f"[Участники] Не удалось найти канал {core_config.MEMBER_EVENT_CHANNEL_ID} "
                f"на сервере {guild.id}: {error!r}",
                flush=True,
            )
            return
    channel_guild = getattr(channel, "guild", None)
    if channel_guild is not None and channel_guild.id != guild.id:
        print(
            f"[Участники] Канал {core_config.MEMBER_EVENT_CHANNEL_ID} относится к другому серверу; "
            f"событие сервера {guild.id} не отправлено.",
            flush=True,
        )
        return
    try:
        await v2.send_message(channel, message, allowed_mentions=discord.AllowedMentions.none())
    except discord.DiscordException as error:
        print(
            f"[Участники] Не удалось отправить событие в канал {core_config.MEMBER_EVENT_CHANNEL_ID} "
            f"на сервере {guild.id}: {error!r}",
            flush=True,
        )


async def send_spam_incident_log(
    guild: discord.Guild,
    member: discord.Member,
    reason: str,
    timeout_minutes: int | None,
    messages: list[discord.Message],
) -> None:
    """Record spam evidence in this guild's configured private moderation channel."""
    channel = guild.get_channel(core_config.MOD_LOG_CHANNEL_ID)
    if channel is None:
        print(f"[Анти-спам] Мод-лог {core_config.MOD_LOG_CHANNEL_ID} недоступен на сервере {guild.id}")
        return

    embed = discord.Embed(
        title="◆ Vivian // MODERATION TRACE",
        description="Зафиксировано нарушение, требующее модерации.",
        color=core_config.VIVIAN_ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    utils_helpers.vivian_brand(embed, "◆ Vivian // MODERATION")
    embed.add_field(name="Участник", value=f"{member.mention} (`{member.id}`)", inline=False)
    embed.add_field(name="Канал", value=messages[0].channel.mention, inline=True)
    embed.add_field(name="Сигнал", value=reason, inline=True)
    embed.add_field(
        name="Автоматическое действие",
        value=(
            f"Удаление {len(messages)} сообщений; таймаут {timeout_minutes} минут."
            if timeout_minutes is not None
            else f"Удаление {len(messages)} сообщений; первое предупреждение."
        ),
        inline=False,
    )
    evidence = "\n".join(
        f"[{index}. Сообщение]({item.jump_url})"
        for index, item in enumerate(messages, start=1)
    )
    embed.add_field(name="Материалы", value=evidence[:1024], inline=False)
    embed.set_footer(
        text="Внутренняя запись. Жалобу в Discord при необходимости отправляет модератор вручную."
    )

    try:
        await v2.send_message(channel, embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.DiscordException as error:
        print(f"[Анти-спам] Не удалось записать инцидент на сервере {guild.id}: {error!r}")
