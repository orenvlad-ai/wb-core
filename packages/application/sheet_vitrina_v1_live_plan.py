"""Live readback plan для sheet_vitrina_v1 по uploaded compact registry package."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping

from packages.adapters.ads_bids_block import HttpBackedAdsBidsSource
from packages.adapters.ads_compact_block import HttpBackedAdsCompactSource
from packages.adapters.fin_report_daily_block import HttpBackedFinReportDailySource
from packages.adapters.prices_snapshot_block import HttpBackedPricesSnapshotSource
from packages.adapters.sales_funnel_history_block import HttpBackedSalesFunnelHistorySource
from packages.adapters.seller_funnel_snapshot_block import HttpBackedSellerFunnelSnapshotSource
from packages.adapters.sf_period_block import HttpBackedSfPeriodSource
from packages.adapters.spp_block import HttpBackedSppSource
from packages.adapters.stocks_block import HttpBackedStocksSource
from packages.adapters.web_source_snapshot_block import HttpBackedWebSourceSnapshotSource
from packages.application.ads_bids_block import AdsBidsBlock
from packages.application.ads_compact_block import AdsCompactBlock
from packages.application.fin_report_daily_block import FinReportDailyBlock
from packages.application.prices_snapshot_block import PricesSnapshotBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock
from packages.application.seller_funnel_snapshot_block import SellerFunnelSnapshotBlock
from packages.application.sf_period_block import SfPeriodBlock
from packages.application.sheet_vitrina_v1 import build_sheet_write_plan
from packages.application.spp_block import SppBlock
from packages.application.stocks_block import StocksBlock
from packages.application.web_source_snapshot_block import WebSourceSnapshotBlock
from packages.contracts.ads_bids_block import AdsBidsRequest
from packages.contracts.ads_compact_block import AdsCompactRequest
from packages.contracts.fin_report_daily_block import FinReportDailyRequest
from packages.contracts.prices_snapshot_block import PricesSnapshotRequest
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, FormulaV2Item, MetricV2Item
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryRequest
from packages.contracts.seller_funnel_snapshot_block import SellerFunnelSnapshotRequest
from packages.contracts.sf_period_block import SfPeriodRequest
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope
from packages.contracts.spp_block import SppRequest
from packages.contracts.stocks_block import StocksRequest
from packages.contracts.web_source_snapshot_block import WebSourceSnapshotRequest

ROOT = Path(__file__).resolve().parents[2]
SHEET_LAYOUT_DIR = ROOT / "artifacts" / "sheet_vitrina_v1" / "layout"
DATA_LAYOUT_PATH = SHEET_LAYOUT_DIR / "data_vitrina_sheet_layout.json"
STATUS_LAYOUT_PATH = SHEET_LAYOUT_DIR / "status_sheet_layout.json"
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
FORMULA_TOKEN_RE = re.compile(r"\{([^}]+)\}")
AGGREGATE_SUM_PREFIX = "total_"
AGGREGATE_AVG_PREFIX = "avg_"
BLOCKED_SOURCE_STATUSES = {
    "promo_by_price": "live HTTP adapter is not implemented; rows stay materialized with blank values",
    "cogs_by_group": "live HTTP adapter is not implemented; rows stay materialized with blank values",
}
PERCENT_SOURCE_KEYS = {"ctr", "ctr_current", "localizationPercent"}
DECISION_SUMMARY = {
    "alias_zone": "openCount and open_card_count remain distinct metrics from different sources",
    "total_avg_policy": "preserve all total_/avg_ uploaded rows; total_=sum, avg_=arithmetic_mean",
    "section_dictionary": "uploaded section values are authoritative and are not remapped",
    "config_service_values": "CONFIG!H:I service block is preserved across prepare/reprepare",
}


@dataclass(frozen=True)
class LiveSourceStatus:
    source_key: str
    kind: str
    freshness: str
    snapshot_date: str
    date: str
    date_from: str
    date_to: str
    requested_count: int
    covered_count: int
    missing_nm_ids: list[int]
    note: str


@dataclass(frozen=True)
class LiveSources:
    statuses: list[LiveSourceStatus]
    seller_funnel_lookup: dict[int, Any]
    history_lookup: dict[int, dict[str, float]]
    web_lookup: dict[int, Any]
    prices_lookup: dict[int, Any]
    sf_period_lookup: dict[int, Any]
    spp_lookup: dict[int, Any]
    ads_bids_lookup: dict[int, Any]
    stocks_lookup: dict[int, Any]
    ads_compact_lookup: dict[int, Any]
    fin_lookup: dict[int, Any]
    fin_storage_fee_total: float | None


class SheetVitrinaV1LivePlanBlock:
    def __init__(
        self,
        runtime: RegistryUploadDbBackedRuntime,
        web_source_block: WebSourceSnapshotBlock | None = None,
        seller_funnel_block: SellerFunnelSnapshotBlock | None = None,
        sales_funnel_history_block: SalesFunnelHistoryBlock | None = None,
        prices_snapshot_block: PricesSnapshotBlock | None = None,
        sf_period_block: SfPeriodBlock | None = None,
        spp_block: SppBlock | None = None,
        ads_bids_block: AdsBidsBlock | None = None,
        stocks_block: StocksBlock | None = None,
        ads_compact_block: AdsCompactBlock | None = None,
        fin_report_daily_block: FinReportDailyBlock | None = None,
    ) -> None:
        self.runtime = runtime
        self.web_source_block = web_source_block or WebSourceSnapshotBlock(HttpBackedWebSourceSnapshotSource())
        self.seller_funnel_block = seller_funnel_block or SellerFunnelSnapshotBlock(HttpBackedSellerFunnelSnapshotSource())
        self.sales_funnel_history_block = sales_funnel_history_block or SalesFunnelHistoryBlock(HttpBackedSalesFunnelHistorySource())
        self.prices_snapshot_block = prices_snapshot_block or PricesSnapshotBlock(HttpBackedPricesSnapshotSource())
        self.sf_period_block = sf_period_block or SfPeriodBlock(HttpBackedSfPeriodSource())
        self.spp_block = spp_block or SppBlock(HttpBackedSppSource())
        self.ads_bids_block = ads_bids_block or AdsBidsBlock(HttpBackedAdsBidsSource())
        self.stocks_block = stocks_block or StocksBlock(HttpBackedStocksSource())
        self.ads_compact_block = ads_compact_block or AdsCompactBlock(HttpBackedAdsCompactSource())
        self.fin_report_daily_block = fin_report_daily_block or FinReportDailyBlock(HttpBackedFinReportDailySource())

    def build_plan(self, as_of_date: str | None = None) -> SheetVitrinaV1Envelope:
        current_state = self.runtime.load_current_state()
        effective_date = _resolve_as_of_date(as_of_date)
        enabled_config = sorted(
            [item for item in current_state.config_v2 if item.enabled],
            key=lambda item: item.display_order,
        )
        if not enabled_config:
            raise ValueError("current registry config_v2 does not contain enabled rows")

        metrics_by_key = {item.metric_key: item for item in current_state.metrics_v2}
        formulas_by_id = {item.formula_id: item for item in current_state.formulas_v2}
        displayed_metrics = sorted(
            [item for item in current_state.metrics_v2 if item.enabled and item.show_in_data],
            key=lambda item: item.display_order,
        )
        if not displayed_metrics:
            raise ValueError("current registry metrics_v2 does not contain enabled show_in_data rows")

        live_sources = self._load_live_sources(enabled_config, effective_date)
        evaluator = _MetricEvaluator(
            enabled_config=enabled_config,
            metrics_by_key=metrics_by_key,
            formulas_by_id=formulas_by_id,
            live_sources=live_sources,
        )

        data_rows: list[list[Any]] = []
        scope_row_counts = {"TOTAL": 0, "GROUP": 0, "SKU": 0}
        section_row_counts: dict[str, int] = {}
        for metric in displayed_metrics:
            rows = _build_metric_rows(metric, enabled_config, evaluator)
            data_rows.extend(rows)
            scope_row_counts[metric.scope] = scope_row_counts.get(metric.scope, 0) + len(rows)
            section_row_counts[metric.section] = section_row_counts.get(metric.section, 0) + len(rows)

        data_header = ["label", "key", effective_date]
        status_rows = _build_status_rows(
            current_state=current_state,
            displayed_metrics=displayed_metrics,
            data_rows=data_rows,
            live_sources=live_sources,
            scope_row_counts=scope_row_counts,
            section_row_counts=section_row_counts,
        )
        delivery_bundle = {
            "delivery_contract_version": "sheet_vitrina_v1_compact_live_v2",
            "snapshot_id": f"{effective_date}__sheet_vitrina_v1_compact_live_v2__current",
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

    def _load_live_sources(self, enabled_config: list[ConfigV2Item], effective_date: str) -> LiveSources:
        requested_nm_ids = [item.nm_id for item in enabled_config]
        statuses: list[LiveSourceStatus] = []
        lookups: dict[str, dict[int, Any]] = {
            "seller_funnel_lookup": {},
            "history_lookup": {},
            "web_lookup": {},
            "prices_lookup": {},
            "sf_period_lookup": {},
            "spp_lookup": {},
            "ads_bids_lookup": {},
            "stocks_lookup": {},
            "ads_compact_lookup": {},
            "fin_lookup": {},
        }
        fin_storage_fee_total: float | None = None

        for source_key, loader in [
            ("seller_funnel_snapshot", lambda: self.seller_funnel_block.execute(SellerFunnelSnapshotRequest(snapshot_type="seller_funnel_snapshot", date=effective_date)).result),
            ("sales_funnel_history", lambda: self.sales_funnel_history_block.execute(SalesFunnelHistoryRequest(snapshot_type="sales_funnel_history", date_from=effective_date, date_to=effective_date, nm_ids=requested_nm_ids)).result),
            ("web_source_snapshot", lambda: self.web_source_block.execute(WebSourceSnapshotRequest(snapshot_type="web_source_snapshot", date_from=effective_date, date_to=effective_date)).result),
            ("prices_snapshot", lambda: self.prices_snapshot_block.execute(PricesSnapshotRequest(snapshot_type="prices_snapshot", snapshot_date=effective_date, nm_ids=requested_nm_ids)).result),
            ("sf_period", lambda: self.sf_period_block.execute(SfPeriodRequest(snapshot_type="sf_period", snapshot_date=effective_date, nm_ids=requested_nm_ids)).result),
            ("spp", lambda: self.spp_block.execute(SppRequest(snapshot_type="spp", snapshot_date=effective_date, nm_ids=requested_nm_ids)).result),
            ("ads_bids", lambda: self.ads_bids_block.execute(AdsBidsRequest(snapshot_type="ads_bids", snapshot_date=effective_date, nm_ids=requested_nm_ids)).result),
            ("stocks", lambda: self.stocks_block.execute(StocksRequest(snapshot_type="stocks", snapshot_date=effective_date, nm_ids=requested_nm_ids)).result),
            ("ads_compact", lambda: self.ads_compact_block.execute(AdsCompactRequest(snapshot_type="ads_compact", snapshot_date=effective_date, nm_ids=requested_nm_ids)).result),
            ("fin_report_daily", lambda: self.fin_report_daily_block.execute(FinReportDailyRequest(snapshot_type="fin_report_daily", snapshot_date=effective_date, nm_ids=requested_nm_ids)).result),
        ]:
            status, payload = _capture_live_source(source_key, requested_nm_ids, loader)
            statuses.append(status)
            if source_key == "seller_funnel_snapshot":
                lookups["seller_funnel_lookup"] = _index_items_by_nm_id(payload)
            elif source_key == "sales_funnel_history":
                lookups["history_lookup"] = _index_history_items(payload)
            elif source_key == "web_source_snapshot":
                lookups["web_lookup"] = _index_items_by_nm_id(payload)
            elif source_key == "prices_snapshot":
                lookups["prices_lookup"] = _index_items_by_nm_id(payload)
            elif source_key == "sf_period":
                lookups["sf_period_lookup"] = _index_items_by_nm_id(payload)
            elif source_key == "spp":
                lookups["spp_lookup"] = _index_items_by_nm_id(payload)
            elif source_key == "ads_bids":
                lookups["ads_bids_lookup"] = _index_items_by_nm_id(payload)
            elif source_key == "stocks":
                lookups["stocks_lookup"] = _index_items_by_nm_id(payload)
            elif source_key == "ads_compact":
                lookups["ads_compact_lookup"] = _index_items_by_nm_id(payload)
            elif source_key == "fin_report_daily":
                lookups["fin_lookup"] = _index_items_by_nm_id(payload)
                storage_total = getattr(payload, "storage_total", None)
                if storage_total is not None:
                    fin_storage_fee_total = float(getattr(storage_total, "fin_storage_fee_total", 0.0))

        for source_key, note in BLOCKED_SOURCE_STATUSES.items():
            statuses.append(
                LiveSourceStatus(
                    source_key=source_key,
                    kind="blocked",
                    freshness="",
                    snapshot_date="",
                    date="",
                    date_from="",
                    date_to="",
                    requested_count=len(requested_nm_ids),
                    covered_count=0,
                    missing_nm_ids=[],
                    note=note,
                )
            )

        return LiveSources(
            statuses=statuses,
            seller_funnel_lookup=lookups["seller_funnel_lookup"],
            history_lookup=lookups["history_lookup"],
            web_lookup=lookups["web_lookup"],
            prices_lookup=lookups["prices_lookup"],
            sf_period_lookup=lookups["sf_period_lookup"],
            spp_lookup=lookups["spp_lookup"],
            ads_bids_lookup=lookups["ads_bids_lookup"],
            stocks_lookup=lookups["stocks_lookup"],
            ads_compact_lookup=lookups["ads_compact_lookup"],
            fin_lookup=lookups["fin_lookup"],
            fin_storage_fee_total=fin_storage_fee_total,
        )


class _MetricEvaluator:
    def __init__(
        self,
        *,
        enabled_config: list[ConfigV2Item],
        metrics_by_key: Mapping[str, MetricV2Item],
        formulas_by_id: Mapping[str, FormulaV2Item],
        live_sources: LiveSources,
    ) -> None:
        self.enabled_config = enabled_config
        self.metrics_by_key = metrics_by_key
        self.formulas_by_id = formulas_by_id
        self.live_sources = live_sources
        self.grouped_config = _group_config(enabled_config)
        self.sku_cache: dict[tuple[int, str], float | None] = {}
        self.total_cache: dict[str, float | None] = {}
        self.group_cache: dict[tuple[str, str], float | None] = {}

    def resolve_sku(self, metric_key: str, nm_id: int) -> float | None:
        cache_key = (nm_id, metric_key)
        if cache_key in self.sku_cache:
            return self.sku_cache[cache_key]

        metric = self.metrics_by_key.get(metric_key)
        if metric is None:
            raise ValueError(f"metric_key missing in current registry: {metric_key}")

        if metric.calc_type == "metric":
            if metric.calc_ref != metric.metric_key:
                value = self.resolve_sku(metric.calc_ref, nm_id)
            else:
                value = self._resolve_direct_sku(metric.metric_key, nm_id)
        elif metric.calc_type == "ratio":
            numerator_key, denominator_key = _split_ratio(metric.calc_ref)
            numerator = self.resolve_sku(numerator_key, nm_id)
            denominator = self.resolve_sku(denominator_key, nm_id)
            value = None if numerator is None or denominator in (None, 0) else float(numerator) / float(denominator)
        elif metric.calc_type == "formula":
            formula = self.formulas_by_id.get(metric.calc_ref)
            if formula is None:
                raise ValueError(f"formula missing for metric {metric_key}")
            value = _evaluate_formula(
                formula.expression,
                lambda dependency: self.resolve_sku(dependency, nm_id),
            )
        else:
            raise ValueError(f"unsupported calc_type: {metric.calc_type}")

        self.sku_cache[cache_key] = value
        return value

    def resolve_total(self, metric_key: str) -> float | None:
        if metric_key in self.total_cache:
            return self.total_cache[metric_key]

        metric = self.metrics_by_key.get(metric_key)
        if metric is None:
            raise ValueError(f"metric_key missing in current registry: {metric_key}")

        if metric.calc_type == "metric":
            if metric.metric_key == "fin_storage_fee_total":
                value = self.live_sources.fin_storage_fee_total
            elif metric.metric_key.startswith(AGGREGATE_SUM_PREFIX):
                value = self._aggregate_sum(metric.calc_ref, self.enabled_config)
            elif metric.metric_key.startswith(AGGREGATE_AVG_PREFIX):
                value = self._aggregate_avg(metric.calc_ref, self.enabled_config)
            elif metric.calc_ref != metric.metric_key:
                value = self._aggregate_sum(metric.calc_ref, self.enabled_config)
            else:
                value = self._resolve_total_direct(metric.metric_key)
        elif metric.calc_type == "ratio":
            numerator_key, denominator_key = _split_ratio(metric.calc_ref)
            numerator = self.resolve_total(numerator_key)
            denominator = self.resolve_total(denominator_key)
            value = None if numerator is None or denominator in (None, 0) else float(numerator) / float(denominator)
        elif metric.calc_type == "formula":
            formula = self.formulas_by_id.get(metric.calc_ref)
            if formula is None:
                raise ValueError(f"formula missing for metric {metric_key}")
            value = _evaluate_formula(formula.expression, self.resolve_total)
        else:
            raise ValueError(f"unsupported calc_type: {metric.calc_type}")

        self.total_cache[metric_key] = value
        return value

    def resolve_group(self, metric_key: str, group_name: str) -> float | None:
        cache_key = (group_name, metric_key)
        if cache_key in self.group_cache:
            return self.group_cache[cache_key]

        metric = self.metrics_by_key.get(metric_key)
        if metric is None:
            raise ValueError(f"metric_key missing in current registry: {metric_key}")
        group_items = self.grouped_config.get(group_name, [])
        if metric.calc_type == "metric":
            if metric.metric_key.startswith(AGGREGATE_AVG_PREFIX):
                value = self._aggregate_avg(metric.calc_ref, group_items)
            else:
                value = self._aggregate_sum(metric.calc_ref, group_items)
        elif metric.calc_type == "ratio":
            numerator_key, denominator_key = _split_ratio(metric.calc_ref)
            numerator = self._aggregate_sum(numerator_key, group_items)
            denominator = self._aggregate_sum(denominator_key, group_items)
            value = None if numerator is None or denominator in (None, 0) else float(numerator) / float(denominator)
        elif metric.calc_type == "formula":
            formula = self.formulas_by_id.get(metric.calc_ref)
            if formula is None:
                raise ValueError(f"formula missing for metric {metric_key}")
            value = _evaluate_formula(
                formula.expression,
                lambda dependency: self._aggregate_sum(dependency, group_items),
            )
        else:
            raise ValueError(f"unsupported calc_type: {metric.calc_type}")

        self.group_cache[cache_key] = value
        return value

    def _resolve_total_direct(self, metric_key: str) -> float | None:
        if metric_key == "fin_storage_fee_total":
            return self.live_sources.fin_storage_fee_total
        return self._aggregate_sum(metric_key, self.enabled_config)

    def _aggregate_sum(self, metric_key: str, config_items: Iterable[ConfigV2Item]) -> float | None:
        values = [self.resolve_sku(metric_key, item.nm_id) for item in config_items]
        numeric = [value for value in values if value is not None]
        return float(sum(numeric)) if numeric else None

    def _aggregate_avg(self, metric_key: str, config_items: Iterable[ConfigV2Item]) -> float | None:
        values = [self.resolve_sku(metric_key, item.nm_id) for item in config_items]
        numeric = [value for value in values if value is not None]
        return float(sum(numeric)) / len(numeric) if numeric else None

    def _resolve_direct_sku(self, metric_key: str, nm_id: int) -> float | None:
        if metric_key == "variable_costs_wb":
            order_sum = self.resolve_sku("orderSum", nm_id)
            return None if order_sum is None else float(order_sum) * 0.4904
        if metric_key in {"profit_proxy_rub", "proxy_profit_rub"}:
            order_sum = self.resolve_sku("orderSum", nm_id)
            order_count = self.resolve_sku("orderCount", nm_id)
            cost_price = self.resolve_sku("cost_price_rub", nm_id)
            ads_sum = self.resolve_sku("ads_sum", nm_id)
            if None in {order_sum, order_count, cost_price, ads_sum}:
                return None
            return float(order_sum) * 0.5096 - float(order_count) * 0.91 * float(cost_price) - float(ads_sum)
        if metric_key == "inventory_value_retail_rub":
            stock_total = self.resolve_sku("stock_total", nm_id)
            price_seller_discounted = self.resolve_sku("price_seller_discounted", nm_id)
            if stock_total is None or price_seller_discounted is None:
                return None
            return float(stock_total) * float(price_seller_discounted)

        for lookup_name, attribute, scale in [
            ("seller_funnel_lookup", "view_count", 1.0),
            ("seller_funnel_lookup", "open_card_count", 1.0),
            ("seller_funnel_lookup", "ctr", 0.01),
            ("web_lookup", "views_current", 1.0),
            ("web_lookup", "ctr_current", 0.01),
            ("web_lookup", "orders_current", 1.0),
            ("web_lookup", "position_avg", 1.0),
            ("prices_lookup", "price_seller", 1.0),
            ("prices_lookup", "price_seller_discounted", 1.0),
            ("sf_period_lookup", "localization_percent", 0.01),
            ("sf_period_lookup", "feedback_rating", 1.0),
            ("spp_lookup", "spp", 1.0),
            ("ads_bids_lookup", "ads_bid_search", 1.0),
            ("stocks_lookup", "stock_total", 1.0),
            ("stocks_lookup", "stock_ru_central", 1.0),
            ("stocks_lookup", "stock_ru_northwest", 1.0),
            ("stocks_lookup", "stock_ru_volga", 1.0),
            ("stocks_lookup", "stock_ru_south_caucasus", 1.0),
            ("stocks_lookup", "stock_ru_ural", 1.0),
            ("stocks_lookup", "stock_ru_far_siberia", 1.0),
            ("ads_compact_lookup", "ads_views", 1.0),
            ("ads_compact_lookup", "ads_clicks", 1.0),
            ("ads_compact_lookup", "ads_atbs", 1.0),
            ("ads_compact_lookup", "ads_orders", 1.0),
            ("ads_compact_lookup", "ads_sum", 1.0),
            ("ads_compact_lookup", "ads_sum_price", 1.0),
            ("ads_compact_lookup", "ads_cpc", 1.0),
            ("ads_compact_lookup", "ads_ctr", 1.0),
            ("ads_compact_lookup", "ads_cr", 1.0),
            ("fin_lookup", "fin_buyout_rub", 1.0),
            ("fin_lookup", "fin_delivery_rub", 1.0),
            ("fin_lookup", "fin_commission_wb_portal", 1.0),
            ("fin_lookup", "fin_acquiring_fee", 1.0),
            ("fin_lookup", "fin_loyalty_rub", 1.0),
        ]:
            if metric_key == _metric_key_from_lookup(lookup_name, attribute):
                return _lookup_attr(self.live_sources, lookup_name, nm_id, attribute, scale)

        if metric_key in {
            "openCount",
            "cartCount",
            "orderCount",
            "orderSum",
            "buyoutCount",
            "buyoutSum",
            "buyoutPercent",
            "addToCartConversion",
            "cartToOrderConversion",
            "addToWishlistCount",
        }:
            return self.live_sources.history_lookup.get(nm_id, {}).get(metric_key)
        if metric_key == "localizationPercent":
            return _lookup_attr(self.live_sources, "sf_period_lookup", nm_id, "localization_percent", 0.01)
        if metric_key == "feedbackRating":
            return _lookup_attr(self.live_sources, "sf_period_lookup", nm_id, "feedback_rating", 1.0)
        if metric_key in BLOCKED_SOURCE_METRIC_KEYS:
            return None
        raise ValueError(f"unsupported direct metric_key: {metric_key}")


BLOCKED_SOURCE_METRIC_KEYS = {
    "promo_participation",
    "promo_count_by_price",
    "promo_entry_price_best",
    "cost_price_rub",
}


def _build_metric_rows(
    metric: MetricV2Item,
    enabled_config: list[ConfigV2Item],
    evaluator: _MetricEvaluator,
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    if metric.scope == "TOTAL":
        rows.append([f"Итого: {metric.label_ru}", f"TOTAL|{metric.metric_key}", _to_sheet_value(evaluator.resolve_total(metric.metric_key))])
        return rows
    if metric.scope == "GROUP":
        for group_name, group_items in _group_config(enabled_config).items():
            if not group_items:
                continue
            rows.append(
                [
                    f"Группа {group_name}: {metric.label_ru}",
                    f"GROUP:{group_name}|{metric.metric_key}",
                    _to_sheet_value(evaluator.resolve_group(metric.metric_key, group_name)),
                ]
            )
        return rows
    if metric.scope == "SKU":
        for config_item in enabled_config:
            rows.append(
                [
                    f"{config_item.display_name}: {metric.label_ru}",
                    f"SKU:{config_item.nm_id}|{metric.metric_key}",
                    _to_sheet_value(evaluator.resolve_sku(metric.metric_key, config_item.nm_id)),
                ]
            )
        return rows
    raise ValueError(f"unsupported metric scope: {metric.scope}")


def _build_status_rows(
    *,
    current_state: Any,
    displayed_metrics: list[MetricV2Item],
    data_rows: list[list[Any]],
    live_sources: LiveSources,
    scope_row_counts: Mapping[str, int],
    section_row_counts: Mapping[str, int],
) -> list[list[Any]]:
    non_empty_value_rows = sum(1 for row in data_rows if row[2] not in ("", None))
    status_rows = [
        [
            "registry_upload_current_state",
            "success",
            current_state.activated_at[:10],
            current_state.activated_at[:10],
            "",
            "",
            "",
            len(current_state.config_v2),
            len([item for item in current_state.config_v2 if item.enabled]),
            "",
            _format_note(
                {
                    "bundle_version": current_state.bundle_version,
                    "config_count": len(current_state.config_v2),
                    "metrics_count": len(current_state.metrics_v2),
                    "formulas_count": len(current_state.formulas_v2),
                    "displayed_metrics": len(displayed_metrics),
                    "alias_zone": "openCount!=open_card_count",
                    "total_avg_policy": "preserve_uploaded_total_avg",
                    "section_dictionary": "uploaded_authoritative",
                    "config_service_values": "preserve_CONFIG_HI",
                }
            ),
        ]
    ]
    status_rows.extend(
        [
            [
                status.source_key,
                status.kind,
                status.freshness,
                status.snapshot_date,
                status.date,
                status.date_from,
                status.date_to,
                status.requested_count,
                status.covered_count,
                _format_missing_nm_ids(status.missing_nm_ids),
                status.note,
            ]
            for status in live_sources.statuses
        ]
    )
    status_rows.append(
        [
            "sheet_vitrina_v1_compact_live_v2",
            "success",
            current_state.activated_at[:10],
            current_state.activated_at[:10],
            "",
            "",
            "",
            len(displayed_metrics),
            len(displayed_metrics),
            "",
            _format_note(
                {
                    "displayed_metrics": len(displayed_metrics),
                    "display_rows": len(data_rows),
                    "non_empty_value_rows": non_empty_value_rows,
                    "scope_row_counts": _format_counter(scope_row_counts),
                    "section_row_counts": _format_counter(section_row_counts),
                    "blocked_sources": ",".join(sorted(BLOCKED_SOURCE_STATUSES)),
                }
            ),
        ]
    )
    return status_rows


def _metric_key_from_lookup(lookup_name: str, attribute: str) -> str:
    if lookup_name == "sf_period_lookup" and attribute == "localization_percent":
        return "localizationPercent"
    if lookup_name == "sf_period_lookup" and attribute == "feedback_rating":
        return "feedbackRating"
    return attribute


def _lookup_attr(live_sources: LiveSources, lookup_name: str, nm_id: int, attribute: str, scale: float) -> float | None:
    lookup = getattr(live_sources, lookup_name)
    item = lookup.get(nm_id)
    if item is None:
        return None
    value = getattr(item, attribute, None)
    if value is None:
        return None
    return float(value) * scale


def _capture_live_source(
    source_key: str,
    requested_nm_ids: list[int],
    loader: Callable[[], Any],
) -> tuple[LiveSourceStatus, Any | None]:
    try:
        payload = loader()
    except Exception as exc:  # pragma: no cover - live transport fallback
        return (
            LiveSourceStatus(
                source_key=source_key,
                kind="error",
                freshness="",
                snapshot_date="",
                date="",
                date_from="",
                date_to="",
                requested_count=len(requested_nm_ids),
                covered_count=0,
                missing_nm_ids=[],
                note=str(exc),
            ),
            None,
        )

    if payload is None:
        return (
            LiveSourceStatus(
                source_key=source_key,
                kind="missing",
                freshness="",
                snapshot_date="",
                date="",
                date_from="",
                date_to="",
                requested_count=len(requested_nm_ids),
                covered_count=0,
                missing_nm_ids=[],
                note="no payload returned",
            ),
            None,
        )

    kind = str(getattr(payload, "kind", "missing"))
    if kind == "incomplete":
        missing_nm_ids = list(getattr(payload, "missing_nm_ids", []))
        requested_count = int(getattr(payload, "requested_count", len(requested_nm_ids)))
        covered_count = int(getattr(payload, "covered_count", 0))
        return (
            LiveSourceStatus(
                source_key=source_key,
                kind=kind,
                freshness=_resolve_freshness(payload),
                snapshot_date=str(getattr(payload, "snapshot_date", "") or ""),
                date=str(getattr(payload, "date", "") or ""),
                date_from=str(getattr(payload, "date_from", "") or ""),
                date_to=str(getattr(payload, "date_to", "") or ""),
                requested_count=requested_count,
                covered_count=covered_count,
                missing_nm_ids=missing_nm_ids,
                note=str(getattr(payload, "detail", "") or ""),
            ),
            payload,
        )

    items = list(getattr(payload, "items", []) or [])
    covered_nm_ids = {getattr(item, "nm_id", None) for item in items if isinstance(getattr(item, "nm_id", None), int)}
    covered_nm_ids.discard(None)
    return (
        LiveSourceStatus(
            source_key=source_key,
            kind=kind,
            freshness=_resolve_freshness(payload),
            snapshot_date=str(getattr(payload, "snapshot_date", "") or ""),
            date=str(getattr(payload, "date", "") or ""),
            date_from=str(getattr(payload, "date_from", "") or ""),
            date_to=str(getattr(payload, "date_to", "") or ""),
            requested_count=len(requested_nm_ids),
            covered_count=len(covered_nm_ids),
            missing_nm_ids=sorted(set(requested_nm_ids) - set(covered_nm_ids)),
            note=_status_note_from_payload(payload),
        ),
        payload,
    )


def _status_note_from_payload(payload: Any) -> str:
    parts: list[str] = []
    detail = getattr(payload, "detail", "")
    if detail:
        parts.append(str(detail))
    storage_total = getattr(payload, "storage_total", None)
    if storage_total is not None:
        fee_total = getattr(storage_total, "fin_storage_fee_total", None)
        if fee_total is not None:
            parts.append(f"fin_storage_fee_total={round(float(fee_total), 6)}")
    return "; ".join(parts)


def _resolve_freshness(payload: Any) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(payload, field, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _index_items_by_nm_id(payload: Any | None) -> dict[int, Any]:
    if payload is None:
        return {}
    items = getattr(payload, "items", None)
    if not isinstance(items, list):
        return {}
    return {int(item.nm_id): item for item in items if isinstance(getattr(item, "nm_id", None), int)}


def _index_history_items(payload: Any | None) -> dict[int, dict[str, float]]:
    if payload is None:
        return {}
    items = getattr(payload, "items", None)
    if not isinstance(items, list):
        return {}
    latest: dict[tuple[int, str], tuple[str, float]] = {}
    for item in items:
        nm_id = getattr(item, "nm_id", None)
        metric = getattr(item, "metric", None)
        date = getattr(item, "date", None)
        value = getattr(item, "value", None)
        if not isinstance(nm_id, int) or not isinstance(metric, str) or not isinstance(date, str):
            continue
        if not isinstance(value, (int, float)):
            continue
        cache_key = (nm_id, metric)
        previous = latest.get(cache_key)
        if previous is None or date > previous[0]:
            latest[cache_key] = (date, float(value))
    out: dict[int, dict[str, float]] = {}
    for (nm_id, metric), (_, value) in latest.items():
        out.setdefault(nm_id, {})[metric] = value
    return out


def _resolve_as_of_date(value: str | None) -> str:
    if value:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    return str((datetime.now(timezone.utc).date() - timedelta(days=1)))


def _group_config(config_items: list[ConfigV2Item]) -> dict[str, list[ConfigV2Item]]:
    grouped: dict[str, list[ConfigV2Item]] = {}
    for item in config_items:
        grouped.setdefault(item.group, []).append(item)
    return {
        group_name: sorted(items, key=lambda row: row.display_order)
        for group_name, items in sorted(grouped.items(), key=lambda pair: pair[1][0].display_order)
    }


def _split_ratio(value: str) -> tuple[str, str]:
    if "/" not in value:
        raise ValueError(f"ratio calc_ref must contain numerator/denominator: {value}")
    numerator, denominator = value.split("/", 1)
    return numerator.strip(), denominator.strip()


def _evaluate_formula(expression: str, resolver: Callable[[str], float | None]) -> float | None:
    expr = expression.strip()
    if expr.upper().startswith("IF(") and expr.endswith(")"):
        args = _split_top_level(expr[3:-1], ";")
        if len(args) != 3:
            raise ValueError(f"unsupported IF formula: {expression}")
        condition_result = _evaluate_condition(args[0], resolver)
        branch = args[1] if condition_result else args[2]
        return _evaluate_formula(branch, resolver)

    dependencies = FORMULA_TOKEN_RE.findall(expr)
    values: dict[str, float] = {}
    for dependency in dependencies:
        resolved = resolver(dependency)
        if resolved is None:
            return None
        values[dependency] = float(resolved)

    normalized = re.sub(r"(?<=\\d),(?=\\d)", ".", expr)
    normalized = FORMULA_TOKEN_RE.sub(lambda match: str(values[match.group(1)]), normalized)
    node = ast.parse(normalized, mode="eval")
    return float(_eval_ast(node.body))


def _evaluate_condition(expression: str, resolver: Callable[[str], float | None]) -> bool:
    dependencies = FORMULA_TOKEN_RE.findall(expression)
    values: dict[str, float] = {}
    for dependency in dependencies:
        resolved = resolver(dependency)
        if resolved is None:
            return False
        values[dependency] = float(resolved)
    normalized = re.sub(r"(?<=\\d),(?=\\d)", ".", expression)
    normalized = FORMULA_TOKEN_RE.sub(lambda match: str(values[match.group(1)]), normalized)
    normalized = normalized.replace("<>", "!=")
    normalized = re.sub(r"(?<![<>=!])=(?!=)", "==", normalized)
    node = ast.parse(normalized, mode="eval")
    return bool(_eval_condition_ast(node.body))


def _split_top_level(value: str, separator: str) -> list[str]:
    depth = 0
    current: list[str] = []
    parts: list[str] = []
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == separator and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    parts.append("".join(current).strip())
    return parts


def _eval_ast(node: ast.AST) -> float:
    if isinstance(node, ast.BinOp):
        left = _eval_ast(node.left)
        right = _eval_ast(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_ast(node.operand)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    raise ValueError("unsupported formula expression")


def _eval_condition_ast(node: ast.AST) -> bool:
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left = _eval_ast(node.left)
        right = _eval_ast(node.comparators[0])
        op = node.ops[0]
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return bool(node.value)
    raise ValueError("unsupported formula condition")


def _to_sheet_value(value: float | None) -> Any:
    if value is None:
        return ""
    return round(float(value), 6)


def _format_missing_nm_ids(value: list[int]) -> str:
    return ",".join(str(item) for item in value)


def _format_counter(value: Mapping[str, int]) -> str:
    return ",".join(f"{key}:{value[key]}" for key in sorted(value))


def _format_note(value: Mapping[str, Any]) -> str:
    return "; ".join(f"{key}={value[key]}" for key in value if value[key] not in (None, ""))


def _load_json(path: Path) -> Mapping[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
