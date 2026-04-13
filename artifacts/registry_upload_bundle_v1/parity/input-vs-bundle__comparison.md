# Input Vs Bundle Comparison

- `input/config_v2__fixture.json`, `input/metrics_v2__fixture.json` и `input/formulas_v2__fixture.json` выступают как нормализованные per-sheet input artifacts для сборки upload bundle.
- Во всех трёх input docs совпадают `bundle_meta.bundle_version` и `bundle_meta.uploaded_at`; builder принимает bundle только при полной meta-consistency.
- `target/registry_upload_bundle__fixture.json` не добавляет runtime semantics в тело bundle и не смешивает туда `metric_runtime_registry`.
- `config_v2` переносится в bundle без переименований и без скрытых sheet-only semantics.
- `metrics_v2` переносится в bundle без потери `scope`, `calc_type`, `calc_ref`, `format`, `section` и canonical `display_order`.
- `formulas_v2` переносится в bundle как локальный словарь `formula_id -> expression`.
- Проверка `calc_ref` для `metric` и `ratio` идёт через внешний runtime seed `registry/pilot_bundle/metric_runtime_registry.json`, а не через поле внутри bundle.
- В pilot scope bundle подтверждены:
  - `5` SKU;
  - `12` метрик;
  - `2` формулы;
  - все три `calc_type`: `metric`, `formula`, `ratio`.
