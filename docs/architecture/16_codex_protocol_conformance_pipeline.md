# Codex Post-Task Protocol Conformance Pipeline v1

## Status and consumer

Owner phrase `«Сверка»` invokes one internal read-only post-task batch for the
designated production-protocol/documentation curator. The pipeline checks
observable conformance facts after technical work has reached a terminal state
and produces a factual report for the separate analysis layer in
[`14_codex_task_audit_checklist.md`](14_codex_task_audit_checklist.md).

This pipeline is not an execution checklist, gate, approval, acceptance step or
task-level enforcement. Ordinary main/domain curators and technical execution
subagents do not read or invoke it for execution and do not change their
behaviour because of it. Their operational entrypoint remains root
[`AGENTS.md`](../../AGENTS.md) plus relevant authoritative domain docs.

## Invocation and batch boundary

One `«Сверка»` invocation produces one bounded batch:

1. If the owner names exact tasks, blocks or a range, those identities are the
   complete scope.
2. Otherwise the curator uses the completed WBC tasks explicitly available as
   the current unaudited batch in the audit-chat context.
3. If that boundary cannot be proved, the curator selects a bounded recent
   terminal set, lists every exact task/block identity in the report and records
   overlap or an unknown earlier boundary as a limitation. The curator does not
   ask the owner only to choose an audit window.

The pipeline creates no persistent queue, registry or state file and defines no
fixed statistical batch size. A later batch may overlap an earlier one; the
overlap is reported rather than silently deduplicated.

## Applicable protocol revision

Each technical block is evaluated only against the exact protocol revision
that was active at that block's recorded start or explicit activation boundary.
The binding uses an exact repository revision and durable evidence for the
block boundary. A rule merged or activated later is never applied
retrospectively.

If either the applicable protocol revision or the relevant activation boundary
cannot be established, affected checks are `UNKNOWN`, not `DEVIATION`. Different
blocks in one task or batch may therefore bind to different revisions.

## Evidence boundary

The pipeline may use only evidence that already exists:

- the task passport and meaningful recorded transitions;
- the terminal technical-subagent handoff;
- immutable GitHub PR, check, Release Runner and Apply Runner receipts;
- durable exact pointers or digests referenced by those artifacts.

It does not wake executors, launch tests or diagnostics, acquire new domain or
production evidence, or mutate repository, runtime, data, task or platform state
for the audit. Missing UI/status history is not inferred as success or failure.
The pipeline reports only mechanically observable facts; it does not claim that
current Codex can inspect hidden reasoning or perfectly machine-extract every
fact.

## Result states

Every applicable check for every technical block has exactly one state:

- `PASS` — existing evidence proves all applicable predicates in the check;
- `DEVIATION` — an exact applicable rule and existing evidence prove a breach;
- `N/A` — no predicate in the check applies to this block;
- `UNKNOWN` — applicability or conformance cannot be proved from allowed
  evidence.

`UNKNOWN` is not failure. A check with a proved breach is `DEVIATION`; otherwise
an unresolved applicable predicate makes it `UNKNOWN`, all proved applicable
predicates make it `PASS`, and a check with no applicable predicate is `N/A`.
The pipeline adds no severity, recommendation or classification code.

## Deterministic checks

The stable check IDs below are report keys. Each check records conformance facts
against its exact applicable rule pointer, never an optimization proposal.

| ID | Conformance fact |
| --- | --- |
| `PC-01` | Exact WBC task and technical-block identities are present, and the block start/activation boundary is bound to an exact applicable protocol revision. |
| `PC-02` | Before execution, the owning main task held the accepted goal, compact passport, included/excluded scope, acceptance predicate and applicable stop-line. |
| `PC-03` | Before implementation dispatch, substantive ambiguity and known outcome-changing dependencies were resolved through the applicable pre-dispatch/router path without speculative scope expansion. |
| `PC-04` | Actor routing used cohesive minimum-sufficient diagnostic packages for the nearest decision transition; adaptive `0/1/N` dispatch and concurrency followed evidence/dependencies without a numeric preference; artificial fragmentation, duplicate packages/questions and unjustified serialization were absent; and at most one mutating subagent, internal naming plus visible Russian communication conformed where applicable. |
| `PC-05` | One owning owner-facing surface was preserved; diagnostic handoffs returned only there, and one event wait covered the active subagent set without lost callbacks, heartbeat, unchanged-status polling or a duplicate monitor. |
| `PC-06` | Any human gate was legal, uniquely routed and deduplicated; accepted goal, business meaning or routine technical decisions were not requested again. |
| `PC-07` | Same-scope corrections stayed with the owning block, one branch/PR boundary was preserved where applicable, scope drift was not introduced and the applicable loop-breaker was followed. |
| `PC-08` | Verification was proportional and relevant; repository work used the exact PR Gate/Release Runner contour, and any production mutation used the separate exact Apply Runner contour. |
| `PC-09` | When a consistent boundary over a changing live resource was required, producer handling avoided blanket pause and proved applicable restore, catch-up, health and query-only readback facts. |
| `PC-10` | Terminal technical-block state and the main task's business outcome were recorded independently without synthesizing owner acceptance from technical completion. |
| `PC-11` | The block emitted one compact terminal handoff with exact durable pointers, and the owning main task produced the applicable short plain-Russian owner summary. |

Business outcome and protocol conformance are independent dimensions. A blocked
or failed business result may be fully conformant; a successful result may
contain one or more deviations.

## Report contract

The report is compact but must contain:

1. A batch header with audit time and inspected time/window, exact task/block
   identities, the boundary source or limitation, and for each block the exact
   applicable protocol revision plus its evidence pointer.
2. A per-task/per-block matrix containing `PC-01` through `PC-11`, separate
   technical-block state and business outcome fields, and reconciled totals for
   `PASS`, `DEVIATION`, `N/A` and `UNKNOWN`. Full `PASS` rows may be compacted
   only when their counts still reconcile with the matrix.
3. An exact deviation list. Every entry contains only task/block identity,
   check ID, exact applicable rule pointer, observed fact with evidence pointer,
   and one consequence category from
   `time|human_gate|context_tokens|ux|safety|none`.
4. A separate unknowns list with task/block identity, check ID and the exact
   missing or ambiguous revision/evidence fact. An unknown never appears in the
   deviation list.
5. The fixed terminal marker `pipeline_state: read_only_no_action`.

Counts in the matrix and both detail lists must reconcile. The report contains
no recommendation, protocol correction, domain correction or platform action.
Owner-facing представление report следует
[`13_codex_curator_workspace.md`](13_codex_curator_workspace.md#owner-facing-result);
exact factual report contract не ограничивается summary body limit.

## Strict stop and analysis handoff

At `read_only_no_action` the pipeline stops. It does not create a follow-up task,
test, change, owner gate or mutation. After owner discussion, the separate
post-task analysis in
[`14_codex_task_audit_checklist.md`](14_codex_task_audit_checklist.md) may
independently classify evidence as `NO_CHANGE`, `OPTIMIZE`, `RELAX`, `CLARIFY`,
`TIGHTEN`, `ROUTE_DOMAIN` or `PLATFORM`. Any resulting change requires a
separately authorized repository block. A single harmless deviation does not
automatically tighten the protocol.

The canonical checks and report contract live in this repository so curator
rotation does not depend on a private prompt or chat memory.
