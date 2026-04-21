---
title: "Модуль: web_vitrina_page_composition_block"
doc_id: "WB-CORE-MODULE-31-WEB-VITRINA-PAGE-COMPOSITION-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded phase-4 слою `web_vitrina_page_composition_block`."
scope: "Real page composition для `GET /sheet-vitrina-v1/vitrina`: separate sibling page shell, compact freshness summary, filters area, table container, truthful loading/empty/error states и minimal inline client island поверх stable server seams `web_vitrina_contract -> web_vitrina_view_model -> web_vitrina_gravity_table_adapter` без SPA/platform redesign."
source_basis:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md"
  - "docs/modules/30_MODULE__WEB_VITRINA_GRAVITY_TABLE_ADAPTER_BLOCK.md"
  - "packages/application/sheet_vitrina_v1_web_vitrina.py"
related_modules:
  - "packages/contracts/web_vitrina_contract.py"
  - "packages/contracts/web_vitrina_view_model.py"
  - "packages/contracts/web_vitrina_gravity_table_adapter.py"
  - "packages/application/sheet_vitrina_v1_web_vitrina.py"
  - "packages/application/web_vitrina_view_model.py"
  - "packages/application/web_vitrina_gravity_table_adapter.py"
  - "packages/application/web_vitrina_page_composition.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
related_tables:
  - "DATA_VITRINA"
related_endpoints:
  - "GET /sheet-vitrina-v1/vitrina"
  - "GET /v1/sheet-vitrina-v1/web-vitrina"
related_runners:
  - "apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_http_smoke.py"
  - "apps/registry_upload_http_entrypoint_hosted_runtime.py"
related_docs:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md"
  - "docs/modules/30_MODULE__WEB_VITRINA_GRAVITY_TABLE_ADAPTER_BLOCK.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Phase 4 materialize-ит реальную live web-vitrina page composition: existing `/sheet-vitrina-v1/vitrina` теперь грузит optional `surface=page_composition` на same read route, server-side собирает summary/filter/table payload поверх stable seams, а browser остаётся thin render/filter/sort island without becoming truth owner."
---

# 1. Идентификатор и статус

- `module_id`: `web_vitrina_page_composition_block`
- `family`: `web-vitrina`
- `status_transfer`: phase-4 live page composition перенесён в `wb-core`
- `status_verification`: targeted composition smoke, browser smoke и hosted/public probe подтверждены
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - stable `web_vitrina_contract` v1
  - stable `web_vitrina_view_model` v1
  - stable `web_vitrina_gravity_table_adapter` v1
  - existing sibling routes `GET /sheet-vitrina-v1/vitrina` + `GET /v1/sheet-vitrina-v1/web-vitrina`
- Семантика блока: не делать новый frontend platform contour и не переносить business truth в браузер, а materialize-ить repo-owned page-only layer над уже готовыми seams.

# 3. Target contract и смысл результата

- `GET /sheet-vitrina-v1/vitrina` теперь является реальной usable web-vitrina page:
  - own page header/meta
  - compact freshness/status summary
  - filters area
  - table container
  - truthful `loading / empty / error` states
  - link back to `/sheet-vitrina-v1/operator`
- Existing `GET /v1/sheet-vitrina-v1/web-vitrina` keeps the default public contract unchanged:
  - default/no-surface path still returns `web_vitrina_contract` v1
  - optional `as_of_date` keeps one-day historical read on the same route
  - optional `date_from/date_to` now materializes a bounded ready-snapshot period window on the same route
  - optional `surface=page_composition` now returns a server-driven page payload for the live page shell
- `page_composition` is server-owned and assembled only from:
  - `web_vitrina_contract`
  - `web_vitrina_view_model`
  - `web_vitrina_gravity_table_adapter`
- Browser role is intentionally narrow:
  - render the received page payload
  - keep only local filter/search/sort state
  - never assemble canonical truth
  - never compute business metrics
  - never replace the server contract/view-model/adapter owner

## 3.1 Minimal bundled/client path

- Chosen path:
  - repo-owned inline client island inside `packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html`
  - same existing read route with optional `surface=page_composition`
- Why this path is minimal:
  - no new endpoint family
  - no Node/React/Vite/Webpack contour
  - no client router
  - no static asset publish campaign
  - no duplication of business-truth assembly in browser
- Gravity-specific logic stays isolated:
  - adapter semantics still live in `packages/application/web_vitrina_gravity_table_adapter.py`
  - page shell only consumes adapter payload and renders sticky/basic table behavior above it

## 3.2 Browser-state boundary

- Browser-side state now exists only as ephemeral page-local filter/search/sort state.
- Historical access state stays query-string-owned on the same route:
  - no query = current cheap daily mode
  - `as_of_date` = one-day historical mode
  - `date_from/date_to` = bounded period mode assembled server-side from materialized ready snapshots
- Namespace is explicit: `wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1`.
- Persistence mode stays `none`; no localStorage/user-profile/server preference path is introduced.
- This state is not canonical for truth and can be dropped without changing server semantics.

# 4. Артефакты и wiring по модулю

- page-composition builder:
  - `packages/application/web_vitrina_page_composition.py`
- HTTP wiring:
  - `packages/application/registry_upload_http_entrypoint.py`
  - `packages/adapters/registry_upload_http_entrypoint.py`
- live page shell:
  - `packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html`
- upstream seams:
  - `packages/application/sheet_vitrina_v1_web_vitrina.py`
  - `packages/application/web_vitrina_view_model.py`
  - `packages/application/web_vitrina_gravity_table_adapter.py`

# 5. Кодовые части

- targeted composition smoke:
  - `apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py`
- local/live browser smoke:
  - `apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py`
- HTTP integration smoke:
  - `apps/sheet_vitrina_v1_web_vitrina_http_smoke.py`
- hosted deploy/probe contract:
  - `apps/registry_upload_http_entrypoint_hosted_runtime.py`

# 6. Какой smoke подтверждён

- `apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py`
  - confirms `composition_name/version`, source chain, state namespace, filter surface and ready/error composition behavior
- `apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py`
  - confirms real page render, visible table, filter controls, empty state on no-match search, reset recovery, period selector UX (`calendar + presets + date_from/date_to + save/reset`) and truthful error state when the ready snapshot is absent
- `apps/sheet_vitrina_v1_web_vitrina_http_smoke.py`
  - confirms default `web_vitrina_contract` path stays stable, optional `date_from/date_to` works as bounded period window and optional `surface=page_composition` works on the same route
- `apps/registry_upload_http_entrypoint_hosted_runtime.py`
  - now probes both the live HTML page and the page-composition JSON surface

# 7. Что уже доказано по модулю

- `/sheet-vitrina-v1/vitrina` is no longer a placeholder shell.
- Existing stable seams are now used end-to-end in a live read-only surface.
- The chosen client path stays intentionally minimal and repo-owned.
- Historical period UX is intentionally thin:
  - calendar/preset panel lives in the same server template
  - `Сохранить` only rewrites query string and re-reads server payload
  - `Сбросить` only removes `as_of_date/date_from/date_to` and returns to cheap daily mode
- The page composition layer knows only page/layout/render state and does not become a second truth owner.

# 8. Что пока не является частью финальной production-сборки

- real bundled `@gravity-ui/table` package/runtime integration
- export layer
- grid virtualization / advanced resizing UX
- Google Sheets cutover
- broad parity campaign with every operator/report/supply surface
- any browser-side business truth assembly
