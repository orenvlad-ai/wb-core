# Parity And Cutover Rules

## Что Такое Parity

Parity означает, что перенесённый модуль воспроизводит требуемый контракт и operator-visible behavior с контролируемым evidence.

Parity не означает:
- тот же внутренний implementation;
- ту же форму репозитория;
- то же runtime location;
- те же incidental logs или helper functions.

Parity означает:
- тот же или осознанно пересмотренный контракт;
- ту же обязательную semantics;
- то же acceptance behavior для missing/partial data;
- объяснимые отличия с reviewable approval.

## Когда Модуль Готов К Cutover

Модуль готов к cutover только если одновременно выполнено всё:
- target contract versioned в `wb-core`;
- legacy contract и semantic deltas задокументированы;
- есть parity tests/fixtures;
- outputs сравнены с legacy на representative samples;
- operational ownership явна;
- есть rollback path;
- заменяемая legacy-dependency достаточно изолирована, чтобы переключение не тянуло collateral rewrite.

## Какой Evidence Обязателен

Минимальный набор evidence:
- contract definition;
- fixture или recorded sample set;
- parity comparison result;
- описание failure-mode behavior;
- runtime observability notes;
- manual review approval.

Inference:
- точное место хранения evidence внутри `wb-core` можно зафиксировать позже.

## Как Не Делать Big-Bang Migration

Правила:
- переносить по одной bounded capability за раз;
- держать legacy рабочим, пока target parity не доказан;
- не сцеплять в один cutover несвязанные модули;
- не требовать однодневной замены table, server и web-source layers одновременно.

## Как Будет Работать Strangler Pattern

Strangler pattern работает так:
- определяется один legacy boundary contract;
- реализуется один replacement module в `wb-core`;
- target сначала запускается в shadow или sidecar mode;
- outputs сравниваются;
- переключается только эта граница, когда evidence достаточно;
- остальной legacy остаётся без изменений.

Предварительный кандидат после foundation:
- web-source snapshot block.

Этот приоритет предварительный, но он согласован с заявленным migration direction и видимым legacy-split между Apps Script consumer, отдельным web-bot capture и server-side snapshot consumers.
