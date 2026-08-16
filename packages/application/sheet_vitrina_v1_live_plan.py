"""Heavy refresh-builder для ready snapshot sheet_vitrina_v1 по uploaded compact registry package."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping, Protocol

from packages.adapters.ads_bids_block import HttpBackedAdsBidsSource
from packages.adapters.ads_compact_block import HttpBackedAdsCompactSource
from packages.adapters.fin_report_daily_block import HttpBackedFinReportDailySource
from packages.adapters.onec_stocks_block import HttpBackedOnecStocksSource
from packages.adapters.prices_snapshot_block import HttpBackedPricesSnapshotSource
from packages.adapters.sales_funnel_history_block import HttpBackedSalesFunnelHistorySource
from packages.adapters.seller_funnel_snapshot_block import HttpBackedSellerFunnelSnapshotSource
from packages.adapters.sf_period_block import HttpBackedSfPeriodSource
from packages.adapters.spp_proxy_block import HttpBackedPublicWbCardBuyerPriceSource
from packages.adapters.spp_block import HttpBackedSppSource
from packages.adapters.stocks_block import HistoricalCsvBackedStocksSource
from packages.adapters.web_source_current_sync import ShellBackedWebSourceCurrentSync
from packages.adapters.web_source_snapshot_block import HttpBackedWebSourceSnapshotSource
from packages.application.ads_bids_block import AdsBidsBlock
from packages.application.ads_compact_block import AdsCompactBlock
from packages.application.calculation_parameters import (
    CalculationParametersBlock,
    DEFAULT_PROXY_PARAMETERS,
    ProxyParameters,
    calculate_proxy_3,
)
from packages.application.calculation_parameters_v4 import (
    ProxyV4Parameters,
    ProxyV4ParametersBlock,
    calculate_proxy_4,
)
from packages.application.fin_report_daily_block import FinReportDailyBlock
from packages.application.onec_stocks_block import OnecStocksBlock
from packages.application.own_product_capital import OwnProductCapitalBlock
from packages.application.promo_live_source import PromoLiveSourceBlock
from packages.application.prices_snapshot_block import PricesSnapshotBlock
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
    TemporalSourceClosureState,
)
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock
from packages.application.seller_funnel_snapshot_block import SellerFunnelSnapshotBlock
from packages.application.sf_period_block import SfPeriodBlock
from packages.application.sheet_vitrina_v1 import build_sheet_write_plan
from packages.application.sheet_vitrina_v1_archived_metrics import (
    ARCHIVED_ONLY_SOURCE_KEYS,
    ARCHIVED_PUBLIC_METRIC_KEYS,
    filter_archived_public_metrics,
)
from packages.application.sheet_vitrina_v1_buyout_percent import (
    capture_mature_buyout_percent_snapshots,
    extend_metrics_with_buyout_percent,
)
from packages.application.sheet_vitrina_v1_onec_stocks import (
    DEFAULT_ONEC_STAGE_MAPPING,
    ONEC_INVENTORY_CAPITAL_RETURN_PCT_METRIC_KEY,
    ONEC_INVENTORY_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY,
    ONEC_PROXY_MARGIN_2_PCT_METRIC_KEY,
    ONEC_PROXY_MARGIN_2_PCT_TOTAL_METRIC_KEY,
    ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY,
    ONEC_STOCKS_SOURCE_KEY,
    ONEC_STOCKS_SKU_TOTAL_COST_RUB_METRIC_KEY,
    ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY,
    ONEC_STOCKS_WB_UNIT_COST_RUB_METRIC_KEY,
    ONEC_TOTAL_PROXY_PROFIT_2_RUB_METRIC_KEY,
    build_onec_stocks_lookup,
    extend_metrics_with_onec_stock_metrics,
    is_onec_stock_sku_metric_key,
    normalize_onec_stage_code,
    onec_weighted_unit_cost_components,
    resolve_onec_stock_metric_value,
    resolve_onec_stocks_account_id,
    summarize_onec_stage_bucket_coverage,
)
from packages.application.sheet_vitrina_v1_incident_stocks import (
    INCIDENT_STOCK_FIELDS,
    INCIDENT_STOCK_METRIC_KEYS,
    extend_metrics_with_incident_stock_metrics,
    incident_stock_metric_key,
    incident_stock_total_metric_key,
    incident_stock_value,
    is_incident_stock_metric_key,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (
    OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
    OUR_WB_COST_OPENING_DATE,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    TOTAL_OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    extend_metrics_with_our_wb_cost_metrics,
)
from packages.application.sheet_vitrina_v1_proxy_v4 import (
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
    extend_metrics_with_proxy_v4,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_AVG_COST_RUB_METRIC_KEY,
    OWN_AVG_COST_RUB_TOTAL_METRIC_KEY,
    OWN_CAPITAL_RETURN_PCT_METRIC_KEY,
    OWN_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY,
    OWN_PRODUCT_CAPITAL_METRIC_KEYS,
    OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS,
    OWN_PRODUCT_CAPITAL_SOURCE_KEY,
    OWN_PRODUCT_CAPITAL_STAGES,
    OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS,
    OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
    OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY,
    OWN_TOTAL_CONFIRMED_SHARE_PCT_METRIC_KEY,
    OWN_TOTAL_CONFIRMED_SHARE_PCT_TOTAL_METRIC_KEY,
    OWN_TOTAL_QTY_METRIC_KEY,
    OWN_TOTAL_QTY_TOTAL_METRIC_KEY,
    OWN_TOTAL_PAID_EQUIVALENT_QTY_METRIC_KEY,
    OWN_TOTAL_PAID_EQUIVALENT_QTY_TOTAL_METRIC_KEY,
    OWN_UNDERACCEPTED_WB_QTY_METRIC_KEY,
    OWN_UNDERACCEPTED_WB_QTY_TOTAL_METRIC_KEY,
    OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_METRIC_KEY,
    OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_TOTAL_METRIC_KEY,
    extend_metrics_with_own_product_capital_metrics,
    is_own_product_capital_sku_metric_key,
    own_product_capital_metric_value,
    own_stage_metric_key,
    own_stage_total_metric_key,
)
from packages.application.sheet_vitrina_v1_sku_actions import (
    ADVERTISING_BID_CHANGE_RUB_METRIC_KEY,
    BUYER_PRICE_RUB_METRIC_KEY,
    SELLER_PRICE_CHANGE_RUB_METRIC_KEY,
    extend_metrics_with_sku_action_metrics,
)
from packages.application.sheet_vitrina_v1_temporal_policy import (
    CANONICAL_SOURCE_TEMPORAL_POLICIES,
    TEMPORAL_POLICY_YESTERDAY_CLOSED_ONLY,
    source_policy_supports_slot as _canonical_source_policy_supports_slot,
)
from packages.application.warehouse_functional import _warehouse_balance_status_presentation
from packages.application.spp_proxy_block import SppProxyBlock
from packages.application.spp_block import SppBlock
from packages.application.stocks_block import StocksBlock
from packages.application.wb_incident_policy import (
    VITRINA_PROVISIONAL_QUALITY_MESSAGE_RU,
    build_vitrina_incident_stock_projection,
)
from packages.application.web_source_snapshot_block import WebSourceSnapshotBlock
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE,
    business_date_from_timestamp,
    business_datetime_for_override,
    current_business_date_iso,
    default_business_as_of_date,
)
from packages.contracts.cost_price_upload import CostPriceCurrentState, CostPriceRow
from packages.contracts.ads_bids_block import AdsBidsRequest
from packages.contracts.ads_compact_block import AdsCompactRequest
from packages.contracts.fin_report_daily_block import FinReportDailyRequest
from packages.contracts.onec_stocks_block import OnecStocksRequest
from packages.contracts.promo_live_source import PromoLiveSourceRequest
from packages.contracts.promo_live_source import (
    PromoLiveSourceEnvelope,
    PromoLiveSourceItem,
    PromoLiveSourceSuccess,
)
from packages.contracts.prices_snapshot_block import PricesSnapshotRequest
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, FormulaV2Item, MetricV2Item
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryRequest
from packages.contracts.seller_funnel_snapshot_block import SellerFunnelSnapshotRequest
from packages.contracts.sf_period_block import SfPeriodRequest
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaV1TemporalSlot
from packages.contracts.spp_proxy_block import SppProxyRequest
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
DELIVERY_CONTRACT_VERSION = "sheet_vitrina_v1_temporal_live_v1"
TEMPORAL_SLOT_YESTERDAY_CLOSED = "yesterday_closed"
TEMPORAL_SLOT_TODAY_CURRENT = "today_current"
EXECUTION_MODE_AUTO_DAILY = "auto_daily"
EXECUTION_MODE_MANUAL_OPERATOR = "manual_operator"
EXECUTION_MODE_PERSISTED_RETRY = "persisted_retry"
STRICT_CLOSED_DAY_SOURCE_KEYS = {"seller_funnel_snapshot", "web_source_snapshot"}
SPP_PROXY_SOURCE_KEY = "spp_proxy"
SPP_PROXY_METRIC_KEY = "spp_proxy"
SKU_ACTION_SOURCE_KEY = "sku_action_events"
HISTORICAL_CLOSED_DAY_SOURCE_KEYS = STRICT_CLOSED_DAY_SOURCE_KEYS | {
    "sales_funnel_history",
    "sf_period",
    "spp",
    "stocks",
    "ads_compact",
    "fin_report_daily",
    ONEC_STOCKS_SOURCE_KEY,
    OWN_PRODUCT_CAPITAL_SOURCE_KEY,
}
CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS = {"prices_snapshot", "ads_bids", "promo_by_price", SPP_PROXY_SOURCE_KEY}
CURRENT_SNAPSHOT_ONLY_ROLLOVER_SOURCE_KEYS = {"prices_snapshot", "ads_bids", "spp", SPP_PROXY_SOURCE_KEY}
ACCEPTED_CURRENT_SOURCE_KEYS = HISTORICAL_CLOSED_DAY_SOURCE_KEYS | CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS
EXACT_DATE_RUNTIME_CACHE_SOURCE_KEYS = {"sales_funnel_history", "stocks", "promo_by_price", ONEC_STOCKS_SOURCE_KEY}
TEMPORAL_ROLE_PROVISIONAL_CURRENT = "provisional_current_snapshot"
TEMPORAL_ROLE_CLOSED_DAY_CANDIDATE = "closed_day_candidate_snapshot"
TEMPORAL_ROLE_ACCEPTED_CLOSED = "accepted_closed_day_snapshot"
TEMPORAL_ROLE_ACCEPTED_CURRENT = "accepted_current_snapshot"
CLOSURE_STATE_PENDING = "closure_pending"
CLOSURE_STATE_RETRYING = "closure_retrying"
CLOSURE_STATE_RATE_LIMITED = "closure_rate_limited"
CLOSURE_STATE_EXHAUSTED = "closure_exhausted"
CLOSURE_STATE_SUCCESS = "success"
CLOSURE_TERMINAL_STATES = {CLOSURE_STATE_SUCCESS, CLOSURE_STATE_EXHAUSTED}
CLOSURE_RETRY_BACKOFF_MINUTES = [15, 30, 60, 120, 240, 480]
CLOSURE_PENDING_STATES = {
    CLOSURE_STATE_PENDING,
    CLOSURE_STATE_RETRYING,
    CLOSURE_STATE_RATE_LIMITED,
}
BLOCKED_SOURCE_STATUSES = {}
SOURCE_TEMPORAL_POLICIES = {
    **CANONICAL_SOURCE_TEMPORAL_POLICIES,
    OWN_PRODUCT_CAPITAL_SOURCE_KEY: "dual_day_capable",
    SKU_ACTION_SOURCE_KEY: "dual_day_capable",
}
SOURCE_CLASSIFICATION_GROUPS = {
    "seller_funnel_snapshot": "A_bot_web_source_historical_closed_day_capable",
    "web_source_snapshot": "A_bot_web_source_historical_closed_day_capable",
    "sales_funnel_history": "B_wb_api_date_period_capable",
    "sf_period": "B_wb_api_date_period_capable",
    "spp": "C_seller_portal_current_snapshot_with_accepted_current_rollover",
    SPP_PROXY_SOURCE_KEY: "C_wb_public_card_current_snapshot_with_accepted_current_rollover",
    "stocks": "B_wb_api_date_period_capable",
    ONEC_STOCKS_SOURCE_KEY: "E_onec_product_capital_date_capable",
    OWN_PRODUCT_CAPITAL_SOURCE_KEY: "F_webcore_product_capital_persisted_events",
    "ads_compact": "B_wb_api_date_period_capable",
    "fin_report_daily": "B_wb_api_date_period_capable",
    "prices_snapshot": "C_wb_api_current_snapshot_only",
    "ads_bids": "C_wb_api_current_snapshot_only",
    "cost_price": "D_other_non_wb_or_blocked",
    "promo_by_price": "D_other_non_wb_or_browser_collector",
    SKU_ACTION_SOURCE_KEY: "F_webcore_confirmed_operator_events",
}
PERCENT_SOURCE_KEYS = {"ctr", "ctr_current", "localizationPercent"}
SEARCH_CTR_AVG_TOTAL_METRIC_KEY = "avg_ctr_current"
SEARCH_CTR_SKU_METRIC_KEY = "ctr_current"
SEARCH_VIEWS_SKU_METRIC_KEY = "views_current"
DECISION_SUMMARY = {
    "alias_zone": "openCount and open_card_count remain distinct metrics from different sources",
    "total_avg_policy": "preserve all total_/avg_ uploaded rows; total_=sum, avg_=arithmetic_mean",
    "section_dictionary": "uploaded section values are authoritative and are not remapped",
    "config_service_values": "CONFIG!H:I service block is preserved across prepare/reprepare",
}
SOURCE_DIAGNOSTIC_SPECS = {
    OWN_PRODUCT_CAPITAL_SOURCE_KEY: {
        "module": "packages.application.own_product_capital",
        "block": "OwnProductCapitalBlock",
        "adapter": "RegistryUploadDbBackedRuntime",
        "endpoint": "sqlite://canonical_cost_components+daily_state",
    },
    "seller_funnel_snapshot": {
        "module": "packages.application.seller_funnel_snapshot_block",
        "block": "SellerFunnelSnapshotBlock",
        "adapter": "HttpBackedSellerFunnelSnapshotSource",
        "endpoint": "GET /v1/sales-funnel/daily?date=<YYYY-MM-DD>",
    },
    "sales_funnel_history": {
        "module": "packages.application.sales_funnel_history_block",
        "block": "SalesFunnelHistoryBlock",
        "adapter": "HttpBackedSalesFunnelHistorySource",
        "endpoint": "POST /api/analytics/v3/sales-funnel/products/history",
    },
    "web_source_snapshot": {
        "module": "packages.application.web_source_snapshot_block",
        "block": "WebSourceSnapshotBlock",
        "adapter": "HttpBackedWebSourceSnapshotSource",
        "endpoint": "GET /v1/search-analytics/snapshot?date_from=<YYYY-MM-DD>&date_to=<YYYY-MM-DD>",
    },
    "prices_snapshot": {
        "module": "packages.application.prices_snapshot_block",
        "block": "PricesSnapshotBlock",
        "adapter": "HttpBackedPricesSnapshotSource",
        "endpoint": "POST /api/v2/list/goods/filter",
    },
    "sf_period": {
        "module": "packages.application.sf_period_block",
        "block": "SfPeriodBlock",
        "adapter": "HttpBackedSfPeriodSource",
        "endpoint": "POST /api/analytics/v3/sales-funnel/products",
    },
    "spp": {
        "module": "packages.application.spp_block",
        "block": "SppBlock",
        "adapter": "HttpBackedSppSource",
        "endpoint": "GET /api/v1/supplier/sales?dateFrom=<YYYY-MM-DD>",
    },
    SPP_PROXY_SOURCE_KEY: {
        "module": "packages.application.spp_proxy_block",
        "block": "SppProxyBlock",
        "adapter": "HttpBackedPublicWbCardBuyerPriceSource",
        "endpoint": "GET https://www.wildberries.ru/catalog/{nmId}/detail.aspx + public card API fallback",
    },
    "ads_bids": {
        "module": "packages.application.ads_bids_block",
        "block": "AdsBidsBlock",
        "adapter": "HttpBackedAdsBidsSource",
        "endpoint": "GET /adv/v1/promotion/count + GET /api/advert/v2/adverts",
    },
    "stocks": {
        "module": "packages.application.stocks_block",
        "block": "StocksBlock",
        "adapter": "HistoricalCsvBackedStocksSource",
        "endpoint": (
            "POST /api/v2/nm-report/downloads + "
            "GET /api/v2/nm-report/downloads + "
            "GET /api/v2/nm-report/downloads/file/{downloadId} "
            "[reportType=STOCK_HISTORY_DAILY_CSV]"
        ),
    },
    ONEC_STOCKS_SOURCE_KEY: {
        "module": "packages.application.onec_stocks_block",
        "block": "OnecStocksBlock",
        "adapter": "HttpBackedOnecStocksSource",
        "endpoint": "GET /hs/soykasoft/stocks_wb?account_id=<account_id>&date=<YYYY-MM-DD>&nmId=<nmId>",
    },
    "ads_compact": {
        "module": "packages.application.ads_compact_block",
        "block": "AdsCompactBlock",
        "adapter": "HttpBackedAdsCompactSource",
        "endpoint": "GET /adv/v1/promotion/count + GET /adv/v3/fullstats",
    },
    "fin_report_daily": {
        "module": "packages.application.fin_report_daily_block",
        "block": "FinReportDailyBlock",
        "adapter": "HttpBackedFinReportDailySource",
        "endpoint": "GET /api/v5/supplier/reportDetailByPeriod?period=daily",
    },
    "cost_price": {
        "module": "packages.application.sheet_vitrina_v1_live_plan",
        "block": "cost_price_overlay",
        "adapter": "RegistryUploadDbBackedRuntime",
        "endpoint": "sqlite://cost_price_current_state",
    },
    "promo_by_price": {
        "module": "packages.application.promo_live_source",
        "block": "PromoLiveSourceBlock",
        "adapter": "PlaywrightPromoCollectorDriver",
        "endpoint": "seller portal dp-promo-calendar -> metadata/workbook sidecar collector",
    },
}
LivePlanLogEmitter = Callable[[str], None]


@dataclass(frozen=True)
class LiveSourceStatus:
    source_key: str
    temporal_slot: str
    temporal_policy: str
    column_date: str
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
    diagnostics: dict[str, Any] = field(default_factory=dict)

@dataclass
class SlotLookups:
    seller_funnel_lookup: dict[int, Any]
    history_lookup: dict[int, dict[str, float]]
    web_lookup: dict[int, Any]
    prices_lookup: dict[int, Any]
    sf_period_lookup: dict[int, Any]
    spp_lookup: dict[int, Any]
    ads_bids_lookup: dict[int, Any]
    stocks_lookup: dict[int, Any]
    onec_stocks_lookup: dict[int, dict[str, float]]
    ads_compact_lookup: dict[int, Any]
    fin_lookup: dict[int, Any]
    fin_storage_fee_total: float | None
    cost_price_lookup: dict[str, "ResolvedCostPrice"]
    promo_lookup: dict[int, dict[str, float]]
    incident_stocks_lookup: dict[int, dict[str, Any]] = field(default_factory=dict)
    incident_policy: dict[str, Any] = field(default_factory=dict)
    incident_projection_quality: dict[str, Any] = field(default_factory=dict)
    spp_proxy_lookup: dict[int, Any] = field(default_factory=dict)
    our_wb_cost_lookup: dict[int, dict[str, Any]] = field(default_factory=dict)
    own_product_capital_lookup: dict[int, dict[str, Any]] = field(default_factory=dict)
    own_product_capital_cutover_date: str = ""
    sku_action_lookup: dict[int, dict[str, float]] = field(default_factory=dict)
    sku_action_error: str = ""
    column_date: str = ""


@dataclass(frozen=True)
class ResolvedCostPrice:
    group_name: str
    cost_price_rub: float
    effective_from: str


@dataclass(frozen=True)
class TemporalLiveSources:
    temporal_slots: list[SheetVitrinaV1TemporalSlot]
    statuses: list[LiveSourceStatus]
    slot_lookups: dict[str, SlotLookups]
    source_temporal_policies: dict[str, str]


class CurrentWebSourceSync(Protocol):
    def ensure_snapshot(self, snapshot_date: str) -> None:
        raise NotImplementedError


class ClosedDayWebSourceSync(Protocol):
    def ensure_closed_day_snapshot(self, *, source_key: str, snapshot_date: str) -> None:
        raise NotImplementedError


class PromoLiveSourceProtocol(Protocol):
    def execute(self, request: PromoLiveSourceRequest) -> PromoLiveSourceEnvelope:
        raise NotImplementedError


def _new_refresh_diagnostics(*, execution_mode: str, started_at: str) -> dict[str, Any]:
    return {
        "schema_version": "refresh_diagnostics_v1",
        "job_id": "",
        "execution_mode": execution_mode,
        "as_of_date": "",
        "bundle_version": "",
        "started_at": started_at,
        "finished_at": "",
        "duration_ms": None,
        "semantic_status": "",
        "technical_status": "",
        "source_summary": [],
        "source_slots": [],
        "phase_summary": [],
        "origin_unclassified_sources": [],
        "counter_gaps": [
            "adapter-internal retry/sleep/batch/page/poll counters are not emitted by most source adapters yet",
        ],
    }


def _start_refresh_phase(
    diagnostics: dict[str, Any] | None,
    phase_key: str,
    *,
    started_at: str,
) -> dict[str, Any] | None:
    if diagnostics is None:
        return None
    return {
        "phase_key": phase_key,
        "started_at": started_at,
        "started_perf": time.perf_counter(),
    }


def _finish_refresh_phase(
    diagnostics: dict[str, Any] | None,
    phase: Mapping[str, Any] | None,
    *,
    finished_at: str,
    status: str,
    note_kind: str | None = None,
) -> None:
    if diagnostics is None or phase is None:
        return
    phase_summary = diagnostics.setdefault("phase_summary", [])
    if not isinstance(phase_summary, list):
        return
    item = {
        "phase_key": str(phase.get("phase_key") or ""),
        "started_at": str(phase.get("started_at") or ""),
        "finished_at": finished_at,
        "duration_ms": _elapsed_ms(float(phase.get("started_perf") or time.perf_counter())),
        "status": status,
    }
    if note_kind:
        item["note_kind"] = note_kind
    phase_summary.append(item)


def _start_source_slot_diagnostic(
    *,
    source_key: str,
    temporal_slot: str,
    requested_date: str,
    started_at: str,
) -> dict[str, Any]:
    return {
        "source_key": source_key,
        "slot_kind": temporal_slot,
        "requested_date": requested_date,
        "started_at": started_at,
        "started_perf": time.perf_counter(),
    }


def _append_source_slot_diagnostic(
    diagnostics: dict[str, Any] | None,
    *,
    source_started: Mapping[str, Any],
    finished_at: str,
    status: LiveSourceStatus,
    payload: Any | None,
    origin: str,
) -> None:
    if diagnostics is None:
        return
    source_slots = diagnostics.setdefault("source_slots", [])
    if not isinstance(source_slots, list):
        return
    normalized_origin = origin if origin in {
        "accepted_slot",
        "temporal_cache",
        "upstream_fetch",
        "fallback_preserved",
        "not_supported",
        "current_rollover",
        "origin_unclassified",
    } else "origin_unclassified"
    if normalized_origin == "origin_unclassified":
        sources = diagnostics.setdefault("origin_unclassified_sources", [])
        if isinstance(sources, list) and status.source_key not in sources:
            sources.append(status.source_key)
    duration_ms = _elapsed_ms(float(source_started.get("started_perf") or time.perf_counter()))
    rows_reused = status.covered_count if normalized_origin in {
        "accepted_slot",
        "temporal_cache",
        "fallback_preserved",
        "current_rollover",
    } else 0
    rows_fetched = status.covered_count if normalized_origin == "upstream_fetch" else 0
    rows_accepted = status.covered_count if status.kind in {"success", "incomplete"} else 0
    rows_skipped = (
        status.requested_count
        if normalized_origin == "not_supported"
        else len(status.missing_nm_ids)
    )
    item = {
        "source_key": status.source_key,
        "slot_kind": status.temporal_slot,
        "requested_date": str(source_started.get("requested_date") or status.column_date),
        "started_at": str(source_started.get("started_at") or ""),
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "status": status.kind,
        "semantic_status": _source_diagnostic_semantic_status(status),
        "origin": normalized_origin,
        "rows_fetched": rows_fetched,
        "rows_accepted": rows_accepted,
        "rows_reused": rows_reused,
        "rows_skipped": rows_skipped,
        "retry_count": None,
        "sleep_total_ms": None,
        "batch_count": None,
        "page_count": None,
        "poll_count": None,
        "note_kind": _source_diagnostic_note_kind(status),
        "requested_count": status.requested_count,
        "covered_count": status.covered_count,
        "missing_count": len(status.missing_nm_ids),
        "counter_basis": "live_source_status; adapter-internal long-tail counters are not available without adapter refactor",
        **_known_payload_diagnostic_counters(status.source_key, payload),
    }
    promo_diagnostics = _promo_slot_diagnostics(
        status=status,
        payload=payload,
        origin=normalized_origin,
        finished_at=finished_at,
        duration_ms=duration_ms,
    )
    if promo_diagnostics:
        item["promo_diagnostics"] = promo_diagnostics
    source_slots.append(item)


def _classify_source_diagnostic_origin(
    status: LiveSourceStatus,
    *,
    source_key: str,
    temporal_slot: str,
) -> str:
    note = str(status.note or "").lower()
    if status.kind in {"not_available", "blocked"}:
        return "not_supported"
    if "preserved_after_invalid_attempt" in note:
        return "fallback_preserved"
    if temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED and source_key in CURRENT_SNAPSHOT_ONLY_ROLLOVER_SOURCE_KEYS:
        return "current_rollover"
    if "runtime_snapshot" in note and "accepted_" in note:
        return "accepted_slot"
    if "runtime_cache" in note or "cache_captured_at" in note:
        return "temporal_cache"
    if "accepted_closed_from_prior_current_cache" in note:
        return "temporal_cache"
    if "current_attempt" in note or "interval_replay" in note or status.kind in {
        "success",
        "empty",
        "incomplete",
        "missing",
        "not_found",
        "error",
    }:
        return "upstream_fetch"
    return "origin_unclassified"


def _source_diagnostic_note_kind(status: LiveSourceStatus) -> str | None:
    note = str(status.note or "").lower()
    if status.kind == "not_available":
        return "not_supported"
    if "preserved_after_invalid_attempt" in note:
        return "fallback_preserved"
    if "accepted_closed_from_prior_current_snapshot" in note:
        return "accepted_current_rollover"
    if "runtime_cache" in note:
        return "temporal_cache"
    if "interval_replay" in note:
        return "interval_replay"
    if "current_attempt" in note:
        return "upstream_fetch_accepted"
    if "current_day_web_source_sync_failed" in note:
        return "current_web_source_sync_error"
    if status.kind in {"missing", "not_found"}:
        return "missing"
    if status.kind == "error":
        return "error"
    if status.kind == "incomplete":
        return "incomplete"
    if status.kind == "empty":
        return "empty"
    return None


def _source_diagnostic_semantic_status(status: LiveSourceStatus) -> str:
    if status.kind == "success":
        return "success"
    if status.kind in {"blocked", "error"}:
        return "error"
    if status.kind == "not_available":
        return "skipped"
    return "warning"


def _known_payload_diagnostic_counters(source_key: str, payload: Any | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if source_key == SPP_PROXY_SOURCE_KEY:
        diagnostics = _payload_diagnostics(payload)
        return {
            "spp_proxy_missing_count": diagnostics.get("missing_count"),
            "spp_proxy_covered_count": diagnostics.get("covered_count"),
        }
    if source_key != "promo_by_price":
        return {}
    return {
        "promo_current_promos": getattr(payload, "current_promos", None),
        "promo_current_promos_downloaded": getattr(payload, "current_promos_downloaded", None),
        "promo_current_promos_blocked": getattr(payload, "current_promos_blocked", None),
        "promo_future_promos": getattr(payload, "future_promos", None),
        "promo_skipped_past_promos": getattr(payload, "skipped_past_promos", None),
        "promo_ambiguous_promos": getattr(payload, "ambiguous_promos", None),
    }


def _promo_slot_diagnostics(
    *,
    status: LiveSourceStatus,
    payload: Any | None,
    origin: str,
    finished_at: str,
    duration_ms: int,
) -> dict[str, Any]:
    if status.source_key != "promo_by_price":
        return {}
    diagnostics = _plain_jsonable(status.diagnostics)
    if not isinstance(diagnostics, dict) or not diagnostics:
        diagnostics = _payload_diagnostics(payload) if payload is not None else {}
    diagnostics = dict(diagnostics) if isinstance(diagnostics, dict) else {}
    note_values = _note_key_values(status.note)
    fallback = dict(diagnostics.get("fallback") or {})
    latest_attempt_kind = str(note_values.get("latest_attempt_kind") or "").strip()
    current_attempt_status = latest_attempt_kind or status.kind
    fallback["attempted_current_fetch"] = bool(
        fallback.get("attempted_current_fetch")
        or (
            status.temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT
            and origin in {"upstream_fetch", "fallback_preserved"}
        )
        or latest_attempt_kind
    )
    candidate_accepted = (
        "accepted_current_current_attempt" in str(status.note)
        or "accepted_closed_current_attempt" in str(status.note)
        or (origin == "upstream_fetch" and status.kind == "success")
    )
    if origin == "fallback_preserved":
        candidate_accepted = False
    fallback["candidate_accepted"] = candidate_accepted
    fallback["candidate_rejected"] = bool(origin == "fallback_preserved" or (current_attempt_status and current_attempt_status != "success"))
    fallback["invalid_reason"] = _promo_invalid_reason(status.note) or fallback.get("invalid_reason")
    fallback["fallback_reason"] = (
        "accepted_current_preserved_after_invalid_attempt"
        if origin == "fallback_preserved" and status.temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT
        else (
            "accepted_closed_preserved_after_invalid_attempt"
            if origin == "fallback_preserved"
            else fallback.get("fallback_reason")
        )
    )
    if origin == "fallback_preserved":
        fallback["preserved_snapshot_date"] = status.snapshot_date or status.column_date
        fallback["preserved_snapshot_role"] = (
            TEMPORAL_ROLE_ACCEPTED_CURRENT
            if status.temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT
            else TEMPORAL_ROLE_ACCEPTED_CLOSED
        )
        fallback["preserved_origin"] = "accepted_slot"
        accepted_at = note_values.get("accepted_at")
        fallback["preserved_snapshot_age_ms"] = _timestamp_age_ms(accepted_at, finished_at) if accepted_at else None
    fallback["current_attempt_status"] = current_attempt_status
    fallback["current_attempt_semantic_status"] = _promo_attempt_semantic_status(current_attempt_status)
    diagnostics["fallback"] = fallback

    dry_run = dict(diagnostics.get("dry_run_skip") or {})
    fingerprints = diagnostics.get("fingerprints") if isinstance(diagnostics.get("fingerprints"), dict) else {}
    would_not_skip_reason = "comparison_not_available"
    if not fingerprints.get("promo_discovery_fingerprint") and not fingerprints.get("promo_archive_fingerprint"):
        would_not_skip_reason = "missing_fingerprint"
    elif not fingerprints.get("accepted_price_truth_fingerprint"):
        would_not_skip_reason = "missing_price_truth_fingerprint"
    elif origin == "fallback_preserved":
        would_not_skip_reason = "candidate_rejected"
    dry_run.update(
        {
            "would_skip_if_fingerprint_unchanged": False,
            "would_skip_reason": None,
            "would_not_skip_reason": would_not_skip_reason,
            "estimated_avoidable_ms": None,
        }
    )
    diagnostics["dry_run_skip"] = dry_run
    _upsert_promo_phase(
        diagnostics,
        "acceptance_decision",
        status="accepted" if candidate_accepted else "rejected",
        note_kind="temporal_policy_layer",
    )
    _upsert_promo_phase(
        diagnostics,
        "fallback_preserve",
        status="success" if origin == "fallback_preserved" else "skipped",
        note_kind="fallback_preserved" if origin == "fallback_preserved" else "not_needed",
    )
    diagnostics["slot_observed_duration_ms"] = duration_ms
    return diagnostics


def _upsert_promo_phase(
    diagnostics: dict[str, Any],
    phase_key: str,
    *,
    status: str,
    note_kind: str,
) -> None:
    phase_summary = diagnostics.setdefault("phase_summary", [])
    if not isinstance(phase_summary, list):
        return
    for item in phase_summary:
        if isinstance(item, dict) and item.get("phase_key") == phase_key:
            item["status"] = status
            item["note_kind"] = note_kind
            item.setdefault("duration_ms", 0)
            return
    ts = str(diagnostics.get("finished_at") or "")
    phase_summary.append(
        {
            "phase_key": phase_key,
            "started_at": ts,
            "finished_at": ts,
            "duration_ms": 0,
            "status": status,
            "note_kind": note_kind,
        }
    )


def _note_key_values(note: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in str(note or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def _promo_invalid_reason(note: str) -> str | None:
    values = _note_key_values(note)
    invalid_exact = values.get("invalid_exact_snapshot")
    if invalid_exact:
        return invalid_exact
    if "missing_daily_price_truth_nm_ids" in values:
        return "missing_price_truth"
    artifact_reason = values.get("artifact_validation_failed", "")
    if (
        ("metadata_only_ended_without_download" in artifact_reason or "ended_without_download" in artifact_reason)
        and "metadata_only_true_artifact_loss" not in artifact_reason
        and "workbook_file_missing" not in artifact_reason
        and "workbook_unusable" not in artifact_reason
    ):
        return "ended_without_download"
    if "missing_campaign_artifacts" in values:
        return "workbook_or_archive_artifact_missing"
    latest_attempt_note = values.get("latest_attempt_note")
    if latest_attempt_note and latest_attempt_note != note:
        return _promo_invalid_reason(latest_attempt_note)
    if "promo_live_source_incomplete" in str(note):
        return "promo_live_source_incomplete"
    return None


def _promo_attempt_semantic_status(kind: str) -> str | None:
    if not kind:
        return None
    if kind == "success":
        return "success"
    if kind in {"blocked", "error"}:
        return "error"
    return "warning"


def _timestamp_age_ms(older: str | None, newer: str | None) -> int | None:
    try:
        if not older or not newer:
            return None
        older_dt = _parse_runtime_timestamp(str(older))
        newer_dt = _parse_runtime_timestamp(str(newer))
        return max(0, int(round((newer_dt - older_dt).total_seconds() * 1000)))
    except Exception:
        return None


def _build_refresh_source_summary(raw_source_slots: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_source_slots, list):
        return []
    by_source: dict[str, dict[str, Any]] = {}
    for item in raw_source_slots:
        if not isinstance(item, Mapping):
            continue
        source_key = str(item.get("source_key") or "")
        if not source_key:
            continue
        summary = by_source.setdefault(
            source_key,
            {
                "source_key": source_key,
                "slot_count": 0,
                "duration_ms": 0,
                "status_counts": {},
                "origin_counts": {},
                "rows_fetched": 0,
                "rows_accepted": 0,
                "rows_reused": 0,
                "rows_skipped": 0,
            },
        )
        summary["slot_count"] += 1
        summary["duration_ms"] += int(item.get("duration_ms") or 0)
        for key in ("status", "origin"):
            value = str(item.get(key) or "")
            counts_key = f"{key}_counts"
            counts = summary[counts_key]
            counts[value] = counts.get(value, 0) + 1
        for key in ("rows_fetched", "rows_accepted", "rows_reused", "rows_skipped"):
            summary[key] += int(item.get(key) or 0)
    return [by_source[key] for key in sorted(by_source)]


def _duration_ms_from_phase_summary(raw_phase_summary: Any) -> int | None:
    if not isinstance(raw_phase_summary, list) or not raw_phase_summary:
        return None
    return sum(
        int(item.get("duration_ms") or 0)
        for item in raw_phase_summary
        if isinstance(item, Mapping)
    )


def _elapsed_ms(started_perf: float) -> int:
    return max(0, int(round((time.perf_counter() - started_perf) * 1000)))


class _SyntheticNoPromoLiveSourceBlock:
    def execute(self, request: PromoLiveSourceRequest) -> PromoLiveSourceEnvelope:
        items = [
            PromoLiveSourceItem(
                snapshot_date=request.snapshot_date,
                nm_id=nm_id,
                promo_count_by_price=0.0,
                promo_entry_price_best=0.0,
                promo_participation=0.0,
            )
            for nm_id in sorted(request.nm_ids)
        ]
        return PromoLiveSourceEnvelope(
            result=PromoLiveSourceSuccess(
                kind="success",
                snapshot_date=request.snapshot_date,
                date_from=request.snapshot_date,
                date_to=request.snapshot_date,
                requested_count=len(request.nm_ids),
                covered_count=len(request.nm_ids),
                items=items,
                detail="synthetic_no_promo_live_source",
                trace_run_dir="",
                current_promos=0,
                current_promos_downloaded=0,
                current_promos_blocked=0,
                future_promos=0,
                skipped_past_promos=0,
                ambiguous_promos=0,
                current_download_export_kinds=[],
            )
        )


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
        spp_proxy_block: SppProxyBlock | None = None,
        ads_bids_block: AdsBidsBlock | None = None,
        stocks_block: StocksBlock | None = None,
        onec_stocks_block: OnecStocksBlock | None = None,
        ads_compact_block: AdsCompactBlock | None = None,
        fin_report_daily_block: FinReportDailyBlock | None = None,
        promo_live_source_block: PromoLiveSourceProtocol | None = None,
        current_web_source_sync: CurrentWebSourceSync | None = None,
        closed_day_web_source_sync: ClosedDayWebSourceSync | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime = runtime
        self.calculation_parameters_block = CalculationParametersBlock(runtime=runtime)
        self.proxy_v4_parameters_block = ProxyV4ParametersBlock(
            runtime=runtime,
            now_factory=now_factory or _default_now_factory,
        )
        self.web_source_block = web_source_block or WebSourceSnapshotBlock(HttpBackedWebSourceSnapshotSource())
        self.seller_funnel_block = seller_funnel_block or SellerFunnelSnapshotBlock(HttpBackedSellerFunnelSnapshotSource())
        self.sales_funnel_history_block = sales_funnel_history_block or SalesFunnelHistoryBlock(HttpBackedSalesFunnelHistorySource())
        self.prices_snapshot_block = prices_snapshot_block or PricesSnapshotBlock(HttpBackedPricesSnapshotSource())
        self.sf_period_block = sf_period_block or SfPeriodBlock(HttpBackedSfPeriodSource())
        self.spp_block = spp_block or SppBlock(HttpBackedSppSource())
        self.spp_proxy_block = spp_proxy_block or SppProxyBlock(HttpBackedPublicWbCardBuyerPriceSource())
        self.ads_bids_block = ads_bids_block or AdsBidsBlock(HttpBackedAdsBidsSource())
        self.stocks_block = stocks_block or StocksBlock(
            HistoricalCsvBackedStocksSource(
                warehouse_region_resolver=lambda _nm_ids: (
                    _persisted_stocks_warehouse_region_map(runtime)
                )
            )
        )
        self.onec_stocks_block = onec_stocks_block or OnecStocksBlock(
            HttpBackedOnecStocksSource(),
            stage_mapping=DEFAULT_ONEC_STAGE_MAPPING,
        )
        self.ads_compact_block = ads_compact_block or AdsCompactBlock(HttpBackedAdsCompactSource())
        self.fin_report_daily_block = fin_report_daily_block or FinReportDailyBlock(HttpBackedFinReportDailySource())
        self.promo_live_source_block = promo_live_source_block or _SyntheticNoPromoLiveSourceBlock()
        self.current_web_source_sync = current_web_source_sync or ShellBackedWebSourceCurrentSync()
        self.closed_day_web_source_sync = closed_day_web_source_sync or self.current_web_source_sync
        self.now_factory = now_factory or _default_now_factory

    def _diagnostic_timestamp(self) -> str:
        return _format_runtime_timestamp(self.now_factory())

    def build_plan(
        self,
        as_of_date: str | None = None,
        log: LivePlanLogEmitter | None = None,
        execution_mode: str = EXECUTION_MODE_AUTO_DAILY,
        source_keys: Iterable[str] | None = None,
        metric_keys: Iterable[str] | None = None,
        _include_archived_metrics_for_audit: bool = False,
    ) -> SheetVitrinaV1Envelope:
        emit = log or _noop_live_plan_log
        selected_source_keys = {str(item).strip() for item in (source_keys or []) if str(item).strip()}
        selected_metric_keys = {str(item).strip() for item in (metric_keys or []) if str(item).strip()}
        if _include_archived_metrics_for_audit and (
            not selected_source_keys
            or not selected_source_keys.issubset(ARCHIVED_ONLY_SOURCE_KEYS)
            or not selected_metric_keys
            or not selected_metric_keys.issubset(ARCHIVED_PUBLIC_METRIC_KEYS)
        ):
            raise ValueError(
                "technical archived metric evaluation requires only archived source and metric keys"
            )
        diagnostics = _new_refresh_diagnostics(
            execution_mode=execution_mode,
            started_at=self._diagnostic_timestamp(),
        )
        current_state = self.runtime.load_current_state()
        effective_date = _resolve_as_of_date(as_of_date, now=self.now_factory())
        diagnostics["as_of_date"] = effective_date
        diagnostics["bundle_version"] = current_state.bundle_version
        current_date = current_business_date_iso(self.now_factory())
        temporal_slots = _build_temporal_slots(
            as_of_date=effective_date,
            current_date=current_date,
        )
        emit(
            _format_log_event(
                "refresh_plan_context",
                bundle_version=current_state.bundle_version,
                requested_as_of_date=as_of_date or "default",
                resolved_as_of_date=effective_date,
                current_business_date=current_date,
                execution_mode=execution_mode,
                temporal_slots=",".join(
                    f"{slot.slot_key}:{slot.column_date}" for slot in temporal_slots
                ),
            )
        )
        enabled_config = sorted(
            [item for item in current_state.config_v2 if item.enabled],
            key=lambda item: item.display_order,
        )
        if not enabled_config:
            raise ValueError("current registry config_v2 does not contain enabled rows")

        mature_buyout_started = _start_refresh_phase(
            diagnostics,
            "mature_buyout_capture",
            started_at=self._diagnostic_timestamp(),
        )
        if not selected_source_keys or "sales_funnel_history" in selected_source_keys:
            try:
                mature_buyout_capture = capture_mature_buyout_percent_snapshots(
                    runtime=self.runtime,
                    sales_funnel_history_block=self.sales_funnel_history_block,
                    enabled_nm_ids=[item.nm_id for item in enabled_config],
                    now=self.now_factory(),
                    captured_at_factory=self._diagnostic_timestamp,
                ).public()
            except Exception as exc:  # noqa: BLE001 - ordinary refresh stays truthful and retryable.
                mature_buyout_capture = {
                    "status": "error",
                    "business_date": current_date,
                    "trusted_cutoff": (
                        date.fromisoformat(current_date) - timedelta(days=6)
                    ).isoformat(),
                    "requested_nm_id_count": len(enabled_config),
                    "detail": str(exc),
                }
            diagnostics["mature_buyout_capture"] = mature_buyout_capture
            _finish_refresh_phase(
                diagnostics,
                mature_buyout_started,
                finished_at=self._diagnostic_timestamp(),
                status=(
                    "error"
                    if mature_buyout_capture.get("status") in {"error", "partial"}
                    else "success"
                ),
            )
            emit(
                _format_log_event(
                    "mature_buyout_capture",
                    **mature_buyout_capture,
                )
            )
        else:
            diagnostics["mature_buyout_capture"] = {
                "status": "skipped",
                "detail": "sales_funnel_history source group is excluded",
            }
            _finish_refresh_phase(
                diagnostics,
                mature_buyout_started,
                finished_at=self._diagnostic_timestamp(),
                status="skipped",
                note_kind="source_scope_excluded",
            )

        proxy_v4_rollover = (
            self.proxy_v4_parameters_block.materialize_latest_confirmed_window(
                business_date=current_date,
            )
            if not selected_source_keys and not selected_metric_keys
            else {
                "status": "skipped_partial_refresh",
                "created": False,
                "effective_date": current_date,
                "detail": "Proxy V4 rollover is owned by the complete Vitrina refresh.",
            }
        )
        diagnostics["proxy_v4_rollover"] = proxy_v4_rollover
        emit(
            _format_log_event(
                "proxy_v4_rollover",
                status=str(proxy_v4_rollover.get("status") or "unknown"),
                created=bool(proxy_v4_rollover.get("created")),
                version_id=str(proxy_v4_rollover.get("version_id") or ""),
            )
        )

        effective_metrics = extend_metrics_with_buyout_percent(
            extend_metrics_with_sku_action_metrics(
                extend_metrics_with_incident_stock_metrics(
                    extend_metrics_with_own_product_capital_metrics(
                        extend_metrics_with_proxy_v4(
                            extend_metrics_with_our_wb_cost_metrics(
                                extend_metrics_with_onec_stock_metrics(current_state.metrics_v2)
                            )
                        )
                    )
                )
            )
        )
        metrics_by_key = {item.metric_key: item for item in effective_metrics}
        formulas_by_id = {item.formula_id: item for item in current_state.formulas_v2}
        public_metrics = (
            effective_metrics
            if _include_archived_metrics_for_audit
            else filter_archived_public_metrics(effective_metrics)
        )
        displayed_metrics = sorted(
            [item for item in public_metrics if item.enabled and item.show_in_data],
            key=lambda item: item.display_order,
        )
        if selected_metric_keys:
            displayed_metrics = [item for item in displayed_metrics if item.metric_key in selected_metric_keys]
        if not displayed_metrics:
            raise ValueError("current registry metrics_v2 does not contain enabled show_in_data rows")
        emit(
            _format_log_event(
                "refresh_registry_state",
                enabled_config_count=len(enabled_config),
                displayed_metrics=len(displayed_metrics),
                formulas=len(current_state.formulas_v2),
                selected_sources=",".join(sorted(selected_source_keys)),
                selected_metrics=",".join(sorted(selected_metric_keys)),
            )
        )

        cost_price_state = _load_cost_price_current_state(self.runtime)
        current_web_source_sync_note = None
        current_sync_started = _start_refresh_phase(
            diagnostics,
            "current_web_source_sync",
            started_at=self._diagnostic_timestamp(),
        )
        if not selected_source_keys or "web_source_snapshot" in selected_source_keys:
            emit(
                _format_log_event(
                    "current_web_source_sync_start",
                    source="current_day_web_source_sync",
                    target_date=current_date,
                    block="ShellBackedWebSourceCurrentSync",
                )
            )
            current_web_source_sync_note = self._sync_current_web_source_snapshot(current_date)
            _finish_refresh_phase(
                diagnostics,
                current_sync_started,
                finished_at=self._diagnostic_timestamp(),
                status="error" if current_web_source_sync_note else "success",
                note_kind="current_web_source_sync_error" if current_web_source_sync_note else None,
            )
            emit(
                _format_log_event(
                    "current_web_source_sync_finish",
                    source="current_day_web_source_sync",
                    target_date=current_date,
                    status="error" if current_web_source_sync_note else "success",
                    note=current_web_source_sync_note or "snapshot ensured or already materialized",
                )
            )
        else:
            _finish_refresh_phase(
                diagnostics,
                current_sync_started,
                finished_at=self._diagnostic_timestamp(),
                status="skipped",
                note_kind="source_scope_excluded",
            )
            emit(
                _format_log_event(
                    "current_web_source_sync_skipped",
                    source="current_day_web_source_sync",
                    target_date=current_date,
                    reason="source group does not include web_source_snapshot",
                )
            )
        live_sources = self._load_live_sources(
            enabled_config,
            temporal_slots,
            cost_price_state,
            current_web_source_sync_note=current_web_source_sync_note,
            execution_mode=execution_mode,
            log=emit,
            source_keys=selected_source_keys or None,
            diagnostics=diagnostics,
        )
        evaluator = _MetricEvaluator(
            enabled_config=enabled_config,
            metrics_by_key=metrics_by_key,
            formulas_by_id=formulas_by_id,
            live_sources=live_sources,
            proxy_parameters_resolver=self.calculation_parameters_block.parameters_for_date,
            proxy_v4_parameters_resolver=self.proxy_v4_parameters_block.parameters_for_date,
        )

        materialize_data_started = _start_refresh_phase(
            diagnostics,
            "materialize_data_vitrina",
            started_at=self._diagnostic_timestamp(),
        )
        data_rows: list[list[Any]] = []
        scope_row_counts = {"TOTAL": 0, "GROUP": 0, "SKU": 0}
        section_row_counts: dict[str, int] = {}
        for metric in displayed_metrics:
            rows = _build_metric_rows(metric, enabled_config, evaluator, temporal_slots)
            data_rows.extend(rows)
            scope_row_counts[metric.scope] = scope_row_counts.get(metric.scope, 0) + len(rows)
            section_row_counts[metric.section] = section_row_counts.get(metric.section, 0) + len(rows)
        _finish_refresh_phase(
            diagnostics,
            materialize_data_started,
            finished_at=self._diagnostic_timestamp(),
            status="success",
        )
        emit(
            _format_log_event(
                "metric_rows_materialized",
                rows=len(data_rows),
                scope_row_counts=_format_counter(scope_row_counts),
                section_row_counts=_format_counter(section_row_counts),
            )
        )
        _emit_metric_batch_logs(
            emit,
            displayed_metrics=displayed_metrics,
            data_rows=data_rows,
            temporal_slots=temporal_slots,
        )

        data_header = ["label", "key", *[slot.column_date for slot in temporal_slots]]
        materialize_status_started = _start_refresh_phase(
            diagnostics,
            "materialize_status",
            started_at=self._diagnostic_timestamp(),
        )
        status_rows = _build_status_rows(
            current_state=current_state,
            displayed_metrics=displayed_metrics,
            data_rows=data_rows,
            live_sources=live_sources,
            temporal_slots=temporal_slots,
            scope_row_counts=scope_row_counts,
            section_row_counts=section_row_counts,
            execution_mode=execution_mode,
        )
        _finish_refresh_phase(
            diagnostics,
            materialize_status_started,
            finished_at=self._diagnostic_timestamp(),
            status="success",
        )
        emit(
            _format_log_event(
                "status_sheet_materialized",
                rows=len(status_rows),
                blocked_sources=",".join(sorted(BLOCKED_SOURCE_STATUSES)),
            )
        )
        delivery_bundle = {
            "delivery_contract_version": DELIVERY_CONTRACT_VERSION,
            "snapshot_id": f"{effective_date}__{temporal_slots[-1].column_date}__{DELIVERY_CONTRACT_VERSION}__current",
            "as_of_date": effective_date,
            "date_columns": [slot.column_date for slot in temporal_slots],
            "temporal_slots": [
                {
                    "slot_key": slot.slot_key,
                    "slot_label": slot.slot_label,
                    "column_date": slot.column_date,
                }
                for slot in temporal_slots
            ],
            "source_temporal_policies": SOURCE_TEMPORAL_POLICIES,
            "source_classification_groups": SOURCE_CLASSIFICATION_GROUPS,
            "execution_mode": execution_mode,
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
        plan = build_sheet_write_plan(
            delivery_bundle=delivery_bundle,
            data_layout=_load_json(DATA_LAYOUT_PATH),
            status_layout=_load_json(STATUS_LAYOUT_PATH),
        )
        diagnostics["finished_at"] = self._diagnostic_timestamp()
        diagnostics["duration_ms"] = _duration_ms_from_phase_summary(diagnostics.get("phase_summary"))
        diagnostics["source_summary"] = _build_refresh_source_summary(
            diagnostics.get("source_slots"),
        )
        return replace(
            plan,
            metadata={
                **dict(getattr(plan, "metadata", {}) or {}),
                "refresh_diagnostics": diagnostics,
                "incident_projection_quality_by_date": {
                    slot.column_date: dict(
                        live_sources.slot_lookups[
                            slot.slot_key
                        ].incident_projection_quality
                    )
                    for slot in temporal_slots
                    if slot.slot_key in live_sources.slot_lookups
                    and live_sources.slot_lookups[
                        slot.slot_key
                    ].incident_projection_quality
                },
                "server_cell_presentation": _merge_cell_presentations(
                    _own_product_capital_cell_presentation(
                        enabled_config=enabled_config,
                        displayed_metrics=displayed_metrics,
                        temporal_slots=temporal_slots,
                        live_sources=live_sources,
                    ),
                    _incident_stock_cell_presentation(
                        enabled_config=enabled_config,
                        displayed_metrics=displayed_metrics,
                        temporal_slots=temporal_slots,
                        live_sources=live_sources,
                    ),
                ),
            },
        )

    def list_due_closed_day_retries(self) -> list[TemporalSourceClosureState]:
        now = self.now_factory()
        states = self.runtime.list_temporal_source_closure_states(
            source_keys=sorted(HISTORICAL_CLOSED_DAY_SOURCE_KEYS),
            slot_kind=TEMPORAL_SLOT_YESTERDAY_CLOSED,
            states=sorted(CLOSURE_PENDING_STATES),
        )
        return [state for state in states if _closure_attempt_is_due(state, now)]

    def list_due_current_capture_retries(
        self,
        *,
        current_date: str | None = None,
    ) -> list[TemporalSourceClosureState]:
        now = self.now_factory()
        effective_current_date = current_date or current_business_date_iso(now)
        states = self.runtime.list_temporal_source_closure_states(
            source_keys=sorted(CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS),
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
            states=sorted(CLOSURE_PENDING_STATES),
        )
        return [
            state
            for state in states
            if state.target_date == effective_current_date and _closure_attempt_is_due(state, now)
        ]

    def _sync_current_web_source_snapshot(self, current_date: str) -> str | None:
        try:
            self.current_web_source_sync.ensure_snapshot(current_date)
        except Exception as exc:  # pragma: no cover - bounded runtime fallback
            return f"current_day_web_source_sync_failed={exc}"
        return None

    def _load_live_sources(
        self,
        enabled_config: list[ConfigV2Item],
        temporal_slots: list[SheetVitrinaV1TemporalSlot],
        cost_price_state: CostPriceCurrentState | None,
        current_web_source_sync_note: str | None = None,
        execution_mode: str = EXECUTION_MODE_AUTO_DAILY,
        log: LivePlanLogEmitter | None = None,
        source_keys: set[str] | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> TemporalLiveSources:
        emit = log or _noop_live_plan_log
        selected_source_keys = _expand_selected_source_keys_for_dependencies(
            {str(item).strip() for item in (source_keys or set()) if str(item).strip()}
        )
        requested_nm_ids = [item.nm_id for item in enabled_config]
        requested_groups = sorted({item.group for item in enabled_config})
        own_product_capital_block = OwnProductCapitalBlock(runtime=self.runtime)
        try:
            own_product_capital_cutover_date = (
                own_product_capital_block.functional_warehouse_cutover_date()
            )
        except Exception:
            own_product_capital_cutover_date = ""
        statuses: list[LiveSourceStatus] = []
        slot_lookups: dict[str, SlotLookups] = {
            slot.slot_key: SlotLookups(
                column_date=slot.column_date,
                seller_funnel_lookup={},
                history_lookup={},
                web_lookup={},
                prices_lookup={},
                sf_period_lookup={},
                spp_lookup={},
                spp_proxy_lookup={},
                ads_bids_lookup={},
                stocks_lookup={},
                incident_stocks_lookup={},
                incident_policy={},
                incident_projection_quality={},
                onec_stocks_lookup={},
                our_wb_cost_lookup={},
                own_product_capital_lookup={},
                own_product_capital_cutover_date=own_product_capital_cutover_date,
                sku_action_lookup={},
                sku_action_error="",
                ads_compact_lookup={},
                fin_lookup={},
                fin_storage_fee_total=None,
                cost_price_lookup={},
                promo_lookup={},
            )
            for slot in temporal_slots
        }
        load_live_started = _start_refresh_phase(
            diagnostics,
            "load_live_sources_total",
            started_at=self._diagnostic_timestamp(),
        )
        for slot in temporal_slots:
            current_lookups = slot_lookups[slot.slot_key]
            for source_key, loader in [
                (
                    "seller_funnel_snapshot",
                    lambda slot=slot: self.seller_funnel_block.execute(
                        SellerFunnelSnapshotRequest(
                            snapshot_type="seller_funnel_snapshot",
                            date=slot.column_date,
                            nm_ids=tuple(requested_nm_ids),
                        )
                    ).result,
                ),
                (
                    "sales_funnel_history",
                    lambda slot=slot: self.sales_funnel_history_block.execute(
                        SalesFunnelHistoryRequest(
                            snapshot_type="sales_funnel_history",
                            date_from=slot.column_date,
                            date_to=slot.column_date,
                            nm_ids=requested_nm_ids,
                        )
                    ).result,
                ),
                (
                    "web_source_snapshot",
                    lambda slot=slot: self.web_source_block.execute(
                        WebSourceSnapshotRequest(
                            snapshot_type="web_source_snapshot",
                            date_from=slot.column_date,
                            date_to=slot.column_date,
                        )
                    ).result,
                ),
                (
                    "prices_snapshot",
                    lambda slot=slot: self.prices_snapshot_block.execute(
                        PricesSnapshotRequest(
                            snapshot_type="prices_snapshot",
                            snapshot_date=slot.column_date,
                            nm_ids=requested_nm_ids,
                        )
                    ).result,
                ),
                (
                    "sf_period",
                    lambda slot=slot: self.sf_period_block.execute(
                        SfPeriodRequest(
                            snapshot_type="sf_period",
                            snapshot_date=slot.column_date,
                            nm_ids=requested_nm_ids,
                        )
                    ).result,
                ),
                (
                    "spp",
                    lambda slot=slot: self.spp_block.execute(
                        SppRequest(
                            snapshot_type="spp",
                            snapshot_date=slot.column_date,
                            nm_ids=requested_nm_ids,
                        )
                    ).result,
                ),
                (
                    SPP_PROXY_SOURCE_KEY,
                    lambda slot=slot, current_lookups=current_lookups: self.spp_proxy_block.execute(
                        SppProxyRequest(
                            snapshot_type=SPP_PROXY_SOURCE_KEY,
                            snapshot_date=slot.column_date,
                            nm_ids=requested_nm_ids,
                            price_seller_discounted_by_nm_id=_price_seller_discounted_by_nm_id(
                                current_lookups.prices_lookup
                            ),
                        )
                    ).result,
                ),
                (
                    "ads_bids",
                    lambda slot=slot: self.ads_bids_block.execute(
                        AdsBidsRequest(
                            snapshot_type="ads_bids",
                            snapshot_date=slot.column_date,
                            nm_ids=requested_nm_ids,
                        )
                    ).result,
                ),
                (
                    "stocks",
                    lambda slot=slot: self.stocks_block.execute(
                        StocksRequest(
                            snapshot_type="stocks",
                            snapshot_date=slot.column_date,
                            nm_ids=requested_nm_ids,
                        )
                    ).result,
                ),
                (
                    ONEC_STOCKS_SOURCE_KEY,
                    lambda slot=slot: self.onec_stocks_block.execute(
                        OnecStocksRequest(
                            snapshot_type=ONEC_STOCKS_SOURCE_KEY,
                            account_id=resolve_onec_stocks_account_id(),
                            nm_ids=requested_nm_ids,
                            date=slot.column_date,
                        )
                    ).result,
                ),
                (
                    "ads_compact",
                    lambda slot=slot: self.ads_compact_block.execute(
                        AdsCompactRequest(
                            snapshot_type="ads_compact",
                            snapshot_date=slot.column_date,
                            nm_ids=requested_nm_ids,
                        )
                    ).result,
                ),
                (
                    "fin_report_daily",
                    lambda slot=slot: self.fin_report_daily_block.execute(
                        FinReportDailyRequest(
                            snapshot_type="fin_report_daily",
                            snapshot_date=slot.column_date,
                            nm_ids=requested_nm_ids,
                        )
                    ).result,
                ),
                (
                    "promo_by_price",
                    lambda slot=slot: self.promo_live_source_block.execute(
                        PromoLiveSourceRequest(
                            snapshot_date=slot.column_date,
                            nm_ids=requested_nm_ids,
                        )
                    ).result,
                ),
            ]:
                if selected_source_keys and source_key not in selected_source_keys:
                    continue
                temporal_policy = SOURCE_TEMPORAL_POLICIES[source_key]
                _emit_source_request_log(
                    emit,
                    source_key=source_key,
                    temporal_slot=slot.slot_key,
                    temporal_policy=temporal_policy,
                    column_date=slot.column_date,
                    requested_nm_ids=requested_nm_ids,
                )
                if not _source_policy_supports_slot(temporal_policy, slot.slot_key):
                    source_started = _start_source_slot_diagnostic(
                        source_key=source_key,
                        temporal_slot=slot.slot_key,
                        requested_date=slot.column_date,
                        started_at=self._diagnostic_timestamp(),
                    )
                    gap_status = _build_temporal_gap_status(
                        source_key=source_key,
                        temporal_slot=slot.slot_key,
                        temporal_policy=temporal_policy,
                        column_date=slot.column_date,
                        requested_count=len(requested_nm_ids),
                    )
                    statuses.append(gap_status)
                    _append_source_slot_diagnostic(
                        diagnostics,
                        source_started=source_started,
                        finished_at=self._diagnostic_timestamp(),
                        status=gap_status,
                        payload=None,
                        origin="not_supported",
                    )
                    _emit_source_status_log(emit, gap_status)
                    continue
                source_started = _start_source_slot_diagnostic(
                    source_key=source_key,
                    temporal_slot=slot.slot_key,
                    requested_date=slot.column_date,
                    started_at=self._diagnostic_timestamp(),
                )
                status, payload = self._capture_slot_source(
                    source_key=source_key,
                    temporal_slot=slot.slot_key,
                    temporal_policy=temporal_policy,
                    column_date=slot.column_date,
                    requested_nm_ids=requested_nm_ids,
                    loader=loader,
                    execution_mode=execution_mode,
                    current_web_source_sync_note=(
                        current_web_source_sync_note
                        if slot.slot_key == TEMPORAL_SLOT_TODAY_CURRENT
                        else None
                    ),
                )
                statuses.append(status)
                _append_source_slot_diagnostic(
                    diagnostics,
                    source_started=source_started,
                    finished_at=self._diagnostic_timestamp(),
                    status=status,
                    payload=payload,
                    origin=_classify_source_diagnostic_origin(
                        status,
                        source_key=source_key,
                        temporal_slot=slot.slot_key,
                    ),
                )
                _emit_source_status_log(emit, status)
                if source_key == "seller_funnel_snapshot":
                    current_lookups.seller_funnel_lookup = _index_items_by_nm_id(payload)
                elif source_key == "sales_funnel_history":
                    current_lookups.history_lookup = _index_history_items(payload)
                elif source_key == "web_source_snapshot":
                    current_lookups.web_lookup = _index_items_by_nm_id(payload)
                elif source_key == "prices_snapshot":
                    current_lookups.prices_lookup = _index_items_by_nm_id(payload)
                elif source_key == "sf_period":
                    current_lookups.sf_period_lookup = _index_items_by_nm_id(payload)
                elif source_key == "spp":
                    current_lookups.spp_lookup = _index_items_by_nm_id(payload)
                elif source_key == SPP_PROXY_SOURCE_KEY:
                    current_lookups.spp_proxy_lookup = _index_items_by_nm_id(payload)
                elif source_key == "ads_bids":
                    current_lookups.ads_bids_lookup = _index_items_by_nm_id(payload)
                elif source_key == "stocks":
                    warehouse_granularity_complete = bool(
                        getattr(payload, "warehouse_granularity_complete", True)
                    )
                    current_lookups.stocks_lookup = _stocks_vitrina_lookup(
                        payload,
                        warehouse_granularity_complete=(
                            warehouse_granularity_complete
                        ),
                    )
                    stock_items = list(getattr(payload, "items", []) or [])
                    if all(hasattr(item, "stock_total") for item in stock_items):
                        projection = build_vitrina_incident_stock_projection(
                            self.runtime,
                            items=stock_items,
                            warehouse_rows=list(getattr(payload, "warehouse_rows", []) or []),
                            snapshot_date=str(getattr(payload, "snapshot_date", "") or slot.column_date),
                            fetched_at=str(getattr(payload, "fetched_at", "") or ""),
                            pagination_complete=bool(getattr(payload, "pagination_complete", False)),
                            raw_rows_digest=str(getattr(payload, "raw_rows_digest", "") or ""),
                            warehouse_granularity_complete=(
                                warehouse_granularity_complete
                            ),
                        )
                        current_lookups.incident_stocks_lookup = {
                            int(nm_id): dict(row)
                            for nm_id, row in dict(projection.get("by_nm_id") or {}).items()
                        }
                        current_lookups.incident_policy = dict(projection.get("policy") or {})
                        current_lookups.incident_projection_quality = dict(
                            projection.get("quality") or {}
                        )
                elif source_key == ONEC_STOCKS_SOURCE_KEY:
                    current_lookups.onec_stocks_lookup = build_onec_stocks_lookup(
                        payload,
                        expected_nm_ids=requested_nm_ids,
                    )
                elif source_key == "ads_compact":
                    current_lookups.ads_compact_lookup = _index_items_by_nm_id(payload)
                elif source_key == "fin_report_daily":
                    current_lookups.fin_lookup = _index_items_by_nm_id(payload)
                    storage_total = getattr(payload, "storage_total", None)
                    if storage_total is not None:
                        current_lookups.fin_storage_fee_total = float(
                            getattr(storage_total, "fin_storage_fee_total", 0.0)
                        )
                elif source_key == "promo_by_price":
                    current_lookups.promo_lookup = _index_promo_items(payload)

            try:
                current_lookups.our_wb_cost_lookup = self.runtime.load_our_wb_cost_daily_state(
                    as_of_date=slot.column_date
                )
            except Exception:
                current_lookups.our_wb_cost_lookup = {}
            try:
                current_lookups.own_product_capital_lookup = own_product_capital_block.load_daily_metric_lookup(
                    slot.column_date,
                    requested_nm_ids=requested_nm_ids,
                    revalidate_current_sources=True,
                )
            except Exception:
                current_lookups.own_product_capital_lookup = {}
            try:
                current_lookups.sku_action_lookup = self.runtime.load_sku_action_daily_metric_lookup(
                    slot.column_date
                )
            except Exception:
                current_lookups.sku_action_lookup = {}
                current_lookups.sku_action_error = "sku action event runtime lookup failed"

            if not selected_source_keys or OWN_PRODUCT_CAPITAL_SOURCE_KEY in selected_source_keys:
                covered = sorted(
                    nm_id for nm_id in requested_nm_ids
                    if nm_id in current_lookups.own_product_capital_lookup
                )
                own_status = LiveSourceStatus(
                    source_key=OWN_PRODUCT_CAPITAL_SOURCE_KEY,
                    temporal_slot=slot.slot_key,
                    temporal_policy=SOURCE_TEMPORAL_POLICIES[OWN_PRODUCT_CAPITAL_SOURCE_KEY],
                    column_date=slot.column_date,
                    kind="success" if covered else "incomplete",
                    freshness=slot.column_date if covered else "",
                    snapshot_date=slot.column_date,
                    date=slot.column_date,
                    date_from="",
                    date_to="",
                    requested_count=len(requested_nm_ids),
                    covered_count=len(covered),
                    missing_nm_ids=sorted(set(requested_nm_ids) - set(covered)),
                    note=(
                        "source=WebCore; persisted paid-event capital materialization"
                        if covered
                        else "source=WebCore; no materialized paid-event capital rows"
                    ),
                )
                statuses.append(own_status)
                _emit_source_status_log(emit, own_status)

            if not selected_source_keys or SKU_ACTION_SOURCE_KEY in selected_source_keys:
                covered = sorted(
                    nm_id for nm_id in requested_nm_ids
                    if nm_id in current_lookups.sku_action_lookup
                )
                action_status = LiveSourceStatus(
                    source_key=SKU_ACTION_SOURCE_KEY,
                    temporal_slot=slot.slot_key,
                    temporal_policy=SOURCE_TEMPORAL_POLICIES[SKU_ACTION_SOURCE_KEY],
                    column_date=slot.column_date,
                    kind="error" if current_lookups.sku_action_error else "success",
                    freshness=slot.column_date,
                    snapshot_date=slot.column_date,
                    date=slot.column_date,
                    date_from="",
                    date_to="",
                    requested_count=len(requested_nm_ids),
                    covered_count=len(covered),
                    missing_nm_ids=[],
                    note=current_lookups.sku_action_error or (
                        "source=WebCore; confirmed operator event daily delta; "
                        "missing rows mean no confirmed change, not zero"
                    ),
                )
                statuses.append(action_status)
                _emit_source_status_log(emit, action_status)

        if not selected_source_keys or "cost_price" in selected_source_keys:
            for slot in temporal_slots:
                _emit_source_request_log(
                    emit,
                    source_key="cost_price",
                    temporal_slot=slot.slot_key,
                    temporal_policy=SOURCE_TEMPORAL_POLICIES["cost_price"],
                    column_date=slot.column_date,
                    requested_nm_ids=requested_nm_ids,
                    requested_groups=requested_groups,
                )
                source_started = _start_source_slot_diagnostic(
                    source_key="cost_price",
                    temporal_slot=slot.slot_key,
                    requested_date=slot.column_date,
                    started_at=self._diagnostic_timestamp(),
                )
                cost_price_status, cost_price_lookup = _build_cost_price_status(
                    current_state=cost_price_state,
                    requested_groups=requested_groups,
                    temporal_slot=slot.slot_key,
                    column_date=slot.column_date,
                )
                slot_lookups[slot.slot_key].cost_price_lookup = cost_price_lookup
                statuses.append(cost_price_status)
                _append_source_slot_diagnostic(
                    diagnostics,
                    source_started=source_started,
                    finished_at=self._diagnostic_timestamp(),
                    status=cost_price_status,
                    payload=None,
                    origin="origin_unclassified",
                )
                _emit_source_status_log(emit, cost_price_status)

        for source_key, note in BLOCKED_SOURCE_STATUSES.items():
            if selected_source_keys and source_key not in selected_source_keys:
                continue
            for slot in temporal_slots:
                _emit_source_request_log(
                    emit,
                    source_key=source_key,
                    temporal_slot=slot.slot_key,
                    temporal_policy="blocked",
                    column_date=slot.column_date,
                    requested_nm_ids=requested_nm_ids,
                )
                blocked_status = LiveSourceStatus(
                    source_key=source_key,
                    temporal_slot=slot.slot_key,
                    temporal_policy="blocked",
                    column_date=slot.column_date,
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
                statuses.append(blocked_status)
                _append_source_slot_diagnostic(
                    diagnostics,
                    source_started=_start_source_slot_diagnostic(
                        source_key=source_key,
                        temporal_slot=slot.slot_key,
                        requested_date=slot.column_date,
                        started_at=self._diagnostic_timestamp(),
                    ),
                    finished_at=self._diagnostic_timestamp(),
                    status=blocked_status,
                    payload=None,
                    origin="not_supported",
                )
                _emit_source_status_log(emit, blocked_status)

        _finish_refresh_phase(
            diagnostics,
            load_live_started,
            finished_at=self._diagnostic_timestamp(),
            status="success",
        )
        return TemporalLiveSources(
            temporal_slots=temporal_slots,
            statuses=statuses,
            slot_lookups=slot_lookups,
            source_temporal_policies=dict(SOURCE_TEMPORAL_POLICIES),
        )

    def _capture_slot_source(
        self,
        *,
        source_key: str,
        temporal_slot: str,
        temporal_policy: str,
        column_date: str,
        requested_nm_ids: list[int],
        loader: Callable[[], Any],
        execution_mode: str,
        current_web_source_sync_note: str | None,
    ) -> tuple[LiveSourceStatus, Any | None]:
        if source_key == "promo_by_price" and temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
            return self._capture_promo_closed_day_from_cache(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
            )
        if (
            temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED
            and source_key in CURRENT_SNAPSHOT_ONLY_ROLLOVER_SOURCE_KEYS
        ):
            return self._capture_current_snapshot_closed_day_from_accepted_current(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                loader=loader,
            )
        if temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED and source_key in HISTORICAL_CLOSED_DAY_SOURCE_KEYS:
            return self._capture_temporal_source_with_acceptance(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                loader=loader,
                execution_mode=execution_mode,
                accepted_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
                allow_persisted_retry=_execution_mode_allows_persisted_retry(execution_mode),
                current_web_source_sync_note=None,
            )
        if temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT and source_key in ACCEPTED_CURRENT_SOURCE_KEYS:
            return self._capture_temporal_source_with_acceptance(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                loader=loader,
                execution_mode=execution_mode,
                accepted_role=TEMPORAL_ROLE_ACCEPTED_CURRENT,
                allow_persisted_retry=(
                    source_key in CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS
                    and _execution_mode_allows_persisted_retry(execution_mode)
                ),
                current_web_source_sync_note=current_web_source_sync_note,
            )
        return _capture_live_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            loader=loader,
        )

    def _capture_promo_closed_day_from_cache(
        self,
        *,
        source_key: str,
        temporal_slot: str,
        temporal_policy: str,
        column_date: str,
        requested_nm_ids: list[int],
    ) -> tuple[LiveSourceStatus, Any | None]:
        replay_status, replay_payload = _capture_live_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            loader=lambda: self.promo_live_source_block.execute(
                PromoLiveSourceRequest(
                    snapshot_date=column_date,
                    nm_ids=requested_nm_ids,
                )
            ).result,
        )
        if replay_payload is not None and _is_exact_snapshot_payload(replay_payload, column_date):
            accepted_at = _format_runtime_timestamp(self.now_factory())
            self.runtime.save_temporal_source_snapshot(
                source_key=source_key,
                snapshot_date=column_date,
                captured_at=accepted_at,
                payload=replay_payload,
            )
            self.runtime.save_temporal_source_slot_snapshot(
                source_key=source_key,
                snapshot_date=column_date,
                snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
                captured_at=accepted_at,
                payload=replay_payload,
            )
            return (
                _append_status_note(
                    replay_status,
                    f"resolution_rule=accepted_closed_from_interval_replay; accepted_at={accepted_at}",
                ),
                replay_payload,
            )

        accepted_snapshot = self._load_slot_snapshot_status(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
            require_closed_day_fresh=True,
        )
        if accepted_snapshot is not None:
            accepted_status, accepted_payload, accepted_at = accepted_snapshot
            note = "resolution_rule=accepted_closed_runtime_snapshot"
            if accepted_at:
                note = f"{note}; accepted_at={accepted_at}"
            return _append_status_note(accepted_status, note), accepted_payload

        cached_snapshot = self._load_cached_temporal_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            runtime_cache_note="resolution_rule=accepted_prior_current_runtime_cache",
            require_closed_day_fresh=True,
        )
        if cached_snapshot is not None:
            cached_status, cached_payload = cached_snapshot
            accepted_at = _format_runtime_timestamp(self.now_factory())
            self.runtime.save_temporal_source_slot_snapshot(
                source_key=source_key,
                snapshot_date=column_date,
                snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
                captured_at=accepted_at,
                payload=cached_payload,
            )
            return (
                _append_status_note(
                    cached_status,
                    f"resolution_rule=accepted_closed_from_prior_current_cache; accepted_at={accepted_at}",
                ),
                cached_payload,
            )

        return replay_status, replay_payload

    def _capture_current_snapshot_closed_day_from_accepted_current(
        self,
        *,
        source_key: str,
        temporal_slot: str,
        temporal_policy: str,
        column_date: str,
        requested_nm_ids: list[int],
        loader: Callable[[], Any] | None = None,
    ) -> tuple[LiveSourceStatus, Any | None]:
        accepted_snapshot = self._load_slot_snapshot_status(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CURRENT,
        )
        if accepted_snapshot is not None:
            accepted_status, accepted_payload, accepted_at = accepted_snapshot
            note = "resolution_rule=accepted_closed_from_prior_current_snapshot"
            if accepted_at:
                note = f"{note}; accepted_at={accepted_at}"
            return _append_status_note(accepted_status, note), accepted_payload

        return (
            LiveSourceStatus(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                kind="missing",
                freshness="",
                snapshot_date="",
                date="",
                date_from="",
                date_to="",
                requested_count=len(requested_nm_ids),
                covered_count=0,
                missing_nm_ids=sorted(set(requested_nm_ids)),
                note=(
                    "current-snapshot-only yesterday_closed requires a prior accepted current snapshot "
                    "for requested date; endpoint has no historical date parameter, so current values "
                    "are not backfilled into a closed-day column"
                ),
            ),
            None,
        )

    def _capture_temporal_source_with_acceptance(
        self,
        *,
        source_key: str,
        temporal_slot: str,
        temporal_policy: str,
        column_date: str,
        requested_nm_ids: list[int],
        loader: Callable[[], Any],
        execution_mode: str,
        accepted_role: str,
        allow_persisted_retry: bool,
        current_web_source_sync_note: str | None,
    ) -> tuple[LiveSourceStatus, Any | None]:
        now = self.now_factory()
        now_iso = _format_runtime_timestamp(now)
        closure_state = self.runtime.load_temporal_source_closure_state(
            source_key=source_key,
            target_date=column_date,
            slot_kind=temporal_slot,
        )
        accepted_snapshot = self._load_slot_snapshot_status(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            snapshot_role=accepted_role,
            require_closed_day_fresh=(
                temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED
                and accepted_role == TEMPORAL_ROLE_ACCEPTED_CLOSED
            ),
        )
        if accepted_snapshot is None and source_key in STRICT_CLOSED_DAY_SOURCE_KEYS and temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT:
            accepted_snapshot = self._load_slot_snapshot_status(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                snapshot_role=TEMPORAL_ROLE_PROVISIONAL_CURRENT,
            )
        if accepted_snapshot is None and temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED and source_key in EXACT_DATE_RUNTIME_CACHE_SOURCE_KEYS:
            cached_snapshot = self._load_cached_temporal_source(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                runtime_cache_note=_runtime_cache_note(source_key),
                require_closed_day_fresh=True,
            )
            if cached_snapshot is not None:
                cached_status, cached_payload = cached_snapshot
                self.runtime.save_temporal_source_slot_snapshot(
                    source_key=source_key,
                    snapshot_date=column_date,
                    snapshot_role=accepted_role,
                    captured_at=now_iso,
                    payload=cached_payload,
                )
                if _source_slot_supports_persisted_retry(source_key=source_key, temporal_slot=temporal_slot):
                    self.runtime.save_temporal_source_closure_state(
                        source_key=source_key,
                        target_date=column_date,
                        slot_kind=temporal_slot,
                        state=CLOSURE_STATE_SUCCESS,
                        attempt_count=closure_state.attempt_count if closure_state is not None else 0,
                        next_retry_at=None,
                        last_reason="accepted_from_runtime_cache",
                        last_attempt_at=now_iso,
                        last_success_at=now_iso,
                        accepted_at=now_iso,
                    )
                return (
                    _append_status_note(
                        cached_status,
                        f"{_accepted_resolution_note(temporal_slot)}; accepted_at={now_iso}",
                    ),
                    cached_payload,
                )

        sync_error: str | None = None
        if source_key in STRICT_CLOSED_DAY_SOURCE_KEYS and temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
            try:
                self.closed_day_web_source_sync.ensure_closed_day_snapshot(
                    source_key=source_key,
                    snapshot_date=column_date,
                )
            except Exception as exc:  # pragma: no cover - live sync fallback
                sync_error = str(exc)

        status, payload = _capture_live_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            loader=loader,
        )
        if current_web_source_sync_note:
            status = _append_current_web_source_sync_note(status, current_web_source_sync_note)
        if sync_error:
            status = _append_status_note(status, f"closed_day_sync_error={sync_error}")

        if payload is not None and _is_exact_snapshot_payload(payload, column_date):
            if source_key in EXACT_DATE_RUNTIME_CACHE_SOURCE_KEYS:
                self.runtime.save_temporal_source_snapshot(
                    source_key=source_key,
                    snapshot_date=column_date,
                    captured_at=now_iso,
                    payload=payload,
                )
            if source_key in STRICT_CLOSED_DAY_SOURCE_KEYS and temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT:
                self.runtime.save_temporal_source_slot_snapshot(
                    source_key=source_key,
                    snapshot_date=column_date,
                    snapshot_role=TEMPORAL_ROLE_PROVISIONAL_CURRENT,
                    captured_at=now_iso,
                    payload=payload,
                )
            if source_key in STRICT_CLOSED_DAY_SOURCE_KEYS and temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
                self.runtime.save_temporal_source_slot_snapshot(
                    source_key=source_key,
                    snapshot_date=column_date,
                    snapshot_role=TEMPORAL_ROLE_CLOSED_DAY_CANDIDATE,
                    captured_at=now_iso,
                    payload=payload,
                )
            if source_key == ONEC_STOCKS_SOURCE_KEY:
                payload, status = self._preserve_onec_missing_stage_buckets(
                    status=status,
                    payload=payload,
                    temporal_slot=temporal_slot,
                    temporal_policy=temporal_policy,
                    column_date=column_date,
                    requested_nm_ids=requested_nm_ids,
                    accepted_snapshot=accepted_snapshot,
                    accepted_role=accepted_role,
                )

        candidate_valid = _is_valid_temporal_candidate(
            source_key=source_key,
            status=status,
            payload=payload,
            column_date=column_date,
            temporal_slot=temporal_slot,
        )
        if payload is not None and _is_exact_snapshot_payload(payload, column_date) and not candidate_valid:
            status = _coerce_invalid_temporal_candidate_status(
                status=status,
                requested_nm_ids=requested_nm_ids,
                note_suffix=_invalid_temporal_candidate_note(source_key, temporal_slot),
            )

        if candidate_valid:
            self.runtime.save_temporal_source_slot_snapshot(
                source_key=source_key,
                snapshot_date=column_date,
                snapshot_role=accepted_role,
                captured_at=now_iso,
                payload=payload,
            )
            if _source_slot_supports_persisted_retry(source_key=source_key, temporal_slot=temporal_slot):
                self.runtime.save_temporal_source_closure_state(
                    source_key=source_key,
                    target_date=column_date,
                    slot_kind=temporal_slot,
                    state=CLOSURE_STATE_SUCCESS,
                    attempt_count=(closure_state.attempt_count if closure_state is not None else 0) + 1,
                    next_retry_at=None,
                    last_reason=_accepted_resolution_note(temporal_slot),
                    last_attempt_at=now_iso,
                    last_success_at=now_iso,
                    accepted_at=now_iso,
                )
            return (
                _append_status_note(
                    status,
                    f"{_accepted_resolution_note(temporal_slot)}; accepted_at={now_iso}",
                ),
                payload,
            )

        if accepted_snapshot is None and source_key in STRICT_CLOSED_DAY_SOURCE_KEYS and temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
            prior_current_snapshot = self._load_slot_snapshot_status(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                snapshot_role=TEMPORAL_ROLE_ACCEPTED_CURRENT,
            )
            if prior_current_snapshot is not None:
                prior_status, prior_payload, prior_accepted_at = prior_current_snapshot
                note_parts = ["resolution_rule=accepted_current_from_prior_closed_day_latest_confirmed"]
                if prior_accepted_at:
                    note_parts.append(f"accepted_at={prior_accepted_at}")
                note_parts.append(f"latest_attempt_kind={status.kind}")
                if status.note:
                    note_parts.append(f"latest_attempt_note={status.note}")
                if allow_persisted_retry and _source_slot_supports_persisted_retry(
                    source_key=source_key,
                    temporal_slot=temporal_slot,
                ):
                    reason = status.note or sync_error or _invalid_temporal_candidate_note(source_key, temporal_slot)
                    next_retry_at, retry_state = _next_closure_retry(
                        now,
                        (closure_state.attempt_count if closure_state is not None else 0) + 1,
                        reason,
                    )
                    self.runtime.save_temporal_source_closure_state(
                        source_key=source_key,
                        target_date=column_date,
                        slot_kind=temporal_slot,
                        state=retry_state,
                        attempt_count=(closure_state.attempt_count if closure_state is not None else 0) + 1,
                        next_retry_at=next_retry_at,
                        last_reason=reason,
                        last_attempt_at=now_iso,
                        last_success_at=prior_accepted_at,
                        accepted_at=None,
                    )
                    note_parts.append(f"closure_state={retry_state}")
                return _append_status_note(prior_status, "; ".join(note_parts)), prior_payload

        cached_snapshot = self._load_cached_temporal_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            runtime_cache_note=_runtime_cache_note(source_key),
            require_closed_day_fresh=(
                temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED
                and source_key in EXACT_DATE_RUNTIME_CACHE_SOURCE_KEYS
            ),
        )
        if accepted_snapshot is None and cached_snapshot is not None:
            accepted_snapshot = (cached_snapshot[0], cached_snapshot[1], None)

        if accepted_snapshot is not None:
            accepted_status, accepted_payload, accepted_at = accepted_snapshot
            preserved_at = accepted_at or now_iso
            self.runtime.save_temporal_source_slot_snapshot(
                source_key=source_key,
                snapshot_date=column_date,
                snapshot_role=accepted_role,
                captured_at=preserved_at,
                payload=accepted_payload,
            )
            if _source_slot_supports_persisted_retry(source_key=source_key, temporal_slot=temporal_slot):
                self.runtime.save_temporal_source_closure_state(
                    source_key=source_key,
                    target_date=column_date,
                    slot_kind=temporal_slot,
                    state=CLOSURE_STATE_SUCCESS,
                    attempt_count=closure_state.attempt_count if closure_state is not None else 0,
                    next_retry_at=None,
                    last_reason="accepted_snapshot_preserved_after_invalid_attempt",
                    last_attempt_at=now_iso,
                    last_success_at=preserved_at,
                    accepted_at=preserved_at,
                )
            return (
                _build_preserved_accepted_status(
                    accepted_status=accepted_status,
                    accepted_at=preserved_at,
                    latest_status=status,
                    temporal_slot=temporal_slot,
                ),
                accepted_payload,
            )

        if allow_persisted_retry and _source_slot_supports_persisted_retry(
            source_key=source_key,
            temporal_slot=temporal_slot,
        ):
            reason = status.note or sync_error or _invalid_temporal_candidate_note(source_key, temporal_slot)
            next_retry_at, retry_state = _next_closure_retry(now, (closure_state.attempt_count if closure_state is not None else 0) + 1, reason)
            self.runtime.save_temporal_source_closure_state(
                source_key=source_key,
                target_date=column_date,
                slot_kind=temporal_slot,
                state=retry_state,
                attempt_count=(closure_state.attempt_count if closure_state is not None else 0) + 1,
                next_retry_at=next_retry_at,
                last_reason=reason,
                last_attempt_at=now_iso,
                last_success_at=closure_state.last_success_at if closure_state is not None else None,
                accepted_at=closure_state.accepted_at if closure_state is not None else None,
            )
            return _build_closure_retry_status(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                closure_state=TemporalSourceClosureState(
                    source_key=source_key,
                    target_date=column_date,
                    slot_kind=temporal_slot,
                    state=retry_state,
                    attempt_count=(closure_state.attempt_count if closure_state is not None else 0) + 1,
                    next_retry_at=next_retry_at,
                    last_reason=reason,
                    last_attempt_at=now_iso,
                    last_success_at=closure_state.last_success_at if closure_state is not None else None,
                    accepted_at=closure_state.accepted_at if closure_state is not None else None,
                ),
            ), None

        return status, None

    def _load_slot_snapshot_status(
        self,
        *,
        source_key: str,
        temporal_slot: str,
        temporal_policy: str,
        column_date: str,
        requested_nm_ids: list[int],
        snapshot_role: str,
        require_closed_day_fresh: bool = False,
    ) -> tuple[LiveSourceStatus, Any, str | None] | None:
        cached_payload, cached_at = self.runtime.load_temporal_source_slot_snapshot(
            source_key=source_key,
            snapshot_date=column_date,
            snapshot_role=snapshot_role,
        )
        if cached_payload is None or not _is_exact_snapshot_payload(cached_payload, column_date):
            return None
        if require_closed_day_fresh and not _closed_day_capture_is_fresh(
            captured_at=cached_at,
            snapshot_date=column_date,
        ):
            return None
        cached_status, _ = _capture_live_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            loader=lambda: cached_payload,
        )
        return cached_status, cached_payload, cached_at

    def _preserve_onec_missing_stage_buckets(
        self,
        *,
        status: LiveSourceStatus,
        payload: Any | None,
        temporal_slot: str,
        temporal_policy: str,
        column_date: str,
        requested_nm_ids: list[int],
        accepted_snapshot: tuple[LiveSourceStatus, Any, str | None] | None,
        accepted_role: str,
    ) -> tuple[Any | None, LiveSourceStatus]:
        if status.source_key != ONEC_STOCKS_SOURCE_KEY or payload is None:
            return payload, status
        missing_buckets = _missing_onec_stage_buckets_from_status(status)
        if not missing_buckets:
            return payload, status

        fallback_candidates: list[tuple[str, LiveSourceStatus, Any, str | None]] = []
        if accepted_snapshot is not None:
            fallback_status, fallback_payload, fallback_at = accepted_snapshot
            fallback_candidates.append((accepted_role, fallback_status, fallback_payload, fallback_at))
        if temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
            accepted_current = self._load_slot_snapshot_status(
                source_key=ONEC_STOCKS_SOURCE_KEY,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                snapshot_role=TEMPORAL_ROLE_ACCEPTED_CURRENT,
            )
            if accepted_current is not None:
                fallback_status, fallback_payload, fallback_at = accepted_current
                fallback_candidates.append(
                    (TEMPORAL_ROLE_ACCEPTED_CURRENT, fallback_status, fallback_payload, fallback_at)
                )

        for fallback_role, _fallback_status, fallback_payload, fallback_at in fallback_candidates:
            merged_payload, preserved_buckets = _merge_onec_missing_stage_buckets_from_payload(
                payload=payload,
                fallback_payload=fallback_payload,
                missing_stage_buckets=missing_buckets,
            )
            if merged_payload is None or not preserved_buckets:
                continue
            diagnostics = dict(status.diagnostics or {})
            diagnostics["onec_stage_bucket_fallback"] = {
                "role": fallback_role,
                "captured_at": fallback_at or "",
                "stage_buckets": preserved_buckets,
                "source": "server_side_accepted_snapshot",
            }
            note = _format_note(
                {
                    "accepted_fallback_stage_buckets": ",".join(preserved_buckets),
                    "accepted_fallback_role": fallback_role,
                    "accepted_fallback_captured_at": fallback_at or "",
                    "missing_stage_bucket_rows": "filled_from_server_side_accepted_snapshot",
                }
            )
            return merged_payload, replace(
                status,
                note=_append_invalid_payload_note(status.note, note),
                diagnostics=diagnostics,
            )
        return payload, status

    def _capture_cached_temporal_source(
        self,
        *,
        source_key: str,
        temporal_slot: str,
        temporal_policy: str,
        column_date: str,
        requested_nm_ids: list[int],
        loader: Callable[[], Any],
        runtime_cache_note: str = "resolution_rule=exact_date_runtime_cache",
        live_fetch_note: str | None = None,
        prefer_cached_first: bool = False,
        require_closed_day_fresh: bool = False,
    ) -> tuple[LiveSourceStatus, Any | None]:
        if prefer_cached_first:
            cached_result = self._load_cached_temporal_source(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                runtime_cache_note=runtime_cache_note,
                require_closed_day_fresh=require_closed_day_fresh,
            )
            if cached_result is not None:
                return cached_result
        status, payload = _capture_live_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            loader=loader,
        )
        if payload is not None and _is_exact_snapshot_payload(payload, column_date):
            self.runtime.save_temporal_source_snapshot(
                source_key=source_key,
                snapshot_date=column_date,
                captured_at=_format_runtime_timestamp(self.now_factory()),
                payload=payload,
            )
            if live_fetch_note:
                return _append_status_note(status, live_fetch_note), payload
            return status, payload

        if status.kind != "not_found":
            return status, payload

        cached_result = self._load_cached_temporal_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            runtime_cache_note=runtime_cache_note,
            require_closed_day_fresh=require_closed_day_fresh,
        )
        if cached_result is not None:
            return cached_result
        return status, payload

    def _load_cached_temporal_source(
        self,
        *,
        source_key: str,
        temporal_slot: str,
        temporal_policy: str,
        column_date: str,
        requested_nm_ids: list[int],
        runtime_cache_note: str,
        require_closed_day_fresh: bool = False,
    ) -> tuple[LiveSourceStatus, Any | None] | None:
        cached_payload, cached_at = self.runtime.load_temporal_source_snapshot(
            source_key=source_key,
            snapshot_date=column_date,
        )
        if cached_payload is None or not _is_exact_snapshot_payload(cached_payload, column_date):
            return None
        if require_closed_day_fresh and not _closed_day_capture_is_fresh(
            captured_at=cached_at,
            snapshot_date=column_date,
        ):
            return None
        cached_status, _ = _capture_live_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            loader=lambda: cached_payload,
        )
        cache_note = runtime_cache_note
        if cached_at:
            cache_note = f"{cache_note}; cache_captured_at={cached_at}"
        return _append_status_note(cached_status, cache_note), cached_payload

    def _capture_provisional_current_web_source(
        self,
        *,
        source_key: str,
        temporal_slot: str,
        temporal_policy: str,
        column_date: str,
        requested_nm_ids: list[int],
        loader: Callable[[], Any],
        current_web_source_sync_note: str | None = None,
    ) -> tuple[LiveSourceStatus, Any | None]:
        status, payload = _capture_live_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            loader=loader,
        )
        if _is_invalid_temporal_web_source_payload(
            source_key=source_key,
            payload=payload,
            column_date=column_date,
            closed_day_required=False,
        ):
            return (
                _append_current_web_source_sync_note(
                    _build_invalid_temporal_web_source_status(
                        source_key=source_key,
                        temporal_slot=temporal_slot,
                        temporal_policy=temporal_policy,
                        column_date=column_date,
                        requested_nm_ids=requested_nm_ids,
                        payload=payload,
                        note_suffix=_invalid_temporal_web_source_note(source_key),
                    ),
                    current_web_source_sync_note,
                ),
                None,
            )
        if payload is not None and _is_exact_snapshot_payload(payload, column_date):
            self.runtime.save_temporal_source_slot_snapshot(
                source_key=source_key,
                snapshot_date=column_date,
                snapshot_role=TEMPORAL_ROLE_PROVISIONAL_CURRENT,
                captured_at=_format_runtime_timestamp(self.now_factory()),
                payload=payload,
            )
            return _append_current_web_source_sync_note(status, current_web_source_sync_note), payload

        if status.kind != "not_found":
            return _append_current_web_source_sync_note(status, current_web_source_sync_note), payload

        cached_payload, cached_at = self.runtime.load_temporal_source_slot_snapshot(
            source_key=source_key,
            snapshot_date=column_date,
            snapshot_role=TEMPORAL_ROLE_PROVISIONAL_CURRENT,
        )
        if cached_payload is None or not _is_exact_snapshot_payload(cached_payload, column_date):
            return _append_current_web_source_sync_note(status, current_web_source_sync_note), payload

        cached_status, _ = _capture_live_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            loader=lambda: cached_payload,
        )
        cache_note = "resolution_rule=exact_date_provisional_runtime_cache"
        if cached_at:
            cache_note = f"{cache_note}; cache_captured_at={cached_at}"
        return (
            _append_current_web_source_sync_note(
                _append_status_note(cached_status, cache_note),
                current_web_source_sync_note,
            ),
            cached_payload,
        )

    def _capture_closed_day_web_source(
        self,
        *,
        source_key: str,
        temporal_slot: str,
        temporal_policy: str,
        column_date: str,
        requested_nm_ids: list[int],
        loader: Callable[[], Any],
    ) -> tuple[LiveSourceStatus, Any | None]:
        accepted_payload, accepted_at = self.runtime.load_temporal_source_slot_snapshot(
            source_key=source_key,
            snapshot_date=column_date,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        )
        if (
            accepted_payload is not None
            and _is_exact_snapshot_payload(accepted_payload, column_date)
            and _closed_day_capture_is_fresh(captured_at=accepted_at, snapshot_date=column_date)
        ):
            accepted_status, _ = _capture_live_source(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                loader=lambda: accepted_payload,
            )
            cache_note = "resolution_rule=accepted_closed_runtime_snapshot"
            if accepted_at:
                cache_note = f"{cache_note}; accepted_at={accepted_at}"
            return _append_status_note(accepted_status, cache_note), accepted_payload

        closure_state = self.runtime.load_temporal_source_closure_state(
            source_key=source_key,
            target_date=column_date,
            slot_kind=temporal_slot,
        )
        now = self.now_factory()
        now_iso = _format_runtime_timestamp(now)
        if not _closure_attempt_is_due(closure_state, now):
            return _build_closure_retry_status(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                closure_state=closure_state,
            ), None

        attempt_count = (closure_state.attempt_count if closure_state is not None else 0) + 1
        attempt_error: str | None = None
        try:
            self.closed_day_web_source_sync.ensure_closed_day_snapshot(
                source_key=source_key,
                snapshot_date=column_date,
            )
        except Exception as exc:  # pragma: no cover - live sync fallback
            attempt_error = str(exc)

        status, payload = _capture_live_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            loader=loader,
        )
        if payload is not None and _is_exact_snapshot_payload(payload, column_date):
            self.runtime.save_temporal_source_slot_snapshot(
                source_key=source_key,
                snapshot_date=column_date,
                snapshot_role=TEMPORAL_ROLE_CLOSED_DAY_CANDIDATE,
                captured_at=now_iso,
                payload=payload,
            )

        if attempt_error is None and not _is_invalid_temporal_web_source_payload(
            source_key=source_key,
            payload=payload,
            column_date=column_date,
            closed_day_required=True,
        ) and payload is not None and _is_exact_snapshot_payload(payload, column_date):
            self.runtime.save_temporal_source_slot_snapshot(
                source_key=source_key,
                snapshot_date=column_date,
                snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
                captured_at=now_iso,
                payload=payload,
            )
            self.runtime.save_temporal_source_closure_state(
                source_key=source_key,
                target_date=column_date,
                slot_kind=temporal_slot,
                state=CLOSURE_STATE_SUCCESS,
                attempt_count=attempt_count,
                next_retry_at=None,
                last_reason="accepted_closed_day_snapshot",
                last_attempt_at=now_iso,
                last_success_at=now_iso,
                accepted_at=now_iso,
            )
            accepted_status, _ = _capture_live_source(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                loader=lambda: payload,
            )
            return _append_status_note(
                accepted_status,
                f"resolution_rule=accepted_closed_current_attempt; accepted_at={now_iso}",
            ), payload

        reason = attempt_error or status.note or _invalid_temporal_web_source_note(source_key)
        next_retry_at, retry_state = _next_closure_retry(now, attempt_count, reason)
        self.runtime.save_temporal_source_closure_state(
            source_key=source_key,
            target_date=column_date,
            slot_kind=temporal_slot,
            state=retry_state,
            attempt_count=attempt_count,
            next_retry_at=next_retry_at,
            last_reason=reason,
            last_attempt_at=now_iso,
            last_success_at=closure_state.last_success_at if closure_state is not None else None,
            accepted_at=closure_state.accepted_at if closure_state is not None else None,
        )
        return _build_closure_retry_status(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            closure_state=TemporalSourceClosureState(
                source_key=source_key,
                target_date=column_date,
                slot_kind=temporal_slot,
                state=retry_state,
                attempt_count=attempt_count,
                next_retry_at=next_retry_at,
                last_reason=reason,
                last_attempt_at=now_iso,
                last_success_at=closure_state.last_success_at if closure_state is not None else None,
                accepted_at=closure_state.accepted_at if closure_state is not None else None,
            ),
        ), None


class _MetricEvaluator:
    def __init__(
        self,
        *,
        enabled_config: list[ConfigV2Item],
        metrics_by_key: Mapping[str, MetricV2Item],
        formulas_by_id: Mapping[str, FormulaV2Item],
        live_sources: TemporalLiveSources,
        proxy_parameters_resolver: Callable[[str], ProxyParameters] | None = None,
        proxy_v4_parameters_resolver: Callable[[str], ProxyV4Parameters | None] | None = None,
    ) -> None:
        self.enabled_config = enabled_config
        self.metrics_by_key = metrics_by_key
        self.formulas_by_id = formulas_by_id
        self.live_sources = live_sources
        self.proxy_parameters_resolver = proxy_parameters_resolver or (lambda _date: DEFAULT_PROXY_PARAMETERS)
        self.proxy_v4_parameters_resolver = proxy_v4_parameters_resolver or (lambda _date: None)
        self.grouped_config = _group_config(enabled_config)
        self.config_by_nm_id = {item.nm_id: item for item in enabled_config}
        self.sku_cache: dict[tuple[str, int, str], float | None] = {}
        self.total_cache: dict[tuple[str, str], float | None] = {}
        self.group_cache: dict[tuple[str, str, str], float | None] = {}

    def resolve_sku(self, metric_key: str, nm_id: int, temporal_slot: str) -> float | None:
        cache_key = (temporal_slot, nm_id, metric_key)
        if cache_key in self.sku_cache:
            return self.sku_cache[cache_key]

        metric = self.metrics_by_key.get(metric_key)
        if metric is None:
            raise ValueError(f"metric_key missing in current registry: {metric_key}")

        if metric.calc_type == "metric":
            if metric.calc_ref != metric.metric_key:
                value = self.resolve_sku(metric.calc_ref, nm_id, temporal_slot)
            else:
                value = self._resolve_direct_sku(metric.metric_key, nm_id, temporal_slot)
        elif metric.calc_type == "ratio":
            numerator_key, denominator_key = _split_ratio(metric.calc_ref)
            numerator = self.resolve_sku(numerator_key, nm_id, temporal_slot)
            denominator = self.resolve_sku(denominator_key, nm_id, temporal_slot)
            value = None if numerator is None or denominator in (None, 0) else float(numerator) / float(denominator)
        elif metric.calc_type == "formula":
            formula = self.formulas_by_id.get(metric.calc_ref)
            if formula is None:
                raise ValueError(f"formula missing for metric {metric_key}")
            value = _evaluate_formula(
                formula.expression,
                lambda dependency: self.resolve_sku(dependency, nm_id, temporal_slot),
            )
        else:
            raise ValueError(f"unsupported calc_type: {metric.calc_type}")

        self.sku_cache[cache_key] = value
        return value

    def resolve_total(self, metric_key: str, temporal_slot: str) -> float | None:
        cache_key = (temporal_slot, metric_key)
        if cache_key in self.total_cache:
            return self.total_cache[cache_key]

        metric = self.metrics_by_key.get(metric_key)
        if metric is None:
            raise ValueError(f"metric_key missing in current registry: {metric_key}")

        if metric.calc_type == "metric":
            if metric.metric_key == "fin_storage_fee_total":
                value = self._slot_lookups(temporal_slot).fin_storage_fee_total
            elif metric.metric_key == ONEC_TOTAL_PROXY_PROFIT_2_RUB_METRIC_KEY:
                value = self._aggregate_sum(
                    ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY,
                    self.enabled_config,
                    temporal_slot,
                )
            elif metric.metric_key == OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY:
                value = self._aggregate_complete_sum(
                    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
                    self.enabled_config,
                    temporal_slot,
                )
            elif metric.metric_key == OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY:
                parameters = self._proxy_parameters(temporal_slot)
                total_orders = self._aggregate_complete_sum(
                    "orderSum", self.enabled_config, temporal_slot
                )
                expected_revenue = (
                    None
                    if total_orders is None
                    else float(total_orders) * float(parameters.buyout_rate)
                )
                value = _divide_or_none(
                    self.resolve_total(
                        OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
                        temporal_slot,
                    ),
                    expected_revenue,
                )
            elif metric.metric_key in {
                PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
                PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
            }:
                aggregate = self._aggregate_proxy_v4(
                    self.enabled_config,
                    temporal_slot,
                )
                value = (
                    aggregate["proxy_profit_4"]
                    if metric.metric_key == PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY
                    else aggregate["proxy_margin_4"]
                )
            elif metric.metric_key == TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY:
                value = self._aggregate_our_wb_unit_cost(temporal_slot)
            elif metric.metric_key == TOTAL_OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY:
                value = self._aggregate_our_wb_confirmed_share(temporal_slot)
            elif metric.metric_key == OWN_TOTAL_QTY_TOTAL_METRIC_KEY:
                value = self._aggregate_complete_sum(
                    OWN_TOTAL_QTY_METRIC_KEY,
                    self.enabled_config,
                    temporal_slot,
                )
            elif metric.metric_key == OWN_TOTAL_PAID_EQUIVALENT_QTY_TOTAL_METRIC_KEY:
                value = self._aggregate_sum(
                    OWN_TOTAL_PAID_EQUIVALENT_QTY_METRIC_KEY,
                    self.enabled_config,
                    temporal_slot,
                )
            elif metric.metric_key == OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY:
                value = self._aggregate_complete_sum(
                    OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
                    self.enabled_config,
                    temporal_slot,
                )
            elif metric.metric_key == OWN_AVG_COST_RUB_TOTAL_METRIC_KEY:
                value = _divide_or_none(
                    self.resolve_total(OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY, temporal_slot),
                    self.resolve_total(OWN_TOTAL_QTY_TOTAL_METRIC_KEY, temporal_slot),
                )
            elif metric.metric_key == OWN_TOTAL_CONFIRMED_SHARE_PCT_TOTAL_METRIC_KEY:
                value = self._aggregate_own_product_capital_confirmed_share(temporal_slot)
            elif metric.metric_key == OWN_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY:
                value = _divide_or_none(
                    self.resolve_total(OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY, temporal_slot),
                    self.resolve_total(OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY, temporal_slot),
                )
            elif metric.metric_key == OWN_UNDERACCEPTED_WB_QTY_TOTAL_METRIC_KEY:
                value = self._aggregate_sum(
                    OWN_UNDERACCEPTED_WB_QTY_METRIC_KEY,
                    self.enabled_config,
                    temporal_slot,
                )
            elif metric.metric_key == OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_TOTAL_METRIC_KEY:
                value = self._aggregate_weighted_avg(
                    OWN_UNDERACCEPTED_WB_UNIT_COST_RUB_METRIC_KEY,
                    OWN_UNDERACCEPTED_WB_QTY_METRIC_KEY,
                    self.enabled_config,
                    temporal_slot,
                )
            elif metric.metric_key in {
                own_stage_total_metric_key(stage, field)
                for stage in OWN_PRODUCT_CAPITAL_STAGES
                for field in ("qty", "capital_rub")
            }:
                stage, field = next(
                    (stage, field)
                    for stage in OWN_PRODUCT_CAPITAL_STAGES
                    for field in ("qty", "capital_rub")
                    if metric.metric_key == own_stage_total_metric_key(stage, field)
                )
                value = self._aggregate_complete_sum(
                    own_stage_metric_key(stage, field),
                    self.enabled_config,
                    temporal_slot,
                )
            elif metric.metric_key in {
                own_stage_total_metric_key(stage, "unit_cost_rub")
                for stage in OWN_PRODUCT_CAPITAL_STAGES
            }:
                stage = next(
                    item for item in OWN_PRODUCT_CAPITAL_STAGES
                    if metric.metric_key == own_stage_total_metric_key(item, "unit_cost_rub")
                )
                value = _divide_or_none(
                    self._aggregate_complete_sum(
                        own_stage_metric_key(stage, "capital_rub"),
                        self.enabled_config,
                        temporal_slot,
                    ),
                    self._aggregate_complete_sum(
                        own_stage_metric_key(stage, "qty"),
                        self.enabled_config,
                        temporal_slot,
                    ),
                )
            elif metric.metric_key in {
                own_stage_total_metric_key(stage, "confirmed_share_pct")
                for stage in OWN_PRODUCT_CAPITAL_STAGES
            }:
                stage = next(
                    item for item in OWN_PRODUCT_CAPITAL_STAGES
                    if metric.metric_key == own_stage_total_metric_key(item, "confirmed_share_pct")
                )
                value = self._aggregate_own_stage_confirmed_share(stage, temporal_slot)
            elif metric.metric_key in {
                own_stage_total_metric_key(stage, "cost_coverage_pct")
                for stage in OWN_PRODUCT_CAPITAL_STAGES
            }:
                stage = next(
                    item for item in OWN_PRODUCT_CAPITAL_STAGES
                    if metric.metric_key == own_stage_total_metric_key(item, "cost_coverage_pct")
                )
                value = self._aggregate_own_stage_cost_coverage(stage, temporal_slot)
            elif metric.metric_key == ONEC_PROXY_MARGIN_2_PCT_TOTAL_METRIC_KEY:
                value = _divide_or_zero(
                    self.resolve_total(ONEC_TOTAL_PROXY_PROFIT_2_RUB_METRIC_KEY, temporal_slot),
                    self.resolve_total("total_orderSum", temporal_slot),
                )
            elif metric.metric_key == ONEC_INVENTORY_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY:
                value = _divide_or_zero(
                    self.resolve_total(ONEC_TOTAL_PROXY_PROFIT_2_RUB_METRIC_KEY, temporal_slot),
                    self.resolve_total(ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY, temporal_slot),
                )
            elif onec_weighted_unit_cost_components(metric.metric_key) is not None:
                value = self._aggregate_onec_weighted_unit_cost(metric.metric_key, temporal_slot)
            elif metric.metric_key == SEARCH_CTR_AVG_TOTAL_METRIC_KEY:
                value = self._aggregate_weighted_avg(
                    SEARCH_CTR_SKU_METRIC_KEY,
                    SEARCH_VIEWS_SKU_METRIC_KEY,
                    self.enabled_config,
                    temporal_slot,
                )
            elif metric.metric_key.startswith(AGGREGATE_SUM_PREFIX):
                value = self._aggregate_sum(metric.calc_ref, self.enabled_config, temporal_slot)
            elif metric.metric_key.startswith(AGGREGATE_AVG_PREFIX):
                value = self._aggregate_avg(metric.calc_ref, self.enabled_config, temporal_slot)
            elif metric.calc_ref != metric.metric_key:
                value = self._aggregate_sum(metric.calc_ref, self.enabled_config, temporal_slot)
            else:
                value = self._resolve_total_direct(metric.metric_key, temporal_slot)
        elif metric.calc_type == "ratio":
            numerator_key, denominator_key = _split_ratio(metric.calc_ref)
            numerator = self.resolve_total(numerator_key, temporal_slot)
            denominator = self.resolve_total(denominator_key, temporal_slot)
            value = None if numerator is None or denominator in (None, 0) else float(numerator) / float(denominator)
        elif metric.calc_type == "formula":
            formula = self.formulas_by_id.get(metric.calc_ref)
            if formula is None:
                raise ValueError(f"formula missing for metric {metric_key}")
            value = _evaluate_formula(
                formula.expression,
                lambda dependency: self.resolve_total(dependency, temporal_slot),
            )
        else:
            raise ValueError(f"unsupported calc_type: {metric.calc_type}")

        self.total_cache[cache_key] = value
        return value

    def resolve_group(self, metric_key: str, group_name: str, temporal_slot: str) -> float | None:
        cache_key = (temporal_slot, group_name, metric_key)
        if cache_key in self.group_cache:
            return self.group_cache[cache_key]

        metric = self.metrics_by_key.get(metric_key)
        if metric is None:
            raise ValueError(f"metric_key missing in current registry: {metric_key}")
        group_items = self.grouped_config.get(group_name, [])
        if metric.calc_type == "metric":
            if metric.metric_key == OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY:
                total_orders = self._aggregate_complete_sum(
                    "orderSum", group_items, temporal_slot
                )
                denominator = (
                    None
                    if total_orders is None
                    else float(total_orders)
                    * float(self._proxy_parameters(temporal_slot).buyout_rate)
                )
                value = _divide_or_none(
                    self._aggregate_complete_sum(
                        OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
                        group_items,
                        temporal_slot,
                    ),
                    denominator,
                )
            elif metric.metric_key == SEARCH_CTR_AVG_TOTAL_METRIC_KEY:
                value = self._aggregate_weighted_avg(
                    SEARCH_CTR_SKU_METRIC_KEY,
                    SEARCH_VIEWS_SKU_METRIC_KEY,
                    group_items,
                    temporal_slot,
                )
            elif metric.metric_key.startswith(AGGREGATE_AVG_PREFIX):
                value = self._aggregate_avg(metric.calc_ref, group_items, temporal_slot)
            else:
                value = self._aggregate_sum(metric.calc_ref, group_items, temporal_slot)
        elif metric.calc_type == "ratio":
            numerator_key, denominator_key = _split_ratio(metric.calc_ref)
            numerator = self._aggregate_sum(numerator_key, group_items, temporal_slot)
            denominator = self._aggregate_sum(denominator_key, group_items, temporal_slot)
            value = None if numerator is None or denominator in (None, 0) else float(numerator) / float(denominator)
        elif metric.calc_type == "formula":
            formula = self.formulas_by_id.get(metric.calc_ref)
            if formula is None:
                raise ValueError(f"formula missing for metric {metric_key}")
            value = _evaluate_formula(
                formula.expression,
                lambda dependency: self._aggregate_sum(dependency, group_items, temporal_slot),
            )
        else:
            raise ValueError(f"unsupported calc_type: {metric.calc_type}")

        self.group_cache[cache_key] = value
        return value

    def _resolve_total_direct(self, metric_key: str, temporal_slot: str) -> float | None:
        if metric_key == "fin_storage_fee_total":
            return self._slot_lookups(temporal_slot).fin_storage_fee_total
        return self._aggregate_sum(metric_key, self.enabled_config, temporal_slot)

    def _aggregate_sum(
        self,
        metric_key: str,
        config_items: Iterable[ConfigV2Item],
        temporal_slot: str,
    ) -> float | None:
        values = [self.resolve_sku(metric_key, item.nm_id, temporal_slot) for item in config_items]
        numeric = [value for value in values if value is not None]
        return float(sum(numeric)) if numeric else None

    def _aggregate_complete_sum(
        self,
        metric_key: str,
        config_items: Iterable[ConfigV2Item],
        temporal_slot: str,
    ) -> float | None:
        values = [self.resolve_sku(metric_key, item.nm_id, temporal_slot) for item in config_items]
        if not values or any(value is None for value in values):
            return None
        return float(sum(value for value in values if value is not None))

    def _aggregate_proxy_v4(
        self,
        config_items: Iterable[ConfigV2Item],
        temporal_slot: str,
    ) -> dict[str, float | None]:
        parameters = self._proxy_v4_parameters(temporal_slot)
        if parameters is None:
            return {
                "proxy_profit_4": None,
                "expected_buyout_revenue": None,
                "proxy_margin_4": None,
            }
        eligible: list[tuple[float, float]] = []
        for item in config_items:
            profit = self.resolve_sku(
                PROXY_V4_PROFIT_RUB_METRIC_KEY,
                item.nm_id,
                temporal_slot,
            )
            order_sum = self.resolve_sku("orderSum", item.nm_id, temporal_slot)
            if profit is None or order_sum is None:
                continue
            eligible.append(
                (float(profit), float(order_sum) * float(parameters.buyout_rate))
            )
        if not eligible:
            return {
                "proxy_profit_4": None,
                "expected_buyout_revenue": None,
                "proxy_margin_4": None,
            }
        profit = sum(item[0] for item in eligible)
        revenue = sum(item[1] for item in eligible)
        return {
            "proxy_profit_4": profit,
            "expected_buyout_revenue": revenue,
            "proxy_margin_4": None if revenue == 0 else profit / revenue,
        }

    def _aggregate_avg(
        self,
        metric_key: str,
        config_items: Iterable[ConfigV2Item],
        temporal_slot: str,
    ) -> float | None:
        values = [self.resolve_sku(metric_key, item.nm_id, temporal_slot) for item in config_items]
        numeric = [value for value in values if value is not None]
        return float(sum(numeric)) / len(numeric) if numeric else None

    def _aggregate_weighted_avg(
        self,
        value_metric_key: str,
        weight_metric_key: str,
        config_items: Iterable[ConfigV2Item],
        temporal_slot: str,
    ) -> float | None:
        weighted_sum = 0.0
        total_weight = 0.0
        numeric_values: list[float] = []
        for item in config_items:
            value = self.resolve_sku(value_metric_key, item.nm_id, temporal_slot)
            weight = self.resolve_sku(weight_metric_key, item.nm_id, temporal_slot)
            if value is None:
                continue
            numeric_values.append(float(value))
            if weight is None or float(weight) <= 0:
                continue
            numeric_weight = float(weight)
            weighted_sum += float(value) * numeric_weight
            total_weight += numeric_weight
        if total_weight > 0:
            return weighted_sum / total_weight
        if numeric_values and all(value == 0.0 for value in numeric_values):
            return 0.0
        return None

    def _aggregate_onec_weighted_unit_cost(self, metric_key: str, temporal_slot: str) -> float | None:
        components = onec_weighted_unit_cost_components(metric_key)
        if components is None:
            return None
        cost_metric_key, qty_metric_key = components
        total_cost = self._aggregate_sum(cost_metric_key, self.enabled_config, temporal_slot)
        total_qty = self._aggregate_sum(qty_metric_key, self.enabled_config, temporal_slot)
        if total_cost is None or total_qty is None:
            return None
        if float(total_qty) == 0.0:
            return 0.0 if float(total_cost) == 0.0 else None
        return float(total_cost) / float(total_qty)

    def _aggregate_our_wb_unit_cost(self, temporal_slot: str) -> float | None:
        lookup = self._slot_lookups(temporal_slot).our_wb_cost_lookup
        weighted_sum = 0.0
        total_stock = 0.0
        for item in self.enabled_config:
            row = lookup.get(item.nm_id)
            if not row:
                return None
            unit_cost = _optional_float(row.get("our_wb_unit_cost_rub"))
            stock_qty = _optional_float(row.get("stock_qty"))
            cost_qty = _optional_float(row.get("cost_covered_qty"))
            weight_qty = cost_qty if cost_qty is not None else stock_qty
            if weight_qty is None:
                return None
            if weight_qty > 0 and unit_cost is None:
                return None
            if unit_cost is None or weight_qty <= 0:
                continue
            weighted_sum += unit_cost * weight_qty
            total_stock += weight_qty
        if total_stock <= 0:
            return None
        return weighted_sum / total_stock

    def _aggregate_our_wb_confirmed_share(self, temporal_slot: str) -> float | None:
        lookup = self._slot_lookups(temporal_slot).our_wb_cost_lookup
        confirmed_qty = 0.0
        stock_qty = 0.0
        has_rows = False
        for item in self.enabled_config:
            row = lookup.get(item.nm_id)
            if not row:
                continue
            row_stock = _optional_float(row.get("stock_qty"))
            if row_stock is None:
                continue
            has_rows = True
            stock_qty += max(row_stock, 0.0)
            confirmed_qty += max(_optional_float(row.get("confirmed_qty")) or 0.0, 0.0)
        if not has_rows or stock_qty <= 0:
            return None
        return confirmed_qty / stock_qty

    def _aggregate_own_stage_confirmed_share(self, stage: str, temporal_slot: str) -> float | None:
        lookup = self._slot_lookups(temporal_slot).own_product_capital_lookup
        qty = 0.0
        confirmed = 0.0
        has_rows = False
        for item in self.enabled_config:
            row = lookup.get(item.nm_id)
            if not row:
                continue
            row_qty = _optional_float(row.get(own_stage_metric_key(stage, "qty")))
            if row_qty is None:
                continue
            has_rows = True
            qty += max(row_qty, 0.0)
            confirmed += max(
                _optional_float(row.get(own_stage_metric_key(stage, "confirmed_qty"))) or 0.0,
                0.0,
            )
        return None if not has_rows or qty <= 0 else confirmed / qty

    def _aggregate_own_stage_cost_coverage(self, stage: str, temporal_slot: str) -> float | None:
        lookup = self._slot_lookups(temporal_slot).own_product_capital_lookup
        qty = 0.0
        covered = 0.0
        has_rows = False
        for item in self.enabled_config:
            row = lookup.get(item.nm_id)
            if not row:
                continue
            row_qty = _optional_float(row.get(own_stage_metric_key(stage, "qty")))
            if row_qty is None:
                continue
            has_rows = True
            qty += max(row_qty, 0.0)
            covered += max(
                _optional_float(row.get(own_stage_metric_key(stage, "cost_covered_qty"))) or 0.0,
                0.0,
            )
        return None if not has_rows or qty <= 0 else covered / qty

    def _aggregate_own_product_capital_confirmed_share(self, temporal_slot: str) -> float | None:
        lookup = self._slot_lookups(temporal_slot).own_product_capital_lookup
        qty = 0.0
        confirmed = 0.0
        has_rows = False
        for item in self.enabled_config:
            row = lookup.get(item.nm_id)
            if not row:
                continue
            for stage in OWN_PRODUCT_CAPITAL_STAGES:
                row_qty = _optional_float(row.get(own_stage_metric_key(stage, "qty")))
                if row_qty is None:
                    continue
                has_rows = True
                qty += max(row_qty, 0.0)
                confirmed += max(
                    _optional_float(row.get(own_stage_metric_key(stage, "confirmed_qty"))) or 0.0,
                    0.0,
                )
        return None if not has_rows or qty <= 0 else confirmed / qty

    def _resolve_direct_sku(self, metric_key: str, nm_id: int, temporal_slot: str) -> float | None:
        if metric_key in {SELLER_PRICE_CHANGE_RUB_METRIC_KEY, ADVERTISING_BID_CHANGE_RUB_METRIC_KEY}:
            return _optional_float(
                self._slot_lookups(temporal_slot).sku_action_lookup.get(nm_id, {}).get(metric_key)
            )
        if metric_key == BUYER_PRICE_RUB_METRIC_KEY:
            return _lookup_attr(
                self._slot_lookups(temporal_slot),
                "spp_proxy_lookup",
                nm_id,
                "public_buyer_price",
                1.0,
            )
        if metric_key == "cost_price_rub":
            config_item = self.config_by_nm_id.get(nm_id)
            if config_item is None:
                return None
            resolved = self._slot_lookups(temporal_slot).cost_price_lookup.get(config_item.group)
            return None if resolved is None else float(resolved.cost_price_rub)
        if metric_key == "variable_costs_wb":
            order_sum = self.resolve_sku("orderSum", nm_id, temporal_slot)
            return None if order_sum is None else float(order_sum) * 0.4904
        if metric_key in {"profit_proxy_rub", "proxy_profit_rub"}:
            order_sum = self.resolve_sku("orderSum", nm_id, temporal_slot)
            order_count = self.resolve_sku("orderCount", nm_id, temporal_slot)
            cost_price = self.resolve_sku("cost_price_rub", nm_id, temporal_slot)
            ads_sum = self.resolve_sku("ads_sum", nm_id, temporal_slot)
            if None in {order_sum, order_count, cost_price, ads_sum}:
                return None
            return float(order_sum) * 0.5096 - float(order_count) * 0.91 * float(cost_price) - float(ads_sum)
        if metric_key == ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY:
            order_sum = self.resolve_sku("orderSum", nm_id, temporal_slot)
            order_count = self.resolve_sku("orderCount", nm_id, temporal_slot)
            onec_wb_unit_cost = self.resolve_sku(
                ONEC_STOCKS_WB_UNIT_COST_RUB_METRIC_KEY,
                nm_id,
                temporal_slot,
            )
            ads_sum = self.resolve_sku("ads_sum", nm_id, temporal_slot)
            if None in {order_sum, order_count, onec_wb_unit_cost, ads_sum}:
                return None
            return (
                float(order_sum) * 0.5096
                - float(order_count) * 0.91 * float(onec_wb_unit_cost)
                - float(ads_sum)
            )
        if metric_key == OUR_WB_UNIT_COST_RUB_METRIC_KEY:
            return _optional_float(
                self._slot_lookups(temporal_slot).our_wb_cost_lookup.get(nm_id, {}).get("our_wb_unit_cost_rub")
            )
        if metric_key == OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY:
            return _optional_float(
                self._slot_lookups(temporal_slot).our_wb_cost_lookup.get(nm_id, {}).get("confirmed_share_pct")
            )
        if metric_key == OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY:
            order_sum = self.resolve_sku("orderSum", nm_id, temporal_slot)
            order_count = self.resolve_sku("orderCount", nm_id, temporal_slot)
            our_wb_unit_cost = self.resolve_sku(OUR_WB_UNIT_COST_RUB_METRIC_KEY, nm_id, temporal_slot)
            ads_sum = self.resolve_sku("ads_sum", nm_id, temporal_slot)
            if None in {order_sum, order_count, our_wb_unit_cost, ads_sum}:
                return None
            calculated = calculate_proxy_3(
                order_sum=order_sum,
                order_count=order_count,
                canonical_wb_wac=our_wb_unit_cost,
                ads_sum=ads_sum,
                parameters=self._proxy_parameters(temporal_slot),
            )
            value = calculated["proxy_profit_3"]
            return None if value is None else float(value)
        if metric_key == OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY:
            order_sum = self.resolve_sku("orderSum", nm_id, temporal_slot)
            expected_revenue = (
                None
                if order_sum is None
                else float(order_sum) * float(self._proxy_parameters(temporal_slot).buyout_rate)
            )
            return _divide_or_none(
                self.resolve_sku(OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY, nm_id, temporal_slot),
                expected_revenue,
            )
        if metric_key == PROXY_V4_PROFIT_RUB_METRIC_KEY:
            column_date = self._slot_lookups(temporal_slot).column_date
            calculated = calculate_proxy_4(
                order_sum=self.resolve_sku("orderSum", nm_id, temporal_slot),
                order_count=self.resolve_sku("orderCount", nm_id, temporal_slot),
                canonical_wb_wac=self.resolve_sku(
                    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
                    nm_id,
                    temporal_slot,
                ),
                ads_sum=self.resolve_sku("ads_sum", nm_id, temporal_slot),
                parameters=self._proxy_v4_parameters(temporal_slot),
                business_date=column_date,
            )
            value = calculated["proxy_profit_4"]
            return None if value is None else float(value)
        if metric_key == PROXY_V4_MARGIN_PCT_METRIC_KEY:
            parameters = self._proxy_v4_parameters(temporal_slot)
            order_sum = self.resolve_sku("orderSum", nm_id, temporal_slot)
            expected_revenue = (
                None
                if parameters is None or order_sum is None
                else float(order_sum) * float(parameters.buyout_rate)
            )
            return _divide_or_none(
                self.resolve_sku(PROXY_V4_PROFIT_RUB_METRIC_KEY, nm_id, temporal_slot),
                expected_revenue,
            )
        if metric_key == ONEC_PROXY_MARGIN_2_PCT_METRIC_KEY:
            return _divide_or_zero(
                self.resolve_sku(ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY, nm_id, temporal_slot),
                self.resolve_sku("orderSum", nm_id, temporal_slot),
            )
        if metric_key == ONEC_INVENTORY_CAPITAL_RETURN_PCT_METRIC_KEY:
            return _divide_or_zero(
                self.resolve_sku(ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY, nm_id, temporal_slot),
                self.resolve_sku(ONEC_STOCKS_SKU_TOTAL_COST_RUB_METRIC_KEY, nm_id, temporal_slot),
            )
        if metric_key == OWN_CAPITAL_RETURN_PCT_METRIC_KEY:
            return _divide_or_none(
                self.resolve_sku(OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY, nm_id, temporal_slot),
                self.resolve_sku(OWN_TOTAL_CAPITAL_RUB_METRIC_KEY, nm_id, temporal_slot),
            )
        if metric_key == "inventory_value_retail_rub":
            stock_total = self.resolve_sku("stock_total", nm_id, temporal_slot)
            price_seller_discounted = self.resolve_sku("price_seller_discounted", nm_id, temporal_slot)
            if stock_total is None or price_seller_discounted is None:
                return None
            return float(stock_total) * float(price_seller_discounted)

        slot_lookups = self._slot_lookups(temporal_slot)
        if is_own_product_capital_sku_metric_key(metric_key):
            return own_product_capital_metric_value(
                metric_key,
                slot_lookups.own_product_capital_lookup.get(nm_id),
            )
        if is_onec_stock_sku_metric_key(metric_key):
            return resolve_onec_stock_metric_value(
                metric_key,
                slot_lookups.onec_stocks_lookup.get(nm_id),
            )
        if is_incident_stock_metric_key(metric_key):
            if not slot_lookups.incident_policy.get("materialize_incident_metrics"):
                return None
            return incident_stock_value(
                metric_key,
                slot_lookups.incident_stocks_lookup.get(nm_id),
            )
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
            ("spp_proxy_lookup", SPP_PROXY_METRIC_KEY, 1.0),
            ("ads_bids_lookup", "ads_bid_search", 1.0),
            ("ads_bids_lookup", "ads_bid_recommendations", 1.0),
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
                return _lookup_attr(slot_lookups, lookup_name, nm_id, attribute, scale)

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
            return slot_lookups.history_lookup.get(nm_id, {}).get(metric_key)
        if metric_key == "localizationPercent":
            return _lookup_attr(slot_lookups, "sf_period_lookup", nm_id, "localization_percent", 0.01)
        if metric_key == "feedbackRating":
            return _lookup_attr(slot_lookups, "sf_period_lookup", nm_id, "feedback_rating", 1.0)
        if metric_key in {"promo_participation", "promo_count_by_price", "promo_entry_price_best"}:
            promo_values = slot_lookups.promo_lookup.get(nm_id, {})
            value = promo_values.get(metric_key)
            return float(value) if value is not None else None
        if metric_key in BLOCKED_SOURCE_METRIC_KEYS:
            return None
        raise ValueError(f"unsupported direct metric_key: {metric_key}")

    def _slot_lookups(self, temporal_slot: str) -> SlotLookups:
        lookups = self.live_sources.slot_lookups.get(temporal_slot)
        if lookups is None:
            raise ValueError(f"missing live source lookups for temporal slot: {temporal_slot}")
        return lookups

    def _proxy_parameters(self, temporal_slot: str) -> ProxyParameters:
        column_date = self._slot_lookups(temporal_slot).column_date
        # The 01.07 effective settings version is projected backwards together
        # with the same-nmID canonical cost; later dates use their own effective
        # version.  This is Proxy 3 on both sides of the boundary.
        effective_date = max(column_date, OUR_WB_COST_OPENING_DATE)
        return self.proxy_parameters_resolver(effective_date)

    def _proxy_v4_parameters(self, temporal_slot: str) -> ProxyV4Parameters | None:
        column_date = self._slot_lookups(temporal_slot).column_date
        return self.proxy_v4_parameters_resolver(column_date)


BLOCKED_SOURCE_METRIC_KEYS: set[str] = set()


def _build_metric_rows(
    metric: MetricV2Item,
    enabled_config: list[ConfigV2Item],
    evaluator: _MetricEvaluator,
    temporal_slots: list[SheetVitrinaV1TemporalSlot],
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    if metric.scope == "TOTAL":
        rows.append(
            [
                f"Итого: {metric.label_ru}",
                f"TOTAL|{metric.metric_key}",
                *[
                    _to_sheet_value(evaluator.resolve_total(metric.metric_key, slot.slot_key))
                    for slot in temporal_slots
                ],
            ]
        )
        return rows
    if metric.scope == "GROUP":
        for group_name, group_items in _group_config(enabled_config).items():
            if not group_items:
                continue
            rows.append(
                [
                    f"Группа {group_name}: {metric.label_ru}",
                    f"GROUP:{group_name}|{metric.metric_key}",
                    *[
                        _to_sheet_value(
                            evaluator.resolve_group(metric.metric_key, group_name, slot.slot_key)
                        )
                        for slot in temporal_slots
                    ],
                ]
            )
        return rows
    if metric.scope == "SKU":
        for config_item in enabled_config:
            rows.append(
                [
                    f"{config_item.display_name}: {metric.label_ru}",
                    f"SKU:{config_item.nm_id}|{metric.metric_key}",
                    *[
                        _to_sheet_value(
                            evaluator.resolve_sku(metric.metric_key, config_item.nm_id, slot.slot_key)
                        )
                        for slot in temporal_slots
                    ],
                ]
            )
        return rows
    raise ValueError(f"unsupported metric scope: {metric.scope}")


def _build_status_rows(
    *,
    current_state: Any,
    displayed_metrics: list[MetricV2Item],
    data_rows: list[list[Any]],
    live_sources: TemporalLiveSources,
    temporal_slots: list[SheetVitrinaV1TemporalSlot],
    scope_row_counts: Mapping[str, int],
    section_row_counts: Mapping[str, int],
    execution_mode: str,
) -> list[list[Any]]:
    non_empty_value_rows = sum(
        1
        for row in data_rows
        if any(cell not in ("", None) for cell in row[2:])
    )
    status_rows = [
        [
            "registry_upload_current_state",
            "success",
            business_date_from_timestamp(current_state.activated_at),
            business_date_from_timestamp(current_state.activated_at),
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
                    "date_columns": ",".join(slot.column_date for slot in temporal_slots),
                    "execution_mode": execution_mode,
                }
            ),
        ]
    ]
    status_rows.extend(
        [
            [
                _format_temporal_source_key(status.source_key, status.temporal_slot),
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
            DELIVERY_CONTRACT_VERSION,
            "success",
            temporal_slots[-1].column_date,
            temporal_slots[-1].column_date,
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
                    "date_columns": ",".join(slot.column_date for slot in temporal_slots),
                    "temporal_slots": ",".join(
                        f"{slot.slot_key}:{slot.column_date}" for slot in temporal_slots
                    ),
                    "execution_mode": execution_mode,
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


def _lookup_attr(slot_lookups: SlotLookups, lookup_name: str, nm_id: int, attribute: str, scale: float) -> float | None:
    lookup = getattr(slot_lookups, lookup_name)
    item = lookup.get(nm_id)
    if item is None:
        return None
    value = (
        item.get(attribute)
        if isinstance(item, Mapping)
        else getattr(item, attribute, None)
    )
    if value is None:
        return None
    return float(value) * scale


def _price_seller_discounted_by_nm_id(prices_lookup: Mapping[int, Any]) -> dict[int, float | None]:
    result: dict[int, float | None] = {}
    for nm_id, item in prices_lookup.items():
        value = getattr(item, "price_seller_discounted", None)
        try:
            result[int(nm_id)] = None if value is None else float(value)
        except (TypeError, ValueError):
            result[int(nm_id)] = None
    return result


def _expand_selected_source_keys_for_dependencies(source_keys: set[str]) -> set[str]:
    if not source_keys:
        return set()
    expanded = set(source_keys)
    if SPP_PROXY_SOURCE_KEY in expanded:
        expanded.add("prices_snapshot")
    return expanded


def _capture_live_source(
    *,
    source_key: str,
    temporal_slot: str,
    temporal_policy: str,
    column_date: str,
    requested_nm_ids: list[int],
    loader: Callable[[], Any],
) -> tuple[LiveSourceStatus, Any | None]:
    try:
        payload = loader()
    except Exception as exc:  # pragma: no cover - live transport fallback
        return (
            LiveSourceStatus(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
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
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
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
    payload_diagnostics = _payload_diagnostics(payload)
    if kind == "incomplete":
        missing_nm_ids = list(getattr(payload, "missing_nm_ids", []))
        requested_count = int(getattr(payload, "requested_count", len(requested_nm_ids)))
        covered_count = int(getattr(payload, "covered_count", 0))
        status = LiveSourceStatus(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            kind=kind,
            freshness=_resolve_freshness(payload),
            snapshot_date=_payload_temporal_value(payload, "snapshot_date"),
            date=_payload_temporal_value(payload, "date"),
            date_from=_payload_temporal_value(payload, "date_from"),
            date_to=_payload_temporal_value(payload, "date_to"),
            requested_count=requested_count,
            covered_count=covered_count,
            missing_nm_ids=missing_nm_ids,
            note=str(getattr(payload, "detail", "") or ""),
            diagnostics=payload_diagnostics,
        )
        return (
            _with_onec_stage_bucket_coverage_status(status, payload),
            payload,
        )

    items = list(getattr(payload, "items", []) or [])
    covered_nm_ids = {getattr(item, "nm_id", None) for item in items if isinstance(getattr(item, "nm_id", None), int)}
    covered_nm_ids.discard(None)
    status = LiveSourceStatus(
        source_key=source_key,
        temporal_slot=temporal_slot,
        temporal_policy=temporal_policy,
        column_date=column_date,
        kind=kind,
        freshness=_resolve_freshness(payload),
        snapshot_date=_payload_temporal_value(payload, "snapshot_date"),
        date=_payload_temporal_value(payload, "date"),
        date_from=_payload_temporal_value(payload, "date_from"),
        date_to=_payload_temporal_value(payload, "date_to"),
        requested_count=len(requested_nm_ids),
        covered_count=len(covered_nm_ids),
        missing_nm_ids=sorted(set(requested_nm_ids) - set(covered_nm_ids)),
        note=_status_note_from_payload(payload),
        diagnostics=payload_diagnostics,
    )
    return (
        _with_onec_stage_bucket_coverage_status(status, payload),
        payload,
    )


def _with_onec_stage_bucket_coverage_status(
    status: LiveSourceStatus,
    payload: Any | None,
) -> LiveSourceStatus:
    if status.source_key != ONEC_STOCKS_SOURCE_KEY or status.kind not in {"success", "incomplete"}:
        return status
    coverage = summarize_onec_stage_bucket_coverage(payload)
    if not coverage or int(coverage.get("item_count") or 0) <= 0:
        return status

    diagnostics = dict(status.diagnostics or {})
    diagnostics["onec_stage_bucket_coverage"] = coverage
    missing_stage_buckets = [
        str(item)
        for item in (coverage.get("missing_stage_buckets") or [])
        if str(item).strip()
    ]
    if not missing_stage_buckets:
        return replace(status, diagnostics=diagnostics)

    covered_stage_buckets = [
        str(item)
        for item in (coverage.get("covered_stage_buckets") or [])
        if str(item).strip()
    ]
    if _onec_missing_stage_buckets_are_zero_stock(status, coverage):
        diagnostics["onec_zero_stock_stage_buckets"] = {
            "stage_buckets": missing_stage_buckets,
            "reason": "empty_bucket_after_active_sku_filter",
            "materialization": "zero_stock_rows",
        }
        coverage_note = _format_note(
            {
                "zero_stock_stage_buckets": ",".join(missing_stage_buckets),
                "covered_stage_buckets": ",".join(covered_stage_buckets),
                "missing_stage_bucket_rows": "materialized_as_zero_stock",
                "zero_stock_reason": "empty_bucket_after_active_sku_filter",
            }
        )
        return replace(
            status,
            kind="success",
            note=_append_invalid_payload_note(status.note, coverage_note),
            diagnostics=diagnostics,
        )
    coverage_note = _format_note(
        {
            "missing_stage_buckets": ",".join(missing_stage_buckets),
            "covered_stage_buckets": ",".join(covered_stage_buckets),
            "missing_stage_bucket_rows": "left_blank_without_fake_zeros",
        }
    )
    return replace(
        status,
        kind="incomplete",
        note=_append_invalid_payload_note(status.note, coverage_note),
        diagnostics=diagnostics,
    )


def _onec_missing_stage_buckets_are_zero_stock(
    status: LiveSourceStatus,
    coverage: Mapping[str, Any],
) -> bool:
    if status.kind != "success":
        return False
    if int(status.requested_count or 0) <= 0:
        return False
    if int(status.covered_count or 0) < int(status.requested_count or 0):
        return False
    if status.missing_nm_ids:
        return False
    if coverage.get("unmapped_stage_names"):
        return False
    return True


def _missing_onec_stage_buckets_from_status(status: LiveSourceStatus) -> list[str]:
    if _note_csv_values(status.note, "zero_stock_stage_buckets"):
        return []
    diagnostics = status.diagnostics if isinstance(status.diagnostics, Mapping) else {}
    zero_stock = diagnostics.get("onec_zero_stock_stage_buckets") if isinstance(diagnostics, Mapping) else None
    if isinstance(zero_stock, Mapping) and zero_stock.get("stage_buckets"):
        return []
    coverage = status.diagnostics.get("onec_stage_bucket_coverage") if isinstance(status.diagnostics, Mapping) else None
    if isinstance(coverage, Mapping):
        missing = [
            str(item).strip()
            for item in (coverage.get("missing_stage_buckets") or [])
            if str(item).strip()
        ]
        if missing:
            return sorted(set(missing))
    return _note_csv_values(status.note, "missing_stage_buckets")


def _merge_onec_missing_stage_buckets_from_payload(
    *,
    payload: Any,
    fallback_payload: Any,
    missing_stage_buckets: Iterable[str],
) -> tuple[Any | None, list[str]]:
    missing = {str(item).strip() for item in missing_stage_buckets if str(item).strip()}
    if not missing:
        return None, []
    current_items = list(getattr(payload, "items", []) or [])
    fallback_items = list(getattr(fallback_payload, "items", []) or [])
    existing_keys = {
        (
            getattr(item, "nm_id", None),
            normalize_onec_stage_code(
                getattr(item, "canonical_stage_code", None)
                or getattr(item, "stage_name", None)
            ),
        )
        for item in current_items
    }
    preserved_items: list[Any] = []
    preserved_buckets: set[str] = set()
    for item in fallback_items:
        stage_key = normalize_onec_stage_code(
            getattr(item, "canonical_stage_code", None)
            or getattr(item, "stage_name", None)
        )
        if stage_key not in missing:
            continue
        dedupe_key = (getattr(item, "nm_id", None), stage_key)
        if dedupe_key in existing_keys:
            continue
        preserved_items.append(item)
        preserved_buckets.add(str(stage_key))
        existing_keys.add(dedupe_key)
    if not preserved_items:
        return None, []

    dynamic_stage_names = [
        str(item)
        for item in (getattr(payload, "dynamic_stage_names", []) or [])
        if str(item).strip()
    ]
    seen_dynamic = set(dynamic_stage_names)
    for item in preserved_items:
        stage_name = str(getattr(item, "stage_name", "") or "").strip()
        if stage_name and stage_name not in seen_dynamic:
            dynamic_stage_names.append(stage_name)
            seen_dynamic.add(stage_name)

    merged_items = [*current_items, *preserved_items]
    return (
        replace(
            payload,
            items=merged_items,
            stage_count=len(merged_items),
            dynamic_stage_names=dynamic_stage_names,
        ),
        sorted(preserved_buckets),
    )


def _note_csv_values(note: str, key: str) -> list[str]:
    prefix = f"{key}="
    for part in str(note or "").split(";"):
        text = part.strip()
        if not text.startswith(prefix):
            continue
        raw = text[len(prefix):].strip()
        return sorted({item.strip() for item in raw.split(",") if item.strip()})
    return []


def _build_temporal_gap_status(
    *,
    source_key: str,
    temporal_slot: str,
    temporal_policy: str,
    column_date: str,
    requested_count: int,
) -> LiveSourceStatus:
    gap_note = (
        "source is current-only in the bounded live contour; "
        "yesterday_closed is left blank instead of backfilling current values into a closed-day column"
    )
    if temporal_policy == TEMPORAL_POLICY_YESTERDAY_CLOSED_ONLY:
        gap_note = (
            "source is not available for today_current in the bounded live contour; "
            "today column stays blank instead of inventing fresh values"
        )
    return LiveSourceStatus(
        source_key=source_key,
        temporal_slot=temporal_slot,
        temporal_policy=temporal_policy,
        column_date=column_date,
        kind="not_available",
        freshness="",
        snapshot_date="",
        date="",
        date_from="",
        date_to="",
        requested_count=requested_count,
        covered_count=0,
        missing_nm_ids=[],
        note=gap_note,
    )


def _build_invalid_temporal_web_source_status(
    *,
    source_key: str,
    temporal_slot: str,
    temporal_policy: str,
    column_date: str,
    requested_nm_ids: list[int],
    payload: Any,
    note_suffix: str,
) -> LiveSourceStatus:
    return LiveSourceStatus(
        source_key=source_key,
        temporal_slot=temporal_slot,
        temporal_policy=temporal_policy,
        column_date=column_date,
        kind="error",
        freshness=_resolve_freshness(payload),
        snapshot_date=_payload_temporal_value(payload, "snapshot_date"),
        date=_payload_temporal_value(payload, "date"),
        date_from=_payload_temporal_value(payload, "date_from"),
        date_to=_payload_temporal_value(payload, "date_to"),
        requested_count=len(requested_nm_ids),
        covered_count=0,
        missing_nm_ids=sorted(set(requested_nm_ids)),
        note=_append_invalid_payload_note(
            _status_note_from_payload(payload),
            note_suffix,
        ),
    )


def _build_closure_retry_status(
    *,
    source_key: str,
    temporal_slot: str,
    temporal_policy: str,
    column_date: str,
    requested_nm_ids: list[int],
    closure_state: TemporalSourceClosureState | None,
) -> LiveSourceStatus:
    state = closure_state.state if closure_state is not None else CLOSURE_STATE_PENDING
    note_parts = [
        f"closure_state={state}",
        (
            f"attempt_count={closure_state.attempt_count}"
            if closure_state is not None
            else "attempt_count=0"
        ),
    ]
    if closure_state is not None and closure_state.next_retry_at:
        note_parts.append(f"next_retry_at={closure_state.next_retry_at}")
    if closure_state is not None and closure_state.last_attempt_at:
        note_parts.append(f"last_attempt_at={closure_state.last_attempt_at}")
    if closure_state is not None and closure_state.last_reason:
        note_parts.append(f"last_reason={closure_state.last_reason}")
    return LiveSourceStatus(
        source_key=source_key,
        temporal_slot=temporal_slot,
        temporal_policy=temporal_policy,
        column_date=column_date,
        kind=state,
        freshness="",
        snapshot_date="",
        date=column_date if source_key == "seller_funnel_snapshot" else "",
        date_from=column_date if source_key == "web_source_snapshot" else "",
        date_to=column_date if source_key == "web_source_snapshot" else "",
        requested_count=len(requested_nm_ids),
        covered_count=0,
        missing_nm_ids=sorted(set(requested_nm_ids)),
        note="; ".join(note_parts),
    )


def _build_cost_price_status(
    *,
    current_state: CostPriceCurrentState | None,
    requested_groups: list[str],
    temporal_slot: str,
    column_date: str,
) -> tuple[LiveSourceStatus, dict[str, ResolvedCostPrice]]:
    if current_state is None:
        return (
            LiveSourceStatus(
                source_key="cost_price",
                temporal_slot=temporal_slot,
                temporal_policy=SOURCE_TEMPORAL_POLICIES["cost_price"],
                column_date=column_date,
                kind="missing",
                freshness="",
                snapshot_date="",
                date=column_date,
                date_from="",
                date_to="",
                requested_count=len(requested_groups),
                covered_count=0,
                missing_nm_ids=[],
                note="authoritative COST_PRICE current state is not materialized",
            ),
            {},
        )

    rows_by_group = _index_cost_price_rows(current_state.cost_price_rows)
    resolved: dict[str, ResolvedCostPrice] = {}
    unmatched_groups: list[str] = []
    for group_name in requested_groups:
        cost_row = _resolve_cost_price_row(rows_by_group, group_name, column_date)
        if cost_row is None:
            unmatched_groups.append(group_name)
            continue
        resolved[group_name] = cost_row

    matched_count = len(resolved)
    if matched_count == len(requested_groups):
        kind = "success"
    elif matched_count == 0:
        kind = "missing"
    else:
        kind = "incomplete"

    note_payload: dict[str, Any] = {
        "dataset_version": current_state.dataset_version,
        "dataset_activated_at": current_state.activated_at,
        "dataset_row_count": len(current_state.cost_price_rows),
        "matched_groups": matched_count,
        "unmatched_groups": len(unmatched_groups),
        "resolution_rule": "latest_effective_from<=slot_date",
    }
    if unmatched_groups:
        note_payload["missing_groups"] = ",".join(unmatched_groups)

    return (
        LiveSourceStatus(
            source_key="cost_price",
            temporal_slot=temporal_slot,
            temporal_policy=SOURCE_TEMPORAL_POLICIES["cost_price"],
            column_date=column_date,
            kind=kind,
            freshness=business_date_from_timestamp(current_state.activated_at),
            snapshot_date=business_date_from_timestamp(current_state.activated_at),
            date=column_date,
            date_from="",
            date_to="",
            requested_count=len(requested_groups),
            covered_count=matched_count,
            missing_nm_ids=[],
            note=_format_note(note_payload),
        ),
        resolved,
    )


def _execution_mode_allows_persisted_retry(execution_mode: str) -> bool:
    return execution_mode in {EXECUTION_MODE_AUTO_DAILY, EXECUTION_MODE_PERSISTED_RETRY}


def _source_slot_supports_persisted_retry(*, source_key: str, temporal_slot: str) -> bool:
    if temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
        return source_key in HISTORICAL_CLOSED_DAY_SOURCE_KEYS
    if temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT:
        return source_key in CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS
    return False


def _accepted_resolution_note(temporal_slot: str) -> str:
    if temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
        return "resolution_rule=accepted_closed_current_attempt"
    return "resolution_rule=accepted_current_current_attempt"


def _runtime_cache_note(source_key: str) -> str:
    if source_key == "stocks":
        return "resolution_rule=exact_date_stocks_history_runtime_cache"
    if source_key == "promo_by_price":
        return "resolution_rule=exact_date_promo_current_runtime_cache"
    return "resolution_rule=exact_date_runtime_cache"


def _invalid_temporal_candidate_note(source_key: str, temporal_slot: str) -> str:
    if source_key in STRICT_CLOSED_DAY_SOURCE_KEYS:
        return _invalid_temporal_web_source_note(source_key)
    if source_key == "promo_by_price":
        return "invalid_exact_snapshot=promo_live_source_incomplete"
    if temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT and source_key == "prices_snapshot":
        return "invalid_exact_snapshot=zero_filled_prices_snapshot"
    if temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT and source_key == "ads_bids":
        return "invalid_exact_snapshot=zero_filled_ads_bids_snapshot"
    if temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT and source_key == SPP_PROXY_SOURCE_KEY:
        return "invalid_exact_snapshot=empty_spp_proxy_snapshot"
    return "invalid_exact_snapshot"


def _is_valid_temporal_candidate(
    *,
    source_key: str,
    status: LiveSourceStatus,
    payload: Any | None,
    column_date: str,
    temporal_slot: str,
) -> bool:
    if payload is None or not _is_exact_snapshot_payload(payload, column_date):
        return False
    if status.kind != "success":
        if not (
            source_key == ONEC_STOCKS_SOURCE_KEY
            and status.kind == "incomplete"
            and status.covered_count > 0
        ) and not (
            source_key == SPP_PROXY_SOURCE_KEY
            and status.kind == "incomplete"
            and status.covered_count > 0
        ):
            return False
    if status.kind == "incomplete" and source_key not in {ONEC_STOCKS_SOURCE_KEY, SPP_PROXY_SOURCE_KEY}:
        return False
    if _is_invalid_temporal_web_source_payload(
        source_key=source_key,
        payload=payload,
        column_date=column_date,
        closed_day_required=temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED,
    ):
        return False
    if source_key == "prices_snapshot":
        items = getattr(payload, "items", None)
        if not isinstance(items, list) or not items:
            return False
        return any(
            _numeric_payload_value(getattr(item, "price_seller", None)) > 0
            or _numeric_payload_value(getattr(item, "price_seller_discounted", None)) > 0
            for item in items
        )
    if source_key == "ads_bids":
        items = getattr(payload, "items", None)
        if not isinstance(items, list) or not items:
            return False
        return any(
            _numeric_payload_value(getattr(item, "ads_bid_search", None)) > 0
            or _numeric_payload_value(getattr(item, "ads_bid_recommendations", None)) > 0
            for item in items
        )
    if source_key == SPP_PROXY_SOURCE_KEY:
        items = getattr(payload, "items", None)
        if not isinstance(items, list) or not items:
            return False
        return any(
            getattr(item, SPP_PROXY_METRIC_KEY, None) is not None
            and _numeric_payload_value(getattr(item, SPP_PROXY_METRIC_KEY, None)) >= 0
            for item in items
        )
    return True


def _build_preserved_accepted_status(
    *,
    accepted_status: LiveSourceStatus,
    accepted_at: str | None,
    latest_status: LiveSourceStatus,
    temporal_slot: str,
) -> LiveSourceStatus:
    resolution_rule = (
        "resolution_rule=accepted_closed_preserved_after_invalid_attempt"
        if temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED
        else "resolution_rule=accepted_current_preserved_after_invalid_attempt"
    )
    note_parts = [resolution_rule]
    if accepted_at:
        note_parts.append(f"accepted_at={accepted_at}")
    note_parts.append(f"latest_attempt_kind={latest_status.kind}")
    if latest_status.note:
        note_parts.append(f"latest_attempt_note={latest_status.note}")
    return _append_status_note(
        replace(
            accepted_status,
            diagnostics=_preserved_status_diagnostics(
                accepted_status=accepted_status,
                latest_status=latest_status,
                accepted_at=accepted_at,
                temporal_slot=temporal_slot,
            ),
        ),
        "; ".join(note_parts),
    )


def _preserved_status_diagnostics(
    *,
    accepted_status: LiveSourceStatus,
    latest_status: LiveSourceStatus,
    accepted_at: str | None,
    temporal_slot: str,
) -> dict[str, Any]:
    if accepted_status.source_key != "promo_by_price":
        return dict(accepted_status.diagnostics or {})
    accepted_diagnostics = _plain_jsonable(accepted_status.diagnostics)
    latest_diagnostics = _plain_jsonable(latest_status.diagnostics)
    diagnostics = dict(latest_diagnostics) if isinstance(latest_diagnostics, dict) and latest_diagnostics else {}
    if not diagnostics and isinstance(accepted_diagnostics, dict):
        diagnostics = dict(accepted_diagnostics)
    if isinstance(accepted_diagnostics, dict) and accepted_diagnostics:
        diagnostics["preserved_payload_diagnostics"] = accepted_diagnostics
    fallback = dict(diagnostics.get("fallback") or {})
    fallback.update(
        {
            "attempted_current_fetch": True,
            "candidate_accepted": False,
            "candidate_rejected": True,
            "invalid_reason": _promo_invalid_reason(latest_status.note),
            "fallback_reason": (
                "accepted_current_preserved_after_invalid_attempt"
                if temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT
                else "accepted_closed_preserved_after_invalid_attempt"
            ),
            "preserved_snapshot_date": accepted_status.snapshot_date or accepted_status.column_date,
            "preserved_snapshot_role": (
                TEMPORAL_ROLE_ACCEPTED_CURRENT
                if temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT
                else TEMPORAL_ROLE_ACCEPTED_CLOSED
            ),
            "preserved_snapshot_captured_at": accepted_at,
            "preserved_snapshot_age_ms": None,
            "preserved_origin": "accepted_slot",
            "current_attempt_status": latest_status.kind,
            "current_attempt_semantic_status": _promo_attempt_semantic_status(latest_status.kind),
        }
    )
    diagnostics["fallback"] = fallback
    return diagnostics


def _coerce_invalid_temporal_candidate_status(
    *,
    status: LiveSourceStatus,
    requested_nm_ids: list[int],
    note_suffix: str,
) -> LiveSourceStatus:
    return replace(
        status,
        kind="error",
        covered_count=0,
        missing_nm_ids=sorted(set(requested_nm_ids)),
        note=_append_invalid_payload_note(status.note, note_suffix),
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


def _payload_diagnostics(payload: Any) -> dict[str, Any]:
    raw = getattr(payload, "diagnostics", None)
    normalized = _plain_jsonable(raw)
    return normalized if isinstance(normalized, dict) else {}


def _plain_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _plain_jsonable(item)
            for key, item in vars(value).items()
        }
    return str(value)


def _resolve_freshness(payload: Any) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = _payload_temporal_value(payload, field)
        if value:
            return value
    return ""


def _payload_temporal_value(payload: Any, field: str) -> str:
    value = getattr(payload, field, None)
    if isinstance(value, str) and value:
        return value
    if field in {"snapshot_date", "date", "date_from", "date_to"}:
        meta = getattr(payload, "meta", None)
        if isinstance(meta, Mapping):
            meta_date = meta.get("date")
        else:
            meta_date = getattr(meta, "date", None)
        if isinstance(meta_date, str) and meta_date:
            return meta_date
    return ""


def _source_policy_supports_slot(temporal_policy: str, temporal_slot: str) -> bool:
    return _canonical_source_policy_supports_slot(temporal_policy, temporal_slot)


def _format_temporal_source_key(source_key: str, temporal_slot: str) -> str:
    return f"{source_key}[{temporal_slot}]"


def _is_exact_snapshot_payload(payload: Any, column_date: str) -> bool:
    kind = str(getattr(payload, "kind", ""))
    if kind == "success":
        return _resolve_freshness(payload) == column_date
    if kind == "incomplete" and bool(getattr(payload, "temporal_snapshot_acceptable", False)):
        return _resolve_freshness(payload) == column_date
    return False


def _is_invalid_temporal_web_source_payload(
    source_key: str,
    payload: Any | None,
    column_date: str,
    *,
    closed_day_required: bool,
) -> bool:
    if payload is None:
        return False
    if not _is_exact_snapshot_payload(payload, column_date):
        return False
    items = getattr(payload, "items", None)
    if not isinstance(items, list) or not items:
        return True
    if source_key == "seller_funnel_snapshot":
        return not any(
            _numeric_payload_value(getattr(item, "view_count", None)) > 0
            or _numeric_payload_value(getattr(item, "open_card_count", None)) > 0
            for item in items
        )
    if source_key == "web_source_snapshot":
        if not any(
            _numeric_payload_value(getattr(item, "views_current", None)) > 0
            or _numeric_payload_value(getattr(item, "ctr_current", None)) > 0
            or _numeric_payload_value(getattr(item, "orders_current", None)) > 0
            for item in items
        ):
            return True
        return False if not closed_day_required else False
    return False


def _invalid_temporal_web_source_note(source_key: str) -> str:
    if source_key == "seller_funnel_snapshot":
        return "invalid_exact_snapshot=zero_filled_seller_funnel_snapshot"
    if source_key == "web_source_snapshot":
        return "invalid_exact_snapshot=zero_filled_web_source_snapshot"
    return "invalid_exact_snapshot"


def _closure_attempt_is_due(
    closure_state: TemporalSourceClosureState | None,
    now: datetime,
) -> bool:
    if closure_state is None:
        return True
    if closure_state.state == CLOSURE_STATE_SUCCESS:
        return False
    if closure_state.state == CLOSURE_STATE_EXHAUSTED:
        return False
    if not closure_state.next_retry_at:
        return True
    return _parse_runtime_timestamp(closure_state.next_retry_at) <= now.astimezone(timezone.utc)


def _next_closure_retry(now: datetime, attempt_count: int, reason: str) -> tuple[str | None, str]:
    if attempt_count >= len(CLOSURE_RETRY_BACKOFF_MINUTES):
        return None, CLOSURE_STATE_EXHAUSTED
    retry_after_minutes = CLOSURE_RETRY_BACKOFF_MINUTES[max(attempt_count - 1, 0)]
    state = CLOSURE_STATE_RATE_LIMITED if _looks_like_rate_limit_reason(reason) else CLOSURE_STATE_RETRYING
    next_retry_at = _format_runtime_timestamp(now.astimezone(timezone.utc) + timedelta(minutes=retry_after_minutes))
    return next_retry_at, state


def _looks_like_rate_limit_reason(reason: str) -> bool:
    lowered = str(reason or "").lower()
    return "429" in lowered or "retry-after" in lowered or "rate limit" in lowered


def _append_status_note(status: LiveSourceStatus, suffix: str) -> LiveSourceStatus:
    merged = "; ".join(part for part in [status.note, suffix] if part)
    return replace(status, note=merged)


def _append_invalid_payload_note(note: str, suffix: str) -> str:
    return "; ".join(part for part in [note, suffix] if part)


def _append_current_web_source_sync_note(
    status: LiveSourceStatus,
    note: str | None,
) -> LiveSourceStatus:
    if not note or status.kind == "success":
        return status
    return _append_status_note(status, note)


def _format_runtime_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_runtime_timestamp(value: str) -> datetime:
    normalized = str(value or "")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _closed_day_capture_is_fresh(*, captured_at: str | None, snapshot_date: str) -> bool:
    if not captured_at:
        return False
    try:
        captured = _parse_runtime_timestamp(captured_at)
        required_after = _closed_day_required_capture_after(snapshot_date)
    except (TypeError, ValueError):
        return False
    return captured >= required_after


def _closed_day_required_capture_after(snapshot_date: str) -> datetime:
    snapshot_day = date.fromisoformat(snapshot_date)
    next_business_day_start = datetime.combine(
        snapshot_day + timedelta(days=1),
        datetime_time(0, 0),
        tzinfo=CANONICAL_BUSINESS_TIMEZONE,
    )
    return next_business_day_start.astimezone(timezone.utc)


def _numeric_payload_value(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, str) and not value.strip():
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _index_items_by_nm_id(payload: Any | None) -> dict[int, Any]:
    if payload is None:
        return {}
    items = getattr(payload, "items", None)
    if not isinstance(items, list):
        return {}
    return {int(item.nm_id): item for item in items if isinstance(getattr(item, "nm_id", None), int)}


def _index_promo_items(payload: Any | None) -> dict[int, dict[str, float]]:
    if payload is None:
        return {}
    items = getattr(payload, "items", None)
    if not isinstance(items, list):
        return {}
    out: dict[int, dict[str, float]] = {}
    for item in items:
        nm_id = getattr(item, "nm_id", None)
        if not isinstance(nm_id, int):
            continue
        out[nm_id] = {
            "promo_count_by_price": float(getattr(item, "promo_count_by_price", 0.0) or 0.0),
            "promo_entry_price_best": float(getattr(item, "promo_entry_price_best", 0.0) or 0.0),
            "promo_participation": float(getattr(item, "promo_participation", 0.0) or 0.0),
        }
    return out


def _index_cost_price_rows(cost_price_rows: list[CostPriceRow]) -> dict[str, list[CostPriceRow]]:
    grouped: dict[str, list[CostPriceRow]] = {}
    for row in sorted(cost_price_rows, key=lambda item: (item.group, item.effective_from)):
        grouped.setdefault(row.group, []).append(row)
    return grouped


def _resolve_cost_price_row(
    rows_by_group: Mapping[str, list[CostPriceRow]],
    group_name: str,
    column_date: str,
) -> ResolvedCostPrice | None:
    candidates = rows_by_group.get(group_name, [])
    applicable = [row for row in candidates if row.effective_from <= column_date]
    if not applicable:
        return None
    selected = applicable[-1]
    return ResolvedCostPrice(
        group_name=group_name,
        cost_price_rub=float(selected.cost_price_rub),
        effective_from=selected.effective_from,
    )


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


def _build_temporal_slots(
    *,
    as_of_date: str,
    current_date: str,
) -> list[SheetVitrinaV1TemporalSlot]:
    return [
        SheetVitrinaV1TemporalSlot(
            slot_key=TEMPORAL_SLOT_YESTERDAY_CLOSED,
            slot_label=TEMPORAL_SLOT_YESTERDAY_CLOSED,
            column_date=as_of_date,
        ),
        SheetVitrinaV1TemporalSlot(
            slot_key=TEMPORAL_SLOT_TODAY_CURRENT,
            slot_label=TEMPORAL_SLOT_TODAY_CURRENT,
            column_date=current_date,
        ),
    ]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_now_factory() -> datetime:
    override = str(os.environ.get("SHEET_VITRINA_CURRENT_DATE_OVERRIDE", "") or "").strip()
    if override:
        return business_datetime_for_override(override)
    return _utc_now()


def _persisted_stocks_warehouse_region_map(
    runtime: RegistryUploadDbBackedRuntime,
) -> dict[str, str]:
    """Reuse only accepted concrete warehouse identities as metadata."""

    mapping: dict[str, str] = {}
    snapshot_dates = runtime.list_temporal_source_snapshot_dates(source_key="stocks")
    for snapshot_date in reversed(snapshot_dates[-31:]):
        payload, _captured_at = runtime.load_temporal_source_snapshot(
            source_key="stocks",
            snapshot_date=snapshot_date,
        )
        if str(getattr(payload, "kind", "") or "") != "success":
            continue
        for row in list(getattr(payload, "warehouse_rows", []) or []):
            warehouse_name = str(
                getattr(row, "warehouse_name", "") or ""
            ).strip()
            region_name = str(
                getattr(row, "region_name", "") or ""
            ).strip()
            warehouse_id = getattr(row, "warehouse_id", None)
            if (
                not warehouse_name
                or not region_name
                or warehouse_name.casefold() in {"склад wb", "остальные"}
                or region_name.casefold() == "склад wb"
                or warehouse_id == 0
            ):
                continue
            if warehouse_id is not None and (
                not isinstance(warehouse_id, int)
                or isinstance(warehouse_id, bool)
                or warehouse_id <= 0
            ):
                continue
            mapping.setdefault(warehouse_name, region_name)
    return mapping


def _stocks_vitrina_lookup(
    payload: Any | None,
    *,
    warehouse_granularity_complete: bool,
) -> dict[int, Any]:
    lookup = _index_items_by_nm_id(payload)
    if warehouse_granularity_complete:
        return lookup
    regional_fields = {
        "stock_ru_central",
        "stock_ru_northwest",
        "stock_ru_volga",
        "stock_ru_south_caucasus",
        "stock_ru_ural",
        "stock_ru_far_siberia",
        "stock_ru_central_north",
        "stock_ru_central_east",
        "stock_ru_central_south",
    }
    safe_lookup: dict[int, Any] = {}
    for nm_id, item in lookup.items():
        if isinstance(item, Mapping):
            row = dict(item)
        elif hasattr(item, "__dict__"):
            row = dict(vars(item))
        else:
            row = {
                field_name: getattr(item, field_name, None)
                for field_name in (
                    "nm_id",
                    "stock_total",
                    "in_way_to_client",
                    "in_way_from_client",
                    "wb_contour_total",
                    *sorted(regional_fields),
                )
            }
        for field_name in regional_fields:
            row[field_name] = None
        safe_lookup[int(nm_id)] = row
    return safe_lookup


def _resolve_as_of_date(value: str | None, *, now: datetime | None = None) -> str:
    if value:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    return default_business_as_of_date(now)


def _group_config(config_items: list[ConfigV2Item]) -> dict[str, list[ConfigV2Item]]:
    grouped: dict[str, list[ConfigV2Item]] = {}
    for item in config_items:
        grouped.setdefault(item.group, []).append(item)
    return {
        group_name: sorted(items, key=lambda row: row.display_order)
        for group_name, items in sorted(grouped.items(), key=lambda pair: pair[1][0].display_order)
    }


def _load_cost_price_current_state(runtime: RegistryUploadDbBackedRuntime) -> CostPriceCurrentState | None:
    try:
        return runtime.load_cost_price_current_state()
    except ValueError as exc:
        if "cost price current state is not materialized" in str(exc):
            return None
        raise


def _split_ratio(value: str) -> tuple[str, str]:
    if "/" not in value:
        raise ValueError(f"ratio calc_ref must contain numerator/denominator: {value}")
    numerator, denominator = value.split("/", 1)
    return numerator.strip(), denominator.strip()


def _divide_or_zero(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    if float(denominator) == 0.0:
        return 0.0
    return float(numerator) / float(denominator)


def _divide_or_none(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or float(denominator) == 0.0:
        return None
    return float(numerator) / float(denominator)


def _own_product_capital_cell_presentation(
    *,
    enabled_config: Iterable[ConfigV2Item],
    displayed_metrics: Iterable[MetricV2Item],
    temporal_slots: Iterable[SheetVitrinaV1TemporalSlot],
    live_sources: TemporalLiveSources,
) -> dict[str, dict[str, dict[str, str]]]:
    metric_keys = {
        metric.metric_key
        for metric in displayed_metrics
        if metric.metric_key in set(OWN_PRODUCT_CAPITAL_METRIC_KEYS)
    }
    if not metric_keys:
        return {}
    result: dict[str, dict[str, dict[str, str]]] = {}
    sku_summary_keys = set(OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS) - {
        own_stage_metric_key(stage, field)
        for stage in OWN_PRODUCT_CAPITAL_STAGES
        for field in ("capital_rub", "qty", "unit_cost_rub", "confirmed_share_pct")
    }
    for slot in temporal_slots:
        lookup = live_sources.slot_lookups.get(slot.slot_key)
        if lookup is None:
            continue
        if slot.column_date >= "2026-07-01" and not lookup.own_product_capital_lookup:
            reason = _warehouse_history_unavailable_reason(
                column_date=slot.column_date,
                cutover_date=lookup.own_product_capital_cutover_date,
            )
            for item in enabled_config:
                for metric_key in metric_keys & set(OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS):
                    result.setdefault(f"SKU:{item.nm_id}|{metric_key}", {})[slot.column_date] = {
                        "state": "unavailable",
                        "tone": "neutral",
                        "reason": reason,
                        "source": "WebCore",
                    }
            for metric_key in metric_keys & set(OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS):
                result.setdefault(f"TOTAL|{metric_key}", {})[slot.column_date] = {
                    "state": "unavailable",
                    "tone": "neutral",
                    "reason": reason,
                    "source": "WebCore",
                }
            continue
        missing_items = [
            item
            for item in enabled_config
            if item.nm_id not in lookup.own_product_capital_lookup
        ]
        if missing_items:
            reason = (
                "Исторические данные отсутствуют: SKU не входила в requested nmID scope "
                "и canonical balances точного складского снимка этой даты. Нулевой остаток не предполагается."
            )
            for item in missing_items:
                for metric_key in metric_keys & set(OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS):
                    result.setdefault(f"SKU:{item.nm_id}|{metric_key}", {})[slot.column_date] = {
                        "state": "unavailable",
                        "tone": "neutral",
                        "reason": reason,
                        "source": "WebCore",
                    }
            total_reason = (
                "Исторические итоги недоступны: не все SKU активной витрины входили в scope "
                "точного складского снимка этой даты. Частичная сумма не публикуется."
            )
            for metric_key in metric_keys & set(OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS):
                result.setdefault(f"TOTAL|{metric_key}", {})[slot.column_date] = {
                    "state": "unavailable",
                    "tone": "neutral",
                    "reason": total_reason,
                    "source": "WebCore",
                }
        unconfirmed_rows = {
            item.nm_id: row
            for item in enabled_config
            if (row := lookup.own_product_capital_lookup.get(item.nm_id))
            and str(row.get("presentation_state") or "") == "unconfirmed"
        }
        for item in enabled_config:
            row = unconfirmed_rows.get(item.nm_id)
            if row is None:
                continue
            reason = _warehouse_quality_reason_ru(
                row.get("presentation_reason") or "provisional"
            )
            for metric_key in metric_keys:
                if metric_key not in set(OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS):
                    continue
                metric_reason = reason
                should_mark = metric_key in sku_summary_keys
                for stage in OWN_PRODUCT_CAPITAL_STAGES:
                    if metric_key in {
                        own_stage_metric_key(stage, field)
                        for field in ("capital_rub", "qty", "unit_cost_rub", "confirmed_share_pct")
                    }:
                        stage_presentation = (row.get("stage_presentation") or {}).get(stage, {})
                        should_mark = str(stage_presentation.get("state") or "") == "unconfirmed"
                        metric_reason = _warehouse_quality_reason_ru(
                            stage_presentation.get("reason") or "provisional"
                        )
                        break
                if not should_mark:
                    continue
                result.setdefault(f"SKU:{item.nm_id}|{metric_key}", {})[slot.column_date] = {
                    "state": "unconfirmed",
                    "tone": "yellow",
                    "reason": metric_reason,
                    "source": "WebCore",
                }
        if unconfirmed_rows and not missing_items:
            total_reason = "; ".join(
                sorted(
                    {
                        _warehouse_quality_reason_ru(
                            row.get("presentation_reason") or "provisional"
                        )
                        for row in unconfirmed_rows.values()
                    }
                )
            )
            for metric_key in metric_keys:
                if metric_key not in set(OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS):
                    continue
                metric_reason = total_reason
                should_mark = metric_key in {
                    OWN_TOTAL_QTY_TOTAL_METRIC_KEY,
                    OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY,
                    OWN_AVG_COST_RUB_TOTAL_METRIC_KEY,
                    OWN_TOTAL_CONFIRMED_SHARE_PCT_TOTAL_METRIC_KEY,
                    OWN_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY,
                }
                for stage in OWN_PRODUCT_CAPITAL_STAGES:
                    if metric_key in {
                        own_stage_total_metric_key(stage, field)
                        for field in ("capital_rub", "qty", "unit_cost_rub", "confirmed_share_pct")
                    }:
                        affected = [
                            row
                            for row in unconfirmed_rows.values()
                            if str(
                                ((row.get("stage_presentation") or {}).get(stage, {})).get("state") or ""
                            ) == "unconfirmed"
                        ]
                        should_mark = bool(affected)
                        metric_reason = "; ".join(
                            sorted(
                                {
                                    _warehouse_quality_reason_ru(
                                        ((row.get("stage_presentation") or {}).get(stage, {})).get("reason")
                                        or "provisional"
                                    )
                                    for row in affected
                                }
                            )
                        )
                        break
                if not should_mark:
                    continue
                result.setdefault(f"TOTAL|{metric_key}", {})[slot.column_date] = {
                    "state": "unconfirmed",
                    "tone": "yellow",
                    "reason": metric_reason,
                    "source": "WebCore",
                }
    return result


def _merge_cell_presentations(
    *presentations: Mapping[str, Mapping[str, Mapping[str, str]]],
) -> dict[str, dict[str, dict[str, str]]]:
    result: dict[str, dict[str, dict[str, str]]] = {}
    for presentation in presentations:
        for row_id, by_date in presentation.items():
            target = result.setdefault(str(row_id), {})
            for column_date, value in by_date.items():
                target[str(column_date)] = dict(value)
    return result


def _incident_stock_cell_presentation(
    *,
    enabled_config: Iterable[ConfigV2Item],
    displayed_metrics: Iterable[MetricV2Item],
    temporal_slots: Iterable[SheetVitrinaV1TemporalSlot],
    live_sources: TemporalLiveSources,
) -> dict[str, dict[str, dict[str, str]]]:
    enabled_ids = [int(item.nm_id) for item in enabled_config]
    available = {
        item.metric_key for item in displayed_metrics if item.metric_key in set(INCIDENT_STOCK_METRIC_KEYS)
    }
    if not available:
        return {}
    result: dict[str, dict[str, dict[str, str]]] = {}
    for slot in temporal_slots:
        lookups = live_sources.slot_lookups.get(slot.slot_key)
        if lookups is None or not lookups.incident_stocks_lookup:
            continue
        policy = lookups.incident_policy
        if not policy.get("active"):
            continue
        quality = lookups.incident_projection_quality
        provisional = str(quality.get("state") or "") == "provisional_received_rows"
        provisional_fields = (
            {
                "quality_state": "provisional_received_rows",
                "quality_label": str(
                    quality.get("label_ru") or "Полнота WB не подтверждена"
                ),
                "quality_reason": str(
                    quality.get("message_ru")
                    or VITRINA_PROVISIONAL_QUALITY_MESSAGE_RU
                ),
            }
            if provisional
            else {}
        )
        names = [
            str(item.get("warehouse_name") or f"warehouseId {item.get('warehouse_id')}")
            for item in policy.get("warehouse_identities") or []
        ]
        policy_detail = (
            f"Склады: {', '.join(names) or 'не указаны'}; "
            f"начало: {policy.get('effective_from') or 'не указано'}; "
            f"revision: {int(policy.get('revision') or 0)}"
        )
        for region, source_field, _suffix in INCIDENT_STOCK_FIELDS:
            fact_key = incident_stock_metric_key("fact", region)
            incident_key = incident_stock_metric_key("incident", region)
            effective_key = incident_stock_metric_key("effective", region)
            projected_rows: list[tuple[int, float, float, float]] = []
            projection_field = (
                "stock_total_mp" if source_field == "stock_total" else source_field
            )
            for nm_id in enabled_ids:
                projection_row = lookups.incident_stocks_lookup.get(nm_id)
                fact = incident_stock_value(fact_key, projection_row)
                incident = incident_stock_value(incident_key, projection_row)
                effective = incident_stock_value(effective_key, projection_row)
                if fact is None or incident is None or effective is None:
                    blank_reason = str(
                        (
                            (projection_row or {}).get("blank_reasons_by_field")
                            or {}
                        ).get(projection_field)
                        or ""
                    )
                    if not blank_reason:
                        blank_reason = (
                            "Недостаточно фактически сохранённых строк для расчёта; "
                            "нулевое значение не предполагается."
                        )
                    for metric_key in (fact_key, incident_key, effective_key):
                        if metric_key in available:
                            result.setdefault(
                                f"SKU:{nm_id}|{metric_key}", {}
                            )[slot.column_date] = {
                                "state": "unavailable",
                                "tone": "neutral",
                                "reason": blank_reason,
                                "source": "WebCore incident projection",
                                **provisional_fields,
                            }
                    continue
                projected_rows.append((nm_id, fact, incident, effective))
                reason = (
                    f"Факт: {fact:g} шт; на инцидентных складах: {incident:g} шт; "
                    f"operational остаток: {effective:g} шт. {policy_detail}"
                )
                for metric_key, _value in (
                    (fact_key, fact),
                    (incident_key, incident),
                    (effective_key, effective),
                ):
                    if metric_key in available:
                        adjusted = (
                            incident > 0 and metric_key in {incident_key, effective_key}
                        )
                        if provisional or adjusted:
                            result.setdefault(
                                f"SKU:{nm_id}|{metric_key}", {}
                            )[slot.column_date] = {
                                "state": "incident_adjusted" if adjusted else "",
                                "tone": "blue_violet" if adjusted else "neutral",
                                "reason": (
                                    reason
                                    if adjusted
                                    else VITRINA_PROVISIONAL_QUALITY_MESSAGE_RU
                                ),
                                "source": "WebCore incident policy",
                                **provisional_fields,
                            }
            if not projected_rows:
                for metric_key in (
                    incident_stock_total_metric_key("fact", region),
                    incident_stock_total_metric_key("incident", region),
                    incident_stock_total_metric_key("effective", region),
                ):
                    if metric_key in available:
                        result.setdefault(f"TOTAL|{metric_key}", {})[
                            slot.column_date
                        ] = {
                            "state": "unavailable",
                            "tone": "neutral",
                            "reason": (
                                "Нет SKU-строк с достаточным фактически сохранённым "
                                "evidence; нулевое TOTAL не предполагается."
                            ),
                            "source": "WebCore incident projection",
                            **provisional_fields,
                        }
                continue
            fact_total = sum(float(item[1]) for item in projected_rows)
            incident_total = sum(float(item[2]) for item in projected_rows)
            effective_total = sum(float(item[3]) for item in projected_rows)
            total_reason = (
                f"Факт: {fact_total:g} шт; на инцидентных складах: "
                f"{incident_total:g} шт; operational остаток: "
                f"{effective_total:g} шт. {policy_detail}"
            )
            for metric_key, _value in (
                (incident_stock_total_metric_key("fact", region), fact_total),
                (incident_stock_total_metric_key("incident", region), incident_total),
                (incident_stock_total_metric_key("effective", region), effective_total),
            ):
                if metric_key in available:
                    adjusted = (
                        incident_total > 0
                        and metric_key
                        in {
                            incident_stock_total_metric_key("incident", region),
                            incident_stock_total_metric_key("effective", region),
                        }
                    )
                    if provisional or adjusted:
                        result.setdefault(f"TOTAL|{metric_key}", {})[
                            slot.column_date
                        ] = {
                            "state": "incident_adjusted" if adjusted else "",
                            "tone": "blue_violet" if adjusted else "neutral",
                            "reason": (
                                total_reason
                                if adjusted
                                else VITRINA_PROVISIONAL_QUALITY_MESSAGE_RU
                            ),
                            "source": "WebCore incident policy",
                            **provisional_fields,
                        }
    return result


def _warehouse_quality_reason_ru(value: Any) -> str:
    codes = [item.strip() for item in str(value or "").split(";") if item.strip()]
    presentations = [
        _warehouse_balance_status_presentation(code, certified=False)
        for code in (codes or ["provisional"])
    ]
    return "; ".join(
        f"{item['label_ru']}. {item['description_ru']}" for item in presentations
    )


def _warehouse_history_unavailable_reason(*, column_date: str, cutover_date: str) -> str:
    if cutover_date and column_date < cutover_date:
        return (
            "Исторические данные отсутствуют: до функционального cutover не сохранялся "
            "полный согласованный шестиступенчатый складской снимок; текущий snapshot назад не копируется."
        )
    return (
        "Исторические данные отсутствуют: для этой даты нет точной успешной "
        "функциональной версии склада; last-good или snapshot другой даты сюда не переносится."
    )


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

    normalized = re.sub(r"(?<=\d),(?=\d)", ".", expr)
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
    normalized = re.sub(r"(?<=\d),(?=\d)", ".", expression)
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


def _format_log_event(event: str, **fields: Any) -> str:
    parts = [f"event={event}"]
    for key, value in fields.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, bool):
            normalized: Any = str(value).lower()
        elif isinstance(value, float):
            normalized: Any = round(value, 6)
        else:
            normalized = value
        text = str(normalized)
        if any(char.isspace() or char in {'"', ";", "="} for char in text):
            text = json.dumps(text, ensure_ascii=False)
        parts.append(f"{key}={text}")
    return " ".join(parts)


def _emit_source_request_log(
    emit: LivePlanLogEmitter,
    *,
    source_key: str,
    temporal_slot: str,
    temporal_policy: str,
    column_date: str,
    requested_nm_ids: list[int],
    requested_groups: list[str] | None = None,
) -> None:
    spec = SOURCE_DIAGNOSTIC_SPECS.get(source_key, {})
    request_context: dict[str, Any] = {
        "column_date": column_date,
        "requested_nm_ids": len(requested_nm_ids),
    }
    if source_key in {"seller_funnel_snapshot"}:
        request_context["date"] = column_date
    elif source_key in {"sales_funnel_history", "web_source_snapshot"}:
        request_context["date_from"] = column_date
        request_context["date_to"] = column_date
    elif source_key == "cost_price":
        request_context["requested_groups"] = len(requested_groups or [])
        request_context["resolution_rule"] = "latest_effective_from<=slot_date"
    else:
        request_context["snapshot_date"] = column_date
    emit(
        _format_log_event(
            "source_step_start",
            source=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            module=spec.get("module"),
            block=spec.get("block"),
            adapter=spec.get("adapter"),
            endpoint=spec.get("endpoint"),
            **request_context,
        )
    )


def _emit_source_status_log(emit: LivePlanLogEmitter, status: LiveSourceStatus) -> None:
    spec = SOURCE_DIAGNOSTIC_SPECS.get(status.source_key, {})
    emit(
        _format_log_event(
            "source_step_finish",
            source=status.source_key,
            temporal_slot=status.temporal_slot,
            temporal_policy=status.temporal_policy,
            column_date=status.column_date,
            module=spec.get("module"),
            block=spec.get("block"),
            adapter=spec.get("adapter"),
            endpoint=spec.get("endpoint"),
            kind=status.kind,
            freshness=status.freshness,
            snapshot_date=status.snapshot_date,
            date=status.date,
            date_from=status.date_from,
            date_to=status.date_to,
            requested_count=status.requested_count,
            covered_count=status.covered_count,
            missing_count=len(status.missing_nm_ids),
            missing_nm_ids=_format_missing_nm_ids(status.missing_nm_ids[:20]),
            note=status.note,
        )
    )


def _emit_metric_batch_logs(
    emit: LivePlanLogEmitter,
    *,
    displayed_metrics: list[MetricV2Item],
    data_rows: list[list[Any]],
    temporal_slots: list[SheetVitrinaV1TemporalSlot],
) -> None:
    metric_meta = {item.metric_key: item for item in displayed_metrics}
    summaries: dict[str, dict[str, Any]] = {}
    for row in data_rows:
        if len(row) < 2:
            continue
        key = str(row[1] or "")
        if "|" not in key:
            continue
        scope_token, metric_key = key.split("|", 1)
        metric = metric_meta.get(metric_key)
        summary = summaries.setdefault(
            metric_key,
            {
                "scope": getattr(metric, "scope", ""),
                "section": getattr(metric, "section", ""),
                "label_ru": getattr(metric, "label_ru", ""),
                "rows": 0,
                "non_zero": 0,
                "zero": 0,
                "blank": 0,
                "text": 0,
                "row_scopes": set(),
            },
        )
        summary["rows"] += 1
        summary["row_scopes"].add(scope_token.split(":", 1)[0])
        for cell in row[2 : 2 + len(temporal_slots)]:
            if cell in ("", None):
                summary["blank"] += 1
                continue
            if isinstance(cell, (int, float)):
                if float(cell) == 0.0:
                    summary["zero"] += 1
                else:
                    summary["non_zero"] += 1
                continue
            summary["text"] += 1

    for metric_key in sorted(summaries):
        summary = summaries[metric_key]
        blocked = (
            metric_key in BLOCKED_SOURCE_METRIC_KEYS
            and summary["non_zero"] == 0
            and summary["zero"] == 0
            and summary["blank"] > 0
        )
        emit(
            _format_log_event(
                "metric_batch_result",
                metric_key=metric_key,
                label_ru=summary["label_ru"],
                scope=summary["scope"],
                section=summary["section"],
                row_scopes=",".join(sorted(summary["row_scopes"])),
                rows=summary["rows"],
                slot_cells=summary["rows"] * len(temporal_slots),
                non_zero=summary["non_zero"],
                zero=summary["zero"],
                blank=summary["blank"],
                text=summary["text"],
                blocked=blocked,
                blocked_source="promo_by_price" if blocked else "",
            )
        )


def _noop_live_plan_log(_: str) -> None:
    return


def _load_json(path: Path) -> Mapping[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
