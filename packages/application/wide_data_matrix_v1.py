"""Application-слой wide data matrix v1 fixture."""

from __future__ import annotations

import ast
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any, Mapping, Optional

from packages.contracts.wide_data_matrix_v1 import (
    WideDataMatrixBlock,
    WideDataMatrixColumn,
    WideDataMatrixRow,
    WideDataMatrixV1Empty,
    WideDataMatrixV1Envelope,
    WideDataMatrixV1Request,
    WideDataMatrixV1Success,
)

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts" / "wide_data_matrix_v1"
PROJECTION_FIELD_MAP = {
    "stock_total": ("official_api", "stocks", "stock_total"),
    "price_seller_discounted": ("official_api", "prices", "price_seller_discounted"),
    "spp": ("official_api", "spp", "spp"),
    "ads_views": ("official_api", "ads_compact", "ads_views"),
    "ads_clicks": ("official_api", "ads_compact", "ads_clicks"),
    "fin_buyout_rub": ("official_api", "fin_report_daily", "fin_buyout_rub"),
    "fin_commission": ("official_api", "fin_report_daily", "fin_commission"),
}


class WideDataMatrixV1FixtureBlock:
    def __init__(self, artifacts_dir: Path = ARTIFACTS) -> None:
        self.artifacts_dir = artifacts_dir

    def execute(self, request: WideDataMatrixV1Request) -> WideDataMatrixV1Envelope:
        payload = _load_json(self.artifacts_dir / "input_bundle" / f"{request.scenario}__template__input-bundle__fixture.json")
        return transform_input_bundle(payload)


def transform_input_bundle(payload: Mapping[str, Any]) -> WideDataMatrixV1Envelope:
    source_basis = _require_mapping(payload, "source_basis")
    display_scope = _require_mapping(payload, "display_scope")
    dates = _require_string_list(payload, "dates")
    if not dates:
        raise ValueError("dates must not be empty")

    columns = _build_columns(dates)
    registry_paths = _require_mapping(source_basis, "registry_paths")
    config_registry = _load_json_from_repo(_require_str(registry_paths, "config_v2"))
    metrics_registry = _load_json_from_repo(_require_str(registry_paths, "metrics_v2"))
    formulas_registry = _load_json_from_repo(_require_str(registry_paths, "formulas_v2"))
    runtime_registry = _load_json_from_repo(_require_str(registry_paths, "metric_runtime_registry"))

    config_items = _require_list(config_registry, "items")
    metrics_items = _require_list(metrics_registry, "items")
    formulas_items = _require_list(formulas_registry, "items")
    runtime_items = _require_list(runtime_registry, "items")

    nm_ids = _require_int_list(display_scope, "nm_ids")
    display_metric_keys = _require_string_list(display_scope, "display_metric_keys")
    safe_aggregate_metric_keys = _require_string_list(display_scope, "safe_aggregate_metric_keys")

    config_by_nm_id = {int(item["nm_id"]): item for item in config_items}
    selected_config = [config_by_nm_id[nm_id] for nm_id in nm_ids if nm_id in config_by_nm_id]
    if len(selected_config) != len(nm_ids):
        missing = sorted(set(nm_ids) - set(config_by_nm_id))
        raise ValueError(f"config_v2 missing nm_ids: {missing}")
    _require_unique("config_v2.display_order", [int(item["display_order"]) for item in selected_config])

    sku_display_fixture = _load_json_from_repo(_require_str(source_basis, "sku_display_bundle_path"))
    _validate_config_against_sku_bundle(selected_config, sku_display_fixture)

    metrics_by_key = {str(item["metric_key"]): item for item in metrics_items}
    runtime_by_key = {str(item["metric_key"]): item for item in runtime_items}
    formulas_by_id = {str(item["formula_id"]): item for item in formulas_items}

    display_metrics = [metrics_by_key[key] for key in display_metric_keys if key in metrics_by_key]
    if len(display_metrics) != len(display_metric_keys):
        missing = sorted(set(display_metric_keys) - set(metrics_by_key))
        raise ValueError(f"metrics_v2 missing display metrics: {missing}")
    display_metrics.sort(key=lambda item: int(item["display_order"]))

    if not set(display_metric_keys).issubset(set(runtime_by_key)):
        missing = sorted(set(display_metric_keys) - set(runtime_by_key))
        raise ValueError(f"runtime registry missing display metrics: {missing}")

    for metric_key in safe_aggregate_metric_keys:
        runtime_metric = _require_runtime_metric(runtime_by_key, metric_key)
        if str(runtime_metric["metric_kind"]) != "direct":
            raise ValueError(f"safe aggregate metric must be direct: {metric_key}")

    series_seed = _require_mapping(payload, "series_seed")
    enabled_config = [item for item in selected_config if bool(item["enabled"])]
    if not enabled_config:
        return WideDataMatrixV1Envelope(
            result=WideDataMatrixV1Empty(
                kind="empty",
                columns=columns,
                dates=dates,
                blocks=[
                    WideDataMatrixBlock(block="TOTAL", row_count=0),
                    WideDataMatrixBlock(block="GROUP", row_count=0),
                    WideDataMatrixBlock(block="SKU", row_count=0),
                ],
                rows=[],
                detail="no enabled sku rows available for wide matrix",
            )
        )

    required_direct_keys = _collect_required_direct_keys(display_metric_keys, runtime_by_key, formulas_by_id)
    _validate_series_seed(series_seed, enabled_config, required_direct_keys, dates)
    projection_fixture = _load_json_from_repo(_require_str(source_basis, "table_projection_bundle_path"))
    _validate_projection_alignment(series_seed, enabled_config, required_direct_keys, dates[-1], projection_fixture)

    groups = _build_group_order(enabled_config)
    rows: list[WideDataMatrixRow] = []

    for metric in display_metrics:
        metric_key = str(metric["metric_key"])
        if metric_key not in safe_aggregate_metric_keys:
            continue
        rows.append(
            WideDataMatrixRow(
                block="TOTAL",
                label=f"Итого: {metric['label_ru']}",
                key=f"TOTAL|{metric_key}",
                metric_key=metric_key,
                values=_aggregate_values(enabled_config, series_seed, metric_key, dates, runtime_by_key, formulas_by_id),
            )
        )

    for group_name, group_items in groups:
        for metric in display_metrics:
            metric_key = str(metric["metric_key"])
            if metric_key not in safe_aggregate_metric_keys:
                continue
            rows.append(
                WideDataMatrixRow(
                    block="GROUP",
                    label=f"Группа {group_name}: {metric['label_ru']}",
                    key=f"GROUP:{group_name}|{metric_key}",
                    metric_key=metric_key,
                    values=_aggregate_values(group_items, series_seed, metric_key, dates, runtime_by_key, formulas_by_id),
                )
            )

    for sku_item in sorted(enabled_config, key=lambda item: int(item["display_order"])):
        nm_id = int(sku_item["nm_id"])
        for metric in display_metrics:
            metric_key = str(metric["metric_key"])
            rows.append(
                WideDataMatrixRow(
                    block="SKU",
                    label=f"{sku_item['display_name']}: {metric['label_ru']}",
                    key=f"SKU:{nm_id}|{metric_key}",
                    metric_key=metric_key,
                    values=_resolve_row_values(nm_id, metric_key, dates, series_seed, runtime_by_key, formulas_by_id),
                )
            )

    blocks = [
        WideDataMatrixBlock(block="TOTAL", row_count=sum(1 for row in rows if row.block == "TOTAL")),
        WideDataMatrixBlock(block="GROUP", row_count=sum(1 for row in rows if row.block == "GROUP")),
        WideDataMatrixBlock(block="SKU", row_count=sum(1 for row in rows if row.block == "SKU")),
    ]

    return WideDataMatrixV1Envelope(
        result=WideDataMatrixV1Success(
            kind="success",
            columns=columns,
            dates=dates,
            blocks=blocks,
            rows=rows,
        )
    )


def _build_columns(dates: list[str]) -> list[WideDataMatrixColumn]:
    columns = [
        WideDataMatrixColumn(column="A", field="label", label="label"),
        WideDataMatrixColumn(column="B", field="key", label="key"),
    ]
    for index, date in enumerate(dates):
        columns.append(WideDataMatrixColumn(column=chr(ord("C") + index), field=date, label=date))
    return columns


def _validate_config_against_sku_bundle(selected_config: list[Mapping[str, Any]], sku_display_fixture: Mapping[str, Any]) -> None:
    sku_result = _require_mapping(sku_display_fixture, "result")
    items = _require_list(sku_result, "items")
    sku_by_nm_id = {int(item["nm_id"]): item for item in items}
    for item in selected_config:
        nm_id = int(item["nm_id"])
        fixture_item = sku_by_nm_id.get(nm_id)
        if fixture_item is None:
            raise ValueError(f"sku display fixture missing nm_id: {nm_id}")
        for field in ["display_name", "group", "enabled"]:
            if fixture_item[field] != item[field]:
                raise ValueError(f"config_v2 mismatch with sku_display_bundle for nm_id={nm_id}, field={field}")


def _collect_required_direct_keys(
    metric_keys: list[str],
    runtime_by_key: Mapping[str, Mapping[str, Any]],
    formulas_by_id: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    required: set[str] = set()
    for metric_key in metric_keys:
        required.update(_expand_direct_dependencies(metric_key, runtime_by_key, formulas_by_id, set()))
    return required


def _expand_direct_dependencies(
    metric_key: str,
    runtime_by_key: Mapping[str, Mapping[str, Any]],
    formulas_by_id: Mapping[str, Mapping[str, Any]],
    visiting: set[str],
) -> set[str]:
    if metric_key in visiting:
        raise ValueError(f"cyclic metric dependency: {metric_key}")
    runtime_metric = _require_runtime_metric(runtime_by_key, metric_key)
    metric_kind = str(runtime_metric["metric_kind"])
    if metric_kind == "direct":
        return {metric_key}

    next_visiting = set(visiting)
    next_visiting.add(metric_key)
    if metric_kind == "ratio":
        return _expand_direct_dependencies(str(runtime_metric["ratio_num_key"]), runtime_by_key, formulas_by_id, next_visiting) | _expand_direct_dependencies(
            str(runtime_metric["ratio_den_key"]), runtime_by_key, formulas_by_id, next_visiting
        )
    if metric_kind == "formula":
        formula_id = str(runtime_metric["formula_id"])
        formula = formulas_by_id.get(formula_id)
        if formula is None:
            raise ValueError(f"missing formula for metric: {metric_key}")
        dependencies = _extract_formula_dependencies(str(formula["expression"]))
        out: set[str] = set()
        for dependency in dependencies:
            out.update(_expand_direct_dependencies(dependency, runtime_by_key, formulas_by_id, next_visiting))
        return out
    raise ValueError(f"unsupported metric_kind: {metric_kind}")


def _validate_series_seed(
    series_seed: Mapping[str, Any],
    enabled_config: list[Mapping[str, Any]],
    required_direct_keys: set[str],
    dates: list[str],
) -> None:
    for item in enabled_config:
        nm_id = str(item["nm_id"])
        sku_series = series_seed.get(nm_id)
        if not isinstance(sku_series, Mapping):
            raise ValueError(f"series_seed missing sku: {nm_id}")
        for metric_key in sorted(required_direct_keys):
            metric_series = sku_series.get(metric_key)
            if not isinstance(metric_series, Mapping):
                raise ValueError(f"series_seed missing metric for sku {nm_id}: {metric_key}")
            for date in dates:
                value = metric_series.get(date)
                if value is None:
                    raise ValueError(f"series_seed missing date value for sku {nm_id}, metric {metric_key}, date {date}")
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError(f"series_seed value must be numeric for sku {nm_id}, metric {metric_key}, date {date}")


def _validate_projection_alignment(
    series_seed: Mapping[str, Any],
    enabled_config: list[Mapping[str, Any]],
    required_direct_keys: set[str],
    as_of_date: str,
    projection_fixture: Mapping[str, Any],
) -> None:
    projection_result = _require_mapping(projection_fixture, "result")
    projection_items = _require_list(projection_result, "items")
    projection_by_nm_id = {int(item["nm_id"]): item for item in projection_items}
    for sku in enabled_config:
        nm_id = int(sku["nm_id"])
        projection_item = projection_by_nm_id.get(nm_id)
        if projection_item is None:
            raise ValueError(f"projection fixture missing enabled sku: {nm_id}")
        sku_series = _require_mapping_value(series_seed.get(str(nm_id)), f"series_seed[{nm_id}]")
        for metric_key in sorted(required_direct_keys):
            path = PROJECTION_FIELD_MAP.get(metric_key)
            if path is None:
                continue
            expected = _read_projection_value(projection_item, path)
            actual = _require_numeric(_require_mapping_value(sku_series.get(metric_key), metric_key).get(as_of_date), f"{nm_id}/{metric_key}/{as_of_date}")
            if expected is None:
                raise ValueError(f"projection value missing for {nm_id}/{metric_key}")
            if round(float(expected), 9) != round(float(actual), 9):
                raise ValueError(f"projection alignment mismatch for {nm_id}/{metric_key}")


def _build_group_order(enabled_config: list[Mapping[str, Any]]) -> list[tuple[str, list[Mapping[str, Any]]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for item in enabled_config:
        group_name = str(item["group"])
        grouped.setdefault(group_name, []).append(item)
    return sorted(
        ((group_name, sorted(items, key=lambda row: int(row["display_order"]))) for group_name, items in grouped.items()),
        key=lambda pair: int(pair[1][0]["display_order"]),
    )


def _aggregate_values(
    config_items: list[Mapping[str, Any]],
    series_seed: Mapping[str, Any],
    metric_key: str,
    dates: list[str],
    runtime_by_key: Mapping[str, Mapping[str, Any]],
    formulas_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Optional[float]]:
    values: dict[str, Optional[float]] = {}
    for date in dates:
        numeric_values: list[float] = []
        for item in config_items:
            value = _resolve_metric_value(
                int(item["nm_id"]),
                metric_key,
                date,
                series_seed,
                runtime_by_key,
                formulas_by_id,
                {},
            )
            if value is not None:
                numeric_values.append(value)
        values[date] = float(sum(numeric_values)) if numeric_values else None
    return values


def _resolve_row_values(
    nm_id: int,
    metric_key: str,
    dates: list[str],
    series_seed: Mapping[str, Any],
    runtime_by_key: Mapping[str, Mapping[str, Any]],
    formulas_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Optional[float]]:
    values: dict[str, Optional[float]] = {}
    for date in dates:
        values[date] = _resolve_metric_value(nm_id, metric_key, date, series_seed, runtime_by_key, formulas_by_id, {})
    return values


def _resolve_metric_value(
    nm_id: int,
    metric_key: str,
    date: str,
    series_seed: Mapping[str, Any],
    runtime_by_key: Mapping[str, Mapping[str, Any]],
    formulas_by_id: Mapping[str, Mapping[str, Any]],
    cache: dict[tuple[int, str, str], Optional[float]],
) -> Optional[float]:
    cache_key = (nm_id, metric_key, date)
    if cache_key in cache:
        return cache[cache_key]

    runtime_metric = _require_runtime_metric(runtime_by_key, metric_key)
    metric_kind = str(runtime_metric["metric_kind"])
    if metric_kind == "direct":
        value = _read_series_value(series_seed, nm_id, metric_key, date)
    elif metric_kind == "ratio":
        numerator = _resolve_metric_value(
            nm_id,
            str(runtime_metric["ratio_num_key"]),
            date,
            series_seed,
            runtime_by_key,
            formulas_by_id,
            cache,
        )
        denominator = _resolve_metric_value(
            nm_id,
            str(runtime_metric["ratio_den_key"]),
            date,
            series_seed,
            runtime_by_key,
            formulas_by_id,
            cache,
        )
        if numerator is None or denominator in (None, 0):
            value = None
        else:
            value = float(numerator) / float(denominator)
    elif metric_kind == "formula":
        formula_id = str(runtime_metric["formula_id"])
        formula = formulas_by_id.get(formula_id)
        if formula is None:
            raise ValueError(f"formula missing for metric {metric_key}")
        expression = str(formula["expression"])
        names = _extract_formula_dependencies(expression)
        env: dict[str, float] = {}
        for name in names:
            resolved = _resolve_metric_value(nm_id, name, date, series_seed, runtime_by_key, formulas_by_id, cache)
            if resolved is None:
                value = None
                break
            env[name] = float(resolved)
        else:
            value = float(_evaluate_expression(expression, env))
    else:
        raise ValueError(f"unsupported metric_kind: {metric_kind}")

    cache[cache_key] = value
    return value


def _evaluate_expression(expression: str, env: Mapping[str, float]) -> float:
    node = ast.parse(expression, mode="eval")
    return float(_eval_ast(node.body, env))


def _eval_ast(node: ast.AST, env: Mapping[str, float]) -> float:
    if isinstance(node, ast.BinOp):
        left = _eval_ast(node.left, env)
        right = _eval_ast(node.right, env)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_ast(node.operand, env)
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise ValueError(f"unknown formula symbol: {node.id}")
        return env[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    raise ValueError("unsupported formula expression")


def _extract_formula_dependencies(expression: str) -> set[str]:
    node = ast.parse(expression, mode="eval")
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _read_series_value(series_seed: Mapping[str, Any], nm_id: int, metric_key: str, date: str) -> Optional[float]:
    sku_series = series_seed.get(str(nm_id))
    if not isinstance(sku_series, Mapping):
        return None
    metric_series = sku_series.get(metric_key)
    if not isinstance(metric_series, Mapping):
        return None
    value = metric_series.get(date)
    if value is None:
        return None
    return float(_require_numeric(value, f"{nm_id}/{metric_key}/{date}"))


def _read_projection_value(item: Mapping[str, Any], path: tuple[str, ...]) -> Optional[float]:
    value: Any = item
    for part in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    if value is None:
        return None
    return float(_require_numeric(value, ".".join(path)))


def _load_json_from_repo(relative_path: str) -> Mapping[str, Any]:
    return _load_json(ROOT / relative_path)


def _load_json(path: Path) -> Mapping[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return _require_mapping_value(data, str(path))


def _require_runtime_metric(runtime_by_key: Mapping[str, Mapping[str, Any]], metric_key: str) -> Mapping[str, Any]:
    runtime_metric = runtime_by_key.get(metric_key)
    if runtime_metric is None:
        raise ValueError(f"runtime metric missing: {metric_key}")
    return runtime_metric


def _require_unique(label: str, values: list[Any]) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate values in {label}: {duplicates}")


def _require_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    return _require_mapping_value(mapping.get(key), key)


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


def _require_string_list(mapping: Mapping[str, Any], key: str) -> list[str]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{key} must contain only strings")
        out.append(item)
    return out


def _require_int_list(mapping: Mapping[str, Any], key: str) -> list[int]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be list")
    out: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError(f"{key} must contain only ints")
        out.append(item)
    return out


def _require_numeric(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    return float(value)
