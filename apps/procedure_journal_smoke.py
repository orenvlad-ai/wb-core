"""Check wire format, redaction, CLI coverage, and diagnostic-only handoffs."""
import contextvars
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import socket
import threading
import unittest
from unittest.mock import patch
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages import procedure_journal as journal
from packages.procedure_journal_writer import forward


class ProcedureJournalTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.enterContext(patch.dict(os.environ, WBC_PROCEDURE_LOG="1"))
        self.enterContext(patch.object(journal, "_send", self.records.append))

    def test_worker_copies_only_observation_and_preserves_error(self):
        secret = "sensitive-example-must-never-appear"
        lease = contextvars.ContextVar("lease", default=None)
        error = ValueError(secret)
        def task():
            self.assertIsNone(lease.get())
            raise error
        with journal.span("http", "dispatch", root=True):
            lease.set(secret)
            call = journal.worker(task)
            with self.assertRaises(ValueError) as caught:
                contextvars.Context().run(call)
            self.assertIs(caught.exception, error)
        self.assertEqual(len(self.records), 4)
        parent, child, failed, done = self.records
        self.assertEqual(child["parent"], parent["span"])
        self.assertEqual(failed["span"], child["span"])
        self.assertEqual(failed["trace"], done["trace"])
        self.assertEqual(failed["state"], "raised")
        self.assertEqual(failed["error_kind"], "value")
        self.assertNotIn(secret, json.dumps(self.records))

    def test_handler_keepalive_routes_and_status(self):
        class Handler(journal.JournalHTTPMixin, BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_GET(self):
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args):
                pass
        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            client = HTTPConnection(*server.server_address, timeout=3)
            try:
                for path in ("/v1/max/webhook?private=canary-secret", "/canary-secret"):
                    client.request("GET", path, headers={"Authorization": "canary-secret"})
                    with client.getresponse() as response:
                        self.assertEqual(response.status, 403)
                        response.read()
            finally:
                client.close()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([r["name"] for r in self.records[::2]], ["GET.max_webhook", "GET.other"])
        self.assertEqual([r["http_status"] for r in self.records[1::2]], [403, 403])
        self.assertNotEqual(self.records[0]["trace"], self.records[2]["trace"])
        self.assertNotIn("canary-secret", json.dumps(self.records))

    def test_failed_sink_and_exit(self):
        with patch.object(journal, "_send", side_effect=BlockingIOError):
            with journal.span("test", "dropped"):
                pass
        with self.assertRaises(SystemExit) as outcome:
            with journal.cli_scope("test_cli"):
                raise SystemExit(7)
        self.assertEqual(outcome.exception.code, 7)
        self.assertGreaterEqual(self.records[0]["dropped_before"], 2)
        self.assertEqual(self.records[-1]["returncode"], 7)

    def test_disabled(self):
        with patch.dict(os.environ, WBC_PROCEDURE_LOG="0"), journal.cli_scope("test_cli"):
            journal.event("receipt", "operation", "written")
        self.assertFalse(self.records)

    def test_native_datagram(self):
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "journal")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
                listener.bind(path)
                listener.settimeout(3)
                a, b = socket.socketpair()
                with a, b:
                    a.send(b'{"v":1}')
                    a.shutdown(socket.SHUT_WR)
                    forward(b, path)
                self.assertEqual(listener.recv(4096),
                    b'SYSLOG_IDENTIFIER=wbc-procedure\nPRIORITY=6\nMESSAGE={"v":1}\n')

    def test_product_commands_and_pinned_exceptions(self):
        contract = json.loads((ROOT / "artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json").read_text())
        import hashlib
        for key in ("formula_code_hashes", "repair_code_hashes"):
            for relative, sha in contract[key].items():
                self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), sha, relative)
        for path in (ROOT / "apps").glob("*.py"):
            if re.search(r"smoke|_test\.py$|^test_|fixture|bench", path.name):
                continue
            text = path.read_text()
            if not re.search(r"if __name__\s*==\s*['\"]__main__['\"]", text):
                continue
            marker = {"web_vitrina_history_group_repair": "with history_procedure_admission(args)",
                      "web_vitrina_owned_history_worker": "with child_scope("}.get(path.stem, "with cli_scope(")
            self.assertIn(marker, text, path.name)


if __name__ == "__main__":
    unittest.main()

