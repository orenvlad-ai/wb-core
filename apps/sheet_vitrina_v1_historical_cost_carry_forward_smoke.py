#!/usr/bin/env python3
"""Smoke coverage for the exact presentation-only historical cost adapter."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_historical_cost_carry_forward import (
    ALLOWED_TOTAL_KEYS,
    OWNER_FIXED_SELECTION_METHOD,
    OWNER_FIXED_NM_ID,
    OWNER_FIXED_UNIT_COST_RUB,
    SKU_FORMULA_KEYS,
    HistoricalCostCarryForwardError,
    _build_plan,
    _readback,
    _submit_once,
    run,
)
from apps.warehouse_fbs_material_rematerialization_smoke import (
    DAY,
    FACILITY_ID,
    NON_TARGET_NM_ID,
    TARGET_NM_ID,
    _payload_rows,
    _seed,
)
from packages.application.ff_pool_foundation import LINES_TABLE, OPERATIONS_TABLE


SOURCE_DAY = "2026-08-25"
OWNER_AUTHORIZATION_DIGEST = "sha256:" + "a" * 64
OWNER_APPROVAL_REFERENCE = "smoke:owner-authorized:" + OWNER_AUTHORIZATION_DIGEST


def main() -> None:
    _exercise_success_and_one_submit()
    _exercise_receipt_blocks_before_submit()
    print("sheet_vitrina_v1_historical_cost_carry_forward_smoke: OK")


def _exercise_success_and_one_submit() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        runtime = _seed(root, mixed=False)
        _remap_target_nm_id(runtime.db_path)
        _prepare_prior_source_and_blank_target(
            runtime.db_path, target_nm_id=OWNER_FIXED_NM_ID
        )
        _seed_blocking_physical_history(
            runtime.db_path, target_nm_id=OWNER_FIXED_NM_ID
        )
        protected_before = _protected_digest(runtime.db_path)
        database_before = _file_sha(runtime.db_path)
        result = run(
            runtime_dir=runtime.runtime_dir,
            evidence_dir=root / "private-evidence",
            operation_id="smoke-historical-analytical-cost",
            business_date=DAY,
            nm_id=OWNER_FIXED_NM_ID,
            apply=False,
            created_at="2026-08-28T12:00:00Z",
            owner_fixed_unit_cost_rub=format(OWNER_FIXED_UNIT_COST_RUB, "f"),
            owner_authorization_digest=OWNER_AUTHORIZATION_DIGEST,
        )
        assert result["status"] == "ready"
        assert result["database_written"] is False
        assert result["source_business_date"] == DAY
        assert result["source_unit_cost_rub"] == "117.537167"
        assert result["selection_method"] == OWNER_FIXED_SELECTION_METHOD
        assert result["owner_authorization_digest"] == OWNER_AUTHORIZATION_DIGEST
        assert result["physical_history_consulted"] is False
        assert _file_sha(runtime.db_path) == database_before
        assert _protected_digest(runtime.db_path) == protected_before

        plan = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        assert plan["cost_event_evidence"]["physical_history_consulted"] is False
        assert plan["before"]["non_target_digest"] == plan["after"]["non_target_digest"]
        assert all(
            value not in {None, ""}
            for value in plan["after"]["metrics"]["after_required_values"]
        )
        submitted = _submit_once(
            db_path=runtime.db_path,
            plan=plan,
            manifest_sha256=result["manifest_sha256"],
            deployed_sha="1" * 40,
            approval_reference=OWNER_APPROVAL_REFERENCE,
            backup={
                "path": str(root / "backup.sqlite3"),
                "sha256": "2" * 64,
                "size_bytes": 1,
                "integrity_check": "ok",
            },
        )
        assert submitted["submit_count"] == 1
        reconciled = _readback(
            db_path=runtime.db_path,
            operation_id="smoke-historical-analytical-cost",
            expected_plan=plan,
        )
        assert reconciled["status"] == "reconciled"
        assert reconciled["submit_count"] == 1
        assert reconciled["source_business_date"] == DAY
        assert reconciled["source_unit_cost_rub"] == "117.537167"
        assert reconciled["owner_authorization_digest"] == OWNER_AUTHORIZATION_DIGEST
        assert _protected_digest(runtime.db_path) == protected_before
        with sqlite3.connect(runtime.db_path) as conn:
            payload = json.loads(
                conn.execute(
                    "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots "
                    "WHERE as_of_date=? ORDER BY activated_at DESC,refreshed_at DESC LIMIT 1",
                    (DAY,),
                ).fetchone()[0]
            )
            cells = _payload_rows(payload)
            assert cells[f"SKU:{NON_TARGET_NM_ID}|sentinel"][2] == 777
            marker = payload["metadata"]["historical_analytical_cost_carry_forward"]
            assert len(marker) == 1
            accepted = next(iter(marker.values()))
            assert accepted["analytical_only"] is True
            assert accepted["warehouse_truth_reconstructed"] is False
            assert accepted["source_business_date"] == DAY
            assert accepted["selection_method"] == OWNER_FIXED_SELECTION_METHOD
            assert accepted["owner_fixed_unit_cost_rub"] == "117.537167"
            assert accepted["owner_authorization_digest"] == OWNER_AUTHORIZATION_DIGEST
            assert accepted["physical_history_consulted"] is False
        try:
            _submit_once(
                db_path=runtime.db_path,
                plan=plan,
                manifest_sha256=result["manifest_sha256"],
                deployed_sha="1" * 40,
                approval_reference=OWNER_APPROVAL_REFERENCE,
                backup={"path": "unused", "sha256": "0", "size_bytes": 0, "integrity_check": "ok"},
            )
        except HistoricalCostCarryForwardError as exc:
            assert exc.code == "operation_identity_not_fresh"
        else:
            raise AssertionError("terminal operation identity was submitted twice")


def _exercise_receipt_blocks_before_submit() -> None:
    with tempfile.TemporaryDirectory() as raw:
        runtime = _seed(Path(raw), mixed=False)
        _prepare_prior_source_and_blank_target(runtime.db_path)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"""INSERT INTO {OPERATIONS_TABLE}(
                       operation_id,operation_type,source_system,source_type,source_id,
                       source_revision,idempotency_epoch,business_date,posted_at,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    "intervening-receipt",
                    "receipt",
                    "smoke",
                    "receipt",
                    "receipt-1",
                    "revision-1",
                    1,
                    DAY,
                    "2026-08-26T09:00:00Z",
                    "{}",
                ),
            )
            conn.execute(
                f"""INSERT INTO {LINES_TABLE}(
                       operation_id,line_no,facility_id,pool,nm_id,quantity_delta,
                       capital_delta_rub,wac_snapshot_rub,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "intervening-receipt",
                    1,
                    FACILITY_ID,
                    "FBS",
                    TARGET_NM_ID,
                    1,
                    "10",
                    "10",
                    "{}",
                ),
            )
            conn.commit()
        before = _file_sha(runtime.db_path)
        try:
            _build_plan(
                db_path=runtime.db_path,
                operation_id="blocked-receipt",
                business_date=DAY,
                nm_id=TARGET_NM_ID,
                created_at="2026-08-28T12:00:00Z",
                storage_generation={"manifest_sha256": "sha256:smoke"},
            )
        except HistoricalCostCarryForwardError as exc:
            assert exc.code == "cost_changing_event_or_ambiguity"
            assert any(item["reason"] == "intervening_receipt" for item in exc.details["blockers"])
        else:
            raise AssertionError("intervening receipt did not block carry-forward")
        assert _file_sha(runtime.db_path) == before


def _prepare_prior_source_and_blank_target(
    db_path: Path, *, target_nm_id: int = TARGET_NM_ID
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        target = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?",
            (DAY,),
        ).fetchone()
        payload = json.loads(target["plan_json"])
        source_payload = json.loads(
            json.dumps(payload, ensure_ascii=False).replace(DAY, SOURCE_DAY)
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_ready_snapshots(
                   bundle_version,activated_at,as_of_date,snapshot_id,
                   plan_version,refreshed_at,plan_json)
               VALUES(?,?,?,?,?,?,?)""",
            (
                "prior-bundle",
                "2026-08-25T23:00:00Z",
                SOURCE_DAY,
                "prior-ready",
                "v1",
                "2026-08-25T23:00:00Z",
                json.dumps(source_payload, sort_keys=True),
            ),
        )
        source_version = conn.execute(
            "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
        ).fetchone()[0]
        prior_version = "whfv_prior_cost_smoke"
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                   version_id,cutover_id,version_kind,effective_at,
                   business_effective_date,published_at,status,plan_fingerprint,
                   local_source_digest,source_watermarks_json,created_at)
               SELECT ?,cutover_id,version_kind,?, ?, ?,'good',plan_fingerprint || '-prior',
                      local_source_digest || '-prior',source_watermarks_json,?
                 FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?""",
            (
                prior_version,
                "2026-08-25T23:00:00Z",
                SOURCE_DAY,
                "2026-08-25T23:00:00Z",
                "2026-08-25T23:00:00Z",
                source_version,
            ),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                   version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                   cost_covered_quantity,quality,certified,wb_quantity,
                   wb_in_way_to_client,wb_in_way_from_client,provenance_json)
               SELECT ?,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                      cost_covered_quantity,quality,certified,wb_quantity,
                      wb_in_way_to_client,wb_in_way_from_client,provenance_json
                 FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=?""",
            (prior_version, source_version),
        )
        target_payload = deepcopy(payload)
        cells = _payload_rows(target_payload)
        for key in ALLOWED_TOTAL_KEYS:
            cells[f"TOTAL|{key}"][2] = None
        for key in SKU_FORMULA_KEYS:
            cells[f"SKU:{target_nm_id}|{key}"][2] = None
        conn.execute(
            "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? "
            "WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?",
            (
                json.dumps(target_payload, sort_keys=True),
                target["bundle_version"],
                target["as_of_date"],
                target["snapshot_id"],
            ),
        )
        conn.commit()


def _remap_target_nm_id(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        snapshots = conn.execute(
            "SELECT rowid,plan_json FROM sheet_vitrina_v1_ready_snapshots"
        ).fetchall()
        for rowid, plan_json in snapshots:
            conn.execute(
                "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE rowid=?",
                (
                    str(plan_json).replace(
                        f"SKU:{TARGET_NM_ID}|", f"SKU:{OWNER_FIXED_NM_ID}|"
                    ),
                    rowid,
                ),
            )
        conn.commit()


def _seed_blocking_physical_history(
    db_path: Path, *, target_nm_id: int
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                   version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                   cost_covered_quantity,quality,certified,wb_quantity,
                   wb_in_way_to_client,wb_in_way_from_client,provenance_json)
               SELECT balance.version_id,balance.warehouse_key,?,balance.quantity,
                      CASE WHEN version.business_effective_date=?
                                AND balance.warehouse_key='ff' THEN '11'
                           ELSE balance.wac_rub END,
                      CASE WHEN version.business_effective_date=?
                                AND balance.warehouse_key='ff'
                           THEN CAST(balance.quantity * 11 AS TEXT)
                           ELSE balance.capital_rub END,
                      balance.cost_covered_quantity,balance.quality,balance.certified,
                      balance.wb_quantity,balance.wb_in_way_to_client,
                      balance.wb_in_way_from_client,balance.provenance_json
                 FROM sheet_vitrina_v1_warehouse_functional_balances balance
                 JOIN sheet_vitrina_v1_warehouse_functional_versions version
                   ON version.version_id=balance.version_id
                WHERE balance.nm_id=?""",
            (target_nm_id, DAY, DAY, TARGET_NM_ID),
        )
        rows = []
        existing = conn.execute(
            "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events "
            "WHERE nm_id=?",
            (target_nm_id,),
        ).fetchone()[0]
        for index in range(92 - existing):
            event_id = (
                "ffbf_87cea959c9d600da99caa1ab68ef"
                if index == 0
                else f"owner-fixed-blocking-event-{index:02d}"
            )
            event_type = "handoff_debit" if index == 0 else "reserve"
            delta = -1 if index == 0 else 1
            capital = "-9" if index == 0 else "117.537167"
            frozen = "9" if index == 0 else "117.537167"
            rows.append(
                (
                    event_id,
                    "warehouse_functional_cutover_v1",
                    20_000 + index,
                    event_type,
                    f"owner-fixed-revision-{index}",
                    f"owner-fixed-status-{index}",
                    FACILITY_ID,
                    target_nm_id,
                    delta,
                    capital,
                    frozen,
                    f"owner-fixed-evidence-{index}",
                )
            )
        conn.executemany(
            """INSERT INTO sheet_vitrina_v1_ff_pool_fbs_lifecycle_events(
                   event_id,cutover_id,order_id,episode_sequence,event_type,
                   source_order_observation_sequence,
                   source_status_observation_sequence,source_revision,
                   status_digest,supplier_status,wb_status,source_observed_at,
                   facility_id,pool,nm_id,quantity,physical_quantity_delta,
                   capital_delta_rub,frozen_wac_rub,evidence_digest,occurred_at)
               VALUES(?,?,?,1,?,1,1,?,?,'complete','sorted',
                      '2026-08-26T12:02:00Z',?,'FBS',?,1,?,?,?,?,
                      '2026-08-26T12:02:00Z')""",
            rows,
        )
        observed = conn.execute(
            "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events "
            "WHERE nm_id=?",
            (target_nm_id,),
        ).fetchone()[0]
        assert observed == 92
        conn.commit()


def _protected_digest(db_path: Path) -> str:
    protected = (
        "sheet_vitrina_v1_warehouse_functional_versions",
        "sheet_vitrina_v1_warehouse_functional_balances",
        "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events",
        OPERATIONS_TABLE,
        LINES_TABLE,
    )
    with sqlite3.connect(db_path) as conn:
        material = {
            table: conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in protected
        }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    main()
