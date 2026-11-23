//! Shared Discord Components V2 replies and small animated Vivian panels.

use anyhow::{anyhow, Context as _, Result};
use gif::{Encoder, Frame, Repeat};
use poise::{serenity_prelude as serenity, Context};
use reqwest::Response;
use serde_json::{json, Value};
use std::{borrow::Cow, collections::HashMap, sync::OnceLock};

const WIDTH: u16 = 480;
const HEIGHT: u16 = 104;
const GIF_FILENAME: &str = "vivian-panel.gif";
const COMPONENTS_V2_FLAG: u64 = 1 << 15;
const EPHEMERAL_FLAG: u64 = 1 << 6;
const PALETTE: [u8; 24] = [
    13, 10, 19, 31, 22, 42, 56, 38, 75, 88, 54, 112, 128, 74, 159, 176, 104, 208, 220, 158, 241,
    250, 238, 255,
];

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum PanelStyle {
    System,
    Music,
    Player,
    Paused,
    Idle,
    Activity,
    Profile,
}

impl PanelStyle {
    fn from_context(ctx: Context<'_, crate::Data, anyhow::Error>) -> Self {
        let command = ctx.command().name.as_ref();
        match command {
            "play" | "search" | "playlist_play" | "playlist_save" => Self::Music,
            "queue" | "nowplaying" | "skip" | "resume" | "volume" | "loop" | "shuffle" | "seek"
            | "filter" => Self::Player,
            "pause" => Self::Paused,
            "stop" | "leave" => Self::Idle,
            "profile" | "rank" | "inventory" | "balance" => Self::Profile,
            "relationship" | "friend_list" | "friend_requests" | "crush_show" => Self::Activity,
            _ => Self::System,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::System => "SYSTEM / SIGNAL",
            Self::Music => "MUSIC / DISCOVERY",
            Self::Player => "AUDIO / PLAYING",
            Self::Paused => "AUDIO / PAUSED",
            Self::Idle => "AUDIO / STANDBY",
            Self::Activity => "COMMUNITY / ACTIVITY",
            Self::Profile => "MEMBER / PROFILE",
        }
    }

    fn accent(self) -> u32 {
        match self {
            Self::System => 0xd58bff,
            Self::Music => 0xff75c8,
            Self::Player => 0x79e2c0,
            Self::Paused => 0xffcb70,
            Self::Idle => 0x9aa0af,
            Self::Activity => 0x83b8ff,
            Self::Profile => 0xc69bff,
        }
    }
}

static GIF_CACHE: OnceLock<HashMap<PanelStyle, Vec<u8>>> = OnceLock::new();

fn gif_for(style: PanelStyle) -> Option<&'static [u8]> {
    GIF_CACHE
        .get_or_init(|| {
            [
                PanelStyle::System,
                PanelStyle::Music,
                PanelStyle::Player,
                PanelStyle::Paused,
                PanelStyle::Idle,
                PanelStyle::Activity,
                PanelStyle::Profile,
            ]
            .into_iter()
            .filter_map(|style| encode_gif(style).ok().map(|gif| (style, gif)))
            .collect()
        })
        .get(&style)
        .map(Vec::as_slice)
}

fn encode_gif(style: PanelStyle) -> std::result::Result<Vec<u8>, gif::EncodingError> {
    let mut output = Vec::new();
    {
        let mut encoder = Encoder::new(&mut output, WIDTH, HEIGHT, &PALETTE)?;
        encoder.set_repeat(Repeat::Infinite)?;
        for phase in 0..20 {
            let pixels = render_frame(style, phase);
            let mut frame = Frame::default();
            frame.width = WIDTH;
            frame.height = HEIGHT;
            frame.delay = 8;
            frame.buffer = Cow::Owned(pixels);
            encoder.write_frame(&frame)?;
        }
    }
    Ok(output)
}

fn render_frame(style: PanelStyle, phase: usize) -> Vec<u8> {
    let width = WIDTH as isize;
    let height = HEIGHT as isize;
    let mut pixels = vec![0; WIDTH as usize * HEIGHT as usize];
    fill_rect(&mut pixels, 1, 1, width - 2, height - 2, 1);
    horizontal_line(&mut pixels, 2, width - 3, 2, 3);
    horizontal_line(&mut pixels, 2, width - 3, height - 3, 2);
    vertical_line(&mut pixels, 2, height - 3, 2, 3);
    vertical_line(&mut pixels, 2, height - 3, width - 3, 2);
    fill_rect(&mut pixels, 20, 17, 4, 70, 5);
    draw_text(&mut pixels, 40, 27, "VIVIAN", 3, 7);
    draw_text(&mut pixels, 41, 63, style.label(), 1, 5);
    draw_text(&mut pixels, 41, 82, "LIVE / RESPONSE", 1, 4);
    vertical_line(&mut pixels, 22, height - 21, 286, 2);

    match style {
        PanelStyle::Profile => {
            for radius in [15, 27, 39] {
                circle(&mut pixels, 384, 55, radius, 2);
            }
            for arm in 0..4 {
                let angle = phase as f32 * 0.22 + arm as f32 * std::f32::consts::FRAC_PI_2;
                let x = 384 + (30.0 * angle.cos()) as isize;
                let y = 55 + (30.0 * angle.sin()) as isize;
                fill_rect(&mut pixels, x - 2, y - 2, 5, 5, 6);
            }
            fill_rect(&mut pixels, 381, 52, 7, 7, 7);
        }
        PanelStyle::Activity => {
            horizontal_line(&mut pixels, 315, 454, 56, 2);
            for column in 0isize..34 {
                let angle = column as f32 * 0.39 + phase as f32 * 0.32;
                let y = 56 + (22.0 * angle.sin()) as isize;
                fill_rect(
                    &mut pixels,
                    318 + column * 4,
                    y - 2,
                    3,
                    5,
                    if column % 5 == (phase % 5) as isize {
                        6
                    } else {
                        4
                    },
                );
            }
        }
        _ => {
            horizontal_line(&mut pixels, 314, 455, 55, 2);
            for column in 0isize..35 {
                let angle = column as f32 * 0.31 + phase as f32 * 0.3;
                let sway = (column as f32 * 0.09 - phase as f32 * 0.12).sin().abs();
                let amplitude = match style {
                    PanelStyle::Idle => 4.0,
                    PanelStyle::Paused => 7.0,
                    PanelStyle::Player | PanelStyle::Music => 23.0,
                    _ => 16.0,
                };
                let half_height =
                    (3.0 + amplitude * angle.sin().abs() * (0.45 + 0.55 * sway)) as isize;
                let x = 316 + column * 4;
                let color = if (column + phase as isize * 2) % 17 < 4 {
                    6
                } else {
                    4
                };
                fill_rect(
                    &mut pixels,
                    x,
                    55 - half_height,
                    2,
                    half_height * 2 + 1,
                    color,
                );
            }
            if matches!(style, PanelStyle::Music | PanelStyle::System) {
                let sweep = 310 + ((phase * 7) % 144) as isize;
                vertical_line(&mut pixels, 24, 83, sweep, 3);
            }
        }
    }
    for dot in 0..5 {
        fill_rect(
            &mut pixels,
            319 + dot * 19,
            91,
            4,
            3,
            if dot == (phase % 5) as isize { 6 } else { 3 },
        );
    }
    pixels
}

fn fill_rect(pixels: &mut [u8], x: isize, y: isize, width: isize, height: isize, color: u8) {
    for py in y.max(0)..(y + height).min(HEIGHT as isize) {
        for px in x.max(0)..(x + width).min(WIDTH as isize) {
            pixels[py as usize * WIDTH as usize + px as usize] = color;
        }
    }
}

fn horizontal_line(pixels: &mut [u8], x1: isize, x2: isize, y: isize, color: u8) {
    fill_rect(pixels, x1, y, x2 - x1 + 1, 1, color);
}

fn vertical_line(pixels: &mut [u8], y1: isize, y2: isize, x: isize, color: u8) {
    fill_rect(pixels, x, y1, 1, y2 - y1 + 1, color);
}

fn circle(pixels: &mut [u8], cx: isize, cy: isize, radius: isize, color: u8) {
    for angle in 0..360 {
        let radians = (angle as f32).to_radians();
        let x = cx + (radius as f32 * radians.cos()) as isize;
        let y = cy + (radius as f32 * radians.sin()) as isize;
        fill_rect(pixels, x, y, 2, 2, color);
    }
}

fn draw_text(pixels: &mut [u8], x: isize, y: isize, text: &str, scale: isize, color: u8) {
    let mut cursor = x;
    for character in text.chars() {
        if character == ' ' {
            cursor += 4 * scale;
            continue;
        }
        let glyph = glyph(character);
        for (row, bits) in glyph.iter().enumerate() {
            for column in 0..5 {
                if bits & (1 << (4 - column)) != 0 {
                    fill_rect(
                        pixels,
                        cursor + column * scale,
                        y + row as isize * scale,
                        scale,
                        scale,
                        color,
                    );
                }
            }
        }
        cursor += 6 * scale;
    }
}

fn glyph(character: char) -> [u8; 7] {
    match character {
        'A' => [14, 17, 17, 31, 17, 17, 17],
        'C' => [14, 17, 16, 16, 16, 17, 14],
        'D' => [30, 17, 17, 17, 17, 17, 30],
        'E' => [31, 16, 16, 30, 16, 16, 31],
        'I' => [31, 4, 4, 4, 4, 4, 31],
        'L' => [16, 16, 16, 16, 16, 16, 31],
        'M' => [17, 27, 21, 21, 17, 17, 17],
        'N' => [17, 25, 25, 21, 19, 19, 17],
        'O' => [14, 17, 17, 17, 17, 17, 14],
        'P' => [30, 17, 17, 30, 16, 16, 16],
        'R' => [30, 17, 17, 30, 20, 18, 17],
        'S' => [15, 16, 16, 14, 1, 1, 30],
        'T' => [31, 4, 4, 4, 4, 4, 4],
        'U' => [17, 17, 17, 17, 17, 17, 14],
        'V' => [17, 17, 17, 17, 17, 10, 4],
        _ => [0; 7],
    }
}

fn text_components(content: &str, style: PanelStyle, controls: &[Value]) -> Vec<Value> {
    let mut container = vec![json!({
        "type": 10,
        "content": format!("## Vivian · {}\n\n{}", style.label(), content)
    })];
    container.push(json!({"type":14,"divider":true,"spacing":1}));
    container.push(json!({
        "type":12,
        "items":[{"media":{"url":format!("attachment://{GIF_FILENAME}")},"description":style.label(),"spoiler":false}]
    }));
    container.extend_from_slice(controls);
    vec![json!({"type":17,"accent_color":style.accent(),"components":container})]
}

fn message_payload(content: &str, ephemeral: bool, style: PanelStyle, controls: &[Value]) -> Value {
    let mut flags = COMPONENTS_V2_FLAG;
    if ephemeral {
        flags |= EPHEMERAL_FLAG;
    }
    json!({
        "flags":flags,
        "components":text_components(content,style,controls),
        "attachments":[{"id":0,"filename":GIF_FILENAME}],
        "allowed_mentions":{"parse":[]}
    })
}

fn update_payload(content: &str, style: PanelStyle, controls: &[Value]) -> Value {
    json!({
        "flags":COMPONENTS_V2_FLAG,
        "components":text_components(content,style,controls),
        "allowed_mentions":{"parse":[]}
    })
}

async fn request_for_style(
    ctx: &serenity::Context,
    route: serenity::http::Route<'_>,
    method: serenity::http::LightMethod,
    payload: Value,
    style: PanelStyle,
    params: Option<Vec<(&'static str, String)>>,
) -> Result<Response> {
    let payload_json = serde_json::to_string(&payload)?;
    let mut request = serenity::http::Request::new(route, method)
        .body(Some(payload_json.as_bytes().to_vec()))
        .params(params);
    if let Some(bytes) = gif_for(style) {
        request = request.multipart(Some(serenity::http::Multipart {
            upload: serenity::http::MultipartUpload::Attachments(vec![
                serenity::CreateAttachment::bytes(bytes.to_vec(), GIF_FILENAME),
            ]),
            fields: Vec::new(),
            payload_json: Some(payload_json),
        }));
    }
    Ok(ctx.http.request(request).await?)
}

pub async fn channel_message(
    context: &serenity::Context,
    channel_id: serenity::ChannelId,
    content: impl AsRef<str>,
) -> Result<()> {
    let payload = message_payload(content.as_ref(), false, PanelStyle::System, &[]);
    request_for_style(
        context,
        serenity::http::Route::ChannelMessages { channel_id },
        serenity::http::LightMethod::Post,
        payload,
        PanelStyle::System,
        None,
    )
    .await?;
    Ok(())
}

async fn response_message_id(response: Response) -> Result<serenity::MessageId> {
    let body: Value = response
        .json()
        .await
        .context("Discord did not return the V2 message")?;
    let raw_id = body
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("Discord response has no message ID"))?;
    Ok(serenity::MessageId::new(raw_id.parse()?))
}

async fn send(
    ctx: Context<'_, crate::Data, anyhow::Error>,
    content: &str,
    ephemeral: bool,
    controls: &[Value],
    need_message_id: bool,
) -> Result<Option<serenity::MessageId>> {
    let style = PanelStyle::from_context(ctx);
    match ctx {
        Context::Prefix(prefix) => {
            // Regular channel messages cannot carry Discord's EPHEMERAL flag.
            let payload = message_payload(content, false, style, controls);
            let response = request_for_style(
                prefix.serenity_context(),
                serenity::http::Route::ChannelMessages {
                    channel_id: prefix.msg.channel_id,
                },
                serenity::http::LightMethod::Post,
                payload,
                style,
                None,
            )
            .await?;
            if need_message_id {
                Ok(Some(response_message_id(response).await?))
            } else {
                Ok(None)
            }
        }
        Context::Application(application) => {
            let payload = message_payload(content, ephemeral, style, controls);
            let interaction = application.interaction;
            let was_sent = application
                .has_sent_initial_response
                .load(std::sync::atomic::Ordering::SeqCst);
            if !was_sent {
                request_for_style(
                    application.framework.serenity_context,
                    serenity::http::Route::InteractionResponse {
                        interaction_id: interaction.id,
                        token: &interaction.token,
                    },
                    serenity::http::LightMethod::Post,
                    json!({"type":4,"data":payload}),
                    style,
                    None,
                )
                .await?;
                application
                    .has_sent_initial_response
                    .store(true, std::sync::atomic::Ordering::SeqCst);
                if need_message_id {
                    let response = application
                        .framework
                        .serenity_context
                        .http
                        .request(serenity::http::Request::new(
                            serenity::http::Route::WebhookOriginalInteractionResponse {
                                application_id: interaction.application_id,
                                token: &interaction.token,
                            },
                            serenity::http::LightMethod::Get,
                        ))
                        .await?;
                    Ok(Some(response_message_id(response).await?))
                } else {
                    Ok(None)
                }
            } else {
                let response = request_for_style(
                    application.framework.serenity_context,
                    serenity::http::Route::WebhookFollowupMessages {
                        application_id: interaction.application_id,
                        token: &interaction.token,
                    },
                    serenity::http::LightMethod::Post,
                    payload,
                    style,
                    Some(vec![("wait", "true".into())]),
                )
                .await?;
                if need_message_id {
                    Ok(Some(response_message_id(response).await?))
                } else {
                    Ok(None)
                }
            }
        }
    }
}

pub async fn say(
    ctx: Context<'_, crate::Data, anyhow::Error>,
    text: impl AsRef<str>,
) -> Result<()> {
    let mut remaining = text.as_ref().trim();
    while !remaining.is_empty() {
        let text = remaining.chars().take(1800).collect::<String>();
        let length = text.len();
        send(ctx, &text, false, &[], false).await?;
        remaining = remaining.get(length..).unwrap_or_default().trim_start();
    }
    Ok(())
}

pub async fn private(
    ctx: Context<'_, crate::Data, anyhow::Error>,
    text: impl AsRef<str>,
) -> Result<()> {
    send(ctx, text.as_ref(), true, &[], false).await?;
    Ok(())
}

pub async fn with_components(
    ctx: Context<'_, crate::Data, anyhow::Error>,
    text: &str,
    ephemeral: bool,
    controls: Vec<Value>,
) -> Result<serenity::MessageId> {
    send(ctx, text, ephemeral, &controls, true)
        .await?
        .ok_or_else(|| anyhow!("Discord did not return a message ID"))
}

pub async fn component_reply(
    interaction: &serenity::ComponentInteraction,
    text: &str,
    ephemeral: bool,
    context: &serenity::Context,
) -> Result<()> {
    let style = PanelStyle::Music;
    request_for_style(
        context,
        serenity::http::Route::InteractionResponse {
            interaction_id: interaction.id,
            token: &interaction.token,
        },
        serenity::http::LightMethod::Post,
        json!({"type":4,"data":message_payload(text,ephemeral,style,&[])}),
        style,
        None,
    )
    .await?;
    Ok(())
}

pub async fn component_update(
    interaction: &serenity::ComponentInteraction,
    text: &str,
    controls: &[Value],
    context: &serenity::Context,
) -> Result<()> {
    let payload = json!({"type":7,"data":update_payload(text,PanelStyle::Music,controls)});
    let payload_json = serde_json::to_string(&payload)?;
    context
        .http
        .request(
            serenity::http::Request::new(
                serenity::http::Route::InteractionResponse {
                    interaction_id: interaction.id,
                    token: &interaction.token,
                },
                serenity::http::LightMethod::Post,
            )
            .body(Some(payload_json.into_bytes())),
        )
        .await?;
    Ok(())
}
