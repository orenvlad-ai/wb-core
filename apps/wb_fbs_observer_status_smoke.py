"""Known-order status refresh stays independent of listing cursors."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_fbs_orders import WbFbsOrdersPage, WbFbsOrderStatus, WbFbsOrdersTransportError
from packages.application.wb_fbs_orders import (
    WbFbsOrdersCollector, WbFbsOrdersError, STATE_TABLE, OBSERVATIONS_TABLE,
    STATUS_CURRENT_TABLE,
)


class Source:
    def __init__(self):
        self.response = [WbFbsOrderStatus(1, "new", "waiting")]
        self.list_calls = 0

    def list_orders(self, **kwargs):
        self.list_calls += 1
        return WbFbsOrdersPage(
            [{"id": 1, "nmId": 100, "deliveryType": "fbs", "createdAt": "2026-08-01T00:00:00Z"}],
            77, kwargs["limit"], kwargs["date_from"], kwargs["date_to"],
        )

    def list_statuses(self, ids):
        return self.response


class StatusRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "observer.sqlite"
        self.source = Source()
        self.now = "2026-09-07T10:00:00Z"
        self.collector = WbFbsOrdersCollector(
            db_path=self.path, source=self.source, enabled=True,
            timestamp_factory=lambda: self.now,
        )
        result = self.collector.collect(date_from=100, date_to=200, max_pages=1)
        self.assertEqual(result["next_cursor"], 77)
        self.assertGreater(result["db_write_count"], 0)
        self.before = self.snapshot()

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return {table: conn.execute(f"SELECT * FROM {table}").fetchall()
                    for table in (STATE_TABLE, OBSERVATIONS_TABLE)}

    def current(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute(f"SELECT * FROM {STATUS_CURRENT_TABLE}").fetchone())

    def test_old_order_refresh_repeated_cancel_reappeared(self):
        self.now = "2026-09-07T11:00:00Z"
        result = self.collector.refresh_known_statuses([1])
        self.assertEqual(result["new_status_observation_count"], 0)
        self.assertEqual(result["transition_count"], 0)
        self.assertEqual(result["db_write_count"], 1)
        self.assertEqual(self.current()["local_last_seen_at"], self.now)
        self.source.response = [WbFbsOrderStatus(1, "cancel", "canceled")]
        result = self.collector.refresh_known_statuses([1])
        self.assertEqual(result["transition_count"], 1)
        self.assertEqual(result["new_status_observation_count"], 1)
        self.source.response = [WbFbsOrderStatus(1, "new", "waiting")]
        result = self.collector.refresh_known_statuses([1])
        self.assertEqual(result["reappeared_pair_count"], 1)
        self.assertEqual(result["new_status_observation_count"], 0)
        self.assertEqual(self.current()["episode_sequence"], 3)
        self.assertEqual(self.source.list_calls, 1)
        self.assertEqual(self.snapshot(), self.before)

    def test_missing_is_not_terminal(self):
        before = self.current()
        self.source.response = []
        result = self.collector.refresh_known_statuses([1])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["missing_status_count"], 1)
        self.assertEqual(result["db_write_count"], 0)
        self.assertEqual(self.current(), before)
        self.assertEqual(self.snapshot(), self.before)

    def test_bad_batch_does_not_partially_persist(self):
        before = self.current()
        valid = WbFbsOrderStatus(1, "cancel", "canceled")
        for response, code in (
            ([valid, WbFbsOrderStatus(2, "new", "waiting")], "status_scope_drift"),
            ([valid, valid], "duplicate_status_response"),
        ):
            self.source.response = response
            with self.assertRaises(WbFbsOrdersError) as raised:
                self.collector.refresh_known_statuses([1])
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(self.current(), before)
        self.assertEqual(self.snapshot(), self.before)

    def test_schema_drift_and_unknown_order(self):
        with self.assertRaises(WbFbsOrdersError) as raised:
            self.collector.refresh_known_statuses([2])
        self.assertEqual(raised.exception.code, "unknown_status_order")
        self.source.response = [WbFbsOrderStatus(1, "new", "future_status")]
        result = self.collector.refresh_known_statuses([1])
        self.assertEqual(result["schema_drift_count"], 1)
        self.assertFalse(result["complete"])
        self.assertEqual(self.snapshot(), self.before)

    def test_status_failure_resumes_uncommitted_listing_page(self):
        class FailingSource:
            fail = True

            def __init__(self):
                self.cursors = []

            def list_orders(self, **kwargs):
                self.cursors.append(kwargs["next_cursor"])
                return WbFbsOrdersPage(
                    [{"id": 2, "nmId": 100, "deliveryType": "fbs"}],
                    88, kwargs["limit"], kwargs["date_from"], kwargs["date_to"],
                )

            def list_statuses(self, ids):
                if self.fail:
                    raise WbFbsOrdersTransportError("temporary failure")
                return [WbFbsOrderStatus(2, "new", "waiting")]

        source = FailingSource()
        self.collector.source = source
        with self.assertRaises(WbFbsOrdersError):
            self.collector.collect(date_from=100, date_to=200, next_cursor=77, max_pages=1)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            state = dict(conn.execute(f"SELECT * FROM {STATE_TABLE}").fetchone())
            self.assertEqual(state["next_cursor"], 77)
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {OBSERVATIONS_TABLE} WHERE order_id=2").fetchone()[0], 0)
        source.fail = False
        result = self.collector.collect(
            date_from=state["window_date_from"], date_to=state["window_date_to"],
            next_cursor=state["next_cursor"], max_pages=1,
        )
        self.assertEqual(source.cursors, [77, 77])
        self.assertEqual(result["new_observation_count"], 1)
        self.assertEqual(result["next_cursor"], 88)


if __name__ == "__main__":
    unittest.main()
