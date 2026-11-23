use super::Context;

#[poise::command(slash_command, prefix_command, aliases("помощь"))]
pub async fn help(ctx: Context<'_>) -> anyhow::Result<()> {
    crate::ui::say(
        ctx,
        "**Vivian · команды** ✨\n\
        **Основное:** `/about`, `/help`, `/ping`, `/birthday`.\n\
        **ИИ:** `/ask`, `/ai`, `/forget`.\n\
        **Музыка:** `/play`, `/search`, `/queue`, `/nowplaying`, `/skip`, `/pause`, `/resume`, `/stop`, `/volume`, `/loop`, `/shuffle`, `/seek`, `/filter`, `/remove`, `/clear`, `/join`, `/leave`, плейлисты.\n\
        **Профиль и игры:** `/balance`, `/daily`, `/work`, `/pay`, `/leaderboard`, `/profile`, `/rank`, `/inventory`, `/status`, друзья, пары и игры.\n\
        **Приватные комнаты:** `/room_create`, `/room_setup`, `/room_manage` и команды управления комнатой. Для настройки `/room_setup` тебе нужно **Manage Server**. Роли Vivian нужны **Manage Channels** и **Manage Roles**, чтобы создавать комнаты.\n\
        **Модерация — право проверяется у запускающего модератора:** `Moderate Members` — `/warn`, `/unwarn`, `/warnings`, `/timeout`, `/untimeout`, `/modlog`; `Kick Members` — `/kick`; `Ban Members` — `/ban`, `/unban`; `Manage Messages` — `/clear`; `Manage Channels` — `/slowmode`, `/lock`, `/unlock`.\n\
        Для выполнения мод-действия Vivian тоже нужно соответствующее право у роли бота: **Moderate Members**, **Kick Members**, **Ban Members**, **Manage Messages** или **Manage Channels**. На ступенях предупреждений 3/5 Vivian нужен `Moderate Members`, на 7 — `Ban Members`. `/modlog` читает журнал Vivian и не требует `View Audit Log`.\n\
        Командам не нужен `Administrator`. Роли и права бота настраиваются отдельно от прав участника. Для большинства команд работает `!`. Разработчик: **yoki**. Подробности: `/about`.",
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn about(ctx: Context<'_>) -> anyhow::Result<()> {
    crate::ui::say(
        ctx,
        "**Привет, я Vivian!** ✨\n\
        Я помогаю сделать Discord-сервер уютнее: могу поддержать разговор с помощью ИИ, включить музыку, показать профиль и помочь с приватными голосовыми комнатами.\n\
        🧑‍💻 **Разработчик:** yoki\n\
        🦀 **Написана на Rust**\n\
        💬 Начать общение: `/ask` · посмотреть команды: `/help`\n\
        Я ещё расту и учусь быть полезнее — если что-то работает не так, сообщи администратору сервера.",
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn ping(ctx: Context<'_>) -> anyhow::Result<()> {
    crate::ui::say(ctx, "Pong! Vivian на связи. 💜").await?;
    Ok(())
}
