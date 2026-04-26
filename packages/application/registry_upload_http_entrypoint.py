"""Application-слой HTTP entrypoint для registry upload и sheet_vitrina_v1 operator flow."""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import shlex
import threading
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from packages.application.factory_order_supply import FactoryOrderSupplyBlock
from packages.application.promo_live_source import PromoLiveSourceBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_daily_report import SheetVitrinaV1DailyReportBlock
from packages.application.sheet_vitrina_v1_load_bridge import (
    LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE,
    LegacyGoogleSheetsContourArchivedError,
    legacy_google_sheets_archive_context,
    load_sheet_vitrina_ready_snapshot_via_clasp,
)
from packages.application.sheet_vitrina_v1_plan_report import SheetVitrinaV1PlanReportBlock
from packages.application.sheet_vitrina_v1_research import SheetVitrinaV1ResearchBlock
from packages.application.sheet_vitrina_v1_stock_report import SheetVitrinaV1StockReportBlock
from packages.application.sheet_vitrina_v1_stock_report import list_active_sku_options
from packages.application.sheet_vitrina_v1_temporal_policy import (
    effective_source_temporal_policy,
    reduce_source_temporal_semantics,
    source_nonblocking_slot_reason,
    slot_counts_toward_source_status,
)
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.application.web_vitrina_gravity_table_adapter import (
    build_web_vitrina_gravity_table_adapter,
)
from packages.application.web_vitrina_page_composition import (
    build_web_vitrina_page_composition,
    build_web_vitrina_page_error_composition,
)
from packages.application.web_vitrina_view_model import build_web_vitrina_view_model
from packages.application.wb_regional_supply import WbRegionalSupplyBlock
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    DAILY_REFRESH_BUSINESS_HOURS,
    DAILY_REFRESH_SYSTEMD_UTC_ONCALENDAR,
    DAILY_REFRESH_SYSTEMD_UTC_TIME,
    current_business_date_iso,
    default_business_as_of_date,
    to_business_datetime,
)
from packages.application.sheet_vitrina_v1_live_plan import (
    BLOCKED_SOURCE_METRIC_KEYS,
    CLOSURE_PENDING_STATES,
    DELIVERY_CONTRACT_VERSION,
    EXECUTION_MODE_AUTO_DAILY,
    EXECUTION_MODE_MANUAL_OPERATOR,
    EXECUTION_MODE_PERSISTED_RETRY,
    HISTORICAL_CLOSED_DAY_SOURCE_KEYS,
    SOURCE_DIAGNOSTIC_SPECS,
    SheetVitrinaV1LivePlanBlock,
    CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS,
    TEMPORAL_SLOT_YESTERDAY_CLOSED,
    TEMPORAL_SLOT_TODAY_CURRENT,
)
from packages.contracts.cost_price_upload import CostPriceUploadResult
from packages.contracts.registry_upload_file_backed_service import RegistryUploadResult
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaWriteTarget

OperatorLogEmitter = Callable[[str], None]
SheetLoadRunner = Callable[[SheetVitrinaV1Envelope, OperatorLogEmitter], dict[str, Any]]
SHEET_OPERATOR_JOB_ID: ContextVar[str] = ContextVar("sheet_vitrina_v1_operator_job_id", default="")
SHEET_VITRINA_REFRESH_ROUTE = "/v1/sheet-vitrina-v1/refresh"
SHEET_VITRINA_LOAD_ROUTE = "/v1/sheet-vitrina-v1/load"
SHEET_VITRINA_GROUP_REFRESH_ROUTE = "/v1/sheet-vitrina-v1/web-vitrina/group-refresh"
SHEET_VITRINA_SELLER_RECOVERY_START_ROUTE = "/v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start"
SHEET_VITRINA_DAILY_TIMER_NAME = "wb-core-sheet-vitrina-refresh.timer"
SHEET_VITRINA_DAILY_AUTO_ACTION = "server-side refresh ready snapshot for website/operator web-vitrina"
SHEET_VITRINA_DAILY_BUSINESS_TIMES = ", ".join(
    f"{hour:02d}:00" for hour in DAILY_REFRESH_BUSINESS_HOURS
)
SHEET_VITRINA_DAILY_AUTO_DESCRIPTION = (
    f"Ежедневно в {SHEET_VITRINA_DAILY_BUSINESS_TIMES} {CANONICAL_BUSINESS_TIMEZONE_NAME}: "
    f"{SHEET_VITRINA_DAILY_AUTO_ACTION}"
)
SHEET_VITRINA_DAILY_TRIGGER_DESCRIPTION = (
    f"{SHEET_VITRINA_DAILY_TIMER_NAME} -> POST {SHEET_VITRINA_REFRESH_ROUTE} "
    f"(auto_refresh=true) в {SHEET_VITRINA_DAILY_BUSINESS_TIMES} {CANONICAL_BUSINESS_TIMEZONE_NAME}"
)
SHEET_VITRINA_RETRY_RUNNER_DESCRIPTION = (
    "Persisted retry runner: дожимает due yesterday_closed для historical/date-period families "
    "и same-day today_current только для WB API current-snapshot-only families; manual refresh такие хвосты не создаёт."
)
WEB_VITRINA_ACTIVITY_TONE_RANK = {
    "error": 0,
    "warning": 1,
    "success": 2,
    "neutral": 3,
}
WEB_VITRINA_ACTIVITY_ITEM_COPY = {
    "seller_funnel_snapshot": {
        "label_ru": "Воронка продавца",
        "description_ru": "Показы карточки, открытия и базовая конверсия за дату.",
    },
    "sales_funnel_history": {
        "label_ru": "История продаж",
        "description_ru": "Заказы, выручка и конверсия WB за период.",
    },
    "web_source_snapshot": {
        "label_ru": "Поисковая аналитика",
        "description_ru": "Просмотры, CTR, заказы и средняя позиция в поиске.",
    },
    "prices_snapshot": {
        "label_ru": "Цены и скидки",
        "description_ru": "Текущие цены продавца и скидки по SKU.",
    },
    "sf_period": {
        "label_ru": "Периодная аналитика WB",
        "description_ru": "Локализация, рейтинг и другие периодные показатели WB.",
    },
    "spp": {
        "label_ru": "СПП",
        "description_ru": "Скидка постоянного покупателя на выбранную дату.",
    },
    "ads_bids": {
        "label_ru": "Ставки рекламы",
        "description_ru": "Ставки в поиске и рекомендациях по SKU.",
    },
    "stocks": {
        "label_ru": "Остатки по складам",
        "description_ru": "История остатков и суммарный stock по складам.",
    },
    "ads_compact": {
        "label_ru": "Рекламная статистика",
        "description_ru": "Просмотры, клики, заказы и расход по рекламе.",
    },
    "fin_report_daily": {
        "label_ru": "Финансовый отчёт",
        "description_ru": "Выкупы, доставка, комиссии и хранение за дату.",
    },
    "cost_price": {
        "label_ru": "Себестоимость",
        "description_ru": "Себестоимость из текущего загруженного bundle.",
    },
    "promo_by_price": {
        "label_ru": "Промо и акции",
        "description_ru": "Промо-показатели из browser-collected promo source.",
    },
}
WEB_VITRINA_SOURCE_METRIC_KEYS = {
    "seller_funnel_snapshot": (
        "total_view_count",
        "total_open_card_count",
        "view_count",
        "ctr",
        "open_card_count",
    ),
    "sales_funnel_history": (
        "total_orderCount",
        "total_orderSum",
        "total_openCount",
        "avg_addToCartConversion",
        "total_cartCount",
        "avg_cartToOrderConversion",
        "total_addToWishlistCount",
        "avg_buyoutPercent",
        "orderCount",
        "orderSum",
        "openCount",
        "addToCartConversion",
        "cartCount",
        "cartToOrderConversion",
        "addToWishlistCount",
        "buyoutPercent",
    ),
    "web_source_snapshot": (
        "total_views_current",
        "avg_ctr_current",
        "total_orders_current",
        "avg_position_avg",
        "views_current",
        "ctr_current",
        "orders_current",
        "position_avg",
    ),
    "prices_snapshot": (
        "avg_price_seller_discounted",
        "price_seller_discounted",
        "price_seller",
    ),
    "sf_period": (
        "avg_localizationPercent",
        "localizationPercent",
        "feedbackRating",
    ),
    "spp": (
        "avg_spp",
        "spp",
    ),
    "ads_bids": (
        "avg_ads_bid_search",
        "ads_bid_search",
        "ads_bid_recommendations",
    ),
    "stocks": (
        "total_stock_total",
        "total_stock_ru_central",
        "total_stock_ru_northwest",
        "total_stock_ru_volga",
        "total_stock_ru_south_caucasus",
        "total_stock_ru_ural",
        "total_stock_ru_far_siberia",
        "stock_total",
        "stock_ru_central",
        "stock_ru_northwest",
        "stock_ru_volga",
        "stock_ru_south_caucasus",
        "stock_ru_ural",
        "stock_ru_far_siberia",
    ),
    "ads_compact": (
        "ads_drr_total",
        "ads_drr_attributed_total",
        "avg_ads_cpc",
        "avg_ads_ctr",
        "avg_ads_cr",
        "total_ads_views",
        "total_ads_clicks",
        "total_ads_atbs",
        "total_ads_orders",
        "total_ads_sum",
        "total_ads_sum_price",
        "ads_drr",
        "ads_drr_attributed",
        "ads_cpc",
        "ads_ctr",
        "ads_cr",
        "ads_views",
        "ads_clicks",
        "ads_atbs",
        "ads_orders",
        "ads_sum",
        "ads_sum_price",
    ),
    "fin_report_daily": (
        "total_fin_buyout_rub",
        "total_fin_delivery_rub",
        "total_fin_commission_wb_portal",
        "total_fin_acquiring_fee",
        "total_fin_loyalty_rub",
        "fin_storage_fee_total",
        "fin_buyout_rub",
        "fin_delivery_rub",
        "fin_commission_wb_portal",
        "fin_acquiring_fee",
        "fin_loyalty_rub",
    ),
    "cost_price": (
        "avg_cost_price_rub",
        "cost_price_rub",
        "proxy_margin_pct_total",
        "total_proxy_profit_rub",
        "proxy_margin_pct",
        "proxy_profit_rub",
    ),
    "promo_by_price": (
        "total_promo_participation",
        "total_promo_count_by_price",
        "avg_promo_entry_price_best",
        "promo_participation",
        "promo_count_by_price",
        "promo_entry_price_best",
    ),
}
WEB_VITRINA_SOURCE_GROUPS = {
    "wb_api": {
        "label_ru": "WB API",
        "source_keys": (
            "sales_funnel_history",
            "sf_period",
            "spp",
            "stocks",
            "ads_compact",
            "fin_report_daily",
            "prices_snapshot",
            "ads_bids",
        ),
    },
    "seller_portal_bot": {
        "label_ru": "Seller Portal / бот",
        "source_keys": (
            "seller_funnel_snapshot",
            "web_source_snapshot",
            "promo_by_price",
        ),
    },
    "other_sources": {
        "label_ru": "Прочие источники",
        "source_keys": (
            "cost_price",
        ),
    },
}
WEB_VITRINA_SOURCE_GROUP_ORDER = ("wb_api", "seller_portal_bot", "other_sources")
WEB_VITRINA_SOURCE_KEY_TO_GROUP = {
    source_key: group_id
    for group_id, group in WEB_VITRINA_SOURCE_GROUPS.items()
    for source_key in group["source_keys"]
}
WEB_VITRINA_OTHER_SOURCES_DERIVED_METRIC_KEYS = (
    "proxy_margin_pct_total",
    "total_proxy_profit_rub",
    "proxy_margin_pct",
    "proxy_profit_rub",
)


class SellerPortalRecoveryController:
    """Thin wrapper around the repo-owned seller relogin tool."""

    def __init__(
        self,
        *,
        config_factory: Callable[[], Any] | None = None,
        start_runner: Callable[[Any, bool], dict[str, Any]] | None = None,
        status_reader: Callable[..., dict[str, Any]] | None = None,
        stop_runner: Callable[[Any], dict[str, Any]] | None = None,
        launcher_builder: Callable[[Any, str, str], tuple[bytes, str]] | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._start_runner = start_runner
        self._status_reader = status_reader
        self._stop_runner = stop_runner
        self._launcher_builder = launcher_builder

    def _tool(self) -> Any:
        return importlib.import_module("apps.seller_portal_relogin_session")

    def _config(self) -> Any:
        if self._config_factory is not None:
            return self._config_factory()
        tool = self._tool()
        return tool.load_relogin_session_config_from_env()

    def read_status(
        self,
        *,
        launcher_download_path: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        config = self._config()
        raw = (
            self._status_reader(config, False, requested_run_id=run_id)
            if self._status_reader is not None
            else self._tool().read_session_status(config, with_probe=False, requested_run_id=run_id)
        )
        running = bool(raw.get("running"))
        if not running:
            raw = (
                self._status_reader(config, True, requested_run_id=run_id)
                if self._status_reader is not None
                else self._tool().read_session_status(config, with_probe=True, requested_run_id=run_id)
            )
        return _build_seller_portal_recovery_payload(
            raw,
            config=config,
            launcher_download_path=launcher_download_path,
        )

    def start(
        self,
        *,
        replace: bool,
        launcher_download_path: str,
    ) -> dict[str, Any]:
        config = self._config()
        raw = (
            self._start_runner(config, replace)
            if self._start_runner is not None
            else self._tool().start_relogin_session(config, replace=replace)
        )
        if not bool(raw.get("running")):
            raw = (
                self._status_reader(config, True)
                if self._status_reader is not None
                else self._tool().read_session_status(config, with_probe=True)
            )
        return _build_seller_portal_recovery_payload(
            raw,
            config=config,
            launcher_download_path=launcher_download_path,
        )

    def stop(
        self,
        *,
        launcher_download_path: str,
    ) -> dict[str, Any]:
        config = self._config()
        raw = dict(
            (
            self._stop_runner(config)
            if self._stop_runner is not None
            else self._tool().stop_relogin_session(config)
            )
            or {}
        )
        probe_payload = (
            self._status_reader(config, True)
            if self._status_reader is not None
            else self._tool().read_session_status(config, with_probe=True)
        )
        if isinstance(probe_payload, Mapping):
            raw["current_storage_probe"] = (
                dict(probe_payload.get("current_storage_probe") or {})
                if isinstance(probe_payload.get("current_storage_probe"), Mapping)
                else probe_payload.get("current_storage_probe")
            )
            if isinstance(probe_payload.get("supplier_context"), Mapping):
                raw["supplier_context"] = dict(probe_payload.get("supplier_context") or {})
        return _build_seller_portal_recovery_payload(
            raw,
            config=config,
            launcher_download_path=launcher_download_path,
        )

    def check_session(
        self,
        *,
        launcher_download_path: str,
    ) -> dict[str, Any]:
        config = self._config()
        raw = (
            self._status_reader(config, True)
            if self._status_reader is not None
            else self._tool().read_session_status(config, with_probe=True)
        )
        return _build_seller_portal_session_check_payload(
            raw,
            config=config,
            launcher_download_path=launcher_download_path,
        )

    def build_launcher_archive(
        self,
        *,
        public_status_url: str,
        public_operator_url: str,
    ) -> tuple[bytes, str]:
        config = self._config()
        if self._launcher_builder is not None:
            return self._launcher_builder(config, public_status_url, public_operator_url)
        return self._tool().build_macos_launcher_archive(
            config,
            public_status_url=public_status_url,
            public_operator_url=public_operator_url,
        )


class RegistryUploadHttpEntrypoint:
    """Тонкий entrypoint: ingest/update current truth, heavy refresh и cheap read готового snapshot."""

    def __init__(
        self,
        runtime_dir: Path,
        runtime: RegistryUploadDbBackedRuntime | None = None,
        activated_at_factory: Callable[[], str] | None = None,
        refreshed_at_factory: Callable[[], str] | None = None,
        now_factory: Callable[[], datetime] | None = None,
        sheet_load_runner: SheetLoadRunner | None = None,
        seller_portal_recovery_controller: SellerPortalRecoveryController | None = None,
    ) -> None:
        self.runtime = runtime or RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        self.activated_at_factory = activated_at_factory or _default_activated_at_factory
        self.refreshed_at_factory = refreshed_at_factory or _default_activated_at_factory
        self.now_factory = now_factory or _default_now_factory
        self._sheet_cycle_lock = threading.RLock()
        self.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
            runtime=self.runtime,
            promo_live_source_block=PromoLiveSourceBlock(runtime_dir=self.runtime.runtime_dir),
        )
        self.daily_report_block = SheetVitrinaV1DailyReportBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
        )
        self.stock_report_block = SheetVitrinaV1StockReportBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
        )
        self.plan_report_block = SheetVitrinaV1PlanReportBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
        )
        self.web_vitrina_block = SheetVitrinaV1WebVitrinaBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
        )
        self.research_block = SheetVitrinaV1ResearchBlock(
            runtime=self.runtime,
            web_vitrina_block=self.web_vitrina_block,
            now_factory=self.now_factory,
        )
        self.sheet_load_runner = sheet_load_runner or load_sheet_vitrina_ready_snapshot_via_clasp
        self.operator_jobs = SheetVitrinaV1OperatorJobStore(timestamp_factory=self.activated_at_factory)
        self.seller_portal_recovery = seller_portal_recovery_controller or SellerPortalRecoveryController()
        self.factory_order_supply_block = FactoryOrderSupplyBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
            timestamp_factory=self.activated_at_factory,
        )
        self.wb_regional_supply_block = WbRegionalSupplyBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
            timestamp_factory=self.activated_at_factory,
        )

    def handle_bundle_payload(self, payload: Mapping[str, Any]) -> RegistryUploadResult:
        return self.runtime.ingest_bundle(
            payload,
            activated_at=self.activated_at_factory(),
        )

    def handle_cost_price_payload(self, payload: Mapping[str, Any]) -> CostPriceUploadResult:
        return self.runtime.ingest_cost_price_payload(
            payload,
            activated_at=self.activated_at_factory(),
        )

    def handle_sheet_plan_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        return asdict(self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date))

    def handle_sheet_status_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        payload = asdict(self.runtime.load_sheet_vitrina_refresh_status(as_of_date=as_of_date))
        payload["technical_status"] = payload["status"]
        payload["status"] = payload["semantic_status"]
        payload["status_label"] = payload["semantic_label"]
        payload["status_reason"] = payload["semantic_reason"]
        payload["server_context"] = self.build_sheet_server_context()
        payload["manual_context"] = self.build_sheet_manual_context()
        payload["load_context"] = self.build_sheet_load_context()
        return payload

    def handle_sheet_daily_report_request(self) -> dict[str, Any]:
        return self.daily_report_block.build()

    def handle_sheet_stock_report_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        return self.stock_report_block.build(as_of_date=as_of_date)

    def handle_sheet_plan_report_request(
        self,
        *,
        period: str,
        plan_drr_pct: float,
        h1_buyout_plan_rub: float | None = None,
        h2_buyout_plan_rub: float | None = None,
        q1_buyout_plan_rub: float | None = None,
        q2_buyout_plan_rub: float | None = None,
        q3_buyout_plan_rub: float | None = None,
        q4_buyout_plan_rub: float | None = None,
        as_of_date: str | None = None,
        use_contract_start_date: bool = False,
        contract_start_date: str | None = None,
    ) -> dict[str, Any]:
        return self.plan_report_block.build(
            period=period,
            plan_drr_pct=plan_drr_pct,
            h1_buyout_plan_rub=h1_buyout_plan_rub,
            h2_buyout_plan_rub=h2_buyout_plan_rub,
            q1_buyout_plan_rub=q1_buyout_plan_rub,
            q2_buyout_plan_rub=q2_buyout_plan_rub,
            q3_buyout_plan_rub=q3_buyout_plan_rub,
            q4_buyout_plan_rub=q4_buyout_plan_rub,
            as_of_date=as_of_date,
            use_contract_start_date=use_contract_start_date,
            contract_start_date=contract_start_date,
        )

    def handle_sheet_plan_report_baseline_template_request(self) -> tuple[bytes, str]:
        return self.plan_report_block.build_baseline_template()

    def handle_sheet_plan_report_baseline_status_request(self) -> dict[str, Any]:
        return self.plan_report_block.build_baseline_status()

    def handle_sheet_plan_report_baseline_upload_request(
        self,
        workbook_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
    ) -> dict[str, Any]:
        return self.plan_report_block.upload_baseline(
            workbook_bytes,
            uploaded_filename=uploaded_filename,
            uploaded_content_type=uploaded_content_type,
        )

    def handle_sheet_web_vitrina_request(
        self,
        *,
        page_route: str,
        read_route: str,
        as_of_date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        return asdict(
            self.web_vitrina_block.build(
                page_route=page_route,
                read_route=read_route,
                as_of_date=as_of_date,
                date_from=date_from,
                date_to=date_to,
            )
        )

    def handle_sheet_web_vitrina_page_composition_request(
        self,
        *,
        page_route: str,
        read_route: str,
        operator_route: str,
        job_path: str = "/v1/sheet-vitrina-v1/job",
        as_of_date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        include_source_status: bool = False,
    ) -> dict[str, Any]:
        page_composition_started_perf = time.perf_counter()
        effective_as_of_date = as_of_date or default_business_as_of_date(self.now_factory())
        available_snapshot_dates = self.web_vitrina_block.list_readable_dates(descending=True)
        default_as_of_date = default_business_as_of_date(self.now_factory())
        selected_date_from = date_from
        selected_date_to = date_to
        try:
            if not as_of_date and not date_from and not date_to:
                seed_contract = self.web_vitrina_block.build(
                    page_route=page_route,
                    read_route=read_route,
                    as_of_date=None,
                    date_from=None,
                    date_to=None,
                )
                default_range = _default_web_vitrina_page_period(
                    seed_contract,
                    available_snapshot_dates=available_snapshot_dates,
                )
                if default_range is not None:
                    selected_date_from, selected_date_to = default_range
                    contract = self.web_vitrina_block.build(
                        page_route=page_route,
                        read_route=read_route,
                        as_of_date=None,
                        date_from=selected_date_from,
                        date_to=selected_date_to,
                    )
                else:
                    contract = seed_contract
            else:
                contract = self.web_vitrina_block.build(
                    page_route=page_route,
                    read_route=read_route,
                    as_of_date=as_of_date,
                    date_from=date_from,
                    date_to=date_to,
                )
            view_model = build_web_vitrina_view_model(contract)
            adapter = build_web_vitrina_gravity_table_adapter(view_model)
        except Exception as exc:
            activity_surface = (
                _web_vitrina_source_status_missing_snapshot_activity_surface(
                    requested_as_of_date=effective_as_of_date,
                    technical_detail=str(exc),
                    now=self.now_factory(),
                )
                if include_source_status and _is_ready_snapshot_missing_error(exc)
                else None
            )
            return _with_page_composition_diagnostics(
                build_web_vitrina_page_error_composition(
                    page_route=page_route,
                    read_route=read_route,
                    operator_route=operator_route,
                    as_of_date=effective_as_of_date,
                    error_message=str(exc),
                    available_snapshot_dates=available_snapshot_dates,
                    default_as_of_date=default_as_of_date,
                    selected_as_of_date=as_of_date,
                    selected_date_from=selected_date_from,
                    selected_date_to=selected_date_to,
                    activity_surface=activity_surface,
                ),
                started_perf=page_composition_started_perf,
                include_source_status=include_source_status,
            )

        source_status_snapshot_as_of_date = _web_vitrina_source_status_snapshot_as_of_date(contract)
        source_status_snapshot_id = _web_vitrina_source_status_snapshot_id(
            self.runtime,
            contract,
            snapshot_as_of_date=source_status_snapshot_as_of_date,
        )
        activity_surface = _web_vitrina_source_status_not_loaded_activity_surface(
            snapshot_as_of_date=source_status_snapshot_as_of_date,
            snapshot_id=source_status_snapshot_id,
            refreshed_at=str(contract.meta.refreshed_at),
            read_model=str(contract.status_summary.read_model),
        )
        if include_source_status:
            try:
                activity_surface = self._build_web_vitrina_activity_surface(
                    snapshot_as_of_date=source_status_snapshot_as_of_date,
                    snapshot_id=source_status_snapshot_id,
                    refreshed_at=str(contract.meta.refreshed_at),
                    read_model=str(contract.status_summary.read_model),
                    job_path=job_path,
                )
            except Exception as exc:  # pragma: no cover - bounded fallback
                if _is_ready_snapshot_missing_error(exc):
                    activity_surface = _web_vitrina_source_status_missing_snapshot_activity_surface(
                        requested_as_of_date=source_status_snapshot_as_of_date,
                        snapshot_as_of_date=source_status_snapshot_as_of_date,
                        technical_detail=str(exc),
                        now=self.now_factory(),
                    )
                else:
                    activity_surface = _empty_web_vitrina_activity_surface(
                        log_message=f"activity surface unavailable: {exc}",
                        upload_message=f"upload summary unavailable: {exc}",
                        update_message=f"update summary unavailable: {exc}",
                    )

        return _with_page_composition_diagnostics(
            build_web_vitrina_page_composition(
                page_route=page_route,
                read_route=read_route,
                operator_route=operator_route,
                available_snapshot_dates=available_snapshot_dates,
                selected_as_of_date=as_of_date,
                selected_date_from=selected_date_from,
                selected_date_to=selected_date_to,
                contract=contract,
                view_model=view_model,
                adapter=adapter,
                activity_surface=activity_surface,
            ),
            started_perf=page_composition_started_perf,
            include_source_status=include_source_status,
        )

    def handle_sheet_research_sku_group_comparison_options_request(
        self,
        *,
        page_route: str,
        read_route: str,
    ) -> dict[str, Any]:
        return self.research_block.build_sku_group_comparison_options(
            page_route=page_route,
            read_route=read_route,
        )

    def handle_sheet_research_sku_group_comparison_calculate_request(
        self,
        payload: Mapping[str, Any],
        *,
        page_route: str,
        read_route: str,
    ) -> dict[str, Any]:
        return self.research_block.calculate_sku_group_comparison(
            payload,
            page_route=page_route,
            read_route=read_route,
        )

    def handle_sheet_refresh_request(
        self,
        as_of_date: str | None = None,
        *,
        auto_load: bool = False,
    ) -> dict[str, Any]:
        if auto_load:
            return self._run_sheet_auto_update(as_of_date=as_of_date, log=None)
        return self._run_sheet_refresh(as_of_date=as_of_date, log=None)

    def handle_sheet_load_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        del as_of_date
        raise LegacyGoogleSheetsContourArchivedError(LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE)

    def start_sheet_refresh_job(
        self,
        as_of_date: str | None = None,
        *,
        auto_load: bool = False,
    ) -> dict[str, Any]:
        return self.operator_jobs.start(
            operation="auto_update" if auto_load else "refresh",
            runner=(
                (lambda log: self._run_sheet_auto_update(as_of_date=as_of_date, log=log))
                if auto_load
                else (lambda log: self._run_sheet_refresh(as_of_date=as_of_date, log=log))
            ),
        )

    def start_sheet_load_job(self, as_of_date: str | None = None) -> dict[str, Any]:
        del as_of_date
        raise LegacyGoogleSheetsContourArchivedError(LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE)

    def start_sheet_source_group_refresh_job(
        self,
        *,
        source_group_id: str,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        normalized_group_id = _normalize_source_group_id(source_group_id)
        now = self.now_factory()
        selected_as_of_date = _resolve_group_refresh_selected_date(as_of_date, now=now)
        available_dates = self.web_vitrina_block.list_readable_dates(descending=False)
        if selected_as_of_date not in set(available_dates):
            available_text = (
                f"{available_dates[0]}..{available_dates[-1]}"
                if available_dates
                else "нет доступных дат"
            )
            raise ValueError(
                f"Дата {selected_as_of_date} недоступна для обновления группы; "
                f"доступный период: {available_text}"
            )
        target_snapshot_as_of_date = _target_snapshot_as_of_date_for_group_refresh(
            selected_as_of_date,
            now=now,
        )
        return self.operator_jobs.start(
            operation="refresh_group",
            runner=lambda log: self._run_sheet_source_group_refresh(
                source_group_id=normalized_group_id,
                selected_as_of_date=selected_as_of_date,
                target_snapshot_as_of_date=target_snapshot_as_of_date,
                log=log,
            ),
        )

    def handle_sheet_operator_job_request(self, job_id: str) -> dict[str, Any]:
        return self.operator_jobs.get(job_id)

    def handle_sheet_operator_job_text_request(self, job_id: str) -> tuple[str, str]:
        return self.operator_jobs.get_text(job_id)

    def handle_seller_portal_recovery_status_request(
        self,
        *,
        launcher_download_path: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        return self.seller_portal_recovery.read_status(
            launcher_download_path=launcher_download_path,
            run_id=run_id,
        )

    def handle_seller_portal_recovery_start_request(
        self,
        *,
        launcher_download_path: str,
        replace: bool = True,
    ) -> dict[str, Any]:
        return self.seller_portal_recovery.start(
            replace=replace,
            launcher_download_path=launcher_download_path,
        )

    def handle_seller_portal_recovery_stop_request(
        self,
        *,
        launcher_download_path: str,
    ) -> dict[str, Any]:
        return self.seller_portal_recovery.stop(
            launcher_download_path=launcher_download_path,
        )

    def handle_seller_portal_recovery_launcher_request(
        self,
        *,
        public_status_url: str,
        public_operator_url: str,
    ) -> tuple[bytes, str]:
        return self.seller_portal_recovery.build_launcher_archive(
            public_status_url=public_status_url,
            public_operator_url=public_operator_url,
        )

    def handle_seller_portal_session_check_request(
        self,
        *,
        launcher_download_path: str,
    ) -> dict[str, Any]:
        return self.seller_portal_recovery.check_session(
            launcher_download_path=launcher_download_path,
        )

    def start_seller_portal_session_check_job(
        self,
        *,
        launcher_download_path: str,
    ) -> dict[str, Any]:
        return self.operator_jobs.start(
            operation="session_check",
            runner=lambda log: self._run_seller_portal_session_check(
                launcher_download_path=launcher_download_path,
                log=log,
            ),
        )

    def start_seller_portal_recovery_start_job(
        self,
        *,
        launcher_download_path: str,
        replace_existing: bool = True,
    ) -> dict[str, Any]:
        return self.operator_jobs.start(
            operation="session_recovery_start",
            runner=lambda log: self._run_seller_portal_recovery_start(
                launcher_download_path=launcher_download_path,
                replace_existing=replace_existing,
                log=log,
            ),
        )

    def _build_web_vitrina_activity_surface(
        self,
        *,
        snapshot_as_of_date: str,
        snapshot_id: str,
        refreshed_at: str,
        read_model: str,
        job_path: str,
    ) -> dict[str, Any]:
        refresh_status = self.runtime.load_sheet_vitrina_refresh_status(as_of_date=snapshot_as_of_date)
        latest_refresh_job = self.operator_jobs.latest_relevant_job(
            operations=("refresh", "auto_update", "refresh_group"),
            preferred_as_of_date=snapshot_as_of_date,
            strict_preferred_as_of_date=True,
        )
        latest_log_job = self.operator_jobs.latest_relevant_job(
            operations=("refresh", "auto_update", "refresh_group", "session_check", "session_recovery_start"),
            preferred_as_of_date=snapshot_as_of_date,
            strict_preferred_as_of_date=False,
        )
        upload_records = (
            _extract_upload_source_records_from_job(latest_refresh_job)
            if latest_refresh_job is not None
            else _extract_source_records_from_outcomes(refresh_status.source_outcomes)
        )
        update_records = _extract_source_records_from_outcomes(refresh_status.source_outcomes)
        shared_source_keys = _collect_activity_source_keys(upload_records, update_records)
        upload_source_keys = _ordered_activity_source_keys(shared_source_keys, upload_records)
        update_source_keys = _ordered_activity_source_keys(shared_source_keys, update_records)
        current_business_date = current_business_date_iso(self.now_factory())
        previous_business_date = default_business_as_of_date(self.now_factory())
        group_refresh_available_dates = self.web_vitrina_block.list_readable_dates(descending=False)
        group_refresh_default_date = _default_group_refresh_date(
            group_refresh_available_dates,
            preferred_date=current_business_date,
        )
        metric_labels_by_source = _build_activity_metric_labels_by_source(
            getattr(self.runtime.load_current_state(), "metrics_v2", [])
        )
        upload_summary = _build_web_vitrina_endpoint_summary_block(
            title="Загрузка данных",
            subtitle=(
                "Что вернули источники в последнем завершённом refresh."
                if latest_refresh_job is not None
                else "Transient refresh-log недоступен; показываем сохранённый итог по текущему срезу."
            ),
            records=upload_records,
            ordered_source_keys=upload_source_keys,
            empty_message=(
                "Последний завершённый refresh-run в памяти сервиса пока не найден. "
                "Показываем только сохранённый итог по текущему срезу."
            ),
            block_updated_at=(
                str(latest_refresh_job.get("finished_at") or latest_refresh_job.get("started_at") or "")
                if latest_refresh_job
                else refreshed_at
            ),
            block_detail=(
                f"job {latest_refresh_job.get('job_id', '')} · {str(latest_refresh_job.get('operation', 'refresh'))}"
                if latest_refresh_job
                else f"snapshot {snapshot_id} · as_of_date {snapshot_as_of_date} · {read_model}"
            ),
        )
        return {
            "log_block": _build_web_vitrina_log_block(
                latest_job=latest_log_job,
                job_path=job_path,
                persisted_refresh_status=refresh_status,
            ),
            "upload_summary": upload_summary,
            "loading_table": _build_web_vitrina_loading_table(
                upload_summary=upload_summary,
                today_date=current_business_date,
                yesterday_date=previous_business_date,
                available_dates=group_refresh_available_dates,
                default_refresh_date=group_refresh_default_date,
                metric_labels_by_source=metric_labels_by_source,
                group_last_updated_at=_source_group_last_updated_at_for_snapshot(
                    self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=snapshot_as_of_date),
                    fallback_updated_at=refreshed_at,
                ),
            ),
            "update_summary": _build_web_vitrina_endpoint_summary_block(
                title="Обновление данных",
                subtitle=(
                    "Сохранённый итог для текущего среза. Повторное открытие страницы перечитывает именно "
                    "это состояние и не запускает скрытую загрузку источников."
                ),
                records=update_records,
                ordered_source_keys=update_source_keys,
                empty_message="STATUS-строки для текущего среза пока не материализованы.",
                block_updated_at=refreshed_at,
                block_detail=f"snapshot {snapshot_id} · as_of_date {snapshot_as_of_date} · {read_model}",
            ),
        }

    def run_sheet_temporal_closure_retry_cycle(
        self,
        *,
        target_dates: list[str] | None = None,
        auto_load_visible: bool = True,
        log: OperatorLogEmitter | None = None,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        requested_dates = sorted({value for value in (target_dates or []) if value})
        default_visible_as_of_date = default_business_as_of_date(self.now_factory())
        current_business_date = current_business_date_iso(self.now_factory())
        due_closed_states = self.sheet_plan_block.list_due_closed_day_retries()
        due_current_states = self.sheet_plan_block.list_due_current_capture_retries(
            current_date=current_business_date
        )
        due_closed_dates = sorted({state.target_date for state in due_closed_states})
        scheduled_dates = sorted(set(requested_dates) | set(due_closed_dates))
        if due_current_states:
            scheduled_dates = sorted(set(scheduled_dates) | {default_visible_as_of_date})

        emit(
            _format_log_event(
                "closure_retry_cycle_start",
                requested_dates=",".join(requested_dates),
                due_closed_dates=",".join(due_closed_dates),
                due_current_capture_sources=",".join(sorted({state.source_key for state in due_current_states})),
                due_current_capture_date=current_business_date if due_current_states else "",
                scheduled_dates=",".join(scheduled_dates),
                historical_sources=",".join(sorted(HISTORICAL_CLOSED_DAY_SOURCE_KEYS)),
                current_capture_sources=",".join(sorted(CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS)),
            )
        )

        refresh_results: list[dict[str, Any]] = []
        with self._sheet_cycle_lock:
            for as_of_date in scheduled_dates:
                emit(
                    _format_log_event(
                        "closure_retry_refresh_start",
                        as_of_date=as_of_date,
                    )
                )
                refresh_payload = self._run_sheet_refresh(
                    as_of_date=as_of_date,
                    log=emit,
                    execution_mode=EXECUTION_MODE_PERSISTED_RETRY,
                )
                refresh_results.append(
                    {
                        "as_of_date": as_of_date,
                        "snapshot_id": refresh_payload["snapshot_id"],
                        "refreshed_at": refresh_payload["refreshed_at"],
                    }
                )

            load_result: dict[str, Any] | None = None
            if auto_load_visible and default_visible_as_of_date in scheduled_dates:
                emit(
                    _format_log_event(
                        "closure_retry_load_skipped",
                        as_of_date=default_visible_as_of_date,
                        reason="legacy_google_sheets_contour_archived",
                    )
                )

            closure_states = self.runtime.list_temporal_source_closure_states(
                source_keys=sorted(HISTORICAL_CLOSED_DAY_SOURCE_KEYS | CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS),
                states=sorted(CLOSURE_PENDING_STATES),
            )
            payload = {
                "status": "success",
                "operation": "temporal_closure_retry_cycle",
                "requested_dates": requested_dates,
                "due_closed_dates": due_closed_dates,
                "due_current_capture_date": current_business_date if due_current_states else "",
                "scheduled_dates": scheduled_dates,
                "refreshed_dates": refresh_results,
                "visible_load_result": load_result,
                "pending_closure_states": [
                    {
                        "source_key": state.source_key,
                        "target_date": state.target_date,
                        "slot_kind": state.slot_kind,
                        "state": state.state,
                        "attempt_count": state.attempt_count,
                        "next_retry_at": state.next_retry_at,
                        "last_reason": state.last_reason,
                        "accepted_at": state.accepted_at,
                    }
                    for state in closure_states
                    if (
                        state.slot_kind == TEMPORAL_SLOT_YESTERDAY_CLOSED
                        or (
                            state.slot_kind == TEMPORAL_SLOT_TODAY_CURRENT
                            and state.target_date == current_business_date
                        )
                    )
                ],
                "server_context": self.build_sheet_server_context(),
                "manual_context": self.build_sheet_manual_context(),
            }
        emit(
            _format_log_event(
                "closure_retry_cycle_finish",
                scheduled_dates=",".join(scheduled_dates),
                refreshed=len(refresh_results),
                loaded_visible=str(bool(load_result)).lower(),
                pending_states=len(payload["pending_closure_states"]),
            )
        )
        return payload

    def handle_factory_order_status_request(self) -> dict[str, Any]:
        payload = asdict(self.factory_order_supply_block.build_status())
        payload["recommendation_download_path"] = "/v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx"
        return payload

    def handle_factory_order_template_request(self, dataset_type: str) -> tuple[bytes, str]:
        return self.factory_order_supply_block.build_template(dataset_type)

    def handle_factory_order_upload_request(
        self,
        dataset_type: str,
        workbook_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
    ) -> dict[str, Any]:
        return asdict(
            self.factory_order_supply_block.upload_dataset(
                dataset_type,
                workbook_bytes,
                uploaded_filename=uploaded_filename,
                uploaded_content_type=uploaded_content_type,
            )
        )

    def handle_factory_order_uploaded_file_request(self, dataset_type: str) -> tuple[bytes, str, str]:
        return self.factory_order_supply_block.download_uploaded_dataset(dataset_type)

    def handle_factory_order_delete_request(self, dataset_type: str) -> dict[str, Any]:
        return asdict(self.factory_order_supply_block.delete_dataset(dataset_type))

    def handle_factory_order_calculate_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = asdict(self.factory_order_supply_block.calculate(payload))
        result["recommendation_download_path"] = "/v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx"
        return result

    def handle_factory_order_recommendation_request(self) -> tuple[bytes, str]:
        return self.factory_order_supply_block.download_recommendation()

    def handle_wb_regional_status_request(self) -> dict[str, Any]:
        return asdict(self.wb_regional_supply_block.build_status())

    def handle_wb_regional_calculate_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return asdict(self.wb_regional_supply_block.calculate(payload))

    def handle_wb_regional_district_recommendation_request(self, district_key: str) -> tuple[bytes, str]:
        return self.wb_regional_supply_block.download_district_recommendation(district_key)

    def _run_sheet_auto_update(
        self,
        *,
        as_of_date: str | None,
        log: OperatorLogEmitter | None,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        with self._sheet_cycle_lock:
            started_at = self.activated_at_factory()
            requested_as_of_date = _resolve_sheet_refresh_as_of_date(
                as_of_date,
                now=self.now_factory(),
            )
            self.runtime.mark_sheet_vitrina_auto_update_started(
                started_at=started_at,
                as_of_date=requested_as_of_date,
            )
            emit(
                _format_log_event(
                    "cycle_start",
                    cycle="auto_update",
                    route=SHEET_VITRINA_REFRESH_ROUTE,
                    requested_as_of_date=requested_as_of_date,
                    action="build_ready_snapshot_for_web_vitrina",
                    trigger=SHEET_VITRINA_DAILY_TIMER_NAME,
                    execution_mode=EXECUTION_MODE_AUTO_DAILY,
                )
            )
            refresh_payload: dict[str, Any] | None = None
            load_payload: dict[str, Any] | None = None
            try:
                refresh_payload = self._run_sheet_refresh(
                    as_of_date=requested_as_of_date,
                    log=emit,
                    execution_mode=EXECUTION_MODE_AUTO_DAILY,
                )
            except Exception as exc:
                finished_at = self.activated_at_factory()
                auto_result = _build_auto_update_result_payload(
                    refresh_payload=refresh_payload,
                    load_payload=load_payload,
                    technical_status="error",
                    finished_at=finished_at,
                    error=str(exc),
                )
                self.runtime.save_sheet_vitrina_auto_update_result(
                    started_at=started_at,
                    finished_at=finished_at,
                    status="error",
                    as_of_date=(
                        str(refresh_payload["as_of_date"])
                        if refresh_payload is not None
                        else requested_as_of_date
                    ),
                    snapshot_id=(
                        str(refresh_payload["snapshot_id"])
                        if refresh_payload is not None
                        else None
                    ),
                    refreshed_at=(
                        str(refresh_payload["refreshed_at"])
                        if refresh_payload is not None
                        else None
                    ),
                    error=str(exc),
                    result_payload=auto_result,
                )
                emit(
                    _format_log_event(
                        "cycle_finish",
                        cycle="auto_update",
                        status="error",
                        semantic_status=auto_result.get("semantic_status"),
                        semantic_reason=auto_result.get("semantic_reason"),
                        route=SHEET_VITRINA_REFRESH_ROUTE,
                        error=str(exc),
                    )
                )
                raise

            finished_at = self.activated_at_factory()
            auto_result = _build_auto_update_result_payload(
                refresh_payload=refresh_payload,
                load_payload=load_payload,
                technical_status="success",
                finished_at=finished_at,
                error=None,
            )
            self.runtime.save_sheet_vitrina_auto_update_result(
                started_at=started_at,
                finished_at=finished_at,
                status="success",
                as_of_date=str(refresh_payload["as_of_date"]),
                snapshot_id=str(refresh_payload["snapshot_id"]),
                refreshed_at=str(refresh_payload["refreshed_at"]),
                error=None,
                result_payload=auto_result,
            )
            emit(
                _format_log_event(
                    "cycle_finish",
                    cycle="auto_update",
                    status="success",
                    semantic_status=auto_result.get("semantic_status"),
                    semantic_reason=auto_result.get("semantic_reason"),
                    route=SHEET_VITRINA_REFRESH_ROUTE,
                    snapshot_id=refresh_payload["snapshot_id"],
                )
            )
            payload = dict(refresh_payload)
            payload["technical_status"] = str(payload.get("technical_status") or payload.get("status") or "success")
            payload["status"] = str(auto_result.get("semantic_status") or "warning")
            payload["status_label"] = str(auto_result.get("semantic_label") or "")
            payload["status_reason"] = str(auto_result.get("semantic_reason") or "")
            payload["semantic_status"] = str(auto_result.get("semantic_status") or "warning")
            payload["semantic_label"] = str(auto_result.get("semantic_label") or "")
            payload["semantic_tone"] = str(auto_result.get("semantic_tone") or "warning")
            payload["semantic_reason"] = str(auto_result.get("semantic_reason") or "")
            payload["auto_result"] = auto_result
            payload["operation"] = "auto_update"
            payload["auto_update_started_at"] = started_at
            payload["auto_update_finished_at"] = finished_at
            payload["server_context"] = self.build_sheet_server_context()
            payload["manual_context"] = self.build_sheet_manual_context()
            payload["load_context"] = self.build_sheet_load_context()
            return payload

    def _run_sheet_refresh(
        self,
        *,
        as_of_date: str | None,
        log: OperatorLogEmitter | None,
        execution_mode: str = EXECUTION_MODE_MANUAL_OPERATOR,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        with self._sheet_cycle_lock:
            refresh_started_at = self.activated_at_factory()
            refresh_started_perf = time.perf_counter()
            refresh_diagnostics = _new_operator_refresh_diagnostics(
                job_id=SHEET_OPERATOR_JOB_ID.get(),
                execution_mode=execution_mode,
                started_at=refresh_started_at,
            )
            try:
                requested_as_of_date = as_of_date or "default"
                resolve_phase = _start_operator_phase(
                    "resolve_effective_date",
                    started_at=self.activated_at_factory(),
                )
                effective_as_of_date = _resolve_sheet_refresh_as_of_date(
                    as_of_date,
                    now=self.now_factory(),
                )
                _finish_operator_phase(
                    refresh_diagnostics,
                    resolve_phase,
                    finished_at=self.activated_at_factory(),
                    status="success",
                )
                load_state_phase = _start_operator_phase(
                    "load_registry_state",
                    started_at=self.activated_at_factory(),
                )
                current_state = self.runtime.load_current_state()
                _finish_operator_phase(
                    refresh_diagnostics,
                    load_state_phase,
                    finished_at=self.activated_at_factory(),
                    status="success",
                )
                refresh_diagnostics["as_of_date"] = effective_as_of_date
                refresh_diagnostics["bundle_version"] = current_state.bundle_version
                emit(
                    _format_log_event(
                        "cycle_start",
                        cycle="refresh",
                        route=SHEET_VITRINA_REFRESH_ROUTE,
                        requested_as_of_date=requested_as_of_date,
                        effective_as_of_date=effective_as_of_date,
                        action="build_ready_snapshot_only",
                        execution_mode=execution_mode,
                    )
                )
                emit(
                    _format_log_event(
                        "bundle_selected",
                        cycle="refresh",
                        bundle_version=current_state.bundle_version,
                        activated_at=current_state.activated_at,
                    )
                )
                emit(
                    _format_log_event(
                        "refresh_build_start",
                        cycle="refresh",
                        route=SHEET_VITRINA_REFRESH_ROUTE,
                        step="server_build_plan",
                    )
                )
                build_plan_phase = _start_operator_phase(
                    "build_plan_total",
                    started_at=self.activated_at_factory(),
                )
                plan = self.sheet_plan_block.build_plan(
                    as_of_date=effective_as_of_date,
                    log=emit,
                    execution_mode=execution_mode,
                )
                _finish_operator_phase(
                    refresh_diagnostics,
                    build_plan_phase,
                    finished_at=self.activated_at_factory(),
                    status="success",
                )
                refresh_diagnostics = _merge_refresh_diagnostics(
                    refresh_diagnostics,
                    _refresh_diagnostics_from_plan(plan),
                )
                row_counts = _sheet_row_counts(plan)
                emit(
                    _format_log_event(
                        "refresh_snapshot_ready",
                        cycle="refresh",
                        snapshot_id=plan.snapshot_id,
                        plan_version=plan.plan_version,
                        as_of_date=plan.as_of_date,
                        date_columns=",".join(plan.date_columns),
                        data_rows=row_counts.get("DATA_VITRINA"),
                        status_rows=row_counts.get("STATUS"),
                    )
                )
                emit(
                    _format_log_event(
                        "refresh_runtime_save_start",
                        cycle="refresh",
                        runtime_store="sheet_vitrina_ready_snapshot",
                        snapshot_id=plan.snapshot_id,
                    )
                )
                refreshed_at = self.refreshed_at_factory()
                plan = _with_full_refresh_metadata(plan, refreshed_at=refreshed_at)
                save_snapshot_phase = _start_operator_phase(
                    "save_ready_snapshot",
                    started_at=self.activated_at_factory(),
                )
                refresh_result = self.runtime.save_sheet_vitrina_ready_snapshot(
                    current_state=current_state,
                    refreshed_at=refreshed_at,
                    plan=plan,
                )
                _finish_operator_phase(
                    refresh_diagnostics,
                    save_snapshot_phase,
                    finished_at=self.activated_at_factory(),
                    status="success",
                )
                refresh_outcome = _build_refresh_result_payload(refresh_result)
                save_operator_phase = _start_operator_phase(
                    "save_operator_state",
                    started_at=self.activated_at_factory(),
                )
                if execution_mode == EXECUTION_MODE_MANUAL_OPERATOR:
                    self.runtime.save_sheet_vitrina_manual_refresh_result(
                        result_payload=refresh_outcome,
                        refreshed_at=refresh_result.refreshed_at,
                    )
                    _finish_operator_phase(
                        refresh_diagnostics,
                        save_operator_phase,
                        finished_at=self.activated_at_factory(),
                        status="success",
                    )
                else:
                    _finish_operator_phase(
                        refresh_diagnostics,
                        save_operator_phase,
                        finished_at=self.activated_at_factory(),
                        status="skipped",
                        note_kind="non_manual_execution_mode",
                    )
                job_finalize_phase = _start_operator_phase(
                    "job_finalize",
                    started_at=self.activated_at_factory(),
                )
                payload = asdict(refresh_result)
                updated_cells = _updated_cells_for_plan(plan)
                payload["technical_status"] = payload["status"]
                payload["status_label"] = payload["semantic_label"]
                payload["status_reason"] = payload["semantic_reason"]
                payload["updated_cells"] = updated_cells
                payload["updated_cell_count"] = _count_updated_cells_by_status(updated_cells, "updated")
                payload["latest_confirmed_cell_count"] = _count_updated_cells_by_status(
                    updated_cells,
                    "latest_confirmed",
                )
                _finish_operator_phase(
                    refresh_diagnostics,
                    job_finalize_phase,
                    finished_at=self.activated_at_factory(),
                    status="success",
                )
                _complete_refresh_diagnostics(
                    refresh_diagnostics,
                    job_id=SHEET_OPERATOR_JOB_ID.get(),
                    execution_mode=execution_mode,
                    as_of_date=refresh_result.as_of_date,
                    bundle_version=refresh_result.bundle_version,
                    started_at=refresh_started_at,
                    finished_at=self.activated_at_factory(),
                    duration_ms=max(0, int(round((time.perf_counter() - refresh_started_perf) * 1000))),
                    semantic_status=refresh_result.semantic_status,
                    technical_status=refresh_result.status,
                )
                plan = _with_refresh_diagnostics_metadata(plan, refresh_diagnostics)
                refresh_result = self.runtime.save_sheet_vitrina_ready_snapshot(
                    current_state=current_state,
                    refreshed_at=refreshed_at,
                    plan=plan,
                )
                payload.update(asdict(refresh_result))
                payload["technical_status"] = payload["status"]
                payload["status_label"] = payload["semantic_label"]
                payload["status_reason"] = payload["semantic_reason"]
                payload["updated_cells"] = updated_cells
                payload["updated_cell_count"] = _count_updated_cells_by_status(updated_cells, "updated")
                payload["latest_confirmed_cell_count"] = _count_updated_cells_by_status(
                    updated_cells,
                    "latest_confirmed",
                )
                payload["refresh_diagnostics"] = refresh_diagnostics
                payload["server_context"] = self.build_sheet_server_context()
                payload["manual_context"] = self.build_sheet_manual_context()
                payload["load_context"] = self.build_sheet_load_context()
                emit(
                    _format_log_event(
                        "refresh_runtime_save_finish",
                        cycle="refresh",
                        snapshot_id=refresh_result.snapshot_id,
                        refreshed_at=refresh_result.refreshed_at,
                        data_rows=refresh_result.sheet_row_counts.get("DATA_VITRINA"),
                        status_rows=refresh_result.sheet_row_counts.get("STATUS"),
                        semantic_status=refresh_result.semantic_status,
                        semantic_reason=refresh_result.semantic_reason,
                        updated_cells=payload["updated_cell_count"],
                        latest_confirmed_cells=payload["latest_confirmed_cell_count"],
                        duration_ms=refresh_diagnostics.get("duration_ms"),
                    )
                )
                emit(
                    _format_log_event(
                        "cycle_finish",
                        cycle="refresh",
                        status="success",
                        semantic_status=refresh_result.semantic_status,
                        semantic_reason=refresh_result.semantic_reason,
                        route=SHEET_VITRINA_REFRESH_ROUTE,
                        snapshot_id=refresh_result.snapshot_id,
                    )
                )
                return payload
            except Exception as exc:
                finished_at = self.activated_at_factory()
                if execution_mode == EXECUTION_MODE_MANUAL_OPERATOR:
                    self.runtime.save_sheet_vitrina_manual_refresh_result(
                        result_payload=_build_refresh_error_payload(
                            requested_as_of_date=as_of_date,
                            finished_at=finished_at,
                            error=str(exc),
                        ),
                        refreshed_at=None,
                    )
                emit(
                    _format_log_event(
                        "cycle_finish",
                        cycle="refresh",
                        status="error",
                        semantic_status="error",
                        semantic_reason=str(exc),
                        route=SHEET_VITRINA_REFRESH_ROUTE,
                    )
                )
                raise

    def _run_sheet_source_group_refresh(
        self,
        *,
        source_group_id: str,
        selected_as_of_date: str,
        target_snapshot_as_of_date: str,
        log: OperatorLogEmitter | None,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        source_group = _source_group_config(source_group_id)
        source_keys = list(source_group["source_keys"])
        group_label = str(source_group["label_ru"])
        stage = "start"
        started_at = self.activated_at_factory()
        emit(
            _format_log_event(
                "group_refresh_start",
                source_group_id=source_group_id,
                source_group_label=group_label,
                as_of_date=selected_as_of_date,
                target_snapshot_as_of_date=target_snapshot_as_of_date,
                initiator="operator_ui",
                route=SHEET_VITRINA_GROUP_REFRESH_ROUTE,
                endpoints=",".join(source_keys),
            )
        )
        with self._sheet_cycle_lock:
            try:
                current_state = self.runtime.load_current_state()
                metric_keys = _metric_keys_for_source_keys(current_state.metrics_v2, source_keys=source_keys)
                if not metric_keys:
                    raise ValueError(f"source group {source_group_id!r} has no enabled web-vitrina metrics")
                stage = "source_fetch"
                emit(
                    _format_log_event(
                        "group_refresh_stage_start",
                        stage=stage,
                        source_group_id=source_group_id,
                        as_of_date=selected_as_of_date,
                        target_snapshot_as_of_date=target_snapshot_as_of_date,
                        source_keys=",".join(source_keys),
                        metric_keys=",".join(metric_keys),
                    )
                )
                partial_plan = self.sheet_plan_block.build_plan(
                    as_of_date=target_snapshot_as_of_date,
                    log=emit,
                    execution_mode=EXECUTION_MODE_MANUAL_OPERATOR,
                    source_keys=source_keys,
                    metric_keys=metric_keys,
                )
                emit(
                    _format_log_event(
                        "group_refresh_stage_finish",
                        stage=stage,
                        status="success",
                        snapshot_id=partial_plan.snapshot_id,
                        as_of_date=selected_as_of_date,
                        target_snapshot_as_of_date=target_snapshot_as_of_date,
                        partial_rows=_data_sheet_row_count(partial_plan),
                    )
                )

                stage = "prepare_materialize"
                emit(
                    _format_log_event(
                        "group_refresh_stage_start",
                        stage=stage,
                        source_group_id=source_group_id,
                        as_of_date=selected_as_of_date,
                        target_snapshot_as_of_date=target_snapshot_as_of_date,
                    )
                )
                previous_plan = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=target_snapshot_as_of_date)
                previous_status = self.runtime.load_sheet_vitrina_refresh_status(as_of_date=target_snapshot_as_of_date)
                refreshed_at = self.refreshed_at_factory()
                merged_plan, merge_summary = _merge_source_group_ready_snapshot(
                    previous_plan=previous_plan,
                    partial_plan=partial_plan,
                    source_group_id=source_group_id,
                    source_keys=source_keys,
                    metric_keys=metric_keys,
                    refreshed_at=refreshed_at,
                    previous_refreshed_at=previous_status.refreshed_at,
                    selected_as_of_date=selected_as_of_date,
                )
                emit(
                    _format_log_event(
                        "group_refresh_stage_finish",
                        stage=stage,
                        status="success",
                        as_of_date=selected_as_of_date,
                        target_snapshot_as_of_date=target_snapshot_as_of_date,
                        rows_updated=merge_summary["rows_updated"],
                        rows_preserved=merge_summary["rows_preserved"],
                        status_rows_updated=merge_summary["status_rows_updated"],
                        updated_cells=merge_summary["updated_cell_count"],
                        latest_confirmed_cells=merge_summary["latest_confirmed_cell_count"],
                    )
                )

                stage = "load_group_to_vitrina"
                emit(
                    _format_log_event(
                        "group_refresh_stage_start",
                        stage=stage,
                        source_group_id=source_group_id,
                        as_of_date=selected_as_of_date,
                        target_snapshot_as_of_date=target_snapshot_as_of_date,
                    )
                )
                refresh_result = self.runtime.save_sheet_vitrina_ready_snapshot(
                    current_state=current_state,
                    refreshed_at=refreshed_at,
                    plan=merged_plan,
                )
                refresh_outcome = _build_refresh_result_payload(refresh_result)
                self.runtime.save_sheet_vitrina_manual_refresh_result(
                    result_payload=refresh_outcome,
                    refreshed_at=refresh_result.refreshed_at,
                )
                emit(
                    _format_log_event(
                        "group_refresh_stage_finish",
                        stage=stage,
                        status="success",
                        snapshot_id=merged_plan.snapshot_id,
                        as_of_date=selected_as_of_date,
                        target_snapshot_as_of_date=target_snapshot_as_of_date,
                        rows_updated=merge_summary["rows_updated"],
                        rows_preserved=merge_summary["rows_preserved"],
                        updated_cells=merge_summary["updated_cell_count"],
                        latest_confirmed_cells=merge_summary["latest_confirmed_cell_count"],
                        untouched_groups=",".join(
                            group_id
                            for group_id in WEB_VITRINA_SOURCE_GROUP_ORDER
                            if group_id != source_group_id
                        ),
                    )
                )
                finished_at = self.activated_at_factory()
                duration_seconds = _duration_seconds(started_at, finished_at)
                payload = asdict(refresh_result)
                payload.update(
                    {
                        "operation": "refresh_group",
                        "source_group_id": source_group_id,
                        "source_group_label": group_label,
                        "selected_as_of_date": selected_as_of_date,
                        "target_snapshot_as_of_date": target_snapshot_as_of_date,
                        "source_keys": source_keys,
                        "metric_keys": metric_keys,
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "duration_seconds": duration_seconds,
                        "merge_summary": merge_summary,
                        "updated_cells": merge_summary["updated_cells"],
                        "updated_cell_count": merge_summary["updated_cell_count"],
                        "latest_confirmed_cell_count": merge_summary["latest_confirmed_cell_count"],
                        "technical_status": payload["status"],
                        "status_label": payload["semantic_label"],
                        "status_reason": payload["semantic_reason"],
                        "server_context": self.build_sheet_server_context(),
                        "manual_context": self.build_sheet_manual_context(),
                        "load_context": self.build_sheet_load_context(),
                    }
                )
                emit(
                    _format_log_event(
                        "group_refresh_finish",
                        status="success",
                        source_group_id=source_group_id,
                        as_of_date=selected_as_of_date,
                        target_snapshot_as_of_date=target_snapshot_as_of_date,
                        duration_seconds=duration_seconds,
                        rows_updated=merge_summary["rows_updated"],
                        rows_preserved=merge_summary["rows_preserved"],
                        updated_cells=merge_summary["updated_cell_count"],
                        latest_confirmed_cells=merge_summary["latest_confirmed_cell_count"],
                    )
                )
                return payload
            except Exception as exc:
                finished_at = self.activated_at_factory()
                emit(
                    _format_log_event(
                        "group_refresh_finish",
                        status="failed",
                        failed_stage=stage,
                        source_group_id=source_group_id,
                        as_of_date=selected_as_of_date,
                        target_snapshot_as_of_date=target_snapshot_as_of_date,
                        reason=str(exc),
                        duration_seconds=_duration_seconds(started_at, finished_at),
                    )
                )
                raise RuntimeError(f"failed at {stage}: {exc}") from exc

    def _run_seller_portal_session_check(
        self,
        *,
        launcher_download_path: str,
        log: OperatorLogEmitter | None,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        started_at = self.activated_at_factory()
        emit(_format_log_event("seller_session_check_start", initiator="operator_ui"))
        try:
            payload = self.handle_seller_portal_session_check_request(
                launcher_download_path=launcher_download_path,
            )
            finished_at = self.activated_at_factory()
            status = str(payload.get("status") or "")
            tone = str(payload.get("status_tone") or "")
            ok = tone == "success" or status == "session_valid_canonical"
            emit(
                _format_log_event(
                    "seller_session_check_finish",
                    result="success" if ok else "failed",
                    status=status,
                    reason=str(payload.get("summary") or payload.get("message") or ""),
                    checked_at=finished_at,
                    duration_seconds=_duration_seconds(started_at, finished_at),
                )
            )
            result = dict(payload)
            result.update(
                {
                    "operation": "session_check",
                    "checked_at": finished_at,
                    "status": "success" if ok else "failed",
                    "session_status": status,
                    "session_ok": ok,
                    "semantic_status": "success" if ok else "error",
                    "semantic_label": "Успешно" if ok else "Ошибка",
                    "semantic_tone": "success" if ok else "error",
                    "semantic_reason": str(payload.get("summary") or payload.get("message") or ""),
                }
            )
            return result
        except Exception as exc:
            finished_at = self.activated_at_factory()
            emit(
                _format_log_event(
                    "seller_session_check_finish",
                    result="failed",
                    reason=str(exc),
                    checked_at=finished_at,
                    duration_seconds=_duration_seconds(started_at, finished_at),
                )
            )
            raise

    def _run_seller_portal_recovery_start(
        self,
        *,
        launcher_download_path: str,
        replace_existing: bool,
        log: OperatorLogEmitter | None,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        started_at = self.activated_at_factory()
        emit(
            _format_log_event(
                "seller_recovery_start",
                initiator="operator_ui",
                route=SHEET_VITRINA_SELLER_RECOVERY_START_ROUTE,
                replace=str(bool(replace_existing)).lower(),
            )
        )
        try:
            payload = self.handle_seller_portal_recovery_start_request(
                launcher_download_path=launcher_download_path,
                replace=replace_existing,
            )
            finished_at = self.activated_at_factory()
            result = dict(payload)
            result.update(
                {
                    "operation": "session_recovery_start",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "duration_seconds": _duration_seconds(started_at, finished_at),
                    "semantic_status": "warning",
                    "semantic_label": str(payload.get("status_label") or "Запрошено"),
                    "semantic_tone": "warning",
                    "semantic_reason": str(payload.get("summary") or payload.get("message") or ""),
                }
            )
            emit(
                _format_log_event(
                    "seller_recovery_finish",
                    result="success",
                    run_status=str(payload.get("run_status") or payload.get("status") or ""),
                    running=bool(payload.get("running")),
                    reason=str(payload.get("summary") or payload.get("message") or ""),
                    duration_seconds=result["duration_seconds"],
                )
            )
            return result
        except Exception as exc:
            finished_at = self.activated_at_factory()
            emit(
                _format_log_event(
                    "seller_recovery_finish",
                    result="failed",
                    reason=str(exc),
                    duration_seconds=_duration_seconds(started_at, finished_at),
                )
            )
            raise

    def _run_sheet_load(
        self,
        *,
        as_of_date: str | None,
        log: OperatorLogEmitter | None,
        execution_mode: str = EXECUTION_MODE_MANUAL_OPERATOR,
    ) -> dict[str, Any]:
        del as_of_date, log, execution_mode
        raise LegacyGoogleSheetsContourArchivedError(LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE)

        emit = log or _noop_log
        with self._sheet_cycle_lock:
            previous_load_state = self.runtime.load_sheet_vitrina_load_state()
            plan: SheetVitrinaV1Envelope | None = None
            refresh_status = None
            row_counts: dict[str, int] = {}
            plan_fingerprint: str | None = None
            try:
                current_state = self.runtime.load_current_state()
                emit(
                    _format_log_event(
                        "cycle_start",
                        cycle="load",
                        route=SHEET_VITRINA_LOAD_ROUTE,
                        requested_as_of_date=as_of_date or "latest_bundle_snapshot",
                        action="write_prepared_snapshot_only",
                        execution_mode=execution_mode,
                    )
                )
                emit(
                    _format_log_event(
                        "bundle_selected",
                        cycle="load",
                        bundle_version=current_state.bundle_version,
                        activated_at=current_state.activated_at,
                    )
                )
                emit(
                    _format_log_event(
                        "snapshot_lookup_start",
                        cycle="load",
                        route=SHEET_VITRINA_LOAD_ROUTE,
                        requested_as_of_date=as_of_date or "latest",
                    )
                )
                plan = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date)
                refresh_status = self.runtime.load_sheet_vitrina_refresh_status(as_of_date=plan.as_of_date)
                row_counts = _sheet_row_counts(plan)
                plan_fingerprint = _plan_fingerprint(plan)
                emit(
                    _format_log_event(
                        "snapshot_lookup_finish",
                        cycle="load",
                        snapshot_id=plan.snapshot_id,
                        plan_version=plan.plan_version,
                        as_of_date=plan.as_of_date,
                        date_columns=",".join(plan.date_columns),
                        refreshed_at=refresh_status.refreshed_at,
                        data_rows=row_counts.get("DATA_VITRINA"),
                        status_rows=row_counts.get("STATUS"),
                        semantic_status=refresh_status.semantic_status,
                        semantic_reason=refresh_status.semantic_reason,
                    )
                )
                _emit_plan_status_sheet_log(plan, emit, cycle="load")
                _emit_plan_metric_sheet_log(plan, emit, cycle="load")
                emit(
                    _format_log_event(
                        "bridge_start",
                        cycle="load",
                        snapshot_id=plan.snapshot_id,
                        bridge_runner=getattr(self.sheet_load_runner, "__name__", self.sheet_load_runner.__class__.__name__),
                    )
                )
                bridge_result = self.sheet_load_runner(plan, emit)
                finished_at = self.activated_at_factory()
                load_outcome = _build_load_result_payload(
                    plan=plan,
                    refresh_status=refresh_status,
                    bridge_result=bridge_result,
                    previous_load_state=previous_load_state,
                    finished_at=finished_at,
                )
                self.runtime.save_sheet_vitrina_load_state(
                    loaded_at=finished_at,
                    snapshot_id=plan.snapshot_id,
                    as_of_date=plan.as_of_date,
                    refreshed_at=refresh_status.refreshed_at,
                    plan_fingerprint=plan_fingerprint,
                    result_payload=load_outcome,
                )
                if execution_mode == EXECUTION_MODE_MANUAL_OPERATOR:
                    self.runtime.save_sheet_vitrina_manual_load_result(
                        result_payload=load_outcome,
                        loaded_at=finished_at,
                    )
                _emit_bridge_result_log(bridge_result, emit, cycle="load")
                emit(
                    _format_log_event(
                        "cycle_finish",
                        cycle="load",
                        status="success",
                        semantic_status=load_outcome.get("semantic_status"),
                        semantic_reason=load_outcome.get("semantic_reason"),
                        route=SHEET_VITRINA_LOAD_ROUTE,
                        snapshot_id=plan.snapshot_id,
                        data_rows=row_counts.get("DATA_VITRINA"),
                        status_rows=row_counts.get("STATUS"),
                    )
                )
                payload = {
                    "status": "success",
                    "technical_status": "success",
                    "status_label": str(load_outcome.get("semantic_label") or ""),
                    "status_reason": str(load_outcome.get("semantic_reason") or ""),
                    "semantic_status": str(load_outcome.get("semantic_status") or "warning"),
                    "semantic_label": str(load_outcome.get("semantic_label") or ""),
                    "semantic_tone": str(load_outcome.get("semantic_tone") or "warning"),
                    "semantic_reason": str(load_outcome.get("semantic_reason") or ""),
                    "operation": "load",
                    "bundle_version": current_state.bundle_version,
                    "activated_at": current_state.activated_at,
                    "refreshed_at": refresh_status.refreshed_at,
                    "as_of_date": plan.as_of_date,
                    "date_columns": plan.date_columns,
                    "temporal_slots": [asdict(item) for item in plan.temporal_slots],
                    "snapshot_id": plan.snapshot_id,
                    "plan_version": plan.plan_version,
                    "sheet_row_counts": row_counts,
                    "bridge_result": bridge_result,
                    "load_result": load_outcome,
                }
                payload["server_context"] = self.build_sheet_server_context()
                payload["manual_context"] = self.build_sheet_manual_context()
                payload["load_context"] = self.build_sheet_load_context()
                return payload
            except Exception as exc:
                finished_at = self.activated_at_factory()
                load_error = _build_load_error_payload(
                    requested_as_of_date=as_of_date,
                    plan=plan,
                    refresh_status=refresh_status,
                    finished_at=finished_at,
                    error=str(exc),
                )
                self.runtime.save_sheet_vitrina_load_state(
                    loaded_at=finished_at,
                    snapshot_id=plan.snapshot_id if plan is not None else None,
                    as_of_date=(plan.as_of_date if plan is not None else as_of_date),
                    refreshed_at=(refresh_status.refreshed_at if refresh_status is not None else None),
                    plan_fingerprint=plan_fingerprint,
                    result_payload=load_error,
                )
                if execution_mode == EXECUTION_MODE_MANUAL_OPERATOR:
                    self.runtime.save_sheet_vitrina_manual_load_result(
                        result_payload=load_error,
                        loaded_at=None,
                    )
                emit(
                    _format_log_event(
                        "cycle_finish",
                        cycle="load",
                        status="error",
                        semantic_status="error",
                        semantic_reason=str(exc),
                        route=SHEET_VITRINA_LOAD_ROUTE,
                        snapshot_id=plan.snapshot_id if plan is not None else None,
                    )
                )
                raise

    def build_sheet_server_context(self) -> dict[str, Any]:
        now = self.now_factory()
        business_now = to_business_datetime(now).replace(microsecond=0).isoformat()
        auto_update_state = self.runtime.load_sheet_vitrina_auto_update_state()
        auto_result = _format_operator_result_payload(auto_update_state.last_run_result)
        return {
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "business_now": business_now,
            "default_as_of_date": default_business_as_of_date(now),
            "today_current_date": current_business_date_iso(now),
            "daily_refresh_business_time": f"{SHEET_VITRINA_DAILY_BUSINESS_TIMES} {CANONICAL_BUSINESS_TIMEZONE_NAME}",
            "daily_refresh_systemd_time": DAILY_REFRESH_SYSTEMD_UTC_TIME,
            "daily_refresh_systemd_oncalendar": DAILY_REFRESH_SYSTEMD_UTC_ONCALENDAR,
            "daily_auto_action": SHEET_VITRINA_DAILY_AUTO_ACTION,
            "daily_auto_description": SHEET_VITRINA_DAILY_AUTO_DESCRIPTION,
            "daily_auto_trigger_name": SHEET_VITRINA_DAILY_TIMER_NAME,
            "daily_auto_trigger_description": SHEET_VITRINA_DAILY_TRIGGER_DESCRIPTION,
            "retry_runner_description": SHEET_VITRINA_RETRY_RUNNER_DESCRIPTION,
            "last_auto_run_status": auto_update_state.last_run_status or "never",
            "last_auto_run_status_label": (
                str(auto_result.get("semantic_label") or "")
                if auto_result
                else _auto_update_status_label(auto_update_state.last_run_status)
            ),
            "last_auto_run_status_reason": (
                str(auto_result.get("semantic_reason") or "")
                if auto_result
                else (auto_update_state.last_run_error or "")
            ),
            "last_auto_run_technical_status_label": _auto_update_status_label(auto_update_state.last_run_status),
            "last_auto_run_time": _format_optional_business_timestamp(auto_update_state.last_run_started_at),
            "last_auto_run_finished_at": _format_optional_business_timestamp(auto_update_state.last_run_finished_at),
            "last_successful_auto_update_at": _format_optional_business_timestamp(
                auto_update_state.last_successful_auto_update_at
            ),
            "last_auto_run_error": auto_update_state.last_run_error or "",
            "last_auto_run_result": auto_result,
        }

    def build_sheet_manual_context(self) -> dict[str, Any]:
        manual_state = self.runtime.load_sheet_vitrina_manual_operator_state()
        return {
            "last_successful_manual_refresh_at": _format_optional_business_timestamp(
                manual_state.last_successful_manual_refresh_at
            ),
            "last_successful_manual_load_at": _format_optional_business_timestamp(
                manual_state.last_successful_manual_load_at
            ),
            "last_manual_refresh_result": _format_operator_result_payload(
                manual_state.last_manual_refresh_result
            ),
            "last_manual_load_result": _format_operator_result_payload(
                manual_state.last_manual_load_result
            ),
        }

    def build_sheet_load_context(self) -> dict[str, Any]:
        load_state = self.runtime.load_sheet_vitrina_load_state()
        return {
            "last_finished_at": _format_optional_business_timestamp(load_state.loaded_at),
            "last_snapshot_id": load_state.snapshot_id or "",
            "last_as_of_date": load_state.as_of_date or "",
            "last_refreshed_at": load_state.refreshed_at or "",
            "last_result": _format_operator_result_payload(load_state.result),
            "legacy_google_sheets_contour": legacy_google_sheets_archive_context(),
        }

    def build_sheet_operator_ui_context(self) -> dict[str, Any]:
        try:
            current_state = self.runtime.load_current_state()
        except (ValueError, sqlite3.Error):
            active_skus: list[dict[str, Any]] = []
        else:
            active_skus = list_active_sku_options(current_state.config_v2)
        return {
            "stock_report_active_skus": active_skus,
            "stock_report_active_sku_count": len(active_skus),
            "stock_report_active_sku_source": "current_registry_config_v2",
        }


def _build_seller_portal_recovery_payload(
    raw_payload: Mapping[str, Any] | None,
    *,
    config: Any,
    launcher_download_path: str,
) -> dict[str, Any]:
    raw = dict(raw_payload or {})
    current_probe = raw.get("current_storage_probe")
    current_probe_payload = dict(current_probe) if isinstance(current_probe, Mapping) else None
    supplier_context = _seller_portal_recovery_supplier_context(raw)
    expected_supplier_id = str(getattr(config, "canonical_supplier_id", "") or "").strip()
    expected_supplier_label = str(getattr(config, "canonical_supplier_label", "") or "").strip()
    canonical_configured = bool(expected_supplier_id)
    organization_confirmed = _seller_portal_recovery_context_matches_expected(
        supplier_context,
        expected_supplier_id=expected_supplier_id,
    )
    session_status = _seller_portal_session_check_status(
        current_probe=current_probe_payload,
        canonical_configured=canonical_configured,
        organization_confirmed=organization_confirmed,
    )
    run_status = _seller_portal_recovery_run_status(raw)
    summary, instruction = _seller_portal_recovery_copy(
        run_status,
        raw=raw,
        current_probe=current_probe_payload,
        canonical_configured=canonical_configured,
        organization_confirmed=organization_confirmed,
        session_status=session_status,
    )
    run_id = str(raw.get("run_id") or "").strip()
    current_run_id = str(raw.get("current_run_id") or "").strip() or run_id
    requested_run_id = str(raw.get("requested_run_id") or "").strip()
    requested_run_mismatch = bool(requested_run_id and current_run_id and requested_run_id != current_run_id)
    run_is_final = run_status in {"completed", "not_needed", "stopped", "timeout", "error"}
    return {
        "status": run_status,
        "status_label": _seller_portal_recovery_status_label(run_status),
        "status_tone": _seller_portal_recovery_status_tone(run_status),
        "run_status": run_status,
        "run_status_label": _seller_portal_recovery_status_label(run_status),
        "run_status_tone": _seller_portal_recovery_status_tone(run_status),
        "summary": summary,
        "instruction": instruction,
        "technical_line": _seller_portal_recovery_technical_line(
            expected_supplier_id=expected_supplier_id,
            expected_supplier_label=expected_supplier_label,
            supplier_context=supplier_context,
            launcher_ready=run_status == "awaiting_login",
        ),
        "raw_status": str(raw.get("status") or "").strip(),
        "running": bool(raw.get("running")),
        "can_start": (not bool(raw.get("running"))) and canonical_configured,
        "can_stop": bool(raw.get("running")) and run_status in {
            "starting",
            "awaiting_login",
            "saving_session",
            "validating_session",
            "checking_canonical_supplier",
            "triggering_refresh",
        },
        "launcher_enabled": bool(run_id) and run_status == "awaiting_login" and not requested_run_mismatch,
        "launcher_download_path": launcher_download_path,
        "updated_at": _format_optional_business_timestamp(str(raw.get("updated_at") or "") or None),
        "started_at": _format_optional_business_timestamp(str(raw.get("started_at") or "") or None),
        "deadline_at": _format_optional_business_timestamp(str(raw.get("deadline_at") or "") or None),
        "finished_at": _format_optional_business_timestamp(str(raw.get("finished_at") or "") or None),
        "run_id": run_id,
        "current_run_id": current_run_id,
        "requested_run_id": requested_run_id,
        "requested_run_mismatch": requested_run_mismatch,
        "run_is_final": run_is_final,
        "run_final_status": run_status if run_is_final else "",
        "run_final_label": _seller_portal_recovery_final_label(run_status) if run_is_final else "",
        "organization_confirmed": organization_confirmed if canonical_configured else None,
        "organization_switch_applied": bool(raw.get("organization_switch_applied")),
        "expected_supplier_id": expected_supplier_id,
        "expected_supplier_label": expected_supplier_label,
        "current_supplier_id": str(
            supplier_context.get("current_supplier_id")
            or supplier_context.get("analytics_supplier_id")
            or ""
        ),
        "current_supplier_external_id": str(supplier_context.get("current_supplier_external_id") or ""),
        "current_storage_probe": current_probe_payload,
        "session_status": session_status,
        "session_status_label": _seller_portal_session_check_status_label(session_status),
        "session_status_tone": _seller_portal_session_check_status_tone(session_status),
        "message": str(raw.get("message") or "").strip(),
        "run_failure_code": _seller_portal_recovery_failure_code(raw),
    }


def _build_seller_portal_session_check_payload(
    raw_payload: Mapping[str, Any] | None,
    *,
    config: Any,
    launcher_download_path: str,
) -> dict[str, Any]:
    raw = dict(raw_payload or {})
    current_probe = raw.get("current_storage_probe")
    current_probe_payload = dict(current_probe) if isinstance(current_probe, Mapping) else None
    supplier_context = _seller_portal_recovery_supplier_context(raw)
    expected_supplier_id = str(getattr(config, "canonical_supplier_id", "") or "").strip()
    expected_supplier_label = str(getattr(config, "canonical_supplier_label", "") or "").strip()
    canonical_configured = bool(expected_supplier_id)
    organization_confirmed = _seller_portal_recovery_context_matches_expected(
        supplier_context,
        expected_supplier_id=expected_supplier_id,
    )
    status = _seller_portal_session_check_status(
        current_probe=current_probe_payload,
        canonical_configured=canonical_configured,
        organization_confirmed=organization_confirmed,
    )
    summary, instruction = _seller_portal_session_check_copy(
        status,
        canonical_configured=canonical_configured,
    )
    return {
        "status": status,
        "status_label": _seller_portal_session_check_status_label(status),
        "status_tone": _seller_portal_session_check_status_tone(status),
        "summary": summary,
        "instruction": instruction,
        "technical_line": _seller_portal_recovery_technical_line(
            expected_supplier_id=expected_supplier_id,
            expected_supplier_label=expected_supplier_label,
            supplier_context=supplier_context,
            launcher_ready=False,
        ),
        "raw_status": str(raw.get("status") or "").strip(),
        "running": False,
        "can_start": canonical_configured,
        "can_stop": False,
        "launcher_enabled": False,
        "launcher_download_path": launcher_download_path,
        "updated_at": _format_optional_business_timestamp(str(raw.get("updated_at") or "") or None),
        "started_at": "",
        "deadline_at": "",
        "finished_at": "",
        "organization_confirmed": (
            organization_confirmed
            if canonical_configured and current_probe_payload is not None and bool(current_probe_payload.get("ok"))
            else None
        ),
        "organization_switch_applied": False,
        "expected_supplier_id": expected_supplier_id,
        "expected_supplier_label": expected_supplier_label,
        "current_supplier_id": str(
            supplier_context.get("current_supplier_id")
            or supplier_context.get("analytics_supplier_id")
            or ""
        ),
        "current_supplier_external_id": str(supplier_context.get("current_supplier_external_id") or ""),
        "current_storage_probe": current_probe_payload,
        "message": str(raw.get("message") or "").strip(),
    }


def _seller_portal_recovery_supplier_context(raw: Mapping[str, Any]) -> dict[str, Any]:
    for value in (
        raw.get("current_storage_probe"),
        raw.get("last_probe"),
        raw.get("supplier_context"),
    ):
        if isinstance(value, Mapping) and isinstance(value.get("supplier_context"), Mapping):
            return dict(value.get("supplier_context") or {})
        if isinstance(value, Mapping) and any(
            key in value
            for key in ("current_supplier_id", "current_supplier_external_id", "analytics_supplier_id")
        ):
            return dict(value)
    return {}


def _seller_portal_recovery_context_matches_expected(
    supplier_context: Mapping[str, Any],
    *,
    expected_supplier_id: str,
) -> bool:
    expected = str(expected_supplier_id or "").strip()
    if not expected:
        return False
    unique_ids = {
        str(value or "").strip()
        for value in (
            supplier_context.get("current_supplier_id"),
            supplier_context.get("current_supplier_external_id"),
            supplier_context.get("analytics_supplier_id"),
        )
        if str(value or "").strip()
    }
    return bool(unique_ids) and unique_ids == {expected}


def _seller_portal_recovery_run_status(raw: Mapping[str, Any]) -> str:
    raw_status = str(raw.get("status") or "").strip()
    normalized = {
        "starting_visual_session": "starting",
        "auth_confirmed": "triggering_refresh",
        "success": "completed",
        "refresh_failed": "error",
        "wrong_organization": "error",
    }.get(raw_status, raw_status)
    if normalized in {
        "starting",
        "awaiting_login",
        "saving_session",
        "validating_session",
        "checking_canonical_supplier",
        "triggering_refresh",
        "completed",
        "not_needed",
        "stopped",
        "timeout",
        "error",
    }:
        return normalized
    return "idle"


def _seller_portal_recovery_failure_code(raw: Mapping[str, Any]) -> str:
    failure_code = str(raw.get("run_failure_code") or "").strip()
    if failure_code:
        return failure_code
    raw_status = str(raw.get("status") or "").strip()
    if raw_status in {"refresh_failed", "wrong_organization"}:
        return raw_status
    return ""


def _seller_portal_session_check_status(
    *,
    current_probe: Mapping[str, Any] | None,
    canonical_configured: bool,
    organization_confirmed: bool,
) -> str:
    if not canonical_configured:
        return "session_probe_error"
    if not isinstance(current_probe, Mapping):
        return "session_probe_error"
    if not bool(current_probe.get("ok")):
        normalized = str(current_probe.get("status") or "").strip()
        if normalized == "seller_portal_session_missing":
            return "session_missing"
        if normalized == "seller_portal_session_invalid":
            return "session_invalid"
        return "session_probe_error"
    if not organization_confirmed:
        return "session_valid_wrong_org"
    return "session_valid_canonical"


def _seller_portal_recovery_status_label(status: str) -> str:
    labels = {
        "idle": "Не запущено",
        "starting": "Запускаем",
        "awaiting_login": "Нужно войти",
        "saving_session": "Сохраняем сессию",
        "validating_session": "Проверяем сессию",
        "checking_canonical_supplier": "Проверяем кабинет",
        "triggering_refresh": "Обновляем данные",
        "completed": "Завершено",
        "not_needed": "Не потребовалось",
        "timeout": "Таймаут",
        "stopped": "Остановлено",
        "error": "Ошибка",
    }
    return labels.get(str(status or "").strip(), "Внимание")


def _seller_portal_session_check_status_label(status: str) -> str:
    labels = {
        "session_valid_canonical": "Сессия активна",
        "session_valid_wrong_org": "Не тот кабинет",
        "session_invalid": "Нужен вход",
        "session_missing": "Сессии нет",
        "session_probe_error": "Ошибка проверки",
    }
    return labels.get(str(status or "").strip(), "Проверка")


def _seller_portal_recovery_status_tone(status: str) -> str:
    if status in {"completed", "not_needed"}:
        return "success"
    if status in {"idle", "stopped"}:
        return "idle"
    if status in {"starting", "saving_session", "validating_session", "checking_canonical_supplier", "triggering_refresh"}:
        return "loading"
    if status in {"awaiting_login", "timeout"}:
        return "warning"
    return "error"


def _seller_portal_session_check_status_tone(status: str) -> str:
    if status == "session_valid_canonical":
        return "success"
    if status == "session_valid_wrong_org":
        return "warning"
    if status in {"session_invalid", "session_missing", "session_probe_error"}:
        return "error"
    return "idle"


def _seller_portal_recovery_copy(
    status: str,
    *,
    raw: Mapping[str, Any],
    current_probe: Mapping[str, Any] | None,
    canonical_configured: bool,
    organization_confirmed: bool,
    session_status: str,
) -> tuple[str, str]:
    failure_code = _seller_portal_recovery_failure_code(raw)
    if status == "idle" and not canonical_configured:
        return (
            "На хосте не настроен нужный кабинет для seller portal.",
            "Добавьте canonical supplier в runtime env и перезапустите сервис.",
        )
    if status == "idle":
        if session_status == "session_valid_canonical":
            return (
                "Новый запуск восстановления сейчас не выполняется. Сохранённая seller-сессия уже активна, нужный кабинет подтверждён.",
                "Если операторский вход снова понадобится, нажмите «Восстановить сессию».",
            )
        if session_status == "session_valid_wrong_org":
            return (
                "Новый запуск восстановления сейчас не выполняется. Сессия жива, но подтверждён не тот кабинет.",
                "Нажмите «Восстановить сессию», чтобы открыть временное окно входа и довести кабинет до canonical supplier.",
            )
        if session_status == "session_invalid":
            return (
                "Новый запуск восстановления сейчас не выполняется. Сохранённая seller-сессия больше не действует.",
                "Нажмите «Восстановить сессию», затем скачайте launcher и выполните вход.",
            )
        if session_status == "session_missing":
            return (
                "Новый запуск восстановления сейчас не выполняется. Сохранённая seller-сессия отсутствует.",
                "Нажмите «Восстановить сессию», затем скачайте launcher и выполните вход.",
            )
        return (
            "Новый запуск восстановления сейчас не выполняется.",
            "Сначала проверьте seller-сессию или запустите восстановление повторно.",
        )
    if status == "starting":
        return (
            "Запускаем текущее временное окно входа на host.",
            "Когда статус сменится на «Нужно войти», скачайте launcher и откройте seller portal для этого запуска.",
        )
    if status == "awaiting_login":
        return (
            "Временное окно входа готово. Откройте launcher и войдите в seller portal.",
            "После входа система сама сохранит storage_state.json, проверит seller-сессию, подтвердит нужный кабинет и завершит текущий запуск.",
        )
    if status == "saving_session":
        return (
            "Логин подтверждён. Сохраняем обновлённую seller-сессию для текущего запуска.",
            "Launcher можно не закрывать до финального статуса.",
        )
    if status == "validating_session":
        return (
            "Сохраняемая seller-сессия уже записана. Проверяем обновлённый storage_state.json.",
            "Дождитесь финального статуса текущего запуска.",
        )
    if status == "checking_canonical_supplier":
        return (
            "Seller-сессия валидна. Проверяем, что после входа подтверждён нужный кабинет.",
            "Если кабинет окажется не тем, запуск завершится явной ошибкой.",
        )
    if status == "triggering_refresh":
        return (
            "Seller-сессия сохранена и кабинет подтверждён. Запускаем post-login refresh.",
            "Launcher можно не закрывать до финального статуса.",
        )
    if status == "completed":
        return (
            "Восстановление завершено: seller-сессия сохранена, нужный кабинет подтверждён, refresh завершён.",
            "Текущий запуск завершён. Launcher печатает финальную строку и закрывается сам.",
        )
    if status == "not_needed":
        return (
            "Повторный вход не потребовался: на момент старта seller-сессия уже была активна и нужный кабинет был подтверждён.",
            "Текущий запуск завершён сразу, без noVNC и launcher.",
        )
    if status == "stopped":
        if isinstance(current_probe, Mapping) and bool(current_probe.get("ok")) and organization_confirmed:
            return (
                "Восстановление остановлено: временное окно входа закрыто. Сохранённая seller-сессия и бот не изменены.",
                "Кнопка «Остановить восстановление» закрывает только временное окно входа: storage_state.json сохраняется, бот не разлогинивается.",
            )
        return (
            "Восстановление остановлено: временное окно входа закрыто до завершения сценария.",
            "Если вход всё ещё нужен, снова нажмите «Восстановить сессию».",
        )
    if status == "timeout":
        return (
            "Восстановление завершено по таймауту: вход не был подтверждён до истечения временного окна.",
            "Запустите восстановление снова и войдите в seller portal.",
        )
    if failure_code == "wrong_organization":
        return (
            "Восстановление завершено с ошибкой: вход выполнен, но подтверждён не тот кабинет.",
            "Запустите восстановление снова: система повторно проверит supplier и переключит кабинет перед сохранением state.",
        )
    if failure_code == "refresh_failed":
        return (
            "Восстановление завершено с ошибкой: seller-сессия сохранена, но post-login refresh не завершился.",
            "Повторите запуск. Если ошибка останется, проверьте host-side логи recovery и refresh.",
        )
    if failure_code == "canonical_supplier_not_configured":
        return (
            "Восстановление не запущено: на хосте не настроен canonical supplier.",
            "Добавьте canonical supplier в runtime env и перезапустите сервис.",
        )
    if failure_code == "run_replaced":
        return (
            "Текущий launcher больше не смотрит на свой запуск: этот recovery run уже не является текущим.",
            "Откройте operator page заново и при необходимости скачайте launcher для нового запуска.",
        )
    if failure_code == "unexpected_exit":
        return (
            "Восстановление завершено с ошибкой: runtime завершился раньше финального статуса.",
            "Запустите восстановление снова. Если ошибка повторится, проверьте host-side лог relogin tool.",
        )
    return (
        "Восстановление завершено с ошибкой.",
        "Запустите восстановление снова. Если ошибка повторится, проверьте host-side лог relogin tool.",
    )


def _seller_portal_recovery_final_label(status: str) -> str:
    if status == "completed":
        return "Восстановление завершено"
    if status == "not_needed":
        return "Повторный вход не потребовался"
    if status == "stopped":
        return "Восстановление остановлено"
    if status == "timeout":
        return "Восстановление завершено по таймауту"
    if status == "error":
        return "Восстановление завершено с ошибкой"
    return ""


def _seller_portal_session_check_copy(
    status: str,
    *,
    canonical_configured: bool,
) -> tuple[str, str]:
    if not canonical_configured or status == "session_probe_error":
        return (
            "Не удалось честно проверить seller-сессию.",
            "Проверьте canonical supplier в runtime env и повторите проверку; если ошибка останется, смотрите лог session probe.",
        )
    if status == "session_valid_canonical":
        return (
            "Сохранённая seller-сессия активна, нужный кабинет подтверждён.",
            "Восстановление не требуется.",
        )
    if status == "session_valid_wrong_org":
        return (
            "Сессия активна, но открыт не тот кабинет.",
            "Нажмите «Восстановить сессию»: система откроет временное окно входа и переключит кабинет на нужный supplier.",
        )
    if status == "session_invalid":
        return (
            "Сохранённая seller-сессия больше не действует.",
            "Нажмите «Восстановить сессию» и войдите через launcher для Mac.",
        )
    if status == "session_missing":
        return (
            "Сохранённая seller-сессия не найдена.",
            "Нажмите «Восстановить сессию» и выполните вход заново.",
        )
    return (
        "Проверка seller-сессии завершилась неопределённо.",
        "Повторите проверку или запустите восстановление, если операторский вход нужен прямо сейчас.",
    )


def _seller_portal_recovery_technical_line(
    *,
    expected_supplier_id: str,
    expected_supplier_label: str,
    supplier_context: Mapping[str, Any],
    launcher_ready: bool,
) -> str:
    parts = []
    if expected_supplier_id:
        expected_line = (
            f"Нужный кабинет: {expected_supplier_label} · supplier {expected_supplier_id}"
            if expected_supplier_label
            else f"Нужный supplier: {expected_supplier_id}"
        )
        parts.append(expected_line)
    current_supplier_id = (
        str(supplier_context.get("current_supplier_id") or "").strip()
        or str(supplier_context.get("analytics_supplier_id") or "").strip()
    )
    if current_supplier_id and current_supplier_id != expected_supplier_id:
        parts.append(f"Сейчас выбран supplier {current_supplier_id}")
    if launcher_ready:
        parts.append("Launcher открывает localhost-only noVNC через SSH tunnel; XQuartz не нужен")
    return " · ".join(part for part in parts if part)


def _default_activated_at_factory() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _default_now_factory() -> datetime:
    return datetime.now(timezone.utc)


def _format_optional_business_timestamp(value: str | None) -> str:
    if not value:
        return ""
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return to_business_datetime(instant).replace(microsecond=0).isoformat()


def _format_operator_result_payload(result_payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result_payload, Mapping):
        return None
    payload = dict(result_payload)
    for field_name in ("finished_at", "loaded_at", "refreshed_at", "last_loaded_at"):
        if field_name in payload:
            payload[field_name] = _format_optional_business_timestamp(str(payload.get(field_name) or "") or None)
    return payload


def _build_refresh_result_payload(refresh_result: Any) -> dict[str, Any]:
    return {
        "technical_status": "success",
        "semantic_status": str(getattr(refresh_result, "semantic_status", "") or "warning"),
        "semantic_label": str(getattr(refresh_result, "semantic_label", "") or "Внимание"),
        "semantic_tone": str(getattr(refresh_result, "semantic_tone", "") or "warning"),
        "semantic_reason": str(getattr(refresh_result, "semantic_reason", "") or ""),
        "snapshot_id": str(getattr(refresh_result, "snapshot_id", "") or ""),
        "as_of_date": str(getattr(refresh_result, "as_of_date", "") or ""),
        "refreshed_at": str(getattr(refresh_result, "refreshed_at", "") or ""),
    }


def _build_refresh_error_payload(
    *,
    requested_as_of_date: str | None,
    finished_at: str,
    error: str,
) -> dict[str, Any]:
    return {
        "technical_status": "error",
        "semantic_status": "error",
        "semantic_label": "Ошибка",
        "semantic_tone": "error",
        "semantic_reason": str(error or "").strip() or "refresh завершился ошибкой",
        "snapshot_id": "",
        "as_of_date": requested_as_of_date or "",
        "finished_at": finished_at,
    }


def _build_load_result_payload(
    *,
    plan: SheetVitrinaV1Envelope,
    refresh_status: Any,
    bridge_result: Mapping[str, Any],
    previous_load_state: Any,
    finished_at: str,
) -> dict[str, Any]:
    previous_fingerprint = str(getattr(previous_load_state, "plan_fingerprint", "") or "").strip()
    current_fingerprint = _plan_fingerprint(plan)
    sheet_verified = _bridge_result_has_sheet_verification(bridge_result)
    if not sheet_verified:
        change_status = "not_verified"
        semantic_status = "warning"
        semantic_reason = "sheet bridge завершился, но не вернул верифицируемое состояние листов"
    elif not previous_fingerprint:
        change_status = "not_verified"
        semantic_status = "warning"
        semantic_reason = "sheet bridge завершился, но предыдущая отправка для сравнения отсутствует"
    elif previous_fingerprint == current_fingerprint:
        change_status = "unchanged"
        semantic_status = "warning"
        semantic_reason = "sheet bridge завершился, но snapshot совпадает с последней отправкой"
    else:
        change_status = "updated"
        semantic_status = "success"
        semantic_reason = "sheet bridge завершился; данные изменились относительно последней отправки"
    return {
        "technical_status": "success",
        "semantic_status": semantic_status,
        "semantic_label": _semantic_status_label(semantic_status),
        "semantic_tone": semantic_status,
        "semantic_reason": semantic_reason,
        "change_status": change_status,
        "change_label": _load_change_label(change_status),
        "change_verified": change_status == "updated",
        "snapshot_id": plan.snapshot_id,
        "as_of_date": plan.as_of_date,
        "refreshed_at": str(getattr(refresh_status, "refreshed_at", "") or ""),
        "finished_at": finished_at,
        "plan_fingerprint": current_fingerprint,
        "last_loaded_at": str(getattr(previous_load_state, "loaded_at", "") or ""),
    }


def _build_load_error_payload(
    *,
    requested_as_of_date: str | None,
    plan: SheetVitrinaV1Envelope | None,
    refresh_status: Any | None,
    finished_at: str,
    error: str,
) -> dict[str, Any]:
    return {
        "technical_status": "error",
        "semantic_status": "error",
        "semantic_label": "Ошибка",
        "semantic_tone": "error",
        "semantic_reason": str(error or "").strip() or "load завершился ошибкой",
        "change_status": "error",
        "change_label": _load_change_label("error"),
        "change_verified": False,
        "snapshot_id": plan.snapshot_id if plan is not None else "",
        "as_of_date": plan.as_of_date if plan is not None else (requested_as_of_date or ""),
        "refreshed_at": str(getattr(refresh_status, "refreshed_at", "") or "") if refresh_status is not None else "",
        "finished_at": finished_at,
        "plan_fingerprint": _plan_fingerprint(plan) if plan is not None else "",
        "last_loaded_at": "",
    }


def _build_auto_update_result_payload(
    *,
    refresh_payload: Mapping[str, Any] | None,
    load_payload: Mapping[str, Any] | None,
    technical_status: str,
    finished_at: str,
    error: str | None,
) -> dict[str, Any]:
    refresh_semantic = str((refresh_payload or {}).get("semantic_status") or "warning")
    load_semantic = str((load_payload or {}).get("semantic_status") or "")
    semantic_status = (
        "error"
        if technical_status == "error"
        else _worst_tone([value for value in [refresh_semantic, load_semantic] if value])
    )
    semantic_reason = (
        str(error or "").strip()
        if technical_status == "error"
        else " | ".join(
            part
            for part in [
                f"refresh: {str((refresh_payload or {}).get('semantic_reason') or '').strip()}",
                f"load: {str((load_payload or {}).get('semantic_reason') or '').strip()}",
                "legacy Google Sheets load: archived / not executed",
            ]
            if not part.endswith(": ") and (load_payload is not None or not part.startswith("load:"))
        )
    )
    return {
        "technical_status": technical_status,
        "semantic_status": semantic_status,
        "semantic_label": _semantic_status_label(semantic_status),
        "semantic_tone": semantic_status,
        "semantic_reason": semantic_reason or ("auto_update завершился" if technical_status == "success" else "auto_update завершился ошибкой"),
        "snapshot_id": str((load_payload or refresh_payload or {}).get("snapshot_id") or ""),
        "as_of_date": str((load_payload or refresh_payload or {}).get("as_of_date") or ""),
        "refreshed_at": str((load_payload or refresh_payload or {}).get("refreshed_at") or ""),
        "finished_at": finished_at,
    }


def _bridge_result_has_sheet_verification(bridge_result: Mapping[str, Any]) -> bool:
    write_result = bridge_result.get("write_result")
    sheet_state = bridge_result.get("sheet_state")
    if not isinstance(write_result, Mapping) or not isinstance(sheet_state, Mapping):
        return False
    written_sheets = write_result.get("sheets")
    state_sheets = sheet_state.get("sheets")
    return isinstance(written_sheets, list) and bool(written_sheets) and isinstance(state_sheets, list) and bool(state_sheets)


def _plan_fingerprint(plan: SheetVitrinaV1Envelope | None) -> str:
    if plan is None:
        return ""
    payload = json.dumps(asdict(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _semantic_status_label(status: str) -> str:
    if status == "success":
        return "Успешно"
    if status == "error":
        return "Ошибка"
    return "Внимание"


def _load_change_label(change_status: str) -> str:
    if change_status == "updated":
        return "Данные изменились"
    if change_status == "unchanged":
        return "Без изменений"
    if change_status == "error":
        return "Ошибка"
    return "Не подтверждено"


def _auto_update_status_label(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "success":
        return "успех"
    if normalized == "error":
        return "ошибка"
    if normalized == "running":
        return "выполняется"
    return "ещё не выполнялся"


@dataclass
class SheetVitrinaV1OperatorJob:
    job_id: str
    operation: str
    status: str
    started_at: str
    log_lines: list[str] = field(default_factory=list)
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "operation": self.operation,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_lines": list(self.log_lines),
            "log_line_count": len(self.log_lines),
        }
        if self.result is not None:
            payload["result"] = self.result
        if self.error is not None:
            payload["error"] = self.error
        return payload


class SheetVitrinaV1OperatorJobStore:
    def __init__(self, timestamp_factory: Callable[[], str]) -> None:
        self.timestamp_factory = timestamp_factory
        self._jobs: dict[str, SheetVitrinaV1OperatorJob] = {}
        self._lock = threading.Lock()

    def start(
        self,
        *,
        operation: str,
        runner: Callable[[OperatorLogEmitter], dict[str, Any]],
    ) -> dict[str, Any]:
        job_id = uuid4().hex
        job = SheetVitrinaV1OperatorJob(
            job_id=job_id,
            operation=operation,
            status="running",
            started_at=self.timestamp_factory(),
        )
        with self._lock:
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run,
            args=(job_id, runner),
            daemon=True,
        )
        thread.start()
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError(f"sheet_vitrina_v1 operator job not found: {job_id}")
            return job.snapshot()

    def get_text(self, job_id: str) -> tuple[str, str]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError(f"sheet_vitrina_v1 operator job not found: {job_id}")
            text = "\n".join(job.log_lines).rstrip()
            if text:
                text = f"{text}\n"
            filename = f"sheet-vitrina-v1-{job.operation}-{job.job_id}.txt"
            return text or "Лог пока пуст.\n", filename

    def latest_relevant_job(
        self,
        *,
        operations: tuple[str, ...],
        preferred_as_of_date: str | None = None,
        strict_preferred_as_of_date: bool = False,
    ) -> dict[str, Any] | None:
        normalized_operations = {str(value).strip() for value in operations if str(value).strip()}
        normalized_as_of_date = str(preferred_as_of_date or "").strip()
        with self._lock:
            jobs = list(self._jobs.values())
        candidates = [
            job
            for job in jobs
            if job.status in {"success", "error"}
            and (not normalized_operations or job.operation in normalized_operations)
        ]
        if not candidates:
            return None
        if normalized_as_of_date:
            preferred = [
                job
                for job in candidates
                if str(((job.result or {}).get("as_of_date") or "")).strip() == normalized_as_of_date
            ]
            if preferred:
                candidates = preferred
            elif strict_preferred_as_of_date:
                return None
        selected = max(
            enumerate(candidates),
            key=lambda item: (
                str(item[1].finished_at or ""),
                str(item[1].started_at or ""),
                item[0],
            ),
        )[1]
        return selected.snapshot()

    def _run(
        self,
        job_id: str,
        runner: Callable[[OperatorLogEmitter], dict[str, Any]],
    ) -> None:
        token = SHEET_OPERATOR_JOB_ID.set(job_id)
        try:
            result = runner(lambda message: self._append_log(job_id, message))
        except Exception as exc:
            self._append_log(job_id, f"Ошибка: {exc}")
            with self._lock:
                job = self._jobs[job_id]
                job.status = "error"
                job.finished_at = self.timestamp_factory()
                job.error = str(exc)
            SHEET_OPERATOR_JOB_ID.reset(token)
            return

        with self._lock:
            job = self._jobs[job_id]
            job.status = "success"
            job.finished_at = self.timestamp_factory()
            job.result = result
        SHEET_OPERATOR_JOB_ID.reset(token)

    def _append_log(self, job_id: str, message: str) -> None:
        timestamp = self.timestamp_factory()
        with self._lock:
            job = self._jobs[job_id]
            job.log_lines.append(f"{timestamp} {message}")
            if len(job.log_lines) > 4000:
                job.log_lines = job.log_lines[-4000:]


def _sheet_row_counts(plan: SheetVitrinaV1Envelope) -> dict[str, int]:
    return {item.sheet_name: item.row_count for item in plan.sheets}


def _find_sheet(plan: SheetVitrinaV1Envelope, sheet_name: str) -> SheetVitrinaWriteTarget | None:
    for sheet in plan.sheets:
        if sheet.sheet_name == sheet_name:
            return sheet
    return None


def _normalize_source_group_id(source_group_id: str) -> str:
    normalized = str(source_group_id or "").strip()
    if normalized not in WEB_VITRINA_SOURCE_GROUPS:
        raise ValueError(
            "unsupported source_group_id: "
            f"{normalized!r}; expected one of {', '.join(WEB_VITRINA_SOURCE_GROUP_ORDER)}"
        )
    return normalized


def _source_group_config(source_group_id: str) -> Mapping[str, Any]:
    return WEB_VITRINA_SOURCE_GROUPS[_normalize_source_group_id(source_group_id)]


def _resolve_sheet_refresh_as_of_date(value: str | None, *, now: datetime) -> str:
    default_as_of_date = default_business_as_of_date(now)
    normalized = str(value or "").strip() or default_as_of_date
    try:
        datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Дата обновления должна быть в формате YYYY-MM-DD, получено {normalized!r}") from exc
    if normalized == current_business_date_iso(now):
        return default_as_of_date
    return normalized


def _resolve_group_refresh_selected_date(value: str | None, *, now: datetime) -> str:
    normalized = str(value or "").strip() or current_business_date_iso(now)
    try:
        datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Дата обновления группы должна быть в формате YYYY-MM-DD, получено {normalized!r}") from exc
    return normalized


def _target_snapshot_as_of_date_for_group_refresh(selected_as_of_date: str, *, now: datetime) -> str:
    current_date = current_business_date_iso(now)
    if selected_as_of_date == current_date:
        return default_business_as_of_date(now)
    return selected_as_of_date


def _default_web_vitrina_page_period(
    contract: Any,
    *,
    available_snapshot_dates: Iterable[str],
) -> tuple[str, str] | None:
    """Default UI period: latest four server-readable business dates, inclusive."""

    date_columns = [str(item) for item in getattr(contract.meta, "date_columns", []) if str(item)]
    readable_dates = {str(item) for item in available_snapshot_dates if str(item)}
    if not date_columns or not readable_dates:
        return None
    status_summary = contract.status_summary
    current_business_date = str(getattr(status_summary, "current_business_date", "") or "")
    default_as_of_date = str(getattr(status_summary, "default_as_of_date", "") or "")
    if current_business_date in readable_dates:
        period_end = current_business_date
    elif default_as_of_date in readable_dates:
        period_end = default_as_of_date
    else:
        period_end = sorted(readable_dates)[-1]
    try:
        end_date = date.fromisoformat(period_end)
    except ValueError:
        return None
    period_start = (end_date - timedelta(days=3)).isoformat()
    expected_dates = [
        (date.fromisoformat(period_start) + timedelta(days=offset)).isoformat()
        for offset in range(4)
    ]
    if any(item not in readable_dates for item in expected_dates):
        return None
    return period_start, period_end


def _web_vitrina_source_status_snapshot_as_of_date(contract: Any) -> str:
    meta = contract.meta
    status_summary = contract.status_summary
    explicit_source_snapshot = str(getattr(status_summary, "source_status_snapshot_as_of_date", "") or "")
    if explicit_source_snapshot:
        return explicit_source_snapshot
    snapshot_as_of_date = str(getattr(meta, "as_of_date", "") or "")
    date_columns = {str(item) for item in getattr(meta, "date_columns", []) if str(item)}
    read_model = str(getattr(status_summary, "read_model", "") or "")
    default_as_of_date = str(getattr(status_summary, "default_as_of_date", "") or "")
    if read_model == "persisted_ready_snapshot_window" and default_as_of_date in date_columns:
        return default_as_of_date
    return snapshot_as_of_date


def _web_vitrina_source_status_snapshot_id(
    runtime: RegistryUploadDbBackedRuntime,
    contract: Any,
    *,
    snapshot_as_of_date: str,
) -> str:
    contract_snapshot_id = str(getattr(contract.meta, "snapshot_id", "") or "")
    if str(getattr(contract.meta, "as_of_date", "") or "") == snapshot_as_of_date:
        return contract_snapshot_id
    try:
        return str(runtime.load_sheet_vitrina_ready_snapshot(as_of_date=snapshot_as_of_date).snapshot_id)
    except Exception:  # pragma: no cover - best-effort display metadata
        return contract_snapshot_id


def _metric_keys_for_source_keys(metrics: Iterable[Any], *, source_keys: Iterable[str]) -> list[str]:
    source_key_set = {str(item).strip() for item in source_keys if str(item).strip()}
    allowed_metric_keys: set[str] = set()
    for source_key in source_key_set:
        allowed_metric_keys.update(WEB_VITRINA_SOURCE_METRIC_KEYS.get(source_key, ()))
    ordered: list[str] = []
    for metric in sorted(metrics, key=lambda item: int(getattr(item, "display_order", 0) or 0)):
        metric_key = str(getattr(metric, "metric_key", "") or "").strip()
        if (
            metric_key
            and metric_key in allowed_metric_keys
            and bool(getattr(metric, "enabled", True))
            and bool(getattr(metric, "show_in_data", True))
        ):
            ordered.append(metric_key)
    return ordered


def _source_key_for_metric_key(metric_key: str) -> str:
    normalized_metric_key = str(metric_key or "").strip()
    for source_key, metric_keys in WEB_VITRINA_SOURCE_METRIC_KEYS.items():
        if normalized_metric_key in set(metric_keys):
            return source_key
    return ""


def _data_sheet_row_count(plan: SheetVitrinaV1Envelope) -> int:
    data_sheet = _find_sheet(plan, "DATA_VITRINA")
    return len(data_sheet.rows) if data_sheet is not None else 0


def _merge_source_group_ready_snapshot(
    *,
    previous_plan: SheetVitrinaV1Envelope,
    partial_plan: SheetVitrinaV1Envelope,
    source_group_id: str,
    source_keys: Iterable[str],
    metric_keys: Iterable[str],
    refreshed_at: str,
    previous_refreshed_at: str,
    selected_as_of_date: str | None = None,
) -> tuple[SheetVitrinaV1Envelope, dict[str, Any]]:
    metric_key_set = {str(item).strip() for item in metric_keys if str(item).strip()}
    source_key_set = {str(item).strip() for item in source_keys if str(item).strip()}
    selected_date = str(selected_as_of_date or "").strip()
    if not selected_date and previous_plan.date_columns != partial_plan.date_columns:
        raise ValueError(
            "partial snapshot date_columns mismatch: "
            f"{partial_plan.date_columns} != {previous_plan.date_columns}"
        )

    previous_data = _require_sheet(previous_plan, "DATA_VITRINA")
    partial_data = _require_sheet(partial_plan, "DATA_VITRINA")
    previous_status = _require_sheet(previous_plan, "STATUS")
    partial_status = _require_sheet(partial_plan, "STATUS")
    previous_date_indexes: list[int] = []
    partial_date_indexes: list[int] = []
    selected_temporal_slots: set[str] = set()
    if selected_date:
        previous_date_indexes = _sheet_header_indexes(previous_data.header, selected_date)
        partial_date_indexes = _sheet_header_indexes(partial_data.header, selected_date)
        if not previous_date_indexes:
            raise ValueError(f"target ready snapshot does not contain selected date {selected_date}")
        if not partial_date_indexes:
            raise ValueError(f"partial group snapshot does not contain selected date {selected_date}")
        selected_temporal_slots = {
            str(slot.slot_key)
            for slot in partial_plan.temporal_slots
            if str(slot.column_date) == selected_date
        }

    partial_rows_by_id = {_row_id(row): list(row) for row in partial_data.rows if _row_id(row)}
    updated_row_ids = {
        row_id
        for row_id in partial_rows_by_id
        if _metric_key_from_row_id(row_id) in metric_key_set
    }
    merged_data_rows: list[list[Any]] = []
    rows_updated = 0
    rows_preserved = 0
    for row in previous_data.rows:
        row_id = _row_id(row)
        if row_id in updated_row_ids:
            if selected_date:
                merged_data_rows.append(
                    _merge_row_selected_date(
                        previous_row=list(row),
                        partial_row=partial_rows_by_id[row_id],
                        previous_indexes=previous_date_indexes,
                        partial_indexes=partial_date_indexes,
                    )
                )
            else:
                merged_data_rows.append(partial_rows_by_id[row_id])
            rows_updated += 1
        else:
            merged_data_rows.append(list(row))
            rows_preserved += 1
    existing_row_ids = {_row_id(row) for row in previous_data.rows if _row_id(row)}
    for row_id in sorted(updated_row_ids - existing_row_ids):
        merged_data_rows.append(partial_rows_by_id[row_id])
        rows_updated += 1
    if source_group_id == "other_sources" and selected_date:
        _recompute_other_sources_derived_rows(
            rows=merged_data_rows,
            header=previous_data.header,
            selected_dates=[selected_date],
            updated_row_ids=updated_row_ids,
        )

    selected_status_rows = [
        list(row)
        for row in partial_status.rows
        if _status_row_source_base(row) in source_key_set
        and (
            not selected_date
            or not selected_temporal_slots
            or _status_row_temporal_slot(row) in selected_temporal_slots
        )
    ]
    selected_status_keys = {
        _status_row_key(row) if selected_date else _status_row_source_base(row)
        for row in selected_status_rows
    }
    merged_status_rows = [
        list(row)
        for row in previous_status.rows
        if (
            (_status_row_key(row) if selected_date else _status_row_source_base(row))
            not in selected_status_keys
        )
    ]
    merged_status_rows.extend(selected_status_rows)

    merged_sheets: list[SheetVitrinaWriteTarget] = []
    for sheet in previous_plan.sheets:
        if sheet.sheet_name == "DATA_VITRINA":
            merged_sheets.append(
                replace(
                    sheet,
                    rows=merged_data_rows,
                    row_count=len(merged_data_rows),
                    column_count=len(sheet.header),
                )
            )
        elif sheet.sheet_name == "STATUS":
            merged_sheets.append(
                replace(
                    sheet,
                    rows=merged_status_rows,
                    row_count=len(merged_status_rows),
                    column_count=len(sheet.header),
                )
            )
        else:
            merged_sheets.append(sheet)

    previous_metadata = dict(getattr(previous_plan, "metadata", {}) or {})
    row_updated_at = _row_updated_at_metadata(
        previous_plan,
        metadata=previous_metadata,
        fallback_updated_at=previous_refreshed_at,
    )
    for row_id in updated_row_ids:
        row_updated_at[row_id] = refreshed_at
    group_updated_at = _source_group_updated_at_metadata(
        metadata=previous_metadata,
        fallback_updated_at=previous_refreshed_at,
    )
    group_updated_at[source_group_id] = refreshed_at
    updated_cells = _updated_cells_for_plan(
        replace(
            previous_plan,
            sheets=merged_sheets,
        ),
        row_ids=updated_row_ids,
        date_columns=[selected_date] if selected_date else list(previous_plan.date_columns),
    )
    metadata = {
        **previous_metadata,
        "row_last_updated_at_by_row_id": row_updated_at,
        "source_group_last_updated_at": group_updated_at,
        "last_partial_group_refresh": {
            "source_group_id": source_group_id,
            "source_keys": sorted(source_key_set),
            "metric_keys": sorted(metric_key_set),
            "selected_as_of_date": selected_date,
            "updated_dates": [selected_date] if selected_date else list(previous_plan.date_columns),
            "updated_cells": updated_cells,
            "refreshed_at": refreshed_at,
        },
    }
    merged_plan = SheetVitrinaV1Envelope(
        plan_version=previous_plan.plan_version,
        snapshot_id=f"{partial_plan.as_of_date}__partial_group_{source_group_id}__{refreshed_at}",
        as_of_date=previous_plan.as_of_date,
        date_columns=previous_plan.date_columns,
        temporal_slots=previous_plan.temporal_slots,
        source_temporal_policies=previous_plan.source_temporal_policies,
        sheets=merged_sheets,
        metadata=metadata,
    )
    return merged_plan, {
        "rows_updated": rows_updated,
        "rows_preserved": rows_preserved,
        "status_rows_updated": len(selected_status_rows),
        "source_group_id": source_group_id,
        "source_keys": sorted(source_key_set),
        "metric_keys": sorted(metric_key_set),
        "selected_as_of_date": selected_date,
        "updated_dates": [selected_date] if selected_date else list(previous_plan.date_columns),
        "updated_row_ids": sorted(updated_row_ids),
        "updated_cells": updated_cells,
        "updated_cell_count": _count_updated_cells_by_status(updated_cells, "updated"),
        "latest_confirmed_cell_count": _count_updated_cells_by_status(updated_cells, "latest_confirmed"),
    }


def _with_full_refresh_metadata(plan: SheetVitrinaV1Envelope, *, refreshed_at: str) -> SheetVitrinaV1Envelope:
    data_sheet = _find_sheet(plan, "DATA_VITRINA")
    row_updated_at = {
        _row_id(row): refreshed_at
        for row in (data_sheet.rows if data_sheet is not None else [])
        if _row_id(row)
    }
    metadata = {
        **dict(getattr(plan, "metadata", {}) or {}),
        "row_last_updated_at_by_row_id": row_updated_at,
        "source_group_last_updated_at": {
            group_id: refreshed_at
            for group_id in WEB_VITRINA_SOURCE_GROUP_ORDER
        },
    }
    return replace(plan, metadata=metadata)


def _new_operator_refresh_diagnostics(
    *,
    job_id: str,
    execution_mode: str,
    started_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": "refresh_diagnostics_v1",
        "job_id": str(job_id or ""),
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
        "counter_gaps": [],
    }


def _start_operator_phase(phase_key: str, *, started_at: str) -> dict[str, Any]:
    return {
        "phase_key": phase_key,
        "started_at": started_at,
        "started_perf": time.perf_counter(),
    }


def _finish_operator_phase(
    diagnostics: dict[str, Any],
    phase: Mapping[str, Any],
    *,
    finished_at: str,
    status: str,
    note_kind: str | None = None,
) -> None:
    item = {
        "phase_key": str(phase.get("phase_key") or ""),
        "started_at": str(phase.get("started_at") or ""),
        "finished_at": finished_at,
        "duration_ms": max(0, int(round((time.perf_counter() - float(phase.get("started_perf") or time.perf_counter())) * 1000))),
        "status": status,
    }
    if note_kind:
        item["note_kind"] = note_kind
    diagnostics.setdefault("phase_summary", []).append(item)


def _refresh_diagnostics_from_plan(plan: SheetVitrinaV1Envelope) -> dict[str, Any]:
    metadata = dict(getattr(plan, "metadata", {}) or {})
    raw = metadata.get("refresh_diagnostics")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _merge_refresh_diagnostics(
    operator_diagnostics: Mapping[str, Any],
    plan_diagnostics: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(plan_diagnostics or {})
    for key, value in operator_diagnostics.items():
        if key == "phase_summary":
            continue
        if key in {"source_summary", "source_slots"} and merged.get(key):
            continue
        if value not in ("", None, []):
            merged[key] = value
        else:
            merged.setdefault(key, value)
    operator_phases = [
        dict(item)
        for item in (operator_diagnostics.get("phase_summary") or [])
        if isinstance(item, Mapping)
    ]
    plan_phases = [
        dict(item)
        for item in (plan_diagnostics.get("phase_summary") or [])
        if isinstance(item, Mapping)
    ]
    merged["phase_summary"] = [*operator_phases, *plan_phases]
    if not merged.get("source_summary") and isinstance(merged.get("source_slots"), list):
        merged["source_summary"] = _summarize_refresh_diagnostic_sources(merged["source_slots"])
    return merged


def _complete_refresh_diagnostics(
    diagnostics: dict[str, Any],
    *,
    job_id: str,
    execution_mode: str,
    as_of_date: str,
    bundle_version: str,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    semantic_status: str,
    technical_status: str,
) -> None:
    diagnostics["job_id"] = str(job_id or diagnostics.get("job_id") or "")
    diagnostics["execution_mode"] = execution_mode
    diagnostics["as_of_date"] = as_of_date
    diagnostics["bundle_version"] = bundle_version
    diagnostics["started_at"] = started_at
    diagnostics["finished_at"] = finished_at
    diagnostics["duration_ms"] = duration_ms
    diagnostics["semantic_status"] = semantic_status
    diagnostics["technical_status"] = technical_status
    if isinstance(diagnostics.get("source_slots"), list):
        diagnostics["source_summary"] = _summarize_refresh_diagnostic_sources(diagnostics["source_slots"])
    diagnostics["counter_gaps"] = sorted({
        str(item)
        for item in (diagnostics.get("counter_gaps") or [])
        if str(item).strip()
    })


def _with_refresh_diagnostics_metadata(
    plan: SheetVitrinaV1Envelope,
    refresh_diagnostics: Mapping[str, Any],
) -> SheetVitrinaV1Envelope:
    return replace(
        plan,
        metadata={
            **dict(getattr(plan, "metadata", {}) or {}),
            "refresh_diagnostics": dict(refresh_diagnostics),
        },
    )


def _summarize_refresh_diagnostic_sources(raw_source_slots: Any) -> list[dict[str, Any]]:
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


def _with_page_composition_diagnostics(
    payload: Mapping[str, Any],
    *,
    started_perf: float,
    include_source_status: bool,
) -> dict[str, Any]:
    normalized = dict(payload)
    meta = dict(normalized.get("meta") or {})
    table_surface = dict(normalized.get("table_surface") or {})
    rows = list(table_surface.get("rows") or [])
    columns = list(table_surface.get("columns") or [])
    diagnostics = {
        "page_composition_build_ms": max(0, int(round((time.perf_counter() - started_perf) * 1000))),
        "payload_bytes": 0,
        "include_source_status": bool(include_source_status),
        "row_count": len(rows),
        "cell_count": _page_composition_cell_count(rows=rows, columns=columns),
    }
    meta["page_composition_diagnostics"] = diagnostics
    normalized["meta"] = meta
    for _ in range(2):
        diagnostics["payload_bytes"] = len(
            json.dumps(normalized, ensure_ascii=False, indent=2).encode("utf-8")
        ) + 1
    return normalized


def _page_composition_cell_count(*, rows: list[Any], columns: list[Any]) -> int:
    explicit = 0
    for row in rows:
        if isinstance(row, Mapping) and isinstance(row.get("values"), Mapping):
            explicit += len(row["values"])
    if explicit:
        return explicit
    return len(rows) * len(columns)


def _require_sheet(plan: SheetVitrinaV1Envelope, sheet_name: str) -> SheetVitrinaWriteTarget:
    sheet = _find_sheet(plan, sheet_name)
    if sheet is None:
        raise ValueError(f"ready snapshot missing {sheet_name}")
    return sheet


def _row_updated_at_metadata(
    plan: SheetVitrinaV1Envelope,
    *,
    metadata: Mapping[str, Any],
    fallback_updated_at: str,
) -> dict[str, str]:
    raw = metadata.get("row_last_updated_at_by_row_id")
    result = {str(key): str(value) for key, value in raw.items() if str(key) and str(value)} if isinstance(raw, Mapping) else {}
    data_sheet = _find_sheet(plan, "DATA_VITRINA")
    for row in (data_sheet.rows if data_sheet is not None else []):
        row_id = _row_id(row)
        if row_id and row_id not in result:
            result[row_id] = fallback_updated_at
    return result


def _source_group_updated_at_metadata(
    *,
    metadata: Mapping[str, Any],
    fallback_updated_at: str,
) -> dict[str, str]:
    raw = metadata.get("source_group_last_updated_at")
    result = {str(key): str(value) for key, value in raw.items() if str(key) and str(value)} if isinstance(raw, Mapping) else {}
    for group_id in WEB_VITRINA_SOURCE_GROUP_ORDER:
        result.setdefault(group_id, fallback_updated_at)
    return result


def _updated_cells_for_plan(
    plan: SheetVitrinaV1Envelope,
    *,
    row_ids: Iterable[str] | None = None,
    date_columns: Iterable[str] | None = None,
) -> list[dict[str, str]]:
    data_sheet = _find_sheet(plan, "DATA_VITRINA")
    if data_sheet is None:
        return []
    row_id_filter = {str(item).strip() for item in (row_ids or []) if str(item).strip()}
    date_filter = {str(item).strip() for item in (date_columns or plan.date_columns) if str(item).strip()}
    status_by_source_date = _updated_cell_statuses_by_source_and_date(plan)
    result: list[dict[str, str]] = []
    for row in data_sheet.rows:
        row_id = _row_id(row)
        if not row_id or (row_id_filter and row_id not in row_id_filter):
            continue
        metric_key = _metric_key_from_row_id(row_id)
        source_key = _source_key_for_metric_key(metric_key)
        source_group_id = _source_group_id_for_source_key(source_key)
        if not source_key or not source_group_id:
            continue
        for as_of_date in plan.date_columns:
            if date_filter and as_of_date not in date_filter:
                continue
            status = status_by_source_date.get((source_key, as_of_date), "updated")
            if status not in {"updated", "latest_confirmed"}:
                continue
            result.append(
                {
                    "row_id": row_id,
                    "metric_key": metric_key,
                    "as_of_date": as_of_date,
                    "source_group_id": source_group_id,
                    "source_key": source_key,
                    "status": status,
                }
            )
    return result


def _updated_cell_statuses_by_source_and_date(plan: SheetVitrinaV1Envelope) -> dict[tuple[str, str], str]:
    status_sheet = _find_sheet(plan, "STATUS")
    if status_sheet is None:
        return {}
    slot_date_by_key = {str(slot.slot_key): str(slot.column_date) for slot in plan.temporal_slots}
    grouped_rows: dict[tuple[str, str], list[list[Any]]] = {}
    for row in status_sheet.rows:
        source_key = _status_row_source_base(row)
        if not source_key or source_key == "registry_upload_current_state":
            continue
        temporal_slot = _status_row_temporal_slot(row)
        as_of_date = slot_date_by_key.get(temporal_slot) or _status_row_date(row)
        if not as_of_date:
            continue
        grouped_rows.setdefault((source_key, as_of_date), []).append(list(row))
    return {
        key: status
        for key, rows in grouped_rows.items()
        if (status := _updated_cell_status_for_status_rows(rows))
    }


def _updated_cell_status_for_status_rows(rows: list[list[Any]]) -> str:
    statuses = [_updated_cell_status_for_status_row(row) for row in rows]
    if "latest_confirmed" in statuses:
        return "latest_confirmed"
    if "updated" in statuses:
        return "updated"
    return ""


def _updated_cell_status_for_status_row(row: list[Any]) -> str:
    kind = str(row[1] if len(row) > 1 else "").strip().lower()
    note = str(row[10] if len(row) > 10 else "").strip().lower()
    if kind in {"error", "missing", "not_found", "blocked", "not_available"}:
        return ""
    if _status_note_is_latest_confirmed(note):
        return "latest_confirmed"
    if kind == "warning":
        return "latest_confirmed"
    if kind == "success":
        return "updated"
    return ""


def _status_row_date(row: list[Any]) -> str:
    for index in (4, 5, 3, 2):
        if len(row) > index and str(row[index] or "").strip():
            return str(row[index] or "").strip()
    return ""


def _count_updated_cells_by_status(updated_cells: Iterable[Mapping[str, Any]], status: str) -> int:
    return sum(1 for item in updated_cells if str(item.get("status") or "") == status)


def _row_id(row: list[Any]) -> str:
    return str(row[1] or "").strip() if len(row) > 1 else ""


def _sheet_header_indexes(header: Iterable[Any], value: str) -> list[int]:
    normalized_value = str(value or "").strip()
    return [
        index
        for index, item in enumerate(header)
        if str(item or "").strip() == normalized_value
    ]


def _merge_row_selected_date(
    *,
    previous_row: list[Any],
    partial_row: list[Any],
    previous_indexes: list[int],
    partial_indexes: list[int],
) -> list[Any]:
    merged = list(previous_row)
    fallback_partial_index = partial_indexes[0]
    for previous_index in previous_indexes:
        partial_index = previous_index if previous_index in partial_indexes else fallback_partial_index
        if partial_index < len(partial_row):
            while previous_index >= len(merged):
                merged.append("")
            merged[previous_index] = partial_row[partial_index]
    return merged


def _recompute_other_sources_derived_rows(
    *,
    rows: list[list[Any]],
    header: Iterable[Any],
    selected_dates: Iterable[str],
    updated_row_ids: set[str],
) -> None:
    row_by_id = {_row_id(row): row for row in rows if _row_id(row)}
    date_indexes = [
        index
        for selected_date in selected_dates
        for index in _sheet_header_indexes(header, selected_date)
    ]
    if not date_indexes:
        return
    for date_index in date_indexes:
        for row_id, row in sorted(row_by_id.items()):
            if row_id not in updated_row_ids or _metric_key_from_row_id(row_id) != "proxy_profit_rub":
                continue
            scope = _row_scope_from_row_id(row_id)
            value = _compute_proxy_profit_for_scope(row_by_id, scope=scope, date_index=date_index)
            _set_row_value(row, date_index, _to_sheet_cell_number(value))
        for row_id, row in sorted(row_by_id.items()):
            if row_id not in updated_row_ids or _metric_key_from_row_id(row_id) != "total_proxy_profit_rub":
                continue
            value = _sum_sku_metric_values(row_by_id, metric_key="proxy_profit_rub", date_index=date_index)
            _set_row_value(row, date_index, _to_sheet_cell_number(value))
        for row_id, row in sorted(row_by_id.items()):
            metric_key = _metric_key_from_row_id(row_id)
            if row_id not in updated_row_ids or metric_key not in {"proxy_margin_pct", "proxy_margin_pct_total"}:
                continue
            scope = _row_scope_from_row_id(row_id)
            order_sum_metric = "total_orderSum" if metric_key == "proxy_margin_pct_total" else "orderSum"
            profit_metric = "total_proxy_profit_rub" if metric_key == "proxy_margin_pct_total" else "proxy_profit_rub"
            order_sum = _row_metric_number(row_by_id, scope=scope, metric_key=order_sum_metric, date_index=date_index)
            profit = _row_metric_number(row_by_id, scope=scope, metric_key=profit_metric, date_index=date_index)
            value = None if order_sum is None or profit is None else (0.0 if order_sum == 0 else profit / order_sum)
            _set_row_value(row, date_index, _to_sheet_cell_number(value))


def _compute_proxy_profit_for_scope(
    row_by_id: Mapping[str, list[Any]],
    *,
    scope: str,
    date_index: int,
) -> float | None:
    order_sum = _row_metric_number(row_by_id, scope=scope, metric_key="orderSum", date_index=date_index)
    order_count = _row_metric_number(row_by_id, scope=scope, metric_key="orderCount", date_index=date_index)
    cost_price = _row_metric_number(row_by_id, scope=scope, metric_key="cost_price_rub", date_index=date_index)
    ads_sum = _row_metric_number(row_by_id, scope=scope, metric_key="ads_sum", date_index=date_index)
    if None in {order_sum, order_count, cost_price, ads_sum}:
        return None
    return float(order_sum) * 0.5096 - float(order_count) * 0.91 * float(cost_price) - float(ads_sum)


def _sum_sku_metric_values(
    row_by_id: Mapping[str, list[Any]],
    *,
    metric_key: str,
    date_index: int,
) -> float | None:
    values = [
        _cell_number(row[date_index] if date_index < len(row) else None)
        for row_id, row in row_by_id.items()
        if row_id.startswith("SKU:") and _metric_key_from_row_id(row_id) == metric_key
    ]
    numeric = [value for value in values if value is not None]
    return float(sum(numeric)) if numeric else None


def _row_metric_number(
    row_by_id: Mapping[str, list[Any]],
    *,
    scope: str,
    metric_key: str,
    date_index: int,
) -> float | None:
    row = row_by_id.get(f"{scope}|{metric_key}")
    if row is None or date_index >= len(row):
        return None
    return _cell_number(row[date_index])


def _cell_number(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _to_sheet_cell_number(value: float | None) -> float | str:
    return "" if value is None else float(value)


def _set_row_value(row: list[Any], index: int, value: Any) -> None:
    while index >= len(row):
        row.append("")
    row[index] = value


def _row_scope_from_row_id(row_id: str) -> str:
    return str(row_id).split("|", 1)[0] if "|" in str(row_id) else str(row_id)


def _metric_key_from_row_id(row_id: str) -> str:
    return str(row_id).split("|", 1)[1] if "|" in str(row_id) else ""


def _status_row_key(row: list[Any]) -> str:
    return str(row[0] or "").strip() if row else ""


def _status_row_source_base(row: list[Any]) -> str:
    source_key = _status_row_key(row)
    base, _ = _split_temporal_source_key(source_key)
    return base


def _status_row_temporal_slot(row: list[Any]) -> str:
    source_key = _status_row_key(row)
    _, temporal_slot = _split_temporal_source_key(source_key)
    return temporal_slot


def _duration_seconds(started_at: str, finished_at: str) -> float:
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        finish = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, (finish - start).total_seconds())


def _emit_plan_status_sheet_log(
    plan: SheetVitrinaV1Envelope,
    emit: OperatorLogEmitter,
    *,
    cycle: str,
) -> None:
    status_sheet = _find_sheet(plan, "STATUS")
    if status_sheet is None:
        emit(_format_log_event("status_sheet_missing", cycle=cycle, sheet="STATUS"))
        return
    emit(
        _format_log_event(
            "status_sheet_selected",
            cycle=cycle,
            sheet="STATUS",
            rows=status_sheet.row_count,
            columns=status_sheet.column_count,
        )
    )
    for row in status_sheet.rows:
        if len(row) < 11:
            continue
        source_key = str(row[0] or "")
        kind = str(row[1] or "")
        note = str(row[10] or "")
        if source_key == "registry_upload_current_state":
            emit(
                _format_log_event(
                    "status_registry_state",
                    cycle=cycle,
                    source=source_key,
                    kind=kind,
                    snapshot_date=row[3],
                    requested_count=row[7],
                    covered_count=row[8],
                    note=note,
                )
            )
            continue
        if source_key == DELIVERY_CONTRACT_VERSION:
            emit(
                _format_log_event(
                    "status_delivery_contract",
                    cycle=cycle,
                    source=source_key,
                    kind=kind,
                    snapshot_date=row[3],
                    requested_count=row[7],
                    covered_count=row[8],
                    note=note,
                )
            )
            continue
        source_name, temporal_slot = _split_temporal_source_key(source_key)
        spec = SOURCE_DIAGNOSTIC_SPECS.get(source_name, {})
        emit(
            _format_log_event(
                "snapshot_source_status",
                cycle=cycle,
                source=source_name,
                temporal_slot=temporal_slot,
                module=spec.get("module"),
                block=spec.get("block"),
                adapter=spec.get("adapter"),
                endpoint=spec.get("endpoint"),
                kind=kind,
                freshness=row[2],
                snapshot_date=row[3],
                date=row[4],
                date_from=row[5],
                date_to=row[6],
                requested_count=row[7],
                covered_count=row[8],
                missing_nm_ids=row[9],
                note=note,
            )
        )


def _emit_plan_metric_sheet_log(
    plan: SheetVitrinaV1Envelope,
    emit: OperatorLogEmitter,
    *,
    cycle: str,
) -> None:
    data_sheet = _find_sheet(plan, "DATA_VITRINA")
    if data_sheet is None:
        emit(_format_log_event("metric_sheet_missing", cycle=cycle, sheet="DATA_VITRINA"))
        return
    summaries: dict[str, dict[str, Any]] = {}
    slot_count = max(len(plan.date_columns), 1)
    for row in data_sheet.rows:
        if len(row) < 2:
            continue
        key = str(row[1] or "")
        if "|" not in key:
            continue
        scope_token, metric_key = key.split("|", 1)
        summary = summaries.setdefault(
            metric_key,
            {
                "label_ru": str(row[0] or ""),
                "row_scopes": set(),
                "rows": 0,
                "non_zero": 0,
                "zero": 0,
                "blank": 0,
                "text": 0,
            },
        )
        summary["row_scopes"].add(scope_token.split(":", 1)[0])
        summary["rows"] += 1
        for cell in row[2 : 2 + slot_count]:
            if cell in ("", None):
                summary["blank"] += 1
            elif isinstance(cell, (int, float)):
                if float(cell) == 0.0:
                    summary["zero"] += 1
                else:
                    summary["non_zero"] += 1
            else:
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
                cycle=cycle,
                metric_key=metric_key,
                label_ru=summary["label_ru"],
                row_scopes=",".join(sorted(summary["row_scopes"])),
                rows=summary["rows"],
                slot_cells=summary["rows"] * slot_count,
                non_zero=summary["non_zero"],
                zero=summary["zero"],
                blank=summary["blank"],
                text=summary["text"],
                blocked=blocked,
                blocked_source="promo_by_price" if blocked else "",
            )
        )


def _emit_bridge_result_log(
    bridge_result: dict[str, Any],
    emit: OperatorLogEmitter,
    *,
    cycle: str,
) -> None:
    emit(
        _format_log_event(
            "bridge_finish",
            cycle=cycle,
            bridge=bridge_result.get("bridge"),
            script_id=bridge_result.get("script_id"),
            spreadsheet_id=bridge_result.get("spreadsheet_id"),
        )
    )
    write_result = bridge_result.get("write_result")
    if isinstance(write_result, Mapping):
        for item in write_result.get("sheets", []) or []:
            if not isinstance(item, Mapping):
                continue
            emit(
                _format_log_event(
                    "bridge_write_sheet",
                    cycle=cycle,
                    sheet=item.get("sheet_name"),
                    row_count=item.get("row_count"),
                    write_rect=item.get("write_rect"),
                )
            )
    sheet_state = bridge_result.get("sheet_state")
    if isinstance(sheet_state, Mapping):
        for item in sheet_state.get("sheets", []) or []:
            if not isinstance(item, Mapping):
                continue
            emit(
                _format_log_event(
                    "bridge_sheet_state",
                    cycle=cycle,
                    sheet=item.get("sheet_name"),
                    present=item.get("present"),
                    last_row=item.get("last_row"),
                    last_column=item.get("last_column"),
                )
            )


def _split_temporal_source_key(source_key: str) -> tuple[str, str]:
    if source_key.endswith("]") and "[" in source_key:
        name, slot = source_key[:-1].split("[", 1)
        return name, slot
    return source_key, ""


def _format_log_event(event: str, **fields: Any) -> str:
    parts = [f"event={event}"]
    for key, value in fields.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, bool):
            normalized = str(value).lower()
        else:
            normalized = round(value, 6) if isinstance(value, float) else value
        text = str(normalized)
        if any(char.isspace() or char in {'"', ";", "="} for char in text):
            text = json.dumps(text, ensure_ascii=False)
        parts.append(f"{key}={text}")
    return " ".join(parts)


def _noop_log(_: str) -> None:
    return


def _empty_web_vitrina_activity_surface(
    *,
    log_message: str = "Последний релевантный run/log пока недоступен.",
    upload_message: str = "Последний upload-run по источникам пока недоступен.",
    update_message: str = "Сохранённый итог по текущему срезу пока недоступен.",
) -> dict[str, Any]:
    current_business_date = current_business_date_iso()
    previous_business_date = default_business_as_of_date()
    available_dates = sorted({current_business_date, previous_business_date})
    return {
        "log_block": {
            "title": "Лог",
            "subtitle": "Последний релевантный refresh-run",
            "status_label": "Нет данных",
            "tone": "neutral",
            "detail": "",
            "preview_lines": [],
            "line_count": 0,
            "download_path": "",
            "log_filename": "",
            "empty_message": log_message,
        },
        "upload_summary": {
            "title": "Загрузка данных",
            "subtitle": "",
            "detail": "",
            "updated_at": "",
            "items": [],
            "empty_message": upload_message,
        },
        "loading_table": {
            "title": "Загрузка данных",
            "subtitle": "",
            "detail": "",
            "updated_at": "",
            "today_date": current_business_date,
            "yesterday_date": previous_business_date,
            "available_dates": available_dates,
            "default_refresh_date": current_business_date,
            "groups": _web_vitrina_loading_table_groups(
                {},
                available_dates=available_dates,
                default_refresh_date=current_business_date,
            ),
            "columns": _web_vitrina_loading_table_columns(
                today_date=current_business_date,
                yesterday_date=previous_business_date,
            ),
            "rows": [],
            "empty_message": upload_message,
        },
        "update_summary": {
            "title": "Обновление данных",
            "subtitle": "",
            "detail": "",
            "updated_at": "",
            "items": [],
            "empty_message": update_message,
        },
    }


def _web_vitrina_source_status_missing_snapshot_activity_surface(
    *,
    requested_as_of_date: str,
    technical_detail: str,
    now: datetime,
    snapshot_as_of_date: str | None = None,
) -> dict[str, Any]:
    current_business_date = current_business_date_iso(now)
    previous_business_date = default_business_as_of_date(now)
    requested = str(requested_as_of_date or snapshot_as_of_date or "").strip()
    snapshot_date = str(snapshot_as_of_date or requested or "").strip()
    display_date = _format_ru_date(snapshot_date or requested)
    message = (
        f"Снимок за {display_date} не подготовлен. "
        "Нажмите «Загрузить и обновить», чтобы подготовить данные."
    )
    technical = str(technical_detail or "").strip()
    detail = (
        f"requested_as_of_date {requested or '—'} · "
        f"snapshot_as_of_date {snapshot_date or '—'} · "
        f"business_timezone {CANONICAL_BUSINESS_TIMEZONE_NAME}"
    )
    return {
        "log_block": {
            "title": "Лог",
            "subtitle": "Source-status details не загружены: отсутствует ready snapshot",
            "status_label": "Нет снимка",
            "tone": "warning",
            "detail": technical,
            "preview_lines": [technical] if technical else [],
            "line_count": 1 if technical else 0,
            "download_path": "",
            "log_filename": "",
            "empty_message": message,
        },
        "upload_summary": {
            "title": "Загрузка данных",
            "subtitle": "Снимок не подготовлен.",
            "detail": detail,
            "updated_at": "",
            "items": [],
            "empty_message": message,
        },
        "loading_table": {
            "title": "Загрузка данных",
            "subtitle": "Снимок не подготовлен.",
            "detail": detail,
            "updated_at": "",
            "today_date": current_business_date,
            "yesterday_date": previous_business_date,
            "available_dates": [],
            "default_refresh_date": "",
            "groups": [],
            "columns": [],
            "rows": [],
            "source_status_state": "missing_snapshot",
            "snapshot_as_of_date": snapshot_date,
            "requested_as_of_date": requested,
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "empty_message": message,
        },
    }


def _is_ready_snapshot_missing_error(exc: Exception) -> bool:
    return "ready snapshot missing" in str(exc)


def _format_ru_date(value: str) -> str:
    normalized = str(value or "").strip()
    try:
        parsed = datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError:
        return normalized or "выбранную дату"
    return parsed.strftime("%d.%m.%Y")


def _web_vitrina_source_status_not_loaded_activity_surface(
    *,
    snapshot_as_of_date: str,
    snapshot_id: str,
    refreshed_at: str,
    read_model: str,
) -> dict[str, Any]:
    current_business_date = current_business_date_iso()
    previous_business_date = default_business_as_of_date()
    return {
        "log_block": {
            "title": "Лог",
            "subtitle": "Лог не загружается вместе с первичным открытием страницы",
            "status_label": "Не загружено",
            "tone": "neutral",
            "detail": f"snapshot {snapshot_id} · as_of_date {snapshot_as_of_date} · {read_model}",
            "preview_lines": [],
            "line_count": 0,
            "download_path": "",
            "log_filename": "",
            "empty_message": "Нажмите «Загрузить» в блоке «Загрузка данных», чтобы прочитать source-status details и лог.",
        },
        "upload_summary": {
            "title": "Загрузка данных",
            "subtitle": "Статусы источников не загружены.",
            "detail": f"snapshot {snapshot_id} · as_of_date {snapshot_as_of_date} · {read_model}",
            "updated_at": refreshed_at,
            "items": [],
            "empty_message": "Статусы источников не загружены. Нажмите «Загрузить», чтобы посмотреть детали.",
        },
        "loading_table": {
            "title": "Загрузка данных",
            "subtitle": "Статусы источников не загружены.",
            "detail": f"snapshot {snapshot_id} · as_of_date {snapshot_as_of_date} · {read_model}",
            "updated_at": refreshed_at,
            "today_date": current_business_date,
            "yesterday_date": previous_business_date,
            "available_dates": [],
            "default_refresh_date": "",
            "groups": [],
            "columns": [],
            "rows": [],
            "source_status_state": "not_loaded",
            "empty_message": "Статусы источников не загружены. Нажмите «Загрузить», чтобы посмотреть детали.",
        },
    }


def _build_web_vitrina_log_block(
    *,
    latest_job: Mapping[str, Any] | None,
    job_path: str,
    persisted_refresh_status: Any | None = None,
) -> dict[str, Any]:
    if latest_job is None:
        semantic_label = (
            str(getattr(persisted_refresh_status, "semantic_label", "") or "Нет transient-лога")
            if persisted_refresh_status is not None
            else "Нет данных"
        )
        semantic_tone = (
            str(getattr(persisted_refresh_status, "semantic_tone", "") or "neutral")
            if persisted_refresh_status is not None
            else "neutral"
        )
        semantic_reason = (
            str(getattr(persisted_refresh_status, "semantic_reason", "") or "")
            if persisted_refresh_status is not None
            else ""
        )
        return {
            "title": "Лог",
            "subtitle": "Лог последнего refresh для текущего среза недоступен",
            "status_label": semantic_label,
            "tone": semantic_tone,
            "detail": semantic_reason,
            "preview_lines": [],
            "line_count": 0,
            "download_path": "",
            "log_filename": "",
            "empty_message": "Сохранённый итог есть, но transient refresh-log для этого среза недоступен.",
        }
    job_payload = _with_job_urls_from_job_snapshot(latest_job, job_path)
    semantic_status = str(((job_payload.get("result") or {}).get("semantic_status")) or "").strip()
    tone = semantic_status if semantic_status in {"success", "warning", "error"} else (
        "success" if str(job_payload.get("status", "")) == "success" else "error"
    )
    status_label = str(((job_payload.get("result") or {}).get("semantic_label")) or "").strip()
    detail_reason = str(((job_payload.get("result") or {}).get("semantic_reason")) or "").strip()
    preview_lines = [str(line) for line in (job_payload.get("log_lines") or []) if str(line).strip()]
    line_limit = 240
    truncated = len(preview_lines) > line_limit
    if truncated:
        preview_lines = preview_lines[-line_limit:]
    detail_parts = [
        f"job {job_payload.get('job_id', '')}",
        str(job_payload.get("operation", "")),
        str(job_payload.get("finished_at") or job_payload.get("started_at") or ""),
    ]
    if detail_reason:
        detail_parts.append(detail_reason)
    if truncated:
        detail_parts.append(f"показаны последние {line_limit} строк")
    return {
        "title": "Лог",
        "subtitle": "Последний релевантный refresh",
        "status_label": status_label or _semantic_status_label(tone),
        "tone": tone,
        "detail": " · ".join(part for part in detail_parts if part),
        "preview_lines": preview_lines,
        "line_count": int(job_payload.get("log_line_count") or len(preview_lines)),
        "download_path": str(job_payload.get("download_path") or ""),
        "log_filename": str(job_payload.get("log_filename") or ""),
        "empty_message": "Лог пока пуст.",
    }


def _with_job_urls_from_job_snapshot(job_payload: Mapping[str, Any], job_path: str) -> dict[str, Any]:
    normalized = dict(job_payload)
    job_id = str(normalized.get("job_id") or "").strip()
    operation = str(normalized.get("operation") or "refresh").strip() or "refresh"
    if not job_id:
        return normalized
    normalized["job_path"] = f"{job_path}?job_id={job_id}"
    normalized["download_path"] = f"{job_path}?job_id={job_id}&format=text&download=1"
    normalized["log_filename"] = f"sheet-vitrina-v1-{operation}-{job_id}.txt"
    return normalized


def _build_web_vitrina_endpoint_summary_block(
    *,
    title: str,
    subtitle: str,
    records: Mapping[str, Mapping[str, Any]],
    ordered_source_keys: list[str],
    empty_message: str,
    block_updated_at: str,
    block_detail: str,
) -> dict[str, Any]:
    if not ordered_source_keys:
        return {
            "title": title,
            "subtitle": subtitle,
            "detail": block_detail,
            "updated_at": block_updated_at,
            "items": [],
            "empty_message": empty_message,
        }
    items = []
    for source_order, source_key in enumerate(ordered_source_keys):
        items.append(
            _build_endpoint_summary_item(
                source_key=source_key,
                record=records.get(source_key),
                source_order=source_order,
            )
        )
    if not any(item.get("status_label") for item in items):
        items = []
    return {
        "title": title,
        "subtitle": subtitle,
        "detail": block_detail,
        "updated_at": block_updated_at,
        "items": items,
        "empty_message": empty_message,
    }


def _build_web_vitrina_loading_table(
    *,
    upload_summary: Mapping[str, Any],
    today_date: str,
    yesterday_date: str,
    available_dates: Iterable[str],
    default_refresh_date: str,
    metric_labels_by_source: Mapping[str, list[str]],
    group_last_updated_at: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    items = list(upload_summary.get("items") or [])
    rows: list[dict[str, Any]] = []
    for item in items:
        item_payload = dict(item or {})
        source_key = str(item_payload.get("source_key") or item_payload.get("endpoint_id") or "").strip()
        source_group_id = _source_group_id_for_source_key(source_key)
        today_status = _loading_table_status_for_slot(
            item_payload,
            target_date=today_date,
            temporal_slot=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        yesterday_status = _loading_table_status_for_slot(
            item_payload,
            target_date=yesterday_date,
            temporal_slot=TEMPORAL_SLOT_YESTERDAY_CLOSED,
        )
        rows.append(
            {
                "source_key": source_key,
                "source_group_id": source_group_id,
                "source_label": str(
                    item_payload.get("label_ru")
                    or item_payload.get("endpoint_label")
                    or source_key
                ),
                "today": today_status,
                "today_reason": str(today_status["reason"]),
                "yesterday": yesterday_status,
                "yesterday_reason": str(yesterday_status["reason"]),
                "metric_labels": list(metric_labels_by_source.get(source_key) or []),
                "technical_endpoint": str(
                    item_payload.get("endpoint_label")
                    or item_payload.get("technical_text")
                    or item_payload.get("technical_key")
                    or source_key
                ),
            }
        )
    return {
        "title": "Загрузка данных",
        "subtitle": str(upload_summary.get("subtitle") or ""),
        "detail": str(upload_summary.get("detail") or ""),
        "updated_at": str(upload_summary.get("updated_at") or ""),
        "today_date": today_date,
        "yesterday_date": yesterday_date,
        "available_dates": _normalize_available_refresh_dates(available_dates, default_refresh_date=default_refresh_date),
        "default_refresh_date": default_refresh_date,
        "groups": _web_vitrina_loading_table_groups(
            group_last_updated_at or {},
            available_dates=_normalize_available_refresh_dates(
                available_dates,
                default_refresh_date=default_refresh_date,
            ),
            default_refresh_date=default_refresh_date,
        ),
        "columns": _web_vitrina_loading_table_columns(
            today_date=today_date,
            yesterday_date=yesterday_date,
        ),
        "rows": rows,
        "source_status_state": "loaded" if rows else "empty",
        "empty_message": str(
            upload_summary.get("empty_message")
            or "Status payload не содержит source rows для текущего среза. Повторите загрузку или смотрите лог."
        ),
    }


def _web_vitrina_loading_table_groups(
    group_last_updated_at: Mapping[str, str],
    *,
    available_dates: Iterable[str] = (),
    default_refresh_date: str = "",
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    normalized_available_dates = _normalize_available_refresh_dates(
        available_dates,
        default_refresh_date=default_refresh_date,
    )
    min_date = normalized_available_dates[0] if normalized_available_dates else ""
    max_date = normalized_available_dates[-1] if normalized_available_dates else ""
    for group_id in WEB_VITRINA_SOURCE_GROUP_ORDER:
        config = WEB_VITRINA_SOURCE_GROUPS[group_id]
        groups.append(
            {
                "group_id": group_id,
                "label": str(config["label_ru"]),
                "source_keys": list(config["source_keys"]),
                "last_updated_at": str(group_last_updated_at.get(group_id) or ""),
                "refresh_action": {
                    "label": "Обновить группу",
                    "source_group_id": group_id,
                    "default_as_of_date": default_refresh_date,
                    "available_dates": normalized_available_dates,
                    "min_date": min_date,
                    "max_date": max_date,
                },
                "session_controls": group_id == "seller_portal_bot",
            }
        )
    return groups


def _normalize_available_refresh_dates(
    dates: Iterable[str],
    *,
    default_refresh_date: str,
) -> list[str]:
    normalized = {str(item).strip() for item in dates if str(item).strip()}
    if not normalized and default_refresh_date:
        normalized.add(default_refresh_date)
    return sorted(normalized)


def _default_group_refresh_date(dates: Iterable[str], *, preferred_date: str) -> str:
    normalized = sorted({str(item).strip() for item in dates if str(item).strip()})
    preferred = str(preferred_date or "").strip()
    if preferred and preferred in normalized:
        return preferred
    if normalized:
        return normalized[-1]
    return preferred


def _source_group_id_for_source_key(source_key: str) -> str:
    return WEB_VITRINA_SOURCE_KEY_TO_GROUP.get(str(source_key or "").strip(), "other_sources")


def _source_group_last_updated_at_for_snapshot(
    snapshot: SheetVitrinaV1Envelope,
    *,
    fallback_updated_at: str,
) -> dict[str, str]:
    metadata = dict(getattr(snapshot, "metadata", {}) or {})
    raw = metadata.get("source_group_last_updated_at")
    result = {str(key): str(value) for key, value in raw.items() if str(key) and str(value)} if isinstance(raw, Mapping) else {}
    for group_id in WEB_VITRINA_SOURCE_GROUP_ORDER:
        result.setdefault(group_id, fallback_updated_at)
    return result


def _web_vitrina_loading_table_columns(
    *,
    today_date: str,
    yesterday_date: str,
) -> list[dict[str, str]]:
    return [
        {"id": "source", "label": "Источник"},
        {"id": "today_status", "label": f"Сегодня: {today_date}"},
        {"id": "today_reason", "label": "Причина сегодня"},
        {"id": "yesterday_status", "label": f"Вчера: {yesterday_date}"},
        {"id": "yesterday_reason", "label": "Причина вчера"},
        {"id": "metrics", "label": "Метрики"},
        {"id": "technical_endpoint", "label": "Технический endpoint"},
    ]


def _build_activity_metric_labels_by_source(metrics: Iterable[Any]) -> dict[str, list[str]]:
    labels_by_key: dict[str, str] = {}
    for item in metrics:
        metric_key = str(getattr(item, "metric_key", "") or "").strip()
        label = str(getattr(item, "label_ru", "") or "").strip()
        enabled = bool(getattr(item, "enabled", True))
        if metric_key and label and enabled:
            labels_by_key[metric_key] = label
    result: dict[str, list[str]] = {}
    for source_key, metric_keys in WEB_VITRINA_SOURCE_METRIC_KEYS.items():
        labels: list[str] = []
        seen: set[str] = set()
        for metric_key in metric_keys:
            label = labels_by_key.get(metric_key)
            if not label or label in seen:
                continue
            labels.append(label)
            seen.add(label)
        result[source_key] = labels
    return result


def _build_endpoint_summary_item(
    *,
    source_key: str,
    record: Mapping[str, Any] | None,
    source_order: int,
) -> dict[str, Any]:
    copy = _web_vitrina_activity_item_copy(source_key)
    tone = str((record or {}).get("tone") or "warning")
    status_label = str((record or {}).get("status_label") or _semantic_status_label(tone))
    severity_rank = _activity_tone_rank(tone)
    if record is None:
        return {
            "endpoint_id": source_key,
            "endpoint_label": copy["endpoint_label"],
            "source_key": source_key,
            "label_ru": copy["label_ru"],
            "description_ru": copy["description_ru"],
            "reason_ru": "обновление не подтверждено",
            "technical_key": copy["technical_key"],
            "technical_text": copy["technical_text"],
            "status_label": "Внимание",
            "tone": "warning",
            "detail": "обновление не подтверждено",
            "slot_statuses": [],
            "severity_rank": _activity_tone_rank("warning"),
            "source_order": source_order,
        }
    reason_ru = _activity_reason_ru(
        tone=tone,
        detail=str(record.get("detail") or ""),
        note=str(record.get("note") or ""),
    )
    detail = _activity_summary_detail(
        description_ru=copy["description_ru"],
        reason_ru=reason_ru,
        fallback_detail=str(record.get("detail") or "").strip(),
    )
    return {
        "endpoint_id": source_key,
        "endpoint_label": copy["endpoint_label"],
        "source_key": source_key,
        "label_ru": copy["label_ru"],
        "description_ru": copy["description_ru"],
        "reason_ru": reason_ru,
        "technical_key": copy["technical_key"],
        "technical_text": copy["technical_text"],
        "status_label": status_label,
        "tone": tone,
        "detail": detail,
        "slot_statuses": _activity_slot_statuses(record),
        "severity_rank": severity_rank,
        "source_order": source_order,
    }


def _activity_slot_statuses(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for raw_slot in record.get("slots") or []:
        slot = dict(raw_slot or {})
        tone = str(slot.get("tone") or slot.get("status") or "warning").strip() or "warning"
        reason = _activity_reason_ru(
            tone=tone,
            detail=str(slot.get("reason") or ""),
            note=str(slot.get("note") or ""),
        )
        statuses.append(
            {
                "temporal_slot": str(slot.get("temporal_slot") or "snapshot"),
                "status": str(slot.get("status") or tone),
                "tone": tone,
                "status_label": str(slot.get("label") or _semantic_status_label(tone)),
                "reason": reason or ("Готово" if tone == "success" else _activity_reason_fallback(tone)),
                "snapshot_date": str(slot.get("snapshot_date") or ""),
                "date": str(slot.get("date") or ""),
                "date_from": str(slot.get("date_from") or ""),
                "date_to": str(slot.get("date_to") or ""),
                "kind": str(slot.get("kind") or "").strip().lower(),
                "note": str(slot.get("note") or "").strip(),
                "requested_count": _coerce_int(slot.get("requested_count")),
                "covered_count": _coerce_int(slot.get("covered_count")),
            }
        )
    return statuses


def _loading_table_status_for_slot(
    item: Mapping[str, Any],
    *,
    target_date: str,
    temporal_slot: str,
) -> dict[str, str]:
    source_key = str(item.get("source_key") or item.get("endpoint_id") or "").strip()
    temporal_policy = effective_source_temporal_policy(source_key, "")
    all_slots = [dict(slot) for slot in (item.get("slot_statuses") or [])]
    has_confirmed_yesterday_success = any(
        _loading_slot_has_confirmed_success(slot)
        for slot in all_slots
        if str(slot.get("temporal_slot") or "") == TEMPORAL_SLOT_YESTERDAY_CLOSED
    )
    matching_slots = [
        dict(slot)
        for slot in all_slots
        if _activity_slot_matches_date_or_slot(slot, target_date=target_date, temporal_slot=temporal_slot)
    ]
    if not matching_slots:
        nonblocking_reason = source_nonblocking_slot_reason(
            source_key=source_key,
            temporal_policy=temporal_policy,
            temporal_slot=temporal_slot,
            slot_outcome={},
            has_confirmed_yesterday_success=has_confirmed_yesterday_success,
        )
        if nonblocking_reason:
            return {
                "date": target_date,
                "ok": True,
                "label": "OK",
                "tone": "success",
                "reason": nonblocking_reason,
            }
        fallback_reason = str(item.get("reason_ru") or item.get("detail") or "").strip()
        return {
            "date": target_date,
            "ok": False,
            "label": "не OK",
            "tone": "error",
            "reason": fallback_reason or "нет подтверждённого статуса за дату",
        }
    worst_slot = sorted(
        matching_slots,
        key=lambda slot: _loading_slot_rank(
            source_key=source_key,
            temporal_policy=temporal_policy,
            temporal_slot=temporal_slot,
            slot=slot,
            has_confirmed_yesterday_success=has_confirmed_yesterday_success,
        ),
    )[0]
    ok = _loading_slot_is_semantic_ok(
        source_key=source_key,
        temporal_policy=temporal_policy,
        temporal_slot=temporal_slot,
        slot=worst_slot,
        has_confirmed_yesterday_success=has_confirmed_yesterday_success,
    )
    return {
        "date": target_date,
        "ok": ok,
        "label": "OK" if ok else "не OK",
        "tone": "success" if ok else "error",
        "reason": _loading_slot_reason(
            ok=ok,
            source_key=source_key,
            temporal_policy=temporal_policy,
            temporal_slot=temporal_slot,
            slot=worst_slot,
            has_confirmed_yesterday_success=has_confirmed_yesterday_success,
        ),
    }


def _loading_slot_rank(
    *,
    source_key: str,
    temporal_policy: str,
    temporal_slot: str,
    slot: Mapping[str, Any],
    has_confirmed_yesterday_success: bool,
) -> int:
    if _loading_slot_is_semantic_ok(
        source_key=source_key,
        temporal_policy=temporal_policy,
        temporal_slot=temporal_slot,
        slot=slot,
        has_confirmed_yesterday_success=has_confirmed_yesterday_success,
    ):
        return _activity_tone_rank("success")
    return _activity_tone_rank(str(slot.get("tone") or slot.get("status") or "warning"))


def _loading_slot_is_semantic_ok(
    *,
    source_key: str,
    temporal_policy: str,
    temporal_slot: str,
    slot: Mapping[str, Any],
    has_confirmed_yesterday_success: bool,
) -> bool:
    if not slot_counts_toward_source_status(
        source_key=source_key,
        temporal_policy=temporal_policy,
        temporal_slot=temporal_slot,
        slot_outcome=slot,
        has_confirmed_yesterday_success=has_confirmed_yesterday_success,
    ):
        return True
    if _loading_slot_has_confirmed_success(slot):
        return True
    return False


def _loading_slot_has_confirmed_success(slot: Mapping[str, Any]) -> bool:
    status = str(slot.get("status") or slot.get("tone") or "").strip()
    kind = str(slot.get("kind") or "").strip().lower()
    note = str(slot.get("note") or "").strip()
    if status == "success":
        return True
    return kind == "success" and _status_note_is_latest_confirmed(note)


def _loading_slot_reason(
    *,
    ok: bool,
    source_key: str,
    temporal_policy: str,
    temporal_slot: str,
    slot: Mapping[str, Any],
    has_confirmed_yesterday_success: bool,
) -> str:
    if ok:
        nonblocking_reason = source_nonblocking_slot_reason(
            source_key=source_key,
            temporal_policy=temporal_policy,
            temporal_slot=temporal_slot,
            slot_outcome=slot,
            has_confirmed_yesterday_success=has_confirmed_yesterday_success,
        )
        if nonblocking_reason:
            return nonblocking_reason
        note = str(slot.get("note") or "")
        if _status_note_is_latest_confirmed(note):
            return _humanize_note(note) or "использована последняя подтверждённая версия"
        return "Готово"
    tone = str(slot.get("tone") or slot.get("status") or "warning")
    return str(slot.get("reason") or _activity_reason_fallback(tone))


def _activity_slot_matches_date_or_slot(
    slot: Mapping[str, Any],
    *,
    target_date: str,
    temporal_slot: str,
) -> bool:
    if str(slot.get("temporal_slot") or "") == temporal_slot:
        return True
    for key in ("snapshot_date", "date", "date_from", "date_to"):
        if str(slot.get(key) or "") == target_date:
            return True
    return False


def _extract_upload_source_records_from_job(
    latest_job: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if latest_job is None:
        return {}
    result_payload = latest_job.get("result") or {}
    source_outcomes = result_payload.get("source_outcomes")
    if isinstance(source_outcomes, list) and source_outcomes:
        return _extract_source_records_from_outcomes(source_outcomes)
    records: dict[str, dict[str, Any]] = {}
    for line in latest_job.get("log_lines") or []:
        parsed = _parse_log_event_line(str(line))
        if parsed is None:
            continue
        event, fields = parsed
        if event != "source_step_finish":
            continue
        source_key = str(fields.get("source") or "").strip()
        if not source_key:
            continue
        _accumulate_source_record(
            records=records,
            source_key=source_key,
            temporal_slot=str(fields.get("temporal_slot") or ""),
            kind=str(fields.get("kind") or ""),
            note=str(fields.get("note") or ""),
            requested_count=_coerce_int(fields.get("requested_count")),
            covered_count=_coerce_int(fields.get("covered_count")),
        )
    return _finalize_source_records(records)


def _extract_source_records_from_outcomes(
    source_outcomes: list[Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for outcome in source_outcomes or []:
        source_key = str(outcome.get("source_key") or "").strip()
        if not source_key:
            continue
        records[source_key] = {
            "status": str(outcome.get("status") or "warning"),
            "tone": str(outcome.get("tone") or outcome.get("status") or "warning"),
            "status_label": str(outcome.get("label") or _semantic_status_label(str(outcome.get("status") or "warning"))),
            "detail": str(outcome.get("reason") or "").strip(),
            "note": "",
            "slots": list(outcome.get("slots") or []),
        }
    return records


def _collect_activity_source_keys(
    upload_records: Mapping[str, Mapping[str, Any]],
    update_records: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    seen = set(upload_records) | set(update_records)
    ordered = [source_key for source_key in SOURCE_DIAGNOSTIC_SPECS if source_key in seen]
    extras = sorted(source_key for source_key in seen if source_key not in SOURCE_DIAGNOSTIC_SPECS)
    return ordered + extras


def _ordered_activity_source_keys(
    source_keys: Iterable[str],
    records: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    ordered = list(source_keys)
    canonical_order = {source_key: index for index, source_key in enumerate(ordered)}
    return sorted(
        ordered,
        key=lambda source_key: (
            _activity_tone_rank(str((records.get(source_key) or {}).get("tone") or "warning")),
            canonical_order.get(source_key, len(canonical_order)),
            source_key,
        ),
    )


def _accumulate_source_record(
    *,
    records: dict[str, dict[str, Any]],
    source_key: str,
    temporal_slot: str,
    kind: str,
    note: str,
    requested_count: int = 0,
    covered_count: int = 0,
) -> None:
    bucket = records.setdefault(
        source_key,
        {
            "slot_records": [],
        },
    )
    normalized_slot = temporal_slot or "snapshot"
    status = _semantic_status_from_kind(
        kind=kind,
        note=note,
        requested_count=requested_count,
        covered_count=covered_count,
    )
    bucket["slot_records"].append(
        {
            "temporal_slot": normalized_slot,
            "status": status,
            "kind": str(kind or "").strip().lower(),
            "note": str(note or "").strip(),
            "requested_count": requested_count,
            "covered_count": covered_count,
            "reason": _slot_reason_from_log_record(
                kind=kind,
                note=note,
                requested_count=requested_count,
                covered_count=covered_count,
            ),
        }
    )


def _finalize_source_records(
    records: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    finalized: dict[str, dict[str, Any]] = {}
    for source_key, record in records.items():
        slot_records = list(record.get("slot_records") or [])
        if not slot_records:
            continue
        reduction = reduce_source_temporal_semantics(
            source_key=source_key,
            temporal_policy="",
            slot_outcomes=slot_records,
        )
        tone = str(reduction["status"])
        finalized[source_key] = {
            "status": tone,
            "tone": tone,
            "status_label": _semantic_status_label(tone),
            "detail": str(reduction["reason"]),
            "note": "",
            "slots": slot_records,
        }
    return finalized


def _semantic_status_from_kind(
    *,
    kind: str,
    note: str,
    requested_count: int,
    covered_count: int,
) -> str:
    normalized_kind = str(kind).strip().lower()
    if normalized_kind in {"error", "closure_exhausted"}:
        return "error"
    if normalized_kind in {
        "missing",
        "incomplete",
        "not_available",
        "blocked",
        "closure_pending",
        "closure_retrying",
        "closure_rate_limited",
        "not_found",
    }:
        return "warning"
    if normalized_kind != "success":
        return "warning"
    if _note_requires_warning(note):
        return "warning"
    if requested_count > 0 and covered_count < requested_count:
        return "warning"
    return "success"


def _slot_reason_from_log_record(
    *,
    kind: str,
    note: str,
    requested_count: int,
    covered_count: int,
) -> str:
    normalized_kind = str(kind).strip().lower()
    human_note = _humanize_note(note)
    if normalized_kind == "success" and requested_count > 0 and covered_count < requested_count:
        return _coverage_reason(requested_count=requested_count, covered_count=covered_count)
    if normalized_kind == "incomplete":
        return _coverage_reason(requested_count=requested_count, covered_count=covered_count)
    if normalized_kind in {"closure_pending", "closure_retrying", "closure_rate_limited"} and human_note:
        return human_note
    if normalized_kind == "closure_exhausted":
        return human_note or "retry исчерпан"
    if human_note:
        return human_note
    if normalized_kind == "success":
        return "обновление подтверждено"
    if normalized_kind == "missing":
        return "данные не получены"
    if normalized_kind == "not_available":
        return "источник не обновлялся"
    if normalized_kind == "not_found":
        return "источник не вернул данные"
    return normalized_kind or "нужна проверка"


def _slot_sort_key(slot: str) -> tuple[int, str]:
    if slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
        return (0, slot)
    if slot == TEMPORAL_SLOT_TODAY_CURRENT:
        return (1, slot)
    if slot == "snapshot":
        return (2, slot)
    return (3, slot)


def _slot_label(slot: str) -> str:
    if slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
        return "вчера"
    if slot == TEMPORAL_SLOT_TODAY_CURRENT:
        return "сегодня"
    if slot == "snapshot":
        return "срез"
    return slot


def _coverage_reason(*, requested_count: int, covered_count: int) -> str:
    if requested_count <= 0:
        return "покрытие не подтверждено"
    if covered_count <= 0:
        return f"нет покрытия по {requested_count} позициям"
    return f"покрыто {covered_count} из {requested_count}"


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def _humanize_note(note: str) -> str:
    normalized = str(note or "").strip()
    if not normalized:
        return ""
    replacements = (
        (
            "seller_portal_session_invalid",
            "сессия seller portal больше не действует; требуется повторный вход",
        ),
        (
            "seller_portal_session_missing",
            "сессия seller portal отсутствует; требуется повторный вход",
        ),
        (
            "seller_portal_wrong_supplier",
            "после входа выбран не тот кабинет; требуется recovery с переключением supplier",
        ),
        (
            "source is not available for today_current in the bounded live contour; today column stays blank instead of inventing fresh values",
            "текущий день для этого источника не требуется",
        ),
        (
            "source is current-only in the bounded live contour; yesterday_closed is left blank instead of backfilling current values into a closed-day column",
            "закрытый день materialize-ится через current-rollover",
        ),
        (
            "resolution_rule=accepted_closed_preserved_after_invalid_attempt",
            "использована последняя подтверждённая закрытая версия",
        ),
        (
            "resolution_rule=accepted_current_preserved_after_invalid_attempt",
            "использована последняя подтверждённая текущая версия",
        ),
        (
            "resolution_rule=accepted_closed_from_prior_current_snapshot",
            "использована подтверждённая версия предыдущего дня",
        ),
        (
            "resolution_rule=accepted_closed_from_prior_current_cache",
            "использована подтверждённая версия из runtime cache",
        ),
        (
            "resolution_rule=accepted_closed_runtime_snapshot",
            "использована последняя подтверждённая закрытая версия",
        ),
        (
            "resolution_rule=accepted_closed_from_interval_replay",
            "использована сохранённая закрытая версия из interval replay",
        ),
        (
            "resolution_rule=accepted_prior_current_runtime_cache",
            "использована подтверждённая версия из runtime cache",
        ),
        (
            "resolution_rule=exact_date_stocks_history_runtime_cache",
            "использована сохранённая версия на точную дату",
        ),
        (
            "resolution_rule=exact_date_promo_current_runtime_cache",
            "использована сохранённая версия на точную дату",
        ),
        (
            "resolution_rule=exact_date_runtime_cache",
            "использована сохранённая версия на точную дату",
        ),
        (
            "invalid_exact_snapshot=zero_filled_seller_funnel_snapshot",
            "источник вернул нулевой результат",
        ),
        (
            "invalid_exact_snapshot=zero_filled_web_source_snapshot",
            "источник вернул нулевой результат",
        ),
        (
            "invalid_exact_snapshot=zero_filled_prices_snapshot",
            "источник вернул нулевой результат",
        ),
        (
            "invalid_exact_snapshot=zero_filled_ads_bids_snapshot",
            "источник вернул нулевой результат",
        ),
        (
            "invalid_exact_snapshot=promo_live_source_incomplete",
            "получена неполная версия",
        ),
        ("no payload returned", "данные не получены"),
    )
    for marker, message in replacements:
        if marker in normalized:
            return message
    if "closure_state=closure_retrying" in normalized:
        return "источник ещё не закрылся на нужную дату; будет повторная попытка"
    if "closure_state=closure_pending" in normalized:
        return "источник ещё не закрылся на нужную дату"
    if "closure_state=closure_rate_limited" in normalized:
        return "источник ограничил запросы; повторная попытка запланирована"
    if "closure_state=closure_exhausted" in normalized:
        return "повторные попытки исчерпаны"
    if "resolution_rule=latest_effective_from<=slot_date" in normalized:
        return ""
    return normalized


def _note_requires_warning(note: str) -> bool:
    normalized = str(note or "").strip()
    if not normalized:
        return False
    if _status_note_is_latest_confirmed(normalized):
        return False
    success_markers = {
        "resolution_rule=accepted_closed_current_attempt",
        "resolution_rule=accepted_current_current_attempt",
        "resolution_rule=latest_effective_from<=slot_date",
    }
    if any(marker in normalized for marker in success_markers):
        return False
    warning_markers = {
        "runtime_cache",
        "preserved_after_invalid_attempt",
        "resolution_rule=accepted_closed_from_",
        "resolution_rule=accepted_closed_runtime_snapshot",
        "resolution_rule=accepted_prior_current_runtime_cache",
    }
    return any(marker in normalized for marker in warning_markers)


def _status_note_is_latest_confirmed(note: str) -> bool:
    normalized = str(note or "").strip().lower()
    if not normalized:
        return False
    latest_confirmed_tokens = (
        "latest_confirmed",
        "fallback",
        "runtime_cache",
        "accepted_closed_runtime_snapshot",
        "accepted_current_runtime_snapshot",
        "accepted_closed_from_prior_current_snapshot",
        "accepted_closed_from_prior_current_cache",
        "accepted_prior_current_runtime_cache",
        "exact_date_provisional_runtime_cache",
        "accepted_closed_from_interval_replay",
        "accepted_current_from_prior",
        "accepted_closed_preserved_after_invalid_attempt",
        "accepted_current_preserved_after_invalid_attempt",
        "exact_date_stocks_history_runtime_cache",
        "exact_date_promo_current_runtime_cache",
        "exact_date_runtime_cache",
    )
    return any(token in normalized for token in latest_confirmed_tokens)


def _worst_tone(statuses: Iterable[str]) -> str:
    values = [str(item or "").strip() for item in statuses]
    if any(item == "error" for item in values):
        return "error"
    if any(item == "warning" for item in values):
        return "warning"
    return "success"


def _activity_tone_rank(tone: str) -> int:
    return WEB_VITRINA_ACTIVITY_TONE_RANK.get(str(tone or "").strip(), 4)


def _web_vitrina_activity_item_copy(source_key: str) -> dict[str, str]:
    spec = SOURCE_DIAGNOSTIC_SPECS.get(source_key, {})
    item_copy = WEB_VITRINA_ACTIVITY_ITEM_COPY.get(source_key, {})
    endpoint_label = str(spec.get("endpoint") or "").strip()
    technical_parts = [source_key] if source_key else []
    if endpoint_label:
        technical_parts.append(endpoint_label)
    return {
        "label_ru": str(item_copy.get("label_ru") or source_key),
        "description_ru": str(item_copy.get("description_ru") or ""),
        "technical_key": source_key,
        "technical_text": " · ".join(part for part in technical_parts if part),
        "endpoint_label": endpoint_label,
    }


def _activity_reason_ru(*, tone: str, detail: str, note: str) -> str:
    normalized_tone = str(tone or "").strip()
    if normalized_tone == "success":
        return ""
    candidate = _humanize_activity_reason_text(detail, tone=normalized_tone) or _humanize_activity_reason_text(
        note,
        tone=normalized_tone,
    )
    if candidate:
        return candidate
    if normalized_tone == "error":
        return "источник завершился ошибкой, подробности доступны в логе"
    if normalized_tone == "warning":
        return "обновление не подтверждено, подробности доступны в логе"
    return ""


def _humanize_activity_reason_text(text: str, *, tone: str = "") -> str:
    normalized = _normalize_activity_reason_text(text)
    if not normalized:
        return ""
    parts: list[tuple[int, int, str]] = []
    for index, raw_part in enumerate(normalized.split(" · ")):
        prefix, body = _split_activity_reason_part(raw_part)
        humanized = _summarize_activity_reason_part(body, prefix=prefix)
        if not humanized:
            continue
        parts.append((_activity_reason_part_rank(body, humanized), index, humanized))
    deduplicated = list(
        dict.fromkeys(
            summary
            for _rank, _index, summary in sorted(parts, key=lambda item: (item[0], item[1], item[2]))
        )
    )
    if deduplicated:
        if len(deduplicated) == 1:
            single = deduplicated[0]
            for prefix in ("сегодня ", "вчера ", "за вчера ", "срез "):
                if single.startswith(prefix):
                    return _truncate_activity_reason(single[len(prefix) :].strip())
        return _truncate_activity_reason("; ".join(deduplicated[:2]))
    if _looks_like_technical_activity_reason_text(normalized):
        return _activity_reason_fallback(tone)
    humanized_note = _humanize_note(normalized)
    if humanized_note and humanized_note != normalized:
        return _truncate_activity_reason(humanized_note)
    if normalized == "обновление подтверждено":
        return ""
    return _truncate_activity_reason(normalized)


def _normalize_activity_reason_text(text: str) -> str:
    return " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split())


def _split_activity_reason_part(part: str) -> tuple[str, str]:
    normalized = str(part or "").strip()
    if ": " not in normalized:
        return "", normalized
    prefix_candidate, body = normalized.split(": ", 1)
    if prefix_candidate not in {"вчера", "сегодня", "snapshot", "срез"}:
        return "", normalized
    prefix = "срез" if prefix_candidate == "snapshot" else prefix_candidate
    return prefix, body.strip()


def _summarize_activity_reason_part(text: str, *, prefix: str = "") -> str:
    normalized = _normalize_activity_reason_text(text)
    if not normalized:
        return ""
    lowered = normalized.lower()
    if _activity_reason_is_success_only(lowered):
        return ""

    rate_limited = _activity_reason_has_any(
        lowered,
        "429",
        "too many requests",
        "retry-after",
        "rate limit",
        "ограничил запросы",
        "closure_rate_limited",
    )
    session_invalid = _activity_reason_has_any(
        lowered,
        "seller_portal_session_invalid",
        "seller_portal_session_missing",
        "seller_portal_wrong_supplier",
        "manual_relogin_required=login_and_save_state",
    )
    sync_failed = _activity_reason_has_any(
        lowered,
        "current_day_web_source_sync_failed=",
        "closed_day_sync_error=",
        "sync failed",
    )
    timeout = _activity_reason_has_any(
        lowered,
        "timeout",
        "timed out",
        "response not captured",
        "not captured",
        "deadline exceeded",
    )
    no_data = _activity_reason_has_any(
        lowered,
        "no payload returned",
        "payload не materialized",
        "payload not materialized",
        "источник не вернул payload",
        "данные не получены",
    )
    empty = _activity_reason_has_any(
        lowered,
        "no compact ads rows returned",
        "empty result",
        "empty payload",
        "пустой результат",
        "вернул пустой результат",
    )
    zero = _activity_reason_has_any(
        lowered,
        "zero_filled",
        "нулев",
    ) or ("invalid_exact_snapshot" in lowered and "=0" in lowered)
    incomplete = _activity_reason_has_any(
        lowered,
        "promo_live_source_incomplete",
        "получена неполная версия",
        "incomplete",
    )
    requested_date_mismatch = (
        ("requested_date=" in lowered and "latest_available_date=" in lowered)
        or ("requested_window=" in lowered and "latest_available_window=" in lowered)
    )
    blocked = _activity_reason_has_any(
        lowered,
        "collector_status=blocked",
        "источник помечен как blocked",
        "source is blocked",
    )
    not_refreshed = _activity_reason_has_any(
        lowered,
        "persisted status не содержит итог по источнику",
        "слот не обновлялся",
        "not refreshed",
        "неактуаль",
        "stale",
        "invalid_exact_snapshot",
    )
    unchanged = _activity_reason_has_any(
        lowered,
        "unchanged",
        "no-op",
        "not changed",
        "обновление не изменило",
    )

    failure_clause = ""
    if session_invalid:
        failure_clause = (
            "после входа выбран не тот кабинет; требуется повторный recovery"
            if "seller_portal_wrong_supplier" in lowered
            else "сессия seller portal больше не действует; требуется повторный вход"
        )
    elif rate_limited and sync_failed and timeout:
        failure_clause = "источник временно ограничил запросы, а дополнительная синхронизация завершилась по таймауту"
    elif rate_limited and sync_failed:
        failure_clause = "источник временно ограничил запросы, а дополнительная синхронизация завершилась с ошибкой"
    elif rate_limited:
        failure_clause = "источник временно ограничил запросы"
    elif sync_failed and timeout:
        failure_clause = "дополнительная синхронизация завершилась по таймауту"
    elif sync_failed:
        failure_clause = "дополнительная синхронизация завершилась с ошибкой"
    elif timeout:
        failure_clause = "запрос завершился по таймауту"

    data_clause = ""
    if no_data:
        data_clause = "данные не получены"
    elif empty:
        data_clause = "источник вернул пустой результат"
    elif zero:
        data_clause = "источник вернул нулевые данные, обновление не подтверждено"
    elif incomplete:
        data_clause = "получена неполная версия"
    elif requested_date_mismatch:
        data_clause = "получены данные за предыдущую доступную дату"
    elif blocked:
        data_clause = "источник временно недоступен"
    elif unchanged:
        data_clause = "обновление не изменило данные"
    elif not_refreshed:
        data_clause = "обновление не подтверждено"

    state_clause = ""
    if _activity_reason_has_any(lowered, "closure_state=closure_exhausted", "retry для closed-day snapshot исчерпан"):
        state_clause = "повторные попытки исчерпаны"
    elif _activity_reason_has_any(
        lowered,
        "closure_state=closure_retrying",
        "closed-day snapshot ещё не принят; будет retry",
        "closed-day snapshot ещё не готов; ожидается retry",
    ):
        state_clause = "повторная попытка уже запланирована"
    elif "closure_state=closure_pending" in lowered:
        state_clause = "источник ещё не закрылся на нужную дату"
    elif _activity_reason_has_any(
        lowered,
        "resolution_rule=accepted_closed_from_prior_current_cache",
        "resolution_rule=accepted_prior_current_runtime_cache",
        "resolution_rule=exact_date_stocks_history_runtime_cache",
        "resolution_rule=exact_date_promo_current_runtime_cache",
        "resolution_rule=exact_date_runtime_cache",
        "runtime cache",
    ):
        state_clause = "использована подтверждённая версия из runtime cache"
    elif _activity_reason_has_any(
        lowered,
        "resolution_rule=accepted_closed_preserved_after_invalid_attempt",
        "resolution_rule=accepted_current_preserved_after_invalid_attempt",
        "resolution_rule=accepted_closed_from_prior_current_snapshot",
        "resolution_rule=accepted_closed_runtime_snapshot",
        "resolution_rule=accepted_closed_from_interval_replay",
        "interval_replay",
        "interval replay",
        "сохранён ранее принятый closed snapshot после невалидной попытки",
        "использован ранее принятый current snapshot предыдущего дня",
        "использована последняя подтверждённая закрытая версия",
    ):
        state_clause = "использована последняя подтверждённая версия"

    primary_clause = _join_activity_reason_clauses(data_clause, failure_clause)
    clauses = [clause for clause in (primary_clause, state_clause) if clause]
    if not clauses:
        mapped = _mapped_activity_reason_text(normalized)
        if not mapped:
            return ""
        clauses = [mapped]
    summary = "; ".join(dict.fromkeys(clauses[:2]))
    return _apply_activity_reason_prefix(summary, prefix)


def _mapped_activity_reason_text(text: str) -> str:
    replacements = (
        ("seller_portal_session_invalid", "сессия seller portal больше не действует; требуется повторный вход"),
        ("seller_portal_session_missing", "сессия seller portal отсутствует; требуется повторный вход"),
        ("seller_portal_wrong_supplier", "после входа выбран не тот кабинет; требуется recovery с переключением supplier"),
        ("Persisted STATUS не содержит итог по источнику", "итог по источнику не подтверждён"),
        ("payload не materialized", "данные не получены"),
        ("источник не вернул payload", "данные не получены"),
        ("слот не обновлялся в текущем contour", "источник не обновлялся"),
        ("источник помечен как blocked в текущем contour", "источник временно недоступен"),
        ("использован сохранённый current snapshot из runtime cache", "использована подтверждённая версия из runtime cache"),
        ("использован ранее принятый current snapshot из runtime cache", "использована подтверждённая версия из runtime cache"),
        ("использован ранее принятый closed-day snapshot", "использована последняя подтверждённая версия"),
        ("использована сохранённая закрытая версия из interval replay", "использована сохранённая закрытая версия"),
        ("closed-day snapshot ещё не готов; ожидается retry", "источник ещё не закрылся на нужную дату"),
        ("closed-day snapshot ещё не принят; будет retry", "источник ещё не закрылся на нужную дату"),
        ("retry для closed-day snapshot исчерпан", "повторные попытки исчерпаны"),
        ("источник ограничил запросы; retry запланирован", "источник ограничил запросы; повторная попытка запланирована"),
        ("источник не вернул данные на точную дату", "данные на нужную дату не получены"),
        ("обновление подтверждено", ""),
    )
    for marker, replacement in replacements:
        if marker in text:
            return replacement
    humanized_note = _humanize_note(text)
    if humanized_note and humanized_note != text:
        return humanized_note
    if _looks_like_technical_activity_reason_text(text):
        return ""
    return text


def _activity_reason_has_any(text: str, *markers: str) -> bool:
    return any(marker in text for marker in markers)


def _activity_reason_part_rank(raw_text: str, humanized: str) -> int:
    lowered = _normalize_activity_reason_text(raw_text).lower()
    if _activity_reason_has_any(
        lowered,
        "429",
        "too many requests",
        "timeout",
        "timed out",
        "current_day_web_source_sync_failed=",
        "closed_day_sync_error=",
        "no payload returned",
        "empty result",
        "zero_filled",
        "invalid_exact_snapshot",
        "collector_status=blocked",
    ):
        return 0
    if _activity_reason_has_any(
        humanized.lower(),
        "использована",
        "повторная попытка",
        "источник ещё не закрылся",
    ):
        return 1
    return 2


def _activity_reason_is_success_only(lowered: str) -> bool:
    success_markers = (
        "resolution_rule=accepted_closed_current_attempt",
        "resolution_rule=accepted_current_current_attempt",
        "resolution_rule=latest_effective_from<=slot_date",
        "resolution_rule=explicit_or_latest_date_match",
        "accepted_at=",
    )
    if not any(marker in lowered for marker in success_markers):
        return False
    non_success_markers = (
        "invalid_exact_snapshot",
        "current_day_web_source_sync_failed=",
        "closed_day_sync_error=",
        "collector_status=blocked",
        "429",
        "too many requests",
        "timeout",
        "timed out",
        "no payload returned",
        "empty result",
        "no compact ads rows returned",
        "runtime cache",
        "preserved_after_invalid_attempt",
        "accepted_closed_from_",
    )
    return not any(marker in lowered for marker in non_success_markers)


def _join_activity_reason_clauses(primary: str, secondary: str) -> str:
    first = str(primary or "").strip()
    second = str(secondary or "").strip()
    if first and second:
        return f"{first}, а {second}"
    return first or second


def _apply_activity_reason_prefix(reason: str, prefix: str) -> str:
    normalized = str(reason or "").strip()
    if not normalized:
        return ""
    if prefix == "сегодня":
        return f"сегодня {normalized}"
    if prefix == "вчера":
        if normalized.startswith("использована"):
            return f"за вчера {normalized}"
        return f"вчера {normalized}"
    return normalized


def _looks_like_technical_activity_reason_text(text: str) -> bool:
    lowered = _normalize_activity_reason_text(text).lower()
    if not lowered:
        return False
    markers = (
        "{",
        "}",
        "traceback",
        "requestid",
        "statustext",
        "origin=",
        "timestamp=",
        "resolution_rule=",
        "accepted_at=",
        "current_day_web_source_sync_failed=",
        "closed_day_sync_error=",
        "collector_mode=",
        "trace_run_dir=",
        "collector_status=",
        "manual_relogin_required=",
        "final_url=",
        "archive_reuse_enabled=",
        "archive_mode=",
        "archive_scanned=",
        "archive_created=",
        "archive_updated=",
        "archive_unchanged=",
        "archive_keys=",
        "covering_campaigns=",
        "usable_campaigns=",
        "playwright._impl._errors",
        "runtimeerror:",
        "http://",
        "https://",
    )
    return any(marker in lowered for marker in markers)


def _activity_reason_fallback(tone: str) -> str:
    if str(tone or "").strip() == "error":
        return "источник завершился ошибкой, подробности доступны в логе"
    return "обновление не подтверждено, подробности доступны в логе"


def _truncate_activity_reason(text: str, *, limit: int = 220) -> str:
    normalized = str(text or "").strip()
    clauses = [clause.strip() for clause in normalized.split("; ") if clause.strip()]
    if len(clauses) > 2:
        normalized = "; ".join(clauses[:2])
    if len(normalized) <= limit:
        return normalized
    head, _, _tail = normalized.partition("; ")
    if head and len(head) <= limit:
        return head
    return normalized[: max(limit - 1, 0)].rstrip(" ,;:") + "…"


def _activity_summary_detail(
    *,
    description_ru: str,
    reason_ru: str,
    fallback_detail: str,
) -> str:
    description = str(description_ru or "").strip()
    reason = str(reason_ru or "").strip()
    if description and reason:
        return f"{description} Причина: {reason}"
    if description:
        return description
    if reason:
        return reason
    return str(fallback_detail or "").strip()


def _first_distinct_note(notes: list[str]) -> str:
    for note in notes:
        normalized = str(note).strip()
        if normalized:
            return normalized
    return ""


def _parse_log_event_line(line: str) -> tuple[str, dict[str, str]] | None:
    text = str(line or "").strip()
    if not text:
        return None
    if " " in text:
        _, candidate = text.split(" ", 1)
    else:
        candidate = text
    try:
        parts = shlex.split(candidate)
    except ValueError:
        return None
    fields: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[str(key)] = str(value)
    event = str(fields.get("event") or "").strip()
    if not event:
        return None
    return event, fields
