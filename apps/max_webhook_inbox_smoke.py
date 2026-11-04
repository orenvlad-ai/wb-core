"""Local-only MAX inbox contract tests. All identities and messages are fake."""

from __future__ import annotations

from contextlib import redirect_stderr
from http.client import HTTPConnection
from io import StringIO
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.adapters.max_webhook_http import (  # noqa: E402
    MAX_WEBHOOK_PATH, build_max_webhook_server, start_max_webhook_contour,
)
from packages.application.max_webhook_inbox import (  # noqa: E402
    DB_FILENAME, MAX_BODY_BYTES, MaxWebhookConfig, load_max_webhook_config,
)

SECRET = "synthetic-test-secret-0123456789abcdef"
CHAT_ID = -123456789
BOT_ID = 123456


def update(kind="message_created", *, chat_id=CHAT_ID, chat_type="chat", text="synthetic source text", timestamp=1000):
    if kind == "message_removed":
        return {"update_type": kind, "timestamp": timestamp, "chat_id": chat_id, "message_id": "fake.mid"}
    return {"update_type": kind, "timestamp": timestamp, "message": {
        "recipient": {"chat_id": chat_id, "chat_type": chat_type},
        "timestamp": 900, "body": {"mid": "fake.mid", "text": text}}}


class InboxContract(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="max-inbox-smoke-")
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir(mode=0o700)
        self.config = MaxWebhookConfig(SECRET, BOT_ID, frozenset({CHAT_ID}), self.inbox)
        self.server = build_max_webhook_server(self.config, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, payload=None, *, secret=SECRET, path=MAX_WEBHOOK_PATH, method="POST", headers=None, raw=None):
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        request_headers = {"Content-Type": "application/json"}
        if secret is not None:
            request_headers["X-Max-Bot-Api-Secret"] = secret
        request_headers.update(headers or {})
        body = raw if raw is not None else json.dumps(payload if payload is not None else update()).encode()
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def rows(self):
        db = self.inbox / DB_FILENAME
        if not db.exists():
            return []
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            return connection.execute("SELECT bot_id, chat_id, update_type, message_id, event_timestamp_ms, raw_payload FROM max_inbox_events ORDER BY rowid").fetchall()

    def test_authentication_before_body_or_storage(self):
        for secret in (None, "incorrect", "non-ascii-\u00e9"):
            self.assertEqual(self.request(secret=secret, raw=b"invalid-json")[0], 401)
        self.assertFalse((self.inbox / DB_FILENAME).exists())
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        connection.putrequest("POST", MAX_WEBHOOK_PATH)
        connection.putheader("X-Max-Bot-Api-Secret", SECRET)
        connection.putheader("X-Max-Bot-Api-Secret", SECRET)
        connection.putheader("Content-Length", "0")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        connection.close()

    def test_foreign_private_channel_and_forwarded_events_are_ignored(self):
        payloads = [update(chat_id=-987654321), update(chat_type="dialog"),
                    update(chat_type="channel"), update("message_removed", chat_id=-987654321),
                    {"update_type": "bot_started"}]
        forwarded = update(chat_id=-987654321)
        forwarded["message"]["link"] = {"chat_id": CHAT_ID}
        payloads.append(forwarded)
        for payload in payloads:
            self.assertEqual(self.request(payload)[0], 200)
        self.assertFalse((self.inbox / DB_FILENAME).exists())

    def test_durable_dedup_edits_deletes_and_raw_provenance(self):
        payload = update()
        first_raw = json.dumps(payload, indent=2).encode()
        self.assertEqual(self.request(raw=first_raw)[0], 200)
        # Read through a new connection after ACK: committed, not in memory.
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.request(raw=json.dumps(payload, sort_keys=True).encode())[0], 200)
        self.assertEqual(len(self.rows()), 1)
        edited = update("message_edited", text="synthetic edit", timestamp=2000)
        self.assertEqual(self.request(edited)[0], 200)
        self.assertEqual(self.request(edited)[0], 200)
        self.assertEqual(self.request(update("message_edited", text="second edit", timestamp=2000))[0], 200)
        self.assertEqual(self.request(update("message_removed", timestamp=3000))[0], 200)
        rows = self.rows()
        self.assertEqual([row[2] for row in rows], ["message_created", "message_edited", "message_edited", "message_removed"])
        self.assertEqual(rows[0][:5], (BOT_ID, CHAT_ID, "message_created", "fake.mid", 1000))
        self.assertEqual(rows[0][5], first_raw)
        self.assertEqual((self.inbox / DB_FILENAME).stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list(self.inbox.iterdir())), 1)

    def test_database_write_failure_is_retryable_without_ack_or_source_logs(self):
        self.assertEqual(self.request()[0], 200)
        db = self.inbox / DB_FILENAME
        with sqlite3.connect(db) as connection:
            connection.execute("CREATE TRIGGER reject_test_insert BEFORE INSERT ON max_inbox_events BEGIN SELECT RAISE(ABORT, 'synthetic private failure'); END")
        failure_payload = update("message_edited", text="private synthetic sentinel", timestamp=2000)
        logs = StringIO()
        with redirect_stderr(logs):
            status, body = self.request(failure_payload)
        self.assertEqual(status, 503)
        self.assertNotIn(b"private", body)
        self.assertNotIn("private", logs.getvalue())
        self.assertNotIn(SECRET, logs.getvalue())
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.request(method="GET", path="/healthz")[0], 503)
        with sqlite3.connect(db) as connection:
            connection.execute("DROP TRIGGER reject_test_insert")
        self.assertEqual(self.request(failure_payload)[0], 200)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.request(method="GET", path="/healthz")[0], 200)

    def test_health_checks_missing_or_exposed_storage_without_creating_it(self):
        self.inbox.rmdir()
        self.assertEqual(self.request(method="GET", path="/healthz")[0], 503)
        self.assertFalse(self.inbox.exists())
        self.inbox.mkdir(mode=0o755)
        self.assertEqual(self.request(method="GET", path="/healthz")[0], 503)
        self.inbox.chmod(0o700)
        self.assertEqual(self.request(method="GET", path="/healthz")[0], 200)
        self.assertFalse((self.inbox / DB_FILENAME).exists())

    def test_rejects_invalid_and_ambiguous_json_without_write(self):
        invalid = [b"[]", b"{", b'{"update_type":"message_created","update_type":"message_removed"}',
                   json.dumps(update(timestamp=True)).encode(), json.dumps(update(timestamp=2**63)).encode(),
                   b'{"update_type":"message_created","value":NaN}',
                   json.dumps(update(text="\ud800")).encode(), b"[" * 2000]
        for raw in invalid:
            self.assertEqual(self.request(raw=raw)[0], 400)
        self.assertFalse((self.inbox / DB_FILENAME).exists())

    def test_body_bounds_media_framing_and_narrow_route(self):
        self.assertEqual(self.request(headers={"Content-Length": str(MAX_BODY_BYTES + 1)})[0], 413)
        self.assertEqual(self.request(headers={"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.request(headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.request(headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request(headers={"Content-Encoding": "gzip"})[0], 415)
        self.assertEqual(self.request(raw=b"")[0], 400)
        self.assertEqual(self.request(path=MAX_WEBHOOK_PATH + "?secret=synthetic")[0], 404)
        self.assertEqual(self.request(path=MAX_WEBHOOK_PATH + "/")[0], 404)
        self.assertEqual(self.request(method="GET")[0], 404)
        self.assertEqual(self.request(method="PUT")[0], 405)
        self.assertFalse((self.inbox / DB_FILENAME).exists())

    def test_body_timeout_and_truncation(self):
        with patch("packages.adapters.max_webhook_http.BODY_READ_SECONDS", 0.05):
            connection = socket.create_connection(self.server.server_address, timeout=2)
            connection.sendall((f"POST {MAX_WEBHOOK_PATH} HTTP/1.1\r\nHost: test\r\nX-Max-Bot-Api-Secret: {SECRET}\r\nContent-Type: application/json\r\nContent-Length: 10\r\n\r\n{{").encode())
            response = connection.recv(4096)
            self.assertIn(b" 408 ", response)
            connection.close()
        connection = socket.create_connection(self.server.server_address, timeout=2)
        connection.sendall((f"POST {MAX_WEBHOOK_PATH} HTTP/1.1\r\nHost: test\r\nX-Max-Bot-Api-Secret: {SECRET}\r\nContent-Type: application/json\r\nContent-Length: 10\r\n\r\n{{").encode())
        connection.shutdown(socket.SHUT_WR)
        self.assertIn(b" 400 ", connection.recv(4096))
        connection.close()
        self.assertFalse((self.inbox / DB_FILENAME).exists())

    def test_component_does_not_touch_business_database(self):
        business = self.root / "business.sqlite3"
        business.write_bytes(b"synthetic business sentinel, not a SQLite database")
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(business.read_bytes(), b"synthetic business sentinel, not a SQLite database")


class ConfigContract(unittest.TestCase):
    def test_fail_closed_and_private_configuration(self):
        with TemporaryDirectory() as tmp:
            business = Path(tmp) / "business"
            env = {"WB_CORE_MAX_WEBHOOK_ENABLED": "1", "WB_CORE_MAX_WEBHOOK_SECRET": SECRET,
                   "WB_CORE_MAX_BOT_ID": str(BOT_ID), "WB_CORE_MAX_WEBHOOK_CHAT_IDS": str(CHAT_ID),
                   "WB_CORE_MAX_INBOX_DIR": str(Path(tmp) / "inbox")}
            self.assertIsNone(load_max_webhook_config(business_runtime_dir=business, environ={}))
            self.assertNotIn(SECRET, repr(load_max_webhook_config(business_runtime_dir=business, environ=env)))
            two_groups = load_max_webhook_config(business_runtime_dir=business,
                environ={**env, "WB_CORE_MAX_WEBHOOK_CHAT_IDS": f"{CHAT_ID},-987654321"})
            self.assertEqual(two_groups.chat_ids, frozenset({CHAT_ID, -987654321}))
            for key in env:
                broken = dict(env)
                broken[key] = "invalid"
                with self.assertRaises(ValueError):
                    load_max_webhook_config(business_runtime_dir=business, environ=broken)
            for value in (str(business), str(business / "nested"), "."):
                with self.assertRaises(ValueError):
                    load_max_webhook_config(business_runtime_dir=business, environ={**env, "WB_CORE_MAX_INBOX_DIR": value})
            for value in ("", "0", "123", str(-(2**63) - 1), str(CHAT_ID) + ","):
                with self.assertRaises(ValueError):
                    load_max_webhook_config(business_runtime_dir=business, environ={**env, "WB_CORE_MAX_WEBHOOK_CHAT_IDS": value})
            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(start_max_webhook_contour(business, port=0))
            with patch.dict(os.environ, {"WB_CORE_MAX_WEBHOOK_ENABLED": "1"}, clear=True), redirect_stderr(StringIO()):
                self.assertIsNone(start_max_webhook_contour(business, port=0))
            occupied = socket.socket()
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            try:
                with patch.dict(os.environ, env, clear=True), redirect_stderr(StringIO()):
                    self.assertIsNone(start_max_webhook_contour(business, port=occupied.getsockname()[1]))
            finally:
                occupied.close()
            self.assertFalse((Path(tmp) / "inbox").exists())


if __name__ == "__main__":
    unittest.main()
