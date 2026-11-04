from __future__ import annotations

from contextlib import closing
import sqlite3

from psycopg import sql


_MIGRATION = "local_feature_data_v1"
_IDENTITY_TABLES = {
    "vivian_capsules", "crossroads", "relationship_requests", "stories", "wall_posts", "wall_comments",
}


def prepare_schema(connection, features, local_path):
    from core import (
        birthday_store, capsule_store, crossroads_store, exchange_store, memories_store,
        status_store, story_store, wall_store,
    )
    import crush_store

    stores = (
        (birthday_store, ("birthdays", "birthday_active_roles")),
        (capsule_store, ("vivian_capsules", "vivian_capsule_notes")),
        (crossroads_store, ("crossroads", "crossroads_votes")),
        (exchange_store, ("exchange_profiles",)),
        (memories_store, ("memory_messages",)),
        (status_store, ("relationships", "relationship_requests", "status_profiles")),
        (story_store, ("story_settings", "stories", "story_views")),
        (wall_store, ("wall_posts", "wall_likes", "wall_comments", "wall_privacy")),
        (crush_store, ("crushes",)),
    )
    tables = [table for _, names in stores for table in names]
    with connection.pipeline():
        for store, _ in stores:
            store._schema(features)
        connection.execute("CREATE TABLE IF NOT EXISTS feature_migrations (name TEXT PRIMARY KEY)")
        for table in (*tables, "feature_migrations"):
            connection.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(sql.Identifier(table)))

    if not local_path.is_file() or connection.execute(
        "SELECT 1 FROM feature_migrations WHERE name = %s", (_MIGRATION,)
    ).fetchone():
        return

    with closing(sqlite3.connect(local_path.as_uri() + "?mode=ro", uri=True)) as source:
        source.execute("BEGIN")
        local_tables = {
            row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        sequences = {}
        if "sqlite_sequence" in local_tables:
            sequences = dict(source.execute("SELECT name, seq FROM sqlite_sequence"))
        imported = 0
        for table in tables:
            if table in local_tables:
                imported += _copy_table(connection, source, table, sequences.get(table, 0))
    if imported:
        connection.execute("INSERT INTO feature_migrations (name) VALUES (%s)", (_MIGRATION,))


def _copy_table(connection, source, name, last_local_id):
    table = sql.Identifier(name)
    rows = source.execute(f'SELECT * FROM "{name}"')  # name is from the fixed list above
    first = rows.fetchone()
    if first is not None:
        if connection.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(table)).fetchone():
            raise RuntimeError(f"Cannot import SQLite data into nonempty PostgreSQL table {name}")
        columns = [sql.Identifier(column[0]) for column in rows.description]
        insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            table, sql.SQL(", ").join(columns), sql.SQL(", ").join([sql.Placeholder()] * len(columns)),
        )
        connection.execute(insert, first)
        with connection.cursor() as cursor:
            cursor.executemany(insert, rows)

    if name in _IDENTITY_TABLES and (first is not None or last_local_id):
        last_inserted_id = connection.execute(sql.SQL("SELECT MAX(id) FROM {}").format(table)).fetchone()[0]
        last_postgres_id = connection.execute(
            "SELECT pg_sequence_last_value(pg_get_serial_sequence(%s, 'id')::regclass)", (name,),
        ).fetchone()[0]
        next_id = max(last_local_id, last_inserted_id or 0, last_postgres_id or 0) + 1
        connection.execute(
            sql.SQL("ALTER TABLE {} ALTER COLUMN id RESTART WITH {}").format(table, sql.Literal(next_id)),
        )
    return int(first is not None or bool(last_local_id))
