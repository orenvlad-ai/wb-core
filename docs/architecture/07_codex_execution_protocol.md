# Codex Execution Protocol

## Назначение

Корневой [`AGENTS.md`](../../AGENTS.md) — самодостаточный execution/governance entrypoint. Этот документ раскрывает устойчивый протокол и не создаёт второй независимый набор правил. Доменные contracts и runtime details остаются в релевантных architecture/module/migration docs.

Codex ведёт задачу автономно до проверяемого применимого результата. Базовая цепочка change-задачи:

`implementation → targeted checks → semantic review → fixes/recheck → closure`

Без явной пользовательской границы нельзя завершать задачу на плане, гипотезе, незакоммиченном diff, только локальных проверках или открытом PR. Допустимый незавершённый финал — точный внешний blocker, который нельзя устранить текущими правами или доступными repo-owned средствами.

Старые project packs, prompt footer templates и прежние служебные mode-строки не требуются. Новый task prompt по возможности начинается явной строкой класса из корневого `AGENTS.md`; её отсутствие запускает deterministic auto-classification, а не блокирующий запрос пользователю.

## Prompt Contract И Technical Path Revalidation

ChatGPT/куратор формирует prompt вокруг результата: цель, необходимые данные, read-only/mutation boundaries, итоговый artifact/answer, acceptance и closure. Он не называет WebCore Data MCP, не назначает connector/server/runtime/storage/SSH alias и не запрещает canonical server-side read, если пользователь сам отдельно и явно не установил такое ограничение. Каждый сформированный prompt содержит provenance-правило:

`Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное.`

Codex до выполнения повторно проверяет предложенный технический путь по current `origin/main`, `AGENTS.md`, релевантным authoritative docs и code truth. Tool/source/path из prompt остаётся технической гипотезой автора prompt даже при повелительной формулировке. Только отдельное явное требование самого пользователя становится binding constraint. Поэтому старый prompt с обязательным MCP или запретом server-side access без отдельного пользовательского требования игнорируется в этой части и не создаёт blocker.

Если нужны production evidence/data, normal acquisition path:

1. определить current active production target, runtime и concrete stores/documents по code и authoritative docs;
2. выполнить фактический `PRODUCTION_READ_PREFLIGHT`, включая штатный SSH connectivity к canonical target и доступность exact source;
3. читать production stores query-only (`mode=ro` + `PRAGMA query_only=ON` для SQLite или эквивалентный read-only режим) и bounded server-owned documents по их current contract;
4. не менять services, schedules, runtime files/data, upstream systems или production config и не раскрывать secrets/raw dumps;
5. объявлять blocker только после точной ошибки canonical access/preflight либо доказанного отсутствия необходимых данных.

Архивный WebCore Data MCP не является normal execution path, не упоминается в новых prompts и не требуется как fallback/preflight. Его отсутствие всегда non-blocking.

## Task Class И Execution Contour

Task class и execution contour ортогональны:

- `ДИАГНОСТИКА` задаёт строго read-only orchestration и никогда не создаёт branch/PR;
- `СТАНДАРТ` задаёт полный применимый closure; для PR-backed изменений это отдельный PR и GitHub Release Train, а для чистого `user-artifact` — фактическое создание и проверка файла без GitHub closure;
- `LOOP` задаёт итерационный live/runtime closure с pre-deploy agent handshake и обязательным production UI acceptance.

Execution contour (`read-only`, `user-artifact`, `repo-only`, `live/runtime`, `production data mutation/backfill`, `archived GAS guard`) описывает техническую границу. PR-backed `СТАНДАРТ` получает GitHub label `task:standard`, `LOOP` — `task:loop`; диагностическая задача и non-PR `user-artifact` в Release Train не входят. Явная строка имеет приоритет, а при её отсутствии класс определяется автоматически по правилам ниже.

Явные строки класса:

- `КЛАСС ЗАДАЧИ: СТАНДАРТ`;
- `КЛАСС ЗАДАЧИ: LOOP`;
- `КЛАСС ЗАДАЧИ: ДИАГНОСТИКА`.

Если явной строки нет, Codex до начала работы выбирает класс по contract order:

- создание или изменение пользовательского файла вне репозитория — `стандарт` с contour `user-artifact`; требуемая запись файла не является `ДИАГНОСТИКОЙ`;
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

## Phase-Local Preflight И Dependency Planning

Preflight не является единым глобальным барьером. Канонические фазы из `apps/github_release_train_spec.py` упорядочиваются по зависимостям, даже если prompt перечисляет production preflight первым:

1. `REPOSITORY_PREFLIGHT` проверяет repository/worktree, `AGENTS.md`, architecture/runners, локальные зависимости, test infrastructure и при необходимости GitHub baseline. Production credentials/database, архивный WebCore Data MCP, browser session, manifests и backup здесь не нужны.
2. Repository implementation/validation/runner preparation, branch/PR, CI и review выполняются до максимально возможного безопасного состояния.
3. `PRODUCTION_READ_PREFLIGHT` выполняется только непосредственно перед чтением конкретного production evidence и проверяет только фактически нужный read-only source/capability.
4. `PRODUCTION_MUTATION_PREFLIGHT` выполняется только непосредственно перед apply и проверяет exact scope, dry-run/coverage, manifest/digests, backup/restore readiness, expected affected entities, non-target invariants, authorization, exact deployed runner/version и reconciliation path.
5. `PRODUCTION_UI_PREFLIGHT` выполняется только перед production UI acceptance и фактически проверяет local Playwright/Chromium и необходимую именно этой операции authorization.

Для задачи с mutation правильная dependency chain: `repository development → PR/review → deploy runner → production dry-run/read preflight → backup/manifests/digests/evidence → explicit apply → readback/reconciliation → UI acceptance`, если UI требуется. Невозможность выполнить поздние production steps не отменяет и не блокирует независимые ранние steps.

Phase context входит в тот же Goal disposition contract через `current_phase`, `blocked_phase`, `safe_phases_remaining`, `required_capability`, `capability_evidence`, `next_executable_action`, `user_intervention_required`. Недоступная будущая capability при оставшейся безопасной работе даёт `CONTINUE_SAFE_PHASES`; `AWAIT_PHASE_CAPABILITY` допустим только у непосредственной phase boundary, когда safe phases завершены, фактический preflight приложен, repo-owned remediation отсутствует/исчерпана и требуется точное human-only действие. Общий `EXTERNAL_BLOCKER` при `safe_phases_remaining` конструктивно запрещён.

Production evidence извлекается через canonical server-side read path, а конкретные target/runtime/store/document определяются из current repo/docs truth, не из prompt. Архивный MCP не проверяется и не запрашивается как prerequisite/fallback: его отсутствие не влияет ни на одну phase и не может дать `AWAIT_PHASE_CAPABILITY`, `EXTERNAL_BLOCKER` или `TERMINAL_FAILURE`. Сохранившийся compatibility implementation остаётся read-only; mutation через него запрещена.

Будущий production-data runner создаётся в репозитории и до production gate тестируется на fixtures/mocks. Его обязательный contract: dry-run по умолчанию, отдельный explicit apply flag, bounded scope, machine-readable manifest, pre-change digest, backup/evidence, expected affected records, non-target invariants, idempotency либо документированный recovery, post-apply readback и reconciliation. Случайные локальные scripts, ad-hoc SQL и server-only drift production mutation не выполняют.

## GOAL Mode И Scope

Задача задаётся через проверяемый конечный результат, а не избыточный микроменеджмент. Каждая change-задача фиксирует:

- цель;
- ожидаемый проверяемый итог;
- bounded scope;
- существенные ограничения и запреты;
- acceptance criteria;
- closure criteria;
- применимый execution-контур.

Для data/artifact-задачи prompt называет нужные данные и read-only boundary, но не выбирает MCP, server, connector или storage. Routine-шаги и technical path, уже определённые `AGENTS.md` и authoritative docs, не нужно подробно повторять в prompt.

Перед repo-changing изменениями:

- проверить `git status --short`, текущую ветку и remotes;
- проверить GitHub auth, если задача включает GitHub state или closure;
- выполнить `git fetch --prune origin`;
- сравнить `HEAD` с актуальным `origin/main`;
- создать отдельную ветку от актуального `origin/main`;
- не смешивать, не очищать, не reset и не изменять чужой dirty state; при необходимости предпочесть отдельный worktree;
- проверить открытые PR и не включать изменения незамёрженных веток.

Scope должен быть явным и bounded. Не добавляй unrelated redesign, application/business logic, production config или runtime data к docs/governance задаче.

## Thread Heartbeat Automation

Этот protocol применяется к Codex/ChatGPT Desktop и любой иной поверхности только тогда, когда в текущем контексте фактически доступен callable automation contract, способный периодически возобновлять exact target thread и поддерживающий остановку либо удаление. Название macOS, Desktop, Codex, ChatGPT, IDE, CLI, project path или client version само по себе ничего не доказывает. Capability проверяется по реально доступной операции; её отсутствие не является blocker, не меняет task class/continuity/closure и не разрешает утверждать, что monitor создан.

### Trigger, Identity И Ownership

Для каждой новой нетерминальной задачи, создаваемой либо получаемой на capability-enabled поверхности, нужен ровно один recurring heartbeat с интервалом 10 минут, связанный с той же task/thread identity:

1. инициирующий Chat/ChatGPT после появления пригодной target thread identity ищет существующий heartbeat этой exact identity и создаёт его только при отсутствии;
2. при передаче Chat → Codex принимающая задача повторяет exact-identity lookup при первой безопасной возможности и служит idempotent fallback, если инициатор не создал monitor;
3. найденный heartbeat переиспользуется независимо от того, кто его создал; новый owner не создаёт второй schedule;
4. если конкурентное создание всё же оставило дубликаты, владелец сохраняет один exact-identity heartbeat, а остальные останавливает/удаляет через поддерживаемый contract;
5. heartbeat не создаёт новую задачу, thread, branch, PR, LOOP root или параллельного исполнителя и не меняет continuity.

Target identity должна однозначно указывать на исходную задачу, а не только на проект, репозиторий или экран. До появления такой identity инициатор не создаёт приблизительный monitor. Принимающий Codex не считает отсутствие monitor доказательством отсутствия capability: сначала он проверяет фактически доступный contract и existing exact-identity schedules.

### Run Contract

Каждый 10-минутный запуск сначала читает фактическое состояние target task:

- `active`: основной turn ещё исполняется — heartbeat не запускает конкурирующую работу, не форкает task и не повторяет действие;
- `idle + non-terminal`: основной turn не исполняется — heartbeat продолжает ближайшее безопасное действие в рамках исходных scope, authority, class и continuity;
- `human-only`: непосредственное продолжение требует только login, approval, permission, unavailable source или иной доказанный human-only action — heartbeat сообщает точный blocker и минимальное действие пользователя, не имитируя прогресс;
- `terminal success`, доказанный `terminal failure` или явная остановка пользователем: heartbeat не возобновляет работу и немедленно останавливается/удаляется по поддерживаемому automation contract.

Ни один heartbeat run не расширяет authorization, не повторяет небезопасную mutation и не подменяет active task owner. Временная ошибка, отсутствие изменения state, elapsed time или внешний queue wait сами по себе не являются terminal failure.

### Progress Report

Каждый осмысленный запуск публикует одну короткую строку:

`Прогресс ≈<процент>% · ETA ≈<диапазон> · сделано: <одна короткая фраза>.`

Процент строится по уже доказанным этапам применимого closure, а ETA — по оставшимся проверяемым этапам. Нельзя повышать процент из-за количества heartbeat runs или выдумывать ETA при внешнем ожидании. В последнем случае поле формулируется как `ETA ≈зависит от <точная внешняя зависимость>`; `сделано` называет последнее подтверждённое изменение/проверку. Проверка, обнаружившая всё ещё исполняющийся основной turn и не изменившая task state, не считается осмысленным запуском для ложного progress update.

### PR-Backed `wb-core`

Thread heartbeat является только 10-минутным wakeup/orchestration layer. Для PR-backed `wb-core` durable truth остаётся в GitHub, а каждое возобновление вызывает canonical:

`python3 apps/github_release_train_wait.py <OWN_PR> --shepherd`

Heartbeat интерпретирует `TERMINAL_SUCCESS`, `CONTINUE_WAITING`, `CONTINUE_SAFE_PHASES`, `AWAIT_PHASE_CAPABILITY`, `OWN_ACTION`, `TAKEOVER_PREDECESSOR`, `RECOVER_OWN_CHAIN`, `EXTERNAL_BLOCKER`, `TERMINAL_FAILURE` и выполняет только разрешённое ближайшее действие. Он не создаёт второй state machine, не переводит PR labels, не выполняет automatic ack/acceptance и не объявляет blocker вопреки disposition. `TAKEOVER_PREDECESSOR` допустим только по существующему exact `release:needs-resume` lost-owner proof; наличие thread heartbeat само по себе ownership не передаёт.

10-минутный Desktop heartbeat и 5-минутное GitHub observation независимы по назначению: первый будит exact task/thread, второй наблюдает durable repository queue и публикует `release:needs-resume` после своего threshold. Thread heartbeat не меняет schedule/threshold worker, не заменяет waiter status comment и не снимает fail-closed gates. Если automation capability недоступна, canonical waiter/shepherd и обычная task continuity продолжают работать без деградации protocol.

### Cleanup И Local Availability

После доказанного terminal state или явного user stop owner проверяет, что exact-identity schedule действительно остановлен либо удалён; одной финальной фразы без supported automation result недостаточно. Для задач, зависящих от локальных файлов, действует эксплуатационное ограничение: компьютер и Desktop должны быть запущены, а проект и target files — оставаться доступны. Это availability limitation, а не новый source of truth и не разрешение копировать локальные данные в другую систему.

## Шесть Execution-Контуров

### `read-only`

Анализ, диагностика или review без изменений. Финальный результат — подтверждённый анализ либо точный внешний blocker. Code, GitHub и production mutations запрещены.

### `user-artifact`

Создание или изменение запрошенного XLSX, CSV, DOCX, PDF, TXT либо аналогичного пользовательского файла является mutation и потому не является `ДИАГНОСТИКОЙ`. Класс задачи — `СТАНДАРТ`, но если единственная mutation — итоговый файл вне репозитория, применяется non-PR closure:

- branch, worktree, commit и PR не создаются;
- GitHub Release Train не запускается, GitHub labels/comments/state не изменяются;
- label `scope:user-artifact` не существует и не создаётся;
- code, docs и любые repo files не меняются;
- production и business data не изменяются;
- разрешены только необходимые read-only источники, временные builder/intermediate files вне repo и итоговый файл по точному пути пользователя.

Изменение Git-tracked документации, кода, tests или repo-owned helper — даже если оно посвящено пользовательским artifacts — не попадает в это исключение: это обычный `СТАНДАРТ + scope:repo-only` с полным GitHub closure.

User-artifact завершён только после фактического создания и проверки запрошенного файла. Подготовленные данные, только CSV вместо XLSX, свободный целевой путь, описание будущих действий или synthetic placeholder completion не являются. Итоговый файл — производный export/snapshot, а не новый canonical source of truth.

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

## Создание Нового XLSX В `user-artifact`

Этот contract применяется к новым обычным табличным XLSX. Для сложного редактирования существующей книги нельзя применять fallback, который может потерять formulas, styles, charts, relationships или workbook structure; нужен format-preserving tool либо точный capability blocker после bounded recovery.

### Основной Path

1. Использовать активный Spreadsheets skill и `@oai/artifact-tool`.
2. Проверить `CODEX_PRIMARY_RUNTIME_ROOT`, `CODEX_PRIMARY_RUNTIME_NODE`, `CODEX_PRIMARY_RUNTIME_NODE_MODULES` и `CODEX_PRIMARY_RUNTIME_PYTHON`.
3. Создать отдельную временную директорию вне репозитория.
4. Создать в ней symlink `node_modules -> CODEX_PRIMARY_RUNTIME_NODE_MODULES`.
5. Запускать текущий builder именно через `CODEX_PRIMARY_RUNTIME_NODE` и импортировать `@oai/artifact-tool` из этого окружения.
6. Не подменять bundled runtime ambient Node, случайными global modules или application dependencies.

`load_workspace_dependencies` можно использовать, когда capability доступна, но её наличие не является обязательным. Отсутствие `load_workspace_dependencies` само по себе не blocker и не разрешает завершить задачу без файла.

### Bounded Recovery

После ошибки нужно прочитать точный error, проверить фактические значения всех четырёх `CODEX_PRIMARY_RUNTIME_*`, существование `CODEX_PRIMARY_RUNTIME_NODE`/`CODEX_PRIMARY_RUNTIME_NODE_MODULES`, правильность symlink и запуск требуемым Node. Затем исправляется минимальная причина и повторяется тот же builder с уже подготовленными данными. Исходные данные не получают и не сопоставляют заново; одинаковые безрезультатные retries не повторяются бесконечно.

### Разрешённый Fallback

Владелец проекта постоянно разрешает для `user-artifact` после доказанно неуспешного bounded recovery создавать новый простой XLSX следующим порядком:

1. уже установленный `openpyxl` через `CODEX_PRIMARY_RUNTIME_PYTHON`, если он задан и исполним, иначе через доступный system Python;
2. уже установленный `xlsxwriter` тем же interpreter order;
3. dependency-free валидный XLSX/`OOXML` через Python standard library (`zipfile` + XML);
4. подходящий минимальный repo-owned helper, например dependency-free финальный fallback `apps/user_artifact_xlsx.py` для новых простых таблиц.

Новые зависимости из сети только ради простого XLSX не устанавливаются. CSV нельзя сохранять с расширением `.xlsx`; fake/corrupt workbook запрещён. Identifier-поля (`nmID`, barcode, article, SKU и значения с leading zero) записываются как text. Prepared data остаётся в одном temporary/intermediate source и не теряется при переключении backend. Итог публикуется точно во внешний путь пользователя; временные builders и data не попадают в Git.

### Проверка И Completion

Перед завершением применимо проверить:

- exact output path, существование и ненулевой размер;
- XLSX как ZIP container и успешный `testzip`/zip integrity;
- обязательные OOXML members и парсинг XML;
- повторное открытие доступным независимым reader;
- ожидаемые sheet names, row/column counts, ключевые values/formulas и отсутствие лишних пустых sheets;
- text type/format идентификаторов без exponent conversion и потери leading zero;
- запрошенные filter, freeze panes, widths и иное оформление;
- визуальный render доступным способом, когда оформление существенно.

Ошибка одного renderer не уничтожает уже созданный и структурно проверенный простой XLSX. Используется следующий доступный visual path без повторного получения исходных данных. Если ни один renderer недоступен, это фиксируется как ограничение только visual phase; структурная и независимая reader-проверка всё равно выполняются.

## Default Completion И Явная Граница

Если пользователь явно не ограничил closure, Codex самостоятельно выполняет полный применимый контур.

Для `repo-only`:

`implementation → checks → semantic review → fixes/recheck → docs sync → commit → push → PR → checks/review → merge → удаление feature-ветки → fetch/prune → подтверждение результата в актуальном origin/main`

Для `user-artifact`:

`read-only source acquisition → prepared data preservation → exact file creation → structural/content/format verification → applicable visual verification`

Для `live/runtime` после всего `repo-only` closure обязательны canonical deploy, deploy-commit equality и live/service/public verify.

Для `production data mutation/backfill` выполняются применимый GitHub/runtime closure, обязательный safety-контур и human gates.

Если PR явно поставлен в repo-owned GitHub Release Train, Codex не передаёт ответственность очереди и не завершает task на метке `release:ready`. `user-artifact` этого раздела не достигает, потому что не создаёт PR. Task owner PR-backed задачи обязан:

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

Goal Mode обязан использовать канонический queue shepherd перед любым blocked handoff:

`python3 apps/github_release_train_wait.py <OWN_PR> --shepherd`

Shepherd не создаёт второй state machine: он интерпретирует machine specification из `apps/github_release_train_spec.py` и возвращает структурированные `disposition`, `own_pr`, `action_pr`, `canonical_github_state`, `reason_code`, `allowed_next_action`, `user_intervention_required`, `evidence`, `remediation_exhausted`, `current_phase`, `blocked_phase`, `safe_phases_remaining`, `required_capability`, `capability_evidence`, `next_executable_action`. Допустимые disposition: `TERMINAL_SUCCESS`, `CONTINUE_WAITING`, `CONTINUE_SAFE_PHASES`, `AWAIT_PHASE_CAPABILITY`, `OWN_ACTION`, `TAKEOVER_PREDECESSOR`, `RECOVER_OWN_CHAIN`, `EXTERNAL_BLOCKER`, `TERMINAL_FAILURE`. Опциональный `--phase-state <JSON>` передаёт текущий dependency/capability context в этот же classifier.

Неизменившееся состояние не доказывает impasse. Elapsed time, число polling-итераций или одинаковых goal-turns, отсутствие GitHub changes, чужой gate, `release:awaiting-ui`, `release:needs-resume`, слова MCP/browser/credentials/database и отсутствие embedded Browser в Codex CLI по отдельности никогда не дают `EXTERNAL_BLOCKER`/`TERMINAL_FAILURE`. При `CONTINUE_WAITING` shepherd продолжает polling/heartbeat; `--once` возвращает код `6` как bounded snapshot, после которого следующий goal-turn продолжает общий Goal. `CONTINUE_SAFE_PHASES` выполняет repository-safe dependency steps. `AWAIT_PHASE_CAPABILITY` приостанавливает только непосредственную production/UI phase и не объявляет всю цель сломанной. При `OWN_ACTION`, `TAKEOVER_PREDECESSOR` и `RECOVER_OWN_CHAIN` агент выполняет разрешённое действие сам.

Exit-code contract shepherd: `0` — proven terminal success; `2` — proven external blocker; `3` — own LOOP UI/recovery; `4` — predecessor ownership resumed/takeover next action; `5` — другое repo-owned own action; `6` — normal waiting snapshot; `7` — proven irrecoverable terminal failure; `8` — `CONTINUE_SAFE_PHASES`; `9` — `AWAIT_PHASE_CAPABILITY`; `130` — interrupt. Только `0`, `2`, `7` terminal для Goal; `8` продолжает работу, `9` — phase-local capability wait. Перед blocked handoff обязателен `--shepherd --once` с актуальным `--phase-state`; он допустим только при disposition `EXTERNAL_BLOCKER`/`TERMINAL_FAILURE`, canonical reason, конкретном evidence, перечне recovery attempts и `remediation_exhausted=true`. `EXTERNAL_BLOCKER` запрещён, пока доступна repo-owned команда или незавершённая независимая safe phase.

Новый LOOP всегда имеет `root == PR`; recovery — `root < PR` и exact proof текущего `awaiting-ui` gate; `root > PR` запрещён. Новый root может нормально ждать за чужим UI gate. Recovery-link немедленно становится stale при исчезновении gate или terminal closure root. Waiter проверяет enrollment proof до heartbeat/ack и завершает fail-closed при classification error.

Trusted comment `/wb-core loop retry-blocked <PR> head <HEAD_SHA>` сохраняет task class, scope и root и применим только к техническому pre-merge blocker; enqueue-команды такой blocker не снимают. После successful baseline на новом fix-head retry может обновить exact-head marker уже доказанной new/recovery identity, но не создать и не переклассифицировать её. Comment обрабатывает trusted-main GitHub Actions, поэтому exact-head proof не зависит от identity локального `gh` token. Локальный waiter при classification mismatch только останавливается fail-closed и не выставляет label/comment от имени пользователя. Classification error сохраняет provenance через последующие смены head и обычным retry не исправляется; его разрешает только более поздний repo-owned new/recovery/correction proof. Отдельная `/wb-core loop correct-to-new <PR> head <HEAD_SHA> old-root <ROOT>` требует open/unmerged exact PR/head, successful baseline, `OWNER`/`MEMBER` authorization, classification-blocker proof, доказанный terminal old root и отсутствие его active gate; она одним label replacement создаёт independent root и идемпотентный audit proof. Повторная доставка уже доказанной enqueue/correction команы после перехода в `running`, `awaiting-agent` или `blocked` безопасно ничего не меняет и не возвращает PR в `ready`.

Нормальное ожидание очереди не превращается в blocker после N polls или goal-turns. Если LOOP heartbeat исчез на `ready/running/awaiting-agent/awaiting-ui`, worker добавляет overlay `release:needs-resume` и точную команду `python3 apps/github_release_train_wait.py <PR> --resume-owner --no-ack-agent`. Shepherd классифицирует чужого lost owner как `TAKEOVER_PREDECESSOR`, только если одновременно доказаны overlay, machine status `owner=unowned`, exact head, для UI gate exact deployed SHA, LOOP root и неизменность root isolation. Без этих evidence takeover запрещён. Resume идемпотентно снимает только overlay и не выполняет ack или acceptance; его код `4` означает continuation, не blocker.

После takeover агент восстанавливает predecessor context из PR, status comment, semantic diff и authoritative docs; определяет точный незавершённый этап; на `release:awaiting-ui` выполняет production UI Flow; оставляет exact-SHA acceptance только после достаточного UI evidence; ждёт terminal predecessor; затем без пользовательского напоминания снова запускает shepherd исходного `OWN_PR`. Если UI выявил дефект, создаётся exact same-root recovery либо сохраняется fail-closed gate; независимый successor остаётся исправным и ожидающим. Takeover никогда сам не выполняет `ack-agent` или `accept-ui`.

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

- browser session и UI authorization не нужны для repository analysis/development/tests/PR и проверяются только в `PRODUCTION_UI_PREFLIGHT`; будущая UI acceptance не останавливает ранние фазы;
- в Codex CLI по умолчанию сразу использовать локальный Python/Node Playwright с установленным Chrome/Chromium в новом изолированном непостоянном browser context; встроенный Browser в CLI недоступен и не является preflight-попыткой;
- в ChatGPT web/desktop встроенный Browser можно использовать, если он доступен; независимо от поверхности применяется один evidence contract;
- по умолчанию не подключать пользовательский Chrome profile, `user_data_dir`, cookies, storage state или сохранённые credentials; авторизованный context допустим только при explicit scope и безопасно доступной авторизации;
- не выполнять click, form fill, keyboard input, refresh/save/submit/delete/run-now или другие business mutations, если они прямо не входят в bounded UI Flow;
- browser package/binary можно установить только когда это необходимо и разрешено текущим permission contour; отсутствие Playwright/Chrome/Chromium или требуемой авторизации не предполагается и не разрешает подменить UI Flow HTTP-probe.

Публичную/неавторизованную UI-проверку выполняют, когда она достаточна для текущего этапа. Отсутствие авторизованной session блокирует только точную navigation/operation, которой она фактически нужна, и только после evidence, а не весь development/PR flow.

В CLI фактический runtime preflight выполняется `python3 apps/github_release_train_wait.py <ACTION_PR> --playwright-preflight`: helper импортирует локальный Playwright и действительно запускает Chrome/Chromium с новым isolated non-persistent context. Успех означает немедленное продолжение UI Flow; доступность embedded Browser не проверяется и не требуется. Ошибка preflight сначала даёт repo-owned recovery action, а не blocker. `EXTERNAL_BLOCKER` возможен только после зафиксированных import/launch errors, выполненных repo-owned repair attempts, `repo_owned_action_available=false`, `remediation_exhausted=true` и доказательства, что следующий шаг требует новых пользовательских полномочий. Недоступность авторизации также подтверждается фактической navigation/auth evidence, а не предположением.

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

Для production read первые два условия требуют фактической проверки current canonical target, штатного SSH и exact store/document access. Непроверенное предположение, обязательность из старого prompt и недоступность архивного MCP недостаточны.

Финал при blocker содержит точную ошибку/ограничение, сохранённое состояние и один минимальный ручной шаг.

## Формат Итогового Ответа

Итог содержит только применимое:

1. итоговый статус;
2. что реально изменено;
3. что реально проверено;
4. что намеренно осталось вне scope;
5. blocker и один минимальный следующий шаг — только если blocker есть.
