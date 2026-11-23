use super::Context;

#[poise::command(slash_command, prefix_command, aliases("about", "помощь"))]
pub async fn help(ctx: Context<'_>) -> anyhow::Result<()> {
    crate::ui::say(ctx, "**Vivian** — Discord-бот на Rust.\nAI: `/ai`, `/ask`, `/forget`.\nМузыка: `/play`, `/search`, `/queue`, `/skip`, `/pause`, `/resume`, `/stop`, `/volume`, `/join`, `/leave`.\nМодерация: `/warn`, `/warnings`, `/timeout`, `/untimeout`, `/kick`, `/ban`.\nЭкономика: `/balance`, `/daily`, `/work`, `/leaderboard`.\nPrefix-команды доступны с `!`.").await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn ping(ctx: Context<'_>) -> anyhow::Result<()> {
    crate::ui::say(ctx, "Pong! Vivian на связи.").await?;
    Ok(())
}
