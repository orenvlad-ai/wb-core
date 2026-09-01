# Migration 181: Change Registry activation writer serialization

## Failure family

An exact-SHA trusted deployment activation could race an already running FBS
or warehouse publication in the shared operational SQLite generation. The
observer's bounded SQLite busy wait then expired even though the existing
writer was legitimate, leaving activation terminal `failed` and the deployment
incomplete.

## Corrected boundary

Every Change Registry observer schema, admission, result and failure-evidence
write now owns the canonical warehouse functional writer lock. Acquisition is
unchanged: all Prices/Ads WB GET calls run after admission releases that lock
and before result persistence reacquires it. The seller lease, exact activation
identity, SQLite transaction boundaries, zero WB mutation contract and terminal
failed-replay rule are unchanged.

The production-shaped smoke holds the exact writer lock while an activation
starts. It proves the activation waits without opening source acquisition,
then completes after release, and independently proves the acquisition callback
owns a non-reentrant writer-lock state.

## Verification

```bash
python3 apps/change_registry_observer_smoke.py
python3 apps/change_registry_baseline_engine_smoke.py
python3 apps/change_registry_source_acquisition_smoke.py
```
