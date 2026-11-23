use super::{guild_key, say_long, Context};
use crate::ai::Provider;

#[poise::command(slash_command, prefix_command)]
pub async fn ai(
    ctx: Context<'_>,
    #[description = "auto, gemini, groq, deepseek или openrouter"] provider: Option<String>,
    #[description = "Переопределение модели из аккаунта выбранного провайдера"] model: Option<
        String,
    >,
) -> anyhow::Result<()> {
    let store = &ctx.data().store;
    let guild_id = guild_key(ctx);
    let user_id = ctx.author().id.get();
    if let Some(provider_name) = provider {
        if provider_name.eq_ignore_ascii_case("auto") {
            store.reset_preference(guild_id, user_id).await?;
            crate::ui::say(
                ctx,
                "Для AI включён автоматический выбор доступного провайдера.",
            )
            .await?;
            return Ok(());
        }
        let provider = Provider::parse(&provider_name).ok_or_else(|| {
            anyhow::anyhow!("Укажи `auto`, `gemini`, `groq`, `deepseek` или `openrouter`.")
        })?;
        if !provider.configured() {
            anyhow::bail!("Для {} не задан API-ключ.", provider.label());
        }
        let model = model.unwrap_or_else(|| match provider {
            Provider::Gemini => "gemini-3.8-flash".into(),
            Provider::Groq => "openai/gpt-oss-20b".into(),
            Provider::DeepSeek => "deepseek-chat".into(),
            Provider::OpenRouter => "google/gemma-4-31b-it:free".into(),
        });
        let model = model.trim();
        if model.is_empty()
            || model.chars().count() > 100
            || !model
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "-_.:/".contains(c))
        {
            anyhow::bail!(
                "В модели разрешены только латинские буквы, цифры и `-_.:/` (до 100 символов)."
            );
        }
        store
            .set_preference(guild_id, user_id, provider.key(), model)
            .await?;
        crate::ui::say(
            ctx,
            format!(
                "AI для тебя настроен: **{} · {}**. Автовыбор можно вернуть командой `/ai auto`.",
                provider.label(),
                model
            ),
        )
        .await?;
        return Ok(());
    }
    let current = store.preference(guild_id, user_id).await?;
    let status = [
        Provider::Gemini,
        Provider::Groq,
        Provider::DeepSeek,
        Provider::OpenRouter,
    ]
    .into_iter()
    .map(|p| {
        format!(
            "{}: {}",
            p.label(),
            if p.configured() {
                "API-ключ задан"
            } else {
                "нет ключа"
            }
        )
    })
    .collect::<Vec<_>>()
    .join("\n");
    let selected = current
        .map(|(p, m)| {
            format!(
                "\nТвой выбор: **{} · {}**",
                Provider::parse(&p).map(|x| x.label()).unwrap_or("unknown"),
                m
            )
        })
        .unwrap_or_else(|| "\nАктивен автоматический выбор.".into());
    crate::ui::say(ctx, format!("**AI провайдеры**\n{status}{selected}\n\nНастройка: `/ai provider:groq model:openai/gpt-oss-20b`. Чтобы сбросить: `/ai provider:auto`. Диалог хранится отдельно для каждого участника и сервера; `/forget` удаляет твою память.")).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, aliases("gpt", "chat"))]
pub async fn ask(
    ctx: Context<'_>,
    #[description = "Сообщение для Vivian"] prompt: String,
) -> anyhow::Result<()> {
    let prompt = prompt.trim();
    if prompt.is_empty() {
        anyhow::bail!("Напиши вопрос для Vivian.");
    }
    let _typing = ctx.defer_or_broadcast().await?;
    let data = ctx.data();
    let (reply, provider, model) = crate::ai::answer(
        &data.store,
        &data.http,
        guild_key(ctx),
        ctx.author().id.get(),
        prompt,
        &[],
        data.config.ai_history_turns,
        &data.config.ai_reply_language,
    )
    .await?;
    say_long(ctx, &format!("**Vivian · {provider} / {model}**\n{reply}")).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, aliases("clearai", "forgetai"))]
pub async fn forget(ctx: Context<'_>) -> anyhow::Result<()> {
    let deleted = ctx
        .data()
        .store
        .clear_history(guild_key(ctx), ctx.author().id.get())
        .await?;
    crate::ui::say(
        ctx,
        format!("Удалено записей из твоей AI-памяти: **{deleted}**."),
    )
    .await?;
    Ok(())
}

pub(crate) async fn handle_mention(
    ctx: &serenity::Context,
    msg: &serenity::Message,
    data: &crate::Data,
    prompt: &str,
) -> anyhow::Result<()> {
    let images = crate::ai::download_images(&data.http, &msg.attachments).await?;
    let (reply, provider, model) = crate::ai::answer(
        &data.store,
        &data.http,
        msg.guild_id.map(|id| id.get()).unwrap_or(0),
        msg.author.id.get(),
        prompt,
        &images,
        data.config.ai_history_turns,
        &data.config.ai_reply_language,
    )
    .await?;
    let response = format!("**Vivian · {provider} / {model}**\n{reply}");
    let mut remaining = response.as_str();
    while !remaining.is_empty() {
        let boundary = remaining
            .char_indices()
            .take_while(|(i, _)| *i < 1850)
            .last()
            .map(|(i, ch)| i + ch.len_utf8())
            .unwrap_or(remaining.len());
        msg.reply(ctx, &remaining[..boundary]).await?;
        remaining = remaining[boundary..].trim_start();
    }
    Ok(())
}

use poise::serenity_prelude as serenity;
