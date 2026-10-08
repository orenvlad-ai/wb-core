"""Payload-free child failures survive parent supervision and cycle restart."""
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps import web_vitrina_owned_history_worker as child
from apps import sheet_vitrina_v1_cycle_smoke as cycle_fixture
from packages.application import owned_history_worker as supervisor
from packages.application.owned_history_worker_capability import (
    HistoryDelegationError, history_child_failure,
)
from packages.application.web_vitrina_history_live_adapter import LiveSourceUnavailable
from packages.application.web_vitrina_history_store import HistoryUnavailable
from packages.application.sheet_vitrina_v1_cycle import CycleReceiptStore, run_cycle
from packages.application.business_data_heavy_admission import heavy_admitted

NOW = datetime(2026, 10, 8, 4, 42, tzinfo=timezone.utc)
SECRET = "provider-token-and-business-payload"


class DiagnosticsTests(unittest.TestCase):
    def worker(self, response):
        # Exercise real complete/capture/verify orchestration; no runtime or child
        # is constructed. Linux kernel transport has its existing separate suite.
        worker = supervisor._OwnedHistoryWorker.__new__(supervisor._OwnedHistoryWorker)
        worker.operation = "cycle"
        worker._completion_used = worker._capture_used = worker._portion_used = False
        worker._anchor = worker._target = None
        worker._check = Mock()
        calls = []
        def invoke(mode, now, anchor):
            calls.append(mode)
            if mode == response[0]:
                return response[1]
            if mode == "capture":
                return {"status": "complete", "result": {"status": "captured", "anchor": {"now": now}}}
            if mode == "verify":
                return {"status": "complete", "invocation": "verify-fixture",
                        "result": {"status": "verified", "invocation": "verify-fixture",
                                   "terminal": False, "completed": {}}}
            self.fail("unexpected portion replay")
        worker._invoke = invoke
        return worker, calls

    def test_child_source_reason_reaches_completion_without_payload(self):
        cap = {"mode": "capture", "invocation": "fixture"}
        sent = []
        with patch.object(sys, "argv", ["worker", "3"]), \
             patch.object(child, "child_bootstrap", return_value=(Mock(), cap)), \
             patch.object(child, "execute", side_effect=LiveSourceUnavailable("live_source_resource_limit:" + SECRET)), \
             patch.object(child, "send_message", side_effect=lambda channel, value: sent.append(value)):
            self.assertEqual(child.main(), 0)
        worker, calls = self.worker(("capture", {"status": "complete", "result": sent[0]["result"]}))
        with self.assertRaises(HistoryDelegationError) as caught:
            worker.complete(NOW)
        self.assertEqual(caught.exception.diagnostic(), {
            "error_code": "history_completion_capture_unproven", "mode": "capture",
            "reason_code": "live_source_resource_limit", "outcome": "failed"})
        self.assertEqual(calls, ["capture"])
        self.assertNotIn(SECRET, json.dumps(sent) + str(caught.exception))

    def test_verify_and_portion_preserve_distinct_failure_modes(self):
        for mode, code, expected_calls in (
            ("verify", "history_completion_readback_unproven", ["capture", "verify"]),
            ("portion", "history_completion_portion_failed", ["capture", "verify", "portion", "verify"]),
        ):
            with self.subTest(mode=mode):
                failure = history_child_failure(HistoryDelegationError("history_worker_anchor_changed"), mode=mode)
                worker, calls = self.worker((mode, {"status": "complete", "result": failure}))
                with self.assertRaises(HistoryDelegationError) as caught:
                    worker.complete(NOW)
                self.assertEqual(caught.exception.diagnostic(), {
                    "error_code": code, "mode": mode,
                    "reason_code": "history_worker_anchor_changed", "outcome": "failed"})
                self.assertEqual(calls, expected_calls)

    def test_known_catalog_storage_and_edition_failures_retain_precise_reason(self):
        for reason in ("history_catalog_limit", "history_storage_limit", "history_edition_corrupt"):
            with self.subTest(reason=reason):
                failure = history_child_failure(HistoryUnavailable(reason + ":" + SECRET),
                                                mode="portion", source_error=True)
                worker, calls = self.worker(("portion", {"status": "complete", "result": failure}))
                with self.assertRaises(HistoryDelegationError) as caught:
                    worker.complete(NOW)
                self.assertEqual(caught.exception.diagnostic()["reason_code"], reason)
                self.assertNotIn(SECRET, json.dumps(caught.exception.diagnostic()))
                self.assertEqual(calls, ["capture", "verify", "portion", "verify"])

    def test_unknown_capture_is_consumed_and_never_replayed(self):
        worker, calls = self.worker(("capture", {"status": "outcome_unknown", "readback": {}}))
        with self.assertRaises(HistoryDelegationError) as caught:
            worker.complete(NOW)
        self.assertEqual(caught.exception.diagnostic()["reason_code"], "history_transport_outcome_unknown")
        self.assertEqual(caught.exception.diagnostic()["outcome"], "outcome_unknown")
        with self.assertRaisesRegex(HistoryDelegationError, "already_consumed"):
            worker.complete(NOW)
        self.assertEqual(calls, ["capture"])

    def test_unknown_portion_no_progress_stops_after_exact_verify(self):
        worker, calls = self.worker(("portion", {"status": "outcome_unknown", "readback": {}}))
        with self.assertRaisesRegex(HistoryDelegationError, "no_dated_progress") as caught:
            worker.complete(NOW)
        self.assertEqual(caught.exception.diagnostic(), {
            "error_code": "history_completion_no_dated_progress", "mode": "portion",
            "reason_code": "history_transport_outcome_unknown", "outcome": "outcome_unknown"})
        self.assertEqual(calls, ["capture", "verify", "portion", "verify"])

    def test_untrusted_child_reason_and_generic_exception_are_sanitized(self):
        failures = [history_child_failure(ValueError(SECRET), mode="capture"),
                    {"status": "failed", "reason": SECRET, "diagnostic": {"reason_code": SECRET, "mode": SECRET}}]
        for failure in failures:
            worker, calls = self.worker(("capture", {"status": "complete", "result": failure}))
            with self.assertRaises(HistoryDelegationError) as caught:
                worker.complete(NOW)
            self.assertNotIn(SECRET, str(caught.exception) + json.dumps(caught.exception.diagnostic()))
            self.assertEqual(caught.exception.diagnostic()["mode"], "capture")
            self.assertEqual(calls, ["capture"])

    def test_standard_exception_type_is_retained_without_message(self):
        class UnknownProviderTimeout(TimeoutError):
            pass
        for exc, reason in (
            (ValueError(SECRET), "history_worker_exception_value"),
            (TimeoutError(SECRET), "history_worker_exception_timeout"),
            (MemoryError(SECRET), "history_worker_exception_memory"),
            (sqlite3.OperationalError(SECRET), "history_worker_exception_sqlite_operational"),
            (UnknownProviderTimeout(SECRET), "history_worker_exception"),
        ):
            with self.subTest(reason=reason):
                failure = history_child_failure(exc, mode="capture")
                worker, calls = self.worker(("capture", {"status": "complete", "result": failure}))
                with self.assertRaises(HistoryDelegationError) as caught:
                    worker.complete(NOW)
                self.assertEqual(caught.exception.diagnostic()["reason_code"], reason)
                self.assertNotIn(SECRET, json.dumps(failure) + str(caught.exception))
                self.assertEqual(calls, ["capture"])

    def test_pretransport_guard_has_invocation_mode(self):
        worker = supervisor._OwnedHistoryWorker.__new__(supervisor._OwnedHistoryWorker)
        worker._invoke_owned = Mock(side_effect=HistoryDelegationError("history_parent_owner_changed"))
        with self.assertRaises(HistoryDelegationError) as caught:
            worker._invoke("verify", NOW.isoformat(), {})
        self.assertEqual(caught.exception.diagnostic()["mode"], "verify")
        self.assertEqual(caught.exception.diagnostic()["reason_code"], "history_parent_owner_changed")

    def test_failed_cycle_diagnostic_survives_restart_and_does_not_replay(self):
        fixture = cycle_fixture.CycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        receipt, lock = fixture.accepted()
        fake = cycle_fixture.CycleFake(fixture.root)
        error = HistoryDelegationError("history_completion_capture_unproven", mode="capture",
                                       reason_code="live_source_read_incomplete")
        try:
            with patch.object(fake, "_cycle_history", side_effect=error), \
                 heavy_admitted(fixture.root, operation="cycle"), self.assertRaises(HistoryDelegationError):
                run_cycle(fake, fixture.store, receipt, fixture.config, lambda _: None)
        finally:
            lock.close()
        reloaded = CycleReceiptStore(fixture.root, lambda: cycle_fixture.STAMP).read(receipt["cycle_id"])
        self.assertEqual(reloaded["history_failure"], error.diagnostic())
        self.assertEqual(reloaded["stages"][-1]["history_failure"], error.diagnostic())
        self.assertEqual(reloaded["error_code"], error.code)
        prior, lock = fixture.accepted()
        self.assertIsNone(lock)
        self.assertEqual(prior["cycle_id"], receipt["cycle_id"])
        self.assertEqual(fake.events.count("api_sources"), 1)
        self.assertEqual(fake.events.count("warehouse"), 1)

    def test_mutated_exception_fields_cannot_write_payload_to_cycle(self):
        fixture = cycle_fixture.CycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        receipt, lock = fixture.accepted()
        fake = cycle_fixture.CycleFake(fixture.root)
        error = HistoryDelegationError(SECRET, reason_code=SECRET)
        error.code = error.mode = error.reason_code = error.outcome = SECRET
        try:
            with patch.object(fake, "_cycle_history", side_effect=error), \
                 heavy_admitted(fixture.root, operation="cycle"), self.assertRaises(HistoryDelegationError):
                run_cycle(fake, fixture.store, receipt, fixture.config, lambda _: None)
        finally:
            lock.close()
        raw = (fixture.store.root / (receipt["cycle_id"] + ".json")).read_text()
        self.assertNotIn(SECRET, raw)
        self.assertEqual(json.loads(raw)["history_failure"], {
            "error_code": "history_failure_unknown", "mode": "parent",
            "reason_code": "history_failure_unknown", "outcome": "failed"})


if __name__ == "__main__":
    unittest.main()
