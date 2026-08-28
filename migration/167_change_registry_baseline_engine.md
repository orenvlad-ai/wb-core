# Migration 167 — dark change-registry baseline engine

Status: additive dark internal engine only.

This bounded migration adds
`packages/application/change_registry_baseline_engine.py`, its deterministic
smoke and module 56. It uses only the already deployed migration-165 tables and
the sanitized migration-166 acquisition result; there is no new production
table, column, manifest or automatic runtime invocation.

An explicit call transactionally persists one checkpoint plus normalized
observations/incidents and, only after a previous joint complete baseline,
proven `checkpoint_diff` facts with checkpoint links. First complete is baseline
only. Partial/failed evidence cannot advance or create facts. Exact repeat is
idempotent; a failed transaction leaves no partial checkpoint or facts.

Campaign creation is the existing campaign-state atomic field transition from
reserved proven `absent` to the exact observed state. The engine proves absence
only from the immediately previous complete Ads manifest; identity ambiguity,
legacy count-only evidence, partial/missing evidence and disappearance never
become creation/deletion.

Verification:

```bash
python3 apps/change_registry_baseline_engine_smoke.py
python3 apps/change_registry_source_acquisition_smoke.py
python3 apps/change_registry_smoke.py
```

The test registry maps the engine/module/migration paths to the existing Web
Vitrina suite and `live_runtime`. Deploy installs dark code only. No scheduler,
source call, registry row, production engine invocation, WB write or production
mutation manifest is added.
