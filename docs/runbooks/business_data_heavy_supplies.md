# Shared heavy admission: Supplies

This integration covers authoritative Supplies collection and enrichment. It does
not activate a schedule/profile, change FBS freshness, or claim all producers are
serialized. Existing formulas, storage schemas, browser/domain locks, source
identity and uncertain-run policy stay unchanged.

## Boundaries

| Entry | Admission boundary and lifetime |
| --- | --- |
| `sync_supplies`, `sync_functional_sources` | Before source fetch, checkpoint/debit and the durable sync receipt; through source save and existing reconciliation. A bound warehouse/cycle stage reenters only its actual live thread owner. |
| `run_full_backfill` | Before state/cursor scans and durable run creation, through existing pagination, source facts and terminal receipt. |
| `collect_transit_costs`, `collect_all_due_transit_costs` | Before candidate/uncertain-run scans and acceptance; through source fetch, saved evidence, economics callback and terminal outcome. |
| Async backfill/transit | Independent maintenance SH then nonblocking heavy EX, before domain locks/scans/durable acceptance/thread construction. Transfer the actual lease to one admitted worker; close after its terminal/finally work. |
| Supply detail GET / `get_supply` | The existing `_ensure_supply_detail_record` enrichment acquires heavy before checkpoint, debit, network or upsert, including already cached detail. HTTP keeps a complete saved card available with explicit deferred enrichment through a coherent query-only cache window. Absent/incomplete cache retains 409 busy or 423 maintenance. Ordinary successful enrichment keeps its shape and behavior. |
| Backfill CLI, composition diagnostics `--live-fetch` | Guard before runtime/block constructors; cached composition diagnostics remain unchanged. Busy emits a bounded truthful outcome without a success claim. |
| HTTP incremental sync + transit | One root owner across both existing sequential phases, so another producer cannot interpose heavy admission after source acceptance. |
| HTTP `sync` with `mode=full_backfill` | Both existing phases execute sequentially in the same async child owner. Request normalization is the previous sync normalization; the dedicated backfill route/CLI remains source-only. |

Cached list, overlay, coverage and status accessors acquire no heavy lease. Their
existing cache infrastructure semantics are unchanged; this is not a refactor of
legacy SQLite read/schema initialization.

## Cached detail during busy/maintenance

The card fallback reads only saved detail+goods operands. Missing optional package
operands retain `None`; no missing data is converted to zero. It uses the existing
`window_read_context` and direct SELECTs, not legacy runtime loaders that can
initialize schema or use rw connections. Record, transit cost and FF overlay share
one pinned query-only operational snapshot. The overlay is the existing static
`approved_overlay_in_connection` calculation; the normal provider/FF writer is
never called. Missing/incompatible cache or overlay schema keeps the actual
busy/maintenance error rather than fabricating an empty successful card.

The returned `meta.enrichment` is `{status: deferred, reason: heavy_busy|maintenance,
attempted: false}`. Existing source hashes, enrichment timestamp/status, raw
payloads and saved values are preserved. HTTP checks this readonly fallback
before the maintenance write guard can emit 423; an acquisition race at the normal
enrichment boundary uses the same fallback. Ordinary successful enrichment is
unchanged and remains admitted through its existing effects.

## Async startup and combined receipt

Startup uses the reviewed `admitted_thread` and shared helper's no-start proof.
Constructor or proven no-start failure/cancellation terminalizes the existing
receipt and releases both leases. Native-spawn uncertainty preserves the leases
before bootstrap and through the possible child. The supported proof is the
existing narrow CPython `_started`/`_limbo` predicate on Unix with `flock`; missing
proof is never permission to resubmit a source operation. Worker cancellation
releases leases in finally, retaining an interrupted/uncertain durable receipt.

Only the combined HTTP path uses the private terminal deferral control. Backfill
first persists its normal source facts, completeness and cursor. The same run
then remains `running`, `phase=backfill_completed_awaiting_transit`, with no
`completed_at`. Its bounded logs retain a source status/phase proof. Success for
the whole request requires both source success and transit `complete`. A degraded
tail gives `partial`/`transit_cost_collection_degraded`; an exception gives
`failed`/`transit_cost_collection_failed`, with bounded phase/run-ID evidence.
The tail never resets saved source facts/cursors or reissues backfill.

A crash between phases leaves that active receipt uncertain. A subsequent start,
after acquiring an otherwise idle heavy lease, returns `unknown`, `accepted=false`
for the same run; it cannot silently resend backfill. Separate explicit transit
collection can consume the saved cache without another full source collection.
No automatic tail retry or new queue is introduced. Existing July25/Oct3 stale
transit records remain `unknown` and cannot trigger a source resend.

## Remaining scope and read-only release checks

Direct supplier/CNY/FF/nomenclature after-save continuations and direct functional
reconciliation are separate integrations. Standalone history and total rolling14
completion, all other heavy producer coverage and dispatch/profile wiring remain
activation dependencies. This candidate exports no all-covered readiness proof.
The unused supplemental auto-transit helper keeps its existing bounded
`failed_to_start` outcome on a rejected start; it has no current production call
site. The canonical warehouse stage collects transit synchronously under its
owner. Short document/source commits are not placed under this long lease.

Offline evidence is `apps/business_data_heavy_supplies_smoke.py`: spawned-process
contention, real private receipts/SQLite and real SH/EX locks with fake services,
owned nesting versus another thread, pre-accept rejection, CLI constructor
boundaries, cached HTTP200+deferred versus absent-cache409/423, readonly DB/WAL/SHM invariance, coherent overlay under concurrent commit, startup/cancellation/uncertain native spawn, worker
finally lifetime, combined order/terminal failure and restart without resend.

After a separately authorized release, use read-only `heavy_admission_status`
and `admission_idle`, cached sync/transit status and query-only run/source-state
readback. Correlate existing IDs, phase proofs, source completeness/cursors and
terminal timestamps. An active combined run must not claim whole completion
before transit. Existing unknown browser runs must remain unknown without a new
fetch/run ID. Do not dispatch collection or retry uncertain work to verify it.
First cutover still requires maintenance plus separate idle proof for workers
started by old code.
