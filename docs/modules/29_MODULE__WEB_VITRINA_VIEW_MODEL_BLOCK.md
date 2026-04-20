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
related_docs:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Phase 2 web-vitrina теперь materialize-ит отдельный library-agnostic `view_model` слой поверх stable `web_vitrina_contract` v1: mapper не меняет server truth, не тянет `@gravity-ui/table` и оставляет `grid_adapter / page_composition / export_layer` как отдельные later phases."
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
- later layers:
  - `grid_adapter`
  - `page_composition`
  - `export_layer`

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

- `@gravity-ui/table` adapter
- full grid UI
- sticky/resizing/virtualization implementation
- page composition
- export implementation
- любой browser-side business truth assembly
