from __future__ import annotations

from core import hub_store


_MARKER = "life-november-2026"
_COPY = (
    """INSERT INTO vivian_private.feature_records(guild_id, namespace, key, payload)
    SELECT guild_id, 'life', 'birthday:' || user_id,
        jsonb_build_object('user_id', user_id, 'day', day, 'month', month,
            'year', CASE WHEN year BETWEEN 1900 AND extract(year FROM current_date) THEN year END)
    FROM vivian_features.birthdays WHERE guild_id=%s
    AND day <= CASE WHEN month=2 THEN 29 WHEN month IN (4,6,9,11) THEN 30 ELSE 31 END
    ON CONFLICT DO NOTHING""",
    """INSERT INTO vivian_private.feature_records(guild_id, namespace, key, payload)
    SELECT guild_id, 'life', 'birthday-role:legacy-' || user_id,
        jsonb_build_object('user_id', user_id, 'role_id', role_id, 'expires_at', expires_at, 'owned', true)
    FROM vivian_features.birthday_active_roles WHERE guild_id=%s
    ON CONFLICT DO NOTHING""",
    """INSERT INTO vivian_private.feature_records(guild_id, namespace, key, payload)
    SELECT stories.guild_id, 'life', 'story:legacy-' || stories.id,
        jsonb_build_object('id', 'legacy-' || stories.id, 'author_id', stories.author_id,
            'channel_id', stories.channel_id, 'message_id', stories.message_id,
            'created_at', stories.created_at, 'expires_at', stories.expires_at,
            'status', 'active', 'viewers', COALESCE(views.items, '{}'::jsonb))
    FROM vivian_features.stories AS stories
    LEFT JOIN LATERAL (
        SELECT jsonb_object_agg(user_id, floor(viewed_at)) AS items
        FROM (SELECT user_id, viewed_at FROM vivian_features.story_views
              WHERE story_id=stories.id ORDER BY viewed_at, user_id LIMIT 1000) AS recent
    ) AS views ON true
    WHERE stories.guild_id=%s AND stories.deleted=0 AND stories.message_id IS NOT NULL
    ON CONFLICT DO NOTHING""",
    """INSERT INTO vivian_private.feature_records(guild_id, namespace, key, payload)
    SELECT guild_id, 'life', 'story-message:' || message_id, jsonb_build_object('id', 'legacy-' || id)
    FROM vivian_features.stories WHERE guild_id=%s AND deleted=0 AND message_id IS NOT NULL
    ON CONFLICT DO NOTHING""",
    """INSERT INTO vivian_private.feature_records AS current(guild_id, namespace, key, payload)
    SELECT guild_id, 'settings', 'guild', jsonb_build_object('stories_enabled', true, 'stories_channel_id', channel_id)
    FROM vivian_features.story_settings WHERE guild_id=%s
    ON CONFLICT (guild_id,namespace,key) DO UPDATE SET payload=excluded.payload || current.payload""",
)


async def migrate():
    async with hub_store.cursor() as current:
        await current.execute("""SELECT guild_id FROM vivian_features.birthdays
            UNION SELECT guild_id FROM vivian_features.birthday_active_roles
            UNION SELECT guild_id FROM vivian_features.story_settings
            UNION SELECT guild_id FROM vivian_features.stories WHERE deleted=0 AND message_id IS NOT NULL""")
        guilds = await current.fetchall()
    for row in guilds:
        guild_id = row["guild_id"]
        async with hub_store.cursor(guild_id) as current:
            await current.execute("""INSERT INTO vivian_private.feature_records(guild_id, namespace, key, payload)
                VALUES (%s,'migrations',%s,'{}'::jsonb) ON CONFLICT DO NOTHING RETURNING key""", (guild_id, _MARKER))
            if await current.fetchone() is not None:
                for statement in _COPY:
                    await current.execute(statement, (guild_id,))
