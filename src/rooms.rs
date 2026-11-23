//! Persistent private voice rooms, owner actions and empty-room cleanup.

use anyhow::{anyhow, bail, Result};
use dashmap::DashMap;
use poise::serenity_prelude as serenity;
use serenity::{
    ChannelType, CreateChannel, EditChannel, PermissionOverwrite, PermissionOverwriteType,
    Permissions,
};
use std::sync::Arc;
use tokio::sync::Mutex;

use crate::storage::{PrivateRoom, Store};

const MAX_ROOMS: i64 = 20;
const EMPTY_GRACE_SECS: u64 = 60;

#[derive(Clone)]
pub struct RoomManager {
    store: Store,
    bot_id: u64,
    locks: Arc<DashMap<u64, Arc<Mutex<()>>>>,
}

impl RoomManager {
    pub fn new(store: Store, bot_id: u64) -> Self {
        Self {
            store,
            bot_id,
            locks: Arc::new(DashMap::new()),
        }
    }

    fn lock(&self, guild: serenity::GuildId) -> Arc<Mutex<()>> {
        self.locks
            .entry(guild.get())
            .or_insert_with(|| Arc::new(Mutex::new(())))
            .clone()
    }

    fn sanitized_name(name: Option<&str>, display: &str) -> Result<String> {
        let value = name
            .unwrap_or_else(|| display)
            .chars()
            .map(|c| if c.is_control() { ' ' } else { c })
            .collect::<String>();
        let value = value.split_whitespace().collect::<Vec<_>>().join(" ");
        if value.is_empty() || value.chars().count() > 80 {
            bail!("Название комнаты должно содержать от 1 до 80 символов.");
        }
        Ok(value)
    }

    async fn check_bot_rights(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
    ) -> Result<()> {
        let bot = guild
            .member(&ctx.http, serenity::UserId::new(self.bot_id))
            .await?;
        #[allow(deprecated)]
        let permissions = bot.permissions(&ctx.cache)?;
        let needed =
            Permissions::VIEW_CHANNEL | Permissions::MANAGE_CHANNELS | Permissions::MANAGE_ROLES;
        if !permissions.contains(needed) {
            let missing = Permissions::from_bits_truncate(needed.bits() & !permissions.bits());
            bail!("Для создания приватных комнат роли Vivian не хватает: **{}**. `Administrator` не требуется.", crate::permissions::describe(missing));
        }
        Ok(())
    }

    async fn ensure_category(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
    ) -> Result<serenity::ChannelId> {
        let settings = self.store.private_room_settings(guild.get()).await?;
        if settings.as_ref().is_some_and(|(_, enabled)| !enabled) {
            bail!("Приватные комнаты отключены администрацией сервера.");
        }
        if let Some(Some(category_id)) = settings.map(|(category, _)| category) {
            let id = serenity::ChannelId::new(category_id);
            if let Some(channel) = ctx
                .cache
                .guild(guild)
                .and_then(|g| g.channels.get(&id).cloned())
            {
                if channel.kind == ChannelType::Category {
                    return Ok(id);
                }
            }
        }
        self.check_bot_rights(ctx, guild).await?;
        let everyone = guild.everyone_role();
        let channel = guild
            .create_channel(
                &ctx.http,
                CreateChannel::new("Vivian · Приватные комнаты")
                    .kind(ChannelType::Category)
                    .permissions([PermissionOverwrite {
                        allow: Permissions::empty(),
                        deny: Permissions::VIEW_CHANNEL,
                        kind: PermissionOverwriteType::Role(everyone),
                    }]),
            )
            .await?;
        self.store
            .set_private_room_settings(guild.get(), Some(channel.id.get()), true)
            .await?;
        Ok(channel.id)
    }

    fn room_overwrites(
        guild: serenity::GuildId,
        owner: serenity::UserId,
        bot: serenity::UserId,
    ) -> Vec<PermissionOverwrite> {
        vec![
            PermissionOverwrite {
                allow: Permissions::empty(),
                deny: Permissions::VIEW_CHANNEL | Permissions::CONNECT,
                kind: PermissionOverwriteType::Role(guild.everyone_role()),
            },
            PermissionOverwrite {
                allow: Permissions::VIEW_CHANNEL
                    | Permissions::CONNECT
                    | Permissions::SPEAK
                    | Permissions::MANAGE_CHANNELS
                    | Permissions::MANAGE_ROLES
                    | Permissions::MOVE_MEMBERS,
                deny: Permissions::empty(),
                kind: PermissionOverwriteType::Member(bot),
            },
            PermissionOverwrite {
                allow: Permissions::VIEW_CHANNEL | Permissions::CONNECT | Permissions::SPEAK,
                deny: Permissions::empty(),
                kind: PermissionOverwriteType::Member(owner),
            },
        ]
    }

    pub async fn setup(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        category: Option<serenity::ChannelId>,
        enabled: bool,
    ) -> Result<()> {
        let _guard = self.lock(guild).lock_owned().await;
        self.check_bot_rights(ctx, guild).await?;
        if let Some(category_id) = category {
            let valid = ctx
                .cache
                .guild(guild)
                .and_then(|g| g.channels.get(&category_id).cloned())
                .is_some_and(|ch| ch.kind == ChannelType::Category);
            if !valid {
                bail!("Выбранная категория не найдена на этом сервере.");
            }
        }
        let existing = self
            .store
            .private_room_settings(guild.get())
            .await?
            .and_then(|(id, _)| id);
        self.store
            .set_private_room_settings(
                guild.get(),
                category.map(|id| id.get()).or(existing),
                enabled,
            )
            .await?;
        Ok(())
    }

    pub async fn create(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        owner: &serenity::User,
        name: Option<&str>,
    ) -> Result<serenity::GuildChannel> {
        let _guard = self.lock(guild).lock_owned().await;
        self.check_bot_rights(ctx, guild).await?;
        let display = ctx
            .cache
            .guild(guild)
            .and_then(|cached| {
                cached
                    .members
                    .get(&owner.id)
                    .map(|m| m.display_name().to_owned())
            })
            .unwrap_or_else(|| owner.name.clone());
        let label = Self::sanitized_name(name, &format!("◇ {display}"))?;
        if let Some(room) = self
            .store
            .room_by_owner(guild.get(), owner.id.get())
            .await?
        {
            if room.state == "active" {
                if let Some(channel) = room.channel_id.and_then(|id| {
                    ctx.cache
                        .guild(guild)
                        .and_then(|g| g.channels.get(&serenity::ChannelId::new(id)).cloned())
                }) {
                    return Ok(channel);
                }
            }
            bail!("Твоя комната уже создаётся или удаляется. Подожди или вызови управление.");
        }
        if self.store.active_room_count(guild.get()).await? >= MAX_ROOMS {
            bail!("Сейчас заняты все 20 приватных комнат. Попробуй позже.");
        }
        let reservation = self.store.reserve_room(guild.get(), owner.id.get()).await?;
        let result = async {
            let category = self.ensure_category(ctx, guild).await?;
            let channel = guild
                .create_channel(
                    &ctx.http,
                    CreateChannel::new(label)
                        .kind(ChannelType::Voice)
                        .category(category)
                        .user_limit(0)
                        .permissions(Self::room_overwrites(
                            guild,
                            owner.id,
                            serenity::UserId::new(self.bot_id),
                        )),
                )
                .await?;
            self.store
                .activate_room(reservation.token, channel.id.get())
                .await?;
            Ok::<_, anyhow::Error>(channel)
        }
        .await;
        let channel = match result {
            Ok(channel) => channel,
            Err(error) => {
                let _ = self.store.release_room(reservation.token).await;
                return Err(error);
            }
        };
        if let Some(state) = ctx
            .cache
            .guild(guild)
            .and_then(|g| g.voice_states.get(&owner.id).cloned())
        {
            if state.channel_id.is_some() {
                let _ = guild.move_member(&ctx.http, owner.id, channel.id).await;
            }
        }
        self.schedule_empty_cleanup(ctx.clone(), guild, channel.id, reservation.token);
        Ok(channel)
    }

    async fn owned_room(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        owner: serenity::UserId,
    ) -> Result<(PrivateRoom, serenity::GuildChannel)> {
        let room = self
            .store
            .room_by_owner(guild.get(), owner.get())
            .await?
            .ok_or_else(|| {
                anyhow!("У тебя пока нет своей комнаты. Создай её через `/room_create`.")
            })?;
        if room.state != "active" {
            bail!("Операция с комнатой ещё завершается. Попробуй чуть позже.");
        }
        let id = room
            .channel_id
            .ok_or_else(|| anyhow!("Комната уже удалена. Создай новую."))?;
        let channel = ctx
            .cache
            .guild(guild)
            .and_then(|g| g.channels.get(&serenity::ChannelId::new(id)).cloned())
            .ok_or_else(|| anyhow!("Комната уже удалена. Создай новую."))?;
        if channel.kind != ChannelType::Voice {
            bail!("Сохранённая комната больше не является голосовым каналом.");
        }
        Ok((room, channel))
    }

    pub async fn summary(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        owner: serenity::UserId,
    ) -> Result<Option<String>> {
        let Some(room) = self.store.room_by_owner(guild.get(), owner.get()).await? else {
            return Ok(None);
        };
        let Some(channel_id) = room.channel_id else {
            bail!("Комната сейчас создаётся или удаляется.");
        };
        let channel = ctx
            .cache
            .guild(guild)
            .and_then(|g| {
                g.channels
                    .get(&serenity::ChannelId::new(channel_id))
                    .cloned()
            })
            .ok_or_else(|| anyhow!("Комната уже удалена. Создай новую."))?;
        Ok(Some(format!(
            "{} · <#{}> · лимит {:?} · автозакрытие через минуту пустоты",
            channel.name, channel_id, channel.user_limit
        )))
    }

    async fn set_member_permissions(
        &self,
        ctx: &serenity::Context,
        channel: &serenity::GuildChannel,
        user: serenity::UserId,
        allow: Permissions,
        deny: Permissions,
    ) -> Result<()> {
        channel
            .create_permission(
                &ctx.http,
                PermissionOverwrite {
                    allow,
                    deny,
                    kind: PermissionOverwriteType::Member(user),
                },
            )
            .await?;
        Ok(())
    }

    pub async fn rename(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        owner: serenity::UserId,
        name: &str,
    ) -> Result<()> {
        let _guard = self.lock(guild).lock_owned().await;
        let (_, mut channel) = self.owned_room(ctx, guild, owner).await?;
        let label = Self::sanitized_name(Some(name), "")?;
        channel
            .edit(&ctx.http, EditChannel::new().name(label))
            .await?;
        Ok(())
    }

    pub async fn limit(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        owner: serenity::UserId,
        value: u32,
    ) -> Result<()> {
        if value > 99 {
            bail!("Лимит участников должен быть от 0 до 99.");
        }
        let _guard = self.lock(guild).lock_owned().await;
        let (_, mut channel) = self.owned_room(ctx, guild, owner).await?;
        channel
            .edit(&ctx.http, EditChannel::new().user_limit(value))
            .await?;
        Ok(())
    }

    pub async fn lock_room(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        owner: serenity::UserId,
        locked: bool,
    ) -> Result<()> {
        let _guard = self.lock(guild).lock_owned().await;
        let (_, channel) = self.owned_room(ctx, guild, owner).await?;
        let (allow, deny) = if locked {
            (Permissions::empty(), Permissions::CONNECT)
        } else {
            (Permissions::CONNECT, Permissions::empty())
        };
        channel
            .create_permission(
                &ctx.http,
                PermissionOverwrite {
                    allow,
                    deny,
                    kind: PermissionOverwriteType::Role(guild.everyone_role()),
                },
            )
            .await?;
        Ok(())
    }

    pub async fn visibility(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        owner: serenity::UserId,
        hidden: bool,
    ) -> Result<()> {
        let _guard = self.lock(guild).lock_owned().await;
        let (_, channel) = self.owned_room(ctx, guild, owner).await?;
        let (allow, deny) = if hidden {
            (Permissions::empty(), Permissions::VIEW_CHANNEL)
        } else {
            (Permissions::VIEW_CHANNEL, Permissions::empty())
        };
        channel
            .create_permission(
                &ctx.http,
                PermissionOverwrite {
                    allow,
                    deny,
                    kind: PermissionOverwriteType::Role(guild.everyone_role()),
                },
            )
            .await?;
        Ok(())
    }

    async fn validate_target(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        target: serenity::UserId,
    ) -> Result<serenity::Member> {
        let member = guild.member(&ctx.http, target).await?;
        if member.user.bot {
            bail!("Приглашать и управлять можно только участниками, не ботами.");
        }
        Ok(member)
    }

    pub async fn invite(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        owner: serenity::UserId,
        target: serenity::UserId,
    ) -> Result<()> {
        if owner == target {
            bail!("Ты уже владелец этой комнаты.");
        }
        let _guard = self.lock(guild).lock_owned().await;
        self.validate_target(ctx, guild, target).await?;
        let (_, channel) = self.owned_room(ctx, guild, owner).await?;
        self.set_member_permissions(
            ctx,
            &channel,
            target,
            Permissions::VIEW_CHANNEL | Permissions::CONNECT | Permissions::SPEAK,
            Permissions::empty(),
        )
        .await
    }

    pub async fn kick(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        owner: serenity::UserId,
        target: serenity::UserId,
    ) -> Result<()> {
        let _guard = self.lock(guild).lock_owned().await;
        let target_member = self.validate_target(ctx, guild, target).await?;
        let (room, channel) = self.owned_room(ctx, guild, owner).await?;
        if target == owner {
            bail!("Себя исключить нельзя. Передай владение или удали комнату.");
        }
        let target_is_admin = ctx.cache.guild(guild).is_some_and(|cached| {
            cached
                .user_permissions_in(&channel, &target_member)
                .contains(Permissions::ADMINISTRATOR)
        });
        if target_is_admin {
            bail!("Администратор сервера может обходить ограничения приватной комнаты.");
        }
        self.set_member_permissions(
            ctx,
            &channel,
            target,
            Permissions::empty(),
            Permissions::VIEW_CHANNEL | Permissions::CONNECT,
        )
        .await?;
        let is_here = ctx
            .cache
            .guild(guild)
            .and_then(|g| g.voice_states.get(&target).cloned())
            .is_some_and(|s| s.channel_id == room.channel_id.map(serenity::ChannelId::new));
        if is_here {
            guild.disconnect_member(&ctx.http, target).await?;
        }
        Ok(())
    }

    async fn transfer_locked(
        &self,
        ctx: &serenity::Context,
        room: PrivateRoom,
        channel: &serenity::GuildChannel,
        target: serenity::UserId,
    ) -> Result<()> {
        let target_member = self
            .validate_target(ctx, serenity::GuildId::new(room.guild_id), target)
            .await?;
        if target == serenity::UserId::new(room.owner_id) {
            bail!("Этот участник уже владелец комнаты.");
        }
        if self
            .store
            .room_by_owner(room.guild_id, target.get())
            .await?
            .is_some()
        {
            bail!("У этого участника уже есть своя комната или она создаётся.");
        }
        let connected = ctx
            .cache
            .guild(serenity::GuildId::new(room.guild_id))
            .and_then(|g| g.voice_states.get(&target).cloned())
            .is_some_and(|state| state.channel_id == Some(channel.id));
        if !connected {
            bail!("Новый владелец должен находиться в этой комнате.");
        }
        let previous = PermissionOverwrite {
            allow: Permissions::VIEW_CHANNEL | Permissions::CONNECT | Permissions::SPEAK,
            deny: Permissions::empty(),
            kind: PermissionOverwriteType::Member(target),
        };
        channel.create_permission(&ctx.http, previous).await?;
        self.store.transfer_room(room.token, target.get()).await?;
        let _ = target_member;
        Ok(())
    }

    pub async fn transfer(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        owner: serenity::UserId,
        target: serenity::UserId,
    ) -> Result<()> {
        let _guard = self.lock(guild).lock_owned().await;
        let (room, channel) = self.owned_room(ctx, guild, owner).await?;
        self.transfer_locked(ctx, room, &channel, target).await
    }

    pub async fn claim(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        claimant: serenity::UserId,
    ) -> Result<()> {
        let channel_id = ctx
            .cache
            .guild(guild)
            .and_then(|g| g.voice_states.get(&claimant).cloned())
            .and_then(|state| state.channel_id)
            .ok_or_else(|| anyhow!("Зайди в комнату, которую хочешь забрать."))?;
        let _guard = self.lock(guild).lock_owned().await;
        let room = self
            .store
            .room_by_channel(guild.get(), channel_id.get())
            .await?
            .ok_or_else(|| {
                anyhow!("Эта комната не принадлежит системе приватных комнат Vivian.")
            })?;
        let owner_present = ctx.cache.guild(guild).is_some_and(|cached| {
            cached.voice_states.iter().any(|(user, state)| {
                user.get() == room.owner_id && state.channel_id == Some(channel_id)
            })
        });
        if owner_present {
            bail!("Владелец ещё находится в комнате. Попроси его передать владение.");
        }
        let channel = ctx
            .cache
            .guild(guild)
            .and_then(|g| g.channels.get(&channel_id).cloned())
            .ok_or_else(|| anyhow!("Голосовой канал не найден."))?;
        self.transfer_locked(ctx, room, &channel, claimant).await
    }

    pub async fn delete(
        &self,
        ctx: &serenity::Context,
        guild: serenity::GuildId,
        owner: serenity::UserId,
    ) -> Result<()> {
        let _guard = self.lock(guild).lock_owned().await;
        let (room, channel) = self.owned_room(ctx, guild, owner).await?;
        self.store.mark_room_deleting(room.token).await?;
        if let Err(error) = channel.delete(&ctx.http).await {
            let _ = self.store.activate_room(room.token, channel.id.get()).await;
            return Err(error.into());
        }
        self.store.release_room(room.token).await?;
        Ok(())
    }

    pub async fn on_voice_state_update(
        &self,
        ctx: serenity::Context,
        guild: serenity::GuildId,
        before: Option<serenity::ChannelId>,
        after: Option<serenity::ChannelId>,
    ) {
        let Some(channel_id) = before.filter(|old| Some(*old) != after) else {
            return;
        };
        let Ok(Some(room)) = self
            .store
            .room_by_channel(guild.get(), channel_id.get())
            .await
        else {
            return;
        };
        self.schedule_empty_cleanup(ctx, guild, channel_id, room.token);
    }

    pub async fn reconcile(&self, ctx: &serenity::Context) -> Result<()> {
        for room in self.store.recoverable_rooms().await? {
            let Some(channel_id) = room.channel_id else {
                self.store.release_room(room.token).await?;
                continue;
            };
            let guild = serenity::GuildId::new(room.guild_id);
            let channel_id = serenity::ChannelId::new(channel_id);
            let channel = match channel_id.to_channel(&ctx.http).await {
                Ok(serenity::Channel::Guild(channel)) if channel.kind == ChannelType::Voice => {
                    channel
                }
                _ => {
                    self.store.release_room(room.token).await?;
                    continue;
                }
            };
            if room.state != "active" {
                self.store
                    .activate_room(room.token, channel.id.get())
                    .await?;
            }
            let Some(cached_guild) = ctx.cache.guild(guild) else {
                continue;
            };
            let empty = !cached_guild
                .voice_states
                .values()
                .any(|state| state.channel_id == Some(channel.id));
            drop(cached_guild);
            if empty {
                self.schedule_empty_cleanup(ctx.clone(), guild, channel.id, room.token);
            }
        }
        Ok(())
    }

    fn schedule_empty_cleanup(
        &self,
        ctx: serenity::Context,
        guild: serenity::GuildId,
        channel: serenity::ChannelId,
        token: i64,
    ) {
        let manager = self.clone();
        tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_secs(EMPTY_GRACE_SECS)).await;
            let _guard = manager.lock(guild).lock_owned().await;
            let still_empty = ctx.cache.guild(guild).is_some_and(|cached| {
                !cached
                    .voice_states
                    .values()
                    .any(|state| state.channel_id == Some(channel))
            });
            if !still_empty {
                return;
            }
            let Ok(Some(room)) = manager
                .store
                .room_by_channel(guild.get(), channel.get())
                .await
            else {
                return;
            };
            if room.token != token {
                return;
            }
            let channel_model = ctx
                .cache
                .guild(guild)
                .and_then(|cached| cached.channels.get(&channel).cloned());
            if manager.store.mark_room_deleting(token).await.is_err() {
                return;
            }
            if let Some(channel_model) = channel_model {
                if let Err(error) = channel_model.delete(&ctx.http).await {
                    tracing::warn!(guild_id = guild.get(), channel_id = channel.get(), error = %error, "empty private room deletion failed; retaining room record");
                    let _ = manager.store.activate_room(token, channel.get()).await;
                    return;
                }
            }
            if let Err(error) = manager.store.release_room(token).await {
                tracing::error!(guild_id = guild.get(), channel_id = channel.get(), error = %error, "private room deleted but database release failed");
            }
        });
    }
}
