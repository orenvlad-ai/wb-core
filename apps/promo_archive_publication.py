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
from packages.application.promo_historical_recovery import (
    PromoHistoricalRecoveryError, find_reconstruction, qualified_reconstruction,
    read_reconstruction_run, reconstruction_artifact_proof,
)
from packages.application.inventory_retention import (
    prove_inventory_retention, publish_inventory_retention, retention_receipts_match,
)
from packages.application.ready_publication import (
    ExpectedReady,
    check_authority,
    operational_authority,
    replace_ready,
)
from packages.application.sheet_vitrina_v1 import _column_name, parse_sheet_write_plan_payload
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
GEOMETRY_LEDGER = "promo_ready_geometry_repairs"
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


def _request(request: dict[str, Any]) -> tuple[Path, list[str], dict[str, Any]]:
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
            or any(value != "auto" and (not isinstance(value, dict) or set(value) != {"identity_run", "price_checkpoint_id"}
                   or any(type(part) is not str or not part for part in value.values()))
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
    presentation = (value.get("metadata") or {}).get("server_cell_presentation") or {}
    for row_id in list(presentation):
        if row_id.rsplit("|", 1)[-1] in {*SKU_METRICS, *[key.split("|", 1)[-1] for key in TOTAL_METRICS.values()]}:
            for day in dates:
                presentation[row_id].pop(day, None)
            if not presentation[row_id]:
                del presentation[row_id]
    if not presentation:
        (value.get("metadata") or {}).pop("server_cell_presentation", None)
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
        "inventory_retention": candidate.get("inventory_retention", {}),
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
        if reconstruction:
            from packages.application.promo_historical_recovery import composite_cell_presentation
            presentation = plan.setdefault("metadata", {}).setdefault("server_cell_presentation", {})
            for row_id, by_day in composite_cell_presentation(rows=data["rows"], day=day, proof=reconstruction).items():
                presentation.setdefault(row_id, {}).update(by_day)
        status_row[1:11] = ["success", day, day, "", day, day, len(items), len(items), "", result["detail"] + "; publication=" + origin]
        refresh = plan.setdefault("metadata", {}).setdefault("refresh_diagnostics", {})
        source_slots = refresh.setdefault("source_slots", [])
        if not any(slot.get("source_key") == SOURCE and slot.get("slot_kind") == slot_kind and slot.get("requested_date") == day for slot in source_slots):
            source_slots.append({"source_key": SOURCE, "slot_kind": slot_kind, "requested_date": day})
        for source_slot in source_slots:
            if source_slot.get("source_key") == SOURCE and source_slot.get("slot_kind") == slot_kind and source_slot.get("requested_date") == day:
                source_slot.update(status="success", semantic_status=("warning" if reconstruction else "success"), origin=origin,
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
    try:
        return read_reconstruction_run(runtime, day, run_name)
    except PromoHistoricalRecoveryError as exc:
        raise AdapterError(str(exc)) from None


def _reconstruction_artifact_proof(runtime: Path, day: str, run_path: Path, summary: dict[str, Any], observed: datetime) -> str:
    try:
        return reconstruction_artifact_proof(runtime, day, run_path, summary, observed)
    except PromoHistoricalRecoveryError as exc:
        raise AdapterError(str(exc)) from None


def _candidate(runtime: Path, dates: list[str], reconstruction: dict[str, Any] | None = None) -> dict[str, Any]:
    reconstruction = dict(reconstruction or {})
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
        for day, spec in list(reconstruction.items()):
            try:
                if spec == "auto":
                    selected = find_reconstruction(runtime, day, conn, price_id_sets[day])
                    if selected is None:
                        raise AdapterError(f"promo-reconstruction-no-qualified-evidence:{day}")
                    spec, truth, proof = selected
                    reconstruction[day] = spec
                else:
                    truth, proof = qualified_reconstruction(runtime, day, spec, conn, price_id_sets[day])
            except PromoHistoricalRecoveryError as exc:
                raise AdapterError(str(exc)) from None
            price_truth[day] = truth
            reconstruction_proof[day] = proof
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
            payload["observation_quality"] = "historical_composite_observation_only"
            payload["detail"] += "; freshness=historical_composite_observation_only; closed_day_freshness_unproven=true"
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
    inventory_retention = {}
    with closing(_connect(db_path, readonly=True)) as inventory_conn:
        inventory_conn.execute("BEGIN")
        for update in ready_updates:
            before = dict(inventory_conn.execute("SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?",
                (bundle, update["as_of_date"])).fetchone())
            if before["plan_json"] != update["old_plan_json"]:
                raise AdapterError("promo-inventory-before-ready-drift")
            inventory_retention[update["as_of_date"]] = prove_inventory_retention(inventory_conn,
                runtime_dir=runtime, before=before, after={**before, "plan_json": update["plan_json"]})
    candidate_sha = _digest({"inventory_retention": inventory_retention, "source": source_sha, "days": results, "roles": roles,
                             "ready": [(u["as_of_date"], u["plan_json"]) for u in ready_updates]})
    candidate = {"authority": authority, "db_path": db_path, "bundle": bundle, "ids": ids,
            "inventory_retention": inventory_retention, "dates": dates, "results": results, "ready_updates": ready_updates,
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
        if request.get("mode") == "repair_inventory_retention":
            return _inventory_repair_preview(request, operation_id)
        if request.get("mode") == "repair_ready_geometry":
            return _geometry_preview(request, operation_id)
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
                      "inventory_retention": candidate["inventory_retention"],
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
        if request.get("mode") == "repair_inventory_retention":
            return _inventory_repair_apply(request, operation_id, preview)
        if request.get("mode") == "repair_ready_geometry":
            return _geometry_apply(request, operation_id, preview)
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
                    if "inventory_retention_json" not in {r[1] for r in conn.execute(f"PRAGMA table_info({LEDGER})")}:
                        conn.execute(f"ALTER TABLE {LEDGER} ADD COLUMN inventory_retention_json TEXT NOT NULL DEFAULT '[]'")
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
                    inventory_receipts = []
                    inventory_before_rows = {}
                    for update in fresh["ready_updates"]:
                        before = dict(conn.execute("SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?",
                            (fresh["bundle"], update["as_of_date"])).fetchone())
                        inventory_before_rows[update["as_of_date"]] = before
                        pointer = prove_inventory_retention(conn, runtime_dir=runtime, before=before,
                            after={**before, "plan_json": update["plan_json"]})
                        if pointer != fresh["inventory_retention"][update["as_of_date"]]:
                            raise AdapterError("promo-inventory-proof-drift")
                        replace_ready(conn, expected=ExpectedReady(fresh["bundle"], update["as_of_date"], update["old_plan_json"]), plan_json=update["plan_json"])
                        receipt = publish_inventory_retention(conn, operation_id=operation_id,
                            pointer=pointer, now=captured_at)
                        if receipt is not None:
                            inventory_receipts.append(receipt)
                    if not retention_receipts_match(conn, inventory_receipts):
                        raise AdapterError("promo-inventory-receipt-poststate-mismatch")
                    actual_target_image = _target_image(conn, bundle=fresh["bundle"], dates=dates,
                                                         ready_asofs=ready_asofs, roles=fresh["roles"])
                    actual_target_sha = _digest(actual_target_image)
                    if actual_target_sha != fresh["expected_target_sha"]:
                        raise AdapterError("promo-poststate-mismatch-before-commit")
                    replay_source_sha = _source_fingerprint(runtime, dates)
                    replay_proof: dict[str, dict[str, Any]] = {}
                    for day, proof in fresh["reconstruction_proof"].items():
                        run_path, run_summary, observed = _reconstruction_run(runtime, day, Path(proof["identity_run"]).parent.name)
                        replay_proof[day] = dict(fresh["reconstruction_proof"][day])
                        replay_proof[day]["artifact_sha256"] = _reconstruction_artifact_proof(runtime, day, run_path, run_summary, observed)
                    if _digest({"archive_and_runs": replay_source_sha, "reconstruction": replay_proof}) != fresh["source_sha"]:
                        raise AdapterError("promo-source-changed-during-submit")
                    for update in fresh["ready_updates"]:
                        after_row = dict(conn.execute("SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?",
                            (fresh["bundle"], update["as_of_date"])).fetchone())
                        if prove_inventory_retention(conn, runtime_dir=runtime,
                            before=inventory_before_rows[update["as_of_date"]], after=after_row) != fresh["inventory_retention"][update["as_of_date"]]:
                            raise AdapterError("promo-inventory-proof-changed-during-submit")
                    after_ready_metadata = _ready_metadata(conn, bundle=fresh["bundle"], ready_asofs=ready_asofs)
                    conn.execute(f"INSERT INTO {LEDGER}(operation_id,request_sha256,preview_json,candidate_sha256,before_target_sha256,after_target_sha256,after_target_json,after_ready_metadata_json,applied_at,backup_path,backup_sha256,inventory_retention_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (operation_id, _digest(request), json.dumps(preview, ensure_ascii=False), fresh["candidate_sha"], before_target_sha, actual_target_sha, json.dumps(actual_target_image, ensure_ascii=False, separators=(",", ":")), json.dumps(after_ready_metadata, ensure_ascii=False), captured_at, str(backup_path), backup_sha, json.dumps(inventory_receipts, ensure_ascii=False)))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        return {"operation_id": operation_id, "disposition": "submitted", "backup_path": str(backup_path)}

    def readback(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]:
        if request.get("mode") == "repair_inventory_retention":
            return _inventory_repair_readback(request, operation_id)
        if request.get("mode") == "repair_ready_geometry":
            return _geometry_readback(request, operation_id)
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
                inventory_receipts = json.loads(row["inventory_retention_json"]) if "inventory_retention_json" in row.keys() else []
                inventory_matches = retention_receipts_match(conn, inventory_receipts)
                superseded = (actual_target_sha != row["after_target_sha256"]
                              and _verified_superseded(conn, scope, json.loads(row["after_target_json"]),
                                                       actual_target_image, row["applied_at"]))
        if row is None:
            return {"operation_id": operation_id, "state": "not_submitted"}
        if row["request_sha256"] != _digest(request):
            return {"operation_id": operation_id, "state": "failed", "reason": "request-mismatch"}
        state = "applied" if inventory_matches and (actual_target_sha == row["after_target_sha256"] or superseded) else "ambiguous"
        return {"operation_id": operation_id, "state": state, "candidate_sha256": row["candidate_sha256"],
                "before_target_sha256": row["before_target_sha256"],
                "expected_target_sha256": row["after_target_sha256"], "actual_target_sha256": actual_target_sha,
                "applied_at": row["applied_at"], "backup_path": row["backup_path"],
                "backup_sha256": row["backup_sha256"],
                "superseded": bool(superseded), "inventory_retention_receipts": len(inventory_receipts),
                "inventory_retention_verified": inventory_matches}


def _geometry_request(request: dict[str, Any]) -> tuple[Path, str, int, str]:
    required = {"mode", "runtime_dir", "as_of_date", "expected_status_row_count", "expected_write_rect"}
    if set(request) != required or request.get("mode") != "repair_ready_geometry":
        raise AdapterError("promo-geometry-request-fields-invalid")
    runtime = Path(str(request["runtime_dir"])).resolve()
    if not runtime.is_absolute() or not runtime.is_dir():
        raise AdapterError("promo-geometry-runtime-invalid")
    as_of = request["as_of_date"]
    try:
        if type(as_of) is not str or date.fromisoformat(as_of).isoformat() != as_of:
            raise ValueError
    except (ValueError, TypeError):
        raise AdapterError("promo-geometry-date-invalid") from None
    count = request["expected_status_row_count"]
    rect = request["expected_write_rect"]
    if type(count) is not int or count < 0 or type(rect) is not str:
        raise AdapterError("promo-geometry-before-shape-invalid")
    return runtime, as_of, count, rect


def _geometry_ledger_row(conn: sqlite3.Connection, operation_id: str) -> sqlite3.Row | None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (GEOMETRY_LEDGER,)).fetchone():
        return None
    return conn.execute(f"SELECT * FROM {GEOMETRY_LEDGER} WHERE operation_id=?", (operation_id,)).fetchone()


def _geometry_ready_row(conn: sqlite3.Connection, bundle: str, as_of: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?",
                       (bundle, as_of)).fetchone()
    if row is None:
        raise AdapterError("promo-geometry-ready-missing")
    return dict(row)


def _geometry_candidate(runtime: Path, as_of: str, expected_count: int, expected_rect: str) -> dict[str, Any]:
    authority = operational_authority(runtime)
    with closing(_connect(authority[0], readonly=True)) as conn:
        conn.execute("BEGIN")
        state = conn.execute("SELECT bundle_version FROM registry_upload_current_state WHERE slot=1").fetchone()
        if state is None:
            raise AdapterError("promo-geometry-bundle-missing")
        bundle = str(state[0])
        before_row = _geometry_ready_row(conn, bundle, as_of)
    before_plan = json.loads(before_row["plan_json"])
    statuses = [sheet for sheet in before_plan.get("sheets") or [] if sheet.get("sheet_name") == "STATUS"]
    if len(statuses) != 1:
        raise AdapterError("promo-geometry-status-sheet-missing-or-duplicate")
    status = statuses[0]
    rows = status.get("rows")
    if (not isinstance(rows, list) or len(rows) != expected_count + 1
            or status.get("row_count") != expected_count or status.get("write_rect") != expected_rect):
        raise AdapterError("promo-geometry-unexpected-before-shape")
    if not rows[-1] or rows[-1][0] not in {
        f"{SOURCE}[{slot.get('slot_key')}]" for slot in before_plan.get("temporal_slots") or []
    }:
        raise AdapterError("promo-geometry-trailing-row-not-promo")
    if (status.get("write_start_cell") != "A1" or status.get("column_count") != 11
            or expected_rect != f"A1:K{expected_count + 1}"):
        raise AdapterError("promo-geometry-before-rect-invalid")
    after_plan = deepcopy(before_plan)
    after_status = next(sheet for sheet in after_plan["sheets"] if sheet.get("sheet_name") == "STATUS")
    _set_status_shape(after_status)
    if after_status["row_count"] != len(rows) or after_status["write_rect"] != f"A1:K{len(rows) + 1}":
        raise AdapterError("promo-geometry-after-shape-invalid")
    try:
        parse_sheet_write_plan_payload(after_plan)
    except ValueError as exc:
        raise AdapterError(f"promo-geometry-after-envelope-invalid:{exc}") from exc
    before_masked = deepcopy(before_plan)
    after_masked = deepcopy(after_plan)
    for plan in (before_masked, after_masked):
        sheet = next(sheet for sheet in plan["sheets"] if sheet.get("sheet_name") == "STATUS")
        sheet.pop("row_count", None)
        sheet.pop("write_rect", None)
    if _digest(before_masked) != _digest(after_masked):
        raise AdapterError("promo-geometry-non-shape-drift")
    after_row = dict(before_row)
    after_row["plan_json"] = json.dumps(after_plan, ensure_ascii=False, separators=(",", ":"))
    prestate = _digest({"authority": [str(authority[0]), authority[1]], "before_row": before_row})
    candidate_sha = _digest({"prestate": prestate, "after_row": after_row})
    return {"authority": authority, "db_path": authority[0], "bundle": bundle, "as_of_date": as_of,
            "before_row": before_row, "after_row": after_row, "before_plan": before_plan,
            "after_plan": after_plan, "prestate_sha": prestate, "candidate_sha": candidate_sha,
            "before_row_sha": _digest(before_row), "after_row_sha": _digest(after_row),
            "non_shape_sha": _digest(before_masked), "dates": [], "roles": {},
            "ready_updates": [{"as_of_date": as_of}]}


def _geometry_preview(request: dict[str, Any], operation_id: str) -> dict[str, Any]:
    runtime, as_of, count, rect = _geometry_request(request)
    authority = operational_authority(runtime)
    with closing(_connect(authority[0], readonly=True)) as conn:
        prior = _geometry_ledger_row(conn, operation_id)
    if prior is not None:
        if prior["request_sha256"] != _digest(request):
            raise AdapterError("promo-geometry-operation-request-mismatch")
        return json.loads(prior["preview_json"])
    candidate = _geometry_candidate(runtime, as_of, count, rect)
    return {"operation_id": operation_id, "target": str(candidate["db_path"]),
            "scope": {"mode": "repair_ready_geometry", "bundle_version": candidate["bundle"],
                      "as_of_date": as_of, "before_row_count": count, "after_row_count": count + 1,
                      "before_write_rect": rect, "after_write_rect": f"A1:K{count + 2}",
                      "metric_cells_changed": 0, "source_rows_changed": 0,
                      "non_shape_sha256": candidate["non_shape_sha"],
                      "before_ready_row_sha256": candidate["before_row_sha"],
                      "after_ready_row_sha256": candidate["after_row_sha"]},
            "prestate_sha256": candidate["prestate_sha"], "candidate_sha256": candidate["candidate_sha"],
            "recovery": {"method": "attested scoped full ready-row SQLite backup before atomic CAS",
                         "rollback": "exact full-row inverse CAS through promo_archive_publication_rollback.py --mode ready_geometry"}}


def _geometry_apply(request: dict[str, Any], operation_id: str, preview: dict[str, Any]) -> dict[str, Any]:
    runtime, as_of, count, rect = _geometry_request(request)
    with promo_archive_fence(runtime):
        candidate = _geometry_candidate(runtime, as_of, count, rect)
        if (candidate["prestate_sha"], candidate["candidate_sha"]) != (
                preview.get("prestate_sha256"), preview.get("candidate_sha256")):
            raise AdapterError("promo-geometry-preview-drift")
        backup_root = (storage_destination_root("promo_archive_publication")
                       if candidate["db_path"].resolve().is_relative_to(Path("/opt/wb-core-runtime/state"))
                       else runtime / "backups" / "promo_archive_publication")
        backup_path = backup_root / f"{candidate['db_path'].stem}__promo_geometry__{operation_id}.sqlite3"
        if backup_path.exists():
            raise AdapterError("promo-geometry-backup-already-exists")
        before_target_sha, before_rows_sha, backup_sha = _create_scoped_backup(
            candidate, operation_id, backup_path,
            production=candidate["db_path"].resolve().is_relative_to(Path("/opt/wb-core-runtime/state")))
        with closing(_connect(candidate["db_path"], readonly=False)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                check_authority(runtime, candidate["authority"])
                fresh = _geometry_candidate(runtime, as_of, count, rect)
                if (fresh["prestate_sha"], fresh["candidate_sha"]) != (candidate["prestate_sha"], candidate["candidate_sha"]):
                    raise AdapterError("promo-geometry-drift-before-submit")
                if _digest(_scoped_backup_rows(conn, candidate)) != before_rows_sha:
                    raise AdapterError("promo-geometry-full-row-drift-before-submit")
                if _digest(_geometry_ready_row(conn, candidate["bundle"], as_of)) != candidate["before_row_sha"]:
                    raise AdapterError("promo-geometry-ready-cas-drift")
                conn.execute(f"CREATE TABLE IF NOT EXISTS {GEOMETRY_LEDGER}(operation_id TEXT PRIMARY KEY,request_sha256 TEXT NOT NULL,preview_json TEXT NOT NULL,candidate_sha256 TEXT NOT NULL,before_target_sha256 TEXT NOT NULL,before_row_sha256 TEXT NOT NULL,after_row_sha256 TEXT NOT NULL,applied_at TEXT NOT NULL,backup_path TEXT NOT NULL,backup_sha256 TEXT NOT NULL,rolled_back_at TEXT)")
                if _geometry_ledger_row(conn, operation_id) is not None:
                    raise AdapterError("promo-geometry-operation-already-submitted")
                replace_ready(conn, expected=ExpectedReady(candidate["bundle"], as_of, candidate["before_row"]["plan_json"]),
                              plan_json=candidate["after_row"]["plan_json"])
                if _digest(_geometry_ready_row(conn, candidate["bundle"], as_of)) != candidate["after_row_sha"]:
                    raise AdapterError("promo-geometry-poststate-mismatch")
                applied_at = datetime.now(timezone.utc).isoformat()
                conn.execute(f"INSERT INTO {GEOMETRY_LEDGER} VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                             (operation_id, _digest(request), json.dumps(preview, ensure_ascii=False),
                              candidate["candidate_sha"], before_target_sha, candidate["before_row_sha"],
                              candidate["after_row_sha"], applied_at, str(backup_path), backup_sha))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    return {"operation_id": operation_id, "disposition": "submitted", "backup_path": str(backup_path)}


def _geometry_readback(request: dict[str, Any], operation_id: str) -> dict[str, Any]:
    runtime, as_of, _count, _rect = _geometry_request(request)
    authority = operational_authority(runtime)
    with closing(_connect(authority[0], readonly=True)) as conn:
        row = _geometry_ledger_row(conn, operation_id)
        if row is None:
            return {"operation_id": operation_id, "state": "not_submitted"}
        if row["request_sha256"] != _digest(request):
            return {"operation_id": operation_id, "state": "failed", "reason": "request-mismatch"}
        scope = json.loads(row["preview_json"])["scope"]
        try:
            current = _geometry_ready_row(conn, scope["bundle_version"], as_of)
        except AdapterError:
            current = None
    actual = _digest(current)
    superseded = False
    if (current is not None and actual != row["after_row_sha256"]
            and _later_timestamp(current["refreshed_at"], row["applied_at"])):
        try:
            parse_sheet_write_plan_payload(json.loads(current["plan_json"]))
        except (ValueError, json.JSONDecodeError):
            pass
        else:
            superseded = True
    return {"operation_id": operation_id,
            "state": ("restored" if row["rolled_back_at"] else "applied" if actual == row["after_row_sha256"] or superseded else "ambiguous"),
            "superseded": superseded, "actual_ready_row_sha256": actual,
            "expected_ready_row_sha256": row["after_row_sha256"], "backup_path": row["backup_path"],
            "backup_sha256": row["backup_sha256"], "applied_at": row["applied_at"]}


def geometry_rollback_preview(runtime: Path, operation_id: str) -> dict[str, Any]:
    authority = operational_authority(runtime)
    with closing(_connect(authority[0], readonly=True)) as conn:
        row = _geometry_ledger_row(conn, operation_id)
        if row is None or row["rolled_back_at"]:
            raise AdapterError("promo-geometry-rollback-not-applicable")
        scope = json.loads(row["preview_json"])["scope"]
        current = _geometry_ready_row(conn, scope["bundle_version"], scope["as_of_date"])
        if _digest(current) != row["after_row_sha256"]:
            raise AdapterError("promo-geometry-rollback-after-row-drift")
        backup_path = Path(row["backup_path"])
        if not backup_path.is_file() or "sha256:" + hashlib.sha256(backup_path.read_bytes()).hexdigest() != row["backup_sha256"]:
            raise AdapterError("promo-geometry-rollback-backup-drift")
        with closing(_connect(backup_path, readonly=True)) as backup:
            _verify_scoped_backup(backup, operation_id=operation_id,
                                  before_target_sha=row["before_target_sha256"],
                                  candidate_sha=row["candidate_sha256"])
            before = _geometry_ready_row(backup, scope["bundle_version"], scope["as_of_date"])
            if _digest(before) != row["before_row_sha256"]:
                raise AdapterError("promo-geometry-rollback-before-row-drift")
    return {"operation_id": operation_id, "target": str(authority[0]), "backup_path": str(backup_path),
            "after_row_sha256": row["after_row_sha256"], "restored_row_sha256": row["before_row_sha256"]}


def geometry_rollback_apply(runtime: Path, operation_id: str, expected_after_row_sha: str) -> dict[str, Any]:
    with promo_archive_fence(runtime):
        preview = geometry_rollback_preview(runtime, operation_id)
        if preview["after_row_sha256"] != expected_after_row_sha:
            raise AdapterError("promo-geometry-rollback-approval-drift")
        authority = operational_authority(runtime)
        backup_path = Path(preview["backup_path"])
        with closing(_connect(backup_path, readonly=True)) as backup, closing(_connect(authority[0], readonly=False)) as conn:
            ledger = _geometry_ledger_row(conn, operation_id)
            scope = json.loads(ledger["preview_json"])["scope"]
            before = _geometry_ready_row(backup, scope["bundle_version"], scope["as_of_date"])
            conn.execute("BEGIN IMMEDIATE")
            try:
                check_authority(runtime, authority)
                ledger = _geometry_ledger_row(conn, operation_id)
                if ledger is None or ledger["rolled_back_at"]:
                    raise AdapterError("promo-geometry-rollback-not-applicable")
                if "sha256:" + hashlib.sha256(backup_path.read_bytes()).hexdigest() != ledger["backup_sha256"]:
                    raise AdapterError("promo-geometry-rollback-backup-drift")
                _verify_scoped_backup(backup, operation_id=operation_id,
                                      before_target_sha=ledger["before_target_sha256"],
                                      candidate_sha=ledger["candidate_sha256"])
                current = _geometry_ready_row(conn, scope["bundle_version"], scope["as_of_date"])
                if _digest(current) != expected_after_row_sha:
                    raise AdapterError("promo-geometry-rollback-after-row-drift")
                replace_ready(conn, expected=ExpectedReady(scope["bundle_version"], scope["as_of_date"], current["plan_json"]),
                              plan_json=before["plan_json"])
                if _digest(_geometry_ready_row(conn, scope["bundle_version"], scope["as_of_date"])) != ledger["before_row_sha256"]:
                    raise AdapterError("promo-geometry-rollback-restored-row-mismatch")
                conn.execute(f"UPDATE {GEOMETRY_LEDGER} SET rolled_back_at=? WHERE operation_id=? AND rolled_back_at IS NULL",
                             (datetime.now(timezone.utc).isoformat(), operation_id))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    return {"operation_id": operation_id, "state": "restored", "restored_row_sha256": preview["restored_row_sha256"]}


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
            old_ready_rows = {as_of: dict(backup.execute(
                "SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?",
                (bundle, as_of)).fetchone()) for as_of in ready_asofs}
            old_plans = {as_of: json.loads(row["plan_json"]) for as_of, row in old_ready_rows.items()}
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
                inventory_receipts = []
                for as_of in ready_asofs:
                    row = current.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?", (bundle, as_of)).fetchone()
                    if row is None:
                        raise AdapterError("promo-rollback-ready-missing")
                    restored = _restore_plan_target(json.loads(row[0]), old_plans[as_of], set(dates))
                    if _plan_non_target(restored, set(dates)) != _plan_non_target(json.loads(row[0]), set(dates)):
                        raise AdapterError("promo-rollback-non-target-drift")
                    # Restore the publisher's original bytes when no unrelated
                    # edit remains. Its original complete digest must still match.
                    restored_raw = (old_ready_rows[as_of]["plan_json"] if restored == old_plans[as_of]
                        else json.dumps(restored, ensure_ascii=False, separators=(",", ":")))
                    after_row = dict(current.execute("SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?",
                        (bundle, as_of)).fetchone())
                    after_row["plan_json"] = restored_raw
                    pointer = prove_inventory_retention(current, runtime_dir=runtime,
                        before=old_ready_rows[as_of], after=after_row)
                    replace_ready(current, expected=ExpectedReady(bundle, as_of, row[0]), plan_json=restored_raw)
                    if restored_raw != old_ready_rows[as_of]["plan_json"]:
                        receipt = publish_inventory_retention(current, operation_id=operation_id + ":rollback",
                            pointer=pointer, now=datetime.now(timezone.utc).isoformat())
                        if receipt is not None:
                            inventory_receipts.append(receipt)
                if not retention_receipts_match(current, inventory_receipts):
                    raise AdapterError("promo-rollback-inventory-receipt-mismatch")
                restored_sha = _digest(_target_image(current, bundle=bundle, dates=dates,
                                                     ready_asofs=ready_asofs, roles=roles))
                if restored_sha != preview["restored_target_sha256"]:
                    raise AdapterError("promo-rollback-restored-target-mismatch")
                if "inventory_retention_json" not in {r[1] for r in current.execute(f"PRAGMA table_info({ROLLBACK_LEDGER})")}:
                    current.execute(f"ALTER TABLE {ROLLBACK_LEDGER} ADD COLUMN inventory_retention_json TEXT NOT NULL DEFAULT '[]'")
                current.execute(f"INSERT INTO {ROLLBACK_LEDGER}(operation_id,after_target_sha256,restored_target_sha256,restored_at,inventory_retention_json) VALUES(?,?,?,?,?)",
                    (operation_id, expected_after_target_sha, restored_sha, datetime.now(timezone.utc).isoformat(),
                     json.dumps(inventory_receipts, ensure_ascii=False)))
                current.commit()
            except Exception:
                current.rollback()
                raise
    return {"operation_id": operation_id, "state": "restored", "restored_target_sha256": preview["restored_target_sha256"]}


INVENTORY_REPAIR_LEDGER = 'promo_inventory_retention_repairs'


def _inventory_repair_request(request):
    if set(request) != {'mode', 'runtime_dir', 'publication_operation_id'} or request['mode'] != 'repair_inventory_retention':
        raise AdapterError('promo-inventory-repair-request-invalid')
    runtime = Path(str(request['runtime_dir'])).resolve()
    operation = request['publication_operation_id']
    if not runtime.is_dir() or not isinstance(operation, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', operation):
        raise AdapterError('promo-inventory-repair-request-invalid')
    return runtime, operation


def _inventory_repair_row(conn, operation_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (INVENTORY_REPAIR_LEDGER,)).fetchone():
        return None
    return conn.execute(f'SELECT * FROM {INVENTORY_REPAIR_LEDGER} WHERE operation_id=?', (operation_id,)).fetchone()


def _inventory_repair_candidate(runtime, owner, conn):
    """Attest a completed owner's exact before/after transition, never a latest plan."""
    publication = _ledger_row(conn, owner)
    if publication is None:
        raise AdapterError('promo-inventory-repair-owner-missing')
    scope = json.loads(publication['preview_json'])['scope']
    if not 1 <= len(scope['dates']) <= 7 or not 1 <= len(scope['ready_snapshots']) <= 14:
        raise AdapterError('promo-inventory-repair-scope-invalid')
    backup_path = Path(publication['backup_path'])
    if not backup_path.is_file() or backup_path.stat().st_size > 64 * 1024 * 1024:
        raise AdapterError('promo-inventory-repair-backup-missing-or-too-large')
    if 'sha256:' + hashlib.sha256(backup_path.read_bytes()).hexdigest() != publication['backup_sha256']:
        raise AdapterError('promo-inventory-repair-backup-drift')
    actual = _target_image(conn, bundle=scope['bundle_version'], dates=scope['dates'],
                           ready_asofs=scope['ready_snapshots'], roles=scope['snapshot_roles'])
    if _digest(actual) != publication['after_target_sha256'] or _digest(actual) != _digest(json.loads(publication['after_target_json'])):
        raise AdapterError('promo-inventory-repair-after-target-drift')
    if (not _after_row_timestamps_match(conn, dates=scope['dates'], roles=scope['snapshot_roles'], captured_at=publication['applied_at'])
            or _digest(_ready_metadata(conn, bundle=scope['bundle_version'], ready_asofs=scope['ready_snapshots']))
            != _digest(json.loads(publication['after_ready_metadata_json']))):
        raise AdapterError('promo-inventory-repair-after-metadata-drift')
    pointers = {}
    with closing(_connect(backup_path, readonly=True)) as backup:
        _verify_scoped_backup(backup, operation_id=owner, before_target_sha=publication['before_target_sha256'],
                              candidate_sha=publication['candidate_sha256'])
        results = {day: actual['slots'][day][scope['snapshot_roles'][day][0]] for day in scope['dates']}
        for as_of in scope['ready_snapshots']:
            key = (scope['bundle_version'], as_of)
            before = dict(backup.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?', key).fetchone())
            after = dict(conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?', key).fetchone())
            # Replay only the original scoped cell transformation: this attests
            # exact full bytes, not merely the promo subset or inventory values.
            expected = json.loads(before['plan_json'])
            _update_plan(expected, results, set(scope['dates']))
            expected_raw = json.dumps(expected, ensure_ascii=False, separators=(',', ':'))
            if after['plan_json'] != expected_raw or _plan_non_target(expected, set(scope['dates'])) != _plan_non_target(json.loads(before['plan_json']), set(scope['dates'])):
                raise AdapterError('promo-inventory-repair-full-ready-drift')
            pointer = prove_inventory_retention(conn, runtime_dir=runtime, before=before, after=after)
            pointer['transition_operation_id'] = owner
            pointer['transition_candidate_sha256'] = publication['candidate_sha256']
            pointer['transition_backup_sha256'] = publication['backup_sha256']
            pointer['transition_after_target_sha256'] = publication['after_target_sha256']
            pointers[as_of] = pointer
    if not any(pointer['dates'] for pointer in pointers.values()):
        raise AdapterError('promo-inventory-repair-no-accepted-captures')
    return {'owner': dict(publication), 'scope': scope, 'inventory_retention': pointers}


def _inventory_repair_preview(request, operation_id):
    runtime, owner = _inventory_repair_request(request)
    authority = operational_authority(runtime)
    with closing(_connect(authority[0], readonly=True)) as conn:
        conn.execute('BEGIN')
        prior = _inventory_repair_row(conn, operation_id)
        if prior is not None:
            if prior['request_sha256'] != _digest(request):
                raise AdapterError('promo-inventory-repair-request-mismatch')
            return json.loads(prior['preview_json'])
        candidate = _inventory_repair_candidate(runtime, owner, conn)
    return {'operation_id': operation_id, 'target': str(authority[0]),
        'scope': {'kind': 'inventory acceptance only', **candidate['scope']},
        'prestate_sha256': _digest(candidate['owner']), 'candidate_sha256': _digest(candidate),
        'candidate': candidate,
        'recovery': {'method': 'additive inventory_retention receipt; no READY/source/book changes',
                     'backup_path': candidate['owner']['backup_path'], 'backup_sha256': candidate['owner']['backup_sha256']}}


def _inventory_repair_apply(request, operation_id, preview):
    runtime, owner = _inventory_repair_request(request)
    authority = operational_authority(runtime)
    with promo_archive_fence(runtime), closing(_connect(authority[0], readonly=False)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            check_authority(runtime, authority)
            candidate = _inventory_repair_candidate(runtime, owner, conn)
            if (_digest(candidate['owner']), _digest(candidate)) != (preview['prestate_sha256'], preview['candidate_sha256']):
                raise AdapterError('promo-inventory-repair-preview-drift')
            conn.execute(f'''CREATE TABLE IF NOT EXISTS {INVENTORY_REPAIR_LEDGER}(
                operation_id TEXT PRIMARY KEY,request_sha256 TEXT NOT NULL,preview_json TEXT NOT NULL,
                receipts_json TEXT NOT NULL,applied_at TEXT NOT NULL)''')
            if _inventory_repair_row(conn, operation_id) is not None:
                raise AdapterError('promo-inventory-repair-already-submitted')
            now = datetime.now(timezone.utc).isoformat()
            receipts = []
            for pointer in candidate['inventory_retention'].values():
                receipt = publish_inventory_retention(conn, operation_id=operation_id, pointer=pointer, now=now)
                if receipt is not None:
                    receipts.append(receipt)
            # Re-prove source/READY/backup after append; receipt writes themselves
            # are outside the original owner's ledger/READY images.
            if _inventory_repair_candidate(runtime, owner, conn) != candidate or not retention_receipts_match(conn, receipts):
                raise AdapterError('promo-inventory-repair-poststate-drift')
            conn.execute(f'INSERT INTO {INVENTORY_REPAIR_LEDGER} VALUES(?,?,?,?,?)',
                (operation_id, _digest(request), json.dumps(preview, ensure_ascii=False),
                 json.dumps(receipts, ensure_ascii=False), now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {'operation_id': operation_id, 'disposition': 'submitted'}


def _inventory_repair_readback(request, operation_id):
    runtime, owner = _inventory_repair_request(request)
    authority = operational_authority(runtime)
    with closing(_connect(authority[0], readonly=True)) as conn:
        conn.execute('BEGIN')
        row = _inventory_repair_row(conn, operation_id)
        if row is None:
            return {'operation_id': operation_id, 'state': 'not_submitted'}
        if row['request_sha256'] != _digest(request):
            return {'operation_id': operation_id, 'state': 'failed', 'reason': 'request-mismatch'}
        preview = json.loads(row['preview_json'])
        receipts = json.loads(row['receipts_json'])
        try:
            candidate = _inventory_repair_candidate(runtime, owner, conn)
            verified = _digest(candidate) == preview['candidate_sha256'] and retention_receipts_match(conn, receipts)
        except (ValueError, AdapterError, sqlite3.Error, OSError):
            verified = False
    return {'operation_id': operation_id, 'state': 'applied' if verified else 'ambiguous',
            'candidate_sha256': preview['candidate_sha256'], 'inventory_retention_receipts': len(receipts),
            'inventory_retention_verified': verified, 'source_ready_book_unchanged': verified}
