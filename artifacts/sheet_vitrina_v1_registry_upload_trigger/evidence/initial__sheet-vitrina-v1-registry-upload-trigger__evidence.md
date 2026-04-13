# Initial Evidence: sheet_vitrina_v1_registry_upload_trigger_block

- Доказан bound Apps Script слой, который:
  - создаёт/поддерживает листы `CONFIG`, `METRICS`, `FORMULAS`;
  - собирает из них канонический registry upload bundle;
  - хранит `endpoint_url` и последний upload result в control block `CONFIG!H:I`;
  - умеет вызывать существующий HTTP entrypoint через `UrlFetchApp`.
- Локальный smoke подтверждает:
  - что bundle, построенный из bound таблицы, совпадает с каноническим target fixture;
  - что именно этот bundle принимается существующим HTTP entrypoint и materialize-ит current truth в runtime DB;
  - что duplicate path возвращает rejected result и не двигает current state.
- Допущение bounded шага:
  - автоматический cloud Apps Script -> local `http://127.0.0.1` вызов в repo не используется как доказательство, потому что live HTTP entrypoint ещё не deploy-нут и не опубликован вовне;
  - для этого блока достаточно доказать sheet-side bundle assembly + thin HTTP wiring + живой server-side accept path;
  - фактический manual/live trigger начинает работать без изменения кода, как только в `CONFIG!I2` указывается внешне достижимый URL уже materialized entrypoint.
