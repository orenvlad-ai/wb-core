# Shared heavy admission: core producer integration

This is a limited, dormant-cycle integration. It does **not** establish global
serialization or permit activation of the three-hour profile. No route, timer,
profile, FBS freshness policy, source formula, schema or history epoch changes.

## Covered entry points

| Producer | Boundary and lifetime |
| --- | --- |
| HTTP full refresh / full auto update | Canonical `_run_sheet_refresh` and `_run_sheet_auto_update`, before scans and `_sheet_cycle_lock`, through publication and existing economics tails. |
| Scheduled / raw auto refresh | Canonical scheduled runner before `mark_run_started`; raw resolver reserves the actual async lease before persisting missed-slot metadata. A busy producer cannot consume this metadata. |
| HTTP source-group refresh / health group recovery | Canonical group runner and its `refresh_group` operator worker, before sources and materialization. Health recovery retains its existing exact observation/action receipt. |
| Temporal closure retry | Canonical closure loop before due scans, through its nested canonical refresh. |
| HTTP finite operator workers | `refresh`, `auto_update`, `refresh_group`, `cycle` with configured runtime: acquire before transient job registration, markers, `on_accept`, thread construction/start; release only after `_run` and marker cleanup. |
| Manual warehouse synchronous / async / pending | Heavy admission before warehouse domain lock, journal recovery, acceptance and claim; worker retains it through the existing phases, terminal journal result and finally. |
| Warehouse CLI | All commands except cached `readback`: admission before runtime/block constructors and domain locks. Existing sync ownership/economics/tails and reviewed-write guards remain. |
| Full internal cycle | Acquire before durable receipt and fixed cycle slot; same worker owns it through all bound stages. Exact duplicate key/UTC-slot receipts use a scope-checked read-only lookup and never resend work. |
| Cycle-owned rolling14 history | Actual `cycle` heavy owner plus existing exact live operator thread/job proof. Existing API markers, systemd, daily-worker, warehouse, formula/storage, finished-builder and candidate guards stay enforced. History runs in-process; no parent/child EX reacquisition. |

The full Finance backup integration is already present separately. Nested bound
stages reenter only the same live owner in the same actual thread. Independent
threads/processes fail immediately with `HeavyAdmissionBusy`; they do not wait
under a maintenance SH lease. HTTP refresh/group/health admission rejects with
409 and `status=busy`, `retryable=true`, `effects_started=false`, without a job ID.
Warehouse CLI emits that bounded busy outcome and exits zero, without claiming
successful work. Warehouse HTTP retains its existing not-accepted busy response.

## Async ownership and pending recovery

The private `_heavy_lease` parameter transfers an actual unentered lease from
raw auto/cycle dispatch to its sole worker. It cannot be supplied by HTTP input,
request ID, environment variable or a persisted PID. Root acquisition owns an
independent maintenance SH before heavy EX. The existing `admitted_thread`
provides finite worker admission and its reviewed no-start handling as well.

Constructor failure or a **proven** no-start `Exception`, `KeyboardInterrupt` or
`SystemExit` releases both leases and keeps the existing terminal/pending outcome.
An exception after native spawn, or an uncertain startup, preserves admission;
only the possible worker may close it in finally. The proof uses the existing
narrow CPython `_started` / `_limbo` predicate; the supported deployment is
CPython on Unix with `flock`. Lack of proof is never permission to repeat effects.

The warehouse picker is a raw idle loop with no long SH/heavy lease. Each actual
pickup reserves heavy admission before journal recovery/claim. A busy attempt
keeps an already committed accepted request pending. Startup/resume can drain
that same request ID once admission is available. Preserved picker references
are occupied even before native bootstrap; normal retirement uses the owning
thread's finally. No new generic queue or automatic source resend is introduced.

## Still open; activation remains closed

- Standalone history builder and its bounded subprocess ownership/handoff.
- Canonical daily/weekly Finance ingestion and independent cost paths.
- Independent FBS collection and policy/reader wiring.
- Supplies backfill/transit workers and other independent warehouse producers.
- Supplier/CNY/FF and nomenclature after-save continuations, including durable
  source-owned intent and writer-lock ordering. Those source commits were not
  put under a long global lock in this candidate.
- Schedule/launcher/profile wiring and backup-priority admission policy.
- Total rolling14 completion budget: the existing cycle helper performs one
  portion of at most 240 seconds and fails if incomplete. Continuation/last-good
  acceptance remains a separate blocker. A future launcher must release its
  service activity after durable acceptance, or provide a separately reviewed
  exact owner mapping; the systemd guard has no generic ignore option.

No all-covered capability is exported. Legacy producers in this list can still
conflict with the covered work until their separate integration is reviewed.
A busy full refresh is rejected before acceptance; it is not a durable deferred
source operation. Ordinary timers and explicit caller retry policy are unchanged.

## Offline evidence and post-release read-only checks

`apps/business_data_heavy_producers_smoke.py` uses disposable runtimes, real
spawned-process contention, real maintenance/heavy/domain locks and journal
accept/claim/finish. External source, cost and apply services are local fakes.
It checks pre-accept rejection, worker/finally lifetime, constructor/cancellation
outcomes, uncertain native startup, pending restart/drain without duplicate
source/cost effects, CLI boundaries, same-owner reentry and HTTP busy responses.
Existing cycle, warehouse, async lifetime, ready publication and formula guard
smokes remain relevant.

After a separately authorized release, read `heavy_admission_status(runtime)`
and `admission_idle(runtime)` without provisioning or acquiring writer context.
Correlate existing operator/warehouse cached status and durable cycle receipts
with the same accepted IDs and terminal results. Confirm the helper is idle only
after worker completion, and that accepted warehouse pending intent remains
visible while admission is busy. Use query-only SQLite reads for journal proofs.
These checks must not dispatch a cycle, create a pending request, collect WB
sources or activate a profile. Old unintegrated daemon workers still require the
separate maintenance and path-specific idle proof for first cutover.
