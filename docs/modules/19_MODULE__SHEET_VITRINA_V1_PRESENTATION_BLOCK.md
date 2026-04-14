---
title: "Модуль: sheet_vitrina_v1_presentation_block"
doc_id: "WB-CORE-MODULE-19-SHEET-VITRINA-V1-PRESENTATION-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `sheet_vitrina_v1_presentation_block`."
scope: "Bounded presentation/layout pass для `DATA_VITRINA` и `STATUS`: server-driven flat readback view без локального reshape/subset logic, semantic number formats и жёсткие границы thin sheet-side presentation шага."
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
  - "apps/sheet_vitrina_v1_presentation_live.py"
related_docs:
  - "migration/85_sheet_vitrina_v1_presentation.md"
  - "docs/modules/18_MODULE__SHEET_VITRINA_V1_WRITE_BRIDGE_BLOCK.md"
  - "artifacts/sheet_vitrina_v1/evidence/initial__sheet-vitrina-v1__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под server-driven flat readback: `DATA_VITRINA` снова materialize-ит полный incoming plan из current truth без локального 7-metric reshape; presentation pass только форматирует composite keys и не меняет row set."
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
- Семантика блока: не менять upload/runtime/server contracts, не invent-ить локальный truth path и не резать incoming rows, а наложить на server-driven readback минимальный читаемый visual scaffold.

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
- Канонические visual targets:
  - `DATA_VITRINA`
  - `STATUS`

## 3.1 DATA_VITRINA flat contract

- `A1 = label`
- `B1 = key`
- `C1..` = incoming date columns from server-side plan; на текущем contour это один `as_of_date`
- `DATA_VITRINA` сохраняет полный incoming row set из `GET /v1/sheet-vitrina-v1/plan`
- composite keys вида `TOTAL|...` / `SKU:...|...` не переписываются локально и не сужаются до subset
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
- Apps Script bridge: `gas/sheet_vitrina_v1/WideVitrinaBridge.gs`
- Apps Script presentation reference: `gas/sheet_vitrina_v1/PresentationPass.gs`

# 6. Какой smoke подтверждён

- Подтверждён targeted smoke через `apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py`.
- Smoke проверяет:
  - что `DATA_VITRINA` сохраняет flat server-driven header `label | key | <date>`;
  - что incoming plan больше не режется до `7` metric keys;
  - что repeated load переписывает текущий server-driven snapshot вместо локального history/subset path;
  - что freeze `A:B`, plain header и semantic formats для `integer / percent / decimal` сохраняются и для composite keys.

# 7. Что уже доказано по модулю

- Новая витрина больше не только рабочая, но и базово читаемая.
- `DATA_VITRINA` остаётся thin presentation layer над server-driven current truth и больше не теряет rows с `show_in_data = true`.
- Presentation pass форматирует flat composite-key rows без изменения incoming load semantics.
- `STATUS` визуально отделяет status/date/coverage поля.
- Это bounded presentation/layout step новой витрины поверх уже живого write bridge.

# 8. Что пока не является частью финальной production-сборки

- сложный visual polish;
- conditional formatting rules;
- filter views;
- formulas и derived rows;
- operator dashboards;
- full legacy groupings / outline / collapse-expand behavior.
