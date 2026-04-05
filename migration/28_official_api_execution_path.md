# Official API Execution Path

## Зачем Нужен Единый Path

`prices_snapshot_block` показал, что для official-API family основной риск уже не в module logic, а в execution environment:
- secrets могут быть доступны в одной среде и отсутствовать в другой;
- upstream может быть reachable из server-side runtime и недоступен локально;
- live-source smoke без общей runtime boundary даёт ad hoc поведение и плохо переносится между модулями.

Поэтому следующим official-API модулям нужен единый minimal execution path, а не отдельные module-specific обходы.

## Что Входит В Execution Path

Минимальный execution path включает:
- secrets boundary;
- runtime boundary;
- live-source smoke path;
- явное разделение local execution и server-side execution.

## Почему Local И Server-Side Нужно Считать Разными Средами

Эти среды считаются разными, потому что:
- набор доступных секретов может отличаться;
- сетевой маршрут до official upstream может отличаться;
- transport/reachability failures в local среде не доказывают server-side failure;
- успешный local env-check не заменяет server-side live execution evidence.

Следствие: local smoke нужен для проверки runtime boundary и reachability attempt, а server-side execution нужен для финального live-source checkpoint.

## Какой Secrets Layer Считается Правильным

Правильный слой секретов:
- repo хранит только names, shape и usage boundary;
- secret values живут только вне Git;
- module code не знает, откуда пришёл секрет;
- runtime boundary читает env и передаёт модулю уже готовую runtime config.

## Какой Path Считается Базовым

Базовый path для official-API модулей:
1. module adapter запрашивает runtime config через shared helper;
2. shared helper валидирует required env и timeout shape;
3. общий smoke entrypoint проверяет env presence и reachability upstream;
4. local execution используется только как preflight;
5. server-side execution используется как authoritative live-source path для checkpoint.

Этот path является базовым для следующих official-API модулей, пока в `wb-core` не появится другой явно зафиксированный runtime slice.
