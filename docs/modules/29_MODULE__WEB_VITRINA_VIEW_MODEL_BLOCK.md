---
title: "Модуль: web_vitrina_view_model_block"
doc_id: "WB-CORE-MODULE-29-WEB-VITRINA-VIEW-MODEL-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded phase-2 слою `web_vitrina_view_model_block`."
scope: "Library-agnostic presentation-domain `view_model` поверх stable `web_vitrina_contract` v1: canonical columns/rows/groups/sections schema, cell kinds, formatter rules, filter/sort descriptors и namespaced state model без grid-adapter coupling, без page composition и без изменения public route/contract boundary."
source_basis:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "packages/contracts/web_vitrina_contract.py"
  - "packages/application/sheet_vitrina_v1_web_vitrina.py"
  - "packages/application/sheet_vitrina_v1_proxy_v4.py"
related_modules:
  - "packages/contracts/web_vitrina_contract.py"
  - "packages/contracts/web_vitrina_view_model.py"
  - "packages/application/sheet_vitrina_v1_web_vitrina.py"
  - "packages/application/web_vitrina_view_model.py"
related_tables:
  - "DATA_VITRINA"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/web-vitrina"
related_runners:
  - "apps/sheet_vitrina_v1_web_vitrina_contract_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_view_model_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_view_model_integration_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_user_config_browser_smoke.py"
  - "apps/sheet_vitrina_v1_inventory_planning_smoke.py"
  - "apps/sheet_vitrina_v1_inventory_planning_browser_smoke.py"
related_docs:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "View model сохраняет server-owned immutable Proxy V4 SKU/TOTAL truth: fixed pre-boundary blanks, ratio-of-aggregates TOTAL и по одному logical picker item на profit/margin pair."
---

# 1. Идентификатор и статус

- `module_id`: `web_vitrina_view_model_block`
- `family`: `web-vitrina`
- `status_transfer`: phase-2 presentation-domain layer перенесён в `wb-core`
- `status_verification`: targeted view-model smoke и contract->view-model integration smoke подтверждены
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `registry_upload_http_entrypoint_block`
  - `sheet_vitrina_v1_mvp_end_to_end_block`
  - stable `GET /v1/sheet-vitrina-v1/web-vitrina`
  - `web_vitrina_contract` v1 как единственный input seam
- Семантика блока: не пересобирать truth, не читать upstream sources напрямую и не shape-ить schema под конкретную grid library, а дать отдельный presentation-domain слой между stable server contract и будущим adapter/page слоем.

# 3. Target contract и смысл результата

- Вход слоя:
  - `web_vitrina_contract` v1 как typed contract object или тот же JSON-shaped payload
- Выход слоя:
  - `web_vitrina_view_model` v1
- `view_model` остаётся library-agnostic:
  - не импортирует `@gravity-ui/table`
  - не содержит React/component config
  - не несёт spreadsheet behavior
  - не меняет server truth semantics
- Канонический состав schema:
  - `columns`
    - `id`, `label`, `kind`, `value_type`, `align`, `sticky`, `width_hint`, `sortable`, `filterable`, `sort_key`, `filter_key`
  - `rows`
    - `row_id`, `row_kind`, `section_id`, `group_id`, `cells`, `search_text`, `filter_tokens`
  - `groups`
    - `group_id`, `label`, `order`, `collapsed_by_default`
  - `sections`
    - `section_id`, `label`, `order`, `collapsed_by_default`
  - `cells`
    - `column_id`, `cell_kind`, `value_type`, `value`, `display_text`, `formatter_id`
  - `filters / sorts`
    - canonical domain descriptors, not library state shapes
  - `formatters`
    - display rules only; no render adapter config
  - `state_model`
    - namespaced `ready / empty / loading / error` descriptors without grid-internal state manager

## 3.1 Cell kinds и formatting rules

- Current canonical cell kinds/hints:
  - `text`
  - `number`
  - `money`
  - `percent`
  - `badge`
  - `empty`
  - `unknown`
- Current formatter library intentionally remains small and repo-owned:
  - `text_default`
  - `number_default`
  - `money_rub`
  - `percent_default`
  - `badge_default`
  - `empty_default`
  - `unknown_default`
- Formatter rules не преобразуют truth path и не исправляют business values; они only describe display intent поверх already accepted contract values.

## 3.2 Separation boundary

- `web_vitrina_contract` v1:
  - server-owned truth/read contract
- `web_vitrina_view_model` v1:
  - library-agnostic presentation-domain schema
- current later layers above it:
  - `web_vitrina_gravity_table_adapter`
  - `web_vitrina_page_composition`
- still-later layer:
  - `export_layer`

## 3.3 Proxy V4 presentation truth

Server read model расширяет активный runtime catalog двумя public families: `Proxy прибыль 4` and `Прокси маржинальность 4`. Каждая family содержит SKU key и deterministic TOTAL key, но unified metric presentation объединяет pair в один logical picker/filter item; отдельный duplicated TOTAL option и scope badge не создаются. Это presentation pairing, а не пересчёт данных.

V4 cells до fixed product boundary `2026-08-01` остаются `null` и форматируются как `—`. После boundary SKU profit/margin приходят только из effective immutable V4 parameter version and complete operands. TOTAL profit is the sum of eligible SKU profits; TOTAL margin is summed eligible profit divided by summed eligible expected buyout revenue. View-model/formatter layers never average SKU margins, substitute V3, invent zero or forward-fill a missing parameter version. Existing Proxy 3 rows, formatting and saved presentation state remain unchanged; old saved configurations gain the new active pairs only through the existing catalog-intersection/append migration.

# 4. Артефакты и wiring по модулю

- contracts:
  - `packages/contracts/web_vitrina_view_model.py`
- application:
  - `packages/application/web_vitrina_view_model.py`
- input seam:
  - `packages/contracts/web_vitrina_contract.py`
  - `packages/application/sheet_vitrina_v1_web_vitrina.py`

# 5. Кодовые части

- typed schema:
  - `packages/contracts/web_vitrina_view_model.py`
- mapper:
  - `packages/application/web_vitrina_view_model.py`
- targeted smoke:
  - `apps/sheet_vitrina_v1_web_vitrina_view_model_smoke.py`
- integration smoke:
  - `apps/sheet_vitrina_v1_web_vitrina_view_model_integration_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён targeted smoke через `apps/sheet_vitrina_v1_web_vitrina_view_model_smoke.py`.
- Подтверждён integration smoke через `apps/sheet_vitrina_v1_web_vitrina_view_model_integration_smoke.py`.
- Smoke проверяют:
  - что `columns` materialize-ят sticky/align/width/filter/sort intent без grid-library fields;
  - что `rows/groups/sections` сохраняют contract ordering и stable ids;
  - что `money / percent / empty` temporal cells truthfully derive display rules only from contract row format;
  - что mapper работает и от JSON-shaped contract payload, и от typed Phase 1 contract object;
  - что `state_model` честно различает `ready` и `empty` без coupling к будущему page state manager.

# 7. Что уже доказано по модулю

- Stable server contract теперь отделён от будущего table/grid adapter не только словами в docs, но и materialized repo-owned слоем.
- Phase 2 не ломает route choice и не требует deploy/public verify, потому что live contour не меняется.
- Future adapter rewrite можно делать дешёво поверх `columns / rows / groups / sections / formatters / filters / sorts / state_model`, не перетаскивая contract semantics в library-specific shape.

# 8. Что пока не является частью финальной production-сборки

- `@gravity-ui/table` runtime/package integration
- advanced grid UI beyond the current bounded page
- sticky/resizing/virtualization implementation beyond the current thin page layer
- export implementation
- любой browser-side business truth assembly

# 9. Server-derived capital presentation

The view model carries optional per-date `presentation_state`, `presentation_tone` and `presentation_reason` supplied by the server. `Товарный капитал — наши данные` preserves these fields and concise expense/date/matching/invariant diagnostics as tooltip/ARIA evidence, but `unconfirmed` does not impose a permanent yellow/amber cell style. The browser does not derive confirmation from localStorage or mutable current status, and user-owned metric highlighting does not change this server semantic state.

The same section exposes canonical six-stage quantity/capital/coverage fields. Open `packed - accepted` belongs to stage `FF → WB` until final acceptance; positive final difference belongs to the separate `Расхождения приёмки WB` warehouse. Legacy paid-equivalent/underaccepted rows are audit compatibility only and are not active quantity or capital sources.

The WebCore source group is additive and adjacent to the existing 1C capital group. SKU/TOTAL values, weighted confirmed share and ratio-of-aggregates profitability are computed before the view-model boundary; the mapper only preserves and renders them.

# 10. Current inventory planning rows

For a window containing the exact current WB snapshot date, the upstream
contract adds six logical `inventory_planning_v1` metric pairs: raw WB,
incident-effective WB, signed FBS available total, dynamic active-facility FBS
available, combined effective stock and combined raw stock. The mapper does not
recalculate them; it preserves per-date value, formula provenance and quality
reason. The familiar combined keys are current presentation aliases, not a
change to persisted ready-snapshot or downstream calculator semantics.

An unavailable current planning cell carries
`quality_state=inventory_planning_unavailable`. This is a first-class N/A
value, not numeric zero. Historical exact-date contracts are left untouched;
new rows inside a current multi-date window expose older unmaterialized cells
only as explicit non-rewritten history.
