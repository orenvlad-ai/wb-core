"""Publish exact-date promo archive replay into accepted slots and ready plans.

The production launcher owns one-submit semantics. This adapter owns promo-only
source evidence, bounded cell publication, and an atomic readback ledger.
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from apps.production_apply_contract import AdapterError
from packages.application.promo_campaign_archive import (
    DailyPriceTruthResolution,
    PromoCampaignArchiveSyncSummary,
    load_promo_campaign_archive,
    materialize_promo_result_from_archive,
    promo_archive_fence,
)
from packages.application.ready_publication import (
    ExpectedReady,
    check_authority,
    operational_authority,
    replace_ready,
)
from packages.application.sheet_vitrina_v1 import _column_name
from packages.application.root_storage_policy import (
    admit_root_write,
    storage_destination_root,
)

SOURCE = "promo_by_price"
ROLE_CLOSED = "accepted_closed_day_snapshot"
ROLE_CURRENT = "accepted_current_snapshot"
SKU_METRICS = ("promo_participation", "promo_count_by_price", "promo_entry_price_best")
TOTAL_METRICS = {
    "promo_participation": "TOTAL|total_promo_participation",
    "promo_count_by_price": "TOTAL|total_promo_count_by_price",
    "promo_entry_price_best": "TOTAL|avg_promo_entry_price_best",
}
LEDGER = "promo_archive_publication_runs"
ROLLBACK_LEDGER = "promo_archive_publication_rollbacks"
BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
SCOPED_BACKUP_TABLES = (
    "temporal_source_slot_snapshots",
    "temporal_source_snapshots",
    "sheet_vitrina_v1_ready_snapshots",
)
SCOPED_BACKUP_MANIFEST = "promo_archive_scoped_backup_manifest"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _connect(db_path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _request(request: dict[str, Any]) -> tuple[Path, list[str], dict[str, dict[str, str]]]:
    if not {"runtime_dir", "dates"}.issubset(request) or set(request) - {"runtime_dir", "dates", "reconstruction"}:
        raise AdapterError("promo-request-fields-invalid")
    runtime = Path(str(request["runtime_dir"])).resolve()
    if not runtime.is_absolute() or not runtime.is_dir():
        raise AdapterError("promo-runtime-invalid")
    dates = request["dates"]
    if not isinstance(dates, list) or not 1 <= len(dates) <= 7 or any(type(day) is not str for day in dates) or dates != sorted(set(dates)):
        raise AdapterError("promo-dates-invalid")
    try:
        if any(date.fromisoformat(day).isoformat() != day for day in dates):
            raise ValueError
    except (ValueError, TypeError):
        raise AdapterError("promo-dates-invalid") from None
    if dates[-1] > datetime.now(BUSINESS_TIMEZONE).date().isoformat():
        raise AdapterError("promo-future-date-invalid")
    reconstruction = request.get("reconstruction", {})
    if (not isinstance(reconstruction, dict) or not set(reconstruction).issubset(dates)
            or any(not isinstance(value, dict) or set(value) != {"identity_run", "price_checkpoint_id"}
                   or any(type(part) is not str or not part for part in value.values())
                   for value in reconstruction.values())):
        raise AdapterError("promo-reconstruction-request-invalid")
    return runtime, dates, reconstruction


def _ledger_row(conn: sqlite3.Connection, operation_id: str) -> sqlite3.Row | None:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (LEDGER,)).fetchone()
    return conn.execute(f"SELECT * FROM {LEDGER} WHERE operation_id=?", (operation_id,)).fetchone() if exists else None


def _source_fingerprint(runtime: Path, dates: list[str]) -> str:
    """Pin all campaign identities and exact-day discovery files plus covering bytes."""
    records = load_promo_campaign_archive(runtime)
    source: list[Any] = [
        (record.archive_key, record.metadata_fingerprint, record.workbook_fingerprint, record.collected_at)
        for record in records
    ]
    for record in records:
        start, end = record.metadata.promo_start_at, record.metadata.promo_end_at
        if not start or not end or not any(start[:10] <= day <= end[:10] for day in dates):
            continue
        archive_dir = Path(record.archive_dir)
        for name in ("archive_record.json", "metadata.json", "campaign_rows_manifest.json", "campaign_rows.jsonl", "workbook.xlsx"):
            path = archive_dir / name
            if path.is_file():
                source.append((str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
    runs_root = runtime / "promo_xlsx_collector_runs"
    for day in dates:
        for path in sorted(runs_root.glob(f"{day}__*/run_summary.json")):
            source.append((str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
    return _digest(source)


def _status_write_rect(status: dict[str, Any]) -> str:
    header = status.get("header")
    columns = status.get("column_count")
    start = status.get("write_start_cell")
    if not isinstance(header, list) or type(columns) is not int or columns != len(header) or not isinstance(start, str) or not start:
        raise AdapterError("promo-ready-status-layout-invalid")
    return f"{start}:{_column_name(columns)}{len(status.get('rows') or []) + 1}"


def _require_status_shape(status: dict[str, Any]) -> None:
    if status.get("row_count") != len(status.get("rows") or []):
        raise AdapterError("promo-ready-status-row-count-mismatch")
    if status.get("write_rect") != _status_write_rect(status):
        raise AdapterError("promo-ready-status-write-rect-mismatch")


def _set_status_shape(status: dict[str, Any]) -> None:
    status["row_count"] = len(status.get("rows") or [])
    status["write_rect"] = _status_write_rect(status)


def _plan_non_target(plan: dict[str, Any], dates: set[str]) -> str:
    value = deepcopy(plan)
    columns = set(index for index, day in enumerate(value.get("date_columns") or []) if day in dates)
    for sheet in value.get("sheets") or []:
        if sheet.get("sheet_name") == "DATA_VITRINA":
            for row in sheet.get("rows") or []:
                if len(row) > 1 and (
                    str(row[1]).startswith("SKU:") and str(row[1]).rsplit("|", 1)[-1] in SKU_METRICS
                    or row[1] in TOTAL_METRICS.values()
                ):
                    for index in columns:
                        if 2 + index < len(row):
                            row[2 + index] = "<target>"
        elif sheet.get("sheet_name") == "STATUS":
            target_slots = {str(item.get("slot_key")) for item in value.get("temporal_slots") or []
                            if item.get("column_date") in dates}
            original_rows = sheet.get("rows") or []
            sheet["rows"] = [row for row in sheet.get("rows") or []
                             if not (row and str(row[0]) in {f"{SOURCE}[{slot}]" for slot in target_slots})]
            if type(sheet.get("row_count")) is not int:
                raise AdapterError("promo-ready-status-row-count-invalid")
            removed = len(original_rows) - len(sheet["rows"])
            sheet["row_count"] -= removed
            match = re.fullmatch(r"([A-Z]+[0-9]+:[A-Z]+)([0-9]+)", str(sheet.get("write_rect") or ""))
            if match is None:
                raise AdapterError("promo-ready-status-write-rect-invalid")
            sheet["write_rect"] = match.group(1) + str(int(match.group(2)) - removed)
    refresh = (value.get("metadata") or {}).get("refresh_diagnostics") or {}
    if "source_slots" in refresh:
        refresh["source_slots"] = [slot for slot in refresh["source_slots"]
                                   if not (slot.get("source_key") == SOURCE and slot.get("requested_date") in dates)]
    if "source_summary" in refresh:
        refresh["source_summary"] = [summary for summary in refresh["source_summary"]
                                     if summary.get("source_key") != SOURCE]
    return _digest(value)


def _plan_target(plan: dict[str, Any], dates: set[str]) -> dict[str, Any]:
    columns = {index: day for index, day in enumerate(plan.get("date_columns") or []) if day in dates}
    slots = list(plan.get("temporal_slots") or [])
    target: dict[str, Any] = {"dates": columns, "data": [], "status": [], "source_slots": [], "source_summary": []}
    for sheet in plan.get("sheets") or []:
        if sheet.get("sheet_name") == "DATA_VITRINA":
            for row in sheet.get("rows") or []:
                if len(row) > 1 and (
                    str(row[1]).startswith("SKU:") and str(row[1]).rsplit("|", 1)[-1] in SKU_METRICS
                    or row[1] in TOTAL_METRICS.values()
                ):
                    target["data"].append((row[1], [(day, row[2 + index]) for index, day in columns.items()]))
        elif sheet.get("sheet_name") == "STATUS":
            target_slots = {str(slots[index].get("slot_key")) for index in columns}
            target["status"] = [row for row in sheet.get("rows") or [] if row and row[0] in {f"{SOURCE}[{slot}]" for slot in target_slots}]
    refresh = (plan.get("metadata") or {}).get("refresh_diagnostics") or {}
    target["source_slots"] = [slot for slot in refresh.get("source_slots") or [] if slot.get("source_key") == SOURCE and slot.get("requested_date") in dates]
    target["source_summary"] = [summary for summary in refresh.get("source_summary") or [] if summary.get("source_key") == SOURCE]
    return target


def _target_image(conn: sqlite3.Connection, *, bundle: str, dates: list[str], ready_asofs: list[str], roles: dict[str, list[str]]) -> dict[str, Any]:
    slots = {}
    exact = {}
    for day in dates:
        slots[day] = {}
        for role in roles[day]:
            row = conn.execute("SELECT payload_json FROM temporal_source_slot_snapshots WHERE source_key=? AND snapshot_date=? AND snapshot_role=?", (SOURCE, day, role)).fetchone()
            slots[day][role] = json.loads(row[0]) if row else None
        row = conn.execute("SELECT payload_json FROM temporal_source_snapshots WHERE source_key=? AND snapshot_date=?", (SOURCE, day)).fetchone()
        exact[day] = json.loads(row[0]) if row else None
    plans = {}
    for as_of in ready_asofs:
        row = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?", (bundle, as_of)).fetchone()
        if row is None:
            raise AdapterError("promo-ready-readback-missing")
        plan = json.loads(row[0])
        plans[as_of] = _plan_target(plan, set(dates))
    return {"slots": slots, "exact": exact, "ready": plans}


def _expected_target_image(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "slots": {day: {role: candidate["results"][day] for role in candidate["roles"][day]} for day in candidate["dates"]},
        "exact": {day: candidate["results"][day] for day in candidate["dates"]},
        "ready": {update["as_of_date"]: _plan_target(json.loads(update["plan_json"]), set(candidate["dates"]))
                  for update in candidate["ready_updates"]},
    }


def _restore_plan_target(current: dict[str, Any], old: dict[str, Any], dates: set[str]) -> dict[str, Any]:
    restored = deepcopy(current)
    if current.get("date_columns") != old.get("date_columns") or current.get("temporal_slots") != old.get("temporal_slots"):
        raise AdapterError("promo-rollback-plan-layout-drift")
    columns = [index for index, day in enumerate(current.get("date_columns") or []) if day in dates]
    sheets_now = {sheet.get("sheet_name"): sheet for sheet in restored.get("sheets") or []}
    sheets_old = {sheet.get("sheet_name"): sheet for sheet in old.get("sheets") or []}
    for name in ("DATA_VITRINA", "STATUS"):
        if name not in sheets_now or name not in sheets_old:
            raise AdapterError("promo-rollback-ready-sheet-missing")
    old_data = {str(row[1]): row for row in sheets_old["DATA_VITRINA"].get("rows") or [] if len(row) > 1}
    for row in sheets_now["DATA_VITRINA"].get("rows") or []:
        if len(row) <= 1:
            continue
        row_id = str(row[1])
        if not (row_id.startswith("SKU:") and row_id.rsplit("|", 1)[-1] in SKU_METRICS or row_id in TOTAL_METRICS.values()):
            continue
        before = old_data.get(row_id)
        if before is None:
            raise AdapterError("promo-rollback-old-row-missing")
        for index in columns:
            row[2 + index] = before[2 + index]
    old_status = {str(row[0]): row for row in sheets_old["STATUS"].get("rows") or [] if row}
    target_status_ids = {f"{SOURCE}[{current['temporal_slots'][index]['slot_key']}]" for index in columns}
    sheets_now["STATUS"]["rows"] = [row for row in sheets_now["STATUS"].get("rows") or []
                                    if not (row and str(row[0]) in target_status_ids)]
    sheets_now["STATUS"]["rows"].extend(deepcopy(row) for row in sheets_old["STATUS"].get("rows") or []
                                         if row and str(row[0]) in target_status_ids)
    _set_status_shape(sheets_now["STATUS"])
    refresh_now = (restored.get("metadata") or {}).get("refresh_diagnostics") or {}
    refresh_old = (old.get("metadata") or {}).get("refresh_diagnostics") or {}
    refresh_now["source_slots"] = [slot for slot in refresh_now.get("source_slots") or []
                                   if not (slot.get("source_key") == SOURCE and slot.get("requested_date") in dates)]
    refresh_now["source_slots"].extend(deepcopy(slot) for slot in refresh_old.get("source_slots") or []
                                       if slot.get("source_key") == SOURCE and slot.get("requested_date") in dates)
    refresh_now["source_summary"] = [item for item in refresh_now.get("source_summary") or [] if item.get("source_key") != SOURCE]
    refresh_now["source_summary"].extend(deepcopy(item) for item in refresh_old.get("source_summary") or []
                                         if item.get("source_key") == SOURCE)
    return restored



def _ready_metadata(conn: sqlite3.Connection, *, bundle: str, ready_asofs: list[str]) -> dict[str, tuple[Any, ...]]:
    values: dict[str, tuple[Any, ...]] = {}
    for as_of in ready_asofs:
        row = conn.execute("SELECT activated_at,snapshot_id,plan_version,refreshed_at FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?", (bundle, as_of)).fetchone()
        if row is None:
            raise AdapterError("promo-ready-metadata-missing")
        values[as_of] = tuple(row)
    return values


def _after_row_timestamps_match(conn: sqlite3.Connection, *, dates: list[str], roles: dict[str, list[str]],
                                captured_at: str) -> bool:
    for day in dates:
        for role in roles[day]:
            row = conn.execute("SELECT captured_at FROM temporal_source_slot_snapshots WHERE source_key=? AND snapshot_date=? AND snapshot_role=?", (SOURCE, day, role)).fetchone()
            if row is None or row[0] != captured_at:
                return False
        row = conn.execute("SELECT captured_at FROM temporal_source_snapshots WHERE source_key=? AND snapshot_date=?", (SOURCE, day)).fetchone()
        if row is None or row[0] != captured_at:
            return False
    return True

def _source_rows(conn: sqlite3.Connection, *, dates: list[str], roles: dict[str, list[str]]) -> dict[str, dict[str, tuple[Any, ...] | None]]:
    slots = {}
    exact = {}
    for day in dates:
        slots[day] = {}
        for role in roles[day]:
            row = conn.execute("SELECT source_key,snapshot_date,snapshot_role,captured_at,payload_json FROM temporal_source_slot_snapshots WHERE source_key=? AND snapshot_date=? AND snapshot_role=?", (SOURCE, day, role)).fetchone()
            slots[day][role] = tuple(row) if row else None
        row = conn.execute("SELECT source_key,snapshot_date,captured_at,payload_json FROM temporal_source_snapshots WHERE source_key=? AND snapshot_date=?", (SOURCE, day)).fetchone()
        exact[day] = tuple(row) if row else None
    return {"slots": slots, "exact": exact}


def _scoped_backup_rows(conn: sqlite3.Connection, candidate: dict[str, Any]) -> dict[str, list[tuple[Any, ...]]]:
    """Retain full rows for exactly the keys this publication can change."""
    dates = candidate["dates"]
    roles = candidate["roles"]
    rows: dict[str, list[tuple[Any, ...]]] = {table: [] for table in SCOPED_BACKUP_TABLES}
    for day in dates:
        for role in sorted(roles[day]):
            row = conn.execute("SELECT * FROM temporal_source_slot_snapshots WHERE source_key=? AND snapshot_date=? AND snapshot_role=?",
                               (SOURCE, day, role)).fetchone()
            if row is not None:
                rows["temporal_source_slot_snapshots"].append(tuple(row))
        row = conn.execute("SELECT * FROM temporal_source_snapshots WHERE source_key=? AND snapshot_date=?",
                           (SOURCE, day)).fetchone()
        if row is not None:
            rows["temporal_source_snapshots"].append(tuple(row))
    for as_of in sorted(update["as_of_date"] for update in candidate["ready_updates"]):
        row = conn.execute("SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?",
                           (candidate["bundle"], as_of)).fetchone()
        if row is None:
            raise AdapterError("promo-scoped-backup-ready-missing")
        rows["sheet_vitrina_v1_ready_snapshots"].append(tuple(row))
    return rows


def _scoped_backup_rows_from_file(conn: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY " + order)]
        for table, order in (
            ("temporal_source_slot_snapshots", "source_key,snapshot_date,snapshot_role"),
            ("temporal_source_snapshots", "source_key,snapshot_date"),
            ("sheet_vitrina_v1_ready_snapshots", "bundle_version,as_of_date"),
        )
    }


def _verify_scoped_backup(conn: sqlite3.Connection, *, operation_id: str,
                          before_target_sha: str, candidate_sha: str | None = None) -> dict[str, Any]:
    row = conn.execute(f"SELECT manifest_json FROM {SCOPED_BACKUP_MANIFEST}").fetchone()
    if row is None:
        raise AdapterError("promo-scoped-backup-manifest-missing")
    manifest = json.loads(row[0])
    if (manifest.get("schema") != "wb_core_promo_scoped_backup_v1"
            or manifest.get("operation_id") != operation_id
            or manifest.get("before_target_sha256") != before_target_sha
            or candidate_sha is not None and manifest.get("candidate_sha256") != candidate_sha):
        raise AdapterError("promo-scoped-backup-manifest-mismatch")
    rows = _scoped_backup_rows_from_file(conn)
    if (manifest.get("row_counts") != {table: len(items) for table, items in rows.items()}
            or manifest.get("rows_sha256") != _digest(rows)):
        raise AdapterError("promo-scoped-backup-rows-mismatch")
    target_keys = manifest.get("target_keys") or {}
    if (any(row[0] != SOURCE for row in rows["temporal_source_slot_snapshots"] + rows["temporal_source_snapshots"])
            or any(row[0] != manifest["bundle_version"] for row in rows["sheet_vitrina_v1_ready_snapshots"])):
        raise AdapterError("promo-scoped-backup-other-source-row")
    if (target_keys.get("slots") != [[day, role] for day in manifest["dates"] for role in sorted(manifest["roles"][day])]
            or target_keys.get("exact") != manifest["dates"]
            or target_keys.get("ready") != manifest["ready_asofs"]
            or manifest.get("present_keys") != {
                "slots": [[row[1], row[2]] for row in rows["temporal_source_slot_snapshots"]],
                "exact": [row[1] for row in rows["temporal_source_snapshots"]],
                "ready": [row[2] for row in rows["sheet_vitrina_v1_ready_snapshots"]],
            }):
        raise AdapterError("promo-scoped-backup-key-scope-mismatch")
    if (not set(map(tuple, manifest["present_keys"]["slots"])).issubset(set(map(tuple, target_keys["slots"])))
            or not set(manifest["present_keys"]["exact"]).issubset(set(target_keys["exact"]))
            or manifest["present_keys"]["ready"] != target_keys["ready"]):
        raise AdapterError("promo-scoped-backup-other-target-row")
    if _digest(_target_image(conn, bundle=manifest["bundle_version"], dates=manifest["dates"],
                             ready_asofs=manifest["ready_asofs"], roles=manifest["roles"])) != before_target_sha:
        raise AdapterError("promo-scoped-backup-target-mismatch")
    return manifest


def _create_scoped_backup(candidate: dict[str, Any], operation_id: str, backup_path: Path,
                          *, production: bool) -> tuple[str, str, str]:
    """Persist an attested, transaction-consistent preimage with no other source rows."""
    with closing(_connect(candidate["db_path"], readonly=True)) as source:
        source.execute("BEGIN")
        rows = _scoped_backup_rows(source, candidate)
        before_target_sha = _digest(_target_image(
            source, bundle=candidate["bundle"], dates=candidate["dates"],
            ready_asofs=[update["as_of_date"] for update in candidate["ready_updates"]],
            roles=candidate["roles"],
        ))
        schemas = {}
        for table in SCOPED_BACKUP_TABLES:
            row = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if row is None or not row[0]:
                raise AdapterError(f"promo-scoped-backup-schema-missing:{table}")
            schemas[table] = str(row[0])
    row_bytes = sum(len(str(value).encode("utf-8")) for items in rows.values() for row in items for value in row)
    predicted_bytes = 16 * 1024 * 1024 + 4 * row_bytes
    if production:
        admit_root_write(owner="promo_archive_publication", destination=backup_path,
                         predicted_output_bytes=predicted_bytes)
    manifest = {
        "schema": "wb_core_promo_scoped_backup_v1", "operation_id": operation_id,
        "source_path": str(candidate["db_path"]), "bundle_version": candidate["bundle"],
        "dates": candidate["dates"], "ready_asofs": sorted(update["as_of_date"] for update in candidate["ready_updates"]),
        "roles": candidate["roles"], "prestate_sha256": candidate["prestate_sha"],
        "candidate_sha256": candidate["candidate_sha"], "before_target_sha256": before_target_sha,
        "row_counts": {table: len(items) for table, items in rows.items()},
        "target_keys": {
            "slots": [[day, role] for day in candidate["dates"] for role in sorted(candidate["roles"][day])],
            "exact": candidate["dates"],
            "ready": sorted(update["as_of_date"] for update in candidate["ready_updates"]),
        },
        "present_keys": {
            "slots": [[row[1], row[2]] for row in rows["temporal_source_slot_snapshots"]],
            "exact": [row[1] for row in rows["temporal_source_snapshots"]],
            "ready": [row[2] for row in rows["sheet_vitrina_v1_ready_snapshots"]],
        },
        "rows_sha256": _digest(rows), "created_at": datetime.now(timezone.utc).isoformat(),
    }
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(str(backup_path))) as backup:
            backup.execute("BEGIN IMMEDIATE")
            for table in SCOPED_BACKUP_TABLES:
                backup.execute(schemas[table])
                items = rows[table]
                if items:
                    placeholders = ",".join("?" for _ in items[0])
                    backup.executemany(f"INSERT INTO {table} VALUES({placeholders})", items)
            backup.execute(f"CREATE TABLE {SCOPED_BACKUP_MANIFEST}(manifest_json TEXT NOT NULL)")
            backup.execute(f"INSERT INTO {SCOPED_BACKUP_MANIFEST} VALUES(?)",
                           (json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),))
            backup.commit()
        if backup_path.stat().st_size > predicted_bytes:
            raise AdapterError("promo-scoped-backup-prediction-exceeded")
        with closing(_connect(backup_path, readonly=True)) as backup:
            _verify_scoped_backup(backup, operation_id=operation_id,
                                  before_target_sha=before_target_sha,
                                  candidate_sha=candidate["candidate_sha"])
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return before_target_sha, manifest["rows_sha256"], "sha256:" + hashlib.sha256(backup_path.read_bytes()).hexdigest()


def _update_plan(plan: dict[str, Any], day_results: dict[str, dict[str, Any]], dates: set[str]) -> dict[str, int]:
    changes = {metric: 0 for metric in SKU_METRICS}
    changes["TOTAL"] = 0
    sheets = {sheet.get("sheet_name"): sheet for sheet in plan.get("sheets") or []}
    data = sheets.get("DATA_VITRINA")
    status = sheets.get("STATUS")
    if data is None or status is None:
        raise AdapterError("promo-ready-sheets-missing")
    _require_status_shape(status)
    rows = {str(row[1]): row for row in data.get("rows") or [] if len(row) > 1}
    status_rows = {str(row[0]): row for row in status.get("rows") or [] if row}
    for index, day in enumerate(plan.get("date_columns") or []):
        if day not in dates:
            continue
        result = day_results[day]
        items = {int(item["nm_id"]): item for item in result["items"]}
        cell_index = 2 + index
        for metric in SKU_METRICS:
            values: list[float] = []
            for nm_id, item in items.items():
                row = rows.get(f"SKU:{nm_id}|{metric}")
                if row is None or cell_index >= len(row):
                    raise AdapterError("promo-ready-sku-row-missing")
                new = float(item[metric])
                values.append(new)
                if row[cell_index] != new:
                    row[cell_index] = new
                    changes[metric] += 1
            total = sum(values) if metric != "promo_entry_price_best" else sum(values) / len(values)
            total = round(total, 6)
            row = rows.get(TOTAL_METRICS[metric])
            if row is None or cell_index >= len(row):
                raise AdapterError("promo-ready-total-row-missing")
            if row[cell_index] != total:
                row[cell_index] = total
                changes["TOTAL"] += 1
        slot = (plan.get("temporal_slots") or [])[index]
        slot_kind = slot.get("slot_key")
        status_row = status_rows.get(f"{SOURCE}[{slot_kind}]")
        if status_row is None:
            status_row = [f"{SOURCE}[{slot_kind}]"]
            status.setdefault("rows", []).append(status_row)
            _set_status_shape(status)
            status_rows[str(status_row[0])] = status_row
        while len(status_row) < 11:
            status_row.append("")
        reconstruction = result.get("diagnostics", {}).get("historical_reconstruction")
        origin = "historical_composite_reconstruction" if reconstruction else "archive_replay"
        status_row[1:11] = ["success", day, day, "", day, day, len(items), len(items), "", result["detail"] + "; publication=" + origin]
        refresh = plan.setdefault("metadata", {}).setdefault("refresh_diagnostics", {})
        source_slots = refresh.setdefault("source_slots", [])
        if not any(slot.get("source_key") == SOURCE and slot.get("slot_kind") == slot_kind and slot.get("requested_date") == day for slot in source_slots):
            source_slots.append({"source_key": SOURCE, "slot_kind": slot_kind, "requested_date": day})
        for source_slot in source_slots:
            if source_slot.get("source_key") == SOURCE and source_slot.get("slot_kind") == slot_kind and source_slot.get("requested_date") == day:
                source_slot.update(status="success", semantic_status="success", origin=origin,
                                   started_at=None, finished_at=None, duration_ms=None,
                                   rows_fetched=0, rows_accepted=len(items), rows_reused=0, rows_skipped=0,
                                   requested_count=len(items), covered_count=len(items), missing_count=0,
                                   counter_basis="archive_replay_materialized_rows; no upstream fetch",
                                   note_kind=("promo_historical_composite_reconstruction" if reconstruction else "promo_archive_replay"),
                                   promo_diagnostics=result.get("diagnostics"))
        source_summaries = refresh.setdefault("source_summary", [])
        if not any(summary.get("source_key") == SOURCE for summary in source_summaries):
            source_summaries.append({"source_key": SOURCE})
        for summary in source_summaries:
            if summary.get("source_key") == SOURCE:
                matching = [s for s in refresh.get("source_slots") or [] if s.get("source_key") == SOURCE]
                summary["slot_count"] = len(matching)
                summary["duration_ms"] = sum(int(entry.get("duration_ms") or 0) for entry in matching)
                for field in ("status", "origin"):
                    summary[f"{field}_counts"] = {
                        value: sum(1 for entry in matching if str(entry.get(field) or "") == value)
                        for value in sorted({str(entry.get(field) or "") for entry in matching})
                    }
                for field in ("rows_fetched", "rows_accepted", "rows_reused", "rows_skipped"):
                    summary[field] = sum(int(entry.get(field) or 0) for entry in matching)
    return changes



def _reconstruction_run(runtime: Path, day: str, run_name: str) -> tuple[Path, dict[str, Any], datetime]:
    if not run_name.startswith(day + "__") or "/" in run_name or ".." in run_name:
        raise AdapterError(f"promo-reconstruction-run-invalid:{day}")
    path = runtime / "promo_xlsx_collector_runs" / run_name / "run_summary.json"
    if not path.is_file():
        raise AdapterError(f"promo-reconstruction-run-missing:{day}")
    summary = json.loads(path.read_text(encoding="utf-8"))
    if Path(str(summary.get("run_dir") or "")).resolve() != path.parent.resolve():
        raise AdapterError(f"promo-reconstruction-run-identity-drift:{day}")
    observed = datetime.fromisoformat(str(summary.get("started_at") or ""))
    if observed.tzinfo is None or observed.astimezone(BUSINESS_TIMEZONE).date().isoformat() != day:
        raise AdapterError(f"promo-reconstruction-run-date-invalid:{day}")
    return path, summary, observed


def _reconstruction_artifact_proof(runtime: Path, day: str, run_path: Path, summary: dict[str, Any], observed: datetime) -> str:
    """Pin original run metadata and pre-observation archive workbooks."""
    run_items = {item.get("promo_id"): item for item in summary.get("promos") or []
                 if isinstance(item, dict) and type(item.get("promo_id")) is int}
    proof: list[Any] = []
    for record in load_promo_campaign_archive(runtime):
        metadata = record.metadata
        if not (metadata.promo_start_at and metadata.promo_end_at and metadata.promo_start_at[:10] <= day <= metadata.promo_end_at[:10]
                and record.workbook_present):
            continue
        item = run_items.get(metadata.promo_id)
        if item is None or item.get("status") not in {"reused_archive", "downloaded"}:
            raise AdapterError(f"promo-reconstruction-workbook-not-in-run:{day}:{metadata.promo_id}")
        workbook = Path(str(record.workbook_path or ""))
        raw_path = Path(str(item.get("metadata_path") or ""))
        if (not workbook.is_file() or Path(str(item.get("saved_path") or "")).resolve() != workbook.resolve()
                or raw_path.parent.parent.parent.resolve() != run_path.parent.resolve()
                or not raw_path.is_file()):
            raise AdapterError(f"promo-reconstruction-artifact-path-invalid:{day}:{metadata.promo_id}")
        reuse_path = raw_path.parent / "archive_reuse.json"
        if item.get("status") == "reused_archive":
            if not reuse_path.is_file():
                raise AdapterError(f"promo-reconstruction-reuse-proof-missing:{day}:{metadata.promo_id}")
            reuse_bytes = reuse_path.read_bytes()
            reuse = json.loads(reuse_bytes)
            downloaded_at = datetime.fromisoformat(str(reuse.get("downloaded_at") or ""))
            if (reuse.get("archive_key") != record.archive_key
                    or Path(str(reuse.get("reused_workbook_path") or "")).resolve() != workbook.resolve()
                    or downloaded_at.tzinfo is None or downloaded_at.astimezone(timezone.utc) > observed.astimezone(timezone.utc)):
                raise AdapterError(f"promo-reconstruction-reuse-proof-invalid:{day}:{metadata.promo_id}")
        else:
            reuse_bytes = b""
        raw_bytes = raw_path.read_bytes()
        raw = json.loads(raw_bytes)
        if any(raw.get(field) != getattr(metadata, field) for field in ("promo_id", "promo_start_at", "promo_end_at", "promo_status")):
            raise AdapterError(f"promo-reconstruction-artifact-identity-drift:{day}:{metadata.promo_id}")
        if datetime.fromtimestamp(workbook.stat().st_mtime, tz=timezone.utc) > observed.astimezone(timezone.utc):
            raise AdapterError(f"promo-reconstruction-workbook-too-new:{day}:{metadata.promo_id}")
        proof.append((metadata.promo_id, hashlib.sha256(raw_bytes).hexdigest(),
                      hashlib.sha256(reuse_bytes).hexdigest(), hashlib.sha256(workbook.read_bytes()).hexdigest()))
    if not proof:
        raise AdapterError(f"promo-reconstruction-no-usable-artifacts:{day}")
    return _digest(sorted(proof))

def _candidate(runtime: Path, dates: list[str], reconstruction: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    reconstruction = reconstruction or {}
    authority = operational_authority(runtime)
    db_path = authority[0]
    source_sha = _source_fingerprint(runtime, dates)
    with closing(_connect(db_path, readonly=True)) as conn:
        conn.execute("BEGIN")
        state = conn.execute("SELECT bundle_version FROM registry_upload_current_state WHERE slot=1").fetchone()
        if state is None:
            raise AdapterError("promo-current-bundle-missing")
        bundle = str(state[0])
        configured_ids = [int(row[0]) for row in conn.execute(
            "SELECT nm_id FROM registry_upload_config_v2 WHERE bundle_version=? AND enabled=1 ORDER BY nm_id", (bundle,)
        )]
        if not configured_ids:
            raise AdapterError("promo-enabled-skus-empty")
        price_rows = [tuple(row) for row in conn.execute(
            "SELECT * FROM temporal_source_slot_snapshots WHERE source_key='prices_snapshot' AND snapshot_date BETWEEN ? AND ? ORDER BY snapshot_date,snapshot_role",
            (dates[0], dates[-1]),
        )]
        if {row[1] for row in price_rows} != set(dates):
            raise AdapterError("promo-price-truth-missing")
        price_truth: dict[str, DailyPriceTruthResolution] = {}
        price_id_sets: dict[str, set[int]] = {}
        for day in dates:
            matches = [row for row in price_rows if row[1] == day and row[2] == ROLE_CURRENT]
            if len(matches) != 1:
                raise AdapterError(f"promo-accepted-price-slot-missing:{day}")
            payload = json.loads(matches[0][4])
            if payload.get("kind") != "success" or payload.get("snapshot_date") != day or not isinstance(payload.get("items"), list):
                raise AdapterError(f"promo-price-payload-invalid:{day}")
            observed: dict[int, float] = {}
            price_ids: set[int] = set()
            for item in payload["items"]:
                if not isinstance(item, dict) or type(item.get("nm_id")) is not int:
                    raise AdapterError(f"promo-price-identity-invalid:{day}")
                nm_id = item["nm_id"]
                if nm_id <= 0 or nm_id in price_ids:
                    raise AdapterError(f"promo-price-identity-duplicate:{day}")
                price_ids.add(nm_id)
                value = item.get("price_seller_discounted")
                if isinstance(value, (int, float)) and type(value) is not bool:
                    observed[nm_id] = float(value)
            price_truth[day] = DailyPriceTruthResolution(
                price_by_nm_id=observed,
                source_note="daily_price_source=prices_snapshot.accepted_current_snapshot; daily_price_captured_at=" + str(matches[0][3]),
                captured_at=str(matches[0][3]),
                fingerprint=_digest(observed),
            )
            price_id_sets[day] = price_ids
        reconstruction_proof: dict[str, dict[str, Any]] = {}
        for day, spec in reconstruction.items():
            run_path, summary, identity_observed = _reconstruction_run(runtime, day, spec["identity_run"])
            checkpoint = conn.execute(
                "SELECT started_at,completed_at,completeness_status FROM change_registry_checkpoints WHERE checkpoint_id=?",
                (spec["price_checkpoint_id"],),
            ).fetchone()
            manifest = conn.execute(
                "SELECT completeness_status,expected_count,observed_count,evidence_digest FROM change_registry_checkpoint_source_manifests WHERE checkpoint_id=? AND source_name='prices'",
                (spec["price_checkpoint_id"],),
            ).fetchone()
            if checkpoint is None or checkpoint[2] != "complete" or manifest is None or manifest[0] != "complete" or manifest[1] != manifest[2]:
                raise AdapterError(f"promo-reconstruction-price-checkpoint-incomplete:{day}")
            price_observed = datetime.fromisoformat(str(checkpoint[1]).replace("Z", "+00:00"))
            if (price_observed.tzinfo is None or price_observed.astimezone(BUSINESS_TIMEZONE).date().isoformat() != day
                    or price_observed >= identity_observed.astimezone(timezone.utc)):
                raise AdapterError(f"promo-reconstruction-observation-order-invalid:{day}")
            observed_rows = list(conn.execute(
                "SELECT nm_id,observation_status,value_kind,value_integer,observed_at,evidence_digest FROM change_registry_observation_values WHERE checkpoint_id=? AND target_kind='price' AND parameter_field='seller_price_minor' ORDER BY nm_id",
                (spec["price_checkpoint_id"],),
            ))
            checkpoint_prices: dict[int, float] = {}
            for item in observed_rows:
                nm_id, status, kind, minor, observed_at, _evidence = item
                if (type(nm_id) is not int or nm_id <= 0 or nm_id in checkpoint_prices
                        or status not in {"exact", "exact_zero"} or kind != "integer" or type(minor) is not int or minor < 0
                        or datetime.fromisoformat(str(observed_at).replace("Z", "+00:00")) > identity_observed.astimezone(timezone.utc)):
                    raise AdapterError(f"promo-reconstruction-price-observation-invalid:{day}")
                checkpoint_prices[nm_id] = minor / 100.0
            if set(checkpoint_prices) != price_id_sets[day] or len(checkpoint_prices) != manifest[1]:
                raise AdapterError(f"promo-reconstruction-price-sku-scope-mismatch:{day}")
            artifact_sha = _reconstruction_artifact_proof(runtime, day, run_path, summary, identity_observed)
            price_rows_sha = _digest([tuple(row) for row in observed_rows])
            price_truth[day] = DailyPriceTruthResolution(
                price_by_nm_id=checkpoint_prices,
                source_note=f"daily_price_source=change_registry_checkpoint; checkpoint_id={spec['price_checkpoint_id']}; price_observed_at={checkpoint[1]}; identities_observed_at={summary['started_at']}; historical_composite_reconstruction=true",
                captured_at=str(checkpoint[1]), fingerprint=price_rows_sha,
            )
            later_runs = sorted((runtime / "promo_xlsx_collector_runs").glob(f"{day}__*/run_summary.json"))
            later_runs = [path for path in later_runs if path.parent.name > run_path.parent.name]
            latest_later = None
            if later_runs:
                latest_path = later_runs[-1]
                latest_bytes = latest_path.read_bytes()
                latest_summary = json.loads(latest_bytes)
                latest_later = {"run_summary": str(latest_path), "status": latest_summary.get("status"),
                                "started_at": latest_summary.get("started_at"),
                                "blocked_before_card_count": latest_summary.get("blocked_before_card_count"),
                                "unresolved_identity_count": sum(1 for item in latest_summary.get("promos") or []
                                                                 if isinstance(item, dict) and item.get("promo_id") is None),
                                "sha256": "sha256:" + hashlib.sha256(latest_bytes).hexdigest()}
            price_values_sha = "sha256:" + hashlib.sha256(json.dumps(
                [[row[0], row[1], row[3]] for row in observed_rows],
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            reconstruction_proof[day] = {"identity_run": str(run_path), "identity_observed_at": summary["started_at"],
                                         "price_checkpoint_id": spec["price_checkpoint_id"], "price_observed_at": checkpoint[1],
                                         "price_rows_sha256": price_rows_sha, "price_values_sha256": price_values_sha,
                                         "price_manifest_evidence_digest": manifest[3],
                                         "artifact_sha256": artifact_sha, "later_run_count_not_used": len(later_runs),
                                         "latest_later_attempt": latest_later,
                                         "freshness": "historical_composite_observation_only"}
        if not price_id_sets[dates[0]] or any(price_id_sets[day] != price_id_sets[dates[0]] for day in dates):
            raise AdapterError("promo-price-sku-scope-drift")
        ids = sorted(price_id_sets[dates[0]])
        if not set(configured_ids).issubset(ids):
            raise AdapterError("promo-configured-skus-not-in-price-scope")
        old_slots = [tuple(row) for row in conn.execute(
            "SELECT * FROM temporal_source_slot_snapshots WHERE source_key=? AND snapshot_date BETWEEN ? AND ? ORDER BY snapshot_date,snapshot_role",
            (SOURCE, dates[0], dates[-1]),
        )]
        old_exact = [tuple(row) for row in conn.execute(
            "SELECT * FROM temporal_source_snapshots WHERE source_key=? AND snapshot_date BETWEEN ? AND ? ORDER BY snapshot_date",
            (SOURCE, dates[0], dates[-1]),
        )]
        ready_rows = [dict(row) for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? ORDER BY as_of_date", (bundle,)
        )]
    sync = PromoCampaignArchiveSyncSummary()
    results: dict[str, dict[str, Any]] = {}
    for day in dates:
        result = materialize_promo_result_from_archive(
            runtime_dir=runtime, snapshot_date=day, requested_nm_ids=ids,
            sync_summary=sync, trace_run_dir=str(runtime / "promo_campaign_archive"),
            detail_prefix=("publication=historical_composite_reconstruction" if day in reconstruction else "publication=promo_archive_replay"),
            diagnostics={}, price_truth=price_truth[day],
            identity_run_summary=(runtime / "promo_xlsx_collector_runs" / reconstruction[day]["identity_run"] / "run_summary.json") if day in reconstruction else None,
        )
        if result.kind != "success" or result.covered_count != len(ids) or len(result.items) != len(ids):
            raise AdapterError(f"promo-archive-replay-incomplete:{day}")
        payload = asdict(result)
        diagnostics = payload.get("diagnostics") or {}
        payload["diagnostics"] = {
            key: diagnostics[key] for key in (
                "schema_version", "counters", "fingerprints", "artifact_validation_summary",
                "expected_non_materializable_artifacts", "announcement_discovery_evidence",
            ) if key in diagnostics
        }
        if day in reconstruction_proof:
            payload["diagnostics"]["historical_reconstruction"] = reconstruction_proof[day]
        results[day] = payload
    ready_updates = []
    ready_covered_dates: set[str] = set()
    ready_roles: dict[str, set[str]] = {day: set() for day in dates}
    all_changes = {metric: 0 for metric in SKU_METRICS}
    all_changes["TOTAL"] = 0
    non_target: list[str] = []
    for row in ready_rows:
        plan = json.loads(row["plan_json"])
        if not set(plan.get("date_columns") or []).intersection(dates):
            continue
        ready_covered_dates.update(set(plan.get("date_columns") or []).intersection(dates))
        for slot in plan.get("temporal_slots") or []:
            day = slot.get("column_date")
            if day not in ready_roles:
                continue
            slot_key = slot.get("slot_key")
            role = {"today_current": ROLE_CURRENT, "yesterday_closed": ROLE_CLOSED}.get(slot_key)
            if role is None:
                raise AdapterError(f"promo-ready-slot-role-unknown:{row['as_of_date']}:{day}")
            ready_roles[day].add(role)
        data_sheet = next((sheet for sheet in plan.get("sheets") or [] if sheet.get("sheet_name") == "DATA_VITRINA"), None)
        if data_sheet is None:
            raise AdapterError("promo-ready-data-sheet-missing")
        for metric in SKU_METRICS:
            ready_ids = {
                int(str(item[1]).split(":", 1)[1].split("|", 1)[0])
                for item in data_sheet.get("rows") or []
                if len(item) > 1 and str(item[1]).startswith("SKU:") and str(item[1]).endswith("|" + metric)
            }
            if ready_ids != set(ids):
                raise AdapterError(f"promo-ready-sku-scope-mismatch:{row['as_of_date']}:{metric}")
        before_non_target = _plan_non_target(plan, set(dates))
        changes = _update_plan(plan, results, set(dates))
        after_non_target = _plan_non_target(plan, set(dates))
        if before_non_target != after_non_target:
            raise AdapterError("promo-non-target-plan-drift")
        non_target.append(before_non_target)
        for key, count in changes.items():
            all_changes[key] += count
        ready_updates.append({"as_of_date": row["as_of_date"], "old_plan_json": row["plan_json"],
                              "plan_json": json.dumps(plan, ensure_ascii=False, separators=(",", ":")), "changes": changes})
    if ready_covered_dates != set(dates):
        raise AdapterError("promo-ready-date-coverage-incomplete")
    roles = {day: sorted(ready_roles[day]) for day in dates}
    if any(not roles[day] for day in dates):
        raise AdapterError("promo-ready-snapshot-role-missing")
    prestate = _digest({"authority": str(authority), "bundle": bundle, "configured_ids": configured_ids, "ids": ids, "prices": price_rows,
                        "slots": old_slots, "exact": old_exact,
                        "ready": [(row["as_of_date"], row["plan_json"]) for row in ready_rows if row["as_of_date"] in {u["as_of_date"] for u in ready_updates}]})
    source_sha = _digest({"archive_and_runs": source_sha, "reconstruction": reconstruction_proof})
    candidate_sha = _digest({"source": source_sha, "days": results, "roles": roles,
                             "ready": [(u["as_of_date"], u["plan_json"]) for u in ready_updates]})
    candidate = {"authority": authority, "db_path": db_path, "bundle": bundle, "ids": ids,
            "dates": dates, "results": results, "ready_updates": ready_updates,
            "roles": roles, "source_sha": source_sha, "prestate_sha": prestate, "candidate_sha": candidate_sha,
            "non_target_sha": _digest(non_target), "changes": all_changes, "reconstruction_proof": reconstruction_proof}
    candidate["expected_target_sha"] = _digest(_expected_target_image(candidate))
    return candidate


def _later_timestamp(value: str | None, reference: str) -> bool:
    try:
        observed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        earlier = datetime.fromisoformat(reference.replace("Z", "+00:00"))
        return observed.tzinfo is not None and earlier.tzinfo is not None and observed > earlier
    except ValueError:
        return False


def _verified_superseded(conn: sqlite3.Connection, scope: dict[str, Any], before: dict[str, Any],
                         now: dict[str, Any], applied_at: str) -> bool:
    """Recognize newer canonical source/ready captures; never excuse unexplained drift."""
    changed = False
    for day, roles in scope["snapshot_roles"].items():
        for role in roles:
            if _digest(before["slots"][day][role]) == _digest(now["slots"][day][role]):
                continue
            changed = True
            row = conn.execute("SELECT captured_at FROM temporal_source_slot_snapshots WHERE source_key=? AND snapshot_date=? AND snapshot_role=?",
                               (SOURCE, day, role)).fetchone()
            if row is None or not _later_timestamp(row[0], applied_at):
                return False
        if _digest(before["exact"][day]) != _digest(now["exact"][day]):
            changed = True
            row = conn.execute("SELECT captured_at FROM temporal_source_snapshots WHERE source_key=? AND snapshot_date=?",
                               (SOURCE, day)).fetchone()
            if row is None or not _later_timestamp(row[0], applied_at):
                return False
    for as_of in scope["ready_snapshots"]:
        if _digest(before["ready"][as_of]) == _digest(now["ready"][as_of]):
            continue
        changed = True
        row = conn.execute("SELECT refreshed_at FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?",
                           (scope["bundle_version"], as_of)).fetchone()
        if row is None or not _later_timestamp(row[0], applied_at):
            return False
    return changed


class PromoArchivePublicationAdapter:
    def preview(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]:
        runtime, dates, reconstruction = _request(request)
        authority = operational_authority(runtime)
        with closing(_connect(authority[0], readonly=True)) as conn:
            prior = _ledger_row(conn, operation_id)
        if prior is not None:
            if prior["request_sha256"] != _digest(request):
                raise AdapterError("promo-operation-request-mismatch")
            return json.loads(prior["preview_json"])
        candidate = _candidate(runtime, dates, reconstruction)
        return {
            "operation_id": operation_id,
            "target": str(candidate["db_path"]),
            "scope": {"dates": dates, "bundle_version": candidate["bundle"], "enabled_sku_count": len(candidate["ids"]),
                      "ready_snapshots": [u["as_of_date"] for u in candidate["ready_updates"]],
                      "snapshot_roles": candidate["roles"],
                      "metric_cells_changed": candidate["changes"],
                      "source_sha256": candidate["source_sha"], "non_target_sha256": candidate["non_target_sha"],
                      "expected_target_sha256": candidate["expected_target_sha"],
                      "historical_reconstruction": candidate["reconstruction_proof"],
                      "date_totals": {day: {
                          TOTAL_METRICS[metric]: round(
                              sum(float(item[metric]) for item in value["items"]) /
                              (len(value["items"]) if metric == "promo_entry_price_best" else 1), 6
                          ) for metric in SKU_METRICS
                      } for day, value in candidate["results"].items()}},
            "prestate_sha256": candidate["prestate_sha"],
            "candidate_sha256": candidate["candidate_sha"],
            "recovery": {"method": "attested promo-target scoped SQLite backup before one atomic transaction",
                         "rollback": "scoped inverse target CAS from retained backup via apps/promo_archive_publication_rollback.py", "no_change_outside_promo_targets": True},
        }

    def apply(self, request: dict[str, Any], operation_id: str, preview: dict[str, Any]) -> dict[str, Any]:
        runtime, dates, reconstruction = _request(request)
        with promo_archive_fence(runtime):
            candidate = _candidate(runtime, dates, reconstruction)
            if (candidate["prestate_sha"], candidate["candidate_sha"]) != (preview["prestate_sha256"], preview["candidate_sha256"]):
                raise AdapterError("promo-preview-drift")
            backup_root = (
                storage_destination_root("promo_archive_publication")
                if candidate["db_path"].resolve().is_relative_to(Path("/opt/wb-core-runtime/state"))
                else runtime / "backups" / "promo_archive_publication"
            )
            backup_path = backup_root / f"{candidate['db_path'].stem}__promo_archive__{operation_id}.sqlite3"
            if backup_path.exists():
                raise AdapterError("promo-backup-already-exists")
            before_backup_target_sha, before_backup_rows_sha, backup_sha = _create_scoped_backup(
                candidate, operation_id, backup_path,
                production=candidate["db_path"].resolve().is_relative_to(Path("/opt/wb-core-runtime/state")),
            )
            captured_at = datetime.now(timezone.utc).isoformat()
            with closing(_connect(candidate["db_path"], readonly=False)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    check_authority(runtime, candidate["authority"])
                    fresh = _candidate(runtime, dates, reconstruction)
                    if (fresh["prestate_sha"], fresh["candidate_sha"]) != (candidate["prestate_sha"], candidate["candidate_sha"]):
                        raise AdapterError("promo-source-or-target-changed-before-submit")
                    if _digest(_scoped_backup_rows(conn, fresh)) != before_backup_rows_sha:
                        raise AdapterError("promo-scoped-backup-full-row-drift")
                    if _digest(_target_image(conn, bundle=fresh["bundle"], dates=dates,
                                             ready_asofs=[update["as_of_date"] for update in fresh["ready_updates"]],
                                             roles=fresh["roles"])) != before_backup_target_sha:
                        raise AdapterError("promo-scoped-backup-preimage-drift")
                    conn.execute(f"CREATE TABLE IF NOT EXISTS {LEDGER}(operation_id TEXT PRIMARY KEY,request_sha256 TEXT NOT NULL,preview_json TEXT NOT NULL,candidate_sha256 TEXT NOT NULL,before_target_sha256 TEXT NOT NULL,after_target_sha256 TEXT NOT NULL,after_target_json TEXT NOT NULL,after_ready_metadata_json TEXT NOT NULL,applied_at TEXT NOT NULL,backup_path TEXT NOT NULL,backup_sha256 TEXT NOT NULL)")
                    if _ledger_row(conn, operation_id) is not None:
                        raise AdapterError("promo-operation-already-submitted")
                    ready_asofs = [update["as_of_date"] for update in fresh["ready_updates"]]
                    before_target_sha = _digest(_target_image(conn, bundle=fresh["bundle"], dates=dates,
                                                             ready_asofs=ready_asofs, roles=fresh["roles"]))
                    if before_target_sha != before_backup_target_sha:
                        raise AdapterError("promo-scoped-backup-preimage-drift")
                    for day, payload in fresh["results"].items():
                        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                        for role in fresh["roles"][day]:
                            conn.execute("INSERT INTO temporal_source_slot_snapshots(source_key,snapshot_date,snapshot_role,captured_at,payload_json) VALUES(?,?,?,?,?) ON CONFLICT(source_key,snapshot_date,snapshot_role) DO UPDATE SET captured_at=excluded.captured_at,payload_json=excluded.payload_json", (SOURCE, day, role, captured_at, body))
                        conn.execute("INSERT INTO temporal_source_snapshots(source_key,snapshot_date,captured_at,payload_json) VALUES(?,?,?,?) ON CONFLICT(source_key,snapshot_date) DO UPDATE SET captured_at=excluded.captured_at,payload_json=excluded.payload_json", (SOURCE, day, captured_at, body))
                    for update in fresh["ready_updates"]:
                        replace_ready(conn, expected=ExpectedReady(fresh["bundle"], update["as_of_date"], update["old_plan_json"]), plan_json=update["plan_json"])
                    actual_target_image = _target_image(conn, bundle=fresh["bundle"], dates=dates,
                                                         ready_asofs=ready_asofs, roles=fresh["roles"])
                    actual_target_sha = _digest(actual_target_image)
                    if actual_target_sha != fresh["expected_target_sha"]:
                        raise AdapterError("promo-poststate-mismatch-before-commit")
                    replay_source_sha = _source_fingerprint(runtime, dates)
                    replay_proof: dict[str, dict[str, Any]] = {}
                    for day, spec in reconstruction.items():
                        run_path, run_summary, observed = _reconstruction_run(runtime, day, spec["identity_run"])
                        replay_proof[day] = dict(fresh["reconstruction_proof"][day])
                        replay_proof[day]["artifact_sha256"] = _reconstruction_artifact_proof(runtime, day, run_path, run_summary, observed)
                    if _digest({"archive_and_runs": replay_source_sha, "reconstruction": replay_proof}) != fresh["source_sha"]:
                        raise AdapterError("promo-source-changed-during-submit")
                    after_ready_metadata = _ready_metadata(conn, bundle=fresh["bundle"], ready_asofs=ready_asofs)
                    conn.execute(f"INSERT INTO {LEDGER} VALUES(?,?,?,?,?,?,?,?,?,?,?)", (operation_id, _digest(request), json.dumps(preview, ensure_ascii=False), fresh["candidate_sha"], before_target_sha, actual_target_sha, json.dumps(actual_target_image, ensure_ascii=False, separators=(",", ":")), json.dumps(after_ready_metadata, ensure_ascii=False), captured_at, str(backup_path), backup_sha))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        return {"operation_id": operation_id, "disposition": "submitted", "backup_path": str(backup_path)}

    def readback(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]:
        runtime, _dates, _reconstruction = _request(request)
        authority = operational_authority(runtime)
        with closing(_connect(authority[0], readonly=True)) as conn:
            row = _ledger_row(conn, operation_id)
            if row is not None and row["request_sha256"] == _digest(request):
                preview = json.loads(row["preview_json"])
                scope = preview["scope"]
                actual_target_image = _target_image(
                    conn, bundle=scope["bundle_version"],
                    dates=scope["dates"], ready_asofs=scope["ready_snapshots"], roles=scope["snapshot_roles"],
                )
                actual_target_sha = _digest(actual_target_image)
                superseded = (actual_target_sha != row["after_target_sha256"]
                              and _verified_superseded(conn, scope, json.loads(row["after_target_json"]),
                                                       actual_target_image, row["applied_at"]))
        if row is None:
            return {"operation_id": operation_id, "state": "not_submitted"}
        if row["request_sha256"] != _digest(request):
            return {"operation_id": operation_id, "state": "failed", "reason": "request-mismatch"}
        state = "applied" if actual_target_sha == row["after_target_sha256"] or superseded else "ambiguous"
        return {"operation_id": operation_id, "state": state, "candidate_sha256": row["candidate_sha256"],
                "before_target_sha256": row["before_target_sha256"],
                "expected_target_sha256": row["after_target_sha256"], "actual_target_sha256": actual_target_sha,
                "applied_at": row["applied_at"], "backup_path": row["backup_path"],
                "backup_sha256": row["backup_sha256"],
                "superseded": bool(superseded)}


def rollback_preview(runtime: Path, operation_id: str) -> dict[str, Any]:
    """Prepare a promo-target-only inverse from the retained pre-submit backup."""
    authority = operational_authority(runtime)
    with closing(_connect(authority[0], readonly=True)) as current:
        row = _ledger_row(current, operation_id)
        if row is None:
            raise AdapterError("promo-rollback-publication-missing")
        scope = json.loads(row["preview_json"])["scope"]
        dates = scope["dates"]
        backup_path = Path(row["backup_path"])
        if not backup_path.is_file():
            raise AdapterError("promo-rollback-backup-missing")
        if "sha256:" + hashlib.sha256(backup_path.read_bytes()).hexdigest() != row["backup_sha256"]:
            raise AdapterError("promo-rollback-backup-file-drift")
        with closing(_connect(backup_path, readonly=True)) as backup:
            _verify_scoped_backup(backup, operation_id=operation_id,
                                  before_target_sha=row["before_target_sha256"],
                                  candidate_sha=row["candidate_sha256"])
            before_image = _target_image(backup, bundle=scope["bundle_version"], dates=dates,
                                         ready_asofs=scope["ready_snapshots"], roles=scope["snapshot_roles"])
            if _digest(before_image) != row["before_target_sha256"]:
                raise AdapterError("promo-rollback-backup-target-mismatch")
        actual_image = _target_image(current, bundle=scope["bundle_version"], dates=dates,
                                     ready_asofs=scope["ready_snapshots"], roles=scope["snapshot_roles"])
        if _digest(actual_image) != row["after_target_sha256"]:
            raise AdapterError("promo-rollback-after-target-drift")
        if not _after_row_timestamps_match(current, dates=dates, roles=scope["snapshot_roles"], captured_at=row["applied_at"]):
            raise AdapterError("promo-rollback-after-source-row-drift")
        ready_meta = _ready_metadata(current, bundle=scope["bundle_version"], ready_asofs=scope["ready_snapshots"])
        if _digest(ready_meta) != _digest(json.loads(row["after_ready_metadata_json"])):
            raise AdapterError("promo-rollback-after-ready-metadata-drift")
        exists = current.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ROLLBACK_LEDGER,)).fetchone()
        if exists and current.execute(f"SELECT 1 FROM {ROLLBACK_LEDGER} WHERE operation_id=?", (operation_id,)).fetchone():
            raise AdapterError("promo-rollback-already-submitted")
        return {"operation_id": operation_id, "target": str(authority[0]), "backup_path": str(backup_path),
                "scope": scope, "after_target_sha256": row["after_target_sha256"],
                "restored_target_sha256": row["before_target_sha256"]}


def rollback_apply(runtime: Path, operation_id: str, expected_after_target_sha: str) -> dict[str, Any]:
    """One scoped inverse CAS; later unrelated writes in ready plans are retained."""
    with promo_archive_fence(runtime):
        preview = rollback_preview(runtime, operation_id)
        if preview["after_target_sha256"] != expected_after_target_sha:
            raise AdapterError("promo-rollback-approval-drift")
        scope = preview["scope"]
        dates = scope["dates"]
        bundle = scope["bundle_version"]
        roles = scope["snapshot_roles"]
        ready_asofs = scope["ready_snapshots"]
        backup_path = Path(preview["backup_path"])
        authority = operational_authority(runtime)
        with closing(_connect(backup_path, readonly=True)) as backup, closing(_connect(authority[0], readonly=False)) as current:
            publication_before = _ledger_row(current, operation_id)
            if publication_before is None:
                raise AdapterError("promo-rollback-publication-missing")
            if "sha256:" + hashlib.sha256(backup_path.read_bytes()).hexdigest() != publication_before["backup_sha256"]:
                raise AdapterError("promo-rollback-backup-file-drift")
            _verify_scoped_backup(backup, operation_id=operation_id,
                                  before_target_sha=publication_before["before_target_sha256"],
                                  candidate_sha=publication_before["candidate_sha256"])
            old_rows = _source_rows(backup, dates=dates, roles=roles)
            old_plans = {as_of: json.loads(backup.execute(
                "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?",
                (bundle, as_of)).fetchone()[0]) for as_of in ready_asofs}
            current.execute("BEGIN IMMEDIATE")
            try:
                check_authority(runtime, authority)
                if _digest(_target_image(current, bundle=bundle, dates=dates, ready_asofs=ready_asofs, roles=roles)) != expected_after_target_sha:
                    raise AdapterError("promo-rollback-after-target-drift")
                publication = _ledger_row(current, operation_id)
                if publication is None or not _after_row_timestamps_match(current, dates=dates, roles=roles, captured_at=publication["applied_at"]):
                    raise AdapterError("promo-rollback-after-source-row-drift")
                if _digest(_ready_metadata(current, bundle=bundle, ready_asofs=ready_asofs)) != _digest(json.loads(publication["after_ready_metadata_json"])):
                    raise AdapterError("promo-rollback-after-ready-metadata-drift")
                current.execute(f"CREATE TABLE IF NOT EXISTS {ROLLBACK_LEDGER}(operation_id TEXT PRIMARY KEY, after_target_sha256 TEXT NOT NULL, restored_target_sha256 TEXT NOT NULL, restored_at TEXT NOT NULL)")
                if current.execute(f"SELECT 1 FROM {ROLLBACK_LEDGER} WHERE operation_id=?", (operation_id,)).fetchone():
                    raise AdapterError("promo-rollback-already-submitted")
                for day in dates:
                    for role in roles[day]:
                        current_row = current.execute("SELECT captured_at,payload_json FROM temporal_source_slot_snapshots WHERE source_key=? AND snapshot_date=? AND snapshot_role=?", (SOURCE, day, role)).fetchone()
                        if current_row is None:
                            raise AdapterError("promo-rollback-after-slot-missing")
                        old_row = old_rows["slots"][day][role]
                        deleted = current.execute("DELETE FROM temporal_source_slot_snapshots WHERE source_key=? AND snapshot_date=? AND snapshot_role=? AND captured_at=? AND payload_json=?", (SOURCE, day, role, current_row[0], current_row[1]))
                        if deleted.rowcount != 1:
                            raise AdapterError("promo-rollback-slot-cas-failed")
                        if old_row is not None:
                            current.execute("INSERT INTO temporal_source_slot_snapshots(source_key,snapshot_date,snapshot_role,captured_at,payload_json) VALUES(?,?,?,?,?)", old_row)
                    current_row = current.execute("SELECT captured_at,payload_json FROM temporal_source_snapshots WHERE source_key=? AND snapshot_date=?", (SOURCE, day)).fetchone()
                    if current_row is None:
                        raise AdapterError("promo-rollback-after-exact-missing")
                    old_row = old_rows["exact"][day]
                    deleted = current.execute("DELETE FROM temporal_source_snapshots WHERE source_key=? AND snapshot_date=? AND captured_at=? AND payload_json=?", (SOURCE, day, current_row[0], current_row[1]))
                    if deleted.rowcount != 1:
                        raise AdapterError("promo-rollback-exact-cas-failed")
                    if old_row is not None:
                        current.execute("INSERT INTO temporal_source_snapshots(source_key,snapshot_date,captured_at,payload_json) VALUES(?,?,?,?)", old_row)
                for as_of in ready_asofs:
                    row = current.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?", (bundle, as_of)).fetchone()
                    if row is None:
                        raise AdapterError("promo-rollback-ready-missing")
                    restored = _restore_plan_target(json.loads(row[0]), old_plans[as_of], set(dates))
                    if _plan_non_target(restored, set(dates)) != _plan_non_target(json.loads(row[0]), set(dates)):
                        raise AdapterError("promo-rollback-non-target-drift")
                    replace_ready(current, expected=ExpectedReady(bundle, as_of, row[0]),
                                  plan_json=json.dumps(restored, ensure_ascii=False, separators=(",", ":")))
                restored_sha = _digest(_target_image(current, bundle=bundle, dates=dates,
                                                     ready_asofs=ready_asofs, roles=roles))
                if restored_sha != preview["restored_target_sha256"]:
                    raise AdapterError("promo-rollback-restored-target-mismatch")
                current.execute(f"INSERT INTO {ROLLBACK_LEDGER} VALUES(?,?,?,?)",
                                (operation_id, expected_after_target_sha, restored_sha, datetime.now(timezone.utc).isoformat()))
                current.commit()
            except Exception:
                current.rollback()
                raise
    return {"operation_id": operation_id, "state": "restored", "restored_target_sha256": preview["restored_target_sha256"]}
