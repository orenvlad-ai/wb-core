# Рабочий протокол `wb-core`

Этот файл — самодостаточный operational entrypoint для Codex и ChatGPT,
работающих с репозиторием. Доменная архитектура живёт в
`docs/architecture/*`, `docs/modules/*` и `migration/*`; здесь находится ровно
один действующий execution flow.

## Действующий flow

Обычная change-задача проходит один последовательный контур:

1. Новый куратор без напоминания задаёт себе короткое полностью видимое имя
   `WBC · <короткая тема> · К<n>` и закрепляет задачу.
2. Пользователь обсуждает с куратором цель, bounded scope, acceptance и closure.
3. После согласования куратор создаёт ровно одного прямого user-owned
   исполнителя, задаёт ему связанное имя
   `WBC · <та же короткая тема> · И<n>` и закрепляет задачу. Куратор не
   реализует change сам и не заменяет исполнителя subagent-ом. Executor prompt
   завершается обязательным указанием самостоятельно дойти до применимого
   terminal state и вернуть в исходную кураторскую задачу один финальный
   technical handoff после `COMPLETE` либо доказанного `BLOCKED`.
4. Исполнитель работает в отдельной branch/worktree от актуального
   `origin/main`, обновляет только необходимые code/docs/tests и выполняет
   targeted checks и semantic self-review.
5. Исполнитель открывает один open non-draft PR в `main` из same-repository
   branch, ставит `task:standard` и ровно одну label:
   `scope:repo-only`, `scope:live-runtime` или, только для фактического apply,
   `scope:production-mutation`.
6. После successful required check `baseline` на current exact head исполнитель
   добавляет `release:ready`. До этого label не ставится.
7. GitHub Release Train повторно проверяет current head, labels, baseline,
   mergeability и safety gates, при необходимости синхронизирует branch с
   current `main`, запускает fresh baseline и сериализует merge и применимый
   exact-SHA deploy/verify.
8. `scope:repo-only` завершается только на `release:done`;
   `scope:live-runtime` — только на `release:production` после canonical
   deploy/verify. `scope:production-mutation` использует отдельный human-gated
   terminalization contract и автоматически не выпускается.
9. Исполнитель передаёт куратору один финальный technical handoff. Куратор без
   повторной технической проверки тезисно пересказывает его владельцу.
   Техническое завершение, merge и release label не являются owner acceptance:
   только владелец пишет `Задача принята` и вручную открепляет задачи.

Ветви, PR и release labels других задач не изменяй. Чужая активная release
операция — штатное ожидание; она не разрешает снимать labels, обходить очередь
или вмешиваться в live release.

## Quiet curator после dispatch

Каждый executor task prompt заканчивается обязательным указанием со следующей
не сокращаемой семантикой:

`Исполнитель самостоятельно доводит задачу до применимого terminal state. После COMPLETE либо доказанного BLOCKED отправь в исходную кураторскую задачу один финальный technical handoff: итоговый статус; что сделано; что не сделано или осталось вне scope; PR и final SHA; проверки; merge/release/deploy/production state; сложности, риски и blockers.`

После успешного dispatch куратор немедленно завершает свой текущий turn.
`Ждёт` означает quiet wait: отсутствие активных model/tool calls, а не
`wait`/poll loop. До пробуждения куратор не инициирует wait/read/list/status
опросы исполнителя, GitHub/CI/runtime/production audit его работы, follow-up
prompts, промежуточные сводки, параллельную реализацию, независимую перепроверку
handoff, heartbeat, automation или любой другой мониторинговый контур.

Куратора пробуждает только один из трёх сигналов: финальный handoff исполнителя,
доказанное strict human-only обращение исполнителя либо новое явное указание
владельца. Обычный progress исполнителя не является сигналом. Вся техническая
проверка, evidence и terminal closure до handoff принадлежат исполнителю. После
финального handoff куратор только тезисно сообщает владельцу статус, сделанное,
не сделанное или исключённое, выполненные проверки и достигнутый
production/terminal state, а также сложности, риски или blocker; второй
технический audit он не выполняет.

## Выключенная legacy-оркестрация

`WB_CORE_ORCHESTRATION_REQUIRED=false`. Global Watcher, external orchestration
registry, Task Passport, acceptance envelope, curator workspace automation,
logical release lane, orchestration admission, shepherd/takeover, persistent
arbiter и обязательные heartbeat/chat callback механизмы выведены из active
flow.

Не запускай, не регистрируй и не восстанавливай эти механизмы; не создавай им
замену в виде scheduler, reviewer, reporter, arbiter или control plane. Они не
нужны для dispatch, PR eligibility, `release:ready`, merge, deploy, closure или
owner acceptance. Исторический contract доступен только через
[`docs/architecture/12_codex_global_orchestration.md`](docs/architecture/12_codex_global_orchestration.md).
Retained compatibility code и historical GitHub labels не являются agent
instructions и не разрешают начинать новый legacy-контур.

GitHub Release Train core, Finance/storage safety, exact-SHA deploy/verify,
production-mutation gates и manual owner acceptance остаются действующими без
ослабления.

## Видимый lifecycle ролей

Единственный подробный contract имён, нумерации, pin/unpin и owner acceptance
находится в разделе [«Видимый жизненный цикл Codex-задач»](docs/architecture/07_codex_execution_protocol.md#видимый-жизненный-цикл-codex-задач).

Короткие правила:

- exact topic у куратора и исполнителя одной цепочки совпадает;
- счётчики `К<n>` и `И<n>` независимы и не переиспользуются;
- title и pin назначаются один раз при получении роли без напоминания владельца;
- агент не закрепляет повторно вручную откреплённую владельцем задачу;
- агент не синтезирует `Задача принята`, не открепляет и не архивирует текущие
  задачи автоматически;
- project/bootstrap instructions только направляют к этому файлу и execution
  protocol, но не дублируют lifecycle как второй source of truth.

## Источники истины и preflight

Приоритет источников:

1. Git-tracked code и актуальный `origin/main` задают code truth. Рабочая ветка
   остаётся proposed change до review и merge.
2. `README.md`, `docs/architecture/*`, `docs/modules/*`, `migration/*` задают
   authoritative documentation truth.
3. GitHub задаёт branch, commit, PR, checks, review, merge и release truth.
4. Production server и его current server-owned stores/documents задают
   canonical deploy/runtime и production-data boundary.
5. WebCore Data MCP — архивный read-only compatibility contour, не normal
   source/acquisition path и не обязательная capability.
6. Legacy artifacts, старые чаты, вложения и прежние project instructions —
   только migration evidence и do-not-lose constraints.

Перед техническим выводом, постановкой задачи, реализацией или проверкой
результата другого агента изучи:

- актуальный GitHub state;
- этот `AGENTS.md`;
- только релевантные authoritative docs;
- фактический code truth, если вывод касается реализации.

Перед изменениями проверь status/branch/remotes/auth, выполни
`git fetch --prune origin`, создай отдельную branch/worktree от актуального
`origin/main` и проверь открытые PR. Не смешивай, не очищай и не теряй чужой
dirty state.

Если меняется code, contract, runtime boundary, module status или другой
зафиксированный truth, синхронизируй затронутые authoritative docs в той же
задаче.

## Prompt и technical path

Любой connector, server, runtime, storage, SSH alias, путь или технический
запрет из prompt повторно проверяется по current `AGENTS.md`, authoritative docs
и code truth. Это гипотеза автора prompt, если пользователь отдельно и явно не
зафиксировал её как своё ограничение.

Новый task prompt не называет WebCore Data MCP и не hardcode-ит access path. Он
фиксирует цель, необходимые данные, read-only/mutation boundaries, ожидаемый
результат и acceptance/closure criteria и содержит правило:

`Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное.`

Если нужны production evidence/data, используй canonical server-side path:
сначала определи current target/runtime и конкретный source по code/docs, затем
выполни фактический
`PRODUCTION_READ_PREFLIGHT`: штатный SSH к canonical server, query-only чтение
server-owned stores (`mode=ro` и `PRAGMA query_only=ON` для SQLite либо
эквивалент) и bounded read server-owned documents. Этот read path не разрешает
deploy, service changes, upstream sync, production writes, ad-hoc mutation или
раскрытие secrets. Недоступность архивного MCP не является blocker.

## Режимы и execution-контуры

Пользователь не выбирает служебный класс специальной строкой. Codex определяет
его по результату:

- исключительно read-only анализ — `ДИАГНОСТИКА`, без files/GitHub/production
  mutations;
- обычная реализация, live change или неоднозначный случай — `СТАНДАРТ` и
  действующий flow выше;
- новый legacy `LOOP` из текущего protocol не запускается. Сохранившиеся LOOP
  states/handlers относятся к compatibility и product fail-closed behavior, а
  не к обязательному callback или отдельной orchestration-сессии.

Execution contours:

- `read-only` — подтверждённый анализ без mutations;
- `user-artifact` — единственная mutation: запрошенный XLSX/CSV/DOCX/PDF/TXT
  вне репозитория. Такая запись не является `ДИАГНОСТИКОЙ`; branch, worktree,
  Release Train и label `scope:user-artifact` не создаются;
- `repo-only` — code/docs/tests change без live/runtime эффекта;
- `live/runtime` — public route, service/process, operator UI, runtime behavior
  или deploy wiring, с canonical deploy и verify;
- `production data mutation/backfill` — только PR/runner, который фактически
  выполняет bounded apply;
- `archived GAS guard` — только явно заданный bounded archive guard scope.

Для нового XLSX основной путь — active Spreadsheets skill и
`@oai/artifact-tool`. Runtime discovery предоставляет
`CODEX_PRIMARY_RUNTIME_ROOT`, `CODEX_PRIMARY_RUNTIME_NODE`,
`CODEX_PRIMARY_RUNTIME_NODE_MODULES` и `CODEX_PRIMARY_RUNTIME_PYTHON`.
Отсутствие `load_workspace_dependencies` само по себе не blocker. После bounded
recovery допустимы уже установленные `openpyxl`, затем `xlsxwriter`, затем
dependency-free ZIP/XML `OOXML`; новые зависимости из сети не устанавливаются.

## Phase-local production safety

Production gates не являются одним глобальным барьером:

1. `REPOSITORY_PREFLIGHT` покрывает repo/worktree/docs/code/tests/GitHub.
2. Repository implementation, fixtures/mocks, review, PR и CI выполняются до
   максимально возможного безопасного состояния.
3. `PRODUCTION_READ_PREFLIGHT` выполняется только перед необходимым read-only
   evidence.
4. `PRODUCTION_MUTATION_PREFLIGHT` выполняется непосредственно перед apply.
5. `PRODUCTION_UI_PREFLIGHT` выполняется только перед production UI verify.

Отсутствие будущих credentials/database/browser/manifests/digest/backup не
блокирует независимые repository phases. Blocker допустим только на
непосредственной boundary после фактического preflight, исчерпанной repo-owned
remediation и отсутствия оставшейся безопасной работы.

Production mutation runner обязан иметь dry-run по умолчанию, отдельный
explicit apply, bounded scope, machine-readable manifest, pre-change digest,
backup/evidence contract, expected affected records, non-target invariants,
idempotency либо документированный recovery, post-apply readback и
reconciliation. Ad-hoc SQL, случайные local/server-only scripts и mutation
через архивный read-only MCP запрещены.

Human-gated `scope:production-mutation` закрывается только trusted-main exact
command после merge, canonical deploy/apply и reconciliation:

`/wb-core production-mutation complete <PR> head <HEAD_SHA> merge <MERGE_SHA> deployed <DEPLOYED_SHA> gate <GATE_COMMENT_ID> gate-digest sha256:<GATE_COMMENT_HASH> reconciliation <RECONCILIATION_COMMENT_ID> reconciliation-digest sha256:<RECONCILIATION_COMMENT_HASH> evidence sha256:<EVIDENCE_HASH>`

Finance/storage migrations дополнительно сохраняют все lease, snapshot,
backup, restore, writer/timer, exact-SHA и non-target contracts из
[`docs/architecture/10_hosted_runtime_deploy_contract.md`](docs/architecture/10_hosted_runtime_deploy_contract.md)
и [GitHub Release Train](docs/architecture/11_github_release_train.md).

## Проверка и closure

Перед `release:ready` проверь:

- изменены только requested и прямо необходимые support files;
- semantic diff прочитан полностью, а не только список файлов;
- targeted checks соответствуют риску;
- findings исправлены, checks и semantic review повторены;
- authoritative docs синхронизированы;
- нет secrets, production data, generated dumps и unrelated edits;
- PR open, non-draft, same-repository, направлен в `main`, current exact head
  имеет successful `baseline`, выставлены `task:standard` и одна `scope:*`.

Если пользователь не задал более раннюю границу, executor самостоятельно ведёт
repo-backed задачу через commit, push, PR, checks/review, `release:ready` и
Release Train до `release:done` либо `release:production`, затем fetch-ит
`origin/main` и подтверждает merged result. Open PR, только local checks или
merge без требуемого deploy/verify не являются completion.

Production UI evidence не заменяется HTTP `200`. Оно включает requested/final
URL и redirects, отсутствие `5xx`, `DOMContentLoaded`, видимый render,
непустые title/body, `pageerror`/fatal/существенные console errors и визуально
проверенный screenshot. По умолчанию используется новый isolated non-persistent
Playwright context без пользовательского profile/cookies/credentials и без
business mutations вне explicit scope.

Исполнитель не подменяет собственную technical verification отчётами других
агентов. Перед финальным handoff он сам проверяет GitHub state, final SHA,
semantic diff, checks/reviews, unresolved threads, docs и, если применимо,
canonical deploy/live/data evidence. Эта обязанность не поручает куратору
повторный audit после handoff.

Пользователь нужен только для strict human-only действия: отсутствующий
credential/permission/approval, interactive login/2FA/captcha, доказанный
необратимый data risk, security change, новая внешняя data destination,
material scope/risk change или platform hard stop.

## Итоговый ответ

Сообщай только применимое:

1. итоговый статус;
2. что реально изменено;
3. что реально проверено;
4. что намеренно осталось вне scope;
5. blocker и один минимальный следующий шаг — только если blocker есть.
