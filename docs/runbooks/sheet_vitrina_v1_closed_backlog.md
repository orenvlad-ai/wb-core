# Dormant finite closed-date obligation

This component has no scheduler, HTTP route, worker launcher or profile switch.
Its finite stages are now connected to the dormant `run_cycle` sequence.
Its caller must hold the real `cycle` heavy owner for the actual runtime. It
does not change the canonical formula, history epoch, native edition schema or
resource budgets.

`ClosedBacklog.collect(cycle_id)` reconciles one private fixed receipt before
selecting any new work. Selection uses canonical due/backoff/exhausted rules,
the active reporting scope and canonical dependencies, with at most two dates
older than today and yesterday. Least-recent attempts take priority. Local
`own_product_capital` is derived in the ordinary dated plan, not acquired as a
temporal source. The receipt precedes each possible source acquisition. Each
selected date calls `_load_live_sources` for one closed slot only. This retains
the canonical source capture/cache side effects, including closed web-source
sync; it does not perform the main plan's mature capture, rollover, current
web sync, economics, warehouse, ready or history publication.

An accepted exact slot anchor discharges acquisition, not publication. After
restart the same receipt remains authoritative; accepted sources are not
fetched again to recover the dates. A possibly dispatched attempt without an
accepted anchor or changed canonical attempt receipt remains `outcome_unknown`.
A later acquisition requires the original canonical closure attempt evidence
and its existing due rule. Pending/backoff/exhausted outcomes remain pending.
The fixed file is private, at most 64 KiB, and status performs metadata reads
only. There is no scan through prior cycle receipts and no generic queue.

The dormant cycle order is `closed_sources → api_sources → finance_sources →
fbs_generation → warehouse → daily_projection → closed_ready → final_ready →
rolling14`. Every stage has a durable running identity before effects. Actual
business date is checked both before and after a stage and between old dates.
No external stage is retried by this runner. Adapter/receipt-helper initialization also occurs inside the
owned terminalization boundary: initialization failure records a terminal
cycle receipt before the actual worker releases its slot/SH/heavy leases.
Ownership is checked before that boundary, so an unowned caller cannot write
a terminal receipt.

The integration sequence is:

1. Reconcile/acquire the finite old batch; independently collect the ordinary
   full `auto_daily` main handle once at the actual clock, without selectors.
2. Run the existing canonical Finance source, complete FBS generation, owned
   warehouse/all tails and source-free daily repair once. Preserve their exact
   source/material versions before deriving any final old/current plan.
3. For each fully accepted old date, `compose(main_handle, day)` constructs a
   private invocation from exact dated accepted caches and the retained actual
   current slots. Then `derive_collected` uses the original evaluator and fresh
   local/material/ready pins; publish through the existing ready CAS protocol.
   Missing/invalid non-due caches refuse with the exact source/date/role while
   preserving the obligation. The canonical archive-only unavailable ONEC and
   pre-cutover/unknown-roster authenticated-buyer semantics remain unavailable;
   they are never substituted with zero or fabricated accepted source evidence.
4. `record_ready(day)` reads the real dated ready row, exact consumed source
   anchors and its matching complete ready-required publication receipt. A
   restart after ready commit can call this method without acquisition or
   derivation. It cannot record an arbitrary caller proof.
5. Derive/publish the retained main current ready **last**. Export one immutable
   `publication_dates()` tuple containing only dates with matching real
   dated-ready proofs. Known pending/backoff or missing non-due old caches
   remain obligations and make the cycle degraded; they never join the tuple
   or receive a publication ack. A possibly unknown acquisition is a typed
   cycle failure before the full main capture, not a warning or fresh resend.
   Give that same tuple to the existing owned native history path for every
   bounded portion. A partial portion, `PENDING` or a date alone is not success.
6. `acknowledge(adapter, store, history_config)` reads a real native adapter and
   store for the same runtime. It requires the existing storage/formula guard,
   exact native dated-ready selection, unchanged accepted source anchors,
   complete ready receipt inputs, and `CURRENT` edition day proofs matching a
   fresh native source vector. It rechecks the vector, pointer and ready chain
   before marking only the fixed tuple acknowledged. New ready/source revisions refuse;
   there is no boolean ack, new edition field or weaker history proof.

A registry bundle revision changes dated publication context, not operational
source authority. The fixed receipt retains `collection_bundle_version`, dates,
accepted anchors and attempt evidence. Reconciliation first checks real
authority, exact anchored bytes and the original uncertain attempt. Source
request-scope fingerprints prevent a changed scope from granting authority to
an unknown earlier dispatch; an unproved legacy unknown context fails closed.
Known accepted raw is re-admitted under the current canonical scope without
fetching it. The publication bundle may then rebind durably. A prior bundle's
unacknowledged ready becomes `previous_ready` evidence and is removed from the
ready tuple until an actual new dated derive/CAS/complete receipt exists.

Known retained-cache refusal is persisted as `deferred_reason` on the same date
and excluded from old derivation/ack for this cycle. The full current cycle
continues degraded. A canonical accepted-partial source keeps its ordinary
admission semantics; missing other non-due operands still refuse at composition.
Next reconciliation checks the same finite debt again, clearing deferral only
for a real later admission attempt. Mixed acknowledged/pending entries retain
their original evidence. Changed raw/authority or unknown write is never a
bundle/cache warning. No new receipt queue, native schema or formula pin is used.

The owned worker uses the same `validate_native_readonly` source→dated ready→
CURRENT predicates in the bounded child. That method performs no receipt write
or ownership/mount grant. `_acknowledge_verified_native` in the parent requires
the actual cycle owner and consumes a one-use private proof authenticated by
the live owned supervisor; copied dictionaries cannot bridge child readback to
parent acknowledgement. Receipt digest and ready tuple must still match.

Native reads retain the canonical contiguous earliest-old→today context needed
by the pinned compiler; HistoryStore limits compilation/publication to rolling14
union the fixed at-most-two old dates. Intervening dates are not enrolled by
reading them. Existing read/deadline/portion budgets stay in force.

The caller must treat old-date refusal as unfinished work. Timer retirement,
profile activation and controlled production pilot remain separate changes.
Registry publication/history entrypoints and owned multiportion worker are
coordinated companion changes. Resource/mount admission errors preserve demand.
This component neither promises all pending dates fit rolling 14 days nor
increases the existing native limits to make a particular batch succeed.

The disposable smoke suite exercises actual canonical source persistence,
dated derivation/ready CAS, native adapter/compiler/store edition and ack.
Only external transports, fixture mature/current sync callbacks and the
physical production-mount admission are substituted. Production calls retain
the full storage guard. It also checks fresh-process crash readback without
accepted-source refetch, incomplete/native resource refusals, altered proofs,
one-snapshot anchor validation, mature raw pin preservation and full-scope
handle ownership.

The cycle wiring suite additionally asserts the exact nine-stage order, one
full main capture with no source selectors, the same verified domain versions,
old dated ready before final main ready, and unchanged SQLite/WAL through native
derivation/ack. In this bounded test Finance/FBS/warehouse/daily callbacks are
fixed verified stage transports; their actual domain operation and journal
predicates are covered by the existing cycle/domain suites. The native
source→ready→edition chain is real, while Linux child FD/terminal/lifetime proof
belongs to the coordinated owned-worker Linux suites. This is not a claim that
a complete production cycle or activation has been demonstrated.
