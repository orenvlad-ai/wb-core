# Initial Evidence

- Legacy samples собраны для `normal-case` и `storage-total`.
- Target samples собраны для `normal-case` и `storage-total`.
- Legacy-source зафиксирован как `reportDetailByPeriod(period=daily)` + current RAW/APPLY semantics.
- Bootstrap `nmId` честно ограничен sample set из уже известных проекту SKU.
- Special row `nmId=0` отдельно зафиксирован для total storage fee.
- Parity по смыслу подтверждена для normal-case.
- Parity по смыслу подтверждена для storage-total.
- Artifact-backed transformation может быть проверена локальным smoke-check.
- Локальный artifact-backed smoke пройден: `normal -> success`, `storage_total -> success`.
- После замены server-side secret layer повторно выполнен authoritative server-side smoke на `root@178.72.152.177` во временной директории `/tmp/wb-core-fin-report-daily-smoke-20260411` без deploy и без изменений в `/opt/...`.
- Во временную server-side среду копировались только `apps/fin_report_daily_block_http_smoke.py`, `packages/adapters/fin_report_daily_block.py`, `packages/adapters/official_api_runtime.py`, `packages/application/fin_report_daily_block.py`, `packages/contracts/fin_report_daily_block.py`.
- Server-side live smoke запущен через `python3` с `PYTHONPATH=/tmp/wb-core-fin-report-daily-smoke-20260411`, при этом актуальный `WB_TOKEN` брался только из `/opt/wb-ai/.env` в runtime запуска и не сохранялся в repo-файлах.
- Server-side smoke дал authoritative результат: `normal: ok -> success`, `normal: count -> 2`, `storage_total: ok -> 0.0`, `http-smoke-check passed`.

Вывод: `fin_report_daily_block` подтверждён как реально рабочий live-source checkpoint на bootstrap sample set; прежний paused auth-blocker снят заменой server-side `WB_TOKEN`.
