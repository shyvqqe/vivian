use super::{guild_key, Context};
use anyhow::{anyhow, Result};
use poise::serenity_prelude as serenity;

fn safe_mentions(value: &str) -> String {
    value.replace('@', "@\u{200b}")
}

#[poise::command(slash_command, prefix_command)]
pub async fn wall_view(
    ctx: Context<'_>,
    #[description = "Чью стену открыть"] user: Option<serenity::User>,
    #[description = "Номер страницы"] page: Option<u16>,
) -> Result<()> {
    let viewer = ctx.author().id.get();
    let owner = user.map(|u| u.id.get()).unwrap_or(viewer);
    let page = page.unwrap_or(1).clamp(1, 1000) as i64;
    let posts = ctx
        .data()
        .store
        .wall_posts(guild_key(ctx), viewer, owner, page - 1)
        .await?;
    if posts.is_empty() {
        crate::ui::say(ctx, "На этой странице стены пока нет записей.").await?;
        return Ok(());
    }
    let text = posts
        .into_iter()
        .map(|(id, author, body, at)| {
            format!(
                "`#{id}` · <@{author}> · <t:{at}:R>\n{}",
                safe_mentions(&body)
            )
        })
        .collect::<Vec<_>>()
        .join("\n\n");
    super::say_long(ctx,&format!("**Стена <@{owner}> · страница {page}**\n{text}\n\n`/wall_like post_id` · `/wall_comment post_id text`")).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn wall_post(
    ctx: Context<'_>,
    #[description = "Чья стена"] owner: serenity::User,
    #[description = "Текст записи, до 800 символов"] text: String,
) -> Result<()> {
    let id = ctx
        .data()
        .store
        .create_wall_post(guild_key(ctx), ctx.author().id.get(), owner.id.get(), &text)
        .await?;
    crate::ui::say(
        ctx,
        format!("Запись добавлена на стену <@{}> (`#{id}`).", owner.id.get()),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn wall_privacy(
    ctx: Context<'_>,
    #[description = "all, friends или private"] value: String,
) -> Result<()> {
    let privacy = value.to_lowercase();
    ctx.data()
        .store
        .set_wall_privacy(guild_key(ctx), ctx.author().id.get(), &privacy)
        .await?;
    crate::ui::say(ctx, format!("Видимость твоей стены: **{privacy}**.")).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn wall_like(
    ctx: Context<'_>,
    #[description = "ID записи стены"] post_id: i64,
) -> Result<()> {
    let (liked, count) = ctx
        .data()
        .store
        .toggle_wall_like(guild_key(ctx), ctx.author().id.get(), post_id)
        .await?;
    crate::ui::say(
        ctx,
        format!(
            "{} Реакций на запись: **{count}**.",
            if liked {
                "Отметка нравится добавлена."
            } else {
                "Отметка нравится убрана."
            }
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn wall_comment(
    ctx: Context<'_>,
    #[description = "ID записи стены"] post_id: i64,
    #[description = "Текст комментария, до 800 символов"] text: String,
) -> Result<()> {
    let id = ctx
        .data()
        .store
        .add_wall_comment(guild_key(ctx), ctx.author().id.get(), post_id, &text)
        .await?;
    crate::ui::say(ctx, format!("Комментарий добавлен (`#{id}`).")).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn wall_comments(
    ctx: Context<'_>,
    #[description = "ID записи стены"] post_id: i64,
    #[description = "Страница"] page: Option<u16>,
) -> Result<()> {
    let page = page.unwrap_or(1).clamp(1, 1000) as i64;
    let comments = ctx
        .data()
        .store
        .wall_comments(guild_key(ctx), ctx.author().id.get(), post_id, page - 1)
        .await?;
    if comments.is_empty() {
        crate::ui::say(
            ctx,
            "У этой записи пока нет комментариев на данной странице.",
        )
        .await?;
        return Ok(());
    }
    let lines = comments
        .into_iter()
        .map(|(author, text, at)| format!("<@{author}> · <t:{at}:R>\n{}", safe_mentions(&text)))
        .collect::<Vec<_>>()
        .join("\n\n");
    super::say_long(
        ctx,
        &format!("**Комментарии к записи `#{post_id}` · страница {page}**\n{lines}"),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn wall_delete(
    ctx: Context<'_>,
    #[description = "ID записи стены"] post_id: i64,
) -> Result<()> {
    anyhow::ensure!(
        ctx.data()
            .store
            .delete_wall_post(guild_key(ctx), ctx.author().id.get(), post_id)
            .await?,
        anyhow!("Запись не найдена или ты не автор и не владелец стены.")
    );
    crate::ui::say(
        ctx,
        format!("Запись `#{post_id}` и её комментарии удалены."),
    )
    .await?;
    Ok(())
}
