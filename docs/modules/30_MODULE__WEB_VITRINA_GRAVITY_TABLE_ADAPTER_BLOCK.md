---
title: "Модуль: web_vitrina_gravity_table_adapter_block"
doc_id: "WB-CORE-MODULE-30-WEB-VITRINA-GRAVITY-TABLE-ADAPTER-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded phase-3 слою `web_vitrina_gravity_table_adapter_block`."
scope: "Первый concrete `grid_adapter` для `@gravity-ui/table` поверх stable `web_vitrina_view_model`: Gravity-specific columns/data/render hints, filter/sort/sticky wiring, state surface и swap-friendly isolation без изменения server contract/view_model/public routes и без broad page/UI redesign."
source_basis:
  - "https://gravity-ui.com/libraries/table"
  - "docs/modules/29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md"
  - "packages/contracts/web_vitrina_view_model.py"
  - "packages/application/web_vitrina_view_model.py"
related_modules:
  - "packages/contracts/web_vitrina_view_model.py"
  - "packages/contracts/web_vitrina_gravity_table_adapter.py"
  - "packages/application/web_vitrina_view_model.py"
  - "packages/application/web_vitrina_gravity_table_adapter.py"
related_tables:
  - "DATA_VITRINA"
related_endpoints: []
related_runners:
  - "apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_integration_smoke.py"
related_docs:
  - "docs/modules/29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Phase 3 web-vitrina materialize-ит первый concrete `grid_adapter` для `@gravity-ui/table`: Gravity-specific config/data/render hints теперь живут в отдельном repo-owned adapter layer над stable `view_model`, а public routes/page shell по-прежнему не меняются и live deploy не требуется."
---

# 1. Идентификатор и статус

- `module_id`: `web_vitrina_gravity_table_adapter_block`
- `family`: `web-vitrina`
- `status_transfer`: phase-3 grid adapter layer перенесён в `wb-core`
- `status_verification`: targeted adapter smoke и full seam integration smoke подтверждены
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `web_vitrina_view_model_block`
  - stable `web_vitrina_view_model` v1
  - official `@gravity-ui/table` surface around `Table`, `useTable` and `ColumnDef`
- Семантика блока: не сделать новый canonical UI state и не утащить grid library обратно в contract/view_model, а materialize-ить isolated adapter layer, который переводит stable presentation-domain schema в Gravity-specific config/data/render hints.

# 3. Target contract и смысл результата

- Вход слоя:
  - `WebVitrinaViewModelV1`
- Выход слоя:
  - `WebVitrinaGravityTableAdapterV1`
- Adapter now materializes:
  - `columns`
    - `accessor_key`, `header`, `size`, `min_size`, `enable_sorting`, `enable_column_filters`, `enable_resizing`
    - Gravity-specific `meta` with `pin`, `align`, `default_cell_renderer_id`, `uses_row_cell_renderers`, `sort_key`, `filter_key`
  - `rows`
    - flattened row payload keyed by view-model `row_id`
    - per-cell `renderer_id` remains authoritative, so mixed temporal renderers (`number / money / percent / empty`) do not leak into canonical column semantics
  - `renderers`
    - Gravity-oriented render variants (`text`, `label`, `placeholder`) plus formatter linkage
  - `groupings`
    - flat section/group descriptors for later composition without forcing a nested canonical row tree
  - `filters / sorts`
    - manual bindings for later `useTable` state wiring
  - `use_table_options`
    - repo-owned default seam for `get_row_id_key`, manual sorting/filtering, column resizing and current `flat` grouping mode
  - `table_props / state_surface`
    - empty/loading/error messages and current state, still outside page-orchestration ownership

## 3.1 Isolation rules

- `web_vitrina_contract` stays server-owned and library-agnostic.
- `web_vitrina_view_model` stays library-agnostic and canonical.
- All Gravity-specific naming/shapes live only in:
  - `packages/contracts/web_vitrina_gravity_table_adapter.py`
  - `packages/application/web_vitrina_gravity_table_adapter.py`
- The adapter does not:
  - compute business metrics
  - alter server truth
  - alter `view_model`
  - become the canonical UI state owner
  - require live route or page-shell changes

## 3.2 Current build/runtime boundary

- Current repo still does not materialize a React/Node build contour for live `@gravity-ui/table` rendering.
- Therefore the phase-3 result is intentionally a serializable adapter payload and render-hint layer, not a full bundled client integration.
- This keeps later library swap cheap and avoids forced SPA/platform work in the current bounded step.

# 4. Артефакты и wiring по модулю

- contracts:
  - `packages/contracts/web_vitrina_gravity_table_adapter.py`
- application:
  - `packages/application/web_vitrina_gravity_table_adapter.py`
- upstream seam:
  - `packages/contracts/web_vitrina_view_model.py`
  - `packages/application/web_vitrina_view_model.py`

# 5. Кодовые части

- typed adapter payload:
  - `packages/contracts/web_vitrina_gravity_table_adapter.py`
- mapper:
  - `packages/application/web_vitrina_gravity_table_adapter.py`
- targeted smoke:
  - `apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_smoke.py`
- integration smoke:
  - `apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_integration_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён targeted smoke через `apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_smoke.py`.
- Подтверждён integration smoke через `apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_integration_smoke.py`.
- Smoke проверяют:
  - что adapter surface честно фиксирует `@gravity-ui/table` seam как `Table/useTable + ColumnDef`;
  - что sticky/sizing/sort/filter wiring живут в adapter meta/options, а не в `view_model`;
  - что per-cell renderer binding остаётся authoritative for mixed temporal columns;
  - что `contract -> view_model -> gravity adapter` проходит без route change и без browser-side truth assembly;
  - что current state/empty/loading/error messages materialize-ятся в adapter surface, но не становятся canonical page-state manager.

# 7. Что уже доказано по модулю

- Swap-friendly separation теперь materialized end-to-end:
  - `web_vitrina_contract`
  - `web_vitrina_view_model`
  - `web_vitrina_gravity_table_adapter`
- Current repo can now prove a concrete library adapter without forcing a live frontend platform or changing public HTML.
- Phase-4 client/page work can now stay narrow because adapter payload already isolates Gravity-specific column/row/renderer/state seams.

# 8. Что пока не является частью финальной production-сборки

- real bundled `@gravity-ui/table` package/runtime rendering on `/sheet-vitrina-v1/vitrina`
- grid virtualization/resizing UX implementation
- export layer
- any business-truth logic in browser
- Google Sheets cutover
