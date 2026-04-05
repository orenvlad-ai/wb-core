# Initial Evidence

- Legacy normal-case sample собран для bootstrap `nmId` set.
- Target normal-case sample собран.
- Legacy-source зафиксирован как official source path + current RAW/APPLY semantics.
- Bootstrap `nmId` честно ограничен sample set из уже известных проекту SKU.
- Parity по смыслу подтверждена для success-case.
- Artifact-backed transformation может быть проверена локальным smoke-check.
- Safe domain-level empty/not-found sample не подтверждён: synthetic unknown `nmId` у этого upstream уводит запрос в `504 Gateway Timeout`, а не в честный domain empty.
- Local live-source preflight нестабилен: на одном прогоне bootstrap sample set проходит, на другом повторно возникает TLS handshake timeout.
- После local preflight выполнен authoritative server-side smoke на `root@178.72.152.177` во временной директории `/tmp/wb-core-sf-period-smoke-v2` без deploy и без изменений в `/opt/...`.
- Во временную server-side среду копировались только `apps/sf_period_block_http_smoke.py`, `packages/adapters/sf_period_block.py`, `packages/adapters/official_api_runtime.py`, `packages/application/sf_period_block.py`, `packages/contracts/sf_period_block.py`.
- Server-side live smoke запущен через `python3` с `PYTHONPATH=/tmp/wb-core-sf-period-smoke-v2`, при этом `WB_TOKEN` передавался только в runtime запуска и не сохранялся в repo-файлах.
- Server-side smoke дал authoritative результат: `normal: ok -> success`, `count: 2`, `http-smoke-check passed`.

Вывод: `sf_period_block` подтверждён как реально рабочий live-source checkpoint на bootstrap sample set; честный empty/not-found case остаётся вне checkpoint, потому что upstream не дал безопасный domain-level empty response.
