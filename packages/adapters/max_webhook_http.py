"""Bounded inbound-only MAX listener, isolated from WebCore's serial queue."""

from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import socket
import sqlite3
import sys
import threading
import time

from packages.application.max_webhook_inbox import (
    MAX_BODY_BYTES, MaxWebhookConfig, load_max_webhook_config,
    check_inbox_storage, parse_inbox_event, persist_inbox_event,
)

MAX_WEBHOOK_PATH = "/v1/max/webhook"
MAX_WEBHOOK_HTTP_PORT = 8783
MAX_WEBHOOK_HEALTH_PATH = "/healthz"
BODY_READ_SECONDS = 8.0


def build_max_webhook_server(config: MaxWebhookConfig, *, port: int = MAX_WEBHOOK_HTTP_PORT) -> HTTPServer:
    class MaxWebhookServer(HTTPServer):
        inbox_healthy = True

        def handle_error(self, request, client_address) -> None:
            self.inbox_healthy = False
            print("MAX webhook handler unavailable", file=sys.stderr)

    class MaxWebhookHandler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(BODY_READ_SECONDS)

        def log_message(self, format, *args) -> None:
            # Never log request lines, headers, payloads or exception details.
            return

        def _respond(self, status: int, code: str) -> None:
            self.close_connection = True
            body = json.dumps({"ok": status == 200, "code": code}).encode("ascii")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass  # A lost ACK is retried and deduplicated by the inbox.

        def do_POST(self) -> None:  # noqa: N802
            if self.path != MAX_WEBHOOK_PATH:
                self._respond(404, "not_found")
                return
            values = self.headers.get_all("X-Max-Bot-Api-Secret", [])
            try:
                authorized = len(values) == 1 and hmac.compare_digest(
                    values[0].encode("ascii"), config.secret.encode("ascii"))
            except UnicodeEncodeError:
                authorized = False
            if not authorized:
                self._respond(401, "unauthorized")
                return
            if self.headers.get("Transfer-Encoding"):
                self._respond(400, "invalid_framing")
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                self._respond(400, "invalid_length")
                return
            length = int(lengths[0])
            if length > MAX_BODY_BYTES:
                self._respond(413, "body_too_large")
                return
            if length == 0:
                self._respond(400, "empty_body")
                return
            if (self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                    or self.headers.get("Content-Encoding", "identity").lower() != "identity"):
                self._respond(415, "unsupported_media_type")
                return
            try:
                deadline = time.monotonic() + BODY_READ_SECONDS
                chunks = []
                remaining = length
                while remaining:
                    read_seconds = deadline - time.monotonic()
                    if read_seconds <= 0:
                        raise TimeoutError
                    self.connection.settimeout(read_seconds)
                    chunk = self.rfile.read1(min(remaining, 65536))
                    if not chunk:
                        raise ValueError("truncated body")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                event = parse_inbox_event(b"".join(chunks), config.chat_ids)
            except (TimeoutError, socket.timeout):
                self._respond(408, "body_timeout")
                return
            except (ValueError, UnicodeError, RecursionError):
                self._respond(400, "invalid_update")
                return
            if event is not None:
                try:
                    persist_inbox_event(config, event)
                except (OSError, sqlite3.Error):
                    self.server.inbox_healthy = False
                    print("MAX webhook inbox unavailable", file=sys.stderr)
                    self._respond(503, "inbox_unavailable")
                    return
                self.server.inbox_healthy = True
            # Deliberately ignored authenticated events also stop MAX retries.
            self._respond(200, "received")

        def do_GET(self) -> None:  # noqa: N802
            if self.path != MAX_WEBHOOK_HEALTH_PATH:
                self._respond(404, "not_found")
                return
            healthy = self.server.inbox_healthy
            try:
                check_inbox_storage(config)
            except OSError:
                healthy = False
            self._respond(200 if healthy else 503, "configured" if healthy else "inbox_unavailable")

        def _reject_method(self) -> None:
            self._respond(405, "method_not_allowed")

        do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _reject_method

    return MaxWebhookServer(("127.0.0.1", port), MaxWebhookHandler)


def start_max_webhook_contour(business_runtime_dir: Path, *, port: int = MAX_WEBHOOK_HTTP_PORT):
    try:
        config = load_max_webhook_config(business_runtime_dir=business_runtime_dir)
        if config is None:
            return None
        server = build_max_webhook_server(config, port=port)
    except (ValueError, OSError):
        print("MAX webhook configuration or listener unavailable", file=sys.stderr)
        return None
    thread = threading.Thread(target=server.serve_forever, name="max-webhook-http", daemon=True)
    try:
        thread.start()
    except BaseException:
        server.server_close()
        raise
    return server, thread
