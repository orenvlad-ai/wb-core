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
unknown portion is never resubmitted. The completion body has up to 60 minutes,
20 portions and 31 recomputes per portion. Ordinary workers have one window
of up to 240 seconds including grace. Each source capture pass has a 20-second
limit. The source, cache and store limits are 32 MiB, 128 MiB and 2 GiB respectively. An
indivisible day that cannot finish stops.

When a cycle owns a validated `SupplierHistory` receipt, the child first
checks its original sources and exact date scope. It then requests time for
the derived work on the same invocation and inherited socket. A writing portion
may request time for dated objects once the catalog, target and baseline have
been checked, before saving its intent. Status-only paths use the current grant.
Only a terminal verification may request the final supplier proof interval
after checking the published objects. The parent checks ownership and the preceding deadline
before each grant. Each phase has at most the configured window including
five seconds of grace. All phases of one child share a limit of two such
windows (at most 480 seconds) and the remaining completion body deadline.
The journal records each phase and its budget under the invocation ID.
A phase transition records zero dated progress. Continuation still requires
the native readback below. The completion body starts after parent source
validation. The parent records the acknowledgement after terminal verification.

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
Physical stamps cover operational/book main+WAL, manifest and two policy files,
including absent/present transitions and nanosecond stat fields. During the
immutable History-object readback only, unrelated operational commits are
allowed: initial/final native captures must retain the exact vector and revision
fence, while operational device/inode and all book/manifest/policy stamps remain
unchanged throughout. A single fresh full stamp is taken before the native receipt
validators and final bounded capture, then compared after all of them. This avoids
treating a cleaner journal commit during object hashing as a source revision;
it does not normalize arbitrary writers or permit dependency/Ready revision ABA.
Concurrent writes in the final strict interval still refuse; there is no retry.
Parent consumption compares the same bounded metadata without a database open
or native capture. Stamps supplement exact native digests; they do not replace
them or make source plus receipt one atomic transaction. Receipt proofs describe
the checked edition/source version; later source drift invalidates latest view.

On Linux the supervisor also holds a nonblocking inotify queue. Its authenticated
verify child watches the source directories and existing file inodes before
reading. During immutable object hashing, the operational database may receive
ordinary commits and its WAL may rotate. The other sources stay fixed. All
watched sources stay fixed during the final native checks and until the parent
consumes the proof. A change followed by restoration also invalidates the proof.
Loss of the watch or overflow of its queue requires fresh verification. This
additional check covers local filesystem operations observed by inotify. Native
revision, digest and physical stamp checks continue to apply.

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
