# Приём и восстановление задания СПП

## Граница

Ручная проверка сохраняет исходную команду в существующем native
`sheet_vitrina_v1_prices/spp_tests/jobs/*.json` до buyer preflight, baseline
и первой записи цены. Это приём задания, а не подтверждение измерения или
восстановления цены. Новый worker, очередь и таблица не создаются.

Остаются действующие enable-флаги, строгий список 1–6 цен, confirmation,
консервативный карантинный порог, active/unrestored pointer, execution.lock,
native worker, Change Registry и обязательное восстановление baseline.
Legacy POST без request_id сохраняет прежний контракт и не получает
придуманную операторскую квитанцию.

## Исходная команда

Текущая форма использует существующий `POST /v1/sheet-vitrina-v1/prices/spp-test/start`:

```json
{
  "request_id": "spp-start:client_generated_unique_id",
  "nmID": 210183919,
  "price_count": 2,
  "prices": [810, 800.5],
  "confirm_live_price_change": true,
  "restore_baseline": true
}
```

Сервер берёт native actor, человекочитаемого автора, seller/account scope из
действующей сессии и native источника. Эти данные не принимаются из body.
`job_id` — детерминированный digest всего server scope и request_id. Один ID
в двух principal scopes не заменяет чужое задание. Внутри одного scope
другие исходные параметры с прежним ID дают conflict.

Immutable acceptance хранит исходную команду, digest, job_id и accepted_at.
После preflight отдельно сохраняются неизменяемые baseline и measurement plan.
Native сохранение использует atomic replace, file fsync и directory fsync.
Приём удерживает execution.lock; admission capacity — 10000 native job files.
Старые ID не вытесняются. Capacity/invalid command отклоняются до source save;
preflight rejection после save сохраняет квитанцию needs_attention и zero write.

## Неопределённый ответ

Восстановление использует только:

`GET /v1/sheet-vitrina-v1/prices/spp-test/status?request_id=<original-id>`

Это exact scoped read без запуска worker, buyer/seller API и orphan reconciliation.
Неполученный/404/400/5xx GET не доказывает отсутствие принятого задания. ID
остаётся в браузере; второй POST автоматически не отправляется. Повторный POST
того же native ID тоже возвращает сохранённое задание и `recovered=true` без
повторения preflight или worker. Новый source save возвращает `recovered=false`.

Форма берёт неизменяемый снимок nmID/ordered prices непосредственно при Start,
до WebLock/await. Под WebLock она сохраняет ID/body/digest в localStorage до
buyer preflight и единственного POST. Storage key включает opaque server scope.
Две вкладки, reload и закрытая страница читают один ID. При недоступном WebLock
или storage форма останавливается до POST. Неизвестные ошибки не снимают fence.
Проверяемые precommit codes и точный maintenance 423 позволяют снять только
свою ссылку. Сохранённая подтверждённая ссылка остаётся до явной «Новой проверки»;
такая команда получает новый ID.

## Остановившийся процесс

GET не восстанавливает цены. Он может read-only открыть уже существующий
execution.lock и проверить свободный OS lock с повторным чтением того же job.
Если процесс остановился до сохранения baseline, receipt показывает прерывание
до записи. Явная «Новая проверка» разрешает отдельный новый Start; native Start
под своим lock завершает прежнюю preflight-запись без provider write.

Если baseline уже сохранён, но runner остановился, receipt требует проверки
восстановления и показывает существующее explicit emergency restore. Это только
restore исходного задания, без возобновления измерений. Неизвестный ответ restore
читается по тому же ID. Автоматической повторной отправки нет.

## Фактический результат

`Принято / Задание сохранено` подтверждает только durable intent.
Completed требует exact immutable baseline/plan, всех заказанных результатов,
final seller tuple/quarantine proof и каждого native Change Registry child:
source, actor, account, nmID, original price, discount, seller price, before/after,
job/stage identity, latest confirmed или resolved-confirmed wb_readback,
readback digest, exact receipt reference и совместимого времени.
Одни native complete/succeeded/restore flags не дают completed. Основной badge,
строки результатов и history показывают неподтверждённый результат без зелёного
завершения. Частичный успех не скрывает неудачное восстановление.

Общий GET-only журнал проецирует source как `domain=spp_test_jobs`. Author/account
и исходные цены остаются native; foreign principal не попадает в count/search/
detail. Существующие WB price/restore children отдельно остаются в native
Change Registry и его прежней проекции `spp_test`. Права обоих видов следуют
действующему доступу к prices source. Новые права не добавляются.

## Локальная проверка

Используются только temporary runtime, synthetic seller/buyer providers,
настоящие native worker/Change Registry, локальный HTTP и Chromium:

```bash
PYTHONPATH=. python3 -W ignore::ResourceWarning apps/operator_spp_job_recovery_smoke.py
PYTHONPATH=. python3 apps/wb_spp_tester_smoke.py
PYTHONPATH=. python3 apps/wb_spp_tester_browser_smoke.py
PYTHONPATH=. python3 apps/wb_prices_management_smoke.py
PYTHONPATH=. python3 apps/wb_prices_management_browser_smoke.py
```

Новый runner называется `apps/operator_spp_job_recovery_smoke.py`. Для CI нужны
только существующие APSW/openpyxl/Playwright Chromium зависимости. CI/routing и
выпуск принадлежат владельцу интеграции. Эти проверки не являются разрешением
на production запуск СПП или изменение реальных WB цен.
