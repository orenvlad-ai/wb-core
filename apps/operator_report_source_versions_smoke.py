"""Native source/receipt atomicity and exact-identity recovery in temporary DBs."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application import operator_report_source_versions as versions
from packages.application import operator_operations as operations

STAMP = "2026-10-08T10:00:00Z"


class SourceVersionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(self.temp.name))

    def dataset(self, identity, value=10, actor="operator"):
        return self.runtime.save_factory_order_dataset_state(dataset_type="stock_ff", uploaded_at=STAMP,
            rows=[{"nm_id": 1, "quantity": value}], uploaded_filename="stock.xlsx",
            uploaded_content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            workbook_bytes=f"synthetic-file-{value}".encode(), operation_id=identity, actor=actor)

    def baseline(self, identity, rows):
        return self.runtime.save_plan_report_monthly_baseline(rows=rows, uploaded_at=STAMP,
            source_kind="manual", uploaded_filename="baseline.xlsx", uploaded_content_type="xlsx",
            workbook_checksum=hashlib.sha256(versions.canonical(rows).encode()).hexdigest(),
            operation_id=identity, actor="operator")

    def rows(self):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(f"SELECT * FROM {versions.TABLE} ORDER BY operation_id")]

    def test_upload_replace_delete_retains_exact_old_versions_and_files(self):
        first = self.dataset("ors_first", 10)
        second = self.dataset("ors_second", 20)
        deleted, receipt = self.runtime.delete_factory_order_dataset_state("stock_ff", operation_id="ors_third",
            actor="operator", deleted_at=STAMP, include_acceptance=True)
        self.assertTrue(deleted)
        self.assertIsNone(self.runtime.load_factory_order_dataset_state("stock_ff"))
        rows = self.rows()
        self.assertEqual([r["action"] for r in rows], ["upload", "upload", "delete"])
        self.assertEqual(rows[1]["before_file"], rows[0]["after_file"])
        self.assertEqual(rows[2]["before_file"], rows[1]["after_file"])
        self.assertEqual(json.loads(rows[1]["before_json"])["rows_json"], '[{"nm_id": 1, "quantity": 10}]')
        self.assertNotEqual(first["source_ref"]["revision"], second["source_ref"]["revision"])
        for value in (first, second, receipt):
            self.assertTrue(value["durable_saved"])
            self.assertFalse(value["calculation_completed"])

    def test_lost_response_retry_after_newer_upload_never_overwrites_current(self):
        first = self.dataset("ors_first", 10)
        self.dataset("ors_second", 20)
        reopened = RegistryUploadDbBackedRuntime(runtime_dir=Path(self.temp.name))
        self.runtime = reopened
        self.assertEqual(self.dataset("ors_first", 10), first)
        self.assertEqual(reopened.load_factory_order_dataset_state("stock_ff")["rows"][0]["quantity"], 20)
        self.assertEqual(len(self.rows()), 2)

    def test_lost_delete_response_retry_does_not_delete_new_upload(self):
        self.dataset("ors_upload", 10)
        args = dict(operation_id="ors_delete", actor="operator", deleted_at=STAMP, include_acceptance=True)
        original = self.runtime.delete_factory_order_dataset_state("stock_ff", **args)
        self.dataset("ors_reupload", 20)
        self.assertEqual(self.runtime.delete_factory_order_dataset_state("stock_ff", **args), original)
        self.assertEqual(self.runtime.load_factory_order_dataset_state("stock_ff")["rows"][0]["quantity"], 20)

    def test_changed_same_identity_rejected_before_source_write(self):
        self.dataset("ors_original", 10)
        for value, actor in ((20, "operator"), (10, "other")):
            with self.assertRaisesRegex(ValueError, "identity_conflict"):
                self.dataset("ors_original", value, actor)
        with self.assertRaisesRegex(ValueError, "identity_conflict"):
            self.runtime.delete_factory_order_dataset_state("stock_ff", operation_id="ors_original", actor="operator")
        self.assertEqual(self.runtime.load_factory_order_dataset_state("stock_ff")["rows"][0]["quantity"], 10)
        self.assertEqual(len(self.rows()), 1)

    def test_receipt_failure_rolls_back_source_replace_and_delete(self):
        self.dataset("ors_original", 10)
        for action in (lambda: self.dataset("ors_failed", 20),
                       lambda: self.runtime.delete_factory_order_dataset_state("stock_ff", operation_id="ors_failed", actor="operator")):
            with patch.object(versions, "record", side_effect=RuntimeError("fixture receipt failure")):
                with self.assertRaisesRegex(RuntimeError, "receipt failure"):
                    action()
            self.assertEqual(self.runtime.load_factory_order_dataset_state("stock_ff")["rows"][0]["quantity"], 10)
            self.assertEqual(len(self.rows()), 1)

    def test_baseline_partial_month_versions_atomic_and_retry_stable(self):
        jan = {"month": "2026-01", "fin_buyout_rub": 100, "ads_sum": 10}
        feb = {"month": "2026-02", "fin_buyout_rub": 200, "ads_sum": 20}
        first = self.baseline("ors_baseline_1", [jan, feb])
        self.baseline("ors_baseline_2", [{**jan, "ads_sum": 15}])
        self.assertEqual(self.baseline("ors_baseline_1", [jan, feb]), first)
        state = self.runtime.load_plan_report_monthly_baseline()
        self.assertEqual([r["ads_sum"] for r in state], [15, 20])
        rows = self.rows()
        self.assertEqual([r["month"] for r in json.loads(rows[1]["before_json"])], ["2026-01"])
        with patch.object(versions, "record", side_effect=RuntimeError("fixture receipt failure")):
            with self.assertRaises(RuntimeError):
                self.baseline("ors_failed", [{**feb, "ads_sum": 25}])
        self.assertEqual(self.runtime.load_plan_report_monthly_baseline(), state)
        self.assertEqual(len(self.rows()), 2)

    def test_concurrent_same_identity_only_one_version(self):
        self.dataset("ors_initial", 1)  # Explicit schema bootstrap before threads.
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.dataset("ors_parallel", 30), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.runtime.load_factory_order_dataset_state("stock_ff")["rows"][0]["quantity"], 30)

    def test_versions_immutable_permission_filter_and_no_blob_in_public(self):
        receipt = self.dataset("ors_saved")
        db = self.runtime.db_path
        self.assertEqual(versions.read(db, "ors_saved", allowed_domains={"factory_order_dataset"}), receipt)
        self.assertIsNone(versions.read(db, "ors_saved", allowed_domains={"plan_report_baseline"}))
        self.assertNotIn("synthetic-file", json.dumps(receipt))
        with closing(sqlite3.connect(db)) as conn:
            for sql in (f"UPDATE {versions.TABLE} SET actor='other'", f"DELETE FROM {versions.TABLE}"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    conn.execute(sql)
        with self.assertRaisesRegex(ValueError, "identity"):
            self.dataset("ffpdr_foreign_domain")

    def test_read_legacy_db_does_not_bootstrap(self):
        db = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE legacy(value TEXT)")
            conn.commit()
        before = db.read_bytes()
        self.assertIsNone(versions.read(db, "ors_missing", allowed_domains=versions.DOMAINS))
        self.assertEqual(db.read_bytes(), before)

    def test_journal_permissions_apply_before_counts_pagination_and_detail(self):
        self.dataset("ors_hidden_supply")
        self.baseline("ors_visible_report", [{"month":"2026-01","fin_buyout_rub":100,"ads_sum":10}])
        db = self.runtime.db_path
        before = db.read_bytes()
        result = operations.journal(db, allowed_domains={"plan_report_baseline"}, limit=1)
        self.assertEqual(result["total"], 1)
        self.assertFalse(result["has_more"])
        self.assertEqual(result["items"][0]["operation_id"], "ors_visible_report")
        self.assertNotIn("ors_hidden_supply", json.dumps(result))
        self.assertIsNone(operations.read_acceptance(db,"ors_hidden_supply",allowed_domains={"plan_report_baseline"}))
        self.assertEqual(operations.journal(db,allowed_domains={"plan_report_baseline"},domain="factory_order_dataset")["total"],0)
        self.assertEqual(operations.journal(db,allowed_domains=set())["total"],0)
        self.assertEqual(db.read_bytes(),before)


if __name__ == "__main__":
    unittest.main()
