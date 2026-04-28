"""Минимальный inbound HTTP entrypoint для registry upload и sheet_vitrina_v1 refresh/read split."""

from __future__ import annotations

from dataclasses import asdict
from email.parser import BytesParser
from email.policy import default as default_email_policy
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socketserver
from typing import Any, Mapping
from urllib import parse as urllib_parse

from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_feedbacks import SheetVitrinaV1FeedbacksError
from packages.application.sheet_vitrina_v1_load_bridge import LegacyGoogleSheetsContourArchivedError
from packages.application.sheet_vitrina_v1_load_bridge import legacy_google_sheets_archive_context
from packages.contracts.factory_order_supply import (
    DATASET_INBOUND_FACTORY_TO_FF,
    DATASET_INBOUND_FF_TO_WB,
    DATASET_STOCK_FF,
)
from packages.contracts.cost_price_upload import CostPriceUploadResult
from packages.contracts.registry_upload_file_backed_service import RegistryUploadResult
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

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
DEFAULT_SHEET_WEB_VITRINA_GROUP_REFRESH_PATH = "/v1/sheet-vitrina-v1/web-vitrina/group-refresh"
DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_OPTIONS_PATH = (
    "/v1/sheet-vitrina-v1/research/sku-group-comparison/options"
)
DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_CALCULATE_PATH = (
    "/v1/sheet-vitrina-v1/research/sku-group-comparison/calculate"
)
DEFAULT_SHEET_FEEDBACKS_PATH = "/v1/sheet-vitrina-v1/feedbacks"
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
DEFAULT_FACTORY_ORDER_STATUS_PATH = "/v1/sheet-vitrina-v1/supply/factory-order/status"
DEFAULT_FACTORY_ORDER_TEMPLATE_STOCK_FF_PATH = "/v1/sheet-vitrina-v1/supply/factory-order/template/stock-ff.xlsx"
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
DEFAULT_WB_REGIONAL_DISTRICT_DOWNLOAD_PREFIX = "/v1/sheet-vitrina-v1/supply/wb-regional/district"
DEFAULT_RUNTIME_DIR = ROOT / ".runtime" / "registry_upload"
OPERATOR_UI_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "sheet_vitrina_v1_operator.html"
WEB_VITRINA_UI_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "sheet_vitrina_v1_web_vitrina.html"


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
        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
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
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return

                if async_requested:
                    try:
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

            _write_json_response(
                self,
                HTTPStatus.NOT_FOUND,
                {"error": f"unsupported path: {parsed.path}"},
            )
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
            if parsed.path == DEFAULT_SHEET_WEB_VITRINA_UI_PATH:
                _write_html_response(
                    self,
                    HTTPStatus.OK,
                    _render_sheet_vitrina_web_vitrina_ui(
                        read_path=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                        operator_path=sheet_operator_ui_path,
                        refresh_path=sheet_refresh_path,
                        job_path=sheet_job_path,
                    ),
                )
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
                        ),
                    )
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
                    payload = entrypoint.handle_seller_portal_recovery_status_request(
                        launcher_download_path=DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                        run_id=run_id or None,
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
                    request_origin = _request_origin(self)
                    archive_bytes, filename = entrypoint.handle_seller_portal_recovery_launcher_request(
                        public_status_url=f"{request_origin}{DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH}",
                        public_operator_url=f"{request_origin}{sheet_operator_ui_path}",
                    )
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
                    as_of_date = _resolve_as_of_date_from_query(parsed.query) or None
                    date_from, date_to = _resolve_web_vitrina_period_window_from_query(parsed.query)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc)},
                    )
                    return

                if surface == DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE:
                    payload = entrypoint.handle_sheet_web_vitrina_page_composition_request(
                        page_route=DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                        read_route=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                        operator_route=sheet_operator_ui_path,
                        job_path=sheet_job_path,
                        as_of_date=as_of_date,
                        date_from=date_from,
                        date_to=date_to,
                        include_source_status=_resolve_optional_query_bool(parsed.query, "include_source_status"),
                    )
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

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
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
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        if part.get_param("name", header="Content-Disposition") != "file":
            continue
        payload = part.get_payload(decode=True)
        if payload:
            workbook_bytes = payload
            filename = str(part.get_filename() or "").strip()
            part_content_type = str(part.get_content_type() or "").strip()
            break
    if not workbook_bytes:
        raise ValueError("multipart/form-data must contain non-empty file field")
    return {
        "workbook_bytes": workbook_bytes,
        "filename": filename,
        "content_type": part_content_type,
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


def _resolve_optional_query_bool(query_string: str, name: str) -> bool:
    value = _resolve_single_query_param(query_string, name).lower()
    if not value:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} query parameter must be true or false")


def _resolve_web_vitrina_period_window_from_query(query_string: str) -> tuple[str | None, str | None]:
    query = urllib_parse.parse_qs(query_string)
    date_from = str(query.get("date_from", [""])[0]).strip()
    date_to = str(query.get("date_to", [""])[0]).strip()
    if bool(date_from) != bool(date_to):
        raise ValueError("date_from and date_to must be provided together")
    if date_from and str(query.get("as_of_date", [""])[0]).strip():
        raise ValueError("as_of_date is mutually exclusive with date_from/date_to")
    if not date_from:
        return None, None
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


def _write_json_response(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    payload: Any,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    handler.send_response(status.value)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


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
    handler.wfile.write(body)


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
    handler.wfile.write(body)


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
    handler.wfile.write(body)


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


def _with_factory_order_dataset_urls(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["datasets"] = _map_dataset_urls(normalized.get("datasets"))
    return normalized


def _with_wb_regional_urls(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["shared_datasets"] = _map_dataset_urls(normalized.get("shared_datasets"))
    if isinstance(normalized.get("districts"), list):
        normalized["districts"] = _map_wb_regional_districts(normalized.get("districts"))
    last_result = normalized.get("last_result")
    if isinstance(last_result, Mapping):
        nested = dict(last_result)
        nested["shared_datasets"] = _map_dataset_urls(nested.get("shared_datasets"))
        if isinstance(nested.get("districts"), list):
            nested["districts"] = _map_wb_regional_districts(nested.get("districts"))
        normalized["last_result"] = nested
    return normalized


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
        "factory_order_template_inbound_factory_path": DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FACTORY_PATH,
        "factory_order_template_inbound_ff_to_wb_path": DEFAULT_FACTORY_ORDER_TEMPLATE_INBOUND_FF_TO_WB_PATH,
        "factory_order_upload_stock_ff_path": DEFAULT_FACTORY_ORDER_UPLOAD_STOCK_FF_PATH,
        "factory_order_upload_inbound_factory_path": DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FACTORY_PATH,
        "factory_order_upload_inbound_ff_to_wb_path": DEFAULT_FACTORY_ORDER_UPLOAD_INBOUND_FF_TO_WB_PATH,
        "factory_order_calculate_path": DEFAULT_FACTORY_ORDER_CALCULATE_PATH,
        "factory_order_recommendation_path": DEFAULT_FACTORY_ORDER_RECOMMENDATION_PATH,
        "wb_regional_status_path": DEFAULT_WB_REGIONAL_STATUS_PATH,
        "wb_regional_calculate_path": DEFAULT_WB_REGIONAL_CALCULATE_PATH,
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


def _render_sheet_vitrina_web_vitrina_ui(
    *,
    read_path: str,
    operator_path: str,
    refresh_path: str,
    job_path: str,
) -> str:
    config_payload = {
        "page_title": "Web-витрина",
        "read_path": read_path,
        "operator_path": operator_path,
        "refresh_path": refresh_path,
        "group_refresh_path": DEFAULT_SHEET_WEB_VITRINA_GROUP_REFRESH_PATH,
        "research_options_path": DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_OPTIONS_PATH,
        "research_calculate_path": DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_CALCULATE_PATH,
        "feedbacks_path": DEFAULT_SHEET_FEEDBACKS_PATH,
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
