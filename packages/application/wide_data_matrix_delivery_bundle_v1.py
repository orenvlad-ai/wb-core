"""Application-слой delivery bundle wide data matrix v1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from packages.contracts.wide_data_matrix_delivery_bundle_v1 import (
    WideDataMatrixDeliveryBundleV1Envelope,
    WideDataMatrixDeliveryBundleV1Request,
    WideDataMatrixDeliverySheet,
)

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts" / "wide_data_matrix_delivery_bundle_v1"


class WideDataMatrixDeliveryBundleV1Block:
    def __init__(self, artifacts_dir: Path = ARTIFACTS) -> None:
        self.artifacts_dir = artifacts_dir

    def execute(self, request: WideDataMatrixDeliveryBundleV1Request) -> WideDataMatrixDeliveryBundleV1Envelope:
        payload = _load_json(self.artifacts_dir / "input_bundle" / f"{request.scenario}__template__input-bundle__fixture.json")
        return transform_input_bundle(payload)


def transform_input_bundle(payload: Mapping[str, Any]) -> WideDataMatrixDeliveryBundleV1Envelope:
    source_basis = _require_mapping(payload, "source_basis")
    delivery_meta = _require_mapping(payload, "delivery_meta")
    sheet_names = _require_mapping(delivery_meta, "sheet_names")

    wide_fixture = _load_json_from_repo(_require_str(source_basis, "wide_data_matrix_path"))
    projection_fixture = _load_json_from_repo(_require_str(source_basis, "table_projection_bundle_path"))
    sku_display_fixture = _load_json_from_repo(_require_str(source_basis, "sku_display_bundle_path"))

    registry_paths = _require_mapping(source_basis, "registry_paths")
    config_registry = _load_json_from_repo(_require_str(registry_paths, "config_v2"))
    metrics_registry = _load_json_from_repo(_require_str(registry_paths, "metrics_v2"))
    runtime_registry = _load_json_from_repo(_require_str(registry_paths, "metric_runtime_registry"))

    wide_result = _require_mapping(wide_fixture, "result")
    wide_dates = _require_string_list(wide_result, "dates")
    if not wide_dates:
        raise ValueError("wide matrix dates must not be empty")
    columns = _require_list(wide_result, "columns")
    wide_rows = _require_list(wide_result, "rows")

    data_header = [str(item["label"]) for item in columns]
    if data_header[:2] != ["label", "key"]:
        raise ValueError("wide matrix header must start with label, key")

    projection_result = _require_mapping(projection_fixture, "result")
    source_statuses = _require_list(projection_result, "source_statuses")
    config_items = _require_list(config_registry, "items")
    metrics_items = _require_list(metrics_registry, "items")
    runtime_items = _require_list(runtime_registry, "items")

    data_rows = _build_data_rows(wide_rows, wide_dates)
    status_header = [
        "source_key",
        "kind",
        "freshness",
        "snapshot_date",
        "date",
        "date_from",
        "date_to",
        "requested_count",
        "covered_count",
        "missing_nm_ids",
        "note",
    ]
    status_rows = _build_status_rows(source_statuses)

    _validate_config_alignment(config_items, sku_display_fixture)
    _validate_metric_alignment(metrics_items, runtime_items, wide_rows)

    data_vitrina = WideDataMatrixDeliverySheet(
        sheet_name=_require_str(sheet_names, "data_vitrina"),
        header=data_header,
        rows=data_rows,
    )
    status = WideDataMatrixDeliverySheet(
        sheet_name=_require_str(sheet_names, "status"),
        header=status_header,
        rows=status_rows,
    )
    _validate_sheet_payload(data_vitrina)
    _validate_sheet_payload(status)

    as_of_date = str(projection_result.get("as_of_date") or wide_dates[-1])
    scenario = _require_str(payload, "scenario")
    delivery_contract_version = _require_str(delivery_meta, "delivery_contract_version")
    snapshot_id = f"{as_of_date}__{delivery_contract_version}__{scenario}"

    return WideDataMatrixDeliveryBundleV1Envelope(
        delivery_contract_version=delivery_contract_version,
        snapshot_id=snapshot_id,
        as_of_date=as_of_date,
        data_vitrina=data_vitrina,
        status=status,
    )


def _build_data_rows(rows: list[Any], dates: list[str]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("wide row must be a mapping")
        values = _require_mapping(row, "values")
        out.append(
            [
                _require_str(row, "label"),
                _require_str(row, "key"),
                *[values.get(date, "") if values.get(date, "") is not None else "" for date in dates],
            ]
        )
    return out


def _build_status_rows(source_statuses: list[Any]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for item in source_statuses:
        if not isinstance(item, Mapping):
            raise ValueError("source status must be a mapping")
        out.append(
            [
                _require_str(item, "source_key"),
                _require_str(item, "kind"),
                str(item.get("freshness") or ""),
                str(item.get("snapshot_date") or ""),
                str(item.get("date") or ""),
                str(item.get("date_from") or ""),
                str(item.get("date_to") or ""),
                int(item.get("requested_count", 0)),
                int(item.get("covered_count", 0)),
                _format_missing_nm_ids(item.get("missing_nm_ids")),
                _format_note(item.get("extra")),
            ]
        )
    return out


def _validate_config_alignment(config_items: list[Any], sku_display_fixture: Mapping[str, Any]) -> None:
    config_nm_ids = {int(item["nm_id"]) for item in config_items if isinstance(item, Mapping)}
    sku_result = _require_mapping(sku_display_fixture, "result")
    sku_items = _require_list(sku_result, "items")
    for item in sku_items:
        if not isinstance(item, Mapping):
            raise ValueError("sku display item must be a mapping")
        nm_id = int(item["nm_id"])
        if nm_id not in config_nm_ids:
            raise ValueError(f"config_v2 missing sku display nm_id: {nm_id}")


def _validate_metric_alignment(metrics_items: list[Any], runtime_items: list[Any], wide_rows: list[Any]) -> None:
    metric_keys = {str(item["metric_key"]) for item in metrics_items if isinstance(item, Mapping)}
    runtime_keys = {str(item["metric_key"]) for item in runtime_items if isinstance(item, Mapping)}
    wide_metric_keys = {str(item["metric_key"]) for item in wide_rows if isinstance(item, Mapping)}
    if not wide_metric_keys.issubset(metric_keys):
        missing = sorted(wide_metric_keys - metric_keys)
        raise ValueError(f"metrics_v2 missing wide metric keys: {missing}")
    if not wide_metric_keys.issubset(runtime_keys):
        missing = sorted(wide_metric_keys - runtime_keys)
        raise ValueError(f"runtime registry missing wide metric keys: {missing}")


def _validate_sheet_payload(payload: WideDataMatrixDeliverySheet) -> None:
    if not payload.sheet_name:
        raise ValueError("sheet_name must not be empty")
    if not payload.header:
        raise ValueError("sheet header must not be empty")
    expected_width = len(payload.header)
    for index, row in enumerate(payload.rows):
        if len(row) != expected_width:
            raise ValueError(f"row width mismatch for {payload.sheet_name} row {index}: {len(row)} != {expected_width}")


def _format_missing_nm_ids(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, list):
        raise ValueError("missing_nm_ids must be a list when present")
    return ",".join(str(int(item)) for item in value)


def _format_note(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, Mapping):
        raise ValueError("extra must be a mapping when present")
    pairs = [f"{key}={value[key]}" for key in sorted(value)]
    return "; ".join(pairs)


def _load_json_from_repo(relative_path: str) -> Any:
    return _load_json(ROOT / relative_path)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return value


def _require_list(mapping: Mapping[str, Any], key: str) -> list[Any]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _require_string_list(mapping: Mapping[str, Any], key: str) -> list[str]:
    value = _require_list(mapping, key)
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
