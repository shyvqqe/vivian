from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import urlsplit

from core import config as core_config
from core import hub_store


_server_thread: threading.Thread | None = None
_server_lock = threading.Lock()


def home():
    return "Vivian is running."


class _KeepAliveHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        path = urlsplit(getattr(self, "path", "")).path[:100]
        print(f"[Web] {getattr(self, 'command', 'HTTP')} {path!r}", flush=True)

    def _respond(self, *, head_only=False):
        from app import dashboard
        if dashboard.handle(self, head_only=head_only):
            return
        path = urlsplit(self.path).path
        content_type = "text/plain; charset=utf-8"
        if path == "/":
            status, content = 200, home()
        elif path == "/healthz":
            from app.github_updates import public_status
            from app.community import public_status as community_status
            from app.rooms import public_status as room_status
            from core.database import public_status as storage_status
            from vivian.memory_store import is_ready as memory_is_ready
            ready = core_config.bot.is_ready()
            status = 200 if ready else 503
            content = json.dumps({
                "discord_ready": ready,
                "revision": core_config.DEPLOY_REVISION,
                "chat_style": core_config.CHAT_STYLE_VERSION,
                "storage": storage_status(),
                "github_updates": public_status(),
                "community": community_status(),
                "private_rooms": room_status(),
                "features_ready": hub_store.is_ready(),
                "ai_memory_ready": memory_is_ready(),
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

    def do_POST(self):
        from app import dashboard
        if not dashboard.handle(self):
            self.send_error(405, "Method not allowed")

    def do_OPTIONS(self):
        from app import dashboard
        if not dashboard.handle(self):
            self.send_error(405, "Method not allowed")


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
