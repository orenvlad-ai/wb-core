#!/usr/bin/env python3
"""Production-shaped smoke for the immutable break-glass last-good contour."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wbc0027_breakglass_last_good import (  # noqa: E402
    BreakglassRunnerError,
    ECONOMICS_KEYS,
    WAC_KEYS,
    apply_manifest,
    build_manifest,
)
from packages.application.sheet_vitrina_v1_breakglass_last_good import (  # noqa: E402
    apply_breakglass_last_good_overlay,
)
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow  # noqa: E402


CAPTURE_ID = "ivhc_published_before_incident"
CAPTURE_DIGEST = "sha256:" + "1" * 64
CHECKPOINT_OPERATION = "recovery_published_checkpoint"


def main() -> None:
    with TemporaryDirectory(prefix="wbc0027-breakglass-last-good-") as raw:
        root = Path(raw)
        db_path = root / "operational.sqlite3"
        checkpoint_path = root / "checkpoint.sqlite3"
        _seed_operational(db_path)
        _seed_checkpoint(checkpoint_path)
        operation_id = "wbc0027-breakglass-smoke-op"
        manifest = build_manifest(
            db_path=db_path,
            operation_id=operation_id,
            source_capture_id=CAPTURE_ID,
            checkpoint_path=checkpoint_path,
            expected_checkpoint_sha256=_file_sha256(checkpoint_path),
            checkpoint_operation_id=CHECKPOINT_OPERATION,
            checkpoint_ready_as_of="2026-08-29",
            checkpoint_column_date="2026-08-30",
            expected_cell_count=18,
            created_at="2026-09-01T15:00:00Z",
        )
        if manifest["scope"]["family_counts"] != {
            "functional_economics": 10,
            "functional_wac": 2,
            "inventory_combined_total": 2,
            "inventory_fbs_facility": 4,
        }:
            raise AssertionError(f"unexpected breakglass scope: {manifest['scope']}")
        manifest_path = root / "manifest.json"
        manifest_path.write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
        manifest_digest = _file_sha256(manifest_path)
        writer_lock_path = root / "writer.lock"
        writer_lock_path.touch()
        receipt = apply_manifest(
            db_path=db_path,
            manifest_path=manifest_path,
            expected_manifest_sha256=manifest_digest,
            operation_id=operation_id,
            evidence_dir=root / "evidence",
            writer_lock_path=writer_lock_path,
        )
        if receipt["cell_insert_count"] != 18 or receipt["readback"]["status"] != "verified":
            raise AssertionError(f"breakglass apply/readback mismatch: {receipt}")
        rows = [
            _row("SKU:101|stock_total", "", ""),
            _row("SKU:101|our_wb_unit_cost_rub", "999", "999"),
            _row("SKU:101|proxy_profit_3_rub", "", ""),
            _row("SKU:101|inventory_wb_total_qty_v1", 7, 8),
        ]
        overlaid = apply_breakglass_last_good_overlay(
            rows,
            db_path=db_path,
            date_columns=["2026-08-31", "2026-09-01"],
        )
        by_id = {item.row_id: item for item in overlaid}
        if by_id["SKU:101|stock_total"].values_by_date != {
            "2026-08-31": 22,
            "2026-09-01": 22,
        }:
            raise AssertionError("blank combined cells must use last-good")
        if by_id["SKU:101|our_wb_unit_cost_rub"].values_by_date != {
            "2026-08-31": "999",
            "2026-09-01": "999",
        }:
            raise AssertionError("newer non-empty cells must win")
        if by_id["SKU:101|proxy_profit_3_rub"].presentation_by_date["2026-09-01"]["quality_state"] != "last_good_provisional":
            raise AssertionError("last-good must remain explicitly provisional")
        if by_id["SKU:101|inventory_wb_total_qty_v1"].values_by_date != {
            "2026-08-31": 7,
            "2026-09-01": 8,
        }:
            raise AssertionError("WB/non-target cells must remain unchanged")
        try:
            apply_manifest(
                db_path=db_path,
                manifest_path=manifest_path,
                expected_manifest_sha256=manifest_digest,
                operation_id=operation_id,
                evidence_dir=root / "evidence",
                writer_lock_path=writer_lock_path,
            )
        except BreakglassRunnerError as exc:
            if "blind retry forbidden" not in str(exc):
                raise
        else:
            raise AssertionError("a second submit must fail closed")
    print("wbc0027_breakglass_last_good_smoke: OK")


def _seed_operational(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE registry_upload_current_state(slot INTEGER PRIMARY KEY,bundle_version TEXT,activated_at TEXT);
        CREATE TABLE registry_upload_config_v2(bundle_version TEXT,nm_id INTEGER,enabled INTEGER,display_name TEXT,group_name TEXT,display_order INTEGER);
        CREATE TABLE sheet_vitrina_v1_inventory_history_captures(
          capture_sequence INTEGER,capture_id TEXT,business_date TEXT,capture_kind TEXT,
          formula_version TEXT,bundle_version TEXT,ready_snapshot_id TEXT,ready_plan_version TEXT,
          generation_identity TEXT,facility_roster_revision TEXT,facility_roster_json TEXT,
          source_manifest_json TEXT,source_digest TEXT,captured_at TEXT
        );
        CREATE TABLE sheet_vitrina_v1_inventory_history_components(
          capture_id TEXT,scope_kind TEXT,scope_key TEXT,nm_id INTEGER,component_kind TEXT,
          component_id TEXT,component_label TEXT,state TEXT,quantity INTEGER,source_revision TEXT,
          source_digest TEXT,source_watermark TEXT,provenance_json TEXT,captured_at TEXT
        );
        CREATE TABLE sheet_vitrina_v1_inventory_history_finalizations(x TEXT);
        CREATE TABLE sheet_vitrina_v1_ready_snapshots(x TEXT);
        CREATE TABLE sheet_vitrina_v1_warehouse_functional_active(x TEXT);
        CREATE TABLE sheet_vitrina_v1_warehouse_wb_snapshots(x TEXT);
        CREATE TABLE sheet_vitrina_v1_ff_pool_balances(x TEXT);
        CREATE TABLE sheet_vitrina_v1_ff_pool_fbs_lifecycle_current(x TEXT);
        INSERT INTO registry_upload_current_state VALUES(1,'bundle','2026-09-01T00:00:00Z');
        INSERT INTO registry_upload_config_v2 VALUES('bundle',101,1,'SKU 101','G',1);
        """
    )
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_inventory_history_captures VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (361, CAPTURE_ID, "2026-08-31", "accepted_refresh", "inventory_planning_v1", "bundle", "ready", "plan", "generation", "roster", "[]", "{}", CAPTURE_DIGEST, "2026-08-31T11:09:59Z"),
    )
    for scope_kind, scope_key, nm_id in (("TOTAL", "TOTAL", None), ("SKU", "SKU:101", 101)):
        for kind, component_id, quantity in (("WB", "WB", 7), ("FBS_FACILITY", "f1", 5), ("FBS_FACILITY", "f2", 10)):
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_inventory_history_components VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (CAPTURE_ID, scope_kind, scope_key, nm_id, kind, component_id, component_id, "exact", quantity, "revision", "sha256:" + "2" * 64, "watermark", "{}", "2026-08-31T11:09:59Z"),
            )
    conn.commit()
    conn.close()


def _seed_checkpoint(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sheet_vitrina_v1_ready_snapshots(as_of_date TEXT,snapshot_id TEXT,refreshed_at TEXT,plan_json TEXT)")
    rows = []
    for metric_key in sorted(WAC_KEYS | ECONOMICS_KEYS):
        if metric_key.startswith("total_") or metric_key.endswith("_total"):
            row_id = f"TOTAL|{metric_key}"
        else:
            row_id = f"SKU:101|{metric_key}"
        rows.append([metric_key, row_id, "", 123.45])
    plan = {
        "as_of_date": "2026-08-29",
        "date_columns": ["2026-08-29", "2026-08-30"],
        "sheets": [{"sheet_name": "DATA_VITRINA", "rows": rows}],
    }
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?)",
        ("2026-08-29", "ready-source", "2026-08-30T17:09:39Z", _canonical_json(plan)),
    )
    conn.commit()
    conn.close()


def _row(row_id: str, first: object, second: object) -> WebVitrinaContractRow:
    scope_key, _, metric_key = row_id.partition("|")
    return WebVitrinaContractRow(
        row_id=row_id,
        row_order=1,
        scope_kind="SKU",
        scope_key=scope_key,
        scope_label=scope_key,
        metric_key=metric_key,
        metric_label=metric_key,
        row_last_updated_at="2026-09-01T00:00:00Z",
        section="test",
        group=None,
        nm_id=101,
        format="number",
        values_by_date={"2026-08-31": first, "2026-09-01": second},
        presentation_by_date={},
    )


def _file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    main()
