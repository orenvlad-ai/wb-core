"""Heavy refresh-builder для ready snapshot sheet_vitrina_v1 по uploaded compact registry package."""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Protocol

from packages.adapters.ads_bids_block import HttpBackedAdsBidsSource
from packages.adapters.ads_compact_block import HttpBackedAdsCompactSource
from packages.adapters.fin_report_daily_block import HttpBackedFinReportDailySource
from packages.adapters.prices_snapshot_block import HttpBackedPricesSnapshotSource
from packages.adapters.sales_funnel_history_block import HttpBackedSalesFunnelHistorySource
from packages.adapters.seller_funnel_snapshot_block import HttpBackedSellerFunnelSnapshotSource
from packages.adapters.sf_period_block import HttpBackedSfPeriodSource
from packages.adapters.spp_block import HttpBackedSppSource
from packages.adapters.stocks_block import HistoricalCsvBackedStocksSource
from packages.adapters.web_source_current_sync import ShellBackedWebSourceCurrentSync
from packages.adapters.web_source_snapshot_block import HttpBackedWebSourceSnapshotSource
from packages.application.ads_bids_block import AdsBidsBlock
from packages.application.ads_compact_block import AdsCompactBlock
from packages.application.fin_report_daily_block import FinReportDailyBlock
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
from packages.application.sheet_vitrina_v1_temporal_policy import (
    CANONICAL_SOURCE_TEMPORAL_POLICIES,
    TEMPORAL_POLICY_YESTERDAY_CLOSED_ONLY,
    source_policy_supports_slot as _canonical_source_policy_supports_slot,
)
from packages.application.spp_block import SppBlock
from packages.application.stocks_block import StocksBlock
from packages.application.web_source_snapshot_block import WebSourceSnapshotBlock
from packages.business_time import (
    business_date_from_timestamp,
    business_datetime_for_override,
    current_business_date_iso,
    default_business_as_of_date,
)
from packages.contracts.cost_price_upload import CostPriceCurrentState, CostPriceRow
from packages.contracts.ads_bids_block import AdsBidsRequest
from packages.contracts.ads_compact_block import AdsCompactRequest
from packages.contracts.fin_report_daily_block import FinReportDailyRequest
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
HISTORICAL_CLOSED_DAY_SOURCE_KEYS = STRICT_CLOSED_DAY_SOURCE_KEYS | {
    "sales_funnel_history",
    "sf_period",
    "spp",
    "stocks",
    "ads_compact",
    "fin_report_daily",
}
CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS = {"prices_snapshot", "ads_bids", "promo_by_price"}
CURRENT_SNAPSHOT_ONLY_ROLLOVER_SOURCE_KEYS = {"prices_snapshot", "ads_bids"}
ACCEPTED_CURRENT_SOURCE_KEYS = HISTORICAL_CLOSED_DAY_SOURCE_KEYS | CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS
EXACT_DATE_RUNTIME_CACHE_SOURCE_KEYS = {"sales_funnel_history", "stocks", "promo_by_price"}
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
}
SOURCE_CLASSIFICATION_GROUPS = {
    "seller_funnel_snapshot": "A_bot_web_source_historical_closed_day_capable",
    "web_source_snapshot": "A_bot_web_source_historical_closed_day_capable",
    "sales_funnel_history": "B_wb_api_date_period_capable",
    "sf_period": "B_wb_api_date_period_capable",
    "spp": "B_wb_api_date_period_capable",
    "stocks": "B_wb_api_date_period_capable",
    "ads_compact": "B_wb_api_date_period_capable",
    "fin_report_daily": "B_wb_api_date_period_capable",
    "prices_snapshot": "C_wb_api_current_snapshot_only",
    "ads_bids": "C_wb_api_current_snapshot_only",
    "cost_price": "D_other_non_wb_or_blocked",
    "promo_by_price": "D_other_non_wb_or_browser_collector",
}
PERCENT_SOURCE_KEYS = {"ctr", "ctr_current", "localizationPercent"}
DECISION_SUMMARY = {
    "alias_zone": "openCount and open_card_count remain distinct metrics from different sources",
    "total_avg_policy": "preserve all total_/avg_ uploaded rows; total_=sum, avg_=arithmetic_mean",
    "section_dictionary": "uploaded section values are authoritative and are not remapped",
    "config_service_values": "CONFIG!H:I service block is preserved across prepare/reprepare",
}
SOURCE_DIAGNOSTIC_SPECS = {
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
    ads_compact_lookup: dict[int, Any]
    fin_lookup: dict[int, Any]
    fin_storage_fee_total: float | None
    cost_price_lookup: dict[str, "ResolvedCostPrice"]
    promo_lookup: dict[int, dict[str, float]]


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
        ads_bids_block: AdsBidsBlock | None = None,
        stocks_block: StocksBlock | None = None,
        ads_compact_block: AdsCompactBlock | None = None,
        fin_report_daily_block: FinReportDailyBlock | None = None,
        promo_live_source_block: PromoLiveSourceProtocol | None = None,
        current_web_source_sync: CurrentWebSourceSync | None = None,
        closed_day_web_source_sync: ClosedDayWebSourceSync | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime = runtime
        self.web_source_block = web_source_block or WebSourceSnapshotBlock(HttpBackedWebSourceSnapshotSource())
        self.seller_funnel_block = seller_funnel_block or SellerFunnelSnapshotBlock(HttpBackedSellerFunnelSnapshotSource())
        self.sales_funnel_history_block = sales_funnel_history_block or SalesFunnelHistoryBlock(HttpBackedSalesFunnelHistorySource())
        self.prices_snapshot_block = prices_snapshot_block or PricesSnapshotBlock(HttpBackedPricesSnapshotSource())
        self.sf_period_block = sf_period_block or SfPeriodBlock(HttpBackedSfPeriodSource())
        self.spp_block = spp_block or SppBlock(HttpBackedSppSource())
        self.ads_bids_block = ads_bids_block or AdsBidsBlock(HttpBackedAdsBidsSource())
        self.stocks_block = stocks_block or StocksBlock(HistoricalCsvBackedStocksSource())
        self.ads_compact_block = ads_compact_block or AdsCompactBlock(HttpBackedAdsCompactSource())
        self.fin_report_daily_block = fin_report_daily_block or FinReportDailyBlock(HttpBackedFinReportDailySource())
        self.promo_live_source_block = promo_live_source_block or _SyntheticNoPromoLiveSourceBlock()
        self.current_web_source_sync = current_web_source_sync or ShellBackedWebSourceCurrentSync()
        self.closed_day_web_source_sync = closed_day_web_source_sync or self.current_web_source_sync
        self.now_factory = now_factory or _default_now_factory

    def build_plan(
        self,
        as_of_date: str | None = None,
        log: LivePlanLogEmitter | None = None,
        execution_mode: str = EXECUTION_MODE_AUTO_DAILY,
    ) -> SheetVitrinaV1Envelope:
        emit = log or _noop_live_plan_log
        current_state = self.runtime.load_current_state()
        effective_date = _resolve_as_of_date(as_of_date, now=self.now_factory())
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

        metrics_by_key = {item.metric_key: item for item in current_state.metrics_v2}
        formulas_by_id = {item.formula_id: item for item in current_state.formulas_v2}
        displayed_metrics = sorted(
            [item for item in current_state.metrics_v2 if item.enabled and item.show_in_data],
            key=lambda item: item.display_order,
        )
        if not displayed_metrics:
            raise ValueError("current registry metrics_v2 does not contain enabled show_in_data rows")
        emit(
            _format_log_event(
                "refresh_registry_state",
                enabled_config_count=len(enabled_config),
                displayed_metrics=len(displayed_metrics),
                formulas=len(current_state.formulas_v2),
            )
        )

        cost_price_state = _load_cost_price_current_state(self.runtime)
        emit(
            _format_log_event(
                "current_web_source_sync_start",
                source="current_day_web_source_sync",
                target_date=current_date,
                block="ShellBackedWebSourceCurrentSync",
            )
        )
        current_web_source_sync_note = self._sync_current_web_source_snapshot(current_date)
        emit(
            _format_log_event(
                "current_web_source_sync_finish",
                source="current_day_web_source_sync",
                target_date=current_date,
                status="error" if current_web_source_sync_note else "success",
                note=current_web_source_sync_note or "snapshot ensured or already materialized",
            )
        )
        live_sources = self._load_live_sources(
            enabled_config,
            temporal_slots,
            cost_price_state,
            current_web_source_sync_note=current_web_source_sync_note,
            execution_mode=execution_mode,
            log=emit,
        )
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
            rows = _build_metric_rows(metric, enabled_config, evaluator, temporal_slots)
            data_rows.extend(rows)
            scope_row_counts[metric.scope] = scope_row_counts.get(metric.scope, 0) + len(rows)
            section_row_counts[metric.section] = section_row_counts.get(metric.section, 0) + len(rows)
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
        return build_sheet_write_plan(
            delivery_bundle=delivery_bundle,
            data_layout=_load_json(DATA_LAYOUT_PATH),
            status_layout=_load_json(STATUS_LAYOUT_PATH),
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
    ) -> TemporalLiveSources:
        emit = log or _noop_live_plan_log
        requested_nm_ids = [item.nm_id for item in enabled_config]
        requested_groups = sorted({item.group for item in enabled_config})
        statuses: list[LiveSourceStatus] = []
        slot_lookups: dict[str, SlotLookups] = {
            slot.slot_key: SlotLookups(
                seller_funnel_lookup={},
                history_lookup={},
                web_lookup={},
                prices_lookup={},
                sf_period_lookup={},
                spp_lookup={},
                ads_bids_lookup={},
                stocks_lookup={},
                ads_compact_lookup={},
                fin_lookup={},
                fin_storage_fee_total=None,
                cost_price_lookup={},
                promo_lookup={},
            )
            for slot in temporal_slots
        }

        for slot in temporal_slots:
            current_lookups = slot_lookups[slot.slot_key]
            for source_key, loader in [
                (
                    "seller_funnel_snapshot",
                    lambda slot=slot: self.seller_funnel_block.execute(
                        SellerFunnelSnapshotRequest(
                            snapshot_type="seller_funnel_snapshot",
                            date=slot.column_date,
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
                    gap_status = _build_temporal_gap_status(
                        source_key=source_key,
                        temporal_slot=slot.slot_key,
                        temporal_policy=temporal_policy,
                        column_date=slot.column_date,
                        requested_count=len(requested_nm_ids),
                    )
                    statuses.append(gap_status)
                    _emit_source_status_log(emit, gap_status)
                    continue
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
                elif source_key == "ads_bids":
                    current_lookups.ads_bids_lookup = _index_items_by_nm_id(payload)
                elif source_key == "stocks":
                    current_lookups.stocks_lookup = _index_items_by_nm_id(payload)
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
            cost_price_status, cost_price_lookup = _build_cost_price_status(
                current_state=cost_price_state,
                requested_groups=requested_groups,
                temporal_slot=slot.slot_key,
                column_date=slot.column_date,
            )
            slot_lookups[slot.slot_key].cost_price_lookup = cost_price_lookup
            statuses.append(cost_price_status)
            _emit_source_status_log(emit, cost_price_status)

        for source_key, note in BLOCKED_SOURCE_STATUSES.items():
            for slot in temporal_slots:
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
                _emit_source_request_log(
                    emit,
                    source_key=source_key,
                    temporal_slot=slot.slot_key,
                    temporal_policy="blocked",
                    column_date=slot.column_date,
                    requested_nm_ids=requested_nm_ids,
                )
                _emit_source_status_log(emit, blocked_status)

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
                    "current-snapshot-only yesterday_closed requires prior accepted current snapshot; "
                    "accepted current snapshot missing for requested closed day"
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

        cached_snapshot = self._load_cached_temporal_source(
            source_key=source_key,
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
            requested_nm_ids=requested_nm_ids,
            runtime_cache_note=_runtime_cache_note(source_key),
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
    ) -> tuple[LiveSourceStatus, Any, str | None] | None:
        cached_payload, cached_at = self.runtime.load_temporal_source_slot_snapshot(
            source_key=source_key,
            snapshot_date=column_date,
            snapshot_role=snapshot_role,
        )
        if cached_payload is None or not _is_exact_snapshot_payload(cached_payload, column_date):
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
    ) -> tuple[LiveSourceStatus, Any | None]:
        if prefer_cached_first:
            cached_result = self._load_cached_temporal_source(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
                runtime_cache_note=runtime_cache_note,
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
    ) -> tuple[LiveSourceStatus, Any | None] | None:
        cached_payload, cached_at = self.runtime.load_temporal_source_snapshot(
            source_key=source_key,
            snapshot_date=column_date,
        )
        if cached_payload is None or not _is_exact_snapshot_payload(cached_payload, column_date):
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
        if accepted_payload is not None and _is_exact_snapshot_payload(accepted_payload, column_date):
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
    ) -> None:
        self.enabled_config = enabled_config
        self.metrics_by_key = metrics_by_key
        self.formulas_by_id = formulas_by_id
        self.live_sources = live_sources
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
            if metric.metric_key.startswith(AGGREGATE_AVG_PREFIX):
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

    def _aggregate_avg(
        self,
        metric_key: str,
        config_items: Iterable[ConfigV2Item],
        temporal_slot: str,
    ) -> float | None:
        values = [self.resolve_sku(metric_key, item.nm_id, temporal_slot) for item in config_items]
        numeric = [value for value in values if value is not None]
        return float(sum(numeric)) / len(numeric) if numeric else None

    def _resolve_direct_sku(self, metric_key: str, nm_id: int, temporal_slot: str) -> float | None:
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
        if metric_key == "inventory_value_retail_rub":
            stock_total = self.resolve_sku("stock_total", nm_id, temporal_slot)
            price_seller_discounted = self.resolve_sku("price_seller_discounted", nm_id, temporal_slot)
            if stock_total is None or price_seller_discounted is None:
                return None
            return float(stock_total) * float(price_seller_discounted)

        slot_lookups = self._slot_lookups(temporal_slot)
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
    value = getattr(item, attribute, None)
    if value is None:
        return None
    return float(value) * scale


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
    if kind == "incomplete":
        missing_nm_ids = list(getattr(payload, "missing_nm_ids", []))
        requested_count = int(getattr(payload, "requested_count", len(requested_nm_ids)))
        covered_count = int(getattr(payload, "covered_count", 0))
        return (
            LiveSourceStatus(
                source_key=source_key,
                temporal_slot=temporal_slot,
                temporal_policy=temporal_policy,
                column_date=column_date,
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
            temporal_slot=temporal_slot,
            temporal_policy=temporal_policy,
            column_date=column_date,
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
        snapshot_date=str(getattr(payload, "snapshot_date", "") or ""),
        date=str(getattr(payload, "date", "") or ""),
        date_from=str(getattr(payload, "date_from", "") or ""),
        date_to=str(getattr(payload, "date_to", "") or ""),
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
    return _append_status_note(accepted_status, "; ".join(note_parts))


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


def _resolve_freshness(payload: Any) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(payload, field, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _source_policy_supports_slot(temporal_policy: str, temporal_slot: str) -> bool:
    return _canonical_source_policy_supports_slot(temporal_policy, temporal_slot)


def _format_temporal_source_key(source_key: str, temporal_slot: str) -> str:
    return f"{source_key}[{temporal_slot}]"


def _is_exact_snapshot_payload(payload: Any, column_date: str) -> bool:
    return str(getattr(payload, "kind", "")) == "success" and _resolve_freshness(payload) == column_date


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


def _numeric_payload_value(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


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
