---
title: "Register: do-not-lose constraints"
doc_id: "WB-CORE-PROJECT-07-CONSTRAINTS"
doc_type: "register"
status: "active"
purpose: "Зафиксировать минимальный набор ограничений, которые нельзя потерять при дальнейших реализациях и docs updates."
scope: "Source-of-truth rules, migration boundaries, sheet/runtime invariants, docs governance invariants и anti-drift constraints."
source_basis:
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
source_of_truth_level: "secondary_project_pack"
related_docs:
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
update_triggers:
  - "изменение migration boundary"
  - "изменение operator/runtime invariant"
  - "изменение docs governance"
built_from_commit: "33be18836bb46f029b48fd19f28d45300171602a"
---

# Summary

Ниже не roadmap, а hard constraints.

Если следующий change нарушает один из них, это уже не "маленькая эволюция", а scope change и его нужно явно review-ить.

# Current norm

| Constraint ID | Constraint |
| --- | --- |
| `C-01` | Git-tracked repo docs и code остаются единственным canonical source of truth. Runtime-only fixes без Git недействительны. |
| `C-02` | Таблица остаётся thin operator shell; production truth и heavy logic не должны возвращаться в Apps Script. |
| `C-03` | `CONFIG!H:I` является service/status block и не должен теряться при `prepare`, `upload`, `load`. |
| `C-04` | Upload flow обязан использовать канонический bundle/result contract и existing HTTP entrypoint, а не локальные sheet-side копии validation logic. |
| `C-05` | Reverse-load в `DATA_VITRINA` должен идти из живого server-side contour, а не из fake local sheet fixture. |
| `C-06` | `wb_core_docs_master` не может становиться dump-копией repo docs или полным legacy mirror. |
| `C-07` | Legacy knowledge разрешён только как thin register/map/constraint layer. |
| `C-08` | При изменении contract/status/checkpoint/smoke/glossary/runbook нужно обновлять и primary docs, и затронутый project-pack, и manifest. |
| `C-09` | `project_upload_required = true` нельзя сбрасывать до фактической внешней загрузки project-pack. |
| `C-10` | Bounded steps не должны тихо превращаться в deploy/platform redesign, full parity campaign или новый parallel contour. |

# Known gaps

- Некоторые constraints ещё опираются на MVP-safe subset, а не на full parity.
- Hosted runtime/deploy hardening пока operational, а не repo-owned contract layer.

# Not in scope

- Полный список всех implementation details.
- Подробная checklist-матрица по каждому модулю.
