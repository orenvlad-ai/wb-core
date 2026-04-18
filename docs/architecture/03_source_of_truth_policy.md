# Source Of Truth Policy

## Code Truth

Git-tracked код в `wb-core` — единственный допустимый source of truth для target implementation.

Runtime-only edits недействительны, пока они не синхронизированы обратно в Git и не прошли review.

## Documentation Truth

Documentation truth в `wb-core` двухслойный:
- primary canonical docs живут в `README.md`, `docs/architecture/*`, `docs/modules/*` и `migration/*`;
- secondary project-oriented pack живёт в `wb_core_docs_master/` и строится из primary docs и текущего code-state.

Правила слоя `wb_core_docs_master/`:
- он не может вводить новые нормы раньше primary docs;
- он не должен быть dump-копией всего `docs/`;
- он должен хранить только retrieval-oriented summary, glossary, register, runbook и manifest;
- legacy knowledge допускается только в тонком register-слое, а не как перенос полного legacy-корпуса.

## Local Project Upload Source

Для внешнего ChatGPT Project единственным допустимым локальным upload source считается:
- `~/Projects/wb-core/wb_core_docs_master`

Нельзя подменять этот source:
- временной копией на Desktop/Downloads;
- zip-архивом без сверки с repo;
- произвольной локальной папкой, не связанной с current `origin/main`.

Готовность pack к upload определяется только по:
- `~/Projects/wb-core/wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md`

Finder timestamps, имя архива, локальные заметки или память исполнителя не считаются признаками readiness.

Если меняется:
- contract;
- status/checkpoint;
- module status;
- smoke/runbook;
- glossary/alias;
- migration boundary или do-not-lose constraint,

сначала обновляется primary canonical doc, а затем затронутый secondary project-pack файл и `wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md`.

Если изменение затронуло primary docs или `wb_core_docs_master/`, внешний ChatGPT Project обновляется уже после merge как один human-only step по загрузке актуального pack.
Этот upload reminder живёт в governance/handoff rules, а не во внутреннем upload-state самого pack.

## Post-Merge Upload-Ready Source Rule

Если изменение затронуло primary docs или `wb_core_docs_master/`, после successful merge Codex обязана:
- безопасно сохранить несвязанный dirty state по правилам workspace policy, если он есть;
- привести `~/Projects/wb-core` к current `origin/main`;
- проверить readiness по `~/Projects/wb-core/wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md`;
- оставить пользователю ровно один human-only remainder: загрузить актуальный `~/Projects/wb-core/wb_core_docs_master` во внешний ChatGPT Project.

Manifest при этом остаётся build-metadata артефактом и не хранит operational state внешней загрузки.

Факты из reference:
- `wb-ai-research/RECONCILE_SUMMARY.md` фиксирует drift между runtime и Git для `wb-ai/analyze.py`;
- `wb-web-bot/RECONCILE_SUMMARY.md` фиксирует тот же класс drift для `bot/fetch_report.py`.

## Schema Truth

Schema truth должен жить в versioned contracts и schema artifacts внутри `wb-core`.

Никакая production schema не должна зависеть от недокументированных table layouts или от SQL, существующего только на сервере.

## Runtime Truth

Runtime truth должен быть наблюдаем через:
- versioned code;
- versioned config shape;
- versioned contract definitions;
- logs/metrics/audit evidence, производимые controlled runtime.

Память человека не является runtime truth.

## Config Truth

Config truth должен быть разделён по ответственности:
- repo-owned config shape и defaults в Git;
- environment-specific secret values вне Git;
- operator-managed business inputs только через явные интерфейсы.

Факты из reference:
- legacy читает `CONFIG` и `METRICS` из Google Sheets;
- legacy server code также зависит от `.env` и service-account files вне Git.

## Data Truth

Data truth должен жить в durable server-side stores и versioned snapshots, а не во временных raw-tabs.

Inference:
- точный target storage engine в этом репозитории пока не зафиксирован.

## Table Truth

Таблица может быть source of truth только для явно выделенных operator-managed inputs.

Таблица не должна быть source of truth для:
- production computation;
- скрытых formulas, определяющих server behavior;
- runtime-only snapshots, нужных downstream-системам.

Факты из reference:
- legacy Apps Script пересобирает `AI_EXPORT` из `DATA`;
- `AI_EXPORT` ingest-ится в `wb-ai-research/wb-ai/ingest.py`;
- текущая таблица до сих пор смешивает operator state и production feed generation.

## Server-Only Truth

Server-only truth допустим только если он:
- представлен явными контрактами;
- воспроизводим из versioned code;
- видим для review через repo artifacts и evidence.

Невидимое server-only поведение запрещено.

## Anti-Drift Policy

Anti-drift rules:
- никакого manual production patch без того же изменения в Git;
- никакой runtime snapshot не принимается как truth без reconcile evidence;
- никакое contract change не проходит без обновления docs/tests/inventory;
- никакое изменение project-pack не проходит без синхронизации с primary repo docs и manifest как build-metadata файла;
- если в задаче менялись primary docs или `wb_core_docs_master/`, после merge `~/Projects/wb-core` должен быть приведён к current `origin/main`, а `~/Projects/wb-core/wb_core_docs_master` должен быть подготовлен как upload-ready source;
- readiness pack определяется по `~/Projects/wb-core/wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md`, а не по Finder timestamps или внешним заметкам;
- manifest внутри pack не должен становиться operational tracker-ом внешней загрузки и не должен требовать post-upload repo-sync loop;
- никакой cutover по принципу "на сервере вроде работает";
- каждый migrated module должен давать reviewable evidence версии кода, версии конфига и snapshot/version semantics.
