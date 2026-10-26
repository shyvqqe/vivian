from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import urlsplit

from core import config as core_config


_server_thread: threading.Thread | None = None
_server_lock = threading.Lock()


def home():
    return "Vivian is running."


class _KeepAliveHandler(BaseHTTPRequestHandler):
    def _respond(self, *, head_only=False):
        path = urlsplit(self.path).path
        content_type = "text/plain; charset=utf-8"
        if path == "/":
            status, content = 200, home()
        elif path == "/healthz":
            from app.github_updates import public_status
            from app.community import public_status as community_status
            from app.rooms import public_status as room_status
            ready = core_config.bot.is_ready()
            status = 200 if ready else 503
            content = json.dumps({
                "discord_ready": ready,
                "revision": core_config.DEPLOY_REVISION,
                "chat_style": core_config.CHAT_STYLE_VERSION,
                "github_updates": public_status(),
                "community": community_status(),
                "private_rooms": room_status(),
            })
            content_type = "application/json; charset=utf-8"
        else:
            status, content = 404, "Not found."
        payload = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def do_GET(self):
        self._respond()

    def do_HEAD(self):
        self._respond(head_only=True)


def run():
    with ThreadingHTTPServer(("0.0.0.0", core_config.PORT), _KeepAliveHandler) as server:
        print(f"[Web] Listening on 0.0.0.0:{core_config.PORT}", flush=True)
        server.serve_forever()


def keep_alive():
    global _server_thread
    with _server_lock:
        if _server_thread is None or not _server_thread.is_alive():
            _server_thread = threading.Thread(
                target=run, name="vivian-keepalive", daemon=True
            )
            _server_thread.start()
