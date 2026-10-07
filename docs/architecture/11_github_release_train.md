# PR Gate, выпуск и Production Apply

## PR Gate

Для `main` требуется один GitHub context — `pr-gate`. Проверка связывается с
точными base/head и состоит из:

- проверки diff и форматов изменённых файлов;
- короткой проверки механизма выбора тестов;
- тестов только затронутой области;
- более широких тестов только для общей основы, схемы или опасного изменения.

Неизвестная область останавливается быстро с понятной причиной. Она не включает
весь репозиторий автоматически. Draft PR может проверяться, но не выпускается.

Выбор тестов выполняется кодом из проверенного base. Изменение самого selector
начинает действовать только после безопасного слияния.

## Release Runner

После успешного Gate доверенный код из `main` проверяет PR, base/head, результат
Gate и неизменность ветки. Затем выполняется один squash merge. После слияния
Runner подтверждает, что родитель merge равен проверенному base и `main` указывает
на этот merge.

`repo_only` завершается без production-доступа. `live_runtime` разворачивает
точный merge и проверяет фактическую версию. Если изменение подготавливает
отдельную запись данных, выпуск заканчивается до неё.

Receipt содержит только нужные связи: PR, Gate run, base, head, merge,
фактически выпущенную версию, результат и причину ошибки. Блокировка завершает
workflow ошибкой, а не зелёным статусом.

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

Успех подтверждают связанный recovery receipt, завершённая metadata, точная
версия, сервисы и неизменный изолированный Finance pilot.

## Production Apply

Общий launcher не содержит бизнес-логику. Он вызывает только зарегистрированный
доменный adapter и связывает:

- operation ID и точную production-цель;
- отпечаток состояния до записи;
- отпечаток кандидата;
- фактический submit и readback;
- ссылку на восстановление.

Adapter отвечает за свои locks, backup/CAS, допустимые строки и предметную
проверку. Launcher допускает один submit. Неоднозначный ответ вызывает один
readback без повторной записи.

Исторические WBC-манифесты, частные режимы и универсальные пакеты проверок в
действующий контур не входят.
