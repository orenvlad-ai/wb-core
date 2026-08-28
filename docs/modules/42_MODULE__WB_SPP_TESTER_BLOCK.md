---
title: "Модуль: wb_spp_tester_block"
doc_id: "WB-CORE-MODULE-42-WB-SPP-TESTER-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать production-контракт ручного инструмента `Цены → Проверка СПП`: только заданные оператором цены, authenticated buyer readback и обязательное восстановление seller tuple."
scope: "Server-owned manual live check одного nmID и списка из 1–6 цен. Адаптивный диапазон, plan/preview, threshold/refinement и ежедневное расписание удалены. Buyer login/recovery/noVNC остаются только в централизованных настройках."
source_basis:
  - "packages/contracts/wb_spp_tester.py"
  - "packages/contracts/wb_price_quarantine.py"
  - "packages/application/wb_spp_tester.py"
  - "packages/application/wb_buyer_session.py"
  - "packages/adapters/wb_buyer_session.py"
  - "packages/adapters/wb_prices_management.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
  - "Official WB Seller instruction `Карантин цен`, updated 2026-05-18: https://seller.wildberries.ru/instructions/ru/tj/material/price-quarantine?recommended=true"
  - "Published WB OpenAPI: Prices and Discounts: https://dev.wildberries.ru/docs/openapi/work-with-products"
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
  - "apps/change_registry_internal_writers_smoke.py"
related_docs:
  - "docs/modules/41_MODULE__WB_PRICES_MANAGEMENT_BLOCK.md"
  - "docs/modules/35_MODULE__SPP_PROXY_BLOCK.md"
  - "docs/architecture/09_official_api_secret_boundary.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Manual exact-price flow now exposes the selected SKU's current seller discounted price, retries one generic transient buyer probe once, and applies a shared conservative 1.5x/33.3% inclusive quarantine guard to the exact integer-price conversion before Start and immediately before every measurement write."
---

## Immutable writer events

Every measurement and every restore bridge/final transition has its own stable
`job_id + stage` registry operation. Preparation occurs after the fresh guard
and before exactly one WB upload call. HTTP 429 stops that stage without a
retry. Exact original price, discount and seller-price readback confirms it;
unverifiable readback stays ambiguous while mandatory restore continues under
its own operations. Native SPP JSONL/job state is linked evidence, not replaced.

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
3. уже загруженную current official Prices tuple выбранного SKU: seller `discountedPrice`, а также original `price` и seller `discount`, если они доступны; выбор SKU не создаёт отдельный upstream request;
4. выбор количества цен `1..6`;
5. ровно выбранное число пронумерованных обязательных полей;
6. конкретное предупреждение о risky transition и проценте снижения с disabled Start;
7. одну основную кнопку `Старт проверки`;
8. компактный текущий результат;
9. компактную server-owned историю newest-first;
10. внизу последние десять полезных sanitized технических событий.

Login, recovery, launcher и noVNC не дублируются: ими владеет `Настройки → Источники и сессии`. Exact capability preflight получает authenticated buyer price и session proof одной atomic persistent-profile операцией: отдельный предварительный browser launch только для `/lk` запрещён, потому что он не добавляет proof и повышает риск WB anti-bot challenge. Все обычные authenticated-price probes запускают persistent Chromium как headed browser на отдельном ephemeral Xvfb display под тем же single-flight lock; headless probe для этой capability запрещён, потому что WB может принять его за `security_challenge` сразу после успешного headed recovery proof. HTTP 498/экран `Подозрительная активность` классифицируется как `security_challenge`; централизованный headed recovery удерживает этот экран до автоматического разрешения или действия оператора, а не превращает его в generic `probe_error`.

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
- backend по fresh baseline сохраняет seller discount, преобразует каждую target discounted price в integer seller `price` с `ROUND_HALF_UP`, вычисляет ожидаемую `discountedPrice` в копейках и проверяет baseline→first и каждую соседнюю пару по формуле `next * 1.5 <= previous`;
- любой risky transition, включая exact inclusive boundary, возвращает controlled `422` до job/write; `upload_task` не вызывается;
- browser и backend выполняют свежий buyer capability preflight непосредственно на Start; backend preflight авторитетен и покрывает первую measurement до любой seller write, поэтому первый worker не открывает третий дублирующий Chromium context;
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

Только общий transient `probe_error`/`session_probe_error` получает ровно один fresh retry после короткой bounded паузы (default 1 секунда; `WB_BUYER_CAPABILITY_RETRY_DELAY_SECONDS` ограничен диапазоном 0.1–5 секунд). Explicit `expired`/`logged_out`, `wrong_account`, `login_redirect`, `security_challenge`, recovery/automation-lock busy и другие явные blocking states не retry-ятся. После второй общей ошибки capability остаётся fail-closed и seller writes равны нулю. Наружу и в source-health сохраняются только allowlisted diagnostic category (`navigation_no_response`, `http_status`, `chromium_failure` и эквивалентные bounded категории), число попыток и факт retry; raw browser payloads, cookies, secrets и внутренние paths исключены.

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

WB Seller instruction `Карантин цен`, обновлённая 18.05.2026, задаёт default/strictest enabled threshold 1.5x (33.3%) и категорийные варианты до 1.9x (47.5%). Published OpenAPI всё ещё описывает 3x и не публикует endpoint текущего per-category threshold. Поэтому tester не использует private endpoints и fail-closed применяет единый консервативный exact contract `new_discounted_kopecks * 15 <= previous_discounted_kopecks * 10`.

Для каждой введённой цены:

1. для первой цены использовать только что полученный authoritative backend Start-preflight, а перед каждой следующей ценой повторить fresh buyer capability preflight;
2. сохранить baseline seller discount и вычислить необходимый integer seller `price` и ожидаемую discounted price после округления;
3. непосредственно перед upload заново прочитать seller tuple и quarantine, потребовать exact ожидаемую previous tuple и повторить conservative transition guard; drift, quarantine, unavailable evidence или newly risky transition выполняют zero-measurement-write stop;
4. выполнить guarded WB upload;
5. дождаться финального upload status;
6. прочитать фактический seller `discountedPrice`;
7. проверить quarantine;
8. получить stable authenticated buyer price: два одинаковых fresh read выполняются в одном persistent Chromium context, а не отдельными browser launches;
9. рассчитать СПП по фактическому seller readback:

`spp = (seller_discounted_price - authenticated_buyer_price) / seller_discounted_price`.

UI показывает процент, но API хранит ratio. Строка результата содержит только target, фактическую seller discounted price, buyer price, СПП и короткий status/error.

Любая ошибка останавливает последовательность. Следующая пользовательская цена не запускается.

# 6. Restore и terminal semantics

Restore обязателен после успеха и любой ошибки. Для большого обратного изменения допустимы bounded bridge steps; финальная истина всегда fresh readback.

Каждый planned bridge и непосредственно предшествующий ему fresh seller/quarantine readback проверяются тем же conservative 1.5x contract. Bridge steps уменьшают discounted price bounded шагами существенно меньше 33.3%; risk или недоступное evidence не выполняет опасный restore upload и сохраняет `manual_restore_required`.

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

History сохраняет canonical jobs, но UI/API summary не показывает evidence, lifecycle diagnostics, raw JSON, request budgets или upstream payloads. UI показывает не более трёх кратких результатов одной history-записи и отдельный счётчик оставшихся цен, поэтому совместимые legacy jobs с длинными measurement-массивами не растягивают основной экран. Кнопка аварийного restore видима только для фактического `manual_restore_required=true`; обычный terminal job с доказанным restore не оставляет ложного recovery-action.

Technical log строится из allowlisted audit events и всегда ограничен последними десятью полезными событиями. Каждая строка имеет только `time`, короткий `stage` и короткий `message`. Sanitizer исключает secrets, cookies, authorization headers, token-like keys, внутренние paths и raw payload presentation.

# 9. Verification Contract

Обязательные repo checks:

- `python3 apps/wb_spp_tester_smoke.py`;
- `python3 apps/wb_spp_tester_browser_smoke.py`;
- `python3 apps/wb_prices_management_smoke.py`;
- `python3 apps/wb_prices_management_browser_smoke.py`;
- применимые public-route, hosted-runtime и maintenance regressions.

Application/browser smokes обязаны доказывать 1 и 6 полей, current discounted/original/discount display from loaded rows, строгую валидацию, exact ordered list, safe sequence, baseline→first and within-list risks, exact inclusive 1.5x boundary, integer-price rounding, controlled direct-POST `422` with zero upload, fresh per-write drift stop, fresh Start preflight, one generic transient retry→success, repeated generic zero-write failure, explicit no-retry states, sanitized diagnostic categories, progressive results, СПП по actual readback, compact history, десять sanitized events, conservative restore bridges, success restore, mid-run stop+restore, честный `manual_restore_required` и отсутствие прежних controls/contracts.

После canonical deploy authenticated production UI evidence фиксирует requested/final URL, redirects, отсутствие `5xx`, `DOMContentLoaded`, видимый render, title/body, `pageerror`/fatal console guards и screenshot с current price display и risky-sequence disabled Start. Отдельный direct risky POST доказывает controlled `422` и отсутствие upload evidence. Только затем выполняется один предусмотренный acceptance bounded safe live SPP run: exact baseline tuple до старта, заранее доказанная safe sequence, job id/per-price results, обязательный restore и fresh post-run seller tuple/quarantine/active-lock proof. Техническое завершение не заменяет owner acceptance.
