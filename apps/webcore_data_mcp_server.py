"""HTTP MCP server for read-only WebCore business data."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
from typing import Any, Mapping
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.webcore_data_mcp import (  # noqa: E402
    APPROVED_TOOL_NAMES,
    WebCoreDataMcpError,
    WebCoreDataMcpGateway,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_MCP_PATH = "/mcp"
DEFAULT_HEALTH_PATH = "/healthz"
DEFAULT_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
DEFAULT_SCOPES = ("wbcore.analytics.read", "wbcore.supply.read", "wbcore.finance.read")
MAX_REQUEST_BODY_BYTES = 256 * 1024
SERVER_NAME = "webcore-data-mcp"
SERVER_VERSION = "0.1.0"


class WebCoreDataMcpServerConfig:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        mcp_path: str,
        health_path: str,
        auth_mode: str,
        bearer_token: str,
        bearer_token_sha256: str,
        runtime_dir: Path | None,
        db_path: Path | None,
        audit_log_path: Path | None,
        resource_url: str,
        resource_documentation_url: str,
        authorization_servers: tuple[str, ...],
        scopes: tuple[str, ...],
    ) -> None:
        self.host = host
        self.port = port
        self.mcp_path = mcp_path
        self.health_path = health_path
        self.auth_mode = auth_mode
        self.bearer_token = bearer_token
        self.bearer_token_sha256 = bearer_token_sha256
        self.runtime_dir = runtime_dir
        self.db_path = db_path
        self.audit_log_path = audit_log_path
        self.resource_url = resource_url
        self.resource_documentation_url = resource_documentation_url
        self.authorization_servers = authorization_servers
        self.scopes = scopes


def load_config_from_env() -> WebCoreDataMcpServerConfig:
    host = os.environ.get("WEBCORE_DATA_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = _parse_port(os.environ.get("WEBCORE_DATA_MCP_PORT", str(DEFAULT_PORT)))
    mcp_path = _normalize_path(os.environ.get("WEBCORE_DATA_MCP_PATH", DEFAULT_MCP_PATH), "WEBCORE_DATA_MCP_PATH")
    health_path = _normalize_path(
        os.environ.get("WEBCORE_DATA_MCP_HEALTH_PATH", DEFAULT_HEALTH_PATH),
        "WEBCORE_DATA_MCP_HEALTH_PATH",
    )
    auth_mode = (os.environ.get("WEBCORE_DATA_MCP_AUTH_MODE", "bearer").strip() or "bearer").lower()
    if auth_mode not in {"bearer", "disabled"}:
        raise ValueError("WEBCORE_DATA_MCP_AUTH_MODE must be bearer or disabled")
    if auth_mode == "disabled" and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("WEBCORE_DATA_MCP_AUTH_MODE=disabled is allowed only on loopback hosts")
    bearer_token = os.environ.get("WEBCORE_DATA_MCP_BEARER_TOKEN", "")
    bearer_token_sha256 = os.environ.get("WEBCORE_DATA_MCP_BEARER_TOKEN_SHA256", "")
    if auth_mode == "bearer" and not bearer_token and not bearer_token_sha256:
        raise ValueError("bearer auth requires WEBCORE_DATA_MCP_BEARER_TOKEN or WEBCORE_DATA_MCP_BEARER_TOKEN_SHA256")
    runtime_dir = _optional_path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR"))
    db_path = _optional_path(os.environ.get("WEBCORE_DATA_MCP_DB_PATH"))
    audit_log_path = _optional_path(os.environ.get("WEBCORE_DATA_MCP_AUDIT_LOG_PATH"))
    resource_url = os.environ.get("WEBCORE_DATA_MCP_RESOURCE_URL", "").strip()
    resource_documentation_url = os.environ.get("WEBCORE_DATA_MCP_RESOURCE_DOCUMENTATION_URL", "").strip()
    auth_servers = _csv(os.environ.get("WEBCORE_DATA_MCP_AUTHORIZATION_SERVERS", ""))
    scopes = tuple(_csv(os.environ.get("WEBCORE_DATA_MCP_SCOPES", ",".join(DEFAULT_SCOPES))) or DEFAULT_SCOPES)
    return WebCoreDataMcpServerConfig(
        host=host,
        port=port,
        mcp_path=mcp_path,
        health_path=health_path,
        auth_mode=auth_mode,
        bearer_token=bearer_token,
        bearer_token_sha256=bearer_token_sha256,
        runtime_dir=runtime_dir,
        db_path=db_path,
        audit_log_path=audit_log_path,
        resource_url=resource_url,
        resource_documentation_url=resource_documentation_url,
        authorization_servers=auth_servers,
        scopes=scopes,
    )


def build_server(config: WebCoreDataMcpServerConfig | None = None) -> HTTPServer:
    resolved_config = config or load_config_from_env()
    gateway = WebCoreDataMcpGateway(
        runtime_dir=resolved_config.runtime_dir,
        db_path=resolved_config.db_path,
        audit_log_path=resolved_config.audit_log_path,
    )
    handler_cls = _build_handler(resolved_config, gateway)
    return HTTPServer((resolved_config.host, resolved_config.port), handler_cls)


def _build_handler(config: WebCoreDataMcpServerConfig, gateway: WebCoreDataMcpGateway) -> type[BaseHTTPRequestHandler]:
    class WebCoreDataMcpHandler(BaseHTTPRequestHandler):
        server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Allow", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == config.health_path:
                _write_json(self, HTTPStatus.OK, {"status": "ok", "server": SERVER_NAME})
                return
            if parsed.path == DEFAULT_PROTECTED_RESOURCE_PATH:
                _write_json(self, HTTPStatus.OK, _protected_resource_metadata(config, self))
                return
            if parsed.path == config.mcp_path:
                _write_json(
                    self,
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "mcp endpoint accepts JSON-RPC POST requests only"},
                )
                return
            _write_json(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != config.mcp_path:
                _write_json(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            identity = _authorize(self, config)
            if identity is None:
                _write_unauthorized(self, config)
                return
            try:
                payload = _read_json_request(self)
            except ValueError as exc:
                _write_json_rpc_error(self, None, -32700, str(exc))
                return
            if isinstance(payload, list):
                responses = [_handle_json_rpc(item, gateway, identity) for item in payload]
                responses = [item for item in responses if item is not None]
                if not responses:
                    self.send_response(HTTPStatus.ACCEPTED)
                    self.end_headers()
                    return
                _write_json(self, HTTPStatus.OK, responses)
                return
            response = _handle_json_rpc(payload, gateway, identity)
            if response is None:
                self.send_response(HTTPStatus.ACCEPTED)
                self.end_headers()
                return
            _write_json(self, HTTPStatus.OK, response)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return WebCoreDataMcpHandler


def _handle_json_rpc(payload: Any, gateway: WebCoreDataMcpGateway, identity: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return _json_rpc_error(None, -32600, "JSON-RPC request must be an object")
    request_id = payload.get("id")
    method = str(payload.get("method") or "")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    if not request_id and method.startswith("notifications/"):
        return None
    try:
        if method == "initialize":
            return _json_rpc_result(
                request_id,
                {
                    "protocolVersion": str(params.get("protocolVersion") or "2025-06-18"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "WebCore Data MCP exposes read-only, allowlisted business analytics tools only. "
                        "No SQL, shell, sync/backfill, raw files, secrets or unbounded payloads are available."
                    ),
                },
            )
        if method == "ping":
            return _json_rpc_result(request_id, {})
        if method == "tools/list":
            return _json_rpc_result(request_id, {"tools": gateway.list_tools()})
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            result = gateway.call_tool(name, arguments, identity=identity)
            return _json_rpc_result(request_id, _tool_result(result))
        if method in {"resources/list", "prompts/list"}:
            key = "resources" if method == "resources/list" else "prompts"
            return _json_rpc_result(request_id, {key: []})
        return _json_rpc_error(request_id, -32601, f"method not found: {method}")
    except WebCoreDataMcpError as exc:
        return _json_rpc_error(request_id, -32000, str(exc), data={"code": exc.code})
    except Exception as exc:
        return _json_rpc_error(request_id, -32001, f"tool execution failed: {_safe_error(str(exc))}")


def _tool_result(result: Mapping[str, Any]) -> dict[str, Any]:
    text = json.dumps(result, ensure_ascii=False, sort_keys=True)
    return {
        "structuredContent": result,
        "content": [{"type": "text", "text": text}],
    }


def _authorize(handler: BaseHTTPRequestHandler, config: WebCoreDataMcpServerConfig) -> str | None:
    if config.auth_mode == "disabled":
        return "loopback-disabled-auth"
    header = handler.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return None
    token = header[len(prefix) :].strip()
    if not token:
        return None
    if config.bearer_token and hmac.compare_digest(token, config.bearer_token):
        return _token_identity(token)
    if config.bearer_token_sha256:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if hmac.compare_digest(digest, config.bearer_token_sha256):
            return _token_identity(token)
    return None


def _write_unauthorized(handler: BaseHTTPRequestHandler, config: WebCoreDataMcpServerConfig) -> None:
    metadata_url = _metadata_url(config, handler)
    challenge = f'Bearer resource_metadata="{metadata_url}", scope="{" ".join(config.scopes)}"'
    body = {
        "error": "authentication_required",
        "auth": "bearer_or_oauth_resource_metadata",
        "resource_metadata": metadata_url,
    }
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    handler.send_response(HTTPStatus.UNAUTHORIZED)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("WWW-Authenticate", challenge)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _protected_resource_metadata(
    config: WebCoreDataMcpServerConfig,
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    resource = config.resource_url or _origin_for_request(handler)
    metadata: dict[str, Any] = {
        "resource": resource,
        "scopes_supported": list(config.scopes),
    }
    if config.authorization_servers:
        metadata["authorization_servers"] = list(config.authorization_servers)
    if config.resource_documentation_url:
        metadata["resource_documentation"] = config.resource_documentation_url
    return metadata


def _metadata_url(config: WebCoreDataMcpServerConfig, handler: BaseHTTPRequestHandler) -> str:
    return f"{config.resource_url or _origin_for_request(handler)}{DEFAULT_PROTECTED_RESOURCE_PATH}"


def _origin_for_request(handler: BaseHTTPRequestHandler) -> str:
    host = handler.headers.get("Host", f"{DEFAULT_HOST}:{DEFAULT_PORT}")
    proto = handler.headers.get("X-Forwarded-Proto", "http")
    return f"{proto}://{host}"


def _read_json_request(handler: BaseHTTPRequestHandler) -> Any:
    length_raw = handler.headers.get("Content-Length", "0")
    try:
        length = int(length_raw)
    except ValueError as exc:
        raise ValueError("invalid Content-Length") from exc
    if length <= 0:
        raise ValueError("empty request body")
    if length > MAX_REQUEST_BODY_BYTES:
        raise ValueError("request body too large")
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ValueError("invalid JSON body") from exc


def _write_json_rpc_error(handler: BaseHTTPRequestHandler, request_id: Any, code: int, message: str) -> None:
    _write_json(handler, HTTPStatus.OK, _json_rpc_error(request_id, code, message))


def _json_rpc_result(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _json_rpc_error(request_id: Any, code: int, message: str, *, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = dict(data)
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _write_json(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _parse_port(raw: str) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError("WEBCORE_DATA_MCP_PORT must be an integer") from exc
    if value < 0 or value > 65535:
        raise ValueError("WEBCORE_DATA_MCP_PORT must be between 0 and 65535")
    return value


def _normalize_path(raw: str, env_name: str) -> str:
    path = str(raw or "").strip()
    if not path.startswith("/"):
        raise ValueError(f"{env_name} must start with /")
    return path


def _optional_path(raw: str | None) -> Path | None:
    if raw is None or not str(raw).strip():
        return None
    return Path(str(raw).strip()).expanduser()


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(raw or "").split(",") if item.strip())


def _token_identity(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def _safe_error(value: str) -> str:
    lowered = value.lower()
    if any(marker in lowered for marker in ("token", "secret", "password", "authorization", "cookie")):
        return "[redacted]"
    return value[:240]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the WebCore Data MCP server.")
    parser.add_argument("--print-config", action="store_true", help="Print sanitized config and exit.")
    args = parser.parse_args(argv)
    config = load_config_from_env()
    if args.print_config:
        print(
            json.dumps(
                {
                    "host": config.host,
                    "port": config.port,
                    "mcp_path": config.mcp_path,
                    "health_path": config.health_path,
                    "auth_mode": config.auth_mode,
                    "has_bearer_token": bool(config.bearer_token or config.bearer_token_sha256),
                    "runtime_dir": str(config.runtime_dir) if config.runtime_dir else "",
                    "db_path": str(config.db_path) if config.db_path else "",
                    "audit_log_path": str(config.audit_log_path) if config.audit_log_path else "",
                    "resource_url": config.resource_url,
                    "resource_documentation_url": config.resource_documentation_url,
                    "scopes": list(config.scopes),
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 0
    server = build_server(config)
    print(f"{SERVER_NAME}: listening on {config.host}:{config.port}{config.mcp_path}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
