#!/usr/bin/env python3
"""Publish canonical cost projections into the persisted web-vitrina snapshot.

The canonical engine owns quantities and costs after 2026-07-01.  This runner
only replaces post-cutover cells in the ready snapshot; legacy dates remain
untouched and remain available as audit history.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.canonical_cost_engine import (  # noqa: E402
    CUTOVER_DATE,
    STAGE_FF,
    STAGE_FF_TO_WB,
    STAGE_PRODUCTION,
    STAGE_WB,
    CanonicalCostEngine,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _connect,
)


STAGE_BY_UI = {
    "CHINA_TO_FF": STAGE_PRODUCTION,
    "FF_STOCK": STAGE_FF,
    "FF_TO_WB": STAGE_FF_TO_WB,
    "WB_STOCK": STAGE_WB,
}


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _dec(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _value_for_metric(metric: str, nm_id: int | None, lookup: dict[int, dict[str, Any]]) -> float | str:
    if nm_id is None:
        entries = list(lookup.values())
    else:
        entries = [lookup.get(nm_id, {})]
    if metric == "onec_total_qty":
        return sum(_dec(stage.get("physical_quantity")) for item in entries for stage in item.get("stages", {}).values())
    if metric == "onec_total_cost_rub":
        return sum(_dec(stage.get("paid_capital_rub")) for item in entries for stage in item.get("stages", {}).values())
    if metric in {"onec_WB_STOCK_unit_cost_rub", "our_wb_unit_cost_rub"}:
        rows = [item.get("stages", {}).get(STAGE_WB, {}) for item in entries]
        if metric == "our_wb_unit_cost_rub":
            capital = sum(_dec(row.get("recognized_capital_rub")) for row in rows)
            qty = sum(_dec(row.get("physical_quantity")) for row in rows)
        else:
            capital = sum(_dec(row.get("paid_capital_rub")) for row in rows)
            qty = sum(_dec(row.get("paid_equivalent_quantity")) for row in rows)
        return capital / qty if qty else ""
    if metric.startswith("onec_"):
        ui_stage = next((name for name in STAGE_BY_UI if metric.startswith(f"onec_{name}_")), None)
        if ui_stage is None:
            return ""
        suffix = metric[len(f"onec_{ui_stage}_"):]
        if suffix == "unit_cost_rub":
            field = "paid_unit_cost_rub"
        elif suffix == "cost_total_rub":
            field = "paid_capital_rub"
        elif suffix == "qty":
            field = "physical_quantity"
        else:
            return ""
        stage = STAGE_BY_UI[ui_stage]
        rows = [item.get("stages", {}).get(stage, {}) for item in entries]
        if field == "paid_unit_cost_rub":
            capital = sum(_dec(row.get("paid_capital_rub")) for row in rows)
            qty = sum(_dec(row.get("paid_equivalent_quantity")) for row in rows)
            return capital / qty if qty else ""
        return sum(_dec(row.get(field)) for row in rows)
    return ""


def _publication_payload(db_path: Path, *, date_from: str, date_to: str) -> dict[str, Any]:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=db_path.parent)
    engine = CanonicalCostEngine(runtime=runtime)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT as_of_date, plan_json FROM sheet_vitrina_v1_ready_snapshots "
            "WHERE as_of_date BETWEEN ? AND ? ORDER BY as_of_date",
            (date_from, date_to),
        ).fetchall()
    available = sorted({str(row[0]) for row in rows})
    raw_lookups = {day: engine.load_daily_metric_lookup(day) for day in available}
    lookups = {}
    latest = None
    for day in available:
        if raw_lookups[day]:
            latest = raw_lookups[day]
        lookups[day] = raw_lookups[day] or (latest or {})
    changed_cells = 0
    snapshots: list[dict[str, Any]] = []
    plans: dict[str, str] = {}
    for row in rows:
        day = str(row[0])
        plan = json.loads(row[1])
        lookup = lookups[day]
        changed = 0
        for sheet in plan.get("sheets", []):
            header = sheet.get("header", [])
            date_columns = [i for i, value in enumerate(header) if str(value) >= date_from]
            for values in sheet.get("rows", []):
                if len(values) < 2:
                    continue
                key = str(values[1])
                scope, _, metric = key.partition("|")
                if not metric.startswith(("onec_", "total_onec_", "avg_onec_", "own_capital_", "total_own_capital_", "avg_own_capital_", "our_wb_unit_cost_rub", "total_our_wb_unit_cost_rub")):
                    continue
                nm_id = None
                if scope.startswith("SKU:"):
                    try:
                        nm_id = int(scope.split(":", 1)[1])
                    except ValueError:
                        continue
                metric_name = metric
                if metric_name.startswith("total_onec_"):
                    metric_name = metric_name[len("total_"):]
                if metric_name.startswith("avg_onec_"):
                    metric_name = metric_name[len("avg_"):]
                if metric_name.startswith("total_own_capital_"):
                    metric_name = metric_name[len("total_"):]
                if metric_name.startswith("avg_own_capital_"):
                    metric_name = metric_name[len("avg_"):]
                if metric_name.startswith("own_capital_"):
                    metric_name = "onec_" + metric_name[len("own_capital_"):]
                if metric_name == "total_our_wb_unit_cost_rub":
                    metric_name = "our_wb_unit_cost_rub"
                value = _value_for_metric(metric_name, nm_id, lookup)
                for index in date_columns:
                    if index < len(values) and values[index] != value:
                        values[index] = value
                        changed += 1
        if changed:
            metadata = plan.setdefault("metadata", {})
            metadata["canonical_cost_projection"] = {
                "contract": "canonical_cost_engine_vitrina_projection_v1",
                "cutover_date": date_from,
                "source": "sheet_vitrina_v1_canonical_cost_daily_state",
                "projection": "paid_capital_for_product_capital_and_recognized_wb_cost",
                "source_date": day,
            }
        plans[day] = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
        snapshots.append({"as_of_date": day, "changed_cells": changed})
        changed_cells += changed
    return {"snapshots": snapshots, "plans": plans, "changed_cells": changed_cells}


def _backup(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        dst.commit()
    destination.chmod(0o600)
    with closing(sqlite3.connect(f"file:{destination.resolve()}?mode=ro", uri=True)) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    digest_hash = hashlib.sha256()
    with destination.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest_hash.update(chunk)
    digest = digest_hash.hexdigest()
    return {"path": str(destination), "mode": oct(destination.stat().st_mode & 0o777), "integrity": integrity, "sha256": digest, "size": destination.stat().st_size}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--date-from", default=CUTOVER_DATE)
    parser.add_argument("--date-to", default=date.today().isoformat())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args()
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    before = _publication_payload(runtime.db_path, date_from=args.date_from, date_to=args.date_to)
    fingerprint = _hash({"date_from": args.date_from, "date_to": args.date_to, "plans": before["plans"]})
    report = {"contract": "canonical_cost_engine_vitrina_publication_v1", "date_from": args.date_from, "date_to": args.date_to, "fingerprint": fingerprint, "changed_cells": before["changed_cells"], "snapshots": before["snapshots"], "mode": "apply" if args.apply else "dry-run", "applied": False, "backup": None, "post_run": None}
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    if args.fingerprint != fingerprint:
        raise SystemExit("exact publication fingerprint mismatch")
    if not args.backup_dir:
        raise SystemExit("apply requires --backup-dir")
    backup_path = Path(args.backup_dir) / f"{runtime.db_path.stem}.vitrina-publication-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    report["backup"] = _backup(runtime.db_path, backup_path)
    with _connect(runtime.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for day, plan_json in before["plans"].items():
                conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?", (plan_json, day))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    after = _publication_payload(runtime.db_path, date_from=args.date_from, date_to=args.date_to)
    after_fp = _hash({"date_from": args.date_from, "date_to": args.date_to, "plans": after["plans"]})
    if after["changed_cells"] or after_fp != _hash({"date_from": args.date_from, "date_to": args.date_to, "plans": before["plans"]}):
        raise SystemExit("post-publication zero-change failed")
    report["applied"] = True
    report["post_run"] = {"changed_cells": 0, "fingerprint": after_fp, "idempotent": True}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
