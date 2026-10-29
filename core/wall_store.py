from __future__ import annotations

import time

from core import feature_storage

PAGE_SIZE = 5
MAX_TEXT = 500


def _ids(*values):
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("A server and member are required.")
    return values


def _text(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= MAX_TEXT:
        raise ValueError(f"Текст поста должен быть от 1 до {MAX_TEXT} символов.")
    return value.strip()


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS wall_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        message_id INTEGER,
        channel_id INTEGER,
        thread_id INTEGER,
        created_at REAL NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS wall_posts_owner ON wall_posts(guild_id, owner_id, deleted, created_at DESC)"
    )
    connection.execute("""CREATE TABLE IF NOT EXISTS wall_likes (
        post_id INTEGER NOT NULL REFERENCES wall_posts(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (post_id, user_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS wall_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL REFERENCES wall_posts(id) ON DELETE CASCADE,
        author_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS wall_privacy (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        privacy TEXT NOT NULL DEFAULT 'all' CHECK (privacy IN ('all', 'friends', 'none')),
        PRIMARY KEY (guild_id, user_id)
    )""")


def _privacy(connection, guild_id, owner_id):
    row = connection.execute(
        "SELECT privacy FROM wall_privacy WHERE guild_id = ? AND user_id = ?", (guild_id, owner_id),
    ).fetchone()
    return row["privacy"] if row else "all"


async def set_privacy(guild_id: int, user_id: int, privacy: str) -> None:
    guild_id, user_id = _ids(guild_id, user_id)
    if privacy not in {"all", "friends", "none"}:
        raise ValueError("Недопустимое значение приватности.")

    def work(connection):
        _schema(connection)
        connection.execute(
            "INSERT INTO wall_privacy (guild_id, user_id, privacy) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET privacy = excluded.privacy",
            (guild_id, user_id, privacy),
        )

    await feature_storage.run(work)


async def get_privacy(guild_id: int, user_id: int) -> str:
    guild_id, user_id = _ids(guild_id, user_id)

    def work(connection):
        _schema(connection)
        return _privacy(connection, guild_id, user_id)

    return await feature_storage.run(work)


async def create_post(guild_id: int, owner_id: int, author_id: int, text: str) -> dict:
    guild_id, owner_id, author_id = _ids(guild_id, owner_id, author_id)
    text = _text(text)

    def work(connection):
        _schema(connection)
        cursor = connection.execute(
            "INSERT INTO wall_posts (guild_id, owner_id, author_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, owner_id, author_id, text, time.time()),
        )
        return {"id": cursor.lastrowid}

    return await feature_storage.run(work)


async def attach_message(post_id: int, channel_id: int, message_id: int, thread_id: int | None) -> None:
    def work(connection):
        _schema(connection)
        connection.execute(
            "UPDATE wall_posts SET channel_id = ?, message_id = ?, thread_id = ? WHERE id = ?",
            (channel_id, message_id, thread_id, post_id),
        )

    await feature_storage.run(work)


async def get_post(post_id: int) -> dict | None:
    def work(connection):
        _schema(connection)
        row = connection.execute(
            "SELECT * FROM wall_posts WHERE id = ? AND deleted = 0", (post_id,)
        ).fetchone()
        return dict(row) if row else None

    return await feature_storage.run(work)


async def delete_post(guild_id: int, post_id: int, requester_id: int, *, is_owner_override: bool) -> dict:
    """Only the wall owner (or the post author) may delete; is_owner_override skips the owner check."""
    guild_id, post_id, requester_id = _ids(guild_id, post_id, requester_id)

    def work(connection):
        _schema(connection)
        row = connection.execute(
            "SELECT owner_id, author_id, channel_id, message_id FROM wall_posts "
            "WHERE id = ? AND guild_id = ? AND deleted = 0",
            (post_id, guild_id),
        ).fetchone()
        if row is None:
            raise ValueError("Пост не найден.")
        allowed = is_owner_override or row["owner_id"] == requester_id or row["author_id"] == requester_id
        if not allowed:
            raise PermissionError("Удалить пост может только владелец стены или автор поста.")
        connection.execute("UPDATE wall_posts SET deleted = 1 WHERE id = ?", (post_id,))
        return dict(row)

    return await feature_storage.run(work)


async def toggle_like(post_id: int, user_id: int) -> dict:
    def work(connection):
        _schema(connection)
        existing = connection.execute(
            "SELECT 1 FROM wall_likes WHERE post_id = ? AND user_id = ?", (post_id, user_id),
        ).fetchone()
        if existing is not None:
            connection.execute(
                "DELETE FROM wall_likes WHERE post_id = ? AND user_id = ?", (post_id, user_id)
            )
            liked = False
        else:
            connection.execute(
                "INSERT INTO wall_likes (post_id, user_id) VALUES (?, ?)", (post_id, user_id)
            )
            liked = True
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM wall_likes WHERE post_id = ?", (post_id,)
        ).fetchone()["n"]
        return {"liked": liked, "count": count}

    return await feature_storage.run(work)


async def add_comment(post_id: int, author_id: int, text: str) -> int:
    text = _text(text)

    def work(connection):
        _schema(connection)
        cursor = connection.execute(
            "INSERT INTO wall_comments (post_id, author_id, text, created_at) VALUES (?, ?, ?, ?)",
            (post_id, author_id, text, time.time()),
        )
        return cursor.lastrowid

    return await feature_storage.run(work)


async def list_posts(guild_id: int, owner_id: int, page: int = 1) -> dict:
    guild_id, owner_id = _ids(guild_id, owner_id)
    page = max(1, int(page))

    def work(connection):
        _schema(connection)
        total = connection.execute(
            "SELECT COUNT(*) AS n FROM wall_posts WHERE guild_id = ? AND owner_id = ? AND deleted = 0",
            (guild_id, owner_id),
        ).fetchone()["n"]
        rows = connection.execute(
            "SELECT id, author_id, text, created_at, channel_id, message_id FROM wall_posts "
            "WHERE guild_id = ? AND owner_id = ? AND deleted = 0 ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (guild_id, owner_id, PAGE_SIZE, (page - 1) * PAGE_SIZE),
        ).fetchall()
        return {"total": total, "page": page, "pages": max(1, -(-total // PAGE_SIZE)),
                "items": [dict(row) for row in rows]}

    return await feature_storage.run(work)


def can_view(privacy: str, viewer_id: int, owner_id: int, is_friend: bool) -> bool:
    if viewer_id == owner_id:
        return True
    if privacy == "none":
        return False
    if privacy == "friends":
        return is_friend
    return True


def can_post(privacy: str, author_id: int, owner_id: int, is_friend: bool) -> bool:
    if author_id == owner_id:
        return True
    if privacy == "none":
        return False
    if privacy == "friends":
        return is_friend
    return True
