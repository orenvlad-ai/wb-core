"""Bounded, repeatable window jobs retain no unpolled Future payloads."""

from __future__ import annotations

import gc
import io
import json
from datetime import date, timedelta
from contextlib import redirect_stderr
from pathlib import Path
import sys
from threading import Event
import time
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.web_vitrina_window_v3 import (
    MAX_JOB_RESULT_BYTES, SESSION_TTL_SECONDS, WindowV3Error, WindowV3Prepared,
    WindowV3Service, _WindowSession,
)


OWNER = ("user-1", "owner", "operator", ("vitrina",))
OWNER_INPUT = {
    "user_id": "user-1", "username": "owner", "role": "operator",
    "allowed_sections": ["vitrina"],
}
FOREIGN_INPUT = {**OWNER_INPUT, "user_id": "user-2"}


def prepared(kind: str, session_id: str, size: int = 1) -> WindowV3Prepared:
    return WindowV3Prepared(kind, session_id, b"x" * size, b"z" * size, 0.0, 0.0, 0.0)


def session(session_id: str) -> _WindowSession:
    now = time.monotonic()
    return _WindowSession(
        session_id, OWNER, "token", ["2026-04-20"], [], "2026-04-21",
        "2026-04-20", None, [], [], [], {}, {}, 0,
        prepared("manifest", session_id), now, now, {},
    )


def wait_completed(service: WindowV3Service, job_id: str) -> None:
    for _ in range(100):
        with service._lock:
            if service._jobs[job_id].future is None:
                return
        time.sleep(0.01)
    raise AssertionError("job did not complete")


class _LargeLocal:
    def __init__(self) -> None:
        self.blob = bytearray(8 * 1024 * 1024)


class WindowJobBudgetSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.service = WindowV3Service(SimpleNamespace())
        self.addCleanup(self.service.close)
        self.service._sessions["s1"] = session("s1")

    def test_unpolled_burst_is_bounded_and_repeatable(self) -> None:
        result = prepared("chunk", "s1", 1024 * 1024)
        job_ids = []
        for _ in range(12):
            status, pending = self.service._submit(OWNER, "chunk", "s1", lambda _cancel: result)
            self.assertEqual(status, 202)
            job_ids.append(pending["job_id"])
            wait_completed(self.service, pending["job_id"])
        with self.service._lock:
            held = sum(
                job.cached_result[1].retained_bytes
                for job in self.service._jobs.values()
                if job.cached_result and isinstance(job.cached_result[1], WindowV3Prepared)
            )
            self.assertLessEqual(held, MAX_JOB_RESULT_BYTES)
            self.assertTrue(all(job.future is None for job in self.service._jobs.values()))
        self.assertEqual(self.service._poll(job_ids[0], OWNER)[0], 200)
        self.assertEqual(self.service._poll(job_ids[0], OWNER)[0], 200)
        self.assertEqual(self.service._poll(job_ids[-1], OWNER)[0], 503)

    def test_failed_future_releases_large_traceback_locals(self) -> None:
        retained: list[weakref.ReferenceType[_LargeLocal]] = []

        def fail(_cancel):
            large = _LargeLocal()
            retained.append(weakref.ref(large))
            raise RuntimeError("fixture failure")

        diagnostic = io.StringIO()
        with redirect_stderr(diagnostic):
            status, pending = self.service._submit(OWNER, "chunk", "s1", fail)
            self.assertEqual(status, 202)
            wait_completed(self.service, pending["job_id"])
        logged = json.loads(diagnostic.getvalue().strip())
        self.assertEqual(logged["event"], "web_vitrina_window_v3_unexpected_error_v1")
        self.assertEqual(logged["operation"], "chunk")
        self.assertEqual(logged["exception_class"], "RuntimeError")
        self.assertTrue(logged["frames"])
        self.assertNotIn("fixture failure", diagnostic.getvalue())
        self.assertEqual(self.service._poll(pending["job_id"], OWNER)[0], 500)
        gc.collect()
        self.assertIsNone(retained[0]())

    def test_cancelled_manifest_retires_only_its_session(self) -> None:
        self.service._sessions["s2"] = session("s2")
        status, pending = self.service._submit(
            OWNER, "manifest", "", lambda _cancel: prepared("manifest", "s1"),
        )
        self.assertEqual(status, 202)
        wait_completed(self.service, pending["job_id"])
        self.assertEqual(self.service._cancel({"job_id": pending["job_id"]}, OWNER)[0], 200)
        self.assertNotIn("s1", self.service._sessions)
        self.assertIn("s2", self.service._sessions)
        with self.assertRaises(WindowV3Error) as caught:
            self.service._poll(pending["job_id"], OWNER)
        self.assertEqual(caught.exception.status, 409)

    def test_cached_result_is_stale_after_session_eviction(self) -> None:
        status, pending = self.service._submit(
            OWNER, "chunk", "s1", lambda _cancel: prepared("chunk", "s1"),
        )
        self.assertEqual(status, 202)
        wait_completed(self.service, pending["job_id"])
        self.assertEqual(self.service._poll(pending["job_id"], OWNER)[0], 200)
        with self.service._lock:
            self.service._retire_session_locked("s1")
        with self.assertRaises(WindowV3Error) as caught:
            self.service._poll(pending["job_id"], OWNER)
        self.assertEqual(caught.exception.status, 409)
        self.assertIsInstance(self.service._jobs[pending["job_id"]].cached_result[1], dict)

    def test_piggyback_ack_releases_only_completed_owner_result(self) -> None:
        result = prepared("chunk", "s1", 1024 * 1024)
        status, first = self.service._submit(OWNER, "chunk", "s1", lambda _cancel: result)
        self.assertEqual(status, 202)
        wait_completed(self.service, first["job_id"])
        self.assertEqual(self.service._poll(first["job_id"], OWNER)[0], 200)
        with self.assertRaises(WindowV3Error) as caught:
            self.service.request(
                "job", {"job_id": first["job_id"], "ack_job_ids": first["job_id"]},
                owner=FOREIGN_INPUT,
            )
        self.assertEqual(caught.exception.status, 404)
        self.assertIn(first["job_id"], self.service._jobs)

        status, second = self.service._submit(OWNER, "chunk", "s1", lambda _cancel: result)
        self.assertEqual(status, 202)
        wait_completed(self.service, second["job_id"])
        status, _ = self.service.request(
            "job", {"job_id": second["job_id"], "ack_job_ids": first["job_id"]},
            owner=OWNER_INPUT,
        )
        self.assertEqual(status, 200)
        self.assertNotIn(first["job_id"], self.service._jobs)
        self.assertIn("s1", self.service._sessions)
        self.assertEqual(self.service._poll(second["job_id"], OWNER)[0], 200)
        # Repeating an accepted ACK is harmless and never cancels the session.
        self.service.request(
            "job", {"job_id": second["job_id"], "ack_job_ids": first["job_id"]},
            owner=OWNER_INPUT,
        )

    def test_ack_cannot_release_pending_or_manifest_session(self) -> None:
        from threading import Event

        release = Event()
        status, pending = self.service._submit(
            OWNER, "chunk", "s1", lambda _cancel: (release.wait(2), prepared("chunk", "s1"))[1],
        )
        self.assertEqual(status, 202)
        status, _ = self.service.request(
            "job", {"job_id": pending["job_id"], "ack_job_ids": pending["job_id"]},
            owner=OWNER_INPUT,
        )
        self.assertEqual(status, 202)
        self.assertIn(pending["job_id"], self.service._jobs)
        release.set()
        wait_completed(self.service, pending["job_id"])
        status, manifest = self.service._submit(
            OWNER, "manifest", "", lambda _cancel: prepared("manifest", "s1"),
        )
        self.assertEqual(status, 202)
        wait_completed(self.service, manifest["job_id"])
        self.assertEqual(self.service._poll(manifest["job_id"], OWNER)[0], 200)
        self.service.request(
            "job", {"job_id": pending["job_id"], "ack_job_ids": manifest["job_id"]},
            owner=OWNER_INPUT,
        )
        self.assertNotIn(manifest["job_id"], self.service._jobs)
        self.assertIn("s1", self.service._sessions)

    def test_ack_shape_is_bounded(self) -> None:
        for value in ("a," * 9, "a,b,,c", "a" * 513):
            with self.subTest(value=value[:20]):
                with self.assertRaises(WindowV3Error) as caught:
                    self.service.request(
                        "job", {"job_id": "none", "ack_job_ids": value}, owner=OWNER_INPUT,
                    )
                self.assertEqual(caught.exception.status, 422)

    def test_global_handle_lives_with_bounded_session(self) -> None:
        current = time.monotonic()
        selected = self.service._sessions["s1"]
        selected.dates = [
            (date(2026, 9, 20) + timedelta(days=offset)).isoformat()
            for offset in range(14)
        ]
        selected.row_ids = ["SKU:1|stock_total"]
        selected.static_search_texts = ["stock"]
        selected.row_orders = [1]
        # A former absolute 60-second lease would discard this result despite
        # the still-live session and the next legitimate viewport seek.
        selected.globals["global-old"] = {
            "created_at": current - 61, "matched_set": {0},
        }
        selected.touched_at = current - 61
        for date_index in range(14):
            status, result = self.service.request(
                "seek", {
                    "session_id": "s1", "content_token": "token",
                    "date_index": str(date_index), "row_start": "0", "row_count": "1",
                    "selected_row_indexes": "0", "global_handle": "global-old",
                }, owner=OWNER_INPUT,
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                self.service._decode_cursor(result["cursor"], selected, OWNER)["g"],
                "global-old",
            )
        self.assertIn("global-old", selected.globals)
        self.assertGreater(selected.touched_at, current - 61)
        with self.assertRaises(WindowV3Error) as foreign:
            self.service.request(
                "seek", {"session_id": "s1", "content_token": "token",
                         "date_index": "0", "row_start": "0", "row_count": "1",
                         "selected_row_indexes": "0", "global_handle": "global-old"},
                owner=FOREIGN_INPUT,
            )
        self.assertEqual(foreign.exception.status, 404)
        with self.assertRaises(WindowV3Error) as wrong_token:
            self.service.request(
                "seek", {"session_id": "s1", "content_token": "other",
                         "date_index": "0", "row_start": "0", "row_count": "1",
                         "selected_row_indexes": "0", "global_handle": "global-old"},
                owner=OWNER_INPUT,
            )
        self.assertEqual(wrong_token.exception.status, 409)
        self.assertIn("global-old", selected.globals)
        selected.touched_at = time.monotonic() - SESSION_TTL_SECONDS - 1
        with self.assertRaises(WindowV3Error) as expired:
            self.service.request(
                "seek", {"session_id": "s1", "content_token": "token",
                         "date_index": "0", "row_start": "0", "row_count": "1",
                         "selected_row_indexes": "0", "global_handle": "global-old"},
                owner=OWNER_INPUT,
            )
        self.assertEqual(expired.exception.status, 409)
        self.assertNotIn("s1", self.service._sessions)

    def test_replacement_global_releases_previous_handle(self) -> None:
        selected = self.service._sessions["s1"]
        selected.row_ids = ["SKU:1|stock_total"]
        selected.static_search_texts = ["stock"]
        selected.row_orders = [1]
        with patch.object(self.service, "_check_input_version"):
            first = self.service._build_global(selected, {"search": "", "sort": ""}, Event())
            second = self.service._build_global(selected, {"search": "", "sort": ""}, Event())
        old = json.loads(first.json_bytes)["global_handle"]
        new = json.loads(second.json_bytes)["global_handle"]
        self.assertNotEqual(old, new)
        self.assertEqual(list(selected.globals), [new])
        with self.assertRaises(WindowV3Error) as stale:
            self.service._seek(
                selected, {"date_index": "0", "row_start": "0", "row_count": "1",
                           "selected_row_indexes": "0", "global_handle": old}, OWNER,
            )
        self.assertEqual(stale.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
