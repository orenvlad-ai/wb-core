---
title: "Модуль: wb_spp_tester_block"
doc_id: "WB-CORE-MODULE-42-WB-SPP-TESTER-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать production-контракт ручного инструмента `Цены → Проверка СПП`: только заданные оператором цены, authenticated buyer readback и обязательное восстановление seller tuple."
scope: "Server-owned manual live check одного nmID и списка из 1–6 цен. Адаптивный диапазон, plan/preview, threshold/refinement и ежедневное расписание удалены. Buyer login/recovery/noVNC остаются только в централизованных настройках."
source_basis:
  - "packages/contracts/wb_spp_tester.py"
  - "packages/application/wb_spp_tester.py"
  - "packages/application/wb_buyer_session.py"
  - "packages/adapters/wb_buyer_session.py"
  - "packages/adapters/wb_prices_management.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
related_modules:
  - "41_MODULE__WB_PRICES_MANAGEMENT_BLOCK.md"
  - "35_MODULE__SPP_PROXY_BLOCK.md"
related_tables:
  - "sheet_vitrina_v1_source_health_status"
related_endpoints:
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/start"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/status"
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/restore"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/history?limit=...&cursor=..."
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/buyer-session/check"
  - "GET /v1/sheet-vitrina-v1/settings/sources-sessions"
related_runners:
  - "apps/wb_spp_tester_smoke.py"
  - "apps/wb_spp_tester_browser_smoke.py"
  - "apps/wb_buyer_session_recovery.py"
  - "apps/wb_buyer_session_smoke.py"
related_docs:
  - "docs/modules/41_MODULE__WB_PRICES_MANAGEMENT_BLOCK.md"
  - "docs/modules/35_MODULE__SPP_PROXY_BLOCK.md"
  - "docs/architecture/09_official_api_secret_boundary.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Контракт заменён на короткий ручной запуск exact ordered list из 1–6 цен. Удалены диапазон, adaptive plan/refinement/threshold, anonymous-control presentation, schedule API, due runner и systemd timer."
---

# 1. Идентификатор и статус

- `module_id`: `wb_spp_tester_block`
- `family`: `sheet_vitrina_v1/operator/official-api/prices/spp-test`
- `status_main`: active production
- `status_write_path`: guarded backend-only; disabled unless both `WB_SPP_TEST_ENABLED=true` and `WB_PRICES_WRITE_ENABLED=true`

# 2. Product Contract

Оператор выбирает один активный товар и вводит ровно от одной до шести положительных денежных цен. Порядок и повторяющиеся значения значимы. Backend не добавляет min/mid/max, не сортирует, не дедуплицирует и не выбирает уточняющие точки.

Экран содержит только:

1. постоянно видимый статус exact authenticated-buyer-price capability: `Проверяем`, `Готов`, `Разлогинен` или `Ошибка`, плюс повторная проверка;
2. выбор SKU/nmID;
3. выбор количества цен `1..6`;
4. ровно выбранное число пронумерованных обязательных полей;
5. одну основную кнопку `Старт проверки`;
6. компактный текущий результат;
7. компактную server-owned историю newest-first;
8. внизу последние десять полезных sanitized технических событий.

Login, recovery, launcher и noVNC не дублируются: ими владеет `Настройки → Источники и сессии`.

# 3. API

## 3.1 Start

`POST /v1/sheet-vitrina-v1/prices/spp-test/start` принимает:

```json
{
  "nmID": 210183919,
  "price_count": 2,
  "prices": [810, 800.50],
  "confirm_live_price_change": true,
  "restore_baseline": true
}
```

Правила:

- `prices` — ordered list длиной `1..6`;
- `price_count`, если передан, точно равен длине списка;
- каждое значение обязательно, конечно, больше нуля и имеет не более двух знаков после запятой;
- значения не исправляются и не округляются молча;
- `confirm_live_price_change=true` и `restore_baseline=true` обязательны;
- browser и backend выполняют свежий buyer capability preflight непосредственно на Start; backend preflight авторитетен;
- capability failure возвращает короткую ошибку до baseline capture и до любого seller write.

Response содержит compact job и `log_events`; raw upstream payloads, headers, paths, fingerprints и внутренний timeline не публикуются.

## 3.2 Status, restore и history

- `GET .../status[?job_id=...]` возвращает current/latest compact job, только реально активный/unrestored `active_job` и последние десять log events.
- `POST .../restore` с `job_id` и `confirm_restore=true` — emergency path для реально недоказанного восстановления.
- `GET .../history` возвращает newest-first summaries с bounded `limit=1..50` и opaque keyset cursor. Старые `jobs/*.json` читаются совместимо, но наружу отдаются только время, nmID, итог, restore flag и компактные per-price results.
- Отдельного history-detail/raw-json endpoint нет.

Удалены endpoints `baseline`, `plan`, `schedule` и `history/{job_id}`.

# 4. Buyer Capability

Открытие подтаба автоматически вызывает exact read-only `check_spp_capability`. Зелёный статус требует одновременно:

- текущий persistent-profile auth proof;
- `valid=true`;
- `capability_valid=true`;
- успешную возможность получить authenticated buyer price.

Generic browser-session presence не считается готовностью. Tester не запускает automatic recovery. Перед каждой measurement write backend повторяет тот же capability preflight. Если preflight перед ценой не проходит, эта цена не записывается, дальнейшие цены не запускаются, а уже изменённая seller tuple восстанавливается.

После seller readback authenticated buyer price принимается только по двум последовательным идентичным read-only наблюдениям с совместимыми destination/payment context и тем же fingerprint либо эквивалентным persistent-profile proof. Anonymous/public price не участвует в этом инструменте и не является fallback.

# 5. Baseline и safety

До первой записи и только после успешного Start-preflight backend захватывает fresh baseline:

- `price`;
- `discount`;
- `discountedPrice`;
- `editableSizePrice`;
- quarantine;
- присутствие nmID в active server-owned nomenclature.

Start fail-closed при `editableSizePrice=true`, quarantine, неполной tuple или неактивном nmID. Один `execution.lock` и один active/unrestored pointer запрещают overlap между run и emergency restore.

Для каждой введённой цены:

1. повторить buyer capability preflight;
2. сохранить текущий seller discount и вычислить необходимый integer seller `price`;
3. выполнить guarded WB upload;
4. дождаться финального upload status;
5. прочитать фактический seller `discountedPrice`;
6. проверить quarantine;
7. получить stable authenticated buyer price;
8. рассчитать СПП по фактическому seller readback:

`spp = (seller_discounted_price - authenticated_buyer_price) / seller_discounted_price`.

UI показывает процент, но API хранит ratio. Строка результата содержит только target, фактическую seller discounted price, buyer price, СПП и короткий status/error.

Любая ошибка останавливает последовательность. Следующая пользовательская цена не запускается.

# 6. Restore и terminal semantics

Restore обязателен после успеха и любой ошибки. Для большого обратного изменения допустимы bounded bridge steps; финальная истина всегда fresh readback.

Успешный terminal job требует одновременно:

- exact integer `price` baseline match;
- exact integer `discount` baseline match;
- exact kopeck `discountedPrice` baseline match;
- quarantine absent.

Только после этого job получает `complete` при успешных измерениях либо `failed`/ `interrupted_restored` при контролируемой ошибке, current pointer очищается и execution lock освобождается. `manual_restore_required` допустим только когда fresh seller readback не доказал restore либо quarantine/readback не позволяет доказательство. Старый proof никогда не заменяет свежую проверку.

Buyer availability не является частью seller restore proof: потеря buyer session не должна мешать обязательному восстановлению seller tuple.

# 7. Runtime State

Server-owned state:

- `sheet_vitrina_v1_prices/spp_tests/current_job.json`;
- `sheet_vitrina_v1_prices/spp_tests/jobs/{job_id}.json`;
- `sheet_vitrina_v1_prices/spp_tests/audit.jsonl`;
- `sheet_vitrina_v1_prices/spp_tests/execution.lock`.

`current_job.json` содержит только active или seller-unrestored job. TTL сам по себе не очищает pointer. Orphan reconciliation возможен лишь когда OS lock свободен и fresh seller tuple/quarantine readback выполнен.

`schedule.json`, schedule tick runner и SPP systemd service/timer удалены. SPP tester больше не является процессом автообновлений в Settings.

# 8. History и technical log

History сохраняет canonical jobs, но UI/API summary не показывает evidence, lifecycle diagnostics, raw JSON, request budgets или upstream payloads.

Technical log строится из allowlisted audit events и всегда ограничен последними десятью полезными событиями. Каждая строка имеет только `time`, короткий `stage` и короткий `message`. Sanitizer исключает secrets, cookies, authorization headers, token-like keys, внутренние paths и raw payload presentation.

# 9. Verification Contract

Обязательные repo checks:

- `python3 apps/wb_spp_tester_smoke.py`;
- `python3 apps/wb_spp_tester_browser_smoke.py`;
- `python3 apps/wb_prices_management_smoke.py`;
- `python3 apps/wb_prices_management_browser_smoke.py`;
- применимые public-route, hosted-runtime и maintenance regressions.

Application/browser smokes обязаны доказывать 1 и 6 полей, строгую валидацию, exact ordered list, fresh Start preflight, logged-out zero writes, progressive results, СПП по actual readback, compact history, десять sanitized events, success restore, mid-run stop+restore, честный `manual_restore_required` и отсутствие прежних controls/contracts.

После deploy LOOP UI Flow дополнительно требует реальный bounded run только после захвата точной baseline tuple. Evidence фиксирует nmID, введённые цены, job id, per-price results, screenshot и fresh post-run seller tuple/quarantine/active-lock proof. Техническое завершение не заменяет owner acceptance.
