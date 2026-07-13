"""Application-слой HTTP entrypoint для registry upload и sheet_vitrina_v1 operator flow."""

from __future__ import annotations

import hashlib
import importlib
from io import BytesIO
import json
import re
import sqlite3
import time
import zipfile
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import shlex
import threading
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from packages.application.factory_order_supply import FactoryOrderSupplyBlock
from packages.application.ff_stock_ledger import FfStockLedgerBlock
from packages.application.fulfillment_services import FulfillmentServicesBlock
from packages.application.our_wb_costs import OurWbCostBlock
from packages.application.own_product_capital import OwnProductCapitalBlock
from packages.application.wb_finance_weekly import block_from_env
from packages.application.promo_live_source import PromoLiveSourceBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_daily_report import SheetVitrinaV1DailyReportBlock
from packages.application.sheet_vitrina_v1_feedbacks import SheetVitrinaV1FeedbacksBlock
from packages.application.sheet_vitrina_v1_feedbacks_ai import SheetVitrinaV1FeedbacksAiBlock
from packages.application.sheet_vitrina_v1_feedbacks_auto_complaints import SheetVitrinaV1FeedbacksAutoComplaintsBlock
from packages.application.sheet_vitrina_v1_feedbacks_complaints import SheetVitrinaV1FeedbacksComplaintsBlock
from packages.application.sheet_vitrina_v1_ads import SheetVitrinaV1AdsBlock
from packages.application.wb_prices_management import WbPricesManagementBlock, WbPricesSafetyConfig
from packages.application.wb_spp_tester import WbSppTesterBlock
from packages.application.sku_management import SkuManagementBlock
from packages.application.sheet_vitrina_v1_load_bridge import (
    LEGACY_GOOGLE_SHEETS_ARCHIVE_MESSAGE,
    LegacyGoogleSheetsContourArchivedError,
    legacy_google_sheets_archive_context,
    load_sheet_vitrina_ready_snapshot_via_clasp,
)
from packages.application.sheet_vitrina_v1_plan_report import SheetVitrinaV1PlanReportBlock
from packages.application.sheet_vitrina_v1_research import SheetVitrinaV1ResearchBlock
from packages.application.sheet_vitrina_v1_auto_refresh import (
    DEFAULT_SCHEDULE_MODE as SHEET_AUTO_REFRESH_SCHEDULE_MODE,
    DEFAULT_SCHEDULE_SOURCE as SHEET_AUTO_REFRESH_SCHEDULE_SOURCE,
    DEFAULT_SYSTEMD_ONCALENDAR as SHEET_AUTO_REFRESH_TICK_ONCALENDAR,
    SheetVitrinaV1AutoRefreshSchedulesBlock,
)
from packages.application.sheet_vitrina_v1_stock_report import SheetVitrinaV1StockReportBlock
from packages.application.sheet_vitrina_v1_stock_report import list_active_sku_options
from packages.application.supplier_shipments import SupplierShipmentsBlock
from packages.application.supplier_financial_documents import (
    SupplierFinancialDocumentsBlock,
    apply_supplier_order_document_match,
)
from packages.application.cny_ledger import CnyLedgerBlock
from packages.application.sheet_vitrina_v1_onec_stocks import (
    ONEC_INVENTORY_CAPITAL_RETURN_PCT_METRIC_KEY,
    ONEC_INVENTORY_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY,
    ONEC_PROXY_MARGIN_2_PCT_METRIC_KEY,
    ONEC_PROXY_MARGIN_2_PCT_TOTAL_METRIC_KEY,
    ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY,
    ONEC_STOCKS_METRIC_KEYS,
    ONEC_STOCKS_SOURCE_GROUP_ID,
    ONEC_STOCKS_SOURCE_GROUP_LABEL_RU,
    ONEC_STOCKS_SOURCE_KEY,
    ONEC_STOCKS_SKU_TOTAL_QTY_METRIC_KEY,
    ONEC_STOCKS_SKU_TOTAL_COST_RUB_METRIC_KEY,
    ONEC_STOCKS_STAGE_KEYS,
    ONEC_STOCKS_TOTAL_QTY_METRIC_KEY,
    ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY,
    ONEC_STOCKS_WB_UNIT_COST_RUB_METRIC_KEY,
    ONEC_TOTAL_PROXY_PROFIT_2_RUB_METRIC_KEY,
    extend_metrics_with_onec_stock_metrics,
    onec_stage_metric_key,
    onec_stage_total_metric_key,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (
    OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    TOTAL_OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    extend_metrics_with_our_wb_cost_metrics,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_PRODUCT_CAPITAL_METRIC_KEYS,
    OWN_PRODUCT_CAPITAL_SOURCE_GROUP_ID,
    OWN_PRODUCT_CAPITAL_SOURCE_GROUP_LABEL_RU,
    OWN_PRODUCT_CAPITAL_SOURCE_KEY,
    extend_metrics_with_own_product_capital_metrics,
)
from packages.application.sheet_vitrina_v1_sku_actions import (
    ADVERTISING_BID_CHANGE_RUB_METRIC_KEY,
    BUYER_PRICE_RUB_METRIC_KEY,
    SELLER_PRICE_CHANGE_RUB_METRIC_KEY,
    extend_metrics_with_sku_action_metrics,
)
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
    resolve_web_vitrina_default_period,
)
from packages.application.web_vitrina_view_model import build_web_vitrina_view_model
from packages.application.wb_regional_supply import WbRegionalSupplyBlock
from packages.application.wb_regional_supply_planning import WbRegionalSupplyPlanningBlock
from packages.application.wb_supplies import WbSuppliesBlock
from apps.promo_campaign_archive_gc import run_promo_campaign_archive_light_gc
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    DAILY_REFRESH_BUSINESS_HOURS,
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
from packages.contracts.supplier_financial_documents import (
    FINANCIAL_DOCUMENT_PARSE_STATUS_EXCLUDED,
    FINANCIAL_DOCUMENT_PARSE_STATUS_NEEDS_REVIEW,
    FINANCIAL_DOCUMENT_PARSE_STATUS_PARSE_ERROR,
    FINANCIAL_DOCUMENT_TYPE_BANK_CONTROL_STATEMENT,
    FINANCIAL_DOCUMENT_TYPE_BANK_FEE_STATEMENT,
    FINANCIAL_DOCUMENT_TYPE_BANK_TRANSFER_APPLICATION,
    FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION,
    FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE,
    FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE,
    FINANCIAL_DOCUMENT_TYPE_PACKING_LIST,
)
from packages.contracts.cny_ledger import (
    CNY_DOCUMENT_SOURCE_CNY_ACCOUNT,
    CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER,
    CNY_DOCUMENT_TYPE_BANK_FEE,
    CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE,
    CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT,
)
from packages.contracts.supplier_shipments import (
    TRADE_DOCUMENT_TYPE_CONTRACT,
    TRADE_DOCUMENT_TYPE_INVOICE,
)

OperatorLogEmitter = Callable[[str], None]
SheetLoadRunner = Callable[[SheetVitrinaV1Envelope, OperatorLogEmitter], dict[str, Any]]
PromoArtifactGcRunner = Callable[..., dict[str, Any]]
SHEET_OPERATOR_JOB_ID: ContextVar[str] = ContextVar("sheet_vitrina_v1_operator_job_id", default="")
WEB_VITRINA_METRIC_PRESENTATION_CONFIG_KEY = "metric_presentation"
WEB_VITRINA_USER_CONFIG_SCHEMA_VERSION = 1
WEB_VITRINA_METRIC_PRESENTATION_PAYLOAD_VERSION = 2
WEB_VITRINA_METRIC_DISPLAY_STATUSES = {"shown", "collapsed", "hidden"}
SHEET_VITRINA_REFRESH_ROUTE = "/v1/sheet-vitrina-v1/refresh"
SHEET_VITRINA_LOAD_ROUTE = "/v1/sheet-vitrina-v1/load"
SHEET_VITRINA_GROUP_REFRESH_ROUTE = "/v1/sheet-vitrina-v1/web-vitrina/group-refresh"
SHEET_VITRINA_AUTO_SCHEDULES_ROUTE = "/v1/sheet-vitrina-v1/web-vitrina/auto-schedules"
SHEET_VITRINA_AUTO_SCHEDULES_RUN_NOW_ROUTE = "/v1/sheet-vitrina-v1/web-vitrina/auto-schedules/run-now"
SHEET_VITRINA_SELLER_SESSION_CHECK_ROUTE = "/v1/sheet-vitrina-v1/seller-portal-session/check"
SHEET_VITRINA_SELLER_RECOVERY_START_ROUTE = "/v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start"
SHEET_VITRINA_SELLER_RECOVERY_LAUNCHER_ROUTE = "/v1/sheet-vitrina-v1/seller-portal-recovery/launcher.zip"
SHEET_VITRINA_DAILY_TIMER_NAME = "wb-core-sheet-vitrina-refresh.timer"
SHEET_VITRINA_DAILY_AUTO_ACTION = "server-side refresh ready snapshot for website/operator web-vitrina"
SHEET_VITRINA_ACTIVE_JOB_STALE_AFTER_SECONDS = 2 * 60 * 60
SHEET_VITRINA_DAILY_BUSINESS_TIMES = ", ".join(
    f"{hour:02d}:00" for hour in DAILY_REFRESH_BUSINESS_HOURS
)
SUPPLIER_ORDER_REQUIRED_DOCUMENT_TYPES = (
    TRADE_DOCUMENT_TYPE_INVOICE,
    TRADE_DOCUMENT_TYPE_CONTRACT,
    FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE,
    FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE,
    FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION,
    FINANCIAL_DOCUMENT_TYPE_BANK_CONTROL_STATEMENT,
    FINANCIAL_DOCUMENT_TYPE_BANK_TRANSFER_APPLICATION,
    FINANCIAL_DOCUMENT_TYPE_BANK_FEE_STATEMENT,
    FINANCIAL_DOCUMENT_TYPE_PACKING_LIST,
)
SUPPLIER_ORDER_LOGISTICS_PACKAGE_DOCUMENT_TYPES = (
    TRADE_DOCUMENT_TYPE_CONTRACT,
    FINANCIAL_DOCUMENT_TYPE_BANK_TRANSFER_APPLICATION,
    FINANCIAL_DOCUMENT_TYPE_BANK_CONTROL_STATEMENT,
)
SUPPLIER_ORDER_DOCUMENT_LABELS_RU = {
    TRADE_DOCUMENT_TYPE_INVOICE: "Invoice",
    TRADE_DOCUMENT_TYPE_CONTRACT: "Контракт",
    FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE: "КП логистов",
    FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE: "Счёт логистов",
    FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION: "ДТ",
    FINANCIAL_DOCUMENT_TYPE_BANK_CONTROL_STATEMENT: "Ведомость банковского контроля",
    FINANCIAL_DOCUMENT_TYPE_BANK_TRANSFER_APPLICATION: "Заявление на перевод ВТБ / платёжка",
    FINANCIAL_DOCUMENT_TYPE_BANK_FEE_STATEMENT: "Комиссии банка",
    FINANCIAL_DOCUMENT_TYPE_PACKING_LIST: "Packing list / Упаковочный лист",
    CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE: "Документ конвертации RUB -> CNY",
    CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT: "Оплата поставщику CNY",
    CNY_DOCUMENT_TYPE_BANK_FEE: "Комиссия банка CNY",
}
SHEET_VITRINA_DAILY_AUTO_DESCRIPTION = (
    f"Ежедневно в {SHEET_VITRINA_DAILY_BUSINESS_TIMES} {CANONICAL_BUSINESS_TIMEZONE_NAME}: "
    f"{SHEET_VITRINA_DAILY_AUTO_ACTION}"
)
SHEET_VITRINA_DAILY_TRIGGER_DESCRIPTION = (
    f"{SHEET_VITRINA_DAILY_TIMER_NAME} -> apps/sheet_vitrina_v1_auto_refresh_tick.py -> "
    f"POST {SHEET_VITRINA_REFRESH_ROUTE} (auto_refresh=true) for due runtime schedules"
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
    OWN_PRODUCT_CAPITAL_SOURCE_KEY: {
        "label_ru": "WebCore",
        "description_ru": "Наш оплаченный товарный капитал по пяти физическим стадиям.",
    },
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
    "spp_proxy": {
        "label_ru": "SPP-прокси",
        "description_ru": "Прокси-оценка public-card SPP по цене продавца и анонимной цене покупателя WB.",
    },
    "ads_bids": {
        "label_ru": "Ставки рекламы",
        "description_ru": "Ставки в поиске и рекомендациях по SKU.",
    },
    "stocks": {
        "label_ru": "Остатки по складам",
        "description_ru": "История остатков и суммарный stock по складам.",
    },
    ONEC_STOCKS_SOURCE_KEY: {
        "label_ru": ONEC_STOCKS_SOURCE_GROUP_LABEL_RU,
        "description_ru": "Остатки и товарный капитал по стадиям 1С.",
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
    "sku_action_events": {
        "label_ru": "Изменения SKU",
        "description_ru": "Подтверждённые операторские изменения цены и ставки.",
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
    "spp_proxy": (
        "avg_spp_proxy",
        "spp_proxy",
        BUYER_PRICE_RUB_METRIC_KEY,
    ),
    "sku_action_events": (
        SELLER_PRICE_CHANGE_RUB_METRIC_KEY,
        ADVERTISING_BID_CHANGE_RUB_METRIC_KEY,
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
        TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
        TOTAL_OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
        OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
        OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
        OUR_WB_UNIT_COST_RUB_METRIC_KEY,
        OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
        OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
        OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    ),
    ONEC_STOCKS_SOURCE_KEY: ONEC_STOCKS_METRIC_KEYS,
    OWN_PRODUCT_CAPITAL_SOURCE_KEY: OWN_PRODUCT_CAPITAL_METRIC_KEYS,
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
    "wb_public_card_bot": {
        "label_ru": "WB public card / бот",
        "source_keys": (
            "spp_proxy",
        ),
    },
    "other_sources": {
        "label_ru": "Прочие источники",
        "source_keys": (
            "cost_price",
            "sku_action_events",
        ),
    },
    ONEC_STOCKS_SOURCE_GROUP_ID: {
        "label_ru": ONEC_STOCKS_SOURCE_GROUP_LABEL_RU,
        "source_keys": (
            ONEC_STOCKS_SOURCE_KEY,
        ),
    },
    OWN_PRODUCT_CAPITAL_SOURCE_GROUP_ID: {
        "label_ru": OWN_PRODUCT_CAPITAL_SOURCE_GROUP_LABEL_RU,
        "source_keys": (OWN_PRODUCT_CAPITAL_SOURCE_KEY,),
    },
}
WEB_VITRINA_SOURCE_GROUP_ORDER = (
    "wb_api",
    ONEC_STOCKS_SOURCE_GROUP_ID,
    OWN_PRODUCT_CAPITAL_SOURCE_GROUP_ID,
    "seller_portal_bot",
    "wb_public_card_bot",
    "other_sources",
)
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
        with_probe: bool = True,
    ) -> dict[str, Any]:
        config = self._config()
        raw = (
            self._status_reader(config, False, requested_run_id=run_id)
            if self._status_reader is not None
            else self._tool().read_session_status(config, with_probe=False, requested_run_id=run_id)
        )
        running = bool(raw.get("running"))
        if with_probe and not running:
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
        try:
            raw = (
                self._status_reader(config, True)
                if self._status_reader is not None
                else self._tool().read_session_status(config, with_probe=True)
            )
        except Exception as exc:  # pragma: no cover - defensive live probe boundary
            raw = {
                "status": "probe_error",
                "message": str(exc),
                "current_storage_probe": {
                    "ok": False,
                    "status": "seller_portal_session_probe_error",
                    "message": str(exc),
                },
            }
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
        feedbacks_block: SheetVitrinaV1FeedbacksBlock | None = None,
        feedbacks_ai_block: SheetVitrinaV1FeedbacksAiBlock | None = None,
        feedbacks_complaints_block: SheetVitrinaV1FeedbacksComplaintsBlock | None = None,
        feedbacks_auto_complaints_block: SheetVitrinaV1FeedbacksAutoComplaintsBlock | None = None,
        ads_block: SheetVitrinaV1AdsBlock | None = None,
        prices_block: WbPricesManagementBlock | None = None,
        spp_tester_block: WbSppTesterBlock | None = None,
        sku_management_block: SkuManagementBlock | None = None,
        promo_artifact_gc_runner: PromoArtifactGcRunner | None = None,
    ) -> None:
        self.runtime = runtime or RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        self.activated_at_factory = activated_at_factory or _default_activated_at_factory
        self.refreshed_at_factory = refreshed_at_factory or _default_activated_at_factory
        self.now_factory = now_factory or _default_now_factory
        self.promo_artifact_gc_runner = promo_artifact_gc_runner or run_promo_campaign_archive_light_gc
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
        self.wb_finance_weekly_block = block_from_env(self.runtime.runtime_dir)
        self.wb_finance_weekly_block.ensure_schema()
        self.web_vitrina_block = SheetVitrinaV1WebVitrinaBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
        )
        self.sheet_auto_refresh_schedules_block = SheetVitrinaV1AutoRefreshSchedulesBlock(
            runtime_dir=self.runtime.runtime_dir,
            now_factory=self.now_factory,
        )
        self.research_block = SheetVitrinaV1ResearchBlock(
            runtime=self.runtime,
            web_vitrina_block=self.web_vitrina_block,
            now_factory=self.now_factory,
        )
        self.feedbacks_block = feedbacks_block or SheetVitrinaV1FeedbacksBlock(now_factory=self.now_factory)
        self.feedbacks_ai_block = feedbacks_ai_block or SheetVitrinaV1FeedbacksAiBlock(
            runtime_dir=self.runtime.runtime_dir,
            now_factory=self.now_factory,
        )
        self.feedbacks_complaints_block = feedbacks_complaints_block or SheetVitrinaV1FeedbacksComplaintsBlock(
            runtime_dir=self.runtime.runtime_dir,
            now_factory=self.now_factory,
        )
        self.feedbacks_auto_complaints_block = feedbacks_auto_complaints_block or SheetVitrinaV1FeedbacksAutoComplaintsBlock(
            runtime_dir=self.runtime.runtime_dir,
            feedbacks_block=self.feedbacks_block,
            feedbacks_ai_block=self.feedbacks_ai_block,
            complaints_block=self.feedbacks_complaints_block,
            now_factory=self.now_factory,
        )
        self.ads_block = ads_block or SheetVitrinaV1AdsBlock(
            runtime=self.runtime,
            runtime_dir=self.runtime.runtime_dir,
            now_factory=self.now_factory,
            timestamp_factory=self.activated_at_factory,
        )
        self.prices_block = prices_block or WbPricesManagementBlock(
            runtime=self.runtime,
            runtime_dir=self.runtime.runtime_dir,
            now_factory=self.now_factory,
            timestamp_factory=self.activated_at_factory,
        )
        self.spp_tester_block = spp_tester_block or WbSppTesterBlock(
            runtime=self.runtime,
            runtime_dir=self.runtime.runtime_dir,
            now_factory=self.now_factory,
            timestamp_factory=self.activated_at_factory,
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
        self.supplier_shipments_block = SupplierShipmentsBlock(
            runtime=self.runtime,
            timestamp_factory=self.activated_at_factory,
        )
        self.supplier_financial_documents_block = SupplierFinancialDocumentsBlock(
            runtime=self.runtime,
            timestamp_factory=self.activated_at_factory,
        )
        self.cny_ledger_block = CnyLedgerBlock(
            runtime=self.runtime,
            timestamp_factory=self.activated_at_factory,
        )
        self.wb_supplies_block = WbSuppliesBlock(
            runtime=self.runtime,
            timestamp_factory=self.activated_at_factory,
        )
        self.fulfillment_services_block = FulfillmentServicesBlock(
            runtime=self.runtime,
            timestamp_factory=self.activated_at_factory,
        )
        self.ff_stock_ledger_block = FfStockLedgerBlock(
            runtime=self.runtime,
            timestamp_factory=self.activated_at_factory,
        )
        self.our_wb_cost_block = OurWbCostBlock(
            runtime=self.runtime,
            timestamp_factory=self.activated_at_factory,
        )
        self.own_product_capital_block = OwnProductCapitalBlock(
            runtime=self.runtime,
            timestamp_factory=self.activated_at_factory,
        )
        self.wb_supplies_block.fulfillment_overlay_provider = (
            self.fulfillment_services_block.approved_overlay_by_supply
        )
        self.wb_regional_supply_planning_block = WbRegionalSupplyPlanningBlock(
            runtime=self.runtime,
            source=self.wb_supplies_block.source,
            now_factory=self.now_factory,
            timestamp_factory=self.activated_at_factory,
        )
        self.factory_order_supply_block.wb_supply_district_mapping_provider = (
            self.wb_supplies_block.current_warehouse_district_mapping
        )
        self.wb_regional_supply_block.wb_supply_district_mapping_provider = (
            self.wb_supplies_block.current_warehouse_district_mapping
        )
        sku_prices_block = WbPricesManagementBlock(
            runtime=self.runtime,
            runtime_dir=self.runtime.runtime_dir,
            source=self.prices_block.source,
            now_factory=self.now_factory,
            timestamp_factory=self.activated_at_factory,
            safety_config=WbPricesSafetyConfig(
                write_enabled=True,
                preview_ttl_seconds=self.prices_block.safety.preview_ttl_seconds,
            ),
        )
        sku_ads_block = SheetVitrinaV1AdsBlock(
            runtime=self.runtime,
            runtime_dir=self.runtime.runtime_dir,
            source=self.ads_block.source,
            now_factory=self.now_factory,
            timestamp_factory=self.activated_at_factory,
            cache_ttl_seconds=self.ads_block.cache_ttl_seconds,
            safety_config=replace(self.ads_block.safety, write_enabled=True),
        )
        self.sku_management_block = sku_management_block or SkuManagementBlock(
            runtime=self.runtime,
            runtime_dir=self.runtime.runtime_dir,
            prices_block=sku_prices_block,
            ads_block=sku_ads_block,
            stocks_block=self.factory_order_supply_block.stocks_block,
            sales_history=self.factory_order_supply_block.sales_history,
            now_factory=self.now_factory,
            timestamp_factory=self.activated_at_factory,
        )

    def handle_bundle_payload(self, payload: Mapping[str, Any]) -> RegistryUploadResult:
        return self.runtime.ingest_bundle(
            payload,
            activated_at=self.activated_at_factory(),
        )

    def handle_wb_finance_weekly_request(self) -> dict[str, Any]:
        return self.wb_finance_weekly_block.build_payload()

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

    def handle_sheet_stock_report_request(
        self,
        as_of_date: str | None = None,
        sales_avg_period_days: int | str | None = None,
    ) -> dict[str, Any]:
        return self.stock_report_block.build(
            as_of_date=as_of_date,
            sales_avg_period_days=sales_avg_period_days,
        )

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
        annual_plan_evenly_distributed: bool = False,
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
            annual_plan_evenly_distributed=annual_plan_evenly_distributed,
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
        return _web_vitrina_contract_response_payload(
            asdict(
                self.web_vitrina_block.build(
                    page_route=page_route,
                    read_route=read_route,
                    as_of_date=as_of_date,
                    date_from=date_from,
                    date_to=date_to,
                )
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
        include_table_data: bool = False,
    ) -> dict[str, Any]:
        page_composition_started_perf = time.perf_counter()
        now = self.now_factory()
        default_period = resolve_web_vitrina_default_period(now)
        canonical_default_range: tuple[str, str] = (default_period.date_from, default_period.date_to)
        effective_as_of_date = as_of_date or default_period.date_to
        available_snapshot_dates = self.web_vitrina_block.list_readable_dates(descending=True)
        default_as_of_date = default_business_as_of_date(now)
        selected_date_from = date_from
        selected_date_to = date_to
        try:
            if not as_of_date and not date_from and not date_to:
                selected_date_from, selected_date_to = canonical_default_range
                contract = self.web_vitrina_block.build(
                    page_route=page_route,
                    read_route=read_route,
                    as_of_date=None,
                    date_from=selected_date_from,
                    date_to=selected_date_to,
                )
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
                    default_date_from=canonical_default_range[0],
                    default_date_to=canonical_default_range[1],
                    activity_surface=activity_surface,
                ),
                started_perf=page_composition_started_perf,
                include_source_status=include_source_status,
                include_table_data=include_table_data,
            )

        source_status_snapshot_as_of_date = _web_vitrina_source_status_snapshot_as_of_date(contract)
        source_status_snapshot_id = _web_vitrina_source_status_snapshot_id(
            self.runtime,
            contract,
            snapshot_as_of_date=source_status_snapshot_as_of_date,
        )
        group_refresh_available_dates = self.web_vitrina_block.list_materialized_readable_dates(descending=False)
        group_refresh_default_date = _default_group_refresh_date(
            group_refresh_available_dates,
            preferred_date=current_business_date_iso(self.now_factory()),
        )
        metric_labels_by_source = _build_activity_metric_labels_by_source(
            extend_metrics_with_sku_action_metrics(
                extend_metrics_with_own_product_capital_metrics(
                    extend_metrics_with_our_wb_cost_metrics(
                        extend_metrics_with_onec_stock_metrics(
                            getattr(self.runtime.load_current_state(), "metrics_v2", [])
                        )
                    )
                )
            )
        )
        activity_surface = _web_vitrina_source_status_not_loaded_activity_surface(
            snapshot_as_of_date=source_status_snapshot_as_of_date,
            snapshot_id=source_status_snapshot_id,
            refreshed_at=str(contract.meta.refreshed_at),
            read_model=str(contract.status_summary.read_model),
            available_dates=group_refresh_available_dates,
            default_refresh_date=group_refresh_default_date,
            metric_labels_by_source=metric_labels_by_source,
            group_last_updated_at=_source_group_last_updated_at_for_runtime_snapshot(
                self.runtime,
                snapshot_as_of_date=source_status_snapshot_as_of_date,
                fallback_updated_at=str(contract.meta.refreshed_at),
            ),
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
                default_date_from=canonical_default_range[0],
                default_date_to=canonical_default_range[1],
                contract=contract,
                view_model=view_model,
                adapter=adapter,
                activity_surface=activity_surface,
                include_table_data=include_table_data,
            ),
            started_perf=page_composition_started_perf,
            include_source_status=include_source_status,
            include_table_data=include_table_data,
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

    def handle_sheet_research_promotions_calculate_request(
        self,
        payload: Mapping[str, Any],
        *,
        page_route: str,
        read_route: str,
    ) -> dict[str, Any]:
        return self.research_block.calculate_promotions(
            payload,
            page_route=page_route,
            read_route=read_route,
        )

    def handle_sheet_feedbacks_request(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        stars: list[int] | None = None,
        is_answered: str = "all",
    ) -> dict[str, Any]:
        return self.feedbacks_block.build(
            date_from=date_from,
            date_to=date_to,
            stars=stars,
            is_answered=is_answered,
        )

    def handle_sheet_feedbacks_export_request(self, payload: Mapping[str, Any]) -> tuple[bytes, str]:
        return self.feedbacks_block.build_export(payload)

    def handle_sheet_feedbacks_ai_prompt_get_request(self) -> dict[str, Any]:
        return self.feedbacks_ai_block.get_prompt()

    def handle_sheet_feedbacks_ai_prompt_save_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.feedbacks_ai_block.save_prompt(payload)

    def handle_sheet_feedbacks_ai_analyze_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.feedbacks_ai_block.analyze(payload)

    def handle_sheet_feedbacks_complaints_request(self) -> dict[str, Any]:
        return self.feedbacks_complaints_block.build_table()

    def handle_sheet_feedbacks_complaints_sync_status_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.feedbacks_complaints_block.sync_status(payload)

    def handle_sheet_feedbacks_complaints_sync_status_job_request(self, run_id: str) -> dict[str, Any]:
        return self.feedbacks_complaints_block.get_sync_status_job(run_id)

    def handle_sheet_feedbacks_complaints_submit_selected_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.feedbacks_complaints_block.submit_selected(payload)

    def handle_sheet_feedbacks_complaints_submit_job_request(self, run_id: str) -> dict[str, Any]:
        return self.feedbacks_complaints_block.get_submit_job(run_id)

    def handle_sheet_feedbacks_auto_complaints_schedules_request(self) -> dict[str, Any]:
        return self.feedbacks_auto_complaints_block.build_schedules()

    def handle_sheet_feedbacks_auto_complaints_schedules_save_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.feedbacks_auto_complaints_block.save_schedules(payload)

    def handle_sheet_feedbacks_auto_complaints_run_now_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.feedbacks_auto_complaints_block.run_now(payload)

    def handle_sheet_feedbacks_auto_complaints_runs_request(self) -> dict[str, Any]:
        return self.feedbacks_auto_complaints_block.list_runs()

    def handle_sheet_feedbacks_auto_complaints_run_request(self, run_id: str) -> dict[str, Any]:
        return self.feedbacks_auto_complaints_block.get_run(run_id)

    def handle_sheet_feedbacks_auto_complaints_tick_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.feedbacks_auto_complaints_block.tick(payload)

    def handle_sheet_ads_skus_request(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.ads_block.build_sku_table(params or {})

    def handle_sheet_ads_sku_request(
        self,
        nm_id: int,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.ads_block.build_sku_detail(nm_id, params or {})

    def handle_sheet_ads_bid_preview_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.ads_block.preview_bid_change(payload)

    def handle_sheet_ads_bid_commit_request(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        return self.ads_block.commit_bid_change(payload, actor=actor)

    def handle_sheet_prices_goods_request(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.prices_block.build_goods_table(params or {})

    def handle_sheet_prices_preview_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.prices_block.preview_changes(payload)

    def handle_sheet_prices_upload_task_request(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        return self.prices_block.upload_task(payload, actor=actor)

    def handle_sheet_prices_upload_task_status_request(self, upload_id: int) -> dict[str, Any]:
        return self.prices_block.get_upload_task(upload_id)

    def handle_sheet_prices_upload_task_goods_request(
        self,
        upload_id: int,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        return self.prices_block.get_upload_task_goods(
            upload_id,
            limit=params.get("limit", 1000),
            offset=params.get("offset", 0),
        )

    def handle_sheet_prices_quarantine_request(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.prices_block.get_quarantine_goods(params or {})

    def handle_sheet_prices_spp_test_baseline_request(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.spp_tester_block.build_baseline(params or {})

    def handle_sheet_prices_spp_test_plan_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.spp_tester_block.build_plan(payload)

    def handle_sheet_prices_spp_test_start_request(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        return self.spp_tester_block.start(payload, actor=actor)

    def handle_sheet_prices_spp_test_status_request(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.spp_tester_block.status(params or {})

    def handle_sheet_prices_spp_test_restore_request(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        return self.spp_tester_block.restore(payload, actor=actor)

    def handle_sku_management_table_request(self, *, user_key: str) -> dict[str, Any]:
        return self.sku_management_block.build_table(user_key=user_key)

    def handle_sku_management_settings_request(self, *, user_key: str) -> dict[str, Any]:
        return self.sku_management_block.get_settings(user_key=user_key)

    def handle_sku_management_settings_save_request(self, payload: Mapping[str, Any], *, user_key: str) -> dict[str, Any]:
        return self.sku_management_block.save_settings(user_key=user_key, payload=payload)

    def handle_sku_management_price_preview_request(self, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        return self.sku_management_block.preview_price(payload, actor=actor)

    def handle_sku_management_price_commit_request(self, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        return self.sku_management_block.commit_price(payload, actor=actor)

    def handle_sku_management_bid_preview_request(self, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        return self.sku_management_block.preview_bid(payload, actor=actor)

    def handle_sku_management_bid_commit_request(self, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        return self.sku_management_block.commit_bid(payload, actor=actor)

    def handle_sku_management_history_request(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.sku_management_block.history(params or {})

    def handle_sheet_web_vitrina_auto_schedules_request(self) -> dict[str, Any]:
        auto_update_state = self.runtime.load_sheet_vitrina_auto_update_state()
        return self.sheet_auto_refresh_schedules_block.build_payload(
            auto_context={
                "last_auto_run_status": auto_update_state.last_run_status or "never",
                "last_auto_run_time": _format_optional_business_timestamp(auto_update_state.last_run_started_at),
                "last_auto_run_finished_at": _format_optional_business_timestamp(auto_update_state.last_run_finished_at),
                "last_successful_auto_update_at": _format_optional_business_timestamp(auto_update_state.last_successful_auto_update_at),
                "last_auto_run_error": auto_update_state.last_run_error or "",
            }
        )

    def handle_sheet_web_vitrina_auto_schedules_save_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        auto_update_state = self.runtime.load_sheet_vitrina_auto_update_state()
        return self.sheet_auto_refresh_schedules_block.save_schedules(
            payload,
            auto_context={
                "last_auto_run_status": auto_update_state.last_run_status or "never",
                "last_auto_run_time": _format_optional_business_timestamp(auto_update_state.last_run_started_at),
                "last_auto_run_finished_at": _format_optional_business_timestamp(auto_update_state.last_run_finished_at),
                "last_successful_auto_update_at": _format_optional_business_timestamp(auto_update_state.last_successful_auto_update_at),
                "last_auto_run_error": auto_update_state.last_run_error or "",
            },
        )

    def handle_sheet_web_vitrina_auto_schedules_run_now_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        schedule_id = str(payload.get("schedule_id") or "").strip()
        if not schedule_id:
            schedules_payload = self.sheet_auto_refresh_schedules_block.build_payload()
            enabled = [
                item
                for item in schedules_payload.get("schedules", [])
                if isinstance(item, Mapping) and bool(item.get("enabled", True))
            ]
            if not enabled:
                raise ValueError("no enabled auto refresh schedules")
            schedule_id = str(enabled[0].get("id") or "")
        return self.start_sheet_scheduled_auto_update_job(
            schedule_id=schedule_id,
            due_at="",
            trigger_source="manual_run_now_from_auto_schedule",
        )

    def handle_sheet_web_vitrina_user_config_request(
        self,
        *,
        user_key: str,
        config_key: str = WEB_VITRINA_METRIC_PRESENTATION_CONFIG_KEY,
    ) -> dict[str, Any]:
        normalized_config_key = _normalize_web_vitrina_user_config_key(config_key)
        record = self.runtime.load_sheet_vitrina_user_config(
            user_key=user_key,
            config_key=normalized_config_key,
        )
        if record.get("status") == "ok":
            record = dict(record)
            record["config"] = _sanitize_web_vitrina_metric_presentation_config(record.get("config"))
        return {
            "status": record.get("status") or "missing",
            "config_key": normalized_config_key,
            "schema_version": int(record.get("schema_version") or 0),
            "revision": int(record.get("revision") or 0),
            "updated_at": str(record.get("updated_at") or ""),
            "config": record.get("config"),
            "canonical_store": "server_runtime_user_config",
        }

    def handle_sheet_web_vitrina_user_config_save_request(
        self,
        *,
        user_key: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized_config_key = _normalize_web_vitrina_user_config_key(
            str(payload.get("config_key") or WEB_VITRINA_METRIC_PRESENTATION_CONFIG_KEY)
        )
        config = _sanitize_web_vitrina_metric_presentation_config(payload.get("config"))
        expected_revision = _optional_int(payload.get("base_revision"))
        saved = self.runtime.save_sheet_vitrina_user_config(
            user_key=user_key,
            config_key=normalized_config_key,
            schema_version=WEB_VITRINA_USER_CONFIG_SCHEMA_VERSION,
            payload=config,
            updated_at=_default_activated_at_factory(),
            expected_revision=expected_revision,
        )
        if saved.get("status") == "conflict":
            current = dict(saved.get("current") or {})
            if current.get("status") == "ok":
                current["config"] = _sanitize_web_vitrina_metric_presentation_config(current.get("config"))
            return {
                "status": "conflict",
                "config_key": normalized_config_key,
                "expected_revision": int(saved.get("expected_revision") or 0),
                "current": current,
                "canonical_store": "server_runtime_user_config",
            }
        return {
            "status": "ok",
            "config_key": normalized_config_key,
            "schema_version": int(saved.get("schema_version") or WEB_VITRINA_USER_CONFIG_SCHEMA_VERSION),
            "revision": int(saved.get("revision") or 0),
            "updated_at": str(saved.get("updated_at") or ""),
            "config": _sanitize_web_vitrina_metric_presentation_config(saved.get("config")),
            "canonical_store": "server_runtime_user_config",
        }

    def handle_sheet_vitrina_users_list_request(self) -> dict[str, Any]:
        return {
            "users": self.runtime.list_sheet_vitrina_users(),
            "canonical_store": "server_runtime_sqlite",
        }

    def load_sheet_vitrina_runtime_user_by_username(self, username: str) -> dict[str, Any] | None:
        return self.runtime.load_sheet_vitrina_user_by_username(username)

    def load_sheet_vitrina_runtime_user(self, user_id: str) -> dict[str, Any] | None:
        return self.runtime.load_sheet_vitrina_user(user_id)

    def handle_sheet_vitrina_user_create_request(self, user: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "user": self.runtime.save_sheet_vitrina_user(user),
            "canonical_store": "server_runtime_sqlite",
        }

    def handle_sheet_vitrina_user_patch_request(
        self,
        user_id: str,
        updates: Mapping[str, Any],
        *,
        updated_at: str,
    ) -> dict[str, Any]:
        return {
            "user": self.runtime.update_sheet_vitrina_user(user_id, updates, updated_at=updated_at),
            "canonical_store": "server_runtime_sqlite",
        }

    def handle_sheet_vitrina_user_archive_request(self, user_id: str, *, updated_at: str) -> dict[str, Any]:
        return {
            "user": self.runtime.archive_sheet_vitrina_user(user_id, updated_at=updated_at),
            "canonical_store": "server_runtime_sqlite",
        }

    def handle_sheet_scheduled_auto_update_request(
        self,
        *,
        schedule_id: str,
        due_at: str = "",
        trigger_source: str = "scheduled",
    ) -> dict[str, Any]:
        self.sheet_auto_refresh_schedules_block.get_schedule(schedule_id)
        if _is_scheduled_auto_refresh_trigger(trigger_source):
            active_job = self.operator_jobs.active_job(operations=("auto_update",))
            if active_job:
                return self._skip_sheet_scheduled_auto_update_for_active_job(
                    schedule_id=schedule_id,
                    due_at=due_at,
                    trigger_source=trigger_source,
                    active_job=active_job,
                )
        return self._run_sheet_scheduled_auto_update(
            schedule_id=schedule_id,
            due_at=due_at,
            trigger_source=trigger_source or "scheduled",
            log=None,
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

    def start_sheet_auto_refresh_job(
        self,
        as_of_date: str | None = None,
        *,
        schedule_id: str = "",
        due_at: str = "",
        trigger_source: str = "scheduled",
    ) -> dict[str, Any]:
        resolved_schedule_id, resolved_due_at = self._resolve_auto_refresh_schedule_context(
            schedule_id=schedule_id,
            due_at=due_at,
        )
        if resolved_schedule_id:
            return self.start_sheet_scheduled_auto_update_job(
                schedule_id=resolved_schedule_id,
                due_at=resolved_due_at,
                trigger_source=trigger_source or "scheduled",
            )
        return self.start_sheet_refresh_job(as_of_date=as_of_date, auto_load=True)

    def start_sheet_scheduled_auto_update_job(
        self,
        *,
        schedule_id: str,
        due_at: str = "",
        trigger_source: str = "scheduled",
    ) -> dict[str, Any]:
        self.sheet_auto_refresh_schedules_block.get_schedule(schedule_id)
        if _is_scheduled_auto_refresh_trigger(trigger_source):
            active_job = self.operator_jobs.active_job(operations=("auto_update",))
            if active_job:
                return self._skip_sheet_scheduled_auto_update_for_active_job(
                    schedule_id=schedule_id,
                    due_at=due_at,
                    trigger_source=trigger_source,
                    active_job=active_job,
                )
        return self.operator_jobs.start(
            operation="auto_update",
            runner=lambda log: self._run_sheet_scheduled_auto_update(
                schedule_id=schedule_id,
                due_at=due_at,
                trigger_source=trigger_source or "scheduled",
                log=log,
            ),
        )

    def handle_sheet_auto_refresh_request(
        self,
        as_of_date: str | None = None,
        *,
        schedule_id: str = "",
        due_at: str = "",
        trigger_source: str = "scheduled",
    ) -> dict[str, Any]:
        resolved_schedule_id, resolved_due_at = self._resolve_auto_refresh_schedule_context(
            schedule_id=schedule_id,
            due_at=due_at,
        )
        if resolved_schedule_id:
            return self.handle_sheet_scheduled_auto_update_request(
                schedule_id=resolved_schedule_id,
                due_at=resolved_due_at,
                trigger_source=trigger_source or "scheduled",
            )
        return self.handle_sheet_refresh_request(as_of_date=as_of_date, auto_load=True)

    def _resolve_auto_refresh_schedule_context(
        self,
        *,
        schedule_id: str,
        due_at: str,
    ) -> tuple[str, str]:
        normalized_schedule_id = str(schedule_id or "").strip()
        if normalized_schedule_id:
            self.sheet_auto_refresh_schedules_block.get_schedule(normalized_schedule_id)
            return normalized_schedule_id, str(due_at or "").strip()
        due = sorted(
            self.sheet_auto_refresh_schedules_block.due_schedules(now=self.now_factory()),
            key=lambda item: str(item[1] or ""),
        )
        if not due:
            return "", ""
        if len(due) > 1:
            for missed_schedule, missed_due_at in due[:-1]:
                missed_schedule_id = str(missed_schedule.get("id") or "")
                if missed_schedule_id:
                    self.sheet_auto_refresh_schedules_block.mark_due_skipped(
                        missed_schedule_id,
                        due_at=str(missed_due_at or ""),
                        reason="missed because a later auto-refresh due slot was selected for this raw auto_refresh call",
                        trigger_source="raw_auto_refresh_missed_due",
                    )
        schedule, resolved_due_at = due[-1]
        return str(schedule.get("id") or ""), str(resolved_due_at or "")

    def _skip_sheet_scheduled_auto_update_for_active_job(
        self,
        *,
        schedule_id: str,
        due_at: str,
        trigger_source: str,
        active_job: Mapping[str, Any],
    ) -> dict[str, Any]:
        active_job_id = str(active_job.get("job_id") or "")
        staleness = _active_job_staleness_payload(active_job, now=self.activated_at_factory())
        reason = (
            "Слот расписания пропущен: предыдущее автообновление"
            + (f" job_id={active_job_id}" if active_job_id else "")
            + " ещё выполняется."
        )
        if staleness.get("active_job_stale"):
            reason += " Active job stale; due slot сохранён для retry и требует recovery/restart вместо silent consume."
        auto_schedule = self.sheet_auto_refresh_schedules_block.get_schedule(schedule_id)
        return {
            "status": "skipped",
            "operation": "auto_update",
            "schedule_id": schedule_id,
            "due_at": due_at,
            "trigger_source": trigger_source or "scheduled",
            "reason": reason,
            "blocker": reason,
            "already_running_job_id": active_job_id,
            "retryable": True,
            "due_preserved": True,
            **staleness,
            "active_job": dict(active_job),
            "auto_schedule": auto_schedule,
        }

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
        available_dates = self.web_vitrina_block.list_materialized_readable_dates(descending=False)
        if normalized_group_id == OWN_PRODUCT_CAPITAL_SOURCE_GROUP_ID:
            available_dates = sorted(
                set(available_dates)
                | set(
                    self.runtime.list_sheet_vitrina_ready_snapshot_dates_any_bundle(
                        descending=False
                    )
                )
            )
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
        with_probe: bool = True,
    ) -> dict[str, Any]:
        try:
            return self.seller_portal_recovery.read_status(
                launcher_download_path=launcher_download_path,
                run_id=run_id,
                with_probe=with_probe,
            )
        except TypeError:
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
        refresh_status = self.runtime.load_sheet_vitrina_refresh_status_any_bundle(as_of_date=snapshot_as_of_date)
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
        persisted_source_records = _extract_source_records_from_outcomes(refresh_status.source_outcomes)
        upload_records = (
            _extract_upload_source_records_from_job(latest_refresh_job)
            if latest_refresh_job is not None
            else persisted_source_records
        )
        upload_records = {**persisted_source_records, **upload_records}
        update_records = persisted_source_records
        shared_source_keys = _collect_activity_source_keys(upload_records, update_records)
        upload_source_keys = _ordered_activity_source_keys(shared_source_keys, upload_records)
        update_source_keys = _ordered_activity_source_keys(shared_source_keys, update_records)
        current_business_date = current_business_date_iso(self.now_factory())
        previous_business_date = default_business_as_of_date(self.now_factory())
        group_refresh_available_dates = self.web_vitrina_block.list_materialized_readable_dates(descending=False)
        group_refresh_default_date = _default_group_refresh_date(
            group_refresh_available_dates,
            preferred_date=current_business_date,
        )
        metric_labels_by_source = _build_activity_metric_labels_by_source(
            extend_metrics_with_sku_action_metrics(
                extend_metrics_with_own_product_capital_metrics(
                    extend_metrics_with_our_wb_cost_metrics(
                        extend_metrics_with_onec_stock_metrics(
                            getattr(self.runtime.load_current_state(), "metrics_v2", [])
                        )
                    )
                )
            )
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
                group_last_updated_at=_source_group_last_updated_at_for_runtime_snapshot(
                    self.runtime,
                    snapshot_as_of_date=snapshot_as_of_date,
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

    def handle_factory_order_stock_ff_onec_check_request(self) -> dict[str, Any]:
        return asdict(self.factory_order_supply_block.build_onec_stock_ff_check())

    def handle_factory_order_stock_ff_onec_xlsx_request(self) -> tuple[bytes, str]:
        return self.factory_order_supply_block.download_onec_stock_ff_workbook()

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

    def handle_wb_regional_recommendations_zip_request(self) -> tuple[bytes, str]:
        return self.wb_regional_supply_block.download_all_recommendations_archive()

    def handle_wb_regional_planning_options_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.wb_regional_supply_planning_block.build_options(payload)

    def handle_supplier_shipments_list_request(self) -> dict[str, Any]:
        return self.supplier_shipments_block.list_shipments()

    def handle_supplier_shipments_parse_request(
        self,
        workbook_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
    ) -> dict[str, Any]:
        return self.supplier_shipments_block.parse_upload(
            workbook_bytes,
            uploaded_filename=uploaded_filename,
            uploaded_content_type=uploaded_content_type,
        )

    def handle_supplier_shipments_create_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.supplier_shipments_block.create_shipment(payload)

    def handle_supplier_shipments_detail_request(self, shipment_id: str) -> dict[str, Any]:
        return self.supplier_shipments_block.get_shipment(shipment_id)

    def handle_supplier_shipments_patch_request(self, shipment_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.supplier_shipments_block.update_shipment(shipment_id, payload)

    def handle_supplier_shipments_order_status_patch_request(
        self,
        shipment_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.supplier_shipments_block.update_order_status(shipment_id, payload.get("order_status"))

    def handle_supplier_shipments_expenses_complete_patch_request(
        self,
        shipment_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.supplier_shipments_block.update_expenses_complete(shipment_id, payload.get("expenses_complete"))

    def handle_our_wb_cost_recalculate_request(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        result = self.our_wb_cost_block.rebuild_all()
        rebuilt = asdict(result)
        finance_recalculation = (
            self.wb_finance_weekly_block.recalculate_stale_cost_weeks()
        )
        return {
            "contract_name": "sheet_vitrina_v1_our_wb_cost_recalculate",
            "status": "ok",
            "result": rebuilt,
            "wb_finance_cost_recalculation": finance_recalculation,
            "requested": dict(payload or {}),
        }

    def handle_our_wb_cost_status_request(self) -> dict[str, Any]:
        return {
            "contract_name": "sheet_vitrina_v1_our_wb_cost_status",
            "status": "ok",
            "result": self.our_wb_cost_block.status(),
        }

    def handle_own_product_capital_recalculate_request(
        self,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        requested = dict(payload or {})
        result = self.own_product_capital_block.recalculate(
            date_from=str(requested.get("date_from") or "") or None,
            date_to=str(requested.get("date_to") or "") or None,
        )
        return {
            "contract_name": "sheet_vitrina_v1_own_product_capital_recalculate",
            "status": "ok",
            "result": asdict(result),
            "requested": requested,
        }

    def handle_own_product_capital_status_request(self) -> dict[str, Any]:
        return self.own_product_capital_block.status()

    def handle_supplier_shipments_delete_request(self, shipment_id: str) -> dict[str, Any]:
        return self.supplier_shipments_block.delete_shipment(shipment_id)

    def handle_supplier_shipments_rematch_request(self, shipment_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.supplier_shipments_block.rematch_shipment(shipment_id, payload)

    def handle_supplier_shipments_price_check_request(
        self,
        shipment_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "",
    ) -> dict[str, Any]:
        context = payload.get("context") if isinstance(payload, Mapping) else {}
        return self.supplier_shipments_block.recheck_shipment_prices(
            shipment_id,
            actor=actor,
            context=context if isinstance(context, Mapping) else {},
        )

    def handle_supplier_shipments_price_backfill_request(self) -> dict[str, Any]:
        return self.supplier_shipments_block.backfill_price_conformity_checks()

    def handle_supplier_shipments_invoice_request(self, shipment_id: str) -> tuple[bytes, str, str]:
        return self.supplier_shipments_block.download_invoice(shipment_id)

    def handle_supplier_shipments_contract_request(self, shipment_id: str) -> tuple[bytes, str, str]:
        return self.supplier_shipments_block.download_shipment_contract(shipment_id)

    def handle_supplier_shipment_registry_request(self) -> dict[str, Any]:
        return self.supplier_financial_documents_block.list_shipment_registry()

    def handle_supplier_shipment_registry_compare_quote_request(
        self,
        shipment_id: str,
        file_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
    ) -> dict[str, Any]:
        return self.supplier_financial_documents_block.compare_registry_quote(
            shipment_id,
            file_bytes=file_bytes,
            uploaded_filename=uploaded_filename,
            uploaded_content_type=uploaded_content_type,
        )

    def handle_supplier_financial_documents_list_request(self, shipment_id: str) -> dict[str, Any]:
        return self.supplier_financial_documents_block.list_documents(shipment_id)

    def handle_supplier_order_documents_list_request(self, shipment_id: str) -> dict[str, Any]:
        shipment = self.supplier_shipments_block.get_shipment(shipment_id)
        financial_payload = self.supplier_financial_documents_block.list_documents(shipment_id)
        financial_documents = [
            apply_supplier_order_document_match(dict(item), shipment)
            for item in financial_payload.get("documents") or []
        ]
        financial_document_ids = {str(item.get("document_id") or "") for item in financial_documents if str(item.get("document_id") or "")}
        cny_status = self.cny_ledger_block.get_status()
        cny_documents = [
            self._supplier_order_cny_document_row(item)
            for item in cny_status.get("documents") or []
            if str(item.get("source_order_id") or "") == str(shipment_id or "")
            and str(item.get("linked_financial_document_id") or "").strip() not in financial_document_ids
        ]
        checklist = _build_supplier_order_documents_checklist(
            shipment=shipment,
            financial_documents=financial_documents,
        )
        return {
            "contract_name": "sheet_vitrina_v1_supplier_order_documents",
            "status": "ok",
            "supplier_order_id": shipment_id,
            "shipment": shipment,
            "required_document_types": list(SUPPLIER_ORDER_REQUIRED_DOCUMENT_TYPES),
            "required_documents": [*checklist, *cny_documents],
            "documents": [*financial_documents, *cny_documents],
            "expense_lines": list(financial_payload.get("expense_lines") or []),
            "summary": financial_payload.get("summary") or {},
            "package_downloads": {
                "all": {
                    "download_path": _supplier_order_documents_archive_path(shipment_id, "archive.zip"),
                    "label": "Скачать все документы",
                },
                "logistics": {
                    "download_path": _supplier_order_documents_archive_path(shipment_id, "logistics-package.zip"),
                    "label": "Скачать пакет для логистов",
                    "document_types": list(SUPPLIER_ORDER_LOGISTICS_PACKAGE_DOCUMENT_TYPES),
                },
            },
            "missing_required_types": [
                str(item.get("document_type") or "")
                for item in checklist
                if not bool(item.get("is_uploaded")) and bool(item.get("required"))
            ],
        }

    def handle_supplier_order_documents_archive_request(
        self,
        shipment_id: str,
        *,
        package_kind: str,
    ) -> tuple[bytes, str]:
        documents_payload = self.handle_supplier_order_documents_list_request(shipment_id)
        package_type = "logistics" if package_kind == "logistics-package.zip" else "all"
        archive_bytes = _build_supplier_order_documents_archive(
            documents_payload,
            package_type=package_type,
            file_loader=lambda item: self._load_supplier_order_document_file(shipment_id, item),
        )
        invoice_no = str((documents_payload.get("shipment") or {}).get("invoice_no") or shipment_id or "supplier-order").strip()
        filename = _safe_archive_filename(f"{invoice_no}-{package_type}-documents.zip")
        return archive_bytes, filename

    def _load_supplier_order_document_file(self, shipment_id: str, item: Mapping[str, Any]) -> tuple[bytes, str, str]:
        document_type = str(item.get("document_type") or "")
        document_id = str(item.get("document_id") or "")
        if document_type == TRADE_DOCUMENT_TYPE_INVOICE:
            return self.supplier_shipments_block.download_invoice(shipment_id)
        if document_type == TRADE_DOCUMENT_TYPE_CONTRACT:
            return self.supplier_shipments_block.download_shipment_contract(shipment_id)
        if str(item.get("source") or "") == "cny_document" and document_id:
            return self.cny_ledger_block.download_document_file(document_id)
        if document_id:
            return self.supplier_financial_documents_block.download_document_file(shipment_id, document_id)
        raise ValueError(f"document file is missing: {document_type}")

    def handle_supplier_financial_documents_upload_request(
        self,
        shipment_id: str,
        file_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
        fields: Mapping[str, Any] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        upload_fields = dict(fields or {})
        suffix = Path(str(uploaded_filename or "")).suffix.lower()
        if suffix == ".pdf":
            preview = self.cny_ledger_block.parse_document_preview(
                file_bytes,
                uploaded_filename=uploaded_filename,
            )
            preview_type = str((preview.get("normalized_parse") or {}).get("document_type") or "")
            if preview_type in {CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE, CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT}:
                return self.cny_ledger_block.upload_document(
                    file_bytes=file_bytes,
                    uploaded_filename=uploaded_filename,
                    uploaded_content_type=uploaded_content_type,
                    source=CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER,
                    source_order_id=shipment_id,
                    context_order_id=shipment_id,
                    reject_unsupported=True,
                    manual_payment_date=str(upload_fields.get("payment_date") or "") or None,
                    manual_payment_date_actor=actor,
                )
        financial_preview = self.supplier_financial_documents_block.parse_document_preview(
            file_bytes,
            uploaded_filename=uploaded_filename,
        )
        financial_preview_type = str((financial_preview.get("normalized_parse") or {}).get("document_type") or "")
        if financial_preview_type == FINANCIAL_DOCUMENT_TYPE_BANK_FEE_STATEMENT:
            return self.supplier_financial_documents_block.upload_bank_fee_statement_preview(
                shipment_id,
                file_bytes=file_bytes,
                uploaded_filename=uploaded_filename,
                uploaded_content_type=uploaded_content_type,
            )
        return self.supplier_financial_documents_block.upload_document(
            shipment_id,
            file_bytes=file_bytes,
            uploaded_filename=uploaded_filename,
            uploaded_content_type=uploaded_content_type,
            manual_payment_date=str(upload_fields.get("payment_date") or "") or None,
            manual_payment_date_actor=actor,
        )

    def handle_supplier_financial_document_confirm_import_request(
        self,
        shipment_id: str,
        document_id: str,
    ) -> dict[str, Any]:
        payload = self.supplier_financial_documents_block.confirm_bank_fee_statement_import(shipment_id, document_id)
        cny_rows = list(payload.pop("cny_fee_rows_for_ledger", []) or [])
        for row in cny_rows:
            self.cny_ledger_block.save_bank_fee_document(
                source_order_id=shipment_id,
                linked_financial_document_id=document_id,
                natural_key=str(row.get("cny_ledger_natural_key") or ""),
                fee_row=row,
                original_filename=str(payload.get("original_filename") or ""),
                stored_file_path=str(payload.get("stored_file_path") or ""),
                file_content_type=str(payload.get("file_content_type") or ""),
            )
        if cny_rows:
            self.cny_ledger_block.replay_ledger(reason="bank_fee_statement_confirm")
        result = self.supplier_financial_documents_block.get_document(shipment_id, document_id)
        for key in ("idempotent", "already_added"):
            if key in payload:
                result[key] = payload[key]
        return result

    def _supplier_order_cny_document_row(self, document: Mapping[str, Any]) -> dict[str, Any]:
        parsed = dict(document.get("parsed_payload") or {})
        document_type = str(document.get("document_type") or "")
        amount = document.get("rub_amount") if document_type == CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE else document.get("cny_amount")
        currency = "RUB" if document_type == CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE else "CNY"
        return {
            **_supplier_order_base_document_row(
                document_type,
                required=False,
                is_uploaded=True,
                parse_status=str(document.get("parse_status") or document.get("status") or ""),
            ),
            "source": "cny_document",
            "document_id": str(document.get("document_id") or ""),
            "document_number": str(document.get("document_number") or parsed.get("document_number") or ""),
            "document_date": str(document.get("operation_date") or parsed.get("document_date") or ""),
            "counterparty": str(parsed.get("bank") or parsed.get("vendor") or ""),
            "amount": amount,
            "currency": currency,
            "download_path": str(document.get("download_path") or ""),
            "parse_status": str(document.get("parse_status") or document.get("status") or ""),
            "warnings": list(document.get("warnings") or []),
            "errors": list(document.get("errors") or []),
            "normalized_parse": parsed,
        }

    def handle_supplier_financial_document_detail_request(self, shipment_id: str, document_id: str) -> dict[str, Any]:
        return self.supplier_financial_documents_block.get_document(shipment_id, document_id)

    def handle_supplier_financial_document_patch_request(
        self,
        shipment_id: str,
        document_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.supplier_financial_documents_block.update_document_status(
            shipment_id,
            document_id,
            str(payload.get("parse_status") or ""),
        )

    def handle_supplier_financial_document_delete_request(self, shipment_id: str, document_id: str) -> dict[str, Any]:
        payload = self.supplier_financial_documents_block.delete_document(shipment_id, document_id)
        if payload.get("cny_documents_deleted"):
            replay = self.cny_ledger_block.replay_ledger(reason="supplier_financial_document_delete")
            payload["cny_replay"] = replay.get("replay") or replay
        return payload

    def handle_supplier_financial_document_file_request(self, shipment_id: str, document_id: str) -> tuple[bytes, str, str]:
        return self.supplier_financial_documents_block.download_document_file(shipment_id, document_id)

    def handle_cny_account_status_request(self) -> dict[str, Any]:
        return self.cny_ledger_block.get_status()

    def handle_cny_account_conversions_request(self) -> dict[str, Any]:
        return self.cny_ledger_block.list_conversions()

    def handle_cny_account_ledger_request(self) -> dict[str, Any]:
        return self.cny_ledger_block.list_ledger_operations()

    def handle_cny_account_upload_request(
        self,
        file_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
        fields: Mapping[str, Any] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        upload_fields = dict(fields or {})
        return self.cny_ledger_block.upload_document(
            file_bytes=file_bytes,
            uploaded_filename=uploaded_filename,
            uploaded_content_type=uploaded_content_type,
            source=CNY_DOCUMENT_SOURCE_CNY_ACCOUNT,
            reject_unsupported=True,
            manual_payment_date=str(upload_fields.get("payment_date") or "") or None,
            manual_payment_date_actor=actor,
        )

    def handle_cny_account_opening_balance_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.cny_ledger_block.create_opening_balance(payload)

    def handle_cny_account_replay_request(self) -> dict[str, Any]:
        return self.cny_ledger_block.replay_ledger(reason="manual_api")

    def handle_cny_account_document_file_request(self, document_id: str) -> tuple[bytes, str, str]:
        return self.cny_ledger_block.download_document_file(document_id)

    def handle_cny_account_document_delete_request(self, document_id: str) -> dict[str, Any]:
        return self.cny_ledger_block.delete_document(document_id)

    def handle_supplier_shipments_contract_patch_request(
        self,
        shipment_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "",
    ) -> dict[str, Any]:
        contract_document_id = str(payload.get("contract_document_id") or "").strip()
        if contract_document_id:
            return self.supplier_shipments_block.link_shipment_contract(
                shipment_id,
                contract_document_id=contract_document_id,
                linked_by=actor,
            )
        return self.supplier_shipments_block.unlink_shipment_contract(shipment_id)

    def handle_supplier_shipments_contract_upload_request(
        self,
        shipment_id: str,
        file_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
        fields: Mapping[str, Any] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        del actor
        fields = fields or {}
        return self.supplier_shipments_block.upload_shipment_contract(
            shipment_id,
            file_bytes=file_bytes,
            uploaded_filename=uploaded_filename,
            uploaded_content_type=uploaded_content_type,
            number=str(fields.get("number") or ""),
            document_date=str(fields.get("document_date") or ""),
            supplier_name=str(fields.get("supplier_name") or ""),
        )

    def handle_trade_documents_list_request(self) -> dict[str, Any]:
        return self.supplier_shipments_block.list_trade_documents()

    def handle_trade_documents_create_request(
        self,
        file_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        fields = fields or {}
        return self.supplier_shipments_block.create_trade_document_from_upload(
            document_type=str(fields.get("document_type") or ""),
            file_bytes=file_bytes,
            uploaded_filename=uploaded_filename,
            uploaded_content_type=uploaded_content_type,
            number=str(fields.get("number") or ""),
            document_date=str(fields.get("document_date") or ""),
            supplier_name=str(fields.get("supplier_name") or ""),
            currency=str(fields.get("currency") or ""),
            amount_total=fields.get("amount_total"),
        )

    def handle_trade_documents_patch_request(self, document_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.supplier_shipments_block.update_trade_document(document_id, payload)

    def handle_trade_documents_archive_request(self, document_id: str) -> dict[str, Any]:
        return self.supplier_shipments_block.archive_trade_document(document_id)

    def handle_trade_documents_file_request(self, document_id: str) -> tuple[bytes, str, str]:
        return self.supplier_shipments_block.download_trade_document_file(document_id)

    def handle_trade_documents_contract_patch_request(
        self,
        invoice_document_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "",
    ) -> dict[str, Any]:
        contract_document_id = str(payload.get("contract_document_id") or "").strip()
        if contract_document_id:
            return self.supplier_shipments_block.link_invoice_to_contract(
                invoice_document_id,
                contract_document_id=contract_document_id,
                linked_by=actor,
            )
        return self.supplier_shipments_block.unlink_invoice_contract(invoice_document_id)

    def handle_trade_documents_contract_delete_request(self, invoice_document_id: str) -> dict[str, Any]:
        return self.supplier_shipments_block.unlink_invoice_contract(invoice_document_id)

    def handle_wb_supplies_list_request(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return self.wb_supplies_block.list_supplies(params)

    def handle_wb_supplies_sync_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.wb_supplies_block.sync_supplies(payload)

    def handle_wb_supplies_backfill_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.wb_supplies_block.start_full_backfill(payload)

    def handle_wb_supplies_sync_status_request(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return self.wb_supplies_block.get_sync_status(params)

    def handle_wb_supplies_transit_cost_enrich_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.wb_supplies_block.start_transit_cost_enrichment(payload)

    def handle_wb_supplies_transit_cost_status_request(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return self.wb_supplies_block.get_transit_cost_enrichment_status(params)

    def handle_wb_supplies_detail_request(self, supply_id: str) -> dict[str, Any]:
        return self.wb_supplies_block.get_supply(supply_id)

    def handle_wb_supplies_overlay_options_request(self) -> dict[str, Any]:
        return self.wb_supplies_block.build_overlay_options()

    def handle_fulfillment_services_template_request(self) -> tuple[bytes, str, str]:
        return self.fulfillment_services_block.build_template()

    def handle_fulfillment_services_upload_request(
        self,
        workbook_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
    ) -> dict[str, Any]:
        return self.fulfillment_services_block.upload_xlsx(
            workbook_bytes,
            uploaded_filename=uploaded_filename,
            uploaded_content_type=uploaded_content_type,
        )

    def handle_fulfillment_services_uploads_request(self) -> dict[str, Any]:
        return self.fulfillment_services_block.list_uploads()

    def handle_fulfillment_services_upload_detail_request(self, upload_id: str) -> dict[str, Any]:
        return self.fulfillment_services_block.get_upload(upload_id)

    def handle_fulfillment_services_upload_delete_request(self, upload_id: str) -> dict[str, Any]:
        return self.fulfillment_services_block.delete_upload(upload_id, deleted_by="operator")

    def handle_fulfillment_services_payment_validation_pdf_request(
        self,
        upload_id: str,
    ) -> tuple[bytes, str, str]:
        return self.fulfillment_services_block.download_pdf(upload_id)

    def handle_ff_stock_status_request(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        query = dict(params or {})
        return self.ff_stock_ledger_block.get_status(
            operations_limit=_first_query_value(query, "operations_limit", "limit", default=50),
            operations_page=_first_query_value(query, "operations_page", "page", default=1),
            operations_offset=_first_query_value(query, "operations_offset", "offset", default=None),
            show_technical_archive=_coerce_query_bool(
                _first_query_value(query, "show_technical_archive", default=None),
                default=True,
            ),
        )

    def handle_ff_stock_export_request(self) -> tuple[bytes, str, str]:
        return self.ff_stock_ledger_block.export_current_balances_xlsx()

    def handle_ff_stock_preview_request(
        self,
        workbook_bytes: bytes,
        *,
        operation_type: str,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
    ) -> dict[str, Any]:
        return self.ff_stock_ledger_block.parse_manual_operation_preview(
            workbook_bytes,
            operation_type=operation_type,
            uploaded_filename=uploaded_filename,
            uploaded_content_type=uploaded_content_type,
        )

    def handle_ff_stock_confirm_request(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        preview_id = str(payload.get("preview_id") or "").strip()
        if not preview_id:
            raise ValueError("preview_id is required")
        return self.ff_stock_ledger_block.confirm_manual_operation(preview_id, created_by=actor)

    def handle_ff_stock_operation_file_request(self, operation_id: str) -> tuple[bytes, str, str]:
        return self.ff_stock_ledger_block.download_operation_source_file(operation_id)

    def handle_nomenclature_list_request(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        visibility = str(params.get("visibility") or "visible")
        return self.supplier_shipments_block.list_nomenclature(visibility=visibility)

    def handle_nomenclature_export_request(self) -> tuple[bytes, str, str]:
        return self.supplier_shipments_block.export_nomenclature_xlsx()

    def handle_nomenclature_import_request(
        self,
        workbook_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        result = self.supplier_shipments_block.import_nomenclature_xlsx(
            workbook_bytes,
            uploaded_filename=uploaded_filename,
            uploaded_content_type=uploaded_content_type,
            dry_run=dry_run,
        )
        return (
            result
            if dry_run
            else self._attach_wb_finance_cost_recalculation(result)
        )

    def handle_nomenclature_create_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._attach_wb_finance_cost_recalculation(
            self.supplier_shipments_block.create_nomenclature_item(payload)
        )

    def handle_nomenclature_patch_request(self, item_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._attach_wb_finance_cost_recalculation(
            self.supplier_shipments_block.update_nomenclature_item(item_id, payload)
        )

    def handle_nomenclature_delete_request(self, item_id: str) -> dict[str, Any]:
        return self._attach_wb_finance_cost_recalculation(
            self.supplier_shipments_block.deactivate_nomenclature_item(item_id)
        )

    def handle_nomenclature_barcode_sync_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._attach_wb_finance_cost_recalculation(
            self.supplier_shipments_block.sync_nomenclature_barcodes(payload)
        )

    def handle_nomenclature_item_barcode_sync_request(self, item_id: str) -> dict[str, Any]:
        return self._attach_wb_finance_cost_recalculation(
            self.supplier_shipments_block.sync_nomenclature_item_barcode(item_id)
        )

    def _attach_wb_finance_cost_recalculation(
        self, result: Mapping[str, Any]
    ) -> dict[str, Any]:
        payload = dict(result)
        payload["wb_finance_cost_recalculation"] = (
            self.wb_finance_weekly_block.recalculate_stale_cost_weeks()
        )
        return payload

    def handle_sku_groups_list_request(self) -> dict[str, Any]:
        return self.supplier_shipments_block.list_sku_groups(include_inactive=True)

    def handle_sku_groups_create_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.supplier_shipments_block.create_sku_group(payload)

    def handle_sku_groups_patch_request(self, group_key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.supplier_shipments_block.update_sku_group(group_key, payload)

    def handle_sku_groups_delete_request(self, group_key: str) -> dict[str, Any]:
        return self.supplier_shipments_block.deactivate_sku_group(group_key)

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
            wb_supplies_payload: dict[str, Any] | None = None
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
                    wb_supplies_payload=wb_supplies_payload,
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

            wb_supplies_payload = self._run_wb_supplies_auto_sync(log=emit)
            finished_at = self.activated_at_factory()
            auto_result = _build_auto_update_result_payload(
                refresh_payload=refresh_payload,
                load_payload=load_payload,
                technical_status="success",
                finished_at=finished_at,
                error=None,
                wb_supplies_payload=wb_supplies_payload,
            )
            auto_status = str(auto_result.get("semantic_status") or "warning")
            self.runtime.save_sheet_vitrina_auto_update_result(
                started_at=started_at,
                finished_at=finished_at,
                status=auto_status,
                as_of_date=str(refresh_payload["as_of_date"]),
                snapshot_id=str(refresh_payload["snapshot_id"]),
                refreshed_at=str(refresh_payload["refreshed_at"]),
                error=None if auto_status == "success" else str(auto_result.get("semantic_reason") or ""),
                result_payload=auto_result,
            )
            emit(
                _format_log_event(
                    "cycle_finish",
                    cycle="auto_update",
                    status=auto_status,
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
            payload["wb_supplies_auto_sync_status"] = auto_result.get("wb_supplies_auto_sync_status")
            payload["wb_supplies_auto_sync"] = auto_result.get("wb_supplies_auto_sync")
            payload["operation"] = "auto_update"
            payload["auto_update_started_at"] = started_at
            payload["auto_update_finished_at"] = finished_at
            payload["server_context"] = self.build_sheet_server_context()
            payload["manual_context"] = self.build_sheet_manual_context()
            payload["load_context"] = self.build_sheet_load_context()
            return payload

    def _run_wb_supplies_auto_sync(self, *, log: OperatorLogEmitter | None) -> dict[str, Any]:
        emit = log or _noop_log
        started_at = self.activated_at_factory()
        result: dict[str, Any] = {
            "status": "warning",
            "semantic_status": "warning",
            "stage": "wb_supplies_auto_sync",
            "started_at": started_at,
            "official_sync": {"status": "not_started"},
            "transit_cost": {"status": "not_started"},
        }
        emit(_format_log_event("wb_supplies_auto_sync_start", stage="official_sync"))
        try:
            official_payload = self.wb_supplies_block.sync_supplies(
                {
                    "mode": "incremental_refresh",
                    "limit": 1000,
                    "enrich": "changed_only",
                    "list_params": {
                        "limit": 20,
                        "offset": 0,
                        "size_filter": "main_250",
                        "sort_key": "supply_date",
                        "sort_dir": "desc",
                    },
                }
            )
            official_sync = dict(official_payload.get("sync") or {})
            result["official_sync"] = {
                "status": "success",
                "run_id": str(official_sync.get("run_id") or ""),
                "raw_merged_count": int(official_sync.get("raw_merged_count") or 0),
                "new_rows": int(official_sync.get("new_rows") or 0),
                "changed_rows": int(official_sync.get("changed_rows") or 0),
                "unchanged_rows": int(official_sync.get("unchanged_rows") or 0),
                "enriched": int(official_sync.get("enriched") or 0),
                "failed_enrich": int(official_sync.get("failed_enrich") or 0),
                "forced_status_refresh_rows": int(official_sync.get("forced_status_refresh_rows") or 0),
                "refreshed_recent_historical_rows": int(official_sync.get("refreshed_recent_historical_rows") or 0),
                "accepted_qty_changed_rows": int(official_sync.get("accepted_qty_changed_rows") or 0),
                "warnings": list(official_sync.get("warnings") or [])[:10],
            }
            result["status"] = "success"
            result["semantic_status"] = "success"
            emit(
                _format_log_event(
                    "wb_supplies_auto_sync_finish",
                    stage="official_sync",
                    status="success",
                    run_id=result["official_sync"]["run_id"],
                    changed_rows=result["official_sync"]["changed_rows"],
                    accepted_qty_changed_rows=result["official_sync"]["accepted_qty_changed_rows"],
                )
            )
        except Exception as exc:  # noqa: BLE001 - auto-refresh must not fail critical web-vitrina snapshot.
            error = str(exc)
            result["official_sync"] = {"status": "failed", "error": error}
            result["status"] = "warning"
            result["semantic_status"] = "warning"
            result["reason"] = f"WB supplies official sync failed: {error}"
            emit(_format_log_event("wb_supplies_auto_sync_finish", stage="official_sync", status="warning", error=error))
            result["finished_at"] = self.activated_at_factory()
            return result

        transit_result = self._maybe_start_wb_supplies_auto_transit_cost_enrichment(log=emit)
        result["transit_cost"] = transit_result
        if str(transit_result.get("status") or "") not in {"success", "queued", "skipped_no_candidates"}:
            result["status"] = "warning"
            result["semantic_status"] = "warning"
        result["reason"] = _wb_supplies_auto_sync_reason(result)
        result["finished_at"] = self.activated_at_factory()
        return result

    def _maybe_start_wb_supplies_auto_transit_cost_enrichment(self, *, log: OperatorLogEmitter | None) -> dict[str, Any]:
        emit = log or _noop_log
        try:
            from apps.seller_portal_automation_guard import current_lock_status

            lock_status = current_lock_status(self.runtime.runtime_dir)
        except Exception as exc:  # noqa: BLE001 - diagnostics-only preflight.
            return {"status": "skipped_lock_probe_failed", "warning": str(exc)}
        if bool(lock_status.get("busy")):
            return {"status": "skipped_lock_busy", "warning": "seller_portal_automation_busy", "lock": lock_status}
        try:
            session = self.handle_seller_portal_session_check_request(
                launcher_download_path=SHEET_VITRINA_SELLER_RECOVERY_LAUNCHER_ROUTE,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics-only preflight.
            return {"status": "skipped_session_probe_failed", "warning": str(exc)}
        session_status = str(session.get("status") or "")
        if session_status != "session_valid_canonical":
            return {
                "status": "skipped_session_not_valid",
                "warning": session_status or "session_not_valid",
                "session_status": session_status,
                "session_status_label": str(session.get("status_label") or ""),
            }
        try:
            payload = self.wb_supplies_block.start_transit_cost_enrichment({"limit": 20, "force": False})
        except Exception as exc:  # noqa: BLE001 - transit cost is supplemental.
            return {"status": "failed_to_start", "warning": str(exc), "session_status": session_status}
        active_run = payload.get("active_run") if isinstance(payload.get("active_run"), Mapping) else {}
        candidate_count = int(payload.get("candidate_count") or active_run.get("candidate_count") or 0)
        run_status = str(payload.get("status") or active_run.get("status") or "")
        status = "skipped_no_candidates" if candidate_count <= 0 else "queued" if run_status in {"queued", "running"} else run_status or "accepted"
        emit(
            _format_log_event(
                "wb_supplies_auto_transit_cost",
                status=status,
                run_id=str(payload.get("run_id") or ""),
                candidate_count=candidate_count,
            )
        )
        return {
            "status": status,
            "run_id": str(payload.get("run_id") or ""),
            "candidate_count": candidate_count,
            "session_status": session_status,
            "limit": 20,
        }

    def _run_sheet_scheduled_auto_update(
        self,
        *,
        schedule_id: str,
        due_at: str,
        trigger_source: str,
        log: OperatorLogEmitter | None,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        started_at = self.activated_at_factory()
        run_id = SHEET_OPERATOR_JOB_ID.get()
        self.sheet_auto_refresh_schedules_block.mark_run_started(
            schedule_id,
            started_at=started_at,
            due_at=due_at,
            run_id=run_id,
            trigger_source=trigger_source,
        )
        emit(
            _format_log_event(
                "auto_schedule_start",
                schedule_id=schedule_id,
                due_at=due_at,
                trigger_source=trigger_source,
                run_id=run_id,
            )
        )
        try:
            result = self._run_sheet_auto_update(as_of_date=None, log=emit)
        except Exception as exc:
            finished_at = self.activated_at_factory()
            self.sheet_auto_refresh_schedules_block.mark_run_finished(
                schedule_id,
                finished_at=finished_at,
                error=str(exc),
            )
            raise
        finished_at = self.activated_at_factory()
        self.sheet_auto_refresh_schedules_block.mark_run_finished(
            schedule_id,
            finished_at=finished_at,
            result_payload=result,
        )
        result["auto_schedule"] = self.sheet_auto_refresh_schedules_block.get_schedule(schedule_id)
        result["auto_schedule_trigger_source"] = trigger_source
        return result

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
                previous_plan, previous_refreshed_at = _load_existing_ready_snapshot_for_preservation(
                    self.runtime,
                    as_of_date=plan.as_of_date,
                )
                plan = _with_full_refresh_metadata(
                    plan,
                    refreshed_at=refreshed_at,
                    previous_plan=previous_plan,
                    previous_refreshed_at=previous_refreshed_at,
                )
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
                our_wb_cost_recalculate = self._run_our_wb_cost_post_refresh_recalculate(
                    emit=emit,
                    refresh_diagnostics=refresh_diagnostics,
                )
                refresh_diagnostics["our_wb_cost_recalculate"] = our_wb_cost_recalculate
                if our_wb_cost_recalculate.get("changed"):
                    emit(
                        _format_log_event(
                            "our_wb_cost_snapshot_rebuild_start",
                            cycle="refresh",
                            as_of_date=effective_as_of_date,
                            reason="post_refresh_recalculate_changed_runtime_state",
                        )
                    )
                    rebuild_phase = _start_operator_phase(
                        "rebuild_plan_after_our_wb_cost_recalculate",
                        started_at=self.activated_at_factory(),
                    )
                    initial_plan = plan
                    plan = self.sheet_plan_block.build_plan(
                        as_of_date=effective_as_of_date,
                        log=emit,
                        execution_mode=execution_mode,
                    )
                    plan = _with_full_refresh_metadata(
                        plan,
                        refreshed_at=refreshed_at,
                        previous_plan=initial_plan,
                        previous_refreshed_at=refreshed_at,
                    )
                    refresh_result = self.runtime.save_sheet_vitrina_ready_snapshot(
                        current_state=current_state,
                        refreshed_at=refreshed_at,
                        plan=plan,
                    )
                    _finish_operator_phase(
                        refresh_diagnostics,
                        rebuild_phase,
                        finished_at=self.activated_at_factory(),
                        status="success",
                    )
                    emit(
                        _format_log_event(
                            "our_wb_cost_snapshot_rebuild_finish",
                            cycle="refresh",
                            snapshot_id=refresh_result.snapshot_id,
                            data_rows=refresh_result.sheet_row_counts.get("DATA_VITRINA"),
                        )
                    )
                promo_gc_phase = _start_operator_phase(
                    "promo_artifact_light_gc",
                    started_at=self.activated_at_factory(),
                )
                promo_gc_summary = _run_promo_artifact_light_gc_after_refresh(
                    runtime_dir=self.runtime.runtime_dir,
                    refresh_diagnostics=refresh_diagnostics,
                    runner=self.promo_artifact_gc_runner,
                    emit=emit,
                )
                _finish_operator_phase(
                    refresh_diagnostics,
                    promo_gc_phase,
                    finished_at=self.activated_at_factory(),
                    status=(
                        "success"
                        if str(promo_gc_summary.get("status") or "") == "success"
                        else "warning"
                    ),
                    note_kind=(
                        None
                        if str(promo_gc_summary.get("status") or "") == "success"
                        else "promo_artifact_gc_warning"
                    ),
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

    def _run_our_wb_cost_post_refresh_recalculate(
        self,
        *,
        emit: OperatorLogEmitter,
        refresh_diagnostics: dict[str, Any],
    ) -> dict[str, Any]:
        emit(_format_log_event("our_wb_cost_recalculate_start", cycle="refresh", stage="post_ready_snapshot"))
        phase = _start_operator_phase(
            "our_wb_cost_post_refresh_recalculate",
            started_at=self.activated_at_factory(),
        )
        result = self.our_wb_cost_block.rebuild_all()
        payload = {
            "supplier_layers_materialized": int(getattr(result, "supplier_layers_materialized", 0) or 0),
            "wb_supply_layers_materialized": int(getattr(result, "wb_supply_layers_materialized", 0) or 0),
            "opening_rows_materialized": int(getattr(result, "opening_rows_materialized", 0) or 0),
            "daily_state_rows_materialized": int(getattr(result, "daily_state_rows_materialized", 0) or 0),
        }
        own_result = self.own_product_capital_block.recalculate()
        payload["own_product_capital"] = asdict(own_result)
        payload["own_product_capital_daily_rows_changed"] = int(
            getattr(own_result, "daily_rows_changed", 0) or 0
        )
        changed = any(
            value > 0
            for key, value in payload.items()
            if key != "own_product_capital" and isinstance(value, (int, float))
        )
        payload["changed"] = changed
        payload["wb_finance_cost_recalculation"] = (
            self.wb_finance_weekly_block.recalculate_stale_cost_weeks()
        )
        _finish_operator_phase(
            refresh_diagnostics,
            phase,
            finished_at=self.activated_at_factory(),
            status="success",
        )
        emit(_format_log_event("our_wb_cost_recalculate_finish", cycle="refresh", **payload))
        return payload

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
                session_preflight: dict[str, Any] | None = None
                if source_group_id == "seller_portal_bot":
                    stage = "session_preflight"
                    emit(
                        _format_log_event(
                            "group_refresh_session_preflight_start",
                            stage=stage,
                            source_group_id=source_group_id,
                            source_group_label=group_label,
                            as_of_date=selected_as_of_date,
                            target_snapshot_as_of_date=target_snapshot_as_of_date,
                        )
                    )
                    session_preflight = self.handle_seller_portal_session_check_request(
                        launcher_download_path=SHEET_VITRINA_SELLER_RECOVERY_LAUNCHER_ROUTE,
                    )
                    session_status = str(session_preflight.get("status") or "").strip()
                    if session_status != "session_valid_canonical":
                        finished_at = self.activated_at_factory()
                        payload = _build_group_refresh_session_action_required_payload(
                            source_group_id=source_group_id,
                            source_group_label=group_label,
                            selected_as_of_date=selected_as_of_date,
                            target_snapshot_as_of_date=target_snapshot_as_of_date,
                            source_keys=source_keys,
                            started_at=started_at,
                            finished_at=finished_at,
                            session_preflight=session_preflight,
                        )
                        emit(
                            _format_log_event(
                                "group_refresh_session_preflight_finish",
                                stage=stage,
                                status="action_required",
                                source_group_id=source_group_id,
                                session_status=session_status,
                                reason=str(payload.get("semantic_reason") or ""),
                            )
                        )
                        emit(
                            _format_log_event(
                                "group_refresh_finish",
                                status="action_required",
                                failed_stage=stage,
                                source_group_id=source_group_id,
                                as_of_date=selected_as_of_date,
                                target_snapshot_as_of_date=target_snapshot_as_of_date,
                                session_status=session_status,
                                reason=str(payload.get("semantic_reason") or ""),
                                duration_seconds=_duration_seconds(started_at, finished_at),
                            )
                        )
                        return payload
                    emit(
                        _format_log_event(
                            "group_refresh_session_preflight_finish",
                            stage=stage,
                            status="success",
                            source_group_id=source_group_id,
                            session_status=session_status,
                        )
                    )

                current_state = self.runtime.load_current_state()
                metric_keys = _metric_keys_for_source_keys(
                    extend_metrics_with_sku_action_metrics(
                        extend_metrics_with_own_product_capital_metrics(
                            extend_metrics_with_our_wb_cost_metrics(
                                extend_metrics_with_onec_stock_metrics(current_state.metrics_v2)
                            )
                        )
                    ),
                    source_keys=source_keys,
                )
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
                current_bundle_dates = set(
                    self.runtime.list_sheet_vitrina_ready_snapshot_dates(
                        date_from=target_snapshot_as_of_date,
                        date_to=target_snapshot_as_of_date,
                    )
                )
                if target_snapshot_as_of_date in current_bundle_dates:
                    previous_plan = self.runtime.load_sheet_vitrina_ready_snapshot(
                        as_of_date=target_snapshot_as_of_date
                    )
                    previous_status = self.runtime.load_sheet_vitrina_refresh_status(
                        as_of_date=target_snapshot_as_of_date
                    )
                elif source_group_id == OWN_PRODUCT_CAPITAL_SOURCE_GROUP_ID:
                    previous_plan = self.runtime.load_sheet_vitrina_ready_snapshot_any_bundle(
                        as_of_date=target_snapshot_as_of_date
                    )
                    previous_status = self.runtime.load_sheet_vitrina_refresh_status_any_bundle(
                        as_of_date=target_snapshot_as_of_date
                    )
                else:
                    raise ValueError(
                        "sheet_vitrina_v1 ready snapshot missing in current bundle: "
                        f"as_of_date={target_snapshot_as_of_date}"
                    )
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
                snapshot_semantic = {
                    "status": str(payload.get("semantic_status") or ""),
                    "label": str(payload.get("semantic_label") or ""),
                    "tone": str(payload.get("semantic_tone") or ""),
                    "reason": str(payload.get("semantic_reason") or ""),
                }
                group_semantic = _source_group_refresh_semantic_payload(merge_summary)
                payload.update(
                    {
                        "operation": "refresh_group",
                        "source_group_id": source_group_id,
                        "source_group_label": group_label,
                        "selected_as_of_date": selected_as_of_date,
                        "target_snapshot_as_of_date": target_snapshot_as_of_date,
                        "source_keys": source_keys,
                        "metric_keys": metric_keys,
                        "session_preflight": session_preflight or {},
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "duration_seconds": duration_seconds,
                        "merge_summary": merge_summary,
                        "updated_cells": merge_summary["updated_cells"],
                        "updated_cell_count": merge_summary["updated_cell_count"],
                        "latest_confirmed_cell_count": merge_summary["latest_confirmed_cell_count"],
                        "technical_status": payload["status"],
                        "semantic_status": group_semantic["semantic_status"],
                        "semantic_label": group_semantic["semantic_label"],
                        "semantic_tone": group_semantic["semantic_tone"],
                        "semantic_reason": group_semantic["semantic_reason"],
                        "status_label": group_semantic["semantic_label"],
                        "status_reason": group_semantic["semantic_reason"],
                        "snapshot_semantic_status": snapshot_semantic["status"],
                        "snapshot_semantic_label": snapshot_semantic["label"],
                        "snapshot_semantic_tone": snapshot_semantic["tone"],
                        "snapshot_semantic_reason": snapshot_semantic["reason"],
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
                error_payload = _build_group_refresh_error_payload(
                    source_group_id=source_group_id,
                    source_group_label=group_label,
                    selected_as_of_date=selected_as_of_date,
                    target_snapshot_as_of_date=target_snapshot_as_of_date,
                    source_keys=source_keys,
                    failed_stage=stage,
                    error=str(exc),
                    started_at=started_at,
                    finished_at=finished_at,
                )
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
                raise SheetVitrinaV1OperatorJobError(
                    f"failed at {stage}: {exc}",
                    result_payload=error_payload,
                ) from exc

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
            run_status = str(payload.get("run_status") or payload.get("status") or "").strip()
            operation_result = _seller_portal_recovery_operation_result(run_status)
            semantic_status = _seller_portal_recovery_operation_semantic_status(run_status)
            result = dict(payload)
            result.update(
                {
                    "operation": "session_recovery_start",
                    "operation_result": operation_result,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "duration_seconds": _duration_seconds(started_at, finished_at),
                    "semantic_status": semantic_status,
                    "semantic_label": str(payload.get("status_label") or "Запрошено"),
                    "semantic_tone": semantic_status,
                    "semantic_reason": str(payload.get("reason") or payload.get("summary") or payload.get("message") or ""),
                }
            )
            emit(
                _format_log_event(
                    "seller_recovery_finish",
                    result=operation_result,
                    run_status=run_status,
                    running=bool(payload.get("running")),
                    launcher_ready=bool(payload.get("launcher_ready") or payload.get("can_download_launcher")),
                    reason=str(payload.get("reason") or payload.get("summary") or payload.get("message") or ""),
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
        auto_result = _format_operator_result_payload(auto_update_state.last_run_result) or {}
        auto_result = _sanitize_auto_result_payload(auto_result)
        auto_context = {
            "last_auto_run_status": auto_update_state.last_run_status or "never",
            "last_auto_run_technical_status": str(auto_result.get("technical_status") or auto_update_state.last_run_status or "never"),
            "last_auto_run_time": _format_optional_business_timestamp(auto_update_state.last_run_started_at),
            "last_auto_run_finished_at": _format_optional_business_timestamp(auto_update_state.last_run_finished_at),
            "last_successful_auto_update_at": _format_optional_business_timestamp(
                auto_update_state.last_successful_auto_update_at
            ),
            "last_auto_run_error": _sanitize_auto_update_reason(auto_update_state.last_run_error or ""),
            "last_auto_run_status_reason": (
                str(auto_result.get("semantic_reason") or "")
                if auto_result
                else _sanitize_auto_update_reason(auto_update_state.last_run_error or "")
            ),
        }
        auto_schedules_payload = self.sheet_auto_refresh_schedules_block.build_payload(auto_context=auto_context)
        auto_schedule_policy = (
            dict(auto_schedules_payload.get("schedule_policy") or {})
            if isinstance(auto_schedules_payload.get("schedule_policy"), Mapping)
            else {}
        )
        schedule_rows = [
            _sanitize_auto_schedule_row(dict(item))
            for item in auto_schedules_payload.get("effective_schedules") or auto_schedules_payload.get("schedules", [])
            if isinstance(item, Mapping)
        ]
        enabled_times = [
            str(item.get("local_time_hhmm") or "")
            for item in schedule_rows
            if bool(item.get("enabled", True)) and str(item.get("local_time_hhmm") or "")
        ]
        business_times = ", ".join(enabled_times) or "disabled"
        last_auto_run_status = str(auto_schedules_payload.get("last_auto_run_status") or auto_update_state.last_run_status or "never")
        last_auto_run_technical_status = str(
            auto_schedules_payload.get("last_auto_run_technical_status")
            or auto_result.get("technical_status")
            or last_auto_run_status
        )
        return {
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "business_now": business_now,
            "default_as_of_date": default_business_as_of_date(now),
            "today_current_date": current_business_date_iso(now),
            "daily_refresh_business_time": f"{business_times} {CANONICAL_BUSINESS_TIMEZONE_NAME}",
            "daily_refresh_systemd_time": "every 10 minutes UTC due-check",
            "daily_refresh_systemd_oncalendar": SHEET_AUTO_REFRESH_TICK_ONCALENDAR,
            "daily_auto_action": SHEET_VITRINA_DAILY_AUTO_ACTION,
            "daily_auto_description": (
                f"Runtime-managed schedules ({business_times} {CANONICAL_BUSINESS_TIMEZONE_NAME}) "
                f"trigger {SHEET_VITRINA_DAILY_AUTO_ACTION}."
            ),
            "daily_auto_trigger_name": SHEET_VITRINA_DAILY_TIMER_NAME,
            "daily_auto_trigger_description": SHEET_VITRINA_DAILY_TRIGGER_DESCRIPTION,
            "daily_auto_schedule_mode": SHEET_AUTO_REFRESH_SCHEDULE_MODE,
            "daily_auto_schedule_mode_type": str(auto_schedules_payload.get("schedule_mode_type") or auto_schedule_policy.get("mode") or "manual"),
            "daily_auto_schedule_source": SHEET_AUTO_REFRESH_SCHEDULE_SOURCE,
            "daily_auto_schedule_policy": auto_schedule_policy,
            "daily_auto_interval_options": auto_schedules_payload.get("interval_options") or [],
            "daily_auto_interval_preview_slots": auto_schedules_payload.get("interval_preview_slots") or [],
            "daily_auto_schedules": schedule_rows,
            "daily_auto_schedule_editable": True,
            "daily_auto_schedule_blocker": "",
            "next_auto_run_at": str(auto_schedules_payload.get("next_auto_run_at") or ""),
            "auto_schedule_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "auto_schedule_source": SHEET_AUTO_REFRESH_SCHEDULE_SOURCE,
            "retry_runner_description": SHEET_VITRINA_RETRY_RUNNER_DESCRIPTION,
            "last_auto_run_status": last_auto_run_status,
            "last_auto_run_technical_status": last_auto_run_technical_status,
            "last_auto_run_status_label": (
                str(auto_schedules_payload.get("last_auto_run_status_label") or "")
                or (str(auto_result.get("semantic_label") or "") if auto_result else "")
                or _auto_update_status_label(last_auto_run_status)
            ),
            "last_auto_run_status_reason": (
                _sanitize_auto_update_reason(str(auto_schedules_payload.get("last_auto_run_status_reason") or ""))
                or (str(auto_result.get("semantic_reason") or "") if auto_result else "")
                or _sanitize_auto_update_reason(auto_update_state.last_run_error or "")
            ),
            "last_auto_run_technical_status_label": _auto_update_status_label(last_auto_run_technical_status),
            "last_auto_run_time": str(auto_schedules_payload.get("last_auto_run_time") or ""),
            "last_auto_run_at": str(auto_schedules_payload.get("last_auto_run_at") or ""),
            "last_auto_run_finished_at": str(auto_schedules_payload.get("last_auto_run_finished_at") or ""),
            "last_successful_auto_update_at": str(auto_schedules_payload.get("last_successful_auto_update_at") or ""),
            "last_auto_success_at": str(auto_schedules_payload.get("last_auto_success_at") or ""),
            "last_auto_error_at": str(auto_schedules_payload.get("last_auto_error_at") or ""),
            "last_auto_error_summary": _sanitize_auto_update_reason(str(auto_schedules_payload.get("last_auto_error_summary") or "")),
            "last_auto_job_id": str(auto_schedules_payload.get("last_auto_job_id") or ""),
            "last_auto_run_error": _sanitize_auto_update_reason(str(auto_schedules_payload.get("last_auto_run_error") or "")),
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
            "current_business_date": current_business_date_iso(self.now_factory()),
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
    can_download_launcher = bool(run_id) and run_status == "awaiting_login" and not requested_run_mismatch
    final_marker = _seller_portal_recovery_final_marker(run_status) if run_is_final else ""
    probe_reason = _seller_portal_probe_reason(current_probe_payload)
    reason = probe_reason or summary or str(raw.get("message") or "").strip()
    return {
        "status": run_status,
        "status_label": _seller_portal_recovery_status_label(run_status),
        "status_tone": _seller_portal_recovery_status_tone(run_status),
        "run_status": run_status,
        "run_status_label": _seller_portal_recovery_status_label(run_status),
        "run_status_tone": _seller_portal_recovery_status_tone(run_status),
        "summary": summary,
        "instruction": instruction,
        "probe_reason": probe_reason,
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
        "launcher_enabled": can_download_launcher,
        "launcher_ready": can_download_launcher,
        "can_download_launcher": can_download_launcher,
        "can_open_login_window": False,
        "open_login_window_url": "",
        "launcher_url": launcher_download_path if can_download_launcher else "",
        "launcher_download_path": launcher_download_path,
        "reason": reason,
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
        "final_marker": final_marker,
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
        "storage_state_path": str(getattr(config, "storage_state_path", "") or ""),
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
    probe_reason = _seller_portal_probe_reason(current_probe_payload)
    reason = probe_reason or summary or str(raw.get("message") or "").strip()
    return {
        "status": status,
        "status_label": _seller_portal_session_check_status_label(status),
        "status_tone": _seller_portal_session_check_status_tone(status),
        "summary": summary,
        "instruction": instruction,
        "probe_reason": probe_reason,
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
        "launcher_ready": False,
        "can_download_launcher": False,
        "can_open_login_window": False,
        "open_login_window_url": "",
        "launcher_url": "",
        "launcher_download_path": launcher_download_path,
        "reason": reason,
        "run_id": "",
        "current_run_id": "",
        "run_status": "idle",
        "run_status_label": _seller_portal_recovery_status_label("idle"),
        "run_status_tone": _seller_portal_recovery_status_tone("idle"),
        "run_is_final": False,
        "run_final_status": "",
        "run_final_label": "",
        "final_marker": "",
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
        "storage_state_path": str(getattr(config, "storage_state_path", "") or ""),
    }


def _build_group_refresh_session_action_required_payload(
    *,
    source_group_id: str,
    source_group_label: str,
    selected_as_of_date: str,
    target_snapshot_as_of_date: str,
    source_keys: list[str],
    started_at: str,
    finished_at: str,
    session_preflight: Mapping[str, Any],
) -> dict[str, Any]:
    session_status = str(session_preflight.get("status") or "session_probe_error").strip()
    reason = _seller_session_action_required_reason(session_preflight)
    return {
        "operation": "refresh_group",
        "status": "action_required",
        "technical_status": "blocked",
        "semantic_status": "action_required",
        "semantic_label": "Требуется вход Seller",
        "semantic_tone": "error",
        "semantic_reason": reason,
        "status_label": "Требуется вход Seller",
        "status_reason": reason,
        "source_group_id": source_group_id,
        "source_group_label": source_group_label,
        "selected_as_of_date": selected_as_of_date,
        "target_snapshot_as_of_date": target_snapshot_as_of_date,
        "source_keys": list(source_keys),
        "failed_stage": "session_preflight",
        "action_required": True,
        "action": "run_seller_portal_recovery",
        "operator_next_step": "Запустите восстановление Seller Portal, дождитесь валидной сессии и повторите обновление группы.",
        "seller_recovery_start_path": SHEET_VITRINA_SELLER_RECOVERY_START_ROUTE,
        "seller_session_check_path": SHEET_VITRINA_SELLER_SESSION_CHECK_ROUTE,
        "session_status": session_status,
        "session_status_label": str(session_preflight.get("status_label") or ""),
        "session_status_tone": str(session_preflight.get("status_tone") or ""),
        "session_probe_reason": str(
            session_preflight.get("probe_reason")
            or session_preflight.get("summary")
            or session_preflight.get("reason")
            or session_preflight.get("message")
            or ""
        ),
        "session_technical_line": str(session_preflight.get("technical_line") or ""),
        "session_preflight": dict(session_preflight),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration_seconds(started_at, finished_at),
        "merge_summary": {
            "rows_updated": 0,
            "rows_preserved": 0,
            "status_rows_updated": 0,
            "updated_cells": [],
            "updated_cell_count": 0,
            "latest_confirmed_cell_count": 0,
        },
        "updated_cells": [],
        "updated_cell_count": 0,
        "latest_confirmed_cell_count": 0,
    }


def _build_group_refresh_error_payload(
    *,
    source_group_id: str,
    source_group_label: str,
    selected_as_of_date: str,
    target_snapshot_as_of_date: str,
    source_keys: list[str],
    failed_stage: str,
    error: str,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    session_related = _group_refresh_error_is_session_related(error)
    session_status = _session_status_from_error_text(error) if session_related else ""
    reason = _group_refresh_error_reason(
        error,
        failed_stage=failed_stage,
        session_status=session_status,
    )
    payload = {
        "operation": "refresh_group",
        "status": "error",
        "technical_status": "error",
        "semantic_status": "error",
        "semantic_label": "Ошибка обновления группы",
        "semantic_tone": "error",
        "semantic_reason": reason,
        "status_label": "Ошибка обновления группы",
        "status_reason": reason,
        "source_group_id": source_group_id,
        "source_group_label": source_group_label,
        "selected_as_of_date": selected_as_of_date,
        "target_snapshot_as_of_date": target_snapshot_as_of_date,
        "source_keys": list(source_keys),
        "failed_stage": failed_stage,
        "command_phase_label": failed_stage,
        "error": str(error or ""),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration_seconds(started_at, finished_at),
        "action_required": session_related,
        "operator_next_step": (
            "Запустите восстановление Seller Portal и повторите обновление группы."
            if session_related
            else "Откройте лог job и проверьте указанный failed_stage."
        ),
        "updated_cells": [],
        "updated_cell_count": 0,
        "latest_confirmed_cell_count": 0,
    }
    if session_related:
        payload.update(
            {
                "session_status": session_status,
                "session_probe_reason": _seller_session_error_reason(error, session_status=session_status),
                "seller_recovery_start_path": SHEET_VITRINA_SELLER_RECOVERY_START_ROUTE,
            }
        )
    return payload


def _seller_session_action_required_reason(session_preflight: Mapping[str, Any]) -> str:
    status = str(session_preflight.get("status") or "").strip()
    summary = str(
        session_preflight.get("probe_reason")
        or session_preflight.get("summary")
        or session_preflight.get("reason")
        or session_preflight.get("message")
        or ""
    ).strip()
    prefix = "Сессия Seller Portal недействительна"
    if status == "session_missing":
        prefix = "Сессия Seller Portal отсутствует"
    elif status == "session_valid_wrong_org":
        prefix = "Сессия Seller Portal открыта не в том кабинете"
    elif status == "session_probe_error":
        prefix = "Ошибка проверки Seller Portal session"
    return f"{prefix}: {summary}" if summary else prefix


def _seller_portal_probe_reason(current_probe: Mapping[str, Any] | None) -> str:
    if not isinstance(current_probe, Mapping):
        return ""
    explicit_reason = str(current_probe.get("reason") or "").strip()
    if explicit_reason:
        return explicit_reason
    if bool(current_probe.get("has_validate_401")):
        return "validate_401"
    final_url = str(current_probe.get("final_url") or "").lower()
    if "seller-auth.wildberries.ru" in final_url:
        return "login_redirect"
    markers = current_probe.get("body_markers")
    if isinstance(markers, Mapping):
        if bool(markers.get("captcha_or_challenge")):
            return "security_challenge"
        if bool(markers.get("access_denied")):
            return "access_denied"
        if bool(markers.get("login_page")):
            return "login_page"
    return ""


def _group_refresh_error_reason(error: str, *, failed_stage: str, session_status: str) -> str:
    normalized_error = str(error or "").strip()
    if session_status:
        return (
            f"failed_stage={failed_stage}; "
            f"{_seller_session_error_reason(normalized_error, session_status=session_status)}"
        )
    return f"failed_stage={failed_stage}; {normalized_error}" if normalized_error else f"failed_stage={failed_stage}"


def _group_refresh_error_is_session_related(error: str) -> bool:
    lowered = str(error or "").lower()
    return any(
        marker in lowered
        for marker in (
            "seller_portal_session_invalid",
            "seller_portal_session_missing",
            "seller_portal_wrong_supplier",
            "seller_portal_session_probe_failed",
            "manual_relogin_required=login_and_save_state",
        )
    )


def _session_status_from_error_text(error: str) -> str:
    lowered = str(error or "").lower()
    if "seller_portal_session_missing" in lowered:
        return "session_missing"
    if "seller_portal_wrong_supplier" in lowered:
        return "session_valid_wrong_org"
    if "seller_portal_session_invalid" in lowered or "manual_relogin_required=login_and_save_state" in lowered:
        return "session_invalid"
    return "session_probe_error"


def _seller_session_error_reason(error: str, *, session_status: str) -> str:
    if session_status == "session_missing":
        return "Сессия Seller Portal отсутствует; запустите recovery/relogin flow."
    if session_status == "session_valid_wrong_org":
        return "Выбран не canonical supplier; запустите recovery/relogin flow и переключите кабинет."
    if session_status == "session_invalid":
        return "Сессия Seller Portal недействительна; запустите recovery/relogin flow."
    return f"Проверка Seller Portal session завершилась ошибкой: {str(error or '').strip()}"


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


def _seller_portal_recovery_operation_result(status: str) -> str:
    normalized = str(status or "").strip()
    if normalized in {"completed", "not_needed"}:
        return "success"
    if normalized == "awaiting_login":
        return "launcher_ready"
    if normalized in {"starting", "saving_session", "validating_session", "checking_canonical_supplier", "triggering_refresh"}:
        return "accepted"
    if normalized in {"stopped", "timeout"}:
        return normalized
    if normalized == "error":
        return "failed"
    return "unknown"


def _seller_portal_recovery_operation_semantic_status(status: str) -> str:
    normalized = str(status or "").strip()
    if normalized in {"completed", "not_needed"}:
        return "success"
    if normalized == "error":
        return "error"
    return "warning"


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
                "Нажмите «Восстановить сессию»: launcher скачается автоматически после готовности окна входа.",
            )
        if session_status == "session_missing":
            return (
                "Новый запуск восстановления сейчас не выполняется. Сохранённая seller-сессия отсутствует.",
                "Нажмите «Восстановить сессию»: launcher скачается автоматически после готовности окна входа.",
            )
        return (
            "Новый запуск восстановления сейчас не выполняется.",
            "Сначала проверьте seller-сессию или запустите восстановление повторно.",
        )
    if status == "starting":
        return (
            "Запускаем текущее временное окно входа на host.",
            "Когда статус сменится на «Нужно войти», launcher скачается автоматически.",
        )
    if status == "awaiting_login":
        return (
            "Временное окно входа готово. Откройте скачанный launcher и войдите в seller portal.",
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
            "Откройте operator page заново и при необходимости запустите восстановление для нового launcher.",
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


def _seller_portal_recovery_final_marker(status: str) -> str:
    if status in {"completed", "not_needed", "stopped", "timeout", "error"}:
        return status
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
            "Нажмите «Восстановить сессию»: launcher скачается автоматически после готовности окна входа.",
        )
    if status == "session_missing":
        return (
            "Сохранённая seller-сессия не найдена.",
            "Нажмите «Восстановить сессию»: launcher скачается автоматически после готовности окна входа.",
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


def _normalize_web_vitrina_user_config_key(value: str) -> str:
    normalized = str(value or WEB_VITRINA_METRIC_PRESENTATION_CONFIG_KEY).strip()
    if normalized != WEB_VITRINA_METRIC_PRESENTATION_CONFIG_KEY:
        raise ValueError("unsupported web-vitrina user config key")
    return normalized


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("base_revision must be an integer") from exc


def _sanitize_web_vitrina_metric_presentation_config(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    scopes_payload = source.get("scopes") if isinstance(source.get("scopes"), Mapping) else {}
    scopes: dict[str, Any] = {}
    for raw_scope_id, raw_scope_payload in scopes_payload.items():
        scope_id = str(raw_scope_id or "").strip()
        if not scope_id or len(scope_id) > 80 or not isinstance(raw_scope_payload, Mapping):
            continue
        raw_order = raw_scope_payload.get("order")
        order: list[str] = []
        seen_order: set[str] = set()
        for metric_key in raw_order if isinstance(raw_order, list) else []:
            normalized_metric_key = str(metric_key or "").strip()
            if not normalized_metric_key or len(normalized_metric_key) > 160 or normalized_metric_key in seen_order:
                continue
            order.append(normalized_metric_key)
            seen_order.add(normalized_metric_key)
        raw_display = raw_scope_payload.get("display")
        display: dict[str, str] = {}
        if isinstance(raw_display, Mapping):
            for raw_metric_key, raw_status in raw_display.items():
                metric_key = str(raw_metric_key or "").strip()
                status = str(raw_status or "").strip()
                if (
                    metric_key
                    and len(metric_key) <= 160
                    and status in WEB_VITRINA_METRIC_DISPLAY_STATUSES
                    and status != "shown"
                ):
                    display[metric_key] = status
        scopes[scope_id] = {
            "order": order,
            "display": display,
            "manual": bool(raw_scope_payload.get("manual")),
        }

    expanded_anchors: list[str] = []
    seen_anchors: set[str] = set()
    for token in source.get("expanded_anchors") if isinstance(source.get("expanded_anchors"), list) else []:
        normalized_token = str(token or "").strip()
        if normalized_token and len(normalized_token) <= 260 and normalized_token not in seen_anchors:
            expanded_anchors.append(normalized_token)
            seen_anchors.add(normalized_token)

    return {
        "version": WEB_VITRINA_METRIC_PRESENTATION_PAYLOAD_VERSION,
        "scopes": scopes,
        "expanded_anchors": expanded_anchors,
    }


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
    wb_supplies_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    refresh_semantic = str((refresh_payload or {}).get("semantic_status") or "warning")
    load_semantic = str((load_payload or {}).get("semantic_status") or "")
    wb_supplies_semantic = str((wb_supplies_payload or {}).get("semantic_status") or "")
    semantic_status = (
        "error"
        if technical_status == "error"
        else _worst_tone([value for value in [refresh_semantic, load_semantic, wb_supplies_semantic] if value])
    )
    semantic_reason = (
        str(error or "").strip()
        if technical_status == "error"
        else " | ".join(
            part
            for part in [
                f"refresh: {str((refresh_payload or {}).get('semantic_reason') or '').strip()}",
                f"load: {str((load_payload or {}).get('semantic_reason') or '').strip()}",
                f"wb_supplies: {_wb_supplies_auto_sync_reason(wb_supplies_payload or {})}",
            ]
            if not part.endswith(": ")
            and (load_payload is not None or not part.startswith("load:"))
            and (wb_supplies_payload is not None or not part.startswith("wb_supplies:"))
        )
    )
    return {
        "technical_status": technical_status,
        "semantic_status": semantic_status,
        "semantic_label": _semantic_status_label(semantic_status),
        "semantic_tone": semantic_status,
        "semantic_reason": _sanitize_auto_update_reason(semantic_reason)
        or ("auto_update завершился" if technical_status == "success" else "auto_update завершился ошибкой"),
        "snapshot_id": str((load_payload or refresh_payload or {}).get("snapshot_id") or ""),
        "as_of_date": str((load_payload or refresh_payload or {}).get("as_of_date") or ""),
        "refreshed_at": str((load_payload or refresh_payload or {}).get("refreshed_at") or ""),
        "finished_at": finished_at,
        "wb_supplies_auto_sync_status": str((wb_supplies_payload or {}).get("status") or ""),
        "wb_supplies_auto_sync": dict(wb_supplies_payload or {}),
    }


def _wb_supplies_auto_sync_reason(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping) or not payload:
        return ""
    explicit_reason = str(payload.get("reason") or "").strip()
    if explicit_reason:
        return explicit_reason
    official = payload.get("official_sync") if isinstance(payload.get("official_sync"), Mapping) else {}
    transit = payload.get("transit_cost") if isinstance(payload.get("transit_cost"), Mapping) else {}
    official_status = str(official.get("status") or "")
    transit_status = str(transit.get("status") or "")
    if official_status == "success":
        changed = int(official.get("changed_rows") or 0)
        accepted_changed = int(official.get("accepted_qty_changed_rows") or 0)
        return (
            "official success"
            + f", changed={changed}, accepted_qty_changed={accepted_changed}"
            + (f", transit_cost={transit_status}" if transit_status else "")
        )
    if official_status:
        return f"official {official_status}: {str(official.get('error') or '').strip()}"
    return str(payload.get("status") or "")


_ARCHIVED_LEGACY_AUTO_UPDATE_REASON = "legacy Google Sheets load: archived / not executed"


def _sanitize_auto_update_reason(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = [
        part.strip()
        for part in text.split("|")
        if part.strip() and part.strip() != _ARCHIVED_LEGACY_AUTO_UPDATE_REASON
    ]
    return " | ".join(parts)


def _sanitize_auto_result_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload or {})
    if "semantic_reason" in result:
        result["semantic_reason"] = _sanitize_auto_update_reason(result.get("semantic_reason"))
    return result


def _sanitize_auto_schedule_row(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("last_error", "last_error_summary", "last_result_summary"):
        if key in row:
            row[key] = _sanitize_auto_update_reason(row.get(key))
    return row


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


def _is_scheduled_auto_refresh_trigger(value: str) -> bool:
    return str(value or "scheduled").strip().lower() == "scheduled"


def _parse_job_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _active_job_staleness_payload(
    active_job: Mapping[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    started_at = str(active_job.get("started_at") or "")
    started = _parse_job_timestamp(started_at)
    current = _parse_job_timestamp(now)
    age_seconds = None
    stale = False
    if started is not None and current is not None:
        age_seconds = max(0, int((current - started).total_seconds()))
        stale = age_seconds >= SHEET_VITRINA_ACTIVE_JOB_STALE_AFTER_SECONDS
    return {
        "active_job_started_at": started_at,
        "active_job_age_seconds": age_seconds,
        "active_job_stale_after_seconds": SHEET_VITRINA_ACTIVE_JOB_STALE_AFTER_SECONDS,
        "active_job_stale": stale,
    }


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


class SheetVitrinaV1OperatorJobError(RuntimeError):
    def __init__(self, message: str, *, result_payload: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result_payload = dict(result_payload or {})


class SheetVitrinaV1OperatorJobStore:
    def __init__(self, timestamp_factory: Callable[[], str]) -> None:
        self.timestamp_factory = timestamp_factory
        self._jobs: dict[str, SheetVitrinaV1OperatorJob] = {}
        self._threads: dict[str, threading.Thread] = {}
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
        thread = threading.Thread(
            target=self._run,
            args=(job_id, runner),
            daemon=True,
        )
        with self._lock:
            self._jobs[job_id] = job
            self._threads[job_id] = thread
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

    def active_job(self, *, operations: tuple[str, ...]) -> dict[str, Any] | None:
        normalized_operations = {str(value).strip() for value in operations if str(value).strip()}
        with self._lock:
            self._reap_stopped_running_threads_unlocked()
            jobs = list(self._jobs.values())
        candidates = [
            job
            for job in jobs
            if job.status == "running"
            and (not normalized_operations or job.operation in normalized_operations)
        ]
        if not candidates:
            return None
        selected = max(
            enumerate(candidates),
            key=lambda item: (
                str(item[1].started_at or ""),
                item[0],
            ),
        )[1]
        return selected.snapshot()

    def _reap_stopped_running_threads_unlocked(self) -> None:
        for job_id, job in list(self._jobs.items()):
            if job.status != "running":
                continue
            thread = self._threads.get(job_id)
            if thread is None or thread.ident is None or thread.is_alive():
                continue
            job.status = "error"
            job.finished_at = self.timestamp_factory()
            job.error = "operator job thread stopped without terminal state"
            job.log_lines.append(f"{job.finished_at} Ошибка: {job.error}")

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
                if isinstance(exc, SheetVitrinaV1OperatorJobError) and exc.result_payload:
                    job.result = dict(exc.result_payload)
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


def _build_supplier_order_documents_checklist(
    *,
    shipment: Mapping[str, Any],
    financial_documents: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.append(_supplier_order_invoice_document_row(shipment))
    rows.append(_supplier_order_contract_document_row(shipment))
    financial_by_type: dict[str, list[Mapping[str, Any]]] = {}
    for document in financial_documents:
        document_type = str(document.get("document_type") or "").strip()
        if document_type:
            financial_by_type.setdefault(document_type, []).append(document)
    for document_type in SUPPLIER_ORDER_REQUIRED_DOCUMENT_TYPES:
        if document_type in {TRADE_DOCUMENT_TYPE_INVOICE, TRADE_DOCUMENT_TYPE_CONTRACT}:
            continue
        documents = financial_by_type.get(document_type) or []
        if documents:
            rows.extend(_supplier_order_financial_document_row(item) for item in documents)
        else:
            rows.append(_supplier_order_missing_document_row(document_type))
    known_types = set(SUPPLIER_ORDER_REQUIRED_DOCUMENT_TYPES)
    for document in financial_documents:
        document_type = str(document.get("document_type") or "").strip()
        if document_type and document_type not in known_types:
            rows.append(_supplier_order_financial_document_row(document, required=False))
    return rows


def _supplier_order_invoice_document_row(shipment: Mapping[str, Any]) -> dict[str, Any]:
    is_uploaded = bool(shipment.get("invoice_download_path") or shipment.get("invoice_document_id"))
    metadata = shipment.get("metadata") if isinstance(shipment.get("metadata"), Mapping) else {}
    return {
        **_supplier_order_base_document_row(TRADE_DOCUMENT_TYPE_INVOICE, required=True, is_uploaded=is_uploaded),
        "source": "trade_document",
        "document_id": str(shipment.get("invoice_document_id") or ""),
        "document_number": str(shipment.get("invoice_no") or metadata.get("invoice_no") or ""),
        "document_date": str(shipment.get("invoice_date") or metadata.get("invoice_date") or ""),
        "counterparty": str(shipment.get("supplier_name") or metadata.get("supplier_name") or ""),
        "amount": shipment.get("invoice_amount_total") if shipment.get("invoice_amount_total") is not None else metadata.get("declared_invoice_total"),
        "currency": str(shipment.get("currency") or metadata.get("currency") or ""),
        "download_path": str(shipment.get("invoice_download_path") or ""),
    }


def _supplier_order_contract_document_row(shipment: Mapping[str, Any]) -> dict[str, Any]:
    is_uploaded = bool(shipment.get("contract_download_path") or shipment.get("contract_document_id"))
    metadata = shipment.get("metadata") if isinstance(shipment.get("metadata"), Mapping) else {}
    return {
        **_supplier_order_base_document_row(TRADE_DOCUMENT_TYPE_CONTRACT, required=True, is_uploaded=is_uploaded),
        "source": "trade_document",
        "document_id": str(shipment.get("contract_document_id") or ""),
        "document_number": str(shipment.get("contract_no") or metadata.get("contract_no") or ""),
        "document_date": str(shipment.get("contract_date") or metadata.get("contract_date") or ""),
        "counterparty": str(shipment.get("supplier_name") or metadata.get("supplier_name") or ""),
        "amount": None,
        "currency": "",
        "download_path": str(shipment.get("contract_download_path") or ""),
    }


def _supplier_order_financial_document_row(
    document: Mapping[str, Any],
    *,
    required: bool = True,
) -> dict[str, Any]:
    document_type = str(document.get("document_type") or "").strip()
    parse_status = str(document.get("parse_status") or "")
    normalized = dict(document.get("normalized_parse") or {})
    order_match_warnings = _string_list(document.get("order_match_warnings") or normalized.get("order_match_warnings"))
    warnings = _dedupe_strings([*_string_list(document.get("warnings")), *order_match_warnings])
    row = _supplier_order_base_document_row(
        document_type,
        required=required,
        is_uploaded=True,
        parse_status=parse_status,
    )
    amount = document.get("total_amount_rub") if document.get("total_amount_rub") is not None else document.get("total_amount")
    row.update(
        {
            "source": "financial_document",
            "document_id": str(document.get("document_id") or ""),
            "document_number": str(document.get("document_number") or ""),
            "document_date": str(document.get("document_date") or ""),
            "counterparty": str(document.get("vendor") or ""),
            "amount": amount,
            "currency": "RUB" if document.get("total_amount_rub") is not None else str(document.get("currency") or ""),
            "download_path": str(document.get("download_path") or ""),
            "parse_status": parse_status,
            "warnings": warnings,
            "errors": list(document.get("errors") or []),
            "normalized_parse": normalized,
            "order_match_status": str(document.get("order_match_status") or normalized.get("order_match_status") or ""),
            "order_match_reasons": _string_list(document.get("order_match_reasons") or normalized.get("order_match_reasons")),
            "order_match_warnings": order_match_warnings,
            "matched_contract_number": str(document.get("matched_contract_number") or normalized.get("matched_contract_number") or ""),
            "matched_contract_date": str(document.get("matched_contract_date") or normalized.get("matched_contract_date") or ""),
        }
    )
    if row["order_match_status"] in {"needs_review", "mismatch"} and row["status"] == "uploaded":
        row["status"] = "needs_review"
        row["status_label"] = _supplier_order_document_status_label(row["status"])
    if document_type == FINANCIAL_DOCUMENT_TYPE_BANK_FEE_STATEMENT:
        statement_import = dict(normalized.get("statement_import") or {})
        if str(statement_import.get("import_status") or "") != "confirmed":
            row["status"] = "needs_review"
            row["status_label"] = _supplier_order_document_status_label(row["status"])
    return row


def _supplier_order_missing_document_row(document_type: str) -> dict[str, Any]:
    return _supplier_order_base_document_row(document_type, required=True, is_uploaded=False)


def _supplier_order_base_document_row(
    document_type: str,
    *,
    required: bool,
    is_uploaded: bool,
    parse_status: str = "",
) -> dict[str, Any]:
    status = _supplier_order_document_status(is_uploaded=is_uploaded, parse_status=parse_status)
    return {
        "document_type": document_type,
        "document_name": SUPPLIER_ORDER_DOCUMENT_LABELS_RU.get(document_type, document_type or "Документ"),
        "required": bool(required),
        "is_uploaded": bool(is_uploaded),
        "status": status,
        "status_label": _supplier_order_document_status_label(status),
        "source": "",
        "document_id": "",
        "document_number": "",
        "document_date": "",
        "counterparty": "",
        "amount": None,
        "currency": "",
        "download_path": "",
        "parse_status": parse_status,
        "warnings": [],
        "errors": [],
    }


def _supplier_order_document_status(*, is_uploaded: bool, parse_status: str = "") -> str:
    if not is_uploaded:
        return "missing"
    if parse_status == FINANCIAL_DOCUMENT_PARSE_STATUS_PARSE_ERROR:
        return "error"
    if parse_status in {FINANCIAL_DOCUMENT_PARSE_STATUS_NEEDS_REVIEW, FINANCIAL_DOCUMENT_PARSE_STATUS_EXCLUDED}:
        return "needs_review"
    return "uploaded"


def _supplier_order_document_status_label(status: str) -> str:
    return {
        "uploaded": "Загружен",
        "missing": "Не загружен",
        "needs_review": "Проверить",
        "error": "Ошибка",
    }.get(status, status or "-")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _supplier_order_documents_archive_path(shipment_id: str, filename: str) -> str:
    return f"/v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/documents/{filename}"


def _build_supplier_order_documents_archive(
    payload: Mapping[str, Any],
    *,
    package_type: str,
    file_loader: Callable[[Mapping[str, Any]], tuple[bytes, str, str]],
) -> bytes:
    required_types = (
        SUPPLIER_ORDER_LOGISTICS_PACKAGE_DOCUMENT_TYPES
        if package_type == "logistics"
        else SUPPLIER_ORDER_REQUIRED_DOCUMENT_TYPES
    )
    rows = [
        dict(item)
        for item in payload.get("required_documents") or []
        if isinstance(item, Mapping) and str(item.get("document_type") or "") in required_types
    ]
    included: list[dict[str, Any]] = []
    warnings: list[str] = []
    missing_required_types = [
        document_type
        for document_type in required_types
        if not any(str(item.get("document_type") or "") == document_type and bool(item.get("is_uploaded")) for item in rows)
    ]
    if missing_required_types:
        warnings.append(
            "Missing required document(s): "
            + ", ".join(SUPPLIER_ORDER_DOCUMENT_LABELS_RU.get(item, item) for item in missing_required_types)
        )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        used_names: set[str] = set()
        for row in rows:
            if not bool(row.get("is_uploaded")):
                continue
            try:
                file_bytes, filename, content_type = file_loader(row)
            except ValueError as exc:
                warnings.append(f"{row.get('document_name') or row.get('document_type')}: {exc}")
                continue
            archive_name = _unique_archive_name(
                used_names,
                _archive_document_filename(row, filename),
            )
            archive.writestr(archive_name, file_bytes)
            included.append(
                {
                    "archive_name": archive_name,
                    "document_type": row.get("document_type") or "",
                    "document_name": row.get("document_name") or "",
                    "document_id": row.get("document_id") or "",
                    "source_filename": filename,
                    "content_type": content_type,
                    "status": row.get("status") or "",
                    "order_match_status": row.get("order_match_status") or "",
                    "warnings": _string_list(row.get("warnings")),
                }
            )
            for warning in _string_list(row.get("warnings")):
                warnings.append(f"{row.get('document_name') or row.get('document_type')}: {warning}")
        manifest = {
            "contract_name": "sheet_vitrina_v1_supplier_order_documents_package_manifest",
            "status": "ok",
            "package_type": package_type,
            "supplier_order_id": payload.get("supplier_order_id") or "",
            "required_document_types": list(required_types),
            "missing_required_types": missing_required_types,
            "included": included,
            "warnings": warnings,
            "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        }
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buffer.getvalue()


def _archive_document_filename(row: Mapping[str, Any], source_filename: str) -> str:
    document_type = _safe_archive_part(str(row.get("document_type") or "document"))
    number = _safe_archive_part(str(row.get("document_number") or row.get("document_id") or ""))
    original = _safe_archive_filename(source_filename or "document.bin")
    prefix = document_type + (f"_{number}" if number else "")
    return f"{prefix}__{original}"


def _unique_archive_name(used_names: set[str], filename: str) -> str:
    candidate = filename
    index = 2
    while candidate in used_names:
        path = Path(filename)
        candidate = f"{path.stem}-{index}{path.suffix}"
        index += 1
    used_names.add(candidate)
    return candidate


def _safe_archive_filename(value: str) -> str:
    name = str(value or "").strip() or "documents.zip"
    safe = re.sub(r"[^A-Za-z0-9А-Яа-яЁё._()-]+", "_", name)
    safe = re.sub(r"_+", "_", safe).strip("._")
    return safe or "documents.zip"


def _safe_archive_part(value: str) -> str:
    safe = _safe_archive_filename(value)
    return safe[:80] if len(safe) > 80 else safe


def _run_promo_artifact_light_gc_after_refresh(
    *,
    runtime_dir: Path,
    refresh_diagnostics: dict[str, Any],
    runner: PromoArtifactGcRunner,
    emit: OperatorLogEmitter,
) -> dict[str, Any]:
    protected_run_dirs = _promo_artifact_gc_protected_run_dirs(refresh_diagnostics)
    try:
        summary = runner(
            runtime_dir=runtime_dir,
            current_run_dirs=protected_run_dirs,
        )
        if not isinstance(summary, Mapping):
            summary = {
                "policy_name": "promo_refresh_light_gc_v1",
                "status": "warning",
                "warning": "gc_runner_returned_non_mapping_summary",
                "deleted_count": 0,
                "freed_bytes": 0,
                "skipped_count": 0,
                "skip_reasons": {},
                "duration_ms": None,
            }
    except Exception as exc:
        summary = {
            "policy_name": "promo_refresh_light_gc_v1",
            "status": "warning",
            "warning": f"{type(exc).__name__}: {exc}",
            "deleted_count": 0,
            "freed_bytes": 0,
            "skipped_count": 0,
            "skip_reasons": {},
            "duration_ms": None,
        }
    normalized = dict(summary)
    normalized.setdefault("policy_name", "promo_refresh_light_gc_v1")
    normalized.setdefault("status", "warning")
    normalized.setdefault("warning", "")
    normalized.setdefault("deleted_count", 0)
    normalized.setdefault("freed_bytes", 0)
    normalized.setdefault("skipped_count", 0)
    normalized.setdefault("skip_reasons", {})
    normalized["protected_run_dirs"] = protected_run_dirs
    refresh_diagnostics["promo_artifact_gc"] = normalized
    emit(
        _format_log_event(
            "promo_artifact_gc_finish",
            cycle="refresh",
            policy_name=normalized.get("policy_name"),
            status=normalized.get("status"),
            warning=normalized.get("warning"),
            deleted_count=normalized.get("deleted_count"),
            freed_bytes=normalized.get("freed_bytes"),
            skipped_count=normalized.get("skipped_count"),
            skip_reasons=json.dumps(normalized.get("skip_reasons") or {}, ensure_ascii=False, sort_keys=True),
            duration_ms=normalized.get("duration_ms"),
        )
    )
    return normalized


def _promo_artifact_gc_protected_run_dirs(refresh_diagnostics: Mapping[str, Any]) -> list[str]:
    protected: list[str] = []
    source_slots = refresh_diagnostics.get("source_slots")
    if not isinstance(source_slots, list):
        return protected
    for slot in source_slots:
        if not isinstance(slot, Mapping):
            continue
        promo_diagnostics = slot.get("promo_diagnostics")
        if not isinstance(promo_diagnostics, Mapping):
            continue
        context = promo_diagnostics.get("context")
        if not isinstance(context, Mapping):
            continue
        for key in ("current_run_dir", "collector_run_dir"):
            value = str(context.get(key) or "").strip()
            if value and value not in protected:
                protected.append(value)
    return protected


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
        return str(runtime.load_sheet_vitrina_ready_snapshot_any_bundle(as_of_date=snapshot_as_of_date).snapshot_id)
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
    partial_cell_statuses = _updated_cell_statuses_by_source_and_date(partial_plan)
    onec_missing_bucket_metric_keys: set[str] = set()
    if source_group_id == ONEC_STOCKS_SOURCE_GROUP_ID and selected_date:
        onec_missing_bucket_metric_keys = _onec_missing_stage_metric_keys_from_status_rows(
            [
                list(row)
                for row in partial_status.rows
                if _status_row_source_base(row) == ONEC_STOCKS_SOURCE_KEY
                and (
                    not selected_temporal_slots
                    or _status_row_temporal_slot(row) in selected_temporal_slots
                )
            ]
        )
    merged_row_ids: set[str] = set()
    merged_data_rows: list[list[Any]] = []
    rows_updated = 0
    rows_preserved = 0
    for row in previous_data.rows:
        row_id = _row_id(row)
        if row_id in updated_row_ids:
            metric_key = _metric_key_from_row_id(row_id)
            source_key = _source_key_for_metric_key(metric_key)
            if selected_date and not _source_date_allows_cell_merge(
                partial_cell_statuses,
                source_key=source_key,
                as_of_date=selected_date,
            ):
                merged_data_rows.append(list(row))
                rows_preserved += 1
                continue
            if selected_date:
                merged_row = _merge_row_selected_date(
                    previous_row=list(row),
                    partial_row=partial_rows_by_id[row_id],
                    previous_indexes=previous_date_indexes,
                    partial_indexes=partial_date_indexes,
                )
                if metric_key in onec_missing_bucket_metric_keys:
                    merged_row = _preserve_selected_date_values_from_previous_when_partial_blank(
                        previous_row=list(row),
                        merged_row=merged_row,
                        previous_indexes=previous_date_indexes,
                    )
                merged_data_rows.append(merged_row)
            else:
                merged_data_rows.append(partial_rows_by_id[row_id])
            merged_row_ids.add(row_id)
            rows_updated += 1
        else:
            merged_data_rows.append(list(row))
            rows_preserved += 1
    existing_row_ids = {_row_id(row) for row in previous_data.rows if _row_id(row)}
    for row_id in sorted(updated_row_ids - existing_row_ids):
        metric_key = _metric_key_from_row_id(row_id)
        source_key = _source_key_for_metric_key(metric_key)
        if selected_date and not _source_date_allows_cell_merge(
            partial_cell_statuses,
            source_key=source_key,
            as_of_date=selected_date,
        ):
            rows_preserved += 1
            continue
        merged_data_rows.append(partial_rows_by_id[row_id])
        merged_row_ids.add(row_id)
        rows_updated += 1
    if source_group_id == "other_sources" and selected_date:
        _recompute_other_sources_derived_rows(
            rows=merged_data_rows,
            header=previous_data.header,
            selected_dates=[selected_date],
            updated_row_ids=merged_row_ids,
        )
    if source_group_id == ONEC_STOCKS_SOURCE_GROUP_ID and selected_date:
        _recompute_onec_derived_rows(
            rows=merged_data_rows,
            header=previous_data.header,
            selected_dates=[selected_date],
            updated_row_ids=merged_row_ids,
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
    for row_id in merged_row_ids:
        row_updated_at[row_id] = refreshed_at
    group_updated_at = _source_group_updated_at_metadata(
        metadata=previous_metadata,
        fallback_updated_at=previous_refreshed_at,
    )
    if merged_row_ids:
        group_updated_at[source_group_id] = refreshed_at
    updated_cells = (
        _updated_cells_for_plan(
            replace(
                previous_plan,
                sheets=merged_sheets,
            ),
            row_ids=merged_row_ids,
            date_columns=[selected_date] if selected_date else list(previous_plan.date_columns),
        )
        if merged_row_ids
        else []
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
        "source_status_counts": _source_status_kind_counts(selected_status_rows),
        "source_group_id": source_group_id,
        "source_keys": sorted(source_key_set),
        "metric_keys": sorted(metric_key_set),
        "selected_as_of_date": selected_date,
        "updated_dates": [selected_date] if selected_date else list(previous_plan.date_columns),
        "updated_row_ids": sorted(merged_row_ids),
        "updated_cells": updated_cells,
        "updated_cell_count": _count_updated_cells_by_status(updated_cells, "updated"),
        "latest_confirmed_cell_count": _count_updated_cells_by_status(updated_cells, "latest_confirmed"),
    }


def _source_group_refresh_semantic_payload(merge_summary: Mapping[str, Any]) -> dict[str, str]:
    updated_cells = _int_from_mapping(merge_summary, "updated_cell_count")
    latest_confirmed_cells = _int_from_mapping(merge_summary, "latest_confirmed_cell_count")
    source_status_counts = merge_summary.get("source_status_counts")
    counts = dict(source_status_counts) if isinstance(source_status_counts, Mapping) else {}
    blocking_count = sum(
        _int_from_any(counts.get(status))
        for status in ("error", "missing", "not_found", "blocked", "not_available")
    )
    warning_count = sum(
        _int_from_any(counts.get(status))
        for status in ("warning", "incomplete")
    )
    confirmed_cells = updated_cells + latest_confirmed_cells
    if blocking_count:
        semantic_status = "error"
        semantic_reason = "Группа не подтвердила данные: источник вернул blocker/error."
    elif confirmed_cells <= 0:
        semantic_status = "warning"
        semantic_reason = "Группа завершилась без подтверждённых ячеек для выбранной даты."
    elif warning_count:
        semantic_status = "warning"
        semantic_reason = (
            f"Группа обновлена частично: обновлено {updated_cells}, "
            f"подтверждено без изменений {latest_confirmed_cells}."
        )
    else:
        semantic_status = "success"
        semantic_reason = (
            f"Группа обновлена: обновлено {updated_cells}, "
            f"подтверждено без изменений {latest_confirmed_cells}."
        )
    return {
        "semantic_status": semantic_status,
        "semantic_label": _semantic_status_label(semantic_status),
        "semantic_tone": semantic_status,
        "semantic_reason": semantic_reason,
    }


def _source_status_kind_counts(rows: Iterable[list[Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        kind = str(row[1] if len(row) > 1 else "").strip().lower()
        if not kind:
            continue
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _int_from_mapping(payload: Mapping[str, Any], key: str) -> int:
    return _int_from_any(payload.get(key))


def _int_from_any(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _with_full_refresh_metadata(
    plan: SheetVitrinaV1Envelope,
    *,
    refreshed_at: str,
    previous_plan: SheetVitrinaV1Envelope | None = None,
    previous_refreshed_at: str = "",
) -> SheetVitrinaV1Envelope:
    preservation_summary: dict[str, Any] | None = None
    if previous_plan is not None:
        plan, preservation_summary = _preserve_unconfirmed_source_cells_from_previous_plan(
            plan=plan,
            previous_plan=previous_plan,
        )
    data_sheet = _find_sheet(plan, "DATA_VITRINA")
    previous_metadata = dict(getattr(previous_plan, "metadata", {}) or {}) if previous_plan is not None else {}
    if previous_plan is None:
        row_updated_at = {
            _row_id(row): refreshed_at
            for row in (data_sheet.rows if data_sheet is not None else [])
            if _row_id(row)
        }
        group_updated_at = {
            group_id: refreshed_at
            for group_id in WEB_VITRINA_SOURCE_GROUP_ORDER
        }
    else:
        previous_timestamp = previous_refreshed_at or refreshed_at
        row_updated_at = _row_updated_at_metadata(
            previous_plan,
            metadata=previous_metadata,
            fallback_updated_at=previous_timestamp,
        )
        updated_cells = _updated_cells_for_plan(plan)
        updated_row_ids = {
            str(item.get("row_id") or "")
            for item in updated_cells
            if str(item.get("row_id") or "")
        }
        for row in (data_sheet.rows if data_sheet is not None else []):
            row_id = _row_id(row)
            if not row_id:
                continue
            row_updated_at.setdefault(row_id, previous_timestamp)
            if row_id in updated_row_ids:
                row_updated_at[row_id] = refreshed_at
        group_updated_at = _source_group_updated_at_metadata(
            metadata=previous_metadata,
            fallback_updated_at=previous_timestamp,
        )
        updated_group_ids = {
            str(item.get("source_group_id") or "")
            for item in updated_cells
            if str(item.get("source_group_id") or "")
        }
        for group_id in updated_group_ids:
            group_updated_at[group_id] = refreshed_at
    metadata = {
        **dict(getattr(plan, "metadata", {}) or {}),
        "row_last_updated_at_by_row_id": row_updated_at,
        "source_group_last_updated_at": group_updated_at,
    }
    if preservation_summary and preservation_summary.get("preserved_cell_count"):
        metadata["last_full_refresh_preservation"] = preservation_summary
    return replace(plan, metadata=metadata)


def _load_existing_ready_snapshot_for_preservation(
    runtime: Any,
    *,
    as_of_date: str,
) -> tuple[SheetVitrinaV1Envelope | None, str]:
    try:
        previous_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date)
        previous_status = runtime.load_sheet_vitrina_refresh_status(as_of_date=as_of_date)
    except ValueError:
        return None, ""
    return previous_plan, str(getattr(previous_status, "refreshed_at", "") or "")


def _preserve_unconfirmed_source_cells_from_previous_plan(
    *,
    plan: SheetVitrinaV1Envelope,
    previous_plan: SheetVitrinaV1Envelope,
) -> tuple[SheetVitrinaV1Envelope, dict[str, Any]]:
    data_sheet = _find_sheet(plan, "DATA_VITRINA")
    previous_data = _find_sheet(previous_plan, "DATA_VITRINA")
    if data_sheet is None or previous_data is None:
        return plan, {"preserved_cell_count": 0, "preserved_row_count": 0}

    previous_rows_by_id = {
        _row_id(row): list(row)
        for row in previous_data.rows
        if _row_id(row)
    }
    if not previous_rows_by_id:
        return plan, {"preserved_cell_count": 0, "preserved_row_count": 0}

    status_by_source_date = _updated_cell_statuses_by_source_and_date(plan)
    plan_indexes_by_date = {
        as_of_date: _sheet_header_indexes(data_sheet.header, as_of_date)
        for as_of_date in plan.date_columns
    }
    previous_indexes_by_date = {
        as_of_date: _sheet_header_indexes(previous_data.header, as_of_date)
        for as_of_date in plan.date_columns
    }

    preserved_cell_count = 0
    preserved_row_ids: set[str] = set()
    merged_rows: list[list[Any]] = []
    for row in data_sheet.rows:
        row_id = _row_id(row)
        previous_row = previous_rows_by_id.get(row_id)
        source_key = _source_key_for_metric_key(_metric_key_from_row_id(row_id))
        if not row_id or previous_row is None or not source_key:
            merged_rows.append(list(row))
            continue

        merged_row = list(row)
        for as_of_date, current_indexes in plan_indexes_by_date.items():
            if _source_date_allows_cell_merge(
                status_by_source_date,
                source_key=source_key,
                as_of_date=as_of_date,
            ):
                continue
            previous_indexes = previous_indexes_by_date.get(as_of_date) or []
            if not current_indexes or not previous_indexes:
                continue
            fallback_previous_index = previous_indexes[0]
            for current_index in current_indexes:
                previous_index = (
                    current_index
                    if current_index in previous_indexes
                    else fallback_previous_index
                )
                if previous_index >= len(previous_row) or _is_blank_sheet_value(previous_row[previous_index]):
                    continue
                while current_index >= len(merged_row):
                    merged_row.append("")
                if merged_row[current_index] != previous_row[previous_index]:
                    merged_row[current_index] = previous_row[previous_index]
                preserved_cell_count += 1
                preserved_row_ids.add(row_id)
        merged_rows.append(merged_row)

    if not preserved_cell_count:
        return plan, {"preserved_cell_count": 0, "preserved_row_count": 0}

    merged_sheets = [
        replace(
            sheet,
            rows=merged_rows,
            row_count=len(merged_rows),
            column_count=len(sheet.header),
        )
        if sheet.sheet_name == "DATA_VITRINA"
        else sheet
        for sheet in plan.sheets
    ]
    return replace(plan, sheets=merged_sheets), {
        "preserved_cell_count": preserved_cell_count,
        "preserved_row_count": len(preserved_row_ids),
        "preserved_row_ids": sorted(preserved_row_ids),
    }


def _source_date_allows_cell_merge(
    status_by_source_date: Mapping[tuple[str, str], str],
    *,
    source_key: str,
    as_of_date: str,
) -> bool:
    if not source_key:
        return True
    key = (source_key, as_of_date)
    if key not in status_by_source_date:
        return True
    return status_by_source_date.get(key) in {"updated", "latest_confirmed"}


def _is_blank_sheet_value(value: Any) -> bool:
    return value is None or str(value).strip() == ""


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


def _web_vitrina_contract_response_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep small contract root fields before heavy rows for bounded live probes."""

    ordered: dict[str, Any] = {}
    for key in (
        "contract_name",
        "contract_version",
        "page_route",
        "read_route",
        "meta",
        "status_summary",
        "schema",
        "capabilities",
        "rows",
    ):
        if key in payload:
            ordered[key] = payload[key]
    for key, value in payload.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def _with_page_composition_diagnostics(
    payload: Mapping[str, Any],
    *,
    started_perf: float,
    include_source_status: bool,
    include_table_data: bool,
) -> dict[str, Any]:
    normalized = dict(payload)
    meta = dict(normalized.get("meta") or {})
    table_surface = dict(normalized.get("table_surface") or {})
    rows = list(table_surface.get("rows") or [])
    columns = list(table_surface.get("columns") or [])
    total_row_count = int(table_surface.get("total_row_count") or len(rows))
    diagnostics = {
        "page_composition_build_ms": max(0, int(round((time.perf_counter() - started_perf) * 1000))),
        "payload_bytes": 0,
        "include_source_status": bool(include_source_status),
        "include_table_data": bool(include_table_data),
        "row_count": total_row_count,
        "returned_row_count": len(rows),
        "cell_count": _page_composition_cell_count(rows=rows, columns=columns),
    }
    meta["page_composition_diagnostics"] = diagnostics
    normalized["meta"] = meta
    for _ in range(2):
        diagnostics["payload_bytes"] = len(
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
        key: _updated_cell_status_for_status_rows(rows)
        for key, rows in grouped_rows.items()
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
    covered_count = _status_row_covered_count(row)
    note = str(row[10] if len(row) > 10 else "").strip().lower()
    if kind in {"error", "missing", "not_found", "blocked", "not_available"}:
        return ""
    if _status_note_is_unverified_closed_day_fallback(note):
        return ""
    if _status_note_is_latest_confirmed(note):
        return "latest_confirmed"
    if kind == "incomplete" and "accepted_fallback_stage_buckets=" in note:
        return "latest_confirmed"
    if kind == "warning":
        return "latest_confirmed"
    if kind == "incomplete" and covered_count > 0:
        return "updated"
    if kind == "success":
        return "updated"
    return ""


def _status_row_covered_count(row: list[Any]) -> int:
    if len(row) <= 8:
        return 0
    try:
        return int(row[8])
    except (TypeError, ValueError):
        return 0


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


def _preserve_selected_date_values_from_previous_when_partial_blank(
    *,
    previous_row: list[Any],
    merged_row: list[Any],
    previous_indexes: list[int],
) -> list[Any]:
    merged = list(merged_row)
    for index in previous_indexes:
        previous_value = previous_row[index] if index < len(previous_row) else ""
        if _is_blank_sheet_value(previous_value):
            continue
        current_value = merged[index] if index < len(merged) else ""
        if not _is_blank_sheet_value(current_value):
            continue
        while index >= len(merged):
            merged.append("")
        merged[index] = previous_value
    return merged


def _onec_missing_stage_metric_keys_from_status_rows(rows: Iterable[list[Any]]) -> set[str]:
    missing_buckets: set[str] = set()
    for row in rows:
        note = str(row[10] if len(row) > 10 else "")
        missing_buckets.update(_note_csv_values(note, "missing_stage_buckets"))
    result: set[str] = set()
    for stage_key in missing_buckets:
        for field in ("qty", "unit_cost_rub", "cost_total_rub"):
            result.add(onec_stage_metric_key(stage_key, field))
            result.add(onec_stage_total_metric_key(stage_key, field))
    return result


def _note_csv_values(note: str, key: str) -> list[str]:
    prefix = f"{key}="
    for part in str(note or "").split(";"):
        text = part.strip()
        if not text.startswith(prefix):
            continue
        value = text[len(prefix):].strip()
        return sorted({item.strip() for item in value.split(",") if item.strip()})
    return []


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


def _recompute_onec_derived_rows(
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
            if row_id not in updated_row_ids:
                continue
            metric_key = _metric_key_from_row_id(row_id)
            scope = _row_scope_from_row_id(row_id)
            if metric_key == ONEC_STOCKS_SKU_TOTAL_QTY_METRIC_KEY:
                value = _sum_scope_metric_values(
                    row_by_id,
                    scope=scope,
                    metric_keys=[
                        onec_stage_metric_key(stage_key, "qty")
                        for stage_key in ONEC_STOCKS_STAGE_KEYS
                    ],
                    date_index=date_index,
                )
                if value is not None:
                    _set_row_value(row, date_index, _to_sheet_cell_number(value))
            elif metric_key == ONEC_STOCKS_SKU_TOTAL_COST_RUB_METRIC_KEY:
                value = _sum_scope_metric_values(
                    row_by_id,
                    scope=scope,
                    metric_keys=[
                        onec_stage_metric_key(stage_key, "cost_total_rub")
                        for stage_key in ONEC_STOCKS_STAGE_KEYS
                    ],
                    date_index=date_index,
                )
                if value is not None:
                    _set_row_value(row, date_index, _to_sheet_cell_number(value))
            elif metric_key == ONEC_STOCKS_TOTAL_QTY_METRIC_KEY:
                value = _sum_sku_metric_values(
                    row_by_id,
                    metric_key=ONEC_STOCKS_SKU_TOTAL_QTY_METRIC_KEY,
                    date_index=date_index,
                )
                if value is not None:
                    _set_row_value(row, date_index, _to_sheet_cell_number(value))
            elif metric_key == ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY:
                value = _sum_sku_metric_values(
                    row_by_id,
                    metric_key=ONEC_STOCKS_SKU_TOTAL_COST_RUB_METRIC_KEY,
                    date_index=date_index,
                )
                if value is not None:
                    _set_row_value(row, date_index, _to_sheet_cell_number(value))
        for row_id, row in sorted(row_by_id.items()):
            if (
                row_id not in updated_row_ids
                or _metric_key_from_row_id(row_id) != ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY
            ):
                continue
            scope = _row_scope_from_row_id(row_id)
            value = _compute_proxy_profit_2_for_scope(row_by_id, scope=scope, date_index=date_index)
            _set_row_value(row, date_index, _to_sheet_cell_number(value))
        for row_id, row in sorted(row_by_id.items()):
            if (
                row_id not in updated_row_ids
                or _metric_key_from_row_id(row_id) != ONEC_TOTAL_PROXY_PROFIT_2_RUB_METRIC_KEY
            ):
                continue
            value = _sum_sku_metric_values(
                row_by_id,
                metric_key=ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY,
                date_index=date_index,
            )
            _set_row_value(row, date_index, _to_sheet_cell_number(value))
        for row_id, row in sorted(row_by_id.items()):
            metric_key = _metric_key_from_row_id(row_id)
            if row_id not in updated_row_ids or metric_key not in {
                ONEC_PROXY_MARGIN_2_PCT_METRIC_KEY,
                ONEC_PROXY_MARGIN_2_PCT_TOTAL_METRIC_KEY,
                ONEC_INVENTORY_CAPITAL_RETURN_PCT_METRIC_KEY,
                ONEC_INVENTORY_CAPITAL_RETURN_PCT_TOTAL_METRIC_KEY,
            }:
                continue
            scope = _row_scope_from_row_id(row_id)
            if metric_key == ONEC_PROXY_MARGIN_2_PCT_METRIC_KEY:
                numerator_metric = ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY
                denominator_metric = "orderSum"
            elif metric_key == ONEC_PROXY_MARGIN_2_PCT_TOTAL_METRIC_KEY:
                numerator_metric = ONEC_TOTAL_PROXY_PROFIT_2_RUB_METRIC_KEY
                denominator_metric = "total_orderSum"
            elif metric_key == ONEC_INVENTORY_CAPITAL_RETURN_PCT_METRIC_KEY:
                numerator_metric = ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY
                denominator_metric = ONEC_STOCKS_SKU_TOTAL_COST_RUB_METRIC_KEY
            else:
                numerator_metric = ONEC_TOTAL_PROXY_PROFIT_2_RUB_METRIC_KEY
                denominator_metric = ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY
            numerator = _row_metric_number(
                row_by_id,
                scope=scope,
                metric_key=numerator_metric,
                date_index=date_index,
            )
            denominator = _row_metric_number(
                row_by_id,
                scope=scope,
                metric_key=denominator_metric,
                date_index=date_index,
            )
            value = _divide_cell_numbers_or_zero(numerator, denominator)
            _set_row_value(row, date_index, _to_sheet_cell_number(value))


def _compute_proxy_profit_2_for_scope(
    row_by_id: Mapping[str, list[Any]],
    *,
    scope: str,
    date_index: int,
) -> float | None:
    order_sum = _row_metric_number(row_by_id, scope=scope, metric_key="orderSum", date_index=date_index)
    order_count = _row_metric_number(row_by_id, scope=scope, metric_key="orderCount", date_index=date_index)
    onec_wb_unit_cost = _row_metric_number(
        row_by_id,
        scope=scope,
        metric_key=ONEC_STOCKS_WB_UNIT_COST_RUB_METRIC_KEY,
        date_index=date_index,
    )
    ads_sum = _row_metric_number(row_by_id, scope=scope, metric_key="ads_sum", date_index=date_index)
    if None in {order_sum, order_count, onec_wb_unit_cost, ads_sum}:
        return None
    return float(order_sum) * 0.5096 - float(order_count) * 0.91 * float(onec_wb_unit_cost) - float(ads_sum)


def _divide_cell_numbers_or_zero(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    if float(denominator) == 0.0:
        return 0.0
    return float(numerator) / float(denominator)


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


def _sum_scope_metric_values(
    row_by_id: Mapping[str, list[Any]],
    *,
    scope: str,
    metric_keys: Iterable[str],
    date_index: int,
) -> float | None:
    values = [
        _row_metric_number(
            row_by_id,
            scope=scope,
            metric_key=metric_key,
            date_index=date_index,
        )
        for metric_key in metric_keys
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
        "Нажмите «Загрузить», чтобы подготовить данные."
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
    available_dates: Iterable[str] = (),
    default_refresh_date: str = "",
    metric_labels_by_source: Mapping[str, list[str]] | None = None,
    group_last_updated_at: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    current_business_date = current_business_date_iso()
    previous_business_date = default_business_as_of_date()
    detail = f"snapshot {snapshot_id} · as_of_date {snapshot_as_of_date} · {read_model}"
    upload_summary = _build_web_vitrina_endpoint_summary_block(
        title="Загрузка данных",
        subtitle="Статусы источников не загружены.",
        records={},
        ordered_source_keys=[],
        empty_message="Статусы источников не загружены. Нажмите «Загрузить», чтобы посмотреть детали.",
        block_updated_at=refreshed_at,
        block_detail=detail,
    )
    loading_table = {
        "title": "Загрузка данных",
        "subtitle": "Статусы источников не загружены.",
        "detail": detail,
        "updated_at": refreshed_at,
        "today_date": current_business_date,
        "yesterday_date": previous_business_date,
        "available_dates": _normalize_available_refresh_dates(
            available_dates,
            default_refresh_date=default_refresh_date,
        ),
        "default_refresh_date": default_refresh_date,
        "groups": [],
        "columns": [],
        "rows": [],
        "source_status_state": "not_loaded",
        "snapshot_as_of_date": snapshot_as_of_date,
        "snapshot_id": snapshot_id,
        "empty_message": "Статусы источников не загружены. Нажмите «Загрузить», чтобы посмотреть детали.",
    }
    return {
        "log_block": {
            "title": "Лог",
            "subtitle": "Лог не загружается вместе с первичным открытием страницы",
            "status_label": "Не загружено",
            "tone": "neutral",
            "detail": detail,
            "preview_lines": [],
            "line_count": 0,
            "download_path": "",
            "log_filename": "",
            "empty_message": "Нажмите «Загрузить» в блоке «Загрузка данных», чтобы прочитать source-status details и лог.",
        },
        "upload_summary": upload_summary,
        "loading_table": loading_table,
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


def _source_group_last_updated_at_for_runtime_snapshot(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    snapshot_as_of_date: str,
    fallback_updated_at: str,
) -> dict[str, str]:
    try:
        snapshot = runtime.load_sheet_vitrina_ready_snapshot_any_bundle(as_of_date=snapshot_as_of_date)
    except Exception:  # pragma: no cover - timestamp metadata is best-effort shell context
        return _source_group_updated_at_metadata(metadata={}, fallback_updated_at=fallback_updated_at)
    return _source_group_last_updated_at_for_snapshot(
        snapshot,
        fallback_updated_at=fallback_updated_at,
    )


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
    if _status_note_is_unverified_closed_day_fallback(note):
        return False
    if kind == "incomplete" and _status_note_has_accepted_stage_fallback(note):
        return True
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
        if _status_note_has_zero_stock_stage_bucket(note):
            return _humanize_note(note) or "1C source свежий; отсутствующий bucket трактуется как нулевой остаток"
        if _status_note_has_accepted_stage_fallback(note):
            return _humanize_note(note) or "использована ранее принятая server-side версия"
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
    seen = set(WEB_VITRINA_SOURCE_METRIC_KEYS) | set(upload_records) | set(update_records)
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
        if human_note:
            return human_note
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
    if "zero_stock_stage_buckets=" in normalized:
        buckets = _note_value(normalized, "zero_stock_stage_buckets")
        bucket_text = f": {buckets}" if buckets else ""
        return (
            f"1C source свежий: stage bucket{bucket_text} отсутствует по active SKU; "
            "трактуется как нулевой остаток"
        )
    if "missing_stage_buckets=" in normalized:
        missing = _note_value(normalized, "missing_stage_buckets")
        bucket_text = f": {missing}" if missing else ""
        fallback = _note_value(normalized, "accepted_fallback_stage_buckets")
        if fallback:
            return (
                f"1C не вернула stage bucket{bucket_text}; "
                "строки bucket заполнены из ранее принятой server-side версии"
            )
        return f"1C не вернула stage bucket{bucket_text}; строки bucket оставлены blank без fake zeros"
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
            "current-snapshot-only yesterday_closed requires a prior accepted current snapshot for requested date; endpoint has no historical date parameter, so current values are not backfilled into a closed-day column",
            "нет ранее принятого current snapshot для этой даты; текущие значения не подставлены в закрытый день",
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


def _note_value(note: str, key: str) -> str:
    prefix = f"{key}="
    for part in str(note or "").split(";"):
        text = part.strip()
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return ""


def _note_requires_warning(note: str) -> bool:
    normalized = str(note or "").strip()
    if not normalized:
        return False
    if _status_note_is_unverified_closed_day_fallback(normalized):
        return True
    preserved_closed_day_markers = {
        "accepted_closed_from_prior_current_snapshot",
        "accepted_closed_preserved_after_invalid_attempt",
        "accepted_current_preserved_after_invalid_attempt",
        "exact_date_provisional_runtime_cache",
    }
    if any(marker in normalized for marker in preserved_closed_day_markers):
        return True
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
    if _status_note_is_unverified_closed_day_fallback(normalized):
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


def _status_note_has_accepted_stage_fallback(note: str) -> bool:
    normalized = str(note or "").strip().lower()
    return "accepted_fallback_stage_buckets=" in normalized


def _status_note_has_zero_stock_stage_bucket(note: str) -> bool:
    normalized = str(note or "").strip().lower()
    return "zero_stock_stage_buckets=" in normalized


def _status_note_is_unverified_closed_day_fallback(note: str) -> bool:
    normalized = str(note or "").strip().lower()
    return "accepted_current_from_prior_closed_day_latest_confirmed" in normalized


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
    missing_public_buyer_price_count = _activity_reason_marker_value(
        lowered,
        marker="missing_public_buyer_price=",
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
    if missing_public_buyer_price_count:
        data_clause = f"публичная цена WB не получена для {missing_public_buyer_price_count} SKU"
    elif "missing_public_buyer_price" in lowered:
        data_clause = "публичная цена WB не получена для части SKU"
    elif no_data:
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


def _activity_reason_marker_value(text: str, *, marker: str) -> str:
    marker_index = text.find(marker)
    if marker_index < 0:
        return ""
    raw_value = text[marker_index + len(marker) :].split(";", 1)[0].split(" ", 1)[0].strip()
    return "".join(ch for ch in raw_value if ch.isdigit())


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
        "missing_public_buyer_price",
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
        "missing_public_buyer_price",
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


def _first_query_value(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, list):
            value = value[-1] if value else None
        if value not in ("", None):
            return value
    return default


def _coerce_query_bool(value: Any, *, default: bool) -> bool:
    if value in ("", None):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


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
