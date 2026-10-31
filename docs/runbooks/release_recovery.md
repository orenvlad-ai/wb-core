# Частные случаи восстановления и первого выпуска

Этот документ читают при соответствующем сбое или первом переходе со старого
runtime. Он сохраняет поддержанные ограничения; перенос текста не означает,
что старые операции завершены, и не расширяет recovery на другие случаи.
Обычный выпуск описан в [Release Runner](../architecture/11_github_release_train.md).

## Завершение прерванного выпуска после merge

Workflow `Post-merge Release Recovery` принимает только явно доказанные стадии
сбоя после merge. Помимо исторических storage/readback и activation-precheck
случаев, точные выпуски 36437349246 (PR 1341) и 37356285295 (PR 1386,
Gate 37355289217, merge `073f62de40c99eb3c58e60d0f27580a962102831`)
допускают восстановление после локального
`SQLiteContentionExhausted` в exact Change Registry activation: failed systemd
invocation и отсутствие activation job в каноническом operational store должны
быть подтверждены чтением. Транспортно неопределённый исход не допускается.

Сначала запусти workflow в режиме `preview` с `release_run_id` исходного
неуспешного Release Runner. Проверь сформированный план: исходные PR, Gate,
base/head/merge, доказательство стадии отказа, цель, незавершённую metadata и
Finance pilot в его утверждённом изолированном состоянии. Для режима `apply`
передай тот же `release_run_id` и точный `preview_fingerprint` из проверенного
плана. Изменение состояния между preview и apply останавливает выполнение.

Storage-tail после уже выполненного restart не повторяет sync, зависимости и
сервисные операции; normal activation tail
после доказанного сбоя drain повторяет с canonical `prepare-deploy` только
оставшиеся activation stages (install/reload, nginx, restart, reconcile,
readback, Change Registry и metadata completion). Оба случая сохраняют один
claim и phase evidence, не делают merge, sync, chown или установку зависимостей,
а незавершённый claim разрешает лишь readback, без обхода или повторной записи.
Exact SQLite activation tail повторяет только незафиксированный activation job,
затем обязательный cleaner `before_complete` probe и каноническое metadata CAS.
Он не повторяет sync, зависимости, nginx или restart; drift исходного unit,
journal или operational job блокирует применение до новой записи.
Для точного выпуска 37598234406 (PR 1409, Gate 37597673218, head
`d86ec9b4901e7b6003c9103eb911657316cc8a2e`, merge
`e4c588c2ea10c47d08b08356a5fb7ac9b355bc3a`) разрешён отдельный случай:
первый `root-storage-status` завершился с кодом 2 после sync/metadata, до
зависимостей. Immutable traceback должен указывать строку 1206 exact merge;
одноимённая конечная проверка на строке 1238 не подходит. Исходный blocked
receipt, успешный предыдущий выпуск 37552375993 с deployed SHA
`91f3403fa9b757b088b6193b26bfe39674e62ac2`, неизменённые dependency/deploy
contracts и установленные Python/Node/OS/browser/unit зависимости проверяются
до применения. Missing, expired или противоречивое доказательство блокирует путь.

После устранения причины storage failure этот случай обновляет только derived
storage status artifact и проверяет его перед claim. Если sync удалил
`make_mvp/node_modules`, допускается один canonical `npm ci --omit=dev
--ignore-scripts --no-audit --no-fund` по неизменённым exact lockfiles. Уже
установленные pinned modules не переустанавливаются; существующий каталог с
неверными или сломанными modules блокируется. Npm stage имеет durable
before/after evidence; транспортная неопределённость оставляет claim только
для readback и не разрешает второй install. Readback допускает только появление
проверенных pinned modules, сохраняя все прочие dependency/target guards.
Далее выполняется обычный canonical activation tail с обязательным cleaner
`before_complete` probe и metadata CAS. Завершённая metadata и retained-claim
readback требуют установленного pinned npm closure. Sync, merge, chown и
OS/Python/browser installation не повторяются. Это разрешение не применяется
к другим выпускам или другим стадиям отказа.

Тот же exact-profile путь поддерживает выпуск 41205730174 (PR 1512,
Gate 41203878113, head `320f145fe5dcc9893ce427c0870c85f6e6640103`, merge
`d9a30f31b1efd68b83305a987af09cd0255de1b5`): отказ первой проверки на строке
1231; конечная строка 1263 не допускается. Предыдущий успешный runtime
подтверждается receipt выпуска 40951711462 (PR 1508), deployed
`167916cc38ec11a95a303493a324977579a806b1`. Это не base PR 1512:
между ними были изменения только репозитория. Dependency diff сравнивается
именно с фактически выпущенной версией.

Единственное разрешённое изменение защищённых файлов для PR 1512 — unit
`wb-core-web-vitrina-finished-snapshot.service`: точные Git blobs
`e6941501a01edccdff523c511805d048c1f87ff7` →
`a81fd77e0ab57c7e3a33641c95a412ae9cddc9e4` отличаются литералом formula epoch.
До завершения canonical activation tail установленный unit может совпадать
с одним из этих двух blobs; исходный файл в app всегда обязан совпадать с merge.
После completion допускается только новый установленный blob. Остальные units,
зависимости, deploy contract, исходные receipts и однозначная стадия отказа
проверяются без ослаблений. Для этого профиля оба autoanswers timer должны оставаться disabled, как в
удерживаемом maintenance-окне; старый профиль 1409 продолжает требовать enabled.
Recovery не возобновляет бизнес-таймеры: удерживаемое
maintenance-окно затем завершается штатным formula-resume по его отдельному
контракту.

Точный выпуск 39635174275 (PR 1486, Gate 39630567883, merge
`3689f51ede065489120f0d199115bfb309490d9a`) поддерживает продолжение после
отказа финального cleaner `before_complete` probe с ConnectionRefusedError.
Исходный immutable log должен доказывать единственный exit 1 на строке 1282,
точную команду и уже завершённую activation с `skipped_maintenance`.
Preview требует восстановленный сервис, успешный cleaner probe и исходного
`mutation_started` owner с прежним held/quiet окном. Apply сохраняет этот owner,
читает activation без повторного запуска, проверяет cleaner и выполняет обычный
metadata CAS и конечный readback. Повтор sync, restart, activation или миграции
не допускается; устранение причины сбоя выполняется и проверяется до preview.

Тот же строго ограниченный cleaner completion поддержан для выпуска
40563653934 (PR 1501, Gate 40560748870, merge
`ecf9c14c76bb6955e77850572a2af0cda13a83fc`). Исходный лог доказывает тот же
единственный отказ на строке 1282 после `skipped_maintenance`: listener начал
принимать соединения после раннего probe. Точные base/head/merge и operation
зафиксированы отдельным профилем; остальные выпуски не получают этого допуска.

Этот же точный completion-путь разрешён для выпуска 40885416921
(PR 1505, Gate 40878949065, merge
`597eafe8f50bbfbe5c38533006dbfde1361cc867`, operation
`release-v3-4b74569e32b7a8fbaaa36d1c03c69106`). Отдельный профиль закрепляет
base/head/merge; immutable log обязан пройти те же проверки единственного
отказа `before_complete` на строке 1282 после `skipped_maintenance`.
Восстановление продолжает исходную операцию только после успешного readback
сервиса и cleaner; повтор deploy, restart и activation не разрешён.

Успех подтверждают связанный recovery receipt, завершённая metadata, точная
версия, сервисы и неизменный изолированный Finance pilot.


## Первый выпуск защиты deploy ownership

До первого sync installed owner helper отсутствует. Trusted candidate source
передаётся inline только после независимой проверки **существующих installed**
pause/profile/barrier/admission APIs. Claim/start и checks первого mkdir/rsync
используют этот bootstrap; rsync receiver не печатает служебный JSON в протокол.
Затем используется installed helper из exact synced code. Проверки bounded;
force/weakening режима нет.

Старый trusted Runner ещё не содержит pre-merge claim, а старые central release
APIs ещё не знают owner. Поэтому **первый выпуск выполняет один ответственный
writer под заранее доказанным explicit held/quiet pause**; concurrent resume,
barrier release, иной deploy и restore запрещены эксплуатационно до sync новой
защиты. Первый pre-merge участок опирается на это явное окно. Не выдавать его за
автоматически защищённый новым Runner. Subsequent releases используют новый
trusted Runner и central owner guards. При неизвестном исходе первого sync
сохраняют окно и читают ту же operation; старый runtime без helper не допускает
новый generic recovery.

Старый Runner передаёт PR/head, но ещё не передаёт operation ID. Новый adapter
до claim восстанавливает **тот же release-v3 ID**, который старый Runner запишет
в receipt: trusted `workflow_run` event, successful Gate run/jobs, один immutable
checked plan с верным hash, exact PR/head/base и единственный merge parent,
совпадающий с plan base, плюс текущий checkout exact merge. Это шесть read-only
GitHub запросов с существующим timeout 30 секунд каждый. Недостаток, timeout или
drift evidence блокирует выпуск до claim/sync; release context никогда не
переходит на manual identity. Вне release context сохраняется standalone manual
identity. Active owner не переименовывается. Поэтому поддержанный recovery после
первого sync получает исходный ID и не оставляет foreign owner.
GitHub после merge может вернуть пустой список PR links у Gate run; он не
является обязательной связью. Binding дают checked plan PR/head/base, env PR/head
и exact merged PR/parent. Непустой противоречивый список links отвергается.

Первый старый Runner также не передаёт defer-finish: candidate adapter завершает
owner после собственного полного readback, а явное ручное окно продолжает
держаться через оставшийся внешний readback старого Runner. Single-writer
граница первого выпуска действует до его окончательного receipt; resume до
этого запрещён. Новые Runner удерживают сам owner через внешний readback.

Этот блок не восстанавливает старый uncertain цикл. Его canonical receipt
остаётся interrupted с честным исходом; следующий distinct scheduled slot
допустим после exact resume. Автоматического replay старой операции, resume или
новой глобальной очереди нет.


## Первый выпуск admission leases для фоновых workers

Первый выпуск требует pause и отдельного idle proof старых raw-thread путей:
старый runtime ещё не удерживает новые leases, а обычный prepare-deploy не
доказывает завершение daemon threads. После выпуска сверяют deployed SHA,
readiness admission и maintenance status; существующие jobs наблюдают через
read-only endpoints/operational sessions (`mode=ro`, `query_only=ON`). Для
естественно активного job drain остаётся non-idle до completion/error; после
завершения всех jobs — idle. Ожидающий SKU worker сам по себе не означает
non-idle. Отсутствие активного job не доказывает lifetime lease в production;
новый job или pause для диагностики запускают только в разрешённой области задачи.
