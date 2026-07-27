# Рабочий протокол `wb-core`

Этот файл — самодостаточный entrypoint для Codex CLI, Codex в приложении ChatGPT для Mac, локального проекта Codex и обычного ChatGPT, читающего репозиторий. Доменные детали не дублируются здесь: они живут в authoritative docs.

## Источники истины

Приоритет источников:

1. Git-tracked code и актуальный `origin/main` задают code truth. Рабочая ветка — только proposed change до review и merge.
2. Authoritative docs: `README.md`, `docs/architecture/*`, `docs/modules/*`, `migration/*`.
3. GitHub задаёт факты о branch, commit, PR, checks, review и merge.
4. Production server — canonical deploy/runtime и production-data boundary. Наблюдаемое состояние читается из его current server-owned stores/documents через актуальный repo/documentation contract.
5. WebCore Data MCP — архивный read-only compatibility contour, а не штатный source/acquisition path. Его не указывают в новых task prompts и не требуют для выполнения; отсутствие никогда не образует blocker.
6. Legacy-артефакты, старые чаты, вложения и прежние project instructions — только migration evidence и do-not-lose constraints, но не current truth и не normal implementation path.

Подробности: [source-of-truth policy](docs/architecture/03_source_of_truth_policy.md), [execution protocol](docs/architecture/07_codex_execution_protocol.md), [hosted runtime contract](docs/architecture/10_hosted_runtime_deploy_contract.md), [GitHub Release Train](docs/architecture/11_github_release_train.md).

## Кураторский preflight

Перед техническим выводом, постановкой задачи, реализацией или проверкой результата другого агента изучи:

- актуальный GitHub state;
- этот `AGENTS.md`;
- только релевантные authoritative docs;
- фактический code truth, если вывод касается текущей реализации.

Если репозиторий или необходимый источник недоступен, не утверждай уверенно текущее состояние: верни точный blocker. Если меняется code, contract, runtime boundary, module status или другой зафиксированный truth, синхронизируй затронутые authoritative docs в той же задаче.

Любой предложенный в prompt инструмент, connector, сервер, runtime, storage, SSH alias, путь или запрет технического пути Codex повторно сверяет с current `AGENTS.md`, authoritative docs и code truth. Это техническая гипотеза prompt, а не пользовательское ограничение, даже если сформулирована повелительно. Исключение — только отдельное явное ограничение, которое сам пользователь зафиксировал как своё требование. Устаревший prompt, называющий MCP обязательным или запрещающий server-side read без такого пользовательского требования, не останавливает работу и не переопределяет current protocol.

ChatGPT/куратор, формирующий новый task prompt для Codex, не называет WebCore Data MCP и не hardcode-ит технический source/access path. Prompt фиксирует цель, необходимые данные, read-only/mutation boundaries, ожидаемый результат, acceptance/closure criteria и содержит правило: `Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное.`

Если задаче нужны production evidence или данные, normal path — фактический `PRODUCTION_READ_PREFLIGHT`, определение current active target/runtime/source из code и authoritative docs, штатный SSH к canonical production server, query-only чтение production stores и bounded read server-owned documents. Для SQLite обязательны `mode=ro` и `PRAGMA query_only=ON`; для других stores — эквивалентная read-only гарантия. Этот путь не разрешает deploy, service changes, upstream sync, запись в production, ad-hoc mutation или раскрытие secrets.

Production gates являются phase-local. Разделяй `REPOSITORY_PREFLIGHT`, `PRODUCTION_READ_PREFLIGHT`, `PRODUCTION_MUTATION_PREFLIGHT` и `PRODUCTION_UI_PREFLIGHT` и строй порядок по реальным зависимостям, а не по порядку пунктов prompt. Отсутствие архивного MCP, browser session, production credentials/database, manifests, digest или backup не блокирует repository analysis, implementation, fixtures, tests, docs, подготовку безопасного repo-owned runner, branch/PR, CI или review. Blocker чтения production допустим только после фактической проверки canonical server-side path и точной ошибки required access либо доказанного отсутствия необходимых данных. Перед blocked handoff выполни все независимые безопасные фазы и докажи, что недоступная capability нужна непосредственному следующему действию.

Для будущей production-data mutation runner всё равно реализуется и тестируется на fixtures/mocks до максимально возможного repo-only состояния. Канонический runner обязан иметь dry-run по умолчанию, отдельный explicit apply, bounded scope, machine-readable manifest, pre-change digest, backup/evidence contract, expected affected records, non-target invariants, idempotency либо документированный recovery, post-apply readback и reconciliation. Случайные локальные scripts, ad-hoc SQL и mutation через архивный read-only MCP запрещены.

## Discussion → отдельная Codex-задача

`discussion-only` — это initiating Chat/кураторский thread, в котором пользователь обсуждает, уточняет или утверждает план, но ещё не выбрал текущий thread как exact исполняемую Codex-задачу. После такого обсуждения фразы «запускай/запусти задачу», «передавай/отправляй в Codex», «начинай/делай/реализуй по этому плану» и смысловые эквиваленты всегда классифицируются как `DISPATCH_REQUEST`. Неоднозначность разрешается в пользу отдельной user-owned Codex task/thread: команда запуска не означает implementation в initiating discussion thread и не даёт ему authority создавать branch, менять файлы или выполнять план.

Допустимы только два явных исключения:

- пользователь отдельно требует выполнить работу прямо в текущей уже исполняемой Codex-задаче;
- пользователь явно продолжает exact non-terminal target, а его active identity подтверждена readback и continuity классифицирована как `ACTIVE_ADDITION` либо `ACTIVE_LOOP_RECOVERY`.

Первое исключение действует только когда current thread сам является доказанной исполняемой target task. Во втором случае initiating discussion thread отправляет bounded follow-up в подтверждённый existing target, а не реализует задачу сам. Ни одно исключение не разрешает `discussion-only` implementation.

При `DISPATCH_REQUEST` initiating thread останавливается на curator/dispatcher boundary, формирует полный task prompt и использует supported user-owned task/thread creation capability (`create_thread` или актуальный эквивалент). `spawn_agent`, subagent, internal multi-agent delegation, fork без отдельной user-owned task identity и реализация в initiating thread не являются dispatch.

Dispatch и monitoring образуют одну неоткладываемую `launch operation` в том же turn:

1. До create/update прочитать existing heartbeat automations initiating thread и все их exact target identities.
2. `TARGET_CREATE_READBACK`: создать отдельный target, получить exact target ID и bounded `wait_threads(timeoutMs: 0)` snapshot, подтверждающий созданную task, доставку prompt и фактический target status.
3. `MONITOR_ATTACH_READBACK`: сразу применить contract из `Thread heartbeat automation` для того же exact target — создать либо update/reuse единственный external reporter, затем readback-проверить automation и первый status report.
4. Только после обоих этапов сообщить результат запуска с exact target identity и доказанным состоянием monitoring; successful create/automation call или UI-card без readback не являются подтверждением.

Если user-owned target creation недоступен, exact ID не получен либо `TARGET_CREATE_READBACK` не подтверждает доставку, dispatch fail closed: initiating discussion thread не начинает implementation и возвращает точный blocker. Если target уже подтверждён, но recurring monitoring capability после bounded проверки действительно недоступна, target execution продолжается в отдельной задаче; инициатор явно сообщает `MONITORING_CAPABILITY_LIMITATION` и не выдаёт silent self recovery heartbeat за reporting monitor. Недоступность creation и недоступность monitoring — разные outcomes и не подменяют друг друга.

## Классы задач

Каждый новый task prompt по возможности начинается ровно с одной канонической строки:

- `КЛАСС ЗАДАЧИ: СТАНДАРТ`
- `КЛАСС ЗАДАЧИ: LOOP`
- `КЛАСС ЗАДАЧИ: ДИАГНОСТИКА`

Класс управляет orchestration/closure, а execution-контур определяет техническую область и риск. PR-backed `СТАНДАРТ` может иметь `scope:repo-only`, `scope:live-runtime` или `scope:production-mutation`; `LOOP` всегда имеет `scope:live-runtime`. `user-artifact` — отдельный non-PR контур класса `СТАНДАРТ`, поэтому label `scope:user-artifact` не создаётся.

Если явная строка отсутствует, Codex самостоятельно классифицирует задачу до начала работы:

- создание или изменение запрошенного пользовательского файла вне репозитория — `стандарт` с execution-контуром `user-artifact`; запись файла не является `ДИАГНОСТИКОЙ`;
- исключительно read-only анализ без изменений code, GitHub state и production — `диагностика`;
- deploy с последующими production UI Flow, Playwright-проверками и итерациями до live-результата — `loop`;
- обычная реализация, repo-only изменение или неоднозначный случай — `стандарт`.

Если выбор остаётся неоднозначным, Codex всегда использует `стандарт`; отсутствие строки больше не требует останавливать работу и запрашивать класс. В начале автоматически классифицированной задачи Codex сообщает `Класс задачи: стандарт — определён автоматически`, `Класс задачи: loop — определён автоматически` или `Класс задачи: диагностика — определён автоматически` и кратко называет основание. Класс не расширяет requested scope или authority для mutations.

Task class и task continuity — два разных решения. Class выбирает `STANDARD`/`LOOP`/`DIAGNOSTIC`, а continuity выбирает одну из машинных категорий `NEW_TASK`, `ACTIVE_ADDITION`, `ACTIVE_LOOP_RECOVERY`, `TERMINAL_STALE_REFERENCE`.

- Только явное продолжение незавершённой активной задачи может наследовать её branch, PR и identity.
- `release:done`, `release:production` и `release:superseded` — terminal boundary: после неё нельзя наследовать branch, PR, task identity, LOOP root, acknowledgement, heartbeat или recovery identity.
- Новый дефект после terminal closure всегда получает новую задачу и новый PR, даже в том же чате, на том же экране или в том же функциональном разделе.
- Формулировки «новая задача», «отдельная задача», «самостоятельная задача» и «новый LOOP» всегда означают `NEW_TASK`.
- Одинаковый чат или функциональная область сами по себе ничего не доказывают. При любой неоднозначности выбирай независимую `NEW_TASK`.
- Task class `LOOP` сам по себе не означает recovery. `ACTIVE_LOOP_RECOVERY` допустим только для дефекта, найденного во время текущего незавершённого `release:awaiting-ui`.

### `ДИАГНОСТИКА`

Строго `read-only`: разрешены чтение code/docs, GitHub state, логов и production evidence; запрещены изменения файлов, branch, commit, PR, labels, merge, deploy и production mutations. Итог содержит подтверждённый диагноз, доказательства и варианты решения. Найденное исправление выполняется только отдельной задачей `СТАНДАРТ` или `LOOP`.

### `СТАНДАРТ`

Для repo-changing и runtime-задач — полный применимый closure в отдельной branch/worktree и PR. Codex добавляет `task:standard`, ровно одну `scope:*` label и после pre-release proof — `release:ready`. Release Train владеет `sync/checks/merge/deploy/verify`; Codex наблюдает очередь и не завершает сессию на открытом PR. `repo-only` завершается только на `release:done`, `live-runtime` — на `release:production`; human-gated `production-mutation` завершается на `release:production` только после Actions-owned exact-evidence terminalization ниже. Чужой exclusive gate — нормальное ожидание, а не blocker; `release:blocked` или `release:halted` требуют bounded диагностики/исправления либо точного внешнего blocker. Handoff содержит PR, merge SHA и проверки. Единственное исключение — `user-artifact`, описанный ниже: он остаётся `СТАНДАРТ`, но не создаёт GitHub-задачу.

### `LOOP`

Итерация с production UI Flow; пользователь обычно запускает её через `/goal`, но при неактивном формальном Goal Mode Loop-протокол всё равно обязателен. Используются отдельная branch/worktree и PR с `task:loop + scope:live-runtime`. `loop:root-*` и `release:ready` для LOOP вручную не назначаются.

Новый самостоятельный LOOP после successful `baseline` регистрируется exact command `/wb-core loop enqueue-new <PR> head <HEAD_SHA>`: repo-owned handler создаёт `loop:root-<собственный PR>`, new-root proof и атомарно ставит `release:ready`. Recovery регистрируется отдельной командой `/wb-core loop enqueue-recovery <PR> head <HEAD_SHA> gate <ACTIVE_GATE_PR> root <ROOT>` и допускается только при active `release:awaiting-ui` exact root. Root меньше номера PR означает recovery, root равен номеру PR — новую цепочку, root больше номера PR запрещён. Чужой active gate для нового root — normal waiting, не blocker.

Перед каждым LOOP merge/deploy Release Train после sync и baseline ставит `release:awaiting-agent`. Активная Codex-сессия подтверждает readiness GitHub-native acknowledgement, привязанным к номеру PR и exact head SHA. Ack одноразовый, потребляется непосредственно перед merge, становится недействительным после изменения head и обязателен заново для каждого recovery PR. Пока ack нет, production и остальная очередь не меняются. Просроченное ожидание получает overlay `release:needs-resume` и точную команду восстановления, но никакого автоматического ack или пропуска очереди.

После deploy задача переходит в `release:awaiting-ui`, блокируя несвязанные production releases. Codex продолжает ту же сессию и выполняет UI Flow. При ошибке создаётся recovery PR с теми же `task:loop + scope:live-runtime`, затем repo-owned recovery registration связывает его с exact gate/root; после нового ack и deploy gate переносится на recovery. После успешного UI Flow Codex оставляет `/wb-core loop accept-ui <PR> deployed <MERGE_SHA> evidence sha256:<EVIDENCE_HASH>`. Acceptance требует repo-owned deployed-SHA proof и допустим только для последнего задеплоенного PR. Только он получает `release:production`; предыдущие merged iterations теряют stale release states, а доказанно заменённые unmerged recovery PR получают `release:superseded` и закрываются. После terminal acceptance этот root нельзя возобновить.

Чужие `release:ready`, `release:running`, `release:awaiting-agent`, `release:awaiting-ui` и занятый global production gate всегда являются normal waiting, а не blocker. Elapsed time, число polls/goal-turns и неизменность GitHub state эту классификацию не меняют. При живом owner продолжай waiter/heartbeat; если bounded goal-turn заканчивается, следующий turn продолжает тот же Goal, не завершая общую задачу.

Потерянный LOOP owner на `ready/running/awaiting-agent/awaiting-ui` получает overlay `release:needs-resume`. Это не blocker, а takeover-path: после подтверждения machine status `owner=unowned`, exact head, deployed SHA/root и сохранения LOOP-root isolation новый владелец запускает `python3 apps/github_release_train_wait.py <PREDECESSOR_PR> --resume-owner --no-ack-agent`. Resume идемпотентен и сам не выполняет ack-agent или accept-ui. Код `4` означает ownership resumed/next action required, а не terminal handoff. Затем агент восстанавливает контекст predecessor из PR/status/diff/docs, завершает его точный этап, выполняет локальный UI Flow при `awaiting-ui`, принимает только exact deployed SHA с достаточным evidence, ждёт terminal predecessor и без напоминания возвращается к shepherd собственного PR. Без `release:needs-resume` и доказанного lost-owner takeover запрещён.

## Thread heartbeat automation

На поверхности с фактически доступной thread-heartbeat capability для каждой новой нетерминальной задачи, создаваемой либо получаемой в интерфейсе, обязательна ровно одна active monitoring role для exact target task/thread identity. `external supervisor reporter` — preferred Chat → Codex role: независимый monitor с destination/attachment в инициирующем Chat/thread и bounded exact target list в durable prompt; он публикует отчёты, пока target turn active. `self recovery heartbeat` (legacy name `self-heartbeat`) прикреплён к самой target thread, только возобновляет idle target и не является reporter. Если пользователь просит «мониторинг», periodic reports каждые N минут, процент/ETA или наблюдение в исходном чате, только `external supervisor reporter` удовлетворяет запросу; silent `self recovery heartbeat` не удовлетворяет reporting intent и не может называться «мониторинг работает» без явного предупреждения об отсутствии periodic reports. Capability определяется callable contract, а не названием macOS/Desktop/CLI/IDE/пути. Если capability недоступна, задача продолжается без blocker и без ложного monitor/reporting claim.

Для `DISPATCH_REQUEST` этот блок исполняется как `MONITOR_ATTACH_READBACK` той же `launch operation`, немедленно после `TARGET_CREATE_READBACK`; attachment нельзя переносить на следующий turn, первый PR, инициативу target или напоминание пользователя.

Для Chat → Codex до create/update изучи existing heartbeat automations initiating thread и все exact monitored target identities. Если reporter отсутствует — создай preferred external reporter. Если initiating thread уже имеет active external reporter для другой non-terminal задачи, update/reuse эту единственную automation как multi-target supervisor с bounded exact target list, сохранив все прежние non-terminal targets; silent self fallback и duplicate reporter запрещены. Destination остаётся initiating reporting thread, а monitored targets живут в durable prompt/target list: destination thread не является target task. Если multi-target update фактически не поддерживается, честно сообщи capability limitation; допустимый mutually-exclusive `self recovery heartbeat` остаётся только silent recovery safeguard и не удовлетворяет reporting intent.

Create/update выполняется только поддерживаемым automation tool без hardcoded schedule syntax. Completion требует readback: `ACTIVE`; cadence 10 минут; правильный initiating destination/monitor thread; роль reporter и каждый exact target ID в durable prompt; прежние non-terminal targets сохранены; для тех же targets нет self heartbeat/duplicate; immediate bounded `wait_threads(timeoutMs: 0)` smoke даёт первый доказанный status report в initiating chat. UI-card или successful create-call без этих проверок не completion. Supervisor при active target только читает доказанный progress и не отправляет follow-up; при idle/non-terminal отправляет ровно один bounded follow-up; при human-only boundary сообщает exact blocker.

Каждый осмысленный supervisor run публикует отдельную подписанную строку на каждый наблюдаемый target: `[<target name/ID>] Прогресс ≈<процент>% · ETA ≈<диапазон> · сделано: <одна короткая фраза>.`; target names/IDs и progress weights должны быть однозначны, при внешнем ожидании используется `ETA ≈зависит от <точная внешняя зависимость>`, а progress без evidence не начисляется. Terminal target удаляется из target list после доказанного terminal result; automation останавливается/удаляется только когда active targets не осталось либо получен explicit user stop. Для PR-backed `wb-core` durable state остаётся в GitHub, а monitor не создаёт второй state machine, не заменяет 5-минутное GitHub observation и не обходит `release:needs-resume`/ownership/continuity. Для локальных файлов компьютер и Desktop должны быть запущены, а проект — оставаться доступным.

## Production UI-проверки

HTTP `200`, успешный `curl`, наличие HTML и HTTP-only public probe не являются полноценной UI-проверкой. В Codex CLI сразу используй Playwright с новым изолированным непостоянным context локального Chrome/Chromium; встроенный Browser в CLI недоступен, поэтому не трать попытки на его запуск. В ChatGPT web/desktop встроенный Browser допустим, если доступен, при том же evidence contract.

UI evidence обязано включать requested/final URL и document response/redirect chain, отсутствие `5xx`, `DOMContentLoaded` и фактический видимый render, непустые title/body, отсутствие `pageerror` и явной fatal-error surface, существенные console errors и визуально проверенный screenshot. По умолчанию не используй пользовательский browser profile, cookies или credentials; не выполняй клики, ввод и business mutations вне explicit scope. Для LOOP отправляй `accept-ui` только после успешной проверки. Если UI Flow не проходит, сохраняй `release:awaiting-ui` fail-closed. Browser blocker допустим только после фактического `--playwright-preflight`, ошибки import/launch изолированного context, исчерпания repo-owned восстановления и evidence, что следующий шаг требует новых полномочий пользователя; недоступность embedded Browser в CLI и неподтверждённое предположение об авторизации evidence не являются. Полный контракт и проверенный пример PR #616: [execution protocol](docs/architecture/07_codex_execution_protocol.md) и [GitHub Release Train](docs/architecture/11_github_release_train.md).

## GOAL mode

Change-задачу формулируй через проверяемый конечный результат. Зафиксируй цель, ожидаемый проверяемый итог, bounded scope, существенные ограничения, acceptance criteria, closure criteria и применимый execution-контур. Для data/artifact-задачи опиши необходимые данные и read-only границу, но не назначай MCP, сервер, connector или storage: technical path выбирает Codex после current preflight. Routine-шаги из этого файла и authoritative docs не нужно копировать в каждый prompt.

Для `LOOP` предпочитай `/goal`; отсутствие формального Goal Mode не отменяет pre-deploy handshake, UI gate, recovery cycle и terminal closure.

## Execution-контуры

- `read-only`: анализ, диагностика или review без code, GitHub и production mutations. Итог — подтверждённый анализ либо точный внешний blocker.
- `user-artifact`: единственная mutation — новый или изменённый пользовательский XLSX/CSV/DOCX/PDF/TXT либо аналогичный файл вне репозитория. Разрешены чтение источников, временные файлы вне repo и точный итоговый файл; branch, worktree, commit, PR, labels/comments, Release Train, repo files, production и business data не меняются. Фактическое изменение Git-tracked инструкций или helper про artifacts остаётся обычным `repo-only`, а не этим исключением.
- `repo-only`: code/docs change без live/runtime эффекта. По умолчанию включает implementation, targeted checks, semantic review, fixes/recheck, docs sync и полный GitHub closure. Deploy не применяется.
- `live/runtime`: изменение public route, service/process, operator UI, runtime behavior, deploy wiring или другого live-контура. Включает полный GitHub closure, canonical repo-owned deploy и live/service/public verify.
- `production data mutation/backfill`: только с explicit bounded scope, read-only preflight, dry-run/plan, verified backup или доказанной reversibility, idempotency/resumability, audit, необходимыми human gates, canonical repo-owned runner, post-run reconciliation и non-target invariants. `release:ready` всегда останавливается до merge. Уже human-gated, merged, deployed и reconciled `task:standard + scope:production-mutation` terminalize-ится только trusted-main command `/wb-core production-mutation complete <PR> head <HEAD_SHA> merge <MERGE_SHA> deployed <DEPLOYED_SHA> gate <GATE_COMMENT_ID> gate-digest sha256:<GATE_COMMENT_HASH> reconciliation <RECONCILIATION_COMMENT_ID> reconciliation-digest sha256:<RECONCILIATION_COMMENT_HASH> evidence sha256:<EVIDENCE_HASH>`. GitHub Actions без production secrets сначала проверяет `OWNER`/`MEMBER`, exact PR/head/merge, successful exact-head `baseline`, pre-merge gate и post-merge reconciliation comments/digests; затем production-environment job read-only доказывает canonical deployed SHA/ancestor relation и только после этого создаёт Actions-owned terminal proof, снимает stale active/failure labels и ставит `release:production`. Повтор идемпотентен; ручной label/marker, stale SHA/digest, missing gate/baseline/deploy/reconciliation/evidence или wrong task/scope fail closed. Ad-hoc SQL, произвольные SSH-команды, server-only scripts и обход safety gates запрещены.
- `archived GAS guard`: только явно заданное bounded изменение archive guard. Оно не восстанавливает Google Sheets/GAS как current runtime; обязательны targeted checks, bounded publish и guard verify.

Production changes доставляются только repo-owned deploy/runbook path. Запрещены server-only drift, секреты в Git/docs/logs/PR и production mutations вне safety-контура.

Для нового XLSX основной путь — активный Spreadsheets skill и `@oai/artifact-tool`: builder запускается через `CODEX_PRIMARY_RUNTIME_NODE` во временной директории вне repo с `node_modules -> CODEX_PRIMARY_RUNTIME_NODE_MODULES`. Отсутствие `load_workspace_dependencies` само по себе не blocker. После bounded recovery допустимы уже установленные `openpyxl`, затем `xlsxwriter`, затем dependency-free ZIP/XML `OOXML`; данные между попытками не собираются заново, новые зависимости из сети не устанавливаются. Подробный recovery/verification contract: [execution protocol](docs/architecture/07_codex_execution_protocol.md).

## Выполнение и closure

Если пользователь явно не ограничил closure, Codex самостоятельно ведёт задачу до полного применимого результата:

- `user-artifact`: фактическое создание файла по точному пути → структурная/содержательная проверка → применимая визуальная проверка; подготовленные данные, свободный путь, CSV вместо запрошенного XLSX или описание будущих действий completion не являются;
- `repo-only`: implementation → checks → semantic review → fixes/recheck → docs sync → commit → push → PR → checks/review → merge → удаление feature-ветки → подтверждение результата в актуальном `origin/main`;
- `live/runtime`: весь `repo-only` closure → canonical deploy → live/service/public verify;
- `production data mutation/backfill`: применимый GitHub/runtime closure плюс весь обязательный safety-контур и human gates.

Явная граница пользователя имеет приоритет: например, «только ветка», «до commit», «до draft PR», «без merge», «без deploy» или «без production mutations». Остановись ровно на ней; отсутствие следующих стадий тогда не является ошибкой. Без такой границы не считай завершением план, гипотезу, незакоммиченный diff, только локальные проверки или открытый PR.

Перед изменениями проверь status/branch/remotes/auth, выполни `git fetch --prune origin` и создай отдельную ветку от актуального `origin/main`. Не смешивай, не очищай и не теряй чужой dirty state; при необходимости используй отдельный worktree.

Независимые change-задачи могут выполняться параллельно только в отдельных branch/worktree и отдельных PR. Для PR, явно поставленного в GitHub Release Train меткой `release:ready`, task owner добавляет ровно одну `task:*` и ровно одну `scope:*` метку и продолжает наблюдать применимый terminal state. Queue владеет только сериализованной секцией sync/checks/merge/deploy/verify; semantic conflict возвращается исходной задаче. `release:ready`, `release:awaiting-agent`, `release:needs-resume`, `release:awaiting-ui` и открытый PR не являются closure. `release:superseded` хранит аудит заменённой незамёрженной LOOP-итерации и не является активной queue-задачей. `scope:production-mutation` автоматически не выпускается; после полного human-gated apply/reconciliation доступная exact Actions-owned terminalization является `OWN_ACTION`, а отсутствие старого unsupported `complete-standard` contour не является global blocker. `AWAIT_PHASE_CAPABILITY` остаётся phase-local и не является terminal failure.

[Канонический монитор исполняемых/ожидающих PR](https://github.com/orenvlad-ai/wb-core/pulls?q=is%3Apr+-label%3Arelease%3Asuperseded+label%3A%22release%3Aready%2Crelease%3Arunning%2Crelease%3Aawaiting-agent%2Crelease%3Aawaiting-ui%2Crelease%3Aneeds-resume%2Crelease%3Ablocked%2Crelease%3Ahalted%22+sort%3Acreated-asc) не ограничивается `is:open`: merged LOOP PR с `release:awaiting-ui` остаётся активным глобальным gate. Монитор исключает `release:superseded`, не включает terminal `release:production`/`release:done` и сортирует задачи по `created-asc`.

Repo-owned waiter для Codex CLI:

`python3 apps/github_release_train_wait.py <PR>`

Канонический Goal/shepherd:

`python3 apps/github_release_train_wait.py <OWN_PR> --shepherd`

Он использует ту же Release Train machine specification, читает own PR и global gate, поддерживает heartbeat/status comment и выводит JSON с `disposition`, `own_pr`, `action_pr`, `canonical_github_state`, `reason_code`, `allowed_next_action`, `user_intervention_required`, `evidence`, `remediation_exhausted`, `current_phase`, `blocked_phase`, `safe_phases_remaining`, `required_capability`, `capability_evidence`, `next_executable_action`. Disposition: `TERMINAL_SUCCESS`, `CONTINUE_WAITING`, `CONTINUE_SAFE_PHASES`, `AWAIT_PHASE_CAPABILITY`, `OWN_ACTION`, `TAKEOVER_PREDECESSOR`, `RECOVER_OWN_CHAIN`, `EXTERNAL_BLOCKER`, `TERMINAL_FAILURE`. `--phase-state <JSON>` добавляет phase/capability context в тот же shepherd; это не отдельный state machine. `--once` возвращает bounded snapshot, но не превращает waiting в terminal event. Коды shepherd: `0` terminal success; `2` доказанный `EXTERNAL_BLOCKER`; `3` own LOOP UI/recovery; `4` predecessor takeover/resume next action; `5` другое repo-owned own action; `6` одно наблюдение normal waiting; `7` доказанный `TERMINAL_FAILURE`; `8` `CONTINUE_SAFE_PHASES`; `9` `AWAIT_PHASE_CAPABILITY`; `130` interrupt. Только `0`, `2` и `7` terminal для Goal; phase-local `9`, elapsed/status timeout и неизменность состояния никогда не являются terminal failure всей цели.

Перед любым handoff со словом `blocked` агент обязан вызвать `python3 apps/github_release_train_wait.py <OWN_PR> --shepherd --once`, при необходимости с `--phase-state`. Handoff допустим только при `EXTERNAL_BLOCKER` или `TERMINAL_FAILURE` и обязан приложить canonical reason code, конкретное evidence, выполненные recovery actions, `remediation_exhausted=true` и минимальное действие, доступное только пользователю. При `CONTINUE_WAITING`, `CONTINUE_SAFE_PHASES`, `AWAIT_PHASE_CAPABILITY`, `OWN_ACTION`, `TAKEOVER_PREDECESSOR` или `RECOVER_OWN_CHAIN` общий blocked handoff является нарушением протокола: `AWAIT_PHASE_CAPABILITY` означает только доказанное ожидание capability на непосредственной фазе. `EXTERNAL_BLOCKER` запрещён при любой доступной repo-owned команде или незавершённой безопасной фазе. Для UI runtime preflight используй `python3 apps/github_release_train_wait.py <ACTION_PR> --playwright-preflight`; успешный локальный Playwright продолжает UI Flow независимо от embedded Browser, а отсутствие browser/auth до UI-фазы не влияет на разработку и PR.

Release states, continuity и transitions определены машинно в `apps/github_release_train_spec.py`: active — `ready/running/awaiting-agent/awaiting-ui/needs-resume/blocked/halted`, terminal — `done/production/superseded`. Кроме временной пары `ready+running`, два primary states запрещены. Ручной label edit не доказывает LOOP root, recovery, ack, deploy, acceptance, halted recovery или production-mutation completion: критический transition требует repo-owned exact PR/head/gate/merge/deployed/evidence command и Actions-owned marker.

После исправления own технического pre-merge blocker LOOP PR возвращается из `release:blocked` в `release:ready` только через trusted comment `/wb-core loop retry-blocked <PR> head <HEAD_SHA>` после successful baseline. Underlying runner `retry-blocked --pr <PR> --expected-head-sha <HEAD_SHA>` выполняет GitHub Actions, чтобы exact-head proof имел repo-owned provenance; локальным user token его не запускают. Command не меняет task class, scope или LOOP root. Enqueue-команды технический blocker не снимают. Если fix изменил head LOOP PR, retry может обновить только exact-head proof уже доказанной identity; создать или переклассифицировать identity он не может. Локальный waiter при classification mismatch только останавливается fail-closed и не выставляет labels/comments. Classification blocker сохраняет свой тип через последующие смены head, обычным retry не лечится и снимается только последующим repo-owned identity proof. Ошибочная stale-terminal recovery identity исправляется только отдельной evidence-bound `/wb-core loop correct-to-new <PR> head <HEAD_SHA> old-root <ROOT>`; вручную root не переназначается. Отложенный повтор уже доказанной enqueue/correction команды на `ready/running/awaiting-agent/blocked` является no-op и не откатывает state.

## Независимая проверка

Отчёт Codex или другого агента не является доказательством сам по себе. Перед подтверждением результата проверь применимое:

- фактический GitHub state, commit и branch;
- semantic diff, а не только список файлов;
- targeted tests/checks и исправление review findings;
- отсутствие unresolved review threads;
- согласованность с authoritative docs;
- для live/runtime — deploy commit и live/service/public результат;
- для production mutation — dry-run, backup/reversibility, audit, reconciliation и non-target invariants.

Пользователь нужен только для действительно human-only действия: login, отсутствующего permission/approval, ненадёжно автоматизируемого manual UI-check, materially different risk decision, production mutation approval или недоступного внешнего источника. Доступные Git, GitHub, test, review, merge и deploy действия в scope выполняет Codex.

## Итоговый ответ

Сообщай только применимое:

1. итоговый статус;
2. что реально изменено;
3. что реально проверено;
4. что намеренно осталось вне scope;
5. blocker и один минимальный следующий шаг — только если blocker есть.
