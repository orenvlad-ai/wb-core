"""Retained source collection and optimistic local derive regression tests."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_onec_zero_stock_empty_bucket_smoke import (
    _runtime, _OnecZeroStockSource, _build_bundle, _data_rows, FF_QTY, FF_COST, TARGET_DATE,
)
from apps.ready_publication_fixture import save_ready_fixture
from apps.sheet_vitrina_v1_refresh_read_split_smoke import CountingBlock
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.onec_stocks_block import OnecStocksBlock
from packages.application.sheet_vitrina_v1_onec_stocks import DEFAULT_ONEC_STAGE_MAPPING
from packages.application.sheet_vitrina_v1_live_plan import (
    SheetVitrinaV1LivePlanBlock, bind_local_derive_publication,
)
from packages.application import sheet_vitrina_v1_live_plan as live_plan
from packages.application import ready_publication as publication

FBS_TABLE = "sheet_vitrina_v1_wb_fbs_stock_snapshot_rows"
NOW = datetime(2026, 5, 20, 8, tzinfo=timezone.utc)


class LocalDeriveTests(unittest.TestCase):
    def setUp(self):
        self.ctx = _runtime()
        self.runtime = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.source = _OnecZeroStockSource()
        self.block = SheetVitrinaV1LivePlanBlock(self.runtime,
            onec_stocks_block=OnecStocksBlock(self.source, stage_mapping=DEFAULT_ONEC_STAGE_MAPPING),
            now_factory=lambda: NOW)
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            # The real immutable source table and its production revision triggers.
            publication.ensure_material_revisions(conn)
            conn.execute(f"INSERT INTO {FBS_TABLE}(run_id,seller_warehouse_id,chrt_id,nm_id,amount,evidence_digest) VALUES('generation-7',1,1,920001,7,'fixture')")
        self.kwargs = dict(as_of_date=TARGET_DATE, source_keys=["onec_stocks"],
            execution_mode="manual_operator", _include_archived_metrics_for_audit=True,
            metric_keys=[FF_QTY, FF_COST])

    def change_fbs(self):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            amount = conn.execute(f"SELECT max(amount)+1 FROM {FBS_TABLE}").fetchone()[0]
            conn.execute(f"INSERT INTO {FBS_TABLE}(run_id,seller_warehouse_id,chrt_id,nm_id,amount,evidence_digest) VALUES(?,1,1,920001,?,'fixture')", (f"generation-{amount}", amount))

    def test_fbs_changes_during_collection_are_reloaded_without_another_source_call(self):
        original = self.block._load_live_sources
        observed = []
        def phases(*args, **kwargs):
            if not kwargs.get("_collect_only"):
                with publication.readonly(self.runtime.db_path) as conn:
                    observed.append(conn.execute(f"SELECT max(amount) FROM {FBS_TABLE}").fetchone()[0])
            result = original(*args, **kwargs)
            if kwargs.get("_collect_only"):
                self.change_fbs()
            return result
        with patch.object(self.block, "_load_live_sources", side_effect=phases), \
             patch.object(self.source, "fetch", wraps=self.source.fetch) as fetch, \
             patch.object(self.runtime, "save_temporal_source_slot_snapshot",
                          wraps=self.runtime.save_temporal_source_slot_snapshot) as accept:
            plan = self.block.build_plan(**self.kwargs)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(accept.call_count, 2)
        self.assertEqual(observed, [8])
        self.assertEqual(plan.metadata["local_derive_attempt"], 1)
        with publication.readonly(self.runtime.db_path) as conn:
            publication.check_build_inputs(conn, plan.metadata["publication_inputs"])
        diagnostics = plan.metadata["refresh_diagnostics"]
        self.assertEqual(len(diagnostics["source_slots"]), 2)
        self.assertIn("local_derive_phase_summary", diagnostics)

    def test_drift_retries_only_local_operands_and_has_finite_budget(self):
        for every_time in [False, True]:
            original = self.block._load_live_sources
            attempts = []
            def phases(*args, **kwargs):
                result = original(*args, **kwargs)
                if not kwargs.get("_collect_only"):
                    attempts.append(1)
                    if every_time or len(attempts) == 1:
                        self.change_fbs()
                return result
            with patch.object(self.block, "_load_live_sources", side_effect=phases), \
                 patch.object(self.source, "fetch", wraps=self.source.fetch) as fetch:
                if every_time:
                    with self.assertRaisesRegex(publication.ReadyPublicationConflict, "ready_material_input_changed"):
                        self.block.build_plan(**self.kwargs)
                else:
                    plan = self.block.build_plan(**self.kwargs)
                    self.assertEqual(plan.metadata["local_derive_attempt"], 2)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(len(attempts), 3 if every_time else 2)

    def test_consumed_source_drift_is_not_relabelled_as_fresh(self):
        original = self.block._load_live_sources
        def phases(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("_collect_only"):
                with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
                    conn.execute("UPDATE temporal_source_slot_snapshots SET captured_at='2026-05-20T09:00:00Z' WHERE source_key='onec_stocks'")
            return result
        with patch.object(self.block, "_load_live_sources", side_effect=phases), \
             patch.object(self.source, "fetch", wraps=self.source.fetch) as fetch:
            with self.assertRaisesRegex(publication.ReadyPublicationConflict, "ready_source_changed:onec_stocks"):
                self.block.build_plan(**self.kwargs)
        self.assertEqual(fetch.call_count, 2)

    def test_cost_operand_and_calculated_cell_are_read_again_after_collection(self):
        bundle = _build_bundle()
        bundle["bundle_version"] = "local-derive-cost"
        bundle["metrics_v2"] = [{"metric_key": "cost_price_rub", "enabled": True,
            "scope": "SKU", "label_ru": "Cost", "calc_type": "metric", "calc_ref": "cost_price_rub",
            "show_in_data": True, "format": "rub", "display_order": 1, "section": "Cost"}]
        self.assertEqual(self.runtime.ingest_bundle(bundle, activated_at="2026-05-19T08:00:00Z").status, "accepted")
        def cost(value):
            self.assertEqual(self.runtime.ingest_cost_price_payload({
                "dataset_version": "cost-" + str(value), "uploaded_at": "2026-05-19T08:00:00Z",
                "cost_price_rows": [{"group": "Zero", "cost_price_rub": value, "effective_from": "2026-05-01"}],
            }, activated_at="2026-05-19T08:00:00Z").status, "accepted")
        kwargs = {**self.kwargs, "source_keys": ["cost_price"], "metric_keys": ["cost_price_rub"]}
        cost(10)
        before = self.block.build_plan(**kwargs)
        self.assertEqual(_data_rows(before)["SKU:920001|cost_price_rub"][-1], 10)
        original = self.block._load_live_sources
        def phases(*args, **options):
            result = original(*args, **options)
            if options.get("_collect_only"):
                cost(20)
            return result
        with patch.object(self.block, "_load_live_sources", side_effect=phases):
            after = self.block.build_plan(**kwargs)
        self.assertEqual(_data_rows(after)["SKU:920001|cost_price_rub"][-1], 20)

    def test_http_path_preserves_exact_publication_created_during_collection(self):
        initial = self.block.build_plan(**self.kwargs)
        current = self.runtime.load_current_state()
        save_ready_fixture(self.runtime, current_state=current, plan=initial,
            refreshed_at="2026-05-20T08:00:00Z")
        original = self.block._load_live_sources
        predecessor = []
        def phases(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("_collect_only"):
                from dataclasses import replace
                rival = replace(initial, metadata={"other_writer": "complete"})
                save_ready_fixture(self.runtime, current_state=current, plan=rival,
                    refreshed_at="2026-05-20T08:01:00Z")
                predecessor.append(self.runtime.prepare_sheet_vitrina_ready_publication(
                    bundle_version=current.bundle_version, as_of_date=TARGET_DATE).fingerprint)
            return result
        entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=self.runtime.runtime_dir,
            runtime=self.runtime, now_factory=lambda: NOW,
            activated_at_factory=lambda: "2026-05-20T08:02:00Z",
            refreshed_at_factory=lambda: "2026-05-20T08:02:00Z")
        entrypoint.sheet_plan_block = self.block
        build = self.block.build_plan
        with patch.object(self.block, "_load_live_sources", side_effect=phases), \
             patch.object(self.block, "build_plan", side_effect=lambda **kwargs: build(**{**kwargs, **self.kwargs})):
            result = entrypoint._run_sheet_refresh(as_of_date=TARGET_DATE, log=None)
        self.assertEqual(result["status"], "success")
        with publication.readonly(self.runtime.db_path) as conn:
            receipt = conn.execute("SELECT expected_digest FROM sheet_vitrina_v1_ready_publications ORDER BY created_at DESC LIMIT 1").fetchone()
        self.assertEqual(receipt[0], predecessor[0])

    def test_midnight_and_changed_registry_scope_reject_retained_collection(self):
        for change in ["date", "registry"]:
            original = self.block._load_live_sources
            def phases(*args, **kwargs):
                result = original(*args, **kwargs)
                if kwargs.get("_collect_only"):
                    if change == "date":
                        self.block.now_factory = lambda: NOW + timedelta(days=1)
                    else:
                        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
                            conn.execute("UPDATE registry_upload_config_v2 SET enabled=0 WHERE rowid=(SELECT min(rowid) FROM registry_upload_config_v2)")
                return result
            self.block.now_factory = lambda: NOW
            with patch.object(self.block, "_load_live_sources", side_effect=phases):
                with self.assertRaisesRegex(publication.ReadyPublicationConflict, "ready_collection_scope_or_date_changed"):
                    self.block.build_plan(**self.kwargs)

    def test_local_projection_never_writes_its_optional_cache(self):
        for nm_id in [920001, 920002]:
            self.runtime.save_nomenclature_item({"item_id": f"fixture-{nm_id}", "nm_id": nm_id,
                "our_sku": f"fixture-{nm_id}", "is_active": True,
                "created_at": "2026-05-19T08:00:00Z", "updated_at": "2026-05-19T08:00:00Z"})
        self.block.stocks_block = CountingBlock("stocks")
        bundle = _build_bundle()
        bundle["bundle_version"] = "local-cache-projection"
        bundle["metrics_v2"].append({"metric_key": "orderSum", "enabled": True,
            "scope": "SKU", "label_ru": "Orders", "calc_type": "metric", "calc_ref": "orderSum",
            "show_in_data": True, "format": "rub", "display_order": 2, "section": "Orders"})
        self.assertEqual(self.runtime.ingest_bundle(bundle, activated_at="2026-05-19T08:00:00Z").status, "accepted")
        with patch.object(self.runtime, "save_wb_incident_projection_cache",
                          side_effect=AssertionError("derive wrote cache")) as cache, \
             patch.object(live_plan, "build_vitrina_incident_stock_projection",
                          wraps=live_plan.build_vitrina_incident_stock_projection) as project:
            self.block.build_plan(as_of_date=TARGET_DATE, source_keys=["stocks"],
                                  metric_keys=["total_orderSum"])
        self.assertGreater(project.call_count, 0)
        self.assertEqual(cache.call_count, 0)
        self.assertTrue(all(call.kwargs["cache_enabled"] is False for call in project.call_args_list))

    def test_newer_publication_after_derive_still_conflicts(self):
        plan = self.block.build_plan(**self.kwargs)
        current = self.runtime.load_current_state()
        expected = self.runtime.prepare_sheet_vitrina_ready_publication(
            bundle_version=current.bundle_version, as_of_date=TARGET_DATE)
        save_ready_fixture(self.runtime, current_state=current, plan=plan,
            refreshed_at="2026-05-20T08:00:00Z")
        with self.assertRaisesRegex(publication.ReadyPublicationConflict, "ready_target_changed_after_local_derive"):
            bind_local_derive_publication(self.runtime, plan, current, expected)


if __name__ == "__main__":
    unittest.main()
