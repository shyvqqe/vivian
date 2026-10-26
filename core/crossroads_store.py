from __future__ import annotations

import json
import time

from core import feature_storage


def _scope(guild_id, channel_id):
    if any(type(value) is not int or value <= 0 for value in (guild_id, channel_id)):
        raise ValueError("A server channel is required.")


def _text(value, limit):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"Text must contain 1 to {limit} characters.")
    return value.strip()


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS crossroads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL, question TEXT NOT NULL,
        options TEXT NOT NULL, expectation TEXT NOT NULL,
        created_at REAL NOT NULL, closes_at REAL NOT NULL,
        review_at REAL NOT NULL, outcome TEXT, canceled INTEGER NOT NULL DEFAULT 0
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS crossroads_votes (
        decision_id INTEGER NOT NULL REFERENCES crossroads(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL, choice INTEGER NOT NULL,
        confidence INTEGER NOT NULL,
        PRIMARY KEY (decision_id, user_id)
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS crossroads_channel ON crossroads(guild_id, channel_id, id)")


def _get(connection, guild_id, channel_id, decision_id):
    _scope(guild_id, channel_id)
    if type(decision_id) is not int or decision_id <= 0:
        raise ValueError("A decision ID is required.")
    _schema(connection)
    row = connection.execute(
        "SELECT * FROM crossroads WHERE id = ? AND guild_id = ? AND channel_id = ? AND canceled = 0",
        (decision_id, guild_id, channel_id),
    ).fetchone()
    if row is None:
        raise ValueError("Decision not found in this channel.")
    value = dict(row)
    value["options"] = json.loads(value["options"])
    return value


async def create(guild_id, channel_id, owner_id, question, options, expectation, minutes, review_hours):
    _scope(guild_id, channel_id)
    if type(owner_id) is not int or owner_id <= 0:
        raise ValueError("A member is required.")
    question = _text(question, 200)
    expectation = _text(expectation, 300)
    if not isinstance(options, (list, tuple)) or not 2 <= len(options) <= 6:
        raise ValueError("Provide 2 to 6 options separated by |.")
    options = [_text(option, 100) for option in options]
    if len({option.casefold() for option in options}) != len(options):
        raise ValueError("Options must be different.")
    if type(minutes) is not int or not 1 <= minutes <= 43200:
        raise ValueError("Voting must last 1 to 43200 minutes.")
    if type(review_hours) is not int or not 1 <= review_hours <= 2160:
        raise ValueError("Review must be 1 to 2160 hours after voting closes.")

    def operation(connection):
        _schema(connection)
        now = time.time()
        count = connection.execute(
            "SELECT COUNT(*) FROM crossroads WHERE guild_id = ? AND channel_id = ? AND canceled = 0 AND closes_at > ?",
            (guild_id, channel_id, now),
        ).fetchone()[0]
        if count >= 30:
            raise ValueError("This channel already has 30 open decisions.")
        closes = now + minutes * 60
        cursor = connection.execute(
            """INSERT INTO crossroads
            (guild_id, channel_id, owner_id, question, options, expectation, created_at, closes_at, review_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, channel_id, owner_id, question, json.dumps(options), expectation,
             now, closes, closes + review_hours * 3600),
        )
        return _get(connection, guild_id, channel_id, cursor.lastrowid)

    return await feature_storage.run(operation)


async def vote(guild_id, channel_id, decision_id, user_id, choice, confidence):
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("A member is required.")
    if type(confidence) is not int or not 1 <= confidence <= 5:
        raise ValueError("Confidence must be 1 to 5.")

    def operation(connection):
        item = _get(connection, guild_id, channel_id, decision_id)
        if time.time() >= item["closes_at"]:
            raise ValueError("Voting has closed.")
        if type(choice) is not int or not 1 <= choice <= len(item["options"]):
            raise ValueError("Choose an option number from this decision.")
        existing = connection.execute(
            "SELECT 1 FROM crossroads_votes WHERE decision_id = ? AND user_id = ?", (decision_id, user_id),
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM crossroads_votes WHERE decision_id = ?", (decision_id,)).fetchone()[0]
        if existing is None and count >= 250:
            raise ValueError("This decision has reached 250 voters.")
        connection.execute(
            """INSERT INTO crossroads_votes (decision_id, user_id, choice, confidence) VALUES (?, ?, ?, ?)
            ON CONFLICT(decision_id, user_id) DO UPDATE SET choice = excluded.choice, confidence = excluded.confidence""",
            (decision_id, user_id, choice, confidence),
        )
        return item["options"][choice - 1]

    return await feature_storage.run(operation)


async def results(guild_id, channel_id, decision_id):
    def operation(connection):
        item = _get(connection, guild_id, channel_id, decision_id)
        if time.time() < item["closes_at"]:
            raise ValueError(f"Results remain sealed until <t:{int(item['closes_at'])}:F>.")
        rows = connection.execute(
            "SELECT choice, COUNT(*) AS votes, AVG(confidence) AS confidence FROM crossroads_votes WHERE decision_id = ? GROUP BY choice",
            (decision_id,),
        ).fetchall()
        grouped = {row["choice"]: dict(row) for row in rows}
        item["tally"] = [
            {"option": option, "votes": grouped.get(index, {}).get("votes", 0),
             "confidence": grouped.get(index, {}).get("confidence")}
            for index, option in enumerate(item["options"], 1)
        ]
        return item

    return await feature_storage.run(operation)


async def ballot(guild_id, channel_id, decision_id):
    return await feature_storage.run(lambda connection: _get(connection, guild_id, channel_id, decision_id))


async def record_outcome(guild_id, channel_id, decision_id, user_id, outcome, *, staff=False):
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("A member is required.")
    outcome = _text(outcome, 1000)

    def operation(connection):
        item = _get(connection, guild_id, channel_id, decision_id)
        if user_id != item["owner_id"] and not staff:
            raise ValueError("Only the author or a server manager can record the outcome.")
        if time.time() < item["review_at"]:
            raise ValueError(f"Review opens <t:{int(item['review_at'])}:F>.")
        if item["outcome"] is not None:
            raise ValueError("The outcome has already been recorded.")
        connection.execute("UPDATE crossroads SET outcome = ? WHERE id = ?", (outcome, decision_id))

    await feature_storage.run(operation)


async def history(guild_id, channel_id):
    _scope(guild_id, channel_id)

    def operation(connection):
        _schema(connection)
        return [dict(row) for row in connection.execute(
            "SELECT id, question, closes_at, review_at, outcome FROM crossroads WHERE guild_id = ? AND channel_id = ? AND canceled = 0 ORDER BY id DESC LIMIT 10",
            (guild_id, channel_id),
        )]

    return await feature_storage.run(operation)


async def cancel(guild_id, channel_id, decision_id, user_id, *, staff=False):
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("A member is required.")
    def operation(connection):
        item = _get(connection, guild_id, channel_id, decision_id)
        if user_id != item["owner_id"] and not staff:
            raise ValueError("Only the author or a server manager can cancel this decision.")
        if time.time() >= item["closes_at"]:
            raise ValueError("A closed decision cannot be canceled.")
        connection.execute("DELETE FROM crossroads_votes WHERE decision_id = ?", (decision_id,))
        connection.execute("UPDATE crossroads SET canceled = 1, question = '', options = '[]', expectation = '' WHERE id = ?", (decision_id,))

    await feature_storage.run(operation)
