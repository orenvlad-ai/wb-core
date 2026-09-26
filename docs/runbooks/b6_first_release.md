# Б6-00а: первое окно после исправления quiesce

Область: доставка кода при заранее остановленном HTTP и сохранение точного
baseline обслуживания. Это дополнение к
[договору hosted runtime](../architecture/10_hosted_runtime_deploy_contract.md)
и [Release Runner](../architecture/11_github_release_train.md).
Оно не разрешает обход storage admission или диагностические WB writes.

## До окна

Сохранить свежие target/runtime SHA, manifest и filesystem identity, baseline
таймеров/schedules/owner policy и допустимый способ восстановления. До первого
merge/sync подтвердить вместимость всей следующей операции с конкуренцией,
ростом и резервом; поздний deploy status не заменяет ранний admission. При
storage alert сначала отдельный доказанный bootstrap lifecycle/ёмкости с
readback. Не удалять last-good в расчёте на будущую замену и не ослаблять guards.

Завершить локальные проверки, review и exact Gate до паузы. Изменения runtime
до начала окна остаются в draft. Назначить одного владельца окна и проверенный
supervision/abort путь: интервал acquire→restore submit сам по себе не защищён
существующим detached restore. Покупка или неизвестные данные блокируют только
зависимое опасное действие.

## Binding до warehouse hold

Использовать один W (`window_id`), P (`plan_fingerprint`) и свежую revision.
Глобальные параметры CLI и target paths брать из проверенного execution record.
Ниже описан порядок, а не готовые команды для слепого исполнения.

1. `business_data_maintenance barrier-acquire`: один submit; readback exact W/P,
   active/acquiring, hold ещё не подтверждён.
2. `business_data_maintenance prepare` с W/P и исходной revision. Сохранить
   baseline, `control_signature_before_hold`, `prepare_readback` и новую
   paused revision. Warehouse timer в этот момент остаётся в исходном режиме.
3. **Повторный `business_data_maintenance prepare` с теми же W/P и paused
   revision до warehouse hold.** Это существующий путь binding, не повторный
   захват baseline. Проверить в результате и приватном state
   `prepared_resume_binding`: W/P, paused revision/policy fingerprint,
   barrier fingerprint, baseline signature, prepare readback fingerprint,
   полный timer inventory. Последняя audit-запись `prepared_resume_bound`
   должна содержать тот же binding. Наличие первого `status=prepared`
   само по себе binding не доказывает.
4. `warehouse_functional_maintenance hold --disable-timer`: один submit,
   ограниченный drain с прежними guards. Сохранить его отдельный baseline.
5. `business_data_maintenance hold` с теми же W/P и paused revision. Проверить
   held и два стабильных quiet readback: terminal/PID 0, пустые writers,
   свободные locks, отсутствие hot sidecars/неизвестных timers/cron. Проверить
   дополнительные CLI/FBS writers по scope окна.
6. `barrier-confirm`: exact W/P и held/hold_confirmed. Fresh target/storage
   admission остаётся обязательным перед зависимой записью.

Неверные W/P/revision, изменённый inventory, timer actual drift или policy
fingerprint останавливают продолжение. Порядок prepare→warehouse hold→binding
недопустим: нельзя переписать baseline или отключить guard, чтобы его принять.

## Доставка quiesce без обходного deploy

Доверенный `apps/github_release_runner.py` проверяет Gate/base/head, выполняет
один merge и `checkout_merge(merge)` перед `deploy_exact`. Deploy запускается
отдельным Python-процессом из checkout exact merge. В
`apps/registry_upload_http_entrypoint_hosted_runtime.py::deploy_current_checkout`
стадия `sync` предшествует `autoanswers-schema-preflight`; последняя вызывает
`python3 apps/wb_autoanswers_activation.py prepare-deploy` в синхронизированном
runtime каталоге с включёнными FORCE_OFF и SERVICE_QUIESCE.

Поэтому **само исправление activation может прибыть в том же штатном live PR**:
оно уже установлено до своего первого вызова. Существующий binding вызывается
старым runtime до sync; новый maintenance API не требуется. Отдельная ручная
доставка файла, старт HTTP ради сброса exit status или выключение quiesce не нужны.
Это заключение о порядке кода, не квитанция выполненного live выпуска.

Если меняется сам уже исполняемый управляющий runner или base-owned selector/
check map, сначала нужен отдельный штатный `repo_only` PR, затем новый Gate
зависимого live PR относительно нового main. Изменение controller в live PR не
обновляет уже загруженный Python-код runner. Новые обязательные тесты должны
быть зарегистрированы в доверенной базе до зависимого выпуска; локальный PASS
этого не заменяет.

После подтверждённого hold, до первого sync, проверить остановку старого HTTP:
inactive/dead, MainPID 0, исчезновение прежнего PID/threads и отсутствие старых
code readers, способных импортировать изменяемый Python. Штатный HTTP SIGTERM
принимается только при Result=success, ExecMainCode=2 (CLD_KILLED),
ExecMainStatus=15 и inactive/dead/PID 0. Exit 15 (CLD_EXITED=1), SIGKILL,
crash/failed/unknown и живой PID отклоняются. Завершённые zero-exit и ещё не
запускавшиеся idle units поддерживаются. SIGTERM provider oneshot не считается
успешным drain. Уже остановленный HTTP не перезапускается quiesce автоматически.
Schema/migration выполняются после допуска, не пропускаются.

## Закрытие и отказ

После одного Runner дождаться receipt PR/base/head/merge/runtime SHA,
`deployment_complete=true`, healthy HTTP и адресного RO readback. Marker без
complete недостаточен. Неоднозначный ответ — readback той же операции и
существующее exact-SHA reconciliation, не повторный release.

Затем barrier-restoring, fresh quiet-confirmed continuity и один существующий
`business_data_maintenance_restore_job submit` с exact deployed SHA,
W/P/paused revision и continuity fingerprint. Проверить succeeded,
exact_prior_state_restored и совпадение control signature; warehouse отдельно
повторно не включать. После этого barrier-release и exact raw readback режимов.

При drain timeout не начинать release. State `holding` не означает, что direct
warehouse restore уже разрешён. Сначала прочитать состояние той же операции;
не создавать новый baseline/W/P. После начавшегося sync нельзя автоматически
стартовать старый код по прежнему marker. При restore failure ответственность
владельца сохраняется до terminal результата; новый job ID не является retry.

## Локальная проверка

- `python3 -m unittest apps.wb_autoanswers_activation_test`
- `python3 apps/business_data_maintenance_binding_smoke.py`
- `python3 apps/business_data_maintenance_smoke.py`
- `python3 apps/business_data_maintenance_restore_job_smoke.py`

Binding fixture выполняет реальные prepare/binding/hold и durable state/audit
временной директории; systemd/policy/status являются локальными входами.
Проверяется неизменность baseline для exact restore. Полный live restore и
скорость production этим fixture не доказаны.
