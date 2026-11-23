//! Lavalink 4.2 audio core, queue policies and Discord voice recovery.

use anyhow::{bail, Context as _, Result};
use dashmap::DashMap;
use lavalink_rs::{
    client::LavalinkClient,
    model::{
        client::NodeDistributionStrategy,
        events::{Events, TrackEnd, TrackEndReason, TrackException, TrackStuck, WebSocketClosed},
        http::UpdatePlayer,
        player::Filters,
        track::{TrackData, TrackLoadData},
        BoxFuture,
    },
    node::NodeBuilder,
    player_context::{PlayerContext, TrackInQueue},
};
use poise::serenity_prelude as serenity;
use songbird::Songbird;
use std::{sync::Arc, time::Duration};

use super::catalog::{MusicCatalog, MusicRequest};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum LoopMode {
    #[default]
    Off,
    Track,
    Queue,
}

#[derive(Default)]
struct Runtime {
    manager: Option<Arc<Songbird>>,
    voice_channels: DashMap<u64, u64>,
    loop_modes: DashMap<u64, LoopMode>,
}

#[derive(Clone)]
pub struct Audio {
    client: LavalinkClient,
    catalog: MusicCatalog,
    runtime: Arc<Runtime>,
    manager: Arc<Songbird>,
}

impl Audio {
    pub async fn connect(
        address: &str,
        password: &str,
        tls: bool,
        bot_id: u64,
        manager: Arc<Songbird>,
    ) -> Result<Self> {
        if bot_id == 0 {
            bail!("Discord bot user ID must be non-zero");
        }
        let normalized = address
            .trim()
            .trim_start_matches("http://")
            .trim_start_matches("https://")
            .trim_end_matches('/')
            .to_owned();
        if normalized.is_empty() {
            bail!("LAVALINK_ADDRESS is empty");
        }
        let runtime = Arc::new(Runtime {
            manager: Some(manager.clone()),
            ..Default::default()
        });
        let events = Events {
            track_end: Some(on_track_end),
            track_exception: Some(on_track_exception),
            track_stuck: Some(on_track_stuck),
            websocket_closed: Some(on_websocket_closed),
            ..Default::default()
        };
        let node = NodeBuilder {
            hostname: normalized,
            password: password.to_owned(),
            user_id: lavalink_rs::model::UserId(bot_id),
            is_ssl: tls,
            ..Default::default()
        };
        let client = LavalinkClient::new_with_data(
            events,
            vec![node],
            NodeDistributionStrategy::sharded(),
            runtime.clone(),
        )
        .await;
        Ok(Self {
            client,
            catalog: MusicCatalog::new()?,
            runtime,
            manager,
        })
    }

    pub fn client(&self) -> &LavalinkClient {
        &self.client
    }

    pub fn manager(&self) -> &Arc<Songbird> {
        &self.manager
    }

    pub async fn join(
        &self,
        _ctx: &serenity::Context,
        guild_id: serenity::GuildId,
        channel_id: serenity::ChannelId,
    ) -> Result<PlayerContext> {
        let (connection, _) = self
            .manager
            .join_gateway(guild_id, channel_id)
            .await
            .context("Не удалось получить голосовую сессию Discord")?;
        self.runtime
            .voice_channels
            .insert(guild_id.get(), channel_id.get());
        if let Some(player) = self.client.get_player_context(guild_id) {
            player
                .update_player(
                    &UpdatePlayer {
                        voice: Some(connection.into()),
                        ..Default::default()
                    },
                    true,
                )
                .await
                .context("Не удалось обновить голосовое подключение Lavalink")?;
            return Ok(player);
        }
        Ok(self
            .client
            .create_player_context(guild_id, connection)
            .await
            .context("Lavalink не смог создать плеер")?)
    }

    pub async fn prepare(&self, query: &str) -> Result<MusicRequest> {
        self.catalog.prepare(query).await
    }

    pub async fn load(
        &self,
        guild_id: serenity::GuildId,
        request: &MusicRequest,
    ) -> Result<Vec<TrackData>> {
        self.load_with_search_limit(guild_id, request, 1).await
    }

    pub async fn search(
        &self,
        guild_id: serenity::GuildId,
        request: &MusicRequest,
    ) -> Result<Vec<TrackData>> {
        self.load_with_search_limit(guild_id, request, 10).await
    }

    async fn load_with_search_limit(
        &self,
        guild_id: serenity::GuildId,
        request: &MusicRequest,
        search_limit: usize,
    ) -> Result<Vec<TrackData>> {
        let query = if let Some(url) = request.direct_url.as_deref() {
            if !super::catalog::direct_url_allowed(url, request.source) {
                bail!("Lavalink принимает прямые ссылки только на YouTube и SoundCloud.");
            }
            url.to_owned()
        } else {
            request.source.query(&request.term)
        };
        let result = self
            .client
            .load_tracks(guild_id, &query)
            .await
            .context("Lavalink не смог найти трек")?;
        let tracks = match result.data {
            Some(TrackLoadData::Track(track)) => vec![track],
            Some(TrackLoadData::Search(tracks)) => tracks.into_iter().take(search_limit).collect(),
            Some(TrackLoadData::Playlist(playlist)) => {
                playlist.tracks.into_iter().take(50).collect()
            }
            Some(TrackLoadData::Error(error)) => bail!("{}", error.message),
            None => Vec::new(),
        };
        Ok(tracks)
    }

    pub fn loop_mode(&self, guild: serenity::GuildId) -> LoopMode {
        self.runtime
            .loop_modes
            .get(&guild.get())
            .map(|mode| *mode)
            .unwrap_or_default()
    }

    pub fn set_loop_mode(&self, guild: serenity::GuildId, mode: LoopMode) {
        self.runtime.loop_modes.insert(guild.get(), mode);
    }

    pub async fn disconnect_guild(&self, guild_id: serenity::GuildId) {
        self.runtime.voice_channels.remove(&guild_id.get());
        self.runtime.loop_modes.remove(&guild_id.get());

        if let Some(player) = self.client.get_player_context(guild_id) {
            if let Err(error) = player.get_queue().clear() {
                tracing::warn!(guild_id = guild_id.get(), error = %error, "could not clear disconnected music queue");
            }
            if let Err(error) = player.stop_now().await {
                tracing::warn!(guild_id = guild_id.get(), error = %error, "could not stop disconnected music player");
            }
            if let Err(error) = self.client.delete_player(guild_id).await {
                tracing::warn!(guild_id = guild_id.get(), error = %error, "could not delete disconnected music player");
            }
        }

        if let Err(error) = self.manager.leave(guild_id).await {
            tracing::debug!(guild_id = guild_id.get(), error = %error, "voice connection was already closed");
        }
    }

    pub fn remember_voice_state(
        &self,
        ctx: serenity::Context,
        guild_id: serenity::GuildId,
        user_id: serenity::UserId,
        channel_id: Option<serenity::ChannelId>,
    ) {
        let Some(voice_channel) = self
            .runtime
            .voice_channels
            .get(&guild_id.get())
            .map(|id| *id)
        else {
            return;
        };
        if channel_id.is_some_and(|id| id.get() == voice_channel) {
            return;
        }
        let bot_id = self.client_user_id();
        let is_empty = ctx.cache.guild(guild_id).is_some_and(|guild| {
            !guild.voice_states.iter().any(|(user, state)| {
                user.get() != user_id.get()
                    && user.get() != bot_id
                    && state
                        .channel_id
                        .is_some_and(|channel| channel.get() == voice_channel)
            })
        });
        if is_empty {
            let client = self.client.clone();
            let manager = self.manager.clone();
            let channels = self.runtime.voice_channels.clone();
            let loop_modes = self.runtime.loop_modes.clone();
            let guild = guild_id;
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_secs(60)).await;
                if channels.get(&guild.get()).map(|id| *id) != Some(voice_channel) {
                    return;
                }
                let still_empty = ctx.cache.guild(guild).is_some_and(|cached| {
                    !cached.voice_states.iter().any(|(user, state)| {
                        user.get() != bot_id
                            && state
                                .channel_id
                                .is_some_and(|channel| channel.get() == voice_channel)
                    })
                });
                if !still_empty {
                    return;
                }
                channels.remove(&guild.get());
                loop_modes.remove(&guild.get());
                if let Some(player) = client.get_player_context(guild) {
                    let _ = player.get_queue().clear();
                    let _ = player.stop_now().await;
                    let _ = client.delete_player(guild).await;
                }
                let _ = manager.leave(guild).await;
            });
        }
    }

    fn client_user_id(&self) -> u64 {
        self.client
            .nodes
            .first()
            .map(|node| node.user_id.0)
            .unwrap_or_default()
    }

    pub async fn apply_filters(&self, player: &PlayerContext, filters: Filters) -> Result<()> {
        player.set_filters(filters).await?;
        Ok(())
    }
}

fn on_track_end(
    client: LavalinkClient,
    _session: String,
    event: &TrackEnd,
) -> BoxFuture<'static, ()> {
    let event = event.clone();
    Box::pin(async move {
        let Some(player) = client.get_player_context(event.guild_id) else {
            return;
        };
        let continue_playback = matches!(
            event.reason,
            TrackEndReason::Finished | TrackEndReason::LoadFailed
        );
        if matches!(event.reason, TrackEndReason::Finished) {
            let mode = client
                .data::<Runtime>()
                .ok()
                .and_then(|runtime| runtime.loop_modes.get(&event.guild_id.0).map(|mode| *mode))
                .unwrap_or_default();
            match mode {
                LoopMode::Track => {
                    let _ = player.get_queue().push_to_front(TrackInQueue {
                        track: event.track.clone(),
                        ..Default::default()
                    });
                }
                LoopMode::Queue => {
                    let _ = player.get_queue().push_to_back(TrackInQueue {
                        track: event.track.clone(),
                        ..Default::default()
                    });
                }
                LoopMode::Off => {}
            }
        }
        if continue_playback {
            if let Err(error) = player.finish(true) {
                tracing::error!(guild_id = event.guild_id.0, error = %error, "failed to advance Lavalink queue");
            }
        }
    })
}

fn on_track_exception(
    _client: LavalinkClient,
    _session: String,
    event: &TrackException,
) -> BoxFuture<'static, ()> {
    let event = event.clone();
    Box::pin(async move {
        tracing::warn!(guild_id = event.guild_id.0, title = %event.track.info.title, error = ?event.exception, "Lavalink track exception; queue will advance through track_end");
    })
}

fn on_track_stuck(
    _client: LavalinkClient,
    _session: String,
    event: &TrackStuck,
) -> BoxFuture<'static, ()> {
    let event = event.clone();
    Box::pin(async move {
        tracing::warn!(guild_id = event.guild_id.0, title = %event.track.info.title, threshold_ms = event.threshold_ms, "Lavalink track stuck; queue will advance through track_end");
    })
}

fn on_websocket_closed(
    client: LavalinkClient,
    _session: String,
    event: &WebSocketClosed,
) -> BoxFuture<'static, ()> {
    let event = event.clone();
    Box::pin(async move {
        let Ok(runtime) = client.data::<Runtime>() else {
            return;
        };
        let Some(channel) = runtime.voice_channels.get(&event.guild_id.0).map(|id| *id) else {
            return;
        };
        let Some(manager) = runtime.manager.clone() else {
            return;
        };
        tracing::warn!(guild_id = event.guild_id.0, code = event.code, reason = %event.reason, "Discord voice websocket closed; reconnecting Lavalink voice");
        for attempt in 0..5u64 {
            tokio::time::sleep(Duration::from_secs(1u64 << attempt)).await;
            let guild_id = serenity::GuildId::new(event.guild_id.0);
            let channel_id = serenity::ChannelId::new(channel);
            let Ok((connection, _)) = manager.join_gateway(guild_id, channel_id).await else {
                continue;
            };
            let Some(player) = client.get_player_context(event.guild_id) else {
                break;
            };
            match player
                .update_player(
                    &UpdatePlayer {
                        voice: Some(connection.into()),
                        ..Default::default()
                    },
                    true,
                )
                .await
            {
                Ok(_) => {
                    tracing::info!(
                        guild_id = event.guild_id.0,
                        attempt = attempt + 1,
                        "Lavalink voice connection restored"
                    );
                    return;
                }
                Err(error) => {
                    tracing::warn!(guild_id = event.guild_id.0, error = %error, "Lavalink voice reconnect attempt failed")
                }
            }
        }
        tracing::error!(
            guild_id = event.guild_id.0,
            "Lavalink voice reconnection failed after retries"
        );
    })
}

pub fn format_duration(length_ms: u64) -> String {
    let total_seconds = length_ms / 1000;
    let hours = total_seconds / 3600;
    let minutes = (total_seconds % 3600) / 60;
    let seconds = total_seconds % 60;
    if hours > 0 {
        format!("{hours}:{minutes:02}:{seconds:02}")
    } else {
        format!("{minutes}:{seconds:02}")
    }
}

pub fn user_voice_channel(
    ctx: &serenity::Context,
    guild_id: serenity::GuildId,
    user_id: serenity::UserId,
) -> Option<serenity::ChannelId> {
    ctx.cache
        .guild(guild_id)?
        .voice_states
        .get(&user_id)?
        .channel_id
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn duration_format_is_readable() {
        assert_eq!(format_duration(90_000), "1:30");
        assert_eq!(format_duration(3_661_000), "1:01:01");
    }
    #[test]
    fn loop_modes_are_explicit_and_off_by_default() {
        assert_eq!(LoopMode::default(), LoopMode::Off);
    }
}
