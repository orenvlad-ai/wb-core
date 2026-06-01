---
title: "Register: do-not-lose constraints"
doc_id: "WB-CORE-PROJECT-07-CONSTRAINTS"
doc_type: "register"
status: "active"
purpose: "Зафиксировать минимальный набор ограничений, которые нельзя потерять при дальнейших реализациях, docs updates и chat execution handoff."
scope: "Source-of-truth rules, migration boundaries, sheet/runtime invariants, docs governance invariants, chat execution invariants и anti-drift constraints."
source_basis:
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/modules/32_MODULE__RESEARCH_SKU_GROUP_COMPARISON_BLOCK.md"
  - "docs/modules/33_MODULE__ONEC_STOCKS_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
source_of_truth_level: "derived_secondary_project_pack"
related_docs:
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/33_MODULE__ONEC_STOCKS_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
update_triggers:
  - "изменение migration boundary"
  - "изменение operator/runtime invariant"
  - "изменение docs governance"
built_from_commit: "623dcc17ad637f04e601f67f71bcb627881cadaa"
---

# Summary

Ниже не roadmap, а hard constraints.

Если следующий change нарушает один из них, это уже не "маленькая эволюция", а scope change и его нужно явно review-ить.

# Current norm

| Constraint ID | Constraint |
| --- | --- |
| `C-01` | Git-tracked repo docs и code остаются единственным canonical source of truth. Runtime-only fixes без Git недействительны. |
| `C-02` | Таблица остаётся thin operator shell; production truth и heavy logic не должны возвращаться в Apps Script. |
| `C-03` | Legacy Google Sheets/GAS contour is `ARCHIVED / DO NOT USE`; former `CONFIG!H:I`, `DATA_VITRINA`, `STATUS`, Apps Script menu and `/load` paths are archive/migration-only, not current runtime/write/load/verify targets. |
| `C-04` | Upload flow обязан использовать канонический bundle/result contract и existing HTTP entrypoint, а не локальные sheet-side копии validation logic; server-side acceptance должна опираться на structure/schema correctness и фактические длины registry lists, а не на hardcoded row-count caps. |
| `C-05` | Current website/operator/web-vitrina reads server-side ready snapshots; reverse-load в Google Sheets `DATA_VITRINA` must stay archived and guarded. |
| `C-06` | `wb_core_docs_master` не может становиться dump-копией repo docs или полным legacy mirror. |
| `C-07` | Legacy knowledge разрешён только как thin register/map/constraint layer. |
| `C-08` | Ordinary task-flow обновляет code/tests и затронутые authoritative docs, если truth изменился; `wb_core_docs_master/**` и manifest не обновляются по умолчанию и не являются completion blocker для обычной задачи. |
| `C-09` | `wb_core_docs_master/**` и manifest обновляются только в explicit derived-sync flow или transitional pack rebuild; после такого merge `~/Projects/wb-core` должен быть приведён к current `origin/main`, `~/Projects/wb-core/wb_core_docs_master` должен быть проверен как upload-ready source по manifest, и только после этого пользователю остаётся один human-only post-merge шаг: загрузить актуальный pack во внешний ChatGPT Project. |
| `C-10` | Bounded steps не должны тихо превращаться в deploy/platform redesign, full parity campaign или новый parallel contour. |
| `C-11` | Для новых WebCore chat prompts prompt к Codex обязан явно содержать `Класс задачи`, `Причина классификации`, `Режим выполнения` и заканчиваться блоками `=== ДЛЯ КУРАТОРА ===` и `=== СЖАТАЯ ПРОВЕРКА ===`; без этого execution handoff считается неполным. |
| `C-12` | Bounded и безопасная техническая работа должна сначала идти через Codex; пользователю можно отдавать только human-only step: логин, права, branch-protection approval / blocker-driven manual merge fallback, ручная UI-проверка или решение по риску. |
| `C-13` | Если manual handoff неизбежен, действует `one step = one action`: один ответ содержит один минимальный практический следующий шаг и не смешивает несколько независимых рискованных действий. |
| `C-14` | Матрица `L1/L2/L3` задаёт минимальный execution burden: `L1` = локальный малорисковый шаг без отдельного read-only review и без `README` / architecture sync по умолчанию, только targeted smoke; `L2` = bounded block с обязательными `module doc + index`, targeted smoke и `1` integration smoke; `L3` = boundary/risk/governance task с усиленным bounded execution, docs sync по смыслу текущего checkpoint и при необходимости отдельной merge-readiness проверкой. |
| `C-15` | Full current truth и `STATUS` остаются authoritative для всего enabled+show_in_data набора; operator-facing `DATA_VITRINA` не должна invent-ить локальный truth path, не должна silently выкидывать `show_in_data` rows и должна materialize-ить incoming server-driven row set как thin data-driven `date_matrix` без sheet-side subset logic. |
| `C-15a` | Current unified `/sheet-vitrina-v1/vitrina` UI remains a consumer of server-owned ready snapshots and source/job/status truth; group refresh, cell highlights, report filters and browser persistence must not become a second source-of-truth layer. |
| `C-15b` | `ЕБД` / `единая база данных` is only a user-facing alias for shared server-side accepted truth/runtime state in `wb-core`; Google Sheets/GAS, HTML/browser UI, browser `localStorage`, report-private manual tables and operator XLSX baseline uploads must not be treated as the canonical data-truth layer. |
| `C-15c` | Plan-report baseline and ready-fact reconcile remain bounded server-side support paths: baseline can fill only full-month plan-report aggregates, ready-fact reconcile can insert only missing accepted `fin_report_daily` / `ads_compact` slots from persisted ready snapshots, and neither path may overwrite existing accepted diffs or fabricate blank values as zeros. |
| `C-16` | Для задач с live/public эффектом `repo-complete` недостаточно: execution handoff не считается complete, пока не достигнуты требуемые `live-complete` / public-web verify, либо пока точный blocker явно не назван. Sheet completion is no longer a success path; for archived GAS changes only guard push/verify is required. |
| `C-17` | Если live deploy/restart или public probe безопасны и доступны, они должны входить в тот же bounded execution по умолчанию, а не откладываться без явной причины. `clasp push` входит в обязательный путь только для archived Apps Script guard changes and verifies blocked/archived behavior, not sheet write success. |
| `C-18` | Если задача добавляет или меняет public route, обязательна внешняя public probe-проверка; `404`/`Not Found` на ожидаемом route трактуется как stale deploy или incomplete publish wiring, пока не доказано обратное. |
| `C-19` | Если requested outcome по смыслу включает Git fixation или GitHub closure и пользователь явно не запретил Git/GitHub actions, Codex сначала проверяет `gh auth status -h github.com`; при working auth и repo write/merge access обычные `git commit`, `git push`, `gh pr create/update`, `gh pr ready`, retarget через `gh pr edit --base ...`, `gh pr merge --delete-branch` являются Codex-owned routine, включая stacked/base-branch merge sequence. При working auth/access Codex обязана довести ordinary GitHub closure до merge + delete-branch; manual merge допустим только как fallback-blocker case. |
| `C-20` | Единственный допустимый локальный source для внешнего ChatGPT Project upload = `~/Projects/wb-core/wb_core_docs_master`; временные копии, zip-архивы и произвольные папки не считаются canonical source. |
| `C-21` | Перед sync `~/Projects/wb-core` к current `origin/main` несвязанный dirty state нужно сохранять только bounded safe method (`stash`, backup, отдельная branch/worktree или эквивалент), без destructive reset поверх пользовательских изменений. |
| `C-22` | Готовность pack к upload определяется по `~/Projects/wb-core/wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md`, а не по Finder timestamps, имени архива или памяти исполнителя. |
| `C-23` | После explicit derived-sync или transitional pack rebuild, когда upload-ready source подготовлен, в handoff должен оставаться ровно один human-only remainder: внешний upload актуального `wb_core_docs_master`; manifest при этом не превращается в upload state machine. |
| `C-24` | Hosted public-route publication for the current contour goes through the repo-owned nginx allowlist and deploy runner; manual broad catch-all live nginx edits are not a completion path. |
| `C-25` | `Отзывы` and feedbacks AI stay read-only/transient for table/AI semantics: they must not persist AI labels as accepted truth/ЕБД, write Google Sheets/GAS or silently bypass `WB_API_TOKEN` feedbacks permission errors. Nested `Жалобы` may expose runtime journal/status and protected selected-row submit jobs; nested `Авто-жалобы` may expose runtime schedules/run-now/tick over the same guarded submit path, but neither path may become unauthenticated, broad or browser-side complaint automation. |
| `C-26` | `Исследования` / SKU group comparison is read-only over accepted truth / persisted ready snapshots, excludes financial metrics in the MVP, makes no causal/statistical claims, and must not trigger refresh/upstream fetch/backfill/reconcile. |
| `C-27` | Promo preflight/manifest/artifact diagnostics and promo current invariant smoke are observability/guard surfaces only; expected ended/no-download non-materializable campaigns must not become fatal missing-artifact blockers, and diagnostics must not become metric truth. |
| `C-28` | Promo historical truth must survive raw artifact retention: normalized campaign rows and manifest/fingerprint metadata are replay-critical, raw XLSX/HAR/screenshots/traces are short-lived debug artifacts, and GC may delete only guarded candidates after replay-critical persistence is proven. |
| `C-29` | Current hosted writes target only the EU runtime (`wb-core-eu-root` / `89.191.226.88` / `/opt/wb-core-runtime/state`). Old selleros (`selleros-root` / `178.72.152.177`) is rollback-only/read-only evidence; routine deploy/apply-nginx/restart/update/GC mutations must fail fast before remote side effects unless the explicit emergency rollback override is set. |
| `C-30` | Current-live EU publication must be production HTTPS, not IP-only/HTTP-only: `public_base_url=https://api.selleros.pro`, nginx `server_name 89.191.226.88 api.selleros.pro;`, `listen 443 ssl` and LetsEncrypt cert/key paths for `api.selleros.pro` are hard invariants. Losing domain/443 is production outage drift, and mutating deploy/apply-nginx must fail locally before remote changes if the invariant is broken. |
| `C-31` | Seller Portal complaint submit is a guarded selected-row/support-runner lane: exact feedback/AI-row match, hard caps and explicit runtime evidence are required; uncertain submit attempts must be resolved through read-only confirmation/detail probes instead of broad resubmission or UI automation. |
| `C-32` | EU bot-backed web-source/seller-funnel owner runtime is host-local: `wb-ai-api.service` binds `/opt/wb-ai/api.py` to `127.0.0.1:8000`; adapters may default to that localhost API and env overrides may relocate the owner runtime, but this must not become a public nginx route or a product-plane source-of-truth shortcut. |
| `C-33` | Seller-session recovery/status/launcher routes must degrade truthfully: missing `/opt/wb-web-bot/venv/bin/python` or unavailable launcher state is surfaced as status/error JSON (`200` status surface or truthful `409` for launcher), not as public 500 or hidden deploy success. |
| `C-34` | WebCore public/operator auth is app-level session auth sourced from runtime env only; probes may create short-lived session cookies in memory, but credentials/session secrets must not be printed, committed or copied into pack metadata. |
| `C-35` | Canonical hosted deploy probes are auth-aware and fast by default; heavy `POST /v1/sheet-vitrina-v1/refresh` verification is an explicit deep probe via `--include-refresh`, not an implicit health-check dependency. |
| `C-36` | Seller Portal browser automation is single-flight in live EU runtime: status sync, submit/batch, auto-complaints tick/run-now, scouts/probes/dry-run/confirmation/detail/relogin and parser/export jobs must acquire `/opt/wb-core-runtime/state/seller_portal_automation.lock.json`, must not run parallel Playwright sessions and must return sanitized busy/blocker metadata when the lock is held. |
| `C-37` | EU live Seller Portal jobs use canonical bot storage state `/opt/wb-web-bot/storage_state.json` or explicit `SELLER_PORTAL_STORAGE_STATE_PATH`; implicit local Mac fallback is forbidden, route-specific capability checks are required, and secrets/session contents must never be printed or copied into reports/pack metadata. |
| `C-38` | Current operator UI labels and visual identity are part of the public surface: top-level menu remains horizontal with `Витрина`, `Поставки`, `Отчёты`, `Отзывы`, `Исследования`; `Витрина 2` and old supply labels must not reappear, and primary/action accent is violet/indigo rather than green. |
| `C-39` | Consumer-visible SPP comes from current Seller Portal `discountOnSite` evidence with exact-date accepted-current preservation/rollover; later blank/failed attempts must not overwrite accepted SPP, and legacy WB Statistics sales-average SPP cannot masquerade as fresh current-visible truth. |
| `C-40` | `Загрузить и обновить` is the canonical full-refresh action for web-vitrina: manual and automatic triggers share the same source/status/materialize/reread semantics, `Asia/Yekaterinburg` date-slot resolution, and truthful warning/error status when expected visible source groups are stale or missing. |
| `C-41` | Web-vitrina/feedbacks visual fixes stay UI-local: wide feedbacks tables must scroll inside bounded containers, and updated-cell feedback uses transient text-color emphasis rather than legacy light backgrounds or persisted styling truth. |
| `C-42` | Reports default-read must not request a not-yet-ready business day: daily-report selects the two latest persisted ready snapshots `<= default_business_as_of_date(now)`, stock-report default selects the latest one, and explicit stock `as_of_date` remains strict exact-read with no fallback/upstream fetch. |
| `C-43` | 1C/Soykasoft `onec_stocks` is a date-capable server-side source group, not a browser/UI truth layer: `onec_product_capital` group refresh must stay date-scoped; historical loads require matching `payload.meta.date`; current stage buckets are `CHINA_TO_FF`, `FF_STOCK`, `FF_TO_WB`, `WB_STOCK`; fresh successful active-SKU-covered payloads may materialize absent canonical buckets as structural zero stock, but source errors/date mismatch/unmapped stages/partial coverage must stay warning/error/blank without fake zeros; 1C profitability totals must remain server-side ratio-of-aggregates where documented. |
| `C-44` | `Метрики` presentation preferences are server-side app-account config, not data/source truth and not browser localStorage truth. LocalStorage may be cache/one-time migration input only; stale/broken browser state must not overwrite newer server config, and new/removed metric keys must merge safely. |
| `C-45` | `CTR в поиске средний` TOTAL must stay a weighted aggregate by SKU `views_current`; low-view SKU CTR outliers must not dominate the total, and valid zero must render/persist as zero instead of dash/missing. |
| `C-46` | Supplier shipment/order UI remains server-owned and supplier-facing: no editable Supplier/Customer fields, fixed supplier metadata `HanShang Technology` shown only in the registry, no order-card `Our SKU`/`Наш SKU`/`我方SKU`, `nmId` and nomenclature remain visible, shipment date is required, original invoice download is auth-protected, delete is operator-only, and unmatched/ambiguous product rows must remain visible. |
| `C-47` | Supplier invoice matching is deterministic only: exact `match_key`, exact aliases, then product-type compatible-model overlap. It must not split invoice quantities into separate rows, must not hardcode filenames/line numbers, and must not use low-confidence fuzzy or first-match selection when candidates are ambiguous. |

# Known gaps

- Operator-facing sheet сейчас intentionally остаётся thin presentation layer поверх current truth; это не новый source-of-truth layer и не место для local subset/fallback logic.
- Hosted runtime deploy/probe contract должен оставаться repo-owned; human-only boundary допускается только для actual access/credentials/target values, а не для route/service archaeology.

# Not in scope

- Полный список всех implementation details.
- Подробная checklist-матрица по каждому модулю.
