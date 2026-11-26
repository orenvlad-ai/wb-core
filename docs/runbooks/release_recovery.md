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

Точный выпуск 41820909577 (PR 1570, Gate 41814633119, head
`321b6ea8b1f0eeb1081a3cc9e57fb91924075409`, merge
`a057f334f5e25d6f63bf256a53f017d3b13b3f65`, operation
`release-v3-0a5490564fc9a849623c43976fe9ebbe`) допускает отдельный хвост после
первого отказа buyer Chrome reserve. Immutable log обязан доказать единственный
определённый exit 1: deploy line 1239 и `install` line 149, до первого изменения
Chrome. Отказ перед копированием package или неопределённый SSH исход не подходят.
Предыдущий успешный выпуск 41810395795 (PR 1555, Gate 41810214857) подтверждает
фактически установленную версию `78cad7d088fd5377c8fbb1c7cd5ad1ccb04a6880`.

До preview резерв root должен быть восстановлен до неизменённых **25 GiB**.
Нативное read-only доказательство проверяет точные policy/browser source blobs,
размер, SHA256 и package identity установленного deb, все packaged runtime files
и links против его payload без распаковки, sandbox root:root/4755, отдельный UID,
владение и режимы private profile/state и отсутствие активного Chrome pilot.
Dependency/deploy/unit contracts должны совпадать с предыдущим successful runtime;
бизнес-таймеры остаются disabled под исходным held/quiet окном и owner
`mutation_started`. Недостаток reserve, foreign owner или любой drift блокируют путь.

Recovery сохраняет исходные operation/window/baseline, не повторяет Chrome,
OS/Python/browser installation, sync, chown или merge. Допускается только уже
описанное однократное восстановление отсутствующего pinned npm closure после
проверки lockfiles; сломанный существующий closure блокируется. Затем выполняется
canonical activation tail, cleaner `before_complete` probe, metadata CAS и final
readback. Резерв 25 GiB проверяется перед owner recovery-start и перед каждой
оставшейся mutation phase, включая npm и metadata CAS. Прежний uncertain claim
остаётся только для readback. Этот профиль не распространяется на другие выпуски;
exact formula-resume того же окна выполняется отдельно после complete receipt.

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

Только для выпуска 45429927905 (PR 1621, Gate 45425668063, base
`62ef453c55f2faddc86c0661960d350084b9fbfd`, head
`7cca15e72938a5c98a12c7089f86c0984a432173`, merge
`336345e6c9b08f6c2741523aa066f93c485a2f6e`, operation
`release-v3-ef96969c1f11e80a3b8c784cbb1cc0e2`) добавлен тот же completion-tail.
Его immutable log доказывает единственный SSH exit 1 после
`skipped_maintenance`, точную строку deploy 1285 и строку 30 `_listener_ready`
с `RuntimeError: cleaner listener not ready for before_complete`.
Сервис объявил готовность в 15:52:35 UTC, через 1,6 секунды после отказа.
Canonical step `ci/github_release_runner.py` связывается с единственным
`GATE_RUN_ID` в том же step; старые профили сохраняют прежние проверки.
После восстановления listener штатный `preview` обязан повторно доказать
исходный `mutation_started` owner, прежнее held/quiet окно, exact runtime,
успешные service/auth/cleaner readbacks и сохранённый Finance pilot. Только
`apply` с точным fingerprint этого preview допускает metadata completion CAS
той же операции и final readback. Ручные restart, activation, повтор deploy
или изменение metadata вместо workflow запрещены; uncertain claim остаётся
только для readback. Новый профиль не разрешает автоматически возобновлять цикл.

Успех подтверждают связанный recovery receipt, завершённая metadata, точная
версия, сервисы и неизменный изолированный Finance pilot.

Этот же bounded-listener completion-путь отдельно разрешён для выпуска
45604611131 (PR 1628, Gate 45602270422, base
`31d1e0e45baff14083e55e7385d4911e024e3541`, head
`3582976572c418919926946001628d008f359765`, merge
`c46787a111c5b26a4f6428695eed9eb03afe1222`, operation
`release-v3-305323744b7ecac2ba7f77b192c06194`). Immutable job log
доказывает единственный SSH exit 1 на той же строке 1285, после успешных
service/auth/storage checks и activation `skipped_maintenance`; строка 30
`_listener_ready` сообщает `cleaner listener not ready for before_complete`.
Последующее чтение canonical probe подтвердило готовый authenticated listener.

Использовать `release-recovery.yml` с `release_run_id=45604611131`: сначала
`mode=preview`, затем один `mode=apply` с проверенным fingerprint. Нужны исходные
owner `mutation_started`, окно `wbc0213-partner-spend-release-2-20261125`, baseline
`sha256:c090c4c94156ac5e29606967d38c2d8526ebf51787edafaa5beac478f704ec16`,
exact runtime, живой cleaner и все прежние guards. Продолжаются только metadata
CAS и конечные readbacks с завершением того же owner. Sync, restart, activation,
установка зависимостей и повтор Release Runner запрещены. После complete receipt
выполнить отдельный canonical formula-resume исходного окна. Этот recovery не
записывает исторические подтверждения рекламных расходов.

Выпуск PR1639 остановился на проверке готовности сервиса очистки после
`skipped_maintenance`. Исходный Release Runner `45890843119` связан с Gate
`45883256251`, merge `fea41398877c6d9d0bf963c13c2a8b8482acc0ae` и операцией
`release-v3-2b2f73f250d0f7d85617893111ce9c19`. Строки 1285 процедуры выпуска
и 30 проверки сервиса в журнале GitHub подтверждают место остановки.

Для этого выпуска запустите `release-recovery.yml` с `release_run_id=45890843119`
и `mode=preview`. Сверьте версию, владельца операции, окно
`wbc-history-catalog-97-20261126` и отпечаток плана. Режим `apply` с этим
отпечатком ещё раз проверяет готовность сервиса, завершает запись версии и
подтверждает результат исходной операции. Снятие сервисного режима
выполняется отдельно после конечной проверки выпуска.

Для выпуска 42687135829 (PR 1604, Gate 42622619635, head
`d88aff18833213ae4be3d48bbe8d86d905359f2a`, merge
`8f32cd7f6fbc436770d4d297b7fb4dbc3486b719`, operation
`release-v3-2cc2ff1cbc3bc183ba33b5fd843ab352`) сохраняется обычный
`storage-tail`: исходный immutable log обязан доказать единственный определённый
exit 3 конечного `status-readback` после restart. До apply нужно устранить
реальный недостаток резерва и штатно обновить derived storage status artifact
под тем же deploy owner; сам generic tail артефакт не обновляет. Старые
исключения для degraded backup health к этому выпуску не применяются.

После оставшейся Change Registry activation этот точный хвост обязательно
выполняет canonical cleaner `before_complete` probe с тем же merge SHA перед
metadata CAS. Probe входит в fingerprint проверяемого preview; его отказ
останавливает completion и оставляет уже принятый claim только для readback.
Это не случай уже завершённой activation: первый запуск activation сохраняется.
Merge, sync, зависимости и restart не повторяются. Все исходные owner,
maintenance, storage, source и Finance pilot проверки сохраняются. После
complete recovery штатный formula-resume исходного окна выполняется отдельно.


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


## Exact historical backup-cap failure: Release Runner 41294599188

PR #1527 (`bfe4706ece716b93c26327f3bab268671ca9e3e7`) reached the final
storage readback after restart. The new backup health truthfully reports the
31 October pre-copy per-set-cap failure; its selected verified backup and
replacement capacity remain valid. The held release owner prevents running a
replacement backup before release completion.

For this run only, the storage-tail recovery accepts a separate live capacity
proof while retaining `degraded` health. It binds the original owner, PR and SHA,
the immutable policy, selector and waiting intent, the exact historical failure,
no pending transaction, native backup inventory/RPO checks, all filesystem
identities and every other storage reserve. The only permitted alert is the
backup reserve's historical-health refusal. Actual available bytes must cover
canonical source size + 8 GiB hard reserve + 64 MiB copy overhead + 1 MiB
compression workspace + 4 GiB emergency reserve. The source manifest and root
policy must not drift from the preview. The check repeats before activation and
immediately before the metadata completion CAS; an expired RPO or reduced free
space refuses completion.

The normal source/owner/quiet guards, service/auth checks, activation,
`before_complete` cleaner probe, metadata CAS and final readback remain required.
No artifact, backup policy, last failure or admission intent is changed by this
exception; there is no code sync, restart, backup execution or automatic resume.
After successful recovery, explicitly resume the existing window and perform
the separately reviewed canonical backup. Recovery completion alone is not
proof that the backup or subsequent scheduled cycle succeeded. An uncertain
recovery remains readback-only under the existing claim rules.

## PR1623: exact storage preflight refusal before sync

Release Runner `45470334066`, Gate `45468017296`, operation
`release-v3-3d39b2cc0010ab71718b7e1fc93e214f`, merged
`48aba616911767c1c9410d013f59cfbe258ce4c8` stopped at adapter line1208:
the read-only root-storage preflight returned exit3 because available
26,385,899,520 bytes was below the unchanged 26,843,545,600-byte reserve.
The source ordering proves owner start, mkdir, sync and every later deploy
stage were unentered. This is outside the generic post-sync recovery lane.

After the blocking files have been safely moved and verified, use the existing
**Post-merge Release Recovery** workflow with `release_run_id=45470334066`,
first `mode=preview`. The exact lane verifies the original immutable failed
job, blocked receipt, Gate/plan and merge lineage; the trusted runner may differ
only by independently classified repo-only descendants. It requires the old
complete `336345e6c9b08f6c2741523aa066f93c485a2f6e` runtime, fresh services,
auth and Finance proof, and the original PREPARED owner's fingerprint, empty
expected SHA, window and baseline. Native held-formula/quiet checks and the
unchanged storage preflight must pass. Free bytes are checked live, not frozen
into a preview fingerprint.

Review the preview and supply its exact fingerprint with `mode=apply`. A durable
recovery claim precedes another full prestate comparison. Only then does an
isolated clean checkout of the original merge execute the unchanged canonical
deploy once, with the original PR/head/operation identity. All normal storage,
owner, dependency, systemd, activation, cleaner readiness and metadata guards
remain enabled. The trusted repo-only runner remains checked out and verifies
its main identity again before and after native owner finish. Complete runtime,
Finance and same-window owner readback precede the terminal recovery receipt.

Any existing claim prohibits another deploy, even after another storage or
cleaner refusal. Repeated preview is read-only. Repeated apply may only verify
already-complete exact runtime markers and unchanged Finance acceptance, finish
the same owner idempotently, and publish/read back its terminal receipt. It never
repeats sync, restart, activation or dependency work. Incomplete or indeterminate
runtime remains ambiguous for a separately reviewed tail; do not rerun Release
Runner, cancel ownership, change the baseline, or repeat mutation manually.
Recovery never resumes `wbc0201-inventory-history-repair-20261124` or starts a
business cycle. A recovery PR touching only `ci/` and this runbook is repo-only;
it does not deploy a second runtime before the original operation is complete.

## PR1626: same unentered storage preflight, backup reserve

The same closed pre-sync profile accepts only original Release `45514713724`,
Gate `45512517259`, PR1626 merge `a7fa3b894c18fc46d96883d0493a60d49f5c7e3a`,
operation `release-v3-63b81852c1d07b3f1b83c45411ecd2d7`. Its immutable job proves
adapter line1208 exited3 before owner start/sync: backup available58,428,649,472
bytes was below the unchanged dynamic Finance replacement reserve59,018,551,296.
The root role passed. No storage threshold or policy is changed by recovery.

Use the same workflow with `release_run_id=45514713724`, preview then the exact
reviewed fingerprint. The original PREPARED owner, empty expected SHA, exact old
`48aba616911767c1c9410d013f59cfbe258ce4c8` runtime, held/quiet window
`wbc0201-supplier-history-ready-pin-repair-20261125` and baseline
`sha256:9d6f60aae1236e07f1fd5ad5aa25e15f95e76940ca11de8db2c8a02efa49dc13`
remain mandatory. Independent capacity remediation must first pass all native
storage roles. Preview/apply retain the same one-shot canonical deploy and
readback-only existing-claim rules above, under the original operation identity.
This case shares the existing pre-sync implementation; unknown runs, other
failure stages, mutation_started owners and unreviewed source drift refuse.
Recovery neither cleans files nor resumes the window nor starts a business cycle.

## PR1635 history publication release

Release Runner `45797154129` stopped at the same storage preflight before owner
start and sync. Its immutable job records 60,424,671,232 bytes available on the
backup volume and a required reserve of 60,811,292,672 bytes. Gate `45771933233`
admitted merge `3d794cb301a7d0a1eeebf0ff67d53e91de922623` under operation
`release-v3-59a3e6acfff3721c8acd4849fde28db1`.

After restoring and verifying capacity, preview recovery for `45797154129`.
The old complete runtime must still be `44f0a9966833350b807818489b809528ed781089`.
The original prepared owner must hold `wbc-history-pr1635-f711cc11-20261126`
with its original baseline and an empty expected SHA. Review the preview
fingerprint and apply it once. The canonical deploy, same-owner completion,
Finance readback and existing-claim rules above apply unchanged.

Recovery `45800208191` consumed that deploy claim. Its adapter finished the
restart and activation readback, then timed out at the final cleaner listener
check on line 1285. For this exact nested run, use Post-merge Release Recovery
with `release_run_id=45800208191`. Preview verifies both GitHub runs, the
original bot claim, the failed recovery artifact and job log, and the installed
merge. It requires the same held window, started owner, unchanged Finance,
and a successful native cleaner check. Compare the preview fingerprint before
apply. The remaining write is the guarded deploy-metadata completion against
the reviewed bytes and service PID, followed by native owner finish. A consumed
continuation claim permits only final readback and finish. Once confirmed, the
parent recovery can record its terminal receipt using its original fingerprint.

## PR1623: exact unentered Chrome RAM guard after sync

Recovery `45473903471` of the original PR1623 operation reached adapter line1242, then
`wb_buyer_chrome_runtime.install` line151 refused insufficient staging RAM.
This guard precedes directory creation, package download/copy, profile adoption
and extraction. The original owner remains `mutation_started`, same SHA/window/
baseline, and deploy metadata remains incomplete. This failure does not permit
another full deploy or a repeat of the earlier recovery claim.

For this failed recovery run only, the same workflow accepts
`release_run_id=45473903471`. Preview proves the original release/Gate/claim,
the exact failed recovery source/artifact/job, every installed dependency and
managed unit, original held/quiet owner and Finance acceptance. Its read-only
Chrome validator verifies the pinned package, every packaged runtime entry and
checksum, sandbox/link/permissions, private identity/profile permissions, idle
pilot processes and library closure. Missing or corrupt installation refuses
continuation; this lane cannot install or launch Chrome. The 3GiB requirement
for a new staging operation is unchanged. Root storage reserve still applies.

After review, apply with the exact preview fingerprint claims a new one-shot
continuation and rechecks the complete prestate. It executes only the unentered
canonical prepare/systemd/nginx/restart/reconcile/readback tail, then activation,
the unchanged cleaner before-complete probe, metadata CAS and native owner
finish. Exact source and repo-only main identity remain pinned. Existing child
claims are readback-only; complete markers may authorize idempotent owner finish
and terminal receipt, never another restart or activation. The parent claim and
original operation are retained, and the maintenance baseline is never replaced.

The native npm dependency stage also followed the Chrome guard and was unentered.
When `node_modules` is entirely absent, this exact continuation may execute the
canonical locked `npm ci --omit=dev --ignore-scripts` once under its new claim.
It verifies the exact `ajv`/`ajv-formats` versions and compares the entire prestate
apart from that one allowed missing-to-complete transition. Existing incomplete,
symlinked or wrong-version modules refuse continuation. OS/Python/browser
installers remain forbidden; an uncertain npm result is never resubmitted.

Before apply, backing out this repository-only change is an ordinary Git revert
and changes no runtime. After any uncertain tail result, keep the original
maintenance window and owner held, inspect the same operation and recorded phase,
and review a continuation for that exact remaining stage. There is no automatic
rollback, owner cancellation, SHA change, installer replay or window resume.
