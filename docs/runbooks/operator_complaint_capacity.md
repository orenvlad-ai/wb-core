# Место для сохранения результата complaint job

Native `JsonFileFeedbacksComplaintsSubmitJobStore.start` под прежним lock сохраняет
`completion_capacity_bytes=2097152` до `admitted_thread`. Admission считает размер
всего JSON и ещё не использованную часть каждого незавершённого резерва. Прежние
ограничения 8 MiB / 5000 records сохраняются. Отказ не сохраняет новую identity и
не запускает worker/provider. Keyed records не вытесняются.

Резерв покрывает весь конечный native контракт: selected20, attempts20, skips20,
events200, существующие пределы каждого текстового поля, пять native aggregate
counters, signed-64-bit числа и source operands до16 KiB. Максимум учитывает
UTF-8 и JSON escaping. Полные диагностические provider reports остаются в
прежних report artifacts; receipt содержит исходные native bounded attempts,
events и пять aggregate counters. Авторитетные attempts не сокращаются ради
места. User operands проверяются до сохранения identity и запуска thread.

Промежуточный `status=error` без `finished_at` сохраняет резерв и остаётся busy:
это может быть неоднозначный external outcome до последнего native report.
Резерв освобождается после durable terminal result. После этого native record
не переоткрывается и не заменяется новым proof; exact повтор читает прежний ID.
Частичная подача и pending readback сохраняют свои прежние значения.

Для старого active job без reservation `_run` сохраняет
`complaint_completion_capacity_not_reserved` до вызова runner, сохраняя прежние
identity/manifest. Это не доказывает отсутствие прежнего external write: его
результат требует точного native readback, повторной отправки нет. Уже
переполненный или повреждённый до исправления store требует owner repair;
недостаток места не разрешает новое внешнее действие. Развёртывание этого кода
не создаёт резерв задним числом для уже выполняющегося старого процесса.

`apps/operator_complaint_capacity_smoke.py` проверяет actual native store/worker
с temporary inventory, held thread и fake provider. Worst-case normalized
terminal shape меньше2 MiB при многобайтных символах и JSON escapes. Near-cap
accepted job сохраняет все20 attempts /200 events после fake external response;
отдельно проверяются отказ до admission, 5000 retained keys, старый active job,
неоднозначный промежуточный error, restart и exact повтор без нового provider.
