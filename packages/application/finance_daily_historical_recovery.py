"""Bounded exact-date Finance recovery for daily Web Vitrina values."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from packages.adapters.fin_report_daily_block import HttpBackedFinReportDailySource
from packages.application.fin_report_daily_block import FinReportDailyBlock
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
    _derive_sheet_vitrina_refresh_semantic_summary,
    _deserialize_sheet_vitrina_plan,
    _deserialize_temporal_source_payload,
    _finance_daily_temporal_state,
    _finance_daily_temporal_state_digest,
    _sheet_vitrina_plan_digest,
)
from packages.contracts.fin_report_daily_block import (
    FinReportDailyRequest,
    FinReportDailySuccess,
)
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaWriteTarget,
)


CONTRACT_NAME = "finance_daily_historical_recovery"
CONTRACT_VERSION = 1
EXPECTED_SKU_COUNT = 33
SKU_METRICS = (
    "fin_buyout_rub",
    "fin_delivery_rub",
    "fin_commission_wb_portal",
    "fin_acquiring_fee",
    "fin_loyalty_rub",
)
TOTAL_METRICS = (
    "total_fin_buyout_rub",
    "total_fin_delivery_rub",
    "total_fin_commission_wb_portal",
    "total_fin_acquiring_fee",
    "total_fin_loyalty_rub",
    "fin_storage_fee_total",
)
EXPECTED_TARGET_CELLS = EXPECTED_SKU_COUNT * len(SKU_METRICS) + len(TOTAL_METRICS)
DATA_SHEET_NAME = "DATA_VITRINA"
STATUS_SHEET_NAME = "STATUS"
FINANCE_STATUS_KEY = "fin_report_daily[yesterday_closed]"
PROXY_GAP_ROW_ID = "SKU:428853741|proxy_profit_3_rub"
ALLOWED_RECOVERY_DATES = frozenset({"2026-08-26", "2026-08-27", "2026-09-01"})
ALLOWED_PARITY_DATES = frozenset({"2026-08-24", "2026-08-25"})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value or "").strip()).isoformat()
    except ValueError as exc:
        raise ValueError("target_date must be YYYY-MM-DD") from exc


def _sheet(plan: SheetVitrinaV1Envelope, name: str) -> SheetVitrinaWriteTarget:
    result = next((item for item in plan.sheets if item.sheet_name == name), None)
    if result is None:
        raise ValueError(f"ready snapshot does not contain {name}")
    return result


def _enabled_nm_ids(runtime: RegistryUploadDbBackedRuntime) -> list[int]:
    current = runtime.load_current_state()
    result = sorted({int(item.nm_id) for item in current.config_v2 if item.enabled})
    if len(result) != EXPECTED_SKU_COUNT:
        raise ValueError(
            "Finance daily recovery requires exact active SKU roster "
            f"{EXPECTED_SKU_COUNT}, got {len(result)}"
        )
    return result


def _source_result(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    target_date: str,
    nm_ids: list[int],
    block: FinReportDailyBlock | None,
) -> FinReportDailySuccess:
    effective = block or FinReportDailyBlock(
        HttpBackedFinReportDailySource(runtime_dir=runtime.runtime_dir)
    )
    result = effective.execute(
        FinReportDailyRequest(
            snapshot_type="fin_report_daily",
            snapshot_date=target_date,
            nm_ids=nm_ids,
        )
    ).result
    diagnostics = dict(result.diagnostics or {})
    pagination = dict(diagnostics.get("pagination") or {})
    covered = sorted({int(item.nm_id) for item in result.items})
    if result.snapshot_date != target_date:
        raise ValueError("Finance daily source returned a different exact date")
    if (
        diagnostics.get("endpoint") != "POST /api/finance/v1/sales-reports/detailed"
        or diagnostics.get("period") != "daily"
        or pagination.get("terminal_status") != 204
        or not bool(pagination.get("complete"))
    ):
        raise ValueError("Finance daily source pagination is not terminal-204 complete")
    if covered != nm_ids or int(diagnostics.get("covered_count") or 0) != len(nm_ids):
        raise ValueError(
            f"Finance daily source coverage is not {len(nm_ids)}/{len(nm_ids)}"
        )
    source_digest = str(diagnostics.get("source_digest") or "")
    if not source_digest.startswith("sha256:"):
        raise ValueError("Finance daily source digest is missing")
    return result


def _source_payload(result: FinReportDailySuccess) -> dict[str, Any]:
    """Only normalized daily aggregates; no raw seller-report rows or PII."""

    return asdict(result)


def _expected_values(
    result: FinReportDailySuccess,
    *,
    nm_ids: list[int],
) -> dict[str, float]:
    def sheet_value(value: float) -> float:
        """Match the canonical Vitrina numeric cell materialization."""

        return round(float(value), 6)

    items = {int(item.nm_id): item for item in result.items}
    if sorted(items) != nm_ids:
        raise ValueError("Finance daily normalized item roster changed")
    values: dict[str, float] = {}
    for nm_id in nm_ids:
        item = items[nm_id]
        for metric in SKU_METRICS:
            values[f"SKU:{nm_id}|{metric}"] = sheet_value(getattr(item, metric))
    for total_metric, sku_metric in zip(TOTAL_METRICS[:5], SKU_METRICS, strict=True):
        values[f"TOTAL|{total_metric}"] = sheet_value(
            sum(float(getattr(items[nm_id], sku_metric)) for nm_id in nm_ids)
        )
    values["TOTAL|fin_storage_fee_total"] = sheet_value(
        result.storage_total.fin_storage_fee_total
    )
    if len(values) != EXPECTED_TARGET_CELLS:
        raise ValueError(
            f"Finance daily exact target count is {len(values)}, expected {EXPECTED_TARGET_CELLS}"
        )
    return values


def _cell_state(value: Any) -> str:
    if value in (None, ""):
        return "missing"
    try:
        return "exact_zero" if float(value) == 0.0 else "exact"
    except (TypeError, ValueError) as exc:
        raise ValueError("Finance target contains a non-numeric current value") from exc


def _target_manifest(
    plan: SheetVitrinaV1Envelope,
    *,
    target_date: str,
    expected_values: Mapping[str, float],
) -> dict[str, Any]:
    data = _sheet(plan, DATA_SHEET_NAME)
    if target_date not in data.header:
        raise ValueError("ready snapshot does not contain the exact Finance date column")
    column_index = data.header.index(target_date)
    matching_rows = [
        row
        for row in data.rows
        if len(row) > 1 and str(row[1] or "") in expected_values
    ]
    rows = {str(row[1] or ""): row for row in matching_rows}
    missing = sorted(set(expected_values) - set(rows))
    if (
        missing
        or len(rows) != EXPECTED_TARGET_CELLS
        or len(matching_rows) != EXPECTED_TARGET_CELLS
    ):
        raise ValueError(
            "ready snapshot Finance target topology is incompatible: "
            f"found={len(matching_rows)} unique={len(rows)} missing={len(missing)}"
        )
    cells = {
        row_id: {
            "state": _cell_state(
                rows[row_id][column_index] if len(rows[row_id]) > column_index else None
            ),
            "value": rows[row_id][column_index] if len(rows[row_id]) > column_index else None,
            "expected": float(expected_values[row_id]),
        }
        for row_id in sorted(expected_values)
    }
    status = _sheet(plan, STATUS_SHEET_NAME)
    status_rows = [
        list(row)
        for row in status.rows
        if row and str(row[0] or "") == FINANCE_STATUS_KEY
    ]
    if len(status_rows) != 1:
        raise ValueError("ready snapshot does not contain one Finance status target")
    return {
        "target_date": target_date,
        "target_count": len(cells),
        "cells": cells,
        "status_header": list(status.header),
        "status_row": status_rows[0],
    }


def _status_note(
    *,
    target_date: str,
    generated_at: str,
    diagnostics: Mapping[str, Any],
) -> str:
    pagination = dict(diagnostics.get("pagination") or {})
    return "; ".join(
        (
            "source=official Finance POST",
            "resolution_rule=historical_recovery_terminal_204",
            f"snapshot_date={target_date}",
            f"recovery_plan_generated_at={generated_at}",
            f"pages={int(pagination.get('pages') or 0)}",
            f"rrdid_end={int(pagination.get('rrdid_end') or 0)}",
            "coverage=33/33",
            f"source_digest={str(diagnostics.get('source_digest') or '')}",
        )
    )


def _rematerialize(
    plan: SheetVitrinaV1Envelope,
    *,
    target_date: str,
    expected_values: Mapping[str, float],
    status_note: str,
) -> SheetVitrinaV1Envelope:
    data = _sheet(plan, DATA_SHEET_NAME)
    status = _sheet(plan, STATUS_SHEET_NAME)
    column_index = data.header.index(target_date)
    data_rows = [list(row) for row in data.rows]
    found: set[str] = set()
    for row in data_rows:
        row_id = str(row[1] or "") if len(row) > 1 else ""
        if row_id not in expected_values:
            continue
        if len(row) <= column_index:
            raise ValueError("Finance target row is shorter than its date column")
        row[column_index] = float(expected_values[row_id])
        found.add(row_id)
    if found != set(expected_values):
        raise ValueError("Finance target rows changed while rematerializing")

    status_rows = [list(row) for row in status.rows]
    status_matches = [row for row in status_rows if row and str(row[0] or "") == FINANCE_STATUS_KEY]
    if len(status_matches) != 1:
        raise ValueError("ready snapshot does not contain one exact closed-day Finance status row")
    status_row = status_matches[0]
    status_values = {
        "source_key": FINANCE_STATUS_KEY,
        "kind": "success",
        "freshness": target_date,
        "snapshot_date": target_date,
        "date": target_date,
        "date_from": "",
        "date_to": "",
        "requested_count": EXPECTED_SKU_COUNT,
        "covered_count": EXPECTED_SKU_COUNT,
        "missing_nm_ids": "",
        "note": status_note,
    }
    for name, value in status_values.items():
        if name not in status.header:
            raise ValueError(f"STATUS header is missing {name}")
        index = status.header.index(name)
        while len(status_row) <= index:
            status_row.append("")
        status_row[index] = value

    replacement_data = replace(data, rows=data_rows, row_count=len(data_rows))
    replacement_status = replace(status, rows=status_rows, row_count=len(status_rows))
    return replace(
        plan,
        sheets=[
            replacement_data if item.sheet_name == DATA_SHEET_NAME else (
                replacement_status if item.sheet_name == STATUS_SHEET_NAME else item
            )
            for item in plan.sheets
        ],
    )


def _non_target_digest(
    plan: SheetVitrinaV1Envelope,
    *,
    target_date: str,
    target_row_ids: set[str],
) -> str:
    payload = asdict(plan)
    for sheet in payload.get("sheets") or []:
        if sheet.get("sheet_name") == DATA_SHEET_NAME:
            header = list(sheet.get("header") or [])
            if target_date not in header:
                continue
            index = header.index(target_date)
            for row in sheet.get("rows") or []:
                if len(row) > 1 and str(row[1] or "") in target_row_ids and len(row) > index:
                    row[index] = "<finance-target>"
        elif sheet.get("sheet_name") == STATUS_SHEET_NAME:
            for row in sheet.get("rows") or []:
                if row and str(row[0] or "") == FINANCE_STATUS_KEY:
                    row[:] = [FINANCE_STATUS_KEY, "<finance-status-target>"]
    return _digest(payload)


def _row_value(plan: SheetVitrinaV1Envelope, *, row_id: str, target_date: str) -> Any:
    data = _sheet(plan, DATA_SHEET_NAME)
    if target_date not in data.header:
        return None
    index = data.header.index(target_date)
    row = next((item for item in data.rows if len(item) > 1 and str(item[1]) == row_id), None)
    return row[index] if row is not None and len(row) > index else None


def _load_temporal_before_state(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    target_date: str,
) -> dict[str, Any]:
    with _readonly_connection(runtime.db_path.resolve()) as conn:
        return _finance_daily_temporal_state(conn, target_date=target_date)


def build_finance_daily_recovery_plan(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    target_date: str,
    deployed_sha: str,
    mode: str = "recovery",
    block: FinReportDailyBlock | None = None,
    generated_at: str | None = None,
) -> tuple[dict[str, Any], SheetVitrinaV1Envelope, FinReportDailySuccess]:
    exact_date = _iso_date(target_date)
    allowed = ALLOWED_RECOVERY_DATES if mode == "recovery" else ALLOWED_PARITY_DATES
    if exact_date not in allowed:
        raise ValueError(f"Finance daily {mode} date is outside the exact accepted scope")
    if mode not in {"recovery", "parity"}:
        raise ValueError("Finance daily plan mode must be recovery or parity")
    exact_sha = str(deployed_sha or "").strip()
    if len(exact_sha) != 40 or any(char not in "0123456789abcdef" for char in exact_sha):
        raise ValueError("Finance daily plan requires exact deployed SHA")
    timestamp = str(generated_at or _now())
    nm_ids = _enabled_nm_ids(runtime)
    source = _source_result(
        runtime,
        target_date=exact_date,
        nm_ids=nm_ids,
        block=block,
    )
    expected = _expected_values(source, nm_ids=nm_ids)
    record = runtime.load_sheet_vitrina_ready_snapshot_record_covering_date_any_bundle(
        column_date=exact_date
    )
    before = record["plan"]
    if not isinstance(before, SheetVitrinaV1Envelope):
        raise ValueError("Finance daily ready snapshot record is invalid")
    before_manifest = _target_manifest(
        before, target_date=exact_date, expected_values=expected
    )
    if exact_date == "2026-09-01" and any(
        cell["value"] not in (None, "") and cell["value"] != float(expected[row_id])
        for row_id, cell in before_manifest["cells"].items()
    ):
        raise ValueError("September 1 recovery may only fill missing financial cells")
    note = _status_note(
        target_date=exact_date,
        generated_at=timestamp,
        diagnostics=source.diagnostics,
    )
    after = _rematerialize(
        before,
        target_date=exact_date,
        expected_values=expected,
        status_note=note,
    )
    after_manifest = _target_manifest(
        after, target_date=exact_date, expected_values=expected
    )
    target_ids = set(expected)
    before_non_target = _non_target_digest(
        before, target_date=exact_date, target_row_ids=target_ids
    )
    after_non_target = _non_target_digest(
        after, target_date=exact_date, target_row_ids=target_ids
    )
    if before_non_target != after_non_target:
        raise ValueError("Finance daily plan changed a non-target field")
    temporal_before_state = _load_temporal_before_state(
        runtime,
        target_date=exact_date,
    )
    temporal_before_state_digest = _finance_daily_temporal_state_digest(
        temporal_before_state
    )
    changed_cells = sum(
        before_manifest["cells"][row_id]["value"] != float(expected[row_id])
        for row_id in expected
    )
    diagnostics = dict(source.diagnostics or {})
    pagination = dict(diagnostics.get("pagination") or {})
    plan_without_identity = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "mode": mode,
        "generated_at": timestamp,
        "target_date": exact_date,
        "deployed_sha": exact_sha,
        "bundle_version": str(record["bundle_version"]),
        "as_of_date": str(record["as_of_date"]),
        "snapshot_id": str(record["snapshot_id"]),
        "snapshot_refreshed_at": str(record["refreshed_at"]),
        "source": {
            "endpoint": diagnostics.get("endpoint"),
            "period": diagnostics.get("period"),
            "source_digest": diagnostics.get("source_digest"),
            "source_row_count": int(diagnostics.get("source_row_count") or 0),
            "exact_date_row_count": int(diagnostics.get("exact_date_row_count") or 0),
            "target_row_count": int(diagnostics.get("target_row_count") or 0),
            "pages": int(pagination.get("pages") or 0),
            "terminal_cursor": int(pagination.get("rrdid_end") or 0),
            "terminal_status": int(pagination.get("terminal_status") or 0),
            "complete": bool(pagination.get("complete")),
            "coverage": f"{len(nm_ids)}/{len(nm_ids)}",
            "normalized_payload": _source_payload(source),
        },
        "expected_sku_count": EXPECTED_SKU_COUNT,
        "expected_target_cells": EXPECTED_TARGET_CELLS,
        "before_plan_digest": _sheet_vitrina_plan_digest(before),
        "after_plan_digest": _sheet_vitrina_plan_digest(after),
        "non_target_digest": before_non_target,
        "before_temporal_state_digest": temporal_before_state_digest,
        "before_temporal_state": temporal_before_state,
        "changed_cells": changed_cells,
        "before_manifest": before_manifest,
        "after_manifest": after_manifest,
        "proxy_gap_exclusion": {
            "row_id": PROXY_GAP_ROW_ID,
            "target_date": "2026-08-26",
            "value": _row_value(before, row_id=PROXY_GAP_ROW_ID, target_date="2026-08-26"),
            "mutated": False,
        },
        "raw_finance_rows_persisted": False,
        "non_target_invariant": "unchanged",
        "reversibility": (
            "171-cell and exact Finance temporal/closure before images "
            "persisted in operation audit"
        ),
    }
    fingerprint = _digest(plan_without_identity)
    plan = {
        **plan_without_identity,
        "fingerprint": fingerprint,
        "operation_id": (
            f"wbc0020-finance-{exact_date.replace('-', '')}-"
            f"{fingerprint.removeprefix('sha256:')[:16]}"
        ),
        "apply_allowed": mode == "recovery",
        "parity_status": "exact" if changed_cells == 0 else "mismatch",
    }
    return plan, after, source


def _validate_reviewed_plan(reviewed_plan: Mapping[str, Any], fingerprint: str) -> None:
    if reviewed_plan.get("contract_name") != CONTRACT_NAME:
        raise ValueError("Finance daily reviewed plan contract is invalid")
    if int(reviewed_plan.get("contract_version") or 0) != CONTRACT_VERSION:
        raise ValueError("Finance daily reviewed plan version is invalid")
    if reviewed_plan.get("mode") != "recovery" or not reviewed_plan.get("apply_allowed"):
        raise ValueError("Finance daily reviewed plan is not applyable recovery scope")
    expected = str(reviewed_plan.get("fingerprint") or "")
    if str(fingerprint or "") != expected:
        raise ValueError("Finance daily reviewed plan fingerprint does not match")
    target_date = _iso_date(str(reviewed_plan.get("target_date") or ""))
    expected_operation_id = (
        f"wbc0020-finance-{target_date.replace('-', '')}-"
        f"{expected.removeprefix('sha256:')[:16]}"
    )
    if str(reviewed_plan.get("operation_id") or "") != expected_operation_id:
        raise ValueError("Finance daily reviewed operation identity is invalid")
    unsigned = {
        key: deepcopy(value)
        for key, value in reviewed_plan.items()
        if key not in {"fingerprint", "operation_id", "apply_allowed", "parity_status"}
    }
    if _digest(unsigned) != expected:
        raise ValueError("Finance daily reviewed plan content fingerprint is invalid")


def _payload_namespace(source: Mapping[str, Any]) -> Any:
    return _to_namespace(dict(source.get("normalized_payload") or {}))


def _to_namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _to_namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


def apply_finance_daily_recovery(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    reviewed_plan: Mapping[str, Any],
    fingerprint: str,
    approval_reference: str,
    actor: str,
    deployed_sha: str,
    applied_at: str | None = None,
) -> dict[str, Any]:
    _validate_reviewed_plan(reviewed_plan, fingerprint)
    if str(reviewed_plan.get("deployed_sha") or "") != str(deployed_sha or ""):
        raise ValueError("Finance daily recovery deployed SHA changed after review")
    target_date = _iso_date(str(reviewed_plan.get("target_date") or ""))
    if target_date not in ALLOWED_RECOVERY_DATES:
        raise ValueError("Finance daily recovery apply date is outside accepted scope")
    source = dict(reviewed_plan.get("source") or {})
    expected_values = {
        row_id: float(dict(cell).get("expected"))
        for row_id, cell in dict(reviewed_plan.get("after_manifest") or {}).get("cells", {}).items()
    }
    if len(expected_values) != EXPECTED_TARGET_CELLS:
        raise ValueError("Finance daily reviewed target manifest is not 171 cells")
    record = runtime.load_sheet_vitrina_ready_snapshot_record_covering_date_any_bundle(
        column_date=target_date
    )
    before = record["plan"]
    if (
        str(record["bundle_version"]) != reviewed_plan.get("bundle_version")
        or str(record["as_of_date"]) != reviewed_plan.get("as_of_date")
        or str(record["snapshot_id"]) != reviewed_plan.get("snapshot_id")
    ):
        raise ValueError("Finance daily newest ready snapshot identity changed after review")
    current_digest = _sheet_vitrina_plan_digest(before)
    before_digest = str(reviewed_plan.get("before_plan_digest") or "")
    after_digest = str(reviewed_plan.get("after_plan_digest") or "")
    if current_digest not in {before_digest, after_digest}:
        raise ValueError("Finance daily ready snapshot changed after review")
    status_note = _status_note(
        target_date=target_date,
        generated_at=str(reviewed_plan.get("generated_at") or ""),
        diagnostics={
            "source_digest": source.get("source_digest"),
            "pagination": {
                "pages": source.get("pages"),
                "rrdid_end": source.get("terminal_cursor"),
            },
        },
    )
    after = _rematerialize(
        before,
        target_date=target_date,
        expected_values=expected_values,
        status_note=status_note,
    )
    if _sheet_vitrina_plan_digest(after) != after_digest:
        raise ValueError("Finance daily reviewed after plan cannot be reconstructed")
    timestamp = str(applied_at or _now())
    result = runtime.apply_finance_daily_historical_recovery(
        operation_id=str(reviewed_plan.get("operation_id") or ""),
        plan_fingerprint=str(fingerprint),
        approval_reference=approval_reference,
        actor=actor,
        deployed_sha=deployed_sha,
        applied_at=timestamp,
        target_date=target_date,
        bundle_version=str(reviewed_plan.get("bundle_version") or ""),
        as_of_date=str(reviewed_plan.get("as_of_date") or ""),
        snapshot_id=str(reviewed_plan.get("snapshot_id") or ""),
        before_plan_digest=before_digest,
        after_plan_digest=after_digest,
        non_target_digest=str(reviewed_plan.get("non_target_digest") or ""),
        before_temporal_state_digest=str(
            reviewed_plan.get("before_temporal_state_digest") or ""
        ),
        before_temporal_state=dict(reviewed_plan.get("before_temporal_state") or {}),
        before_manifest=dict(reviewed_plan.get("before_manifest") or {}),
        after_manifest=dict(reviewed_plan.get("after_manifest") or {}),
        changed_cells=int(reviewed_plan.get("changed_cells") or 0),
        source_digest=str(source.get("source_digest") or ""),
        source_pages=int(source.get("pages") or 0),
        source_terminal_cursor=int(source.get("terminal_cursor") or 0),
        source_payload=_payload_namespace(source),
        captured_at=timestamp,
        after_plan=after,
    )
    readback = readback_finance_daily_recovery(
        runtime,
        operation_id=str(reviewed_plan.get("operation_id") or ""),
    )
    if readback.get("status") != "complete":
        raise ValueError("Finance daily recovery query-only readback is incomplete")
    return {**result, "readback": readback}


def _readonly_connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def readback_finance_daily_recovery(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    operation_id: str,
) -> dict[str, Any]:
    db_path = runtime.db_path.resolve()
    if not db_path.is_file():
        raise ValueError("Finance daily recovery runtime DB is missing")
    with _readonly_connection(db_path) as conn:
        audit = conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_finance_daily_recovery_audit
            WHERE operation_id=?
            """,
            (str(operation_id or "").strip(),),
        ).fetchone()
        if audit is None:
            raise ValueError("Finance daily recovery operation receipt is missing")
        ready = conn.execute(
            """
            SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
            WHERE bundle_version=? AND as_of_date=?
            """,
            (audit["bundle_version"], audit["as_of_date"]),
        ).fetchone()
        temporal = conn.execute(
            """
            SELECT captured_at, payload_json FROM temporal_source_slot_snapshots
            WHERE source_key='fin_report_daily' AND snapshot_date=?
              AND snapshot_role='accepted_closed'
            """,
            (audit["target_date"],),
        ).fetchone()
        closure = conn.execute(
            """
            SELECT * FROM temporal_source_closure_state
            WHERE source_key='fin_report_daily' AND target_date=?
              AND slot_kind='yesterday_closed'
            """,
            (audit["target_date"],),
        ).fetchone()
        successors = conn.execute(
            """
            SELECT *
            FROM sheet_vitrina_v1_finance_daily_recovery_audit
            WHERE bundle_version=? AND as_of_date=? AND applied_at>?
            ORDER BY applied_at, operation_id
            """,
            (audit["bundle_version"], audit["as_of_date"], audit["applied_at"]),
        ).fetchall()
    if ready is None or temporal is None or closure is None:
        raise ValueError("Finance daily recovery accepted state is incomplete")
    plan = _deserialize_sheet_vitrina_plan(str(ready["plan_json"]))
    payload = _deserialize_temporal_source_payload(str(temporal["payload_json"]))
    expected_manifest = json.loads(str(audit["after_manifest_json"]))
    temporal_before_state = json.loads(str(audit["before_temporal_state_json"]))
    expected_values = {
        row_id: float(dict(cell).get("expected"))
        for row_id, cell in dict(expected_manifest.get("cells") or {}).items()
    }
    actual_manifest = _target_manifest(
        plan,
        target_date=str(audit["target_date"]),
        expected_values=expected_values,
    )
    accepted_cells = sum(
        dict(cell).get("value") == float(dict(cell).get("expected"))
        for cell in actual_manifest["cells"].values()
    )
    target_ids = set(expected_values)
    non_target_digest = _non_target_digest(
        plan,
        target_date=str(audit["target_date"]),
        target_row_ids=target_ids,
    )
    diagnostics = dict(getattr(payload, "diagnostics", SimpleNamespace()).__dict__ or {})
    pagination_value = diagnostics.get("pagination")
    pagination = (
        dict(pagination_value.__dict__)
        if isinstance(pagination_value, SimpleNamespace)
        else dict(pagination_value or {})
    )
    covered = sorted(int(item.nm_id) for item in list(getattr(payload, "items", []) or []))
    semantic = _derive_sheet_vitrina_refresh_semantic_summary(plan)
    current_plan_digest = _sheet_vitrina_plan_digest(plan)
    expected_chain_digest = str(audit["after_plan_digest"])
    successor_operations: list[str] = []
    chain_valid = True
    for successor in successors:
        if (
            str(successor["target_date"]) not in ALLOWED_RECOVERY_DATES
            or str(successor["before_plan_digest"]) != expected_chain_digest
        ):
            chain_valid = False
            break
        expected_chain_digest = str(successor["after_plan_digest"])
        successor_operations.append(str(successor["operation_id"]))
    chain_valid = bool(successor_operations) and chain_valid and (
        expected_chain_digest == current_plan_digest
    )
    direct_plan_match = current_plan_digest == audit["after_plan_digest"]
    if chain_valid:
        last_successor = successors[len(successor_operations) - 1]
        last_manifest = json.loads(str(last_successor["after_manifest_json"]))
        last_target_ids = set(dict(last_manifest.get("cells") or {}))
        effective_non_target_digest = _non_target_digest(
            plan,
            target_date=str(last_successor["target_date"]),
            target_row_ids=last_target_ids,
        )
        non_target_preserved = (
            effective_non_target_digest == last_successor["non_target_digest"]
        )
    else:
        non_target_preserved = non_target_digest == audit["non_target_digest"]
    proxy_value = _row_value(
        plan,
        row_id=PROXY_GAP_ROW_ID,
        target_date="2026-08-26",
    )
    checks = {
        "ready_plan_digest": direct_plan_match or chain_valid,
        "accepted_cells": accepted_cells == EXPECTED_TARGET_CELLS,
        "non_target_digest": non_target_preserved,
        "terminal_204": pagination.get("terminal_status") == 204 and bool(pagination.get("complete")),
        "source_digest": diagnostics.get("source_digest") == audit["source_digest"],
        "coverage": len(covered) == EXPECTED_SKU_COUNT and len(set(covered)) == EXPECTED_SKU_COUNT,
        "closure_success": str(closure["state"]) == "success" and closure["next_retry_at"] is None,
        "no_duplicates": len(covered) == len(set(covered)),
        "temporal_before_state_backup": (
            _finance_daily_temporal_state_digest(temporal_before_state)
            == audit["before_temporal_state_digest"]
        ),
    }
    return {
        "status": "complete" if all(checks.values()) else "blocked",
        "operation_id": str(audit["operation_id"]),
        "target_date": str(audit["target_date"]),
        "deployed_sha": str(audit["deployed_sha"]),
        "source_digest": str(audit["source_digest"]),
        "pages": int(audit["source_pages"]),
        "terminal_cursor": int(audit["source_terminal_cursor"]),
        "coverage": f"{len(set(covered))}/{EXPECTED_SKU_COUNT}",
        "accepted_cells": f"{accepted_cells}/{EXPECTED_TARGET_CELLS}",
        "temporal_before_state_digest": str(audit["before_temporal_state_digest"]),
        "checks": checks,
        "overall_semantic_status": semantic.get("status"),
        "overall_semantic_reason": semantic.get("reason"),
        "successor_operations": successor_operations,
        "proxy_gap_exclusion": {
            "row_id": PROXY_GAP_ROW_ID,
            "date": "2026-08-26",
            "value": proxy_value,
            "mutated": False,
        },
        "query_only": True,
    }
