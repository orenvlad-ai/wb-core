# Codex Execution Protocol

## Назначение

Корневой [`AGENTS.md`](../../AGENTS.md) — самодостаточный execution/governance entrypoint. Этот документ раскрывает устойчивый протокол и не создаёт второй независимый набор правил. Доменные contracts и runtime details остаются в релевантных architecture/module/migration docs.

Codex ведёт задачу автономно до проверяемого применимого результата. Базовая цепочка change-задачи:

`implementation → targeted checks → semantic review → fixes/recheck → closure`

Без явной пользовательской границы нельзя завершать задачу на плане, гипотезе, незакоммиченном diff, только локальных проверках или открытом PR. Допустимый незавершённый финал — точный внешний blocker, который нельзя устранить текущими правами или доступными repo-owned средствами.

Старые project packs, prompt footer templates и прежние служебные mode-строки не требуются. Новый task prompt по возможности начинается явной строкой класса из корневого `AGENTS.md`; её отсутствие запускает deterministic auto-classification, а не блокирующий запрос пользователю.

## Task Class И Execution Contour

Task class и execution contour ортогональны:

- `ДИАГНОСТИКА` задаёт строго read-only orchestration и никогда не создаёт branch/PR;
- `СТАНДАРТ` задаёт полный применимый closure через отдельный PR и GitHub Release Train;
- `LOOP` задаёт итерационный live/runtime closure с pre-deploy agent handshake и обязательным production UI acceptance.

Execution contour (`read-only`, `repo-only`, `live/runtime`, `production data mutation/backfill`, `archived GAS guard`) описывает техническую границу. `СТАНДАРТ` получает GitHub label `task:standard`, `LOOP` — `task:loop`; диагностическая задача не входит в Release Train. Явная строка имеет приоритет, а при её отсутствии класс определяется автоматически по правилам ниже.

Явные строки класса:

- `КЛАСС ЗАДАЧИ: СТАНДАРТ`;
- `КЛАСС ЗАДАЧИ: LOOP`;
- `КЛАСС ЗАДАЧИ: ДИАГНОСТИКА`.

Если явной строки нет, Codex до начала работы выбирает класс по contract order:

- исключительно read-only анализ без изменений code, GitHub state и production — `диагностика`;
- deploy с последующими production UI Flow, Playwright-проверками и итерациями до live-результата — `loop`;
- обычная реализация, repo-only изменение или неоднозначный случай — `стандарт`.

Неоднозначный выбор всегда завершается `стандарт`, поэтому отдельное уточнение класса не требуется. Codex начинает автоматически классифицированную работу сообщением `Класс задачи: стандарт — определён автоматически`, `Класс задачи: loop — определён автоматически` или `Класс задачи: диагностика — определён автоматически` и кратко фиксирует основание. Класс определяет orchestration, но не расширяет requested scope или authority.

Task class и task continuity определяются независимо. Machine-readable continuity из `apps/github_release_train_spec.py` имеет четыре значения:

- `NEW_TASK` — самостоятельная identity с новой branch/PR;
- `ACTIVE_ADDITION` — явное дополнение к незавершённой активной задаче;
- `ACTIVE_LOOP_RECOVERY` — дефект текущего production UI acceptance с active `release:awaiting-ui`;
- `TERMINAL_STALE_REFERENCE` — недопустимая попытка recovery terminal-задачи.

Только `ACTIVE_ADDITION` наследует текущую branch/PR; только `ACTIVE_LOOP_RECOVERY` наследует активный LOOP root. `release:ready`, `release:running`, `release:awaiting-agent`, `release:awaiting-ui`, `release:needs-resume`, `release:blocked` и `release:halted` активны. `release:done`, `release:production` и `release:superseded` terminal: после них запрещено наследовать branch, PR, task identity, LOOP root, acknowledgement, owner heartbeat и recovery identity.

Фразы «новая задача», «отдельная задача», «самостоятельная задача» и «новый LOOP» всегда выбирают `NEW_TASK`. Новый дефект после `release:done`/`release:production` является новой задачей, даже если обсуждается в том же чате, относится к тому же экрану или функциональному разделу либо логически продолжает прежнюю реализацию. Одинаковый чат/раздел не доказывает continuity; неоднозначность всегда даёт `NEW_TASK`. Сам класс `LOOP` recovery не означает.

`LOOP` обычно запускается через `/goal`. Если формальный Goal Mode не активирован, Codex всё равно ведёт ту же сессию через handshake, deploy, UI Flow, recovery iterations и terminal acceptance, не завершая её на промежуточном label.

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

Если PR явно поставлен в repo-owned GitHub Release Train, Codex не передаёт ответственность очереди и не завершает task на метке `release:ready`. Task owner обязан:

- использовать отдельную branch/worktree и отдельный PR для каждого независимого change;
- добавить ровно одну task label: `task:standard` или `task:loop`;
- добавить ровно одну label `scope:repo-only`, `scope:live-runtime` или `scope:production-mutation`;
- ставить STANDARD `release:ready` только после targeted checks, semantic review, fixes/recheck и docs sync;
- LOOP после successful baseline регистрировать только одной из разных repo-owned commands: `/wb-core loop enqueue-new <PR> head <HEAD_SHA>` или `/wb-core loop enqueue-recovery <PR> head <HEAD_SHA> gate <ACTIVE_GATE_PR> root <ROOT>`; вручную `loop:root-*`/`release:ready` не назначать;
- для STANDARD наблюдать workflow до `release:done`/`release:production` либо исправить `release:blocked`/`release:halted`;
- для LOOP подтвердить exact-head `release:awaiting-agent`, продолжить на `release:awaiting-ui`, выполнить production UI Flow и закрыть gate GitHub-native acceptance-командой;
- считать gate другой LOOP-цепочки штатным waiting независимо от числа polls, goal-turns и продолжительности: не называть его blocker, не снимать/обходить/перехватывать и не завершать task handoff-сообщением;
- не разрешать Release Train автоматически выполнять production data mutation/backfill.

Release Train сериализует только финальную критическую секцию и не выполняет semantic conflict resolution. Полный контракт: [`11_github_release_train.md`](11_github_release_train.md).

Codex CLI наблюдает очередь без AI polling loop:

`python3 apps/github_release_train_wait.py <PR>`

Waiter ведёт один обновляемый status/heartbeat comment на активном PR и не создаёт повторяющиеся comments. Для LOOP он при own `release:awaiting-agent` заново читает actual head и публикует `/wb-core loop ack-agent <PR> head <HEAD_SHA>`; handler создаёт repo-owned proof, поэтому manually added `loop:ack-*` label не открывает merge. Чужие `ready/running/awaiting-agent/awaiting-ui/halted` — normal waiting без terminal timeout. Код `3` означает own UI Flow, `4` — owner resume без ack, `2` — own blocker или conflicting invariant, `130` — interrupt.

Новый LOOP всегда имеет `root == PR`; recovery — `root < PR` и exact proof текущего `awaiting-ui` gate; `root > PR` запрещён. Новый root может нормально ждать за чужим UI gate. Recovery-link немедленно становится stale при исчезновении gate или terminal closure root. Waiter проверяет enrollment proof до heartbeat/ack и завершает fail-closed при classification error.

Trusted comment `/wb-core loop retry-blocked <PR> head <HEAD_SHA>` сохраняет task class, scope и root и применим только к техническому pre-merge blocker; enqueue-команды такой blocker не снимают. После successful baseline на новом fix-head retry может обновить exact-head marker уже доказанной new/recovery identity, но не создать и не переклассифицировать её. Comment обрабатывается trusted-main GitHub Actions, поэтому exact-head proof не зависит от identity локального `gh` token. Локальный waiter при classification mismatch только останавливается fail-closed и не выставляет label/comment от имени пользователя. Classification error сохраняет provenance через последующие смены head и обычным retry не исправляется; его разрешает только более поздний repo-owned new/recovery/correction proof. Отдельная `/wb-core loop correct-to-new <PR> head <HEAD_SHA> old-root <ROOT>` требует open/unmerged exact PR/head, successful baseline, `OWNER`/`MEMBER` authorization, classification-blocker proof, доказанный terminal old root и отсутствие его active gate; она одним label replacement создаёт independent root и идемпотентный audit proof. Повторная доставка уже доказанной enqueue/correction команды после перехода в `running`, `awaiting-agent` или `blocked` безопасно ничего не меняет и не возвращает PR в `ready`.

Нормальное ожидание очереди не превращается в blocker после N polls или goal-turns. Если LOOP heartbeat исчез на `ready/running/awaiting-agent/awaiting-ui`, worker добавляет overlay `release:needs-resume` и точную команду `python3 apps/github_release_train_wait.py <PR> --resume-owner --no-ack-agent`. Resume проверяет exact head/root, снимает только overlay и не выполняет ack или acceptance.

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

## Production UI Verification

Production UI-проверка доказывает фактический browser render и не заменяется HTTP/service evidence. HTTP `200`, успешный `curl`, наличие HTML, совпадение route tokens или canonical `public-probe` остаются полезными transport/content checks, но сами по себе не подтверждают, что пользовательская surface загрузилась и отрисовалась без client-side failure.

Surface policy:

- в Codex CLI по умолчанию сразу использовать локальный Python/Node Playwright с установленным Chrome/Chromium в новом изолированном непостоянном browser context; встроенный Browser в CLI недоступен и не является preflight-попыткой;
- в ChatGPT web/desktop встроенный Browser можно использовать, если он доступен; независимо от поверхности применяется один evidence contract;
- по умолчанию не подключать пользовательский Chrome profile, `user_data_dir`, cookies, storage state или сохранённые credentials; авторизованный context допустим только при explicit scope и безопасно доступной авторизации;
- не выполнять click, form fill, keyboard input, refresh/save/submit/delete/run-now или другие business mutations, если они прямо не входят в bounded UI Flow;
- browser package/binary можно установить только когда это необходимо и разрешено текущим permission contour; отсутствие Playwright/Chrome/Chromium или требуемой авторизации является точным blocker, а не основанием подменить UI Flow HTTP-probe.

Минимальное production UI evidence включает:

1. requested URL, final URL и document response/redirect chain;
2. отсутствие `5xx` у navigation и загруженных ресурсов;
3. ожидание `DOMContentLoaded`, видимый фактический render и непустые `document.title`/`body`;
4. собранные `pageerror`, явные fatal-error surface matches и существенные console errors; безвредные ошибки вроде missing favicon можно классифицировать отдельно, но не скрывать;
5. локальный screenshot фактической final surface и его визуальную проверку.

Screenshot и временный test harness не коммитятся без отдельного explicit scope. Для LOOP exact command `/wb-core loop accept-ui <PR> deployed <MERGE_SHA> evidence sha256:<EVIDENCE_HASH>` допустима только после успешного browser evidence и для current deployed proof. Старый PR после recovery не принимается. При UI-проблеме или недоступной авторизации `release:awaiting-ui` сохраняется fail-closed.

Проверенный post-registration reference flow — [PR #616](https://github.com/orenvlad-ai/wb-core/pull/616): изолированный CLI Playwright/Chrome подтвердил защищённый operator route через ожидаемый redirect на отрендеренную login surface, после чего exact UI acceptance перевёл LOOP в `release:production`, а post-accept worker подтвердил пустую очередь. Этот исторический PR предшествует отдельным new/recovery enrollment proofs и не разрешает ручную identity.

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

Для PR, вошедшего в Release Train, steps 5–10 выполняются через canonical queue workflow и подтверждаются его фактическим terminal state; Codex остаётся task owner и проверяет GitHub/live evidence после завершения workflow.

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
