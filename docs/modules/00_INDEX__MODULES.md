---
title: "Индекс канонической модульной документации wb-core"
doc_id: "WB-CORE-MODULE-00-INDEX"
doc_type: "index"
status: "active"
purpose: "Дать единый navigation entrypoint для канонической модульной документации `wb-core` в текущем checkpoint PR."
scope: "Папка `docs/modules/`, её статус как source of truth и список модульных документов, уже зафиксированных в этой ветке."
source_basis:
  - "docs/modules/12_MODULE__COGS_BY_GROUP_BLOCK.md"
  - "migration/61_cogs_by_group_block_contract.md"
  - "artifacts/cogs_by_group_block/evidence/initial__cogs-by-group__evidence.md"
  - "packages/contracts/cogs_by_group_block.py"
  - "packages/adapters/cogs_by_group_block.py"
  - "packages/application/cogs_by_group_block.py"
related_modules: []
related_tables: []
related_endpoints: []
related_runners: []
related_docs:
  - "12_MODULE__COGS_BY_GROUP_BLOCK.md"
source_of_truth_level: "navigation_only"
update_note: "Добавлен в ветке `checkpoint/cogs-by-group-block`, чтобы канонический модульный документ `cogs_by_group_block` был отражён в общем индексе `docs/modules/`."
---

# 1. Назначение индекса

`docs/modules/` — это канонический source of truth для модульной документации `wb-core`.

Полные модульные документы живут здесь. В других местах репозитория могут оставаться:
- migration contracts;
- parity/checklist документы;
- evidence;
- короткие указатели.

Но канонический свод по модулю должен отражаться в `docs/modules/`.

# 2. Что уже задокументировано в этой ветке

| Файл | Модуль | Семейство | Короткий статус |
| --- | --- | --- | --- |
| `12_MODULE__COGS_BY_GROUP_BLOCK.md` | `cogs_by_group_block` | `rule-based` | перенесён, проверен, checkpoint PR |

# 3. Как использовать этот индекс

- при добавлении нового модульного документа обновлять этот файл вместе с соответствующим `NN_MODULE__*.md`;
- считать этот файл navigation entrypoint для пакета `docs/modules/`;
- не дублировать полный канонический модульный текст в других местах репозитория.
