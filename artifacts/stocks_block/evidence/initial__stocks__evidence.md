# Initial Evidence

- Legacy samples собраны для `normal-case` и `partial-case`.
- Target samples собраны для `normal-case` и `partial-case`.
- Legacy-source зафиксирован как official source path + current RAW/APPLY semantics.
- Bootstrap `nmId` честно ограничен sample set из уже известных проекту SKU.
- Parity по смыслу подтверждена для `normal-case`.
- Parity по смыслу подтверждена для `partial-case`.
- Artifact-backed transformation может быть проверена локальным smoke-check.
- Coverage guard сохранён в bounded форме: success допустим только при полном coverage requested `nmId` set.
- После implementation выполнен authoritative server-side smoke на `root@178.72.152.177` во временной директории `/tmp/wb-core-stocks-smoke` без deploy и без изменений в `/opt/...`.
- Во временную server-side среду копировались только `apps/stocks_block_http_smoke.py`, `packages/adapters/stocks_block.py`, `packages/adapters/official_api_runtime.py`, `packages/application/stocks_block.py`, `packages/contracts/stocks_block.py`.
- Server-side live smoke запущен через `python3` с `PYTHONPATH=/tmp/wb-core-stocks-smoke`, при этом `WB_TOKEN` передавался только в runtime запуска и не сохранялся в repo-файлах.
- Server-side smoke дал authoritative результат: `normal: ok -> success`, `normal: count -> 2`, `http-smoke-check passed`.

Вывод: `stocks_block` подтверждён как реально рабочий live-source checkpoint на bootstrap sample set; publish guard сохранён в bounded форме через `incomplete` result при неполном coverage requested `nmId` set.
