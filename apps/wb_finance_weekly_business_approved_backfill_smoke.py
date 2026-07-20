#!/usr/bin/env python3
"""Production-safe dry-run/apply smoke for Finance business-approved retro cost."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock  # noqa: E402


def main() -> None:
    with TemporaryDirectory(prefix="wb-finance-approved-runner-") as tmp:
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

        dry = _run_cli(runtime, "--date-from", "2026-04-27", "--date-to", "2026-05-03")
        if dry.returncode != 0:
            raise AssertionError(f"dry-run failed: {dry.stderr}\n{dry.stdout}")
        plan = json.loads(dry.stdout)
        _assert_plan(plan)
        fingerprint = plan["fingerprint"]
        try:
            block.apply_business_approved_backfill(
                expected_fingerprint=fingerprint,
                approval_reference="",
                date_from=date(2026, 4, 27),
                date_to=date(2026, 5, 3),
            )
        except ValueError as exc:
            if "approval_reference" not in str(exc):
                raise
        else:
            raise AssertionError("application boundary accepted an apply without human approval")

        missing_gate = _run_cli(
            runtime,
            "--date-from", "2026-04-27",
            "--date-to", "2026-05-03",
            "--apply",
            "--confirm-fingerprint", fingerprint,
            "--backup-dir", str(backups),
        )
        if missing_gate.returncode == 0 or "--approval-reference" not in missing_gate.stderr:
            raise AssertionError("apply without the human approval reference was not rejected")

        wrong = _run_cli(
            runtime,
            "--date-from", "2026-04-27",
            "--date-to", "2026-05-03",
            "--apply",
            "--confirm-fingerprint", "sha256:wrong",
            "--backup-dir", str(backups),
            "--approval-reference", "fixture-approval",
        )
        if wrong.returncode == 0 or "does not match" not in wrong.stderr:
            raise AssertionError("apply with an unreviewed fingerprint was not rejected")

        applied = _run_cli(
            runtime,
            "--date-from", "2026-04-27",
            "--date-to", "2026-05-03",
            "--apply",
            "--confirm-fingerprint", fingerprint,
            "--backup-dir", str(backups),
            "--approval-reference", "fixture-approval",
        )
        if applied.returncode != 0:
            raise AssertionError(f"approved apply failed: {applied.stderr}\n{applied.stdout}")
        result = json.loads(applied.stdout)
        _assert_apply(block, result)

        repeated = _run_cli(
            runtime,
            "--date-from", "2026-04-27",
            "--date-to", "2026-05-03",
            "--apply",
            "--confirm-fingerprint", fingerprint,
            "--backup-dir", str(backups),
            "--approval-reference", "fixture-approval",
        )
        if repeated.returncode != 0:
            raise AssertionError(f"repeat exact apply failed: {repeated.stderr}\n{repeated.stdout}")
        repeat_result = json.loads(repeated.stdout)
        if (
            repeat_result["status"] != "already_current"
            or repeat_result["runtime_mutation"]
            or repeat_result["backup"] is not None
        ):
            raise AssertionError(f"repeat exact apply was not a no-op: {repeat_result}")

    print(
        "wb_finance_business_approved_backfill: ok -> manifests, fingerprint, human gate, "
        "0600 backup, atomic apply, mixed week, reconciliation, non-target invariant, repeat no-op"
    )


def _run_cli(runtime: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["SELLER_PORTAL_CANONICAL_SUPPLIER_ID"] = "seller-1"
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "apps/wb_finance_weekly.py"),
            "business-approved-backfill",
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


def _assert_plan(plan: dict) -> None:
    if not plan["apply_allowed"] or plan["target_week_count"] != 1:
        raise AssertionError(f"unexpected dry-run target: {plan}")
    if plan["runtime_mutation"] or plan["status"] != "dry_run":
        raise AssertionError(f"dry-run mutated state: {plan}")
    if plan["source_manifests"]["finance"]["sale_return_nm_ids"] != ["101"]:
        raise AssertionError(f"Finance manifest mismatch: {plan['source_manifests']['finance']}")
    if plan["source_manifests"]["cost"]["proposed_row_count"] != 1:
        raise AssertionError(f"cost manifest mismatch: {plan['source_manifests']['cost']}")
    coverage = plan["source_manifests"]["coverage"]
    if (
        coverage["week_count"] != 1
        or coverage["complete_week_count"] != 1
        or coverage["weeks"][0]["expected"]["coverage_pct"] != "100.0000"
        or not str(coverage["digest"]).startswith("sha256:")
    ):
        raise AssertionError(f"per-week coverage manifest mismatch: {coverage}")
    ads = plan["source_manifests"]["ads"]
    if ads["complete"] or ads["missing_date_nm_id_count"] != 3:
        raise AssertionError(f"ads evidence gaps must be explicit without blocking Finance: {ads}")
    if plan["weeks"][0]["expected"]["cogs"] != "150.0000":
        raise AssertionError(f"mixed-week row-level COGS mismatch: {plan['weeks']}")
    for key in ("source_digest", "target_before_digest", "non_target_digest", "fingerprint"):
        if not str(plan.get(key) or "").startswith("sha256:"):
            raise AssertionError(f"missing {key}: {plan}")
    if plan["backup_plan"]["integrity_check"] != "required_ok":
        raise AssertionError(f"backup contract missing: {plan['backup_plan']}")


def _assert_apply(block: WbFinanceWeeklyBlock, result: dict) -> None:
    if (
        result["status"] != "applied"
        or result["recalculated_week_count"] != 1
        or not result["non_target_preserved"]
        or result["post_apply_target_week_count"] != 0
    ):
        raise AssertionError(f"apply/reconciliation mismatch: {result}")
    backup = result["backup"]
    backup_path = Path(backup["path"])
    if backup["integrity_check"] != "ok" or backup_path.stat().st_mode & 0o777 != 0o600:
        raise AssertionError(f"backup safety mismatch: {backup}")
    actual_sha = "sha256:" + hashlib.sha256(backup_path.read_bytes()).hexdigest()
    if actual_sha != backup["sha256"]:
        raise AssertionError(f"backup digest mismatch: {backup}")
    week = next(
        item for item in block.build_payload()["weeks"] if item["week_start"] == "2026-04-27"
    )
    if (
        week["metrics"]["cogs"] != "150.0000"
        or week["cost_coverage"]["coverage_pct"] != "100.0000"
        or week["cost_coverage"]["quality"]["source_units"]
        != {
            "cost_price": 1,
            "business_approved_retro": 3,
            "our_wb_cost_daily_state": 0,
        }
    ):
        raise AssertionError(f"post-apply mixed week mismatch: {week}")
    with sqlite3.connect(block.db_path) as conn:
        audit_rows = conn.execute(
            "SELECT result_json FROM wb_finance_projection_audit WHERE fingerprint=?",
            (result["fingerprint"],),
        ).fetchall()
        retro = conn.execute(
            "SELECT status,selection_method FROM wb_finance_retro_cost_map WHERE nm_id='101'"
        ).fetchone()
    audit_result = json.loads(audit_rows[0][0]) if len(audit_rows) == 1 else {}
    if (
        len(audit_rows) != 1
        or audit_result.get("approval_reference") != "fixture-approval"
        or retro != ("business_approved_retro", "exact_2026_07_01")
    ):
        raise AssertionError(
            f"immutable map/audit mismatch: audits={audit_rows}, retro={retro}"
        )


def _seed_sources(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE registry_upload_current_state(slot INTEGER PRIMARY KEY,bundle_version TEXT,activated_at TEXT);
            CREATE TABLE registry_upload_config_v2(bundle_version TEXT,nm_id INTEGER,enabled INTEGER,display_name TEXT,group_name TEXT,display_order INTEGER);
            CREATE TABLE cost_price_current_state(slot INTEGER PRIMARY KEY,dataset_version TEXT,activated_at TEXT);
            CREATE TABLE cost_price_upload_rows(dataset_version TEXT,row_order INTEGER,group_name TEXT,cost_price_rub TEXT,effective_from TEXT);
            CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                is_active INTEGER,nm_id INTEGER,vendor_code TEXT,barcode TEXT,
                barcodes_json TEXT,product_type TEXT,aliases_json TEXT
            );
            CREATE TABLE sheet_vitrina_v1_wb_cost_daily_state(
                as_of_date TEXT NOT NULL,nm_id INTEGER NOT NULL,stock_qty REAL NOT NULL,
                our_wb_unit_cost_rub REAL,confirmed_qty REAL NOT NULL,estimated_qty REAL NOT NULL,
                fallback_qty REAL NOT NULL,confirmed_share_pct REAL,source_status TEXT NOT NULL,
                component_status_json TEXT NOT NULL,calculated_at TEXT NOT NULL,inputs_hash TEXT NOT NULL,
                PRIMARY KEY(as_of_date,nm_id)
            );
            INSERT INTO registry_upload_current_state VALUES(1,'bundle','2026-01-01');
            INSERT INTO registry_upload_config_v2 VALUES('bundle',101,1,'SKU 101','Group',1);
            INSERT INTO cost_price_current_state VALUES(1,'cost','2026-01-01');
            INSERT INTO cost_price_upload_rows VALUES('cost',1,'Group','50','2026-01-01');
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(1,101,'VC101','BC101','["BC101"]','other','[]');
            INSERT INTO sheet_vitrina_v1_wb_cost_daily_state VALUES(
                '2026-07-01',101,10,100,0,10,0,0,'historical_provisional','{}',
                '2026-07-01T23:00:00Z','sha256:cost-101-july-1'
            );
            """
        )
        conn.commit()


def _seed_weeks(block: WbFinanceWeeklyBlock) -> None:
    old = {
        "dateFrom": "2026-04-20",
        "dateTo": "2026-04-26",
        "reportId": 1,
        "reportType": 1,
        "rrdId": 1,
        "rrDate": "2026-04-22",
        "nmId": 101,
        "vendorCode": "VC101",
        "sku": "BC101",
        "docTypeName": "Продажа",
        "quantity": 1,
        "retailPriceWithDisc": "200",
        "forPay": "150",
    }
    block.ingest_week(date(2026, 4, 20), date(2026, 4, 26), [old])
    rows = [
        {**old, "dateFrom": "2026-04-27", "dateTo": "2026-05-03", "reportId": 2, "rrdId": 2, "rrDate": "2026-04-30"},
        {**old, "dateFrom": "2026-04-27", "dateTo": "2026-05-03", "reportId": 2, "rrdId": 3, "rrDate": "2026-05-01", "quantity": 2, "retailPriceWithDisc": "400", "forPay": "300"},
        {**old, "dateFrom": "2026-04-27", "dateTo": "2026-05-03", "reportId": 3, "reportType": 2, "rrdId": 4, "rrDate": "2026-05-02", "docTypeName": "Возврат", "quantity": 1, "retailPriceWithDisc": "200", "forPay": "150"},
        {**old, "dateFrom": "2026-04-27", "dateTo": "2026-05-03", "reportId": 2, "rrdId": 5, "rrDate": "2026-04-30", "docTypeName": "", "quantity": 0, "retailPriceWithDisc": "0", "forPay": "0", "paidAcceptance": "5"},
        {**old, "dateFrom": "2026-04-27", "dateTo": "2026-05-03", "reportId": 2, "rrdId": 6, "rrDate": "2026-05-02", "docTypeName": "", "quantity": 0, "retailPriceWithDisc": "0", "forPay": "0", "paidAcceptance": "10", "deduction": "20", "bonusTypeName": "Услуги доставки транзитных поставок"},
    ]
    block.ingest_week(date(2026, 4, 27), date(2026, 5, 3), rows)


if __name__ == "__main__":
    main()
