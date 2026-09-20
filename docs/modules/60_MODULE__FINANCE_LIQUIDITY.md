---
title: "Модуль: финансовая ликвидность"
doc_id: "WB-CORE-MODULE-60-FINANCE-LIQUIDITY"
doc_type: "module"
status: "foundation_dormant"
purpose: "Зафиксировать отдельный управленческий казначейский контур счетов, касс и денежных документов без подключения к действующим операционным процессам."
scope: "K01: идентичность модуля, explicit-only авторизация и отдельная CI-группа; без Finance storage, проводок, HTTP, UI, migration, pilot и production writes."
related_modules:
  - "packages/contracts/finance_liquidity.py"
  - "packages/domain/finance_liquidity/"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "ci/checks.json"
source_of_truth_level: "module_canonical"
---

# Финансовая ликвидность

## Статус и граница

Модуль находится в состоянии `foundation_dormant`. K01 добавляет только
версионируемый контракт, права доступа и независимый CI-маршрут. Он не создаёт
финансовую базу, проводки, API, интерфейс, миграцию или production-процесс.
Основной сервис продолжает работать без участия этого модуля.

Это управленческий казначейский контур, а не регламентированная бухгалтерия 1С.
Он не владеет товарным капиталом, Supplier Shipments, CNY ledger,
`finance_raw`, дневным или недельным отчётом WB. Будущие связи с ними проходят
через отдельные подтверждённые коннекторы и outbox-контракты.

Код предметной модели размещается в изолированном namespace
`packages.domain.finance_liquidity` рядом с существующим
`packages.domain.finance_daily_report`; K01 ещё не создаёт предметные сущности.

## Авторизация

Канонические capabilities:

- `finance` — чтение;
- `finance_operate` — чтение и ручные финансовые операции;
- `finance_admin` — чтение, операции и администрирование.

Иерархия направлена сверху вниз:
`finance_admin -> finance_operate -> finance`. Все три ключа входят в
`explicit_only`: ни `admin`, ни bootstrap admin, ни `operator`, ни другая роль
не получает их из role defaults. Supplier не может получить Finance-доступ.
Только явный сохранённый grant проходит нормализацию; более высокий явный grant
добавляет нижние capabilities своей иерархии.

K01 не добавляет финансовый endpoint. Будущий sidecar обязан повторно читать
эти grants из канонического operational auth owner по контракту D02/D03.

## CI и владение

CI-группа `finance_liquidity` запускает
`apps/finance_liquidity_contract_smoke.py`. Она отделена от legacy-группы
`finance`, которая принадлежит WB Finance Weekly и продолжает запускать
`apps/wb_finance_weekly_smoke.py`. Изменения нового namespace не должны
подключать legacy weekly smoke; изменения общего auth-normalizer должны
проверять Finance explicit-only контракт.

## Следующий gate

После K01 допустимо предлагать K02. Сам K01 не разрешает K02, PR merge, deploy,
production, создание storage, выдачу grants или включение нового раздела.
