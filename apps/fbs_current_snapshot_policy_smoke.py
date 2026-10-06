#!/usr/bin/env python3
"""Owned offline evidence for canonical nine-hour current-source admission."""
from contextlib import closing, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.web_vitrina_official_fbs_smoke import fixture
from apps.fbs_inventory_presentation_smoke import snapshot
from packages.application import fbs_accounting_runtime as book
from packages.application import business_data_schedule_profile as schedule
from packages.application.fbs_current_snapshot_policy import resolve_current_snapshot_policy, FbsSnapshotPolicyError, open_current_snapshot_readonly
from packages.application.official_fbs_stock_read import read_complete_official_fbs_stock, current_official_fbs_facilities
from packages.application.storage_registry import build_manifest, atomic_write_manifest, StoreRegistry, MANIFEST_FILENAME
from packages.application.wb_fbs_warehouse_registry import _freshness, FACILITIES_TABLE
from packages.application.ff_pool_foundation import FACILITY_PROFILES_TABLE

NOW = datetime(2026, 9, 5, 6, tzinfo=timezone.utc)
BOOK_NOW = datetime(2026, 9, 7, 14, tzinfo=timezone.utc)


def signed(value):
    return {**value, "fingerprint": schedule.fingerprint(value)}


def select(runtime, *, phase="released"):
    # Synthetic committed metadata in an owned fixture, never run the cutover.
    operation = "offline-fbs-policy"
    plan = signed({"schema_version": schedule.PLAN_SCHEMA, "runtime_dir": str(runtime.resolve()),
                   "operation_id": operation, "profile": schedule.PROFILE,
                   "profile_fingerprint": schedule.fingerprint(schedule.PROFILE),
                   "baseline": {}, "baseline_fingerprint": schedule.fingerprint({}),
                   "window_id": "offline-window", "deployed_sha": "a" * 40})
    transition = signed({"schema_version": schedule.TRANSITION_SCHEMA, "plan": plan, "phase": phase})
    directory = runtime / schedule.TRANSITIONS_DIRECTORY
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
    path = directory / (operation + ".json")
    path.write_text(json.dumps(transition)); path.chmod(0o600)
    selector = signed({"schema_version": schedule.SCHEMA, "runtime_dir": str(runtime.resolve()),
        "profile": schedule.PROFILE, "profile_fingerprint": schedule.fingerprint(schedule.PROFILE),
        "operation_id": operation, "plan_fingerprint": plan["fingerprint"]})
    selector.update({key: plan[key] for key in ("window_id", "baseline_fingerprint", "deployed_sha")})
    selector = signed({key: value for key, value in selector.items() if key != "fingerprint"})
    path = runtime / schedule.SELECTOR_FILENAME
    path.write_text(json.dumps(selector)); path.chmod(0o600)


def canonical(runtime, path, conn, *, monolith=False):
    relative = str(path.relative_to(runtime))
    manifest = build_manifest(state="monolith" if monolith else "cutover", canonical_source="monolith" if monolith else "split",
        generation_epoch="epoch1", raw_generation_id="op1" if monolith else "raw1",
        raw_relative_path=relative if monolith else "generations/raw.sqlite3", raw_watermark="raw-proof",
        operational_generation_id="op1", operational_relative_path=relative, operational_watermark="op-proof",
        rollback_generation_id="legacy", source_fingerprint="source-proof")
    if not monolith:
        conn.execute("CREATE TABLE finance_operational_schema_meta(singleton INTEGER PRIMARY KEY,schema_revision,logical_store,generation_id,generation_epoch,source_fingerprint)")
        conn.execute("INSERT INTO finance_operational_schema_meta VALUES(1,?,?,?,?,?)",
                     (manifest.operational.schema_revision, "operational", "op1", "epoch1", "source-proof"))
        conn.commit()
    atomic_write_manifest(runtime / MANIFEST_FILENAME, manifest)
    return manifest


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime = self.root / "runtime"; self.runtime.mkdir()
        self.path = self.runtime / "generations" / "op.sqlite3"; self.path.parent.mkdir()
        self.conn = fixture(self.path); self.conn.row_factory = sqlite3.Row
        self.conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_runs SET snapshot_at=?", (NOW.isoformat(),)); self.conn.commit()
        self.addCleanup(self.conn.close)
        canonical(self.runtime, self.path, self.conn)

    def read(self, now=NOW, *, connection=None):
        with (nullcontext(connection) if connection is not None else closing(open_current_snapshot_readonly(self.path))) as conn:
            return read_complete_official_fbs_stock(conn, universe=[1, 2], day="2026-09-05", now=now)

    def test_legacy_no_provision_and_canonical_selected_boundary(self):
        before = {p: p.read_bytes() for p in self.runtime.rglob("*") if p.is_file()}
        self.assertTrue(self.read()["available"])
        with self.assertRaisesRegex(ValueError, "not_fresh"):
            self.read(NOW + timedelta(minutes=31))
        self.assertEqual(before, {p: p.read_bytes() for p in self.runtime.rglob("*") if p.is_file()})
        select(self.runtime)
        self.assertEqual(self.read(NOW + timedelta(hours=9))["freshness_max_age_seconds"], 32400)
        with self.assertRaisesRegex(ValueError, "not_fresh"):
            self.read(NOW + timedelta(hours=9, seconds=1))
        with self.assertRaisesRegex(ValueError, "not_fresh"):
            self.read(NOW + timedelta(days=1))
        self.assertEqual(_freshness(NOW.isoformat(), (NOW - timedelta(minutes=5)).isoformat(), max_age_seconds=32400), "fresh")
        with self.assertRaisesRegex(ValueError, "not_fresh"):
            self.read(NOW - timedelta(minutes=5, seconds=1))

    def test_unknown_copy_in_memory_and_selector_alone_never_grant_nine_hours(self):
        select(self.runtime)
        copy = self.runtime / "generations" / "copied.sqlite3"
        shutil.copyfile(self.path, copy)
        with closing(sqlite3.connect(copy)) as conn:
            self.assertEqual(resolve_current_snapshot_policy(connection=conn).official_max_age_seconds, 1800)
            with self.assertRaisesRegex(FbsSnapshotPolicyError, "authority_mismatch"):
                resolve_current_snapshot_policy(connection=conn, runtime_dir=self.runtime)
        with closing(sqlite3.connect(":memory:")) as conn:
            self.assertEqual(resolve_current_snapshot_policy(connection=conn).book_max_age_seconds, 10800)
        unproven = self.root / "unproven"; unproven.mkdir(); select(unproven)
        self.assertFalse(resolve_current_snapshot_policy(runtime_dir=unproven).selected)

    def test_canonical_bad_profile_and_generation_fail_closed(self):
        select(self.runtime, phase="selector_selected")
        with self.assertRaises(FbsSnapshotPolicyError): self.read()
        select(self.runtime)
        path = self.runtime / schedule.SELECTOR_FILENAME
        path.write_text('{"unknown":true}')
        with self.assertRaises(FbsSnapshotPolicyError): self.read()
        select(self.runtime)
        self.conn.execute("UPDATE finance_operational_schema_meta SET generation_id='old'"); self.conn.commit()
        with self.assertRaisesRegex(FbsSnapshotPolicyError, "generation_mismatch"): self.read()

    def test_manifest_and_profile_races_fail_closed_without_transaction_commands(self):
        select(self.runtime)
        original = schedule.load_selector
        reads = []
        def moved(runtime):
            result = original(runtime); reads.append(result)
            if len(reads) == 1:
                (runtime / schedule.SELECTOR_FILENAME).unlink()
            return result
        with closing(open_current_snapshot_readonly(self.path)) as conn:
            conn.execute("BEGIN")
            def authorize(action, name, *_):
                return sqlite3.SQLITE_DENY if action in {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT} else sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            with patch.object(schedule, "load_selector", side_effect=moved), self.assertRaises(FbsSnapshotPolicyError):
                self.read(connection=conn)
            self.assertTrue(conn.in_transaction)
            conn.set_authorizer(None); conn.rollback()
        select(self.runtime)
        from packages.application import fbs_current_snapshot_policy as policy_module
        original_load = policy_module._load_manifest
        count = []
        def changed(registry):
            result = original_load(registry)
            if registry.runtime_dir == self.runtime:
                count.append(1)
                if len(count) == 2:
                    (self.runtime / MANIFEST_FILENAME).unlink()
            return result
        with patch.object(policy_module, "_load_manifest", changed), self.assertRaises(FbsSnapshotPolicyError): self.read()

    def test_mapping_dense_scope_and_provenance_are_not_relaxed(self):
        select(self.runtime)
        before = self.conn.execute("SELECT rowid,provenance FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_rows").fetchall()
        self.conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_rows SET provenance='unknown'"); self.conn.commit()
        with self.assertRaises(ValueError): self.read(NOW + timedelta(hours=4))
        self.conn.executemany("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_rows SET provenance=? WHERE rowid=?", [(r[1], r[0]) for r in before]); self.conn.commit()
        self.assertTrue(self.read(NOW + timedelta(hours=4))["available"])
        self.conn.execute(f"UPDATE {FACILITIES_TABLE} SET active=0"); self.conn.commit()
        with self.assertRaises(ValueError): self.read(NOW + timedelta(hours=4))
        self.conn.execute(f"UPDATE {FACILITIES_TABLE} SET active=1"); self.conn.commit()
        self.assertTrue(self.read(NOW + timedelta(hours=4))["available"])
        self.conn.execute("DELETE FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_rows WHERE run_id='A' AND nm_id=2"); self.conn.commit()
        with self.assertRaises(ValueError): self.read(NOW + timedelta(hours=4))

    def test_facility_wrapper_uses_actual_policy_and_truthful_label(self):
        self.conn.execute(f"ALTER TABLE {FACILITIES_TABLE} ADD COLUMN code")
        self.conn.execute(f"ALTER TABLE {FACILITIES_TABLE} ADD COLUMN name")
        self.conn.execute(f"CREATE TABLE {FACILITY_PROFILES_TABLE}(facility_id,city)"); self.conn.commit()
        def read(at): return current_official_fbs_facilities(self.path, requested_nm_ids=[1, 2], now=at)["facilities"]
        self.assertIsNone(read(NOW + timedelta(hours=4))[0]["available"])
        select(self.runtime)
        self.assertEqual(read(NOW + timedelta(hours=4))[0]["available"], 3)
        self.assertIn("9 часов", read(NOW + timedelta(hours=9, seconds=1))[0]["source_blocker"])

    def test_book_uses_consumed_source_not_publication_or_latest_generation(self):
        data = snapshot().payload()
        active = {"active": True, "effective_date": data["date"], "presentations": {data["date"]: data},
                  "prepared_at": (BOOK_NOW + timedelta(hours=8)).isoformat()}
        at = BOOK_NOW + timedelta(hours=4)
        self.assertEqual(book.inventory_from_book(active, now=at, runtime_dir=self.runtime).payload()["quality"], "unavailable")
        select(self.runtime)
        self.assertNotEqual(book.inventory_from_book(active, now=at, runtime_dir=self.runtime).payload()["quality"], "unavailable")
        old = deepcopy(active)
        old["presentations"][data["date"]]["quantity_snapshot"]["captured_at"] = (BOOK_NOW - timedelta(hours=6)).isoformat()
        self.assertEqual(book.inventory_from_book(old, now=at, runtime_dir=self.runtime).payload()["quality"], "unavailable")
        # A fresh official generation never substitutes into the old book.
        self.conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_runs SET snapshot_at=?", (at.isoformat(),)); self.conn.commit()
        self.assertEqual(book.inventory_from_book(old, now=at, runtime_dir=self.runtime).payload()["quality"], "unavailable")
        self.assertEqual(book.inventory_from_book(active, now=BOOK_NOW-timedelta(seconds=1), runtime_dir=self.runtime).payload()["quality"], "unavailable")
        self.assertEqual(book.inventory_from_book(active, now=BOOK_NOW+timedelta(days=1), runtime_dir=self.runtime).payload()["quality"], "unavailable")
        (self.runtime / schedule.SELECTOR_FILENAME).write_text('{}')
        self.assertEqual(book.inventory_from_book(active, now=BOOK_NOW, runtime_dir=self.runtime).payload()["quality"], "unavailable")

    def test_bound_window_and_ambiguous_manifests(self):
        select(self.runtime)
        from packages.application.web_vitrina_window_read_context import window_read_context
        self.conn.execute("CREATE TABLE registry_upload_current_state(slot,bundle_version)")
        self.conn.execute("CREATE TABLE sheet_vitrina_v1_ready_snapshots(snapshot_id)"); self.conn.commit()
        with window_read_context(self.path, runtime_dir=self.runtime) as context:
            borrowed = context.borrow(self.path)
            self.assertTrue(resolve_current_snapshot_policy(connection=borrowed).selected)
            self.assertTrue(resolve_current_snapshot_policy(runtime_dir=self.runtime).selected)
        # A second valid ancestor manifest selecting the same file is ambiguous.
        manifest = build_manifest(state="cutover", canonical_source="split", generation_epoch="epoch1",
            raw_generation_id="raw1", raw_relative_path="other-raw.sqlite3", raw_watermark="raw-proof",
            operational_generation_id="op1", operational_relative_path=str(self.path.relative_to(self.root)),
            operational_watermark="op-proof", rollback_generation_id="legacy", source_fingerprint="source-proof")
        atomic_write_manifest(self.root / MANIFEST_FILENAME, manifest)
        with self.assertRaisesRegex(FbsSnapshotPolicyError, "ambiguous_runtime"): self.read()

    def test_raw_old_inode_is_unproven_and_trusted_old_inode_is_rejected(self):
        select(self.runtime)
        with closing(open_current_snapshot_readonly(self.path)) as trusted:
            self.assertTrue(resolve_current_snapshot_policy(connection=trusted).selected)
            old = self.path.with_name("prior-inode.sqlite3")
            self.path.rename(old); shutil.copyfile(old, self.path)
            # The raw connection opened before rename has no recoverable inode proof.
            self.assertFalse(resolve_current_snapshot_policy(connection=self.conn).selected)
            with self.assertRaisesRegex(ValueError, "not_fresh"):
                self.read(NOW + timedelta(hours=4), connection=self.conn)
            with self.assertRaisesRegex(FbsSnapshotPolicyError, "opened_file_authority_changed"):
                resolve_current_snapshot_policy(connection=trusted)
            # A genuinely new connection binds the replacement inode.
            self.assertTrue(self.read(NOW + timedelta(hours=4))["available"])

    def test_borrowed_context_prior_inode_and_exact_header_are_rejected(self):
        select(self.runtime)
        from packages.application.web_vitrina_window_read_context import window_read_context
        self.conn.execute("CREATE TABLE registry_upload_current_state(slot,bundle_version)")
        self.conn.execute("CREATE TABLE sheet_vitrina_v1_ready_snapshots(snapshot_id)"); self.conn.commit()
        with window_read_context(self.path, runtime_dir=self.runtime) as context:
            borrowed = context.borrow(self.path)
            original = borrowed.ready_header_cache_generation
            borrowed.__dict__["ready_header_cache_generation"] = ("wrong",)
            with self.assertRaisesRegex(FbsSnapshotPolicyError, "borrowed_generation_mismatch"):
                resolve_current_snapshot_policy(connection=borrowed)
            borrowed.__dict__["ready_header_cache_generation"] = original
            self.assertTrue(self.read(NOW + timedelta(hours=4), connection=borrowed)["available"])
            old = self.path.with_name("prior-window.sqlite3")
            self.path.rename(old); shutil.copyfile(old, self.path)
            for kwargs in ({"connection": borrowed}, {"runtime_dir": self.runtime}):
                with self.assertRaisesRegex(FbsSnapshotPolicyError, "opened_file_authority_changed"):
                    resolve_current_snapshot_policy(**kwargs)

    def test_new_connection_replacement_before_first_read_is_refused(self):
        from packages.application import fbs_current_snapshot_policy as policy
        connect = sqlite3.connect
        def replaced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            old = self.path.with_name("replaced-during-open.sqlite3")
            self.path.rename(old); shutil.copyfile(old, self.path)
            return conn
        with patch.object(policy.sqlite3, "connect", side_effect=replaced), self.assertRaisesRegex(FbsSnapshotPolicyError, "during_open"):
            open_current_snapshot_readonly(self.path)

    def test_actual_facilities_factory_replacement_returns_unavailable(self):
        select(self.runtime)
        from packages.application import fbs_current_snapshot_policy as policy
        connect = sqlite3.connect
        opened = []
        def replaced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            opened.append(conn)
            old = self.path.with_name("replaced-facilities.sqlite3")
            self.path.rename(old); shutil.copyfile(old, self.path)
            return conn
        with patch.object(policy.sqlite3, "connect", side_effect=replaced):
            result = current_official_fbs_facilities(self.path, requested_nm_ids=[1, 2], now=NOW)
        self.assertEqual(result["facilities"], [])
        self.assertEqual(result["reason"], "Официальный источник остатков FBS недоступен.")
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")  # Failed factory still closes its own conn.

    def test_structurally_invalid_signed_transition_is_typed_and_book_unavailable(self):
        select(self.runtime)
        path = next((self.runtime / schedule.TRANSITIONS_DIRECTORY).glob("*.json"))
        record = json.loads(path.read_text())
        record["plan"] = []
        path.write_text(json.dumps(signed({k:v for k,v in record.items() if k != "fingerprint"})))
        with self.assertRaises(FbsSnapshotPolicyError): self.read()
        active = {"active": True, "effective_date": "2026-09-07", "presentations": {"2026-09-07": snapshot().payload()}}
        self.assertEqual(book.inventory_from_book(active, now=BOOK_NOW, runtime_dir=self.runtime).payload()["quality"], "unavailable")

    def test_actual_unpinned_capture_estimate_and_publication_connections_grant_nine_hours(self):
        select(self.runtime)
        self.conn.execute("CREATE TABLE sheet_vitrina_v1_nomenclature_items(item_id,nm_id,is_active,is_hidden,updated_at)")
        self.conn.executemany("INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?,?,1,0,'fixture')", [(str(nm),nm) for nm in (1,2)])
        self.conn.commit()
        from packages.application.web_vitrina_official_fbs import build_current_official_fbs_estimate
        from packages.application.fbs_snapshot_cost_sources import capture_current
        from packages.application.ready_publication import readonly
        at = NOW + timedelta(hours=4)
        self.assertTrue(build_current_official_fbs_estimate(self.path, nm_ids=[1, 2], now=at)["available"])
        self.assertTrue(capture_current(self.path, now=at, include_baseline=False)["quantity_snapshot"]["complete"])
        with readonly(self.path) as conn:
            self.assertTrue(self.read(at, connection=conn)["available"])
            self.assertTrue(conn.in_transaction)
        self.assertFalse(resolve_current_snapshot_policy(connection=self.conn).selected)


class HistoryPolicyTests(unittest.TestCase):
    def test_management_binding_and_runtime_wrappers_keep_consumed_revision(self):
        from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
        from apps.ready_publication_smoke import make_book, make_plan
        from packages.application.ready_publication import ensure_publication_schema
        now = datetime(2026, 4, 20, 6, tzinfo=timezone.utc)
        server = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=1, now=now)
        with server:
            runtime = server.entrypoint.runtime
            root = runtime.runtime_dir.resolve()
            with closing(sqlite3.connect(runtime.db_path)) as conn:
                ensure_publication_schema(conn)
                canonical(root, runtime.db_path.resolve(), conn, monolith=True)
            first, _ = make_book(root, opening=True, now=now, typed=True)
            version = book._save_book(root, first, expected=None, operation_id="owned-binding-old")
            plan = make_plan("2026-04-19", "2026-04-20")
            target = {"as_of_date": plan.as_of_date, "bundle_version": "owned-fixture"}
            data = first["presentations"]["2026-04-20"]
            plan.metadata.update(ready_publication_target=target, fbs_accounting_bindings={"2026-04-20": {
                "book_version": version, "presentation_version": data["version_id"], "quality": data["quality"],
                "date": data["date"], "source": book.SOURCE, "effective_date": first["effective_date"], "ready_target": target}})
            at = now + timedelta(hours=4)
            self.assertIsNone(book.load_management_inventory(root, plan, now=at))
            select(root)
            self.assertIsNotNone(book.load_management_inventory(root, plan, now=at))
            self.assertNotEqual(book.load_inventory(root, now=at).payload()["quality"], "unavailable")
            materialized = book.materialize(plan, runtime_dir=root, now=at, book=first, book_version=version, ready_target=target)
            self.assertEqual(materialized.metadata["fbs_accounting_bindings"]["2026-04-20"]["book_version"], version)
            later = now + timedelta(hours=10)
            fresh = deepcopy(first)
            fresh["presentations"]["2026-04-20"]["quantity_snapshot"]["captured_at"] = later.isoformat()
            book._save_book(root, fresh, expected=version, operation_id="owned-binding-new")
            self.assertNotEqual(book.load_inventory(root, now=later).payload()["quality"], "unavailable")
            self.assertIsNone(book.load_management_inventory(root, plan, now=later))
            bad = deepcopy(plan)
            bad.metadata["fbs_accounting_bindings"]["2026-04-20"]["presentation_version"] = "wrong"
            with self.assertRaisesRegex(ValueError, "binding_mismatch"):
                book.load_management_inventory(root, bad, now=at)

    def test_new_consumed_book_ready_changes_current_proof_not_old_archive_epoch(self):
        from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
        from apps.ready_publication_smoke import make_book
        from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter
        from packages.application.ready_publication import ensure_publication_schema
        now = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)
        server = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=206, now=now)
        with server, TemporaryDirectory() as temp:
            runtime = server.entrypoint.runtime
            root = runtime.runtime_dir.resolve()
            with closing(sqlite3.connect(runtime.db_path)) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                ensure_publication_schema(conn)
                canonical(root, runtime.db_path.resolve(), conn, monolith=True)
                # Keep existing WAL sidecars open for the genuine native RO bridge.
                keeper = sqlite3.connect(runtime.db_path)
                self.addCleanup(keeper.close)
                keeper.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            first, _ = make_book(root, opening=True, now=now, typed=True)
            version = book._save_book(root, first, expected=None, operation_id="owned-old-book")
            def bind(value, operation):
                with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
                    bundle, text = conn.execute("SELECT bundle_version,plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-04-20'").fetchone()
                    plan = json.loads(text)
                    plan.setdefault("metadata", {})["fbs_accounting_bindings"] = {"2026-04-20": {"book_version": value}}
                    conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date='2026-04-20'", (json.dumps(plan),))
                    conn.execute("""INSERT INTO sheet_vitrina_v1_ready_publications
                        (operation_id,attempt_id,kind,bundle_version,as_of_date,expected_digest,inputs_json,
                         book_required,ready_required,state,book_version,after_digest,created_at,finished_at)
                        VALUES(?,'attempt','book_ready',?,'2026-04-20','expected','{}',1,1,'complete',?,'after','created','finished')""", (operation, bundle, value))
            bind(version, "old-ready")
            start = (now.date() - timedelta(days=205)).isoformat()
            adapter = LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=root, cache_dir=Path(temp)/"cache",
                now=now, date_from=start, date_to="2026-04-20", formula_epoch="owned-unchanged-formula")
            before = adapter.capture()
            select(root)
            # A selector alone is deliberately not a native global formula/policy
            # epoch. Activation must consume a new generation/book/ready first.
            self.assertEqual(adapter.capture(), before)
            after_book = deepcopy(first)
            source = after_book["presentations"]["2026-04-20"]["quantity_snapshot"]
            source.update(id="owned-new-official-generation", digest="owned-new-digest",
                          captured_at="2026-04-20T12:05:00Z")
            new_version = book._save_book(root, after_book, expected=version, operation_id="owned-new-book")
            bind(new_version, "new-ready")
            after = adapter.capture()
            self.assertEqual(before["epoch"], after["epoch"])
            self.assertNotEqual(before["dates"]["2026-04-20"], after["dates"]["2026-04-20"])
            old = [day for day in before["dates"] if day < "2026-04-19"]
            self.assertEqual(len(old), 204)
            self.assertTrue(all(before["dates"][day] == after["dates"][day] for day in old))


if __name__ == "__main__":
    unittest.main()
