---
title: "Модуль: sheet_vitrina_v1_presentation_block"
doc_id: "WB-CORE-MODULE-19-SHEET-VITRINA-V1-PRESENTATION-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `sheet_vitrina_v1_presentation_block`."
scope: "Первый presentation/UI pass новой Google Sheets-витрины, visual formatting для `DATA_VITRINA` и `STATUS`, direct Google Sheets API delivery path, локальный live runner, подтверждённый live smoke и жёсткие границы visual-only шага."
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
  - "apps/sheet_vitrina_v1_presentation_live.py"
related_docs:
  - "migration/85_sheet_vitrina_v1_presentation.md"
  - "docs/modules/18_MODULE__SHEET_VITRINA_V1_WRITE_BRIDGE_BLOCK.md"
  - "artifacts/sheet_vitrina_v1/evidence/initial__sheet-vitrina-v1__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ для первого visual-only presentation pass новой Google Sheets-витрины."
---

# 1. Идентификатор и статус

- `module_id`: `sheet_vitrina_v1_presentation_block`
- `family`: `sheet-side`
- `status_transfer`: presentation pass перенесён в `wb-core`
- `status_verification`: live smoke для визуального шага подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `sheet_vitrina_v1_write_bridge_block`
  - `migration/85_sheet_vitrina_v1_presentation.md`
  - `apps/sheet_vitrina_v1_presentation_live.py`
  - `gas/sheet_vitrina_v1/PresentationPass.gs`
- Семантика блока: не менять data path новой витрины, а наложить поверх уже записанных листов минимальный читаемый visual scaffold через direct Google Sheets API.

# 3. Target contract и смысл результата

- Presentation pass не меняет:
  - `sheet_name`
  - `header`
  - `rows`
  - `full_overwrite` semantics write bridge
- Presentation pass меняет только:
  - frozen panes;
  - column widths;
  - header styling;
  - number/date-like formatting;
  - базовые alignments.
- Канонические visual targets:
  - `DATA_VITRINA`
  - `STATUS`

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
- presentation live runner via direct Google Sheets API: `apps/sheet_vitrina_v1_presentation_live.py`
- Apps Script bridge: `gas/sheet_vitrina_v1/WideVitrinaBridge.gs`
- Apps Script presentation reference: `gas/sheet_vitrina_v1/PresentationPass.gs`

# 6. Какой smoke подтверждён

- Подтверждён live smoke через `apps/sheet_vitrina_v1_presentation_live.py`.
- Live smoke проверяет:
  - что `DATA_VITRINA` получил freeze колонок `A:B` и базовые widths;
  - что `STATUS` получил freeze header row и базовые widths;
  - что header rows стали визуально читаемыми;
  - что для процентов, рублей и coverage/count колонок проставлены базовые formats;
  - что значения до и после presentation pass совпадают по смыслу.
  - что delivery path не требует `clasp push` и deploy в bound Apps Script.

# 7. Что уже доказано по модулю

- Новая витрина больше не только рабочая, но и базово читаемая.
- `DATA_VITRINA` визуально отделяет label/key от date-columns.
- `STATUS` визуально отделяет status/date/coverage поля.
- Это первый presentation/UI step новой витрины поверх уже живого write bridge.

# 8. Что пока не является частью финальной production-сборки

- сложный visual polish;
- conditional formatting rules;
- filter views;
- formulas и derived rows;
- operator dashboards;
- partial update presentation logic.
