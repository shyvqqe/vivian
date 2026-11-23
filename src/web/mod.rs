use axum::{
    body::Body,
    extract::{DefaultBodyLimit, Path, Query, State},
    http::{header, HeaderMap, HeaderValue, Request, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use hmac::{Hmac, Mac};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, VecDeque},
    env,
    net::SocketAddr,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
use tokio::sync::RwLock;
use tracing::{info, warn};

use crate::{config::Config, storage::Store};

type HmacSha256 = Hmac<Sha256>;
type WebResult<T> = std::result::Result<T, WebError>;

#[derive(Clone)]
struct WebState {
    store: Store,
    http: reqwest::Client,
    bot_token: String,
    bot_id: u64,
    discord_ready: Arc<AtomicBool>,
    sessions: Arc<RwLock<HashMap<String, OAuthSession>>>,
    request_limits: Arc<tokio::sync::Mutex<HashMap<(String, String), VecDeque<Instant>>>>,
}

#[derive(Clone)]
struct OAuthSession {
    user_id: String,
    access_token: String,
    expires_at: i64,
}

#[derive(Clone, Debug)]
struct Claims {
    user_id: String,
    nonce: String,
    expires_at: i64,
}

#[derive(Debug)]
struct WebError {
    status: StatusCode,
    message: String,
}

impl WebError {
    fn new(status: StatusCode, message: impl Into<String>) -> Self {
        Self {
            status,
            message: message.into(),
        }
    }
    fn unauthorized() -> Self {
        Self::new(StatusCode::UNAUTHORIZED, "Sign in with Discord.")
    }
    fn unavailable(message: impl Into<String>) -> Self {
        Self::new(StatusCode::SERVICE_UNAVAILABLE, message)
    }
    fn forbidden(message: impl Into<String>) -> Self {
        Self::new(StatusCode::FORBIDDEN, message)
    }
    fn bad_request(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, message)
    }
}

impl IntoResponse for WebError {
    fn into_response(self) -> Response {
        let mut response = (self.status, Json(json!({"error":self.message}))).into_response();
        security_headers(response.headers_mut());
        response
    }
}

fn security_headers(headers: &mut HeaderMap) {
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(
        "x-content-type-options",
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert("x-frame-options", HeaderValue::from_static("DENY"));
    headers.insert(header::CONTENT_SECURITY_POLICY, HeaderValue::from_static("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' https://cdn.discordapp.com https://media.discordapp.net; connect-src 'self' https://discord.com; base-uri 'none'; form-action 'self' https://discord.com; frame-ancestors 'none'"));
}

async fn no_cache(
    State(state): State<Arc<WebState>>,
    request: Request<Body>,
    next: Next,
) -> Response {
    let path = request.uri().path();
    if path.starts_with("/api/") || path.starts_with("/auth/") {
        let address = request
            .extensions()
            .get::<axum::extract::ConnectInfo<SocketAddr>>()
            .map(|info| info.0.ip().to_string())
            .unwrap_or_else(|| "unknown".to_owned());
        let group = if path.starts_with("/auth/") {
            "auth"
        } else {
            "api"
        };
        let now = Instant::now();
        let mut budgets = state.request_limits.lock().await;
        if !budgets.contains_key(&(address.clone(), group.to_owned())) && budgets.len() >= 4096 {
            if let Some(key) = budgets.keys().next().cloned() {
                budgets.remove(&key);
            }
        }
        let requests = budgets.entry((address, group.to_owned())).or_default();
        while requests
            .front()
            .is_some_and(|at| now.duration_since(*at) >= Duration::from_secs(60))
        {
            requests.pop_front();
        }
        let limit = if group == "auth" { 30 } else { 120 };
        if requests.len() >= limit {
            drop(budgets);
            return WebError::new(
                StatusCode::TOO_MANY_REQUESTS,
                "Too many requests. Please try again in a minute.",
            )
            .into_response();
        }
        requests.push_back(now);
    }
    let mut response = next.run(request).await;
    security_headers(response.headers_mut());
    response
}

pub(crate) fn settings_defaults() -> Value {
    json!({
        "social_enabled":true,"ambient_enabled":true,"confessions_enabled":false,"vibecheck_enabled":false,
        "memories_enabled":false,"birthdays_enabled":false,"stories_enabled":false,"timeline_enabled":false,
        "ask_enabled":false,"journal_enabled":false,"economy_enabled":false,"events_enabled":false,
        "voice_stats_enabled":false,"server_style_enabled":false,"highlights_enabled":false,"games_enabled":true,
        "voice_tts_enabled":false,"seasons_enabled":false,"welcome_enabled":true,"autorole_enabled":true,
        "ai_moderation_enabled":true,"public_profiles_enabled":false,"weekly_enabled":false,
        "social_channel_id":null,"confessions_channel_id":null,"conversation_channel_id":null,"welcome_channel_id":null,
        "autorole_id":null,"anonymous_log_channel_id":null,"memories_channel_id":null,"birthday_channel_id":null,
        "birthday_role_id":null,"stories_channel_id":null,"journal_channel_id":null,"modlog_channel_id":null,
        "appeals_channel_id":null,"member_event_channel_id":null,"jail_role_id":null,"jail_channel_id":null,
        "quarantine_role_id":null,"quarantine_channel_id":null,"events_channel_id":null,"highlights_channel_id":null,
        "voice_channel_id":null,"voice_commentary_enabled":false,"archive_channel_ids":[],"timezone":"Europe/Riga",
        "birthday_hour":9,"weekly_day":6,"weekly_hour":18,"monthly_day":1,"monthly_hour":18,
        "moderate_warning_ladder":[{"count":3,"action":"timeout","seconds":3600},{"count":5,"action":"timeout","seconds":86400},{"count":7,"action":"ban","seconds":0}],
        "moderate_quarantine_enabled":false,"moderate_min_account_age_hours":24,"moderate_require_avatar":false,
        "moderate_raid_auto":false,"moderate_raid_join_count":6,"moderate_raid_window_seconds":15,
        "moderate_raid_link_filter":true,"moderate_link_filter":false,"jail_bail":0,"currency_name":"sparks",
        "daily_currency_cap":100,"daily_reward":25,"season_theme":"auto","server_style":""
    })
}

fn secret() -> WebResult<Vec<u8>> {
    let value = env::var("DASHBOARD_SESSION_SECRET").unwrap_or_default();
    if value.as_bytes().len() < 32 {
        return Err(WebError::unavailable(
            "Set DASHBOARD_SESSION_SECRET to a stable secret of at least 32 bytes.",
        ));
    }
    Ok(value.into_bytes())
}

fn base_url() -> WebResult<String> {
    let raw = env::var("DASHBOARD_BASE_URL")
        .or_else(|_| env::var("RENDER_EXTERNAL_URL"))
        .map_err(|_| {
            WebError::unavailable("Configure DASHBOARD_BASE_URL as the HTTPS dashboard origin.")
        })?;
    let parsed = url::Url::parse(raw.trim_end_matches('/'))
        .map_err(|_| WebError::unavailable("Configure an HTTPS dashboard URL."))?;
    if parsed.scheme() != "https"
        || parsed.host_str().is_none()
        || parsed.path() != "/"
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return Err(WebError::unavailable(
            "DASHBOARD_BASE_URL must be an HTTPS origin.",
        ));
    }
    Ok(parsed.origin().ascii_serialization())
}

fn site_url() -> WebResult<String> {
    let value = env::var("PUBLIC_SITE_URL").unwrap_or_else(|_| "https://esil.life".into());
    let parsed = url::Url::parse(value.trim_end_matches('/'))
        .map_err(|_| WebError::unavailable("PUBLIC_SITE_URL must be an HTTPS origin."))?;
    if parsed.scheme() != "https" || parsed.host_str().is_none() {
        return Err(WebError::unavailable(
            "PUBLIC_SITE_URL must be an HTTPS origin.",
        ));
    }
    Ok(parsed.origin().ascii_serialization())
}

fn sign(purpose: &str, payload: &Value) -> WebResult<String> {
    let encoded = URL_SAFE_NO_PAD.encode(
        serde_json::to_vec(payload).map_err(|_| WebError::bad_request("Invalid token payload."))?,
    );
    let mut mac = HmacSha256::new_from_slice(&secret()?)
        .map_err(|_| WebError::unavailable("Dashboard signing is unavailable."))?;
    mac.update(format!("{purpose}.{encoded}").as_bytes());
    Ok(format!(
        "{encoded}.{}",
        URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
    ))
}

fn verify(purpose: &str, token: &str, lifetime: i64) -> Option<Value> {
    if token.len() > 4096 {
        return None;
    }
    let (body, signature) = token.split_once('.')?;
    let expected = URL_SAFE_NO_PAD.decode(signature).ok()?;
    let mut mac = HmacSha256::new_from_slice(&secret().ok()?).ok()?;
    mac.update(format!("{purpose}.{body}").as_bytes());
    mac.verify_slice(&expected).ok()?;
    let payload: Value = serde_json::from_slice(&URL_SAFE_NO_PAD.decode(body).ok()?).ok()?;
    let now = chrono::Utc::now().timestamp();
    let iat = payload.get("iat")?.as_i64()?;
    let exp = payload.get("exp")?.as_i64()?;
    if iat > now + 30 || exp <= now || exp <= iat || exp - iat > lifetime {
        return None;
    }
    Some(payload)
}

fn cookie(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(';'))
        .find_map(|part| {
            let (key, value) = part.trim().split_once('=')?;
            (key == name).then(|| value.to_owned())
        })
}

fn set_cookie(
    headers: &mut HeaderMap,
    name: &str,
    value: &str,
    max_age: i64,
    path: &str,
) -> WebResult<()> {
    let value =
        format!("{name}={value}; Max-Age={max_age}; Path={path}; HttpOnly; Secure; SameSite=Lax");
    headers.append(
        header::SET_COOKIE,
        HeaderValue::from_str(&value)
            .map_err(|_| WebError::bad_request("Invalid session cookie."))?,
    );
    Ok(())
}

fn valid_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 20
        && value.as_bytes()[0] != b'0'
        && value.bytes().all(|b| b.is_ascii_digit())
}
fn valid_nonce(value: &str) -> bool {
    (24..=80).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

fn parse_claims(headers: &HeaderMap) -> WebResult<Claims> {
    let token = cookie(headers, "__Host-vivian_session").ok_or_else(WebError::unauthorized)?;
    let value = verify("session", &token, 3600).ok_or_else(WebError::unauthorized)?;
    let uid = value
        .get("uid")
        .and_then(Value::as_str)
        .filter(|v| valid_id(v))
        .ok_or_else(WebError::unauthorized)?;
    let nonce = value
        .get("nonce")
        .and_then(Value::as_str)
        .filter(|v| valid_nonce(v))
        .ok_or_else(WebError::unauthorized)?;
    Ok(Claims {
        user_id: uid.into(),
        nonce: nonce.into(),
        expires_at: value["exp"].as_i64().ok_or_else(WebError::unauthorized)?,
    })
}

fn hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 15) as usize] as char);
    }
    out
}

fn csrf(claims: &Claims) -> WebResult<String> {
    let mut mac = HmacSha256::new_from_slice(&secret()?)
        .map_err(|_| WebError::unavailable("Dashboard signing is unavailable."))?;
    mac.update(
        format!(
            "csrf:{}:{}:{}",
            claims.user_id, claims.nonce, claims.expires_at
        )
        .as_bytes(),
    );
    Ok(hex_lower(&mac.finalize().into_bytes()))
}

fn check_origin(headers: &HeaderMap, required: bool) -> WebResult<()> {
    let origin = headers.get(header::ORIGIN).and_then(|v| v.to_str().ok());
    let expected = base_url()?;
    if (required && origin != Some(expected.as_str())) || origin.is_some_and(|o| o != expected) {
        return Err(WebError::forbidden(
            "The request must come from the dashboard origin.",
        ));
    }
    if headers.get("sec-fetch-site").and_then(|v| v.to_str().ok()) == Some("cross-site") {
        return Err(WebError::forbidden(
            "Cross-site dashboard requests are not allowed.",
        ));
    }
    Ok(())
}

async fn bearer_session(state: &WebState, claims: &Claims) -> WebResult<String> {
    let sessions = state.sessions.read().await;
    let session = sessions
        .get(&claims.nonce)
        .filter(|s| s.user_id == claims.user_id && s.expires_at > chrono::Utc::now().timestamp())
        .ok_or_else(WebError::unauthorized)?;
    Ok(session.access_token.clone())
}

fn client_id(state: &WebState) -> String {
    env::var("DISCORD_CLIENT_ID").unwrap_or_else(|_| state.bot_id.to_string())
}
fn redirect(location: &str) -> Response {
    let mut response = Response::new(Body::empty());
    *response.status_mut() = StatusCode::FOUND;
    if let Ok(value) = HeaderValue::from_str(location) {
        response.headers_mut().insert(header::LOCATION, value);
    }
    response
}

fn pkce(nonce: &str) -> WebResult<String> {
    let mut mac = HmacSha256::new_from_slice(&secret()?)
        .map_err(|_| WebError::unavailable("Dashboard signing is unavailable."))?;
    mac.update(format!("pkce:{nonce}").as_bytes());
    Ok(URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes()))
}

async fn oauth_login(State(state): State<Arc<WebState>>) -> WebResult<Response> {
    if env::var("DISCORD_CLIENT_SECRET")
        .unwrap_or_default()
        .is_empty()
    {
        return Err(WebError::unavailable(
            "Configure DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET for OAuth.",
        ));
    }
    let base = base_url()?;
    let nonce = URL_SAFE_NO_PAD.encode(rand::random::<[u8; 24]>());
    let now = chrono::Utc::now().timestamp();
    let state_token = sign("oauth", &json!({"nonce":nonce,"iat":now,"exp":now+600}))?;
    let verifier = pkce(&nonce)?;
    let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
    let mut url = url::Url::parse("https://discord.com/oauth2/authorize").unwrap();
    url.query_pairs_mut()
        .append_pair("client_id", &client_id(&state))
        .append_pair("response_type", "code")
        .append_pair("redirect_uri", &format!("{base}/auth/callback"))
        .append_pair("scope", "identify guilds")
        .append_pair("state", &state_token)
        .append_pair("code_challenge", &challenge)
        .append_pair("code_challenge_method", "S256");
    let mut response = redirect(url.as_str());
    set_cookie(
        response.headers_mut(),
        "__Secure-vivian_oauth",
        &nonce,
        600,
        "/auth",
    )?;
    Ok(response)
}

#[derive(serde::Deserialize)]
struct OAuthCallback {
    code: Option<String>,
    state: Option<String>,
    error: Option<String>,
    iss: Option<String>,
}

async fn oauth_callback(
    State(state): State<Arc<WebState>>,
    headers: HeaderMap,
    Query(query): Query<OAuthCallback>,
) -> WebResult<Response> {
    let state_value = query
        .state
        .as_deref()
        .and_then(|t| verify("oauth", t, 600))
        .ok_or_else(|| {
            WebError::forbidden("Discord sign-in state expired. Please sign in again.")
        })?;
    let expected = state_value
        .get("nonce")
        .and_then(Value::as_str)
        .ok_or_else(WebError::unauthorized)?;
    let nonce = cookie(&headers, "__Secure-vivian_oauth")
        .filter(|n| valid_nonce(n))
        .ok_or_else(|| {
            WebError::forbidden("Discord sign-in state expired. Please sign in again.")
        })?;
    if expected != nonce {
        return Err(WebError::forbidden(
            "Discord sign-in state expired. Please sign in again.",
        ));
    }
    if query.iss.as_deref().unwrap_or("https://discord.com") != "https://discord.com" {
        return Err(WebError::forbidden("The OAuth issuer is invalid."));
    }
    if query.error.is_some() {
        return Err(WebError::new(
            StatusCode::UNAUTHORIZED,
            "Discord sign-in was cancelled.",
        ));
    }
    let code = query
        .code
        .filter(|v| !v.is_empty() && v.len() <= 2000)
        .ok_or_else(|| WebError::new(StatusCode::UNAUTHORIZED, "Discord sign-in was cancelled."))?;
    let client_secret = env::var("DISCORD_CLIENT_SECRET")
        .map_err(|_| WebError::unavailable("Configure DISCORD_CLIENT_SECRET."))?;
    let form = [
        ("client_id", client_id(&state)),
        ("client_secret", client_secret),
        ("grant_type", "authorization_code".into()),
        ("code", code),
        ("code_verifier", pkce(&nonce)?),
        ("redirect_uri", format!("{}/auth/callback", base_url()?)),
    ];
    let response = state
        .http
        .post("https://discord.com/api/oauth2/token")
        .form(&form)
        .timeout(Duration::from_secs(8))
        .send()
        .await
        .map_err(|_| WebError::unavailable("Discord OAuth did not respond."))?;
    if !response.status().is_success() {
        return Err(WebError::new(
            StatusCode::UNAUTHORIZED,
            "Discord sign-in could not be completed. Please sign in again.",
        ));
    }
    let token_data: Value = response.json().await.map_err(|_| {
        WebError::new(
            StatusCode::UNAUTHORIZED,
            "Discord token response was invalid.",
        )
    })?;
    let access_token = token_data
        .get("access_token")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
        .ok_or_else(WebError::unauthorized)?
        .to_owned();
    let user: Value = state
        .http
        .get("https://discord.com/api/v10/users/@me")
        .bearer_auth(&access_token)
        .timeout(Duration::from_secs(8))
        .send()
        .await
        .map_err(|_| WebError::unavailable("Discord identity could not be verified."))?
        .json()
        .await
        .map_err(|_| WebError::unauthorized())?;
    let user_id = user
        .get("id")
        .and_then(Value::as_str)
        .filter(|v| valid_id(v) && user.get("bot").and_then(Value::as_bool) != Some(true))
        .ok_or_else(WebError::unauthorized)?;
    let session_nonce = URL_SAFE_NO_PAD.encode(rand::random::<[u8; 24]>());
    let now = chrono::Utc::now().timestamp();
    let session = sign(
        "session",
        &json!({"uid":user_id,"nonce":session_nonce,"iat":now,"exp":now+3600}),
    )?;
    state.sessions.write().await.insert(
        session_nonce,
        OAuthSession {
            user_id: user_id.into(),
            access_token,
            expires_at: now + 3600,
        },
    );
    let mut response = redirect("/dashboard");
    set_cookie(
        response.headers_mut(),
        "__Host-vivian_session",
        &session,
        3600,
        "/",
    )?;
    set_cookie(
        response.headers_mut(),
        "__Secure-vivian_oauth",
        "",
        0,
        "/auth",
    )?;
    Ok(response)
}

async fn discord_bot_get(state: &WebState, path: &str) -> WebResult<Value> {
    let response = state
        .http
        .get(format!("https://discord.com/api/v10{path}"))
        .bearer_auth(&state.bot_token)
        .timeout(Duration::from_secs(8))
        .send()
        .await
        .map_err(|_| WebError::unavailable("Discord could not be reached."))?;
    if response.status() == StatusCode::NOT_FOUND {
        return Err(WebError::new(
            StatusCode::NOT_FOUND,
            "The bot is not a member of this server.",
        ));
    }
    if !response.status().is_success() {
        return Err(WebError::unavailable(
            "Discord permissions could not be verified.",
        ));
    }
    response
        .json()
        .await
        .map_err(|_| WebError::unavailable("Discord returned an invalid response."))
}

async fn oauth_get(state: &WebState, token: &str, path: &str) -> WebResult<Value> {
    let response = state
        .http
        .get(format!("https://discord.com/api/v10{path}"))
        .bearer_auth(token)
        .timeout(Duration::from_secs(8))
        .send()
        .await
        .map_err(|_| WebError::unavailable("Discord could not be reached."))?;
    if !response.status().is_success() {
        return Err(WebError::unavailable(
            "Discord permissions could not be verified.",
        ));
    }
    response
        .json()
        .await
        .map_err(|_| WebError::unavailable("Discord returned an invalid response."))
}

async fn user_guilds(state: &WebState, token: &str) -> WebResult<Vec<Value>> {
    oauth_get(state, token, "/users/@me/guilds")
        .await?
        .as_array()
        .cloned()
        .ok_or_else(|| WebError::unavailable("Discord guild list was invalid."))
}
fn can_manage_guild(guild: &Value) -> bool {
    guild.get("owner").and_then(Value::as_bool) == Some(true)
        || guild
            .get("permissions")
            .and_then(Value::as_str)
            .and_then(|v| v.parse::<u64>().ok())
            .is_some_and(|p| p & 0x20 != 0)
}

async fn accessible_guild(state: &WebState, token: &str, guild_id: &str) -> WebResult<Value> {
    if !valid_id(guild_id) {
        return Err(WebError::bad_request("Invalid server ID."));
    }
    let guilds = user_guilds(state, token).await?;
    if !guilds
        .iter()
        .any(|g| g["id"].as_str() == Some(guild_id) && can_manage_guild(g))
    {
        return Err(WebError::forbidden("Manage Server permission is required."));
    }
    discord_bot_get(state, &format!("/guilds/{guild_id}?with_counts=true")).await
}

fn guild_icon(guild: &Value) -> Option<String> {
    Some(format!(
        "https://cdn.discordapp.com/icons/{}/{}.png?size=128",
        guild.get("id")?.as_str()?,
        guild.get("icon")?.as_str()?
    ))
}
fn guild_info(guild: &Value) -> Value {
    json!({"id":guild.get("id"),"name":guild.get("name"),"icon":guild_icon(guild),"member_count":guild.get("approximate_member_count").and_then(Value::as_u64).unwrap_or(0)})
}

async fn guild_list(
    State(state): State<Arc<WebState>>,
    headers: HeaderMap,
) -> WebResult<Json<Value>> {
    check_origin(&headers, false)?;
    let claims = parse_claims(&headers)?;
    let token = bearer_session(&state, &claims).await?;
    let mut rows = Vec::new();
    for user_guild in user_guilds(&state, &token)
        .await?
        .into_iter()
        .filter(can_manage_guild)
    {
        let Some(id) = user_guild.get("id").and_then(Value::as_str) else {
            continue;
        };
        match discord_bot_get(&state, &format!("/guilds/{id}?with_counts=true")).await {
            Ok(guild) => rows.push(guild_info(&guild)),
            Err(error) if error.status == StatusCode::NOT_FOUND => {}
            Err(error) => return Err(error),
        }
    }
    rows.sort_by(|a, b| {
        a["name"]
            .as_str()
            .unwrap_or("")
            .to_lowercase()
            .cmp(&b["name"].as_str().unwrap_or("").to_lowercase())
    });
    Ok(Json(json!({"guilds":rows})))
}

async fn guild_details(
    State(state): State<Arc<WebState>>,
    headers: HeaderMap,
    Path(guild_id): Path<String>,
) -> WebResult<Json<Value>> {
    check_origin(&headers, false)?;
    let claims = parse_claims(&headers)?;
    let token = bearer_session(&state, &claims).await?;
    let guild = accessible_guild(&state, &token, &guild_id).await?;
    let channels = discord_bot_get(&state, &format!("/guilds/{guild_id}/channels")).await?;
    let roles = discord_bot_get(&state, &format!("/guilds/{guild_id}/roles")).await?;
    let id = guild_id
        .parse::<u64>()
        .map_err(|_| WebError::bad_request("Invalid server ID."))?;
    let settings = state
        .store
        .guild_settings(id, &settings_defaults())
        .await
        .map_err(|_| WebError::unavailable("Settings storage is unavailable."))?;
    let tracked: i64 = sqlx::query_scalar("SELECT count(*) FROM member_progress WHERE guild_id=?")
        .bind(&guild_id)
        .fetch_one(&state.store.pool)
        .await
        .unwrap_or(0);
    let messages: i64 = sqlx::query_scalar(
        "SELECT coalesce(sum(message_count),0) FROM member_progress WHERE guild_id=?",
    )
    .bind(&guild_id)
    .fetch_one(&state.store.pool)
    .await
    .unwrap_or(0);
    let channels = channels
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|c| {
            let kind = match c.get("type").and_then(Value::as_u64) {
                Some(0) => "text",
                Some(2) => "voice",
                Some(4) => "category",
                _ => return None,
            };
            Some(json!({"id":c["id"],"name":c["name"],"kind":kind}))
        })
        .collect::<Vec<_>>();
    let roles = roles
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|r| {
            r.get("managed").and_then(Value::as_bool) != Some(true)
                && r.get("id").and_then(Value::as_str) != Some(guild_id.as_str())
        })
        .map(|r| json!({"id":r["id"],"name":r["name"],"managed":r["managed"]}))
        .collect::<Vec<_>>();
    Ok(Json(
        json!({"guild":guild_info(&guild),"settings":settings_json(settings),"channels":channels,"roles":roles,
        "stats":{"messages":messages,"tracked_members":tracked,"commands":crate::commands::all().len()}}),
    ))
}

fn settings_json(mut settings: Value) -> Value {
    if let Some(object) = settings.as_object_mut() {
        for (key, value) in object.iter_mut() {
            if key.ends_with("_id") && !value.is_null() {
                *value = Value::String(value.to_string().trim_matches('"').to_owned());
            } else if key.ends_with("_ids") {
                if let Some(items) = value.as_array_mut() {
                    for item in items {
                        *item = Value::String(item.to_string().trim_matches('"').to_owned());
                    }
                }
            }
        }
    }
    settings
}

async fn validate_settings(
    state: &WebState,
    guild_id: &str,
    patch: &Value,
) -> WebResult<Map<String, Value>> {
    let object = patch
        .as_object()
        .ok_or_else(|| WebError::bad_request("Only recognized server settings can be changed."))?;
    let defaults = settings_defaults();
    let known = defaults.as_object().unwrap();
    if object.is_empty() || object.len() > known.len() {
        return Err(WebError::bad_request(
            "Only recognized server settings can be changed.",
        ));
    }
    let channels = discord_bot_get(state, &format!("/guilds/{guild_id}/channels"))
        .await?
        .as_array()
        .cloned()
        .unwrap_or_default();
    let roles = discord_bot_get(state, &format!("/guilds/{guild_id}/roles"))
        .await?
        .as_array()
        .cloned()
        .unwrap_or_default();
    let mut output = Map::new();
    for (key, raw) in object {
        let default = known.get(key).ok_or_else(|| {
            WebError::bad_request(format!(
                "Unknown setting: {}",
                key.chars().take(80).collect::<String>()
            ))
        })?;
        let mut value = raw.clone();
        if default.is_boolean() {
            if !value.is_boolean() {
                return Err(WebError::bad_request(format!(
                    "{key} must be true or false."
                )));
            }
        } else if key.ends_with("_channel_id") {
            if !value.is_null() && value.as_str().is_none_or(|s| !valid_id(s)) {
                return Err(WebError::bad_request("A channel identifier is required."));
            }
            if let Some(id) = value.as_str() {
                let is_voice = key == "voice_channel_id";
                if !channels.iter().any(|c| {
                    c["id"].as_str() == Some(id)
                        && c["type"].as_u64() == Some(if is_voice { 2 } else { 0 })
                }) {
                    return Err(WebError::bad_request(if is_voice {
                        "Choose a voice channel on this server."
                    } else {
                        "Choose a text channel on this server."
                    }));
                }
                value = Value::Number(id.parse::<u64>().unwrap().into());
            }
        } else if key.ends_with("_role_id") || key == "autorole_id" {
            if !value.is_null() && value.as_str().is_none_or(|s| !valid_id(s)) {
                return Err(WebError::bad_request("A role identifier is required."));
            }
            if let Some(id) = value.as_str() {
                let role = roles
                    .iter()
                    .find(|r| r["id"].as_str() == Some(id) && r["managed"].as_bool() != Some(true))
                    .ok_or_else(|| {
                        WebError::bad_request("Choose an unmanaged role on this server.")
                    })?;
                let perms = role["permissions"]
                    .as_str()
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(u64::MAX);
                let dangerous = (1 << 3)
                    | (1 << 5)
                    | (1 << 28)
                    | (1 << 30)
                    | (1 << 31)
                    | (1 << 32)
                    | (1 << 40)
                    | (1 << 13)
                    | (1 << 4);
                if perms & dangerous != 0 {
                    return Err(WebError::bad_request(
                        "This feature cannot grant a moderation role.",
                    ));
                }
                value = Value::Number(id.parse::<u64>().unwrap().into());
            }
        } else if key == "archive_channel_ids" {
            let items = value
                .as_array()
                .ok_or_else(|| WebError::bad_request("Choose up to 20 archive channels."))?;
            if items.len() > 20 {
                return Err(WebError::bad_request("Choose up to 20 archive channels."));
            }
            let mut ids = Vec::new();
            for item in items {
                let id = item
                    .as_str()
                    .filter(|s| valid_id(s))
                    .ok_or_else(|| WebError::bad_request("Invalid archive channel ID."))?;
                if !channels
                    .iter()
                    .any(|c| c["id"].as_str() == Some(id) && c["type"].as_u64() == Some(0))
                {
                    return Err(WebError::bad_request(
                        "Archive channels must be text channels on this server.",
                    ));
                }
                let n = id
                    .parse::<u64>()
                    .map_err(|_| WebError::bad_request("Invalid channel ID."))?;
                if !ids.contains(&n) {
                    ids.push(n);
                }
            }
            value = Value::Array(ids.into_iter().map(Value::from).collect());
        } else if key == "timezone" {
            let timezone = value.as_str().filter(|s| s.len() <= 80).ok_or_else(|| {
                WebError::bad_request("Use an IANA timezone such as Europe/Riga.")
            })?;
            if timezone.parse::<chrono_tz::Tz>().is_err() {
                return Err(WebError::bad_request(
                    "Use an IANA timezone such as Europe/Riga.",
                ));
            }
        } else if key == "moderate_warning_ladder" {
            let steps = value
                .as_array_mut()
                .filter(|a| !a.is_empty() && a.len() <= 10)
                .ok_or_else(|| WebError::bad_request("Set 1 to 10 warning steps."))?;
            let mut seen = std::collections::HashSet::new();
            for step in steps.iter() {
                let count = step["count"].as_i64();
                let action = step["action"].as_str();
                let seconds = step["seconds"].as_i64();
                if step.as_object().is_none_or(|o| o.len() != 3)
                    || count.is_none_or(|n| !(1..=100).contains(&n))
                    || !seen.insert(count.unwrap())
                    || !matches!(action, Some("timeout" | "ban"))
                    || seconds.is_none_or(|s| !(0..=31_536_000).contains(&s))
                    || (action == Some("timeout") && seconds == Some(0))
                {
                    return Err(WebError::bad_request("Invalid warning ladder step."));
                }
            }
            steps.sort_by_key(|s| s["count"].as_i64().unwrap_or(i64::MAX));
        } else if default.is_number() {
            let n = value
                .as_i64()
                .ok_or_else(|| WebError::bad_request(format!("{key} must be an integer.")))?;
            let (min, max) = match key.as_str() {
                "birthday_hour" | "weekly_hour" | "monthly_hour" => (0, 23),
                "weekly_day" => (0, 6),
                "monthly_day" => (1, 28),
                "moderate_min_account_age_hours" => (0, 8760),
                "moderate_raid_join_count" => (3, 100),
                "moderate_raid_window_seconds" => (5, 300),
                "jail_bail" => (0, 1_000_000),
                "daily_currency_cap" | "daily_reward" => (1, 1000),
                _ => (0, 1000),
            };
            if !(min..=max).contains(&n) {
                return Err(WebError::bad_request(format!(
                    "{key} must be between {min} and {max}."
                )));
            }
        } else if default.is_string() {
            let text = value
                .as_str()
                .filter(|v| v.chars().count() <= if key == "server_style" { 1000 } else { 32 })
                .ok_or_else(|| WebError::bad_request(format!("Invalid {key}.")))?;
            if text
                .chars()
                .any(|c| c.is_control() && c != '\n' && c != '\t')
            {
                return Err(WebError::bad_request(format!("Invalid {key}.")));
            }
        } else {
            return Err(WebError::bad_request(format!(
                "This setting is not editable: {key}"
            )));
        }
        output.insert(key.clone(), value);
    }
    Ok(output)
}

async fn update_settings(
    State(state): State<Arc<WebState>>,
    headers: HeaderMap,
    Path(guild_id): Path<String>,
    Json(body): Json<Value>,
) -> WebResult<Json<Value>> {
    check_origin(&headers, true)?;
    let claims = parse_claims(&headers)?;
    let csrf_value = headers
        .get("x-csrf-token")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if csrf_value != csrf(&claims)? {
        return Err(WebError::forbidden(
            "The form session expired. Reload the dashboard.",
        ));
    }
    let token = bearer_session(&state, &claims).await?;
    accessible_guild(&state, &token, &guild_id).await?;
    let raw = body
        .get("patch")
        .ok_or_else(|| WebError::bad_request("Only recognized server settings can be changed."))?;
    let patch = validate_settings(&state, &guild_id, raw).await?;
    let guild_num = guild_id
        .parse::<u64>()
        .map_err(|_| WebError::bad_request("Invalid server ID."))?;
    let settings = state
        .store
        .update_guild_settings(guild_num, &patch, &settings_defaults())
        .await
        .map_err(|_| WebError::unavailable("Settings storage is unavailable."))?;
    Ok(Json(json!({"settings":settings_json(settings)})))
}

async fn api_session(
    State(state): State<Arc<WebState>>,
    headers: HeaderMap,
) -> WebResult<Json<Value>> {
    check_origin(&headers, false)?;
    let claims = parse_claims(&headers)?;
    bearer_session(&state, &claims).await?;
    Ok(Json(
        json!({"user":{"id":claims.user_id},"csrf":csrf(&claims)?,"expires_at":claims.expires_at}),
    ))
}

async fn logout(State(state): State<Arc<WebState>>, headers: HeaderMap) -> WebResult<Response> {
    check_origin(&headers, true)?;
    let claims = parse_claims(&headers)?;
    if headers.get("x-csrf-token").and_then(|v| v.to_str().ok()) != Some(csrf(&claims)?.as_str()) {
        return Err(WebError::forbidden(
            "The form session expired. Reload the dashboard.",
        ));
    }
    state.sessions.write().await.remove(&claims.nonce);
    let mut response = Json(json!({"signed_out":true})).into_response();
    set_cookie(response.headers_mut(), "__Host-vivian_session", "", 0, "/")?;
    Ok(response)
}

async fn public_profile(
    State(state): State<Arc<WebState>>,
    headers: HeaderMap,
    Path((guild_id, user_id)): Path<(String, String)>,
) -> WebResult<Response> {
    let origin = headers.get(header::ORIGIN).and_then(|v| v.to_str().ok());
    let site = site_url()?;
    let dashboard = base_url()?;
    if origin.is_some_and(|v| v != site && v != dashboard) {
        return Err(WebError::forbidden("This origin is not allowed."));
    }
    if !valid_id(&guild_id) || !valid_id(&user_id) {
        return Err(WebError::new(
            StatusCode::NOT_FOUND,
            "Public profile not found.",
        ));
    }
    let guild_num = guild_id
        .parse::<u64>()
        .map_err(|_| WebError::new(StatusCode::NOT_FOUND, "Public profile not found."))?;
    let user_num = user_id
        .parse::<u64>()
        .map_err(|_| WebError::new(StatusCode::NOT_FOUND, "Public profile not found."))?;
    let settings = state
        .store
        .guild_settings(guild_num, &settings_defaults())
        .await
        .map_err(|_| WebError::unavailable("The service is temporarily unavailable."))?;
    let consent = state
        .store
        .public_profile_consent(guild_num, user_num)
        .await
        .map_err(|_| WebError::unavailable("The service is temporarily unavailable."))?;
    if settings["public_profiles_enabled"] != true || !consent.0 {
        return Err(WebError::new(
            StatusCode::NOT_FOUND,
            "Public profile not found.",
        ));
    }
    let guild = discord_bot_get(&state, &format!("/guilds/{guild_id}?with_counts=true")).await?;
    let member = discord_bot_get(&state, &format!("/guilds/{guild_id}/members/{user_id}"))
        .await
        .map_err(|_| WebError::new(StatusCode::NOT_FOUND, "Public profile not found."))?;
    if member["user"]["bot"] == true {
        return Err(WebError::new(
            StatusCode::NOT_FOUND,
            "Public profile not found.",
        ));
    }
    let display = member
        .get("nick")
        .and_then(Value::as_str)
        .or(member["user"]["global_name"].as_str())
        .or(member["user"]["username"].as_str())
        .unwrap_or("Member");
    let avatar = member["user"]["avatar"]
        .as_str()
        .map(|hash| format!("https://cdn.discordapp.com/avatars/{user_id}/{hash}.png?size=256"))
        .unwrap_or_else(|| "https://cdn.discordapp.com/embed/avatars/0.png".into());
    let (xp, voice, coins) = state
        .store
        .progress(guild_num, user_num)
        .await
        .map_err(|_| WebError::unavailable("The service is temporarily unavailable."))?;
    let (status_text, mood) = state
        .store
        .profile_status(guild_num, user_num)
        .await
        .map_err(|_| WebError::unavailable("The service is temporarily unavailable."))?;
    let messages: i64 = sqlx::query_scalar(
        "SELECT message_count FROM member_progress WHERE guild_id=? AND user_id=?",
    )
    .bind(&guild_id)
    .bind(&user_id)
    .fetch_optional(&state.store.pool)
    .await
    .map_err(|_| WebError::unavailable("The service is temporarily unavailable."))?
    .unwrap_or(0);
    let social_enabled = settings["social_enabled"] == true;
    let friend_ids = if social_enabled && consent.1 {
        state
            .store
            .friends(guild_num, user_num)
            .await
            .map_err(|_| WebError::unavailable("The service is temporarily unavailable."))?
    } else {
        Vec::new()
    };
    let mut friends = Vec::new();
    for friend_id in friend_ids.into_iter().take(12) {
        let Ok(friend_num) = friend_id.parse::<u64>() else {
            continue;
        };
        let visible = state
            .store
            .public_profile_consent(guild_num, friend_num)
            .await
            .unwrap_or((false, false, false));
        if !visible.0 || !visible.1 {
            continue;
        }
        if let Ok(friend) =
            discord_bot_get(&state, &format!("/guilds/{guild_id}/members/{friend_id}")).await
        {
            if friend["user"]["bot"] != true {
                let friend_name = friend
                    .get("nick")
                    .and_then(Value::as_str)
                    .or(friend["user"]["global_name"].as_str())
                    .or(friend["user"]["username"].as_str())
                    .unwrap_or("Member");
                friends.push(json!({"id":friend_id,"display_name":friend_name}));
            }
        }
    }
    let mut relationship = Value::Null;
    if social_enabled {
        if let Some(partner) = state
            .store
            .relationship(guild_num, user_num)
            .await
            .map_err(|_| WebError::unavailable("The service is temporarily unavailable."))?
        {
            let partner_id = partner.to_string();
            let partner_public = state
                .store
                .public_profile_consent(guild_num, partner)
                .await
                .unwrap_or((false, false, false));
            if partner_public.0 {
                if let Ok(partner_member) =
                    discord_bot_get(&state, &format!("/guilds/{guild_id}/members/{partner_id}"))
                        .await
                {
                    let partner_name = partner_member
                        .get("nick")
                        .and_then(Value::as_str)
                        .or(partner_member["user"]["global_name"].as_str())
                        .or(partner_member["user"]["username"].as_str())
                        .unwrap_or("Member");
                    relationship = json!({"id":partner_id,"display_name":partner_name});
                }
            }
        }
    }
    let mut wall = Vec::new();
    if social_enabled
        && consent.2
        && state
            .store
            .wall_privacy(guild_num, user_num)
            .await
            .unwrap_or_default()
            == "all"
    {
        if let Ok(posts) = state
            .store
            .wall_posts(guild_num, user_num, user_num, 0)
            .await
        {
            for (post_id, author_id, text, created_at) in posts.into_iter().take(3) {
                let Ok(author_num) = author_id.parse::<u64>() else {
                    continue;
                };
                let author_consent = state
                    .store
                    .public_profile_consent(guild_num, author_num)
                    .await
                    .unwrap_or((false, false, false));
                if !author_consent.0 || !author_consent.2 {
                    continue;
                }
                let Ok(author) =
                    discord_bot_get(&state, &format!("/guilds/{guild_id}/members/{author_id}"))
                        .await
                else {
                    continue;
                };
                let author_name = author
                    .get("nick")
                    .and_then(Value::as_str)
                    .or(author["user"]["global_name"].as_str())
                    .or(author["user"]["username"].as_str())
                    .unwrap_or("Member");
                let mut comments_out = Vec::new();
                if let Ok(comments) = state
                    .store
                    .wall_comments(guild_num, user_num, post_id, 0)
                    .await
                {
                    for (comment_author, comment_text, comment_at) in
                        comments.into_iter().rev().take(3)
                    {
                        let Ok(comment_num) = comment_author.parse::<u64>() else {
                            continue;
                        };
                        let share = state
                            .store
                            .public_profile_consent(guild_num, comment_num)
                            .await
                            .unwrap_or((false, false, false));
                        if !share.0 || !share.2 {
                            continue;
                        }
                        if let Ok(comment_member) = discord_bot_get(
                            &state,
                            &format!("/guilds/{guild_id}/members/{comment_author}"),
                        )
                        .await
                        {
                            let name = comment_member
                                .get("nick")
                                .and_then(Value::as_str)
                                .or(comment_member["user"]["global_name"].as_str())
                                .or(comment_member["user"]["username"].as_str())
                                .unwrap_or("Member");
                            comments_out.push(json!({"author":{"id":comment_author,"display_name":name},"text":comment_text,"created_at":comment_at}));
                        }
                    }
                }
                let likes: i64 = sqlx::query_scalar(
                    "SELECT count(*) FROM wall_likes WHERE guild_id=? AND post_id=?",
                )
                .bind(&guild_id)
                .bind(post_id)
                .fetch_one(&state.store.pool)
                .await
                .unwrap_or(0);
                wall.push(json!({"author":{"id":author_id,"display_name":author_name},"text":text,"created_at":created_at,"likes":likes,"comments":comments_out}));
            }
        }
    }
    let mut profile = json!({"id":user_id,"guild":guild_info(&guild),"display_name":display,"avatar_url":avatar,"level":xp/500+1,"total_xp":xp,
        "xp_into":xp%500,"xp_required":500,"messages":messages,"badges":[],"friend_count":friends.len(),"status_text":status_text,"mood":mood,
        "relationship":relationship,"wall":wall,"updated_at":chrono::Utc::now().timestamp(),
        "economy":if settings["economy_enabled"]==true{json!({"balance":coins,"currency":settings["currency_name"].as_str().unwrap_or("sparks"),"badges":[]})}else{Value::Null},
        "voice_seconds":voice});
    if !friends.is_empty() {
        profile["friends"] = Value::Array(friends);
    }
    let mut response = Json(profile).into_response();
    if origin == Some(site.as_str()) {
        response.headers_mut().insert(
            header::ACCESS_CONTROL_ALLOW_ORIGIN,
            HeaderValue::from_str(&site).unwrap(),
        );
        response
            .headers_mut()
            .insert(header::VARY, HeaderValue::from_static("Origin"));
    }
    Ok(response)
}

async fn public_options(headers: HeaderMap) -> Response {
    let mut response = Response::new(Body::empty());
    *response.status_mut() = StatusCode::NO_CONTENT;
    if let (Some(origin), Ok(site)) = (
        headers.get(header::ORIGIN).and_then(|v| v.to_str().ok()),
        site_url(),
    ) {
        if origin == site {
            response.headers_mut().insert(
                header::ACCESS_CONTROL_ALLOW_ORIGIN,
                HeaderValue::from_str(&site).unwrap(),
            );
            response
                .headers_mut()
                .insert(header::VARY, HeaderValue::from_static("Origin"));
            response.headers_mut().insert(
                header::ACCESS_CONTROL_ALLOW_METHODS,
                HeaderValue::from_static("GET, HEAD, OPTIONS"),
            );
        }
    }
    security_headers(response.headers_mut());
    response
}

async fn dashboard_page() -> Response {
    let html = include_str!("assets/dashboard-app.html")
        .replace("=\"styles.css\"", "=\"/dashboard/assets/styles.css\"")
        .replace("=\"theme.css\"", "=\"/dashboard/assets/theme.css\"")
        .replace("=\"dashboard.css\"", "=\"/dashboard/assets/dashboard.css\"")
        .replace("=\"config.js\"", "=\"/dashboard/assets/config.js\"")
        .replace("=\"dashboard.js\"", "=\"/dashboard/assets/dashboard.js\"");
    ([(header::CONTENT_TYPE, "text/html; charset=utf-8")], html).into_response()
}
async fn profile_page() -> Response {
    let html = include_str!("assets/profile.html")
        .replace("=\"styles.css\"", "=\"/dashboard/assets/styles.css\"")
        .replace("=\"theme.css\"", "=\"/dashboard/assets/theme.css\"")
        .replace("=\"dashboard.css\"", "=\"/dashboard/assets/dashboard.css\"")
        .replace("=\"config.js\"", "=\"/dashboard/assets/config.js\"")
        .replace("=\"profile.js\"", "=\"/dashboard/assets/profile.js\"");
    ([(header::CONTENT_TYPE, "text/html; charset=utf-8")], html).into_response()
}
async fn static_asset(Path(name): Path<String>) -> WebResult<Response> {
    let (body, mime) = match name.as_str() {
        "dashboard.css" => (
            include_str!("assets/dashboard.css"),
            "text/css; charset=utf-8",
        ),
        "styles.css" => (include_str!("assets/styles.css"), "text/css; charset=utf-8"),
        "theme.css" => (include_str!("assets/theme.css"), "text/css; charset=utf-8"),
        "dashboard.js" => (
            include_str!("assets/dashboard.js"),
            "text/javascript; charset=utf-8",
        ),
        "profile.js" => (
            include_str!("assets/profile.js"),
            "text/javascript; charset=utf-8",
        ),
        "config.js" => {
            let config = json!({"apiBase":env::var("DASHBOARD_BASE_URL").ok(),"publicSiteUrl":env::var("PUBLIC_SITE_URL").unwrap_or_else(|_|"https://esil.life".into())});
            let body = format!(
                "window.VIVIAN_CONFIG={};",
                serde_json::to_string(&config).unwrap_or_else(|_| "{}".into())
            );
            return Ok((
                [(header::CONTENT_TYPE, "text/javascript; charset=utf-8")],
                body,
            )
                .into_response());
        }
        _ => return Err(WebError::new(StatusCode::NOT_FOUND, "Asset not found.")),
    };
    Ok(([(header::CONTENT_TYPE, mime)], body).into_response())
}

async fn health(State(state): State<Arc<WebState>>) -> Response {
    let ready = state.discord_ready.load(Ordering::Relaxed);
    let storage_ready = sqlx::query("SELECT 1")
        .execute(&state.store.pool)
        .await
        .is_ok();
    let status = if ready && storage_ready {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (
        status,
        Json(json!({
            "discord_ready":ready,
            "revision":env::var("DEPLOY_REVISION").unwrap_or_default(),
            "chat_style":"rust",
            "storage":{"ready":storage_ready,"backend":"sqlite"},
            "github_updates":{"state":"not_ported","last_commit":null},
            "community":{"state":"not_ported","storage_ready":false},
            "private_rooms":{"state":if ready{"ready"}else{"starting"},"storage_ready":storage_ready,"pending_recovery":null},
            "features_ready":false,
            "ai_memory_ready":false,
            "web_dashboard":{"ready":true},
            "bot_id":state.bot_id
        })),
    )
        .into_response()
}

async fn root() -> &'static str {
    "Vivian is running."
}

pub async fn start(
    config: &Config,
    store: Store,
    http: reqwest::Client,
    bot_id: u64,
    discord_ready: Arc<AtomicBool>,
) -> anyhow::Result<tokio::task::JoinHandle<()>> {
    let port = env::var("PORT")
        .ok()
        .and_then(|v| v.parse::<u16>().ok())
        .unwrap_or(10000);
    let bind = env::var("WEB_BIND").unwrap_or_else(|_| "0.0.0.0".into());
    let listener = tokio::net::TcpListener::bind((bind.as_str(), port)).await?;
    let state = Arc::new(WebState {
        store,
        http,
        bot_token: config.discord_token.clone(),
        bot_id,
        discord_ready,
        sessions: Arc::new(RwLock::new(HashMap::new())),
        request_limits: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
    });
    let app = Router::new()
        .route("/", get(root))
        .route("/healthz", get(health))
        .route("/dashboard", get(dashboard_page))
        .route("/dashboard/", get(dashboard_page))
        .route("/dashboard/assets/{name}", get(static_asset))
        .route("/profile/{guild_id}/{user_id}", get(profile_page))
        .route("/auth/login", get(oauth_login))
        .route("/auth/callback", get(oauth_callback))
        .route("/auth/logout", post(logout))
        .route("/api/session", get(api_session))
        .route("/api/guilds", get(guild_list))
        .route("/api/guilds/{guild_id}", get(guild_details))
        .route("/api/guilds/{guild_id}/settings", post(update_settings))
        .route(
            "/api/public/guilds/{guild_id}/members/{user_id}",
            get(public_profile).options(public_options),
        )
        .layer(DefaultBodyLimit::max(32_768))
        .layer(middleware::from_fn_with_state(state.clone(), no_cache))
        .with_state(state);
    info!(%bind,port,"Rust dashboard/API listening");
    Ok(tokio::spawn(async move {
        if let Err(error) = axum::serve(
            listener,
            app.into_make_service_with_connect_info::<SocketAddr>(),
        )
        .await
        {
            warn!(%error,"dashboard/API server stopped");
        }
    }))
}
