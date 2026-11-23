use super::{guild_key, Context};
use anyhow::Result;
use chrono::Datelike;

#[poise::command(
    slash_command,
    prefix_command,
    guild_only,
    subcommands("set", "remove", "list")
)]
pub async fn birthday(_ctx: Context<'_>) -> Result<()> {
    anyhow::bail!("Выбери действие: `/birthday set`, `/birthday remove` или `/birthday list`.")
}

async fn enabled(ctx: Context<'_>) -> Result<serde_json::Value> {
    let settings = ctx
        .data()
        .store
        .guild_settings(guild_key(ctx), &crate::web::settings_defaults())
        .await?;
    anyhow::ensure!(
        settings["birthdays_enabled"] == true,
        "Эта функция выключена. Включи birthdays_enabled и задай birthday_channel_id в настройках сервера."
    );
    Ok(settings)
}

#[poise::command(slash_command, prefix_command, guild_only)]
pub async fn set(
    ctx: Context<'_>,
    #[description = "День (1–31)"] day: u32,
    #[description = "Месяц (1–12)"] month: u32,
    #[description = "Год рождения (необязательно, остаётся закрытым)"] year: Option<i32>,
) -> Result<()> {
    enabled(ctx).await?;
    ctx.data()
        .store
        .set_birthday(guild_key(ctx), ctx.author().id.get(), day, month, year)
        .await?;
    crate::ui::private(
        ctx,
        format!(
            "Дата сохранена: {day:02}/{month:02}. Сервер может поздравить тебя публично; год рождения и возраст не публикуются."
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, guild_only)]
pub async fn remove(ctx: Context<'_>) -> Result<()> {
    ctx.data()
        .store
        .remove_birthday(guild_key(ctx), ctx.author().id.get())
        .await?;
    crate::ui::private(
        ctx,
        "Дата рождения удалена. Уже выданная временная роль исчезнет по исходному таймеру.",
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, guild_only)]
pub async fn list(
    ctx: Context<'_>,
    #[description = "Страница списка (по 15 участников)"] page: Option<u16>,
) -> Result<()> {
    let settings = enabled(ctx).await?;
    let timezone = settings["timezone"]
        .as_str()
        .unwrap_or("Europe/Riga")
        .parse::<chrono_tz::Tz>()
        .unwrap_or(chrono_tz::Europe::Riga);
    let today = chrono::Utc::now().with_timezone(&timezone).date_naive();
    let page = page.unwrap_or(1).clamp(1, 500) as usize;
    let rows = ctx
        .data()
        .store
        .upcoming_birthdays(
            guild_key(ctx),
            today.year(),
            today.month(),
            today.day(),
            10_000,
        )
        .await?;
    let start = (page - 1) * 15;
    let total_pages = rows.len().div_ceil(15).max(1);
    let content = if start >= rows.len() {
        "Нет участников на этой странице.".to_owned()
    } else {
        rows[start..(start + 15).min(rows.len())]
            .iter()
            .map(|(user, day, month)| format!("<@{user}> · {day:02}/{month:02}"))
            .collect::<Vec<_>>()
            .join("\n")
    };
    let content = format!("**Ближайшие дни рождения**\n{content}\nСтраница {page}/{total_pages}");
    crate::ui::private(ctx, content).await?;
    Ok(())
}
