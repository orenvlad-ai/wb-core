"""Минимальный inbound HTTP entrypoint для registry upload и sheet_vitrina_v1 refresh/read split."""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
from datetime import date, datetime, timezone
from email.parser import BytesParser
from email.policy import default as default_email_policy
import hashlib
import hmac
import html
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import re
import socketserver
import time
from typing import Any, Mapping, Sequence
from urllib import parse as urllib_parse
from uuid import uuid4

from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_feedbacks_auto_complaints import (
    SheetVitrinaV1FeedbacksAutoComplaintsError,
)
from packages.application.sheet_vitrina_v1_feedbacks_ai import SheetVitrinaV1FeedbacksAiError
from packages.application.sheet_vitrina_v1_feedbacks_complaints import (
    SheetVitrinaV1FeedbacksComplaintsError,
)
from packages.application.sheet_vitrina_v1_feedbacks import (
    FEEDBACKS_EXPORT_CONTENT_TYPE,
    SheetVitrinaV1FeedbacksError,
)
from packages.application.sheet_vitrina_v1_ads import SheetVitrinaV1AdsError
from packages.application.wb_supplies import WbSuppliesBlockError
from packages.application.sheet_vitrina_v1_load_bridge import LegacyGoogleSheetsContourArchivedError
from packages.application.sheet_vitrina_v1_load_bridge import legacy_google_sheets_archive_context
from packages.application.demand_estimation import parse_sales_avg_period_days
from packages.contracts.factory_order_supply import (
    DATASET_INBOUND_FACTORY_TO_FF,
    DATASET_INBOUND_FF_TO_WB,
    DATASET_STOCK_FF,
)
from packages.contracts.cost_price_upload import CostPriceUploadResult
from packages.contracts.registry_upload_file_backed_service import RegistryUploadResult
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.contracts.wb_regional_supply import DISTRICT_KEYS, DISTRICT_LABELS_RU

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_UPLOAD_PATH = "/v1/registry-upload/bundle"
DEFAULT_COST_PRICE_UPLOAD_PATH = "/v1/cost-price/upload"
DEFAULT_SHEET_PLAN_PATH = "/v1/sheet-vitrina-v1/plan"
DEFAULT_SHEET_DAILY_REPORT_PATH = "/v1/sheet-vitrina-v1/daily-report"
DEFAULT_SHEET_STOCK_REPORT_PATH = "/v1/sheet-vitrina-v1/stock-report"
DEFAULT_SHEET_PLAN_REPORT_PATH = "/v1/sheet-vitrina-v1/plan-report"
DEFAULT_SHEET_PLAN_REPORT_BASELINE_TEMPLATE_PATH = "/v1/sheet-vitrina-v1/plan-report/baseline-template.xlsx"
DEFAULT_SHEET_PLAN_REPORT_BASELINE_UPLOAD_PATH = "/v1/sheet-vitrina-v1/plan-report/baseline-upload"
DEFAULT_SHEET_PLAN_REPORT_BASELINE_STATUS_PATH = "/v1/sheet-vitrina-v1/plan-report/baseline-status"
DEFAULT_SHEET_WEB_VITRINA_READ_PATH = "/v1/sheet-vitrina-v1/web-vitrina"
DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE = "page_composition"
WEB_VITRINA_HISTORY_MODE_EXPLICIT = "explicit"
DEFAULT_SHEET_WEB_VITRINA_GROUP_REFRESH_PATH = "/v1/sheet-vitrina-v1/web-vitrina/group-refresh"
DEFAULT_SHEET_WEB_VITRINA_AUTO_SCHEDULES_PATH = "/v1/sheet-vitrina-v1/web-vitrina/auto-schedules"
DEFAULT_SHEET_WEB_VITRINA_AUTO_SCHEDULES_RUN_NOW_PATH = "/v1/sheet-vitrina-v1/web-vitrina/auto-schedules/run-now"
DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH = "/v1/sheet-vitrina-v1/web-vitrina/user-config"
DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_OPTIONS_PATH = (
    "/v1/sheet-vitrina-v1/research/sku-group-comparison/options"
)
DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_CALCULATE_PATH = (
    "/v1/sheet-vitrina-v1/research/sku-group-comparison/calculate"
)
DEFAULT_SHEET_RESEARCH_PROMOTIONS_CALCULATE_PATH = "/v1/sheet-vitrina-v1/research/promotions/calculate"
DEFAULT_SHEET_FEEDBACKS_PATH = "/v1/sheet-vitrina-v1/feedbacks"
DEFAULT_SHEET_FEEDBACKS_EXPORT_PATH = "/v1/sheet-vitrina-v1/feedbacks/export.xlsx"
DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH = "/v1/sheet-vitrina-v1/feedbacks/ai-prompt"
DEFAULT_SHEET_FEEDBACKS_AI_ANALYZE_PATH = "/v1/sheet-vitrina-v1/feedbacks/ai-analyze"
DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH = "/v1/sheet-vitrina-v1/feedbacks/complaints"
DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_PATH = "/v1/sheet-vitrina-v1/feedbacks/complaints/sync-status"
DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH = (
    "/v1/sheet-vitrina-v1/feedbacks/complaints/sync-status/job"
)
DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_SELECTED_PATH = (
    "/v1/sheet-vitrina-v1/feedbacks/complaints/submit-selected"
)
DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_JOB_PATH = "/v1/sheet-vitrina-v1/feedbacks/complaints/submit-job"
DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_SCHEDULES_PATH = "/v1/sheet-vitrina-v1/feedbacks/automation/schedules"
DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_RUN_NOW_PATH = "/v1/sheet-vitrina-v1/feedbacks/automation/run-now"
DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_RUNS_PATH = "/v1/sheet-vitrina-v1/feedbacks/automation/runs"
DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_RUN_PATH = "/v1/sheet-vitrina-v1/feedbacks/automation/run"
DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_TICK_PATH = "/v1/sheet-vitrina-v1/feedbacks/automation/tick"
DEFAULT_SHEET_ADS_SKUS_PATH = "/v1/sheet-vitrina-v1/ads/skus"
DEFAULT_SHEET_ADS_SKU_PREFIX = "/v1/sheet-vitrina-v1/ads/sku"
DEFAULT_SHEET_ADS_BID_PREVIEW_PATH = "/v1/sheet-vitrina-v1/ads/bid-change/preview"
DEFAULT_SHEET_ADS_BID_COMMIT_PATH = "/v1/sheet-vitrina-v1/ads/bid-change/commit"
DEFAULT_SHEET_REFRESH_PATH = "/v1/sheet-vitrina-v1/refresh"
DEFAULT_SHEET_LOAD_PATH = "/v1/sheet-vitrina-v1/load"
DEFAULT_SHEET_STATUS_PATH = "/v1/sheet-vitrina-v1/status"
DEFAULT_SHEET_JOB_PATH = "/v1/sheet-vitrina-v1/job"
DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH = "/v1/sheet-vitrina-v1/seller-portal-session/check"
DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH = "/v1/sheet-vitrina-v1/seller-portal-recovery/status"
DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH = "/v1/sheet-vitrina-v1/seller-portal-recovery/start"
DEFAULT_SHEET_WEB_VITRINA_SELLER_RECOVERY_START_PATH = "/v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start"
DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH = "/v1/sheet-vitrina-v1/seller-portal-recovery/stop"
DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH = "/v1/sheet-vitrina-v1/seller-portal-recovery/launcher.zip"
DEFAULT_SHEET_OPERATOR_UI_PATH = "/sheet-vitrina-v1/operator"
DEFAULT_SHEET_WEB_VITRINA_UI_PATH = "/sheet-vitrina-v1/vitrina"
DEFAULT_SHEET_SUPPLIER_UI_PATH = "/sheet-vitrina-v1/supplier"
DEFAULT_WEB_AUTH_LOGIN_PATH = "/login"
DEFAULT_WEB_AUTH_LOGOUT_PATH = "/logout"
WEB_AUTH_COOKIE_NAME = "wb_core_web_session"
WEB_AUTH_DEFAULT_MAX_AGE_SECONDS = 8 * 60 * 60
WEB_AUTH_ROLE_ADMIN = "admin"
WEB_AUTH_ROLE_OPERATOR = "operator"
WEB_AUTH_ROLE_SUPPLIER = "supplier"
WEB_AUTH_ROLE_SUPPLY_OPERATOR = "supply_operator"
WEB_AUTH_FULL_OPERATOR_ROLES = {WEB_AUTH_ROLE_ADMIN, WEB_AUTH_ROLE_OPERATOR}
WEB_AUTH_SUPPLY_OPERATOR_ROLES = {
    WEB_AUTH_ROLE_ADMIN,
    WEB_AUTH_ROLE_OPERATOR,
    WEB_AUTH_ROLE_SUPPLY_OPERATOR,
}
WEB_AUTH_RUNTIME_ROLES = {
    WEB_AUTH_ROLE_ADMIN,
    WEB_AUTH_ROLE_OPERATOR,
    WEB_AUTH_ROLE_SUPPLIER,
    WEB_AUTH_ROLE_SUPPLY_OPERATOR,
}
WEB_AUTH_SECTION_VITRINA = "vitrina"
WEB_AUTH_SECTION_SUPPLY = "supply"
WEB_AUTH_SECTION_REPORTS = "reports"
WEB_AUTH_SECTION_FEEDBACKS = "feedbacks"
WEB_AUTH_SECTION_ADS = "ads"
WEB_AUTH_SECTION_RESEARCH = "research"
WEB_AUTH_SECTION_SETTINGS = "settings"
WEB_AUTH_SECTION_DEFINITIONS = (
    {"section_id": WEB_AUTH_SECTION_VITRINA, "label": "Витрина"},
    {"section_id": WEB_AUTH_SECTION_SUPPLY, "label": "Поставки"},
    {"section_id": WEB_AUTH_SECTION_REPORTS, "label": "Отчёты"},
    {"section_id": WEB_AUTH_SECTION_FEEDBACKS, "label": "Отзывы"},
    {"section_id": WEB_AUTH_SECTION_ADS, "label": "Реклама"},
    {"section_id": WEB_AUTH_SECTION_RESEARCH, "label": "Исследования"},
    {"section_id": WEB_AUTH_SECTION_SETTINGS, "label": "Настройки"},
)
WEB_AUTH_SECTION_IDS = tuple(str(section["section_id"]) for section in WEB_AUTH_SECTION_DEFINITIONS)
WEB_AUTH_UNIFIED_TAB_SECTIONS = {
    "vitrina": WEB_AUTH_SECTION_VITRINA,
    "factory-order": WEB_AUTH_SECTION_SUPPLY,
    "reports": WEB_AUTH_SECTION_REPORTS,
    "feedbacks": WEB_AUTH_SECTION_FEEDBACKS,
    "ads": WEB_AUTH_SECTION_ADS,
    "research": WEB_AUTH_SECTION_RESEARCH,
    "settings": WEB_AUTH_SECTION_SETTINGS,
}
SERVICE_USER_USERNAME_PREFIXES = (
    "codex_",
    "codex_live_",
    "codex_debug_",
    "smoke_",
    "test_",
)
SERVICE_USER_DISPLAY_NAME_PHRASES = (
    "codex live",
    "codex debug",
)
SERVICE_USER_DISPLAY_NAME_WORDS = ("smoke", "test")
SERVICE_USER_KINDS = {"service", "test", "debug"}
SERVICE_USER_CREATORS = {"codex", "public_verify", "smoke"}
DEFAULT_FACTORY_ORDER_STATUS_PATH = "/v1/sheet-vitrina-v1/supply/factory-order/status"
DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH = "/v1/sheet-vitrina-v1/supply/factory-order/template/stock-ff.xlsx"
DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_CHECK_PATH = "/v1/sheet-vitrina-v1/supply/factory-order/stock-ff/onec-check"
DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_XLSX_PATH = "/v1/sheet-vitrina-v1/supply/factory-order/stock-ff/onec.xlsx"
DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH = (
    "/v1/sheet-vitrina-v1/supply/factory-order/template/inbound-factory.xlsx"
)
DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH = (
    "/v1/sheet-vitrina-v1/supply/factory-order/template/inbound-ff-to-wb.xlsx"
)
DEFAULT_FACTORY_ORDER_UPLOAD_STOCK_FF_PATH = "/v1/sheet-vitrina-v1/supply/factory-order/upload/stock-ff"
DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FACTORY_PATH = (
    "/v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-factory"
)
DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FF_TO_WB_PATH = (
    "/v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-ff-to-wb"
)
DEFAULT_FACTORY_ORDER_UPLOADED_STOCK_FF_PATH = "/v1/sheet-vitrina-v1/supply/factory-order/uploaded/stock-ff.xlsx"
DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FACTORY_PATH = (
    "/v1/sheet-vitrina-v1/supply/factory-order/uploaded/inbound-factory.xlsx"
)
DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FF_TO_WB_PATH = (
    "/v1/sheet-vitrina-v1/supply/factory-order/uploaded/inbound-ff-to-wb.xlsx"
)
DEFAULT_FACTORY_ORDER_DELETE_STOCK_FF_PATH = "/v1/sheet-vitrina-v1/supply/factory-order/uploaded/stock-ff"
DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FACTORY_PATH = (
    "/v1/sheet-vitrina-v1/supply/factory-order/uploaded/inbound-factory"
)
DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FF_TO_WB_PATH = (
    "/v1/sheet-vitrina-v1/supply/factory-order/uploaded/inbound-ff-to-wb"
)
DEFAULT_FACTORY_ORDER_CALCULATE_PATH = "/v1/sheet-vitrina-v1/supply/factory-order/calculate"
DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH = "/v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx"
DEFAULT_WB_REGIONAL_STATUS_PATH = "/v1/sheet-vitrina-v1/supply/wb-regional/status"
DEFAULT_WB_REGIONAL_CALCULATE_PATH = "/v1/sheet-vitrina-v1/supply/wb-regional/calculate"
DEFAULT_WB_REGIONAL_PLANNING_OPTIONS_PATH = "/v1/sheet-vitrina-v1/supply/wb-regional/planning-options"
DEFAULT_WB_REGIONAL_DISTRICT_DOWNLOAD_PREFIX = "/v1/sheet-vitrina-v1/supply/wb-regional/district"
DEFAULT_WB_REGIONAL_RECOMMENDATIONS_ZIP_PATH = "/v1/sheet-vitrina-v1/supply/wb-regional/recommendations.zip"
DEFAULT_WB_SUPPLIES_PATH = "/v1/sheet-vitrina-v1/supply/wb-supplies"
DEFAULT_WB_SUPPLIES_SYNC_PATH = "/v1/sheet-vitrina-v1/supply/wb-supplies/sync"
DEFAULT_WB_SUPPLIES_BACKFILL_PATH = "/v1/sheet-vitrina-v1/supply/wb-supplies/backfill"
DEFAULT_WB_SUPPLIES_SYNC_STATUS_PATH = "/v1/sheet-vitrina-v1/supply/wb-supplies/sync-status"
DEFAULT_WB_SUPPLIES_TRANSIT_COST_ENRICH_PATH = "/v1/sheet-vitrina-v1/supply/wb-supplies/transit-cost/enrich"
DEFAULT_WB_SUPPLIES_TRANSIT_COST_STATUS_PATH = "/v1/sheet-vitrina-v1/supply/wb-supplies/transit-cost/status"
DEFAULT_WB_SUPPLIES_OVERLAY_OPTIONS_PATH = "/v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options"
DEFAULT_SUPPLIER_SHIPMENTS_PATH = "/v1/sheet-vitrina-v1/supply/supplier-shipments"
DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH = "/v1/sheet-vitrina-v1/supply/supplier-shipments/parse"
DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_PATH = "/v1/sheet-vitrina-v1/supply/supplier-shipments/registry"
DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_COMPARE_QUOTE_PATH = f"{DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_PATH}/compare-quote"
DEFAULT_SUPPLIER_ORDER_DOCUMENTS_SEGMENT = "documents"
DEFAULT_SUPPLIER_FINANCIAL_DOCUMENTS_SEGMENT = "financial-documents"
DEFAULT_CNY_ACCOUNT_PATH = "/v1/sheet-vitrina-v1/supply/cny-account"
DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH = f"{DEFAULT_CNY_ACCOUNT_PATH}/documents"
DEFAULT_CNY_ACCOUNT_CONVERSIONS_PATH = f"{DEFAULT_CNY_ACCOUNT_PATH}/conversions"
DEFAULT_CNY_ACCOUNT_LEDGER_PATH = f"{DEFAULT_CNY_ACCOUNT_PATH}/ledger"
DEFAULT_CNY_ACCOUNT_OPENING_BALANCE_PATH = f"{DEFAULT_CNY_ACCOUNT_PATH}/opening-balance"
DEFAULT_CNY_ACCOUNT_REPLAY_PATH = f"{DEFAULT_CNY_ACCOUNT_PATH}/replay"
DEFAULT_SETTINGS_UI_PATH = "/sheet-vitrina-v1/settings"
DEFAULT_NOMENCLATURE_PATH = "/v1/sheet-vitrina-v1/settings/nomenclature"
DEFAULT_NOMENCLATURE_EXPORT_PATH = "/v1/sheet-vitrina-v1/settings/nomenclature/export.xlsx"
DEFAULT_NOMENCLATURE_IMPORT_PATH = "/v1/sheet-vitrina-v1/settings/nomenclature/import.xlsx"
DEFAULT_NOMENCLATURE_BARCODE_SYNC_PATH = "/v1/sheet-vitrina-v1/settings/nomenclature/barcode-sync"
DEFAULT_TRADE_DOCUMENTS_PATH = "/v1/sheet-vitrina-v1/settings/documents"
DEFAULT_SETTINGS_USERS_PATH = "/v1/sheet-vitrina-v1/settings/users"
DEFAULT_RUNTIME_DIR = ROOT / ".runtime" / "registry_upload"
OPERATOR_UI_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "sheet_vitrina_v1_operator.html"
WEB_VITRINA_UI_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "sheet_vitrina_v1_web_vitrina.html"
SUPPLIER_UI_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "sheet_vitrina_v1_supplier.html"
SETTINGS_UI_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "sheet_vitrina_v1_settings.html"


def load_registry_upload_http_entrypoint_config() -> RegistryUploadHttpEntrypointConfig:
    host = os.environ.get("REGISTRY_UPLOAD_HTTP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST

    raw_port = os.environ.get("REGISTRY_UPLOAD_HTTP_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(f"REGISTRY_UPLOAD_HTTP_PORT must be an integer, got {raw_port!r}") from exc
    if port < 0 or port > 65535:
        raise ValueError(f"REGISTRY_UPLOAD_HTTP_PORT must be between 0 and 65535, got {port}")

    upload_path = os.environ.get("REGISTRY_UPLOAD_HTTP_PATH", DEFAULT_UPLOAD_PATH).strip() or DEFAULT_UPLOAD_PATH
    if not upload_path.startswith("/"):
        raise ValueError("REGISTRY_UPLOAD_HTTP_PATH must start with /")

    cost_price_upload_path = (
        os.environ.get("COST_PRICE_UPLOAD_HTTP_PATH", DEFAULT_COST_PRICE_UPLOAD_PATH).strip()
        or DEFAULT_COST_PRICE_UPLOAD_PATH
    )
    if not cost_price_upload_path.startswith("/"):
        raise ValueError("COST_PRICE_UPLOAD_HTTP_PATH must start with /")

    sheet_plan_path = os.environ.get("SHEET_VITRINA_HTTP_PATH", DEFAULT_SHEET_PLAN_PATH).strip() or DEFAULT_SHEET_PLAN_PATH
    if not sheet_plan_path.startswith("/"):
        raise ValueError("SHEET_VITRINA_HTTP_PATH must start with /")

    sheet_refresh_path = (
        os.environ.get("SHEET_VITRINA_REFRESH_HTTP_PATH", DEFAULT_SHEET_REFRESH_PATH).strip()
        or DEFAULT_SHEET_REFRESH_PATH
    )
    if not sheet_refresh_path.startswith("/"):
        raise ValueError("SHEET_VITRINA_REFRESH_HTTP_PATH must start with /")

    sheet_status_path = (
        os.environ.get("SHEET_VITRINA_STATUS_HTTP_PATH", DEFAULT_SHEET_STATUS_PATH).strip()
        or DEFAULT_SHEET_STATUS_PATH
    )
    if not sheet_status_path.startswith("/"):
        raise ValueError("SHEET_VITRINA_STATUS_HTTP_PATH must start with /")

    sheet_operator_ui_path = (
        os.environ.get("SHEET_VITRINA_OPERATOR_UI_PATH", DEFAULT_SHEET_OPERATOR_UI_PATH).strip()
        or DEFAULT_SHEET_OPERATOR_UI_PATH
    )
    if not sheet_operator_ui_path.startswith("/"):
        raise ValueError("SHEET_VITRINA_OPERATOR_UI_PATH must start with /")

    raw_runtime_dir = os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", str(DEFAULT_RUNTIME_DIR)).strip()
    runtime_dir = Path(raw_runtime_dir).expanduser()

    return RegistryUploadHttpEntrypointConfig(
        host=host,
        port=port,
        upload_path=upload_path,
        cost_price_upload_path=cost_price_upload_path,
        sheet_plan_path=sheet_plan_path,
        sheet_refresh_path=sheet_refresh_path,
        sheet_status_path=sheet_status_path,
        sheet_operator_ui_path=sheet_operator_ui_path,
        runtime_dir=runtime_dir,
    )


def build_registry_upload_http_server(
    config: RegistryUploadHttpEntrypointConfig,
    entrypoint: RegistryUploadHttpEntrypoint | None = None,
) -> HTTPServer:
    runtime_entrypoint = entrypoint or RegistryUploadHttpEntrypoint(runtime_dir=config.runtime_dir)
    handler_cls = _build_handler(
        runtime_entrypoint,
        upload_path=config.upload_path,
        cost_price_upload_path=config.cost_price_upload_path,
        sheet_plan_path=config.sheet_plan_path,
        sheet_refresh_path=config.sheet_refresh_path,
        sheet_load_path=DEFAULT_SHEET_LOAD_PATH,
        sheet_status_path=config.sheet_status_path,
        sheet_job_path=DEFAULT_SHEET_JOB_PATH,
        sheet_operator_ui_path=config.sheet_operator_ui_path,
    )
    return RegistryUploadHttpServer((config.host, config.port), handler_cls)


def _build_handler(
    entrypoint: RegistryUploadHttpEntrypoint,
    *,
    upload_path: str,
    cost_price_upload_path: str,
    sheet_plan_path: str,
    sheet_refresh_path: str,
    sheet_load_path: str,
    sheet_status_path: str,
    sheet_job_path: str,
    sheet_operator_ui_path: str,
) -> type[BaseHTTPRequestHandler]:
    class RegistryUploadHandler(BaseHTTPRequestHandler):
        runtime_entrypoint = entrypoint

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
            if parsed.path == DEFAULT_WEB_AUTH_LOGIN_PATH:
                _handle_web_auth_login(self, parsed.query)
                return
            if parsed.path == DEFAULT_WEB_AUTH_LOGOUT_PATH:
                _handle_web_auth_logout(self)
                return
            if not _ensure_web_auth(self, parsed):
                return
            if parsed.path == DEFAULT_SETTINGS_USERS_PATH:
                _handle_settings_user_create(self, entrypoint)
                return
            if parsed.path == upload_path:
                try:
                    payload = _load_request_payload(self)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return

                try:
                    result = entrypoint.handle_bundle_payload(payload)
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"registry upload runtime failed: {exc}"},
                    )
                    return

                _write_json_response(
                    self,
                    _http_status_for_result(result),
                    asdict(result),
                )
                return

            if parsed.path == cost_price_upload_path:
                try:
                    payload = _load_request_payload(self)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return

                try:
                    result = entrypoint.handle_cost_price_payload(payload)
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"cost price upload runtime failed: {exc}"},
                    )
                    return

                _write_json_response(
                    self,
                    _http_status_for_cost_price_result(result),
                    asdict(result),
                )
                return

            if parsed.path == sheet_refresh_path:
                try:
                    payload = _load_optional_request_payload(self)
                    as_of_date = _resolve_as_of_date(parsed.query, payload)
                    async_requested = _resolve_async_requested(payload)
                    auto_load_requested = _resolve_auto_load_requested(payload)
                    auto_refresh_requested = _resolve_auto_refresh_requested(payload)
                    auto_schedule_id = _resolve_auto_schedule_id(payload)
                    auto_schedule_due_at = _resolve_auto_schedule_due_at(payload)
                    auto_trigger_source = _resolve_auto_trigger_source(payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return

                if async_requested:
                    try:
                        if auto_refresh_requested:
                            job_payload = entrypoint.start_sheet_auto_refresh_job(
                                as_of_date=as_of_date or None,
                                schedule_id=auto_schedule_id,
                                due_at=auto_schedule_due_at,
                                trigger_source=auto_trigger_source or "scheduled",
                            )
                        else:
                            job_payload = entrypoint.start_sheet_refresh_job(
                                as_of_date=as_of_date or None,
                                auto_load=auto_refresh_requested,
                            )
                    except Exception as exc:  # pragma: no cover - bounded fallback
                        _write_json_response(
                            self,
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"error": f"sheet vitrina refresh runtime failed: {exc}"},
                        )
                        return

                    _write_json_response(
                        self,
                        HTTPStatus.ACCEPTED,
                        _with_sheet_job_urls(job_payload, sheet_job_path),
                    )
                    return

                try:
                    if auto_refresh_requested:
                        refresh_result = entrypoint.handle_sheet_auto_refresh_request(
                            as_of_date=as_of_date or None,
                            schedule_id=auto_schedule_id,
                            due_at=auto_schedule_due_at,
                            trigger_source=auto_trigger_source or "scheduled",
                        )
                    else:
                        refresh_result = entrypoint.handle_sheet_refresh_request(
                            as_of_date=as_of_date or None,
                            auto_load=auto_refresh_requested,
                        )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except LegacyGoogleSheetsContourArchivedError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.GONE,
                        {
                            "error": str(exc),
                            "status": "archived",
                            "target": "legacy_google_sheets_contour",
                        },
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina refresh runtime failed: {exc}"},
                    )
                    return

                _write_json_response(
                    self,
                    HTTPStatus.OK,
                    refresh_result,
                )
                return

            if parsed.path == sheet_load_path:
                try:
                    payload = _load_optional_request_payload(self)
                    as_of_date = _resolve_as_of_date(parsed.query, payload)
                    async_requested = _resolve_async_requested(payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return

                if async_requested:
                    try:
                        job_payload = entrypoint.start_sheet_load_job(as_of_date=as_of_date or None)
                    except LegacyGoogleSheetsContourArchivedError as exc:
                        _write_json_response(
                            self,
                            HTTPStatus.GONE,
                            {
                                "error": str(exc),
                                "status": "archived",
                                "target": "legacy_google_sheets_contour",
                            },
                        )
                        return
                    except Exception as exc:  # pragma: no cover - bounded fallback
                        _write_json_response(
                            self,
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"error": f"sheet vitrina load runtime failed: {exc}"},
                        )
                        return

                    _write_json_response(
                        self,
                        HTTPStatus.ACCEPTED,
                        _with_sheet_job_urls(job_payload, sheet_job_path),
                    )
                    return

                try:
                    load_result = entrypoint.handle_sheet_load_request(as_of_date=as_of_date or None)
                except LegacyGoogleSheetsContourArchivedError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.GONE,
                        {
                            "error": str(exc),
                            "status": "archived",
                            "target": "legacy_google_sheets_contour",
                        },
                    )
                    return
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina load runtime failed: {exc}"},
                    )
                    return

                _write_json_response(
                    self,
                    HTTPStatus.OK,
                    load_result,
                )
                return

            if parsed.path == DEFAULT_SHEET_WEB_VITRINA_GROUP_REFRESH_PATH:
                try:
                    payload = _load_optional_request_payload(self)
                    source_group_id = _resolve_source_group_id(parsed.query, payload)
                    as_of_date = _resolve_as_of_date(parsed.query, payload)
                    job_payload = entrypoint.start_sheet_source_group_refresh_job(
                        source_group_id=source_group_id,
                        as_of_date=as_of_date or None,
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina group refresh runtime failed: {exc}"},
                    )
                    return

                _write_json_response(
                    self,
                    HTTPStatus.ACCEPTED,
                    _with_sheet_job_urls(job_payload, sheet_job_path),
                )
                return

            if parsed.path == DEFAULT_SHEET_WEB_VITRINA_AUTO_SCHEDULES_PATH:
                try:
                    payload = _load_optional_request_payload(self)
                    result = entrypoint.handle_sheet_web_vitrina_auto_schedules_save_request(payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina auto schedule runtime failed: {exc}"},
                    )
                    return

                _write_json_response(
                    self,
                    HTTPStatus.OK,
                    result,
                )
                return

            if parsed.path == DEFAULT_SHEET_WEB_VITRINA_AUTO_SCHEDULES_RUN_NOW_PATH:
                try:
                    payload = _load_optional_request_payload(self)
                    job_payload = entrypoint.handle_sheet_web_vitrina_auto_schedules_run_now_request(payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina auto schedule run-now failed: {exc}"},
                    )
                    return

                _write_json_response(
                    self,
                    HTTPStatus.ACCEPTED,
                    _with_sheet_job_urls(job_payload, sheet_job_path),
                )
                return

            if parsed.path == DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH:
                try:
                    payload = _load_optional_request_payload(self)
                    result = entrypoint.handle_sheet_web_vitrina_user_config_save_request(
                        user_key=_current_web_user_config_key(self),
                        payload=payload,
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina user config save failed: {exc}"},
                    )
                    return
                if result.get("status") == "conflict":
                    _write_json_response(self, HTTPStatus.CONFLICT, result)
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_CALCULATE_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_sheet_research_sku_group_comparison_calculate_request(
                        payload,
                        page_route=DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                        read_route=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina research calculation failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_SHEET_RESEARCH_PROMOTIONS_CALCULATE_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_sheet_research_promotions_calculate_request(
                        payload,
                        page_route=DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                        read_route=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina research promotions calculation failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_SHEET_ADS_BID_PREVIEW_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_sheet_ads_bid_preview_request(payload)
                except SheetVitrinaV1AdsError as exc:
                    response_payload = {"error": str(exc)}
                    response_payload.update(exc.payload)
                    _write_json_response(self, HTTPStatus(exc.http_status), response_payload)
                    return
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina ads bid preview failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_SHEET_ADS_BID_COMMIT_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_sheet_ads_bid_commit_request(
                        payload,
                        actor=_current_web_user_config_key(self),
                    )
                except SheetVitrinaV1AdsError as exc:
                    response_payload = {"error": str(exc)}
                    response_payload.update(exc.payload)
                    _write_json_response(self, HTTPStatus(exc.http_status), response_payload)
                    return
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina ads bid commit failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_sheet_feedbacks_ai_prompt_save_request(payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except SheetVitrinaV1FeedbacksAiError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina feedbacks AI prompt failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_AI_ANALYZE_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_sheet_feedbacks_ai_analyze_request(payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except SheetVitrinaV1FeedbacksAiError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina feedbacks AI analyze failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_EXPORT_PATH:
                try:
                    payload = _load_request_payload(self)
                    workbook_bytes, filename = entrypoint.handle_sheet_feedbacks_export_request(payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina feedbacks export failed: {exc}"},
                    )
                    return

                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    workbook_bytes,
                    content_type=FEEDBACKS_EXPORT_CONTENT_TYPE,
                    filename=filename,
                    as_attachment=True,
                )
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_sheet_feedbacks_complaints_sync_status_request(payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except SheetVitrinaV1FeedbacksComplaintsError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina feedbacks complaints status sync failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, _with_complaints_sync_job_urls(result))
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_SELECTED_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_sheet_feedbacks_complaints_submit_selected_request(payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except SheetVitrinaV1FeedbacksComplaintsError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina feedbacks complaints submit job failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, _with_complaints_submit_job_urls(result))
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_SCHEDULES_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_sheet_feedbacks_auto_complaints_schedules_save_request(payload)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                    return
                except SheetVitrinaV1FeedbacksAutoComplaintsError as exc:
                    _write_json_response(self, HTTPStatus(exc.http_status), _auto_complaints_error_payload(exc))
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"sheet vitrina feedbacks automation schedules failed: {exc}"})
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_RUN_NOW_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_sheet_feedbacks_auto_complaints_run_now_request(payload)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                    return
                except SheetVitrinaV1FeedbacksAutoComplaintsError as exc:
                    _write_json_response(self, HTTPStatus(exc.http_status), _auto_complaints_error_payload(exc))
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"sheet vitrina feedbacks automation run-now failed: {exc}"})
                    return
                _write_json_response(self, HTTPStatus.ACCEPTED, result)
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_TICK_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_sheet_feedbacks_auto_complaints_tick_request(payload)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                    return
                except SheetVitrinaV1FeedbacksAutoComplaintsError as exc:
                    _write_json_response(self, HTTPStatus(exc.http_status), _auto_complaints_error_payload(exc))
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"sheet vitrina feedbacks automation tick failed: {exc}"})
                    return
                _write_json_response(self, HTTPStatus.ACCEPTED, result)
                return

            if parsed.path == DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH:
                try:
                    job_payload = entrypoint.start_seller_portal_session_check_job(
                        launcher_download_path=DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                    )
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"seller portal session check failed: {exc}"},
                    )
                    return
                _write_json_response(
                    self,
                    HTTPStatus.ACCEPTED,
                    _with_sheet_job_urls(job_payload, sheet_job_path),
                )
                return

            if parsed.path == DEFAULT_SHEET_WEB_VITRINA_SELLER_RECOVERY_START_PATH:
                try:
                    payload = _load_optional_request_payload(self)
                    replace = _resolve_replace_requested(payload)
                    job_payload = entrypoint.start_seller_portal_recovery_start_job(
                        launcher_download_path=DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                        replace_existing=replace,
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"seller portal recovery start failed: {exc}"},
                    )
                    return
                _write_json_response(
                    self,
                    HTTPStatus.ACCEPTED,
                    _with_sheet_job_urls(job_payload, sheet_job_path),
                )
                return

            if parsed.path == DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH:
                try:
                    payload = _load_optional_request_payload(self)
                    replace = _resolve_replace_requested(payload)
                    recovery_payload = entrypoint.handle_seller_portal_recovery_start_request(
                        launcher_download_path=DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                        replace=replace,
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"seller portal recovery start failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, recovery_payload)
                return

            if parsed.path == DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH:
                try:
                    recovery_payload = entrypoint.handle_seller_portal_recovery_stop_request(
                        launcher_download_path=DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                    )
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"seller portal recovery stop failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, recovery_payload)
                return

            if parsed.path == DEFAULT_SHEET_PLAN_REPORT_BASELINE_UPLOAD_PATH:
                try:
                    upload_payload = _load_uploaded_file_payload(self)
                    payload = entrypoint.handle_sheet_plan_report_baseline_upload_request(
                        upload_payload["workbook_bytes"],
                        uploaded_filename=str(upload_payload.get("filename") or ""),
                        uploaded_content_type=str(upload_payload.get("content_type") or ""),
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH:
                try:
                    upload_payload = _load_uploaded_file_payload(self)
                    payload = entrypoint.handle_supplier_shipments_parse_request(
                        upload_payload["workbook_bytes"],
                        uploaded_filename=str(upload_payload.get("filename") or ""),
                        uploaded_content_type=str(upload_payload.get("content_type") or ""),
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": f"supplier invoice parse failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_COMPARE_QUOTE_PATH:
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    upload_payload = _load_uploaded_file_payload(self)
                    fields = upload_payload.get("fields") if isinstance(upload_payload.get("fields"), Mapping) else {}
                    shipment_id = str(fields.get("shipment_id") or "").strip()
                    if not shipment_id:
                        raise ValueError("shipment_id field is required")
                    payload = entrypoint.handle_supplier_shipment_registry_compare_quote_request(
                        shipment_id,
                        upload_payload["workbook_bytes"],
                        uploaded_filename=str(upload_payload.get("filename") or ""),
                        uploaded_content_type=str(upload_payload.get("content_type") or ""),
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier shipment registry quote comparison failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH:
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    upload_payload = _load_uploaded_file_payload(self)
                    payload = entrypoint.handle_cny_account_upload_request(
                        upload_payload["workbook_bytes"],
                        uploaded_filename=str(upload_payload.get("filename") or ""),
                        uploaded_content_type=str(upload_payload.get("content_type") or ""),
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"CNY account document upload failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_CNY_ACCOUNT_OPENING_BALANCE_PATH:
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_cny_account_opening_balance_request(payload)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"CNY opening balance save failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_CNY_ACCOUNT_REPLAY_PATH:
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    result = entrypoint.handle_cny_account_replay_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"CNY ledger replay failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if _is_supplier_shipment_contract_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id = _resolve_supplier_shipment_id_from_contract_path(parsed.path)
                    upload_payload = _load_uploaded_file_payload(self)
                    payload = entrypoint.handle_supplier_shipments_contract_upload_request(
                        shipment_id,
                        upload_payload["workbook_bytes"],
                        uploaded_filename=str(upload_payload.get("filename") or ""),
                        uploaded_content_type=str(upload_payload.get("content_type") or ""),
                        fields=upload_payload.get("fields") if isinstance(upload_payload.get("fields"), Mapping) else {},
                        actor=_current_web_user_config_key(self),
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier shipment contract upload failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_supplier_financial_documents_collection_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id = _resolve_supplier_financial_shipment_id(parsed.path)
                    upload_payload = _load_uploaded_file_payload(self)
                    payload = entrypoint.handle_supplier_financial_documents_upload_request(
                        shipment_id,
                        upload_payload["workbook_bytes"],
                        uploaded_filename=str(upload_payload.get("filename") or ""),
                        uploaded_content_type=str(upload_payload.get("content_type") or ""),
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier financial document upload failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_supplier_financial_document_confirm_import_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id, document_id = _resolve_supplier_financial_document_ids(parsed.path)
                    payload = entrypoint.handle_supplier_financial_document_confirm_import_request(
                        shipment_id,
                        document_id,
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier financial document import confirm failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SUPPLIER_SHIPMENTS_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_supplier_shipments_create_request(payload)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier shipment create failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_TRADE_DOCUMENTS_PATH:
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    upload_payload = _load_uploaded_file_payload(self)
                    result = entrypoint.handle_trade_documents_create_request(
                        upload_payload["workbook_bytes"],
                        uploaded_filename=str(upload_payload.get("filename") or ""),
                        uploaded_content_type=str(upload_payload.get("content_type") or ""),
                        fields=upload_payload.get("fields") if isinstance(upload_payload.get("fields"), Mapping) else {},
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"trade document upload failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if _is_supplier_shipment_rematch_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id = _resolve_supplier_shipment_id_from_rematch_path(parsed.path)
                    payload = _load_optional_request_payload(self)
                    result = entrypoint.handle_supplier_shipments_rematch_request(shipment_id, payload)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier shipment rematch failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if _is_supplier_shipment_price_check_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id = _resolve_supplier_shipment_id_from_price_check_path(parsed.path)
                    payload = _load_optional_request_payload(self)
                    result = entrypoint.handle_supplier_shipments_price_check_request(
                        shipment_id,
                        payload,
                        actor=_current_web_user_config_key(self),
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier shipment price check failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_NOMENCLATURE_IMPORT_PATH:
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    upload_payload = _load_uploaded_file_payload(self)
                    result = entrypoint.handle_nomenclature_import_request(
                        upload_payload["workbook_bytes"],
                        uploaded_filename=str(upload_payload.get("filename") or ""),
                        uploaded_content_type=str(upload_payload.get("content_type") or ""),
                        dry_run=_resolve_optional_query_bool(parsed.query, "dry_run"),
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"nomenclature import failed: {exc}"},
                    )
                    return
                response_status = HTTPStatus.OK if result.get("status") == "ok" else HTTPStatus.BAD_REQUEST
                _write_json_response(self, response_status, result)
                return

            if parsed.path == DEFAULT_NOMENCLATURE_BARCODE_SYNC_PATH:
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    payload = _load_optional_request_payload(self)
                    result = entrypoint.handle_nomenclature_barcode_sync_request(payload)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"nomenclature barcode sync failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if _is_nomenclature_item_barcode_sync_path(parsed.path):
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    item_id = _resolve_nomenclature_item_barcode_sync_id(parsed.path)
                    result = entrypoint.handle_nomenclature_item_barcode_sync_request(item_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"nomenclature barcode sync failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_NOMENCLATURE_PATH:
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_nomenclature_create_request(payload)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"nomenclature create failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path in {
                DEFAULT_FACTORY_ORDER_UPLOAD_STOCK_FF_PATH,
                DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FACTORY_PATH,
                DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FF_TO_WB_PATH,
            }:
                try:
                    upload_payload = _load_uploaded_file_payload(self)
                    dataset_type = _resolve_factory_order_dataset_type_from_upload_path(parsed.path)
                    payload = entrypoint.handle_factory_order_upload_request(
                        dataset_type,
                        upload_payload["workbook_bytes"],
                        uploaded_filename=str(upload_payload.get("filename") or ""),
                        uploaded_content_type=str(upload_payload.get("content_type") or ""),
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_FACTORY_ORDER_CALCULATE_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_factory_order_calculate_request(payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"factory order runtime failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_WB_REGIONAL_CALCULATE_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_wb_regional_calculate_request(payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"wb regional supply runtime failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, _with_wb_regional_urls(result))
                return

            if parsed.path == DEFAULT_WB_REGIONAL_PLANNING_OPTIONS_PATH:
                try:
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_wb_regional_planning_options_request(payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"wb regional planning runtime failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if parsed.path == DEFAULT_WB_SUPPLIES_SYNC_PATH:
                try:
                    payload = _load_optional_request_payload(self)
                    result = entrypoint.handle_wb_supplies_sync_request(payload)
                except WbSuppliesBlockError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc), "contract_name": "sheet_vitrina_v1_wb_supplies"},
                    )
                    return
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"WB supplies sync failed: {exc}"},
                    )
                    return
                response_status = HTTPStatus.ACCEPTED if result.get("accepted") else HTTPStatus.OK
                _write_json_response(self, response_status, result)
                return

            if parsed.path == DEFAULT_WB_SUPPLIES_BACKFILL_PATH:
                try:
                    payload = _load_optional_request_payload(self)
                    result = entrypoint.handle_wb_supplies_backfill_request(payload)
                except WbSuppliesBlockError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc), "contract_name": "sheet_vitrina_v1_wb_supplies"},
                    )
                    return
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"WB supplies backfill failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.ACCEPTED, result)
                return

            if parsed.path == DEFAULT_WB_SUPPLIES_TRANSIT_COST_ENRICH_PATH:
                try:
                    payload = _load_optional_request_payload(self)
                    result = entrypoint.handle_wb_supplies_transit_cost_enrich_request(payload)
                except WbSuppliesBlockError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc), "contract_name": "sheet_vitrina_v1_wb_supplies"},
                    )
                    return
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"WB transit cost enrichment failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.ACCEPTED, result)
                return

            _write_json_response(
                self,
                HTTPStatus.NOT_FOUND,
                {"error": f"unsupported path: {parsed.path}"},
            )
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
            if parsed.path == DEFAULT_WEB_AUTH_LOGIN_PATH:
                _write_login_form_response(self, parsed.query)
                return
            if parsed.path == DEFAULT_WEB_AUTH_LOGOUT_PATH:
                _handle_web_auth_logout(self)
                return
            if not _ensure_web_auth(self, parsed):
                return
            if parsed.path == DEFAULT_SHEET_WEB_VITRINA_UI_PATH:
                _write_html_response(
                    self,
                    HTTPStatus.OK,
                    _render_sheet_vitrina_web_vitrina_ui(
                        read_path=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                        operator_path=sheet_operator_ui_path,
                        refresh_path=sheet_refresh_path,
                        job_path=sheet_job_path,
                        role=_current_web_user_role(self),
                        allowed_sections=_current_web_user_allowed_sections(self),
                    ),
                )
                return

            if parsed.path == DEFAULT_SHEET_SUPPLIER_UI_PATH:
                role = _current_web_user_role(self)
                has_supply_access = role != WEB_AUTH_ROLE_SUPPLIER and (
                    WEB_AUTH_SECTION_SUPPLY in _current_web_user_allowed_sections(self)
                )
                is_operator_embedded = (
                    has_supply_access
                    and _resolve_single_query_param(parsed.query, "embedded") == "operator"
                )
                _write_html_response(
                    self,
                    HTTPStatus.OK,
                    _render_sheet_vitrina_supplier_ui(
                        can_delete_shipments=has_supply_access,
                        can_edit_order_status=is_operator_embedded,
                        can_recheck_prices=is_operator_embedded,
                        can_manage_documents=is_operator_embedded,
                        can_manage_financial_documents=is_operator_embedded,
                        embedded="operator" if is_operator_embedded else "",
                    ),
                )
                return

            if parsed.path == DEFAULT_SETTINGS_UI_PATH:
                if not _ensure_operator_role(self, parsed.path):
                    return
                if _resolve_single_query_param(parsed.query, "embedded") == "1":
                    _write_html_response(
                        self,
                        HTTPStatus.OK,
                        _render_sheet_vitrina_settings_ui(
                            embedded=True,
                            can_manage_users=_current_web_user_can_manage_users(self),
                        ),
                    )
                    return
                _write_html_response(
                    self,
                    HTTPStatus.OK,
                    _render_sheet_vitrina_web_vitrina_ui(
                        read_path=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                        operator_path=sheet_operator_ui_path,
                        refresh_path=sheet_refresh_path,
                        job_path=sheet_job_path,
                        active_tab="settings",
                        role=_current_web_user_role(self),
                        allowed_sections=_current_web_user_allowed_sections(self),
                    ),
                )
                return

            if parsed.path == DEFAULT_SETTINGS_USERS_PATH:
                _handle_settings_users_list(self, entrypoint, query=parsed.query)
                return

            if parsed.path == sheet_operator_ui_path:
                try:
                    embedded_tab = _resolve_operator_embedded_tab_from_query(parsed.query)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                if not embedded_tab:
                    _write_html_response(
                        self,
                        HTTPStatus.OK,
                        _render_sheet_vitrina_web_vitrina_ui(
                            read_path=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                            operator_path=sheet_operator_ui_path,
                            refresh_path=sheet_refresh_path,
                            job_path=sheet_job_path,
                            role=_current_web_user_role(self),
                            allowed_sections=_current_web_user_allowed_sections(self),
                        ),
                    )
                    return
                embedded_section = WEB_AUTH_UNIFIED_TAB_SECTIONS.get(embedded_tab, "")
                if embedded_section and embedded_section not in _current_web_user_allowed_sections(self):
                    _write_auth_forbidden(self, parsed.path)
                    return
                _write_html_response(
                    self,
                    HTTPStatus.OK,
                    _render_sheet_vitrina_operator_ui(
                        daily_report_path=DEFAULT_SHEET_DAILY_REPORT_PATH,
                        stock_report_path=DEFAULT_SHEET_STOCK_REPORT_PATH,
                        plan_report_path=DEFAULT_SHEET_PLAN_REPORT_PATH,
                        refresh_path=sheet_refresh_path,
                        load_path=sheet_load_path,
                        status_path=sheet_status_path,
                        job_path=sheet_job_path,
                        operator_context=entrypoint.build_sheet_operator_ui_context(),
                        embedded_tab=embedded_tab,
                    ),
                )
                return

            if parsed.path == DEFAULT_SHEET_ADS_SKUS_PATH:
                try:
                    payload = entrypoint.handle_sheet_ads_skus_request(_flatten_query_params(parsed.query))
                except SheetVitrinaV1AdsError as exc:
                    response_payload = {"error": str(exc)}
                    response_payload.update(exc.payload)
                    _write_json_response(self, HTTPStatus(exc.http_status), response_payload)
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina ads skus failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path.startswith(DEFAULT_SHEET_ADS_SKU_PREFIX + "/"):
                try:
                    nm_id = _resolve_sheet_ads_sku_nm_id(parsed.path)
                    payload = entrypoint.handle_sheet_ads_sku_request(
                        nm_id,
                        _flatten_query_params(parsed.query),
                    )
                except SheetVitrinaV1AdsError as exc:
                    response_payload = {"error": str(exc)}
                    response_payload.update(exc.payload)
                    _write_json_response(self, HTTPStatus(exc.http_status), response_payload)
                    return
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina ads sku failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH:
                try:
                    payload = entrypoint.handle_seller_portal_session_check_request(
                        launcher_download_path=DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                    )
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"seller portal session check failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH:
                try:
                    run_id = _resolve_single_query_param(parsed.query, "run_id")
                    with_probe = _resolve_query_bool_default_true(parsed.query, "probe")
                    payload = entrypoint.handle_seller_portal_recovery_status_request(
                        launcher_download_path=DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                        run_id=run_id or None,
                        with_probe=with_probe,
                    )
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"seller portal recovery status failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH:
                try:
                    status_payload = entrypoint.handle_seller_portal_recovery_status_request(
                        launcher_download_path=DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                        run_id=None,
                    )
                    if not bool(status_payload.get("can_download_launcher") or status_payload.get("launcher_ready")):
                        _write_json_response(
                            self,
                            HTTPStatus.CONFLICT,
                            _seller_recovery_launcher_unavailable_payload(status_payload),
                        )
                        return
                    request_origin = _request_origin(self)
                    archive_bytes, filename = entrypoint.handle_seller_portal_recovery_launcher_request(
                        public_status_url=f"{request_origin}{DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH}",
                        public_operator_url=f"{request_origin}{sheet_operator_ui_path}",
                    )
                except RuntimeError as exc:
                    status_payload = entrypoint.handle_seller_portal_recovery_status_request(
                        launcher_download_path=DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                        run_id=None,
                    )
                    _write_json_response(
                        self,
                        HTTPStatus.CONFLICT,
                        _seller_recovery_launcher_unavailable_payload(status_payload, error=str(exc)),
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"seller portal recovery launcher failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    archive_bytes,
                    content_type="application/zip",
                    filename=filename,
                    as_attachment=True,
                )
                return

            if parsed.path == DEFAULT_SHEET_WEB_VITRINA_READ_PATH:
                try:
                    surface = _resolve_sheet_web_vitrina_surface_from_query(parsed.query)
                    include_source_status = _resolve_optional_query_bool(parsed.query, "include_source_status")
                    include_table_data = _resolve_optional_query_bool(parsed.query, "include_table_data")
                    as_of_date = _resolve_web_vitrina_as_of_date_from_query(
                        parsed.query,
                        surface=surface,
                        include_source_status=include_source_status,
                    )
                    date_from, date_to = _resolve_web_vitrina_period_window_from_query(parsed.query)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return

                if surface == DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE:
                    try:
                        payload = entrypoint.handle_sheet_web_vitrina_page_composition_request(
                            page_route=DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                            read_route=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                            operator_route=sheet_operator_ui_path,
                            job_path=sheet_job_path,
                            as_of_date=as_of_date,
                            date_from=date_from,
                            date_to=date_to,
                            include_source_status=include_source_status,
                            include_table_data=include_table_data,
                        )
                    except Exception as exc:  # pragma: no cover - last-resort public JSON guard
                        _write_json_response(
                            self,
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {
                                "error": f"sheet_vitrina_v1 page composition failed: {exc}",
                                "surface": DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE,
                            },
                        )
                        return
                    _write_json_response(
                        self,
                        HTTPStatus.OK,
                        payload,
                    )
                    return
                try:
                    payload = entrypoint.handle_sheet_web_vitrina_request(
                        page_route=DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                        read_route=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                        as_of_date=as_of_date,
                        date_from=date_from,
                        date_to=date_to,
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina web-vitrina runtime failed: {exc}"},
                    )
                    return

                _write_json_response(
                    self,
                    HTTPStatus.OK,
                    payload,
                )
                return

            if parsed.path == DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_OPTIONS_PATH:
                try:
                    payload = entrypoint.handle_sheet_research_sku_group_comparison_options_request(
                        page_route=DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                        read_route=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina research options failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH:
                try:
                    payload = entrypoint.handle_sheet_feedbacks_ai_prompt_get_request()
                except SheetVitrinaV1FeedbacksAiError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina feedbacks AI prompt runtime failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH:
                try:
                    run_id = _resolve_single_query_param(parsed.query, "run_id")
                    if not run_id:
                        raise ValueError("run_id query parameter is required")
                    payload = entrypoint.handle_sheet_feedbacks_complaints_sync_status_job_request(run_id)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except SheetVitrinaV1FeedbacksComplaintsError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina feedbacks complaints status sync job failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, _with_complaints_sync_job_urls(payload))
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_JOB_PATH:
                try:
                    run_id = _resolve_single_query_param(parsed.query, "run_id")
                    if not run_id:
                        raise ValueError("run_id query parameter is required")
                    payload = entrypoint.handle_sheet_feedbacks_complaints_submit_job_request(run_id)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except SheetVitrinaV1FeedbacksComplaintsError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina feedbacks complaints submit job failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, _with_complaints_submit_job_urls(payload))
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH:
                try:
                    payload = entrypoint.handle_sheet_feedbacks_complaints_request()
                except SheetVitrinaV1FeedbacksComplaintsError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina feedbacks complaints runtime failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_SCHEDULES_PATH:
                try:
                    payload = entrypoint.handle_sheet_feedbacks_auto_complaints_schedules_request()
                except SheetVitrinaV1FeedbacksAutoComplaintsError as exc:
                    _write_json_response(self, HTTPStatus(exc.http_status), _auto_complaints_error_payload(exc))
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"sheet vitrina feedbacks automation schedules runtime failed: {exc}"})
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SHEET_WEB_VITRINA_AUTO_SCHEDULES_PATH:
                try:
                    payload = entrypoint.handle_sheet_web_vitrina_auto_schedules_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina auto schedule runtime failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH:
                try:
                    payload = entrypoint.handle_sheet_web_vitrina_user_config_request(
                        user_key=_current_web_user_config_key(self),
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina user config runtime failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_RUNS_PATH:
                try:
                    payload = entrypoint.handle_sheet_feedbacks_auto_complaints_runs_request()
                except SheetVitrinaV1FeedbacksAutoComplaintsError as exc:
                    _write_json_response(self, HTTPStatus(exc.http_status), _auto_complaints_error_payload(exc))
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"sheet vitrina feedbacks automation runs runtime failed: {exc}"})
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_RUN_PATH:
                try:
                    run_id = _resolve_single_query_param(parsed.query, "run_id")
                    if not run_id:
                        raise ValueError("run_id query parameter is required")
                    payload = entrypoint.handle_sheet_feedbacks_auto_complaints_run_request(run_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                    return
                except SheetVitrinaV1FeedbacksAutoComplaintsError as exc:
                    _write_json_response(self, HTTPStatus(exc.http_status), _auto_complaints_error_payload(exc))
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"sheet vitrina feedbacks automation run runtime failed: {exc}"})
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SHEET_FEEDBACKS_PATH:
                try:
                    feedbacks_query = _resolve_feedbacks_query(parsed.query)
                    payload = entrypoint.handle_sheet_feedbacks_request(**feedbacks_query)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except SheetVitrinaV1FeedbacksError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina feedbacks runtime failed: {exc}"},
                    )
                    return

                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SHEET_DAILY_REPORT_PATH:
                try:
                    payload = entrypoint.handle_sheet_daily_report_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina daily report runtime failed: {exc}"},
                    )
                    return

                _write_json_response(
                    self,
                    HTTPStatus.OK,
                    payload,
                )
                return

            if parsed.path == DEFAULT_SHEET_STOCK_REPORT_PATH:
                try:
                    payload = entrypoint.handle_sheet_stock_report_request(
                        as_of_date=_resolve_as_of_date_from_query(parsed.query) or None,
                        sales_avg_period_days=_resolve_sales_avg_period_days_from_query(parsed.query),
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina stock report runtime failed: {exc}"},
                    )
                    return

                _write_json_response(
                    self,
                    HTTPStatus.OK,
                    payload,
                )
                return

            if parsed.path == DEFAULT_SHEET_PLAN_REPORT_PATH:
                try:
                    payload = entrypoint.handle_sheet_plan_report_request(
                        period=_resolve_required_query_value(parsed.query, "period"),
                        plan_drr_pct=_resolve_required_query_float(parsed.query, "plan_drr_pct"),
                        h1_buyout_plan_rub=_resolve_optional_query_float(parsed.query, "h1_buyout_plan_rub"),
                        h2_buyout_plan_rub=_resolve_optional_query_float(parsed.query, "h2_buyout_plan_rub"),
                        q1_buyout_plan_rub=_resolve_optional_query_float(parsed.query, "q1_buyout_plan_rub"),
                        q2_buyout_plan_rub=_resolve_optional_query_float(parsed.query, "q2_buyout_plan_rub"),
                        q3_buyout_plan_rub=_resolve_optional_query_float(parsed.query, "q3_buyout_plan_rub"),
                        q4_buyout_plan_rub=_resolve_optional_query_float(parsed.query, "q4_buyout_plan_rub"),
                        as_of_date=_resolve_as_of_date_from_query(parsed.query) or None,
                        use_contract_start_date=_resolve_optional_query_bool(parsed.query, "use_contract_start_date"),
                        contract_start_date=_resolve_single_query_param(parsed.query, "contract_start_date") or None,
                        annual_plan_evenly_distributed=_resolve_optional_query_bool(
                            parsed.query, "annual_plan_evenly_distributed"
                        ),
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina plan report runtime failed: {exc}"},
                    )
                    return

                _write_json_response(
                    self,
                    HTTPStatus.OK,
                    payload,
                )
                return

            if parsed.path == DEFAULT_SHEET_PLAN_REPORT_BASELINE_STATUS_PATH:
                try:
                    payload = entrypoint.handle_sheet_plan_report_baseline_status_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina plan report baseline status failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SHEET_PLAN_REPORT_BASELINE_TEMPLATE_PATH:
                try:
                    workbook_bytes, filename = entrypoint.handle_sheet_plan_report_baseline_template_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina plan report baseline template failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    workbook_bytes,
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    filename=filename,
                    as_attachment=True,
                )
                return

            if parsed.path == DEFAULT_SUPPLIER_SHIPMENTS_PATH:
                try:
                    payload = entrypoint.handle_supplier_shipments_list_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier shipments list failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_PATH:
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    payload = entrypoint.handle_supplier_shipment_registry_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier shipment registry failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_CNY_ACCOUNT_PATH:
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    payload = entrypoint.handle_cny_account_status_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"CNY account status failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_CNY_ACCOUNT_CONVERSIONS_PATH:
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    payload = entrypoint.handle_cny_account_conversions_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"CNY account conversions failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_CNY_ACCOUNT_LEDGER_PATH:
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    payload = entrypoint.handle_cny_account_ledger_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"CNY account ledger failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_cny_account_document_file_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    document_id = _resolve_cny_account_document_id(parsed.path)
                    file_bytes, filename, content_type = entrypoint.handle_cny_account_document_file_request(document_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"CNY account document download failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    file_bytes,
                    content_type=content_type,
                    filename=filename,
                    as_attachment=True,
                )
                return

            if parsed.path == DEFAULT_WB_SUPPLIES_PATH:
                try:
                    payload = entrypoint.handle_wb_supplies_list_request(_flatten_query_params(parsed.query))
                except WbSuppliesBlockError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc), "contract_name": "sheet_vitrina_v1_wb_supplies"},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"WB supplies list failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_WB_SUPPLIES_SYNC_STATUS_PATH:
                try:
                    payload = entrypoint.handle_wb_supplies_sync_status_request(_flatten_query_params(parsed.query))
                except WbSuppliesBlockError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc), "contract_name": "sheet_vitrina_v1_wb_supplies"},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"WB supplies sync status failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_WB_SUPPLIES_TRANSIT_COST_STATUS_PATH:
                try:
                    payload = entrypoint.handle_wb_supplies_transit_cost_status_request(_flatten_query_params(parsed.query))
                except WbSuppliesBlockError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc), "contract_name": "sheet_vitrina_v1_wb_supplies"},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"WB transit cost enrichment status failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_WB_SUPPLIES_OVERLAY_OPTIONS_PATH:
                try:
                    payload = entrypoint.handle_wb_supplies_overlay_options_request()
                except WbSuppliesBlockError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc), "contract_name": "sheet_vitrina_v1_wb_supplies"},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"WB supplies overlay options failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_wb_supply_detail_path(parsed.path):
                try:
                    supply_id = _resolve_wb_supply_id_from_detail_path(parsed.path)
                    payload = entrypoint.handle_wb_supplies_detail_request(supply_id)
                except WbSuppliesBlockError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus(exc.http_status),
                        {"error": str(exc), "contract_name": "sheet_vitrina_v1_wb_supplies"},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"WB supply detail failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_NOMENCLATURE_EXPORT_PATH:
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    workbook_bytes, filename, content_type = entrypoint.handle_nomenclature_export_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"nomenclature export failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    workbook_bytes,
                    content_type=content_type,
                    filename=filename,
                    as_attachment=True,
                )
                return

            if parsed.path == DEFAULT_NOMENCLATURE_PATH:
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    payload = entrypoint.handle_nomenclature_list_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"nomenclature list failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_TRADE_DOCUMENTS_PATH:
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    payload = entrypoint.handle_trade_documents_list_request()
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"trade documents list failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_supplier_order_documents_archive_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id, package_kind = _resolve_supplier_order_documents_archive_ids(parsed.path)
                    archive_bytes, filename = entrypoint.handle_supplier_order_documents_archive_request(
                        shipment_id,
                        package_kind=package_kind,
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier order documents archive failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    archive_bytes,
                    content_type="application/zip",
                    filename=filename,
                    as_attachment=True,
                )
                return

            if _is_supplier_order_documents_collection_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id = _resolve_supplier_order_documents_shipment_id(parsed.path)
                    payload = entrypoint.handle_supplier_order_documents_list_request(shipment_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier order documents list failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_supplier_financial_documents_collection_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id = _resolve_supplier_financial_shipment_id(parsed.path)
                    payload = entrypoint.handle_supplier_financial_documents_list_request(shipment_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier financial documents list failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_supplier_financial_document_file_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id, document_id = _resolve_supplier_financial_document_ids(parsed.path)
                    file_bytes, filename, content_type = entrypoint.handle_supplier_financial_document_file_request(
                        shipment_id,
                        document_id,
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier financial document download failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    file_bytes,
                    content_type=content_type,
                    filename=filename,
                    as_attachment=True,
                )
                return

            if _is_supplier_financial_document_detail_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id, document_id = _resolve_supplier_financial_document_ids(parsed.path)
                    payload = entrypoint.handle_supplier_financial_document_detail_request(shipment_id, document_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier financial document detail failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_trade_document_file_path(parsed.path):
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    document_id = _resolve_trade_document_id(parsed.path)
                    file_bytes, filename, content_type = entrypoint.handle_trade_documents_file_request(document_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"trade document download failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    file_bytes,
                    content_type=content_type,
                    filename=filename,
                    as_attachment=True,
                )
                return

            if _is_supplier_shipment_invoice_path(parsed.path):
                try:
                    shipment_id = _resolve_supplier_shipment_id_from_invoice_path(parsed.path)
                    workbook_bytes, filename, content_type = entrypoint.handle_supplier_shipments_invoice_request(
                        shipment_id
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier invoice download failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    workbook_bytes,
                    content_type=content_type,
                    filename=filename,
                    as_attachment=True,
                )
                return

            if _is_supplier_shipment_contract_path(parsed.path):
                try:
                    shipment_id = _resolve_supplier_shipment_id_from_contract_path(parsed.path)
                    file_bytes, filename, content_type = entrypoint.handle_supplier_shipments_contract_request(shipment_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier contract download failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    file_bytes,
                    content_type=content_type,
                    filename=filename,
                    as_attachment=True,
                )
                return

            if _is_supplier_shipment_detail_path(parsed.path):
                try:
                    shipment_id = _resolve_supplier_shipment_id_from_detail_path(parsed.path)
                    payload = entrypoint.handle_supplier_shipments_detail_request(shipment_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier shipment detail failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_FACTORY_ORDER_STATUS_PATH:
                try:
                    payload = entrypoint.handle_factory_order_status_request()
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"factory order status runtime failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, _with_factory_order_dataset_urls(payload))
                return

            if parsed.path == DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_CHECK_PATH:
                try:
                    payload = entrypoint.handle_factory_order_stock_ff_onec_check_request()
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"factory order 1C stock FF check failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if parsed.path == DEFAULT_WB_REGIONAL_STATUS_PATH:
                try:
                    payload = entrypoint.handle_wb_regional_status_request()
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"wb regional supply status runtime failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, _with_wb_regional_urls(payload))
                return

            if parsed.path == DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_XLSX_PATH:
                try:
                    workbook_bytes, filename = entrypoint.handle_factory_order_stock_ff_onec_xlsx_request()
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"factory order 1C stock FF XLSX failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    workbook_bytes,
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    filename=filename,
                    as_attachment=True,
                )
                return

            if parsed.path in {
                DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH,
                DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH,
                DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH,
            }:
                try:
                    dataset_type = _resolve_factory_order_dataset_type_from_template_path(parsed.path)
                    workbook_bytes, filename = entrypoint.handle_factory_order_template_request(dataset_type)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"factory order template runtime failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    workbook_bytes,
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    filename=filename,
                    as_attachment=True,
                )
                return

            if parsed.path == DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH:
                try:
                    workbook_bytes, filename = entrypoint.handle_factory_order_recommendation_request()
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"factory order recommendation runtime failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    workbook_bytes,
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    filename=filename,
                    as_attachment=True,
                )
                return

            if parsed.path == DEFAULT_WB_REGIONAL_RECOMMENDATIONS_ZIP_PATH:
                try:
                    archive_bytes, filename = entrypoint.handle_wb_regional_recommendations_zip_request()
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"wb regional supply recommendations ZIP failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    archive_bytes,
                    content_type="application/zip",
                    filename=filename,
                    as_attachment=True,
                )
                return

            if (
                parsed.path.startswith(DEFAULT_WB_REGIONAL_DISTRICT_DOWNLOAD_PREFIX + "/")
                and parsed.path.endswith(".xlsx")
            ):
                try:
                    district_key = _resolve_wb_regional_district_from_download_path(parsed.path)
                    workbook_bytes, filename = entrypoint.handle_wb_regional_district_recommendation_request(
                        district_key
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"wb regional supply district runtime failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    workbook_bytes,
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    filename=filename,
                    as_attachment=True,
                )
                return

            if parsed.path in {
                DEFAULT_FACTORY_ORDER_UPLOADED_STOCK_FF_PATH,
                DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FACTORY_PATH,
                DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FF_TO_WB_PATH,
            }:
                try:
                    dataset_type = _resolve_factory_order_dataset_type_from_uploaded_path(parsed.path)
                    workbook_bytes, filename, content_type = entrypoint.handle_factory_order_uploaded_file_request(
                        dataset_type
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"factory order uploaded file runtime failed: {exc}"},
                    )
                    return
                _write_binary_response(
                    self,
                    HTTPStatus.OK,
                    workbook_bytes,
                    content_type=content_type,
                    filename=filename,
                    as_attachment=True,
                )
                return

            if parsed.path == sheet_job_path:
                try:
                    job_id = _resolve_job_id_from_query(parsed.query)
                    response_format = _resolve_job_response_format(parsed.query)
                    if response_format == "text":
                        body_text, filename = entrypoint.handle_sheet_operator_job_text_request(job_id)
                    else:
                        payload = entrypoint.handle_sheet_operator_job_request(job_id)
                except ValueError as exc:
                    status = (
                        HTTPStatus.NOT_FOUND
                        if "operator job not found" in str(exc)
                        else HTTPStatus.BAD_REQUEST
                    )
                    _write_json_response(
                        self,
                        status,
                        {"error": str(exc)},
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina job runtime failed: {exc}"},
                    )
                    return

                if response_format == "text":
                    _write_text_response(
                        self,
                        HTTPStatus.OK,
                        body_text,
                        filename=filename,
                        as_attachment=_resolve_download_requested(parsed.query),
                    )
                else:
                    _write_json_response(
                        self,
                        HTTPStatus.OK,
                        _with_sheet_job_urls(payload, sheet_job_path),
                    )
                return

            if parsed.path == sheet_status_path:
                try:
                    payload = entrypoint.handle_sheet_status_request(
                        as_of_date=_resolve_as_of_date_from_query(parsed.query) or None
                    )
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {
                            "error": str(exc),
                            "server_context": entrypoint.build_sheet_server_context(),
                            "manual_context": entrypoint.build_sheet_manual_context(),
                            "load_context": entrypoint.build_sheet_load_context(),
                        },
                    )
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"sheet vitrina status runtime failed: {exc}"},
                    )
                    return

                _write_json_response(
                    self,
                    HTTPStatus.OK,
                    payload,
                )
                return

            if parsed.path != sheet_plan_path:
                _write_json_response(
                    self,
                    HTTPStatus.NOT_FOUND,
                    {"error": f"unsupported path: {parsed.path}"},
                )
                return

            try:
                payload = entrypoint.handle_sheet_plan_request(
                    as_of_date=_resolve_as_of_date_from_query(parsed.query) or None
                )
            except ValueError as exc:
                _write_json_response(
                    self,
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"error": str(exc)},
                )
                return
            except Exception as exc:  # pragma: no cover - bounded fallback
                _write_json_response(
                    self,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"sheet vitrina plan runtime failed: {exc}"},
                )
                return

            _write_json_response(
                self,
                HTTPStatus.OK,
                payload,
            )
            return

        def do_PATCH(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
            if not _ensure_web_auth(self, parsed):
                return
            if _is_settings_user_item_path(parsed.path):
                _handle_settings_user_patch(self, entrypoint, _resolve_settings_user_id(parsed.path))
                return
            if _is_supplier_shipment_contract_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id = _resolve_supplier_shipment_id_from_contract_path(parsed.path)
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_supplier_shipments_contract_patch_request(
                        shipment_id,
                        payload,
                        actor=_current_web_user_config_key(self),
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier shipment contract patch failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if _is_trade_document_contract_link_path(parsed.path):
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    invoice_document_id = _resolve_trade_document_id(parsed.path)
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_trade_documents_contract_patch_request(
                        invoice_document_id,
                        payload,
                        actor=_current_web_user_config_key(self),
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"trade document contract link failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if _is_trade_document_item_path(parsed.path):
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    document_id = _resolve_trade_document_id(parsed.path)
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_trade_documents_patch_request(document_id, payload)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"trade document patch failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if _is_supplier_financial_document_detail_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id, document_id = _resolve_supplier_financial_document_ids(parsed.path)
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_supplier_financial_document_patch_request(
                        shipment_id,
                        document_id,
                        payload,
                    )
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier financial document patch failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if _is_supplier_shipment_detail_path(parsed.path):
                try:
                    shipment_id = _resolve_supplier_shipment_id_from_detail_path(parsed.path)
                    payload = _load_request_payload(self)
                    if "contract_document_id" in payload and not _ensure_supply_operator_role(self, parsed.path):
                        return
                    if "order_status" in payload:
                        if not _ensure_supply_operator_role(self, parsed.path):
                            return
                        if _is_supplier_order_status_only_payload(payload):
                            result = entrypoint.handle_supplier_shipments_order_status_patch_request(shipment_id, payload)
                        else:
                            result = entrypoint.handle_supplier_shipments_patch_request(shipment_id, payload)
                    else:
                        result = entrypoint.handle_supplier_shipments_patch_request(shipment_id, payload)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier shipment patch failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            if _is_nomenclature_item_path(parsed.path):
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    item_id = _resolve_nomenclature_item_id(parsed.path)
                    payload = _load_request_payload(self)
                    result = entrypoint.handle_nomenclature_patch_request(item_id, payload)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"nomenclature patch failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, result)
                return

            _write_json_response(
                self,
                HTTPStatus.NOT_FOUND,
                {"error": f"unsupported path: {parsed.path}"},
            )
            return

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
            if not _ensure_web_auth(self, parsed):
                return
            if _is_settings_user_item_path(parsed.path):
                _handle_settings_user_delete(self, entrypoint, _resolve_settings_user_id(parsed.path))
                return
            if parsed.path in {
                DEFAULT_FACTORY_ORDER_DELETE_STOCK_FF_PATH,
                DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FACTORY_PATH,
                DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FF_TO_WB_PATH,
            }:
                try:
                    dataset_type = _resolve_factory_order_dataset_type_from_delete_path(parsed.path)
                    payload = entrypoint.handle_factory_order_delete_request(dataset_type)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"factory order delete runtime failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_supplier_shipment_detail_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id = _resolve_supplier_shipment_id_from_detail_path(parsed.path)
                    payload = entrypoint.handle_supplier_shipments_delete_request(shipment_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier shipment delete failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_supplier_financial_document_detail_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    shipment_id, document_id = _resolve_supplier_financial_document_ids(parsed.path)
                    payload = entrypoint.handle_supplier_financial_document_delete_request(shipment_id, document_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"supplier financial document delete failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_cny_account_document_detail_path(parsed.path):
                if not _ensure_supply_operator_role(self, parsed.path):
                    return
                try:
                    document_id = _resolve_cny_account_document_id(parsed.path)
                    payload = entrypoint.handle_cny_account_document_delete_request(document_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"CNY account document delete failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_trade_document_contract_link_path(parsed.path):
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    invoice_document_id = _resolve_trade_document_id(parsed.path)
                    payload = entrypoint.handle_trade_documents_contract_delete_request(invoice_document_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"trade document contract unlink failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_trade_document_item_path(parsed.path):
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    document_id = _resolve_trade_document_id(parsed.path)
                    payload = entrypoint.handle_trade_documents_archive_request(document_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"trade document archive failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            if _is_nomenclature_item_path(parsed.path):
                if not _ensure_operator_role(self, parsed.path):
                    return
                try:
                    item_id = _resolve_nomenclature_item_id(parsed.path)
                    payload = entrypoint.handle_nomenclature_delete_request(item_id)
                except ValueError as exc:
                    _write_json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - bounded fallback
                    _write_json_response(
                        self,
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"nomenclature delete failed: {exc}"},
                    )
                    return
                _write_json_response(self, HTTPStatus.OK, payload)
                return

            _write_json_response(
                self,
                HTTPStatus.NOT_FOUND,
                {"error": f"unsupported path: {parsed.path}"},
            )
            return

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    return RegistryUploadHandler


class RegistryUploadHttpServer(HTTPServer):
    """Минимальный HTTP server без reverse-DNS lookup на bind."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def _load_request_payload(handler: BaseHTTPRequestHandler) -> Mapping[str, Any]:
    raw_length = handler.headers.get("Content-Length", "").strip()
    if not raw_length:
        raise ValueError("request body is required")

    try:
        content_length = int(raw_length)
    except ValueError as exc:
        raise ValueError(f"Content-Length must be integer, got {raw_length!r}") from exc
    if content_length <= 0:
        raise ValueError("request body must not be empty")

    raw_body = handler.rfile.read(content_length)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be valid UTF-8 JSON") from exc

    if not isinstance(payload, Mapping):
        raise ValueError("request body must be a JSON object")
    return payload


def _load_optional_request_payload(handler: BaseHTTPRequestHandler) -> Mapping[str, Any]:
    raw_length = handler.headers.get("Content-Length", "").strip()
    if not raw_length:
        return {}

    try:
        content_length = int(raw_length)
    except ValueError as exc:
        raise ValueError(f"Content-Length must be integer, got {raw_length!r}") from exc
    if content_length < 0:
        raise ValueError("Content-Length must not be negative")
    if content_length == 0:
        return {}

    raw_body = handler.rfile.read(content_length)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("request body must be a JSON object")
    return payload


def _load_uploaded_file_payload(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_type = str(handler.headers.get("Content-Type", "") or "")
    if "multipart/form-data" not in content_type.lower():
        raise ValueError("file upload must use multipart/form-data")
    raw_length = handler.headers.get("Content-Length", "").strip()
    if not raw_length:
        raise ValueError("uploaded file request body is required")
    try:
        content_length = int(raw_length)
    except ValueError as exc:
        raise ValueError(f"Content-Length must be integer, got {raw_length!r}") from exc
    if content_length <= 0:
        raise ValueError("uploaded file request body must not be empty")
    raw_body = handler.rfile.read(content_length)
    message = BytesParser(policy=default_email_policy).parsebytes(
        (
            f"Content-Type: {content_type}\r\n"
            "MIME-Version: 1.0\r\n"
            "\r\n"
        ).encode("utf-8")
        + raw_body
    )
    workbook_bytes = b""
    filename = ""
    part_content_type = ""
    fields: dict[str, str] = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        part_name = str(part.get_param("name", header="Content-Disposition") or "").strip()
        if part_name != "file":
            if part_name:
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    fields[part_name] = payload.decode(charset, errors="replace").strip()
            continue
        payload = part.get_payload(decode=True)
        if payload and not workbook_bytes:
            workbook_bytes = payload
            filename = str(part.get_filename() or "").strip()
            part_content_type = str(part.get_content_type() or "").strip()
    if not workbook_bytes:
        raise ValueError("multipart/form-data must contain non-empty file field")
    return {
        "workbook_bytes": workbook_bytes,
        "filename": filename,
        "content_type": part_content_type,
        "fields": fields,
    }


def _resolve_as_of_date(query_string: str, payload: Mapping[str, Any]) -> str:
    query_value = _resolve_as_of_date_from_query(query_string)
    body_value = str(payload.get("as_of_date", "") or "").strip()
    if query_value and body_value and query_value != body_value:
        raise ValueError("as_of_date mismatch between query string and request body")
    return query_value or body_value


def _resolve_as_of_date_from_query(query_string: str) -> str:
    query = urllib_parse.parse_qs(query_string)
    return str(query.get("as_of_date", [""])[0]).strip()


def _resolve_web_vitrina_as_of_date_from_query(
    query_string: str,
    *,
    surface: str,
    include_source_status: bool,
) -> str | None:
    value = _resolve_as_of_date_from_query(query_string) or None
    if not value:
        return None
    if _web_vitrina_history_mode_is_explicit(query_string):
        return value
    if _web_vitrina_query_has_history_mode(query_string):
        return None
    if surface != DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE:
        return value
    if include_source_status and not _web_vitrina_query_has_period_window(query_string):
        return value
    return None


def _resolve_single_query_param(query_string: str, name: str) -> str:
    query = urllib_parse.parse_qs(query_string)
    return str(query.get(name, [""])[0]).strip()


def _resolve_source_group_id(query_string: str, payload: Mapping[str, Any]) -> str:
    query_value = _resolve_single_query_param(query_string, "source_group_id")
    body_value = str(payload.get("source_group_id", "") or "").strip()
    if query_value and body_value and query_value != body_value:
        raise ValueError("source_group_id mismatch between query string and request body")
    value = query_value or body_value
    if not value:
        raise ValueError("source_group_id is required")
    return value


def _resolve_required_query_value(query_string: str, name: str) -> str:
    value = _resolve_single_query_param(query_string, name)
    if not value:
        raise ValueError(f"{name} query parameter is required")
    return value


def _resolve_required_query_float(query_string: str, name: str) -> float:
    value = _resolve_required_query_value(query_string, name)
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} query parameter must be numeric") from exc


def _resolve_optional_query_float(query_string: str, name: str) -> float | None:
    value = _resolve_single_query_param(query_string, name)
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} query parameter must be numeric") from exc


def _flatten_query_params(query_string: str) -> dict[str, Any]:
    query = urllib_parse.parse_qs(query_string or "", keep_blank_values=False)
    flattened: dict[str, Any] = {}
    for key, values in query.items():
        if values:
            flattened[key] = values if len(values) > 1 else values[-1]
    return flattened


def _resolve_sheet_ads_sku_nm_id(path: str) -> int:
    prefix = DEFAULT_SHEET_ADS_SKU_PREFIX + "/"
    if not path.startswith(prefix):
        raise ValueError("unsupported ads sku path")
    remainder = path[len(prefix) :].strip("/")
    if not remainder or "/" in remainder:
        raise ValueError("ads sku path must end with nm_id")
    try:
        nm_id = int(remainder)
    except ValueError as exc:
        raise ValueError("ads sku nm_id must be numeric") from exc
    if nm_id <= 0:
        raise ValueError("ads sku nm_id must be positive")
    return nm_id


def _resolve_optional_query_bool(query_string: str, name: str) -> bool:
    value = _resolve_single_query_param(query_string, name).lower()
    if not value:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} query parameter must be true or false")


def _resolve_query_bool_default_true(query_string: str, name: str) -> bool:
    value = _resolve_single_query_param(query_string, name).lower()
    if not value:
        return True
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} query parameter must be true or false")


def _resolve_sales_avg_period_days_from_query(query_string: str) -> int:
    return parse_sales_avg_period_days(_resolve_single_query_param(query_string, "sales_avg_period_days"))


def _web_vitrina_history_mode_is_explicit(query_string: str) -> bool:
    query = urllib_parse.parse_qs(query_string, keep_blank_values=False)
    values = query.get("history_mode") or []
    return len(values) == 1 and str(values[0]).strip() == WEB_VITRINA_HISTORY_MODE_EXPLICIT


def _web_vitrina_query_has_history_mode(query_string: str) -> bool:
    query = urllib_parse.parse_qs(query_string, keep_blank_values=False)
    return bool(query.get("history_mode"))


def _web_vitrina_query_has_period_window(query_string: str) -> bool:
    query = urllib_parse.parse_qs(query_string, keep_blank_values=False)
    return bool(str(query.get("date_from", [""])[0]).strip() or str(query.get("date_to", [""])[0]).strip())


def _resolve_web_vitrina_period_window_from_query(query_string: str) -> tuple[str | None, str | None]:
    query = urllib_parse.parse_qs(query_string)
    date_from = str(query.get("date_from", [""])[0]).strip()
    date_to = str(query.get("date_to", [""])[0]).strip()
    if not _web_vitrina_history_mode_is_explicit(query_string):
        return None, None
    if bool(date_from) != bool(date_to):
        raise ValueError("date_from and date_to must be provided together")
    if date_from and str(query.get("as_of_date", [""])[0]).strip():
        raise ValueError("as_of_date is mutually exclusive with date_from/date_to")
    if not date_from:
        return None, None
    try:
        parsed_from = date.fromisoformat(date_from)
        parsed_to = date.fromisoformat(date_to)
    except ValueError as exc:
        raise ValueError("date_from and date_to must use YYYY-MM-DD") from exc
    if parsed_to < parsed_from:
        raise ValueError("date_to must be >= date_from")
    return date_from, date_to


def _resolve_feedbacks_query(query_string: str) -> dict[str, Any]:
    query = urllib_parse.parse_qs(query_string, keep_blank_values=False)
    date_from = str(query.get("date_from", [""])[0]).strip() or None
    date_to = str(query.get("date_to", [""])[0]).strip() or None
    is_answered = str(query.get("is_answered", ["all"])[0]).strip() or "all"
    raw_stars = str(query.get("stars", [""])[0]).strip()
    stars: list[int] | None = None
    if raw_stars:
        stars = []
        for raw_value in raw_stars.split(","):
            value = raw_value.strip()
            if not value:
                continue
            try:
                stars.append(int(value))
            except ValueError as exc:
                raise ValueError("stars query parameter must be a comma-separated list of integers") from exc
    return {
        "date_from": date_from,
        "date_to": date_to,
        "stars": stars,
        "is_answered": is_answered,
    }


def _resolve_job_id_from_query(query_string: str) -> str:
    query = urllib_parse.parse_qs(query_string)
    job_id = str(query.get("job_id", [""])[0]).strip()
    if not job_id:
        raise ValueError("job_id query parameter is required")
    return job_id


def _resolve_job_response_format(query_string: str) -> str:
    query = urllib_parse.parse_qs(query_string)
    value = str(query.get("format", ["json"])[0] or "json").strip().lower()
    if value not in {"json", "text"}:
        raise ValueError("format query parameter must be json or text")
    return value


def _resolve_download_requested(query_string: str) -> bool:
    query = urllib_parse.parse_qs(query_string)
    value = str(query.get("download", ["0"])[0] or "0").strip().lower()
    return value in {"1", "true", "yes"}


def _resolve_async_requested(payload: Mapping[str, Any]) -> bool:
    if "async" in payload:
        raw = payload["async"]
        if not isinstance(raw, bool):
            raise ValueError("async must be boolean when provided")
        return raw

    if "wait" in payload:
        raw = payload["wait"]
        if not isinstance(raw, bool):
            raise ValueError("wait must be boolean when provided")
        return not raw

    return False


def _resolve_auto_load_requested(payload: Mapping[str, Any]) -> bool:
    if "auto_load" not in payload:
        return False
    raw = payload["auto_load"]
    if not isinstance(raw, bool):
        raise ValueError("auto_load must be boolean when provided")
    if raw:
        raise ValueError("auto_load targets the archived legacy Google Sheets contour; use refresh only")
    return raw


def _resolve_auto_refresh_requested(payload: Mapping[str, Any]) -> bool:
    if "auto_refresh" not in payload:
        return False
    raw = payload["auto_refresh"]
    if not isinstance(raw, bool):
        raise ValueError("auto_refresh must be boolean when provided")
    return raw


def _resolve_auto_schedule_id(payload: Mapping[str, Any]) -> str:
    if "schedule_id" not in payload:
        return ""
    raw = payload["schedule_id"]
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("schedule_id must be a string when provided")
    return raw.strip()


def _resolve_auto_schedule_due_at(payload: Mapping[str, Any]) -> str:
    if "due_at" not in payload:
        return ""
    raw = payload["due_at"]
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("due_at must be a string when provided")
    return raw.strip()


def _resolve_auto_trigger_source(payload: Mapping[str, Any]) -> str:
    if "trigger_source" not in payload:
        return ""
    raw = payload["trigger_source"]
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("trigger_source must be a string when provided")
    return raw.strip()[:80]


def _resolve_replace_requested(payload: Mapping[str, Any]) -> bool:
    if "replace" not in payload:
        return True
    raw = payload["replace"]
    if not isinstance(raw, bool):
        raise ValueError("replace must be boolean when provided")
    return raw


def _resolve_factory_order_dataset_type_from_template_path(path: str) -> str:
    mapping = {
        DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH: DATASET_STOCK_FF,
        DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH: DATASET_INBOUND_FACTORY_TO_FF,
        DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH: DATASET_INBOUND_FF_TO_WB,
    }
    dataset_type = mapping.get(path, "")
    if not dataset_type:
        raise ValueError(f"unsupported factory-order template path: {path}")
    return dataset_type


def _resolve_factory_order_dataset_type_from_upload_path(path: str) -> str:
    mapping = {
        DEFAULT_FACTORY_ORDER_UPLOAD_STOCK_FF_PATH: DATASET_STOCK_FF,
        DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FACTORY_PATH: DATASET_INBOUND_FACTORY_TO_FF,
        DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FF_TO_WB_PATH: DATASET_INBOUND_FF_TO_WB,
    }
    dataset_type = mapping.get(path, "")
    if not dataset_type:
        raise ValueError(f"unsupported factory-order upload path: {path}")
    return dataset_type


def _resolve_factory_order_dataset_type_from_uploaded_path(path: str) -> str:
    mapping = {
        DEFAULT_FACTORY_ORDER_UPLOADED_STOCK_FF_PATH: DATASET_STOCK_FF,
        DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FACTORY_PATH: DATASET_INBOUND_FACTORY_TO_FF,
        DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FF_TO_WB_PATH: DATASET_INBOUND_FF_TO_WB,
    }
    dataset_type = mapping.get(path, "")
    if not dataset_type:
        raise ValueError(f"unsupported factory-order uploaded-file path: {path}")
    return dataset_type


def _resolve_factory_order_dataset_type_from_delete_path(path: str) -> str:
    mapping = {
        DEFAULT_FACTORY_ORDER_DELETE_STOCK_FF_PATH: DATASET_STOCK_FF,
        DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FACTORY_PATH: DATASET_INBOUND_FACTORY_TO_FF,
        DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FF_TO_WB_PATH: DATASET_INBOUND_FF_TO_WB,
    }
    dataset_type = mapping.get(path, "")
    if not dataset_type:
        raise ValueError(f"unsupported factory-order delete path: {path}")
    return dataset_type


def _is_supplier_shipment_detail_path(path: str) -> bool:
    if not path.startswith(DEFAULT_SUPPLIER_SHIPMENTS_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_SUPPLIER_SHIPMENTS_PATH) + 1 :]
    return bool(suffix) and "/" not in suffix and suffix != "parse"


def _is_supplier_shipment_invoice_path(path: str) -> bool:
    if not path.startswith(DEFAULT_SUPPLIER_SHIPMENTS_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_SUPPLIER_SHIPMENTS_PATH) + 1 :]
    parts = suffix.split("/")
    return len(parts) == 2 and bool(parts[0]) and parts[1] == "invoice"


def _is_supplier_shipment_contract_path(path: str) -> bool:
    if not path.startswith(DEFAULT_SUPPLIER_SHIPMENTS_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_SUPPLIER_SHIPMENTS_PATH) + 1 :]
    parts = suffix.split("/")
    return len(parts) == 2 and bool(parts[0]) and parts[1] == "contract"


def _is_supplier_shipment_rematch_path(path: str) -> bool:
    if not path.startswith(DEFAULT_SUPPLIER_SHIPMENTS_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_SUPPLIER_SHIPMENTS_PATH) + 1 :]
    parts = suffix.split("/")
    return len(parts) == 2 and bool(parts[0]) and parts[1] == "rematch"


def _is_supplier_shipment_price_check_path(path: str) -> bool:
    if not path.startswith(DEFAULT_SUPPLIER_SHIPMENTS_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_SUPPLIER_SHIPMENTS_PATH) + 1 :]
    parts = suffix.split("/")
    return len(parts) == 2 and bool(parts[0]) and parts[1] == "price-check"


def _supplier_order_documents_path_parts(path: str) -> list[str]:
    if not path.startswith(DEFAULT_SUPPLIER_SHIPMENTS_PATH + "/"):
        return []
    suffix = path[len(DEFAULT_SUPPLIER_SHIPMENTS_PATH) + 1 :]
    return suffix.split("/") if suffix else []


def _is_supplier_order_documents_collection_path(path: str) -> bool:
    parts = _supplier_order_documents_path_parts(path)
    return len(parts) == 2 and bool(parts[0]) and parts[1] == DEFAULT_SUPPLIER_ORDER_DOCUMENTS_SEGMENT


def _is_supplier_order_documents_archive_path(path: str) -> bool:
    parts = _supplier_order_documents_path_parts(path)
    return (
        len(parts) == 3
        and bool(parts[0])
        and parts[1] == DEFAULT_SUPPLIER_ORDER_DOCUMENTS_SEGMENT
        and parts[2] in {"archive.zip", "logistics-package.zip"}
    )


def _supplier_financial_path_parts(path: str) -> list[str]:
    if not path.startswith(DEFAULT_SUPPLIER_SHIPMENTS_PATH + "/"):
        return []
    suffix = path[len(DEFAULT_SUPPLIER_SHIPMENTS_PATH) + 1 :]
    return suffix.split("/") if suffix else []


def _is_supplier_financial_documents_collection_path(path: str) -> bool:
    parts = _supplier_financial_path_parts(path)
    return len(parts) == 2 and bool(parts[0]) and parts[1] == DEFAULT_SUPPLIER_FINANCIAL_DOCUMENTS_SEGMENT


def _is_supplier_financial_document_detail_path(path: str) -> bool:
    parts = _supplier_financial_path_parts(path)
    return (
        len(parts) == 3
        and bool(parts[0])
        and parts[1] == DEFAULT_SUPPLIER_FINANCIAL_DOCUMENTS_SEGMENT
        and bool(parts[2])
    )


def _is_supplier_financial_document_file_path(path: str) -> bool:
    parts = _supplier_financial_path_parts(path)
    return (
        len(parts) == 4
        and bool(parts[0])
        and parts[1] == DEFAULT_SUPPLIER_FINANCIAL_DOCUMENTS_SEGMENT
        and bool(parts[2])
        and parts[3] == "file"
    )


def _is_supplier_financial_document_confirm_import_path(path: str) -> bool:
    parts = _supplier_financial_path_parts(path)
    return (
        len(parts) == 4
        and bool(parts[0])
        and parts[1] == DEFAULT_SUPPLIER_FINANCIAL_DOCUMENTS_SEGMENT
        and bool(parts[2])
        and parts[3] == "confirm-import"
    )


def _is_wb_supply_detail_path(path: str) -> bool:
    if not path.startswith(DEFAULT_WB_SUPPLIES_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_WB_SUPPLIES_PATH) + 1 :]
    return bool(suffix) and "/" not in suffix and suffix != "sync"


def _is_nomenclature_item_path(path: str) -> bool:
    if not path.startswith(DEFAULT_NOMENCLATURE_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_NOMENCLATURE_PATH) + 1 :]
    return bool(suffix) and "/" not in suffix


def _is_nomenclature_item_barcode_sync_path(path: str) -> bool:
    if not path.startswith(DEFAULT_NOMENCLATURE_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_NOMENCLATURE_PATH) + 1 :]
    parts = [part for part in suffix.split("/") if part]
    return len(parts) == 2 and parts[1] == "barcode-sync" and parts[0] != "barcode-sync"


def _is_trade_document_item_path(path: str) -> bool:
    if not path.startswith(DEFAULT_TRADE_DOCUMENTS_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_TRADE_DOCUMENTS_PATH) + 1 :]
    return bool(suffix) and "/" not in suffix


def _is_trade_document_file_path(path: str) -> bool:
    if not path.startswith(DEFAULT_TRADE_DOCUMENTS_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_TRADE_DOCUMENTS_PATH) + 1 :]
    parts = suffix.split("/")
    return len(parts) == 2 and bool(parts[0]) and parts[1] == "file"


def _is_trade_document_contract_link_path(path: str) -> bool:
    if not path.startswith(DEFAULT_TRADE_DOCUMENTS_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_TRADE_DOCUMENTS_PATH) + 1 :]
    parts = suffix.split("/")
    return len(parts) == 2 and bool(parts[0]) and parts[1] == "contract"


def _is_cny_account_document_file_path(path: str) -> bool:
    if not path.startswith(DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH) + 1 :]
    parts = suffix.split("/")
    return len(parts) == 2 and bool(parts[0]) and parts[1] == "file"


def _is_cny_account_document_detail_path(path: str) -> bool:
    if not path.startswith(DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH) + 1 :]
    parts = suffix.split("/")
    return len(parts) == 1 and bool(parts[0])


def _is_settings_user_item_path(path: str) -> bool:
    if not path.startswith(DEFAULT_SETTINGS_USERS_PATH + "/"):
        return False
    suffix = path[len(DEFAULT_SETTINGS_USERS_PATH) + 1 :]
    return bool(suffix) and "/" not in suffix


def _is_supplier_order_status_only_payload(payload: Mapping[str, Any]) -> bool:
    return set(payload.keys()) == {"order_status"}


def _resolve_supplier_shipment_id_from_detail_path(path: str) -> str:
    if not _is_supplier_shipment_detail_path(path):
        raise ValueError(f"unsupported supplier shipment detail path: {path}")
    return path[len(DEFAULT_SUPPLIER_SHIPMENTS_PATH) + 1 :]


def _resolve_supplier_shipment_id_from_invoice_path(path: str) -> str:
    if not _is_supplier_shipment_invoice_path(path):
        raise ValueError(f"unsupported supplier shipment invoice path: {path}")
    suffix = path[len(DEFAULT_SUPPLIER_SHIPMENTS_PATH) + 1 :]
    return suffix.split("/", 1)[0]


def _resolve_supplier_shipment_id_from_contract_path(path: str) -> str:
    if not _is_supplier_shipment_contract_path(path):
        raise ValueError(f"unsupported supplier shipment contract path: {path}")
    suffix = path[len(DEFAULT_SUPPLIER_SHIPMENTS_PATH) + 1 :]
    return suffix.split("/", 1)[0]


def _resolve_supplier_shipment_id_from_rematch_path(path: str) -> str:
    if not _is_supplier_shipment_rematch_path(path):
        raise ValueError(f"unsupported supplier shipment rematch path: {path}")
    suffix = path[len(DEFAULT_SUPPLIER_SHIPMENTS_PATH) + 1 :]
    return suffix.split("/", 1)[0]


def _resolve_supplier_shipment_id_from_price_check_path(path: str) -> str:
    if not _is_supplier_shipment_price_check_path(path):
        raise ValueError(f"unsupported supplier shipment price check path: {path}")
    suffix = path[len(DEFAULT_SUPPLIER_SHIPMENTS_PATH) + 1 :]
    return suffix.split("/", 1)[0]


def _resolve_supplier_financial_shipment_id(path: str) -> str:
    if not _is_supplier_financial_documents_collection_path(path):
        raise ValueError(f"unsupported supplier financial documents path: {path}")
    return _supplier_financial_path_parts(path)[0]


def _resolve_supplier_order_documents_shipment_id(path: str) -> str:
    if not _is_supplier_order_documents_collection_path(path):
        raise ValueError(f"unsupported supplier order documents path: {path}")
    return _supplier_order_documents_path_parts(path)[0]


def _resolve_supplier_order_documents_archive_ids(path: str) -> tuple[str, str]:
    if not _is_supplier_order_documents_archive_path(path):
        raise ValueError(f"unsupported supplier order documents archive path: {path}")
    parts = _supplier_order_documents_path_parts(path)
    return parts[0], parts[2]


def _resolve_supplier_financial_document_ids(path: str) -> tuple[str, str]:
    if not (
        _is_supplier_financial_document_detail_path(path)
        or _is_supplier_financial_document_file_path(path)
        or _is_supplier_financial_document_confirm_import_path(path)
    ):
        raise ValueError(f"unsupported supplier financial document path: {path}")
    parts = _supplier_financial_path_parts(path)
    return parts[0], parts[2]


def _resolve_wb_supply_id_from_detail_path(path: str) -> str:
    if not _is_wb_supply_detail_path(path):
        raise ValueError(f"unsupported WB supply detail path: {path}")
    return urllib_parse.unquote(path[len(DEFAULT_WB_SUPPLIES_PATH) + 1 :])


def _resolve_cny_account_document_id(path: str) -> str:
    if not (_is_cny_account_document_file_path(path) or _is_cny_account_document_detail_path(path)):
        raise ValueError(f"unsupported CNY account document path: {path}")
    suffix = path[len(DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH) + 1 :]
    return urllib_parse.unquote(suffix.split("/", 1)[0])


def _resolve_nomenclature_item_id(path: str) -> str:
    if not _is_nomenclature_item_path(path):
        raise ValueError(f"unsupported nomenclature item path: {path}")
    return path[len(DEFAULT_NOMENCLATURE_PATH) + 1 :]


def _resolve_nomenclature_item_barcode_sync_id(path: str) -> str:
    if not _is_nomenclature_item_barcode_sync_path(path):
        raise ValueError(f"unsupported nomenclature barcode sync path: {path}")
    suffix = path[len(DEFAULT_NOMENCLATURE_PATH) + 1 :]
    return suffix.split("/", 1)[0]


def _resolve_trade_document_id(path: str) -> str:
    if _is_trade_document_file_path(path) or _is_trade_document_contract_link_path(path):
        suffix = path[len(DEFAULT_TRADE_DOCUMENTS_PATH) + 1 :]
        return suffix.split("/", 1)[0]
    if _is_trade_document_item_path(path):
        return path[len(DEFAULT_TRADE_DOCUMENTS_PATH) + 1 :]
    raise ValueError(f"unsupported trade document path: {path}")


def _resolve_settings_user_id(path: str) -> str:
    if not _is_settings_user_item_path(path):
        raise ValueError(f"unsupported settings user path: {path}")
    return urllib_parse.unquote(path[len(DEFAULT_SETTINGS_USERS_PATH) + 1 :])


def _resolve_wb_regional_district_from_download_path(path: str) -> str:
    prefix = DEFAULT_WB_REGIONAL_DISTRICT_DOWNLOAD_PREFIX + "/"
    if not path.startswith(prefix) or not path.endswith(".xlsx"):
        raise ValueError(f"unsupported wb-regional district path: {path}")
    district_key = path[len(prefix):-5].strip().lower()
    if not district_key:
        raise ValueError(f"unsupported wb-regional district path: {path}")
    return district_key


def _http_status_for_result(result: RegistryUploadResult) -> HTTPStatus:
    if result.status == "accepted":
        return HTTPStatus.OK

    if any("bundle_version already accepted" in error for error in result.validation_errors):
        return HTTPStatus.CONFLICT

    return HTTPStatus.UNPROCESSABLE_ENTITY


def _http_status_for_cost_price_result(result: CostPriceUploadResult) -> HTTPStatus:
    if result.status == "accepted":
        return HTTPStatus.OK

    if any("dataset_version already accepted" in error for error in result.validation_errors):
        return HTTPStatus.CONFLICT

    return HTTPStatus.UNPROCESSABLE_ENTITY


def _write_response_body(handler: BaseHTTPRequestHandler, body: bytes) -> None:
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client disconnected after headers
        return


def _write_json_response(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    payload: Any,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    handler.send_response(status.value)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    _write_response_body(handler, body)


def _request_origin(handler: BaseHTTPRequestHandler) -> str:
    forwarded_proto = str(handler.headers.get("X-Forwarded-Proto", "") or "").strip()
    forwarded_host = str(handler.headers.get("X-Forwarded-Host", "") or "").strip()
    host = forwarded_host or str(handler.headers.get("Host", "") or "").strip()
    if not host:
        server_host, server_port = handler.server.server_address[:2]
        host = f"{server_host}:{server_port}"
    scheme = forwarded_proto or ("http" if host.startswith(("127.0.0.1", "localhost")) else "https")
    return f"{scheme}://{host}"


def _write_html_response(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    body_text: str,
) -> None:
    body = body_text.encode("utf-8")
    handler.send_response(status.value)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    _write_response_body(handler, body)


def _write_text_response(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    body_text: str,
    *,
    filename: str | None = None,
    as_attachment: bool = False,
) -> None:
    body = body_text.encode("utf-8")
    handler.send_response(status.value)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    if filename:
        handler.send_header("Content-Disposition", _build_content_disposition(
            "attachment" if as_attachment else "inline",
            filename,
        ))
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    _write_response_body(handler, body)


def _write_binary_response(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    body: bytes,
    *,
    content_type: str,
    filename: str | None = None,
    as_attachment: bool = False,
) -> None:
    handler.send_response(status.value)
    handler.send_header("Content-Type", content_type)
    if filename:
        handler.send_header("Content-Disposition", _build_content_disposition(
            "attachment" if as_attachment else "inline",
            filename,
        ))
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    _write_response_body(handler, body)


def _web_auth_config() -> dict[str, Any]:
    username = str(os.environ.get("WB_CORE_WEB_AUTH_USERNAME", "") or "").strip()
    password_hash = str(os.environ.get("WB_CORE_WEB_AUTH_PASSWORD_HASH", "") or "").strip()
    supplier_username = str(os.environ.get("WB_CORE_SUPPLIER_AUTH_USERNAME", "") or "").strip()
    supplier_password_hash = str(os.environ.get("WB_CORE_SUPPLIER_AUTH_PASSWORD_HASH", "") or "").strip()
    supplier_display_name = str(os.environ.get("WB_CORE_SUPPLIER_AUTH_DISPLAY_NAME", "") or "").strip()
    session_secret = str(os.environ.get("WB_CORE_WEB_AUTH_SESSION_SECRET", "") or "").strip()
    required = _truthy_env("WB_CORE_WEB_AUTH_REQUIRED")
    max_age = _safe_positive_int(
        os.environ.get("WB_CORE_WEB_AUTH_SESSION_MAX_AGE_SECONDS"),
        WEB_AUTH_DEFAULT_MAX_AGE_SECONDS,
    )
    enabled = bool(username and password_hash and session_secret)
    supplier_enabled = bool(supplier_username and supplier_password_hash and session_secret)
    configured = enabled or not required
    return {
        "enabled": enabled,
        "configured": configured,
        "required": required,
        "username": username,
        "password_hash": password_hash,
        "operator": {
            "username": username,
            "password_hash": password_hash,
            "role": WEB_AUTH_ROLE_ADMIN,
            "display_name": username,
        },
        "supplier": {
            "enabled": supplier_enabled,
            "username": supplier_username,
            "password_hash": supplier_password_hash,
            "role": WEB_AUTH_ROLE_SUPPLIER,
            "display_name": supplier_display_name or supplier_username,
        },
        "session_secret": session_secret,
        "max_age": max_age,
    }


def _ensure_web_auth(handler: BaseHTTPRequestHandler, parsed: urllib_parse.ParseResult) -> bool:
    config = _web_auth_config()
    if not config["configured"]:
        _write_auth_setup_error(handler, parsed.path)
        return False
    if not config["enabled"]:
        return True
    user = _authenticated_web_user(handler, config)
    if user:
        if _user_can_access_path(user, parsed.path, query=parsed.query):
            return True
        _write_auth_forbidden(handler, parsed.path)
        return False
    if _is_json_route(parsed.path, handler):
        _write_json_response(
            handler,
            HTTPStatus.UNAUTHORIZED,
            {
                "error": "authentication_required",
                "login_url": DEFAULT_WEB_AUTH_LOGIN_PATH,
            },
        )
        return False
    location = DEFAULT_WEB_AUTH_LOGIN_PATH
    next_path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    if next_path and next_path != DEFAULT_WEB_AUTH_LOGIN_PATH:
        location += "?" + urllib_parse.urlencode({"next": next_path})
    _write_redirect_response(handler, HTTPStatus.SEE_OTHER, location)
    return False


def _ensure_operator_role(handler: BaseHTTPRequestHandler, path: str) -> bool:
    config = _web_auth_config()
    if not config["enabled"]:
        return True
    user = _authenticated_web_user(handler, config)
    if user and _user_can_access_path(user, path):
        return True
    _write_auth_forbidden(handler, path)
    return False


def _ensure_supply_operator_role(handler: BaseHTTPRequestHandler, path: str) -> bool:
    config = _web_auth_config()
    if not config["enabled"]:
        return True
    user = _authenticated_web_user(handler, config)
    if user and _user_has_section_access(user, WEB_AUTH_SECTION_SUPPLY):
        return True
    _write_auth_forbidden(handler, path)
    return False


def _ensure_admin_role(handler: BaseHTTPRequestHandler, path: str) -> bool:
    return _ensure_manage_users_access(handler, path)


def _ensure_manage_users_access(handler: BaseHTTPRequestHandler, path: str) -> bool:
    config = _web_auth_config()
    if not config["enabled"]:
        return True
    user = _authenticated_web_user(handler, config)
    if user and _user_can_manage_users(user):
        return True
    _write_auth_forbidden(handler, path)
    return False


def _current_web_user_role(handler: BaseHTTPRequestHandler) -> str:
    config = _web_auth_config()
    if not config["enabled"]:
        return WEB_AUTH_ROLE_ADMIN
    user = _authenticated_web_user(handler, config) or {}
    return str(user.get("role") or "").strip() or WEB_AUTH_ROLE_ADMIN


def _current_web_user_allowed_sections(handler: BaseHTTPRequestHandler) -> list[str]:
    config = _web_auth_config()
    if not config["enabled"]:
        return _default_allowed_sections_for_role(WEB_AUTH_ROLE_ADMIN)
    user = _authenticated_web_user(handler, config) or {}
    return _user_allowed_sections(user)


def _current_web_user_can_manage_users(handler: BaseHTTPRequestHandler) -> bool:
    config = _web_auth_config()
    if not config["enabled"]:
        return True
    user = _authenticated_web_user(handler, config) or {}
    return _user_can_manage_users(user)


def _current_web_user_config_key(handler: BaseHTTPRequestHandler) -> str:
    config = _web_auth_config()
    if config["enabled"]:
        user = _authenticated_web_user(handler, config) or {}
        username = str(user.get("username") or "").strip()
        role = str(user.get("role") or WEB_AUTH_ROLE_ADMIN).strip() or WEB_AUTH_ROLE_ADMIN
    else:
        username = "local_operator"
        role = WEB_AUTH_ROLE_ADMIN
    principal = f"{role}:{username or 'anonymous_operator'}"
    digest = hashlib.sha256(principal.encode("utf-8")).hexdigest()[:32]
    return f"webcore_user_{digest}"


def _handle_web_auth_login(handler: BaseHTTPRequestHandler, query: str) -> None:
    config = _web_auth_config()
    if not config["configured"]:
        _write_auth_setup_error(handler, DEFAULT_WEB_AUTH_LOGIN_PATH)
        return
    if not config["enabled"]:
        _write_redirect_response(handler, HTTPStatus.SEE_OTHER, DEFAULT_SHEET_WEB_VITRINA_UI_PATH)
        return
    try:
        payload = _load_login_payload(handler)
    except ValueError:
        _write_login_form_response(handler, query, error="Не удалось прочитать форму входа.")
        return
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    next_path = _safe_next_path(str(payload.get("next") or _resolve_single_query_param(query, "next") or DEFAULT_SHEET_WEB_VITRINA_UI_PATH))
    principal = _match_web_auth_principal(username, password, config, _handler_runtime_entrypoint(handler))
    if principal is None:
        _write_login_form_response(handler, urllib_parse.urlencode({"next": next_path}), error="Неверный логин или пароль.")
        return
    role = str(principal.get("role") or "")
    if not _user_can_access_path(principal, next_path):
        next_path = DEFAULT_SHEET_SUPPLIER_UI_PATH if role == "supplier" else DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    cookie = _build_session_cookie(
        handler,
        username,
        config,
        role=role,
        display_name=str(principal.get("display_name") or username),
    )
    _write_redirect_response(handler, HTTPStatus.SEE_OTHER, next_path, headers={"Set-Cookie": cookie})


def _handle_web_auth_logout(handler: BaseHTTPRequestHandler) -> None:
    _write_redirect_response(
        handler,
        HTTPStatus.SEE_OTHER,
        DEFAULT_WEB_AUTH_LOGIN_PATH,
        headers={"Set-Cookie": _expired_session_cookie(handler)},
    )


def _write_login_form_response(
    handler: BaseHTTPRequestHandler,
    query: str,
    *,
    error: str = "",
) -> None:
    config = _web_auth_config()
    if not config["configured"]:
        _write_auth_setup_error(handler, DEFAULT_WEB_AUTH_LOGIN_PATH)
        return
    next_path = _safe_next_path(_resolve_single_query_param(query, "next") or DEFAULT_SHEET_WEB_VITRINA_UI_PATH)
    error_markup = (
        '<p class="login-error">' + html.escape(error, quote=True) + "</p>"
        if error
        else ""
    )
    body = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Вход в WebCore</title>
  <style>
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; font-family:Inter,Arial,sans-serif; background:#f4f7fb; color:#1f2937; }}
    main {{ width:min(360px, calc(100vw - 32px)); border:1px solid #d8e0ea; border-radius:8px; background:#fff; padding:24px; box-shadow:0 12px 36px rgba(15,23,42,.08); }}
    h1 {{ margin:0 0 14px; font-size:22px; line-height:1.2; }}
    label {{ display:block; margin:12px 0 6px; font-size:12px; font-weight:800; color:#526071; }}
    input {{ box-sizing:border-box; width:100%; min-height:40px; border:1px solid #cbd5e1; border-radius:6px; padding:8px 10px; font:inherit; }}
    button {{ width:100%; min-height:40px; margin-top:16px; border:0; border-radius:6px; background:#2463eb; color:#fff; font:inherit; font-weight:800; cursor:pointer; }}
    .login-error {{ margin:0 0 12px; color:#b42318; font-size:13px; }}
  </style>
</head>
<body>
  <main>
    <h1>Вход в WebCore</h1>
    {error_markup}
    <form method="post" action="{html.escape(DEFAULT_WEB_AUTH_LOGIN_PATH, quote=True)}">
      <input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">
      <label for="username">Логин</label>
      <input id="username" name="username" autocomplete="username" autofocus required>
      <label for="password">Пароль</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">Войти</button>
    </form>
  </main>
</body>
</html>"""
    _write_html_response(handler, HTTPStatus.OK, body)


def _write_auth_setup_error(handler: BaseHTTPRequestHandler, path: str) -> None:
    if _is_json_path(path):
        _write_json_response(
            handler,
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "web auth is required but not configured"},
        )
        return
    _write_html_response(
        handler,
        HTTPStatus.SERVICE_UNAVAILABLE,
        "<!doctype html><meta charset=\"utf-8\"><title>WebCore auth</title><p>WebCore auth is required but not configured.</p>",
    )


def _write_auth_forbidden(handler: BaseHTTPRequestHandler, path: str) -> None:
    if _is_json_path(path):
        _write_json_response(
            handler,
            HTTPStatus.FORBIDDEN,
            {"error": "forbidden", "reason": "role_not_allowed_for_route"},
        )
        return
    _write_html_response(
        handler,
        HTTPStatus.FORBIDDEN,
        "<!doctype html><meta charset=\"utf-8\"><title>Forbidden</title><p>Недостаточно прав для этого раздела.</p>",
    )


def _handle_settings_users_list(
    handler: BaseHTTPRequestHandler,
    entrypoint: RegistryUploadHttpEntrypoint,
    *,
    query: str = "",
) -> None:
    if not _ensure_admin_role(handler, DEFAULT_SETTINGS_USERS_PATH):
        return
    try:
        include_service_users = _coerce_bool(_resolve_single_query_param(query, "include_service"))
        _write_json_response(
            handler,
            HTTPStatus.OK,
            _settings_users_response_payload(
                entrypoint,
                _web_auth_config(),
                include_service_users=include_service_users,
            ),
        )
    except Exception as exc:  # pragma: no cover - bounded fallback
        _write_json_response(
            handler,
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {"error": f"settings users list failed: {exc}"},
        )


def _handle_settings_user_create(
    handler: BaseHTTPRequestHandler,
    entrypoint: RegistryUploadHttpEntrypoint,
) -> None:
    if not _ensure_admin_role(handler, DEFAULT_SETTINGS_USERS_PATH):
        return
    config = _web_auth_config()
    try:
        payload = _load_request_payload(handler)
        now = _utc_now_iso()
        username = _normalize_runtime_username(payload.get("username"))
        display_name = str(payload.get("display_name") or "").strip()
        _ensure_user_facing_runtime_identity(username=username, display_name=display_name)
        _ensure_runtime_username_available(username, config, entrypoint)
        password = _validate_runtime_password(payload.get("password"))
        requested_role = _validate_runtime_role(payload.get("role")) if "role" in payload else ""
        role = requested_role or WEB_AUTH_ROLE_OPERATOR
        allowed_sections = _validate_runtime_allowed_sections(
            payload.get("allowed_sections") if "allowed_sections" in payload else None,
            role=role,
        )
        manage_users = _validate_runtime_manage_users(
            payload.get("manage_users") if "manage_users" in payload else None,
            role=role,
        )
        if not requested_role and "allowed_sections" in payload:
            role = _runtime_role_for_allowed_sections(allowed_sections, manage_users=manage_users)
        _ensure_runtime_access_consistent(role, allowed_sections, manage_users)
        user_payload = {
            "user_id": f"user_{uuid4().hex}",
            "username": username,
            "display_name": display_name,
            "role": role,
            "allowed_sections": allowed_sections,
            "manage_users": manage_users,
            "password_hash": _hash_pbkdf2_password(password),
            "is_active": _coerce_bool(payload.get("is_active", True)),
            "created_at": now,
            "updated_at": now,
        }
        result = entrypoint.handle_sheet_vitrina_user_create_request(user_payload)
    except ValueError as exc:
        _write_json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        return
    except Exception as exc:  # pragma: no cover - bounded fallback
        _write_json_response(
            handler,
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {"error": f"settings user create failed: {exc}"},
        )
        return
    _write_json_response(handler, HTTPStatus.CREATED, _with_settings_users_context(result, entrypoint, config))


def _handle_settings_user_patch(
    handler: BaseHTTPRequestHandler,
    entrypoint: RegistryUploadHttpEntrypoint,
    user_id: str,
) -> None:
    if not _ensure_admin_role(handler, DEFAULT_SETTINGS_USERS_PATH):
        return
    if user_id.startswith("env:"):
        _write_json_response(handler, HTTPStatus.BAD_REQUEST, {"error": "env principal is read-only"})
        return
    config = _web_auth_config()
    try:
        payload = _load_request_payload(handler)
        existing = entrypoint.load_sheet_vitrina_runtime_user(user_id)
        if existing is None:
            _write_json_response(handler, HTTPStatus.NOT_FOUND, {"error": f"settings user not found: {user_id}"})
            return
        if "username" in payload:
            requested_username = _normalize_runtime_username(payload.get("username"))
            if requested_username != str(existing.get("username") or ""):
                raise ValueError("username is immutable")
        updates: dict[str, Any] = {}
        if "display_name" in payload:
            updates["display_name"] = str(payload.get("display_name") or "").strip()
        if "role" in payload:
            updates["role"] = _validate_runtime_role(payload.get("role"))
        candidate_role = str(updates.get("role", existing.get("role") or ""))
        if "allowed_sections" in payload:
            updates["allowed_sections"] = _validate_runtime_allowed_sections(
                payload.get("allowed_sections"),
                role=candidate_role,
            )
        elif "role" in updates:
            updates["allowed_sections"] = _default_allowed_sections_for_role(candidate_role)
        if "manage_users" in payload:
            updates["manage_users"] = _validate_runtime_manage_users(payload.get("manage_users"), role=candidate_role)
        elif "role" in updates:
            updates["manage_users"] = _default_manage_users_for_role(candidate_role)
        if "allowed_sections" in updates and "role" not in updates:
            updates["role"] = _runtime_role_for_allowed_sections(
                updates["allowed_sections"],
                manage_users=bool(updates.get("manage_users", existing.get("manage_users"))),
            )
            candidate_role = str(updates.get("role") or candidate_role)
        if "is_active" in payload:
            updates["is_active"] = _coerce_bool(payload.get("is_active"))
        if "password" in payload:
            password = _validate_runtime_password(payload.get("password"))
            updates["password_hash"] = _hash_pbkdf2_password(password)
        if not updates:
            result = {"user": _public_runtime_user(existing), "canonical_store": "server_runtime_sqlite"}
        else:
            candidate_allowed_sections = _normalize_public_allowed_sections(
                updates.get("allowed_sections", existing.get("allowed_sections")),
                role=candidate_role,
            )
            candidate_manage_users = bool(
                updates.get(
                    "manage_users",
                    existing.get("manage_users", _default_manage_users_for_role(candidate_role)),
                )
            )
            _ensure_runtime_access_consistent(candidate_role, candidate_allowed_sections, candidate_manage_users)
            candidate_is_active = bool(updates.get("is_active", existing.get("is_active")))
            _ensure_manage_users_not_exhausted(
                config,
                entrypoint,
                candidate_user_id=user_id,
                candidate_allowed_sections=candidate_allowed_sections,
                candidate_manage_users=candidate_manage_users,
                candidate_is_active=candidate_is_active,
            )
            result = entrypoint.handle_sheet_vitrina_user_patch_request(
                user_id,
                updates,
                updated_at=_utc_now_iso(),
            )
    except ValueError as exc:
        _write_json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        return
    except Exception as exc:  # pragma: no cover - bounded fallback
        _write_json_response(
            handler,
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {"error": f"settings user patch failed: {exc}"},
        )
        return
    _write_json_response(handler, HTTPStatus.OK, _with_settings_users_context(result, entrypoint, config))


def _handle_settings_user_delete(
    handler: BaseHTTPRequestHandler,
    entrypoint: RegistryUploadHttpEntrypoint,
    user_id: str,
) -> None:
    if not _ensure_admin_role(handler, DEFAULT_SETTINGS_USERS_PATH):
        return
    if user_id.startswith("env:"):
        _write_json_response(handler, HTTPStatus.BAD_REQUEST, {"error": "env principal is read-only"})
        return
    config = _web_auth_config()
    try:
        existing = entrypoint.load_sheet_vitrina_runtime_user(user_id)
        if existing is None:
            _write_json_response(handler, HTTPStatus.NOT_FOUND, {"error": f"settings user not found: {user_id}"})
            return
        _ensure_manage_users_not_exhausted(
            config,
            entrypoint,
            candidate_user_id=user_id,
            candidate_allowed_sections=_normalize_public_allowed_sections(
                existing.get("allowed_sections"),
                role=str(existing.get("role") or ""),
            ),
            candidate_manage_users=bool(existing.get("manage_users")),
            candidate_is_active=False,
        )
        result = entrypoint.handle_sheet_vitrina_user_archive_request(user_id, updated_at=_utc_now_iso())
    except ValueError as exc:
        _write_json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        return
    except Exception as exc:  # pragma: no cover - bounded fallback
        _write_json_response(
            handler,
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {"error": f"settings user delete failed: {exc}"},
        )
        return
    _write_json_response(handler, HTTPStatus.OK, _with_settings_users_context(result, entrypoint, config))


def _settings_users_response_payload(
    entrypoint: RegistryUploadHttpEntrypoint,
    config: Mapping[str, Any],
    *,
    include_service_users: bool = False,
) -> dict[str, Any]:
    runtime_payload = entrypoint.handle_sheet_vitrina_users_list_request()
    users = _env_principal_user_records(config)
    service_users: list[dict[str, Any]] = []
    for user in runtime_payload.get("users", []):
        if not isinstance(user, Mapping):
            continue
        service_reason = _runtime_service_user_reason(user)
        public_user = _public_runtime_user(user)
        if service_reason:
            public_user["is_service_user"] = True
            public_user["service_user_reason"] = service_reason
            service_users.append(public_user)
            continue
        users.append(public_user)
    payload: dict[str, Any] = {
        "users": users,
        "available_sections": _available_section_records(),
        "roles": [
            WEB_AUTH_ROLE_ADMIN,
            WEB_AUTH_ROLE_OPERATOR,
            WEB_AUTH_ROLE_SUPPLY_OPERATOR,
            WEB_AUTH_ROLE_SUPPLIER,
        ],
        "hidden_service_users_count": len(service_users),
        "service_users_hidden": True,
        "canonical_store": "server_runtime_sqlite",
    }
    if include_service_users:
        payload["service_users"] = service_users
    return payload


def _with_settings_users_context(
    result: Mapping[str, Any],
    entrypoint: RegistryUploadHttpEntrypoint,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(result)
    if isinstance(payload.get("user"), Mapping):
        payload["user"] = _public_runtime_user(payload["user"])
    context = _settings_users_response_payload(entrypoint, config)
    payload["users"] = context["users"]
    payload["available_sections"] = context["available_sections"]
    payload["roles"] = context["roles"]
    payload["hidden_service_users_count"] = context["hidden_service_users_count"]
    payload["service_users_hidden"] = context["service_users_hidden"]
    return payload


def _env_principal_user_records(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    operator = config.get("operator") if isinstance(config.get("operator"), Mapping) else {}
    operator_username = str(operator.get("username") or "").strip()
    if bool(config.get("enabled")) and operator_username:
        users.append(
            {
                "user_id": "env:bootstrap-admin",
                "username": operator_username,
                "display_name": str(operator.get("display_name") or operator_username),
                "role": WEB_AUTH_ROLE_ADMIN,
                "allowed_sections": _default_allowed_sections_for_role(WEB_AUTH_ROLE_ADMIN),
                "manage_users": True,
                "is_active": True,
                "created_at": "",
                "updated_at": "",
                "source": "env_bootstrap",
                "readonly": True,
                "readonly_reason": "env-пользователь: меняется только через env/runtime secret",
            }
        )
    supplier = config.get("supplier") if isinstance(config.get("supplier"), Mapping) else {}
    supplier_username = str(supplier.get("username") or "").strip()
    if bool(supplier.get("enabled")) and supplier_username:
        users.append(
            {
                "user_id": "env:supplier",
                "username": supplier_username,
                "display_name": str(supplier.get("display_name") or supplier_username),
                "role": WEB_AUTH_ROLE_SUPPLIER,
                "allowed_sections": _default_allowed_sections_for_role(WEB_AUTH_ROLE_SUPPLIER),
                "manage_users": False,
                "is_active": True,
                "created_at": "",
                "updated_at": "",
                "source": "env_supplier",
                "readonly": True,
                "readonly_reason": "env-пользователь: меняется только через env/runtime secret",
            }
        )
    return users


def _public_runtime_user(user: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "user_id": str(user.get("user_id") or ""),
        "username": str(user.get("username") or ""),
        "display_name": str(user.get("display_name") or ""),
        "role": str(user.get("role") or ""),
        "allowed_sections": _normalize_public_allowed_sections(
            user.get("allowed_sections"),
            role=str(user.get("role") or ""),
        ),
        "manage_users": bool(user.get("manage_users")),
        "is_active": bool(user.get("is_active")),
        "created_at": str(user.get("created_at") or ""),
        "updated_at": str(user.get("updated_at") or ""),
        "source": "runtime",
        "readonly": False,
    }


def _runtime_service_user_reason(user: Mapping[str, Any]) -> str:
    if _coerce_bool(user.get("is_service_user")):
        return "is_service_user"
    user_kind = str(user.get("user_kind") or "").strip().lower()
    if user_kind in SERVICE_USER_KINDS:
        return "user_kind"
    created_by = str(user.get("created_by") or "").strip().lower()
    if created_by in SERVICE_USER_CREATORS:
        return "created_by"
    username = _normalize_web_auth_username(str(user.get("username") or "")).lower()
    if username.startswith(SERVICE_USER_USERNAME_PREFIXES):
        return "username_prefix"
    display_name = str(user.get("display_name") or "").strip().lower()
    if any(phrase in display_name for phrase in SERVICE_USER_DISPLAY_NAME_PHRASES):
        return "display_name"
    if any(
        re.search(r"(^|[\s_-])" + re.escape(word) + r"([\s_-]|$)", display_name)
        for word in SERVICE_USER_DISPLAY_NAME_WORDS
    ):
        return "display_name"
    return ""


def _ensure_user_facing_runtime_identity(*, username: str, display_name: str) -> None:
    reason = _runtime_service_user_reason({"username": username, "display_name": display_name})
    if reason:
        raise ValueError(
            "username/display_name uses a reserved service/debug/test identity; "
            "create service users through a service path"
        )


def _normalize_runtime_username(value: Any) -> str:
    username = _normalize_web_auth_username(str(value or ""))
    if not username:
        raise ValueError("username is required")
    if len(username) > 64:
        raise ValueError("username is too long")
    if any(char.isspace() for char in username):
        raise ValueError("username must not contain whitespace")
    return username


def _validate_runtime_role(value: Any) -> str:
    role = _normalize_runtime_role(value)
    if not role:
        raise ValueError("role is invalid")
    return role


def _available_section_records() -> list[dict[str, str]]:
    return [
        {"section_id": str(section["section_id"]), "label": str(section["label"])}
        for section in WEB_AUTH_SECTION_DEFINITIONS
    ]


def _default_allowed_sections_for_role(role: str) -> list[str]:
    normalized = str(role or "").strip()
    if normalized in {WEB_AUTH_ROLE_ADMIN, WEB_AUTH_ROLE_OPERATOR}:
        return list(WEB_AUTH_SECTION_IDS)
    if normalized == WEB_AUTH_ROLE_SUPPLY_OPERATOR:
        return [WEB_AUTH_SECTION_SUPPLY]
    return []


def _default_manage_users_for_role(role: str) -> bool:
    return str(role or "").strip() == WEB_AUTH_ROLE_ADMIN


def _normalize_public_allowed_sections(value: Any, *, role: str = "") -> list[str]:
    if value is None:
        return _default_allowed_sections_for_role(role)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = []
        value = parsed
    if not isinstance(value, (list, tuple, set)):
        return _default_allowed_sections_for_role(role)
    valid = set(WEB_AUTH_SECTION_IDS)
    sections: list[str] = []
    seen: set[str] = set()
    for item in value:
        section_id = str(item or "").strip()
        if section_id in valid and section_id not in seen:
            sections.append(section_id)
            seen.add(section_id)
    return sections


def _validate_runtime_allowed_sections(value: Any, *, role: str = "") -> list[str]:
    if value is None:
        return _default_allowed_sections_for_role(role)
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("allowed_sections must be a list")
    valid = set(WEB_AUTH_SECTION_IDS)
    sections: list[str] = []
    seen: set[str] = set()
    for item in value:
        section_id = str(item or "").strip()
        if section_id not in valid:
            raise ValueError(f"allowed_sections contains unsupported section: {section_id}")
        if section_id not in seen:
            sections.append(section_id)
            seen.add(section_id)
    return sections


def _validate_runtime_manage_users(value: Any, *, role: str = "") -> bool:
    if value is None:
        return _default_manage_users_for_role(role)
    return _coerce_bool(value)


def _runtime_role_for_allowed_sections(allowed_sections: Sequence[str], *, manage_users: bool) -> str:
    normalized = [str(section or "").strip() for section in allowed_sections if str(section or "").strip()]
    if manage_users:
        return WEB_AUTH_ROLE_ADMIN
    if set(normalized) == {WEB_AUTH_SECTION_SUPPLY}:
        return WEB_AUTH_ROLE_SUPPLY_OPERATOR
    return WEB_AUTH_ROLE_OPERATOR


def _ensure_runtime_access_consistent(role: str, allowed_sections: Sequence[str], manage_users: bool) -> None:
    normalized_role = _validate_runtime_role(role)
    sections = [str(section or "").strip() for section in allowed_sections if str(section or "").strip()]
    if normalized_role == WEB_AUTH_ROLE_SUPPLIER:
        if sections or manage_users:
            raise ValueError("supplier users must use supplier-only access without shell sections")
        return
    if not sections:
        raise ValueError("allowed_sections must include at least one section")
    if manage_users and WEB_AUTH_SECTION_SETTINGS not in sections:
        raise ValueError("manage_users requires settings access")


def _validate_runtime_password(value: Any) -> str:
    password = str(value or "")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if len(password) > 1024:
        raise ValueError("password is too long")
    return password


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "active", "enabled"}
    return bool(value)


def _ensure_runtime_username_available(
    username: str,
    config: Mapping[str, Any],
    entrypoint: RegistryUploadHttpEntrypoint,
) -> None:
    normalized = _normalize_runtime_username(username)
    for env_user in _env_principal_user_records(config):
        if normalized == _normalize_web_auth_username(str(env_user.get("username") or "")):
            raise ValueError("username already exists")
    existing = entrypoint.load_sheet_vitrina_runtime_user_by_username(normalized)
    if existing is not None:
        raise ValueError("username already exists")


def _ensure_manage_users_not_exhausted(
    config: Mapping[str, Any],
    entrypoint: RegistryUploadHttpEntrypoint,
    *,
    candidate_user_id: str,
    candidate_allowed_sections: Sequence[str],
    candidate_manage_users: bool,
    candidate_is_active: bool,
) -> None:
    if _active_manage_users_count_after(
        config,
        entrypoint,
        candidate_user_id=candidate_user_id,
        candidate_allowed_sections=candidate_allowed_sections,
        candidate_manage_users=candidate_manage_users,
        candidate_is_active=candidate_is_active,
    ) < 1:
        raise ValueError("cannot disable or demote the last admin/manage-users access")


def _active_manage_users_count_after(
    config: Mapping[str, Any],
    entrypoint: RegistryUploadHttpEntrypoint,
    *,
    candidate_user_id: str,
    candidate_allowed_sections: Sequence[str],
    candidate_manage_users: bool,
    candidate_is_active: bool,
) -> int:
    count = 0
    operator = config.get("operator") if isinstance(config.get("operator"), Mapping) else {}
    if bool(config.get("enabled")) and str(operator.get("username") or "").strip():
        count += 1
    runtime_payload = entrypoint.handle_sheet_vitrina_users_list_request()
    for user in runtime_payload.get("users", []):
        if not isinstance(user, Mapping):
            continue
        user_id = str(user.get("user_id") or "")
        is_active = bool(user.get("is_active"))
        allowed_sections = _normalize_public_allowed_sections(
            user.get("allowed_sections"),
            role=str(user.get("role") or ""),
        )
        manage_users = bool(user.get("manage_users"))
        if user_id == candidate_user_id:
            is_active = bool(candidate_is_active)
            allowed_sections = [str(section or "").strip() for section in candidate_allowed_sections]
            manage_users = bool(candidate_manage_users)
        if is_active and manage_users and WEB_AUTH_SECTION_SETTINGS in allowed_sections:
            count += 1
    return count


def _hash_pbkdf2_password(password: str) -> str:
    salt = os.urandom(16)
    iterations = 260_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_base64url_encode(salt)}${_base64url_encode(digest)}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_redirect_response(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    location: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> None:
    body = b""
    handler.send_response(status.value)
    handler.send_header("Location", location)
    for key, value in (headers or {}).items():
        handler.send_header(str(key), str(value))
    handler.send_header("Content-Length", "0")
    handler.end_headers()
    _write_response_body(handler, body)


def _load_login_payload(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    raw_length = handler.headers.get("Content-Length", "").strip()
    if not raw_length:
        raise ValueError("login request body is required")
    try:
        content_length = int(raw_length)
    except ValueError as exc:
        raise ValueError("Content-Length must be integer") from exc
    if content_length <= 0:
        raise ValueError("login request body must not be empty")
    raw_body = handler.rfile.read(content_length)
    content_type = str(handler.headers.get("Content-Type", "") or "").lower()
    if "application/json" in content_type:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("login JSON must be valid") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("login JSON must be an object")
        return {str(key): str(value) for key, value in payload.items()}
    form = urllib_parse.parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
    return {key: str(values[0] if values else "") for key, values in form.items()}


def _handler_runtime_entrypoint(handler: BaseHTTPRequestHandler) -> RegistryUploadHttpEntrypoint | None:
    candidate = getattr(handler, "runtime_entrypoint", None)
    return candidate if isinstance(candidate, RegistryUploadHttpEntrypoint) else None


def _match_web_auth_principal(
    username: str,
    password: str,
    config: Mapping[str, Any],
    entrypoint: RegistryUploadHttpEntrypoint | None,
) -> dict[str, str] | None:
    normalized_username = _normalize_web_auth_username(username)
    operator = config.get("operator") if isinstance(config.get("operator"), Mapping) else {}
    if (
        normalized_username == _normalize_web_auth_username(str(operator.get("username") or ""))
        and _verify_pbkdf2_password(password, str(operator.get("password_hash") or ""))
    ):
        return {
            "username": str(operator.get("username") or username),
            "role": WEB_AUTH_ROLE_ADMIN,
            "display_name": str(operator.get("display_name") or username),
            "allowed_sections": _default_allowed_sections_for_role(WEB_AUTH_ROLE_ADMIN),
            "manage_users": True,
        }
    runtime_user = _load_runtime_user_by_username(entrypoint, normalized_username)
    if (
        runtime_user
        and bool(runtime_user.get("is_active"))
        and _verify_pbkdf2_password(password, str(runtime_user.get("password_hash") or ""))
    ):
        role = _normalize_runtime_role(runtime_user.get("role"))
        if role:
            return {
                "username": str(runtime_user.get("username") or username),
                "role": role,
                "display_name": str(runtime_user.get("display_name") or runtime_user.get("username") or username),
                "allowed_sections": _normalize_public_allowed_sections(runtime_user.get("allowed_sections"), role=role),
                "manage_users": bool(runtime_user.get("manage_users")),
            }
    supplier = config.get("supplier") if isinstance(config.get("supplier"), Mapping) else {}
    if (
        bool(supplier.get("enabled"))
        and normalized_username == _normalize_web_auth_username(str(supplier.get("username") or ""))
        and _verify_pbkdf2_password(password, str(supplier.get("password_hash") or ""))
    ):
        return {
            "username": str(supplier.get("username") or username),
            "role": WEB_AUTH_ROLE_SUPPLIER,
            "display_name": str(supplier.get("display_name") or username),
            "allowed_sections": _default_allowed_sections_for_role(WEB_AUTH_ROLE_SUPPLIER),
            "manage_users": False,
        }
    return None


def _authenticated_web_user(handler: BaseHTTPRequestHandler, config: Mapping[str, Any]) -> dict[str, str] | None:
    cookie_value = _request_cookie(handler, WEB_AUTH_COOKIE_NAME)
    if not cookie_value or "." not in cookie_value:
        return None
    payload_b64, signature_b64 = cookie_value.rsplit(".", 1)
    expected_signature = _base64url_encode(
        hmac.new(
            str(config.get("session_secret") or "").encode("utf-8"),
            payload_b64.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    if not hmac.compare_digest(signature_b64, expected_signature):
        return None
    try:
        payload = json.loads(_base64url_decode(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    username = str(payload.get("u") or "")
    expires_at = int(payload.get("exp") or 0)
    if expires_at < int(time.time()):
        return None
    role = str(payload.get("r") or "").strip() or WEB_AUTH_ROLE_OPERATOR
    operator = config.get("operator") if isinstance(config.get("operator"), Mapping) else {}
    if (
        _normalize_web_auth_username(username) == _normalize_web_auth_username(str(operator.get("username") or ""))
        and role in {WEB_AUTH_ROLE_ADMIN, WEB_AUTH_ROLE_OPERATOR}
    ):
        return {
            "username": str(operator.get("username") or username),
            "role": WEB_AUTH_ROLE_ADMIN,
            "display_name": str(payload.get("d") or operator.get("display_name") or username),
            "allowed_sections": _default_allowed_sections_for_role(WEB_AUTH_ROLE_ADMIN),
            "manage_users": True,
        }
    runtime_user = _load_runtime_user_by_username(
        _handler_runtime_entrypoint(handler),
        _normalize_web_auth_username(username),
    )
    runtime_role = _normalize_runtime_role(runtime_user.get("role") if runtime_user else "")
    if (
        runtime_user
        and bool(runtime_user.get("is_active"))
        and runtime_role
        and role == runtime_role
    ):
        return {
            "username": str(runtime_user.get("username") or username),
            "role": runtime_role,
            "display_name": str(payload.get("d") or runtime_user.get("display_name") or runtime_user.get("username") or username),
            "allowed_sections": _normalize_public_allowed_sections(runtime_user.get("allowed_sections"), role=runtime_role),
            "manage_users": bool(runtime_user.get("manage_users")),
        }
    supplier = config.get("supplier") if isinstance(config.get("supplier"), Mapping) else {}
    if (
        bool(supplier.get("enabled"))
        and _normalize_web_auth_username(username) == _normalize_web_auth_username(str(supplier.get("username") or ""))
        and role == WEB_AUTH_ROLE_SUPPLIER
    ):
        return {
            "username": str(supplier.get("username") or username),
            "role": WEB_AUTH_ROLE_SUPPLIER,
            "display_name": str(payload.get("d") or supplier.get("display_name") or username),
            "allowed_sections": _default_allowed_sections_for_role(WEB_AUTH_ROLE_SUPPLIER),
            "manage_users": False,
        }
    return None


def _build_session_cookie(
    handler: BaseHTTPRequestHandler,
    username: str,
    config: Mapping[str, Any],
    *,
    role: str,
    display_name: str,
) -> str:
    max_age = int(config.get("max_age") or WEB_AUTH_DEFAULT_MAX_AGE_SECONDS)
    payload = _base64url_encode(
        json.dumps(
            {
                "u": username,
                "r": role,
                "d": display_name,
                "exp": int(time.time()) + max_age,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signature = _base64url_encode(
        hmac.new(
            str(config.get("session_secret") or "").encode("utf-8"),
            payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    return _session_cookie_header(
        handler,
        f"{payload}.{signature}",
        max_age=max_age,
    )


def _expired_session_cookie(handler: BaseHTTPRequestHandler) -> str:
    return _session_cookie_header(handler, "", max_age=0)


def _session_cookie_header(handler: BaseHTTPRequestHandler, value: str, *, max_age: int) -> str:
    parts = [
        f"{WEB_AUTH_COOKIE_NAME}={value}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max(0, int(max_age))}",
    ]
    if _request_origin(handler).startswith("https://"):
        parts.append("Secure")
    return "; ".join(parts)


def _verify_pbkdf2_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, raw_iterations, salt_b64, digest_b64 = encoded_hash.split("$", 3)
        iterations = int(raw_iterations)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256" or iterations < 100_000:
        return False
    try:
        salt = _base64url_decode(salt_b64)
        expected = _base64url_decode(digest_b64)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _request_cookie(handler: BaseHTTPRequestHandler, name: str) -> str:
    cookie_header = str(handler.headers.get("Cookie", "") or "")
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == name:
            return value.strip()
    return ""


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("invalid base64url value") from exc


def _safe_next_path(value: str) -> str:
    next_path = str(value or "").strip() or DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    if not next_path.startswith("/") or next_path.startswith("//"):
        return DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    if next_path.startswith(DEFAULT_WEB_AUTH_LOGIN_PATH):
        return DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    return next_path


def _normalize_web_auth_username(value: str) -> str:
    return str(value or "").strip().lower()


def _normalize_runtime_role(value: Any) -> str:
    role = str(value or "").strip()
    return role if role in WEB_AUTH_RUNTIME_ROLES else ""


def _role_has_full_operator_access(role: str) -> bool:
    return str(role or "").strip() in WEB_AUTH_FULL_OPERATOR_ROLES


def _role_has_supply_operator_access(role: str) -> bool:
    return str(role or "").strip() in WEB_AUTH_SUPPLY_OPERATOR_ROLES


def _user_allowed_sections(user: Mapping[str, Any]) -> list[str]:
    return _normalize_public_allowed_sections(user.get("allowed_sections"), role=str(user.get("role") or ""))


def _user_has_section_access(user: Mapping[str, Any], section_id: str) -> bool:
    if str(user.get("role") or "").strip() == WEB_AUTH_ROLE_SUPPLIER:
        return False
    return str(section_id or "").strip() in _user_allowed_sections(user)


def _user_can_manage_users(user: Mapping[str, Any]) -> bool:
    return bool(user.get("manage_users")) and _user_has_section_access(user, WEB_AUTH_SECTION_SETTINGS)


def _allowed_unified_tabs_for_user(user: Mapping[str, Any]) -> list[str]:
    return _allowed_unified_tabs_for_sections(_user_allowed_sections(user))


def _allowed_unified_tabs_for_sections(allowed_sections: Sequence[str]) -> list[str]:
    allowed = {str(section or "").strip() for section in allowed_sections}
    return [
        tab_id
        for tab_id, section_id in WEB_AUTH_UNIFIED_TAB_SECTIONS.items()
        if section_id in allowed
    ]


def _allowed_unified_tabs_for_role(role: str) -> list[str]:
    normalized = str(role or "").strip()
    if normalized == WEB_AUTH_ROLE_SUPPLY_OPERATOR:
        return ["factory-order"]
    if _role_has_full_operator_access(normalized):
        return ["vitrina", "factory-order", "reports", "feedbacks", "ads", "research", "settings"]
    return []


def _default_unified_tab_for_role(role: str) -> str:
    tabs = _allowed_unified_tabs_for_role(role)
    return tabs[0] if tabs else "vitrina"


def _default_unified_tab_for_sections(allowed_sections: Sequence[str]) -> str:
    tabs = _allowed_unified_tabs_for_sections(allowed_sections)
    return tabs[0] if tabs else "vitrina"


def _required_section_for_path(path: str) -> str:
    normalized = str(path or "").split("?", 1)[0]
    if normalized in {
        DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
        DEFAULT_SHEET_WEB_VITRINA_GROUP_REFRESH_PATH,
        DEFAULT_SHEET_WEB_VITRINA_AUTO_SCHEDULES_PATH,
        DEFAULT_SHEET_WEB_VITRINA_AUTO_SCHEDULES_RUN_NOW_PATH,
        DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH,
        DEFAULT_SHEET_REFRESH_PATH,
        DEFAULT_SHEET_LOAD_PATH,
        DEFAULT_SHEET_STATUS_PATH,
        DEFAULT_SHEET_JOB_PATH,
        DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH,
        DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
        DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
        DEFAULT_SHEET_WEB_VITRINA_SELLER_RECOVERY_START_PATH,
        DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH,
        DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
    }:
        return WEB_AUTH_SECTION_VITRINA
    if normalized in {
        DEFAULT_SHEET_DAILY_REPORT_PATH,
        DEFAULT_SHEET_STOCK_REPORT_PATH,
        DEFAULT_SHEET_PLAN_REPORT_PATH,
        DEFAULT_SHEET_PLAN_REPORT_BASELINE_TEMPLATE_PATH,
        DEFAULT_SHEET_PLAN_REPORT_BASELINE_UPLOAD_PATH,
        DEFAULT_SHEET_PLAN_REPORT_BASELINE_STATUS_PATH,
    }:
        return WEB_AUTH_SECTION_REPORTS
    if normalized == DEFAULT_SETTINGS_UI_PATH:
        return WEB_AUTH_SECTION_SETTINGS
    if (
        normalized == DEFAULT_NOMENCLATURE_PATH
        or normalized.startswith(DEFAULT_NOMENCLATURE_PATH + "/")
        or normalized == DEFAULT_NOMENCLATURE_EXPORT_PATH
        or normalized == DEFAULT_NOMENCLATURE_IMPORT_PATH
        or normalized == DEFAULT_TRADE_DOCUMENTS_PATH
        or normalized.startswith(DEFAULT_TRADE_DOCUMENTS_PATH + "/")
    ):
        return WEB_AUTH_SECTION_SETTINGS
    if (
        normalized == DEFAULT_SHEET_FEEDBACKS_PATH
        or normalized.startswith(DEFAULT_SHEET_FEEDBACKS_PATH + "/")
    ):
        return WEB_AUTH_SECTION_FEEDBACKS
    if normalized.startswith("/v1/sheet-vitrina-v1/ads/"):
        return WEB_AUTH_SECTION_ADS
    if normalized.startswith("/v1/sheet-vitrina-v1/research/"):
        return WEB_AUTH_SECTION_RESEARCH
    if normalized.startswith("/v1/sheet-vitrina-v1/supply/"):
        return WEB_AUTH_SECTION_SUPPLY
    return ""


def _user_can_access_path(user: Mapping[str, Any], path: str, *, query: str = "") -> bool:
    normalized = str(path or "").split("?", 1)[0]
    role = str(user.get("role") or "").strip()
    if normalized == DEFAULT_SETTINGS_USERS_PATH or normalized.startswith(DEFAULT_SETTINGS_USERS_PATH + "/"):
        return _user_can_manage_users(user)
    if normalized == DEFAULT_SHEET_WEB_VITRINA_UI_PATH:
        return role != WEB_AUTH_ROLE_SUPPLIER and bool(_allowed_unified_tabs_for_user(user))
    if normalized == DEFAULT_SHEET_OPERATOR_UI_PATH:
        embedded_tab = ""
        try:
            embedded_tab = _resolve_operator_embedded_tab_from_query(query)
        except ValueError:
            return role != WEB_AUTH_ROLE_SUPPLIER and bool(_allowed_unified_tabs_for_user(user))
        if embedded_tab:
            section_id = WEB_AUTH_UNIFIED_TAB_SECTIONS.get(embedded_tab, "")
            return bool(section_id) and _user_has_section_access(user, section_id)
        return role != WEB_AUTH_ROLE_SUPPLIER and bool(_allowed_unified_tabs_for_user(user))
    if normalized == DEFAULT_SHEET_SUPPLIER_UI_PATH:
        return role == WEB_AUTH_ROLE_SUPPLIER or _user_has_section_access(user, WEB_AUTH_SECTION_SUPPLY)
    if normalized == DEFAULT_SUPPLIER_SHIPMENTS_PATH or normalized.startswith(DEFAULT_SUPPLIER_SHIPMENTS_PATH + "/"):
        return role == WEB_AUTH_ROLE_SUPPLIER or _user_has_section_access(user, WEB_AUTH_SECTION_SUPPLY)
    required_section = _required_section_for_path(normalized)
    if required_section:
        return _user_has_section_access(user, required_section)
    return _role_has_full_operator_access(role)


def _load_runtime_user_by_username(
    entrypoint: RegistryUploadHttpEntrypoint | None,
    username: str,
) -> dict[str, Any] | None:
    normalized_username = _normalize_web_auth_username(username)
    if not entrypoint or not normalized_username:
        return None
    try:
        return entrypoint.load_sheet_vitrina_runtime_user_by_username(normalized_username)
    except Exception:
        return None


def _allowed_roles_for_path(path: str) -> set[str]:
    normalized = str(path or "").split("?", 1)[0]
    full_operator_roles = set(WEB_AUTH_FULL_OPERATOR_ROLES)
    supply_operator_roles = set(WEB_AUTH_SUPPLY_OPERATOR_ROLES)
    if normalized == DEFAULT_SHEET_WEB_VITRINA_UI_PATH:
        return supply_operator_roles
    if normalized == DEFAULT_SHEET_OPERATOR_UI_PATH:
        return supply_operator_roles
    if normalized == DEFAULT_SETTINGS_USERS_PATH or normalized.startswith(DEFAULT_SETTINGS_USERS_PATH + "/"):
        return {WEB_AUTH_ROLE_ADMIN}
    if normalized == DEFAULT_SETTINGS_UI_PATH:
        return full_operator_roles
    if normalized == DEFAULT_SHEET_SUPPLIER_UI_PATH:
        return full_operator_roles | {WEB_AUTH_ROLE_SUPPLIER}
    if normalized == DEFAULT_SUPPLIER_SHIPMENTS_PATH or normalized.startswith(DEFAULT_SUPPLIER_SHIPMENTS_PATH + "/"):
        return supply_operator_roles | {WEB_AUTH_ROLE_SUPPLIER}
    if normalized.startswith("/v1/sheet-vitrina-v1/supply/"):
        return supply_operator_roles
    return full_operator_roles


def _is_json_route(path: str, handler: BaseHTTPRequestHandler) -> bool:
    accept = str(handler.headers.get("Accept", "") or "").lower()
    return _is_json_path(path) or "application/json" in accept


def _is_json_path(path: str) -> bool:
    return str(path or "").startswith("/v1/")


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_positive_int(value: Any, default: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    return normalized if normalized > 0 else default


def _build_content_disposition(disposition: str, filename: str) -> str:
    raw_filename = str(filename or "").strip() or "download.bin"
    ascii_fallback = "".join(char if ord(char) < 128 and char not in {'"', "\\"} else "_" for char in raw_filename)
    ascii_fallback = ascii_fallback or "download.bin"
    encoded = urllib_parse.quote(raw_filename, safe="")
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def _build_sheet_job_url(job_path: str, job_id: str) -> str:
    return f"{job_path}?{urllib_parse.urlencode({'job_id': job_id})}"


def _build_sheet_job_download_url(job_path: str, job_id: str) -> str:
    return (
        f"{job_path}?"
        f"{urllib_parse.urlencode({'job_id': job_id, 'format': 'text', 'download': '1'})}"
    )


def _with_sheet_job_urls(payload: Mapping[str, Any], job_path: str) -> dict[str, Any]:
    normalized = dict(payload)
    job_id = str(normalized.get("job_id", "") or "").strip()
    if not job_id:
        return normalized
    operation = str(normalized.get("operation", "") or "job").strip()
    normalized["job_path"] = _build_sheet_job_url(job_path, job_id)
    normalized["download_path"] = _build_sheet_job_download_url(job_path, job_id)
    normalized["log_filename"] = f"sheet-vitrina-v1-{operation}-{job_id}.txt"
    return normalized


def _seller_recovery_launcher_unavailable_payload(
    status_payload: Mapping[str, Any] | None,
    *,
    error: str = "",
) -> dict[str, Any]:
    status = dict(status_payload or {})
    run_status = str(status.get("run_status") or status.get("status") or "idle").strip() or "idle"
    run_id = str(status.get("run_id") or status.get("current_run_id") or "").strip()
    run_is_final = bool(status.get("run_is_final")) or run_status in {"completed", "not_needed", "stopped", "timeout", "error"}
    if bool(status.get("requested_run_mismatch")):
        code = "run_replaced"
    elif not run_id or run_status == "idle":
        code = "no_active_run"
    elif run_status == "starting":
        code = "run_starting"
    elif run_status == "awaiting_login":
        code = "launcher_artifact_missing"
    elif run_is_final:
        code = "run_final"
    else:
        code = "launcher_not_ready"
    reason = str(status.get("reason") or status.get("summary") or status.get("message") or error or "").strip()
    if not reason:
        reason = "seller recovery launcher is not ready for the current run"
    return {
        "error": f"seller portal recovery launcher unavailable: {reason}",
        "status": "launcher_unavailable",
        "launcher_status": code,
        "run_id": run_id,
        "run_status": run_status,
        "running": bool(status.get("running")),
        "launcher_ready": False,
        "can_download_launcher": False,
        "can_open_login_window": bool(status.get("can_open_login_window")),
        "launcher_url": "",
        "launcher_download_path": str(status.get("launcher_download_path") or DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH),
        "summary": str(status.get("summary") or reason),
        "reason": reason,
        "final_marker": str(status.get("final_marker") or ""),
        "run_final_status": str(status.get("run_final_status") or ""),
        "retryable": code in {"run_starting", "launcher_not_ready", "launcher_artifact_missing"},
    }


def _with_complaints_sync_job_urls(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    run_id = str(normalized.get("run_id", "") or "").strip()
    if not run_id:
        return normalized
    normalized["poll_url"] = (
        f"{DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH}?"
        f"{urllib_parse.urlencode({'run_id': run_id})}"
    )
    normalized["complaints_url"] = DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH
    return normalized


def _with_complaints_submit_job_urls(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    run_id = str(normalized.get("run_id", "") or "").strip()
    if not run_id:
        return normalized
    normalized["poll_url"] = (
        f"{DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_JOB_PATH}?"
        f"{urllib_parse.urlencode({'run_id': run_id})}"
    )
    normalized["complaints_url"] = DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH
    return normalized


def _auto_complaints_error_payload(exc: SheetVitrinaV1FeedbacksAutoComplaintsError) -> dict[str, Any]:
    payload = {"error": str(exc)}
    reason = str(getattr(exc, "reason", "") or "").strip()
    status = str(getattr(exc, "status", "") or reason).strip()
    if status:
        payload["status"] = status
    if reason:
        payload["reason"] = reason
    return payload


def _with_factory_order_dataset_urls(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["stock_ff_onec_check_path"] = DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_CHECK_PATH
    normalized["stock_ff_onec_xlsx_path"] = DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_XLSX_PATH
    normalized["datasets"] = _map_dataset_urls(normalized.get("datasets"))
    manual_state = normalized.get("manual_factory_inbound_dataset")
    if isinstance(manual_state, Mapping):
        mapped_manual = _map_dataset_urls({DATASET_INBOUND_FACTORY_TO_FF: manual_state})
        normalized["manual_factory_inbound_dataset"] = mapped_manual.get(DATASET_INBOUND_FACTORY_TO_FF, manual_state)
    last_result = normalized.get("last_result")
    if isinstance(last_result, Mapping):
        nested = dict(last_result)
        nested["stock_ff_onec_check_path"] = DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_CHECK_PATH
        nested["stock_ff_onec_xlsx_path"] = DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_XLSX_PATH
        nested["datasets"] = _map_dataset_urls(nested.get("datasets"))
        manual_nested = nested.get("manual_factory_inbound_dataset")
        if isinstance(manual_nested, Mapping):
            mapped_manual_nested = _map_dataset_urls({DATASET_INBOUND_FACTORY_TO_FF: manual_nested})
            nested["manual_factory_inbound_dataset"] = mapped_manual_nested.get(DATASET_INBOUND_FACTORY_TO_FF, manual_nested)
        normalized["last_result"] = nested
    return normalized


def _with_wb_regional_urls(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["recommendations_zip_path"] = DEFAULT_WB_REGIONAL_RECOMMENDATIONS_ZIP_PATH
    normalized["planning_options_path"] = DEFAULT_WB_REGIONAL_PLANNING_OPTIONS_PATH
    normalized["shared_datasets"] = _map_dataset_urls(normalized.get("shared_datasets"))
    if isinstance(normalized.get("districts"), list):
        normalized["districts"] = _map_wb_regional_districts(_filter_wb_regional_districts(normalized))
    last_result = normalized.get("last_result")
    if isinstance(last_result, Mapping):
        nested = dict(last_result)
        nested["recommendations_zip_path"] = DEFAULT_WB_REGIONAL_RECOMMENDATIONS_ZIP_PATH
        nested["planning_options_path"] = DEFAULT_WB_REGIONAL_PLANNING_OPTIONS_PATH
        nested["shared_datasets"] = _map_dataset_urls(nested.get("shared_datasets"))
        if isinstance(nested.get("districts"), list):
            nested["districts"] = _map_wb_regional_districts(_filter_wb_regional_districts(nested))
        normalized["last_result"] = nested
    return normalized


def _filter_wb_regional_districts(result_payload: Mapping[str, Any]) -> list[Any]:
    districts = result_payload.get("districts")
    if not isinstance(districts, list):
        return []
    included = set(_wb_regional_included_keys_from_result(result_payload))
    if not included:
        return list(districts)
    filtered: list[Any] = []
    for item in districts:
        if not isinstance(item, Mapping):
            filtered.append(item)
            continue
        district_key = str(item.get("district_key", "") or "").strip().lower()
        if district_key in included:
            filtered.append(item)
    return filtered


def _wb_regional_included_keys_from_result(result_payload: Mapping[str, Any]) -> tuple[str, ...]:
    diagnostics = result_payload.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        parsed = _parse_wb_regional_included_keys(diagnostics.get("included_district_keys"))
        if parsed:
            return parsed
    settings = result_payload.get("settings")
    if isinstance(settings, Mapping):
        parsed = _parse_wb_regional_included_keys(settings.get("included_district_keys"))
        if parsed:
            return parsed
    return tuple(DISTRICT_KEYS)


def _parse_wb_regional_included_keys(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_values = [item.strip().lower() for item in value.split(",")]
    elif isinstance(value, (list, tuple)):
        raw_values = [str(item or "").strip().lower() for item in value]
    else:
        return ()
    requested = {item for item in raw_values if item in DISTRICT_KEYS}
    return tuple(key for key in DISTRICT_KEYS if key in requested)


def _map_dataset_urls(datasets: Any) -> Any:
    if not isinstance(datasets, Mapping):
        return datasets
    mapped: dict[str, Any] = {}
    for dataset_type, raw_value in datasets.items():
        if not isinstance(raw_value, Mapping):
            mapped[str(dataset_type)] = raw_value
            continue
        value = dict(raw_value)
        delete_path = _factory_order_delete_path_for_dataset(str(dataset_type))
        value["delete_path"] = delete_path if delete_path and str(value.get("status", "") or "") == "uploaded" else ""
        value["download_path"] = (
            _factory_order_uploaded_path_for_dataset(str(dataset_type))
            if bool(value.get("file_available"))
            else ""
        )
        mapped[str(dataset_type)] = value
    return mapped


def _map_wb_regional_districts(items: Any) -> Any:
    if not isinstance(items, list):
        return items
    mapped: list[Any] = []
    for raw_value in items:
        if not isinstance(raw_value, Mapping):
            mapped.append(raw_value)
            continue
        value = dict(raw_value)
        district_key = str(value.get("district_key", "") or "").strip().lower()
        value["download_path"] = _wb_regional_district_download_path_for_key(district_key)
        mapped.append(value)
    return mapped


def _factory_order_uploaded_path_for_dataset(dataset_type: str) -> str:
    mapping = {
        DATASET_STOCK_FF: DEFAULT_FACTORY_ORDER_UPLOADED_STOCK_FF_PATH,
        DATASET_INBOUND_FACTORY_TO_FF: DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FACTORY_PATH,
        DATASET_INBOUND_FF_TO_WB: DEFAULT_FACTORY_ORDER_UPLOADED_INBOUND_FF_TO_WB_PATH,
    }
    return mapping.get(dataset_type, "")


def _factory_order_delete_path_for_dataset(dataset_type: str) -> str:
    mapping = {
        DATASET_STOCK_FF: DEFAULT_FACTORY_ORDER_DELETE_STOCK_FF_PATH,
        DATASET_INBOUND_FACTORY_TO_FF: DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FACTORY_PATH,
        DATASET_INBOUND_FF_TO_WB: DEFAULT_FACTORY_ORDER_DELETE_INBOUND_FF_TO_WB_PATH,
    }
    return mapping.get(dataset_type, "")


def _wb_regional_district_download_path_for_key(district_key: str) -> str:
    normalized = str(district_key or "").strip().lower()
    if not normalized:
        return ""
    return f"{DEFAULT_WB_REGIONAL_DISTRICT_DOWNLOAD_PREFIX}/{normalized}.xlsx"


def _render_sheet_vitrina_operator_ui(
    *,
    daily_report_path: str,
    stock_report_path: str,
    plan_report_path: str,
    refresh_path: str,
    load_path: str,
    status_path: str,
    job_path: str,
    operator_context: Mapping[str, Any] | None = None,
    embedded_tab: str = "",
) -> str:
    web_vitrina_url = DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    operator_ui_context = operator_context or {}
    normalized_embedded_tab = embedded_tab if embedded_tab in {"vitrina", "factory-order", "reports"} else ""
    config_payload = {
        "page_title": "Операторский сайт" if normalized_embedded_tab else "sheet_vitrina_v1",
        "embedded": bool(normalized_embedded_tab),
        "initial_tab": normalized_embedded_tab,
        "daily_report_path": daily_report_path,
        "stock_report_path": stock_report_path,
        "plan_report_path": plan_report_path,
        "plan_report_baseline_template_path": DEFAULT_SHEET_PLAN_REPORT_BASELINE_TEMPLATE_PATH,
        "plan_report_baseline_upload_path": DEFAULT_SHEET_PLAN_REPORT_BASELINE_UPLOAD_PATH,
        "plan_report_baseline_status_path": DEFAULT_SHEET_PLAN_REPORT_BASELINE_STATUS_PATH,
        "refresh_path": refresh_path,
        "load_path": load_path,
        "legacy_google_sheets_contour": legacy_google_sheets_archive_context(),
        "status_path": status_path,
        "job_path": job_path,
        "seller_session_check_path": DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH,
        "seller_recovery_status_path": DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
        "seller_recovery_start_path": DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
        "seller_recovery_stop_path": DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH,
        "seller_recovery_launcher_path": DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
        "factory_order_status_path": DEFAULT_FACTORY_ORDER_STATUS_PATH,
        "factory_order_template_stock_ff_path": DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH,
        "factory_order_stock_ff_onec_check_path": DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_CHECK_PATH,
        "factory_order_stock_ff_onec_xlsx_path": DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_XLSX_PATH,
        "factory_order_template_inbound_factory_path": DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH,
        "factory_order_template_inbound_ff_to_wb_path": DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH,
        "factory_order_upload_stock_ff_path": DEFAULT_FACTORY_ORDER_UPLOAD_STOCK_FF_PATH,
        "factory_order_upload_inbound_factory_path": DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FACTORY_PATH,
        "factory_order_upload_inbound_ff_to_wb_path": DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FF_TO_WB_PATH,
        "factory_order_calculate_path": DEFAULT_FACTORY_ORDER_CALCULATE_PATH,
        "factory_order_recommendation_path": DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH,
        "wb_regional_status_path": DEFAULT_WB_REGIONAL_STATUS_PATH,
        "wb_regional_calculate_path": DEFAULT_WB_REGIONAL_CALCULATE_PATH,
        "wb_regional_planning_options_path": DEFAULT_WB_REGIONAL_PLANNING_OPTIONS_PATH,
        "wb_regional_recommendations_zip_path": DEFAULT_WB_REGIONAL_RECOMMENDATIONS_ZIP_PATH,
        "wb_regional_district_options": [
            {
                "district_key": key,
                "district_name_ru": DISTRICT_LABELS_RU[key],
            }
            for key in DISTRICT_KEYS
        ],
        "wb_regional_default_included_district_keys": list(DISTRICT_KEYS),
        "wb_supplies_path": DEFAULT_WB_SUPPLIES_PATH,
        "wb_supplies_sync_path": DEFAULT_WB_SUPPLIES_SYNC_PATH,
        "wb_supplies_backfill_path": DEFAULT_WB_SUPPLIES_BACKFILL_PATH,
        "wb_supplies_sync_status_path": DEFAULT_WB_SUPPLIES_SYNC_STATUS_PATH,
        "wb_supplies_transit_cost_enrich_path": DEFAULT_WB_SUPPLIES_TRANSIT_COST_ENRICH_PATH,
        "wb_supplies_transit_cost_status_path": DEFAULT_WB_SUPPLIES_TRANSIT_COST_STATUS_PATH,
        "wb_supplies_overlay_options_path": DEFAULT_WB_SUPPLIES_OVERLAY_OPTIONS_PATH,
        "supplier_shipments_path": DEFAULT_SUPPLIER_SHIPMENTS_PATH,
        "supplier_shipments_parse_path": DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH,
        "supplier_shipment_registry_path": DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_PATH,
        "supplier_shipment_registry_compare_quote_path": DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_COMPARE_QUOTE_PATH,
        "cny_account_path": DEFAULT_CNY_ACCOUNT_PATH,
        "cny_account_documents_path": DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH,
        "cny_account_conversions_path": DEFAULT_CNY_ACCOUNT_CONVERSIONS_PATH,
        "cny_account_ledger_path": DEFAULT_CNY_ACCOUNT_LEDGER_PATH,
        "cny_account_opening_balance_path": DEFAULT_CNY_ACCOUNT_OPENING_BALANCE_PATH,
        "cny_account_replay_path": DEFAULT_CNY_ACCOUNT_REPLAY_PATH,
        "trade_documents_path": DEFAULT_TRADE_DOCUMENTS_PATH,
        "supplier_ui_path": DEFAULT_SHEET_SUPPLIER_UI_PATH,
        "current_business_date": str(operator_ui_context.get("current_business_date") or ""),
        "stock_report_active_skus": list(operator_ui_context.get("stock_report_active_skus") or []),
        "stock_report_active_sku_count": int(operator_ui_context.get("stock_report_active_sku_count") or 0),
        "stock_report_active_sku_source": str(
            operator_ui_context.get("stock_report_active_sku_source") or "current_registry_config_v2"
        ),
    }
    template = OPERATOR_UI_TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("__SHEET_VITRINA_V1_OPERATOR_PAGE_TITLE__", config_payload["page_title"])
        .replace(
            "__SHEET_VITRINA_V1_OPERATOR_BODY_CLASS__",
            "is-embedded" if normalized_embedded_tab else "",
        )
        .replace("__SHEET_VITRINA_V1_WEB_VITRINA_URL__", web_vitrina_url)
        .replace("__SHEET_VITRINA_V1_OPERATOR_CONFIG_JSON__", json.dumps(config_payload, ensure_ascii=False))
    )


def _render_sheet_vitrina_supplier_ui(
    *,
    can_delete_shipments: bool = True,
    can_edit_order_status: bool = False,
    can_recheck_prices: bool = True,
    can_manage_documents: bool = False,
    can_manage_financial_documents: bool = False,
    embedded: str = "",
) -> str:
    config_payload = {
        "page_title": "Реестр заказов",
        "embedded": str(embedded or ""),
        "supplier_shipments_path": DEFAULT_SUPPLIER_SHIPMENTS_PATH,
        "supplier_shipments_parse_path": DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH,
        "trade_documents_path": DEFAULT_TRADE_DOCUMENTS_PATH,
        "supplier_ui_path": DEFAULT_SHEET_SUPPLIER_UI_PATH,
        "logout_path": DEFAULT_WEB_AUTH_LOGOUT_PATH,
        "can_delete_shipments": bool(can_delete_shipments),
        "can_edit_order_status": bool(can_edit_order_status),
        "can_recheck_prices": bool(can_recheck_prices),
        "can_manage_documents": bool(can_manage_documents),
        "can_manage_financial_documents": bool(can_manage_financial_documents),
    }
    price_check_button_html = (
        '<button id="priceCheckButton" type="button" hidden>Проверить цены</button>'
        if can_recheck_prices
        else ""
    )
    template = SUPPLIER_UI_TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace(
            "__SHEET_VITRINA_V1_SUPPLIER_CONFIG_JSON__",
            json.dumps(config_payload, ensure_ascii=False),
        )
        .replace("__SHEET_VITRINA_V1_PRICE_CHECK_BUTTON_HTML__", price_check_button_html)
    )


def _render_sheet_vitrina_settings_ui(*, embedded: bool = False, can_manage_users: bool = True) -> str:
    config_payload = {
        "page_title": "Настройки",
        "nomenclature_path": DEFAULT_NOMENCLATURE_PATH,
        "nomenclature_export_path": DEFAULT_NOMENCLATURE_EXPORT_PATH,
        "nomenclature_import_path": DEFAULT_NOMENCLATURE_IMPORT_PATH,
        "nomenclature_barcode_sync_path": DEFAULT_NOMENCLATURE_BARCODE_SYNC_PATH,
        "trade_documents_path": DEFAULT_TRADE_DOCUMENTS_PATH,
        "settings_users_path": DEFAULT_SETTINGS_USERS_PATH,
        "vitrina_path": DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
        "logout_path": DEFAULT_WEB_AUTH_LOGOUT_PATH,
        "embedded": bool(embedded),
        "can_manage_users": bool(can_manage_users),
    }
    template = SETTINGS_UI_TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("__SHEET_VITRINA_V1_SETTINGS_BODY_CLASS__", "is-embedded" if embedded else "")
        .replace(
            "__SHEET_VITRINA_V1_SETTINGS_CONFIG_JSON__",
            json.dumps(config_payload, ensure_ascii=False),
        )
    )


def _render_sheet_vitrina_web_vitrina_ui(
    *,
    read_path: str,
    operator_path: str,
    refresh_path: str,
    job_path: str,
    role: str = WEB_AUTH_ROLE_ADMIN,
    allowed_sections: Sequence[str] | None = None,
    active_tab: str = "",
) -> str:
    normalized_role = _normalize_runtime_role(role) or WEB_AUTH_ROLE_ADMIN
    normalized_sections = (
        _normalize_public_allowed_sections(list(allowed_sections), role=normalized_role)
        if allowed_sections is not None
        else _default_allowed_sections_for_role(normalized_role)
    )
    allowed_tabs = _allowed_unified_tabs_for_sections(normalized_sections)
    initial_tab = active_tab if active_tab in allowed_tabs else _default_unified_tab_for_sections(normalized_sections)
    config_payload = {
        "page_title": "Web-витрина",
        "current_role": normalized_role,
        "allowed_sections": normalized_sections,
        "allowed_tabs": allowed_tabs,
        "initial_tab": initial_tab,
        "read_path": read_path,
        "operator_path": operator_path,
        "refresh_path": refresh_path,
        "group_refresh_path": DEFAULT_SHEET_WEB_VITRINA_GROUP_REFRESH_PATH,
        "auto_schedules_path": DEFAULT_SHEET_WEB_VITRINA_AUTO_SCHEDULES_PATH,
        "auto_schedules_run_now_path": DEFAULT_SHEET_WEB_VITRINA_AUTO_SCHEDULES_RUN_NOW_PATH,
        "user_config_path": DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH,
        "research_options_path": DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_OPTIONS_PATH,
        "research_calculate_path": DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_CALCULATE_PATH,
        "research_promotions_calculate_path": DEFAULT_SHEET_RESEARCH_PROMOTIONS_CALCULATE_PATH,
        "feedbacks_path": DEFAULT_SHEET_FEEDBACKS_PATH,
        "feedbacks_export_path": DEFAULT_SHEET_FEEDBACKS_EXPORT_PATH,
        "feedbacks_ai_prompt_path": DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH,
        "feedbacks_ai_analyze_path": DEFAULT_SHEET_FEEDBACKS_AI_ANALYZE_PATH,
        "feedbacks_complaints_path": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH,
        "feedbacks_complaints_sync_status_path": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_PATH,
        "feedbacks_complaints_sync_status_job_path": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH,
        "feedbacks_complaints_submit_selected_path": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_SELECTED_PATH,
        "feedbacks_complaints_submit_job_path": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_JOB_PATH,
        "feedbacks_auto_complaints_schedules_path": DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_SCHEDULES_PATH,
        "feedbacks_auto_complaints_run_now_path": DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_RUN_NOW_PATH,
        "feedbacks_auto_complaints_runs_path": DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_RUNS_PATH,
        "feedbacks_auto_complaints_run_path": DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_RUN_PATH,
        "feedbacks_auto_complaints_tick_path": DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_TICK_PATH,
        "ads_skus_path": DEFAULT_SHEET_ADS_SKUS_PATH,
        "ads_sku_path": DEFAULT_SHEET_ADS_SKU_PREFIX,
        "ads_bid_preview_path": DEFAULT_SHEET_ADS_BID_PREVIEW_PATH,
        "ads_bid_commit_path": DEFAULT_SHEET_ADS_BID_COMMIT_PATH,
        "settings_path": DEFAULT_SETTINGS_UI_PATH,
        "settings_users_path": DEFAULT_SETTINGS_USERS_PATH,
        "nomenclature_path": DEFAULT_NOMENCLATURE_PATH,
        "nomenclature_export_path": DEFAULT_NOMENCLATURE_EXPORT_PATH,
        "nomenclature_import_path": DEFAULT_NOMENCLATURE_IMPORT_PATH,
        "nomenclature_barcode_sync_path": DEFAULT_NOMENCLATURE_BARCODE_SYNC_PATH,
        "trade_documents_path": DEFAULT_TRADE_DOCUMENTS_PATH,
        "logout_path": DEFAULT_WEB_AUTH_LOGOUT_PATH,
        "job_path": job_path,
        "seller_session_check_path": DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH,
        "seller_recovery_status_path": DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
        "seller_recovery_start_path": DEFAULT_SHEET_WEB_VITRINA_SELLER_RECOVERY_START_PATH,
        "seller_recovery_launcher_path": DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
        "page_composition_surface": DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE,
    }
    template = WEB_VITRINA_UI_TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("__SHEET_VITRINA_V1_WEB_VITRINA_PAGE_TITLE__", config_payload["page_title"])
        .replace("__SHEET_VITRINA_V1_WEB_VITRINA_CONFIG_JSON__", json.dumps(config_payload, ensure_ascii=False))
    )


def _resolve_sheet_web_vitrina_surface_from_query(query: str) -> str:
    params = urllib_parse.parse_qs(query or "", keep_blank_values=False)
    values = params.get("surface") or []
    if not values:
        return "contract"
    if len(values) != 1:
        raise ValueError("surface must be provided at most once")
    surface = values[0].strip() or "contract"
    if surface in {"contract", DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE}:
        return surface
    raise ValueError(
        "unsupported web-vitrina surface: "
        f"{surface!r}; expected 'contract' or '{DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE}'"
    )


def _resolve_operator_embedded_tab_from_query(query: str) -> str:
    params = urllib_parse.parse_qs(query or "", keep_blank_values=False)
    values = params.get("embedded_tab") or []
    if not values:
        return ""
    if len(values) != 1:
        raise ValueError("embedded_tab must be provided at most once")
    tab = values[0].strip()
    if tab in {"vitrina", "factory-order", "reports"}:
        return tab
    raise ValueError("unsupported embedded_tab: expected 'vitrina', 'factory-order', or 'reports'")
