use super::Context;
use anyhow::{anyhow, Result};
use chrono::{Duration, Utc};
use poise::serenity_prelude as serenity;

async fn require_permission(ctx: Context<'_>, required: serenity::Permissions) -> Result<bool> {
    if ctx.guild_id().is_none() {
        crate::ui::say(ctx, "Эта команда доступна только на сервере.").await?;
        return Ok(false);
    }
    let Some(member) = ctx.author_member().await else {
        crate::ui::say(ctx, "Не удалось проверить права участника.").await?;
        return Ok(false);
    };
    let Some(channel) = ctx.guild_channel().await else {
        crate::ui::say(ctx, "Не удалось проверить права канала.").await?;
        return Ok(false);
    };
    let Some(permissions) = ctx
        .guild()
        .map(|guild_cache| guild_cache.user_permissions_in(&channel, &member))
    else {
        crate::ui::say(ctx, "Не удалось проверить права сервера.").await?;
        return Ok(false);
    };
    if !permissions.contains(required) {
        crate::ui::say(
            ctx,
            format!(
                "This command requires **{}** from you (это право нужно участнику, запускающему команду).",
                crate::permissions::describe(required)
            ),
        )
        .await?;
        return Ok(false);
    }
    Ok(true)
}

fn guild(ctx: Context<'_>) -> Result<serenity::GuildId> {
    ctx.guild_id()
        .ok_or_else(|| anyhow!("Эта команда доступна только на сервере."))
}

#[poise::command(slash_command, prefix_command)]
pub async fn warn(
    ctx: Context<'_>,
    #[description = "Участник"] user: serenity::User,
    #[description = "Причина предупреждения"] reason: String,
) -> Result<()> {
    if !require_permission(ctx, serenity::Permissions::MODERATE_MEMBERS).await? {
        return Ok(());
    }
    let guild = guild(ctx)?;
    let reason = reason.trim();
    if reason.is_empty() {
        anyhow::bail!("Укажи причину предупреждения.");
    }
    ctx.data()
        .store
        .add_warning(guild.get(), user.id.get(), ctx.author().id.get(), reason)
        .await?;
    ctx.data()
        .store
        .log_moderation_action(
            guild.get(),
            ctx.author().id.get(),
            "warn",
            Some(user.id.get()),
            None,
            reason,
        )
        .await?;
    let count = ctx
        .data()
        .store
        .warning_count(guild.get(), user.id.get())
        .await?;
    let escalation = if count >= 7 {
        match guild
            .ban_with_reason(
                ctx.serenity_context(),
                user.id,
                0,
                "Vivian warning ladder: seven warnings",
            )
            .await
        {
            Ok(()) => {
                ctx.data()
                    .store
                    .log_moderation_action(
                        guild.get(),
                        ctx.author().id.get(),
                        "warning_ladder_ban",
                        Some(user.id.get()),
                        None,
                        "7 warnings",
                    )
                    .await?;
                " · сработал порог: бан за 7 предупреждений".to_owned()
            }
            Err(error) => {
                tracing::warn!(%error,user_id=user.id.get(),"warning ladder ban failed");
                " · порог бана достигнут, но Discord отклонил действие".to_owned()
            }
        }
    } else if count >= 5 || count >= 3 {
        let seconds = if count >= 5 { 86_400 } else { 3_600 };
        let until =
            serenity::Timestamp::from_unix_timestamp(chrono::Utc::now().timestamp() + seconds)?;
        match guild
            .edit_member(
                ctx.serenity_context(),
                user.id,
                serenity::EditMember::new()
                    .disable_communication_until_datetime(until)
                    .audit_log_reason("Vivian warning ladder"),
            )
            .await
        {
            Ok(_) => {
                ctx.data()
                    .store
                    .add_timeout(guild.get(), user.id.get(), ctx.author().id.get())
                    .await?;
                let duration = if seconds == 86_400 {
                    "24 часа"
                } else {
                    "1 час"
                };
                ctx.data()
                    .store
                    .log_moderation_action(
                        guild.get(),
                        ctx.author().id.get(),
                        "warning_ladder_timeout",
                        Some(user.id.get()),
                        None,
                        &format!("{count} warnings; {duration}"),
                    )
                    .await?;
                format!(" · авто-тайм-аут {duration} за {count} предупреждений")
            }
            Err(error) => {
                tracing::warn!(%error,user_id=user.id.get(),"warning ladder timeout failed");
                " · порог тайм-аута достигнут, но Discord отклонил действие".to_owned()
            }
        }
    } else {
        String::new()
    };
    crate::ui::say(
        ctx,
        format!(
            "<@{}> получил предупреждение #{count}. Причина: {}{escalation}",
            user.id.get(),
            reason
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn unwarn(
    ctx: Context<'_>,
    #[description = "Участник"] user: serenity::User,
    #[description = "ID предупреждения"] warning_id: i64,
) -> Result<()> {
    if !require_permission(ctx, serenity::Permissions::MODERATE_MEMBERS).await? {
        return Ok(());
    }
    let guild = guild(ctx)?;
    anyhow::ensure!(
        ctx.data()
            .store
            .remove_warning(guild.get(), user.id.get(), warning_id)
            .await?,
        "Предупреждение с таким ID не найдено."
    );
    ctx.data()
        .store
        .log_moderation_action(
            guild.get(),
            ctx.author().id.get(),
            "unwarn",
            Some(user.id.get()),
            None,
            &format!("warning #{warning_id}"),
        )
        .await?;
    crate::ui::say(
        ctx,
        format!(
            "Предупреждение `#{warning_id}` для <@{}> снято.",
            user.id.get()
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn modlog(
    ctx: Context<'_>,
    #[description = "Количество записей (до 50)"] limit: Option<i64>,
) -> Result<()> {
    if !require_permission(ctx, serenity::Permissions::VIEW_AUDIT_LOG).await? {
        return Ok(());
    }
    let rows = ctx
        .data()
        .store
        .moderation_log(guild(ctx)?.get(), limit.unwrap_or(20))
        .await?;
    if rows.is_empty() {
        crate::ui::say(ctx, "Журнал действий пока пуст.").await?;
        return Ok(());
    }
    let lines = rows
        .into_iter()
        .map(|(id, action, target, reason, at)| {
            format!(
                "`#{id}` · <t:{at}:R> · **{action}** · {} · {}",
                target
                    .map(|v| format!("<@{v}>"))
                    .unwrap_or_else(|| "без участника".into()),
                if reason.is_empty() {
                    "без причины".into()
                } else {
                    reason
                }
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    crate::ui::say(ctx, format!("**Последние действия модерации**\n{lines}")).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, aliases("warns"))]
pub async fn warnings(
    ctx: Context<'_>,
    #[description = "Участник"] user: serenity::User,
) -> Result<()> {
    if !require_permission(ctx, serenity::Permissions::MODERATE_MEMBERS).await? {
        return Ok(());
    }
    let rows = ctx
        .data()
        .store
        .warnings(guild(ctx)?.get(), user.id.get())
        .await?;
    if rows.is_empty() {
        crate::ui::say(ctx, format!("У <@{}> нет предупреждений.", user.id.get())).await?;
        return Ok(());
    }
    let content = rows
        .into_iter()
        .map(|(id, reason, at)| format!("`#{id}` · <t:{at}:R> · {reason}"))
        .collect::<Vec<_>>()
        .join("\n");
    crate::ui::say(
        ctx,
        format!("**Предупреждения <@{}>**\n{content}", user.id.get()),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, aliases("mute"))]
pub async fn timeout(
    ctx: Context<'_>,
    #[description = "Участник"] user: serenity::User,
    #[description = "Минуты, от 1 до 40320"] minutes: u64,
    #[description = "Причина"] reason: String,
) -> Result<()> {
    if !require_permission(ctx, serenity::Permissions::MODERATE_MEMBERS).await? {
        return Ok(());
    }
    if !(1..=40_320).contains(&minutes) {
        anyhow::bail!("Срок должен быть от 1 минуты до 28 дней.");
    }
    let guild = guild(ctx)?;
    let reason = reason.trim();
    let until = serenity::Timestamp::from_unix_timestamp(
        (Utc::now() + Duration::minutes(minutes as i64)).timestamp(),
    )?;
    guild
        .edit_member(
            ctx.serenity_context(),
            user.id,
            serenity::EditMember::new()
                .disable_communication_until_datetime(until)
                .audit_log_reason(reason),
        )
        .await?;
    ctx.data()
        .store
        .add_timeout(guild.get(), user.id.get(), ctx.author().id.get())
        .await?;
    ctx.data()
        .store
        .log_moderation_action(
            guild.get(),
            ctx.author().id.get(),
            "timeout",
            Some(user.id.get()),
            None,
            reason,
        )
        .await?;
    crate::ui::say(
        ctx,
        format!(
            "<@{}> получил тайм-аут на **{minutes} мин.** Причина: {}",
            user.id.get(),
            reason
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, aliases("unmute"))]
pub async fn untimeout(
    ctx: Context<'_>,
    #[description = "Участник"] user: serenity::User,
) -> Result<()> {
    if !require_permission(ctx, serenity::Permissions::MODERATE_MEMBERS).await? {
        return Ok(());
    }
    let guild = guild(ctx)?;
    guild
        .edit_member(
            ctx.serenity_context(),
            user.id,
            serenity::EditMember::new().enable_communication(),
        )
        .await?;
    ctx.data()
        .store
        .clear_timeout(guild.get(), user.id.get())
        .await?;
    ctx.data()
        .store
        .log_moderation_action(
            guild.get(),
            ctx.author().id.get(),
            "untimeout",
            Some(user.id.get()),
            None,
            "",
        )
        .await?;
    crate::ui::say(
        ctx,
        format!("Тайм-аут Vivian для <@{}> снят.", user.id.get()),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn kick(
    ctx: Context<'_>,
    #[description = "Участник"] user: serenity::User,
    #[description = "Причина"] reason: String,
) -> Result<()> {
    if !require_permission(ctx, serenity::Permissions::KICK_MEMBERS).await? {
        return Ok(());
    }
    let guild = guild(ctx)?;
    guild
        .kick_with_reason(ctx.serenity_context(), user.id, reason.trim())
        .await?;
    ctx.data()
        .store
        .log_moderation_action(
            guild.get(),
            ctx.author().id.get(),
            "kick",
            Some(user.id.get()),
            None,
            reason.trim(),
        )
        .await?;
    crate::ui::say(ctx, format!("<@{}> удалён с сервера.", user.id.get())).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn ban(
    ctx: Context<'_>,
    #[description = "Участник"] user: serenity::User,
    #[description = "Причина"] reason: String,
) -> Result<()> {
    if !require_permission(ctx, serenity::Permissions::BAN_MEMBERS).await? {
        return Ok(());
    }
    let guild = guild(ctx)?;
    guild
        .ban_with_reason(ctx.serenity_context(), user.id, 0, reason.trim())
        .await?;
    ctx.data()
        .store
        .log_moderation_action(
            guild.get(),
            ctx.author().id.get(),
            "ban",
            Some(user.id.get()),
            None,
            reason.trim(),
        )
        .await?;
    crate::ui::say(
        ctx,
        format!("<@{}> заблокирован на сервере.", user.id.get()),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn unban(
    ctx: Context<'_>,
    #[description = "ID пользователя"] user_id: u64,
    #[description = "Причина"] reason: Option<String>,
) -> Result<()> {
    if !require_permission(ctx, serenity::Permissions::BAN_MEMBERS).await? {
        return Ok(());
    }
    let guild = guild(ctx)?;
    let reason = reason.unwrap_or_else(|| "Снятие блокировки модератором".into());
    guild
        .unban(ctx.serenity_context(), serenity::UserId::new(user_id))
        .await?;
    ctx.data()
        .store
        .log_moderation_action(
            guild.get(),
            ctx.author().id.get(),
            "unban",
            Some(user_id),
            None,
            &reason,
        )
        .await?;
    crate::ui::say(ctx, format!("Блокировка пользователя `{user_id}` снята.")).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn clear(
    ctx: Context<'_>,
    #[description = "Число последних сообщений от 1 до 100"] amount: u8,
) -> Result<()> {
    if !require_permission(ctx, serenity::Permissions::MANAGE_MESSAGES).await? {
        return Ok(());
    }
    anyhow::ensure!(
        (1..=100).contains(&amount),
        "Количество должно быть от 1 до 100."
    );
    let channel = ctx.channel_id();
    let messages = channel
        .messages(
            ctx.serenity_context(),
            serenity::GetMessages::new().limit(amount),
        )
        .await?;
    let count = messages.len();
    for message in messages {
        message.delete(ctx.serenity_context()).await?;
    }
    let guild = guild(ctx)?;
    ctx.data()
        .store
        .log_moderation_action(
            guild.get(),
            ctx.author().id.get(),
            "clear",
            None,
            Some(channel.get()),
            &format!("deleted {count} messages"),
        )
        .await?;
    crate::ui::say(ctx, format!("Удалено сообщений: **{count}**.")).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn slowmode(
    ctx: Context<'_>,
    #[description = "Интервал в секундах, 0 отключает (до 21600)"] seconds: u16,
) -> Result<()> {
    if !require_permission(ctx, serenity::Permissions::MANAGE_CHANNELS).await? {
        return Ok(());
    }
    anyhow::ensure!(seconds <= 21600, "Максимум 21600 секунд.");
    let channel = ctx.channel_id();
    channel
        .edit(
            ctx.serenity_context(),
            serenity::EditChannel::new().rate_limit_per_user(seconds),
        )
        .await?;
    let guild = guild(ctx)?;
    ctx.data()
        .store
        .log_moderation_action(
            guild.get(),
            ctx.author().id.get(),
            "slowmode",
            None,
            Some(channel.get()),
            &format!("{seconds}s"),
        )
        .await?;
    crate::ui::say(
        ctx,
        if seconds == 0 {
            "Медленный режим отключён.".to_owned()
        } else {
            format!("Медленный режим: {seconds} сек.")
        },
    )
    .await?;
    Ok(())
}

async fn set_channel_lock(ctx: Context<'_>, lock: bool) -> Result<()> {
    if !require_permission(ctx, serenity::Permissions::MANAGE_CHANNELS).await? {
        return Ok(());
    }
    let guild = guild(ctx)?;
    let channel = ctx.channel_id();
    let role = guild.everyone_role();
    if lock {
        channel
            .create_permission(
                ctx.serenity_context(),
                serenity::PermissionOverwrite {
                    allow: serenity::Permissions::empty(),
                    deny: serenity::Permissions::SEND_MESSAGES,
                    kind: serenity::PermissionOverwriteType::Role(role),
                },
            )
            .await?;
    } else {
        channel
            .delete_permission(
                ctx.serenity_context(),
                serenity::PermissionOverwriteType::Role(role),
            )
            .await?;
    }
    let action = if lock { "lock" } else { "unlock" };
    ctx.data()
        .store
        .log_moderation_action(
            guild.get(),
            ctx.author().id.get(),
            action,
            None,
            Some(channel.get()),
            "",
        )
        .await?;
    crate::ui::say(
        ctx,
        if lock {
            "Канал закрыт для @everyone."
        } else {
            "Переопределение для @everyone очищено; доступ задан ролями канала."
        },
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn lock(ctx: Context<'_>) -> Result<()> {
    set_channel_lock(ctx, true).await
}

#[poise::command(slash_command, prefix_command)]
pub async fn unlock(ctx: Context<'_>) -> Result<()> {
    set_channel_lock(ctx, false).await
}
