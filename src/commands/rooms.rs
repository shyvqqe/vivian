use super::Context;
use anyhow::{anyhow, bail, Result};
use poise::serenity_prelude as serenity;

fn guild_id(ctx: Context<'_>) -> Result<serenity::GuildId> {
    ctx.guild_id()
        .ok_or_else(|| anyhow!("Команды приватных комнат доступны только на сервере."))
}

fn data(ctx: Context<'_>) -> &crate::rooms::RoomManager {
    &ctx.data().rooms
}

#[poise::command(slash_command)]
pub async fn room_create(
    ctx: Context<'_>,
    #[description = "Необязательное название комнаты"] name: Option<String>,
) -> Result<()> {
    let guild = guild_id(ctx)?;
    let channel = data(ctx)
        .create(ctx.serenity_context(), guild, ctx.author(), name.as_deref())
        .await?;
    crate::ui::say(
        ctx,
        format!(
            "Приватная комната готова: <#{}>. Управление: `/room_manage`.",
            channel.id
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn room_manage(ctx: Context<'_>) -> Result<()> {
    let guild = guild_id(ctx)?;
    let status = data(ctx)
        .summary(ctx.serenity_context(), guild, ctx.author().id)
        .await?;
    let Some(status) = status else {
        bail!("У тебя пока нет комнаты. Создай её через `/room_create`.");
    };
    crate::ui::say(ctx, format!("**Твоя приватная комната**\n{status}\n\n`/room_rename`, `/room_limit`, `/room_lock`, `/room_visibility`, `/room_invite`, `/room_kick`, `/room_transfer`, `/room_delete`.")).await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn room_claim(ctx: Context<'_>) -> Result<()> {
    let guild = guild_id(ctx)?;
    data(ctx)
        .claim(ctx.serenity_context(), guild, ctx.author().id)
        .await?;
    crate::ui::say(ctx, "Комната теперь под твоим управлением.").await?;
    Ok(())
}

#[poise::command(slash_command)]
#[allow(deprecated)]
pub async fn room_setup(
    ctx: Context<'_>,
    #[description = "Категория комнат; если пусто, Vivian создаст её автоматически"]
    category: Option<serenity::ChannelId>,
    #[description = "Включить приватные комнаты"] enabled: Option<bool>,
) -> Result<()> {
    let guild = guild_id(ctx)?;
    let enabled = enabled.unwrap_or(true);
    let bot_ctx = ctx.serenity_context();
    let member = guild.member(&bot_ctx.http, ctx.author().id).await?;
    if !member
        .permissions(&bot_ctx.cache)?
        .contains(serenity::Permissions::MANAGE_GUILD)
    {
        bail!("Для `/room_setup` вызывающему пользователю нужно право **Manage Server (Управлять сервером)**. Это право относится к человеку, который настраивает комнату.");
    }
    data(ctx).setup(bot_ctx, guild, category, enabled).await?;
    crate::ui::say(ctx, if enabled { "Приватные комнаты включены. Новые комнаты будут создаваться в выбранной категории или в категории Vivian." } else { "Создание приватных комнат отключено. Существующие комнаты продолжают работать." }).await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn room_rename(
    ctx: Context<'_>,
    #[description = "Название от 1 до 80 символов"] name: String,
) -> Result<()> {
    data(ctx)
        .rename(
            ctx.serenity_context(),
            guild_id(ctx)?,
            ctx.author().id,
            &name,
        )
        .await?;
    crate::ui::say(ctx, "Название комнаты изменено.").await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn room_limit(
    ctx: Context<'_>,
    #[description = "0–99 участников; 0 — без лимита"] limit: u32,
) -> Result<()> {
    data(ctx)
        .limit(
            ctx.serenity_context(),
            guild_id(ctx)?,
            ctx.author().id,
            limit,
        )
        .await?;
    crate::ui::say(
        ctx,
        format!(
            "Лимит комнаты: **{}**.",
            if limit == 0 {
                "без ограничений".into()
            } else {
                limit.to_string()
            }
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn room_lock(
    ctx: Context<'_>,
    #[description = "Запереть вход"] locked: bool,
) -> Result<()> {
    data(ctx)
        .lock_room(
            ctx.serenity_context(),
            guild_id(ctx)?,
            ctx.author().id,
            locked,
        )
        .await?;
    crate::ui::say(
        ctx,
        if locked {
            "Вход в комнату закрыт для новых участников."
        } else {
            "Вход в комнату открыт для участников с доступом."
        },
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn room_visibility(
    ctx: Context<'_>,
    #[description = "Скрыть комнату от остальных"] hidden: bool,
) -> Result<()> {
    data(ctx)
        .visibility(
            ctx.serenity_context(),
            guild_id(ctx)?,
            ctx.author().id,
            hidden,
        )
        .await?;
    crate::ui::say(
        ctx,
        if hidden {
            "Комната скрыта от остальных участников."
        } else {
            "Комната видна на сервере; подключение по-прежнему ограничено."
        },
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn room_invite(
    ctx: Context<'_>,
    #[description = "Участник сервера"] user: serenity::User,
) -> Result<()> {
    data(ctx)
        .invite(
            ctx.serenity_context(),
            guild_id(ctx)?,
            ctx.author().id,
            user.id,
        )
        .await?;
    crate::ui::say(ctx, format!("<@{}> приглашён в твою комнату.", user.id)).await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn room_kick(
    ctx: Context<'_>,
    #[description = "Участник комнаты"] user: serenity::User,
) -> Result<()> {
    data(ctx)
        .kick(
            ctx.serenity_context(),
            guild_id(ctx)?,
            ctx.author().id,
            user.id,
        )
        .await?;
    crate::ui::say(ctx, format!("<@{}> удалён из комнаты.", user.id)).await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn room_transfer(
    ctx: Context<'_>,
    #[description = "Новый владелец, который находится в комнате"] user: serenity::User,
) -> Result<()> {
    data(ctx)
        .transfer(
            ctx.serenity_context(),
            guild_id(ctx)?,
            ctx.author().id,
            user.id,
        )
        .await?;
    crate::ui::say(ctx, format!("Управление комнатой передано <@{}>.", user.id)).await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn room_delete(ctx: Context<'_>) -> Result<()> {
    data(ctx)
        .delete(ctx.serenity_context(), guild_id(ctx)?, ctx.author().id)
        .await?;
    crate::ui::say(
        ctx,
        "Комната удалена. Её можно создать снова через `/room_create`.",
    )
    .await?;
    Ok(())
}
