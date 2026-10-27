#!/usr/bin/env python3
"""Synthetic checks for bounded last-known Vitrina cost estimates."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.application.fbs_accounting_runtime import SCHEMA as BOOK_SCHEMA, path as accounting_path
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.shared_sku_cost import COMPONENTS, POLICY, SCHEMA
from packages.application.vitrina_last_known_cost import capture_source, project_costs
from packages.application.web_vitrina_management_history import SOURCE


def shared_day(day: str, rows: dict[str, tuple[str, str]]) -> dict:
    values = {}
    for nm_id, (status, cost) in rows.items():
        row = {"nm_id": int(nm_id), "quantity": "1", "capital_rub": cost,
               "unit_cost_rub": cost if status == "resolved" else None,
               "status": status, "reason": "" if status == "resolved" else "missing_cost",
               "components": []}
        row["source_digest"] = fingerprint(row)
        values[nm_id] = row
    result = {"schema": SCHEMA, "policy": POLICY, "candidate_only": True,
              "business_date": day, "components": list(COMPONENTS),
              "status": "closed", "quality": "complete", "reason": "",
              "rows": values, "source": {"fbs_baseline_id": "baseline-test"}}
    result["version_id"] = fingerprint(result)
    return result


def save_book(runtime: Path, periods: dict[str, dict], *, active=True, publication_error=None) -> None:
    path = accounting_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    shared_refs = {}
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE accounting_revisions(version TEXT PRIMARY KEY,operation_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,previous_version TEXT);"
            "CREATE TABLE accounting_current(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version TEXT NOT NULL);"
            "CREATE TABLE accounting_blobs(digest TEXT PRIMARY KEY,payload BLOB NOT NULL);"
        )
        for day, period in periods.items():
            raw = canonical(period)
            blob_digest = fingerprint(period)
            shared_refs[day] = blob_digest
            conn.execute("INSERT INTO accounting_blobs VALUES(?,?)", (blob_digest, zlib.compress(raw.encode())))
        from packages.application.fbs_accounting_runtime import pack
        book = {"schema": BOOK_SCHEMA, "active": active, "effective_date": "2026-09-08",
                "shared_days": periods, "wb_days": {}, "retained_days": {}, "presentations": {},
                "state": {"baseline": {}, "periods": {}, "observed_documents": {}},
                "publication_error": publication_error}
        raw = pack(conn, book)
        book_version = fingerprint(book)
        conn.execute("INSERT INTO accounting_revisions VALUES(?,?,?,?)",
                     (book_version, "fixture", raw, None))
        conn.execute("INSERT INTO accounting_current VALUES(1,?)", (book_version,))



class ReportingEstimateTests(unittest.TestCase):
    def test_approved_overlay_exact_priority_and_invalid_source(self):
        from datetime import date
        from packages.application.shared_sku_cost import SharedSkuCostSnapshot
        from packages.application.vitrina_last_known_cost import approved_reporting_snapshot
        from packages.application.wb_finance_weekly import recalculate_cost_derived_metrics
        prior = shared_day("2026-10-15", {"1": ("resolved", "12.5")})
        exact_day = shared_day("2026-10-21", {"1": ("resolved", "15")})
        exact = SharedSkuCostSnapshot([prior, exact_day], effective_date="2026-10-15")
        cell = {"source": SOURCE, "quality_state": "management_estimate", "target_date": "2026-10-16",
                "operation_id": "approved-test", "source_snapshot_id": "last-known-cost:test",
                "source_as_of_date": "2026-10-15", "management_value": "12.5",
                "source_day_version_id": prior["version_id"], "source_digest": prior["rows"]["1"]["source_digest"],
                "evidence": {"selection_method": "latest_same_sku_closed_resolved_on_or_before_target"}}
        with sqlite3.connect(":memory:") as conn:
            conn.executescript("CREATE TABLE registry_upload_current_state(slot,bundle_version); INSERT INTO registry_upload_current_state VALUES(1,'b'); CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version,plan_json,refreshed_at,as_of_date,snapshot_id);")
            def save(c):
                conn.execute("DELETE FROM sheet_vitrina_v1_ready_snapshots")
                conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?)", ('b', json.dumps({'metadata': {'server_cell_presentation': {'SKU:1|our_wb_unit_cost_rub': {'2026-10-16': c}}}}), 'now','2026-10-21','s'))
            save(cell)
            only_prior = SharedSkuCostSnapshot([prior], effective_date='2026-10-15')
            self.assertEqual(approved_reporting_snapshot(only_prior, conn).resolve(nm_id='1', operation_date=date(2026,10,16))['quality'], 'management_estimate')
            report = approved_reporting_snapshot(exact, conn)
            resolve = lambda snap, day, nm='1': snap.resolve(nm_id=nm, operation_date=date.fromisoformat(day))
            self.assertEqual(resolve(report, '2026-10-16')['unit_cost_rub'], '12.5')
            self.assertEqual(resolve(report, '2026-10-16')['quality'], 'management_estimate')
            self.assertEqual(resolve(report, '2026-10-21')['unit_cost_rub'], '15')
            self.assertEqual(resolve(report, '2026-10-17')['status'], 'missing')
            self.assertEqual(resolve(report, '2026-10-16', '2')['status'], 'missing')
            object.__setattr__(only_prior, 'invalid_from_date', '2026-10-16')
            self.assertEqual(resolve(approved_reporting_snapshot(only_prior, conn), '2026-10-16')['status'], 'missing')
            revised = SharedSkuCostSnapshot([prior, shared_day('2026-10-16', {'1': ('resolved','14')}), exact_day], effective_date='2026-10-15')
            self.assertEqual(resolve(approved_reporting_snapshot(revised, conn), '2026-10-16')['unit_cost_rub'], '14')
            for field, value in [('management_value','99'), ('source_digest','bad'), ('source_day_version_id','bad'), ('source_as_of_date','2026-10-21')]:
                save({**cell, field:value})
                self.assertEqual(resolve(approved_reporting_snapshot(exact, conn), '2026-10-16')['status'], 'missing')
        from apps.wb_finance_weekly_smoke import _seed_canonical_cost, _fixture_rows
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        with TemporaryDirectory() as tmp:
            block = WbFinanceWeeklyBlock(Path(tmp))
            block.ensure_schema()
            _seed_canonical_cost(block.db_path)
            rows = [{**r, 'nmId':1, 'rrDate':'2026-10-16', 'saleDt':'2026-10-16'} for r in _fixture_rows()[:2]]
            with sqlite3.connect(block.db_path) as conn:
                conn.row_factory = sqlite3.Row
                block._shared_cost_snapshot = report
                block.shared_cost_is_candidate = True
                metrics, coverage, _ = block._aggregate_rows(conn, rows, date(2026,10,12))
                self.assertEqual(coverage['estimated_units'],4)
                self.assertEqual(coverage['estimated_sales_revenue_rub'],'360.0000')
                self.assertEqual(coverage['cogs_rub'],'25.0000')
                self.assertEqual(metrics['profit_coverage_status'],'estimated')
                old_hash = coverage['cost_state_hash']
                block._shared_cost_snapshot = revised
                metrics2, coverage2, _ = block._aggregate_rows(conn,rows,date(2026,10,12))
                self.assertEqual(coverage2['estimated_units'],0)
                self.assertEqual(coverage2['cogs_rub'],'28.0000')
                self.assertNotEqual(old_hash,coverage2['cost_state_hash'])
                self.assertEqual(metrics2['profit_coverage_status'],'complete')
        metrics = {'net_revenue':'1000', 'profit_period_expenses':'200', 'positive_adjustments':'0'}
        coverage = {'covered_net_revenue_rub':'600','uncovered_net_revenue_rub':'400', 'uncovered_sales_revenue_rub':'400',
                    'matched_units':6,'unmatched_units':4,'partial_cogs_rub':'150','uncovered_sales_order_count':4,
                    'uncovered_sales_units':4,'sales_revenue_coverage_pct':'60','profit_coverage_status':'partial'}
        result = recalculate_cost_derived_metrics(metrics,coverage)
        self.assertEqual(result['before_cogs_profit'],'800.0000')
        self.assertEqual(result['before_cogs_margin_pct'],'80.0000')
        self.assertIsNone(result['profit_after_cogs'])
        full = recalculate_cost_derived_metrics(metrics, {**coverage,'matched_units':10,'unmatched_units':0,'partial_cogs_rub':'250','profit_coverage_status':'estimated','estimated_sales_revenue_rub':'400'})
        self.assertEqual(full['profit_after_cogs'],'550.0000')
        self.assertEqual(full['final_margin_pct'],'55.0000')
        self.assertEqual(full['sales_with_estimated_cost_rub'],'400')


class LastKnownCostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)

    def test_as_of_selection_skips_future_and_uses_older_resolved_cost(self):
        save_book(self.runtime, {
            "2026-10-16": shared_day("2026-10-16", {"1": ("resolved", "12.5"), "2": ("resolved", "20")}),
            "2026-10-18": shared_day("2026-10-18", {"1": ("missing", ""), "2": ("resolved", "22")}),
            "2026-10-20": shared_day("2026-10-20", {"1": ("resolved", "99"), "2": ("resolved", "33")}),
        })
        source = capture_source(self.runtime, dates=["2026-10-17", "2026-10-19"], bundle_version="bundle-a")
        self.assertEqual(source["costs_by_date"]["2026-10-17"]["SKU:1|our_wb_unit_cost_rub"]["management_value"], "12.5")
        self.assertEqual(source["costs_by_date"]["2026-10-19"]["SKU:1|our_wb_unit_cost_rub"]["source_as_of_date"], "2026-10-16")
        self.assertEqual(source["costs_by_date"]["2026-10-19"]["SKU:2|our_wb_unit_cost_rub"]["source_as_of_date"], "2026-10-18")
        self.assertNotEqual(source["costs_by_date"]["2026-10-19"]["SKU:1|our_wb_unit_cost_rub"]["management_value"], "99")

    def test_publication_error_keeps_only_closed_days_before_invalid_boundary(self):
        save_book(self.runtime, {
            "2026-10-16": shared_day("2026-10-16", {"1": ("resolved", "12")}),
            "2026-10-18": shared_day("2026-10-18", {"1": ("resolved", "18")}),
        }, publication_error={"reason": "source_gap", "invalid_from_date": "2026-10-18"})
        source = capture_source(self.runtime, dates=["2026-10-19"], bundle_version="bundle-a")
        self.assertEqual(source["costs_by_date"]["2026-10-19"]["SKU:1|our_wb_unit_cost_rub"]["management_value"], "12")

    def test_inactive_or_invalid_zero_cost_source_is_rejected(self):
        save_book(self.runtime, {"2026-10-16": shared_day("2026-10-16", {"1": ("resolved", "12")})}, active=False)
        with self.assertRaisesRegex(ValueError, "inactive"):
            capture_source(self.runtime, dates=["2026-10-17"], bundle_version="bundle-a")
        bad = shared_day("2026-10-16", {"1": ("resolved", "0")})
        save_book(self.runtime, {"2026-10-16": bad})
        with self.assertRaisesRegex(ValueError, "shared_day_invalid"):
            capture_source(self.runtime, dates=["2026-10-17"], bundle_version="bundle-a")

    def test_projection_preserves_existing_costs_and_never_fills_total_or_stock(self):
        save_book(self.runtime, {"2026-10-16": shared_day("2026-10-16", {"1": ("resolved", "12.5")})})
        source = capture_source(self.runtime, dates=["2026-10-17"], bundle_version="bundle-a")
        plan = {"date_columns": ["2026-10-17"], "metadata": {},
                "sheets": [{"sheet_name": "DATA_VITRINA", "header": ["label", "key", "2026-10-17"],
                    "rows": [["cost", "SKU:1|our_wb_unit_cost_rub", ""],
                             ["exact", "SKU:2|our_wb_unit_cost_rub", 0],
                             ["total cost", "TOTAL|total_our_wb_unit_cost_rub", ""],
                             ["stock", "SKU:1|stock_total", ""],
                             ["profit", "SKU:1|proxy_profit_3_rub", ""]]}]}
        result = project_costs(plan, dates=["2026-10-17"], source=source, operation_id="estimate-test")
        rows = {row[1]: row for row in result["plan"]["sheets"][0]["rows"]}
        self.assertEqual(rows["SKU:1|our_wb_unit_cost_rub"][2], 12.5)
        self.assertEqual(rows["SKU:2|our_wb_unit_cost_rub"][2], 0)
        self.assertEqual(rows["TOTAL|total_our_wb_unit_cost_rub"][2], "")
        self.assertEqual(rows["SKU:1|stock_total"][2], "")
        self.assertEqual(rows["SKU:1|proxy_profit_3_rub"][2], "")
        cell = result["plan"]["metadata"]["server_cell_presentation"]["SKU:1|our_wb_unit_cost_rub"]["2026-10-17"]
        self.assertEqual((cell["source"], cell["quality_state"], cell["source_as_of_date"]),
                         (SOURCE, "management_estimate", "2026-10-16"))
        again = project_costs(result["plan"], dates=["2026-10-17"], source=source, operation_id="estimate-test")
        self.assertEqual(again["changes"], [])

    def test_missing_cost_is_reported_without_zero_fill(self):
        save_book(self.runtime, {"2026-10-16": shared_day("2026-10-16", {"1": ("missing", "")})})
        source = capture_source(self.runtime, dates=["2026-10-17"], bundle_version="bundle-a")
        plan = {"date_columns": ["2026-10-17"], "metadata": {},
                "sheets": [{"sheet_name": "DATA_VITRINA", "header": ["label", "key", "2026-10-17"],
                    "rows": [["cost", "SKU:1|our_wb_unit_cost_rub", ""]]}]}
        result = project_costs(plan, dates=["2026-10-17"], source=source, operation_id="missing-test")
        self.assertEqual(result["plan"]["sheets"][0]["rows"][0][2], "")
        self.assertEqual(result["remaining"][0]["reason"], "no_prior_or_same_closed_resolved_cost")


if __name__ == "__main__":
    unittest.main()
