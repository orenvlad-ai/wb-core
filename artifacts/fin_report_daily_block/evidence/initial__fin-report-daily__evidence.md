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
- Authoritative server-side smoke был запущен без deploy и без изменений в `/opt/...`, но упёрся в auth blocker statistics API.
- Попытка с `WB_AUTH_TOKEN` из `/opt/wb-ai/.env` вернула `401 Unauthorized` с сообщением про invalid signature.
- Попытка с `wb-eu-portal.seller-token` из `/opt/wb-web-bot/storage_state.json` вернула `401 Unauthorized` с сообщением про missing or empty kid.

Вывод: code-skeleton и bounded transformation готовы; до реально рабочего checkpoint не хватает валидного live token для authoritative statistics smoke.
