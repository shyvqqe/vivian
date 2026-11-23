use anyhow::{anyhow, bail, Context as _, Result};
use futures_util::StreamExt;
use regex::Regex;
use reqwest::{redirect::Policy, Client, Url};
use serde_json::Value;
use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::Mutex;

const MAX_RESPONSE_BYTES: usize = 512 * 1024;
const CACHE_TTL: Duration = Duration::from_secs(90);
const CACHE_CAPACITY: usize = 256;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SearchSource {
    YouTube,
    SoundCloud,
}

impl SearchSource {
    pub fn query(self, term: &str) -> String {
        let prefix = match self {
            Self::YouTube => "ytsearch",
            Self::SoundCloud => "scsearch",
        };
        format!("{prefix}:{term}")
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MusicRequest {
    pub term: String,
    pub source: SearchSource,
    pub requested_from: Option<String>,
    pub direct_url: Option<String>,
}

#[derive(Clone)]
pub struct MusicCatalog {
    client: Client,
    cache: Arc<Mutex<HashMap<String, (Instant, MusicRequest)>>>,
}

impl MusicCatalog {
    pub fn new() -> Result<Self> {
        let client = Client::builder()
            .timeout(Duration::from_secs(6))
            .user_agent("Mozilla/5.0 (compatible; VivianMusicBot/1.0)")
            .redirect(Policy::custom(|attempt| {
                if attempt.previous().len() >= 5 || !is_allowed_url(attempt.url()) {
                    attempt.stop()
                } else {
                    attempt.follow()
                }
            }))
            .build()
            .context("music metadata HTTP client setup failed")?;
        Ok(Self {
            client,
            cache: Arc::new(Mutex::new(HashMap::new())),
        })
    }

    pub async fn prepare(&self, raw: &str) -> Result<MusicRequest> {
        let value = raw.trim();
        if value.is_empty() || value.chars().count() > 400 {
            bail!("Укажи название песни или HTTPS-ссылку (до 400 символов).");
        }
        let cache_key = value.to_lowercase();
        if let Some((created, request)) = self.cache.lock().await.get(&cache_key).cloned() {
            if created.elapsed() < CACHE_TTL {
                return Ok(request);
            }
        }

        let (tagged_source, query) = split_source_prefix(value)?;
        if query.is_empty() {
            bail!("После названия сервиса укажи песню или ссылку.");
        }
        let mut resolved = if query.starts_with("https://") || query.starts_with("http://") {
            self.resolve_url(query, tagged_source).await?
        } else {
            if query.contains("://") || query.starts_with("//") {
                bail!("Некорректная музыкальная ссылка.");
            }
            if query.chars().count() > 200 {
                bail!("Название песни не должно быть длиннее 200 символов.");
            }
            match tagged_source {
                Some(SourceTag::SoundCloud) => MusicRequest {
                    term: query.to_owned(),
                    source: SearchSource::SoundCloud,
                    requested_from: None,
                    direct_url: None,
                },
                Some(SourceTag::Apple) => {
                    let term = self.apple_search(query).await.unwrap_or_else(|error| {
                        tracing::warn!(error = %error, "Apple public metadata search failed; using the supplied search text");
                        query.to_owned()
                    });
                    MusicRequest {
                        term,
                        source: SearchSource::YouTube,
                        requested_from: Some("Apple Music".into()),
                        direct_url: None,
                    }
                }
                source => MusicRequest {
                    term: query.to_owned(),
                    source: SearchSource::YouTube,
                    requested_from: source.and_then(SourceTag::catalog_name).map(str::to_owned),
                    direct_url: None,
                },
            }
        };
        if let (Some(tagged), Some(url)) = (tagged_source, resolved.direct_url.as_deref()) {
            let detected = detect_source(url)?;
            if tagged != detected {
                bail!("Указанный сервис не совпадает с сервисом ссылки.");
            }
        }
        resolved.term = clean_text(&resolved.term);
        if resolved.term.is_empty() && resolved.direct_url.is_none() {
            bail!("Не удалось определить название трека.");
        }
        self.cache
            .lock()
            .await
            .insert(cache_key, (Instant::now(), resolved.clone()));
        self.trim_cache().await;
        Ok(resolved)
    }

    async fn trim_cache(&self) {
        let mut cache = self.cache.lock().await;
        cache.retain(|_, (created, _)| created.elapsed() < CACHE_TTL);
        if cache.len() > CACHE_CAPACITY {
            if let Some(oldest_key) = cache
                .iter()
                .min_by_key(|(_, (created, _))| *created)
                .map(|(key, _)| key.clone())
            {
                cache.remove(&oldest_key);
            }
        }
    }

    async fn resolve_url(&self, raw: &str, tagged: Option<SourceTag>) -> Result<MusicRequest> {
        let mut url = parse_allowed_url(raw)?;
        if let Some(tagged) = tagged {
            if tagged != detect_source(url.as_str())? {
                bail!("Указанный сервис не совпадает с сервисом ссылки.");
            }
        }
        let source = detect_source(url.as_str())?;
        match source {
            SourceTag::YouTube => Ok(MusicRequest {
                term: raw.to_owned(),
                source: SearchSource::YouTube,
                requested_from: None,
                direct_url: Some(raw.to_owned()),
            }),
            SourceTag::SoundCloud => Ok(MusicRequest {
                term: raw.to_owned(),
                source: SearchSource::SoundCloud,
                requested_from: None,
                direct_url: Some(raw.to_owned()),
            }),
            SourceTag::Spotify => {
                if url.host_str() == Some("spotify.link") {
                    let response = self
                        .client
                        .get(url.clone())
                        .send()
                        .await
                        .context("не удалось раскрыть короткую ссылку Spotify")?;
                    url = response.url().clone();
                    if url.host_str() != Some("open.spotify.com") {
                        bail!("Короткая ссылка ведёт не на Spotify.");
                    }
                }
                let track_id = spotify_track_id(&url)?;
                let canonical = format!("https://open.spotify.com/track/{track_id}");
                let mut endpoint = Url::parse("https://open.spotify.com/oembed")?;
                endpoint.query_pairs_mut().append_pair("url", &canonical);
                let payload = self.fetch_json(endpoint).await.context(
                    "Spotify не отдал публичные метаданные; укажи исполнителя и песню текстом",
                )?;
                let title = clean_text(
                    payload
                        .get("title")
                        .and_then(Value::as_str)
                        .unwrap_or_default(),
                );
                let artist = clean_text(
                    payload
                        .get("author_name")
                        .and_then(Value::as_str)
                        .unwrap_or_default(),
                );
                if title.is_empty() {
                    bail!("Spotify не вернул название трека.");
                }
                let title = title
                    .strip_prefix(&format!("{artist} - "))
                    .unwrap_or(&title)
                    .to_owned();
                Ok(bridged_request("Spotify", &artist, &title))
            }
            SourceTag::Apple => {
                let track_id = apple_track_id(&url)?;
                let mut endpoint = Url::parse("https://itunes.apple.com/lookup")?;
                endpoint
                    .query_pairs_mut()
                    .append_pair("id", &track_id)
                    .append_pair("entity", "song")
                    .append_pair("country", &apple_music_country());
                let payload = self.fetch_json(endpoint).await.context(
                    "Apple Music не отдал публичные метаданные; укажи исполнителя и песню текстом",
                )?;
                let (artist, title) = apple_song(&payload)?;
                Ok(bridged_request("Apple Music", &artist, &title))
            }
            SourceTag::Yandex | SourceTag::Vk => {
                let payload = self.fetch_text(url).await?;
                let title = open_graph_title(&payload)
                    .ok_or_else(|| anyhow!("Сервис не отдал публичные данные трека. Напиши исполнителя и название песни."))?;
                let title = clean_catalog_title(&title);
                if title.is_empty() || is_service_name(&title) {
                    bail!("Не удалось прочитать публичные данные трека. Пришли название и исполнителя текстом.");
                }
                Ok(MusicRequest {
                    term: title,
                    source: SearchSource::YouTube,
                    requested_from: source.catalog_name().map(str::to_owned),
                    direct_url: None,
                })
            }
        }
    }

    async fn apple_search(&self, query: &str) -> Result<String> {
        let mut endpoint = Url::parse("https://itunes.apple.com/search")?;
        endpoint
            .query_pairs_mut()
            .append_pair("term", query)
            .append_pair("entity", "song")
            .append_pair("limit", "1")
            .append_pair("country", &apple_music_country());
        let payload = self.fetch_json(endpoint).await?;
        let (artist, title) = apple_song(&payload)?;
        Ok(format!("{artist} - {title}"))
    }

    async fn fetch_json(&self, url: Url) -> Result<Value> {
        let bytes = self.fetch(url).await?;
        serde_json::from_slice(&bytes).context("invalid public catalog JSON")
    }

    async fn fetch_text(&self, url: Url) -> Result<String> {
        let bytes = self.fetch(url).await?;
        Ok(String::from_utf8_lossy(&bytes).into_owned())
    }

    async fn fetch(&self, url: Url) -> Result<Vec<u8>> {
        if !is_allowed_url(&url) {
            bail!("Поддерживаются только безопасные HTTPS-ссылки музыкальных сервисов.");
        }
        let response = self
            .client
            .get(url)
            .send()
            .await
            .context("ошибка запроса публичных метаданных")?;
        if !response.status().is_success() {
            bail!(
                "Сервис вернул HTTP {} при запросе метаданных.",
                response.status().as_u16()
            );
        }
        if response
            .content_length()
            .is_some_and(|length| length > MAX_RESPONSE_BYTES as u64)
        {
            bail!("Страница музыкального сервиса слишком большая.");
        }
        let mut stream = response.bytes_stream();
        let mut bytes = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.context("ошибка загрузки метаданных")?;
            if bytes.len() + chunk.len() > MAX_RESPONSE_BYTES {
                bail!("Страница музыкального сервиса слишком большая.");
            }
            bytes.extend_from_slice(&chunk);
        }
        Ok(bytes)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SourceTag {
    YouTube,
    SoundCloud,
    Spotify,
    Apple,
    Yandex,
    Vk,
}

impl SourceTag {
    fn catalog_name(self) -> Option<&'static str> {
        match self {
            Self::YouTube | Self::SoundCloud => None,
            Self::Spotify => Some("Spotify"),
            Self::Apple => Some("Apple Music"),
            Self::Yandex => Some("Яндекс Музыка"),
            Self::Vk => Some("VK Музыка"),
        }
    }
}

fn split_source_prefix(value: &str) -> Result<(Option<SourceTag>, &str)> {
    let Some((prefix, rest)) = value.split_once(char::is_whitespace) else {
        if source_prefix(value).is_some() {
            bail!("После названия сервиса укажи песню или ссылку.");
        }
        return Ok((None, value));
    };
    let tag = source_prefix(prefix);
    Ok((tag, if tag.is_some() { rest.trim() } else { value }))
}

fn source_prefix(value: &str) -> Option<SourceTag> {
    match value.to_lowercase().as_str() {
        "yt" | "youtube" | "ютуб" => Some(SourceTag::YouTube),
        "sc" | "soundcloud" | "саундклауд" => Some(SourceTag::SoundCloud),
        "spotify" | "спотифай" | "споти" => Some(SourceTag::Spotify),
        "apple" | "applemusic" | "эпл" | "айтьюнс" => Some(SourceTag::Apple),
        "yandex" | "yandexmusic" | "яндекс" | "ям" | "ямузыка" | "я.музыка" => {
            Some(SourceTag::Yandex)
        }
        "vk" | "вк" | "вконтакте" | "vkontakte" => Some(SourceTag::Vk),
        _ => None,
    }
}

fn allowed_host(host: &str) -> bool {
    matches!(
        host,
        "youtube.com"
            | "www.youtube.com"
            | "m.youtube.com"
            | "music.youtube.com"
            | "youtu.be"
            | "www.youtu.be"
            | "soundcloud.com"
            | "www.soundcloud.com"
            | "m.soundcloud.com"
            | "on.soundcloud.com"
            | "open.spotify.com"
            | "spotify.link"
            | "music.apple.com"
            | "itunes.apple.com"
            | "music.yandex.ru"
            | "music.yandex.com"
            | "music.yandex.kz"
            | "music.yandex.by"
            | "vk.com"
            | "m.vk.com"
            | "music.vk.com"
            | "vk.ru"
            | "music.vk.ru"
    )
}

fn is_allowed_url(url: &Url) -> bool {
    url.scheme() == "https"
        && url.username().is_empty()
        && url.password().is_none()
        && url.port_or_known_default() == Some(443)
        && url.host_str().is_some_and(allowed_host)
}

fn parse_allowed_url(raw: &str) -> Result<Url> {
    let url = Url::parse(raw).context("Некорректная ссылка на трек.")?;
    if !is_allowed_url(&url) {
        bail!("Поддерживаются только безопасные HTTPS-ссылки YouTube, SoundCloud, VK, Яндекс, Spotify и Apple Music.");
    }
    Ok(url)
}

fn detect_source(raw: &str) -> Result<SourceTag> {
    let url = parse_allowed_url(raw)?;
    let host = url.host_str().unwrap_or_default();
    Ok(match host {
        "youtube.com" | "www.youtube.com" | "m.youtube.com" | "music.youtube.com" | "youtu.be"
        | "www.youtu.be" => SourceTag::YouTube,
        "soundcloud.com" | "www.soundcloud.com" | "m.soundcloud.com" | "on.soundcloud.com" => {
            SourceTag::SoundCloud
        }
        "open.spotify.com" | "spotify.link" => SourceTag::Spotify,
        "music.apple.com" | "itunes.apple.com" => SourceTag::Apple,
        "music.yandex.ru" | "music.yandex.com" | "music.yandex.kz" | "music.yandex.by" => {
            SourceTag::Yandex
        }
        "vk.com" | "m.vk.com" | "music.vk.com" | "vk.ru" | "music.vk.ru" => SourceTag::Vk,
        _ => bail!("Неподдерживаемый музыкальный сервис."),
    })
}

pub(crate) fn direct_url_allowed(raw: &str, requested_source: SearchSource) -> bool {
    matches!(
        (detect_source(raw), requested_source),
        (Ok(SourceTag::YouTube), SearchSource::YouTube)
            | (Ok(SourceTag::SoundCloud), SearchSource::SoundCloud)
    )
}

fn apple_music_country() -> String {
    let country = std::env::var("APPLE_MUSIC_COUNTRY").unwrap_or_else(|_| "RU".into());
    apple_music_country_value(&country)
}

fn apple_music_country_value(country: &str) -> String {
    if country.len() == 2 && country.bytes().all(|byte| byte.is_ascii_alphabetic()) {
        country.to_ascii_uppercase()
    } else {
        "RU".into()
    }
}

fn spotify_track_id(url: &Url) -> Result<String> {
    let parts = url
        .path_segments()
        .into_iter()
        .flatten()
        .collect::<Vec<_>>();
    let track_index = parts
        .iter()
        .position(|part| *part == "track")
        .ok_or_else(|| {
            anyhow!(
                "Spotify: поддерживаются ссылки на отдельные треки, а не альбомы или плейлисты."
            )
        })?;
    let id = parts.get(track_index + 1).copied().unwrap_or_default();
    if id.len() != 22 || !id.bytes().all(|byte| byte.is_ascii_alphanumeric()) {
        bail!("Некорректная ссылка Spotify на трек.");
    }
    Ok(id.to_owned())
}

fn apple_track_id(url: &Url) -> Result<String> {
    if let Some((_, id)) = url.query_pairs().find(|(key, _)| key == "i") {
        if id.len() >= 5 && id.bytes().all(|byte| byte.is_ascii_digit()) {
            return Ok(id.into_owned());
        }
        bail!("Некорректный ID трека Apple Music.");
    }
    let parts = url
        .path_segments()
        .into_iter()
        .flatten()
        .collect::<Vec<_>>();
    let song = parts
        .iter()
        .position(|part| *part == "song")
        .ok_or_else(|| {
            anyhow!(
                "Apple Music: поддерживаются ссылки на отдельные песни, не альбомы и плейлисты."
            )
        })?;
    let id = parts
        .get(song + 2)
        .copied()
        .or_else(|| parts.get(song + 1).copied())
        .unwrap_or_default();
    if id.len() >= 5 && id.bytes().all(|byte| byte.is_ascii_digit()) {
        return Ok(id.to_owned());
    }
    bail!("Некорректная ссылка Apple Music на трек.");
}

fn apple_song(payload: &Value) -> Result<(String, String)> {
    let records = payload
        .get("results")
        .and_then(Value::as_array)
        .ok_or_else(|| anyhow!("Apple Music не вернул результаты поиска."))?;
    let record = records
        .iter()
        .find(|entry| {
            entry
                .get("kind")
                .and_then(Value::as_str)
                .is_none_or(|kind| kind == "song")
        })
        .ok_or_else(|| anyhow!("Apple Music не нашёл отдельный трек."))?;
    let artist = clean_text(
        record
            .get("artistName")
            .and_then(Value::as_str)
            .unwrap_or_default(),
    );
    let title = clean_text(
        record
            .get("trackName")
            .and_then(Value::as_str)
            .unwrap_or_default(),
    );
    if artist.is_empty() || title.is_empty() {
        bail!("Apple Music не вернул исполнителя и название трека.");
    }
    Ok((artist, title))
}

fn bridged_request(source_name: &str, artist: &str, title: &str) -> MusicRequest {
    let term = if artist.is_empty() {
        title.to_owned()
    } else {
        format!("{artist} - {title}")
    };
    MusicRequest {
        term,
        source: SearchSource::YouTube,
        requested_from: Some(source_name.into()),
        direct_url: None,
    }
}

fn clean_text(value: &str) -> String {
    let decoded = html_escape::decode_html_entities(value);
    decoded
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim_matches([' ', '-', '–', '—', '|'])
        .chars()
        .take(200)
        .collect()
}

fn clean_catalog_title(value: &str) -> String {
    let value = clean_text(value);
    let lower = value.to_lowercase();
    let suffixes = [
        " | spotify",
        " | apple music",
        " | яндекс музыка",
        " | yandex music",
        " | vk музыка",
        " | вконтакте",
    ];
    for suffix in suffixes {
        if let Some(index) = lower.rfind(suffix) {
            return value[..index].trim().to_owned();
        }
    }
    value
}

fn is_service_name(value: &str) -> bool {
    matches!(
        value.to_lowercase().as_str(),
        "vk" | "вконтакте" | "vk музыка" | "яндекс музыка" | "yandex music"
    )
}

fn open_graph_title(html: &str) -> Option<String> {
    static META: std::sync::LazyLock<Regex> =
        std::sync::LazyLock::new(|| Regex::new(r"(?is)<meta\b[^>]*>").expect("static regex"));
    static ATTR: std::sync::LazyLock<Regex> = std::sync::LazyLock::new(|| {
        Regex::new(r#"(?i)([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))"#)
            .expect("static regex")
    });
    for tag in META.find_iter(html) {
        let mut property = None;
        let mut content = None;
        for attr in ATTR.captures_iter(tag.as_str()) {
            let key = attr.get(1)?.as_str().to_ascii_lowercase();
            let value = attr
                .get(2)
                .or_else(|| attr.get(3))
                .or_else(|| attr.get(4))?
                .as_str();
            match key.as_str() {
                "property" | "name"
                    if matches!(
                        value.to_ascii_lowercase().as_str(),
                        "og:title" | "twitter:title"
                    ) =>
                {
                    property = Some(value.to_owned())
                }
                "content" => content = Some(value.to_owned()),
                _ => {}
            }
        }
        if property.is_some() {
            if let Some(content) = content {
                return Some(content);
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_prefixes_map_to_safe_search_sources() {
        assert_eq!(
            split_source_prefix("soundcloud artist song").unwrap(),
            (Some(SourceTag::SoundCloud), "artist song")
        );
        assert_eq!(
            split_source_prefix("яндекс Кино - Группа крови").unwrap(),
            (Some(SourceTag::Yandex), "Кино - Группа крови")
        );
        assert_eq!(
            SearchSource::YouTube.query("Кино - Группа крови"),
            "ytsearch:Кино - Группа крови"
        );
        assert_eq!(
            SearchSource::SoundCloud.query("artist song"),
            "scsearch:artist song"
        );
        assert!(split_source_prefix("spotify").is_err());
    }

    #[test]
    fn only_safe_https_catalog_hosts_are_accepted() {
        assert_eq!(
            detect_source("https://youtu.be/ABCDEFGHI12").unwrap(),
            SourceTag::YouTube
        );
        assert!(parse_allowed_url("http://open.spotify.com/track/1234567890123456789012").is_err());
        assert!(parse_allowed_url(
            "https://open.spotify.com.evil.test/track/1234567890123456789012"
        )
        .is_err());
        assert!(parse_allowed_url("https://127.0.0.1/audio").is_err());
        assert!(parse_allowed_url("https://user@vk.com/audio-1_2").is_err());
    }

    #[test]
    fn only_youtube_and_soundcloud_urls_can_be_loaded_directly() {
        assert!(direct_url_allowed(
            "https://www.youtube.com/watch?v=abcdefghijk",
            SearchSource::YouTube
        ));
        assert!(direct_url_allowed(
            "https://soundcloud.com/artist/track",
            SearchSource::SoundCloud
        ));
        for url in [
            "https://open.spotify.com/track/1234567890123456789012",
            "https://music.apple.com/ru/album/song/123456789",
            "https://music.yandex.ru/album/1/track/2",
            "https://vk.com/audio-1_2",
        ] {
            assert!(!direct_url_allowed(url, SearchSource::YouTube), "{url}");
            assert!(!direct_url_allowed(url, SearchSource::SoundCloud), "{url}");
        }
        assert!(!direct_url_allowed(
            "https://soundcloud.com/artist/track",
            SearchSource::YouTube
        ));
    }

    #[test]
    fn apple_music_country_is_validated_and_normalized() {
        assert_eq!(apple_music_country_value("lv"), "LV");
        assert_eq!(apple_music_country_value("RUS"), "RU");
        assert_eq!(apple_music_country_value("!@"), "RU");
    }

    #[test]
    fn only_single_catalog_tracks_resolve() {
        assert_eq!(
            spotify_track_id(
                &Url::parse("https://open.spotify.com/intl-ru/track/1234567890123456789012?si=x")
                    .unwrap()
            )
            .unwrap(),
            "1234567890123456789012"
        );
        assert!(spotify_track_id(
            &Url::parse("https://open.spotify.com/playlist/1234567890123456789012").unwrap()
        )
        .is_err());
        assert_eq!(
            apple_track_id(
                &Url::parse("https://music.apple.com/ru/album/example/123456789?i=123456789")
                    .unwrap()
            )
            .unwrap(),
            "123456789"
        );
        assert!(apple_track_id(
            &Url::parse("https://music.apple.com/ru/album/example/123456789").unwrap()
        )
        .is_err());
    }

    #[test]
    fn catalog_html_is_decoded_and_service_branding_removed() {
        let html = r#"<meta content="Кино &amp; Виктор Цой | Яндекс Музыка" property="og:title">"#;
        assert_eq!(
            clean_catalog_title(&open_graph_title(html).unwrap()),
            "Кино & Виктор Цой"
        );
        assert_eq!(
            bridged_request("Spotify", "Pink Floyd", "Speak To Me").term,
            "Pink Floyd - Speak To Me"
        );
    }
}
