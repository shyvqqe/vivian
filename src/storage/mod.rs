use anyhow::Result;
use chrono::Datelike;
use sqlx::{sqlite::SqlitePoolOptions, SqlitePool};

#[derive(Clone)]
pub struct Store {
    pub pool: SqlitePool,
}

#[derive(Clone, Debug)]
pub struct HistoryItem {
    pub role: String,
    pub content: String,
}

#[derive(Clone, Debug)]
pub struct PrivateRoom {
    pub token: i64,
    pub guild_id: u64,
    pub owner_id: u64,
    pub channel_id: Option<u64>,
    pub state: String,
}

impl Store {
    pub async fn connect(url: &str) -> Result<Self> {
        let pool = SqlitePoolOptions::new()
            .max_connections(5)
            .connect(url)
            .await?;
        let store = Self { pool };
        store.migrate().await?;
        Ok(store)
    }

    async fn migrate(&self) -> Result<()> {
        for statement in [
            "CREATE TABLE IF NOT EXISTS ai_history (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL, created_at INTEGER NOT NULL DEFAULT (unixepoch()))",
            "CREATE INDEX IF NOT EXISTS ai_history_scope ON ai_history(guild_id, user_id, id)",
            "CREATE TABLE IF NOT EXISTS ai_preferences (guild_id TEXT NOT NULL, user_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, PRIMARY KEY(guild_id, user_id))",
            "CREATE TABLE IF NOT EXISTS guild_settings (guild_id TEXT PRIMARY KEY, settings TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL DEFAULT (unixepoch()))",
            "CREATE TABLE IF NOT EXISTS public_profiles (guild_id TEXT NOT NULL,user_id TEXT NOT NULL,public INTEGER NOT NULL DEFAULT 0,share_friends INTEGER NOT NULL DEFAULT 0,share_wall INTEGER NOT NULL DEFAULT 0,updated_at INTEGER NOT NULL DEFAULT (unixepoch()),PRIMARY KEY(guild_id,user_id))",
            "CREATE TABLE IF NOT EXISTS birthdays (guild_id TEXT NOT NULL,user_id TEXT NOT NULL,day INTEGER NOT NULL CHECK(day BETWEEN 1 AND 31),month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),year INTEGER,PRIMARY KEY(guild_id,user_id))",
            "CREATE INDEX IF NOT EXISTS birthdays_month_day ON birthdays(guild_id,month,day)",
            "CREATE TABLE IF NOT EXISTS birthday_active_roles (guild_id TEXT NOT NULL,user_id TEXT NOT NULL,role_id TEXT NOT NULL,expires_at INTEGER NOT NULL,PRIMARY KEY(guild_id,user_id))",
            "CREATE TABLE IF NOT EXISTS birthday_announcement_users (guild_id TEXT NOT NULL,user_id TEXT NOT NULL,local_date TEXT NOT NULL,PRIMARY KEY(guild_id,user_id,local_date))",
            "CREATE TABLE IF NOT EXISTS warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, user_id TEXT NOT NULL, moderator_id TEXT NOT NULL, reason TEXT NOT NULL, created_at INTEGER NOT NULL DEFAULT (unixepoch()))",
            "CREATE INDEX IF NOT EXISTS warnings_scope ON warnings(guild_id, user_id, id)",
            "CREATE TABLE IF NOT EXISTS system_timeouts (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, user_id TEXT NOT NULL, moderator_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL DEFAULT (unixepoch()))",
            "CREATE TABLE IF NOT EXISTS economy (guild_id TEXT NOT NULL, user_id TEXT NOT NULL, coins INTEGER NOT NULL DEFAULT 0 CHECK(coins >= 0), daily_date TEXT, work_at INTEGER, PRIMARY KEY(guild_id, user_id))",
            "CREATE TABLE IF NOT EXISTS moderation_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, moderator_id TEXT NOT NULL, action TEXT NOT NULL, target_id TEXT, channel_id TEXT, reason TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL DEFAULT (unixepoch()))",
            "CREATE INDEX IF NOT EXISTS moderation_actions_scope ON moderation_actions(guild_id, id DESC)",
            "CREATE TABLE IF NOT EXISTS member_progress (guild_id TEXT NOT NULL, user_id TEXT NOT NULL, xp INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0), message_at INTEGER NOT NULL DEFAULT 0, last_digest TEXT, voice_seconds INTEGER NOT NULL DEFAULT 0, message_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,user_id))",
            "CREATE INDEX IF NOT EXISTS member_progress_rank ON member_progress(guild_id,xp DESC)",
            "CREATE TABLE IF NOT EXISTS economy_transfers (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, sender_id TEXT NOT NULL, recipient_id TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount > 0), created_at INTEGER NOT NULL DEFAULT (unixepoch()))",
            "CREATE TABLE IF NOT EXISTS economy_daily_earnings (guild_id TEXT NOT NULL, user_id TEXT NOT NULL, day TEXT NOT NULL, amount INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,user_id,day))",
            "CREATE TABLE IF NOT EXISTS shop_items (guild_id TEXT NOT NULL, item_key TEXT NOT NULL, name TEXT NOT NULL, price INTEGER NOT NULL CHECK(price > 0), role_id TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(guild_id,item_key), UNIQUE(guild_id,role_id))",
            "CREATE TABLE IF NOT EXISTS inventory (guild_id TEXT NOT NULL, user_id TEXT NOT NULL, item_key TEXT NOT NULL, purchase_id TEXT NOT NULL, purchased_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(guild_id,user_id,item_key), UNIQUE(purchase_id))",
            "CREATE TABLE IF NOT EXISTS profile_status (guild_id TEXT NOT NULL, user_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT '', mood TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(guild_id,user_id))",
            "CREATE TABLE IF NOT EXISTS wall_settings (guild_id TEXT NOT NULL,user_id TEXT NOT NULL,privacy TEXT NOT NULL DEFAULT 'all' CHECK(privacy IN ('all','friends','private')),PRIMARY KEY(guild_id,user_id))",
            "CREATE TABLE IF NOT EXISTS wall_posts (id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id TEXT NOT NULL,owner_id TEXT NOT NULL,author_id TEXT NOT NULL,text TEXT NOT NULL,created_at INTEGER NOT NULL DEFAULT (unixepoch()))",
            "CREATE INDEX IF NOT EXISTS wall_posts_scope ON wall_posts(guild_id,owner_id,id DESC)",
            "CREATE TABLE IF NOT EXISTS wall_likes (guild_id TEXT NOT NULL,post_id INTEGER NOT NULL,user_id TEXT NOT NULL,created_at INTEGER NOT NULL DEFAULT (unixepoch()),PRIMARY KEY(guild_id,post_id,user_id))",
            "CREATE TABLE IF NOT EXISTS wall_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id TEXT NOT NULL,post_id INTEGER NOT NULL,author_id TEXT NOT NULL,text TEXT NOT NULL,created_at INTEGER NOT NULL DEFAULT (unixepoch()))",
            "CREATE INDEX IF NOT EXISTS wall_comments_scope ON wall_comments(guild_id,post_id,id)",
            "CREATE TABLE IF NOT EXISTS relationship_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, requester_id TEXT NOT NULL, target_id TEXT NOT NULL, created_at INTEGER NOT NULL DEFAULT (unixepoch()), UNIQUE(guild_id,requester_id,target_id))",
            "CREATE TABLE IF NOT EXISTS relationships (guild_id TEXT NOT NULL, user_id TEXT NOT NULL, partner_id TEXT NOT NULL, created_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(guild_id,user_id), UNIQUE(guild_id,partner_id))",
            "CREATE TABLE IF NOT EXISTS friends (guild_id TEXT NOT NULL, user_id TEXT NOT NULL, friend_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','accepted')), created_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(guild_id,user_id,friend_id))",
            "CREATE INDEX IF NOT EXISTS friends_incoming ON friends(guild_id,friend_id,status)",
            "CREATE TABLE IF NOT EXISTS crush_choices (guild_id TEXT NOT NULL,user_id TEXT NOT NULL,target_id TEXT NOT NULL,week TEXT NOT NULL,created_at INTEGER NOT NULL DEFAULT (unixepoch()),PRIMARY KEY(guild_id,user_id,target_id,week))",
            "CREATE TABLE IF NOT EXISTS crush_selection (guild_id TEXT NOT NULL,user_id TEXT NOT NULL,target_id TEXT NOT NULL,week TEXT NOT NULL,PRIMARY KEY(guild_id,user_id))",
            "CREATE TABLE IF NOT EXISTS pokes (guild_id TEXT NOT NULL, sender_id TEXT NOT NULL, target_id TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, last_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,sender_id,target_id))",
            "CREATE TABLE IF NOT EXISTS word_games (guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, starter_id TEXT NOT NULL, answer TEXT NOT NULL, hint TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','finished')), winner_id TEXT, created_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(guild_id,channel_id))",
            "CREATE TABLE IF NOT EXISTS word_game_players (guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, user_id TEXT NOT NULL, score INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,channel_id,user_id))",
            "CREATE TABLE IF NOT EXISTS automod_state (guild_id TEXT NOT NULL, user_id TEXT NOT NULL, last_incident INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,user_id))",
            "CREATE TABLE IF NOT EXISTS private_room_settings (guild_id TEXT PRIMARY KEY, category_id TEXT, enabled INTEGER NOT NULL DEFAULT 1)",
            "CREATE TABLE IF NOT EXISTS private_rooms (token INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, owner_id TEXT NOT NULL, channel_id TEXT, state TEXT NOT NULL CHECK(state IN ('creating','active','deleting','released')), created_at INTEGER NOT NULL DEFAULT (unixepoch()))",
            "CREATE UNIQUE INDEX IF NOT EXISTS private_rooms_active_owner ON private_rooms(guild_id, owner_id) WHERE state != 'released'",
            "CREATE UNIQUE INDEX IF NOT EXISTS private_rooms_active_channel ON private_rooms(guild_id, channel_id) WHERE channel_id IS NOT NULL AND state != 'released'",
            "CREATE TABLE IF NOT EXISTS music_playlists (guild_id TEXT NOT NULL, owner_id TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL, tracks TEXT NOT NULL, updated_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(guild_id, owner_id, name_key))",
        ] {
            sqlx::query(statement).execute(&self.pool).await?;
        }
        // Upgrade older databases created before the full profile/economy fields existed.
        let columns: Vec<(String,)> = sqlx::query_as("PRAGMA table_info(economy)")
            .fetch_all(&self.pool)
            .await?
            .into_iter()
            .map(|row: (i64, String, String, i64, Option<String>, i64)| (row.1,))
            .collect();
        if !columns.iter().any(|(name,)| name == "daily_date") {
            sqlx::query("ALTER TABLE economy ADD COLUMN daily_date TEXT")
                .execute(&self.pool)
                .await?;
        }
        if !columns.iter().any(|(name,)| name == "work_at") {
            sqlx::query("ALTER TABLE economy ADD COLUMN work_at INTEGER")
                .execute(&self.pool)
                .await?;
        }
        let progress_columns: Vec<(String,)> = sqlx::query_as("PRAGMA table_info(member_progress)")
            .fetch_all(&self.pool)
            .await?
            .into_iter()
            .map(|row: (i64, String, String, i64, Option<String>, i64)| (row.1,))
            .collect();
        if !progress_columns
            .iter()
            .any(|(name,)| name == "message_count")
        {
            sqlx::query(
                "ALTER TABLE member_progress ADD COLUMN message_count INTEGER NOT NULL DEFAULT 0",
            )
            .execute(&self.pool)
            .await?;
        }
        Ok(())
    }

    fn room_from_row(row: (i64, String, String, Option<String>, String)) -> Result<PrivateRoom> {
        Ok(PrivateRoom {
            token: row.0,
            guild_id: row.1.parse()?,
            owner_id: row.2.parse()?,
            channel_id: row.3.map(|value| value.parse()).transpose()?,
            state: row.4,
        })
    }

    pub async fn room_by_owner(&self, guild_id: u64, owner_id: u64) -> Result<Option<PrivateRoom>> {
        let row = sqlx::query_as::<_, (i64, String, String, Option<String>, String)>(
            "SELECT token,guild_id,owner_id,channel_id,state FROM private_rooms WHERE guild_id=? AND owner_id=? AND state!='released' ORDER BY token DESC LIMIT 1",
        ).bind(guild_id.to_string()).bind(owner_id.to_string()).fetch_optional(&self.pool).await?;
        row.map(Self::room_from_row).transpose()
    }

    pub async fn room_by_channel(
        &self,
        guild_id: u64,
        channel_id: u64,
    ) -> Result<Option<PrivateRoom>> {
        let row = sqlx::query_as::<_, (i64, String, String, Option<String>, String)>(
            "SELECT token,guild_id,owner_id,channel_id,state FROM private_rooms WHERE guild_id=? AND channel_id=? AND state!='released' ORDER BY token DESC LIMIT 1",
        ).bind(guild_id.to_string()).bind(channel_id.to_string()).fetch_optional(&self.pool).await?;
        row.map(Self::room_from_row).transpose()
    }

    pub async fn reserve_room(&self, guild_id: u64, owner_id: u64) -> Result<PrivateRoom> {
        sqlx::query("INSERT INTO private_rooms(guild_id,owner_id,state) VALUES(?,?,'creating')")
            .bind(guild_id.to_string())
            .bind(owner_id.to_string())
            .execute(&self.pool)
            .await?;
        self.room_by_owner(guild_id, owner_id)
            .await?
            .ok_or_else(|| anyhow::anyhow!("room reservation disappeared"))
    }

    pub async fn activate_room(&self, token: i64, channel_id: u64) -> Result<()> {
        sqlx::query("UPDATE private_rooms SET channel_id=?,state='active' WHERE token=? AND state IN ('creating','active')")
            .bind(channel_id.to_string()).bind(token).execute(&self.pool).await?;
        Ok(())
    }

    pub async fn mark_room_deleting(&self, token: i64) -> Result<()> {
        sqlx::query("UPDATE private_rooms SET state='deleting' WHERE token=? AND state='active'")
            .bind(token)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn release_room(&self, token: i64) -> Result<()> {
        sqlx::query("UPDATE private_rooms SET state='released',channel_id=NULL WHERE token=?")
            .bind(token)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn transfer_room(&self, token: i64, new_owner: u64) -> Result<()> {
        sqlx::query("UPDATE private_rooms SET owner_id=? WHERE token=? AND state='active'")
            .bind(new_owner.to_string())
            .bind(token)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn private_room_settings(
        &self,
        guild_id: u64,
    ) -> Result<Option<(Option<u64>, bool)>> {
        let row = sqlx::query_as::<_, (Option<String>, i64)>(
            "SELECT category_id,enabled FROM private_room_settings WHERE guild_id=?",
        )
        .bind(guild_id.to_string())
        .fetch_optional(&self.pool)
        .await?;
        row.map(|(category, enabled)| {
            Ok((
                category.map(|value| value.parse()).transpose()?,
                enabled != 0,
            ))
        })
        .transpose()
    }

    pub async fn set_private_room_settings(
        &self,
        guild_id: u64,
        category_id: Option<u64>,
        enabled: bool,
    ) -> Result<()> {
        sqlx::query("INSERT INTO private_room_settings(guild_id,category_id,enabled) VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET category_id=excluded.category_id,enabled=excluded.enabled")
            .bind(guild_id.to_string()).bind(category_id.map(|id| id.to_string())).bind(i64::from(enabled)).execute(&self.pool).await?;
        Ok(())
    }

    pub async fn active_room_count(&self, guild_id: u64) -> Result<i64> {
        let (count,): (i64,) = sqlx::query_as("SELECT count(*) FROM private_rooms WHERE guild_id=? AND state IN ('creating','active','deleting')")
            .bind(guild_id.to_string()).fetch_one(&self.pool).await?;
        Ok(count)
    }

    pub async fn recoverable_rooms(&self) -> Result<Vec<PrivateRoom>> {
        let rows = sqlx::query_as::<_, (i64, String, String, Option<String>, String)>(
            "SELECT token,guild_id,owner_id,channel_id,state FROM private_rooms WHERE state!='released' ORDER BY token",
        ).fetch_all(&self.pool).await?;
        rows.into_iter().map(Self::room_from_row).collect()
    }

    pub async fn save_playlist(
        &self,
        guild_id: u64,
        owner_id: u64,
        name: &str,
        tracks: &str,
    ) -> Result<()> {
        let name_key = name.to_lowercase();
        sqlx::query("INSERT INTO music_playlists(guild_id,owner_id,name,name_key,tracks) VALUES(?,?,?,?,?) ON CONFLICT(guild_id,owner_id,name_key) DO UPDATE SET name=excluded.name,tracks=excluded.tracks,updated_at=unixepoch()")
            .bind(guild_id.to_string()).bind(owner_id.to_string()).bind(name).bind(name_key).bind(tracks).execute(&self.pool).await?;
        Ok(())
    }

    pub async fn playlists(&self, guild_id: u64, owner_id: u64) -> Result<Vec<(String, String)>> {
        Ok(sqlx::query_as("SELECT name,tracks FROM music_playlists WHERE guild_id=? AND owner_id=? ORDER BY updated_at DESC,name ASC LIMIT 25")
            .bind(guild_id.to_string()).bind(owner_id.to_string()).fetch_all(&self.pool).await?)
    }

    pub async fn delete_playlist(&self, guild_id: u64, owner_id: u64, name: &str) -> Result<bool> {
        let result = sqlx::query(
            "DELETE FROM music_playlists WHERE guild_id=? AND owner_id=? AND name_key=?",
        )
        .bind(guild_id.to_string())
        .bind(owner_id.to_string())
        .bind(name.to_lowercase())
        .execute(&self.pool)
        .await?;
        Ok(result.rows_affected() > 0)
    }

    pub async fn history(
        &self,
        guild_id: u64,
        user_id: u64,
        limit: i64,
    ) -> Result<Vec<HistoryItem>> {
        if limit <= 0 {
            return Ok(Vec::new());
        }
        let mut rows = sqlx::query_as::<_, (String, String)>(
            "SELECT role, content FROM ai_history WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
        )
        .bind(guild_id.to_string())
        .bind(user_id.to_string())
        .bind(limit * 2)
        .fetch_all(&self.pool)
        .await?;
        rows.reverse();
        Ok(rows
            .into_iter()
            .map(|(role, content)| HistoryItem { role, content })
            .collect())
    }

    pub async fn save_turn(
        &self,
        guild_id: u64,
        user_id: u64,
        prompt: &str,
        reply: &str,
    ) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        for (role, content) in [("user", prompt), ("assistant", reply)] {
            sqlx::query(
                "INSERT INTO ai_history(guild_id, user_id, role, content) VALUES (?, ?, ?, ?)",
            )
            .bind(guild_id.to_string())
            .bind(user_id.to_string())
            .bind(role)
            .bind(content)
            .execute(&mut *tx)
            .await?;
        }
        sqlx::query("DELETE FROM ai_history WHERE guild_id = ? AND user_id = ? AND id NOT IN (SELECT id FROM ai_history WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 80)")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(guild_id.to_string()).bind(user_id.to_string())
            .execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }

    pub async fn clear_history(&self, guild_id: u64, user_id: u64) -> Result<u64> {
        Ok(
            sqlx::query("DELETE FROM ai_history WHERE guild_id = ? AND user_id = ?")
                .bind(guild_id.to_string())
                .bind(user_id.to_string())
                .execute(&self.pool)
                .await?
                .rows_affected(),
        )
    }

    pub async fn preference(
        &self,
        guild_id: u64,
        user_id: u64,
    ) -> Result<Option<(String, String)>> {
        Ok(sqlx::query_as::<_, (String, String)>(
            "SELECT provider, model FROM ai_preferences WHERE guild_id = ? AND user_id = ?",
        )
        .bind(guild_id.to_string())
        .bind(user_id.to_string())
        .fetch_optional(&self.pool)
        .await?)
    }

    pub async fn set_preference(
        &self,
        guild_id: u64,
        user_id: u64,
        provider: &str,
        model: &str,
    ) -> Result<()> {
        sqlx::query("INSERT INTO ai_preferences(guild_id,user_id,provider,model) VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET provider=excluded.provider, model=excluded.model")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(provider).bind(model).execute(&self.pool).await?;
        Ok(())
    }

    pub async fn reset_preference(&self, guild_id: u64, user_id: u64) -> Result<()> {
        sqlx::query("DELETE FROM ai_preferences WHERE guild_id = ? AND user_id = ?")
            .bind(guild_id.to_string())
            .bind(user_id.to_string())
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn guild_settings(
        &self,
        guild_id: u64,
        defaults: &serde_json::Value,
    ) -> Result<serde_json::Value> {
        let row: Option<(String,)> =
            sqlx::query_as("SELECT settings FROM guild_settings WHERE guild_id=?")
                .bind(guild_id.to_string())
                .fetch_optional(&self.pool)
                .await?;
        let mut settings = defaults.clone();
        if let Some((saved,)) = row {
            let saved: serde_json::Value = serde_json::from_str(&saved)?;
            if let (Some(defaults), Some(saved)) = (settings.as_object_mut(), saved.as_object()) {
                for (key, value) in saved {
                    defaults.insert(key.clone(), value.clone());
                }
            }
        }
        Ok(settings)
    }

    pub async fn update_guild_settings(
        &self,
        guild_id: u64,
        patch: &serde_json::Map<String, serde_json::Value>,
        defaults: &serde_json::Value,
    ) -> Result<serde_json::Value> {
        let mut tx = self.pool.begin().await?;
        let row: Option<(String,)> =
            sqlx::query_as("SELECT settings FROM guild_settings WHERE guild_id=?")
                .bind(guild_id.to_string())
                .fetch_optional(&mut *tx)
                .await?;
        let mut settings = defaults.clone();
        if let Some((saved,)) = row {
            let saved: serde_json::Value = serde_json::from_str(&saved)?;
            if let (Some(defaults), Some(saved)) = (settings.as_object_mut(), saved.as_object()) {
                for (key, value) in saved {
                    defaults.insert(key.clone(), value.clone());
                }
            }
        }
        let object = settings
            .as_object_mut()
            .ok_or_else(|| anyhow::anyhow!("settings defaults must be an object"))?;
        for (key, value) in patch {
            object.insert(key.clone(), value.clone());
        }
        let serialized = serde_json::to_string(&settings)?;
        sqlx::query("INSERT INTO guild_settings(guild_id,settings) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET settings=excluded.settings,updated_at=unixepoch()")
            .bind(guild_id.to_string()).bind(serialized).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(settings)
    }

    pub async fn public_profile_consent(
        &self,
        guild_id: u64,
        user_id: u64,
    ) -> Result<(bool, bool, bool)> {
        let row:Option<(i64,i64,i64)>=sqlx::query_as("SELECT public,share_friends,share_wall FROM public_profiles WHERE guild_id=? AND user_id=?")
            .bind(guild_id.to_string()).bind(user_id.to_string()).fetch_optional(&self.pool).await?;
        Ok(row
            .map(|(public, friends, wall)| (public != 0, friends != 0, wall != 0))
            .unwrap_or((false, false, false)))
    }

    pub async fn set_public_profile_consent(
        &self,
        guild_id: u64,
        user_id: u64,
        public: bool,
        share_friends: bool,
        share_wall: bool,
    ) -> Result<()> {
        sqlx::query("INSERT INTO public_profiles(guild_id,user_id,public,share_friends,share_wall) VALUES(?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET public=excluded.public,share_friends=excluded.share_friends,share_wall=excluded.share_wall,updated_at=unixepoch()")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(i64::from(public)).bind(i64::from(share_friends)).bind(i64::from(share_wall)).execute(&self.pool).await?;
        Ok(())
    }

    pub async fn set_birthday(
        &self,
        guild_id: u64,
        user_id: u64,
        day: u32,
        month: u32,
        year: Option<i32>,
    ) -> Result<()> {
        anyhow::ensure!(
            guild_id > 0
                && user_id > 0
                && chrono::NaiveDate::from_ymd_opt(2000, month, day).is_some(),
            "Укажи корректный день и месяц."
        );
        if let Some(year) = year {
            let current_year = chrono::Utc::now().year();
            anyhow::ensure!(
                (current_year - 120..=current_year).contains(&year)
                    && chrono::NaiveDate::from_ymd_opt(year, month, day)
                        .is_some_and(|date| date <= chrono::Utc::now().date_naive()),
                "Укажи корректный год рождения или оставь поле пустым."
            );
        }
        sqlx::query("INSERT INTO birthdays(guild_id,user_id,day,month,year) VALUES(?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET day=excluded.day,month=excluded.month,year=excluded.year")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(day).bind(month).bind(year)
            .execute(&self.pool).await?;
        Ok(())
    }

    pub async fn remove_birthday(&self, guild_id: u64, user_id: u64) -> Result<bool> {
        let result = sqlx::query("DELETE FROM birthdays WHERE guild_id=? AND user_id=?")
            .bind(guild_id.to_string())
            .bind(user_id.to_string())
            .execute(&self.pool)
            .await?;
        Ok(result.rows_affected() > 0)
    }

    pub async fn birthdays_for_day(&self, guild_id: u64, month: u32, day: u32) -> Result<Vec<u64>> {
        let observe_leap_day = month == 3
            && day == 1
            && chrono::NaiveDate::from_ymd_opt(chrono::Utc::now().year(), 2, 29).is_none();
        let rows: Vec<(String,)> = sqlx::query_as("SELECT user_id FROM birthdays WHERE guild_id=? AND ((month=? AND day=?) OR (?=1 AND month=2 AND day=29)) ORDER BY user_id")
            .bind(guild_id.to_string()).bind(month).bind(day).bind(i64::from(observe_leap_day)).fetch_all(&self.pool).await?;
        rows.into_iter().map(|(id,)| Ok(id.parse()?)).collect()
    }

    pub async fn upcoming_birthdays(
        &self,
        guild_id: u64,
        year: i32,
        month: u32,
        day: u32,
        limit: i64,
    ) -> Result<Vec<(u64, u32, u32)>> {
        let rows: Vec<(String, i64, i64)> =
            sqlx::query_as("SELECT user_id,day,month FROM birthdays WHERE guild_id=?")
                .bind(guild_id.to_string())
                .fetch_all(&self.pool)
                .await?;
        let today = chrono::NaiveDate::from_ymd_opt(year, month, day)
            .ok_or_else(|| anyhow::anyhow!("Invalid current date."))?;
        let next_date = |birthday_month: u32, birthday_day: u32| {
            let make_date = |year| {
                if birthday_month == 2
                    && birthday_day == 29
                    && chrono::NaiveDate::from_ymd_opt(year, 2, 29).is_none()
                {
                    chrono::NaiveDate::from_ymd_opt(year, 3, 1)
                } else {
                    chrono::NaiveDate::from_ymd_opt(year, birthday_month, birthday_day)
                }
            };
            make_date(today.year())
                .filter(|date| *date >= today)
                .or_else(|| make_date(today.year() + 1))
        };
        let mut sorted = rows
            .into_iter()
            .map(|(user, birthday_day, birthday_month)| {
                let user_id = user.parse::<u64>()?;
                let birthday_day = birthday_day as u32;
                let birthday_month = birthday_month as u32;
                let next = next_date(birthday_month, birthday_day)
                    .ok_or_else(|| anyhow::anyhow!("Invalid saved birthday."))?;
                Ok((next, user_id, birthday_day, birthday_month))
            })
            .collect::<Result<Vec<_>>>()?;
        sorted.sort_by_key(|(next, user_id, _, _)| (*next, *user_id));
        Ok(sorted
            .into_iter()
            .take(limit.clamp(1, 10_000) as usize)
            .map(|(_, user_id, day, month)| (user_id, day, month))
            .collect())
    }

    pub async fn claim_birthday_announcement(
        &self,
        guild_id: u64,
        user_id: u64,
        local_date: &str,
    ) -> Result<bool> {
        let inserted = sqlx::query("INSERT OR IGNORE INTO birthday_announcement_users(guild_id,user_id,local_date) VALUES(?,?,?)")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(local_date).execute(&self.pool).await?;
        Ok(inserted.rows_affected() == 1)
    }

    pub async fn set_birthday_active_role(
        &self,
        guild_id: u64,
        user_id: u64,
        role_id: u64,
        expires_at: i64,
    ) -> Result<()> {
        sqlx::query("INSERT INTO birthday_active_roles(guild_id,user_id,role_id,expires_at) VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET role_id=excluded.role_id,expires_at=excluded.expires_at")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(role_id.to_string()).bind(expires_at)
            .execute(&self.pool).await?;
        Ok(())
    }

    pub async fn due_birthday_roles(&self, now: i64) -> Result<Vec<(u64, u64, u64)>> {
        let rows: Vec<(String, String, String)> = sqlx::query_as("SELECT guild_id,user_id,role_id FROM birthday_active_roles WHERE expires_at<=? ORDER BY expires_at")
            .bind(now).fetch_all(&self.pool).await?;
        rows.into_iter()
            .map(|(guild, user, role)| Ok((guild.parse()?, user.parse()?, role.parse()?)))
            .collect()
    }

    pub async fn clear_birthday_role(&self, guild_id: u64, user_id: u64) -> Result<()> {
        sqlx::query("DELETE FROM birthday_active_roles WHERE guild_id=? AND user_id=?")
            .bind(guild_id.to_string())
            .bind(user_id.to_string())
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn add_warning(
        &self,
        guild_id: u64,
        user_id: u64,
        moderator_id: u64,
        reason: &str,
    ) -> Result<()> {
        sqlx::query("INSERT INTO warnings(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)")
            .bind(guild_id.to_string())
            .bind(user_id.to_string())
            .bind(moderator_id.to_string())
            .bind(reason)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn warnings(&self, guild_id: u64, user_id: u64) -> Result<Vec<(i64, String, i64)>> {
        Ok(sqlx::query_as("SELECT id, reason, created_at FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 20")
            .bind(guild_id.to_string()).bind(user_id.to_string()).fetch_all(&self.pool).await?)
    }

    pub async fn add_timeout(&self, guild_id: u64, user_id: u64, moderator_id: u64) -> Result<()> {
        sqlx::query("INSERT INTO system_timeouts(guild_id,user_id,moderator_id) VALUES(?,?,?)")
            .bind(guild_id.to_string())
            .bind(user_id.to_string())
            .bind(moderator_id.to_string())
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn has_active_timeout(&self, guild_id: u64, user_id: u64) -> Result<bool> {
        let row = sqlx::query_as::<_, (i64,)>("SELECT id FROM system_timeouts WHERE guild_id = ? AND user_id = ? AND active = 1 ORDER BY id DESC LIMIT 1")
            .bind(guild_id.to_string()).bind(user_id.to_string()).fetch_optional(&self.pool).await?;
        Ok(row.is_some())
    }

    pub async fn clear_timeout(&self, guild_id: u64, user_id: u64) -> Result<()> {
        sqlx::query("UPDATE system_timeouts SET active = 0 WHERE guild_id = ? AND user_id = ? AND active = 1")
            .bind(guild_id.to_string()).bind(user_id.to_string()).execute(&self.pool).await?;
        Ok(())
    }

    pub async fn balance(&self, guild_id: u64, user_id: u64) -> Result<i64> {
        let result = sqlx::query_as::<_, (i64,)>(
            "SELECT coins FROM economy WHERE guild_id = ? AND user_id = ?",
        )
        .bind(guild_id.to_string())
        .bind(user_id.to_string())
        .fetch_optional(&self.pool)
        .await?;
        Ok(result.map(|row| row.0).unwrap_or(0))
    }

    pub async fn claim_daily(
        &self,
        guild_id: u64,
        user_id: u64,
        today: &str,
        reward: i64,
    ) -> Result<bool> {
        let result = sqlx::query("INSERT INTO economy(guild_id,user_id,coins,daily_date) VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET coins=coins+excluded.coins,daily_date=excluded.daily_date WHERE economy.daily_date IS NULL OR economy.daily_date != excluded.daily_date")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(reward).bind(today).execute(&self.pool).await?;
        Ok(result.rows_affected() > 0)
    }

    pub async fn claim_work(
        &self,
        guild_id: u64,
        user_id: u64,
        now: i64,
        cooldown_secs: i64,
        reward: i64,
    ) -> Result<bool> {
        let result = sqlx::query("INSERT INTO economy(guild_id,user_id,coins,work_at) VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET coins=coins+excluded.coins,work_at=excluded.work_at WHERE economy.work_at IS NULL OR economy.work_at <= ?")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(reward).bind(now).bind(now - cooldown_secs)
            .execute(&self.pool).await?;
        Ok(result.rows_affected() > 0)
    }

    pub async fn leaderboard(&self, guild_id: u64) -> Result<Vec<(String, i64)>> {
        Ok(sqlx::query_as(
            "SELECT user_id, coins FROM economy WHERE guild_id = ? ORDER BY coins DESC LIMIT 10",
        )
        .bind(guild_id.to_string())
        .fetch_all(&self.pool)
        .await?)
    }

    pub async fn warning_count(&self, guild_id: u64, user_id: u64) -> Result<i64> {
        let (count,): (i64,) =
            sqlx::query_as("SELECT count(*) FROM warnings WHERE guild_id=? AND user_id=?")
                .bind(guild_id.to_string())
                .bind(user_id.to_string())
                .fetch_one(&self.pool)
                .await?;
        Ok(count)
    }

    pub async fn remove_warning(
        &self,
        guild_id: u64,
        user_id: u64,
        warning_id: i64,
    ) -> Result<bool> {
        let result = sqlx::query("DELETE FROM warnings WHERE id=? AND guild_id=? AND user_id=?")
            .bind(warning_id)
            .bind(guild_id.to_string())
            .bind(user_id.to_string())
            .execute(&self.pool)
            .await?;
        Ok(result.rows_affected() != 0)
    }

    pub async fn log_moderation_action(
        &self,
        guild_id: u64,
        moderator_id: u64,
        action: &str,
        target_id: Option<u64>,
        channel_id: Option<u64>,
        reason: &str,
    ) -> Result<()> {
        sqlx::query("INSERT INTO moderation_actions(guild_id,moderator_id,action,target_id,channel_id,reason) VALUES(?,?,?,?,?,?)")
            .bind(guild_id.to_string()).bind(moderator_id.to_string()).bind(action)
            .bind(target_id.map(|id| id.to_string())).bind(channel_id.map(|id| id.to_string()))
            .bind(reason).execute(&self.pool).await?;
        Ok(())
    }

    pub async fn moderation_log(
        &self,
        guild_id: u64,
        limit: i64,
    ) -> Result<Vec<(i64, String, Option<String>, String, i64)>> {
        Ok(sqlx::query_as("SELECT id,action,target_id,reason,created_at FROM moderation_actions WHERE guild_id=? ORDER BY id DESC LIMIT ?")
            .bind(guild_id.to_string()).bind(limit.clamp(1, 50)).fetch_all(&self.pool).await?)
    }

    pub async fn transfer(
        &self,
        guild_id: u64,
        sender_id: u64,
        recipient_id: u64,
        amount: i64,
    ) -> Result<()> {
        anyhow::ensure!(
            sender_id != recipient_id,
            "Нельзя переводить монеты самому себе."
        );
        anyhow::ensure!(
            (1..=1_000_000_000).contains(&amount),
            "Сумма должна быть от 1 до 1 000 000 000."
        );
        let mut tx = self.pool.begin().await?;
        let guild = guild_id.to_string();
        let sender = sender_id.to_string();
        let recipient = recipient_id.to_string();
        sqlx::query("INSERT OR IGNORE INTO economy(guild_id,user_id) VALUES(?,?),(?,?)")
            .bind(&guild)
            .bind(&sender)
            .bind(&guild)
            .bind(&recipient)
            .execute(&mut *tx)
            .await?;
        let debited = sqlx::query(
            "UPDATE economy SET coins=coins-? WHERE guild_id=? AND user_id=? AND coins>=?",
        )
        .bind(amount)
        .bind(&guild)
        .bind(&sender)
        .bind(amount)
        .execute(&mut *tx)
        .await?;
        anyhow::ensure!(
            debited.rows_affected() == 1,
            "Недостаточно Vivian coins для перевода."
        );
        sqlx::query("UPDATE economy SET coins=coins+? WHERE guild_id=? AND user_id=?")
            .bind(amount)
            .bind(&guild)
            .bind(&recipient)
            .execute(&mut *tx)
            .await?;
        sqlx::query(
            "INSERT INTO economy_transfers(guild_id,sender_id,recipient_id,amount) VALUES(?,?,?,?)",
        )
        .bind(&guild)
        .bind(sender)
        .bind(recipient)
        .bind(amount)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok(())
    }

    pub async fn award_message_activity(
        &self,
        guild_id: u64,
        user_id: u64,
        content: &str,
        now: i64,
    ) -> Result<bool> {
        let normalized = content.trim().to_lowercase();
        let words: std::collections::HashSet<_> = normalized.split_whitespace().collect();
        use std::hash::{Hash, Hasher};
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        normalized.hash(&mut hasher);
        let digest = format!("{:016x}", hasher.finish());
        let guild = guild_id.to_string();
        let user = user_id.to_string();
        let day = chrono::DateTime::from_timestamp(now, 0)
            .unwrap_or_else(chrono::Utc::now)
            .with_timezone(&chrono_tz::Europe::Riga)
            .format("%Y-%m-%d")
            .to_string();
        let mut tx = self.pool.begin().await?;
        sqlx::query("INSERT OR IGNORE INTO member_progress(guild_id,user_id) VALUES(?,?)")
            .bind(&guild)
            .bind(&user)
            .execute(&mut *tx)
            .await?;
        sqlx::query("UPDATE member_progress SET message_count=message_count+1 WHERE guild_id=? AND user_id=?")
            .bind(&guild)
            .bind(&user)
            .execute(&mut *tx)
            .await?;
        if normalized.chars().count() < 12 || words.len() < 3 {
            tx.commit().await?;
            return Ok(false);
        }
        let (last_at, last_digest): (i64, Option<String>) = sqlx::query_as(
            "SELECT message_at,last_digest FROM member_progress WHERE guild_id=? AND user_id=?",
        )
        .bind(&guild)
        .bind(&user)
        .fetch_one(&mut *tx)
        .await?;
        if now < last_at.saturating_add(60) || last_digest.as_deref() == Some(digest.as_str()) {
            tx.commit().await?;
            return Ok(false);
        }
        sqlx::query("UPDATE member_progress SET xp=xp+15,message_at=?,last_digest=? WHERE guild_id=? AND user_id=?")
            .bind(now).bind(digest).bind(&guild).bind(&user).execute(&mut *tx).await?;
        sqlx::query("INSERT OR IGNORE INTO economy_daily_earnings(guild_id,user_id,day,amount) VALUES(?,?,?,0)")
            .bind(&guild).bind(&user).bind(&day).execute(&mut *tx).await?;
        let earned = sqlx::query("UPDATE economy_daily_earnings SET amount=amount+1 WHERE guild_id=? AND user_id=? AND day=? AND amount<100")
            .bind(&guild).bind(&user).bind(&day).execute(&mut *tx).await?;
        if earned.rows_affected() > 0 {
            sqlx::query("INSERT INTO economy(guild_id,user_id,coins) VALUES(?,?,1) ON CONFLICT(guild_id,user_id) DO UPDATE SET coins=coins+1")
                .bind(&guild).bind(&user).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(true)
    }

    pub async fn award_voice_activity(
        &self,
        guild_id: u64,
        user_id: u64,
        seconds: i64,
    ) -> Result<()> {
        let seconds = seconds.clamp(0, 3600);
        if seconds < 60 {
            return Ok(());
        }
        sqlx::query("INSERT INTO member_progress(guild_id,user_id,voice_seconds,xp) VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET voice_seconds=voice_seconds+excluded.voice_seconds,xp=xp+excluded.xp")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(seconds).bind((seconds / 60) * 5)
            .execute(&self.pool).await?;
        Ok(())
    }

    pub async fn progress(&self, guild_id: u64, user_id: u64) -> Result<(i64, i64, i64)> {
        let xp = sqlx::query_as::<_, (i64, i64)>(
            "SELECT xp,voice_seconds FROM member_progress WHERE guild_id=? AND user_id=?",
        )
        .bind(guild_id.to_string())
        .bind(user_id.to_string())
        .fetch_optional(&self.pool)
        .await?;
        let coins = self.balance(guild_id, user_id).await?;
        Ok(xp
            .map(|(xp, voice)| (xp, voice, coins))
            .unwrap_or((0, 0, coins)))
    }

    pub async fn xp_leaderboard(&self, guild_id: u64, offset: i64) -> Result<Vec<(String, i64)>> {
        Ok(sqlx::query_as("SELECT user_id,xp FROM member_progress WHERE guild_id=? ORDER BY xp DESC,user_id LIMIT 10 OFFSET ?")
            .bind(guild_id.to_string()).bind(offset.max(0)).fetch_all(&self.pool).await?)
    }

    pub async fn add_shop_item(
        &self,
        guild_id: u64,
        key: &str,
        name: &str,
        price: i64,
        role_id: u64,
        description: &str,
    ) -> Result<()> {
        anyhow::ensure!(
            !key.is_empty()
                && key.len() <= 32
                && key
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-'),
            "ID магазина: 1–32 символа a-z, цифры, _ или -."
        );
        anyhow::ensure!(
            (1..=1_000_000_000).contains(&price),
            "Цена должна быть от 1 до 1 000 000 000."
        );
        anyhow::ensure!(
            !name.trim().is_empty()
                && name.chars().count() <= 64
                && description.chars().count() <= 200,
            "Проверь длину названия или описания."
        );
        sqlx::query("INSERT INTO shop_items(guild_id,item_key,name,price,role_id,description) VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id,item_key) DO UPDATE SET name=excluded.name,price=excluded.price,role_id=excluded.role_id,description=excluded.description,updated_at=unixepoch()")
            .bind(guild_id.to_string()).bind(key).bind(name.trim()).bind(price).bind(role_id.to_string()).bind(description.trim())
            .execute(&self.pool).await?;
        Ok(())
    }

    pub async fn shop_items(
        &self,
        guild_id: u64,
    ) -> Result<Vec<(String, String, i64, String, String)>> {
        Ok(sqlx::query_as("SELECT item_key,name,price,role_id,description FROM shop_items WHERE guild_id=? ORDER BY item_key LIMIT 100")
            .bind(guild_id.to_string()).fetch_all(&self.pool).await?)
    }

    pub async fn remove_shop_item(&self, guild_id: u64, key: &str) -> Result<bool> {
        let result = sqlx::query("DELETE FROM shop_items WHERE guild_id=? AND item_key=?")
            .bind(guild_id.to_string())
            .bind(key)
            .execute(&self.pool)
            .await?;
        Ok(result.rows_affected() > 0)
    }

    pub async fn buy_shop_item(
        &self,
        guild_id: u64,
        user_id: u64,
        key: &str,
        purchase_id: &str,
    ) -> Result<(i64, u64)> {
        let mut tx = self.pool.begin().await?;
        let item = sqlx::query_as::<_, (i64, String)>(
            "SELECT price,role_id FROM shop_items WHERE guild_id=? AND item_key=?",
        )
        .bind(guild_id.to_string())
        .bind(key)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| anyhow::anyhow!("Такого товара нет в магазине."))?;
        let role_id: u64 = item.1.parse()?;
        let exists: Option<(i64,)> =
            sqlx::query_as("SELECT 1 FROM inventory WHERE guild_id=? AND user_id=? AND item_key=?")
                .bind(guild_id.to_string())
                .bind(user_id.to_string())
                .bind(key)
                .fetch_optional(&mut *tx)
                .await?;
        anyhow::ensure!(exists.is_none(), "Этот товар уже куплен.");
        sqlx::query("INSERT OR IGNORE INTO economy(guild_id,user_id) VALUES(?,?)")
            .bind(guild_id.to_string())
            .bind(user_id.to_string())
            .execute(&mut *tx)
            .await?;
        let charged = sqlx::query(
            "UPDATE economy SET coins=coins-? WHERE guild_id=? AND user_id=? AND coins>=?",
        )
        .bind(item.0)
        .bind(guild_id.to_string())
        .bind(user_id.to_string())
        .bind(item.0)
        .execute(&mut *tx)
        .await?;
        anyhow::ensure!(charged.rows_affected() == 1, "Недостаточно Vivian coins.");
        sqlx::query("INSERT INTO inventory(guild_id,user_id,item_key,purchase_id) VALUES(?,?,?,?)")
            .bind(guild_id.to_string())
            .bind(user_id.to_string())
            .bind(key)
            .bind(purchase_id)
            .execute(&mut *tx)
            .await?;
        let (balance,): (i64,) =
            sqlx::query_as("SELECT coins FROM economy WHERE guild_id=? AND user_id=?")
                .bind(guild_id.to_string())
                .bind(user_id.to_string())
                .fetch_one(&mut *tx)
                .await?;
        tx.commit().await?;
        Ok((balance, role_id))
    }

    pub async fn inventory(
        &self,
        guild_id: u64,
        user_id: u64,
    ) -> Result<Vec<(String, String, String)>> {
        Ok(sqlx::query_as("SELECT i.item_key,COALESCE(s.name,i.item_key),COALESCE(s.role_id,'') FROM inventory i LEFT JOIN shop_items s ON s.guild_id=i.guild_id AND s.item_key=i.item_key WHERE i.guild_id=? AND i.user_id=? ORDER BY i.purchased_at DESC LIMIT 50")
            .bind(guild_id.to_string()).bind(user_id.to_string()).fetch_all(&self.pool).await?)
    }

    pub async fn rollback_shop_purchase(
        &self,
        guild_id: u64,
        user_id: u64,
        key: &str,
        purchase_id: &str,
        price: i64,
    ) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        let deleted = sqlx::query(
            "DELETE FROM inventory WHERE guild_id=? AND user_id=? AND item_key=? AND purchase_id=?",
        )
        .bind(guild_id.to_string())
        .bind(user_id.to_string())
        .bind(key)
        .bind(purchase_id)
        .execute(&mut *tx)
        .await?;
        if deleted.rows_affected() > 0 {
            sqlx::query("UPDATE economy SET coins=coins+? WHERE guild_id=? AND user_id=?")
                .bind(price)
                .bind(guild_id.to_string())
                .bind(user_id.to_string())
                .execute(&mut *tx)
                .await?;
        }
        tx.commit().await?;
        Ok(())
    }

    pub async fn profile_status(&self, guild_id: u64, user_id: u64) -> Result<(String, String)> {
        Ok(
            sqlx::query_as("SELECT status,mood FROM profile_status WHERE guild_id=? AND user_id=?")
                .bind(guild_id.to_string())
                .bind(user_id.to_string())
                .fetch_optional(&self.pool)
                .await?
                .unwrap_or_default(),
        )
    }

    pub async fn set_profile_status(
        &self,
        guild_id: u64,
        user_id: u64,
        status: &str,
        mood: &str,
    ) -> Result<()> {
        anyhow::ensure!(
            status.chars().count() <= 120 && mood.chars().count() <= 32,
            "Статус должен быть до 120 символов, настроение — до 32."
        );
        sqlx::query("INSERT INTO profile_status(guild_id,user_id,status,mood) VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET status=excluded.status,mood=excluded.mood,updated_at=unixepoch()")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(status.trim()).bind(mood.trim()).execute(&self.pool).await?;
        Ok(())
    }

    pub async fn wall_privacy(&self, guild_id: u64, user_id: u64) -> Result<String> {
        let row: Option<(String,)> =
            sqlx::query_as("SELECT privacy FROM wall_settings WHERE guild_id=? AND user_id=?")
                .bind(guild_id.to_string())
                .bind(user_id.to_string())
                .fetch_optional(&self.pool)
                .await?;
        Ok(row.map(|row| row.0).unwrap_or_else(|| "all".to_owned()))
    }

    pub async fn set_wall_privacy(&self, guild_id: u64, user_id: u64, privacy: &str) -> Result<()> {
        anyhow::ensure!(
            ["all", "friends", "private"].contains(&privacy),
            "Видимость: all, friends или private."
        );
        sqlx::query("INSERT INTO wall_settings(guild_id,user_id,privacy) VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET privacy=excluded.privacy")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(privacy).execute(&self.pool).await?;
        Ok(())
    }

    async fn can_view_wall(&self, guild_id: u64, viewer: u64, owner: u64) -> Result<bool> {
        if viewer == owner {
            return Ok(true);
        }
        match self.wall_privacy(guild_id, owner).await?.as_str() {
            "all" => Ok(true),
            "private" => Ok(false),
            _ => {
                let row:Option<(i64,)>=sqlx::query_as("SELECT 1 FROM friends WHERE guild_id=? AND ((user_id=? AND friend_id=? AND status='accepted') OR (user_id=? AND friend_id=? AND status='accepted')) LIMIT 1")
                    .bind(guild_id.to_string()).bind(viewer.to_string()).bind(owner.to_string()).bind(owner.to_string()).bind(viewer.to_string()).fetch_optional(&self.pool).await?;
                Ok(row.is_some())
            }
        }
    }

    pub async fn create_wall_post(
        &self,
        guild_id: u64,
        author: u64,
        owner: u64,
        text: &str,
    ) -> Result<i64> {
        let text = text.trim();
        anyhow::ensure!(
            !text.is_empty() && text.chars().count() <= 800,
            "Запись стены должна содержать от 1 до 800 символов."
        );
        anyhow::ensure!(
            self.can_view_wall(guild_id, author, owner).await?,
            "Эта стена закрыта для тебя."
        );
        let result =
            sqlx::query("INSERT INTO wall_posts(guild_id,owner_id,author_id,text) VALUES(?,?,?,?)")
                .bind(guild_id.to_string())
                .bind(owner.to_string())
                .bind(author.to_string())
                .bind(text)
                .execute(&self.pool)
                .await?;
        Ok(result.last_insert_rowid())
    }

    pub async fn wall_posts(
        &self,
        guild_id: u64,
        viewer: u64,
        owner: u64,
        page: i64,
    ) -> Result<Vec<(i64, String, String, i64)>> {
        anyhow::ensure!(
            self.can_view_wall(guild_id, viewer, owner).await?,
            "Эта стена закрыта для тебя."
        );
        Ok(sqlx::query_as("SELECT id,author_id,text,created_at FROM wall_posts WHERE guild_id=? AND owner_id=? ORDER BY id DESC LIMIT 10 OFFSET ?")
            .bind(guild_id.to_string()).bind(owner.to_string()).bind(page.max(0)*10).fetch_all(&self.pool).await?)
    }

    pub async fn toggle_wall_like(
        &self,
        guild_id: u64,
        viewer: u64,
        post_id: i64,
    ) -> Result<(bool, i64)> {
        let post: Option<(String,)> =
            sqlx::query_as("SELECT owner_id FROM wall_posts WHERE guild_id=? AND id=?")
                .bind(guild_id.to_string())
                .bind(post_id)
                .fetch_optional(&self.pool)
                .await?;
        let (owner,) = post.ok_or_else(|| anyhow::anyhow!("Запись стены не найдена."))?;
        anyhow::ensure!(
            self.can_view_wall(guild_id, viewer, owner.parse()?).await?,
            "Эта стена закрыта для тебя."
        );
        let existing: Option<(i64,)> =
            sqlx::query_as("SELECT 1 FROM wall_likes WHERE guild_id=? AND post_id=? AND user_id=?")
                .bind(guild_id.to_string())
                .bind(post_id)
                .bind(viewer.to_string())
                .fetch_optional(&self.pool)
                .await?;
        let liked = if existing.is_some() {
            sqlx::query("DELETE FROM wall_likes WHERE guild_id=? AND post_id=? AND user_id=?")
                .bind(guild_id.to_string())
                .bind(post_id)
                .bind(viewer.to_string())
                .execute(&self.pool)
                .await?;
            false
        } else {
            sqlx::query("INSERT INTO wall_likes(guild_id,post_id,user_id) VALUES(?,?,?)")
                .bind(guild_id.to_string())
                .bind(post_id)
                .bind(viewer.to_string())
                .execute(&self.pool)
                .await?;
            true
        };
        let (count,): (i64,) =
            sqlx::query_as("SELECT count(*) FROM wall_likes WHERE guild_id=? AND post_id=?")
                .bind(guild_id.to_string())
                .bind(post_id)
                .fetch_one(&self.pool)
                .await?;
        Ok((liked, count))
    }

    pub async fn add_wall_comment(
        &self,
        guild_id: u64,
        author: u64,
        post_id: i64,
        text: &str,
    ) -> Result<i64> {
        let text = text.trim();
        anyhow::ensure!(
            !text.is_empty() && text.chars().count() <= 800,
            "Комментарий должен содержать от 1 до 800 символов."
        );
        let post: Option<(String,)> =
            sqlx::query_as("SELECT owner_id FROM wall_posts WHERE guild_id=? AND id=?")
                .bind(guild_id.to_string())
                .bind(post_id)
                .fetch_optional(&self.pool)
                .await?;
        let (owner,) = post.ok_or_else(|| anyhow::anyhow!("Запись стены не найдена."))?;
        anyhow::ensure!(
            self.can_view_wall(guild_id, author, owner.parse()?).await?,
            "Эта стена закрыта для тебя."
        );
        let result = sqlx::query(
            "INSERT INTO wall_comments(guild_id,post_id,author_id,text) VALUES(?,?,?,?)",
        )
        .bind(guild_id.to_string())
        .bind(post_id)
        .bind(author.to_string())
        .bind(text)
        .execute(&self.pool)
        .await?;
        Ok(result.last_insert_rowid())
    }

    pub async fn wall_comments(
        &self,
        guild_id: u64,
        viewer: u64,
        post_id: i64,
        page: i64,
    ) -> Result<Vec<(String, String, i64)>> {
        let post: Option<(String,)> =
            sqlx::query_as("SELECT owner_id FROM wall_posts WHERE guild_id=? AND id=?")
                .bind(guild_id.to_string())
                .bind(post_id)
                .fetch_optional(&self.pool)
                .await?;
        let (owner,) = post.ok_or_else(|| anyhow::anyhow!("Запись стены не найдена."))?;
        anyhow::ensure!(
            self.can_view_wall(guild_id, viewer, owner.parse()?).await?,
            "Эта стена закрыта для тебя."
        );
        Ok(sqlx::query_as("SELECT author_id,text,created_at FROM wall_comments WHERE guild_id=? AND post_id=? ORDER BY id LIMIT 10 OFFSET ?")
            .bind(guild_id.to_string()).bind(post_id).bind(page.max(0)*10).fetch_all(&self.pool).await?)
    }

    pub async fn delete_wall_post(&self, guild_id: u64, actor: u64, post_id: i64) -> Result<bool> {
        let result = sqlx::query(
            "DELETE FROM wall_posts WHERE guild_id=? AND id=? AND (owner_id=? OR author_id=?)",
        )
        .bind(guild_id.to_string())
        .bind(post_id)
        .bind(actor.to_string())
        .bind(actor.to_string())
        .execute(&self.pool)
        .await?;
        if result.rows_affected() > 0 {
            sqlx::query("DELETE FROM wall_likes WHERE guild_id=? AND post_id=?")
                .bind(guild_id.to_string())
                .bind(post_id)
                .execute(&self.pool)
                .await?;
            sqlx::query("DELETE FROM wall_comments WHERE guild_id=? AND post_id=?")
                .bind(guild_id.to_string())
                .bind(post_id)
                .execute(&self.pool)
                .await?;
        }
        Ok(result.rows_affected() > 0)
    }

    pub async fn relationship(&self, guild_id: u64, user_id: u64) -> Result<Option<u64>> {
        let row: Option<(String,)> =
            sqlx::query_as("SELECT partner_id FROM relationships WHERE guild_id=? AND user_id=?")
                .bind(guild_id.to_string())
                .bind(user_id.to_string())
                .fetch_optional(&self.pool)
                .await?;
        row.map(|(id,)| Ok(id.parse()?)).transpose()
    }

    pub async fn request_relationship(
        &self,
        guild_id: u64,
        requester: u64,
        target: u64,
    ) -> Result<()> {
        anyhow::ensure!(requester != target, "Нельзя отправить запрос самому себе.");
        anyhow::ensure!(
            self.relationship(guild_id, requester).await?.is_none()
                && self.relationship(guild_id, target).await?.is_none(),
            "Один из участников уже состоит в отношениях."
        );
        sqlx::query("INSERT INTO relationship_requests(guild_id,requester_id,target_id) VALUES(?,?,?) ON CONFLICT(guild_id,requester_id,target_id) DO UPDATE SET created_at=unixepoch()")
            .bind(guild_id.to_string()).bind(requester.to_string()).bind(target.to_string()).execute(&self.pool).await?;
        Ok(())
    }

    pub async fn respond_relationship(
        &self,
        guild_id: u64,
        target: u64,
        requester: u64,
        accept: bool,
    ) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        let req: Option<(i64,)> = sqlx::query_as("SELECT id FROM relationship_requests WHERE guild_id=? AND requester_id=? AND target_id=?")
            .bind(guild_id.to_string()).bind(requester.to_string()).bind(target.to_string()).fetch_optional(&mut *tx).await?;
        anyhow::ensure!(req.is_some(), "Входящий запрос отношений не найден.");
        if accept {
            let taken: Option<(i64,)> = sqlx::query_as("SELECT 1 FROM relationships WHERE guild_id=? AND user_id IN (?,?) OR guild_id=? AND partner_id IN (?,?)")
                .bind(guild_id.to_string()).bind(requester.to_string()).bind(target.to_string()).bind(guild_id.to_string()).bind(requester.to_string()).bind(target.to_string()).fetch_optional(&mut *tx).await?;
            anyhow::ensure!(
                taken.is_none(),
                "Один из участников уже состоит в отношениях."
            );
            sqlx::query(
                "INSERT INTO relationships(guild_id,user_id,partner_id) VALUES(?,?,?),(?,?,?)",
            )
            .bind(guild_id.to_string())
            .bind(requester.to_string())
            .bind(target.to_string())
            .bind(guild_id.to_string())
            .bind(target.to_string())
            .bind(requester.to_string())
            .execute(&mut *tx)
            .await?;
        }
        sqlx::query("DELETE FROM relationship_requests WHERE guild_id=? AND ((requester_id=? AND target_id=?) OR (requester_id=? AND target_id=?))")
            .bind(guild_id.to_string()).bind(requester.to_string()).bind(target.to_string()).bind(target.to_string()).bind(requester.to_string()).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }

    pub async fn end_relationship(&self, guild_id: u64, user_id: u64) -> Result<bool> {
        let result = sqlx::query(
            "DELETE FROM relationships WHERE guild_id=? AND (user_id=? OR partner_id=?)",
        )
        .bind(guild_id.to_string())
        .bind(user_id.to_string())
        .bind(user_id.to_string())
        .execute(&self.pool)
        .await?;
        sqlx::query("DELETE FROM relationship_requests WHERE guild_id=? AND (requester_id=? OR target_id=?)")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(user_id.to_string()).execute(&self.pool).await?;
        Ok(result.rows_affected() > 0)
    }

    pub async fn send_poke(
        &self,
        guild_id: u64,
        sender: u64,
        target: u64,
        now: i64,
    ) -> Result<u64> {
        anyhow::ensure!(sender != target, "Нельзя пинговать самого себя.");
        let result = sqlx::query("INSERT INTO pokes(guild_id,sender_id,target_id,count,last_at) VALUES(?,?,?,1,?) ON CONFLICT(guild_id,sender_id,target_id) DO UPDATE SET count=count+1,last_at=excluded.last_at WHERE pokes.last_at<=?")
            .bind(guild_id.to_string()).bind(sender.to_string()).bind(target.to_string()).bind(now).bind(now-60).execute(&self.pool).await?;
        anyhow::ensure!(
            result.rows_affected() == 1,
            "Эту пару можно пинговать раз в минуту."
        );
        let (count,): (i64,) = sqlx::query_as(
            "SELECT count FROM pokes WHERE guild_id=? AND sender_id=? AND target_id=?",
        )
        .bind(guild_id.to_string())
        .bind(sender.to_string())
        .bind(target.to_string())
        .fetch_one(&self.pool)
        .await?;
        Ok(count as u64)
    }

    pub async fn request_friend(&self, guild_id: u64, sender: u64, target: u64) -> Result<()> {
        anyhow::ensure!(sender != target, "Нельзя добавить себя в друзья.");
        let exists: Option<(String,)> = sqlx::query_as("SELECT status FROM friends WHERE guild_id=? AND ((user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)) LIMIT 1")
            .bind(guild_id.to_string()).bind(sender.to_string()).bind(target.to_string())
            .bind(target.to_string()).bind(sender.to_string()).fetch_optional(&self.pool).await?;
        anyhow::ensure!(
            exists.as_ref().is_none_or(|(status,)| status != "accepted"),
            "Вы уже друзья."
        );
        sqlx::query("INSERT INTO friends(guild_id,user_id,friend_id,status) VALUES(?,?,?,'pending') ON CONFLICT(guild_id,user_id,friend_id) DO UPDATE SET status='pending',created_at=unixepoch()")
            .bind(guild_id.to_string()).bind(sender.to_string()).bind(target.to_string()).execute(&self.pool).await?;
        Ok(())
    }

    pub async fn respond_friend(
        &self,
        guild_id: u64,
        actor: u64,
        other: u64,
        accept: bool,
    ) -> Result<()> {
        let mut tx = self.pool.begin().await?;
        let request: Option<(i64,)> = sqlx::query_as("SELECT 1 FROM friends WHERE guild_id=? AND user_id=? AND friend_id=? AND status='pending'")
            .bind(guild_id.to_string()).bind(other.to_string()).bind(actor.to_string()).fetch_optional(&mut *tx).await?;
        anyhow::ensure!(request.is_some(), "Входящий запрос дружбы не найден.");
        if accept {
            sqlx::query("UPDATE friends SET status='accepted' WHERE guild_id=? AND user_id=? AND friend_id=?")
                .bind(guild_id.to_string()).bind(other.to_string()).bind(actor.to_string()).execute(&mut *tx).await?;
            sqlx::query("INSERT INTO friends(guild_id,user_id,friend_id,status) VALUES(?,?,?,'accepted') ON CONFLICT(guild_id,user_id,friend_id) DO UPDATE SET status='accepted'")
                .bind(guild_id.to_string()).bind(actor.to_string()).bind(other.to_string()).execute(&mut *tx).await?;
        } else {
            sqlx::query("DELETE FROM friends WHERE guild_id=? AND user_id=? AND friend_id=? AND status='pending'")
                .bind(guild_id.to_string()).bind(other.to_string()).bind(actor.to_string()).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(())
    }

    pub async fn friends(&self, guild_id: u64, user_id: u64) -> Result<Vec<String>> {
        let rows=sqlx::query_as::<_,(String,)>("SELECT friend_id FROM friends WHERE guild_id=? AND user_id=? AND status='accepted' ORDER BY created_at DESC LIMIT 100")
            .bind(guild_id.to_string()).bind(user_id.to_string()).fetch_all(&self.pool).await?;
        Ok(rows.into_iter().map(|row| row.0).collect())
    }

    pub async fn incoming_friend_requests(
        &self,
        guild_id: u64,
        user_id: u64,
    ) -> Result<Vec<String>> {
        let rows=sqlx::query_as::<_,(String,)>("SELECT user_id FROM friends WHERE guild_id=? AND friend_id=? AND status='pending' ORDER BY created_at DESC LIMIT 50")
            .bind(guild_id.to_string()).bind(user_id.to_string()).fetch_all(&self.pool).await?;
        Ok(rows.into_iter().map(|row| row.0).collect())
    }

    pub async fn remove_friend(&self, guild_id: u64, user_id: u64, other: u64) -> Result<bool> {
        let result = sqlx::query("DELETE FROM friends WHERE guild_id=? AND ((user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?))")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(other.to_string()).bind(other.to_string()).bind(user_id.to_string()).execute(&self.pool).await?;
        Ok(result.rows_affected() > 0)
    }

    pub async fn pick_crush(
        &self,
        guild_id: u64,
        user_id: u64,
        target: u64,
        week: &str,
    ) -> Result<(u64, bool)> {
        anyhow::ensure!(user_id != target, "Выбери другого участника.");
        let mut tx = self.pool.begin().await?;
        let prior: Option<(i64,)> = sqlx::query_as(
            "SELECT 1 FROM crush_choices WHERE guild_id=? AND user_id=? AND target_id=? AND week=?",
        )
        .bind(guild_id.to_string())
        .bind(user_id.to_string())
        .bind(target.to_string())
        .bind(week)
        .fetch_optional(&mut *tx)
        .await?;
        if prior.is_none() {
            let (count,): (i64,) = sqlx::query_as(
                "SELECT count(*) FROM crush_choices WHERE guild_id=? AND user_id=? AND week=?",
            )
            .bind(guild_id.to_string())
            .bind(user_id.to_string())
            .bind(week)
            .fetch_one(&mut *tx)
            .await?;
            anyhow::ensure!(
                count < 3,
                "На этой неделе можно выбрать не больше трёх разных участников."
            );
            sqlx::query(
                "INSERT INTO crush_choices(guild_id,user_id,target_id,week) VALUES(?,?,?,?)",
            )
            .bind(guild_id.to_string())
            .bind(user_id.to_string())
            .bind(target.to_string())
            .bind(week)
            .execute(&mut *tx)
            .await?;
        }
        sqlx::query("INSERT INTO crush_selection(guild_id,user_id,target_id,week) VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET target_id=excluded.target_id,week=excluded.week")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(target.to_string()).bind(week).execute(&mut *tx).await?;
        let mutual: Option<(i64,)> = sqlx::query_as("SELECT 1 FROM crush_selection WHERE guild_id=? AND user_id=? AND target_id=? AND week=?")
            .bind(guild_id.to_string()).bind(target.to_string()).bind(user_id.to_string()).bind(week).fetch_optional(&mut *tx).await?;
        let (count,): (i64,) = sqlx::query_as(
            "SELECT count(*) FROM crush_choices WHERE guild_id=? AND user_id=? AND week=?",
        )
        .bind(guild_id.to_string())
        .bind(user_id.to_string())
        .bind(week)
        .fetch_one(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok((count as u64, mutual.is_some()))
    }

    pub async fn show_crush(
        &self,
        guild_id: u64,
        user_id: u64,
    ) -> Result<Option<(String, String, i64)>> {
        Ok(sqlx::query_as("SELECT s.target_id,s.week,(SELECT count(*) FROM crush_choices c WHERE c.guild_id=s.guild_id AND c.user_id=s.user_id AND c.week=s.week) FROM crush_selection s WHERE s.guild_id=? AND s.user_id=?")
            .bind(guild_id.to_string()).bind(user_id.to_string()).fetch_optional(&self.pool).await?)
    }

    pub async fn clear_crush(&self, guild_id: u64, user_id: u64) -> Result<bool> {
        let result = sqlx::query("DELETE FROM crush_selection WHERE guild_id=? AND user_id=?")
            .bind(guild_id.to_string())
            .bind(user_id.to_string())
            .execute(&self.pool)
            .await?;
        Ok(result.rows_affected() > 0)
    }

    pub async fn start_word_game(
        &self,
        guild_id: u64,
        channel_id: u64,
        starter: u64,
        answer: &str,
        hint: &str,
    ) -> Result<()> {
        let created=sqlx::query("INSERT INTO word_games(guild_id,channel_id,starter_id,answer,hint,state) VALUES(?,?,?,?,?,'active') ON CONFLICT(guild_id,channel_id) DO UPDATE SET starter_id=excluded.starter_id,answer=excluded.answer,hint=excluded.hint,state='active',winner_id=NULL,created_at=unixepoch() WHERE word_games.state='finished'")
            .bind(guild_id.to_string()).bind(channel_id.to_string()).bind(starter.to_string()).bind(answer.to_lowercase()).bind(hint).execute(&self.pool).await?;
        anyhow::ensure!(
            created.rows_affected() == 1,
            "В этом канале уже идёт word hunt; заверши её через `/wordhunt stop`."
        );
        sqlx::query("DELETE FROM word_game_players WHERE guild_id=? AND channel_id=?")
            .bind(guild_id.to_string())
            .bind(channel_id.to_string())
            .execute(&self.pool)
            .await?;
        sqlx::query("INSERT INTO word_game_players(guild_id,channel_id,user_id) VALUES(?,?,?)")
            .bind(guild_id.to_string())
            .bind(channel_id.to_string())
            .bind(starter.to_string())
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn stop_word_game(&self, guild_id: u64, channel_id: u64, actor: u64) -> Result<bool> {
        let result=sqlx::query("UPDATE word_games SET state='finished' WHERE guild_id=? AND channel_id=? AND starter_id=? AND state='active'")
            .bind(guild_id.to_string()).bind(channel_id.to_string()).bind(actor.to_string()).execute(&self.pool).await?;
        Ok(result.rows_affected() > 0)
    }

    pub async fn guess_word(
        &self,
        guild_id: u64,
        channel_id: u64,
        user_id: u64,
        guess: &str,
    ) -> Result<Option<bool>> {
        let game = sqlx::query_as::<_, (String, String)>(
            "SELECT answer,state FROM word_games WHERE guild_id=? AND channel_id=?",
        )
        .bind(guild_id.to_string())
        .bind(channel_id.to_string())
        .fetch_optional(&self.pool)
        .await?;
        let Some((answer, state)) = game else {
            anyhow::bail!("В этом канале нет активной игры.");
        };
        anyhow::ensure!(state == "active", "Игра уже завершена.");
        sqlx::query(
            "INSERT OR IGNORE INTO word_game_players(guild_id,channel_id,user_id) VALUES(?,?,?)",
        )
        .bind(guild_id.to_string())
        .bind(channel_id.to_string())
        .bind(user_id.to_string())
        .execute(&self.pool)
        .await?;
        let correct = answer.eq_ignore_ascii_case(guess.trim());
        if correct {
            let mut tx = self.pool.begin().await?;
            let winner=sqlx::query("UPDATE word_games SET state='finished',winner_id=? WHERE guild_id=? AND channel_id=? AND state='active'")
                .bind(user_id.to_string()).bind(guild_id.to_string()).bind(channel_id.to_string()).execute(&mut *tx).await?;
            if winner.rows_affected() == 0 {
                tx.rollback().await?;
                return Ok(None);
            }
            sqlx::query("UPDATE word_game_players SET score=score+1 WHERE guild_id=? AND channel_id=? AND user_id=?")
                .bind(guild_id.to_string()).bind(channel_id.to_string()).bind(user_id.to_string()).execute(&mut *tx).await?;
            sqlx::query("INSERT INTO economy(guild_id,user_id,coins) VALUES(?,?,50) ON CONFLICT(guild_id,user_id) DO UPDATE SET coins=coins+50")
                .bind(guild_id.to_string()).bind(user_id.to_string()).execute(&mut *tx).await?;
            tx.commit().await?;
            Ok(Some(true))
        } else {
            Ok(Some(false))
        }
    }

    pub async fn word_game_hint(&self, guild_id: u64, channel_id: u64) -> Result<Option<String>> {
        Ok(sqlx::query_as::<_, (String,)>(
            "SELECT hint FROM word_games WHERE guild_id=? AND channel_id=? AND state='active'",
        )
        .bind(guild_id.to_string())
        .bind(channel_id.to_string())
        .fetch_optional(&self.pool)
        .await?
        .map(|row| row.0))
    }

    pub async fn record_automod_incident(
        &self,
        guild_id: u64,
        user_id: u64,
        now: i64,
    ) -> Result<bool> {
        let previous: Option<(i64,)> = sqlx::query_as(
            "SELECT last_incident FROM automod_state WHERE guild_id=? AND user_id=?",
        )
        .bind(guild_id.to_string())
        .bind(user_id.to_string())
        .fetch_optional(&self.pool)
        .await?;
        sqlx::query("INSERT INTO automod_state(guild_id,user_id,last_incident) VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET last_incident=excluded.last_incident")
            .bind(guild_id.to_string()).bind(user_id.to_string()).bind(now).execute(&self.pool).await?;
        Ok(previous.is_some_and(|(last,)| now - last <= 600))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn private_room_and_playlist_records_persist_and_release() {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        let store = Store { pool };
        store.migrate().await.unwrap();

        let room = store.reserve_room(12, 34).await.unwrap();
        assert_eq!(room.owner_id, 34);
        store.activate_room(room.token, 56).await.unwrap();
        assert_eq!(
            store.room_by_channel(12, 56).await.unwrap().unwrap().token,
            room.token
        );
        store.transfer_room(room.token, 78).await.unwrap();
        assert_eq!(
            store
                .room_by_owner(12, 78)
                .await
                .unwrap()
                .unwrap()
                .channel_id,
            Some(56)
        );
        store.release_room(room.token).await.unwrap();
        assert!(store.room_by_owner(12, 78).await.unwrap().is_none());

        store.save_playlist(12, 34, "Focus", "[]").await.unwrap();
        assert_eq!(
            store.playlists(12, 34).await.unwrap(),
            vec![("Focus".to_owned(), "[]".to_owned())]
        );
        assert!(store.delete_playlist(12, 34, "FOCUS").await.unwrap());
        assert!(store.playlists(12, 34).await.unwrap().is_empty());
    }

    async fn test_store() -> Store {
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect("sqlite::memory:")
            .await
            .unwrap();
        let store = Store { pool };
        store.migrate().await.unwrap();
        store
    }

    #[tokio::test]
    async fn transfers_are_atomic_and_message_rewards_obey_cooldown_and_deduplication() {
        let store = test_store().await;
        assert!(store.claim_daily(7, 10, "d1", 100).await.unwrap());
        store.transfer(7, 10, 11, 40).await.unwrap();
        assert_eq!(store.balance(7, 10).await.unwrap(), 60);
        assert_eq!(store.balance(7, 11).await.unwrap(), 40);
        assert!(store.transfer(7, 10, 11, 100).await.is_err());
        assert!(store
            .award_message_activity(7, 10, "a sufficiently useful message", 1000)
            .await
            .unwrap());
        assert!(!store
            .award_message_activity(7, 10, "another useful message content", 1030)
            .await
            .unwrap());
        assert!(!store
            .award_message_activity(7, 10, "a sufficiently useful message", 1060)
            .await
            .unwrap());
        assert!(store
            .award_message_activity(7, 10, "yet another useful message content", 1061)
            .await
            .unwrap());
        let (xp, _, coins) = store.progress(7, 10).await.unwrap();
        assert_eq!(xp, 30);
        assert_eq!(coins, 62);
        let messages: i64 = sqlx::query_scalar(
            "SELECT message_count FROM member_progress WHERE guild_id='7' AND user_id='10'",
        )
        .fetch_one(&store.pool)
        .await
        .unwrap();
        assert_eq!(messages, 4);
    }

    #[tokio::test]
    async fn birthdays_validate_dates_sort_upcoming_and_claim_daily_work_once() {
        let store = test_store().await;
        store.set_birthday(7, 10, 25, 11, None).await.unwrap();
        store.set_birthday(7, 11, 1, 1, Some(2000)).await.unwrap();
        store.set_birthday(7, 12, 29, 2, None).await.unwrap();
        assert!(store.set_birthday(7, 12, 31, 4, None).await.is_err());
        assert_eq!(store.birthdays_for_day(7, 11, 25).await.unwrap(), vec![10]);
        if chrono::NaiveDate::from_ymd_opt(chrono::Utc::now().year(), 2, 29).is_none() {
            assert_eq!(store.birthdays_for_day(7, 3, 1).await.unwrap(), vec![12]);
        }
        assert_eq!(
            store
                .upcoming_birthdays(7, chrono::Utc::now().year(), 11, 22, 10)
                .await
                .unwrap(),
            vec![(10, 25, 11), (11, 1, 1), (12, 29, 2)]
        );
        assert!(store
            .claim_birthday_announcement(7, 10, "2026-11-25")
            .await
            .unwrap());
        assert!(!store
            .claim_birthday_announcement(7, 10, "2026-11-25")
            .await
            .unwrap());
        assert!(store
            .claim_birthday_announcement(7, 11, "2026-11-25")
            .await
            .unwrap());
        store
            .set_birthday_active_role(7, 10, 99, 1234)
            .await
            .unwrap();
        assert!(store.due_birthday_roles(1233).await.unwrap().is_empty());
        assert_eq!(
            store.due_birthday_roles(1234).await.unwrap(),
            vec![(7, 10, 99)]
        );
        store.clear_birthday_role(7, 10).await.unwrap();
        assert!(store.due_birthday_roles(9999).await.unwrap().is_empty());
        assert!(store.remove_birthday(7, 10).await.unwrap());
        assert!(store.birthdays_for_day(7, 11, 25).await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn relationship_requires_target_consent_and_shop_purchase_is_recorded() {
        let store = test_store().await;
        store.request_relationship(9, 1, 2).await.unwrap();
        assert_eq!(store.relationship(9, 1).await.unwrap(), None);
        store.respond_relationship(9, 2, 1, true).await.unwrap();
        assert_eq!(store.relationship(9, 1).await.unwrap(), Some(2));
        assert_eq!(store.relationship(9, 2).await.unwrap(), Some(1));
        assert!(store.end_relationship(9, 1).await.unwrap());
        store.claim_daily(9, 1, "first", 500).await.unwrap();
        store
            .add_shop_item(9, "blue", "Blue role", 150, 123, "Cosmetic")
            .await
            .unwrap();
        let (balance, role) = store
            .buy_shop_item(9, 1, "blue", "purchase-1")
            .await
            .unwrap();
        assert_eq!((balance, role), (350, 123));
        assert_eq!(store.inventory(9, 1).await.unwrap().len(), 1);
        store
            .rollback_shop_purchase(9, 1, "blue", "purchase-1", 150)
            .await
            .unwrap();
        assert_eq!(store.balance(9, 1).await.unwrap(), 500);
        assert!(store.inventory(9, 1).await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn word_game_has_single_winner_and_reward() {
        let store = test_store().await;
        store
            .start_word_game(2, 3, 4, "comet", "space rock")
            .await
            .unwrap();
        assert_eq!(
            store.word_game_hint(2, 3).await.unwrap().as_deref(),
            Some("space rock")
        );
        assert_eq!(
            store.guess_word(2, 3, 5, "wrong").await.unwrap(),
            Some(false)
        );
        assert_eq!(
            store.guess_word(2, 3, 5, "COMET").await.unwrap(),
            Some(true)
        );
        assert_eq!(store.balance(2, 5).await.unwrap(), 50);
        assert_eq!(
            store
                .guess_word(2, 3, 6, "comet")
                .await
                .unwrap_err()
                .to_string(),
            "Игра уже завершена."
        );
    }

    #[tokio::test]
    async fn friend_requests_require_acceptance_and_crush_choices_enforce_weekly_limit() {
        let store = test_store().await;
        store.request_friend(4, 10, 11).await.unwrap();
        assert_eq!(store.friends(4, 10).await.unwrap().len(), 0);
        assert_eq!(
            store.incoming_friend_requests(4, 11).await.unwrap(),
            vec!["10".to_owned()]
        );
        store.respond_friend(4, 11, 10, true).await.unwrap();
        assert_eq!(store.friends(4, 10).await.unwrap(), vec!["11".to_owned()]);
        store.pick_crush(4, 10, 11, "2026-47").await.unwrap();
        store.pick_crush(4, 10, 12, "2026-47").await.unwrap();
        store.pick_crush(4, 10, 13, "2026-47").await.unwrap();
        assert!(store.pick_crush(4, 10, 14, "2026-47").await.is_err());
        assert!(store.clear_crush(4, 10).await.unwrap());
        assert_eq!(store.show_crush(4, 10).await.unwrap(), None);
    }

    #[tokio::test]
    async fn wall_privacy_is_enforced_for_posts_comments_and_likes() {
        let store = test_store().await;
        let post = store
            .create_wall_post(1, 7, 7, "hello from the wall")
            .await
            .unwrap();
        assert_eq!(store.wall_posts(1, 8, 7, 0).await.unwrap().len(), 1);
        store.set_wall_privacy(1, 7, "friends").await.unwrap();
        assert!(store.wall_posts(1, 8, 7, 0).await.is_err());
        store.request_friend(1, 8, 7).await.unwrap();
        store.respond_friend(1, 7, 8, true).await.unwrap();
        assert_eq!(store.wall_posts(1, 8, 7, 0).await.unwrap().len(), 1);
        store.add_wall_comment(1, 8, post, "nice").await.unwrap();
        assert_eq!(store.wall_comments(1, 8, post, 0).await.unwrap().len(), 1);
        assert_eq!(store.toggle_wall_like(1, 8, post).await.unwrap(), (true, 1));
        store.set_wall_privacy(1, 7, "private").await.unwrap();
        assert!(store.wall_comments(1, 8, post, 0).await.is_err());
        assert!(store.toggle_wall_like(1, 8, post).await.is_err());
        assert!(store.delete_wall_post(1, 7, post).await.unwrap());
    }
}
