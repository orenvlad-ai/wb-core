#!/usr/bin/env python3
"""Regression smoke for the exact 18-SKU archival estimate recovery."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.canonical_wb_cost_resolver import (  # noqa: E402
    CANONICAL_COST_FORMULA_VERSION,
    CanonicalWbCostSnapshot,
    resolve_finance_canonical_cost,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_archival_estimate import (  # noqa: E402
    QUALITY,
    apply_archival_estimate_plan,
    build_archival_estimate_plan,
    load_archival_estimate_manifest,
    readback_archival_estimate,
    rollback_archival_estimate,
)
from packages.application.warehouse_functional import (  # noqa: E402
    WarehouseFunctionalBlock,
    ensure_warehouse_functional_schema,
    moving_weighted_average,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)


def main() -> None:
    with TemporaryDirectory(prefix="warehouse-archival-estimate-") as tmp:
        root = Path(tmp)
        runtime_dir = root / "runtime"
        runtime_dir.mkdir()
        backups = root / "backups"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir)
        manifest = load_archival_estimate_manifest()
        _seed(runtime.db_path, manifest)

        before = _business_state(runtime.db_path, manifest)
        plan = build_archival_estimate_plan(runtime)
        after_dry_run = _business_state(runtime.db_path, manifest)
        if before != after_dry_run:
            raise AssertionError("dry-run changed database state")
        _assert_plan(plan, manifest)
        _assert_factual_daily_layer_blocks_plan(runtime, manifest)
        _assert_duplicate_nomenclature_blocks_plan(runtime, manifest)

        with _connection(runtime.db_path) as conn:
            old = resolve_finance_canonical_cost(
                conn,
                nm_id=str(manifest["targets"][0]["nm_id"]),
                operation_date=date(2026, 1, 5),
            )
        if old["status"] != "missing" or old["reason"] != "canonical_cost_exact_date_missing":
            raise AssertionError(f"missing exact 01.07 basis was not rejected: {old}")

        applied = apply_archival_estimate_plan(
            runtime,
            plan,
            confirm_fingerprint=plan["plan_fingerprint"],
            approval_reference="fixture owner approval",
            backup_dir=backups.resolve(),
        )
        if (
            applied["status"] != "applied"
            or applied["target_count"] != 18
            or not applied["invariants_ok"]
            or applied["recovery_policy"]["tier"] != "T1"
            or applied["recovery_policy"]["lifecycle"] != "retained"
            or applied["recovery_policy"]["actual_bytes"] <= 0
            or applied["recovery_policy"]["read_bytes"] < 0
            or applied["recovery_policy"]["artifacts"][0]["artifact_kind"] != "undo"
        ):
            raise AssertionError(f"apply/readback mismatch: {applied}")
        readback = readback_archival_estimate(runtime)
        if readback["target_nm_ids"] != sorted(int(item["nm_id"]) for item in manifest["targets"]):
            raise AssertionError("exact target manifest was not preserved")
        _assert_functional_cutover_rollback_requires_archival_deactivation(
            runtime=runtime,
            backup_dir=root / "functional-rollback-backups",
        )

        with _connection(runtime.db_path) as conn:
            snapshot = CanonicalWbCostSnapshot.from_connection(conn)
            all_nm_ids = (
                [int(item["nm_id"]) for item in manifest["targets"]]
                + [int(item) for item in manifest["active_vitrina_nm_ids"]]
            )
            for nm_id in all_nm_ids:
                for operation_day in (
                    date(2026, 1, 5),
                    date(2026, 7, 1),
                    date(2026, 7, 19),
                ):
                    direct = resolve_finance_canonical_cost(
                        conn,
                        nm_id=str(nm_id),
                        operation_date=operation_day,
                    )
                    cached = resolve_finance_canonical_cost(
                        conn,
                        nm_id=str(nm_id),
                        operation_date=operation_day,
                        snapshot=snapshot,
                    )
                    if direct != cached:
                        raise AssertionError(
                            "snapshot-bound canonical resolution changed semantics: "
                            f"{nm_id} {operation_day}: {direct} != {cached}"
                        )
            snapshot_queries: list[str] = []
            conn.set_trace_callback(snapshot_queries.append)
            resolved_51 = [
                resolve_finance_canonical_cost(
                    conn,
                    nm_id=str(nm_id),
                    operation_date=date(2026, 7, 1),
                    snapshot=snapshot,
                )
                for nm_id in all_nm_ids
            ]
            if len(resolved_51) != 51 or any(
                item["status"] != "resolved" for item in resolved_51
            ):
                raise AssertionError(f"51-SKU Finance coverage is incomplete: {resolved_51}")
            for operation_day in (
                date(2026, 1, 5),
                date(2026, 7, 1),
                date(2026, 7, 15),
                date(2026, 7, 19),
            ):
                resolved = resolve_finance_canonical_cost(
                    conn,
                    nm_id=str(manifest["targets"][0]["nm_id"]),
                    operation_date=operation_day,
                    snapshot=snapshot,
                )
                if (
                    resolved["status"] != "resolved"
                    or resolved["unit_cost_rub"] not in {"100.00", "100"}
                    or resolved["quality"] != QUALITY
                    or not resolved["source_digest"].startswith("sha256:")
                    or not resolved["canonical_source_identity"]
                ):
                    raise AssertionError(f"approved estimate resolution failed: {resolved}")
            conn.set_trace_callback(None)
            if snapshot_queries:
                raise AssertionError(
                    "snapshot-bound canonical resolution issued repeated SQL: "
                    f"{snapshot_queries[:3]}"
                )
            traced_sql: list[str] = []
            conn.set_trace_callback(traced_sql.append)
            active = resolve_finance_canonical_cost(
                conn,
                nm_id=str(manifest["active_vitrina_nm_ids"][0]),
                operation_date=date(2026, 7, 1),
            )
            conn.set_trace_callback(None)
            if active["unit_cost_rub"] != "77" or active["quality"] != "certified":
                raise AssertionError(f"active SKU cost changed: {active}")
            if any(
                "FROM sheet_vitrina_v1_warehouse_functional_events" in statement
                for statement in traced_sql
            ):
                raise AssertionError("non-target canonical lookup scanned archival factual events")
        if CANONICAL_COST_FORMULA_VERSION != "canonical_our_wb_cost_temporal_policy_v4":
            raise AssertionError("Finance formula/source digest version was not bumped")

        repeated = apply_archival_estimate_plan(
            runtime,
            plan,
            confirm_fingerprint=plan["plan_fingerprint"],
            approval_reference="fixture owner approval",
            backup_dir=backups.resolve(),
        )
        if (
            repeated["status"] != "no_op_already_applied"
            or not repeated["idempotent"]
            or repeated["backup"] is not None
        ):
            raise AssertionError(f"repeat apply was not a no-op: {repeated}")

        fresh_no_op = build_archival_estimate_plan(runtime)
        if fresh_no_op["status"] != "no_op" or fresh_no_op["apply_allowed"]:
            raise AssertionError(f"fresh no-op plan remained applicable: {fresh_no_op}")
        with warehouse_functional_write_lock(runtime.runtime_dir):
            fresh_repeated = apply_archival_estimate_plan(
                runtime,
                fresh_no_op,
                confirm_fingerprint=fresh_no_op["plan_fingerprint"],
                approval_reference="fixture owner approval",
                backup_dir=backups.resolve(),
            )
        if (
            fresh_repeated["status"] != "no_op_already_active"
            or not fresh_repeated["idempotent"]
            or fresh_repeated["database_written"]
            or fresh_repeated["backup"] is not None
        ):
            raise AssertionError(f"fresh no-op plan was not inert: {fresh_repeated}")
        _assert_cli_serializes_with_hourly_lock(
            root=root,
            runtime=runtime,
            plan=fresh_no_op,
            backups=backups,
        )
        with _connection(runtime.db_path) as conn:
            version_count = conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_archival_estimate_versions"
            ).fetchone()[0]
        if version_count != 1:
            raise AssertionError(f"no-op plans created duplicate versions: {version_count}")

        quantity, capital, wac = moving_weighted_average(
            quantity="10",
            capital="1000",
            inbound_quantity="10",
            inbound_capital="2000",
        )
        if (quantity, capital, wac) != (20, 3000, 150):
            raise AssertionError("factual future receipt did not roll ordinary WAC")

        target_nm_id = int(manifest["targets"][0]["nm_id"])
        with _connection(runtime.db_path) as conn:
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_events(
                       event_id,version_id,event_type,source_id,source_fingerprint,
                       business_date,nm_id,quantity,capital_rub,provenance_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "future-factual-fixture",
                    "future-version",
                    "wb_final_acceptance",
                    "future-supply",
                    "sha256:future",
                    "2026-07-19",
                    target_nm_id,
                    "10",
                    "2000",
                    "{}",
                    "2026-07-20T00:00:00Z",
                ),
            )
            conn.commit()
            after_factual = resolve_finance_canonical_cost(
                conn,
                nm_id=str(target_nm_id),
                operation_date=date(2026, 7, 19),
            )
            if after_factual["status"] != "missing":
                raise AssertionError(
                    "archival estimate survived a factual layer without an exact daily replay"
                )
            pending_readback = readback_archival_estimate(runtime)
            if (
                pending_readback["status"] != "blocked"
                or pending_readback["invariants_ok"]
                or not pending_readback["pending_factual_replay_rows"]
            ):
                raise AssertionError(
                    f"stale estimate was reported ready before factual replay: {pending_readback}"
                )
            try:
                apply_archival_estimate_plan(
                    runtime,
                    plan,
                    confirm_fingerprint=plan["plan_fingerprint"],
                    approval_reference="fixture owner approval",
                    backup_dir=backups.resolve(),
                )
            except Exception as exc:
                if "readback is blocked" not in str(exc):
                    raise AssertionError(
                        f"unexpected blocked idempotent retry error: {exc}"
                    ) from exc
            else:
                raise AssertionError("idempotent retry masked a blocked archival readback")
            conn.execute(
                "DELETE FROM sheet_vitrina_v1_warehouse_functional_events WHERE event_id=?",
                ("future-factual-fixture",),
            )
            conn.commit()

        rolled_back = rollback_archival_estimate(
            runtime,
            plan_fingerprint=plan["plan_fingerprint"],
            reason="fixture rollback",
            backup_dir=backups.resolve(),
        )
        if rolled_back["status"] != "rolled_back" or rolled_back["restored_daily_row_count"] != 18:
            raise AssertionError(f"rollback failed: {rolled_back}")
        with _connection(runtime.db_path) as conn:
            restored = resolve_finance_canonical_cost(
                conn,
                nm_id=str(manifest["targets"][0]["nm_id"]),
                operation_date=date(2026, 1, 5),
            )
        if restored["status"] != "missing":
            raise AssertionError("rollback did not restore forbidden fallback behavior")
        rolled_back_plan = build_archival_estimate_plan(runtime)
        if (
            rolled_back_plan["status"] != "blocked"
            or rolled_back_plan["apply_allowed"]
            or not any(
                item.get("code") == "plan_fingerprint_previously_rolled_back"
                for item in rolled_back_plan["blockers"]
            )
        ):
            raise AssertionError(
                f"rolled-back fingerprint was advertised as reusable: {rolled_back_plan}"
            )
        try:
            apply_archival_estimate_plan(
                runtime,
                plan,
                confirm_fingerprint=plan["plan_fingerprint"],
                approval_reference="fixture owner approval",
                backup_dir=backups.resolve(),
            )
        except Exception as exc:
            if "drifted after dry-run" not in str(exc):
                raise AssertionError(f"unexpected stale reapply rejection: {exc}") from exc
        else:
            raise AssertionError("a rolled-back archival plan fingerprint was reapplied")

    print(
        "warehouse_archival_estimate: ok -> exact 18/33 disjoint manifest, read-only plan, "
        "verified backup, atomic apply, 100 RUB pre/on/post 01.07 resolution, active costs "
        "unchanged, ordinary future WAC, shared reentrant writer lock, idempotent apply, "
        "factual-replay readback gate, audited non-reusable rollback"
    )


def _seed(path: Path, manifest: dict) -> None:
    with _connection(path) as conn:
        conn.execute(
            """CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                   nm_id INTEGER,vendor_code TEXT,nomenclature_name TEXT,
                   purchase_price_yuan TEXT)"""
        )
        ensure_warehouse_functional_schema(conn)
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers(
                   cutover_id,cutover_at,status,plan_fingerprint,source_watermarks_json,
                   absorbed_supply_revisions_json,backup_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "warehouse_functional_cutover_v1",
                "2026-07-19T00:00:00Z",
                "posted",
                "sha256:cutover",
                "{}",
                "{}",
                "{}",
                "2026-07-19T00:00:00Z",
                "2026-07-19T00:00:00Z",
            ),
        )
        for item in manifest["targets"]:
            nm_id = int(item["nm_id"])
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?,?,?,NULL)",
                (nm_id, item["vendor_code"], item["canonical_nomenclature_name"]),
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_opening_cost_map(
                       cutover_id,nm_id,ff_unit_cost_rub,wb_unit_cost_rub,quality,
                       provenance_json,fingerprint,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "warehouse_functional_cutover_v1",
                    nm_id,
                    "111.181389",
                    manifest["supersedes_unit_cost_rub"],
                    "fallback_average",
                    json.dumps({"missing_purchase_price": True}),
                    f"sha256:opening:{nm_id}",
                    "2026-07-19T00:00:00Z",
                ),
            )
            _insert_daily(
                conn,
                day="2026-07-19",
                nm_id=nm_id,
                quantity="0",
                wac=manifest["supersedes_unit_cost_rub"],
                quality="periodic_snapshot_wac_closed",
            )
        for nm_id in manifest["active_vitrina_nm_ids"]:
            _insert_daily(
                conn,
                day="2026-07-01",
                nm_id=int(nm_id),
                quantity="10",
                wac="77",
                quality="certified",
            )
        conn.commit()


def _insert_daily(
    conn: sqlite3.Connection,
    *,
    day: str,
    nm_id: int,
    quantity: str,
    wac: str,
    quality: str,
) -> None:
    capital = str(int(quantity) * float(wac))
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost(
               cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,quality,
               provenance_json,fingerprint,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "warehouse_functional_cutover_v1",
            day,
            nm_id,
            quantity,
            wac,
            capital,
            quality,
            "{}",
            f"sha256:daily:{day}:{nm_id}",
            f"{day}T00:00:00Z",
        ),
    )


def _assert_plan(plan: dict, manifest: dict) -> None:
    if (
        plan["status"] != "ready"
        or not plan["apply_allowed"]
        or plan["target_count"] != 18
        or plan["active_vitrina_count"] != 33
        or plan["target_active_intersection"]
        or plan["write_set"]["derived_daily_rows"] != 18
        or plan["write_set"]["primary_source_rows"] != 0
        or not all(plan["invariants"].values())
        or not str(plan["plan_fingerprint"]).startswith("sha256:")
    ):
        raise AssertionError(f"unexpected correction plan: {plan}")
    if plan["production_dry_run_plan_sha256"] != (
        "dc4802b590a3540a9357f52a8bf04ae1a7e043573813321a61104f7604cfe6da"
    ):
        raise AssertionError("production dry-run evidence was not pinned")
    if set(plan["target_nm_ids"]) & set(manifest["active_vitrina_nm_ids"]):
        raise AssertionError("18 legacy targets overlap the 33 active SKU")
    proof = plan["nomenclature_identity_proof"]
    if len(proof) != 18 or any(
        not row["matches"]
        or row["expected_vendor_code"] != row["actual_vendor_code"]
        or row["expected_nomenclature_name"] != row["actual_nomenclature_name"]
        for row in proof
    ):
        raise AssertionError(f"canonical nomenclature identity proof failed: {proof}")
    if not any(
        row["descriptive_name"] != row["actual_nomenclature_name"] for row in proof
    ):
        raise AssertionError("fixture did not distinguish description from canonical name")


def _assert_factual_daily_layer_blocks_plan(
    runtime: RegistryUploadDbBackedRuntime,
    manifest: dict,
) -> None:
    nm_id = int(manifest["targets"][0]["nm_id"])
    with _connection(runtime.db_path) as conn:
        original = dict(
            conn.execute(
                """SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                   WHERE nm_id=? ORDER BY as_of_date LIMIT 1""",
                (nm_id,),
            ).fetchone()
        )
        conn.execute(
            """UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost
               SET wac_rub='200',capital_rub='0',quality='periodic_snapshot_wac',
                   provenance_json=? WHERE cutover_id=? AND as_of_date=? AND nm_id=?""",
            (
                json.dumps(
                    {
                        "source": "persisted_historical_daily_quantity",
                        "inbound_quantity": "10",
                        "inbound_supply_ids": ["factual-supply"],
                    }
                ),
                original["cutover_id"],
                original["as_of_date"],
                nm_id,
            ),
        )
        conn.commit()
    blocked = build_archival_estimate_plan(runtime)
    if blocked["status"] != "blocked" or not any(
        item.get("code") == "target_daily_rows_have_factual_cost_basis"
        for item in blocked["blockers"]
    ):
        raise AssertionError(f"factual daily WAC was eligible for overwrite: {blocked}")
    with _connection(runtime.db_path) as conn:
        conn.execute(
            """UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost
               SET quantity=?,wac_rub=?,capital_rub=?,quality=?,provenance_json=?,
                   fingerprint=?,created_at=?
               WHERE cutover_id=? AND as_of_date=? AND nm_id=?""",
            (
                original["quantity"],
                original["wac_rub"],
                original["capital_rub"],
                original["quality"],
                original["provenance_json"],
                original["fingerprint"],
                original["created_at"],
                original["cutover_id"],
                original["as_of_date"],
                nm_id,
            ),
        )
        conn.commit()


def _assert_duplicate_nomenclature_blocks_plan(
    runtime: RegistryUploadDbBackedRuntime,
    manifest: dict,
) -> None:
    target = manifest["targets"][0]
    nm_id = int(target["nm_id"])
    with _connection(runtime.db_path) as conn:
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?,?,?,?)",
            (nm_id, "conflicting", "Conflicting factual row", "42"),
        )
        conn.commit()
    blocked = build_archival_estimate_plan(runtime)
    blocker_codes = {
        item.get("code")
        for item in blocked["blockers"]
        if item.get("nm_id") == nm_id
    }
    if blocked["status"] != "blocked" or not {
        "nomenclature_target_ambiguous",
        "nomenclature_identity_drift",
        "target_now_has_factual_purchase_price",
    }.issubset(blocker_codes):
        raise AssertionError(
            f"duplicate/factual nomenclature evidence was ignored: {blocked}"
        )
    identity_drift = next(
        item
        for item in blocked["blockers"]
        if item.get("code") == "nomenclature_identity_drift"
        and item.get("nm_id") == nm_id
    )
    if (
        identity_drift["expected_vendor_code"] != target["vendor_code"]
        or identity_drift["expected_nomenclature_name"]
        != target["canonical_nomenclature_name"]
        or identity_drift["actual_vendor_code"] != "conflicting"
        or identity_drift["actual_nomenclature_name"] != "Conflicting factual row"
    ):
        raise AssertionError(f"identity drift evidence is incomplete: {identity_drift}")
    with _connection(runtime.db_path) as conn:
        conn.execute(
            """DELETE FROM sheet_vitrina_v1_nomenclature_items
               WHERE nm_id=? AND vendor_code='conflicting'""",
            (nm_id,),
        )
        conn.commit()


def _assert_functional_cutover_rollback_requires_archival_deactivation(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    backup_dir: Path,
) -> None:
    block = object.__new__(WarehouseFunctionalBlock)
    block.runtime = runtime
    block.timestamp_factory = lambda: "2026-07-21T00:00:00Z"
    try:
        block.rollback_functional_cutover(
            confirm_fingerprint="sha256:cutover",
            backup_dir=backup_dir.resolve(),
        )
    except Exception as exc:
        if "archival estimate must be rolled back" not in str(exc):
            raise AssertionError(f"unexpected functional rollback blocker: {exc}") from exc
    else:
        raise AssertionError("functional rollback orphaned an active archival estimate")
    if backup_dir.exists():
        raise AssertionError("blocked functional rollback created a backup")


def _assert_cli_serializes_with_hourly_lock(
    *,
    root: Path,
    runtime: RegistryUploadDbBackedRuntime,
    plan: dict,
    backups: Path,
) -> None:
    plan_path = root / "fresh-no-op-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    command = [
        sys.executable,
        str(ROOT / "apps" / "warehouse_archival_estimate.py"),
        "apply",
        "--runtime-dir",
        str(runtime.runtime_dir),
        "--plan-file",
        str(plan_path),
        "--fingerprint",
        str(plan["plan_fingerprint"]),
        "--approval-reference",
        "fixture owner approval",
        "--backup-dir",
        str(backups.resolve()),
    ]
    with warehouse_functional_write_lock(runtime.runtime_dir):
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.25)
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                "archival CLI bypassed the hourly writer lock: " + stdout + stderr
            )
    stdout, stderr = process.communicate(timeout=10)
    if process.returncode != 0:
        raise AssertionError("serialized archival CLI failed: " + stdout + stderr)
    payload = json.loads(stdout)
    if payload["status"] != "no_op_already_active" or payload["database_written"]:
        raise AssertionError(f"serialized no-op CLI changed state: {payload}")


def _business_state(path: Path, manifest: dict) -> dict:
    with _connection(path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        active = (
            [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_archival_estimate_active"
            )]
            if "sheet_vitrina_v1_warehouse_archival_estimate_active" in tables
            else []
        )
        daily = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost ORDER BY as_of_date,nm_id"
            )
        ]
    return {"active": active, "daily": daily}


def _connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    main()
