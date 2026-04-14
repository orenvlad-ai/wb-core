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
from packages.contracts.registry_upload_file_backed_service import RegistryUploadResult
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_UPLOAD_PATH = "/v1/registry-upload/bundle"
DEFAULT_SHEET_PLAN_PATH = "/v1/sheet-vitrina-v1/plan"
DEFAULT_SHEET_REFRESH_PATH = "/v1/sheet-vitrina-v1/refresh"
DEFAULT_RUNTIME_DIR = ROOT / ".runtime" / "registry_upload"


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

    sheet_plan_path = os.environ.get("SHEET_VITRINA_HTTP_PATH", DEFAULT_SHEET_PLAN_PATH).strip() or DEFAULT_SHEET_PLAN_PATH
    if not sheet_plan_path.startswith("/"):
        raise ValueError("SHEET_VITRINA_HTTP_PATH must start with /")

    sheet_refresh_path = (
        os.environ.get("SHEET_VITRINA_REFRESH_HTTP_PATH", DEFAULT_SHEET_REFRESH_PATH).strip()
        or DEFAULT_SHEET_REFRESH_PATH
    )
    if not sheet_refresh_path.startswith("/"):
        raise ValueError("SHEET_VITRINA_REFRESH_HTTP_PATH must start with /")

    raw_runtime_dir = os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", str(DEFAULT_RUNTIME_DIR)).strip()
    runtime_dir = Path(raw_runtime_dir).expanduser()

    return RegistryUploadHttpEntrypointConfig(
        host=host,
        port=port,
        upload_path=upload_path,
        sheet_plan_path=sheet_plan_path,
        sheet_refresh_path=sheet_refresh_path,
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
        sheet_plan_path=config.sheet_plan_path,
        sheet_refresh_path=config.sheet_refresh_path,
    )
    return RegistryUploadHttpServer((config.host, config.port), handler_cls)


def _build_handler(
    entrypoint: RegistryUploadHttpEntrypoint,
    *,
    upload_path: str,
    sheet_plan_path: str,
    sheet_refresh_path: str,
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

            if parsed.path == sheet_refresh_path:
                try:
                    payload = _load_optional_request_payload(self)
                    as_of_date = _resolve_as_of_date(parsed.query, payload)
                except ValueError as exc:
                    _write_json_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc)},
                    )
                    return

                try:
                    refresh_result = entrypoint.handle_sheet_refresh_request(as_of_date=as_of_date or None)
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

            _write_json_response(
                self,
                HTTPStatus.NOT_FOUND,
                {"error": f"unsupported path: {parsed.path}"},
            )
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
            if parsed.path != sheet_plan_path:
                _write_json_response(
                    self,
                    HTTPStatus.NOT_FOUND,
                    {"error": f"unsupported path: {parsed.path}"},
                )
                return

            query = urllib_parse.parse_qs(parsed.query)
            as_of_date = ""
            if query.get("as_of_date"):
                as_of_date = str(query["as_of_date"][0]).strip()

            try:
                payload = entrypoint.handle_sheet_plan_request(as_of_date=as_of_date or None)
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
    query = urllib_parse.parse_qs(query_string)
    query_value = str(query.get("as_of_date", [""])[0]).strip()
    body_value = str(payload.get("as_of_date", "") or "").strip()
    if query_value and body_value and query_value != body_value:
        raise ValueError("as_of_date mismatch between query string and request body")
    return query_value or body_value


def _http_status_for_result(result: RegistryUploadResult) -> HTTPStatus:
    if result.status == "accepted":
        return HTTPStatus.OK

    if any("bundle_version already accepted" in error for error in result.validation_errors):
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
