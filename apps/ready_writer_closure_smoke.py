#!/usr/bin/env python3
"""Reachable writer registry and deterministic repair/ordinary conflict pairs."""
import ast
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application import ready_publication as common
from apps import spp_metric_recompute as spp
from apps.spp_metric_recompute_smoke import _init_db
from ci.fixture_process import fixture_process, checkpoint

WRITERS = {
    "apps/ads_partial_publication.py",
    "apps/canonical_cost_engine_vitrina_publication.py",
    "apps/finance_daily_publication.py",
    "apps/supplier_shipment_publication_chain.py",  # disposable proof, apply remains disabled
    "apps/promo_metric_eligibility_recompute.py",
    "apps/spp_metric_recompute.py",
    "apps/sheet_vitrina_v1_historical_cost_carry_forward.py",
    "apps/sheet_vitrina_v1_proxy_v4_initialize.py",
    "apps/sheet_vitrina_v1_proxy_v4_reconcile.py",
    "apps/sheet_vitrina_v1_proxy_v4_transit_repair.py",
    "apps/web_vitrina_management_history.py",
    "apps/web_vitrina_web_source_publication.py",
    "packages/application/registry_upload_db_backed_runtime.py",
    "packages/application/warehouse_functional_economics_backfill.py",
    "packages/application/warehouse_historical_recovery.py",
    "packages/application/warehouse_fbs_material_rematerialization.py",
    "packages/application/warehouse_recovery_policy.py",
}
DIRECT_EXCEPTIONS = {
    "apps/sheet_vitrina_v1_proxy_margin_3_historical_backfill.py": "disabled legacy apply",
    "packages/application/warehouse_business_projection.py": "AFTER UPDATE trigger reads ready, writes projection invalidation",
}


def archive_owner_child(channel, root):
    from packages.application.promo_campaign_archive import promo_archive_fence
    with promo_archive_fence(Path(root)):
        checkpoint(channel, "archive_owned")


class ClosureTests(unittest.TestCase):
    def test_reachable_writer_registry_has_no_unclassified_direct_sql(self):
        actual = set()
        for directory in ("apps", "packages"):
            for path in (ROOT / directory).rglob("*.py"):
                if path.name.endswith("_smoke.py") or path.name.endswith("_fixture.py"):
                    continue
                relative = str(path.relative_to(ROOT))
                tree = ast.parse(path.read_text())
                calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
                if "replace_ready" in calls:
                    actual.add(relative)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(
                        r"(?:UPDATE(?:\s+OF\s+\w+\s+ON)?|INSERT\s+(?:OR\s+\w+\s+)?INTO|DELETE\s+FROM)\s+sheet_vitrina_v1_ready_snapshots\b", node.value, re.I):
                        self.assertIn(relative, DIRECT_EXCEPTIONS, relative)
        self.assertEqual(actual, WRITERS)

    def spp_fixture(self, root):
        db = root / spp.DB_FILENAME
        _init_db(db)
        goods = root / "goods.json"
        goods.write_text(json.dumps([{"nmID": 210183919, "discountOnSite": 23}, {"nmID": 210184534, "discountOnSite": 30}]))
        return db, dict(command="apply", runtime_dir=root, date_from=None, date_to=None,
            current_date="2026-05-06", storage_state_path=None, fixture_goods_json=goods, backup=True)

    def state(self, db):
        with closing(sqlite3.connect(db)) as conn:
            return (conn.execute("SELECT * FROM temporal_source_slot_snapshots ORDER BY source_key,snapshot_date,snapshot_role").fetchall(),
                    conn.execute("SELECT * FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date").fetchall())

    def test_spp_collection_source_and_catalog_drift(self):
        for kind in ("source", "catalog"):
            with self.subTest(kind=kind), TemporaryDirectory() as tmp:
                db, args = self.spp_fixture(Path(tmp))
                original = spp._load_fixture_goods
                observed = []
                def collect(path):
                    value = original(path)
                    with sqlite3.connect(db) as conn:
                        if kind == "source":
                            conn.execute("UPDATE temporal_source_slot_snapshots SET payload_json='{}' WHERE source_key='spp'")
                        else:
                            conn.execute("UPDATE registry_upload_config_v2 SET enabled=0")
                    observed.append(self.state(db))
                    return value
                with patch.object(spp, "_load_fixture_goods", side_effect=collect):
                    with self.assertRaises(common.ReadyPublicationConflict):
                        spp.run_recompute(**args)
                self.assertEqual(self.state(db), observed[0])

    def test_spp_ready_r2_and_transaction_rollback(self):
        for kind in ("target", "between_writes"):
            with self.subTest(kind=kind), TemporaryDirectory() as tmp:
                db, args = self.spp_fixture(Path(tmp))
                before = self.state(db)
                if kind == "target":
                    original = spp._backup_db
                    observed = []
                    def backup(path):
                        result = original(path)
                        with sqlite3.connect(db) as conn:
                            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=json_set(plan_json,'$.r2',1)")
                        observed.append(self.state(db))
                        return result
                    with patch.object(spp, "_backup_db", side_effect=backup):
                        with self.assertRaises(common.ReadyPublicationConflict):
                            spp.run_recompute(**args)
                    self.assertEqual(self.state(db), observed[0])
                else:
                    with patch.object(common, "replace_ready", side_effect=ValueError("after_temporal")):
                        with self.assertRaisesRegex(ValueError, "after_temporal"):
                            spp.run_recompute(**args)
                    self.assertEqual(self.state(db), before)

    def test_spp_exact_bundle_and_outer_date_not_column_date(self):
        with TemporaryDirectory() as tmp:
            db, args = self.spp_fixture(Path(tmp))
            with sqlite3.connect(db) as conn:
                conn.row_factory = sqlite3.Row
                row = dict(conn.execute("SELECT * FROM sheet_vitrina_v1_ready_snapshots LIMIT 1").fetchone())
                conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET as_of_date='2026-05-05'")
                row.update(bundle_version="untouched", as_of_date="2026-05-05")
                conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
            spp.run_recompute(**args)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version='untouched'").fetchone()[0], row["plan_json"])

    def test_promo_actual_file_bytes_source_and_ready_conflicts(self):
        from apps import promo_metric_eligibility_recompute as promo
        from apps import promo_metric_eligibility_recompute_smoke as fixture
        original = promo._apply_updates
        for kind in ("ready", "prices", "file"):
            with self.subTest(kind=kind):
                observed = []
                def before_commit(**args):
                    db = args["db_path"]
                    with sqlite3.connect(db) as conn:
                        if kind == "ready":
                            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=json_set(plan_json,'$.r2',1)")
                        elif kind == "prices":
                            conn.execute("UPDATE temporal_source_slot_snapshots SET payload_json='{}' WHERE source_key='prices_snapshot'")
                    if kind == "file":
                        path = next((args["runtime_dir"] / "promo_campaign_archive").glob("*/campaign_rows.jsonl"))
                        path.write_bytes(path.read_bytes().replace(b"508", b"507"))
                    expected = self.state(db)
                    with self.assertRaises(common.ReadyPublicationConflict):
                        original(**args)
                    self.assertEqual(self.state(db), expected)
                    observed.append(True)
                    raise ValueError("fixture_conflict_verified")
                with patch.object(promo, "_apply_updates", side_effect=before_commit):
                    with self.assertRaisesRegex(ValueError, "fixture_conflict_verified"):
                        fixture.main()
                self.assertEqual(observed, [True])

    def test_real_archive_owner_excludes_sync_and_releases_after_crash(self):
        from packages.application.promo_campaign_archive import sync_promo_campaign_archive
        with TemporaryDirectory() as tmp:
            with fixture_process(archive_owner_child, tmp) as child:
                child.wait("archive_owned")
                with self.assertRaises(BlockingIOError):
                    sync_promo_campaign_archive(Path(tmp))
                child.crash()
            sync_promo_campaign_archive(Path(tmp))

    def test_whole_after_image_undo_guard_preserves_newer_ready(self):
        from packages.application.warehouse_recovery_policy import _apply_undo_row, RecoveryPolicyError
        with TemporaryDirectory() as tmp:
            db, _ = self.spp_fixture(Path(tmp))
            with closing(sqlite3.connect(db)) as conn:
                conn.row_factory = sqlite3.Row
                row = dict(conn.execute("SELECT * FROM sheet_vitrina_v1_ready_snapshots LIMIT 1").fetchone())
                item = {"table_name": "sheet_vitrina_v1_ready_snapshots", "key": {key: row[key] for key in ("bundle_version", "as_of_date")}, "before": row, "after": row}
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json='newer'")
                with self.assertRaises(RecoveryPolicyError):
                    _apply_undo_row(conn, item)
                self.assertEqual(conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots").fetchone()[0], "newer")

    def test_canonical_prepared_before_images_reject_source_or_ready_r2(self):
        sys.path.insert(0, str(ROOT / "apps"))
        from apps import canonical_cost_engine_vitrina_publication_smoke as fixture
        from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry
        original = WarehouseRecoveryRegistry.begin_mutation
        for kind in ("source", "target"):
            observed = []
            def begin(registry, *args, **kwargs):
                result = original(registry, *args, **kwargs)
                with sqlite3.connect(registry.db_path) as conn:
                    if kind == "source":
                        conn.execute("UPDATE sheet_vitrina_v1_canonical_cost_daily_state SET physical_quantity='20'")
                    else:
                        conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=json_set(plan_json,'$.r2',1) WHERE bundle_version='bundle-2'")
                    observed.append(conn.execute("SELECT bundle_version,plan_json FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version").fetchall())
                return result
            # The original fixture itself owns the temporary DB lifetime. Verify
            # the rejected candidate before that fixture removes the directory.
            original_fail = WarehouseRecoveryRegistry.fail_recoverable
            def fail(registry, *args, **kwargs):
                with closing(sqlite3.connect(registry.db_path)) as conn:
                    self.assertEqual(conn.execute("SELECT bundle_version,plan_json FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version").fetchall(), observed[0])
                return original_fail(registry, *args, **kwargs)
            with self.subTest(kind=kind), patch.object(WarehouseRecoveryRegistry, "begin_mutation", new=begin), \
                 patch.object(WarehouseRecoveryRegistry, "fail_recoverable", new=fail):
                with self.assertRaisesRegex(ValueError, "publication .* input drift"):
                    fixture.main()
            self.assertEqual(len(observed), 1)

    def test_all_three_proxy_v4_repairs_fence_preflight_sources_before_begin(self):
        from apps import sheet_vitrina_v1_proxy_v4_initialize as initialize
        from apps import sheet_vitrina_v1_proxy_v4_reconcile as reconcile
        from apps import sheet_vitrina_v1_proxy_v4_transit_repair as transit
        from apps import sheet_vitrina_v1_proxy_v4_initialize_smoke as init_fixture
        from apps import sheet_vitrina_v1_proxy_v4_transit_repair_smoke as transit_fixture
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        for owner, fixture in ((initialize, init_fixture), (reconcile, init_fixture), (transit, transit_fixture)):
            target, method = (RegistryUploadDbBackedRuntime, "backup_database") if owner is transit else (owner, "_backup_and_verify")
            original = getattr(target, method)
            observed = []
            def backup(first, destination, **kwargs):
                result = original(first, destination, **kwargs)
                db = first.db_path if owner is transit else first
                with sqlite3.connect(db) as conn:
                    conn.execute("UPDATE registry_upload_config_v2 SET enabled=1-enabled")
                    observed.append((Path(db), conn.execute("SELECT bundle_version,plan_json FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date").fetchall()))
                return result
            real_check = common.check_queries
            def check(conn, expected):
                if not observed:
                    return real_check(conn, expected)
                with self.assertRaises(common.ReadyPublicationConflict):
                    real_check(conn, expected)
                self.assertEqual([tuple(row) for row in conn.execute("SELECT bundle_version,plan_json FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date")], observed[0][1])
                raise common.ReadyPublicationConflict("fixture_source_conflict_verified")
            with self.subTest(owner=owner.__name__), patch.object(target, method, new=backup), \
                 patch.object(common, "check_queries", side_effect=check):
                with self.assertRaisesRegex(common.ReadyPublicationConflict, "fixture_source_conflict_verified"):
                    fixture.main()
            self.assertEqual(len(observed), 1)


if __name__ == "__main__":
    unittest.main()
