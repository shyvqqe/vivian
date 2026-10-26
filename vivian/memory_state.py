from __future__ import annotations
import re


AI_MEMORY_DAYS = 30


AI_MEMORY_FACT_DAYS = 90


AI_MEMORY_CHANNEL_LIMIT = 2000


AI_MEMORY_CACHE_LIMIT = 1000


AI_MEMORY_MAX_SCOPES = 32


AI_MEMORY_FACT_LIMIT = 16


AI_MEMORY_PROMPT_CHARS = 18000


AI_MEMORY_CHANNELS = {}


AI_MEMORY_FACTS = {}


AI_MEMORY_LOADED = set()


AI_MEMORY_LOAD_LOCKS = {}


AI_MEMORY_LAST_USED = {}


AI_MEMORY_CLEANED_AT = {}


AI_MEMORY_READY = False


AI_MEMORY_LOAD_RETRY = {}


AI_MEMORY_PENDING = {}


AI_MEMORY_LAST_MAINTENANCE = 0.0


AI_MEMORY_LAST_OPEN_ATTEMPT = 0.0


AI_MEMORY_SECRET_PATTERN = re.compile(
    r"(?:\b(?:пароль|password|api[_ -]?key|token|токен|секретный\s+ключ)\b|"
    r"(?:sk-|gsk_|xai-)[A-Za-z0-9_-]{12,}|postgres(?:ql)?://|"
    r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b)", re.IGNORECASE,
)


AI_MEMORY_STOP_WORDS = set(
    "vivian вивиан привет хорошо плохо спасибо пожалуйста вообще просто сейчас потом сегодня "
    "вчера завтра какие какой какая какому почему потому чтобы только очень меня тебе тебя "
    "мне было были будет делать скажи ответь помнишь помни запомни спросил спросила вопрос "
    "сообщение собеседник этом этот этом такое такой тоже уже когда сколько ничего всё все".split()
)


AI_MEMORY_SCHEMA_SQL = (
    "CREATE SCHEMA IF NOT EXISTS elis_private",
    "REVOKE ALL ON SCHEMA elis_private FROM PUBLIC",
    """CREATE TABLE IF NOT EXISTS elis_private.chat_messages (
        guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
        author_id BIGINT NOT NULL, audience_id BIGINT NOT NULL DEFAULT 0,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        display_name TEXT NOT NULL, content TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (guild_id, channel_id, message_id)
    )""",
    """CREATE INDEX IF NOT EXISTS chat_messages_recent_idx ON elis_private.chat_messages
        (guild_id, channel_id, created_at DESC, message_id DESC)""",
    """CREATE INDEX IF NOT EXISTS chat_messages_personal_idx ON elis_private.chat_messages
        (guild_id, channel_id, audience_id, created_at DESC, message_id DESC) WHERE audience_id <> 0""",
    "CREATE INDEX IF NOT EXISTS chat_messages_expiry_idx ON elis_private.chat_messages (created_at)",
    """CREATE TABLE IF NOT EXISTS elis_private.member_facts (
        guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
        fact_key TEXT NOT NULL, content TEXT NOT NULL, source_message_id BIGINT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (guild_id, channel_id, user_id, fact_key)
    )""",
    "CREATE INDEX IF NOT EXISTS member_facts_expiry_idx ON elis_private.member_facts (updated_at)",
    "ALTER TABLE elis_private.chat_messages ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE elis_private.member_facts ENABLE ROW LEVEL SECURITY",
    "REVOKE ALL ON ALL TABLES IN SCHEMA elis_private FROM PUBLIC",
    """DO $vivian_memory$
    DECLARE api_role TEXT;
    BEGIN
        FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
                EXECUTE format('REVOKE ALL ON SCHEMA elis_private FROM %I', api_role);
                EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA elis_private FROM %I', api_role);
            END IF;
        END LOOP;
    END $vivian_memory$""",
)
