#!/usr/bin/env python3
"""Publish canonical cost projections into the persisted web-vitrina snapshot.

The canonical engine owns quantities and costs after 2026-07-01.  This runner
only replaces post-cutover cells in the ready snapshot; legacy dates remain
untouched and remain available as audit history.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
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


def _publication_date_column(value: Any, *, date_from: str, date_to: str) -> bool:
    normalized = str(value or "").strip()
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError:
        return False
    return date.fromisoformat(date_from) <= parsed <= date.fromisoformat(date_to)


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


def _semantic_lookup(lookup: dict[int, dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "physical_quantity",
        "paid_equivalent_quantity",
        "recognized_capital_rub",
        "paid_capital_rub",
    )
    return {
        str(nm_id): {
            str(stage): {field: values.get(field) for field in fields}
            for stage, values in sorted((item.get("stages") or {}).items())
        }
        for nm_id, item in sorted(lookup.items())
    }


def _semantic_lookups_conn(
    conn: sqlite3.Connection, available: list[str]
) -> tuple[dict[str, dict[int, dict[str, Any]]], dict[str, Any]]:
    lookups: dict[str, dict[int, dict[str, Any]]] = {}
    semantic: dict[str, Any] = {}
    latest: dict[int, dict[str, Any]] = {}
    for day in available:
        rows = conn.execute(
            """
            SELECT nm_id,stage,physical_quantity,paid_equivalent_quantity,
                   recognized_capital_rub,paid_capital_rub
            FROM sheet_vitrina_v1_canonical_cost_daily_state
            WHERE as_of_date=? ORDER BY nm_id,stage
            """,
            (day,),
        ).fetchall()
        current: dict[int, dict[str, Any]] = {}
        for row in rows:
            item = current.setdefault(int(row["nm_id"]), {"stages": {}})
            item["stages"][str(row["stage"])] = dict(row)
        if current:
            latest = current
        resolved = current or latest
        lookups[day] = resolved
        semantic[day] = _semantic_lookup(resolved)
    return lookups, semantic


def _publication_payload(db_path: Path, *, date_from: str, date_to: str) -> dict[str, Any]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT as_of_date, plan_json FROM sheet_vitrina_v1_ready_snapshots "
            "WHERE as_of_date BETWEEN ? AND ? ORDER BY as_of_date",
            (date_from, date_to),
        ).fetchall()
        if not rows:
            raise ValueError("no ready snapshots exist in the publication range")
        decoded_plans = {str(row[0]): json.loads(row[1]) for row in rows}
        projection_dates = sorted(
            {
                str(value)
                for plan in decoded_plans.values()
                for sheet in plan.get("sheets", [])
                for value in sheet.get("header", [])
                if _publication_date_column(
                    value, date_from=date_from, date_to=date_to
                )
            }
        )
        lookups, semantic_lookups = _semantic_lookups_conn(conn, projection_dates)
    changed_cells = 0
    snapshots: list[dict[str, Any]] = []
    plans: dict[str, str] = {}
    for row in rows:
        day = str(row[0])
        plan = decoded_plans[day]
        changed = 0
        for sheet in plan.get("sheets", []):
            header = sheet.get("header", [])
            date_columns = [
                (index, str(value))
                for index, value in enumerate(header)
                if _publication_date_column(
                    value, date_from=date_from, date_to=date_to
                )
            ]
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
                for index, projection_date in date_columns:
                    value = _value_for_metric(
                        metric_name, nm_id, lookups.get(projection_date, {})
                    )
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
    snapshot_inputs = {
        str(row[0]): "sha256:" + hashlib.sha256(str(row[1]).encode()).hexdigest()
        for row in rows
    }
    return {
        "snapshots": snapshots,
        "plans": plans,
        "changed_cells": changed_cells,
        "snapshot_input_digest": "sha256:" + _hash(snapshot_inputs),
        "canonical_input_digest": "sha256:" + _hash(semantic_lookups),
        "published_output_digest": "sha256:" + _hash(plans),
    }


def build_publication_report(
    db_path: Path,
    *,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    payload = _publication_payload(db_path, date_from=date_from, date_to=date_to)
    approval = {
        "contract_name": "canonical_cost_engine_vitrina_publication_v2",
        "date_from": date_from,
        "date_to": date_to,
        "snapshot_input_digest": payload["snapshot_input_digest"],
        "canonical_input_digest": payload["canonical_input_digest"],
        "published_output_digest": payload["published_output_digest"],
        "snapshots": payload["snapshots"],
        "changed_cells": payload["changed_cells"],
    }
    return {
        **approval,
        "fingerprint": "sha256:" + _hash(approval),
        "plans": payload["plans"],
    }


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
    return {"path": str(destination), "mode": f"{destination.stat().st_mode & 0o777:04o}", "integrity": integrity, "sha256": digest, "size": destination.stat().st_size}


def _restore_backup(backup: Path, destination: Path) -> dict[str, Any]:
    inode = destination.stat().st_ino
    with closing(sqlite3.connect(backup)) as source, closing(
        sqlite3.connect(destination, timeout=60)
    ) as target:
        source.backup(target)
        target.commit()
    with closing(
        sqlite3.connect(f"file:{destination.resolve()}?mode=ro", uri=True)
    ) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if destination.stat().st_ino != inode or integrity.lower() != "ok":
        raise ValueError("post-publication restore verification failed")
    return {"inode_preserved": True, "integrity": "ok", "path": str(destination)}


def apply_publication(
    db_path: Path,
    *,
    date_from: str,
    date_to: str,
    fingerprint: str,
    backup_dir: Path,
) -> dict[str, Any]:
    before = build_publication_report(
        db_path, date_from=date_from, date_to=date_to
    )
    if str(fingerprint) != str(before["fingerprint"]):
        raise ValueError("exact publication fingerprint mismatch")
    backup_path = Path(backup_dir) / (
        f"{db_path.stem}.vitrina-publication-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    )
    backup = _backup(db_path, backup_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current_rows = conn.execute(
                "SELECT as_of_date,plan_json FROM sheet_vitrina_v1_ready_snapshots "
                "WHERE as_of_date BETWEEN ? AND ? ORDER BY as_of_date",
                (date_from, date_to),
            ).fetchall()
            current_input = "sha256:" + _hash(
                {
                    str(row[0]): "sha256:"
                    + hashlib.sha256(str(row[1]).encode()).hexdigest()
                    for row in current_rows
                }
            )
            if current_input != before["snapshot_input_digest"]:
                raise ValueError("publication snapshot input drift")
            projection_dates = sorted(
                {
                    str(value)
                    for row in current_rows
                    for sheet in json.loads(row[1]).get("sheets", [])
                    for value in sheet.get("header", [])
                    if _publication_date_column(
                        value, date_from=date_from, date_to=date_to
                    )
                }
            )
            _, semantic_lookups = _semantic_lookups_conn(conn, projection_dates)
            current_canonical_input = "sha256:" + _hash(semantic_lookups)
            if current_canonical_input != before["canonical_input_digest"]:
                raise ValueError("publication canonical input drift")
            for day, plan_json in before["plans"].items():
                conn.execute(
                    "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?",
                    (plan_json, day),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    try:
        after = build_publication_report(
            db_path, date_from=date_from, date_to=date_to
        )
        if int(after["changed_cells"]) != 0:
            raise ValueError("post-publication zero-change failed")
    except Exception:
        _restore_backup(backup_path, db_path)
        raise
    return {
        **{key: value for key, value in before.items() if key != "plans"},
        "mode": "apply",
        "applied": True,
        "backup": backup,
        "post_run": {
            "changed_cells": 0,
            "idempotent": True,
            "fingerprint": after["fingerprint"],
            "published_output_digest": after["published_output_digest"],
        },
    }


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
    before = build_publication_report(
        runtime.db_path, date_from=args.date_from, date_to=args.date_to
    )
    report = {
        **{key: value for key, value in before.items() if key != "plans"},
        "mode": "apply" if args.apply else "dry-run",
        "applied": False,
        "backup": None,
        "post_run": None,
    }
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    if not args.backup_dir:
        raise SystemExit("apply requires --backup-dir")
    report = apply_publication(
        runtime.db_path,
        date_from=args.date_from,
        date_to=args.date_to,
        fingerprint=args.fingerprint,
        backup_dir=Path(args.backup_dir),
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
