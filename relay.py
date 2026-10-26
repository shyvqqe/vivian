
import json
import logging
import math
import os
import secrets
import shutil
import string
import subprocess
import threading
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from flask import Flask, Response, abort, jsonify, request


app = Flask(__name__)
TOKEN = os.environ["RELAY_TOKEN"]
if not TOKEN:
    raise RuntimeError("RELAY_TOKEN must not be empty")

# Flask's access log otherwise prints the secret in every request's query string.
logging.getLogger("werkzeug").disabled = True
SEARCH_TIMEOUT = 25
SOURCES = {"youtube", "soundcloud"}
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
SOUNDCLOUD_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"}


def _webpage_url(value):
    """Accept public platform pages, never arbitrary or direct media endpoints."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return None
        if parsed.port not in {None, 80, 443}:
            return None
        if hostname not in YOUTUBE_HOSTS | SOUNDCLOUD_HOSTS:
            return None
        if hostname in YOUTUBE_HOSTS and parsed.path.rstrip("/") == "/playlist":
            return None
        if hostname in SOUNDCLOUD_HOSTS and "/sets/" in parsed.path:
            return None
    except ValueError:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _source_for_url(value):
    return "soundcloud" if (urlsplit(value).hostname or "").lower() in SOUNDCLOUD_HOSTS else "youtube"


def _request_query():
    supplied = request.args.get("token", "")
    if not secrets.compare_digest(supplied.encode(), TOKEN.encode()):
        abort(403)
    query = (request.args.get("q") or "").strip()
    if not query or len(query) > 200 or any(ord(char) < 32 for char in query):
        abort(400, description="Query must contain 1 to 200 characters")
    source = request.args.get("source", "youtube").lower()
    if source not in SOURCES:
        abort(400, description="Source must be youtube or soundcloud")
    if query.lower().startswith(("http:", "https:")):
        query = _webpage_url(query)
        if query is None:
            abort(400, description="Only YouTube and SoundCloud page URLs are supported")
        source = _source_for_url(query)
        return query, source, True
    if "://" in query:
        abort(400, description="Unsupported URL")
    return query, source, False


def _target(query, source, is_url, limit=1):
    if is_url:
        return query
    prefix = "scsearch" if source == "soundcloud" else "ytsearch"
    return f"{prefix}{limit}:{query}"


def _javascript_options():
    # Plain `pip install yt-dlp` omits EJS; allow its official GitHub solver
    # download at home. Node must be enabled explicitly when installed.
    options = ["--remote-components", "ejs:github"]
    if shutil.which("node"):
        options.extend(["--js-runtimes", "node"])
    return options


def _search_track(entry, fallback_source):
    if not isinstance(entry, dict):
        return None
    page = _webpage_url(entry.get("webpage_url")) or _webpage_url(entry.get("url"))
    extractor = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
    if page is None and fallback_source == "youtube" and "soundcloud" not in extractor:
        video_id = entry.get("id")
        if isinstance(video_id, str) and len(video_id) == 11 and all(char in string.ascii_letters + string.digits + "_-" for char in video_id):
            page = "https://www.youtube.com/watch?" + urlencode({"v": video_id})
    title = entry.get("title")
    if not page or not isinstance(title, str) or not title.strip():
        return None
    source = _source_for_url(page)
    if source == "youtube":
        parsed = urlsplit(page)
        video_id = parse_qs(parsed.query).get("v", [None])[0]
        if video_id:
            # Do not carry tracking data or a playlist into a selected track.
            page = "https://www.youtube.com/watch?" + urlencode({"v": video_id})
    duration = entry.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
        duration = None
    thumbnails = entry.get("thumbnails") or []
    thumbnail = entry.get("thumbnail")
    if not thumbnail and isinstance(thumbnails, list):
        thumbnail = next((item.get("url") for item in reversed(thumbnails) if isinstance(item, dict) and item.get("url")), None)
    if not isinstance(thumbnail, str) or not thumbnail.startswith(("https://", "http://")):
        thumbnail = None
    artist = entry.get("artist") or entry.get("uploader") or entry.get("channel") or ""
    return {
        "title": title.strip()[:300],
        "webpage_url": page,
        "artist": str(artist)[:200],
        "duration": duration,
        "source": source,
        "thumbnail": thumbnail,
    }


@app.after_request
def _no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/search")
def search():
    query, source, is_url = _request_query()
    try:
        limit = int(request.args.get("limit", "10"))
    except (TypeError, ValueError):
        abort(400)
    if not 1 <= limit <= 10:
        abort(400)
    command = [
        "yt-dlp", *_javascript_options(), "--dump-single-json", "--skip-download", "--flat-playlist",
        "--no-playlist", "--quiet", "--no-warnings", "--playlist-end", str(limit),
        "--", _target(query, source, is_url, limit),
    ]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=SEARCH_TIMEOUT, check=False)
    except FileNotFoundError:
        return jsonify(error="yt-dlp is not installed on the relay computer"), 503
    except subprocess.TimeoutExpired:
        return jsonify(error="Search timed out"), 502
    if result.returncode:
        return jsonify(error="The selected source could not complete the search"), 502
    try:
        payload = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        return jsonify(error="Invalid search response"), 502
    if not isinstance(payload, dict):
        return jsonify(error="Invalid search response"), 502
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else [payload]
    tracks, seen = [], set()
    for entry in entries:
        track = _search_track(entry, source)
        if track and track["webpage_url"] not in seen:
            tracks.append(track)
            seen.add(track["webpage_url"])
        if len(tracks) >= limit:
            break
    return jsonify(tracks=tracks)


@app.get("/audio")
def audio():
    query, source, is_url = _request_query()
    try:
        proc = subprocess.Popen(
            ["yt-dlp", *_javascript_options(), "-f", "ba/b", "-o", "-", "--no-playlist", "--quiet", "--no-warnings", "--", _target(query, source, is_url)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        abort(503, description="yt-dlp is not installed on the relay computer")
    cleanup_lock = threading.Lock()
    cleaned = False

    def cleanup():
        nonlocal cleaned
        with cleanup_lock:
            if cleaned:
                return
            cleaned = True
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            cleanup()

    response = Response(generate(), mimetype="application/octet-stream")
    # A response can close before the generator's first iteration.
    response.call_on_close(cleanup)
    return response


if __name__ == "__main__":
    app.run("127.0.0.1", 8787, threaded=True, debug=False)
