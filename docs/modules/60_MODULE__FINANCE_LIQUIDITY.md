---
title: "Модуль: финансовая ликвидность"
doc_id: "WB-CORE-MODULE-60-FINANCE-LIQUIDITY"
doc_type: "module"
status: "cash_release_dormant"
purpose: "Зафиксировать отдельный управленческий казначейский контур счетов, касс и денежных документов без подключения к действующим операционным процессам."
scope: "K01 explicit-only capabilities; K2 cash code, isolated optional sidecar and dormant rollout artifacts. No business activation, real cash data, grants, active route, managed unit, migration of existing data or production write."
related_modules:
  - "packages/contracts/finance_liquidity.py"
  - "packages/domain/finance_liquidity/"
  - "packages/application/finance_liquidity_cash.py"
  - "packages/adapters/finance_liquidity_http.py"
  - "artifacts/finance_liquidity_cash/dormant/"
  - "docs/runbooks/finance_liquidity_cash_dormant_release.md"
  - "ci/checks.json"
source_of_truth_level: "module_canonical"
---

# Финансовая ликвидность

## Статус и граница

Модуль находится в состоянии `cash_release_dormant`. K01 добавил
версионируемый контракт, права доступа и независимый CI-маршрут. K2 добавляет
изолированный cash-код, optional loopback sidecar и неподключённые артефакты
будущего запуска. Текущий deploy не включает unit или nginx routes, не создаёт
cash store/schema/данные и не выдаёт grants. Основной сервис продолжает
работать без синхронной зависимости от этого модуля.

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
Для runtime-пользователя проходит только явно сохранённый grant. Env-bootstrap
principal не имеет строки в таблице пользователей, поэтому ограниченный TEST
pilot использует отдельный repo-owned non-secret access contract: exact
username должен одновременно совпасть с canonical
`WB_CORE_WEB_AUTH_USERNAME`, signed session должна иметь роль `admin`, а
capability должна быть одной из Finance explicit-only. Это совместимость для
одного указанного env principal, а не role fallback; отсутствие, отзыв,
ошибка или несовпадение файла запрещает Finance. Более высокий явный grant
добавляет нижние capabilities своей иерархии.

Sidecar сохраняет descriptor-bound query-only проверку канонического
operational auth owner по контракту D02/D03. Для runtime users он повторно
читает текущую строку grants; для exact env-bootstrap principal после той же
проверки перечитывает общий pilot access contract на каждом запросе. Навигация
`Финансы` → `/finance/` появляется только при
явном grant, `FINANCE_LIQUIDITY_ENABLED=1` и
`FINANCE_LIQUIDITY_READ_ENABLED=1`; supplier не получает ссылку. Эта проверка
не открывает Finance store и не вызывает sidecar, а sidecar снова проверяет
сессию и capability на каждом собственном запросе.

## Устойчивые связи следующих этапов

Кассовый выпуск сохраняет отдельные `account_id`, `document_id`,
`operation_id` и идентификаторы транзакций. Исправление создаёт новый документ
со ссылкой на исходный; идентификатор исходного факта не переиспользуется.
Будущие связи не создают новую копию денежного факта:

| Зарезервированный контракт | Назначение и владелец |
| --- | --- |
| `finance_document_supply_link_v1` — документ ↔ поставка | Контекстная связь N:M без суммы: стабильный Finance `document_id` и внешний `supply_id`. Поставка остаётся у Supplier Shipments. |
| `finance_payment_obligation_allocation_v1` — платёж ↔ обязательство | Распределение уже проведённого платежного документа по обязательствам N:M: локальный `document_id`, внешний `obligation_id`, отдельные стабильные `allocation_id` и revision. Будущий allocation-контур хранит распределения отдельно от денег/P&L; обязательство остаётся у своего владельца. |
| `finance_operation_statement_match_v1` — операция ↔ строка выписки | Сопоставление N:M внутреннего `operation_id`/`document_id` и внешнего `statement_line_id`. Выписка и импорт остаются у будущего import/reconciliation-контура; сопоставление не создаёт проводок или второго платежа. |

Внешний ID всегда квалифицируется пространством источника и владельцем
данных. Будущий импорт использует стабильную пару `(source_namespace,
external_event_id)` внутри конкретного контракта: повтор того же события с
тем же содержимым не создаёт вторую связь, изменение содержимого при прежней
identity конфликтует. Одинаковая строка внешнего ID в разных контрактах не
смешивает их. Денежный документ и его операция остаются локальными
неизменяемыми основаниями; revision распределения не меняет проводки.

Эти три отношения имеют разные основания и жизненные циклы. Кассовый выпуск
не записывает их, не создаёт таблицы связей или outbox, не подключает внешние
источники и не вводит общий
`external_id`, который подменял бы все три вида связи. Детальные wire/storage
контракты распределений и банковского импорта относятся к своим следующим
этапам.

## CI и владение

CI-группа `finance_liquidity` запускает contract, auth, cash, HTTP, browser и
integration smokes. Она отделена от legacy-группы `finance`, которая
принадлежит WB Finance Weekly и продолжает запускать
`apps/wb_finance_weekly_smoke.py`. Изменения нового namespace не должны
подключать legacy weekly smoke; изменения общего auth-normalizer должны
проверять Finance explicit-only контракт.

## Следующий gate

Кодовый rollout и business activation разделены. Путь активации, включая
recovery plan и backup существующих невосстановимых данных, описан в
`docs/runbooks/finance_liquidity_cash_dormant_release.md`. После явного
разрешения owner scope охватывает кассы, суммы, даты и explicit grants;
штатный release train для этого не меняется.
