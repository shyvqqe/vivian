use super::Context;
use crate::music::{
    audio::{format_duration, user_voice_channel, LoopMode},
    catalog::MusicRequest,
};
use anyhow::{anyhow, bail, Context as _, Result};
use futures_util::StreamExt;
use lavalink_rs::{
    model::player::{Distortion, Equalizer, Filters, Karaoke, Rotation, Timescale, TremoloVibrato},
    player_context::{PlayerContext, TrackInQueue},
};
use poise::serenity_prelude as serenity;
use rand::seq::SliceRandom;
use serde_json::json;
use std::{collections::VecDeque, time::Duration};

fn guild_id(ctx: Context<'_>) -> Result<serenity::GuildId> {
    ctx.guild_id()
        .ok_or_else(|| anyhow!("Музыкальные команды работают только на сервере."))
}

fn voice_channel(ctx: Context<'_>, guild: serenity::GuildId) -> Result<serenity::ChannelId> {
    user_voice_channel(ctx.serenity_context(), guild, ctx.author().id)
        .ok_or_else(|| anyhow!("Сначала зайди в голосовой канал."))
}

async fn player(ctx: Context<'_>, guild: serenity::GuildId) -> Result<PlayerContext> {
    ctx.data()
        .audio
        .client()
        .get_player_context(guild)
        .ok_or_else(|| anyhow!("Плеер не подключён. Используй `/join` или `/play` сначала."))
}

async fn prepared_tracks(
    ctx: Context<'_>,
    query: &str,
) -> Result<(MusicRequest, Vec<lavalink_rs::model::track::TrackData>)> {
    let guild = guild_id(ctx)?;
    let request = ctx.data().audio.prepare(query).await?;
    let tracks = ctx.data().audio.load(guild, &request).await?;
    Ok((request, tracks))
}

async fn prepared_search_tracks(
    ctx: Context<'_>,
    query: &str,
) -> Result<(MusicRequest, Vec<lavalink_rs::model::track::TrackData>)> {
    let guild = guild_id(ctx)?;
    let request = ctx.data().audio.prepare(query).await?;
    let tracks = ctx.data().audio.search(guild, &request).await?;
    Ok((request, tracks))
}

async fn play_tracks(ctx: Context<'_>, query: &str, first_only: bool) -> Result<()> {
    let _typing = ctx.defer_or_broadcast().await?;
    let guild = guild_id(ctx)?;
    let (request, tracks) = prepared_tracks(ctx, query).await?;
    if tracks.is_empty() {
        bail!(
            "Ничего не найдено для `{}`.",
            query.chars().take(100).collect::<String>()
        );
    }
    let channel = voice_channel(ctx, guild)?;
    let player = ctx
        .data()
        .audio
        .join(ctx.serenity_context(), guild, channel)
        .await?;
    let mut tracks = tracks.into_iter();
    let first = tracks
        .next()
        .ok_or_else(|| anyhow!("Пустой результат поиска."))?;
    let current = player.get_player().await?.track.is_some();
    let prefix_replaces = matches!(ctx, poise::Context::Prefix(_));
    if prefix_replaces && current {
        player.get_queue().clear()?;
        player.play_now(&first).await?;
    } else if current {
        player.queue(first.clone())?;
    } else {
        player.play_now(&first).await?;
    }
    let mut count = 1usize;
    if !first_only && !prefix_replaces {
        for track in tracks.take(49) {
            player.queue(track)?;
            count += 1;
        }
    }
    let action = if current && !prefix_replaces {
        "Добавлено в очередь"
    } else {
        "Сейчас играет"
    };
    let provider = request
        .requested_from
        .as_deref()
        .map(|p| format!(" · найдено по метаданным {p}"))
        .unwrap_or_default();
    crate::ui::say(
        ctx,
        format!(
            "**{action}:** [{} — {}]({}) · `{}` · треков: {count}{provider}",
            first.info.author,
            first.info.title,
            first.info.uri.as_deref().unwrap_or("https://lavalink.dev"),
            format_duration(first.info.length)
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, aliases("p"))]
pub async fn play(
    ctx: Context<'_>,
    #[description = "Название, ссылка или префикс spotify/apple/soundcloud"] query: String,
) -> Result<()> {
    play_tracks(ctx, &query, false).await
}

#[poise::command(slash_command, prefix_command)]
pub async fn search(
    ctx: Context<'_>,
    #[description = "Название, ссылка или сервисный запрос"] query: String,
) -> Result<()> {
    let _typing = ctx.defer_or_broadcast().await?;
    let (_, tracks) = prepared_search_tracks(ctx, &query).await?;
    if tracks.is_empty() {
        crate::ui::say(ctx, "Ничего не найдено.").await?;
        return Ok(());
    }
    let tracks = tracks.into_iter().take(10).collect::<Vec<_>>();
    let lines = tracks
        .iter()
        .enumerate()
        .map(|(i, track)| {
            format!(
                "{}. [{} — {}]({}) · `{}`",
                i + 1,
                track.info.author,
                track.info.title,
                track.info.uri.as_deref().unwrap_or("https://lavalink.dev"),
                format_duration(track.info.length)
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    let id = format!("vivian_search_{}", ctx.id());
    let buttons = tracks
        .iter()
        .enumerate()
        .take(5)
        .map(|(index, _)| {
            json!({
                "type":2,
                "style":1,
                "label":format!("▶ {}", index + 1),
                "custom_id":format!("{id}_{index}")
            })
        })
        .collect::<Vec<_>>();
    let controls = if buttons.is_empty() {
        Vec::new()
    } else {
        vec![json!({"type":1,"components":buttons})]
    };
    let message_id = crate::ui::with_components(
        ctx,
        &format!("**Результаты поиска**\n{lines}\n\nНажми кнопку, чтобы поставить трек в очередь."),
        true,
        controls,
    )
    .await?;
    let mut collector = serenity::ComponentInteractionCollector::new(ctx.serenity_context())
        .author_id(ctx.author().id)
        .channel_id(ctx.channel_id())
        .message_id(message_id)
        .timeout(Duration::from_secs(120))
        .stream();
    while let Some(interaction) = collector.next().await {
        let Some(index) = interaction
            .data
            .custom_id
            .strip_prefix(&id)
            .and_then(|s| s.strip_prefix('_'))
            .and_then(|s| s.parse::<usize>().ok())
        else {
            interaction
                .create_response(
                    ctx.serenity_context(),
                    serenity::CreateInteractionResponse::Acknowledge,
                )
                .await?;
            continue;
        };
        let Some(track) = tracks.get(index) else {
            interaction
                .create_response(
                    ctx.serenity_context(),
                    serenity::CreateInteractionResponse::Acknowledge,
                )
                .await?;
            continue;
        };
        let guild = guild_id(ctx)?;
        let channel = voice_channel(ctx, guild)?;
        let player = ctx
            .data()
            .audio
            .join(ctx.serenity_context(), guild, channel)
            .await?;
        if player.get_player().await?.track.is_some() {
            player.queue(track.clone())?;
        } else {
            player.play_now(track).await?;
        }
        crate::ui::component_reply(
            &interaction,
            &format!(
                "Добавлено: **{} — {}**",
                track.info.author, track.info.title
            ),
            true,
            ctx.serenity_context(),
        )
        .await?;
        break;
    }
    Ok(())
}

fn queue_page(
    current: Option<&lavalink_rs::model::track::TrackData>,
    queue: &[TrackInQueue],
    page: usize,
    per_page: usize,
) -> String {
    let pages = queue.len().div_ceil(per_page).max(1);
    let page = page.min(pages - 1);
    let mut lines = Vec::new();
    if let Some(track) = current {
        lines.push(format!(
            "**Сейчас:** {} — {} · `{}`",
            track.info.author,
            track.info.title,
            format_duration(track.info.length)
        ));
    }
    let start = page * per_page;
    lines.extend(
        queue
            .iter()
            .skip(start)
            .take(per_page)
            .enumerate()
            .map(|(i, item)| {
                format!(
                    "{}. {} — {} · `{}`",
                    start + i + 1,
                    item.track.info.author,
                    item.track.info.title,
                    format_duration(item.track.info.length)
                )
            }),
    );
    if queue.is_empty() {
        lines.push("Очередь пуста.".into());
    }
    format!(
        "**Очередь** · {} треков · страница {}/{}\n{}",
        queue.len(),
        page + 1,
        pages,
        lines.join("\n")
    )
}

#[poise::command(slash_command, prefix_command, aliases("q"))]
pub async fn queue(ctx: Context<'_>) -> Result<()> {
    let guild = guild_id(ctx)?;
    let current = player(ctx, guild).await?;
    let snapshot = current.get_player().await?;
    let queue = current
        .get_queue()
        .get_queue()
        .await?
        .into_iter()
        .collect::<Vec<_>>();
    let id = format!("vivian_queue_{}", ctx.id());
    let pages = queue.len().div_ceil(10).max(1);
    let controls = if pages > 1 {
        vec![json!({"type":1,"components":[
            {"type":2,"style":2,"label":"← Назад","custom_id":format!("{id}_prev")},
            {"type":2,"style":1,"label":"Дальше →","custom_id":format!("{id}_next")}
        ]})]
    } else {
        Vec::new()
    };
    let message_id = crate::ui::with_components(
        ctx,
        &queue_page(snapshot.track.as_ref(), &queue, 0, 10),
        true,
        controls,
    )
    .await?;
    if pages <= 1 {
        return Ok(());
    }
    let mut page = 0usize;
    let mut collector = serenity::ComponentInteractionCollector::new(ctx.serenity_context())
        .author_id(ctx.author().id)
        .channel_id(ctx.channel_id())
        .message_id(message_id)
        .timeout(Duration::from_secs(300))
        .stream();
    while let Some(interaction) = collector.next().await {
        if interaction.data.custom_id == format!("{id}_next") {
            page = (page + 1).min(pages - 1);
        } else if interaction.data.custom_id == format!("{id}_prev") {
            page = page.saturating_sub(1);
        }
        let controls = vec![json!({"type":1,"components":[
            {"type":2,"style":2,"label":"← Назад","custom_id":format!("{id}_prev")},
            {"type":2,"style":1,"label":"Дальше →","custom_id":format!("{id}_next")}
        ]})];
        crate::ui::component_update(
            &interaction,
            &queue_page(snapshot.track.as_ref(), &queue, page, 10),
            &controls,
            ctx.serenity_context(),
        )
        .await?;
    }
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn nowplaying(ctx: Context<'_>) -> Result<()> {
    let current = player(ctx, guild_id(ctx)?).await?;
    let snapshot = current.get_player().await?;
    let Some(track) = snapshot.track else {
        bail!("Сейчас ничего не играет.");
    };
    let position = snapshot.state.position;
    let duration = track.info.length;
    let bar_width = 18usize;
    let filled = if duration == 0 {
        0
    } else {
        ((position.min(duration) as f64 / duration as f64) * bar_width as f64).round() as usize
    };
    crate::ui::say(
        ctx,
        format!(
            "**Сейчас играет:** [{} — {}]({})\n`{}` ▰{}▱{} `{}`{}",
            track.info.author,
            track.info.title,
            track.info.uri.as_deref().unwrap_or("https://lavalink.dev"),
            format_duration(position),
            "▰".repeat(filled),
            "▱".repeat(bar_width - filled),
            format_duration(duration),
            if snapshot.paused {
                " · пауза"
            } else {
                ""
            }
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, aliases("next"))]
pub async fn skip(ctx: Context<'_>) -> Result<()> {
    player(ctx, guild_id(ctx)?).await?.skip()?;
    crate::ui::say(ctx, "Пропускаю текущий трек.").await?;
    Ok(())
}
#[poise::command(slash_command, prefix_command)]
pub async fn pause(ctx: Context<'_>) -> Result<()> {
    player(ctx, guild_id(ctx)?).await?.set_pause(true).await?;
    crate::ui::say(ctx, "Музыка на паузе.").await?;
    Ok(())
}
#[poise::command(slash_command, prefix_command, aliases("unpause"))]
pub async fn resume(ctx: Context<'_>) -> Result<()> {
    player(ctx, guild_id(ctx)?).await?.set_pause(false).await?;
    crate::ui::say(ctx, "Продолжаю воспроизведение.").await?;
    Ok(())
}

async fn stop_voice(ctx: Context<'_>, guild: serenity::GuildId) -> Result<()> {
    ctx.data().audio.disconnect_guild(guild).await;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn stop(ctx: Context<'_>) -> Result<()> {
    let guild = guild_id(ctx)?;
    stop_voice(ctx, guild).await?;
    crate::ui::say(
        ctx,
        "Остановила музыку, очистила очередь и вышла из канала.",
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn volume(
    ctx: Context<'_>,
    #[description = "Громкость 0–150 процентов; 0 отключает звук"] value: u16,
) -> Result<()> {
    if value > 150 {
        bail!("Громкость должна быть от 0 до 150 процентов. Значение 0 полностью отключает звук.");
    }
    player(ctx, guild_id(ctx)?)
        .await?
        .set_volume(value as u16)
        .await?;
    crate::ui::say(ctx, format!("Громкость установлена на **{value}%**.")).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn join(ctx: Context<'_>) -> Result<()> {
    let guild = guild_id(ctx)?;
    let channel = voice_channel(ctx, guild)?;
    ctx.data()
        .audio
        .join(ctx.serenity_context(), guild, channel)
        .await?;
    crate::ui::say(ctx, "Подключилась к голосовому каналу.").await?;
    Ok(())
}
#[poise::command(slash_command, prefix_command)]
pub async fn leave(ctx: Context<'_>) -> Result<()> {
    let guild = guild_id(ctx)?;
    stop_voice(ctx, guild).await?;
    crate::ui::say(ctx, "Вышла из голосового канала.").await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, rename = "loop")]
pub async fn loop_mode(
    ctx: Context<'_>,
    #[description = "off, track или queue"] mode: String,
) -> Result<()> {
    let mode = match mode.to_lowercase().as_str() {
        "off" | "выкл" | "нет" => LoopMode::Off,
        "track" | "трек" | "песня" => LoopMode::Track,
        "queue" | "очередь" | "all" => LoopMode::Queue,
        _ => bail!("Укажи режим: `off`, `track` или `queue`."),
    };
    let guild = guild_id(ctx)?;
    ctx.data().audio.set_loop_mode(guild, mode);
    let label = match mode {
        LoopMode::Off => "выключен",
        LoopMode::Track => "текущий трек",
        LoopMode::Queue => "очередь",
    };
    crate::ui::say(ctx, format!("Повтор: **{label}**.")).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn shuffle(ctx: Context<'_>) -> Result<()> {
    let player = player(ctx, guild_id(ctx)?).await?;
    let mut queue = player
        .get_queue()
        .get_queue()
        .await?
        .into_iter()
        .collect::<Vec<_>>();
    if queue.len() < 2 {
        bail!("Для перемешивания нужны хотя бы два трека в очереди.");
    }
    queue.shuffle(&mut rand::rng());
    player.get_queue().replace(VecDeque::from(queue))?;
    crate::ui::say(ctx, "Очередь перемешана.").await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn seek(
    ctx: Context<'_>,
    #[description = "Позиция в секундах или формате mm:ss"] position: String,
) -> Result<()> {
    let seconds = parse_position(&position)?;
    let player = player(ctx, guild_id(ctx)?).await?;
    let snapshot = player.get_player().await?;
    let track = snapshot
        .track
        .ok_or_else(|| anyhow!("Сейчас ничего не играет."))?;
    if track.info.length > 0 && seconds.saturating_mul(1000) >= track.info.length {
        bail!(
            "Позиция должна быть меньше длины трека ({}).",
            format_duration(track.info.length)
        );
    }
    player.set_position(Duration::from_secs(seconds)).await?;
    crate::ui::say(
        ctx,
        format!("Перемотано на **{}**.", format_duration(seconds * 1000)),
    )
    .await?;
    Ok(())
}

fn parse_position(value: &str) -> Result<u64> {
    let parts = value.trim().split(':').collect::<Vec<_>>();
    let seconds = match parts.as_slice() {
        [secs] => secs
            .parse::<u64>()
            .map_err(|_| anyhow!("Укажи позицию в секундах или `mm:ss`."))?,
        [mins, secs] => {
            let mins = mins.parse::<u64>()?;
            let secs = secs.parse::<u64>()?;
            if secs >= 60 {
                bail!("Секунды должны быть меньше 60.");
            }
            mins.checked_mul(60)
                .and_then(|v| v.checked_add(secs))
                .ok_or_else(|| anyhow!("Некорректная позиция."))?
        }
        [hours, mins, secs] => {
            let hours = hours.parse::<u64>()?;
            let mins = mins.parse::<u64>()?;
            let secs = secs.parse::<u64>()?;
            if mins >= 60 || secs >= 60 {
                bail!("Минуты и секунды должны быть меньше 60.");
            }
            hours
                .checked_mul(3600)
                .and_then(|v| v.checked_add(mins.checked_mul(60)?))
                .and_then(|v| v.checked_add(secs))
                .ok_or_else(|| anyhow!("Некорректная позиция."))?
        }
        _ => bail!("Укажи позицию в секундах, `mm:ss` или `hh:mm:ss`."),
    };
    if parts
        .iter()
        .skip(1)
        .any(|part| part.len() != 2 || !part.bytes().all(|b| b.is_ascii_digit()))
    {
        bail!("Минуты и секунды после двоеточия должны состоять из двух цифр.");
    }
    Ok(seconds)
}

#[poise::command(slash_command, prefix_command)]
pub async fn remove(
    ctx: Context<'_>,
    #[description = "Номер трека в очереди, начиная с 1"] index: u32,
) -> Result<()> {
    if index == 0 {
        bail!("Номер трека начинается с 1.");
    }
    let player = player(ctx, guild_id(ctx)?).await?;
    let queue = player.get_queue().get_queue().await?;
    let Some(track) = queue.get((index - 1) as usize) else {
        bail!("В очереди нет трека номер {index}.");
    };
    let title = format!("{} — {}", track.track.info.author, track.track.info.title);
    player.get_queue().remove((index - 1) as usize)?;
    crate::ui::say(ctx, format!("Удалён трек **{title}** из очереди.")).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, rename = "clear_queue")]
pub async fn clear(ctx: Context<'_>) -> Result<()> {
    let player = player(ctx, guild_id(ctx)?).await?;
    let count = player.get_queue().get_count().await?;
    player.get_queue().clear()?;
    crate::ui::say(
        ctx,
        format!("Очередь очищена ({count} треков). Текущий трек продолжает играть."),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn filter(
    ctx: Context<'_>,
    #[description = "off, nightcore, bassboost, 8d, vaporwave, karaoke, tremolo, distortion"]
    effect: String,
) -> Result<()> {
    let key = effect.to_lowercase();
    let filters = match key.as_str() {
        "off" | "clear" | "выкл" => Filters::default(),
        "nightcore" | "ночной" => Filters { timescale: Some(Timescale { speed: Some(1.15), pitch: Some(1.15), rate: Some(1.0) }), ..Default::default() },
        "vaporwave" => Filters { timescale: Some(Timescale { speed: Some(0.85), pitch: Some(0.8), rate: Some(1.0) }), ..Default::default() },
        "8d" | "8д" => Filters { rotation: Some(Rotation { rotation_hz: Some(0.2) }), ..Default::default() },
        "bassboost" | "bass" => Filters { equalizer: Some(vec![Equalizer { band: 0, gain: 0.18 }, Equalizer { band: 1, gain: 0.15 }, Equalizer { band: 2, gain: 0.1 }, Equalizer { band: 3, gain: 0.05 }]), ..Default::default() },
        "karaoke" => Filters { karaoke: Some(Karaoke { level: Some(1.0), mono_level: Some(1.0), filter_band: Some(220.0), filter_width: Some(100.0) }), ..Default::default() },
        "tremolo" => Filters { tremolo: Some(TremoloVibrato { frequency: Some(4.0), depth: Some(0.5) }), ..Default::default() },
        "distortion" => Filters { distortion: Some(Distortion { sin_scale: Some(1.0), cos_scale: Some(1.0), tan_scale: Some(1.0), offset: Some(0.0), scale: Some(1.0), ..Default::default() }), ..Default::default() },
        _ => bail!("Неизвестный эффект. Доступны: off, nightcore, bassboost, 8d, vaporwave, karaoke, tremolo, distortion."),
    };
    let player = player(ctx, guild_id(ctx)?).await?;
    ctx.data().audio.apply_filters(&player, filters).await?;
    crate::ui::say(
        ctx,
        if key == "off" {
            "Эффекты выключены.".to_owned()
        } else {
            format!("Эффект **{effect}** включён.")
        },
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn playlist_save(
    ctx: Context<'_>,
    #[description = "Название личного плейлиста"] name: String,
    #[description = "Запрос или музыкальная ссылка; сохраняются до 10 результатов"] query: String,
) -> Result<()> {
    let _typing = ctx.defer_or_broadcast().await?;
    let guild = guild_id(ctx)?;
    let name = name.trim();
    if name.is_empty() || name.chars().count() > 80 {
        bail!("Название должно содержать от 1 до 80 символов.");
    }
    let (_, tracks) = prepared_search_tracks(ctx, &query).await?;
    let tracks = tracks.into_iter().take(10).collect::<Vec<_>>();
    if tracks.is_empty() {
        bail!("Ничего не найдено для сохранения.");
    }
    let encoded = serde_json::to_string(&tracks)?;
    ctx.data()
        .store
        .save_playlist(guild.get(), ctx.author().id.get(), name, &encoded)
        .await?;
    crate::ui::say(
        ctx,
        format!(
            "Личный плейлист **{}** сохранён · {} трек(ов).",
            name,
            tracks.len()
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn playlists(ctx: Context<'_>) -> Result<()> {
    let guild = guild_id(ctx)?;
    let lists = ctx
        .data()
        .store
        .playlists(guild.get(), ctx.author().id.get())
        .await?;
    if lists.is_empty() {
        crate::ui::say(
            ctx,
            "Сохранённых плейлистов пока нет. Создай первый через `/playlist_save`.",
        )
        .await?;
        return Ok(());
    }
    let lines = lists
        .iter()
        .enumerate()
        .map(|(i, (name, json))| {
            let count = serde_json::from_str::<Vec<lavalink_rs::model::track::TrackData>>(json)
                .map(|tracks| tracks.len())
                .unwrap_or(0);
            format!("{}. **{}** · {} трек(ов)", i + 1, name, count)
        })
        .collect::<Vec<_>>()
        .join("\n");
    crate::ui::private(
        ctx,
        format!("**Твои личные плейлисты**\n{lines}\n\nЗапусти: `/playlist_play` · Удали: `/playlist_delete`."),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn playlist_play(
    ctx: Context<'_>,
    #[description = "Название личного плейлиста"] name: String,
) -> Result<()> {
    let guild = guild_id(ctx)?;
    let lists = ctx
        .data()
        .store
        .playlists(guild.get(), ctx.author().id.get())
        .await?;
    let (_, json) = lists
        .into_iter()
        .find(|(saved, _)| saved.eq_ignore_ascii_case(name.trim()))
        .ok_or_else(|| anyhow!("Личный плейлист **{}** не найден.", name))?;
    let tracks: Vec<lavalink_rs::model::track::TrackData> =
        serde_json::from_str(&json).context("Плейлист содержит повреждённые данные.")?;
    if tracks.is_empty() {
        bail!("В этом плейлисте нет треков.");
    }
    let channel = voice_channel(ctx, guild)?;
    let player = ctx
        .data()
        .audio
        .join(ctx.serenity_context(), guild, channel)
        .await?;
    let active = player.get_player().await?.track.is_some();
    let mut queued = 0usize;
    for track in tracks.into_iter().take(50) {
        if !active && queued == 0 {
            player.play_now(&track).await?;
        } else {
            player.queue(track)?;
        }
        queued += 1;
    }
    crate::ui::say(
        ctx,
        format!("Плейлист **{}** добавлен · {} трек(ов).", name, queued),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command)]
pub async fn playlist_delete(
    ctx: Context<'_>,
    #[description = "Название личного плейлиста"] name: String,
) -> Result<()> {
    let guild = guild_id(ctx)?;
    if !ctx
        .data()
        .store
        .delete_playlist(guild.get(), ctx.author().id.get(), name.trim())
        .await?
    {
        bail!("Личный плейлист **{}** не найден.", name);
    }
    crate::ui::say(ctx, format!("Плейлист **{}** удалён.", name)).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn seek_positions_parse_all_supported_formats() {
        assert_eq!(parse_position("92").unwrap(), 92);
        assert_eq!(parse_position("01:32").unwrap(), 92);
        assert_eq!(parse_position("1:01:32").unwrap(), 3692);
        assert!(parse_position("1:2").is_err());
        assert!(parse_position("1:72").is_err());
        assert!(parse_position("1:60:00").is_err());
    }
}
