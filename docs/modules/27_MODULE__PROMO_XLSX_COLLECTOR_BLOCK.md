---
title: "Модуль: promo_xlsx_collector_block"
doc_id: "WB-CORE-MODULE-27-PROMO-XLSX-COLLECTOR-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `promo_xlsx_collector_block`."
scope: "Repo-owned bounded promo campaign collector contour: thin browser adapter boundary, canonical hydration/modal/drawer sequences, archive-first workbook reuse, metadata sidecar, workbook inspection, смоки и границы текущего checkpoint."
source_basis:
  - "artifacts/promo_xlsx_collector_block/evidence/initial__promo-xlsx-collector__evidence.md"
  - "artifacts/promo_xlsx_collector_block/fixture/card__cross_year__fixture.json"
  - "artifacts/promo_xlsx_collector_block/fixture/workbook_headers__exclude_list_template__fixture.json"
  - "artifacts/promo_xlsx_collector_block/fixture/workbook_headers__eligible_items_report__fixture.json"
  - "apps/promo_xlsx_collector_contract_smoke.py"
  - "apps/promo_xlsx_collector_integration_smoke.py"
  - "apps/promo_xlsx_collector_live.py"
related_modules:
  - "packages/contracts/promo_xlsx_collector_block.py"
  - "packages/adapters/promo_xlsx_collector_block.py"
  - "packages/application/promo_xlsx_collector_block.py"
  - "packages/application/promo_campaign_archive.py"
related_tables: []
related_endpoints: []
related_runners:
  - "apps/promo_xlsx_collector_contract_smoke.py"
  - "apps/promo_xlsx_collector_integration_smoke.py"
  - "apps/promo_xlsx_collector_live.py"
related_docs:
  - "README.md"
  - "docs/architecture/01_target_architecture.md"
  - "docs/modules/00_INDEX__MODULES.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под archive-first promo semantics: collector reuse-ит already archived unchanged campaign artifacts, скачивает только новые/изменившиеся кампании и оставляет truthful metadata/workbook archive для interval-based historical replay, а live wiring этого precursor описывается в `28_MODULE__PROMO_LIVE_SOURCE_WIRING_BLOCK.md`."
---

# 1. Идентификатор и статус

- `module_id`: `promo_xlsx_collector_block`
- `family`: `browser-capture`
- `status_transfer`: repo-owned bounded collector contour materialized
- `status_verification`: targeted contract smoke и bounded live integration smoke подтверждены
- `status_checkpoint`: рабочий local-runner checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy browser/session contour по-прежнему lives outside `wb-core` как seller portal session reuse path из `wb-web-bot`.
- В `wb-core` переносится не весь browser runtime, а только bounded collector semantics:
  - canonical hydration entry через direct open `dp-promo-calendar`
  - cookie accept seam `Принимаю`
  - optional auto-promo modal close
  - canonical drawer reset between promos
  - promo card extraction как source of truth для promo-level metadata
  - truthful split `downloaded / skipped_past / blocked_* / ambiguous`
- Workbook не считается authoritative source для promo-level fields:
  - `promo_title`
  - `promo_period_text`
  - `promo_status`
  - `promo_status_text`
  - `promo_id`
  - `period_id`

# 3. Target contract и смысл результата

- Для каждого processed promo collector materialize-ит:
  - `metadata.json`
  - `card.json`
  - `card.png`
- Для downloaded current/future promo collector дополнительно materialize-ит:
  - `workbook.xlsx`
  - `generate_screen.png`
  - `ready_signal.png`
  - `workbook_inspection.json`
- Для already archived unchanged promo collector больше не скачивает duplicate workbook повторно:
  - outcome `reused_archive`
  - local run dir сохраняет fresh `card.json` + `metadata.json` + `archive_reuse.json`
  - `saved_path` указывает на already archived workbook artifact
- Archive-first invariant:
  - stable campaign identity = `promo_id` + `period_id` + canonical title slug
  - archive root хранит `archive_record.json`, normalized `metadata.json`, `workbook.xlsx` и optional `workbook_inspection.json`
  - unchanged campaign metadata must reuse existing workbook artifact instead of generating a new download
- Canonical metadata fields:
  - `collected_at`
  - `trace_run_dir`
  - `source_tab`
  - `source_filter_code`
  - `calendar_url`
  - `promo_id`
  - `period_id`
  - `promo_title`
  - `promo_period_text`
  - `promo_start_at`
  - `promo_end_at`
  - `period_parse_confidence`
  - `temporal_classification`
  - `promo_status`
  - `promo_status_text`
  - `eligible_count`
  - `participating_count`
  - `excluded_count`
  - `export_kind`
  - `original_suggested_filename`
  - `saved_filename`
  - `saved_path`
  - `workbook_sheet_names`
  - `workbook_row_count`
  - `workbook_col_count`
  - `workbook_header_summary`
  - `workbook_has_date_fields`
  - `workbook_item_status_distinct_values`

# 4. Артефакты по модулю

- fixture/evidence:
  - `artifacts/promo_xlsx_collector_block/evidence/initial__promo-xlsx-collector__evidence.md`
  - `artifacts/promo_xlsx_collector_block/fixture/card__cross_year__fixture.json`
  - `artifacts/promo_xlsx_collector_block/fixture/workbook_headers__exclude_list_template__fixture.json`
  - `artifacts/promo_xlsx_collector_block/fixture/workbook_headers__eligible_items_report__fixture.json`
  - `artifacts/promo_xlsx_collector_block/fixture/metadata__canonical__fixture.json`
- bounded live output living outside repo tree:
  - temp run dir with `artifacts/`, `promos/`, `logs/`, `downloads/`, `run_summary.json`

# 5. Кодовые части

- contracts: `packages/contracts/promo_xlsx_collector_block.py`
- adapters: `packages/adapters/promo_xlsx_collector_block.py`
- application: `packages/application/promo_xlsx_collector_block.py`
- targeted contract smoke: `apps/promo_xlsx_collector_contract_smoke.py`
- bounded live integration smoke: `apps/promo_xlsx_collector_integration_smoke.py`
- local runner: `apps/promo_xlsx_collector_live.py`

# 6. Какой smoke подтверждён

- Targeted contract smoke подтверждён через `apps/promo_xlsx_collector_contract_smoke.py`:
  - sidecar contract serialization
  - export kind classification
  - low-confidence cross-year rule
  - canonical selectors/state helper
- Bounded live integration smoke подтверждён через `apps/promo_xlsx_collector_integration_smoke.py`:
  - one current/future download with sidecar against existing session reuse path
  - canonical `direct_open -> cookie -> hydrated DOM -> modal close` entry

# 7. Что уже доказано по модулю

- `wb-core` теперь владеет bounded repo-owned collector logic и local runner без копирования всего `wb-web-bot`.
- Browser internals остаются thin adapter boundary.
- Collector теперь archive-first, а не download-everything:
  - unchanged campaign artifacts reuse-ятся из `promo_campaign_archive`
  - workbook redownload допускается только когда metadata/content changed or archive artifact missing
- Export kinds truthfully materialize-ятся как:
  - `exclude_list_template`
  - `eligible_items_report`
  - `unknown`
- Cross-year short labels `декабрь -> январь` не invent-ят exact dates:
  - `promo_start_at = null`
  - `promo_end_at = null`
  - `period_parse_confidence = low`
- Canonical drawer reset внутри repo совпадает с доказанным live seam:
  - close selector внутри `#Portal-drawer`
  - waiting for overlay disappearance
  - only then next timeline click

# 8. Что пока не является частью финальной production-сборки

- live scheduler/timer wiring именно как отдельного promo-only contour;
- public HTTP route для promo collector;
- operator UI redesign или bulk operator wiring;
- sheet-side/browser-side heavy logic вместо server-owned live wiring через module `28`;
- перенос всего seller-site browser runtime внутрь `wb-core`.
