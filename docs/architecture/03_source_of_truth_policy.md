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

Если меняется:
- contract;
- status/checkpoint;
- module status;
- smoke/runbook;
- glossary/alias;
- migration boundary или do-not-lose constraint,

сначала обновляется primary canonical doc, а затем затронутый secondary project-pack файл и `wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md`.

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
- никакое изменение project-pack не проходит без синхронизации с primary repo docs и manifest-флагом `project_upload_required = true`;
- никакой cutover по принципу "на сервере вроде работает";
- каждый migrated module должен давать reviewable evidence версии кода, версии конфига и snapshot/version semantics.
