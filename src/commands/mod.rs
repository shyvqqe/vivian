pub(crate) mod ai;
mod birthday;
mod economy;
mod general;
mod moderation;
mod music;
mod rooms;
mod wall;

pub(crate) type Context<'a> = poise::Context<'a, crate::Data, anyhow::Error>;

pub fn all() -> Vec<poise::Command<crate::Data, anyhow::Error>> {
    vec![
        general::help(),
        general::about(),
        general::ping(),
        birthday::birthday(),
        ai::ai(),
        ai::ask(),
        ai::forget(),
        music::play(),
        music::search(),
        music::queue(),
        music::nowplaying(),
        music::skip(),
        music::pause(),
        music::resume(),
        music::stop(),
        music::volume(),
        music::loop_mode(),
        music::shuffle(),
        music::seek(),
        music::remove(),
        music::clear(),
        music::filter(),
        music::playlist_save(),
        music::playlists(),
        music::playlist_play(),
        music::playlist_delete(),
        rooms::room_create(),
        rooms::room_manage(),
        rooms::room_claim(),
        rooms::room_setup(),
        rooms::room_rename(),
        rooms::room_limit(),
        rooms::room_lock(),
        rooms::room_visibility(),
        rooms::room_invite(),
        rooms::room_kick(),
        rooms::room_transfer(),
        rooms::room_delete(),
        music::join(),
        music::leave(),
        moderation::warn(),
        moderation::unwarn(),
        moderation::warnings(),
        moderation::modlog(),
        moderation::timeout(),
        moderation::untimeout(),
        moderation::kick(),
        moderation::ban(),
        moderation::unban(),
        moderation::clear(),
        moderation::slowmode(),
        moderation::lock(),
        moderation::unlock(),
        economy::balance(),
        economy::daily(),
        economy::work(),
        economy::pay(),
        economy::leaderboard(),
        economy::profile(),
        economy::rank(),
        economy::inventory(),
        economy::shop(),
        economy::shop_add(),
        economy::shop_remove(),
        economy::shop_buy(),
        economy::status(),
        economy::relationship(),
        economy::relationship_accept(),
        economy::relationship_reject(),
        economy::single(),
        economy::poke(),
        economy::wordhunt(),
        economy::friend_add(),
        economy::friend_accept(),
        economy::friend_reject(),
        economy::friend_requests(),
        economy::friend_list(),
        economy::friend_remove(),
        economy::crush_pick(),
        economy::crush_show(),
        economy::crush_clear(),
        wall::wall_view(),
        wall::wall_post(),
        wall::wall_privacy(),
        wall::wall_like(),
        wall::wall_comment(),
        wall::wall_comments(),
        wall::wall_delete(),
    ]
}

pub(crate) async fn say_long(ctx: Context<'_>, text: &str) -> anyhow::Result<()> {
    let mut rest = text.trim();
    while !rest.is_empty() {
        let mut end = rest
            .char_indices()
            .take_while(|(i, _)| *i < 1900)
            .last()
            .map(|(i, ch)| i + ch.len_utf8())
            .unwrap_or(rest.len());
        while !rest.is_char_boundary(end) {
            end -= 1;
        }
        crate::ui::say(ctx, &rest[..end]).await?;
        rest = rest[end..].trim_start();
    }
    Ok(())
}

pub(crate) fn guild_key(ctx: Context<'_>) -> u64 {
    ctx.guild_id().map(|id| id.get()).unwrap_or(0)
}
