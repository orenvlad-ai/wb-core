---
title: "Модуль: webcore_data_mcp_block"
doc_id: "WB-CORE-MODULE-38-WEBCORE-DATA-MCP-BLOCK"
doc_type: "module"
status: "repo_implemented_private_loopback_live_gated"
purpose: "Зафиксировать отдельный read-only MCP gateway для безопасного доступа ChatGPT Project/custom app к business data `wb-core`."
scope: "Standalone HTTP MCP server over allowlisted read-only business tools: freshness, search, universal persisted ready-snapshot metrics by key/label/date/SKU, metric source explanation, cached WB supplies, supplier shipments, factory-order state, persisted stock/SKU snapshots and explicit revenue ambiguity handling. No arbitrary SQL, shell, SSH, upstream sync/backfill, runtime file downloads, secrets or raw payload dumps."
source_basis:
  - "packages/application/webcore_data_mcp.py"
  - "apps/webcore_data_mcp_server.py"
  - "apps/webcore_data_mcp_smoke.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
related_modules:
  - "registry_upload_db_backed_runtime_block"
  - "registry_upload_http_entrypoint_block"
  - "web_vitrina_page_composition_block"
  - "supplier_shipments_block"
  - "wb_supplies_block"
  - "onec_stocks_block"
related_tables:
  - "sheet_vitrina_v1_ready_snapshots"
  - "temporal_source_snapshots"
  - "temporal_source_slot_snapshots"
  - "registry_upload_config_v2"
  - "registry_upload_metrics_v2"
  - "registry_upload_formulas_v2"
  - "sheet_vitrina_v1_wb_supplies"
  - "sheet_vitrina_v1_wb_supplies_sync_runs"
  - "sheet_vitrina_v1_supplier_shipments"
  - "sheet_vitrina_v1_supplier_shipment_lines"
  - "sheet_vitrina_v1_supplier_financial_documents"
  - "sheet_vitrina_v1_supplier_financial_expense_lines"
  - "sheet_vitrina_v1_trade_documents"
  - "sheet_vitrina_v1_nomenclature_items"
  - "sheet_vitrina_v1_factory_order_dataset_state"
  - "sheet_vitrina_v1_factory_order_result_state"
  - "sheet_vitrina_v1_wb_regional_supply_result_state"
related_endpoints:
  - "POST /mcp"
  - "GET /healthz"
  - "GET /.well-known/oauth-protected-resource"
  - "GET /.well-known/oauth-authorization-server"
  - "GET /.well-known/openid-configuration"
  - "GET/POST /oauth/authorize"
  - "POST /oauth/token"
related_runners:
  - "apps/webcore_data_mcp_server.py"
  - "apps/webcore_data_mcp_smoke.py"
  - "artifacts/registry_upload_http_entrypoint/systemd/wb-core-data-mcp.service"
related_docs:
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "EU loopback service is installed and enabled on 127.0.0.1:8766. Public exact OAuth metadata/authorize/token and /mcp routes proxy to that loopback service. ChatGPT connector auth uses owner-only OAuth 2.1 auth-code + PKCE S256; bearer auth remains a server/admin diagnostic path. Metric tools project bounded values from persisted DATA_VITRINA ready snapshots, including TOTAL|total_* and SKU:<nm_id>|* rows, without exposing raw plan_json."
---

# 1. Identifier and Status

- `module_id`: `webcore_data_mcp_block`
- `family`: `production-facing-integration/read-only-data-gateway`
- `status_repo`: implemented
- `status_live`: loopback service installed/enabled on the active EU host; exact public `/mcp` route is allowed only as bearer-auth fail-closed proxy to `127.0.0.1:8766`
- `status_auth`: owner-only OAuth 2.1 auth-code + PKCE S256 for ChatGPT connector use; bearer auth retained only for protected server/admin diagnostics

This module is intentionally separate from DevControl MCP and from the main WebCore operator HTTP handler.

# 2. Runtime Boundary

The gateway reads the existing runtime SQLite DB with:

- SQLite URI `mode=ro`;
- `PRAGMA query_only=ON`;
- no calls to `RegistryUploadDbBackedRuntime._ensure_schema()`;
- no application methods that can refresh, sync, upload, backfill, mutate or call upstream WB/1C/Seller Portal services.

Default DB source:

`REGISTRY_UPLOAD_RUNTIME_DIR/registry_upload_runtime.sqlite3`

Optional override:

`WEBCORE_DATA_MCP_DB_PATH`

# 3. MCP Surface

Server runner:

`apps/webcore_data_mcp_server.py`

Systemd artifact:

`artifacts/registry_upload_http_entrypoint/systemd/wb-core-data-mcp.service`

Current active EU live state:

- installed as `wb-core-data-mcp.service`;
- enabled and running;
- listens only on `127.0.0.1:8766`;
- uses root-only `/etc/wb-core-data-mcp.env` for the bearer secret;
- may be published through nginx only as exact OAuth metadata/authorize/token and `/mcp` locations that proxy to `127.0.0.1:8766` and fail closed without OAuth/bearer auth.

Default local listener:

- `WEBCORE_DATA_MCP_HOST=127.0.0.1`
- `WEBCORE_DATA_MCP_PORT=8766`
- `WEBCORE_DATA_MCP_PATH=/mcp`
- `WEBCORE_DATA_MCP_HEALTH_PATH=/healthz`

Supported JSON-RPC methods:

- `initialize`
- `ping`
- `tools/list`
- `tools/call`
- `resources/list` returns empty
- `prompts/list` returns empty

The gateway is data-only. It does not expose UI resources/components.

# 4. Tool Allowlist

P0:

- `get_data_freshness_status`
- `search_business_objects`
- `explain_metric_source`
- `get_wb_supplies_summary`
- `get_wb_supply_details`
- `rank_supplier_shipments_by_unit_cost`
- `get_supplier_shipment_details`

P1:

- `get_latest_factory_order_calculation`
- `list_metrics`
- `get_metric_values`
- `get_snapshot_metrics`
- `get_available_metric_dates`
- `get_stock_report`
- `get_sku_snapshot`
- `get_revenue_by_date`
- `get_revenue_range`

Every tool is emitted with MCP annotations:

`{"readOnlyHint": true}`

No write-like tool names are exposed. There is no SQL, shell, SSH, file browser, invoice download, sync, backfill, upload, refresh or mutation tool.

# 5. Auth Model

Production data must not be exposed unauthenticated.

Implemented MVP auth:

- `WEBCORE_DATA_MCP_AUTH_MODE=bearer_oauth` on live when ChatGPT connector auth is enabled;
- OAuth 2.1 authorization-code flow with PKCE `S256`;
- public client token endpoint auth method `none`;
- exact `resource`/audience binding to the configured HTTPS resource URL;
- one-time short-lived authorization codes under runtime state;
- short-lived HMAC-signed access tokens;
- owner login uses env-only WebCore owner credentials or dedicated MCP owner hash;
- existing WebCore owner session cookie can auto-consent when the session secret is provided to the MCP service;
- optional bearer auth requires `WEBCORE_DATA_MCP_BEARER_TOKEN` or `WEBCORE_DATA_MCP_BEARER_TOKEN_SHA256` and is only a server/admin diagnostic path;
- unauthenticated MCP POST returns `401` with no business data;
- health check returns only `{"status":"ok","server":"webcore-data-mcp"}`;
- protected-resource metadata is available at `/.well-known/oauth-protected-resource`;
- authorization-server metadata is available at `/.well-known/oauth-authorization-server` and `/.well-known/openid-configuration`;
- authorize/token endpoints are `/oauth/authorize` and `/oauth/token`.

OAuth/env config:

- `WEBCORE_DATA_MCP_RESOURCE_URL`
- `WEBCORE_DATA_MCP_RESOURCE_DOCUMENTATION_URL`
- `WEBCORE_DATA_MCP_AUTHORIZATION_SERVERS`
- `WEBCORE_DATA_MCP_SCOPES`
- `WEBCORE_DATA_MCP_OAUTH_ISSUER`
- `WEBCORE_DATA_MCP_OAUTH_SIGNING_SECRET`
- `WEBCORE_DATA_MCP_OAUTH_OWNER_USERNAME` or `WB_CORE_WEB_AUTH_USERNAME`
- `WEBCORE_DATA_MCP_OAUTH_OWNER_PASSWORD_HASH` or `WB_CORE_WEB_AUTH_PASSWORD_HASH`
- `WEBCORE_DATA_MCP_OAUTH_SESSION_SECRET` or `WB_CORE_WEB_AUTH_SESSION_SECRET`
- `WEBCORE_DATA_MCP_OAUTH_CODE_STORE_PATH`
- `WEBCORE_DATA_MCP_OAUTH_ALLOWED_REDIRECT_PREFIXES`
- `WEBCORE_DATA_MCP_OAUTH_ALLOWED_CLIENT_ID_PREFIXES`
- `WEBCORE_DATA_MCP_OAUTH_CODE_TTL_SECONDS`
- `WEBCORE_DATA_MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS`

# 6. Redaction and Limits

The gateway redacts:

- secrets, tokens, passwords, cookies, authorization headers;
- `storage_state`;
- file paths;
- hashes;
- raw parse JSON;
- raw upstream payload blobs;
- workbook blobs.

Boundaries:

- request body max: 256 KiB;
- tool output lists are bounded;
- default limit: 50;
- max limit: 100;
- revenue range max: 62 days;
- audit log stores argument keys and hashes, not raw payloads.

# 7. Audit

Optional audit log:

`WEBCORE_DATA_MCP_AUDIT_LOG_PATH`

Events include:

- timestamp;
- tool name;
- identity hash;
- argument keys;
- arguments hash;
- status;
- row count;
- duration.

The audit log must not include secrets, raw arguments, raw DB payloads or file contents.

# 8. Data Semantics

Freshness:

Reads ready snapshot max dates, temporal source slot max dates/captured timestamps, WB supplies sync run state, supplier shipment/doc freshness, factory-order result timestamps and DB mtime.

WB supplies:

Reads cached rows only from `sheet_vitrina_v1_wb_supplies` and related sync/enrichment tables. It never calls WB sync/backfill/detail lazy fetch.

Supplier shipments:

Reads shipment metadata, line aggregates, financial document status aggregates, expense summaries and trade document status counts. It never exposes raw invoice/contract/PDF contents or paths.

Factory order:

Reads latest dataset/result state only. It does not recalculate and does not upload/download XLSX files.

Stock/SKU snapshots:

Reads persisted ready snapshots and metric/config/nomenclature identity. It never triggers refresh.

Universal metrics:

Reads only `sheet_vitrina_v1_ready_snapshots.plan_json` through bounded projections. The supported current ready-snapshot layout is `sheets[].sheet_name = DATA_VITRINA` with header columns `label`, `key` and date columns such as `YYYY-MM-DD`; metric rows use projection keys such as:

- `TOTAL|total_orderSum` for total-level metrics;
- `SKU:<nm_id>|orderSum` for SKU/nmId-level metrics;
- `GROUP:<group>|...` if group-like rows are present.

The MCP returns rows with date, metric key, Russian label, level/scope, SKU/group identity when present, scalar value, format/unit, source snapshot id, refreshed timestamp, source table and a safe projection label. It never returns raw `plan_json`, raw `STATUS` sheet payloads, arbitrary JSON blobs, file paths, secrets or unbounded row dumps.

Universal metric tools:

- `list_metrics(query?, section?, scope?, limit?)`: combines `registry_upload_metrics_v2` with latest ready-snapshot coverage hints and Russian labels.
- `get_metric_values(metric_key_or_label, date?, date_from?, date_to?, sku_or_nm_id?, group_by?, limit?)`: primary tool for any known metric by key or Russian label over a bounded date/date-range.
- `get_snapshot_metrics(date, sku_or_nm_id?, metric_query?, limit?)`: bounded list of metric values for one ready-snapshot date.
- `get_available_metric_dates(metric_key_or_label?)`: ready-snapshot date coverage, optionally filtered by metric.

Revenue:

There is no canonical MCP-level revenue metric yet. Without explicit `revenue_metric`, revenue tools return `ambiguous_revenue_metric` with candidate metric keys and no fabricated totals. When the caller passes an explicit persisted metric such as `total_orderSum`, revenue tools use the universal ready-snapshot metric extractor and can answer total order-sum questions for dates covered by `DATA_VITRINA`.

# 9. Smokes

Canonical smoke:

`python3 apps/webcore_data_mcp_smoke.py`

The smoke proves:

- read-only SQLite connection rejects writes;
- MCP tool list exposes only approved tools;
- all tools have `readOnlyHint: true`;
- unauthenticated HTTP MCP POST leaks no business data;
- all P0/P1 tools work on a fixture DB;
- redaction removes paths/storage-state markers;
- revenue ambiguity is explicit;
- universal metric projection reads the `DATA_VITRINA` layout and returns `total_orderSum`;
- OAuth-auth MCP can call `get_metric_values(total_orderSum, date)`;
- no sync/backfill/refresh/write tools are reachable.

# 10. Live Publication Gate

This module is repo-implemented and private-live on the active EU host. Public HTTPS publication is auth-gated and ChatGPT-ready after OAuth env is configured on the live service.

Current verified state:

- local MCP URL on the host: `http://127.0.0.1:8766/mcp`;
- public `https://api.selleros.pro/mcp` may be routed by nginx to the loopback MCP service;
- public no-token/no-bearer requests must return auth-required/no business data;
- no Secure MCP Tunnel client is configured yet;
- normal ChatGPT Project connector use selects OAuth and signs in through `/oauth/authorize`.

Before treating `/mcp` as live-verified ChatGPT-ready:

1. Configure env-only OAuth signing secret and owner auth material on the host.
2. Keep all secrets env-only/server-side.
3. Prove unauthenticated public probe returns 401 and no data.
4. Prove authenticated MCP `initialize`, `tools/list` and a safe tool call.
5. Keep DevControl MCP unchanged and separate.
