#!/usr/bin/env python3
"""Production-safe canonical Finance dry-run/apply regression smoke."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock  # noqa: E402

REVOKED = "sha256:621323d6f03759cb8685dfffe20639fa18a16c7b5f6a5b1685205a579c6bbf2d"


def main() -> None:
    with TemporaryDirectory(prefix="wb-finance-canonical-runner-") as tmp:
        runtime = Path(tmp) / "runtime"
        runtime.mkdir()
        backups = Path(tmp) / "backups"
        block = WbFinanceWeeklyBlock(
            runtime,
            seller_id="seller-1",
            now_factory=lambda: datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        )
        block.ensure_schema()
        _seed_sources(block.db_path)
        _seed_weeks(block)
        _make_stored_results_stale(block.db_path)

        before_hash = _sha256(block.db_path)
        plan = block.plan_canonical_finance_backfill()
        after_hash = _sha256(block.db_path)
        if before_hash != after_hash:
            raise AssertionError("canonical dry-run wrote to the runtime database")
        _assert_plan(plan)
        fingerprint = str(plan["fingerprint"])

        dry = _run_cli(runtime)
        if dry.returncode != 0:
            raise AssertionError(f"CLI dry-run failed: {dry.stderr}\n{dry.stdout}")
        cli_plan = json.loads(dry.stdout)
        if cli_plan["fingerprint"] != fingerprint:
            raise AssertionError("application and CLI dry-run fingerprints diverged")

        _assert_apply_gates(block, runtime, backups, fingerprint)
        applied = _run_cli(
            runtime,
            "--apply",
            "--confirm-fingerprint",
            fingerprint,
            "--backup-dir",
            str(backups),
            "--approval-reference",
            "fixture-human-approval",
        )
        if applied.returncode != 0:
            raise AssertionError(f"approved local apply failed: {applied.stderr}\n{applied.stdout}")
        result = json.loads(applied.stdout)
        _assert_apply_result(block, result, backups)

        repeated = _run_cli(
            runtime,
            "--apply",
            "--confirm-fingerprint",
            fingerprint,
            "--backup-dir",
            str(backups),
            "--approval-reference",
            "fixture-human-approval",
        )
        if repeated.returncode != 0:
            raise AssertionError(f"repeat exact apply failed: {repeated.stderr}\n{repeated.stdout}")
        repeat_result = json.loads(repeated.stdout)
        if (
            repeat_result["status"] != "no_op_already_applied"
            or not repeat_result["idempotent"]
            or repeat_result["backup"] is not None
            or len(list(backups.glob("*.sqlite3"))) != 1
        ):
            raise AssertionError(f"repeat exact apply was not a true no-op: {repeat_result}")
        with sqlite3.connect(block.db_path) as conn:
            conn.execute(
                """UPDATE temporal_source_slot_snapshots SET captured_at='2026-07-20T01:00:00Z'
                   WHERE rowid=(SELECT rowid FROM temporal_source_slot_snapshots LIMIT 1)"""
            )
            conn.commit()
        try:
            block.apply_canonical_finance_backfill(
                expected_fingerprint=fingerprint,
                approval_reference="fixture-human-approval",
            )
        except ValueError as exc:
            if "drifted" not in str(exc):
                raise
        else:
            raise AssertionError("old approval remained reusable after source drift")

    _assert_partial_schema_returns_blocker()

    print(
        "wb_finance_business_approved_backfill: ok -> read-only all-history manifests, "
        "reconciliation deltas, new fingerprint gate, 0600 backup, atomic apply, "
        "non-target invariants, repeat no-op, revoked plan rejection"
    )


def _assert_partial_schema_returns_blocker() -> None:
    with TemporaryDirectory(prefix="wb-finance-partial-schema-") as tmp:
        runtime = Path(tmp)
        db_path = runtime / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """CREATE TABLE wb_finance_weekly_raw_rows(
                   seller_id TEXT,report_id TEXT,rrd_id TEXT,week_start TEXT,week_end TEXT,
                   nm_id TEXT,row_hash TEXT,raw_json TEXT)"""
            )
            row = _row(900, "2026-01-06")
            conn.execute(
                "INSERT INTO wb_finance_weekly_raw_rows VALUES(?,?,?,?,?,?,?,?)",
                (
                    "seller-1",
                    "900",
                    "900",
                    "2026-01-05",
                    "2026-01-11",
                    "101",
                    "sha256:fixture",
                    json.dumps(row, separators=(",", ":")),
                ),
            )
            conn.commit()
        block = WbFinanceWeeklyBlock(runtime, seller_id="seller-1")
        plan = block.plan_canonical_finance_backfill()
        schema_blocker = next(
            (item for item in plan["blockers"] if item["code"] == "required_schema_missing"),
            None,
        )
        if plan["status"] != "blocked" or not schema_blocker or not schema_blocker["tables"]:
            raise AssertionError(f"partial deployment did not produce a precise blocker: {plan}")


def _assert_plan(plan: dict) -> None:
    if (
        plan["status"] != "ready"
        or not plan["dry_run"]
        or not plan["apply_allowed"]
        or plan["week_count"] != 3
        or plan["finance_row_count"] != 5
        or plan["finance_nm_id_count"] != 1
    ):
        raise AssertionError(f"unexpected all-history plan scope: {plan}")
    if plan["date_from"] != "2026-01-05" or plan["date_to"] != "2026-07-19":
        raise AssertionError(f"all loaded history was not selected: {plan}")
    if plan["fingerprint"] == REVOKED or REVOKED not in plan["revoked_fingerprints"]:
        raise AssertionError("revoked Finance plan identity was reused or not recorded")
    if not str(plan["fingerprint"]).startswith("sha256:"):
        raise AssertionError(f"invalid plan fingerprint: {plan['fingerprint']}")
    if plan["source_manifests"]["ads"]["complete"] is not True:
        raise AssertionError(f"valid root ads envelopes were treated as missing: {plan}")
    july_manifest = plan["source_manifests"]["cost"]["canonical_2026_07_01_manifest"]
    if (
        july_manifest["row_count"] != 1
        or july_manifest["resolved_count"] != 1
        or july_manifest["missing_nm_ids"]
        or july_manifest["rows"][0]["nm_id"] != "101"
    ):
        raise AssertionError(f"canonical 01.07 manifest mismatch: {july_manifest}")
    if any(item["envelope_origin"] != "root" for item in plan["source_manifests"]["ads"]["dates"]):
        raise AssertionError("root ads payload did not use the shared compatibility resolver")
    matrix = plan["week_nm_operation_date_matrix"]
    by_date = {item["operation_date"]: item for item in matrix}
    if by_date["2026-01-06"]["canonical_source_date"] != "2026-07-01":
        raise AssertionError(f"January source-date projection mismatch: {matrix}")
    if by_date["2026-06-30"]["canonical_source_date"] != "2026-07-01":
        raise AssertionError(f"30 June source-date projection mismatch: {matrix}")
    if by_date["2026-07-02"]["canonical_source_date"] != "2026-07-02":
        raise AssertionError(f"post-cutover exact-date mismatch: {matrix}")
    if by_date["2026-07-14"]["canonical_source_date"] != "2026-07-14":
        raise AssertionError(f"latest exact-date mismatch: {matrix}")
    for week in plan["weeks"]:
        if not week["profit_delta_inputs"]["raw_fields"] or not week["profit_delta_inputs"]["source_contracts"]:
            raise AssertionError(f"profit delta evidence is incomplete: {week}")
        after = week["after"]
        if after["agent_remuneration_rub"] + "" == "":
            raise AssertionError(f"agent reconciliation is absent: {week}")
        if after["commission_control_reconciliation_rub"] != "0.0000":
            raise AssertionError(f"agent + acquiring does not equal combined: {week}")
    last = next(item for item in plan["weeks"] if item["week_start"] == "2026-07-13")
    if last["delta"]["cogs_rub"] != "0.0000" or last["delta"]["profit_after_cogs_rub"] == "0.0000":
        raise AssertionError(f"unchanged COGS / changed profit reconciliation missing: {last}")
    if (
        "before-COGS profit delta" not in last["profit_delta_explanation"]
        or not last["profit_delta_inputs"]["component_reconciliation"]
        or last["profit_delta_inputs"]["profit_identity"]
        != "profit_after_cogs_delta = before_cogs_profit_delta - cogs_delta"
    ):
        raise AssertionError(f"profit delta explanation is not explicit: {last}")
    invariants = plan["invariants"]
    required_true = (
        "raw_finance_rows_immutable",
        "ads_rows_immutable",
        "ads_missing_never_written_as_zero",
        "canonical_cost_rows_immutable",
        "exact_date_cost_values_from_2026_07_01_unchanged",
        "sku_aggregate_bound_to_target_readback",
    )
    if not all(invariants[key] for key in required_true):
        raise AssertionError(f"non-target invariants absent: {invariants}")
    if plan["write_set"]["retro_cost_map_rows"] != 0:
        raise AssertionError("new plan still proposes independent retro-cost values")
    if (
        plan["source_manifests"]["cost"]["missing_nm_id_count"] != 0
        or plan["write_set"]["expected_sku_projection_row_count"] != 6
        or len(plan["write_set"]["weeks"]) != 3
    ):
        raise AssertionError(f"cost/write manifests are incomplete: {plan}")


def _assert_apply_gates(
    block: WbFinanceWeeklyBlock,
    runtime: Path,
    backups: Path,
    fingerprint: str,
) -> None:
    for expected, action in (
        (
            "approval_reference",
            lambda: block.apply_canonical_finance_backfill(
                expected_fingerprint=fingerprint,
                approval_reference="",
            ),
        ),
        (
            "permanently revoked",
            lambda: block.apply_canonical_finance_backfill(
                expected_fingerprint=REVOKED,
                approval_reference="fixture",
            ),
        ),
        (
            "drifted",
            lambda: block.apply_canonical_finance_backfill(
                expected_fingerprint="sha256:wrong",
                approval_reference="fixture",
            ),
        ),
    ):
        try:
            action()
        except ValueError as exc:
            if expected not in str(exc):
                raise AssertionError(f"wrong gate error: {exc}") from exc
        else:
            raise AssertionError(f"apply gate {expected!r} did not fail closed")

    no_approval = _run_cli(
        runtime,
        "--apply",
        "--confirm-fingerprint",
        fingerprint,
        "--backup-dir",
        str(backups),
    )
    if no_approval.returncode == 0 or "--approval-reference" not in no_approval.stderr:
        raise AssertionError("CLI accepted apply without the explicit human gate")

    with sqlite3.connect(block.db_path) as conn:
        saved = conn.execute(
            """SELECT metrics_json FROM wb_finance_weekly_sku_aggregates
               ORDER BY week_start,nm_id LIMIT 1"""
        ).fetchone()[0]
        conn.execute(
            """UPDATE wb_finance_weekly_sku_aggregates SET metrics_json='{"drift":true}'
               WHERE rowid=(SELECT rowid FROM wb_finance_weekly_sku_aggregates
                            ORDER BY week_start,nm_id LIMIT 1)"""
        )
        conn.commit()
    try:
        block.apply_canonical_finance_backfill(
            expected_fingerprint=fingerprint,
            approval_reference="fixture",
        )
    except ValueError as exc:
        if "drifted" not in str(exc):
            raise
    else:
        raise AssertionError("per-SKU target drift did not invalidate the reviewed plan")
    finally:
        with sqlite3.connect(block.db_path) as conn:
            conn.execute(
                """UPDATE wb_finance_weekly_sku_aggregates SET metrics_json=?
                   WHERE rowid=(SELECT rowid FROM wb_finance_weekly_sku_aggregates
                                ORDER BY week_start,nm_id LIMIT 1)""",
                (saved,),
            )
            conn.commit()


def _assert_apply_result(
    block: WbFinanceWeeklyBlock,
    result: dict,
    backups: Path,
) -> None:
    if (
        result["status"] != "applied"
        or result["week_count"] != 3
        or result["retro_cost_map_rows_written"] != 0
        or result["non_target_digest_before"] != result["non_target_digest_after"]
        or not result["idempotent"]
    ):
        raise AssertionError(f"canonical apply reconciliation mismatch: {result}")
    backup_files = list(backups.glob("*.sqlite3"))
    if len(backup_files) != 1:
        raise AssertionError(f"expected exactly one coherent backup: {backup_files}")
    backup = result["backup"]
    if (
        backup["integrity_check"] != "ok"
        or not str(backup["sha256"]).startswith("sha256:")
        or stat.S_IMODE(backup_files[0].stat().st_mode) != 0o600
    ):
        raise AssertionError(f"backup evidence mismatch: {backup}")
    with sqlite3.connect(block.db_path) as conn:
        retro_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wb_finance_retro_cost_map'"
        ).fetchone()
        retro_count = (
            conn.execute("SELECT COUNT(*) FROM wb_finance_retro_cost_map").fetchone()[0]
            if retro_table is not None
            else 0
        )
        raw_count = conn.execute("SELECT COUNT(*) FROM wb_finance_weekly_raw_rows").fetchone()[0]
        ads_count = conn.execute("SELECT COUNT(*) FROM temporal_source_slot_snapshots").fetchone()[0]
        sku_projection_count = conn.execute(
            "SELECT COUNT(*) FROM wb_finance_weekly_sku_aggregates"
        ).fetchone()[0]
        stale_sku_projection_count = conn.execute(
            """SELECT COUNT(*) FROM wb_finance_weekly_sku_aggregates
               WHERE formula_version<>'wb_finance_weekly_sku_aggregate_v2'"""
        ).fetchone()[0]
    if (
        retro_count
        or raw_count != 5
        or ads_count != 196
        or sku_projection_count != 6
        or stale_sku_projection_count
    ):
        raise AssertionError("apply mutated raw Finance, ads, or retro-cost storage")


def _seed_sources(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                is_active INTEGER,nm_id INTEGER,vendor_code TEXT,barcode TEXT,
                barcodes_json TEXT,product_type TEXT
            );
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES
                (1,101,'VC101','BAR101','["BAR101"]','other');
            CREATE TABLE sheet_vitrina_v1_warehouse_functional_cutovers(
                cutover_id TEXT PRIMARY KEY,cutover_at TEXT,status TEXT,
                plan_fingerprint TEXT,source_watermarks_json TEXT,
                absorbed_supply_revisions_json TEXT,backup_json TEXT,
                created_at TEXT,updated_at TEXT
            );
            INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers VALUES(
                'warehouse_functional_cutover_v1','2026-07-01T00:00:00Z','posted',
                'sha256:cutover','{}','[]','{}','2026-07-01T00:00:00Z','2026-07-01T00:00:00Z'
            );
            CREATE TABLE sheet_vitrina_v1_warehouse_wb_daily_cost(
                cutover_id TEXT,as_of_date TEXT,nm_id INTEGER,quantity TEXT,wac_rub TEXT,
                capital_rub TEXT,quality TEXT,provenance_json TEXT,fingerprint TEXT,
                created_at TEXT,PRIMARY KEY(cutover_id,as_of_date,nm_id)
            );
            INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost VALUES
                ('warehouse_functional_cutover_v1','2026-07-01',101,'10','100','1000','certified','{}','sha256:jul1','2026-07-01T00:00:00Z'),
                ('warehouse_functional_cutover_v1','2026-07-02',101,'10','120','1200','certified','{}','sha256:jul2','2026-07-02T00:00:00Z'),
                ('warehouse_functional_cutover_v1','2026-07-14',101,'10','140','1400','certified','{}','sha256:jul14','2026-07-14T00:00:00Z');
            CREATE TABLE temporal_source_slot_snapshots(
                source_key TEXT,snapshot_date TEXT,snapshot_role TEXT,captured_at TEXT,payload_json TEXT
            );
            """
        )
        cursor = date(2026, 1, 5)
        while cursor <= date(2026, 7, 19):
            day = cursor.isoformat()
            payload = {
                "kind": "success",
                "snapshot_date": day,
                "items": [{"nm_id": 101, "ads_sum": "0"}],
            }
            conn.execute(
                "INSERT INTO temporal_source_slot_snapshots VALUES(?,?,?,?,?)",
                (
                    "ads_compact",
                    day,
                    "accepted_closed_day_snapshot",
                    day + "T23:00:00Z",
                    json.dumps(payload, separators=(",", ":")),
                ),
            )
            cursor += timedelta(days=1)
        conn.commit()


def _seed_weeks(block: WbFinanceWeeklyBlock) -> None:
    block.ingest_week(date(2026, 1, 5), date(2026, 1, 11), [_row(1, "2026-01-06")])
    block.ingest_week(
        date(2026, 6, 29),
        date(2026, 7, 5),
        [
            _row(2, "2026-06-30", quantity=2),
            _row(3, "2026-07-01"),
            _row(4, "2026-07-02", returned=True),
        ],
    )
    block.ingest_week(date(2026, 7, 13), date(2026, 7, 19), [_row(5, "2026-07-14")])


def _make_stored_results_stale(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT seller_id,week_start,week_end,metrics_json FROM wb_finance_weekly_aggregates"
        ).fetchall()
        for seller_id, week_start, week_end, raw_metrics in rows:
            metrics = json.loads(raw_metrics)
            if week_start == "2026-07-13":
                # Preserve exact COGS but emulate the previous combined-classification profit.
                metrics["profit_after_cogs"] = str(float(metrics["profit_after_cogs"]) + 10)
                metrics["final_margin_pct"] = str(float(metrics["final_margin_pct"]) + 1)
                metrics["before_cogs_profit"] = str(float(metrics["before_cogs_profit"]) + 10)
            else:
                metrics["cogs"] = "1.0000"
                metrics["profit_after_cogs"] = "2.0000"
                metrics["final_margin_pct"] = "3.0000"
                metrics["before_cogs_profit"] = "4.0000"
            conn.execute(
                """UPDATE wb_finance_weekly_aggregates SET classifier_version='legacy',metrics_json=?
                   WHERE seller_id=? AND week_start=? AND week_end=?""",
                (json.dumps(metrics, sort_keys=True), seller_id, week_start, week_end),
            )
        conn.commit()


def _row(
    rrd_id: int,
    operation_date: str,
    *,
    quantity: int = 1,
    returned: bool = False,
) -> dict:
    revenue = 200 * quantity
    for_pay = 140 * quantity
    acquiring = 10 * quantity
    return {
        "dateFrom": operation_date,
        "dateTo": operation_date,
        "reportId": rrd_id,
        "reportType": 1,
        "rrdId": rrd_id,
        "nmId": 101,
        "vendorCode": "VC101",
        "sku": "BAR101",
        "rrDate": operation_date,
        "saleDt": operation_date,
        "docTypeName": "Возврат" if returned else "Продажа",
        "sellerOperName": "Возврат" if returned else "Продажа",
        "quantity": quantity,
        "retailPriceWithDisc": str(revenue),
        "forPay": str(for_pay),
        "acquiringFee": str(acquiring),
    }


def _run_cli(runtime: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["SELLER_PORTAL_CANONICAL_SUPPLIER_ID"] = "seller-1"
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "apps/wb_finance_weekly.py"),
            "canonical-cost-backfill",
            "--runtime-dir",
            str(runtime),
            "--env-file",
            str(runtime / "missing.env"),
            *arguments,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
