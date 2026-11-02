#!/usr/bin/env python3
"""Query-only historical FBO capture on real, disposable native SQLite rows."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from decimal import Decimal,localcontext
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import fbs_historical_fbo_native_smoke as native_fixture
from apps.fbs_historical_fbo_gap_smoke import cap
from apps.fbs_historical_fbo_dispatch_smoke import seal
from packages.application import fbs_historical_fbo_stage_sources as sources
from packages.application import fbs_historical_fbo_native as native
from packages.application import ready_publication as ready
from packages.application.fbs_snapshot_cost import initialize_candidate, evaluate_candidate, close_candidate_period, fingerprint, canonical
from packages.application.fbs_historical_fbo_dispatch import prepare_historical_fbo_dispatch_plan
from packages.application.fbs_historical_fbo_sources import capture_historical_fbo_documents
from packages.application.shared_sku_cost_sources import capture_wb_component, _digest
from packages.application.fbs_inventory_presentation import capture_retained_stages
from packages.application.registry_upload_db_backed_runtime import _connect
from packages.application.warehouse_functional import ensure_warehouse_functional_schema, _materialize_compact_warehouse_read_models, _stable_id
from packages.application.warehouse_business_projection import ensure_warehouse_business_projection_schema, ensure_functional_version_business_time_schema, publish_functional_version_business_projection
from packages.application.warehouse_archival_estimate import ensure_archival_estimate_schema
from packages.application.wb_initial_fbs_cost import POLICY, QUALITY, digest
from packages.application.fulfillment_services import _ensure_schema as ensure_fee_schema, UPLOADS_TABLE, LINES_TABLE

P = sources.P
STAMP = native_fixture.STAMP


class HistoricalFboStageSources(unittest.TestCase):
    apply = native_fixture.NativeHistoricalFboTests.apply

    def setUp(self):
        native_fixture.NativeHistoricalFboTests.setUp(self)
        state = initialize_candidate(cap("2026-10-15"))
        state["baseline"]["document_cost_state"]["fbo_rows"] = deepcopy(self.capture["opening"]["fbo_rows"])
        state["baseline"]["id"] = fingerprint({k: v for k, v in state["baseline"].items() if k != "id"})
        state = evaluate_candidate(state, cap("2026-10-21"), allow_cost_continuity_gap=True)
        state = close_candidate_period(state, "2026-10-21", today="2026-10-24")
        for day in ("2026-10-22", "2026-10-23", "2026-10-24"):
            state = evaluate_candidate(state, cap(day))
            if day != "2026-10-24":
                state = close_candidate_period(state, day, today="2026-10-24")
        self.book = dict(schema="fbs_active_snapshot_accounting_v1", active=True, state=state,
                         wb_days={}, retained_days={}, shared_days={}, presentations={})
        with closing(_connect(self.runtime.db_path)) as conn:
            ensure_warehouse_functional_schema(conn)
            ensure_warehouse_business_projection_schema(conn)
            ensure_functional_version_business_time_schema(conn)
            ensure_archival_estimate_schema(conn)
            ensure_fee_schema(conn)
            conn.execute(f"INSERT INTO {P}functional_cutovers VALUES(?,?,?,?,?,?,?,?,?)",
                (sources.FUNCTIONAL_CUTOVER_ID, "2026-10-15T12:00:00Z", "posted", fingerprint("cutover"), "{}", "{}", "{}", STAMP, STAMP))
            conn.execute(f"INSERT INTO {P}opening_cost_map VALUES(?,?,?,?,?,?,?,?)",
                (sources.FUNCTIONAL_CUTOVER_ID, 1, "100", "100", "synthetic_basis", "{}", fingerprint("opening"), STAMP))
            self.seed_version(conn, "2026-10-15", "cutover", kind="functional_cutover")
            self.seed_version(conn, "2026-10-16", "anchor", anchor=True)
            for day in state["periods"]:
                self.seed_version(conn, day, "saved:" + day)
            for dispatch in self.scope["dispatches"]:
                sid = dispatch["supply_id"]
                goods = canonical([{"nmID": 1, "quantity": dispatch["expected_quantity"]}])
                conn.execute("INSERT INTO sheet_vitrina_v1_wb_supplies(supply_id,cache_key,wb_supply_id,normalized_row_json,raw_goods_json,raw_goods_hash,synced_at) VALUES(?,?,?,?,?,?,?)",
                    (sid, "supply:" + sid, sid, "{}", goods, fingerprint(goods), STAMP))
                conn.execute("INSERT INTO sheet_vitrina_v1_wb_supply_cost_layers(wb_supply_cost_layer_id,wb_supply_id,nm_id,transit_cost_status,source_status,calculated_at,inputs_hash,version) VALUES(?,?,?,?,?,?,?,?)",
                    ("layer:" + sid, sid, 1, "unknown_route", "pending", STAMP, fingerprint([sid, "pending"]), 1))
                self.seed_fee(conn, sid, "upload:" + sid)
            for event, sid, day in (("foreign", "999", "2026-10-17"), ("selected", "101", "2026-10-20")):
                conn.execute(f"INSERT INTO {P}functional_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (event, "saved:2026-10-21", "wb_final_acceptance", sid, fingerprint(event), day, 1, "1", "100", "{}", STAMP))
            # Already-validated upstream fee authority is represented by one
            # LOCAL immutable source query; this suite tests its fencing only.
            conn.execute("CREATE TABLE local_validated_fee_source(supply TEXT PRIMARY KEY,amount TEXT)")
            conn.executemany("INSERT INTO local_validated_fee_source VALUES(?,?)", [(sid, "1") for sid in ("101", "102", "103")])
            conn.commit()
            conn.execute("PRAGMA query_only=ON"); conn.execute("BEGIN")
            for day in state["periods"]:
                self.book["wb_days"][day] = capture_wb_component(None, day=day, nm_ids=[1], version_id="saved:" + day, connection=conn)
                self.assertTrue(self.book["wb_days"][day]["complete"], self.book["wb_days"][day])
                self.book["retained_days"][day] = capture_retained_stages(None, day=day, wb_version_id="saved:" + day, nm_ids=[1], connection=conn)
            self.fee_scope = dict(contract="historical_fbo_exact_fulfillment_fee_scope_v1",
                requests=[dict(wb_supply_id=sid) for sid in ("101", "102", "103")],
                uploads=[dict(wb_supply_id=sid, upload_id="upload:" + sid, file_sha256=fingerprint(["file", "upload:" + sid]))
                         for sid in ("101", "102", "103")],
                query_fence=json.loads(canonical(ready.pin_queries(conn, [("SELECT * FROM local_validated_fee_source ORDER BY supply", ())]))))
        self.capture["book_version"] = fingerprint(self.book); seal(self.capture)
        self.plan = prepare_historical_fbo_dispatch_plan(self.capture, self.scope)
        native.load.return_value = (deepcopy(self.book), self.capture["book_version"])
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute("BEGIN"); self.guard = native.capture_native_guard(conn, self.plan)
        self.apply()
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute("PRAGMA query_only=ON"); conn.execute("BEGIN")
            self.docs = capture_historical_fbo_documents(conn)
        self.accounting_capture = cap("2026-10-24", self.docs)

    def seed_fee(self, conn, sid, uid):
        conn.execute(f"INSERT INTO {UPLOADS_TABLE}(upload_id,original_filename,stored_file_path,file_sha256,uploaded_at,validation_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (uid, "synthetic.xlsx", "/LOCAL/synthetic.xlsx", fingerprint(["file", uid]), STAMP, "ok", STAMP, STAMP))
        conn.execute(f"INSERT INTO {LINES_TABLE}(upload_id,row_index,matched_wb_supply_id,matched_wb_cache_key,match_status,amount_with_vat,raw_row_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (uid, 1, sid, "supply:" + sid, "ok", 1, "{}", STAMP))

    def seed_version(self, conn, day, vid, *, kind="hourly_wb_sync", anchor=False, hour="12"):
        stamp = day + "T" + hour + ":00:00Z"
        raw = [dict(nmId=1, warehouseId=7, stockCount=50, inWayToClient=0, inWayFromClient=0)]
        items = [dict(nm_id=1, quantity="50", in_way_to_client="0", in_way_from_client="0", wb_contour_quantity="50")]
        source_id = "source:" + vid
        watermark = dict(snapshot_id=source_id, digest=_digest(raw), fetched_at=stamp,
                         pagination_complete=True, raw_row_count=1, requested_count=1)
        conn.execute(f"INSERT INTO {P}functional_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (vid, sources.FUNCTIONAL_CUTOVER_ID, kind, stamp, "good", fingerprint(vid), fingerprint([vid, "local"]),
             canonical(dict(wb_snapshot=watermark, captured_at=stamp)), stamp, day, stamp))
        snapshot_id = _stable_id("wbsnapv", dict(source_snapshot_id=source_id, version_id=vid))
        conn.execute(f"INSERT INTO {P}wb_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, vid, stamp, day, "[1]", 1, 1, "[0]", 1, _digest(raw), canonical(raw), canonical(items), stamp))
        provenance = dict(source_records=[dict(source="official_wb_snapshot", snapshot_id=source_id, snapshot_date=day, fetched_at=stamp)])
        if anchor:
            proof = dict(policy=POLICY, quality=QUALITY, proves_physical_movement=False,
                         nm_id=1, business_date=day, snapshot_id=source_id, wac_rub="100")
            proof["proof_digest"] = digest(proof); provenance["initial_valuation_anchor"] = proof
        line = dict(warehouse_key="wb", nm_id=1, quantity="50", wac_rub="100", capital_rub="5000",
            cost_covered_quantity="50", quality="synthetic_saved_native", certified=0, wb_quantity="50",
            wb_in_way_to_client="0", wb_in_way_from_client="0", provenance=provenance)
        conn.execute(f"INSERT INTO {P}functional_balances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, "wb", 1, "50", "100", "5000", "50", line["quality"], 0, "50", "0", "0", canonical(provenance)))
        _materialize_compact_warehouse_read_models(conn, version_id=vid, plan=dict(lines=[line]), created_at=stamp,
                                                  effective_at=stamp, business_effective_date=day)
        publish_functional_version_business_projection(conn, published_version_id=vid, business_effective_date=day,
                                                       published_at=stamp, source_revision=fingerprint([vid, "publication"]))

    def read(self, **changes):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute("PRAGMA query_only=ON"); conn.execute("BEGIN")
            args = dict(book=self.book, native_plan=self.plan, accounting_capture=self.accounting_capture, fee_scope=self.fee_scope)
            args.update(changes)
            return sources.capture_historical_fbo_stages(conn, **args)

    def test_initial_native_balance_uses_native_precision_for_long_documentary_anchor(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            row=conn.execute(f"SELECT provenance_json FROM {P}functional_balances WHERE version_id='anchor'").fetchone()
            proof=json.loads(row[0]);anchor=proof['initial_valuation_anchor'];anchor['wac_rub']='100.12345678901234567890123456789012345678'
            anchor['proof_digest']=digest({k:v for k,v in anchor.items() if k!='proof_digest'})
            with localcontext() as context:
                context.prec=28
                capital=Decimal(50)*Decimal(anchor['wac_rub']);wac=capital/50
            conn.execute(f"UPDATE {P}functional_balances SET wac_rub=?,capital_rub=?,provenance_json=? WHERE version_id='anchor'",(str(wac),str(capital),canonical(proof)))
            conn.commit()
        result=self.read();self.assertEqual(result['wb_replay']['initial_anchors'][0]['balance']['capital_rub'],str(capital))

    def test_future_acceptance_is_preserved_but_not_replayed(self):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute(f"INSERT INTO {P}functional_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("future", "saved:2026-10-21", "wb_final_acceptance", "future-source", fingerprint("future"), "2026-11-10", 1, "1", "100", "{}", STAMP))
        result=self.read()
        self.assertNotIn("future",{e['event_id'] for e in result['acceptance_events']})
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            self.assertEqual(conn.execute(f"SELECT count(*) FROM {P}functional_events WHERE event_id='future'").fetchone()[0],1)

    def test_snapshot_source_id_binding_cannot_be_forged(self):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute(f"UPDATE {P}wb_snapshots SET snapshot_id='wrong-source' WHERE snapshot_date='2026-10-15'")
        with self.assertRaisesRegex(ValueError,'snapshot_raw_mapping_or_watermark_invalid'):
            self.read()

    def test_exact_saved_stages_pending_layers_foreign_events_and_missing_dates(self):
        result = self.read()
        self.assertEqual(set(result["dates"]), set(self.book["state"]["periods"]))
        self.assertEqual({e["source_id"] for e in result["acceptance_events"]}, {"101", "999"})
        self.assertEqual(len(result["cost_layers"]), 3)
        self.assertTrue(all(r["sku_ff_unit_cost_rub"] is None and r["source_status"] == "pending" for r in result["cost_layers"]))
        self.assertEqual(set(result["supplies"]), {"101", "102", "103"})
        self.assertEqual(set(result["fee_sources"]), {"upload:101", "upload:102", "upload:103"})
        self.assertTrue(all(len(v["read_models"]) == 6 and "provenance" in v["balances"][0] for v in result["dates"].values()))
        self.assertNotIn("2026-10-19", result["wb_replay"]["snapshots_by_day"])
        self.assertNotIn("2026-10-20", result["wb_replay"]["snapshots_by_day"])
        self.assertEqual(result["wb_replay"]["initial_anchors"][0]["version"]["version_id"], "anchor")
        self.assertEqual(result["source_digest"], fingerprint({k: v for k, v in result.items() if k != "source_digest"}))
        self.assertEqual(self.read(), result)

    def test_capture_is_query_only_and_does_not_change_native_rows(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            before = conn.total_changes
            with self.assertRaisesRegex(ValueError, "pinned_query_only_required"):
                sources.capture_historical_fbo_stages(conn, book=self.book, native_plan=self.plan,
                    accounting_capture=self.accounting_capture, fee_scope=self.fee_scope)
            conn.execute("PRAGMA query_only=ON"); conn.execute("BEGIN"); trace = []; conn.set_trace_callback(trace.append)
            sources.capture_historical_fbo_stages(conn, book=self.book, native_plan=self.plan,
                accounting_capture=self.accounting_capture, fee_scope=self.fee_scope)
            self.assertEqual(conn.total_changes, before)
            self.assertFalse(any(s.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ", "COMMIT")) for s in trace))

    def test_native_alias_drift_is_not_stitched(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_wb_supplies SET cache_key='alias' WHERE supply_id='101'"); conn.commit()
        with self.assertRaisesRegex(ValueError, "supply_alias_or_identity_changed"): self.read()

    def test_fee_and_native_source_fences_detect_late_changes(self):
        captured = self.read()
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute("UPDATE local_validated_fee_source SET amount='2' WHERE supply='101'"); conn.commit()
            with self.assertRaisesRegex(ready.ReadyPublicationConflict, "ready_repair_source_changed"):
                sources.check_historical_fbo_stage_queries(conn, captured)
        with self.assertRaisesRegex(ValueError, "fee_source_fence_changed"): self.read()

    def test_foreign_acceptance_insertion_changes_negative_evidence_fence(self):
        captured = self.read()
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute(f"INSERT INTO {P}functional_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("foreign-new", "saved:2026-10-21", "wb_final_acceptance", "888", fingerprint("new"), "2026-10-18", 1, "1", "100", "{}", STAMP)); conn.commit()
            with self.assertRaises(ready.ReadyPublicationConflict): sources.check_historical_fbo_stage_queries(conn, captured)
        self.assertEqual(len(self.read()["acceptance_events"]), 3)

    def test_existing_other_fee_for_selected_supply_is_preserved_and_fenced(self):
        captured = self.read()
        with closing(_connect(self.runtime.db_path)) as conn:
            self.seed_fee(conn, "101", "prior:101"); conn.commit()
            with self.assertRaises(ready.ReadyPublicationConflict): sources.check_historical_fbo_stage_queries(conn, captured)
        self.assertIn("prior:101", self.read()["fee_sources"])

    def test_pinned_book_version_wins_over_newer_same_day_actual_snapshot(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            self.seed_version(conn, "2026-10-21", "newer:21", hour="20"); conn.commit()
        result = self.read()
        self.assertEqual(result["dates"]["2026-10-21"]["version"]["version_id"], "saved:2026-10-21")
        self.assertEqual(result["wb_replay"]["snapshots_by_day"]["2026-10-21"]["version"]["version_id"], "saved:2026-10-21")

    def test_corrupt_actual_prefix_quantity_is_refused(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute(f"UPDATE {P}wb_snapshots SET raw_rows_digest='tampered' WHERE version_id='anchor'"); conn.commit()
        with self.assertRaisesRegex(ValueError, "snapshot_quantity_authority_invalid"): self.read()

    def test_limits_refuse_partial_capture(self):
        with patch.object(sources, "MAX_ROWS", 1):
            with self.assertRaisesRegex(ValueError, "row_limit"): self.read()
        with patch.object(sources, "MAX_BYTES", 256):
            with self.assertRaisesRegex(ValueError, "source_byte_limit"): self.read()

    def test_empty_fee_authority_or_absent_saved_stage_is_not_zero(self):
        fees = deepcopy(self.fee_scope); fees["query_fence"] = []
        with self.assertRaisesRegex(ValueError, "fee_scope_changed"): self.read(fee_scope=fees)
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute(f"DELETE FROM {P}functional_read_models WHERE version_id='saved:2026-10-21' AND warehouse_key='ff_to_wb'"); conn.commit()
        with self.assertRaisesRegex(ValueError, "six_stage_models_required"): self.read()

    def test_earliest_anchor_drift_cannot_fall_back_to_later_good_value(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute(f"UPDATE {P}functional_balances SET capital_rub='1' WHERE version_id='anchor' AND warehouse_key='wb'"); conn.commit()
        with self.assertRaisesRegex(ValueError, "initial_valuation_anchor_invalid"): self.read()

    def test_existing_archival_overlay_is_preserved_with_its_native_source(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute(f"INSERT INTO {P}archival_estimate_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("estimate", "2026-10-15", "123", "business_approved_archival_estimate", "LOCAL owner approval",
                 fingerprint("estimate"), "a" * 64, fingerprint("source"), fingerprint("plan"), None, "{}", "[]", "[]", STAMP))
            conn.execute(f"INSERT INTO {P}archival_estimate_rows VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("estimate", 1, "synthetic", "synthetic", "123", "business_approved_archival_estimate", "100", "old", "{}", fingerprint("row")))
            conn.execute(f"INSERT INTO {P}archival_estimate_active VALUES(1,?,?)", ("estimate", STAMP)); conn.commit()
        result = self.read()
        self.assertEqual(result["wb_replay"]["opening_cost_rows"][0]["wb_unit_cost_rub"], "100")
        overlay = result["wb_replay"]["opening_cost_map"][0]
        self.assertEqual(overlay["wb_unit_cost_rub"], "123")
        self.assertEqual(overlay["quality"], "business_approved_archival_estimate")
        self.assertEqual(overlay["ff_unit_cost_rub"], "100")

    def test_daily_cost_capture_is_selected_scope_and_fences_new_target_row(self):
        captured = self.read()
        with closing(_connect(self.runtime.db_path)) as conn:
            for day, nm in (("2026-10-18", 1), ("2026-10-19", 1), ("2026-10-20", 2)):
                conn.execute(f"INSERT INTO {P}wb_daily_cost VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sources.FUNCTIONAL_CUTOVER_ID, day, nm, "50", "100", "5000", "provisional", "{}", fingerprint([day, nm]), STAMP))
            conn.commit()
            with self.assertRaises(ready.ReadyPublicationConflict): sources.check_historical_fbo_stage_queries(conn, captured)
        self.assertEqual([(r["as_of_date"], r["nm_id"]) for r in self.read()["wb_replay"]["daily_cost_rows"]], [("2026-10-19", 1)])


if __name__ == "__main__":
    unittest.main()
