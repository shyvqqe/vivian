from __future__ import annotations

import hashlib
from datetime import datetime
import secrets
import time
from zoneinfo import ZoneInfo

from core import economy_store, guild_settings, hub_store


QUESTIONS = (
    ("What has keys but opens no doors?", "keyboard"),
    ("What gets wetter the more it dries?", "towel"),
    ("What has a face and hands but no arms or legs?", "clock"),
    ("What travels around the world while staying in a corner?", "stamp"),
    ("What has many teeth but cannot bite?", "comb"),
    ("What has a neck but no head?", "bottle"),
    ("What belongs to you but other people use more than you?", "name"),
    ("What has cities, roads and rivers, but no people or water?", "map"),
    ("What can fill a room without taking up space?", "light"),
    ("What must be broken before it can be used?", "egg"),
    ("What has one eye but cannot see?", "needle"),
    ("What follows you in sunlight but disappears in darkness?", "shadow"),
    ("What has a head and a tail but no body?", "coin"),
    ("What can you catch but never throw?", "cold"),
    ("What has branches but no leaves or fruit?", "bank"),
    ("What goes up when rain comes down?", "umbrella"),
    ("What has words but never speaks?", "book"),
    ("What can run without legs and has a mouth?", "river"),
    ("What connects pages across the internet?", "link"),
    ("What disappears as soon as you say its name?", "silence"),
)


async def start_game(guild_id, channel_id, owner_id, *, automatic=False):
    for value in (guild_id, channel_id, owner_id):
        hub_store.identifier(value)
    settings = await guild_settings.get(guild_id)
    if settings.get("seasons_enabled"):
        from core.season_style import theme_for, themed_questions
        month = datetime.now(ZoneInfo(settings["timezone"])).strftime("%Y-%m")
        theme = theme_for(month, settings.get("season_theme", "auto"))
        selected = secrets.choice(themed_questions(theme["id"]))
        question, answers = selected["question"], selected["answers"]
    else:
        question, answer = secrets.choice(QUESTIONS)
        answers = [answer]
    now, token = time.time(), secrets.token_hex(6)
    key = f"game:{channel_id}"

    def operation(game):
        if game.get("state") == "won" and not game.get("reward_done"):
            raise ValueError("The previous round's reward is still being settled. Try again shortly.")
        if game.get("state") in {"active", "preparing"} and game.get("expires_at", 0) > now:
            raise ValueError("This channel already has a word hunt.")
        if now - game.get("started_at", 0) < 900:
            raise ValueError("Wait 15 minutes between word hunts in this channel.")
        game.clear()
        game.update(token=token, channel_id=channel_id, owner_id=owner_id, question=question, answer=answers[0], answers=answers,
                    started_at=now, expires_at=now + 3600, state="preparing", players=[] if automatic else [owner_id],
                    automatic=automatic, attempts={}, message_id=None)
        return dict(game)

    return await hub_store.mutate(guild_id, "community_play", key, operation)


async def join(guild_id, channel_id, user_id, token):
    hub_store.identifier(user_id)

    def operation(game):
        if game.get("token") != token or game.get("state") != "active" or game.get("expires_at", 0) <= time.time():
            raise ValueError("This word hunt is closed.")
        players = game.setdefault("players", [])
        if user_id not in players:
            if len(players) >= 50:
                raise ValueError("This word hunt already has 50 players.")
            players.append(user_id)
        return len(players)

    return await hub_store.mutate(guild_id, "community_play", f"game:{channel_id}", operation)


async def guess(message):
    now = time.time()
    key = f"game:{message.channel.id}"
    game = await hub_store.get(message.guild.id, "community_play", key, {})
    if game.get("state") != "active" or game.get("expires_at", 0) <= now:
        return None
    content = (message.content or "").strip().casefold().strip(".!?,;:")
    if len(content) > 80:
        return None

    def operation(current):
        current_time = time.time()
        if (current.get("token") != game["token"] or current.get("state") != "active"
                or current.get("expires_at", 0) <= current_time):
            return None
        if not current.get("automatic") and message.author.id not in current.get("players", []):
            return None
        attempts = current.setdefault("attempts", {})
        last = attempts.get(str(message.author.id), 0)
        if current_time - last < 5:
            return None
        if len(attempts) >= 500 and str(message.author.id) not in attempts:
            return None
        attempts[str(message.author.id)] = current_time
        if content not in current.get("answers", [current["answer"]]):
            return None
        current.update(state="won", winner_id=message.author.id, winning_message_id=message.id,
                       outcome_posted=False, reward_done=False)
        return dict(current)

    return await hub_store.mutate(message.guild.id, "community_play", key, operation)


async def stop_game(guild_id, channel_id, user_id, *, manager=False):
    def operation(game):
        if game.get("owner_id") != user_id and not manager:
            raise ValueError("Only the host or a server manager can stop this word hunt.")
        if game.get("state") not in {"active", "preparing"}:
            raise ValueError("This word hunt has already ended.")
        game.update(state="stopped", expires_at=time.time())
    await hub_store.mutate(guild_id, "community_play", f"game:{channel_id}", operation)


async def game_reward(guild_id, game):
    current = await hub_store.get(guild_id, "community_play", f"game:{game['channel_id']}", {})
    if current.get("token") != game["token"] or current.get("state") != "won":
        return
    if current.get("reward_done"):
        return
    settings = await guild_settings.get(guild_id)
    amount = 20 if settings.get("economy_enabled") else 0
    if amount:
        await economy_store.credit(guild_id, game["winner_id"], 20, f"wordhunt:{game['token']}")

    def operation(current):
        if current.get("token") == game["token"]:
            current["reward_done"] = True
            current["reward_amount"] = amount
            current["reward_currency"] = settings.get("currency_name", "sparks")
    await hub_store.mutate(guild_id, "community_play", f"game:{game['channel_id']}", operation)


def nonce(value):
    return hashlib.blake2b(str(value).encode(), digest_size=10).hexdigest()
