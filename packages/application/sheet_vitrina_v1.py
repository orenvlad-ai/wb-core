"""Application-слой sheet-side scaffold для vitrina v1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1Request,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts" / "sheet_vitrina_v1"


class SheetVitrinaV1Block:
    def __init__(self, artifacts_dir: Path = ARTIFACTS) -> None:
        self.artifacts_dir = artifacts_dir

    def execute(self, request: SheetVitrinaV1Request) -> SheetVitrinaV1Envelope:
        delivery_bundle = _load_json(self.artifacts_dir / "input" / f"{request.scenario}__template__delivery-bundle__fixture.json")
        data_layout = _load_json(self.artifacts_dir / "layout" / "data_vitrina_sheet_layout.json")
        status_layout = _load_json(self.artifacts_dir / "layout" / "status_sheet_layout.json")
        return build_sheet_write_plan(delivery_bundle, data_layout, status_layout)


def build_sheet_write_plan(
    delivery_bundle: Mapping[str, Any],
    data_layout: Mapping[str, Any],
    status_layout: Mapping[str, Any],
) -> SheetVitrinaV1Envelope:
    delivery_contract_version = _require_str(delivery_bundle, "delivery_contract_version")
    snapshot_id = _require_str(delivery_bundle, "snapshot_id")
    as_of_date = _require_str(delivery_bundle, "as_of_date")
    data_vitrina = _require_mapping(delivery_bundle, "data_vitrina")
    status = _require_mapping(delivery_bundle, "status")

    data_target = _build_write_target(data_vitrina, data_layout)
    status_target = _build_write_target(status, status_layout)
    date_columns = _require_date_columns_or_infer(delivery_bundle, data_target.header)
    temporal_slots = _require_temporal_slots_or_default(
        delivery_bundle=delivery_bundle,
        as_of_date=as_of_date,
        date_columns=date_columns,
    )
    source_temporal_policies = _require_string_mapping_or_default(
        delivery_bundle,
        "source_temporal_policies",
    )

    if len({data_target.sheet_name, status_target.sheet_name}) != 2:
        raise ValueError("sheet scaffold must contain exactly two distinct sheets")

    return SheetVitrinaV1Envelope(
        plan_version=f"{delivery_contract_version}__sheet_scaffold_v1",
        snapshot_id=snapshot_id,
        as_of_date=as_of_date,
        date_columns=date_columns,
        temporal_slots=temporal_slots,
        source_temporal_policies=source_temporal_policies,
        sheets=[data_target, status_target],
    )


def parse_sheet_write_plan_payload(payload: Mapping[str, Any]) -> SheetVitrinaV1Envelope:
    plan_version = _require_str(payload, "plan_version")
    snapshot_id = _require_str(payload, "snapshot_id")
    as_of_date = _require_str(payload, "as_of_date")
    sheets_raw = payload.get("sheets")
    if not isinstance(sheets_raw, list) or len(sheets_raw) != 2:
        raise ValueError("sheet write plan payload must contain two sheet targets")

    sheets = [_parse_write_target(item) for item in sheets_raw]
    if len({item.sheet_name for item in sheets}) != 2:
        raise ValueError("sheet write plan payload must contain exactly two distinct sheets")
    data_target = next((item for item in sheets if item.sheet_name == "DATA_VITRINA"), None)
    if data_target is None:
        raise ValueError("sheet write plan payload must contain DATA_VITRINA target")
    date_columns = _require_date_columns_or_infer(payload, data_target.header)
    temporal_slots = _require_temporal_slots_or_default(
        delivery_bundle=payload,
        as_of_date=as_of_date,
        date_columns=date_columns,
    )
    source_temporal_policies = _require_string_mapping_or_default(payload, "source_temporal_policies")

    return SheetVitrinaV1Envelope(
        plan_version=plan_version,
        snapshot_id=snapshot_id,
        as_of_date=as_of_date,
        date_columns=date_columns,
        temporal_slots=temporal_slots,
        source_temporal_policies=source_temporal_policies,
        sheets=sheets,
    )


def _build_write_target(section: Mapping[str, Any], layout: Mapping[str, Any]) -> SheetVitrinaWriteTarget:
    sheet_name = _require_str(section, "sheet_name")
    if sheet_name != _require_str(layout, "sheet_name"):
        raise ValueError(f"layout mismatch for sheet {sheet_name}")

    header = _require_string_list(section, "header")
    expected_header = _require_string_list(layout, "expected_header")
    if not _is_supported_header(sheet_name, header, expected_header):
        raise ValueError(f"header mismatch for sheet {sheet_name}")

    rows = _require_rows(section, "rows", expected_width=len(header), sheet_name=sheet_name)
    column_count = len(header)
    row_count = len(rows)
    write_rect = f"{_require_str(layout, 'write_start_cell')}:{_column_name(column_count)}{row_count + 1}"

    return SheetVitrinaWriteTarget(
        sheet_name=sheet_name,
        write_start_cell=_require_str(layout, "write_start_cell"),
        write_rect=write_rect,
        clear_range=_require_str(layout, "clear_range"),
        write_mode=_require_str(layout, "write_mode"),
        partial_update_allowed=_require_bool(layout, "partial_update_allowed"),
        header=header,
        rows=rows,
        row_count=row_count,
        column_count=column_count,
    )


def _require_date_columns_or_infer(payload: Mapping[str, Any], header: list[str]) -> list[str]:
    expected = header[2:] if header[:2] == ["label", "key"] else []
    raw = payload.get("date_columns")
    if raw is None:
        return expected
    if not isinstance(raw, list):
        raise ValueError("date_columns must be a list when present")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not _is_iso_date(item):
            raise ValueError("date_columns must contain ISO date strings only")
        out.append(item)
    if out != expected:
        raise ValueError("date_columns must match DATA_VITRINA header dates")
    return out


def _require_temporal_slots_or_default(
    *,
    delivery_bundle: Mapping[str, Any],
    as_of_date: str,
    date_columns: list[str],
) -> list[SheetVitrinaV1TemporalSlot]:
    raw = delivery_bundle.get("temporal_slots")
    if raw is None:
        default_date = date_columns[0] if date_columns else as_of_date
        return [
            SheetVitrinaV1TemporalSlot(
                slot_key="as_of_date",
                slot_label="as_of_date",
                column_date=default_date,
            )
        ]
    if not isinstance(raw, list) or not raw:
        raise ValueError("temporal_slots must be a non-empty list when present")
    out: list[SheetVitrinaV1TemporalSlot] = []
    slot_dates: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("temporal_slots must contain objects only")
        slot_key = _require_str(item, "slot_key")
        slot_label = _require_str(item, "slot_label")
        column_date = _require_str(item, "column_date")
        if not _is_iso_date(column_date):
            raise ValueError("temporal_slots.column_date must be an ISO date")
        out.append(
            SheetVitrinaV1TemporalSlot(
                slot_key=slot_key,
                slot_label=slot_label,
                column_date=column_date,
            )
        )
        slot_dates.append(column_date)
    if slot_dates != date_columns:
        raise ValueError("temporal_slots must follow DATA_VITRINA date_columns order")
    return out


def _require_string_mapping_or_default(mapping: Mapping[str, Any], key: str) -> dict[str, str]:
    value = mapping.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object when present")
    out: dict[str, str] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str) or not isinstance(item_value, str):
            raise ValueError(f"{key} must contain string pairs only")
        out[item_key] = item_value
    return out


def _parse_write_target(value: Any) -> SheetVitrinaWriteTarget:
    if not isinstance(value, Mapping):
        raise ValueError("sheet write target must be an object")

    header = _require_string_list(value, "header")
    rows = _require_rows(value, "rows", expected_width=len(header), sheet_name=_require_str(value, "sheet_name"))
    row_count = _require_int(value, "row_count")
    column_count = _require_int(value, "column_count")
    if row_count != len(rows):
        raise ValueError("row_count must match rows length")
    if column_count != len(header):
        raise ValueError("column_count must match header length")

    return SheetVitrinaWriteTarget(
        sheet_name=_require_str(value, "sheet_name"),
        write_start_cell=_require_str(value, "write_start_cell"),
        write_rect=_require_str(value, "write_rect"),
        clear_range=_require_str(value, "clear_range"),
        write_mode=_require_str(value, "write_mode"),
        partial_update_allowed=_require_bool(value, "partial_update_allowed"),
        header=header,
        rows=rows,
        row_count=row_count,
        column_count=column_count,
    )


def _is_supported_header(sheet_name: str, header: list[str], expected_header: list[str]) -> bool:
    if header == expected_header:
        return True
    if sheet_name != "DATA_VITRINA":
        return False
    if len(header) < 3:
        return False
    if header[:2] != ["label", "key"]:
        return False
    for item in header[2:]:
        if not _is_iso_date(item):
            return False
    return True


def _is_iso_date(value: str) -> bool:
    parts = value.split("-")
    if len(parts) != 3:
        return False
    year, month, day = parts
    return len(year) == 4 and len(month) == 2 and len(day) == 2 and value.replace("-", "").isdigit()


def _column_name(index: int) -> str:
    if index <= 0:
        raise ValueError("column index must be positive")
    out = ""
    current = index
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        out = chr(65 + remainder) + out
    return out


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return value


def _require_rows(mapping: Mapping[str, Any], key: str, expected_width: int, sheet_name: str) -> list[list[Any]]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    rows: list[list[Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, list):
            raise ValueError(f"{sheet_name}: row {index} must be a list")
        if len(item) != expected_width:
            raise ValueError(f"{sheet_name}: row {index} width mismatch")
        rows.append(item)
    return rows


def _require_string_list(mapping: Mapping[str, Any], key: str) -> list[str]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{key} must contain strings only")
        out.append(item)
    return out


def _require_str(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_bool(mapping: Mapping[str, Any], key: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _require_int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value
