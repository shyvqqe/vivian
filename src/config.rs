use anyhow::{Context as _, Result};
use std::{env, path::PathBuf};

#[derive(Clone, Debug)]
pub struct Config {
    pub discord_token: String,
    pub discord_guild_id: Option<u64>,
    pub lavalink_address: String,
    pub lavalink_password: String,
    pub lavalink_tls: bool,
    pub database_url: String,
    pub ai_history_turns: i64,
    pub ai_reply_language: String,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        dotenvy::dotenv().ok();
        let discord_token = env::var("DISCORD_TOKEN").context("DISCORD_TOKEN is required")?;
        let discord_guild_id = env::var("DISCORD_GUILD_ID")
            .ok()
            .and_then(|value| value.trim().parse().ok());
        let database_url =
            env::var("DATABASE_URL").unwrap_or_else(|_| "sqlite://data/vivian.db".into());
        if let Some(path) = database_url.strip_prefix("sqlite://") {
            if let Some(parent) = PathBuf::from(path).parent() {
                if !parent.as_os_str().is_empty() {
                    std::fs::create_dir_all(parent)
                        .context("could not create database directory")?;
                }
            }
        }
        Ok(Self {
            discord_token,
            discord_guild_id,
            lavalink_address: env::var("LAVALINK_ADDRESS")
                .unwrap_or_else(|_| "127.0.0.1:2333".into()),
            lavalink_password: env::var("LAVALINK_PASSWORD")
                .unwrap_or_else(|_| "change-this-password".into()),
            lavalink_tls: env::var("LAVALINK_TLS")
                .is_ok_and(|value| value.eq_ignore_ascii_case("true")),
            database_url,
            ai_history_turns: env::var("AI_HISTORY_TURNS")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(12)
                .clamp(0, 40),
            ai_reply_language: env::var("AI_REPLY_LANGUAGE").unwrap_or_else(|_| "ru".into()),
        })
    }
}
