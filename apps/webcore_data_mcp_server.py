"""HTTP MCP server for read-only WebCore business data."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import html
import hmac
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import secrets
import sys
import time
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.webcore_data_mcp import (  # noqa: E402
    APPROVED_TOOL_NAMES,
    SCOPE_ANALYTICS_READ,
    SCOPE_FINANCE_READ,
    SCOPE_SUPPLY_READ,
    WebCoreDataMcpError,
    WebCoreDataMcpGateway,
    tool_required_scope,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_MCP_PATH = "/mcp"
DEFAULT_HEALTH_PATH = "/healthz"
DEFAULT_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
DEFAULT_AUTHORIZATION_SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server"
DEFAULT_OPENID_CONFIGURATION_PATH = "/.well-known/openid-configuration"
DEFAULT_OAUTH_AUTHORIZE_PATH = "/oauth/authorize"
DEFAULT_OAUTH_TOKEN_PATH = "/oauth/token"
DEFAULT_SCOPES = (SCOPE_ANALYTICS_READ, SCOPE_SUPPLY_READ, SCOPE_FINANCE_READ)
DEFAULT_OAUTH_CODE_TTL_SECONDS = 300
DEFAULT_OAUTH_ACCESS_TOKEN_TTL_SECONDS = 3600
DEFAULT_OAUTH_ALLOWED_REDIRECT_PREFIXES = (
    "https://chatgpt.com/connector/oauth/",
    "https://chatgpt.com/connector_platform_oauth_redirect",
)
DEFAULT_OAUTH_ALLOWED_CLIENT_ID_PREFIXES = ("https://chatgpt.com/oauth/",)
MAX_REQUEST_BODY_BYTES = 256 * 1024
MAX_FORM_BODY_BYTES = 64 * 1024
MAX_OAUTH_FIELD_CHARS = 2048
SERVER_NAME = "webcore-data-mcp"
SERVER_VERSION = "0.1.0"
WEB_AUTH_COOKIE_NAME = "wb_core_web_session"
_OAUTH_LOGIN_FAILURES: dict[str, list[float]] = {}


@dataclass(frozen=True)
class AuthIdentity:
    label: str
    scopes: tuple[str, ...]
    auth_type: str


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
        oauth_signing_secret: str,
        oauth_owner_username: str,
        oauth_owner_password_hash: str,
        oauth_session_secret: str,
        oauth_code_store_path: Path | None,
        oauth_allowed_redirect_prefixes: tuple[str, ...],
        oauth_allowed_client_id_prefixes: tuple[str, ...],
        oauth_code_ttl_seconds: int,
        oauth_access_token_ttl_seconds: int,
        oauth_issuer: str,
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
        self.oauth_signing_secret = oauth_signing_secret
        self.oauth_owner_username = oauth_owner_username
        self.oauth_owner_password_hash = oauth_owner_password_hash
        self.oauth_session_secret = oauth_session_secret
        self.oauth_code_store_path = oauth_code_store_path
        self.oauth_allowed_redirect_prefixes = oauth_allowed_redirect_prefixes
        self.oauth_allowed_client_id_prefixes = oauth_allowed_client_id_prefixes
        self.oauth_code_ttl_seconds = oauth_code_ttl_seconds
        self.oauth_access_token_ttl_seconds = oauth_access_token_ttl_seconds
        self.oauth_issuer = oauth_issuer

    @property
    def oauth_enabled(self) -> bool:
        return self.auth_mode in {"oauth", "bearer_oauth"}

    @property
    def bearer_enabled(self) -> bool:
        return self.auth_mode in {"bearer", "bearer_oauth"}


def load_config_from_env() -> WebCoreDataMcpServerConfig:
    host = os.environ.get("WEBCORE_DATA_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = _parse_port(os.environ.get("WEBCORE_DATA_MCP_PORT", str(DEFAULT_PORT)))
    mcp_path = _normalize_path(os.environ.get("WEBCORE_DATA_MCP_PATH", DEFAULT_MCP_PATH), "WEBCORE_DATA_MCP_PATH")
    health_path = _normalize_path(
        os.environ.get("WEBCORE_DATA_MCP_HEALTH_PATH", DEFAULT_HEALTH_PATH),
        "WEBCORE_DATA_MCP_HEALTH_PATH",
    )
    auth_mode = (os.environ.get("WEBCORE_DATA_MCP_AUTH_MODE", "bearer").strip() or "bearer").lower()
    if auth_mode not in {"bearer", "oauth", "bearer_oauth", "disabled"}:
        raise ValueError("WEBCORE_DATA_MCP_AUTH_MODE must be bearer, oauth, bearer_oauth or disabled")
    if auth_mode == "disabled" and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("WEBCORE_DATA_MCP_AUTH_MODE=disabled is allowed only on loopback hosts")
    bearer_token = os.environ.get("WEBCORE_DATA_MCP_BEARER_TOKEN", "")
    bearer_token_sha256 = os.environ.get("WEBCORE_DATA_MCP_BEARER_TOKEN_SHA256", "")
    if auth_mode in {"bearer", "bearer_oauth"} and not bearer_token and not bearer_token_sha256:
        raise ValueError("bearer auth requires WEBCORE_DATA_MCP_BEARER_TOKEN or WEBCORE_DATA_MCP_BEARER_TOKEN_SHA256")
    runtime_dir = _optional_path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR"))
    db_path = _optional_path(os.environ.get("WEBCORE_DATA_MCP_DB_PATH"))
    audit_log_path = _optional_path(os.environ.get("WEBCORE_DATA_MCP_AUDIT_LOG_PATH"))
    resource_url = os.environ.get("WEBCORE_DATA_MCP_RESOURCE_URL", "").strip()
    resource_documentation_url = os.environ.get("WEBCORE_DATA_MCP_RESOURCE_DOCUMENTATION_URL", "").strip()
    auth_servers = _csv(os.environ.get("WEBCORE_DATA_MCP_AUTHORIZATION_SERVERS", ""))
    scopes = tuple(_csv(os.environ.get("WEBCORE_DATA_MCP_SCOPES", ",".join(DEFAULT_SCOPES))) or DEFAULT_SCOPES)
    oauth_enabled = auth_mode in {"oauth", "bearer_oauth"}
    oauth_issuer = os.environ.get("WEBCORE_DATA_MCP_OAUTH_ISSUER", "").strip() or resource_url
    if oauth_enabled and not auth_servers and oauth_issuer:
        auth_servers = (oauth_issuer,)
    oauth_signing_secret = os.environ.get("WEBCORE_DATA_MCP_OAUTH_SIGNING_SECRET", "")
    oauth_owner_username = (
        os.environ.get("WEBCORE_DATA_MCP_OAUTH_OWNER_USERNAME", "")
        or os.environ.get("WB_CORE_WEB_AUTH_USERNAME", "")
    ).strip()
    oauth_owner_password_hash = (
        os.environ.get("WEBCORE_DATA_MCP_OAUTH_OWNER_PASSWORD_HASH", "")
        or os.environ.get("WB_CORE_WEB_AUTH_PASSWORD_HASH", "")
    ).strip()
    oauth_session_secret = (
        os.environ.get("WEBCORE_DATA_MCP_OAUTH_SESSION_SECRET", "")
        or os.environ.get("WB_CORE_WEB_AUTH_SESSION_SECRET", "")
    ).strip()
    default_code_store_path = None
    if runtime_dir is not None:
        default_code_store_path = runtime_dir / "webcore_data_mcp_oauth_codes.json"
    oauth_code_store_path = _optional_path(os.environ.get("WEBCORE_DATA_MCP_OAUTH_CODE_STORE_PATH")) or default_code_store_path
    oauth_allowed_redirect_prefixes = tuple(
        _csv(os.environ.get("WEBCORE_DATA_MCP_OAUTH_ALLOWED_REDIRECT_PREFIXES", ""))
        or DEFAULT_OAUTH_ALLOWED_REDIRECT_PREFIXES
    )
    oauth_allowed_client_id_prefixes = tuple(
        _csv(os.environ.get("WEBCORE_DATA_MCP_OAUTH_ALLOWED_CLIENT_ID_PREFIXES", ""))
        or DEFAULT_OAUTH_ALLOWED_CLIENT_ID_PREFIXES
    )
    oauth_code_ttl_seconds = _parse_positive_int(
        os.environ.get("WEBCORE_DATA_MCP_OAUTH_CODE_TTL_SECONDS"),
        DEFAULT_OAUTH_CODE_TTL_SECONDS,
        "WEBCORE_DATA_MCP_OAUTH_CODE_TTL_SECONDS",
    )
    oauth_access_token_ttl_seconds = _parse_positive_int(
        os.environ.get("WEBCORE_DATA_MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS"),
        DEFAULT_OAUTH_ACCESS_TOKEN_TTL_SECONDS,
        "WEBCORE_DATA_MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS",
    )
    if oauth_enabled:
        if not resource_url.startswith("https://"):
            raise ValueError("OAuth mode requires WEBCORE_DATA_MCP_RESOURCE_URL with https://")
        if not oauth_issuer.startswith("https://"):
            raise ValueError("OAuth mode requires an https:// WEBCORE_DATA_MCP_OAUTH_ISSUER")
        if not auth_servers:
            raise ValueError("OAuth mode requires at least one authorization server")
        if len(oauth_signing_secret) < 32:
            raise ValueError("OAuth mode requires WEBCORE_DATA_MCP_OAUTH_SIGNING_SECRET with at least 32 characters")
        if not oauth_owner_username or not oauth_owner_password_hash:
            raise ValueError("OAuth mode requires owner username and password hash env")
        if oauth_code_store_path is None:
            raise ValueError("OAuth mode requires REGISTRY_UPLOAD_RUNTIME_DIR or WEBCORE_DATA_MCP_OAUTH_CODE_STORE_PATH")
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
        oauth_signing_secret=oauth_signing_secret,
        oauth_owner_username=oauth_owner_username,
        oauth_owner_password_hash=oauth_owner_password_hash,
        oauth_session_secret=oauth_session_secret,
        oauth_code_store_path=oauth_code_store_path,
        oauth_allowed_redirect_prefixes=oauth_allowed_redirect_prefixes,
        oauth_allowed_client_id_prefixes=oauth_allowed_client_id_prefixes,
        oauth_code_ttl_seconds=oauth_code_ttl_seconds,
        oauth_access_token_ttl_seconds=oauth_access_token_ttl_seconds,
        oauth_issuer=oauth_issuer,
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
            self.send_header("Allow", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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
            if parsed.path in {DEFAULT_AUTHORIZATION_SERVER_METADATA_PATH, DEFAULT_OPENID_CONFIGURATION_PATH}:
                _write_json(self, HTTPStatus.OK, _authorization_server_metadata(config, self))
                return
            if parsed.path == DEFAULT_OAUTH_AUTHORIZE_PATH:
                _handle_authorize_get(self, config, parsed.query)
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
            if parsed.path == DEFAULT_OAUTH_AUTHORIZE_PATH:
                _handle_authorize_post(self, config)
                return
            if parsed.path == DEFAULT_OAUTH_TOKEN_PATH:
                _handle_token_post(self, config)
                return
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


def _handle_json_rpc(payload: Any, gateway: WebCoreDataMcpGateway, identity: AuthIdentity) -> dict[str, Any] | None:
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
            required_scope = tool_required_scope(name)
            if required_scope not in identity.scopes:
                return _json_rpc_error(
                    request_id,
                    -32003,
                    "insufficient OAuth scope",
                    data={
                        "code": "insufficient_scope",
                        "_meta": {"mcp/www_authenticate": f'Bearer scope="{required_scope}"'},
                    },
                )
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            result = gateway.call_tool(name, arguments, identity=identity.label)
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


def _authorize(handler: BaseHTTPRequestHandler, config: WebCoreDataMcpServerConfig) -> AuthIdentity | None:
    if config.auth_mode == "disabled":
        return AuthIdentity("loopback-disabled-auth", tuple(config.scopes), "disabled")
    header = handler.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return None
    token = header[len(prefix) :].strip()
    if not token:
        return None
    if config.bearer_enabled and config.bearer_token and hmac.compare_digest(token, config.bearer_token):
        return AuthIdentity(f"bearer:{_token_identity(token)}", tuple(config.scopes), "bearer")
    if config.bearer_enabled and config.bearer_token_sha256:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if hmac.compare_digest(digest, config.bearer_token_sha256):
            return AuthIdentity(f"bearer:{_token_identity(token)}", tuple(config.scopes), "bearer")
    if config.oauth_enabled:
        oauth_identity = _verify_oauth_access_token(token, config)
        if oauth_identity is not None:
            return oauth_identity
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


def _authorization_server_metadata(
    config: WebCoreDataMcpServerConfig,
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    issuer = config.oauth_issuer or config.resource_url or _origin_for_request(handler)
    metadata: dict[str, Any] = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}{DEFAULT_OAUTH_AUTHORIZE_PATH}",
        "token_endpoint": f"{issuer}{DEFAULT_OAUTH_TOKEN_PATH}",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(config.scopes),
        "resource": config.resource_url or _origin_for_request(handler),
        "client_id_metadata_document_supported": True,
    }
    return metadata


def _handle_authorize_get(handler: BaseHTTPRequestHandler, config: WebCoreDataMcpServerConfig, query: str) -> None:
    if not config.oauth_enabled:
        _write_json(handler, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        return
    params = _single_value_params(parse_qs(query, keep_blank_values=True))
    validation = _validate_authorize_params(params, config)
    if validation.get("error"):
        _write_oauth_error(handler, HTTPStatus.BAD_REQUEST, str(validation["error"]))
        return
    session_user = _authenticated_oauth_session_user(handler, config)
    if session_user:
        try:
            _issue_authorization_redirect(handler, config, params, subject=session_user)
        except Exception as exc:
            _write_oauth_error(handler, HTTPStatus.INTERNAL_SERVER_ERROR, _safe_error(str(exc)))
        return
    _write_html(handler, HTTPStatus.OK, _authorize_form_html(params))


def _handle_authorize_post(handler: BaseHTTPRequestHandler, config: WebCoreDataMcpServerConfig) -> None:
    if not config.oauth_enabled:
        _write_json(handler, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        return
    try:
        form = _read_form_request(handler)
    except ValueError as exc:
        _write_oauth_error(handler, HTTPStatus.BAD_REQUEST, str(exc))
        return
    params = {key: value for key, value in form.items() if key not in {"username", "password"}}
    validation = _validate_authorize_params(params, config)
    if validation.get("error"):
        _write_oauth_error(handler, HTTPStatus.BAD_REQUEST, str(validation["error"]))
        return
    client_key = _client_key(handler)
    if _oauth_rate_limited(client_key):
        _write_html(handler, HTTPStatus.TOO_MANY_REQUESTS, _authorize_form_html(params, error="Too many attempts. Try later."))
        return
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    if not _verify_owner_password(username, password, config):
        _record_oauth_login_failure(client_key)
        _write_html(handler, HTTPStatus.UNAUTHORIZED, _authorize_form_html(params, error="Invalid username or password."))
        return
    _clear_oauth_login_failures(client_key)
    try:
        _issue_authorization_redirect(handler, config, params, subject=config.oauth_owner_username)
    except Exception as exc:
        _write_oauth_error(handler, HTTPStatus.INTERNAL_SERVER_ERROR, _safe_error(str(exc)))


def _handle_token_post(handler: BaseHTTPRequestHandler, config: WebCoreDataMcpServerConfig) -> None:
    if not config.oauth_enabled:
        _write_json(handler, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        return
    try:
        form = _read_form_request(handler)
    except ValueError as exc:
        _write_token_error(handler, HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        return
    if form.get("grant_type") != "authorization_code":
        _write_token_error(handler, HTTPStatus.BAD_REQUEST, "unsupported_grant_type", "grant_type must be authorization_code")
        return
    code = str(form.get("code") or "")
    redirect_uri = str(form.get("redirect_uri") or "")
    client_id = str(form.get("client_id") or "")
    code_verifier = str(form.get("code_verifier") or "")
    resource = str(form.get("resource") or "")
    if not code or not redirect_uri or not client_id or not code_verifier:
        _write_token_error(handler, HTTPStatus.BAD_REQUEST, "invalid_request", "missing required token parameter")
        return
    try:
        token_result = _exchange_authorization_code(
            config,
            code=code,
            redirect_uri=redirect_uri,
            client_id=client_id,
            code_verifier=code_verifier,
            resource=resource,
        )
    except Exception as exc:
        _write_token_error(handler, HTTPStatus.INTERNAL_SERVER_ERROR, "server_error", _safe_error(str(exc)))
        return
    if token_result.get("error"):
        _write_token_error(handler, HTTPStatus.BAD_REQUEST, str(token_result["error"]), str(token_result.get("detail") or ""))
        return
    _write_json(handler, HTTPStatus.OK, token_result)


def _metadata_url(config: WebCoreDataMcpServerConfig, handler: BaseHTTPRequestHandler) -> str:
    return f"{config.resource_url or _origin_for_request(handler)}{DEFAULT_PROTECTED_RESOURCE_PATH}"


def _origin_for_request(handler: BaseHTTPRequestHandler) -> str:
    host = handler.headers.get("Host", f"{DEFAULT_HOST}:{DEFAULT_PORT}")
    proto = handler.headers.get("X-Forwarded-Proto", "http")
    return f"{proto}://{host}"


def _validate_authorize_params(params: Mapping[str, str], config: WebCoreDataMcpServerConfig) -> dict[str, str]:
    required = ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state", "resource")
    for key in required:
        if not str(params.get(key) or ""):
            return {"error": f"missing {key}"}
    if params.get("response_type") != "code":
        return {"error": "response_type must be code"}
    if params.get("code_challenge_method") != "S256":
        return {"error": "code_challenge_method must be S256"}
    for key, value in params.items():
        if len(str(value)) > MAX_OAUTH_FIELD_CHARS:
            return {"error": f"{key} is too long"}
    if not _allowed_by_prefix(str(params["client_id"]), config.oauth_allowed_client_id_prefixes):
        return {"error": "client_id is not allowed"}
    if not _allowed_redirect_uri(str(params["redirect_uri"]), config):
        return {"error": "redirect_uri is not allowed"}
    if str(params["resource"]) != _resource(config):
        return {"error": "resource mismatch"}
    if not _valid_pkce_challenge(str(params["code_challenge"])):
        return {"error": "invalid code_challenge"}
    scope_result = _validated_scope(params.get("scope", ""), config)
    if scope_result.get("error"):
        return {"error": str(scope_result["error"])}
    return {}


def _issue_authorization_redirect(
    handler: BaseHTTPRequestHandler,
    config: WebCoreDataMcpServerConfig,
    params: Mapping[str, str],
    *,
    subject: str,
) -> None:
    scope_result = _validated_scope(params.get("scope", ""), config)
    scopes = tuple(scope_result.get("scopes") or config.scopes)
    code = secrets.token_urlsafe(32)
    now = int(time.time())
    record = {
        "client_id": str(params["client_id"]),
        "redirect_uri": str(params["redirect_uri"]),
        "code_challenge": str(params["code_challenge"]),
        "resource": str(params["resource"]),
        "scope": " ".join(scopes),
        "subject": subject,
        "expires_at": now + config.oauth_code_ttl_seconds,
        "used": False,
    }
    _store_authorization_code(config, code, record)
    query = {"code": code, "state": str(params["state"])}
    location = str(params["redirect_uri"])
    separator = "&" if "?" in location else "?"
    _write_redirect(handler, location + separator + urlencode(query))


def _exchange_authorization_code(
    config: WebCoreDataMcpServerConfig,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: str,
    resource: str,
) -> dict[str, Any]:
    if resource != _resource(config):
        return {"error": "invalid_target", "detail": "resource mismatch"}
    if not _valid_pkce_verifier(code_verifier):
        return {"error": "invalid_grant", "detail": "invalid code_verifier"}
    code_hash = _hash_text(code)
    store = _load_code_store(config)
    record = store.get(code_hash)
    now = int(time.time())
    if not isinstance(record, dict):
        return {"error": "invalid_grant", "detail": "unknown authorization code"}
    if bool(record.get("used")):
        return {"error": "invalid_grant", "detail": "authorization code already used"}
    if int(record.get("expires_at") or 0) < now:
        return {"error": "invalid_grant", "detail": "authorization code expired"}
    if str(record.get("redirect_uri") or "") != redirect_uri or str(record.get("client_id") or "") != client_id:
        return {"error": "invalid_grant", "detail": "authorization code binding mismatch"}
    if str(record.get("resource") or "") != resource:
        return {"error": "invalid_target", "detail": "authorization code resource mismatch"}
    expected_challenge = str(record.get("code_challenge") or "")
    if not hmac.compare_digest(expected_challenge, _pkce_s256_challenge(code_verifier)):
        return {"error": "invalid_grant", "detail": "PKCE verification failed"}
    record["used"] = True
    store[code_hash] = record
    _save_code_store(config, store)
    scopes = tuple(item for item in str(record.get("scope") or "").split() if item)
    access_token = _make_access_token(
        config,
        subject=str(record.get("subject") or config.oauth_owner_username),
        client_id=client_id,
        scopes=scopes,
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": config.oauth_access_token_ttl_seconds,
        "scope": " ".join(scopes),
    }


def _make_access_token(
    config: WebCoreDataMcpServerConfig,
    *,
    subject: str,
    client_id: str,
    scopes: tuple[str, ...],
) -> str:
    now = int(time.time())
    payload = {
        "iss": config.oauth_issuer,
        "sub": subject,
        "aud": _resource(config),
        "client_id": client_id,
        "scope": " ".join(scopes),
        "iat": now,
        "exp": now + config.oauth_access_token_ttl_seconds,
        "jti": secrets.token_urlsafe(16),
    }
    body = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64url(hmac.new(config.oauth_signing_secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"wc1.{body}.{sig}"


def _verify_oauth_access_token(token: str, config: WebCoreDataMcpServerConfig) -> AuthIdentity | None:
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "wc1":
        return None
    body = parts[1]
    expected_sig = _b64url(hmac.new(config.oauth_signing_secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected_sig, parts[2]):
        return None
    try:
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception:
        return None
    now = int(time.time())
    if str(payload.get("iss") or "") != config.oauth_issuer:
        return None
    if str(payload.get("aud") or "") != _resource(config):
        return None
    if int(payload.get("exp") or 0) <= now:
        return None
    scopes = tuple(item for item in str(payload.get("scope") or "").split() if item in config.scopes)
    if not scopes:
        return None
    subject = str(payload.get("sub") or "oauth")
    token_id = str(payload.get("jti") or _token_identity(token))
    return AuthIdentity(f"oauth:{subject}:{_hash_text(token_id)[:16]}", scopes, "oauth")


def _store_authorization_code(config: WebCoreDataMcpServerConfig, code: str, record: Mapping[str, Any]) -> None:
    store = _load_code_store(config)
    store[_hash_text(code)] = dict(record)
    _save_code_store(config, store)


def _load_code_store(config: WebCoreDataMcpServerConfig) -> dict[str, Any]:
    path = config.oauth_code_store_path
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"OAuth code store is unreadable: {_safe_error(str(exc))}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("OAuth code store must contain a JSON object")
    now = int(time.time())
    return {
        str(key): value
        for key, value in payload.items()
        if isinstance(value, dict) and int(value.get("expires_at") or 0) >= now - 60
    }


def _save_code_store(config: WebCoreDataMcpServerConfig, store: Mapping[str, Any]) -> None:
    path = config.oauth_code_store_path
    if path is None:
        raise RuntimeError("OAuth code store path is not configured")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(dict(store), ensure_ascii=True, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _authenticated_oauth_session_user(handler: BaseHTTPRequestHandler, config: WebCoreDataMcpServerConfig) -> str | None:
    if not config.oauth_session_secret:
        return None
    cookie = handler.headers.get("Cookie", "")
    prefix = WEB_AUTH_COOKIE_NAME + "="
    for part in cookie.split(";"):
        item = part.strip()
        if not item.startswith(prefix):
            continue
        raw = item[len(prefix) :]
        user = _verify_web_session_cookie(raw, config)
        if user:
            return user
    return None


def _verify_web_session_cookie(raw: str, config: WebCoreDataMcpServerConfig) -> str | None:
    if "." not in raw:
        return None
    payload_b64, sig = raw.rsplit(".", 1)
    expected = _b64url(hmac.new(config.oauth_session_secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp") or 0) <= int(time.time()):
        return None
    username = str(payload.get("u") or "")
    if username != config.oauth_owner_username:
        return None
    return username


def _verify_owner_password(username: str, password: str, config: WebCoreDataMcpServerConfig) -> bool:
    if username != config.oauth_owner_username or not password:
        return False
    return _verify_pbkdf2_sha256(password, config.oauth_owner_password_hash)


def _verify_pbkdf2_sha256(password: str, encoded: str) -> bool:
    parts = str(encoded or "").split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
        salt = _b64url_decode(parts[2])
        expected = _b64url_decode(parts[3])
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _validated_scope(raw_scope: str | None, config: WebCoreDataMcpServerConfig) -> dict[str, Any]:
    scopes = tuple(item for item in str(raw_scope or "").split() if item)
    if not scopes:
        return {"scopes": tuple(config.scopes)}
    unsupported = [scope for scope in scopes if scope not in config.scopes]
    if unsupported:
        return {"error": "unsupported scope"}
    return {"scopes": scopes}


def _read_form_request(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length_raw = handler.headers.get("Content-Length", "0")
    try:
        length = int(length_raw)
    except ValueError as exc:
        raise ValueError("invalid Content-Length") from exc
    if length <= 0:
        raise ValueError("empty request body")
    if length > MAX_FORM_BODY_BYTES:
        raise ValueError("request body too large")
    raw = handler.rfile.read(length).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    return _single_value_params(parsed)


def _single_value_params(parsed: Mapping[str, list[str]]) -> dict[str, str]:
    return {str(key): str(values[-1] if values else "") for key, values in parsed.items()}


def _authorize_form_html(params: Mapping[str, str], *, error: str = "") -> str:
    hidden = "\n".join(
        f'<input type="hidden" name="{html.escape(str(key), quote=True)}" value="{html.escape(str(value), quote=True)}">'
        for key, value in sorted(params.items())
        if key not in {"username", "password"}
    )
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WebCore Data MCP OAuth</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 420px; margin: 48px auto; padding: 0 16px; }}
    label {{ display: block; margin: 12px 0 4px; }}
    input {{ width: 100%; box-sizing: border-box; padding: 10px; }}
    button {{ margin-top: 16px; padding: 10px 14px; }}
    .error {{ color: #a40000; }}
  </style>
</head>
<body>
  <h1>WebCore Data MCP</h1>
  {error_html}
  <form method="post" action="{DEFAULT_OAUTH_AUTHORIZE_PATH}">
    {hidden}
    <label>Username</label>
    <input name="username" autocomplete="username" required>
    <label>Password</label>
    <input name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Authorize</button>
  </form>
</body>
</html>
"""


def _allowed_redirect_uri(value: str, config: WebCoreDataMcpServerConfig) -> bool:
    parsed = urlparse(value)
    if parsed.scheme != "https" and not value.startswith(("http://127.0.0.1", "http://localhost")):
        return False
    return _allowed_by_prefix(value, config.oauth_allowed_redirect_prefixes)


def _allowed_by_prefix(value: str, prefixes: tuple[str, ...]) -> bool:
    return any(value.startswith(prefix) for prefix in prefixes)


def _valid_pkce_challenge(value: str) -> bool:
    return 43 <= len(value) <= 128 and all(ch.isalnum() or ch in "-._~" for ch in value)


def _valid_pkce_verifier(value: str) -> bool:
    return _valid_pkce_challenge(value)


def _pkce_s256_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _resource(config: WebCoreDataMcpServerConfig) -> str:
    return config.resource_url.rstrip("/")


def _client_key(handler: BaseHTTPRequestHandler) -> str:
    return str(handler.client_address[0] if handler.client_address else "unknown")


def _oauth_rate_limited(client_key: str) -> bool:
    now = time.time()
    failures = [item for item in _OAUTH_LOGIN_FAILURES.get(client_key, []) if now - item < 600]
    _OAUTH_LOGIN_FAILURES[client_key] = failures
    return len(failures) >= 5


def _record_oauth_login_failure(client_key: str) -> None:
    failures = _OAUTH_LOGIN_FAILURES.setdefault(client_key, [])
    failures.append(time.time())


def _clear_oauth_login_failures(client_key: str) -> None:
    _OAUTH_LOGIN_FAILURES.pop(client_key, None)


def _write_redirect(handler: BaseHTTPRequestHandler, location: str) -> None:
    handler.send_response(HTTPStatus.SEE_OTHER)
    handler.send_header("Location", location)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _write_html(handler: BaseHTTPRequestHandler, status: HTTPStatus, body: str) -> None:
    raw = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _write_oauth_error(handler: BaseHTTPRequestHandler, status: HTTPStatus, error: str) -> None:
    _write_json(handler, status, {"error": error})


def _write_token_error(handler: BaseHTTPRequestHandler, status: HTTPStatus, error: str, detail: str) -> None:
    payload = {"error": error}
    if detail:
        payload["error_description"] = _safe_error(detail)
    _write_json(handler, status, payload)


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


def _parse_positive_int(raw: str | None, default: int, env_name: str) -> int:
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{env_name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{env_name} must be positive")
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


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


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
                    "oauth_enabled": config.oauth_enabled,
                    "oauth_issuer": config.oauth_issuer,
                    "has_oauth_signing_secret": bool(config.oauth_signing_secret),
                    "has_oauth_owner_password_hash": bool(config.oauth_owner_password_hash),
                    "has_oauth_session_secret": bool(config.oauth_session_secret),
                    "oauth_code_store_path": str(config.oauth_code_store_path) if config.oauth_code_store_path else "",
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
