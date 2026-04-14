"""Application-слой registry upload bundle v1."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from packages.contracts.registry_upload_bundle_v1 import (
    ConfigV2Item,
    FormulaV2Item,
    MetricV2Item,
    RegistryUploadBundleV1,
    RegistryUploadBundleV1ValidationReport,
)

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = ROOT / "artifacts" / "registry_upload_bundle_v1"
INPUT_DIR = ARTIFACTS_DIR / "input"
TARGET_DIR = ARTIFACTS_DIR / "target"
TARGET_FIXTURE = TARGET_DIR / "registry_upload_bundle__fixture.json"
RUNTIME_REGISTRY_PATH = ARTIFACTS_DIR / "input" / "metric_runtime_registry__fixture.json"
ALLOWED_SCOPES = {"SKU", "GROUP", "TOTAL"}
ALLOWED_CALC_TYPES = {"metric", "formula", "ratio"}


class RegistryUploadBundleV1Block:
    def __init__(
        self,
        artifacts_dir: Path = ARTIFACTS_DIR,
        runtime_registry_path: Path = RUNTIME_REGISTRY_PATH,
    ) -> None:
        self.artifacts_dir = artifacts_dir
        self.input_dir = artifacts_dir / "input"
        self.target_dir = artifacts_dir / "target"
        self.runtime_registry_path = runtime_registry_path

    def build_bundle(self) -> RegistryUploadBundleV1:
        config_doc = _load_json(self.input_dir / "config_v2__fixture.json")
        metrics_doc = _load_json(self.input_dir / "metrics_v2__fixture.json")
        formulas_doc = _load_json(self.input_dir / "formulas_v2__fixture.json")

        config_mapping = _require_mapping_value(config_doc, "config input")
        metrics_mapping = _require_mapping_value(metrics_doc, "metrics input")
        formulas_mapping = _require_mapping_value(formulas_doc, "formulas input")

        _require_registry_id(config_mapping, "config_v2")
        _require_registry_id(metrics_mapping, "metrics_v2")
        _require_registry_id(formulas_mapping, "formulas_v2")

        config_meta = _read_bundle_meta(config_mapping)
        metrics_meta = _read_bundle_meta(metrics_mapping)
        formulas_meta = _read_bundle_meta(formulas_mapping)
        if config_meta != metrics_meta or config_meta != formulas_meta:
            raise ValueError("bundle_meta must match across all input fixtures")

        return RegistryUploadBundleV1(
            bundle_version=config_meta["bundle_version"],
            uploaded_at=config_meta["uploaded_at"],
            config_v2=[_parse_config_item(item) for item in _require_list(config_mapping, "items")],
            metrics_v2=[_parse_metric_item(item) for item in _require_list(metrics_mapping, "items")],
            formulas_v2=[_parse_formula_item(item) for item in _require_list(formulas_mapping, "items")],
        )

    def load_target_fixture(self) -> dict[str, Any]:
        return _require_mapping_value(_load_json(self.target_dir / "registry_upload_bundle__fixture.json"), "target bundle")

    def validate_bundle(
        self,
        bundle: RegistryUploadBundleV1,
        bundle_path: Path = TARGET_FIXTURE,
        enforce_fixture_uniqueness: bool = True,
    ) -> RegistryUploadBundleV1ValidationReport:
        _validate_uploaded_at(bundle.uploaded_at)
        if enforce_fixture_uniqueness:
            _require_unique_bundle_version(bundle.bundle_version, bundle_path, self.target_dir)

        if not 1 <= len(bundle.config_v2) <= 256:
            raise ValueError("registry upload bundle must contain 1-256 config_v2 entries")
        if not 1 <= len(bundle.metrics_v2) <= 256:
            raise ValueError("registry upload bundle must contain 1-256 metrics_v2 entries")
        if not 0 <= len(bundle.formulas_v2) <= 128:
            raise ValueError("registry upload bundle must contain 0-128 formulas_v2 entries")

        _require_unique("config_v2.nm_id", (item.nm_id for item in bundle.config_v2))
        _require_unique("config_v2.display_order", (item.display_order for item in bundle.config_v2))
        _require_unique("metrics_v2.metric_key", (item.metric_key for item in bundle.metrics_v2))
        _require_unique("metrics_v2.display_order", (item.display_order for item in bundle.metrics_v2))
        _require_unique("formulas_v2.formula_id", (item.formula_id for item in bundle.formulas_v2))

        runtime_doc = _require_mapping_value(_load_json(self.runtime_registry_path), "runtime registry")
        runtime_items = _require_list(runtime_doc, "items")
        runtime_by_key: dict[str, Mapping[str, Any]] = {}
        for raw in runtime_items:
            runtime_row = _require_mapping_value(raw, "runtime metric")
            metric_key = _require_str(runtime_row, "metric_key")
            if metric_key in runtime_by_key:
                raise ValueError(f"duplicate runtime metric_key: {metric_key}")
            runtime_by_key[metric_key] = runtime_row

        formula_ids = {item.formula_id for item in bundle.formulas_v2}
        scope_values = sorted({item.scope for item in bundle.metrics_v2})
        calc_types = sorted({item.calc_type for item in bundle.metrics_v2})

        checked_runtime_keys: set[str] = set()
        for metric in bundle.metrics_v2:
            if metric.scope not in ALLOWED_SCOPES:
                raise ValueError(f"unsupported scope: {metric.scope}")
            if metric.calc_type not in ALLOWED_CALC_TYPES:
                raise ValueError(f"unsupported calc_type: {metric.calc_type}")

            runtime_for_metric = runtime_by_key.get(metric.metric_key)
            if runtime_for_metric is None:
                raise ValueError(f"runtime registry missing metric_key: {metric.metric_key}")
            if not _require_bool(runtime_for_metric, "is_runtime_enabled"):
                raise ValueError(f"runtime metric is disabled: {metric.metric_key}")
            runtime_kind = _require_str(runtime_for_metric, "metric_kind")
            if metric.calc_type == "formula" and runtime_kind != "formula":
                raise ValueError(f"formula metric must map to formula runtime kind: {metric.metric_key}")
            if metric.calc_type == "ratio" and runtime_kind != "ratio":
                raise ValueError(f"ratio metric must map to ratio runtime kind: {metric.metric_key}")
            if metric.calc_type == "metric" and runtime_kind not in {"direct", "formula", "ratio"}:
                raise ValueError(f"metric_key/runtime kind mismatch for {metric.metric_key}")
            checked_runtime_keys.add(metric.metric_key)

            if metric.calc_type == "metric":
                runtime_target = runtime_by_key.get(metric.calc_ref)
                if runtime_target is None:
                    raise ValueError(f"metric calc_ref missing in runtime registry: {metric.calc_ref}")
                target_kind = _require_str(runtime_target, "metric_kind")
                if target_kind not in {"direct", "formula", "ratio"}:
                    raise ValueError(f"metric calc_ref must point to runtime metric: {metric.metric_key}")
                checked_runtime_keys.add(metric.calc_ref)
            elif metric.calc_type == "ratio":
                ratio_num_key = _require_nullable_str(runtime_for_metric, "ratio_num_key")
                ratio_den_key = _require_nullable_str(runtime_for_metric, "ratio_den_key")
                if not ratio_num_key or not ratio_den_key:
                    raise ValueError(f"ratio runtime metric missing numerator/denominator: {metric.metric_key}")
                if "/" in metric.calc_ref:
                    calc_num_key, calc_den_key = [part.strip() for part in metric.calc_ref.split("/", 1)]
                    if ratio_num_key != calc_num_key or ratio_den_key != calc_den_key:
                        raise ValueError(f"ratio calc_ref mismatch for {metric.metric_key}")
                else:
                    runtime_target = runtime_by_key.get(metric.calc_ref)
                    if runtime_target is None:
                        raise ValueError(f"ratio calc_ref missing in runtime registry: {metric.calc_ref}")
                    if _require_str(runtime_target, "metric_kind") != "ratio":
                        raise ValueError(f"ratio calc_ref must point to ratio runtime metric: {metric.metric_key}")
                checked_runtime_keys.add(ratio_num_key)
                checked_runtime_keys.add(ratio_den_key)
            elif metric.calc_type == "formula":
                if metric.calc_ref not in formula_ids:
                    raise ValueError(f"formula calc_ref missing in formulas_v2: {metric.calc_ref}")
                if _require_str(runtime_for_metric, "metric_kind") != "formula":
                    raise ValueError(f"formula metric must map to formula runtime kind: {metric.metric_key}")
                runtime_formula_id = _require_nullable_str(runtime_for_metric, "formula_id")
                if runtime_formula_id != metric.calc_ref:
                    raise ValueError(f"runtime formula_id mismatch for {metric.metric_key}")

        for formula in bundle.formulas_v2:
            if not formula.formula_id:
                raise ValueError("formula_id must not be empty")

        return RegistryUploadBundleV1ValidationReport(
            bundle_version=bundle.bundle_version,
            config_count=len(bundle.config_v2),
            metric_count=len(bundle.metrics_v2),
            formula_count=len(bundle.formulas_v2),
            scope_values=scope_values,
            calc_types=calc_types,
            runtime_metric_keys_checked=len(checked_runtime_keys),
        )


def load_registry_upload_bundle_v1_from_path(path: Path) -> RegistryUploadBundleV1:
    return parse_registry_upload_bundle_v1_payload(_load_json(path))


def parse_registry_upload_bundle_v1_payload(payload: Any) -> RegistryUploadBundleV1:
    mapping = _require_mapping_value(payload, "registry upload bundle payload")
    return RegistryUploadBundleV1(
        bundle_version=_require_str(mapping, "bundle_version"),
        uploaded_at=_require_str(mapping, "uploaded_at"),
        config_v2=[_parse_config_item(item) for item in _require_list(mapping, "config_v2")],
        metrics_v2=[_parse_metric_item(item) for item in _require_list(mapping, "metrics_v2")],
        formulas_v2=[_parse_formula_item(item) for item in _require_list(mapping, "formulas_v2")],
    )


def _read_bundle_meta(document: Mapping[str, Any]) -> dict[str, str]:
    meta = _require_mapping(document, "bundle_meta")
    return {
        "bundle_version": _require_str(meta, "bundle_version"),
        "uploaded_at": _require_str(meta, "uploaded_at"),
    }


def _validate_uploaded_at(value: str) -> None:
    if not value.endswith("Z"):
        raise ValueError("uploaded_at must be an ISO 8601 UTC timestamp ending with Z")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("uploaded_at must be a valid ISO 8601 timestamp") from exc


def _require_unique_bundle_version(bundle_version: str, bundle_path: Path, target_dir: Path) -> None:
    matching_paths: list[str] = []
    for path in sorted(target_dir.glob("*.json")):
        payload = _load_json(path)
        if isinstance(payload, Mapping) and payload.get("bundle_version") == bundle_version:
            matching_paths.append(str(path.relative_to(ROOT)))
    expected_path = str(bundle_path.relative_to(ROOT))
    if expected_path not in matching_paths:
        raise ValueError(f"bundle_version is not materialized in expected target fixture: {bundle_version}")
    if len(matching_paths) != 1:
        raise ValueError(f"bundle_version must be unique inside target fixtures: {matching_paths}")


def _require_unique(label: str, values: Iterable[Any]) -> None:
    seen: set[Any] = set()
    duplicates: set[Any] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise ValueError(f"{label} contains duplicates: {sorted(duplicates)}")


def _require_registry_id(document: Mapping[str, Any], expected: str) -> None:
    actual = _require_str(document, "registry_id")
    if actual != expected:
        raise ValueError(f"registry_id mismatch: expected {expected}, got {actual}")


def _parse_config_item(raw: Any) -> ConfigV2Item:
    row = _require_mapping_value(raw, "config_v2 item")
    return ConfigV2Item(
        nm_id=_require_int(row, "nm_id"),
        enabled=_require_bool(row, "enabled"),
        display_name=_require_str(row, "display_name"),
        group=_require_str(row, "group"),
        display_order=_require_int(row, "display_order"),
    )


def _parse_metric_item(raw: Any) -> MetricV2Item:
    row = _require_mapping_value(raw, "metrics_v2 item")
    calc_type = _require_str(row, "calc_type")
    if calc_type not in ALLOWED_CALC_TYPES:
        raise ValueError(f"unsupported calc_type: {calc_type}")
    return MetricV2Item(
        metric_key=_require_str(row, "metric_key"),
        enabled=_require_bool(row, "enabled"),
        scope=_require_str(row, "scope"),
        label_ru=_require_str(row, "label_ru"),
        calc_type=calc_type,
        calc_ref=_require_str(row, "calc_ref"),
        show_in_data=_require_bool(row, "show_in_data"),
        format=_require_str(row, "format"),
        display_order=_require_int(row, "display_order"),
        section=_require_str(row, "section"),
    )


def _parse_formula_item(raw: Any) -> FormulaV2Item:
    row = _require_mapping_value(raw, "formulas_v2 item")
    return FormulaV2Item(
        formula_id=_require_str(row, "formula_id"),
        expression=_require_str(row, "expression"),
        description=_require_str(row, "description"),
    )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_mapping(document: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = document.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return value


def _require_list(document: Mapping[str, Any], key: str) -> list[Any]:
    value = document.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _require_mapping_value(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _require_str(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_nullable_str(document: Mapping[str, Any], key: str) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be null or a non-empty string")
    return value


def _require_int(document: Mapping[str, Any], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _require_bool(document: Mapping[str, Any], key: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value
