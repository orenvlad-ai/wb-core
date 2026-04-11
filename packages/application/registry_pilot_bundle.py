"""Application-слой pilot registry bundle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from packages.contracts.registry_pilot_bundle import (
    ConfigV2Item,
    FormulaV2Item,
    MetricV2Item,
    RegistryPilotBundle,
    RegistryPilotValidationReport,
    RuntimeMetricItem,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUNDLE_DIR = ROOT / "registry" / "pilot_bundle"


def load_registry_pilot_bundle(bundle_dir: Path = DEFAULT_BUNDLE_DIR) -> RegistryPilotBundle:
    config_doc = _load_registry_doc(bundle_dir / "config_v2.json", "config_v2")
    metrics_doc = _load_registry_doc(bundle_dir / "metrics_v2.json", "metrics_v2")
    formulas_doc = _load_registry_doc(bundle_dir / "formulas_v2.json", "formulas_v2")
    runtime_doc = _load_registry_doc(bundle_dir / "metric_runtime_registry.json", "metric_runtime_registry")
    bridge_doc = _load_json(bundle_dir / "bridge_export_bundle.json")

    return RegistryPilotBundle(
        config_v2=[_parse_config_item(item) for item in _require_list(config_doc, "items")],
        metrics_v2=[_parse_metric_item(item) for item in _require_list(metrics_doc, "items")],
        formulas_v2=[_parse_formula_item(item) for item in _require_list(formulas_doc, "items")],
        metric_runtime_registry=[_parse_runtime_item(item) for item in _require_list(runtime_doc, "items")],
        bridge_export_bundle=bridge_doc,
    )


def validate_registry_pilot_bundle(
    bundle: RegistryPilotBundle,
    bundle_dir: Path = DEFAULT_BUNDLE_DIR,
) -> RegistryPilotValidationReport:
    _require_unique("config_v2.nm_id", (item.nm_id for item in bundle.config_v2))
    _require_unique("config_v2.display_order", (item.display_order for item in bundle.config_v2))
    _require_unique("metrics_v2.metric_key", (item.metric_key for item in bundle.metrics_v2))
    _require_unique("metrics_v2.display_order", (item.display_order for item in bundle.metrics_v2))
    _require_unique("formulas_v2.formula_id", (item.formula_id for item in bundle.formulas_v2))
    _require_unique("metric_runtime_registry.metric_key", (item.metric_key for item in bundle.metric_runtime_registry))

    runtime_by_key = {item.metric_key: item for item in bundle.metric_runtime_registry}
    formula_ids = {item.formula_id for item in bundle.formulas_v2}
    metric_keys_v2 = {item.metric_key for item in bundle.metrics_v2}
    runtime_keys = set(runtime_by_key)

    if metric_keys_v2 != runtime_keys:
        missing_in_runtime = sorted(metric_keys_v2 - runtime_keys)
        missing_in_display = sorted(runtime_keys - metric_keys_v2)
        raise ValueError(
            "metrics_v2 and metric_runtime_registry diverge: "
            f"missing_in_runtime={missing_in_runtime}, missing_in_display={missing_in_display}"
        )

    for metric in bundle.metrics_v2:
        runtime_entry = runtime_by_key[metric.metric_key]
        if metric.calc_type == "metric":
            referenced = runtime_by_key.get(metric.calc_ref)
            if referenced is None:
                raise ValueError(f"metrics_v2.calc_ref missing in runtime registry: {metric.calc_ref}")
            if referenced.metric_kind != "direct":
                raise ValueError(f"metric calc_ref must point to direct runtime metric: {metric.metric_key}")
            if metric.calc_ref != metric.metric_key:
                raise ValueError(f"direct metric calc_ref must equal metric_key: {metric.metric_key}")
        elif metric.calc_type == "formula":
            if metric.calc_ref not in formula_ids:
                raise ValueError(f"formula calc_ref missing in formulas_v2: {metric.calc_ref}")
            if runtime_entry.metric_kind != "formula":
                raise ValueError(f"formula metric must map to formula runtime kind: {metric.metric_key}")
            if runtime_entry.formula_id != metric.calc_ref:
                raise ValueError(f"runtime formula_id mismatch for {metric.metric_key}")
        elif metric.calc_type == "ratio":
            referenced = runtime_by_key.get(metric.calc_ref)
            if referenced is None:
                raise ValueError(f"ratio calc_ref missing in runtime registry: {metric.calc_ref}")
            if referenced.metric_kind != "ratio":
                raise ValueError(f"ratio calc_ref must point to ratio runtime metric: {metric.metric_key}")
            if referenced.metric_key != metric.metric_key:
                raise ValueError(f"ratio calc_ref must equal metric_key: {metric.metric_key}")
            if not referenced.ratio_num_key or not referenced.ratio_den_key:
                raise ValueError(f"ratio metric missing numerator/denominator: {metric.metric_key}")
        else:
            raise ValueError(f"unsupported calc_type: {metric.calc_type}")

    metric_kind_set = set()
    for runtime_item in bundle.metric_runtime_registry:
        metric_kind_set.add(runtime_item.metric_kind)
        if runtime_item.metric_kind == "direct":
            if runtime_item.formula_id or runtime_item.ratio_num_key or runtime_item.ratio_den_key:
                raise ValueError(f"direct metric must not carry formula/ratio links: {runtime_item.metric_key}")
        elif runtime_item.metric_kind == "formula":
            if not runtime_item.formula_id:
                raise ValueError(f"formula metric missing formula_id: {runtime_item.metric_key}")
            if runtime_item.formula_id not in formula_ids:
                raise ValueError(f"formula_id missing in formulas_v2: {runtime_item.metric_key}")
            if runtime_item.ratio_num_key or runtime_item.ratio_den_key:
                raise ValueError(f"formula metric must not carry ratio links: {runtime_item.metric_key}")
        elif runtime_item.metric_kind == "ratio":
            if runtime_item.formula_id is not None:
                raise ValueError(f"ratio metric must not carry formula_id: {runtime_item.metric_key}")
            if not runtime_item.ratio_num_key or not runtime_item.ratio_den_key:
                raise ValueError(f"ratio metric missing numerator/denominator: {runtime_item.metric_key}")
            if runtime_item.ratio_num_key not in runtime_by_key:
                raise ValueError(f"ratio numerator missing in runtime registry: {runtime_item.metric_key}")
            if runtime_item.ratio_den_key not in runtime_by_key:
                raise ValueError(f"ratio denominator missing in runtime registry: {runtime_item.metric_key}")
        else:
            raise ValueError(f"unsupported metric_kind: {runtime_item.metric_kind}")

    bridge_paths = _validate_bridge_export(bundle, bundle_dir)

    return RegistryPilotValidationReport(
        sku_count=len(bundle.config_v2),
        metric_count=len(bundle.metrics_v2),
        formula_count=len(bundle.formulas_v2),
        runtime_metric_count=len(bundle.metric_runtime_registry),
        metric_kinds=sorted(metric_kind_set),
        bridge_paths=bridge_paths,
    )


def _validate_bridge_export(bundle: RegistryPilotBundle, bundle_dir: Path) -> list[str]:
    bridge = bundle.bridge_export_bundle
    pilot_scope = _require_mapping(bridge, "pilot_scope")
    source_basis = _require_mapping(bridge, "source_basis")
    tabular_bridge = _require_mapping(source_basis, "tabular_bridge")
    server_runtime_seed = _require_mapping(source_basis, "server_runtime_seed")
    normalization = _require_mapping(bridge, "normalization")
    output_files = _require_mapping(bridge, "output_files")

    expected_paths = [
        "registry/pilot_bundle/config_v2.json",
        "registry/pilot_bundle/metrics_v2.json",
        "registry/pilot_bundle/formulas_v2.json",
        "registry/pilot_bundle/metric_runtime_registry.json",
        "registry/pilot_bundle/bridge_export_bundle.json",
    ]
    actual_paths = sorted(_require_str(output_files, key) for key in output_files)
    if sorted(expected_paths) != actual_paths:
        raise ValueError(f"bridge output paths mismatch: {actual_paths}")

    for path in expected_paths:
        if not (ROOT / path).exists():
            raise ValueError(f"bridge references missing file: {path}")

    if _require_int(pilot_scope, "sku_count") != len(bundle.config_v2):
        raise ValueError("bridge pilot_scope.sku_count mismatch")
    if _require_int(pilot_scope, "metric_count") != len(bundle.metrics_v2):
        raise ValueError("bridge pilot_scope.metric_count mismatch")
    if _require_int(pilot_scope, "formula_count") != len(bundle.formulas_v2):
        raise ValueError("bridge pilot_scope.formula_count mismatch")

    bridge_metric_kinds = sorted(_require_string_list(pilot_scope, "metric_kinds"))
    bundle_metric_kinds = sorted({item.metric_kind for item in bundle.metric_runtime_registry})
    if bridge_metric_kinds != bundle_metric_kinds:
        raise ValueError("bridge pilot_scope.metric_kinds mismatch")

    config_rows = _require_list(tabular_bridge, "config_rows")
    metrics_rows = _require_list(tabular_bridge, "metrics_rows")
    formulas_rows = _require_list(tabular_bridge, "formulas_rows")
    if len(config_rows) != len(bundle.config_v2):
        raise ValueError("bridge config_rows count mismatch")
    if len(metrics_rows) != len(bundle.metrics_v2):
        raise ValueError("bridge metrics_rows count mismatch")
    if len(formulas_rows) != len(bundle.formulas_v2):
        raise ValueError("bridge formulas_rows count mismatch")

    _validate_field_map(
        _require_mapping(normalization, "config_v2"),
        required_fields=["nm_id", "enabled", "display_name", "group", "display_order"],
    )
    _validate_field_map(
        _require_mapping(normalization, "metrics_v2"),
        required_fields=[
            "metric_key",
            "enabled",
            "scope",
            "label_ru",
            "calc_type",
            "calc_ref",
            "show_in_data",
            "format",
            "display_order",
            "section",
        ],
    )
    _validate_field_map(
        _require_mapping(normalization, "formulas_v2"),
        required_fields=["formula_id", "expression", "description"],
    )

    runtime_normalization = _require_mapping(normalization, "metric_runtime_registry")
    if _require_str(runtime_normalization, "source") != "server_runtime_seed":
        raise ValueError("bridge runtime normalization must point to server_runtime_seed")
    if _require_str(server_runtime_seed, "path") != "registry/pilot_bundle/metric_runtime_registry.json":
        raise ValueError("bridge server_runtime_seed path mismatch")

    if bundle_dir != DEFAULT_BUNDLE_DIR:
        for path in expected_paths:
            candidate = bundle_dir.parent.parent / path
            if not candidate.exists():
                raise ValueError(f"bundle_dir layout mismatch for path: {path}")

    return actual_paths


def _validate_field_map(section: Mapping[str, Any], required_fields: list[str]) -> None:
    field_map = _require_mapping(section, "field_map")
    missing = [field for field in required_fields if field not in field_map]
    if missing:
        raise ValueError(f"normalization field_map missing keys: {missing}")
    _require_str(section, "path")


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
    if calc_type not in {"metric", "formula", "ratio"}:
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


def _parse_runtime_item(raw: Any) -> RuntimeMetricItem:
    row = _require_mapping_value(raw, "runtime registry item")
    metric_kind = _require_str(row, "metric_kind")
    if metric_kind not in {"direct", "formula", "ratio"}:
        raise ValueError(f"unsupported metric_kind: {metric_kind}")
    return RuntimeMetricItem(
        metric_key=_require_str(row, "metric_key"),
        metric_kind=metric_kind,
        value_unit=_require_str(row, "value_unit"),
        value_scale=_require_str(row, "value_scale"),
        missing_policy=_require_str(row, "missing_policy"),
        period_agg=_require_str(row, "period_agg"),
        formula_id=_optional_str(row, "formula_id"),
        ratio_num_key=_optional_str(row, "ratio_num_key"),
        ratio_den_key=_optional_str(row, "ratio_den_key"),
        source_family=_require_str(row, "source_family"),
        source_module=_require_str(row, "source_module"),
        is_runtime_enabled=_require_bool(row, "is_runtime_enabled"),
    )


def _load_registry_doc(path: Path, expected_registry_id: str) -> Mapping[str, Any]:
    document = _load_json(path)
    registry_id = _require_str(document, "registry_id")
    if registry_id != expected_registry_id:
        raise ValueError(f"unexpected registry_id in {path.name}: {registry_id}")
    _require_str(document, "version")
    return document


def _load_json(path: Path) -> Mapping[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return _require_mapping_value(data, str(path))


def _require_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    return _require_mapping_value(value, key)


def _require_mapping_value(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be object")
    return value


def _require_list(mapping: Mapping[str, Any], key: str) -> list[Any]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be list")
    return value


def _require_str(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be string")
    return value


def _optional_str(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be string or null")
    return value


def _require_bool(mapping: Mapping[str, Any], key: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean")
    return value


def _require_int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be int")
    return value


def _require_string_list(mapping: Mapping[str, Any], key: str) -> list[str]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{key} must contain only strings")
        result.append(item)
    return result


def _require_unique(label: str, values: Iterable[Any]) -> None:
    seen: set[Any] = set()
    duplicates: set[Any] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise ValueError(f"duplicate values in {label}: {sorted(duplicates)}")
