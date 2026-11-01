VIVIAN MUSIC BRIDGE PATCH — 2026-11-01

This archive contains the original bot project plus two changes:
- vivian.py: launches music bridge task on ready.
- vivian/music_web_bridge.py: polls requests and publishes status to the Supabase database.

Deployment:
1. Backup your existing repository (e.g. git commit).
2. Transfer the archive contents to the same repo that deploys your Discord bot, preserving the root layout. Do not deploy to vivian-web2 (website).
3. Set WEB_MUSIC_BRIDGE_ENABLED=true and DATABASE_URL (Supabase PostgreSQL URI) in the *bot's* Render service. Keep secrets in Render; never commit them.
4. Confirm vivian_web.music_requests and vivian_web.music_status exist.
5. Redeploy the Discord bot. Check Render logs for '[Vivian] Music bridge task started' and new errors.
6. Join a voice channel and submit a NEW web music request. Prior pending requests over two minutes old will not be consumed.

Known issue: AI memory _ai_memory_load/_ai_memory_save AttributeErrors in older logs are unrelated and are not fixed here.
