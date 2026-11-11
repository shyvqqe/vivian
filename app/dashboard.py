from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import json
import os
from pathlib import Path
import re
import secrets
import time
import threading
from collections import deque
from urllib.parse import parse_qs, urlencode, urlsplit

import discord
import httpx

from core import config, guild_settings, hub_store


_loop = None
_ASSETS = Path(__file__).with_name("dashboard_assets")
_SESSION_COOKIE = "__Host-vivian_session"
_STATE_COOKIE = "__Secure-vivian_oauth"
_ASSET_NAMES = {"dashboard.css", "dashboard.js", "profile.js", "styles.css", "theme.css"}
_INFLIGHT = threading.BoundedSemaphore(12)
_REQUEST_LOCK = threading.Lock()
_REQUESTS = {}


class DashboardError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def start():
    global _loop
    _loop = asyncio.get_running_loop()


def _secret():
    value = os.getenv("DASHBOARD_SESSION_SECRET", "")
    if len(value.encode("utf-8")) < 32:
        raise DashboardError(503, "Set DASHBOARD_SESSION_SECRET to a stable secret of at least 32 bytes.")
    return value.encode("utf-8")


def _origin(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise DashboardError(503, "Configure an HTTPS dashboard URL.")
    return f"https://{parsed.netloc.lower()}"


def _base():
    value = (os.getenv("DASHBOARD_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    origin = _origin(value)
    if value != origin:
        raise DashboardError(503, "DASHBOARD_BASE_URL must be the HTTPS origin of the Render service.")
    return origin


def _site():
    return _origin(os.getenv("PUBLIC_SITE_URL", "https://esil.life").rstrip("/"))


def _encoded(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _signed(purpose, payload):
    body = _encoded(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    digest = hmac.new(_secret(), f"{purpose}.{body}".encode("ascii"), hashlib.sha256).digest()
    return body + "." + _encoded(digest)


def _verify(purpose, token, lifetime):
    if not isinstance(token, str) or len(token) > 4096:
        return None
    try:
        body, signature = token.split(".", 1)
        expected = _signed(purpose, json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))))
        if not hmac.compare_digest(expected, token):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not isinstance(payload, dict):
            return None
        now = int(time.time())
        if (type(payload.get("iat")) is not int or type(payload.get("exp")) is not int
                or payload["iat"] > now + 30 or payload["exp"] <= now
                or payload["exp"] - payload["iat"] > lifetime or payload["exp"] <= payload["iat"]):
            return None
        return payload
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError, RecursionError, OverflowError):
        return None


def _cookies(handler):
    result = SimpleCookie()
    try:
        result.load(handler.headers.get("Cookie", ""))
    except CookieError:
        return {}
    return {key: value.value for key, value in result.items()}


def _cookie(name, value, *, max_age, path="/"):
    return f"{name}={value}; Max-Age={max_age}; Path={path}; HttpOnly; Secure; SameSite=Lax"


def _session(handler):
    payload = _verify("session", _cookies(handler).get(_SESSION_COOKIE, ""), 3600)
    if (payload is None or not isinstance(payload.get("uid"), str)
            or re.fullmatch(r"[1-9][0-9]{0,19}", payload["uid"]) is None
            or not isinstance(payload.get("nonce"), str) or re.fullmatch(r"[A-Za-z0-9_-]{24,80}", payload["nonce"]) is None):
        raise DashboardError(401, "Sign in with Discord.")
    return payload


def _csrf(session):
    body = f"csrf:{session['uid']}:{session['nonce']}:{session['exp']}"
    return hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()


def _pkce(nonce):
    return _encoded(hmac.new(_secret(), ("pkce:" + nonce).encode("ascii"), hashlib.sha256).digest())


def _check_origin(handler, *, required=False):
    origin = handler.headers.get("Origin")
    if (required and origin != _base()) or (origin is not None and origin != _base()):
        raise DashboardError(403, "The request must come from the dashboard origin.")
    if handler.headers.get("Sec-Fetch-Site") == "cross-site":
        raise DashboardError(403, "Cross-site dashboard requests are not allowed.")


def _check_csrf(handler, session):
    _check_origin(handler, required=True)
    token = handler.headers.get("X-CSRF-Token", "")
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None or not hmac.compare_digest(token, _csrf(session)):
        raise DashboardError(403, "The form session expired. Reload the dashboard.")


def _bridge(coroutine):
    if _loop is None or not _loop.is_running():
        coroutine.close()
        raise DashboardError(503, "Discord is still connecting. Please try again shortly.")
    if not _INFLIGHT.acquire(blocking=False):
        coroutine.close()
        raise DashboardError(429, "The dashboard is busy. Please try again shortly.")
    try:
        pending = asyncio.run_coroutine_threadsafe(coroutine, _loop)
        try:
            return pending.result(timeout=15)
        except concurrent.futures.TimeoutError:
            pending.cancel()
            raise DashboardError(503, "Discord or storage did not respond in time.") from None
    finally:
        _INFLIGHT.release()


def _request_budget(handler, group):
    address = getattr(handler, "client_address", ("local", 0))[0]
    now = time.monotonic()
    key = (str(address)[:100], group)
    limit = 30 if group == "auth" else 120
    with _REQUEST_LOCK:
        if key not in _REQUESTS and len(_REQUESTS) >= 4096:
            _REQUESTS.pop(next(iter(_REQUESTS)))
        queue = _REQUESTS.setdefault(key, deque())
        while queue and queue[0] <= now - 60:
            queue.popleft()
        if len(queue) >= limit:
            raise DashboardError(429, "Too many requests. Please try again in a minute.")
        queue.append(now)


async def _managed(guild_id, user_id):
    guild = config.bot.get_guild(guild_id)
    if guild is None or guild.me is None:
        raise DashboardError(404, "The bot is not a member of this server.")
    try:
        async with asyncio.timeout(5):
            member = await guild.fetch_member(user_id)
    except discord.NotFound:
        raise DashboardError(403, "You are no longer a member of this server.") from None
    except (discord.DiscordException, TimeoutError):
        raise DashboardError(503, "Discord permissions could not be verified.") from None
    if member.bot or not member.guild_permissions.manage_guild:
        raise DashboardError(403, "Manage Server permission is required.")
    return guild, member


def _guild_info(guild):
    return {"id": str(guild.id), "name": guild.name, "icon": str(guild.icon.url) if guild.icon else None,
            "member_count": guild.member_count or 0}


def _settings_json(settings):
    result = {}
    for key in guild_settings.DEFAULTS:
        if key not in settings:
            continue
        value = settings[key]
        if key.endswith("_id") and value is not None:
            value = str(value)
        elif key.endswith("_ids"):
            value = [str(item) for item in value]
        result[key] = value
    return result


async def _guilds(user_id):
    gate = asyncio.Semaphore(6)

    async def inspect(guild):
        async with gate:
            try:
                await _managed(guild.id, user_id)
                return _guild_info(guild)
            except DashboardError as error:
                if error.status in {403, 404}:
                    return None
                raise

    rows = await asyncio.gather(*(inspect(guild) for guild in config.bot.guilds))
    return {"guilds": sorted((item for item in rows if item is not None), key=lambda item: item["name"].casefold())}


async def _guild_details(guild_id, user_id):
    from core import state
    guild, _ = await _managed(guild_id, user_id)
    settings = await guild_settings.get(guild_id)
    channels = [{"id": str(channel.id), "name": channel.name, "kind": str(channel.type)}
                for channel in guild.channels if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.CategoryChannel))]
    roles = [{"id": str(role.id), "name": role.name, "managed": role.managed}
             for role in guild.roles if not role.is_default()]
    records = [record for (gid, _), record in state.RANK_ACTIVITY.items() if gid == guild_id]
    return {"guild": _guild_info(guild), "settings": _settings_json(settings), "channels": channels, "roles": roles,
            "stats": {"messages": sum(max(0, int(record.get("messages", 0))) for record in records),
                      "tracked_members": len(records), "commands": len(config.bot.tree.get_commands())}}


def _patch(body):
    patch = body.get("patch") if isinstance(body, dict) else None
    if not isinstance(patch, dict) or len(patch) > len(guild_settings.DEFAULTS) or any(key not in guild_settings.DEFAULTS for key in patch):
        raise DashboardError(400, "Only recognized server settings can be changed.")
    result = dict(patch)
    for key, value in result.items():
        if key.endswith("_id"):
            if value in (None, ""):
                result[key] = None
            elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value):
                result[key] = int(value)
        elif key.endswith("_ids") and isinstance(value, list):
            result[key] = [int(item) if isinstance(item, str) and re.fullmatch(r"[1-9][0-9]{0,19}", item) else item for item in value]
    return result


async def _update_settings(guild_id, user_id, body):
    from core.settings_validation import validate_patch
    guild, _ = await _managed(guild_id, user_id)
    patch = await validate_patch(guild, _patch(body))
    return {"settings": _settings_json(await guild_settings.update(guild_id, patch))}


async def _live_member(guild, user_id):
    try:
        async with asyncio.timeout(4):
            member = await guild.fetch_member(user_id)
    except discord.NotFound:
        raise DashboardError(404, "Public profile not found.") from None
    except (discord.DiscordException, TimeoutError):
        raise DashboardError(503, "Discord membership could not be verified.") from None
    if member.bot:
        raise DashboardError(404, "Public profile not found.")
    return member


async def _public_profile(guild_id, user_id):
    from commands import profile_data
    from core import economy_store, social_store
    if not (0 < guild_id < 2**63 and 0 < user_id < 2**63):
        raise DashboardError(404, "Public profile not found.")
    guild = config.bot.get_guild(guild_id)
    if guild is None:
        raise DashboardError(404, "Public profile not found.")
    settings = await guild_settings.get(guild_id)
    consent = await hub_store.get(guild_id, "profile_settings", str(user_id), {})
    if settings.get("public_profiles_enabled") is not True or consent.get("public") is not True:
        raise DashboardError(404, "Public profile not found.")
    member = await _live_member(guild, user_id)
    data, social = profile_data.snapshot(member), await social_store.profile_summary(guild_id, user_id)
    result = {"id": str(user_id), "guild": _guild_info(guild), "display_name": member.display_name,
              "avatar_url": str(member.display_avatar.url), "level": data["level"], "total_xp": data["total_xp"],
              "xp_into": data["xp_into"], "xp_required": data["xp_required"], "messages": data["messages"],
              "badges": list(data.get("badges", []))[:3], "friend_count": int(social.get("friend_count", 0)),
              "status_text": str(social.get("status_text", ""))[:120], "mood": str(social.get("mood", ""))[:32],
              "relationship": None, "wall": [], "updated_at": int(time.time())}
    if settings.get("economy_enabled") is True:
        economy = await economy_store.profile_summary(guild_id, user_id)
        result["economy"] = {"balance": max(0, int(economy.get("balance", 0))),
                             "currency": str(economy.get("currency", "sparks"))[:40],
                             "badges": [str(item)[:100] for item in economy.get("badges", [])[-12:]]}
    social_enabled = settings.get("social_enabled", True) is True
    partner_id = social.get("relationship_user_id") if social_enabled else None
    posts = await social_store.public_wall_posts(guild_id, user_id) if social_enabled and consent.get("share_wall") is True else []
    friend_ids = await social_store.public_friend_ids(guild_id, user_id) if social_enabled and consent.get("share_friends") is True else []
    related_ids = set(friend_ids)
    for post in posts:
        related_ids.add(post["author"])
        related_ids.update(comment["author"] for comment in post["comments"])
    if partner_id and (await hub_store.get(guild_id, "profile_settings", str(partner_id), {})).get("public") is True:
        related_ids.add(partner_id)
    else:
        partner_id = None
    identities = {user_id: {"id": str(user_id), "display_name": member.display_name}}
    lookup_slots = asyncio.Semaphore(5)

    async def load_identity(related_id):
        async with lookup_slots:
            try:
                related = await _live_member(guild, related_id)
            except DashboardError:
                return
            identities[related_id] = {"id": str(related_id), "display_name": related.display_name}

    await asyncio.gather(*(load_identity(related_id) for related_id in sorted(related_ids - {user_id})))
    if partner_id in identities:
        result["relationship"] = {**identities[partner_id], "since": social.get("relationship_since")}
    friends = [identities[friend_id] for friend_id in friend_ids if friend_id in identities]
    if friends:
        result["friends"] = friends
    for post in posts:
        if post["author"] not in identities:
            continue
        result["wall"].append({
            "author": identities[post["author"]], "text": post["text"], "created_at": post["created_at"], "likes": post["likes"],
            "comments": [
                {"author": identities[comment["author"]], "text": comment["text"], "created_at": comment["created_at"]}
                for comment in post["comments"] if comment["author"] in identities
            ],
        })
    return result


async def _oauth_user(code, nonce):
    client_id = os.getenv("DISCORD_CLIENT_ID") or str(getattr(config.bot.user, "id", ""))
    client_secret = os.getenv("DISCORD_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise DashboardError(503, "Configure DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET for OAuth.")
    async with httpx.AsyncClient(timeout=8) as client:
        response = await client.post("https://discord.com/api/oauth2/token", data={
            "client_id": client_id, "client_secret": client_secret, "grant_type": "authorization_code",
            "code": code, "code_verifier": _pkce(nonce), "redirect_uri": _base() + "/auth/callback"})
        if response.status_code != 200:
            raise DashboardError(401, "Discord sign-in could not be completed. Please sign in again.")
        token_data = response.json()
        token = token_data.get("access_token") if isinstance(token_data, dict) else None
        if not isinstance(token, str) or not token:
            raise DashboardError(401, "Discord sign-in could not be completed.")
        user_response = await client.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bearer {token}"})
        if user_response.status_code != 200:
            raise DashboardError(401, "Discord identity could not be verified.")
        user = user_response.json()
        if (not isinstance(user, dict) or not isinstance(user.get("id"), str)
                or re.fullmatch(r"[1-9][0-9]{0,19}", user["id"]) is None or user.get("bot")):
            raise DashboardError(401, "Discord identity could not be verified.")
        return user["id"]


def _respond(handler, status, value=b"", *, content_type="application/json; charset=utf-8", headers=(), head_only=False):
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    elif isinstance(value, str):
        value = value.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(value)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' https://cdn.discordapp.com https://media.discordapp.net; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    for key, header_value in headers:
        handler.send_header(key, header_value)
    handler.end_headers()
    if not head_only and status not in {204, 304}:
        handler.wfile.write(value)


def _json_body(handler):
    if handler.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
        raise DashboardError(415, "Use an application/json request.")
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise DashboardError(400, "Invalid request length.") from None
    if not 1 <= length <= 32768:
        raise DashboardError(413, "The settings request is too large or empty.")
    try:
        return json.loads(handler.rfile.read(length))
    except (ValueError, UnicodeError):
        raise DashboardError(400, "Invalid JSON request.") from None


def _page(name):
    value = (_ASSETS / name).read_text(encoding="utf-8")
    for asset in ("styles.css", "theme.css", "dashboard.css", "config.js", "profile.js", "dashboard.js"):
        value = value.replace(f'="{asset}"', f'="/dashboard/assets/{asset}"')
    return value


def handle(handler, head_only=False):
    parsed = urlsplit(handler.path)
    path, method = parsed.path, handler.command
    head_only = head_only or method == "HEAD"
    if not (path == "/dashboard" or path.startswith(("/dashboard/", "/auth/", "/api/", "/profile/"))):
        return False
    if path.startswith("/auth/"):
        handler.requestline = f"{method} {path} HTTP/1.1"
    public = re.fullmatch(r"/api/public/guilds/([1-9][0-9]{0,19})/members/([1-9][0-9]{0,19})", path)
    cors = []
    try:
        if public:
            origin = handler.headers.get("Origin")
            if origin == _site():
                cors = [("Access-Control-Allow-Origin", origin), ("Vary", "Origin")]
            elif origin is not None and origin != _base():
                raise DashboardError(403, "This origin is not allowed.")
            if method == "OPTIONS":
                _respond(handler, 204, headers=cors + [("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")], head_only=True)
                return True
        if path.startswith(("/auth/", "/api/")):
            _request_budget(handler, "auth" if path.startswith("/auth/") else "api")
        if method in {"GET", "HEAD"}:
            if path in {"/dashboard", "/dashboard/"}:
                _respond(handler, 200, _page("dashboard-app.html"), content_type="text/html; charset=utf-8", head_only=head_only)
            elif path == "/dashboard/assets/config.js":
                code = "window.VIVIAN_CONFIG=" + json.dumps({"apiBase": _base(), "publicSiteUrl": _site()}) + ";"
                _respond(handler, 200, code, content_type="text/javascript; charset=utf-8", head_only=head_only)
            elif path.startswith("/dashboard/assets/") and path.rsplit("/", 1)[1] in _ASSET_NAMES:
                name = path.rsplit("/", 1)[1]
                content_type = "text/css; charset=utf-8" if name.endswith(".css") else "text/javascript; charset=utf-8"
                _respond(handler, 200, (_ASSETS / name).read_bytes(), content_type=content_type, head_only=head_only)
            elif re.fullmatch(r"/profile/[1-9][0-9]{0,19}/[1-9][0-9]{0,19}", path):
                _respond(handler, 200, _page("profile.html"), content_type="text/html; charset=utf-8", head_only=head_only)
            elif path == "/auth/login":
                client_id = os.getenv("DISCORD_CLIENT_ID") or str(getattr(config.bot.user, "id", ""))
                if not client_id or not os.getenv("DISCORD_CLIENT_SECRET"):
                    raise DashboardError(503, "Configure Discord OAuth credentials for this Render service.")
                now, nonce = int(time.time()), secrets.token_urlsafe(24)
                state = _signed("oauth", {"nonce": nonce, "iat": now, "exp": now + 600})
                location = "https://discord.com/oauth2/authorize?" + urlencode({"client_id": client_id, "response_type": "code",
                    "redirect_uri": _base() + "/auth/callback", "scope": "identify", "state": state,
                    "code_challenge": _encoded(hashlib.sha256(_pkce(nonce).encode("ascii")).digest()), "code_challenge_method": "S256"})
                _respond(handler, 302, headers=[("Location", location), ("Set-Cookie", _cookie(_STATE_COOKIE, nonce, max_age=600, path="/auth"))], head_only=True)
            elif path == "/auth/callback":
                query = parse_qs(parsed.query)
                state = _verify("oauth", query.get("state", [""])[0], 600)
                nonce = _cookies(handler).get(_STATE_COOKIE, "")
                if (state is None or not isinstance(state.get("nonce"), str)
                        or re.fullmatch(r"[A-Za-z0-9_-]{24,80}", nonce) is None
                        or not hmac.compare_digest(nonce, state["nonce"])):
                    raise DashboardError(403, "Discord sign-in state expired. Please sign in again.")
                code = query.get("code", [""])[0]
                if query.get("iss", ["https://discord.com"])[0] != "https://discord.com":
                    raise DashboardError(403, "The OAuth issuer is invalid.")
                if not code or len(code) > 2000:
                    raise DashboardError(401, "Discord sign-in was cancelled.")
                user_id, now = _bridge(_oauth_user(code, nonce)), int(time.time())
                token = _signed("session", {"uid": user_id, "nonce": secrets.token_urlsafe(24), "iat": now, "exp": now + 3600})
                _respond(handler, 302, headers=[("Location", "/dashboard"), ("Set-Cookie", _cookie(_SESSION_COOKIE, token, max_age=3600)),
                                               ("Set-Cookie", _cookie(_STATE_COOKIE, "", max_age=0, path="/auth"))], head_only=True)
            elif public:
                value = _bridge(_public_profile(int(public[1]), int(public[2])))
                _respond(handler, 200, value, headers=cors, head_only=head_only)
            elif path.startswith("/api/"):
                _check_origin(handler)
                session = _session(handler)
                if path == "/api/session":
                    value = {"user": {"id": session["uid"]}, "csrf": _csrf(session), "expires_at": session["exp"]}
                elif path == "/api/guilds":
                    value = _bridge(_guilds(int(session["uid"])))
                elif match := re.fullmatch(r"/api/guilds/([1-9][0-9]{0,19})", path):
                    value = _bridge(_guild_details(int(match[1]), int(session["uid"])))
                else:
                    raise DashboardError(404, "API route not found.")
                _respond(handler, 200, value, head_only=head_only)
            else:
                raise DashboardError(404, "Page not found.")
        elif method == "POST":
            session = _session(handler)
            _check_csrf(handler, session)
            if path == "/auth/logout":
                _respond(handler, 200, {"signed_out": True}, headers=[("Set-Cookie", _cookie(_SESSION_COOKIE, "", max_age=0))], head_only=head_only)
            elif match := re.fullmatch(r"/api/guilds/([1-9][0-9]{0,19})/settings", path):
                value = _bridge(_update_settings(int(match[1]), int(session["uid"]), _json_body(handler)))
                _respond(handler, 200, value, head_only=head_only)
            else:
                raise DashboardError(404, "API route not found.")
        else:
            raise DashboardError(405, "Method not allowed.")
    except DashboardError as error:
        _respond(handler, error.status, {"error": error.message}, headers=cors, head_only=head_only)
    except (hub_store.Unavailable, httpx.HTTPError):
        _respond(handler, 503, {"error": "The service is temporarily unavailable."}, headers=cors, head_only=head_only)
    except ValueError as error:
        _respond(handler, 400, {"error": str(error)[:300]}, headers=cors, head_only=head_only)
    except (OSError, asyncio.TimeoutError):
        _respond(handler, 503, {"error": "The dashboard is temporarily unavailable."}, headers=cors, head_only=head_only)
    return True
