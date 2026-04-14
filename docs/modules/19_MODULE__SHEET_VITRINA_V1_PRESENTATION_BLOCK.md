---
title: "Модуль: sheet_vitrina_v1_presentation_block"
doc_id: "WB-CORE-MODULE-19-SHEET-VITRINA-V1-PRESENTATION-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `sheet_vitrina_v1_presentation_block`."
scope: "Bounded presentation/layout pass для `DATA_VITRINA` и `STATUS`: legacy-aligned date-matrix view без row groupings, semantic number formats, right-growing date columns и жёсткие границы thin sheet-side presentation шага."
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
update_note: "Обновлён под legacy-aligned date-matrix layout: `DATA_VITRINA` теперь пишет section rows + 7 metric rows на блок, даты растут вправо, dark header removed, semantic formats закреплены через Apps Script presentation pass."
---

# 1. Идентификатор и статус

- `module_id`: `sheet_vitrina_v1_presentation_block`
- `family`: `sheet-side`
- `status_transfer`: presentation pass перенесён в `wb-core`
- `status_verification`: targeted matrix smoke для layout/presentation подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `sheet_vitrina_v1_write_bridge_block`
  - `migration/85_sheet_vitrina_v1_presentation.md`
  - `apps/sheet_vitrina_v1_presentation_live.py`
  - `gas/sheet_vitrina_v1/PresentationPass.gs`
- Семантика блока: не менять upload/runtime/server contracts, а reshaped-ить уже загруженный readback в operator-facing matrix layout и наложить поверх него минимальный читаемый visual scaffold.

# 3. Target contract и смысл результата

- Presentation pass не меняет:
  - `sheet_name`;
  - server-side current truth;
  - upload/result contracts;
  - existing operator buttons/menu labels.
- Presentation pass меняет только:
  - date-matrix shape `дата | key | <day1> | <day2> | ...`;
  - section rows / metric rows / separator rows;
  - right-growing history columns;
  - frozen panes;
  - column widths;
  - plain readable header styling without dark fill;
  - semantic number/date formatting;
  - базовые alignments.
- Канонические visual targets:
  - `DATA_VITRINA`
  - `STATUS`

## 3.1 DATA_VITRINA matrix contract

- `A1 = дата`
- `B1 = key`
- `C1..` = date columns
- logical block = section row + `7` metric rows + blank separator
- stable metric order:
  - `view_count`
  - `ctr`
  - `open_card_count`
  - `views_current`
  - `ctr_current`
  - `orders_current`
  - `position_avg`
- first load умеет мигрировать из flat one-day layout без потери текущего дня
- same-day load обновляет существующую date-column
- новый день добавляет новую колонку справа
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
- targeted matrix smoke: `apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py`
- Apps Script bridge: `gas/sheet_vitrina_v1/WideVitrinaBridge.gs`
- Apps Script presentation reference: `gas/sheet_vitrina_v1/PresentationPass.gs`

# 6. Какой smoke подтверждён

- Подтверждён targeted smoke через `apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py`.
- Smoke проверяет:
  - миграцию flat one-day layout в matrix layout;
  - same-day overwrite без роста строк вниз;
  - append-to-right для нового дня;
  - freeze `A:B`, plain header без dark fill и semantic formats для `integer / percent / decimal`.

# 7. Что уже доказано по модулю

- Новая витрина больше не только рабочая, но и базово читаемая.
- `DATA_VITRINA` визуально приближена к old-style `DATA`, но без row-groupings.
- История по дням теперь растёт вправо, а не дублирует строки вниз.
- `STATUS` визуально отделяет status/date/coverage поля.
- Это bounded presentation/layout step новой витрины поверх уже живого write bridge.

# 8. Что пока не является частью финальной production-сборки

- сложный visual polish;
- conditional formatting rules;
- filter views;
- formulas и derived rows;
- operator dashboards;
- full legacy groupings / outline / collapse-expand behavior.
