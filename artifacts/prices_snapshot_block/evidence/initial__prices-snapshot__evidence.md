# Initial Evidence

- Legacy samples зафиксированы для `normal-case` и `empty-case`.
- Target samples зафиксированы для `normal-case` и `empty-case`.
- Legacy-source зафиксирован как official source path + current RAW/APPLY semantics.
- Bootstrap `nmId` честно ограничен sample set из уже известных проекту SKU.
- Parity по смыслу подтверждена для `normal-case`.
- Parity по смыслу подтверждена для `empty-case`.
- Artifact-backed transformation может быть проверена локальным smoke-check.
- `WB_TOKEN` найден в окружении и live-source path повторно прогнан.
- Live-source smoke-check не дошёл до HTTP auth/payload phase: TLS handshake к `https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter` стабильно истекает по таймауту.
- После local transport-failure выполнен authoritative server-side smoke на `root@178.72.152.177` во временной директории `/tmp/wb-core-prices-smoke` без deploy и без изменений в `/opt/...`.
- Во временную server-side среду копировались только `apps/prices_snapshot_block_http_smoke.py`, `packages/adapters/prices_snapshot_block.py`, `packages/adapters/official_api_runtime.py`, `packages/application/prices_snapshot_block.py`, `packages/contracts/prices_snapshot_block.py`.
- Server-side live smoke запущен через `python3` с `PYTHONPATH=/tmp/wb-core-prices-smoke`, при этом `WB_TOKEN` передавался только в runtime запуска и не сохранялся в repo-файлах.
- Server-side smoke дал authoritative результат: `normal: ok -> success`, `empty: ok -> empty`, `http-smoke-check passed`.

Вывод: `prices_snapshot_block` подтверждён как реально рабочий live-source checkpoint в server-side среде; local transport-problem признан средовым отличием, а не blocker'ом модуля.
