use chrono::{Datelike, Timelike, Utc};
use poise::serenity_prelude as serenity;
use std::time::Duration;
use tokio::time::MissedTickBehavior;
use tracing::{info, warn};

use crate::storage::Store;

const ROLE_DURATION_SECONDS: i64 = 24 * 60 * 60;

async fn announce_guild(ctx: &serenity::Context, store: &Store, guild_id: serenity::GuildId) {
    let settings = match store
        .guild_settings(guild_id.get(), &crate::web::settings_defaults())
        .await
    {
        Ok(settings) => settings,
        Err(error) => {
            warn!(guild_id = guild_id.get(), %error, "could not read birthday settings");
            return;
        }
    };
    if settings["birthdays_enabled"] != true {
        return;
    }
    let timezone = settings["timezone"]
        .as_str()
        .unwrap_or("UTC")
        .parse::<chrono_tz::Tz>()
        .unwrap_or(chrono_tz::UTC);
    let now = Utc::now().with_timezone(&timezone);
    let hour = settings["birthday_hour"].as_u64().unwrap_or(9).min(23) as u32;
    if now.hour() < hour {
        return;
    }
    let user_ids = match store
        .birthdays_for_day(guild_id.get(), now.month(), now.day())
        .await
    {
        Ok(users) if !users.is_empty() => users,
        Ok(_) => return,
        Err(error) => {
            warn!(guild_id = guild_id.get(), %error, "could not read today's birthdays");
            return;
        }
    };
    let channel_id = settings["birthday_channel_id"]
        .as_u64()
        .or_else(|| settings["birthday_channel_id"].as_str()?.parse().ok())
        .map(serenity::ChannelId::new);
    let Some(channel_id) = channel_id else {
        return;
    };
    let local_date = now.format("%Y-%m-%d").to_string();
    let mut new_user_ids = Vec::new();
    for user_id in user_ids {
        match store
            .claim_birthday_announcement(guild_id.get(), user_id, &local_date)
            .await
        {
            Ok(true) => new_user_ids.push(user_id),
            Ok(false) => {}
            Err(error) => {
                warn!(guild_id = guild_id.get(), user_id, %error, "could not claim birthday announcement")
            }
        }
    }
    if new_user_ids.is_empty() {
        return;
    }

    let birthday_role_id = settings["birthday_role_id"]
        .as_u64()
        .or_else(|| settings["birthday_role_id"].as_str()?.parse().ok())
        .map(serenity::RoleId::new);
    let role_is_valid = if let Some(role_id) = birthday_role_id {
        match guild_id.roles(&ctx.http).await {
            Ok(roles) => roles
                .get(&role_id)
                .is_some_and(|role| role.id.get() != guild_id.get() && !role.managed),
            Err(error) => {
                warn!(guild_id = guild_id.get(), %error, "could not read birthday role");
                false
            }
        }
    } else {
        false
    };

    let mut mentions = Vec::new();
    for user_id in new_user_ids {
        let member = match guild_id
            .member(&ctx.http, serenity::UserId::new(user_id))
            .await
        {
            Ok(member) if !member.user.bot => member,
            _ => continue,
        };
        mentions.push(member.user.id.to_string());
        if role_is_valid {
            if let Some(role_id) = birthday_role_id.filter(|role| !member.roles.contains(role)) {
                if member.add_role(&ctx.http, role_id).await.is_ok() {
                    if let Err(error) = store
                        .set_birthday_active_role(
                            guild_id.get(),
                            user_id,
                            role_id.get(),
                            now.timestamp() + ROLE_DURATION_SECONDS,
                        )
                        .await
                    {
                        warn!(guild_id = guild_id.get(), user_id, %error, "could not track birthday role expiry");
                    }
                }
            }
        }
    }
    if mentions.is_empty() {
        return;
    }
    let mentions = mentions
        .iter()
        .map(|id| format!("<@{id}>"))
        .collect::<Vec<_>>()
        .join(" ");
    let content = format!("{mentions}\nС днём рождения! 🎉");
    if let Err(error) = crate::ui::channel_message(&ctx, channel_id, content).await {
        warn!(guild_id = guild_id.get(), %error, "could not send birthday greeting");
    }
}

async fn remove_expired_roles(ctx: &serenity::Context, store: &Store) {
    let due = match store.due_birthday_roles(Utc::now().timestamp()).await {
        Ok(rows) => rows,
        Err(error) => {
            warn!(%error, "could not load expired birthday roles");
            return;
        }
    };
    for (guild_id, user_id, role_id) in due {
        let guild = serenity::GuildId::new(guild_id);
        match guild
            .member(&ctx.http, serenity::UserId::new(user_id))
            .await
        {
            Ok(member) if member.roles.contains(&serenity::RoleId::new(role_id)) => {
                if let Err(error) = member
                    .remove_role(&ctx.http, serenity::RoleId::new(role_id))
                    .await
                {
                    if !matches!(error, serenity::Error::Http(ref http) if http.status_code().map(|code| code.as_u16()) == Some(404))
                    {
                        warn!(guild_id, user_id, %error, "could not remove expired birthday role");
                        continue;
                    }
                }
            }
            Err(error) if !matches!(error, serenity::Error::Http(ref http) if http.status_code().map(|code| code.as_u16()) == Some(404)) =>
            {
                warn!(guild_id, user_id, %error, "could not check expired birthday role");
                continue;
            }
            _ => {}
        }
        if let Err(error) = store.clear_birthday_role(guild_id, user_id).await {
            warn!(guild_id, user_id, %error, "could not clear expired birthday role state");
        }
    }
}

pub async fn run(ctx: serenity::Context, store: Store) {
    let mut tick = tokio::time::interval(Duration::from_secs(60));
    tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
    info!("birthday scheduler started");
    loop {
        tick.tick().await;
        for guild_id in ctx.cache.guilds() {
            announce_guild(&ctx, &store, guild_id).await;
        }
        remove_expired_roles(&ctx, &store).await;
    }
}
