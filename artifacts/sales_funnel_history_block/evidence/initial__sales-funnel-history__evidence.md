# Initial Evidence

- Legacy samples собраны для `normal-case` и `empty-case`.
- Target samples собраны для `normal-case` и `empty-case`.
- Legacy-source зафиксирован как official source path + current RAW/APPLY semantics.
- Bootstrap `nmId` честно ограничен sample set из уже известных проекту SKU.
- Parity по смыслу подтверждена для `normal-case`.
- Parity по смыслу подтверждена для `empty-case`.
- Artifact-backed transformation может быть проверена локальным smoke-check.
- Local live-source preflight на этом Mac повторно упирается в TLS handshake timeout и не считается authoritative.
- После implementation выполнен authoritative server-side smoke на `root@178.72.152.177` во временной директории `/tmp/wb-core-sfh-smoke` без deploy и без изменений в `/opt/...`.
- Во временную server-side среду копировались только `apps/sales_funnel_history_block_http_smoke.py`, `packages/adapters/sales_funnel_history_block.py`, `packages/adapters/official_api_runtime.py`, `packages/application/sales_funnel_history_block.py`, `packages/contracts/sales_funnel_history_block.py`.
- Server-side live smoke запущен через `python3` с `PYTHONPATH=/tmp/wb-core-sfh-smoke`, при этом `WB_TOKEN` передавался только в runtime запуска и не сохранялся в repo-файлах.
- Server-side smoke дал authoritative результат: `normal: ok -> success`, `normal: count -> 140`, `http-smoke-check passed`.

Вывод: `sales_funnel_history_block` подтверждён как реально рабочий live-source checkpoint на bootstrap sample set.
