# Codex Execution Protocol

## Назначение

Корневой [`AGENTS.md`](../../AGENTS.md) — самодостаточный execution/governance entrypoint. Этот документ раскрывает устойчивый протокол и не создаёт второй независимый набор правил. Доменные contracts и runtime details остаются в релевантных architecture/module/migration docs.

Codex ведёт задачу автономно до проверяемого применимого результата. Базовая цепочка change-задачи:

`implementation → targeted checks → semantic review → fixes/recheck → closure`

Без явной пользовательской границы нельзя завершать задачу на плане, гипотезе, незакоммиченном diff, только локальных проверках или открытом PR. Допустимый незавершённый финал — точный внешний blocker, который нельзя устранить текущими правами или доступными repo-owned средствами.

Отдельные classification fields, prompt footer templates, старые project packs и обязательная служебная строка о режиме выполнения не требуются.

## Кураторский Протокол

Перед техническим выводом, формулированием задачи, реализацией или проверкой результата другого агента необходимо изучить:

- актуальный GitHub state;
- корневой `AGENTS.md`;
- только релевантные authoritative docs;
- фактический код, если вывод касается текущей реализации.

Рабочая ветка остаётся proposed change и не подменяет актуальный `origin/main`. Старые чаты, вложения, прежние ChatGPT Project instructions и legacy artifacts могут использоваться только как migration evidence или do-not-lose constraints. Если репозиторий или обязательный источник недоступен, нельзя уверенно утверждать current state: результатом должен быть точный blocker.

## GOAL Mode И Scope

Задача задаётся через проверяемый конечный результат, а не избыточный микроменеджмент. Каждая change-задача фиксирует:

- цель;
- ожидаемый проверяемый итог;
- bounded scope;
- существенные ограничения и запреты;
- acceptance criteria;
- closure criteria;
- применимый execution-контур.

Routine-шаги, уже определённые `AGENTS.md` и authoritative docs, не нужно подробно повторять в prompt.

Перед изменениями:

- проверить `git status --short`, текущую ветку и remotes;
- проверить GitHub auth, если задача включает GitHub state или closure;
- выполнить `git fetch --prune origin`;
- сравнить `HEAD` с актуальным `origin/main`;
- создать отдельную ветку от актуального `origin/main`;
- не смешивать, не очищать, не reset и не изменять чужой dirty state; при необходимости предпочесть отдельный worktree;
- проверить открытые PR и не включать изменения незамёрженных веток.

Scope должен быть явным и bounded. Не добавляй unrelated redesign, application/business logic, production config или runtime data к docs/governance задаче.

## Пять Execution-Контуров

### `read-only`

Анализ, диагностика или review без изменений. Финальный результат — подтверждённый анализ либо точный внешний blocker. Code, GitHub и production mutations запрещены.

### `repo-only`

Code/docs change без live/runtime эффекта. По умолчанию включает:

- implementation;
- targeted checks;
- semantic review полного diff;
- исправление findings и повторные checks/review;
- синхронизацию authoritative docs;
- полный GitHub closure.

Deploy не применяется.

### `live/runtime`

Используется, если change влияет на public route, service/process, operator UI, runtime behavior, nginx/proxy publication, deploy wiring или другой live contour:

- полный `repo-only` closure;
- после merge — canonical repo-owned deploy;
- проверка deploy commit;
- live/service probe и public/active-surface verify.

Manual server patch, broad catch-all nginx edit и server-only workaround не являются closure. Отсутствие deploy rights или required target value оформляется как exact blocker.

### `production data mutation/backfill`

До любой production data mutation обязательны:

- explicit bounded scope: records/date range и ожидаемый effect;
- read-only preflight;
- dry-run/plan без mutations;
- verified backup либо доказанная reversibility;
- idempotent/resumable execution contract;
- audit trail;
- необходимые human approval gates;
- canonical repo-owned runner/path;
- post-run reconciliation;
- targeted data checks и non-target invariants.

Ad-hoc SQL, произвольные SSH-команды, незафиксированный server-only script и обход safety gates запрещены. Итог фиксирует точные `changed/skipped/failed` и reconciliation evidence.

### `archived GAS guard`

Используется только при явно заданном bounded изменении archive guard:

- не возрождает Google Sheets/GAS как current runtime;
- требует targeted guard checks;
- публикует guard через canonical bounded path;
- проверяет, что archived functions продолжают fail fast.

Этот контур не является normal completion path для website/operator задач.

## Default Completion И Явная Граница

Если пользователь явно не ограничил closure, Codex самостоятельно выполняет полный применимый контур.

Для `repo-only`:

`implementation → checks → semantic review → fixes/recheck → docs sync → commit → push → PR → checks/review → merge → удаление feature-ветки → fetch/prune → подтверждение результата в актуальном origin/main`

Для `live/runtime` после всего `repo-only` closure обязательны canonical deploy, deploy-commit equality и live/service/public verify.

Для `production data mutation/backfill` выполняются применимый GitHub/runtime closure, обязательный safety-контур и human gates.

Явное ограничение пользователя имеет приоритет: «только ветка», «до commit», «до draft PR», «без merge», «без deploy», «без production mutations» или другая точная граница. Тогда Codex останавливается ровно на ней, подтверждает достигнутое состояние и не считает отсутствие дальнейшего closure ошибкой. Ограничение closure не расширяет authority для иных mutations.

## Проверка И Semantic Review

Для каждого change проверь применимое:

- scope: изменены только requested и прямо необходимые support files;
- diff hygiene: нет случайных secrets, credentials, runtime paths с секретами, production data, generated dumps или unrelated edits;
- contracts/boundaries: утверждения подтверждены code и authoritative docs;
- targeted tests/checks соответствуют риску;
- documentation truth синхронизирован с change;
- итоговый semantic diff отдельно прочитан полностью;
- findings исправлены, после чего checks и semantic review повторены;
- GitHub/live/data closure соответствует выбранному контуру и явной границе.

Review только списка файлов не считается semantic review.

## Независимое Подтверждение Результата

Отчёт Codex или другого агента не является доказательством сам по себе. Перед подтверждением проверь применимое:

- фактический GitHub state;
- branch и commit SHA;
- semantic diff;
- targeted tests/checks;
- review findings и внесённые исправления;
- отсутствие unresolved review threads;
- согласованность с authoritative docs;
- для `live/runtime` — deployed commit и live/service/public result;
- для production mutation — dry-run, backup/reversibility, audit, reconciliation и non-target invariants.

## GitHub Closure Принадлежит Codex

Если полный GitHub closure входит в scope, Codex выполняет:

1. stage только intended files;
2. commit с ясным заголовком;
3. push рабочей ветки;
4. PR в требуемую base branch;
5. ожидание и проверку CI/checks;
6. чтение comments, reviews и unresolved threads;
7. fixes, повторные tests/review и push;
8. merge;
9. удаление remote/local feature branch;
10. `fetch --prune` и подтверждение результата в актуальном `origin/main`.

Если пользователь задал более раннюю границу, например draft PR без merge, выполняются только шаги до этой границы включительно. Manual handoff допустим только при конкретной permission/protection/approval ошибке.

## Documentation Sync

Authoritative docs живут в:

- `README.md`;
- `docs/architecture/*`;
- `docs/modules/*`;
- `migration/*`.

Новый module doc добавляется в `docs/modules/00_INDEX__MODULES.md` в той же задаче. Изменение documented contract, runtime boundary, verification path или module status требует синхронного docs update.

## Human-Only Boundary И Blockers

Участие пользователя требуется только для действительно human-only действия:

- login;
- отсутствующего permission или approval;
- manual UI-check, который нельзя надёжно автоматизировать;
- materially different risk decision;
- production mutation approval;
- предоставления недоступного внешнего источника.

Обычные Git, GitHub, test, review, merge и deploy действия не перекладываются на пользователя, если они входят в scope и доступны Codex.

Остановиться до заданной closure boundary можно только когда:

- требуются отсутствующие права, approval или credentials;
- внешний сервис недоступен и безопасные retries/диагностика исчерпаны;
- repository evidence действительно конфликтует и выбор изменит requested scope;
- необходима новая authority для production mutation или materially different action.

Финал при blocker содержит точную ошибку/ограничение, сохранённое состояние и один минимальный ручной шаг.

## Формат Итогового Ответа

Итог содержит только применимое:

1. итоговый статус;
2. что реально изменено;
3. что реально проверено;
4. что намеренно осталось вне scope;
5. blocker и один минимальный следующий шаг — только если blocker есть.
