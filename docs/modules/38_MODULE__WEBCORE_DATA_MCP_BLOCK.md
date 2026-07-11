---
title: "Модуль: webcore_data_mcp_block"
doc_id: "WB-CORE-MODULE-38-WEBCORE-DATA-MCP-BLOCK"
doc_type: "module"
status: "repo_implemented_private_loopback_live_gated"
purpose: "Зафиксировать отдельный read-only MCP gateway для безопасного доступа ChatGPT Project/custom app к business data и bounded ops diagnostics `wb-core`."
scope: "Standalone bounded-concurrency HTTP MCP server with 16 short model-visible read-only tools for freshness, metrics, SKU, supplier/WB supplies, artifacts, factory-order/stock and ops health; legacy tool names remain server-callable compatibility aliases but are hidden from tools/list. Exact output schemas, compact structured results, deadlines, bounded SQLite, payload caps, lifecycle audit and repo-owned deploy identity are part of the contract. No arbitrary SQL, shell, SSH, upstream sync/backfill/refresh/load, arbitrary filesystem browsing, secrets, auth/session material, env dumps or unbounded payload dumps."
source_basis:
  - "packages/application/webcore_data_mcp.py"
  - "packages/application/webcore_ops_diagnostics.py"
  - "apps/webcore_data_mcp_server.py"
  - "apps/webcore_data_mcp_smoke.py"
  - "apps/webcore_data_mcp_reliability_smoke.py"
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
  - "temporal_source_closure_state"
  - "sheet_vitrina_v1_auto_update_state"
  - "sheet_vitrina_v1_manual_operator_state"
  - "sheet_vitrina_v1_load_state"
  - "registry_upload_config_v2"
  - "registry_upload_metrics_v2"
  - "registry_upload_formulas_v2"
  - "sheet_vitrina_v1_wb_supplies"
  - "sheet_vitrina_v1_wb_supplies_sync_state"
  - "sheet_vitrina_v1_wb_supplies_sync_runs"
  - "sheet_vitrina_v1_wb_supplies_warehouses"
  - "sheet_vitrina_v1_wb_supply_transit_cost_enrichment"
  - "sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs"
  - "sheet_vitrina_v1_supplier_shipments"
  - "sheet_vitrina_v1_supplier_shipment_lines"
  - "sheet_vitrina_v1_supplier_shipment_uploads"
  - "sheet_vitrina_v1_supplier_financial_documents"
  - "sheet_vitrina_v1_supplier_financial_expense_lines"
  - "sheet_vitrina_v1_trade_documents"
  - "sheet_vitrina_v1_invoice_contract_links"
  - "sheet_vitrina_v1_nomenclature_items"
  - "sheet_vitrina_v1_cny_documents"
  - "sheet_vitrina_v1_cny_ledger_operations"
  - "sheet_vitrina_v1_cny_ledger_replay_state"
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
  - "apps/webcore_data_mcp_reliability_smoke.py"
  - "artifacts/registry_upload_http_entrypoint/systemd/wb-core-data-mcp.service"
related_docs:
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "The production contract uses bounded threaded HTTP/tool execution, a per-call deadline, SQLite progress cancellation, a 512 KiB result cap and start/finish/timeout/error audit events. tools/list publishes 16 short exact-schema tools; all previous names stay callable for existing chats but navigation/resolver/generic-table/debug tools are hidden. Direct requests no longer require a data-map/resolver preflight. Owner-only OAuth 2.1 + PKCE/scopes and all read-only/redaction boundaries are unchanged."
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

The gateway exposes data/diagnostics tools only. It does not expose UI resources/components.

# 4. Tool Profiles and Compatibility

`tools/list` publishes exactly 16 primary model-visible tools:

- analytics: `freshness`, `metric_catalog`, `metric_values`, `sku_search`, `sku_snapshot`, `stock_report`;
- supplier/WB data: `supplier_shipments`, `supplier_shipment`, `wb_supplies`, `wb_supply`;
- documents/state: `supply_artifacts`, `supply_artifact`, `factory_order`;
- ops (`webcore.ops.read`): `runtime_health`, `refresh_diagnostics`, `deploy_state`.

The profile follows current OpenAI Apps SDK guidance: one user job per tool, explicit/defaulted inputs, predictable `outputSchema`, stable follow-up identifiers and concise model-visible `structuredContent` ([Define tools](https://developers.openai.com/apps-sdk/plan/tools), [Build your MCP server](https://developers.openai.com/apps-sdk/build/mcp-server)).

Direct freshness, metric, SKU, shipment, WB supply, document and runtime-health prompts map to one primary tool without calling a map or resolver first. List tools return stable `shipment_id`, `supply_id` or opaque `artifact_ref` identifiers for their matching detail tools.

Every published descriptor contains a title, explicit/defaulted inputs, a tool-specific strict top-level `outputSchema`, OAuth scope metadata and:

`{"readOnlyHint": true, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false}`

Compatibility behavior:

- every previously accepted tool name remains allowlisted and callable server-side, including resolver aliases, generic business-table tools, legacy summaries/details, metric/date helpers, revenue helpers and detailed ops tools;
- compatibility-only names are intentionally absent from `tools/list`, so existing chats can continue a known call while new model selection sees no duplicate resolver/summary/table competitors;
- `get_webcore_data_map` remains callable, returns no full tool-list copy for `domain=all`, filters its tool/table/artifact sections for a concrete `domain`, defaults examples/limitations off and is never a required preflight;
- `resolve_webcore_data_request` and `resolve_webcore_data_intent` remain compatibility-only recommendation helpers and do not execute data calls.

No write-like tool is exposed. There is no arbitrary SQL, shell, SSH, arbitrary filesystem browser, sync, backfill, upload, refresh, replay, load, restart, delete, patch or mutation tool. Artifact reads require an opaque `artifact_ref` and stay bounded/scrubbed. Compatibility ops diagnostics retain fixed enum allowlists and fixed `systemctl show` / `journalctl --output=json` argument vectors with `shell=False`.

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
- `WEBCORE_DATA_MCP_MAX_HTTP_WORKERS` (default `16`, bounded `2..64`)
- `WEBCORE_DATA_MCP_MAX_TOOL_WORKERS` (default `8`, bounded `2..32`)
- `WEBCORE_DATA_MCP_TOOL_DEADLINE_SECONDS` (default `12`, bounded `0.05..120`)
- `WEBCORE_DATA_MCP_MAX_TOOL_RESULT_BYTES` (default `524288`, bounded `1024..2097152`)

Default scopes exposed by the server:

- `webcore.analytics.read`
- `webcore.supply.read`
- `webcore.finance.read`
- `webcore.ops.read`

# 6. Redaction and Limits

The gateway redacts:

- secrets, tokens, passwords, cookies, authorization headers;
- `storage_state`;
- file paths;
- hashes;
- auth/session/OAuth/env/private-key material;
- journal log messages are sanitized for bearer tokens, cookies, passwords, secret-like key/value pairs, private keys and private host paths before return;
- raw parse JSON unless requested through explicit scrubbed business payload tools;
- raw upstream payload blobs unless requested through explicit scrubbed business payload tools;
- workbook blobs.

Boundaries:

- request body max: 256 KiB;
- JSON-RPC batch max: 20 items;
- HTTP request threads are capped at 16 by default; excess accepted sockets receive controlled `503 server_overloaded` instead of creating unbounded threads;
- tool execution is capped at 8 workers by default with no unbounded task queue; capacity exhaustion returns `tool_capacity_exhausted`;
- every tool call has one 12-second default deadline; timeout returns `tool_timeout`, signals SQLite cancellation and leaves the server available for `ping`, `tools/list` and other calls;
- SQLite connections use `mode=ro`, `query_only`, bounded busy timeout and a progress handler tied to the call deadline/cancellation event;
- serialized `structuredContent` max is 512 KiB by default; oversize output is replaced by controlled `tool_result_too_large` metadata;
- tool output lists are bounded;
- default limit: 50;
- max limit: 100;
- service log default limit: 100;
- service log max limit: 300;
- service log time window max: 7 days;
- artifact chunks are bounded by server-side `max_bytes` caps;
- revenue range max: 62 days;
- snapshot/refresh diagnostics date range max: 62 days;
- normal model-visible results return concise `structuredContent` plus non-model `_meta` request/duration/size fields; they do not duplicate the full JSON in `content`;
- audit log stores no arguments or business payloads.

# 7. Audit

Optional audit log:

`WEBCORE_DATA_MCP_AUDIT_LOG_PATH`

Events include:

- timestamp and request/correlation ID;
- lifecycle event: `start`, `finish`, `timeout` or `controlled_error`;
- tool name;
- identity hash;
- status;
- row count;
- server-side duration;
- serialized result size and safe error code.

The start event is written before execution, so an interrupted or unfinished call remains visible. Events are also emitted as safe JSON to stdout/systemd journal. `runtime_health` aggregates recent terminal statuses, timeout/error counts and unmatched start events without exposing raw identity, arguments, credentials, paths or payloads.

# 8. Data Semantics

Freshness:

Reads ready snapshot max dates, temporal source slot max dates/captured timestamps, WB supplies sync run state, supplier shipment/doc freshness, factory-order result timestamps and DB mtime.

Ops diagnostics:

- `runtime_health` reads fixed unit states for `wb-core-registry-http.service`, sheet-vitrina refresh/closure timers/services and `wb-core-data-mcp.service`, plus runtime disk/DB summaries and bounded MCP lifecycle aggregates. It does not expose env values or arbitrary paths.
- `get_service_logs` reads only one enum unit through `journalctl --output=json`, bounds `since/until/priority/limit`, redacts entries and returns only timestamp/unit/priority/identifier/pid/message fields.
- `refresh_diagnostics` reads persisted ready snapshot, auto/manual refresh, load state, temporal source snapshot and closure-state summaries for a requested date/range. It never triggers refresh/load/upstream calls.
- `get_runtime_snapshot_status` summarizes ready/temporal/source-slot snapshot presence and counts without returning raw payload blobs.
- `deploy_state` returns safe active-EU target labels, public base URL, required commit/deploy timestamp from repo-owned `.wb-core-deploy.json`, and fixed source mtimes by label only. It does not print raw env, tokens, cookies or runtime paths.

Navigation:

`get_webcore_data_map` is an optional compatibility guide over current tool definitions, scope constants, allowlisted runtime tables, artifact kinds and canonical module docs. It is not a source of truth and filters the returned tool subset by requested domain. Resolver names remain accepted only for old-call compatibility. New ChatGPT selection uses direct tool metadata and server instructions explicitly state that no map/resolver preflight is required.

Business table access:

`list_webcore_business_tables`, `get_webcore_business_table_schema` and `get_webcore_business_table_rows` expose only allowlisted runtime business tables. The caller cannot submit SQL text. Table name, filter columns and order columns must exist in the allowlist/current schema. Generated `SELECT` statements use bounded `limit/offset` pagination, safe date filters and scrubbed payload columns. Auth/session/audit/secrets tables are not part of the catalog. Sensitive path/hash/auth columns are omitted or redacted; raw business JSON columns are returned only when `include_raw_business_payloads=true`, and even then as scrubbed/bounded payloads.

WB supplies:

Reads cached rows only from `sheet_vitrina_v1_wb_supplies` and related sync/enrichment tables. It never calls WB sync/backfill/detail lazy fetch. `get_wb_supplies_registry` is the broader cached registry/list surface. `get_wb_supply_full_details` returns one cached row plus scrubbed normalized/detail/goods/package business payloads when explicitly requested.

Supplier shipments:

Legacy `get_supplier_shipment_details` reads shipment metadata, line aggregates, financial document status aggregates, expense summaries, trade document status counts and compact `packing_list_summary`. `get_supplier_shipments_registry` exposes a read-only registry/list with shipment dates/statuses/totals, completeness flags and optional `sort_by` (`date_desc`, `shipment_date_desc`, `product_qty_total_desc`, `invoice_amount_total_desc`, `expense_amount_rub_desc`). When a parsed `packing_list` exists in `sheet_vitrina_v1_supplier_financial_documents.normalized_parse_json`, registry rows include top-level `packing_list_*` fields: document count/status, `total_cartons`, aliases `box_count`/`carton_count`/`total_boxes`, total quantity, gross weight kg, volume m3, model count and avg qty/carton. Missing parsed data is explicit via null fields plus `packing_list_reason`. `get_supplier_shipment_full_details` expands one shipment to safe header, line rows, price conformity fields when present, financial documents, expense lines, trade documents, CNY-linked rows, artifact refs, `packing_list_summary`, parsed field availability and bounded packing-list line samples. It never exposes absolute paths, hashes, secrets or unbounded raw payloads.

Artifacts:

`list_supply_artifacts` returns metadata and opaque refs for server-owned runtime artifacts from allowlisted rows in `sheet_vitrina_v1_trade_documents`, `sheet_vitrina_v1_supplier_financial_documents` and `sheet_vitrina_v1_cny_documents`. Supported kinds include `invoice`, `contract`, `packing_list`, `logistics_quote`, `logistics_invoice`, `customs_declaration`, `bank_control_statement`, `bank_transfer_application`, `bank_fee_statement`, `cny_conversion_purchase`, `supplier_cny_payment`, `document_package` and `unknown_business_document`. `get_supply_artifact` accepts only an `artifact_ref`; it never accepts a filesystem path. Modes are `metadata`, `parsed`, `text`, `text_chunk` and `base64_chunk`. For `packing_list` artifacts, `mode=parsed` returns scrubbed parsed business payload plus `packing_list_summary` with cartons/box aliases and bounded line samples. File reads require the registered path to resolve inside the WebCore runtime root, enforce size/chunk caps and redact secret-like text. PDF/text extraction is intentionally conservative; parsed metadata is preferred where available.

CNY:

CNY rows are readable through the allowlisted table tools and shipment full details/artifact refs. MCP exposes CNY account documents and ledger state as read-only runtime evidence only; it does not replay, upload, delete or recalculate the ledger.

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

- `metric_catalog(query?, section?, scope?, limit?)`: combines `registry_upload_metrics_v2` with latest ready-snapshot coverage hints and Russian labels; use only when the metric is not already known.
- `metric_values(metric_key_or_label, date?, date_from?, date_to?, sku_or_nm_id?, group_by?, limit?)`: one primary tool for any known metric by key or Russian label over a bounded date/date-range.
- previous snapshot/date/revenue helper names remain callable but are compatibility-only and do not compete in `tools/list`.

Revenue:

There is no canonical MCP-level revenue metric yet. Without explicit `revenue_metric`, revenue tools return `ambiguous_revenue_metric` with candidate metric keys and no fabricated totals. When the caller passes an explicit persisted metric such as `total_orderSum`, revenue tools use the universal ready-snapshot metric extractor and can answer total order-sum questions for dates covered by `DATA_VITRINA`.

# 9. Smokes

Canonical smoke:

`python3 apps/webcore_data_mcp_smoke.py`

Reliability/metadata/auth smoke:

`python3 apps/webcore_data_mcp_reliability_smoke.py`

The smoke proves:

- read-only SQLite connection rejects writes;
- MCP tool list exposes exactly 16 primary tools while every legacy name remains server-callable;
- every published tool has the complete read-only annotations and a distinct strict output schema matching fixture output;
- unauthenticated HTTP MCP POST leaks no business data;
- all P0/P1 tools work on a fixture DB;
- navigation routes cover largest-shipment and packing-list intents;
- supplier registry/full-details/artifact/table reads expose parsed packing-list totals/aliases without paths/secrets;
- redaction removes paths/storage-state markers;
- ops tools expose `readOnlyHint: true` and only `webcore.ops.read`;
- fake-runner ops smoke covers fixed unit health, bounded sanitized logs, refresh diagnostics, snapshot status and deploy labels;
- unknown units, path traversal-like unit strings, shell-injection strings, unsupported priorities and over-wide date ranges are rejected;
- service log output redacts authorization/token/cookie/password/secret/private-key/private-path markers and enforces max 300 entries;
- revenue ambiguity is explicit;
- universal metric projection reads the `DATA_VITRINA` layout and returns `total_orderSum`;
- OAuth-auth MCP can call `get_metric_values(total_orderSum, date)` and `get_deploy_state` when scoped for ops;
- OAuth tokens without `webcore.ops.read` receive `insufficient_scope` for ops tools;
- invalid and expired tokens receive `401` plus `WWW-Authenticate` resource metadata and no data; token responses contain no refresh token;
- five delayed read-only calls complete concurrently; one slow fixture does not block `ping`, `tools/list` or a fast business call;
- worker capacity is bounded and overload is controlled; a recursive SQLite read is interrupted by its progress deadline;
- timed-out calls return `tool_timeout`, the server remains healthy, and runtime health reports the timeout/in-flight lifecycle aggregate;
- oversized structured results return `tool_result_too_large`, while ordinary results omit duplicated JSON `content`;
- lifecycle audit contains request/correlation id, safe identity hash, start/terminal state, duration and result size without arguments, tokens, paths or payloads;
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
4. Prove authenticated MCP `initialize`, `tools/list`, `freshness`, `metric_values`, representative supplier/WB calls, `runtime_health` and `deploy_state`.
5. Prove five common live calls overlap in wall time and journal/audit records server-side latency without arguments or payloads.
6. Confirm `deploy_state.app.commit` equals the merged commit deployed through the canonical runner.
7. Keep DevControl MCP unchanged and separate.

After a live `tools/list` metadata change, the only permitted human UI step is: `ChatGPT -> Settings -> Plugins -> WebCore Data MCP -> Refresh`.
