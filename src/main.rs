#![recursion_limit = "256"]

mod ai;
mod birthday;
mod commands;
mod config;
mod music;
mod permissions;
mod rooms;
mod storage;
mod ui;
mod web;

use anyhow::{Context as _, Result};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use config::Config;
use dashmap::DashMap;
use poise::serenity_prelude as serenity;
use reqwest::Client as HttpClient;
use songbird::SerenityInit;
use std::{
    collections::VecDeque,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};
use storage::Store;
use tokio::time::Instant;
use tracing::{info, warn};

#[derive(Clone)]
pub struct Data {
    pub config: Config,
    pub store: Store,
    pub http: HttpClient,
    pub audio: music::audio::Audio,
    pub rooms: rooms::RoomManager,
    pub bot_user_id: u64,
    pub spam_tracker: Arc<DashMap<(u64, u64), VecDeque<(Instant, String)>>>,
    pub voice_joined_at: Arc<DashMap<(u64, u64), i64>>,
    pub discord_ready: Arc<AtomicBool>,
    pub birthday_task_started: Arc<AtomicBool>,
}

type Error = anyhow::Error;
type FrameworkContext<'a> = poise::FrameworkContext<'a, Data, Error>;

fn bot_id_from_config(config: &Config) -> Result<u64> {
    if let Ok(value) = std::env::var("DISCORD_CLIENT_ID") {
        if let Ok(id) = value.trim().parse::<u64>() {
            return Ok(id);
        }
    }
    let encoded = config.discord_token.split('.').next().unwrap_or_default();
    let bytes = URL_SAFE_NO_PAD
        .decode(encoded)
        .context("Не удалось определить ID бота из DISCORD_TOKEN; задай DISCORD_CLIENT_ID")?;
    String::from_utf8(bytes)
        .context("Некорректный ID в DISCORD_TOKEN")?
        .parse::<u64>()
        .context("Некорректный ID в DISCORD_TOKEN")
}

async fn event_handler(framework: FrameworkContext<'_>, event: &serenity::FullEvent) -> Result<()> {
    let data = framework.user_data;
    let ctx = framework.serenity_context;
    match event {
        serenity::FullEvent::Ready { data_about_bot, .. } => {
            data.discord_ready.store(true, Ordering::Relaxed);
            info!(username = %data_about_bot.user.name, id = data_about_bot.user.id.get(), "Vivian is ready");
            let rooms = data.rooms.clone();
            let room_ctx = ctx.clone();
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_secs(3)).await;
                if let Err(error) = rooms.reconcile(&room_ctx).await {
                    warn!(error = %error, "private voice room recovery did not complete");
                }
            });
            if !data.birthday_task_started.swap(true, Ordering::AcqRel) {
                let store = data.store.clone();
                let ctx = ctx.clone();
                tokio::spawn(async move { birthday::run(ctx, store).await });
            }
        }
        serenity::FullEvent::VoiceServerUpdate { event } => {
            if let Some(guild_id) = event.guild_id {
                data.audio.client().handle_voice_server_update(
                    guild_id,
                    event.token.clone(),
                    event.endpoint.clone(),
                );
            }
        }
        serenity::FullEvent::VoiceStateUpdate { old, new } => {
            if new.user_id.get() == data.bot_user_id {
                if let Some(guild_id) = new.guild_id {
                    data.audio.client().handle_voice_state_update(
                        guild_id,
                        new.channel_id,
                        new.user_id,
                        new.session_id.clone(),
                    );
                    if new.channel_id.is_none() {
                        data.audio.disconnect_guild(guild_id).await;
                    }
                }
            }
            if let Some(guild_id) = new.guild_id {
                if new.user_id.get() != data.bot_user_id {
                    let old_channel = old.as_ref().and_then(|state| state.channel_id);
                    if old_channel != new.channel_id {
                        let key = (guild_id.get(), new.user_id.get());
                        if let Some((_, started)) = data.voice_joined_at.remove(&key) {
                            let seconds = chrono::Utc::now().timestamp().saturating_sub(started);
                            if let Err(error) = data
                                .store
                                .award_voice_activity(guild_id.get(), new.user_id.get(), seconds)
                                .await
                            {
                                warn!(error=%error,"could not save voice XP");
                            }
                        }
                        if new.channel_id.is_some() {
                            data.voice_joined_at
                                .insert(key, chrono::Utc::now().timestamp());
                        }
                    }
                }
                data.audio
                    .remember_voice_state(ctx.clone(), guild_id, new.user_id, new.channel_id);
                data.rooms
                    .on_voice_state_update(
                        ctx.clone(),
                        guild_id,
                        old.as_ref().and_then(|state| state.channel_id),
                        new.channel_id,
                    )
                    .await;
            }
        }
        serenity::FullEvent::Message { new_message } => {
            if new_message.author.bot {
                return Ok(());
            }
            let Some(guild_id) = new_message.guild_id else {
                return Ok(());
            };
            let is_command = new_message.content.starts_with('!');
            if !is_command {
                if let Err(error) = data
                    .store
                    .award_message_activity(
                        guild_id.get(),
                        new_message.author.id.get(),
                        &new_message.content,
                        new_message.timestamp.unix_timestamp(),
                    )
                    .await
                {
                    warn!(error=%error,"could not save message XP");
                }
                let elevated = if let Some(member) = new_message.member.as_ref() {
                    if let Some(guild) = ctx.cache.guild(guild_id) {
                        if guild.owner_id == new_message.author.id {
                            true
                        } else {
                            let mut permissions = serenity::Permissions::empty();
                            if let Some(role) =
                                guild.roles.get(&serenity::RoleId::new(guild_id.get()))
                            {
                                permissions |= role.permissions;
                            }
                            for role_id in &member.roles {
                                if let Some(role) = guild.roles.get(role_id) {
                                    permissions |= role.permissions;
                                }
                            }
                            permissions.intersects(
                                serenity::Permissions::ADMINISTRATOR
                                    | serenity::Permissions::MANAGE_MESSAGES
                                    | serenity::Permissions::MODERATE_MEMBERS,
                            )
                        }
                    } else {
                        false
                    }
                } else {
                    false
                };
                if !elevated {
                    let now = Instant::now();
                    let normalized = new_message
                        .content
                        .split_whitespace()
                        .collect::<Vec<_>>()
                        .join(" ")
                        .to_lowercase();
                    let key = (guild_id.get(), new_message.author.id.get());
                    let mut tracker = data.spam_tracker.entry(key).or_default();
                    while tracker
                        .front()
                        .is_some_and(|(at, _)| now.duration_since(*at) > Duration::from_secs(30))
                    {
                        tracker.pop_front();
                    }
                    tracker.push_back((now, normalized.clone()));
                    while tracker.len() > 20 {
                        tracker.pop_front();
                    }
                    let rapid = tracker
                        .iter()
                        .filter(|(at, _)| now.duration_since(*at) <= Duration::from_secs(8))
                        .count()
                        >= 5;
                    let repeated = tracker
                        .iter()
                        .filter(|(at, text)| {
                            now.duration_since(*at) <= Duration::from_secs(20)
                                && !normalized.is_empty()
                                && text.as_str() == normalized.as_str()
                        })
                        .count()
                        >= 3;
                    drop(tracker);
                    if rapid || repeated {
                        let reason = if repeated {
                            "Повтор одинакового сообщения"
                        } else {
                            "Флуд: 5 сообщений за 8 секунд"
                        };
                        if let Err(error) = new_message.delete(ctx).await {
                            warn!(error=%error,"automod could not delete spam message");
                        }
                        let repeated_incident = data
                            .store
                            .record_automod_incident(
                                guild_id.get(),
                                new_message.author.id.get(),
                                chrono::Utc::now().timestamp(),
                            )
                            .await
                            .unwrap_or(false);
                        let _ = data
                            .store
                            .add_warning(
                                guild_id.get(),
                                new_message.author.id.get(),
                                data.bot_user_id,
                                reason,
                            )
                            .await;
                        let _ = data
                            .store
                            .log_moderation_action(
                                guild_id.get(),
                                data.bot_user_id,
                                "automod",
                                Some(new_message.author.id.get()),
                                Some(new_message.channel_id.get()),
                                reason,
                            )
                            .await;
                        if repeated_incident {
                            let until = serenity::Timestamp::from_unix_timestamp(
                                chrono::Utc::now().timestamp() + 300,
                            )?;
                            if let Err(error) = guild_id
                                .edit_member(
                                    ctx,
                                    new_message.author.id,
                                    serenity::EditMember::new()
                                        .disable_communication_until_datetime(until)
                                        .audit_log_reason("Vivian automod repeated spam"),
                                )
                                .await
                            {
                                warn!(error=%error,user_id=new_message.author.id.get(),"automod timeout failed");
                            } else {
                                let _ = data
                                    .store
                                    .log_moderation_action(
                                        guild_id.get(),
                                        data.bot_user_id,
                                        "automod_timeout",
                                        Some(new_message.author.id.get()),
                                        Some(new_message.channel_id.get()),
                                        "Repeated spam",
                                    )
                                    .await;
                            }
                        }
                        let _=crate::ui::channel_message(ctx,new_message.channel_id,if repeated_incident{"Автомодерация удалила спам и выдала тайм-аут на 5 минут за повторное нарушение."}else{"Автомодерация удалила спам; повторное нарушение приведёт к тайм-ауту."}).await;
                        return Ok(());
                    }
                }
            }
            if is_command {
                return Ok(());
            }
            let mentioned = new_message
                .mentions
                .iter()
                .any(|user| user.id.get() == data.bot_user_id);
            if mentioned {
                let mention_a = format!("<@{}>", data.bot_user_id);
                let mention_b = format!("<@!{}>", data.bot_user_id);
                let prompt = new_message
                    .content
                    .replace(&mention_a, "")
                    .replace(&mention_b, "")
                    .trim()
                    .to_owned();
                let prompt = if prompt.is_empty() {
                    "Привет!".to_owned()
                } else {
                    prompt
                };
                if let Err(error) =
                    commands::ai::handle_mention(ctx, new_message, data, &prompt).await
                {
                    warn!(error = %error, user_id = new_message.author.id.get(), "AI mention handler failed");
                    let _ = new_message.reply(ctx, "Не удалось получить ответ от AI. Проверь подключение провайдера и попробуй позже.").await;
                }
            }
        }
        _ => {}
    }
    Ok(())
}

fn command_error_detail(command: &str, error: &Error) -> String {
    let chain = format!("{error:#}");
    let lower = chain.to_ascii_lowercase();
    let missing_permission = lower.contains("50013")
        || lower.contains("missing permissions")
        || lower.contains("missing permission")
        || (lower.contains("discord")
            && (lower.contains("403 forbidden") || lower.contains("status code: 403")));
    if missing_permission {
        if let Some(required) = permissions::bot_requirement_for_command(command) {
            let hierarchy = if matches!(command, "kick" | "ban" | "timeout" | "untimeout") {
                " Для действий над участником также проверь, что его высшая роль ниже роли Vivian."
            } else if command == "shop_buy" {
                " Проверь также, что роль Vivian стоит выше покупаемой косметической роли."
            } else {
                " Проверь также переопределения прав в канале."
            };
            return format!(
                "Для команды `/{command}` у Vivian должно быть право **{required}**, но Discord отклонил действие. Проверь это право у роли бота.{hierarchy} `Administrator` не требуется."
            );
        }
        return format!(
            "Discord отклонил действие команды `/{command}` из-за прав. Проверь доступ Vivian к этому каналу и права, необходимые именно этой команде; `Administrator` не требуется."
        );
    }
    if lower.contains("50001") || lower.contains("missing access") {
        return format!(
            "У Vivian нет доступа к каналу или участнику для команды `/{command}`. Проверь право **View Channels (Просматривать каналы)** и переопределения доступа."
        );
    }
    chain.chars().take(900).collect()
}

async fn command_error_reply(ctx: poise::Context<'_, Data, Error>, detail: String) {
    let lower = detail.to_lowercase();
    let detail = if lower.contains("429")
        || lower.contains("rate limit")
        || lower.contains("too many requests")
    {
        "Сервис временно ограничил запросы. Подожди пару минут и попробуй ещё раз.".to_owned()
    } else if lower.contains("у vivian должно быть право")
        || lower.contains("роли vivian не хватает права")
        || lower.contains("отклонил действие команды")
        || lower.contains("у vivian нет доступа к каналу")
    {
        detail.chars().take(900).collect::<String>()
    } else if lower.contains("50013")
        || lower.contains("missing permissions")
        || lower.contains("missing permission")
        || lower.contains("403 forbidden")
    {
        "Discord отклонил действие из-за отсутствующего права. Проверь право, указанное для этой команды в сообщении, и права роли Vivian в канале.".to_owned()
    } else if lower.contains("lavalink") || lower.contains("voice connection") {
        "Музыкальный сервер сейчас не ответил. Проверь подключение Lavalink и попробуй ещё раз через минуту.".to_owned()
    } else if lower.contains("sqlite") || lower.contains("database") || lower.contains("row decode")
    {
        "Не удалось сохранить данные из-за внутренней ошибки. Ошибка записана в журнал сервера; попробуй позже.".to_owned()
    } else if lower.contains("discord")
        || lower.contains("interaction")
        || lower.contains("websocket")
    {
        "Discord временно не выполнил запрос. Подожди минуту и повтори команду.".to_owned()
    } else if lower.contains("timed out")
        || lower.contains("timeout")
        || lower.contains("connection refused")
        || lower.contains("error sending request")
    {
        "Не удалось подключиться к одному из сервисов. Проверь состояние сервера и повтори команду позже.".to_owned()
    } else {
        detail.chars().take(900).collect::<String>()
    };
    let message = format!(
        "⚠️ **Команда не выполнена.**\n{detail}\nЕсли ошибка повторится, сообщи администратору сервера название команды и примерное время."
    );
    if let Err(send_error) = crate::ui::private(ctx, message).await {
        warn!(error = %send_error, "could not send command error reply");
    }
}

fn framework_error_handler(
    error: poise::FrameworkError<'_, Data, Error>,
) -> poise::BoxFuture<'_, ()> {
    Box::pin(async move {
        match error {
            poise::FrameworkError::Command { ctx, error, .. } => {
                let raw = command_error_detail(&ctx.command().name, &error);
                tracing::error!(
                    command = %ctx.command().name,
                    user_id = ctx.author().id.get(),
                    error = %error,
                    "command execution failed"
                );
                command_error_reply(ctx, raw).await;
            }
            poise::FrameworkError::ArgumentParse { ctx, error, .. } => {
                command_error_reply(
                    ctx,
                    format!("Не удалось разобрать аргумент команды: {error}. Проверь значения и попробуй снова."),
                )
                .await;
            }
            poise::FrameworkError::CooldownHit {
                ctx,
                remaining_cooldown,
                ..
            } => {
                command_error_reply(
                    ctx,
                    format!(
                        "Команда пока на паузе. Повтори через {} сек.",
                        remaining_cooldown.as_secs().max(1)
                    ),
                )
                .await
            }
            poise::FrameworkError::MissingBotPermissions {
                ctx,
                missing_permissions,
                ..
            } => {
                let detail = format!(
                    "Для команды `/{}` роли Vivian нужно право **{}**. Выдай это право роли бота и проверь переопределения прав канала. `Administrator` не требуется.",
                    ctx.command().name,
                    crate::permissions::describe(missing_permissions)
                );
                command_error_reply(ctx, detail).await;
            }
            poise::FrameworkError::MissingUserPermissions {
                ctx,
                missing_permissions,
                ..
            } => {
                let detail = format!(
                    "Для запуска `/{}` тебе нужно право участника **{}** на сервере. Это требование относится к пользователю команды, а не к роли Vivian.",
                    ctx.command().name,
                    missing_permissions.map(crate::permissions::describe).unwrap_or_else(|| "права сервера или канала".into())
                );
                command_error_reply(ctx, detail).await;
            }
            poise::FrameworkError::GuildOnly { ctx, .. } => {
                command_error_reply(
                    ctx,
                    "Эта команда работает только на сервере Discord.".into(),
                )
                .await;
            }
            poise::FrameworkError::DmOnly { ctx, .. } => {
                command_error_reply(
                    ctx,
                    "Эту команду нужно запускать в личных сообщениях с ботом.".into(),
                )
                .await;
            }
            poise::FrameworkError::NsfwOnly { ctx, .. } => {
                command_error_reply(
                    ctx,
                    "Эта команда доступна только в канале с возрастным ограничением.".into(),
                )
                .await;
            }
            poise::FrameworkError::NotAnOwner { ctx, .. } => {
                command_error_reply(ctx, "Эта команда доступна только владельцу бота.".into())
                    .await;
            }
            poise::FrameworkError::CommandCheckFailed { ctx, error, .. } => {
                if let Some(error) = error {
                    tracing::warn!(error = %error, command = %ctx.command().name, "command check failed");
                }
                command_error_reply(ctx, format!("Команда `/{}` не прошла проверку доступа. Проверь, что запускаешь её на сервере и что Discord не ограничивает её для твоей роли в настройках сервера → Интеграции → Vivian.", ctx.command().name)).await;
            }
            poise::FrameworkError::PermissionFetchFailed { ctx, .. } => {
                command_error_reply(ctx, "Не удалось проверить права участника. Проверь, что Vivian видит текущий канал и сервер; `Administrator` не нужен.".into()).await;
            }
            poise::FrameworkError::CommandPanic { ctx, .. } => {
                tracing::error!(command = %ctx.command().name, user_id = ctx.author().id.get(), "command panicked");
                command_error_reply(ctx, "Внутренняя ошибка. Попробуй ещё раз; если она повторяется, сообщи администратору сервера.".into()).await;
            }
            other => {
                if let Err(send_error) = poise::builtins::on_error(other).await {
                    tracing::error!(error = %send_error, "framework error handler failed");
                }
            }
        }
    })
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();
    let config = Config::from_env()?;
    let bot_user_id = bot_id_from_config(&config)?;
    let store = Store::connect(&config.database_url)
        .await
        .context("SQLite initialization failed")?;
    let http = HttpClient::builder()
        .timeout(Duration::from_secs(45))
        .user_agent("VivianDiscordBot/0.1 (+https://esil.life)")
        .build()?;
    let voice = songbird::Songbird::serenity();
    let rooms = rooms::RoomManager::new(store.clone(), bot_user_id);
    let audio = music::audio::Audio::connect(
        &config.lavalink_address,
        &config.lavalink_password,
        config.lavalink_tls,
        bot_user_id,
        voice.clone(),
    )
    .await?;
    if ![
        ai::Provider::Gemini,
        ai::Provider::Groq,
        ai::Provider::DeepSeek,
        ai::Provider::OpenRouter,
    ]
    .into_iter()
    .any(ai::Provider::configured)
    {
        warn!("No AI API key configured; AI commands will report setup instructions.");
    }
    info!(address = %config.lavalink_address, "Lavalink 4.x client initialized");

    let data = Data {
        config: config.clone(),
        store,
        http,
        audio,
        rooms,
        bot_user_id,
        spam_tracker: Arc::new(DashMap::new()),
        voice_joined_at: Arc::new(DashMap::new()),
        discord_ready: Arc::new(AtomicBool::new(false)),
        birthday_task_started: Arc::new(AtomicBool::new(false)),
    };
    let _web_server = web::start(
        &config,
        data.store.clone(),
        data.http.clone(),
        bot_user_id,
        data.discord_ready.clone(),
    )
    .await
    .context("dashboard/API server could not start")?;
    let setup_data = data.clone();
    let framework = poise::Framework::builder()
        .options(poise::FrameworkOptions {
            commands: commands::all(),
            on_error: framework_error_handler,
            prefix_options: poise::PrefixFrameworkOptions {
                prefix: Some("!".into()),
                ..Default::default()
            },
            event_handler: |framework, event| Box::pin(event_handler(framework, event)),
            ..Default::default()
        })
        .setup(move |ctx, ready, framework| {
            let data = setup_data.clone();
            Box::pin(async move {
                info!(username = %ready.user.name, "Registering Vivian commands");
                if let Some(guild_id) = data.config.discord_guild_id {
                    let commands =
                        poise::builtins::create_application_commands(&framework.options().commands);
                    serenity::GuildId::new(guild_id)
                        .set_commands(ctx, commands)
                        .await?;
                    info!(guild_id, "Registered guild slash commands");
                } else {
                    poise::builtins::register_globally(ctx, &framework.options().commands).await?;
                }
                Ok(data)
            })
        })
        .build();

    let intents = serenity::GatewayIntents::GUILDS
        | serenity::GatewayIntents::GUILD_MESSAGES
        | serenity::GatewayIntents::DIRECT_MESSAGES
        | serenity::GatewayIntents::MESSAGE_CONTENT
        | serenity::GatewayIntents::GUILD_VOICE_STATES;
    let mut client = serenity::ClientBuilder::new(config.discord_token, intents)
        .framework(framework)
        .register_songbird_with(voice)
        .await
        .context("Discord client setup failed")?;
    client.start().await.context("Discord gateway stopped")?;
    Ok(())
}
