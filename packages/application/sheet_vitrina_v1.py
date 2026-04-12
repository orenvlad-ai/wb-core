"""Application-слой sheet-side scaffold для vitrina v1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1Request,
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

    if len({data_target.sheet_name, status_target.sheet_name}) != 2:
        raise ValueError("sheet scaffold must contain exactly two distinct sheets")

    return SheetVitrinaV1Envelope(
        plan_version=f"{delivery_contract_version}__sheet_scaffold_v1",
        snapshot_id=snapshot_id,
        as_of_date=as_of_date,
        sheets=[data_target, status_target],
    )


def _build_write_target(section: Mapping[str, Any], layout: Mapping[str, Any]) -> SheetVitrinaWriteTarget:
    sheet_name = _require_str(section, "sheet_name")
    if sheet_name != _require_str(layout, "sheet_name"):
        raise ValueError(f"layout mismatch for sheet {sheet_name}")

    header = _require_string_list(section, "header")
    expected_header = _require_string_list(layout, "expected_header")
    if header != expected_header:
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
