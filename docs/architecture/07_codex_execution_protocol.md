# Codex Execution Protocol

## Модель Выполнения

Codex может выполнять длинные, но bounded execution chains внутри `wb-core`, если задача конкретна и следующий безопасный шаг выводим из контекста репозитория.

Bounded означает:
- явный scope;
- явные stop conditions;
- без silent repo-wide redesign;
- без скрытых side effects вне текущей задачи.

## Task Classification Before Execution

Перед любым планом или prompt-ом для Codex задача сначала классифицируется как `L1`, `L2` или `L3`.

В prompt для Codex обязательно указываются:
- `Класс задачи`
- `Причина классификации`
- `Режим выполнения`

Эта классификация задаёт минимальный execution burden и ожидаемый уровень docs/smoke/review discipline для текущего bounded шага.

## L1/L2/L3 Execution Matrix

| Класс | Когда использовать | Минимальная норма выполнения |
| --- | --- | --- |
| `L1` | локальный малорисковый шаг | без отдельного read-only review по умолчанию; без `README` / architecture sync по умолчанию; только targeted smoke |
| `L2` | обычный bounded block | `module doc + index` обязательны; нужны targeted smoke + `1` integration smoke; без отдельного read-only review по умолчанию |
| `L3` | boundary/risk/governance task | усиленный bounded execution; docs sync идёт по смыслу текущего checkpoint; при необходимости делается отдельная merge-readiness проверка |

Матрица задаёт минимальную норму. Более строгая проверка допустима, если риск конкретной задачи выше.

## Stop Conditions

Codex должна остановиться и вынести задачу на review, если:
- задача требует менять migration scope;
- architecture docs конфликтуют с наблюдаемыми фактами репозитория;
- reference evidence недостаточен для заявленного контракта;
- запрошенное изменение вводит business-логику на foundation-этапе;
- требуется runtime-only behavior, который нельзя воспроизвести из repo artifacts;
- задача потребовала бы менять reference-репозитории.

## Verification Matrix

Для каждой существенной задачи Codex должна проверять, где применимо:

| Проверка | Минимальное ожидание |
| --- | --- |
| Scope | Изменены только запрошенные файлы и прямо необходимые support files |
| Task classification | Перед execution указаны `L1/L2/L3`, причина классификации и режим выполнения |
| Execution class obligations | Минимальные требования выбранного класса `L1/L2/L3` выполнены |
| Contract | Заявленные контракты совпадают с repository evidence или помечены как inference |
| Boundaries | В foundation work не добавлена business-логика |
| Diff hygiene | Нет случайных правок в reference-репозиториях |
| Documentation sync | При значимом техническом изменении обновлена каноническая документация в той же задаче |
| Project-pack sync | Если изменение влияет на project-oriented pack, обновлены затронутые файлы `wb_core_docs_master/` и manifest |
| Git capture | Если requested outcome по смыслу включает Git fixation или GitHub closure и пользователь явно не запретил Git/GitHub actions, канонический source-of-truth артефакт доведён до commit/push/PR/merge/delete-branch либо execution возвращён как incomplete с exact blocker |
| Completion state | Для live/public задач явно зафиксировано, достигнуты ли `repo-complete`, `live-complete`, active-surface verify, `pack-complete`, либо где именно блокер; `sheet-complete` фиксируется только для bound Apps Script/live sheet scope |
| Local validation | Базовые структурные проверки проходят |
| Reviewability | Результат понятен ручному reviewer без runtime-археологии |
| Step discipline | Если нужен manual step, один ответ содержит один практический следующий шаг; несколько независимых рискованных действий не смешиваются |
| Codex-first path | Bounded и безопасная техническая работа сначала выполняется через Codex; user handoff допустим только для human-only step |
| Prompt footer | Prompt для Codex заканчивается блоками `=== ДЛЯ КУРАТОРА ===` и `=== СЖАТАЯ ПРОВЕРКА ===` с обязательными полями |

## Completion State Model

Execution handoff должен явно различать четыре состояния завершения:

- `repo-complete`:
  - intended repo files обновлены;
  - локальные проверки/targeted smoke пройдены или blocker явно назван;
  - canonical результат не остался только в рабочем дереве.
- `live-complete`:
  - если задача меняет public HTTP route, runtime/service wiring, process restart contract, nginx/proxy publish или другой live runtime behavior, соответствующий live contour обновлён;
  - route/process/service existence проверены в живом contour;
  - public probe или equivalent live verify выполнен.
- `sheet-complete`:
  - если задача меняет bound Apps Script, live sheet behavior или sheet-side flow, выполнен `clasp push` или equivalent publish step;
  - минимальный live verify по затронутому sheet-flow выполнен.
- `pack-complete`:
  - если задача меняет policy/contract/checkpoint/runbook/status wording, обновлены и primary canonical docs, и затронутый `wb_core_docs_master`;
  - manifest обновлён;
  - если задача меняла primary docs или `wb_core_docs_master/`, после successful merge локальный `~/Projects/wb-core` приведён к current `origin/main`, `~/Projects/wb-core/wb_core_docs_master` проверен как upload-ready source по manifest, а в финальном handoff оставлен ровно один human-only шаг: загрузить актуальный pack во внешний ChatGPT Project.

Важно:
- `repo-complete` не означает, что задача полностью завершена.
- Если задача меняет public HTTP route, runtime/service/nginx publish, bound Apps Script, operator UI или live sheet behavior, execution handoff не считается complete на этапе "код готов в repo".
- Для таких задач Codex обязана довести нужный active contour в одном bounded execution: live/public/web-vitrina задачи закрываются через `repo-complete + live-complete + public/web-vitrina verify`, а `sheet-complete` добавляется только для изменений bound Apps Script/live sheet write path.
- Human-only step допускается только там, где действительно нужны логин, права, branch-protection approval / явный GitHub write blocker, ручная UI-проверка или решение по риску.
- Если `live-complete` или `sheet-complete` не достигнуты, финальный отчёт обязан явно назвать это состояние и точный blocker, а не маскировать задачу как "готово".
- Если hosted runtime уже имеет repo-owned deploy/probe contract, отсутствие deploy access или target values больше не считается "неизвестным operational контекстом": в blocker нужно точно назвать, каких именно values/rights не хватает.
- `pack-complete` не равен внешнему upload в ChatGPT Project: внешний upload остаётся отдельным human-only post-merge step.
- Repo-owned pack sync должен быть доведён в той же задаче, но отдельный post-upload manifest-sync больше не требуется.

## Execution Step Discipline

Assistant ведёт bounded работу по шагам.

- Для любого Codex handoff сначала фиксируется `L1/L2/L3` classification, а затем уже план шага.
- One step = one action: если нужен manual step, один ответ должен содержать один практический следующий шаг.
- Не нужно дробить задачу на искусственно мелкие сообщения, если несколько безопасных локальных подшагов естественно входят в один bounded execution chain.
- Но нельзя смешивать в одном ответе несколько независимых рискованных действий, которые лучше подтверждать по очереди.

## Codex-First Rule

Если bounded и безопасную техническую работу можно выполнить через Codex, assistant должна сначала выбирать путь через Codex, а не перекладывать эту работу на пользователя.

- Пользователя подключают только там, где действительно нужен human-only step: логин, права, branch-protection approval / blocker-driven manual merge fallback, ручная UI-проверка, решение по риску.
- Техническую рутину, которую Codex может безопасно выполнить сама, просить у пользователя нельзя.
- Если manual step неизбежен, он формулируется как один минимальный следующий шаг.
- Если задача по смыслу требует live/runtime closure и безопасные доступы уже есть, Codex не должна останавливаться на `repo-complete`; она обязана дотянуть deploy / public verify до полного bounded completion либо вернуть incomplete с exact blocker. `clasp push` требуется только для scope, который реально меняет bound Apps Script или live sheet write.
- Для hosted runtime family вокруг `registry_upload_http_entrypoint_block` canonical repo-owned path теперь живёт в `apps/registry_upload_http_entrypoint_hosted_runtime.py`; если задача затрагивает этот contour, сначала используется этот runner, а не ручное угадывание host/service steps.

## Git Fixation And GitHub Closure Ownership

Если requested outcome задачи по смыслу включает Git fixation или GitHub closure и пользователь явно не запретил Git/GitHub actions, commit / push / PR update / ready / retarget / merge / delete-branch входят в тот же bounded execution и больше не считаются default human-only step.

- Сначала проверяется `gh auth status -h github.com`.
- Если `gh` доступен, active auth валиден и у текущего execution context есть repo write/merge access, обычные GitHub operations считаются Codex-owned technical routine:
  - `git commit`
  - `git push`
  - `gh pr create` или equivalent PR update
  - `gh pr ready`
  - retarget через `gh pr edit --base ...`
  - `gh pr merge --delete-branch`
- Эта норма одинаково действует и для stacked PR sequence: merge в промежуточную base branch так же Codex-owned, как и merge в `main`.
- Auto-merge остаётся optional enhancement и не заменяет обычный merge для stacked/base-branch sequence.
- Если `gh` работает, write/merge access есть и нет blocker-ов, Codex обязана довести ordinary GitHub closure до merge + delete-branch, а не останавливаться на PR-ready/review-ready.
- Manual merge допустим только как fallback-blocker case: `gh` отсутствует, auth неактивен, scopes/permissions недостаточны, GitHub возвращает явный write blocker, или branch protection требует human approval.
- Если ordinary GitHub closure из текущего execution context невозможен, финальный handoff обязан назвать точный blocker и вернуть один минимальный human-only следующий шаг.
- Это правило не отменяет task-scope discipline: если requested outcome не включает Git fixation / GitHub closure или пользователь явно запретил Git/GitHub actions, Codex не должна самовольно выполнять commit/push/PR routine.

## Mandatory Codex Prompt Footer

Любой prompt для Codex считается неполным, если в нём нет обязательного classification header и двух финальных блоков.

В начале prompt обязательно указываются:
- `Класс задачи`
- `Причина классификации`
- `Режим выполнения`

В конце prompt обязательно указываются:
- `=== ДЛЯ КУРАТОРА ===`
- `=== СЖАТАЯ ПРОВЕРКА ===`

В блоке `=== ДЛЯ КУРАТОРА ===` обязательны поля:
- `Статус`
- `Что сделано`
- `Изменённые/созданные файлы`
- `Ключевой результат`
- `Что НЕ тронуто / что осталось вне scope`
- `Следующий шаг`
- `Если есть блокер — точная причина`
- `Repo state`
- `Live deploy state`
- `Public verify result`
- `Sheet verify result`
- `Upload-ready source state`
- `Manual-only remainder`

Для полей вне текущего scope указывается truthful значение `not in scope`, а не пустой пропуск.

Если в задаче есть Git-изменения, в блок `=== ДЛЯ КУРАТОРА ===` дополнительно обязательно включаются:
- `Commit hash`
- `Push`
- `PR`
- `Ссылка на PR`

В блоке `=== СЖАТАЯ ПРОВЕРКА ===` обязательны:
- `3-5 коротких пунктов по сути`
- `одна строка с главным выводом`

## Documentation Sync Rule

Если в рамках задачи Codex меняет:
- код;
- структуру каталогов и файлов;
- smoke-path или критерии проверки;
- артефакты;
- runtime helpers;
- другой значимый технический результат,

она должна в той же задаче обновить и соответствующую документацию, если такая документация существует или по смыслу уже должна существовать как часть source of truth проекта.

Задача не считается полностью завершённой, если:
- технический результат уже изменён;
- документация по смыслу должна была быть обновлена;
- но фактически не обновлена.

Если Codex создаёт или меняет канонический source-of-truth артефакт, задача не считается завершённой, пока результат не:
- зафиксирован в Git;
- оформлен отдельным commit;
- запушен в удалённую ветку;
- оформлен в PR,
либо, если PR сознательно откладывается, не сохранён хотя бы в отдельной paused-ветке.

Минимально к таким артефактам относятся:
- код, который должен стать частью проекта;
- policy / regulation / architecture документы;
- `docs/modules/*`;
- `docs/modules/00_INDEX__MODULES.md`;
- другие документы, явно помеченные как canonical / source of truth.

Для `wb-core` частное обязательное правило такое:
- канонический source of truth для модульной документации живёт в `docs/modules/`;
- если модуль новый, Codex обязана:
  - создать новый файл модуля в `docs/modules/`;
  - обновить `docs/modules/00_INDEX__MODULES.md`;
- если модуль изменён, Codex обязана:
  - обновить соответствующий файл модуля в `docs/modules/`;
  - и при необходимости обновить `docs/modules/00_INDEX__MODULES.md`;
- создание или изменение модульной документации без Git-фиксации запрещено;
- создание новых документов в `docs/modules/` без commit/push/PR или paused-ветки считается незавершённой задачей;
- финальный кураторский блок обязан явно показывать:
  - попало ли изменение в Git;
  - commit hash;
  - push;
  - PR / paused-ветка.

Для `wb_core_docs_master` действует дополнительное частное правило:
- primary canonical docs всегда первичны по отношению к project-pack;
- `wb_core_docs_master/` обновляется только как compact curated-pack, а не как dump-копия `docs/`;
- если изменение влияет на contract/status/smoke/checkpoint/module status/glossary/common runbook/migration boundary, Codex обязана:
  - обновить primary repo docs;
  - обновить затронутые файлы в `wb_core_docs_master/`;
  - обновить `wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md`;
  - если в этой задаче менялись primary docs или `wb_core_docs_master/`, после merge привести `~/Projects/wb-core` к current `origin/main`, проверить `~/Projects/wb-core/wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md` как признак upload-ready source и в финальном handoff оставить ровно один human-only следующий шаг: загрузить актуальный pack во внешний ChatGPT Project;
- manifest для `wb_core_docs_master` остаётся build-metadata файлом и не ведёт operational state внешней загрузки;
- legacy knowledge в `wb_core_docs_master` разрешён только в register-слое (`LEGACY_TO_WEBCORE_MAP`, `DO_NOT_LOSE_CONSTRAINTS`), а не как перенос полного legacy-корпуса;
- создание или изменение `wb_core_docs_master` без Git-фиксации так же считается незавершённой задачей.

## Когда Codex Может Действовать Самостоятельно

Codex может действовать самостоятельно, если:
- задача находится внутри `wb-core`;
- изменение укладывается в уже зафиксированные migration principles;
- разумные assumptions низкорисковые и явно помечены;
- repository policy не требует внешнего approval.

## Когда Нужен Reasoning-Review

Reasoning-review нужен, если:
- контракт неоднозначен;
- остаются две валидные архитектурные опции;
- изменение может зафиксировать долгосрочную границу;
- предлагаемый шаг влияет на cutover sequencing или ownership source of truth.

## Что Codex Нельзя Делать Без Явного Разрешения

Codex не должна без явного разрешения:
- менять reference-репозитории;
- добавлять business или production runtime code во время foundation-only work;
- создавать CI/CD workflows;
- создавать миграции БД;
- создавать ad-hoc deploy scripts вне already fixed repo-owned deploy contract;
- создавать или заменять operator table;
- делать commit, push или открывать PR, если requested outcome задачи по смыслу не включает Git fixation / GitHub closure или пользователь явно запретил Git/GitHub actions;
- перекладывать на пользователя bounded безопасную техническую рутину, которую может выполнить сама, кроме human-only steps;
- считать runtime snapshots authoritative без зафиксированного reconcile evidence.
