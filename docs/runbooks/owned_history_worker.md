# Owned history completion

The fixed history child performs only native reads and derived history writes.
It never invokes source collection, warehouse, Finance or ready publication.
Cycle entry `build_owned_cycle_history(..., backfill_dates=(), closed_receipt=None)`
requires the actual current-thread cycle heavy owner and its maintenance SH.
It captures the canonical contiguous earliest selected old date through today
context. Writes and progress cover only rolling14 union the immutable tuple of
at most two confirmed old closed dates; intervening archive dates are read and
retained without enrollment or recomputation. The closed receipt is the canonical same-runtime `ClosedBacklog`.
Known pending dates are excluded by the caller; no pending date is acknowledged.
This module does not add a timer, route, profile or readiness capability.

Failed cycle receipts retain `history_failure` with the invocation mode, stable
error and inner reason codes, and a separate `outcome_unknown` outcome when the
transport cannot prove completion. Only fixed payload-free codes cross this
boundary; provider messages, source values and arbitrary exception text do not.
The same diagnostic is retained on the failed stage and survives a service
restart. A recorded failure is evidence, never permission to replay a cycle or
resubmit an uncertain child. Capture, initial verification and portion failures
must remain distinguishable when investigating an unchanged `CURRENT` edition.

One supervisor retains finished-builder and candidate EX descriptors across
all children. API/systemd/daily/warehouse/storage admission precedes domain
acquisition. Each fixed Unix-socket capability authenticates parent/child
kernel generations, socket peer, source authority, contract/code hashes and
four actual held FLOCK open-file descriptions. A cycle child additionally
requires the exact live API marker; ordinary standalone requires no active
API jobs. No child acquires a second heavy EX or installs ContextVar authority.
Direct invocation or arbitrary inherited FDs do not authorize the child.

The first capture freezes source dates, clock, vector, fence, CURRENT base,
existing catalog and context. Each portion uses that same anchor. Target
catalog identities and scope digest are fixed before its first dated write.
A bounded read-only child checks actual immutable SQLite day contents, unit
hash, catalog row set, exact dated vector token, pending target/base and CURRENT.
Only a strict increase of completed dated proofs, retaining every previous
reference, permits another portion. Cache size/mtime, file presence, transient
quality cache or a newly adopted source vector do not count as progress.
A separately admitted supervisor may read an older pending checkpoint only in
its first pre-write baseline: matching fresh-catalog/epoch/token day objects
are verified and counted as prior reuse, never new progress. Unsafe/corrupt
metadata or matching objects refuse without deleting evidence. Its new fixed
target intent then makes every post-write/continuation check exact; an older
unknown portion is never resubmitted. A count ceiling of 20 and total body deadline of 60 minutes are stop limits,
not a completion guarantee or a three-hour SLA. Every portion remains within
240 seconds including grace; capture20s, source32MiB, cache128MiB, store2GiB and
max31 recomputes remain unchanged. An indivisible day that cannot finish stops.

Timeout, crash or lost response is read back only after actual kill/reap. The
first action after any portion is exact CURRENT/pending verification. A proven
terminal edition completes without a second compile; only proven new durable
days permit continuation after an unknown response. No-progress, inconsistent
objects, drift, exhausted limits or unproven readback fail closed. CURRENT and
previous are retained by the existing store; failures do not assert a fabricated
last-good or perform an automatic source/warehouse/Finance/ready resend.
Canonical metadata-only rolling status publication is accepted only when all
other immutable edition fields equal the frozen base and exact new status.

For closed dates, an exact canonical `LiveNativeAdapter` in the authenticated
child runs the same dated-ready/source/complete-receipt predicates as standalone
closed acknowledgement. Its vector and native fence must equal the anchor.
The live supervisor constructs a private single-use proof tied to its actual
terminal invocation, receipt digest, tuple and CURRENT. Only that same supervisor
may authorize the parent receipt write while domain/heavy/SH remain held. A
caller dictionary, boolean, copied proof, subclass or fork supplies no authority.
Before/after physical stamps cover operational/book main+WAL, manifest and two
policy files, including absent/present transitions and nanosecond stat fields.
Parent consumption compares the same bounded metadata without a database open
or native capture. Stamps supplement exact native digests; they do not replace
them or make source plus receipt one atomic transaction. Receipt proofs describe
the checked edition/source version; later source drift invalidates latest view.

Ordinary `web_vitrina_history_candidate_build` enters history SH/heavy before
constructors and delegates through the same fixed child. It retains its original
source range and one-portion/240-second outer ceiling. Its hidden `--worker`
argument alone is not a capability. Ordinary manual trials also require the
existing exact `--runtime-contract`; omission now returns a typed refusal before
constructors. The canonical scheduled service already supplies this argument. The existing explicit confirmed held-window
manual repair path remains separate and unchanged; it does not acquire ordinary
heavy admission or become an automatic cycle path.

All unwind paths kill the process group and actually reap before domain FDs
close. PDEATHSIG plus inherited descriptor references preserves exclusion after
parent death, including before bootstrap. No explicit LOCK_UN closes inherited
ownership. Kernel termination latency is real: an unkillable child holds locks
and cannot start another portion, even after the body deadline. Supported
startup proof is Linux CPython procfs and the existing per-native-thread child
recovery algorithm, including exceptions before a handle/PID is fully recorded.
This is not a general subprocess capability framework.

`apps/owned_history_worker_smoke.py` checks real FD/process failure boundaries.
`apps/owned_history_completion_smoke.py` adds actual native multiportion/CURRENT,
lost response, no-progress, corruption, count bounds, old-date native ack and
pre-constructor contention. Fixtures are disposable, use no WB and preserve
source database contents. Parent mount/systemd probes alone are represented by
fixture stubs. The closed-ack integration requires the matching reviewed private
`ClosedBacklog.validate_native_readonly/_acknowledge_verified_native` seam.
