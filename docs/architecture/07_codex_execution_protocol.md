# Codex Execution Protocol

## Цель

Codex ведёт конкретную change-задачу автономно до проверяемого результата. Нормальная цепочка:

`implementation → targeted tests → review → fixes/recheck → closure`

Нельзя завершать задачу на плане, гипотезе, локальном diff или открытом PR. Допустимый незавершённый финал — только точный внешний blocker, который нельзя устранить текущими правами или доступными repo-owned средствами.

Отдельные classification fields, prompt footer templates и обязательная служебная строка о режиме выполнения не требуются.

## Scope И Preflight

Перед изменениями:

- проверить `git status --short`, текущую ветку и remotes;
- проверить GitHub auth, если задача включает GitHub closure;
- выполнить `git fetch --prune origin`;
- сравнить `HEAD` с current `origin/main`;
- создать отдельную ветку от current `origin/main`;
- сохранить и не смешивать чужой dirty state.

Scope должен быть явным и bounded. Не добавляй unrelated redesign, application/business logic, production config или runtime data к docs/governance задаче.

## Четыре Практических Контура

### `repo-only`

Используется для code/docs changes без live/runtime эффекта:

- targeted checks;
- review итогового diff;
- исправление findings и повторная проверка;
- commit, push, PR, checks/review, merge и удаление ветки;
- deploy: not applicable.

### `live/runtime`

Используется, если change влияет на public route, service/process wiring, runtime behavior, nginx/proxy publication, operator UI или другой live contour:

- полный `repo-only` closure;
- после merge — canonical repo-owned deploy;
- live/service probe и public/active-surface verify;
- отсутствие deploy rights или required target value оформляется как exact blocker.

Manual server patch, broad catch-all nginx edit и server-only workaround не являются closure.

### `production data mutation/backfill`

До любой production data mutation обязательны:

- explicit bounded scope и затрагиваемые records/date range;
- read-only preflight и оценка ожидаемого effect;
- dry-run/plan без mutations;
- verified backup либо доказанная reversibility;
- idempotent/resumable execution contract;
- audit trail и post-run reconciliation;
- требуемые human approval gates;
- canonical repo-owned runner/path.

Ad-hoc SQL, произвольная SSH-команда, незафиксированный server script или обход safety gates запрещены. После выполнения нужны targeted data checks, non-target invariants и точный итог changed/skipped/failed.

### `archived GAS guard`

Используется только при явно заданном bounded изменении archive guard:

- не возрождает Google Sheets/GAS как current runtime;
- выполняет targeted guard checks;
- публикует guard через canonical bounded path;
- проверяет, что archived functions продолжают fail fast.

Этот контур не является normal completion path для website/operator задач.

## Проверка И Review

Для каждого change проверь применимое:

- scope: изменены только запрошенные и прямо необходимые support files;
- diff hygiene: нет случайных secrets, runtime data, generated dumps или unrelated edits;
- contracts/boundaries: утверждения подтверждены code и authoritative docs;
- targeted tests/checks: минимальный набор соответствует риску изменения;
- documentation sync: truth и docs обновлены вместе;
- review: итоговый diff прочитан отдельно в read-only режиме;
- findings: замечания исправлены, затем проверки и review повторены;
- closure: GitHub и, где нужно, live/runtime контур полностью закрыты.

Review не считается выполненным, если проверялся только список файлов без чтения semantic diff.

## GitHub Closure Принадлежит Codex

Если requested outcome включает Git/GitHub closure и пользователь не запретил writes, Codex выполняет:

1. stage только intended files;
2. commit с ясным заголовком;
3. push рабочей ветки;
4. PR в требуемую base branch;
5. ожидание и проверку CI/checks;
6. чтение review feedback и unresolved threads;
7. fixes, повторные tests и push;
8. merge;
9. удаление remote/local feature branch;
10. `fetch --prune` и подтверждение merge commit в current `origin/main`.

Manual handoff допустим только при конкретной GitHub permission/protection/approval ошибке. В этом случае укажи точный blocker и один минимальный ручной шаг.

## Documentation Sync

Authoritative docs живут в:

- `README.md`;
- `docs/architecture/*`;
- `docs/modules/*`;
- `migration/*`.

Новый module doc добавляется в `docs/modules/00_INDEX__MODULES.md` в той же задаче. Изменение documented contract, runtime boundary, verification path или module status требует синхронного docs update.

## Stop Conditions

Остановиться можно только когда:

- требуются отсутствующие права, approval или credentials;
- внешний сервис недоступен и безопасные retries/диагностика исчерпаны;
- repository evidence действительно конфликтует и выбор изменит requested scope;
- необходима новая authority для production mutation или materially different action.

Финал при blocker должен содержать точную ошибку/ограничение, сохранённое состояние работы и один минимальный ручной шаг. В остальных случаях работа продолжается до полного closure.
