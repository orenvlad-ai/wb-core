"""Reliability, metadata, payload and OAuth contract checks for WebCore Data MCP."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
import time
from tempfile import TemporaryDirectory
from urllib import error as urllib_error
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.webcore_data_mcp_server import (  # noqa: E402
    DEFAULT_HEALTH_PATH,
    DEFAULT_MCP_PATH,
    SCOPE_ANALYTICS_READ,
    SCOPE_FINANCE_READ,
    SCOPE_OPS_READ,
    SCOPE_SUPPLY_READ,
    WebCoreDataMcpServerConfig,
    _make_access_token,
    _exchange_authorization_code,
    _pkce_s256_challenge,
    _store_authorization_code,
    build_server,
)
from apps.webcore_data_mcp_smoke import _create_fixture_db, _password_hash  # noqa: E402
from packages.application.webcore_data_mcp import (  # noqa: E402
    MODEL_VISIBLE_TOOL_NAMES,
    WebCoreDataMcpGateway,
)

MEASUREMENTS: dict[str, object] = {}


class FixtureGateway(WebCoreDataMcpGateway):
    def _call_tool(self, name: str, args: dict[str, object]) -> dict[str, object]:
        query = str(args.get("query") or "")
        if name in {"sku_search", "search_business_objects"} and query == "slow-fixture":
            time.sleep(0.45)
        elif name in {"sku_search", "search_business_objects"} and query.startswith("capacity-"):
            time.sleep(0.45)
        elif name in {"sku_search", "search_business_objects"} and query.startswith("parallel-"):
            time.sleep(0.08)
        if name in {"sku_search", "search_business_objects"} and query == "large-fixture":
            return {
                "status": "ok",
                "query": query,
                "results": [{"id": f"row-{index}", "text": "z" * 500} for index in range(100)],
            }
        return super()._call_tool(name, args)  # type: ignore[return-value]


def main() -> int:
    with TemporaryDirectory(prefix="webcore-data-mcp-reliability-") as tmp:
        root = Path(tmp)
        db_path = root / "registry_upload_runtime.sqlite3"
        audit_path = root / "webcore_data_mcp_audit.jsonl"
        _create_fixture_db(db_path)
        gateway = FixtureGateway(db_path=db_path, audit_log_path=audit_path)
        _assert_sqlite_deadline(gateway)
        config = _config(db_path, audit_path)
        server = build_server(config, gateway=gateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            token = _make_access_token(
                config,
                subject="reliability-smoke",
                client_id="reliability-smoke",
                scopes=tuple(config.scopes),
            )
            headers = {"Authorization": f"Bearer {token}"}
            _assert_metadata_and_golden_prompts(base_url, headers)
            _assert_parallel_requests(base_url, headers)
            _assert_timeout_does_not_block(base_url, headers)
            _assert_bounded_capacity(base_url, headers)
            _assert_payload_limit(base_url, headers)
            _assert_exact_output_contracts(base_url, headers)
            _assert_oauth_failures(base_url, config)
            _assert_concurrent_code_single_use(config)
            _assert_observability(base_url, headers, audit_path)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print(json.dumps({"status": "ok", **MEASUREMENTS}, sort_keys=True))
    print("webcore_data_mcp_reliability_smoke: OK")
    return 0


def _assert_sqlite_deadline(gateway: FixtureGateway) -> None:
    gateway._call_context.deadline_at = time.monotonic() + 0.03
    gateway._call_context.cancel_event = threading.Event()
    started = time.monotonic()
    try:
        with gateway._connect() as conn:
            conn.execute(
                "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n"
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "interrupted" not in str(exc).lower():
            raise
    else:
        raise AssertionError("SQLite progress deadline did not interrupt a long read")
    finally:
        gateway._call_context.deadline_at = None
        gateway._call_context.cancel_event = None
    elapsed = time.monotonic() - started
    MEASUREMENTS["sqlite_deadline_ms"] = round(elapsed * 1000, 1)
    if elapsed > 0.25:
        raise AssertionError("SQLite deadline did not stop execution promptly")


def _config(db_path: Path, audit_path: Path) -> WebCoreDataMcpServerConfig:
    scopes = (SCOPE_ANALYTICS_READ, SCOPE_SUPPLY_READ, SCOPE_FINANCE_READ, SCOPE_OPS_READ)
    return WebCoreDataMcpServerConfig(
        host="127.0.0.1",
        port=0,
        mcp_path=DEFAULT_MCP_PATH,
        health_path=DEFAULT_HEALTH_PATH,
        auth_mode="oauth",
        bearer_token="",
        bearer_token_sha256="",
        runtime_dir=db_path.parent,
        db_path=db_path,
        audit_log_path=audit_path,
        resource_url="https://mcp.example.test",
        resource_documentation_url="",
        authorization_servers=("https://mcp.example.test",),
        scopes=scopes,
        oauth_signing_secret="s" * 48,
        oauth_owner_username="owner",
        oauth_owner_password_hash=_password_hash("owner-password"),
        oauth_session_secret="session-secret-for-reliability-smoke",
        oauth_code_store_path=db_path.parent / "oauth_codes.json",
        oauth_allowed_redirect_prefixes=("http://127.0.0.1/callback",),
        oauth_allowed_client_id_prefixes=("https://chatgpt.com/oauth/",),
        oauth_code_ttl_seconds=300,
        oauth_access_token_ttl_seconds=3600,
        oauth_issuer="https://mcp.example.test",
        max_http_workers=16,
        max_tool_workers=8,
        tool_deadline_seconds=0.2,
        max_tool_result_bytes=32768,
    )


def _assert_metadata_and_golden_prompts(base_url: str, headers: dict[str, str]) -> None:
    response = _rpc(base_url, headers, 1, "tools/list")
    tools = response["result"]["tools"]
    names = tuple(tool["name"] for tool in tools)
    if names != MODEL_VISIBLE_TOOL_NAMES or len(names) > 18:
        raise AssertionError(f"unexpected model-visible tools: {names}")
    if any(name.startswith("resolve_") or "business_table" in name for name in names):
        raise AssertionError(f"resolver/generic table tools must be compatibility-only: {names}")
    prompts = {
        "свежесть данных": "freshness",
        "значение метрики за дату": "metric_values",
        "значение метрики за диапазон": "metric_values",
        "найди SKU": "sku_search",
        "список поставок поставщика": "supplier_shipments",
        "карточка поставки поставщика": "supplier_shipment",
        "список поставок WB": "wb_supplies",
        "детали поставки WB": "wb_supply",
        "документы поставки": "supply_artifacts",
        "состояние runtime": "runtime_health",
    }
    descriptions = {tool["name"]: str(tool.get("description") or "").lower() for tool in tools}
    required_markers = {
        "freshness": "fresh",
        "metric_values": "metric value",
        "sku_search": "find a sku",
        "supplier_shipments": "shipment registry",
        "supplier_shipment": "one known supplier shipment",
        "wb_supplies": "wb fbw supply list",
        "wb_supply": "one known cached wb supply",
        "supply_artifacts": "documents",
        "runtime_health": "production health",
    }
    for prompt, target in prompts.items():
        if target not in names:
            raise AssertionError(f"golden prompt {prompt!r} has no direct tool {target}")
        marker = required_markers[target]
        if marker not in descriptions[target]:
            raise AssertionError(f"golden prompt metadata is not explicit for {target}: {descriptions[target]}")
    for tool in tools:
        annotations = tool.get("annotations") or {}
        if annotations.get("readOnlyHint") is not True or annotations.get("destructiveHint") is not False:
            raise AssertionError(f"unsafe annotations: {tool}")
        schema = tool.get("outputSchema") or {}
        if schema.get("additionalProperties") is not False or schema.get("required") != ["status"]:
            raise AssertionError(f"non-exact output contract: {tool['name']} {schema}")


def _assert_parallel_requests(base_url: str, headers: dict[str, str]) -> None:
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(
                _rpc,
                base_url,
                headers,
                100 + index,
                "tools/call",
                {"name": "sku_search", "arguments": {"query": f"parallel-{index}"}},
            )
            for index in range(5)
        ]
        results = [future.result(timeout=2) for future in futures]
    elapsed = time.monotonic() - started
    MEASUREMENTS["five_parallel_calls_ms"] = round(elapsed * 1000, 1)
    if elapsed >= 0.28:
        raise AssertionError(f"five read-only calls were not concurrent: {elapsed:.3f}s")
    if any(result.get("result", {}).get("structuredContent", {}).get("status") != "ok" for result in results):
        raise AssertionError(f"parallel calls failed: {results}")


def _assert_timeout_does_not_block(base_url: str, headers: dict[str, str]) -> None:
    with ThreadPoolExecutor(max_workers=3) as pool:
        slow = pool.submit(
            _rpc,
            base_url,
            headers,
            200,
            "tools/call",
            {"name": "sku_search", "arguments": {"query": "slow-fixture"}},
        )
        time.sleep(0.03)
        ping_started = time.monotonic()
        ping = _rpc(base_url, headers, 201, "ping")
        ping_elapsed = time.monotonic() - ping_started
        tools_list = _rpc(base_url, headers, 204, "tools/list")
        fast = _rpc(base_url, headers, 202, "tools/call", {"name": "freshness", "arguments": {}})
        slow_result = slow.result(timeout=2)
    if ping.get("result") != {} or ping_elapsed >= 0.1:
        raise AssertionError(f"slow call blocked ping: {ping_elapsed:.3f}s {ping}")
    if len(tools_list.get("result", {}).get("tools", [])) != len(MODEL_VISIBLE_TOOL_NAMES):
        raise AssertionError(f"slow call blocked tools/list: {tools_list}")
    if fast.get("result", {}).get("structuredContent", {}).get("status") != "ok":
        raise AssertionError(f"slow call blocked fast business tool: {fast}")
    if slow_result.get("error", {}).get("data", {}).get("code") != "tool_timeout":
        raise AssertionError(f"slow call did not return controlled timeout: {slow_result}")
    MEASUREMENTS["ping_during_slow_call_ms"] = round(ping_elapsed * 1000, 1)
    MEASUREMENTS["tool_timeout_code"] = slow_result.get("error", {}).get("data", {}).get("code")
    health = _rpc(base_url, headers, 203, "tools/call", {"name": "freshness", "arguments": {}})
    if health.get("result", {}).get("structuredContent", {}).get("status") != "ok":
        raise AssertionError(f"server did not recover after timeout: {health}")


def _assert_payload_limit(base_url: str, headers: dict[str, str]) -> None:
    response = _rpc(
        base_url,
        headers,
        300,
        "tools/call",
        {"name": "sku_search", "arguments": {"query": "large-fixture"}},
    )
    error = response.get("error", {}).get("data", {})
    if error.get("code") != "tool_result_too_large" or error.get("result_bytes", 0) <= error.get("limit_bytes", 0):
        raise AssertionError(f"payload limit was not enforced: {response}")
    MEASUREMENTS["payload_result_bytes"] = error.get("result_bytes")
    MEASUREMENTS["payload_limit_bytes"] = error.get("limit_bytes")


def _assert_bounded_capacity(base_url: str, headers: dict[str, str]) -> None:
    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = [
            pool.submit(
                _rpc,
                base_url,
                headers,
                250 + index,
                "tools/call",
                {"name": "sku_search", "arguments": {"query": f"capacity-{index}"}},
            )
            for index in range(9)
        ]
        results = [future.result(timeout=2) for future in futures]
    codes = [result.get("error", {}).get("data", {}).get("code") for result in results]
    if "tool_capacity_exhausted" not in codes or any(code not in {"tool_timeout", "tool_capacity_exhausted"} for code in codes):
        raise AssertionError(f"bounded capacity response mismatch: {codes}")
    MEASUREMENTS["capacity_timeout_count"] = codes.count("tool_timeout")
    MEASUREMENTS["capacity_rejected_count"] = codes.count("tool_capacity_exhausted")
    time.sleep(0.3)


def _assert_exact_output_contracts(base_url: str, headers: dict[str, str]) -> None:
    tools = _rpc(base_url, headers, 400, "tools/list")["result"]["tools"]
    schemas = {tool["name"]: tool["outputSchema"] for tool in tools}
    calls = {
        "freshness": {},
        "metric_catalog": {"query": "total_orderSum", "limit": 5},
        "metric_values": {"metric_key_or_label": "total_orderSum", "date": "2026-06-26", "limit": 5},
        "sku_search": {"query": "210183142"},
        "sku_snapshot": {"sku_or_nm_id": "210183142", "date": "2026-06-26"},
        "supplier_shipments": {"limit": 5},
        "supplier_shipment": {"shipment_id": "SHIP-1", "line_limit": 5, "document_limit": 5},
        "wb_supplies": {"limit": 5},
        "wb_supply": {"supply_id": "WB-SUP-1"},
        "supply_artifacts": {"shipment_id": "SHIP-1", "limit": 5},
        "supply_artifact": {"artifact_ref": "trade_document:TD-1", "mode": "metadata"},
        "factory_order": {},
        "stock_report": {"date": "2026-06-26", "sku_or_nm_id": "210183142"},
        "runtime_health": {},
        "refresh_diagnostics": {"date": "2026-06-26"},
        "deploy_state": {},
    }
    for index, (name, arguments) in enumerate(calls.items(), start=401):
        response = _rpc(base_url, headers, index, "tools/call", {"name": name, "arguments": arguments})
        result_wrapper = response.get("result") or {}
        if result_wrapper.get("content"):
            raise AssertionError(f"full result duplicated in content for {name}: {result_wrapper}")
        result = result_wrapper.get("structuredContent") or {}
        if "status" not in result:
            raise AssertionError(f"missing structured result for {name}: {response}")
        extra = set(result) - set(schemas[name].get("properties") or {})
        if extra:
            raise AssertionError(f"outputSchema mismatch for {name}; unexpected fields {sorted(extra)}")
        for field, value in result.items():
            _assert_schema_type(name, field, value, schemas[name]["properties"][field])
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 32768:
            raise AssertionError(f"fixture result exceeded configured payload limit without controlled error: {name}")


def _assert_schema_type(tool: str, field: str, value: object, schema: dict[str, object]) -> None:
    declared = schema.get("type")
    allowed = set(declared if isinstance(declared, list) else [declared])
    actual = (
        "null" if value is None else
        "boolean" if isinstance(value, bool) else
        "integer" if isinstance(value, int) else
        "number" if isinstance(value, float) else
        "string" if isinstance(value, str) else
        "array" if isinstance(value, list) else
        "object" if isinstance(value, dict) else
        "unknown"
    )
    if actual not in allowed and not (actual == "integer" and "number" in allowed):
        raise AssertionError(f"outputSchema type mismatch for {tool}.{field}: {actual} not in {sorted(allowed)}")
    if actual == "array" and isinstance(schema.get("items"), dict):
        for item in value:  # type: ignore[union-attr]
            _assert_schema_type(tool, f"{field}[]", item, schema["items"])  # type: ignore[arg-type]


def _assert_oauth_failures(base_url: str, config: WebCoreDataMcpServerConfig) -> None:
    invalid = _rpc_http_error(base_url, {"Authorization": "Bearer invalid-token"}, 500, "tools/list")
    if invalid[0] != 401 or "resource_metadata=" not in invalid[1].get("WWW-Authenticate", ""):
        raise AssertionError(f"invalid token must return discoverable 401: {invalid}")
    original_ttl = config.oauth_access_token_ttl_seconds
    config.oauth_access_token_ttl_seconds = 1
    try:
        expired_token = _make_access_token(
            config,
            subject="expired-smoke",
            client_id="expired-smoke",
            scopes=(SCOPE_ANALYTICS_READ,),
        )
    finally:
        config.oauth_access_token_ttl_seconds = original_ttl
    time.sleep(1.05)
    expired = _rpc_http_error(base_url, {"Authorization": f"Bearer {expired_token}"}, 501, "tools/list")
    if expired[0] != 401 or "resource_metadata=" not in expired[1].get("WWW-Authenticate", ""):
        raise AssertionError(f"expired token must return reauthorization challenge: {expired}")
    analytics_token = _make_access_token(
        config,
        subject="analytics-only",
        client_id="analytics-only",
        scopes=(SCOPE_ANALYTICS_READ,),
    )
    response = _rpc(
        base_url,
        {"Authorization": f"Bearer {analytics_token}"},
        502,
        "tools/call",
        {"name": "deploy_state", "arguments": {}},
    )
    if response.get("error", {}).get("data", {}).get("code") != "insufficient_scope":
        raise AssertionError(f"insufficient scope was not rejected: {response}")
    if "structuredContent" in json.dumps(response):
        raise AssertionError(f"insufficient scope leaked business data: {response}")


def _assert_observability(base_url: str, headers: dict[str, str], audit_path: Path) -> None:
    response = _rpc(base_url, headers, 600, "tools/call", {"name": "runtime_health", "arguments": {}})
    calls = response.get("result", {}).get("structuredContent", {}).get("mcp_calls", {})
    if calls.get("status") != "ok" or calls.get("timeout_count", 0) < 1:
        raise AssertionError(f"runtime health does not aggregate MCP timeout state: {calls}")
    lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    events = {line.get("event") for line in lines}
    if not {"start", "finish", "timeout", "controlled_error"}.issubset(events):
        raise AssertionError(f"audit lifecycle events missing: {events}")
    serialized = json.dumps(lines, ensure_ascii=False).lower()
    forbidden = ("slow-fixture", "large-fixture", "capacity-", "parallel-", "bearer ", "password", "/users/", "/opt/")
    if any(marker in serialized for marker in forbidden):
        raise AssertionError("MCP audit leaked arguments, credentials or paths")
    required = {"request_id", "correlation_id", "tool", "status", "duration_ms", "result_bytes", "identity_hash"}
    if any(not required.issubset(line) for line in lines):
        raise AssertionError("MCP audit event is missing safe observability fields")


def _assert_concurrent_code_single_use(config: WebCoreDataMcpServerConfig) -> None:
    code = secrets.token_urlsafe(32)
    verifier = "V" * 64
    client_id = "https://chatgpt.com/oauth/reliability-smoke/client.json"
    redirect_uri = "http://127.0.0.1/callback"
    _store_authorization_code(
        config,
        code,
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": _pkce_s256_challenge(verifier),
            "resource": config.resource_url,
            "scope": SCOPE_ANALYTICS_READ,
            "subject": "owner",
            "expires_at": int(time.time()) + 300,
            "used": False,
        },
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _exchange_authorization_code,
                config,
                code=code,
                redirect_uri=redirect_uri,
                client_id=client_id,
                code_verifier=verifier,
                resource=config.resource_url,
            )
            for _ in range(2)
        ]
        results = [future.result(timeout=2) for future in futures]
    if sum(1 for result in results if result.get("access_token")) != 1:
        raise AssertionError(f"authorization code was not atomically single-use: {results}")
    if sum(1 for result in results if result.get("error") == "invalid_grant") != 1:
        raise AssertionError(f"authorization code reuse did not fail safely: {results}")


def _rpc(
    base_url: str,
    headers: dict[str, str],
    request_id: int,
    method: str,
    params: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    raw = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        f"{base_url}{DEFAULT_MCP_PATH}",
        data=raw,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _rpc_http_error(
    base_url: str,
    headers: dict[str, str],
    request_id: int,
    method: str,
) -> tuple[int, object, str]:
    try:
        _rpc(base_url, headers, request_id, method)
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, exc.headers, body
    raise AssertionError("expected HTTP auth error")


if __name__ == "__main__":
    raise SystemExit(main())
