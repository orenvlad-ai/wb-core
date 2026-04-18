"""Минимальный inbound HTTP entrypoint для registry upload и sheet_vitrina_v1 refresh/read split."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socketserver
from typing import Any, Mapping
from urllib import parse as urllib_parse

from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.cost_price_upload import CostPriceUploadResult
from packages.contracts.registry_upload_file_backed_service import RegistryUploadResult
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_UPLOAD_PATH = "/v1/registry-upload/bundle"
DEFAULT_COST_PRICE_UPLOAD_PATH = "/v1/cost-price/upload"
DEFAULT_SHEET_PLAN_PATH = "/v1/sheet-vitrina-v1/plan"
DEFAULT_SHEET_REFRESH_PATH = "/v1/sheet-vitrina-v1/refresh"
DEFAULT_SHEET_LOAD_PATH = "/v1/sheet-vitrina-v1/load"
DEFAULT_SHEET_STATUS_PATH = "/v1/sheet-vitrina-v1/status"
DEFAULT_SHEET_JOB_PATH = "/v1/sheet-vitrina-v1/job"
DEFAULT_SHEET_OPERATOR_UI_PATH = "/sheet-vitrina-v1/operator"
DEFAULT_RUNTIME_DIR = ROOT / ".runtime" / "registry_upload"
OPERATOR_UI_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "sheet_vitrina_v1_operator.html"


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
                            auto_load=auto_load_requested,
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
                        auto_load=auto_load_requested,
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

            _write_json_response(
                self,
                HTTPStatus.NOT_FOUND,
                {"error": f"unsupported path: {parsed.path}"},
            )
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
            if parsed.path == sheet_operator_ui_path:
                _write_html_response(
                    self,
                    HTTPStatus.OK,
                    _render_sheet_vitrina_operator_ui(
                        refresh_path=sheet_refresh_path,
                        load_path=sheet_load_path,
                        status_path=sheet_status_path,
                        job_path=sheet_job_path,
                    ),
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


def _resolve_as_of_date(query_string: str, payload: Mapping[str, Any]) -> str:
    query_value = _resolve_as_of_date_from_query(query_string)
    body_value = str(payload.get("as_of_date", "") or "").strip()
    if query_value and body_value and query_value != body_value:
        raise ValueError("as_of_date mismatch between query string and request body")
    return query_value or body_value


def _resolve_as_of_date_from_query(query_string: str) -> str:
    query = urllib_parse.parse_qs(query_string)
    return str(query.get("as_of_date", [""])[0]).strip()


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
    return raw


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
        disposition = "attachment" if as_attachment else "inline"
        handler.send_header(
            "Content-Disposition",
            f'{disposition}; filename="{filename}"',
        )
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


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


def _render_sheet_vitrina_operator_ui(
    *,
    refresh_path: str,
    load_path: str,
    status_path: str,
    job_path: str,
) -> str:
    config_payload = {
        "page_title": "Обновление данных витрины",
        "refresh_path": refresh_path,
        "load_path": load_path,
        "status_path": status_path,
        "job_path": job_path,
    }
    template = OPERATOR_UI_TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("__SHEET_VITRINA_V1_OPERATOR_PAGE_TITLE__", config_payload["page_title"])
        .replace("__SHEET_VITRINA_V1_OPERATOR_CONFIG_JSON__", json.dumps(config_payload, ensure_ascii=False))
    )
