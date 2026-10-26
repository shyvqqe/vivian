"""Durable reservations for private voice rooms, outside the public Data API."""
from __future__ import annotations


SQL = (
    "CREATE SCHEMA IF NOT EXISTS esil_internal",
    "REVOKE ALL ON SCHEMA esil_internal FROM PUBLIC",
    """CREATE TABLE IF NOT EXISTS esil_internal.private_voice_rooms (
        guild_id BIGINT NOT NULL CHECK (guild_id > 0),
        owner_id BIGINT NOT NULL CHECK (owner_id > 0),
        channel_id BIGINT CHECK (channel_id > 0),
        state TEXT NOT NULL CHECK (state IN ('creating','active','deleting')),
        token TEXT NOT NULL CHECK (char_length(token) = 36),
        panel_channel_id BIGINT CHECK (panel_channel_id > 0),
        panel_message_id BIGINT CHECK (panel_message_id > 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (guild_id, owner_id),
        UNIQUE (guild_id, channel_id),
        CHECK ((state = 'creating' AND channel_id IS NULL)
            OR (state IN ('active','deleting') AND channel_id IS NOT NULL))
    )""",
    # The unique (guild_id, channel_id) index serves channel lookup as well.
    """CREATE INDEX IF NOT EXISTS private_voice_rooms_state_updated
        ON esil_internal.private_voice_rooms (state, updated_at)""",
    "ALTER TABLE esil_internal.private_voice_rooms ENABLE ROW LEVEL SECURITY",
    "REVOKE ALL ON esil_internal.private_voice_rooms FROM PUBLIC",
    """DO $vivian_rooms$ DECLARE api_role TEXT; BEGIN
        FOREACH api_role IN ARRAY ARRAY['anon','authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
                EXECUTE format('REVOKE ALL ON SCHEMA esil_internal FROM %I', api_role);
                EXECUTE format('REVOKE ALL ON esil_internal.private_voice_rooms FROM %I', api_role);
            END IF;
        END LOOP;
    END $vivian_rooms$""",
)
