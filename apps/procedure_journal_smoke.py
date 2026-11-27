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

    def test_history_refusal_enums_survive_serialization_without_arbitrary_text(self):
        for key, values in journal._HISTORY_ENUMS.items():
            for value in values:
                journal.event('history', 'source_watch_refused', 'failed', **{key:value})
                record = json.loads(json.dumps(self.records[-1]))
                self.assertEqual(record[key], value)
        secret = '/private/customer/canary-SQL-select'
        journal.event('history', 'source_watch_refused', 'failed', reason_code=secret,
                      source_class=secret, event_class=secret, path=secret, sql=secret)
        record = json.loads(json.dumps(self.records[-1]))
        self.assertFalse({'reason_code','source_class','event_class','path','sql'} & set(record))
        self.assertNotIn(secret, json.dumps(self.records))
        journal.event('history', 'handoff_revalidation', 'fresh_verify',
                      reason_code='history_source_changed_after_readback')
        self.assertEqual(json.loads(json.dumps(self.records[-1]))['reason_code'],
                         'history_source_changed_after_readback')

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

    def test_guarded_timers_leave_only_native_skip_before_and_after_lock(self):
        from contextlib import redirect_stdout
        import io
        import runpy
        from packages.application import business_data_procedure_admission as admission
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, REGISTRY_UPLOAD_RUNTIME_DIR=directory):
            admission.initialize_admission(Path(directory))
            for script in ("wb_buyer_authenticated_collect", "sheet_vitrina_v1_feedbacks_auto_complaints_tick",
                           "sheet_vitrina_v1_health_tick"):
                path = ROOT / "apps" / (script + ".py")
                for states in ((True,), (False, True)):
                    output = io.StringIO()
                    with self.subTest(script=script, states=states), redirect_stdout(output), \
                            patch.object(sys, "argv", [str(path)]), \
                            patch.object(admission, "barrier_status", side_effect=[{"active": b} for b in states]):
                        try:
                            runpy.run_path(str(path), run_name="__main__")
                        except SystemExit as exit_status:
                            self.assertEqual(exit_status.code, 0)
                    self.assertEqual(output.getvalue().splitlines(), [
                        '{"status": "skipped_maintenance", "reason": "skipped_maintenance"}'])
                    self.assertFalse(self.records)

    def test_admitted_cli_reports_return_value_and_help_uses_no_writer(self):
        from packages.application import business_data_procedure_admission as admission
        with tempfile.TemporaryDirectory() as directory:
            admission.initialize_admission(Path(directory))
            result = admission.guarded_cli(lambda: 7, journal_name="test", argv=["--runtime-dir", directory])
            self.assertEqual(result, 7)
            self.assertEqual(self.records[-1]["returncode"], 7)
            self.records.clear()
            self.assertEqual(admission.guarded_cli(lambda: 0, journal_name="test", argv=["--help"]), 0)
            self.assertFalse(self.records)

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
            if marker == "with cli_scope(" and "main = guard_cli(" in text:
                self.assertIn("from packages.application.business_data_procedure_admission import guard_cli", text)
            else:
                self.assertIn(marker, text, path.name)


if __name__ == "__main__":
    unittest.main()
