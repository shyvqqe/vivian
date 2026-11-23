use super::{guild_key, Context};
use anyhow::Result;
use chrono::{Datelike, Utc};
use rand::Rng;

#[poise::command(slash_command, prefix_command, aliases("bal"))]
pub async fn balance(
    ctx: Context<'_>,
    #[description = "Оставь пустым, чтобы проверить себя"] user: Option<
        poise::serenity_prelude::User,
    >,
) -> Result<()> {
    let user = user.unwrap_or_else(|| ctx.author().clone());
    let coins = ctx
        .data()
        .store
        .balance(guild_key(ctx), user.id.get())
        .await?;
    crate::ui::say(
        ctx,
        format!("У **{}**: **{coins}** Vivian coins.", user.name),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn daily(ctx: Context<'_>) -> Result<()> {
    let reward = 25;
    let today = Utc::now()
        .with_timezone(&chrono_tz::Europe::Riga)
        .date_naive()
        .format("%Y-%m-%d")
        .to_string();
    let claimed = ctx
        .data()
        .store
        .claim_daily(guild_key(ctx), ctx.author().id.get(), &today, reward)
        .await?;
    if claimed {
        crate::ui::say(
            ctx,
            format!("Ежедневная награда: **{reward}** Vivian coins. Возвращайся завтра."),
        )
        .await?;
    } else {
        crate::ui::say(ctx, "Ежедневная награда уже получена. Попробуй завтра.").await?;
    }
    Ok(())
}

#[poise::command(slash_command, prefix_command, aliases("pay", "transfer"))]
pub async fn pay(
    ctx: Context<'_>,
    #[description = "Получатель перевода"] user: poise::serenity_prelude::User,
    #[description = "Сумма"] amount: i64,
) -> Result<()> {
    ctx.data()
        .store
        .transfer(guild_key(ctx), ctx.author().id.get(), user.id.get(), amount)
        .await?;
    crate::ui::say(
        ctx,
        format!(
            "Переведено **{amount}** Vivian coins пользователю <@{}>.",
            user.id.get()
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn work(ctx: Context<'_>) -> Result<()> {
    let reward = rand::rng().random_range(25..=100);
    let now = Utc::now().timestamp();
    let claimed = ctx
        .data()
        .store
        .claim_work(guild_key(ctx), ctx.author().id.get(), now, 3600, reward)
        .await?;
    if claimed {
        crate::ui::say(
            ctx,
            format!(
                "Смена завершена. Заработано **{reward}** Vivian coins. Следующая смена через час."
            ),
        )
        .await?;
    } else {
        crate::ui::say(
            ctx,
            "Ты уже работал(а) недавно. Попробуй ещё раз через час.",
        )
        .await?;
    }
    Ok(())
}

#[poise::command(slash_command, prefix_command, aliases("top"))]
pub async fn leaderboard(ctx: Context<'_>) -> Result<()> {
    let rows = ctx.data().store.leaderboard(guild_key(ctx)).await?;
    if rows.is_empty() {
        crate::ui::say(ctx, "Пока нет записей в таблице Vivian coins.").await?;
        return Ok(());
    }
    let table = rows
        .into_iter()
        .enumerate()
        .map(|(i, (user, coins))| format!("{}. <@{user}> — **{coins}**", i + 1))
        .collect::<Vec<_>>()
        .join("\n");
    crate::ui::say(ctx, format!("**Топ Vivian coins**\n{table}")).await?;
    Ok(())
}

#[poise::command(
    slash_command,
    prefix_command,
    guild_only,
    subcommands("publish", "sharing", "link")
)]
pub async fn profile(
    ctx: Context<'_>,
    #[description = "Участник (по умолчанию ты)"] user: Option<poise::serenity_prelude::User>,
) -> Result<()> {
    let user = user.unwrap_or_else(|| ctx.author().clone());
    let (xp, voice, coins) = ctx
        .data()
        .store
        .progress(guild_key(ctx), user.id.get())
        .await?;
    let (status, mood) = ctx
        .data()
        .store
        .profile_status(guild_key(ctx), user.id.get())
        .await?;
    let level = xp / 500 + 1;
    let relation = ctx
        .data()
        .store
        .relationship(guild_key(ctx), user.id.get())
        .await?;
    let partner = relation
        .map(|id| format!("\nПара: <@{id}> "))
        .unwrap_or_default();
    let status = if status.is_empty() {
        "не задан".to_owned()
    } else {
        status
    };
    let mood = if mood.is_empty() {
        "—".to_owned()
    } else {
        mood
    };
    crate::ui::say(ctx, format!("**Профиль {}**\nУровень **{level}** · XP **{xp}/{}**\nБаланс: **{coins}** Vivian coins\nГолосовая активность: **{} мин.**\nСтатус: {} · Настроение: {}{partner}",user.name,(level*500),voice/60,status,mood)).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, guild_only)]
pub async fn publish(
    ctx: Context<'_>,
    #[description = "Публиковать профиль на сайте"] enabled: bool,
) -> Result<()> {
    let guild = guild_key(ctx);
    let user = ctx.author().id.get();
    let sharing = ctx.data().store.public_profile_consent(guild, user).await?;
    ctx.data()
        .store
        .set_public_profile_consent(
            guild,
            user,
            enabled,
            enabled && sharing.1,
            enabled && sharing.2,
        )
        .await?;
    crate::ui::say(ctx, if enabled {
        "Профиль опубликован, если администратор включил public profiles. Для друзей и стены настрой `/profile sharing`."
    } else {
        "Профиль скрыт; публикация друзей и стены отключена."
    }).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, guild_only)]
pub async fn sharing(
    ctx: Context<'_>,
    #[description = "Показывать друзей, которые тоже включили публикацию"] friends: Option<bool>,
    #[description = "Показывать публикации публичной стены"] wall: Option<bool>,
) -> Result<()> {
    let guild = guild_key(ctx);
    let user = ctx.author().id.get();
    let current = ctx.data().store.public_profile_consent(guild, user).await?;
    let (share_friends, share_wall) = match (friends, wall) {
        (None, None) => (current.1, current.2),
        (friends, wall) => {
            anyhow::ensure!(
                current.0 || (friends != Some(true) && wall != Some(true)),
                "Сначала включи `/profile publish`."
            );
            let next_friends = friends.unwrap_or(current.1);
            let next_wall = wall.unwrap_or(current.2);
            ctx.data()
                .store
                .set_public_profile_consent(guild, user, current.0, next_friends, next_wall)
                .await?;
            (next_friends, next_wall)
        }
    };
    crate::ui::say(
        ctx,
        format!(
            "Публикация друзей: **{}**. Публикация стены: **{}**.",
            if share_friends {
                "включена"
            } else {
                "выключена"
            },
            if share_wall {
                "включена"
            } else {
                "выключена"
            }
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, guild_only)]
pub async fn link(ctx: Context<'_>) -> Result<()> {
    let guild = guild_key(ctx);
    let user = ctx.author().id.get();
    let settings = ctx
        .data()
        .store
        .guild_settings(guild, &crate::web::settings_defaults())
        .await?;
    let consent = ctx.data().store.public_profile_consent(guild, user).await?;
    anyhow::ensure!(settings["public_profiles_enabled"] == true && consent.0, "Профили скрыты. Включи `/profile publish` и попроси администратора включить public profiles.");
    let base = std::env::var("DASHBOARD_BASE_URL")
        .or_else(|_| std::env::var("RENDER_EXTERNAL_URL"))
        .unwrap_or_else(|_| "https://esil.life".into());
    crate::ui::say(
        ctx,
        format!("{}/profile/{guild}/{user}", base.trim_end_matches('/')),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command, aliases("xp"))]
pub async fn rank(
    ctx: Context<'_>,
    #[description = "Страница таблицы (начиная с 1)"] page: Option<u16>,
) -> Result<()> {
    let page = page.unwrap_or(1).clamp(1, 1000);
    let rows = ctx
        .data()
        .store
        .xp_leaderboard(guild_key(ctx), i64::from(page - 1) * 10)
        .await?;
    if rows.is_empty() {
        crate::ui::say(ctx, "Пока нет участников с XP.").await?;
        return Ok(());
    }
    let lines = rows
        .into_iter()
        .enumerate()
        .map(|(i, (user, xp))| {
            format!(
                "{}. <@{user}> — **{xp} XP**, уровень **{}**",
                (page as usize - 1) * 10 + i + 1,
                xp / 500 + 1
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    crate::ui::say(
        ctx,
        format!("**Таблица уровней · страница {page}**\n{lines}"),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn inventory(
    ctx: Context<'_>,
    #[description = "Участник (по умолчанию ты)"] user: Option<poise::serenity_prelude::User>,
) -> Result<()> {
    let user = user.unwrap_or_else(|| ctx.author().clone());
    let items = ctx
        .data()
        .store
        .inventory(guild_key(ctx), user.id.get())
        .await?;
    if items.is_empty() {
        crate::ui::say(ctx, "Инвентарь пуст.").await?;
        return Ok(());
    }
    let lines = items
        .into_iter()
        .map(|(key, name, role)| format!("`{key}` · {name} · роль `{role}`"))
        .collect::<Vec<_>>()
        .join("\n");
    crate::ui::say(ctx, format!("**Инвентарь {}**\n{lines}", user.name)).await?;
    Ok(())
}

async fn require_economy_manager(ctx: Context<'_>) -> Result<bool> {
    use poise::serenity_prelude as serenity;
    if ctx.guild_id().is_none() {
        crate::ui::say(ctx, "Команда доступна только на сервере.").await?;
        return Ok(false);
    }
    let Some(member) = ctx.author_member().await else {
        crate::ui::say(ctx, "Не удалось проверить права.").await?;
        return Ok(false);
    };
    let Some(channel) = ctx.guild_channel().await else {
        crate::ui::say(ctx, "Не удалось проверить права канала.").await?;
        return Ok(false);
    };
    let has_manage_guild = ctx
        .guild()
        .map(|guild| {
            guild
                .user_permissions_in(&channel, &member)
                .contains(serenity::Permissions::MANAGE_GUILD)
        })
        .unwrap_or(false);
    if !has_manage_guild {
        crate::ui::say(
            ctx,
            "Для настройки каталога тебе нужно право **Manage Server (Управлять сервером)** на твоей роли. Это право проверяется у человека, а не у роли Vivian.",
        )
        .await?;
        return Ok(false);
    }
    Ok(true)
}

#[poise::command(slash_command, prefix_command)]
pub async fn shop(ctx: Context<'_>) -> Result<()> {
    let items = ctx.data().store.shop_items(guild_key(ctx)).await?;
    if items.is_empty() {
        crate::ui::say(
            ctx,
            "Магазин пуст. Администратор может добавить косметические роли через `!shop_add`.",
        )
        .await?;
        return Ok(());
    }
    let lines = items
        .into_iter()
        .map(|(key, name, price, _, description)| {
            format!(
                "`{key}` · **{name}** — {price} coins{}",
                if description.is_empty() {
                    String::new()
                } else {
                    format!(" · {description}")
                }
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    crate::ui::say(
        ctx,
        format!("**Магазин ролей**\n{lines}\nКупить: `/shop_buy item_id`."),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn shop_add(
    ctx: Context<'_>,
    #[description = "ID товара: a-z, цифры, _ и -"] key: String,
    #[description = "Название"] name: String,
    #[description = "Цена"] price: i64,
    #[description = "Существующая косметическая роль"] role: poise::serenity_prelude::Role,
    #[description = "Описание"] description: Option<String>,
) -> Result<()> {
    if !require_economy_manager(ctx).await? {
        return Ok(());
    }
    anyhow::ensure!(
        !role.managed && role.permissions.is_empty(),
        "Магазин принимает только самостоятельные роли без разрешений."
    );
    ctx.data()
        .store
        .add_shop_item(
            guild_key(ctx),
            &key,
            &name,
            price,
            role.id.get(),
            description.as_deref().unwrap_or(""),
        )
        .await?;
    crate::ui::say(
        ctx,
        format!(
            "Товар `{key}` добавлен: **{name}** за {price} coins; роль <@&{}>.",
            role.id.get()
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn shop_remove(
    ctx: Context<'_>, #[description = "ID товара"] key: String
) -> Result<()> {
    if !require_economy_manager(ctx).await? {
        return Ok(());
    }
    anyhow::ensure!(
        ctx.data()
            .store
            .remove_shop_item(guild_key(ctx), &key)
            .await?,
        "Товар не найден."
    );
    crate::ui::say(
        ctx,
        format!("Товар `{key}` удалён из каталога. Уже купленные роли остаются у владельцев."),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn shop_buy(
    ctx: Context<'_>,
    #[description = "ID товара из магазина"] key: String,
) -> Result<()> {
    let guild_id = ctx
        .guild_id()
        .ok_or_else(|| anyhow::anyhow!("Только на сервере."))?;
    let purchase_id = format!(
        "{}-{}-{}",
        guild_id.get(),
        ctx.author().id.get(),
        chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default()
    );
    let items = ctx.data().store.shop_items(guild_id.get()).await?;
    let item = items
        .iter()
        .find(|(item_key, _, _, _, _)| item_key == &key)
        .ok_or_else(|| anyhow::anyhow!("Товар не найден."))?;
    let price = item.2;
    let member = guild_id
        .member(ctx.serenity_context(), ctx.author().id)
        .await?;
    let (balance, role_id) = ctx
        .data()
        .store
        .buy_shop_item(guild_id.get(), ctx.author().id.get(), &key, &purchase_id)
        .await?;
    let role_id = poise::serenity_prelude::RoleId::new(role_id);
    if let Err(error) = member.add_role(ctx.serenity_context(), role_id).await {
        ctx.data()
            .store
            .rollback_shop_purchase(
                guild_id.get(),
                ctx.author().id.get(),
                &key,
                &purchase_id,
                price,
            )
            .await?;
        return Err(error.into());
    }
    crate::ui::say(
        ctx,
        format!(
            "Куплено **{}** за **{price}** Vivian coins. Остаток: **{balance}**.",
            item.1
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn status(
    ctx: Context<'_>,
    #[description = "Короткий статус (до 120 символов)"] text: Option<String>,
    #[description = "Настроение / emoji (до 32 символов)"] mood: Option<String>,
) -> Result<()> {
    let text = text.unwrap_or_default();
    let mood = mood.unwrap_or_default();
    ctx.data()
        .store
        .set_profile_status(guild_key(ctx), ctx.author().id.get(), &text, &mood)
        .await?;
    crate::ui::say(
        ctx,
        if text.is_empty() && mood.is_empty() {
            "Статус и настроение очищены.".to_owned()
        } else {
            "Статус профиля обновлён.".to_owned()
        },
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn relationship(
    ctx: Context<'_>,
    #[description = "Участник для запроса (оставь пустым для согласия на запрос)"] user: Option<
        poise::serenity_prelude::User,
    >,
) -> Result<()> {
    let guild = guild_key(ctx);
    let me = ctx.author().id.get();
    if let Some(user) = user {
        ctx.data()
            .store
            .request_relationship(guild, me, user.id.get())
            .await?;
        crate::ui::say(ctx, format!("Запрос отправлен <@{}>. Он станет парой только после явного согласия через `/relationship_accept`.",user.id.get())).await?;
    } else if let Some(partner) = ctx.data().store.relationship(guild, me).await? {
        crate::ui::say(ctx, format!("Ты уже состоишь в паре с <@{partner}>.")).await?;
    } else {
        crate::ui::say(ctx, "Нет активной пары. Чтобы принять запрос, укажи отправителя: `/relationship_accept user`.").await?;
    }
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn relationship_accept(
    ctx: Context<'_>,
    #[description = "Кто отправил запрос"] user: poise::serenity_prelude::User,
) -> Result<()> {
    ctx.data()
        .store
        .respond_relationship(guild_key(ctx), ctx.author().id.get(), user.id.get(), true)
        .await?;
    crate::ui::say(
        ctx,
        format!(
            "<@{}> и <@{}> теперь пара.",
            ctx.author().id.get(),
            user.id.get()
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn relationship_reject(
    ctx: Context<'_>,
    #[description = "Кто отправил запрос"] user: poise::serenity_prelude::User,
) -> Result<()> {
    ctx.data()
        .store
        .respond_relationship(guild_key(ctx), ctx.author().id.get(), user.id.get(), false)
        .await?;
    crate::ui::say(ctx, "Запрос отношений отклонён.").await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn single(ctx: Context<'_>) -> Result<()> {
    if ctx
        .data()
        .store
        .end_relationship(guild_key(ctx), ctx.author().id.get())
        .await?
    {
        crate::ui::say(ctx, "Отношения и ожидающие запросы очищены.").await?;
    } else {
        crate::ui::say(ctx, "У тебя нет активной пары.").await?;
    }
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn poke(
    ctx: Context<'_>,
    #[description = "Кого пнуть"] user: poise::serenity_prelude::User,
) -> Result<()> {
    let count = ctx
        .data()
        .store
        .send_poke(
            guild_key(ctx),
            ctx.author().id.get(),
            user.id.get(),
            chrono::Utc::now().timestamp(),
        )
        .await?;
    crate::ui::say(
        ctx,
        format!(
            "<@{}> пнул(а) <@{}> · всего между вами: {count}.",
            ctx.author().id.get(),
            user.id.get()
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn wordhunt(
    ctx: Context<'_>,
    #[description = "start, guess или stop (остановить может создатель)"] action: String,
    #[description = "Ответ для guess; не нужен для start"] value: Option<String>,
) -> Result<()> {
    let guild = ctx
        .guild_id()
        .ok_or_else(|| anyhow::anyhow!("Игра доступна только на сервере."))?
        .get();
    let channel = ctx.channel_id().get();
    match action.to_lowercase().as_str() {
        "start" => {
            const WORDS: [(&str, &str); 8] = [
                ("комета", "Небесное тело с хвостом"),
                ("лаванда", "Ароматное фиолетовое растение"),
                ("клавиатура", "Устройство для ввода текста"),
                ("океан", "Огромное солёное водное пространство"),
                ("робот", "Программируемая машина"),
                ("планета", "Тело, вращающееся вокруг звезды"),
                ("мозаика", "Изображение из множества фрагментов"),
                ("север", "Одна из сторон света"),
            ];
            let (answer, hint) = WORDS[rand::rng().random_range(0..WORDS.len())];
            ctx.data()
                .store
                .start_word_game(guild, channel, ctx.author().id.get(), answer, hint)
                .await?;
            crate::ui::say(ctx, format!("Игра «Охота за словом» началась! Подсказка: **{hint}**. Ответь командой `/wordhunt guess <слово>`. Победитель получает 50 coins." )).await?;
        }
        "guess" => {
            let value = value
                .as_deref()
                .ok_or_else(|| anyhow::anyhow!("Укажи слово для проверки."))?;
            let result = ctx
                .data()
                .store
                .guess_word(guild, channel, ctx.author().id.get(), value)
                .await?;
            if result == Some(true) {
                crate::ui::say(
                    ctx,
                    format!(
                        "Верно, <@{}> победил(а)! Ответ: **{value}**. Награда: 50 coins.",
                        ctx.author().id.get()
                    ),
                )
                .await?;
            } else {
                crate::ui::say(ctx, "Не угадал(а). Попробуй ещё раз!").await?;
            }
        }
        "stop" => {
            anyhow::ensure!(
                ctx.data()
                    .store
                    .stop_word_game(guild, channel, ctx.author().id.get())
                    .await?,
                "Активная игра не найдена или ты не её создатель."
            );
            crate::ui::say(ctx, "Охота за словом остановлена создателем.").await?;
        }
        _ => anyhow::bail!("Действие должно быть `start`, `guess` или `stop`."),
    }
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn friend_add(
    ctx: Context<'_>,
    #[description = "Участник"] user: poise::serenity_prelude::User,
) -> Result<()> {
    ctx.data()
        .store
        .request_friend(guild_key(ctx), ctx.author().id.get(), user.id.get())
        .await?;
    crate::ui::say(
        ctx,
        format!(
            "Запрос дружбы отправлен <@{}>. Подтвердить: `/friend_accept user`. ",
            user.id.get()
        ),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn friend_accept(
    ctx: Context<'_>,
    #[description = "Кто отправил запрос"] user: poise::serenity_prelude::User,
) -> Result<()> {
    ctx.data()
        .store
        .respond_friend(guild_key(ctx), ctx.author().id.get(), user.id.get(), true)
        .await?;
    crate::ui::say(ctx, format!("Вы с <@{}> теперь друзья.", user.id.get())).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn friend_reject(
    ctx: Context<'_>,
    #[description = "Кто отправил запрос"] user: poise::serenity_prelude::User,
) -> Result<()> {
    ctx.data()
        .store
        .respond_friend(guild_key(ctx), ctx.author().id.get(), user.id.get(), false)
        .await?;
    crate::ui::say(ctx, "Запрос дружбы отклонён.").await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn friend_requests(ctx: Context<'_>) -> Result<()> {
    let requests = ctx
        .data()
        .store
        .incoming_friend_requests(guild_key(ctx), ctx.author().id.get())
        .await?;
    let text = if requests.is_empty() {
        "Входящих запросов дружбы нет.".to_owned()
    } else {
        format!(
            "**Запросы дружбы**\n{}\nПрими через `/friend_accept user`.",
            requests
                .into_iter()
                .map(|id| format!("<@{id}>"))
                .collect::<Vec<_>>()
                .join("\n")
        )
    };
    crate::ui::private(ctx, text).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn friend_list(ctx: Context<'_>) -> Result<()> {
    let friends = ctx
        .data()
        .store
        .friends(guild_key(ctx), ctx.author().id.get())
        .await?;
    crate::ui::say(
        ctx,
        if friends.is_empty() {
            "Список друзей пуст.".to_owned()
        } else {
            format!(
                "**Друзья**\n{}",
                friends
                    .into_iter()
                    .map(|id| format!("<@{id}>"))
                    .collect::<Vec<_>>()
                    .join("\n")
            )
        },
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn friend_remove(
    ctx: Context<'_>,
    #[description = "Участник"] user: poise::serenity_prelude::User,
) -> Result<()> {
    anyhow::ensure!(
        ctx.data()
            .store
            .remove_friend(guild_key(ctx), ctx.author().id.get(), user.id.get())
            .await?,
        "Запись о дружбе или запросе не найден."
    );
    crate::ui::say(ctx, format!("Связь с <@{}> удалена.", user.id.get())).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn crush_pick(
    ctx: Context<'_>,
    #[description = "Кого выбрать"] user: poise::serenity_prelude::User,
) -> Result<()> {
    let now = chrono::Utc::now().with_timezone(&chrono_tz::Europe::Riga);
    let week = now.iso_week();
    let week_key = format!("{}-{:02}", week.year(), week.week());
    let (count, mutual) = ctx
        .data()
        .store
        .pick_crush(
            guild_key(ctx),
            ctx.author().id.get(),
            user.id.get(),
            &week_key,
        )
        .await?;
    let result = if mutual {
        "И это взаимно — ваш выбор совпал.".to_owned()
    } else {
        "Твой выбор сохранён приватно.".to_owned()
    };
    crate::ui::private(
        ctx,
        format!("{result} Уникальных выборов на этой неделе: {count}/3."),
    )
    .await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn crush_show(ctx: Context<'_>) -> Result<()> {
    let choice = ctx
        .data()
        .store
        .show_crush(guild_key(ctx), ctx.author().id.get())
        .await?;
    let text=choice.map(|(target,_,count)|format!("Твой текущий выбор: <@{target}>. На этой неделе выбрано разных участников: {count}/3.")).unwrap_or_else(||"Тайного выбора пока нет.".to_owned());
    crate::ui::private(ctx, text).await?;
    Ok(())
}

#[poise::command(slash_command, prefix_command)]
pub async fn crush_clear(ctx: Context<'_>) -> Result<()> {
    let cleared = ctx
        .data()
        .store
        .clear_crush(guild_key(ctx), ctx.author().id.get())
        .await?;
    crate::ui::private(
        ctx,
        if cleared {
            "Приватный выбор очищен; недельный лимит не восстановлен."
        } else {
            "Активного выбора нет."
        },
    )
    .await?;
    Ok(())
}
