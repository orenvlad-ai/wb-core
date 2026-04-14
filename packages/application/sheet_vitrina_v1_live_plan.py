"""Live readback plan для первого end-to-end MVP sheet_vitrina_v1."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from packages.adapters.seller_funnel_snapshot_block import HttpBackedSellerFunnelSnapshotSource
from packages.adapters.web_source_snapshot_block import HttpBackedWebSourceSnapshotSource
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.seller_funnel_snapshot_block import SellerFunnelSnapshotBlock
from packages.application.sheet_vitrina_v1 import build_sheet_write_plan
from packages.application.web_source_snapshot_block import WebSourceSnapshotBlock
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope
from packages.contracts.seller_funnel_snapshot_block import SellerFunnelSnapshotRequest
from packages.contracts.web_source_snapshot_block import WebSourceSnapshotRequest

ROOT = Path(__file__).resolve().parents[2]
SHEET_LAYOUT_DIR = ROOT / "artifacts" / "sheet_vitrina_v1" / "layout"
DATA_LAYOUT_PATH = SHEET_LAYOUT_DIR / "data_vitrina_sheet_layout.json"
STATUS_LAYOUT_PATH = SHEET_LAYOUT_DIR / "status_sheet_layout.json"
LIVE_VALUE_SUPPORTED_METRIC_KEYS = (
    "view_count",
    "ctr",
    "open_card_count",
    "views_current",
    "ctr_current",
    "orders_current",
    "position_avg",
)
STATUS_HEADER = [
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


class SheetVitrinaV1LivePlanBlock:
    def __init__(
        self,
        runtime: RegistryUploadDbBackedRuntime,
        web_source_block: WebSourceSnapshotBlock | None = None,
        seller_funnel_block: SellerFunnelSnapshotBlock | None = None,
    ) -> None:
        self.runtime = runtime
        self.web_source_block = web_source_block or WebSourceSnapshotBlock(HttpBackedWebSourceSnapshotSource())
        self.seller_funnel_block = seller_funnel_block or SellerFunnelSnapshotBlock(HttpBackedSellerFunnelSnapshotSource())

    def build_plan(self, as_of_date: str | None = None) -> SheetVitrinaV1Envelope:
        current_state = self.runtime.load_current_state()
        effective_date = _resolve_as_of_date(as_of_date)
        enabled_config = sorted(
            [item for item in current_state.config_v2 if item.enabled],
            key=lambda item: item.display_order,
        )
        if not enabled_config:
            raise ValueError("current registry config_v2 does not contain enabled rows")

        configured_metrics = sorted(
            [item for item in current_state.metrics_v2 if item.enabled and item.show_in_data],
            key=lambda item: item.display_order,
        )
        displayed_metrics = configured_metrics
        live_value_supported_metrics = [
            item for item in configured_metrics if item.metric_key in LIVE_VALUE_SUPPORTED_METRIC_KEYS
        ]
        live_value_unmapped_metrics = [
            item.metric_key for item in configured_metrics if item.metric_key not in LIVE_VALUE_SUPPORTED_METRIC_KEYS
        ]
        if not displayed_metrics:
            raise ValueError("current registry metrics_v2 does not contain enabled show_in_data metrics")

        web_result = self.web_source_block.execute(
            WebSourceSnapshotRequest(
                snapshot_type="web_source_snapshot",
                date_from=effective_date,
                date_to=effective_date,
            )
        ).result
        funnel_result = self.seller_funnel_block.execute(
            SellerFunnelSnapshotRequest(
                snapshot_type="seller_funnel_snapshot",
                date=effective_date,
            )
        ).result

        if getattr(web_result, "kind", "") != "success" and getattr(funnel_result, "kind", "") != "success":
            raise ValueError("live readback sources returned no usable data")

        web_lookup = {item.nm_id: item for item in getattr(web_result, "items", [])}
        funnel_lookup = {item.nm_id: item for item in getattr(funnel_result, "items", [])}

        data_header = ["label", "key", effective_date]
        data_rows: list[list[Any]] = []

        for metric in displayed_metrics:
            total_value = _aggregate_metric(enabled_config, metric.metric_key, web_lookup, funnel_lookup)
            data_rows.append([f"Итого: {metric.label_ru}", f"TOTAL|{metric.metric_key}", _to_sheet_value(total_value)])

        for group_name, group_items in _group_config(enabled_config):
            for metric in displayed_metrics:
                group_value = _aggregate_metric(group_items, metric.metric_key, web_lookup, funnel_lookup)
                data_rows.append(
                    [
                        f"Группа {group_name}: {metric.label_ru}",
                        f"GROUP:{group_name}|{metric.metric_key}",
                        _to_sheet_value(group_value),
                    ]
                )

        for config_item in enabled_config:
            for metric in displayed_metrics:
                sku_value = _resolve_metric_value(config_item.nm_id, metric.metric_key, web_lookup, funnel_lookup)
                data_rows.append(
                    [
                        f"{config_item.display_name}: {metric.label_ru}",
                        f"SKU:{config_item.nm_id}|{metric.metric_key}",
                        _to_sheet_value(sku_value),
                    ]
                )

        requested_nm_ids = [item.nm_id for item in enabled_config]
        web_missing = sorted(set(requested_nm_ids) - set(web_lookup))
        funnel_missing = sorted(set(requested_nm_ids) - set(funnel_lookup))
        status_rows = [
            [
                "registry_upload_current_state",
                "success",
                current_state.activated_at[:10],
                current_state.activated_at[:10],
                "",
                "",
                "",
                len(enabled_config),
                len(enabled_config),
                "",
                _format_note(
                    {
                        "bundle_version": current_state.bundle_version,
                        "config_count": len(current_state.config_v2),
                        "metrics_count": len(current_state.metrics_v2),
                        "formulas_count": len(current_state.formulas_v2),
                        "displayed_metrics": len(displayed_metrics),
                        "live_value_supported_metrics": len(live_value_supported_metrics),
                        "live_value_unmapped_metrics": ",".join(live_value_unmapped_metrics),
                    }
                ),
            ],
            [
                "seller_funnel_snapshot",
                getattr(funnel_result, "kind", "missing"),
                getattr(funnel_result, "date", ""),
                "",
                getattr(funnel_result, "date", ""),
                "",
                "",
                len(enabled_config),
                len(set(requested_nm_ids) & set(funnel_lookup)),
                _format_missing_nm_ids(funnel_missing),
                getattr(funnel_result, "detail", ""),
            ],
            [
                "web_source_snapshot",
                getattr(web_result, "kind", "missing"),
                effective_date,
                "",
                "",
                getattr(web_result, "date_from", ""),
                getattr(web_result, "date_to", ""),
                len(enabled_config),
                len(set(requested_nm_ids) & set(web_lookup)),
                _format_missing_nm_ids(web_missing),
                getattr(web_result, "detail", ""),
            ],
            [
                "sheet_vitrina_v1_mvp",
                "success",
                effective_date,
                effective_date,
                "",
                "",
                "",
                len(displayed_metrics),
                len(displayed_metrics),
                "",
                _format_note(
                    {
                        "displayed_metrics": ",".join(item.metric_key for item in displayed_metrics),
                        "live_value_supported_metrics": ",".join(
                            item.metric_key for item in live_value_supported_metrics
                        ),
                        "live_value_unmapped_metrics": ",".join(live_value_unmapped_metrics),
                    }
                ),
            ],
        ]

        delivery_bundle = {
            "delivery_contract_version": "sheet_vitrina_v1_mvp_live_v1",
            "snapshot_id": f"{effective_date}__sheet_vitrina_v1_mvp_live_v1__current",
            "as_of_date": effective_date,
            "data_vitrina": {
                "sheet_name": "DATA_VITRINA",
                "header": data_header,
                "rows": data_rows,
            },
            "status": {
                "sheet_name": "STATUS",
                "header": STATUS_HEADER,
                "rows": status_rows,
            },
        }
        return build_sheet_write_plan(
            delivery_bundle=delivery_bundle,
            data_layout=_load_json(DATA_LAYOUT_PATH),
            status_layout=_load_json(STATUS_LAYOUT_PATH),
        )


def _resolve_as_of_date(value: str | None) -> str:
    if value:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    return str((datetime.now(timezone.utc).date() - timedelta(days=1)))


def _group_config(config_items: list[ConfigV2Item]) -> list[tuple[str, list[ConfigV2Item]]]:
    grouped: dict[str, list[ConfigV2Item]] = {}
    for item in config_items:
        grouped.setdefault(item.group, []).append(item)
    return sorted(
        ((group_name, sorted(items, key=lambda row: row.display_order)) for group_name, items in grouped.items()),
        key=lambda pair: pair[1][0].display_order,
    )


def _aggregate_metric(
    config_items: Iterable[ConfigV2Item],
    metric_key: str,
    web_lookup: Mapping[int, Any],
    funnel_lookup: Mapping[int, Any],
) -> float | None:
    items = list(config_items)
    if metric_key not in LIVE_VALUE_SUPPORTED_METRIC_KEYS:
        return None
    if metric_key in {"view_count", "open_card_count", "views_current", "orders_current"}:
        values = [_resolve_metric_value(item.nm_id, metric_key, web_lookup, funnel_lookup) for item in items]
        numeric = [value for value in values if value is not None]
        return float(sum(numeric)) if numeric else None
    if metric_key == "ctr":
        numerator = _aggregate_metric(items, "open_card_count", web_lookup, funnel_lookup)
        denominator = _aggregate_metric(items, "view_count", web_lookup, funnel_lookup)
        if numerator is None or denominator in (None, 0):
            return None
        return float(numerator) / float(denominator)
    if metric_key in {"ctr_current", "position_avg"}:
        pairs: list[tuple[float, float]] = []
        for item in items:
            weight = _resolve_metric_value(item.nm_id, "views_current", web_lookup, funnel_lookup)
            value = _resolve_metric_value(item.nm_id, metric_key, web_lookup, funnel_lookup)
            if weight is None or value is None or weight <= 0:
                continue
            pairs.append((value, weight))
        if not pairs:
            return None
        weight_total = sum(weight for _, weight in pairs)
        return sum(value * weight for value, weight in pairs) / weight_total
    return None


def _resolve_metric_value(
    nm_id: int,
    metric_key: str,
    web_lookup: Mapping[int, Any],
    funnel_lookup: Mapping[int, Any],
) -> float | None:
    if metric_key not in LIVE_VALUE_SUPPORTED_METRIC_KEYS:
        return None
    funnel_item = funnel_lookup.get(nm_id)
    web_item = web_lookup.get(nm_id)
    if metric_key == "view_count":
        return float(funnel_item.view_count) if funnel_item else None
    if metric_key == "open_card_count":
        return float(funnel_item.open_card_count) if funnel_item else None
    if metric_key == "ctr":
        return float(funnel_item.ctr) / 100.0 if funnel_item else None
    if metric_key == "views_current":
        return float(web_item.views_current) if web_item else None
    if metric_key == "ctr_current":
        return float(web_item.ctr_current) / 100.0 if web_item else None
    if metric_key == "orders_current":
        return float(web_item.orders_current) if web_item else None
    if metric_key == "position_avg":
        return float(web_item.position_avg) if web_item else None
    return None


def _to_sheet_value(value: float | None) -> Any:
    if value is None:
        return ""
    return round(float(value), 6)


def _format_missing_nm_ids(value: list[int]) -> str:
    return ",".join(str(item) for item in value)


def _format_note(value: Mapping[str, Any]) -> str:
    pairs = [f"{key}={value[key]}" for key in value if value[key] not in (None, "")]
    return "; ".join(pairs)


def _load_json(path: Path) -> Mapping[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
