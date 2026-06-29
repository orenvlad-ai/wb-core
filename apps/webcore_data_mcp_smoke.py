"""Smoke tests for the read-only WebCore Data MCP gateway."""

from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import sqlite3
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import parse as urllib_parse
from urllib import error as urllib_error
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.webcore_data_mcp_server import (  # noqa: E402
    DEFAULT_AUTHORIZATION_SERVER_METADATA_PATH,
    DEFAULT_HEALTH_PATH,
    DEFAULT_MCP_PATH,
    DEFAULT_OAUTH_AUTHORIZE_PATH,
    DEFAULT_OAUTH_TOKEN_PATH,
    DEFAULT_OPENID_CONFIGURATION_PATH,
    DEFAULT_PROTECTED_RESOURCE_PATH,
    WebCoreDataMcpServerConfig,
    build_server,
)
from packages.application.webcore_data_mcp import (  # noqa: E402
    APPROVED_TOOL_NAMES,
    SCOPE_ANALYTICS_READ,
    SCOPE_FINANCE_READ,
    SCOPE_SUPPLY_READ,
    WebCoreDataMcpGateway,
)


def main() -> int:
    with TemporaryDirectory(prefix="webcore-data-mcp-smoke-") as tmp:
        root = Path(tmp)
        db_path = root / "registry_upload_runtime.sqlite3"
        audit_log_path = root / "audit" / "webcore_data_mcp_audit.jsonl"
        _create_fixture_db(db_path)
        gateway = WebCoreDataMcpGateway(db_path=db_path, audit_log_path=audit_log_path)
        _assert_read_only(gateway)
        _assert_tool_list(gateway)
        _assert_direct_tools(gateway)
        _assert_http_server(db_path, audit_log_path)
        _assert_audit(audit_log_path)
    print("webcore_data_mcp_smoke: OK")
    return 0


def _assert_read_only(gateway: WebCoreDataMcpGateway) -> None:
    probe = gateway.verify_read_only_connection()
    if probe.get("status") != "ok" or not probe.get("write_probe_blocked"):
        raise AssertionError(f"read-only DB probe failed: {probe}")


def _assert_tool_list(gateway: WebCoreDataMcpGateway) -> None:
    tools = gateway.list_tools()
    names = tuple(tool["name"] for tool in tools)
    if names != APPROVED_TOOL_NAMES:
        raise AssertionError(f"unexpected tools: {names}")
    for tool in tools:
        if tool.get("annotations", {}).get("readOnlyHint") is not True:
            raise AssertionError(f"tool is not marked read-only: {tool['name']}")
        if any(marker in tool["name"] for marker in ("sql", "shell", "ssh", "sync", "backfill", "upload")):
            raise AssertionError(f"forbidden tool name exposed: {tool['name']}")
        properties = (tool.get("inputSchema") or {}).get("properties") or {}
        if tool["name"] == "get_supplier_shipments_registry" and "sort_by" not in properties:
            raise AssertionError("supplier registry schema must expose sort_by")
        if tool["name"] == "get_webcore_business_table_rows" and "sort_by" in properties:
            raise AssertionError("business table row schema must not expose registry sort_by")


def _assert_direct_tools(gateway: WebCoreDataMcpGateway) -> None:
    calls = [
        ("get_data_freshness_status", {}),
        ("get_webcore_data_map", {"domain": "all"}),
        ("get_webcore_data_map", {"domain": "supplier_shipments"}),
        ("get_webcore_data_map", {"domain": "artifacts"}),
        ("get_webcore_data_map", {"domain": "business_tables"}),
        ("resolve_webcore_data_request", {"intent": "покажи реестр поставок", "limit": 5}),
        ("resolve_webcore_data_request", {"intent": "найди самую большую поставку", "limit": 5}),
        ("resolve_webcore_data_intent", {"intent": "сколько коробок по упаковочному листу", "shipment_id": "SHIP-1"}),
        ("resolve_webcore_data_request", {"intent": "открой packing list", "shipment_id": "SHIP-1"}),
        ("resolve_webcore_data_request", {"intent": "покажи parsed-поля packing list", "shipment_id": "SHIP-1"}),
        ("resolve_webcore_data_request", {"intent": "покажи документы по поставке", "shipment_id": "SHIP-1"}),
        ("resolve_webcore_data_request", {"intent": "открой инвойс", "shipment_id": "SHIP-1"}),
        ("resolve_webcore_data_request", {"intent": "покажи БТТ", "shipment_id": "SHIP-1"}),
        ("resolve_webcore_data_request", {"intent": "покажи ВБК", "shipment_id": "SHIP-1"}),
        ("resolve_webcore_data_request", {"intent": "покажи ДТ", "shipment_id": "SHIP-1"}),
        ("resolve_webcore_data_request", {"intent": "покажи КП логистов", "shipment_id": "SHIP-1"}),
        ("resolve_webcore_data_request", {"intent": "покажи CNY документы", "shipment_id": "SHIP-1"}),
        ("resolve_webcore_data_request", {"intent": "покажи WB supply", "supply_id": "WB-SUP-1"}),
        ("resolve_webcore_data_request", {"intent": "покажи метрику за дату", "metric_key_or_label": "total_orderSum", "date": "2026-06-26"}),
        ("resolve_webcore_data_request", {"intent": "найди SKU", "sku_or_nm_id": "210183142"}),
        ("resolve_webcore_data_request", {"intent": "проверь свежесть данных"}),
        ("list_webcore_business_tables", {}),
        ("get_webcore_business_table_schema", {"table": "sheet_vitrina_v1_supplier_shipments"}),
        ("get_webcore_business_table_rows", {"table": "sheet_vitrina_v1_wb_supplies", "limit": 5, "include_raw_business_payloads": True}),
        ("get_webcore_business_table_rows", {"table": "sheet_vitrina_v1_supplier_financial_documents", "filters": {"supplier_order_id": "SHIP-1", "document_type": "packing_list"}, "limit": 5, "include_raw_business_payloads": True}),
        ("get_supplier_shipments_registry", {"limit": 5, "sort_by": "product_qty_total_desc"}),
        ("get_supplier_shipment_full_details", {"shipment_id": "SHIP-1", "include_raw_business_payloads": True}),
        ("get_wb_supplies_registry", {"limit": 5}),
        ("get_wb_supply_full_details", {"supply_id": "WB-SUP-1", "include_raw_business_payloads": True}),
        ("list_supply_artifacts", {"shipment_id": "SHIP-1", "limit": 10}),
        ("list_supply_artifacts", {"shipment_id": "SHIP-1", "artifact_kind": "packing_list", "limit": 10}),
        ("get_supply_artifact", {"artifact_ref": "trade_document:TD-1", "mode": "metadata"}),
        ("get_supply_artifact", {"artifact_ref": "trade_document:TD-1", "mode": "parsed"}),
        ("get_supply_artifact", {"artifact_ref": "financial_document:SHIP-1:FD-PACK", "mode": "metadata"}),
        ("get_supply_artifact", {"artifact_ref": "financial_document:SHIP-1:FD-PACK", "mode": "parsed"}),
        ("get_supply_artifact", {"artifact_ref": "trade_document:TD-1", "mode": "text", "max_bytes": 4}),
        ("get_supply_artifact", {"artifact_ref": "trade_document:TD-1", "mode": "text", "max_bytes": 200}),
        ("get_supply_artifact", {"artifact_ref": "financial_document:SHIP-1:FD-1", "mode": "base64_chunk", "max_bytes": 64}),
        ("get_supply_artifact", {"artifact_ref": "financial_document:SHIP-1:FD-BAD", "mode": "base64_chunk"}),
        ("search_business_objects", {"query": "210183142"}),
        ("explain_metric_source", {"metric_key": "orders_revenue_rub"}),
        ("get_wb_supplies_summary", {"limit": 5}),
        ("get_wb_supply_details", {"supply_id": "WB-SUP-1"}),
        ("rank_supplier_shipments_by_unit_cost", {"limit": 5}),
        ("get_supplier_shipment_details", {"shipment_id": "SHIP-1"}),
        ("get_latest_factory_order_calculation", {}),
        ("list_metrics", {"query": "Сумма заказов", "limit": 10}),
        ("list_metrics", {"query": "total_orderSum", "limit": 10}),
        ("get_available_metric_dates", {"metric_key_or_label": "total_orderSum"}),
        ("get_snapshot_metrics", {"date": "2026-06-26", "metric_query": "Сумма заказов", "limit": 10}),
        ("get_metric_values", {"metric_key_or_label": "total_orderSum", "date": "2026-06-26", "limit": 10}),
        ("get_stock_report", {"date": "2026-06-26", "sku_or_nm_id": "210183142"}),
        ("get_sku_snapshot", {"sku_or_nm_id": "210183142", "date": "2026-06-26"}),
        ("get_revenue_by_date", {"date": "2026-06-26"}),
        ("get_revenue_by_date", {"date": "2026-06-26", "revenue_metric": "total_orderSum"}),
        ("get_revenue_range", {"date_from": "2026-06-25", "date_to": "2026-06-26", "group_by": "date", "revenue_metric": "total_orderSum"}),
    ]
    for name, args in calls:
        result = gateway.call_tool(name, args, identity="smoke")
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if "/tmp/secret" in serialized or "storage_state" in serialized:
            raise AssertionError(f"redaction failed for {name}: {serialized}")
        if name == "get_revenue_by_date" and not args.get("revenue_metric"):
            if result.get("status") != "ambiguous_revenue_metric":
                raise AssertionError(f"revenue ambiguity not explicit: {result}")
        if name in {"get_metric_values", "get_revenue_by_date"} and args.get("metric_key_or_label") == "total_orderSum":
            rows = result.get("rows") or result.get("values") or []
            if not rows or rows[0].get("value") != 1000.0:
                raise AssertionError(f"total_orderSum projection failed: {result}")
        if name == "get_revenue_by_date" and args.get("revenue_metric") == "total_orderSum":
            if result.get("status") != "ok" or not result.get("values"):
                raise AssertionError(f"total_orderSum revenue projection failed: {result}")
        if name == "get_webcore_data_map":
            domains = {item.get("domain") for item in result.get("domains") or []}
            if args.get("domain") == "all" and not {"supplier_shipments", "artifacts", "metrics"}.issubset(domains):
                raise AssertionError(f"data map missing required domains: {result}")
            if args.get("domain") in {"all", "artifacts"}:
                artifact_kinds = {item.get("artifact_kind") for item in result.get("artifact_catalog") or []}
                if "packing_list" not in artifact_kinds:
                    raise AssertionError(f"data map missing packing_list artifact kind: {result}")
        if name == "resolve_webcore_data_request" and "реестр поставок" in str(args.get("intent")):
            if not any(call.get("tool") == "get_supplier_shipments_registry" for call in result.get("recommended_calls") or []):
                raise AssertionError(f"resolver did not route registry intent: {result}")
        if name in {"resolve_webcore_data_request", "resolve_webcore_data_intent"} and "самую большую" in str(args.get("intent")):
            first_call = (result.get("recommended_calls") or [{}])[0]
            if first_call.get("tool") != "get_supplier_shipments_registry" or (first_call.get("arguments") or {}).get("sort_by") != "product_qty_total_desc":
                raise AssertionError(f"resolver did not route largest shipment intent: {result}")
        if name in {"resolve_webcore_data_request", "resolve_webcore_data_intent"} and "упаковочному листу" in str(args.get("intent")):
            if not any(call.get("tool") == "get_supplier_shipment_full_details" for call in result.get("recommended_calls") or []):
                raise AssertionError(f"resolver did not route packing-list summary intent: {result}")
        if name == "get_webcore_business_table_rows":
            rows = result.get("rows") or []
            serialized_rows = json.dumps(rows, ensure_ascii=False)
            if "secret_path" in serialized_rows and "/tmp/secret" in serialized_rows:
                raise AssertionError(f"raw business payload was not scrubbed: {result}")
            if args.get("table") == "sheet_vitrina_v1_supplier_financial_documents":
                payload = (((rows or [{}])[0] or {}).get("normalized_parse_json_scrubbed_payload") or {})
                if payload.get("total_cartons") != 221.0 or payload.get("document_type") != "packing_list":
                    raise AssertionError(f"packing-list business payload not exposed safely: {result}")
        if name == "get_supplier_shipments_registry":
            row = (result.get("rows") or [{}])[0]
            if row.get("shipment_id") != "SHIP-1" or row.get("packing_list_total_cartons") != 221.0:
                raise AssertionError(f"registry missing packing-list summary fields: {result}")
        if name == "get_supplier_shipment_full_details":
            packing = result.get("packing_list_summary") or {}
            if packing.get("total_cartons") != 221.0 or packing.get("total_boxes") != 221.0 or packing.get("line_item_count") != 2:
                raise AssertionError(f"full details missing packing-list summary: {result}")
        if name == "list_supply_artifacts" and args.get("artifact_kind") == "packing_list":
            artifacts = result.get("artifacts") or []
            if not artifacts or artifacts[0].get("artifact_ref") != "financial_document:SHIP-1:FD-PACK":
                raise AssertionError(f"packing-list artifact not listed: {result}")
        if name == "get_supply_artifact" and args.get("artifact_ref") == "financial_document:SHIP-1:FD-PACK" and args.get("mode") == "parsed":
            packing = result.get("packing_list_summary") or {}
            if packing.get("total_cartons") != 221.0 or packing.get("box_count") != 221.0:
                raise AssertionError(f"packing-list parsed artifact missing aliases: {result}")
        if name == "get_supply_artifact" and args.get("artifact_ref") == "trade_document:TD-1" and args.get("mode") == "text":
            if args.get("max_bytes") == 4 and result.get("status") != "too_large":
                raise AssertionError(f"large text artifact should require chunk mode: {result}")
            if args.get("max_bytes") != 4 and "token=" in json.dumps(result, ensure_ascii=False).lower():
                raise AssertionError(f"artifact text was not scrubbed: {result}")
        if name == "get_supply_artifact" and args.get("artifact_ref") == "financial_document:SHIP-1:FD-BAD":
            if result.get("status") != "path_outside_runtime_root":
                raise AssertionError(f"path traversal artifact must be blocked: {result}")
    unknown = gateway.call_tool("explain_metric_source", {"metric_key": "not_real_metric"}, identity="smoke")
    if unknown.get("status") != "metric_not_found":
        raise AssertionError(f"metric_not_found expected: {unknown}")
    for bad_name, bad_args in [
        ("get_webcore_business_table_schema", {"table": "sqlite_master"}),
        ("get_webcore_business_table_rows", {"table": "sheet_vitrina_v1_supplier_shipments", "filters": {"source_file_path": "/tmp/secret"}}),
        ("get_webcore_business_table_rows", {"table": "sheet_vitrina_v1_supplier_shipments", "order_by": "source_file_path"}),
    ]:
        try:
            gateway.call_tool(bad_name, bad_args, identity="smoke")
        except Exception:
            pass
        else:
            raise AssertionError(f"unsafe table access should fail: {bad_name} {bad_args}")


def _assert_http_server(db_path: Path, audit_log_path: Path) -> None:
    token = "smoke-mcp-token"
    owner_password = "owner-password"
    verifier = "A" * 64
    challenge = _pkce_challenge(verifier)
    resource_url = "https://mcp.example.test"
    owner_hash = _password_hash(owner_password)
    config = WebCoreDataMcpServerConfig(
        host="127.0.0.1",
        port=0,
        mcp_path=DEFAULT_MCP_PATH,
        health_path=DEFAULT_HEALTH_PATH,
        auth_mode="bearer_oauth",
        bearer_token=token,
        bearer_token_sha256="",
        runtime_dir=None,
        db_path=db_path,
        audit_log_path=audit_log_path,
        resource_url=resource_url,
        resource_documentation_url="",
        authorization_servers=(resource_url,),
        scopes=(SCOPE_ANALYTICS_READ, SCOPE_SUPPLY_READ, SCOPE_FINANCE_READ),
        oauth_signing_secret="x" * 48,
        oauth_owner_username="owner",
        oauth_owner_password_hash=owner_hash,
        oauth_session_secret="session-secret-for-smoke",
        oauth_code_store_path=audit_log_path.parent / "oauth_codes.json",
        oauth_allowed_redirect_prefixes=("http://127.0.0.1/callback",),
        oauth_allowed_client_id_prefixes=("https://chatgpt.com/oauth/",),
        oauth_code_ttl_seconds=300,
        oauth_access_token_ttl_seconds=3600,
        oauth_issuer=resource_url,
    )
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        health = _get_json(f"{base_url}{DEFAULT_HEALTH_PATH}")
        if health.get("status") != "ok":
            raise AssertionError(f"health failed: {health}")
        protected = _get_json(f"{base_url}{DEFAULT_PROTECTED_RESOURCE_PATH}")
        if protected.get("resource") != resource_url or protected.get("authorization_servers") != [resource_url]:
            raise AssertionError(f"protected resource metadata mismatch: {protected}")
        auth_metadata = _get_json(f"{base_url}{DEFAULT_AUTHORIZATION_SERVER_METADATA_PATH}")
        if auth_metadata.get("token_endpoint_auth_methods_supported") != ["none"]:
            raise AssertionError(f"OAuth metadata must support public PKCE client: {auth_metadata}")
        if auth_metadata.get("code_challenge_methods_supported") != ["S256"]:
            raise AssertionError(f"OAuth metadata must require S256 PKCE: {auth_metadata}")
        openid_metadata = _get_json(f"{base_url}{DEFAULT_OPENID_CONFIGURATION_PATH}")
        if openid_metadata.get("authorization_endpoint") != auth_metadata.get("authorization_endpoint"):
            raise AssertionError("OpenID metadata compatibility endpoint must match OAuth metadata")
        try:
            _get_json(f"{base_url}{DEFAULT_OAUTH_AUTHORIZE_PATH}")
        except urllib_error.HTTPError as exc:
            if exc.code != 400:
                raise AssertionError(f"invalid authorize request must fail with 400, got {exc.code}") from exc
            exc.read()
        else:
            raise AssertionError("invalid authorize request must fail")
        try:
            _post_json(f"{base_url}{DEFAULT_MCP_PATH}", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            if exc.code != 401 or "orders_revenue_rub" in body or "WB-SUP-1" in body:
                raise AssertionError(f"unauth response leaked data: {exc.code} {body}")
        else:
            raise AssertionError("unauthenticated MCP request must fail")
        headers = {"Authorization": f"Bearer {token}"}
        init = _post_json(
            f"{base_url}{DEFAULT_MCP_PATH}",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            headers=headers,
        )
        if init.get("result", {}).get("serverInfo", {}).get("name") != "webcore-data-mcp":
            raise AssertionError(f"initialize failed: {init}")
        tools = _post_json(
            f"{base_url}{DEFAULT_MCP_PATH}",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=headers,
        )
        names = tuple(tool["name"] for tool in tools["result"]["tools"])
        if names != APPROVED_TOOL_NAMES:
            raise AssertionError(f"HTTP tool list mismatch: {names}")
        for tool in tools["result"]["tools"]:
            schemes = tool.get("securitySchemes") or []
            if not schemes or not str((schemes[0].get("scopes") or [""])[0]).startswith("webcore."):
                raise AssertionError(f"tool OAuth scope must use webcore.* namespace: {tool}")
        call = _post_json(
            f"{base_url}{DEFAULT_MCP_PATH}",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_data_freshness_status", "arguments": {}},
            },
            headers=headers,
        )
        if call.get("result", {}).get("structuredContent", {}).get("status") != "ok":
            raise AssertionError(f"tool call failed: {call}")
        code = _oauth_authorize_code(
            base_url,
            owner_password=owner_password,
            verifier_challenge=challenge,
            resource_url=resource_url,
        )
        try:
            _post_form(
                f"{base_url}{DEFAULT_OAUTH_TOKEN_PATH}",
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "http://127.0.0.1/callback",
                    "client_id": "https://chatgpt.com/oauth/webcore-smoke/client.json",
                    "code_verifier": "wrong" * 16,
                    "resource": resource_url,
                },
            )
        except urllib_error.HTTPError as exc:
            if exc.code != 400:
                raise AssertionError(f"bad PKCE verifier must fail with 400, got {exc.code}") from exc
            exc.read()
        else:
            raise AssertionError("bad PKCE verifier must fail")
        code = _oauth_authorize_code(
            base_url,
            owner_password=owner_password,
            verifier_challenge=challenge,
            resource_url=resource_url,
        )
        token_payload = _post_form(
            f"{base_url}{DEFAULT_OAUTH_TOKEN_PATH}",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://127.0.0.1/callback",
                "client_id": "https://chatgpt.com/oauth/webcore-smoke/client.json",
                "code_verifier": verifier,
                "resource": resource_url,
            },
        )
        access_token = str(token_payload.get("access_token") or "")
        if not access_token or token_payload.get("token_type") != "Bearer":
            raise AssertionError(f"OAuth token endpoint failed: {token_payload}")
        try:
            _post_form(
                f"{base_url}{DEFAULT_OAUTH_TOKEN_PATH}",
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "http://127.0.0.1/callback",
                    "client_id": "https://chatgpt.com/oauth/webcore-smoke/client.json",
                    "code_verifier": verifier,
                    "resource": resource_url,
                },
            )
        except urllib_error.HTTPError as exc:
            if exc.code != 400:
                raise AssertionError(f"authorization code reuse must fail with 400, got {exc.code}") from exc
            exc.read()
        else:
            raise AssertionError("authorization code reuse must fail")
        oauth_headers = {"Authorization": f"Bearer {access_token}"}
        oauth_init = _post_json(
            f"{base_url}{DEFAULT_MCP_PATH}",
            {"jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            headers=oauth_headers,
        )
        if oauth_init.get("result", {}).get("serverInfo", {}).get("name") != "webcore-data-mcp":
            raise AssertionError(f"OAuth initialize failed: {oauth_init}")
        oauth_tools = _post_json(
            f"{base_url}{DEFAULT_MCP_PATH}",
            {"jsonrpc": "2.0", "id": 11, "method": "tools/list"},
            headers=oauth_headers,
        )
        if len(oauth_tools.get("result", {}).get("tools", [])) != len(APPROVED_TOOL_NAMES):
            raise AssertionError(f"OAuth tools/list failed: {oauth_tools}")
        oauth_call = _post_json(
            f"{base_url}{DEFAULT_MCP_PATH}",
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {"name": "get_data_freshness_status", "arguments": {}},
            },
            headers=oauth_headers,
        )
        if oauth_call.get("result", {}).get("structuredContent", {}).get("status") != "ok":
            raise AssertionError(f"OAuth tool call failed: {oauth_call}")
        oauth_metric_call = _post_json(
            f"{base_url}{DEFAULT_MCP_PATH}",
            {
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {
                    "name": "get_metric_values",
                    "arguments": {"metric_key_or_label": "total_orderSum", "date": "2026-06-26", "limit": 10},
                },
            },
            headers=oauth_headers,
        )
        metric_content = oauth_metric_call.get("result", {}).get("structuredContent", {})
        if metric_content.get("status") != "ok" or not metric_content.get("rows"):
            raise AssertionError(f"OAuth metric projection failed: {oauth_metric_call}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _assert_audit(audit_log_path: Path) -> None:
    if not audit_log_path.exists():
        raise AssertionError("audit log was not written")
    text = audit_log_path.read_text(encoding="utf-8")
    if "smoke-mcp-token" in text or "orders_revenue_rub" in text or "wc1." in text or "owner-password" in text:
        raise AssertionError(f"audit log leaked sensitive/raw args: {text}")


def _post_json(url: str, payload: dict[str, object], headers: dict[str, str] | None = None) -> dict[str, object]:
    raw = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str) -> dict[str, object]:
    with urllib_request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_form(url: str, payload: dict[str, str]) -> dict[str, object]:
    raw = urllib_parse.urlencode(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _oauth_authorize_code(
    base_url: str,
    *,
    owner_password: str,
    verifier_challenge: str,
    resource_url: str,
) -> str:
    payload = {
        "response_type": "code",
        "client_id": "https://chatgpt.com/oauth/webcore-smoke/client.json",
        "redirect_uri": "http://127.0.0.1/callback",
        "code_challenge": verifier_challenge,
        "code_challenge_method": "S256",
        "state": "smoke-state",
        "resource": resource_url,
        "scope": f"{SCOPE_ANALYTICS_READ} {SCOPE_SUPPLY_READ} {SCOPE_FINANCE_READ}",
        "username": "owner",
        "password": owner_password,
    }
    raw = urllib_parse.urlencode(payload).encode("utf-8")
    req = urllib_request.Request(
        f"{base_url}{DEFAULT_OAUTH_AUTHORIZE_PATH}",
        data=raw,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    opener = urllib_request.build_opener(_NoRedirectHandler)
    try:
        opener.open(req, timeout=10)
    except urllib_error.HTTPError as exc:
        if exc.code != 303:
            raise
        location = exc.headers.get("Location", "")
        parsed = urllib_parse.urlparse(location)
        values = urllib_parse.parse_qs(parsed.query)
        code = (values.get("code") or [""])[0]
        if not code or (values.get("state") or [""])[0] != "smoke-state":
            raise AssertionError(f"OAuth authorize redirect missing code/state: {location}")
        return code
    raise AssertionError("OAuth authorize must redirect with code")


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _pkce_challenge(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())


def _password_hash(password: str) -> str:
    salt = b"webcore-data-mcp-smoke"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return "pbkdf2_sha256$260000$" + _b64(salt) + "$" + _b64(digest)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _create_fixture_db(db_path: Path) -> None:
    runtime_root = db_path.parent
    trade_file = Path("trade_documents/files/contract/TD-1/contract.txt")
    financial_file = Path("supplier_financial_documents/files/SHIP-1/FD-1/fd.pdf")
    packing_file = Path("supplier_financial_documents/files/SHIP-1/FD-PACK/packing-list.xlsx")
    cny_file = Path("cny_documents/files/CNY-1/cny.pdf")
    for relative_path, body in (
        (trade_file, b"Contract text token=must-redact"),
        (financial_file, b"%PDF-1.4\nfinancial fixture\n"),
        (packing_file, b"PK\x03\x04packing-list-fixture"),
        (cny_file, b"%PDF-1.4\ncny fixture\n"),
    ):
        target = runtime_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE registry_upload_config_v2 (
                bundle_version TEXT NOT NULL,
                nm_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                group_name TEXT NOT NULL,
                display_order INTEGER NOT NULL
            );
            CREATE TABLE registry_upload_metrics_v2 (
                bundle_version TEXT NOT NULL,
                metric_key TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                scope TEXT NOT NULL,
                label_ru TEXT NOT NULL,
                calc_type TEXT NOT NULL,
                calc_ref TEXT NOT NULL,
                show_in_data INTEGER NOT NULL,
                format_name TEXT NOT NULL,
                display_order INTEGER NOT NULL,
                section_name TEXT NOT NULL
            );
            CREATE TABLE registry_upload_formulas_v2 (
                bundle_version TEXT NOT NULL,
                row_order INTEGER NOT NULL,
                formula_id TEXT NOT NULL,
                expression TEXT NOT NULL,
                description TEXT NOT NULL
            );
            CREATE TABLE sheet_vitrina_v1_ready_snapshots (
                bundle_version TEXT NOT NULL,
                activated_at TEXT NOT NULL,
                as_of_date TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                plan_version TEXT NOT NULL,
                refreshed_at TEXT NOT NULL,
                plan_json TEXT NOT NULL
            );
            CREATE TABLE temporal_source_slot_snapshots (
                source_key TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                snapshot_role TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE temporal_source_snapshots (
                source_key TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE sheet_vitrina_v1_wb_supplies (
                supply_id TEXT PRIMARY KEY,
                cache_key TEXT,
                wb_supply_id TEXT,
                preorder_id TEXT,
                normalized_row_json TEXT NOT NULL,
                raw_detail_json TEXT,
                raw_goods_json TEXT,
                raw_package_json TEXT,
                warehouse_id TEXT,
                status_id INTEGER,
                quantity_for_size_filter REAL,
                source_created_at TEXT,
                supply_date TEXT,
                fact_date TEXT,
                updated_date TEXT,
                synced_at TEXT NOT NULL,
                last_list_synced_at TEXT,
                last_enriched_at TEXT,
                enrichment_status TEXT,
                enrichment_error TEXT
            );
            CREATE TABLE sheet_vitrina_v1_wb_supplies_sync_runs (
                run_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                raw_fetched INTEGER DEFAULT 0,
                upserted INTEGER DEFAULT 0,
                last_error TEXT
            );
            CREATE TABLE sheet_vitrina_v1_supplier_shipments (
                shipment_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                shipment_date TEXT NOT NULL,
                actual_shipment_date TEXT,
                actual_ff_acceptance_date TEXT,
                order_status TEXT NOT NULL,
                invoice_no TEXT,
                invoice_date TEXT,
                contract_no TEXT,
                contract_date TEXT,
                supplier_name TEXT,
                customer_name TEXT,
                currency TEXT,
                product_qty_total REAL,
                product_amount_total REAL,
                extras_amount_total REAL,
                invoice_amount_total REAL,
                declared_invoice_total REAL,
                match_status TEXT NOT NULL,
                source_file_path TEXT,
                parser_version TEXT,
                warnings_json TEXT NOT NULL,
                errors_json TEXT NOT NULL
            );
            CREATE TABLE sheet_vitrina_v1_supplier_shipment_lines (
                line_id TEXT PRIMARY KEY,
                shipment_id TEXT NOT NULL,
                line_type TEXT NOT NULL,
                sort_order INTEGER NOT NULL,
                internal_nm_id INTEGER,
                qty REAL,
                amount REAL,
                match_status TEXT
            );
            CREATE TABLE sheet_vitrina_v1_supplier_financial_documents (
                document_id TEXT PRIMARY KEY,
                supplier_order_id TEXT NOT NULL,
                document_type TEXT NOT NULL,
                original_filename TEXT,
                stored_file_path TEXT NOT NULL,
                file_content_type TEXT,
                file_sha256 TEXT,
                uploaded_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                parse_status TEXT NOT NULL,
                vendor TEXT,
                document_number TEXT,
                document_date TEXT,
                currency TEXT,
                total_amount REAL,
                total_amount_rub REAL,
                raw_parse_json TEXT,
                normalized_parse_json TEXT,
                warnings_json TEXT,
                errors_json TEXT
            );
            CREATE TABLE sheet_vitrina_v1_supplier_financial_expense_lines (
                line_id TEXT PRIMARY KEY,
                financial_document_id TEXT NOT NULL,
                supplier_order_id TEXT NOT NULL,
                sort_order INTEGER NOT NULL,
                category TEXT NOT NULL,
                stage TEXT,
                amount_rub REAL,
                raw_json TEXT
            );
            CREATE TABLE sheet_vitrina_v1_trade_documents (
                document_id TEXT PRIMARY KEY,
                document_type TEXT NOT NULL,
                number TEXT,
                document_date TEXT,
                supplier_name TEXT,
                currency TEXT,
                amount_total REAL,
                source_shipment_id TEXT,
                file_original_name TEXT,
                file_content_type TEXT,
                file_sha256 TEXT,
                file_path TEXT NOT NULL,
                parsed_metadata_json TEXT,
                warnings_json TEXT,
                errors_json TEXT,
                status TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE sheet_vitrina_v1_invoice_contract_links (
                invoice_document_id TEXT PRIMARY KEY,
                contract_document_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                linked_by TEXT,
                source TEXT NOT NULL
            );
            CREATE TABLE sheet_vitrina_v1_cny_documents (
                document_id TEXT PRIMARY KEY,
                document_type TEXT NOT NULL,
                source TEXT NOT NULL,
                source_order_id TEXT,
                context_order_id TEXT,
                linked_financial_document_id TEXT,
                original_filename TEXT,
                stored_file_path TEXT,
                file_content_type TEXT,
                file_sha256 TEXT,
                natural_key TEXT,
                uploaded_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                operation_date TEXT,
                operation_datetime TEXT,
                status TEXT NOT NULL,
                document_number TEXT,
                currency TEXT,
                rub_amount TEXT,
                cny_amount TEXT,
                bank_rate TEXT,
                parsed_payload_json TEXT,
                raw_parse_json TEXT,
                warnings_json TEXT,
                errors_json TEXT
            );
            CREATE TABLE sheet_vitrina_v1_cny_ledger_operations (
                operation_id TEXT PRIMARY KEY,
                operation_type TEXT NOT NULL,
                source_document_id TEXT,
                source_order_id TEXT,
                operation_date TEXT,
                operation_datetime TEXT,
                sequence_key TEXT NOT NULL,
                cny_delta TEXT,
                rub_value_delta TEXT,
                status TEXT NOT NULL,
                error_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE sheet_vitrina_v1_cny_ledger_replay_state (
                slot INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                reason TEXT,
                replayed_at TEXT NOT NULL,
                operation_count INTEGER,
                document_count INTEGER,
                balance_cny TEXT,
                balance_rub_value TEXT,
                average_rate TEXT,
                diagnostics_json TEXT
            );
            CREATE TABLE sheet_vitrina_v1_nomenclature_items (
                item_id TEXT PRIMARY KEY,
                is_active INTEGER NOT NULL,
                our_sku TEXT,
                nm_id INTEGER,
                nomenclature_name TEXT NOT NULL,
                product_type TEXT NOT NULL,
                purchase_price_yuan REAL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE sheet_vitrina_v1_factory_order_dataset_state (
                dataset_type TEXT PRIMARY KEY,
                uploaded_at TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                rows_json TEXT NOT NULL,
                uploaded_filename TEXT,
                uploaded_content_type TEXT
            );
            CREATE TABLE sheet_vitrina_v1_factory_order_result_state (
                slot INTEGER PRIMARY KEY,
                calculated_at TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            CREATE TABLE sheet_vitrina_v1_wb_regional_supply_result_state (
                slot INTEGER PRIMARY KEY,
                calculated_at TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            """
        )
        plan_json = json.dumps(
            {
                "as_of_date": "2026-06-26",
                "date_columns": ["2026-06-26", "2026-06-27"],
                "metadata": {"fixture": "webcore_data_mcp_smoke"},
                "sheets": [
                    {
                        "sheet_name": "DATA_VITRINA",
                        "header": ["label", "key", "2026-06-26", "2026-06-27"],
                        "rows": [
                            ["Итого: Сумма заказов всего", "TOTAL|total_orderSum", 1000.0, 1200.0],
                            ["SKU-1: Сумма заказов", "SKU:210183142|orderSum", 1000.0, 1200.0],
                            ["SKU-1: Выкуп", "SKU:210183142|fin_buyout_rub", 820.0, 900.0],
                            ["SKU-1: Остатки", "SKU:210183142|stock_qty", 42, 41],
                        ],
                    },
                    {
                        "sheet_name": "STATUS",
                        "header": ["source_key", "kind", "freshness", "snapshot_date", "note"],
                        "rows": [["stocks", "source", "ok", "2026-06-26", "fixture"]],
                    },
                ],
            },
            ensure_ascii=False,
        )
        conn.executemany(
            "INSERT INTO registry_upload_config_v2 VALUES(?, ?, ?, ?, ?, ?)",
            [("bundle-v1", 210183142, 1, "SKU-1", "Group A", 1)],
        )
        conn.executemany(
            "INSERT INTO registry_upload_metrics_v2 VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("bundle-v1", "total_orderSum", 1, "total", "Сумма заказов всего", "source", "sales_funnel_history.orders", 1, "rub", 0, "sales"),
                ("bundle-v1", "orderSum", 1, "sku", "Сумма заказов", "source", "sales_funnel_history.orders", 1, "rub", 1, "sales"),
                ("bundle-v1", "orders_revenue_rub", 1, "sku", "Orders revenue", "source", "sales_funnel_history.orders", 1, "rub", 1, "sales"),
                ("bundle-v1", "fin_buyout_rub", 1, "sku", "Buyout revenue", "source", "fin_report_daily.buyout", 1, "rub", 2, "finance"),
                ("bundle-v1", "stock_qty", 1, "sku", "Stock qty", "source", "stocks.qty", 1, "number", 3, "stock"),
            ],
        )
        conn.execute("INSERT INTO registry_upload_formulas_v2 VALUES(?, ?, ?, ?, ?)", ("bundle-v1", 1, "gross_margin", "revenue-cost", "sample"))
        conn.executemany(
            "INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?, ?, ?, ?, ?, ?, ?)",
            [
                ("bundle-v1", "2026-06-25T20:00:00Z", "2026-06-25", "snap-25", "plan-v1", "2026-06-26T17:00:00Z", plan_json),
                ("bundle-v1", "2026-06-26T20:00:00Z", "2026-06-26", "snap-26", "plan-v1", "2026-06-27T17:00:00Z", plan_json),
            ],
        )
        conn.executemany(
            "INSERT INTO temporal_source_slot_snapshots VALUES(?, ?, ?, ?, ?)",
            [
                ("fin_report_daily", "2026-06-26", "yesterday_closed", "2026-06-27T17:01:20Z", "{}"),
                ("stocks", "2026-06-26", "yesterday_closed", "2026-06-27T17:00:36Z", "{}"),
                ("sales_funnel_history", "2026-06-26", "yesterday_closed", "2026-06-27T17:01:51Z", "{}"),
            ],
        )
        conn.execute("INSERT INTO temporal_source_snapshots VALUES(?, ?, ?, ?)", ("stocks", "2026-06-26", "2026-06-27T17:00:36Z", "{}"))
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_wb_supplies VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "WB-SUP-1",
                "cache-1",
                "wb-1",
                "pre-1",
                json.dumps({"status_name": "accepted", "warehouse_name": "WH", "quantity": 300, "route": "route-a"}),
                json.dumps({"id": "detail-1", "secret_path": "/tmp/secret/detail"}),
                json.dumps([{"nm_id": 210183142, "qty": 10}]),
                json.dumps({"boxes": 2}),
                "wh-1",
                5,
                300,
                "2026-06-20T00:00:00+03:00",
                "2026-06-27T00:00:00+03:00",
                "2026-06-27T00:00:00+03:00",
                "2026-06-27T01:00:00+03:00",
                "2026-06-27T19:40:51Z",
                "2026-06-27T19:40:51Z",
                "2026-06-27T19:41:51Z",
                "success",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_wb_supplies_sync_runs VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("run-1", "latest_window", "success", "done", "2026-06-27T19:40:51Z", "2026-06-27T19:40:58Z", "2026-06-27T19:40:58Z", 1, 1, None),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_supplier_shipments VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "SHIP-1",
                "2026-06-25T10:00:00Z",
                "2026-06-25T18:00:00Z",
                "2026-06-25",
                None,
                None,
                "in_transit",
                "INV-1",
                "2026-06-24",
                "CON-1",
                "2026-06-01",
                "Supplier",
                "Customer",
                "USD",
                10,
                500,
                0,
                500,
                500,
                "matched",
                "/tmp/secret/invoice.xlsx",
                "parser-v1",
                "[]",
                "[]",
            ),
        )
        conn.executemany(
            "INSERT INTO sheet_vitrina_v1_supplier_shipment_lines VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            [("line-1", "SHIP-1", "product", 1, 210183142, 10, 500, "matched")],
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_supplier_financial_documents VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "FD-1",
                "SHIP-1",
                "logistics_invoice",
                "fd.pdf",
                str(financial_file),
                "application/pdf",
                "hash-redacted",
                "2026-06-26T15:35:38Z",
                "2026-06-26T15:35:38Z",
                "parsed",
                "Logistics vendor",
                "FD-001",
                "2026-06-25",
                "RUB",
                2000,
                2000,
                json.dumps({"token": "must-redact"}),
                json.dumps({"document_type": "logistics_invoice", "total_amount_rub": 2000}),
                "[]",
                "[]",
            ),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_supplier_financial_documents VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "FD-BAD",
                "SHIP-1",
                "bank_transfer_application",
                "bad.pdf",
                "/tmp/secret/fd.pdf",
                "application/pdf",
                "hash-redacted",
                "2026-06-26T15:35:38Z",
                "2026-06-26T15:35:38Z",
                "parsed",
                "Bank",
                "BAD-001",
                "2026-06-25",
                "CNY",
                500,
                None,
                "{}",
                json.dumps({"document_type": "bank_transfer_application"}),
                "[]",
                "[]",
            ),
        )
        packing_payload = {
            "document_type": "packing_list",
            "document_title": "PACKING LIST",
            "document_number": "PL-001",
            "total_cartons": 221.0,
            "total_quantity": 55250.0,
            "total_gross_weight_kg": 4680.45,
            "total_volume_m3": 21.538881,
            "carton_size": "51*39*49",
            "model_count": 2,
            "avg_qty_per_carton": 250.0,
            "line_item_count": 2,
            "line_items": [
                {
                    "row_number": 8,
                    "carton_range": "1-120",
                    "model": "SKU-A",
                    "carton_count": 120.0,
                    "qty_per_carton": 250.0,
                    "total_qty": 30000.0,
                    "total_gross_weight_kg": 2541.0,
                    "carton_size": "51*39*49",
                    "volume_m3": 11.696224,
                },
                {
                    "row_number": 9,
                    "carton_range": "121-221",
                    "model": "SKU-B",
                    "carton_count": 101.0,
                    "qty_per_carton": 250.0,
                    "total_qty": 25250.0,
                    "total_gross_weight_kg": 2139.45,
                    "carton_size": "51*39*49",
                    "volume_m3": 9.842657,
                },
            ],
        }
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_supplier_financial_documents VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "FD-PACK",
                "SHIP-1",
                "packing_list",
                "packing-list.xlsx",
                str(packing_file),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "hash-redacted",
                "2026-06-26T16:35:38Z",
                "2026-06-26T16:35:38Z",
                "parsed",
                "Supplier",
                "PL-001",
                "2026-06-25",
                "",
                None,
                None,
                json.dumps({"storage_state": "must-redact"}),
                json.dumps(packing_payload),
                "[]",
                "[]",
            ),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_supplier_financial_expense_lines VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            ("EL-1", "FD-1", "SHIP-1", 1, "logistics", "china_to_ff", 2000, json.dumps({"secret_path": "/tmp/secret/line"})),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_trade_documents VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "TD-1",
                "contract",
                "CON-1",
                "2026-06-01",
                "Supplier",
                "USD",
                None,
                "SHIP-1",
                "contract.txt",
                "text/plain",
                "hash-redacted",
                str(trade_file),
                json.dumps({"contract_number": "CON-1", "secret_path": "/tmp/secret/contract"}),
                "[]",
                "[]",
                "active",
                "2026-06-25T09:47:56Z",
                "2026-06-25T09:47:56Z",
            ),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_invoice_contract_links VALUES(?, ?, ?, ?, ?, ?)",
            ("TD-INVOICE-1", "TD-1", "2026-06-25T09:47:56Z", "2026-06-25T09:47:56Z", "smoke", "operator"),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_cny_documents VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "CNY-1",
                "supplier_cny_payment",
                "supplier_order",
                "SHIP-1",
                "SHIP-1",
                "",
                "cny.pdf",
                str(cny_file),
                "application/pdf",
                "hash-redacted",
                "cny-natural-key-redacted",
                "2026-06-26T15:35:38Z",
                "2026-06-26T15:35:38Z",
                "2026-06-26T15:35:38Z",
                "2026-06-25",
                "2026-06-25T10:00:00Z",
                "posted",
                "PAY-1",
                "CNY",
                "",
                "500",
                "",
                json.dumps({"document_type": "supplier_cny_payment", "payment_purpose": "invoice INV-1"}),
                "{}",
                "[]",
                "[]",
            ),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_cny_ledger_operations VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "OP-1",
                "supplier_payment_out",
                "CNY-1",
                "SHIP-1",
                "2026-06-25",
                "2026-06-25T10:00:00Z",
                "00000001",
                "-500",
                "-6000",
                "posted",
                "",
                "2026-06-26T15:35:38Z",
                "2026-06-26T15:35:38Z",
            ),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_cny_ledger_replay_state VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "ok", "smoke", "2026-06-26T15:35:38Z", 1, 1, "1000", "12000", "12", "[]"),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            ("NOM-1", 1, "SKU-1", 210183142, "SKU-1 name", "type-a", 12.5, "2026-06-09T17:35:29Z"),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_factory_order_dataset_state VALUES(?, ?, ?, ?, ?, ?)",
            ("stock_ff", "2026-06-26T09:04:43Z", 1, "[]", "stock.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_factory_order_result_state VALUES(?, ?, ?)",
            (1, "2026-06-25T07:48:34Z", json.dumps({"warnings": [], "rows": [{"nm_id": 210183142, "qty": 10}]})),
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_wb_regional_supply_result_state VALUES(?, ?, ?)",
            (1, "2026-06-26T09:05:44Z", json.dumps({"districts": ["central"], "rows": 1})),
        )
        conn.commit()


if __name__ == "__main__":
    raise SystemExit(main())
