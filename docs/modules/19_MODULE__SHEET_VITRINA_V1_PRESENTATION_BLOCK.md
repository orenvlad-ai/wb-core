---
title: "Модуль: sheet_vitrina_v1_presentation_block"
doc_id: "WB-CORE-MODULE-19-SHEET-VITRINA-V1-PRESENTATION-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `sheet_vitrina_v1_presentation_block`."
scope: "Bounded presentation/layout pass для `DATA_VITRINA` и `STATUS`: server-driven two-slot `date_matrix` view поверх incoming current-truth rows без локального subset/fallback logic, semantic number formats и жёсткие границы thin sheet-side presentation шага."
source_basis:
  - "migration/85_sheet_vitrina_v1_presentation.md"
  - "artifacts/sheet_vitrina_v1/evidence/initial__sheet-vitrina-v1__evidence.md"
  - "apps/sheet_vitrina_v1_presentation_live.py"
  - "gas/sheet_vitrina_v1/PresentationPass.gs"
related_modules:
  - "packages/contracts/sheet_vitrina_v1.py"
  - "packages/application/sheet_vitrina_v1.py"
related_tables:
  - "DATA_VITRINA"
  - "STATUS"
related_endpoints: []
related_runners:
  - "apps/sheet_vitrina_v1_smoke.py"
  - "apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py"
  - "apps/sheet_vitrina_v1_presentation_percent_smoke.py"
  - "apps/sheet_vitrina_v1_presentation_live.py"
related_docs:
  - "migration/85_sheet_vitrina_v1_presentation.md"
  - "docs/modules/18_MODULE__SHEET_VITRINA_V1_WRITE_BRIDGE_BLOCK.md"
  - "artifacts/sheet_vitrina_v1/evidence/initial__sheet-vitrina-v1__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под server-driven two-day date-matrix presentation: `DATA_VITRINA` visual-но снова близка к legacy sheet, но row set, block set, date columns и metric rows полностью зависят от incoming current truth; локальный 7-metric reshape и локальное угадывание дат не возвращены."
---

# 1. Идентификатор и статус

- `module_id`: `sheet_vitrina_v1_presentation_block`
- `family`: `sheet-side`
- `status_transfer`: presentation pass перенесён в `wb-core`
- `status_verification`: targeted smoke для server-driven materialization/layout подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `sheet_vitrina_v1_write_bridge_block`
  - `migration/85_sheet_vitrina_v1_presentation.md`
  - `apps/sheet_vitrina_v1_presentation_live.py`
  - `gas/sheet_vitrina_v1/PresentationPass.gs`
- Семантика блока: не менять upload/runtime/server contracts, не invent-ить локальный truth path и не резать incoming rows, а наложить на server-driven readback минимальный legacy-like visual scaffold.

# 3. Target contract и смысл результата

- Presentation pass не меняет:
  - `sheet_name`;
  - server-side current truth;
  - upload/result contracts;
  - existing operator buttons/menu labels.
- Presentation pass меняет только:
  - frozen panes;
  - column widths;
  - plain readable header styling without dark fill;
  - semantic number/date formatting;
  - базовые alignments.
- Percent-format classification остаётся canonical-key driven; `ads_cr` и `avg_ads_cr` явно относятся к percent rows и не должны деградировать до integer pattern.
- Канонические visual targets:
  - `DATA_VITRINA`
  - `STATUS`

## 3.1 DATA_VITRINA date-matrix contract

- `A1 = дата`
- `B1 = key`
- `C1..` = server-owned date columns из ready snapshot; текущий bounded live load materialize-ит как минимум `yesterday_closed` и `today_current`
- incoming flat row set из ready snapshot `GET /v1/sheet-vitrina-v1/plan` reshaped only for presentation:
  - block header row = human title + canonical block key;
  - metric rows = display label + canonical metric key;
  - history по датам живёт только в `C:...`
- Apps Script bridge не должен локально выводить даты из refresh time или source quirks: authoritative порядок и список колонок приходят из `plan.date_columns`
- Для current-only sources честный blank в yesterday-column является допустимым результатом; presentation layer не имеет права backfill-ить туда `today_current`
- блоки идут как `TOTAL`, затем `GROUP:*`, затем `SKU:*`; metric rows внутри блока сохраняют incoming current-truth ordering
- row grouping / outline / hidden rows intentionally не используются

# 4. Артефакты и wiring по модулю

- migration:
  - `migration/85_sheet_vitrina_v1_presentation.md`
- live runner:
  - `apps/sheet_vitrina_v1_presentation_live.py`
- reference formatting spec:
  - `gas/sheet_vitrina_v1/PresentationPass.gs`
- evidence:
  - `artifacts/sheet_vitrina_v1/evidence/initial__sheet-vitrina-v1__evidence.md`

# 5. Кодовые части

- artifact-backed smoke: `apps/sheet_vitrina_v1_smoke.py`
- live write runner: `apps/sheet_vitrina_v1_live_write.py`
- targeted server-driven smoke: `apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py`
- targeted percent-format smoke: `apps/sheet_vitrina_v1_presentation_percent_smoke.py`
- Apps Script bridge: `gas/sheet_vitrina_v1/WideVitrinaBridge.gs`
- Apps Script presentation reference: `gas/sheet_vitrina_v1/PresentationPass.gs`

# 6. Какой smoke подтверждён

- Подтверждён targeted smoke через `apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py`.
- Подтверждён targeted smoke через `apps/sheet_vitrina_v1_presentation_percent_smoke.py`.
- Smoke проверяет:
  - что `DATA_VITRINA` materialize-ит `date_matrix` header `дата | key | <date...>`;
  - что incoming plan больше не режется до `7` metric keys и не требует hardcoded metric list;
  - что current two-day load материализует server-owned `yesterday_closed + today_current`, а same-day rerun только переписывает matching date-columns;
  - что freeze `A:B`, plain header, section rows и semantic formats для `integer / percent / decimal` сохраняются.
  - что canonical `ads_cr` и `avg_ads_cr` сохраняют percent pattern и не попадают в integer formatting.

# 7. Что уже доказано по модулю

- Новая витрина больше не только рабочая, но и базово читаемая.
- `DATA_VITRINA` остаётся thin presentation layer над server-driven current truth и больше не теряет rows с `show_in_data = true`.
- Presentation pass превращает incoming flat composite-key rows в data-driven `date_matrix` без изменения incoming load semantics и без локального re-dating source values.
- `STATUS` визуально отделяет status/date/coverage поля.
- Это bounded presentation/layout step новой витрины поверх уже живого write bridge.

# 8. Что пока не является частью финальной production-сборки

- сложный visual polish;
- conditional formatting rules;
- filter views;
- formulas и derived rows;
- operator dashboards;
- full legacy groupings / outline / collapse-expand behavior.
