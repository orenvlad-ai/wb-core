# WBC0027 reconciliation failure evidence

## Scope

This correction preserves terminal evidence for the mutation-incapable
`wbc0027-receipt-reconciliation` contour. It does not replay product or
economics, create a recovery row or private manifest, change business data, or
dispatch reconciliation automatically.

## Diagnosed predecessor

Run `33363863580` at trusted-main SHA
`f389cacff6786a4280f0fdd0acce928af798867f` passed exact source preflight and
then failed in job `99400411103` at the fixed query-only collect step. The
upload and publish steps were skipped, so the run has zero artifacts and no
supersession marker. Its durable log proves the original PR/source/release
inputs and preflight `query_only=true`, `database_written=false`, zero mutation
and zero product/economics replay, but the old collect path collapsed remote
nonzero, transport ambiguity, invalid JSON and validator failure into one
exception before writing a receipt.

The continuation reuses the existing `prior_reconciliation_run_id` input and
accepts only `33363863580`. It validates the exact repository, workflow, event,
attempt, head SHA, job, step outcomes, zero artifacts, marker chronology,
logged source inputs and preflight mutation truth. Every other prior artifact,
comment and a02 input remains zero or empty. This is a source-specific
continuation for WBC0027, not a generic failed-run bypass.

## Failure receipt contract

Collect always writes one canonical v2 receipt after a successful preflight. A
transport failure, remote nonzero, invalid/non-object JSON payload or failed
exact validator produces a distinct closed reason code and `state=blocked`.
The receipt binds the source, deployed reconciliation release, workflow bridge
and diagnosed predecessor, and records return code, transport status,
parse status/error, named validator predicate failures, bounded redacted
stdout/stderr previews plus byte counts/digests, and query-only/zero-mutation/
zero-replay truth. Full command streams and secrets are not copied into the
receipt.

The workflow uploads that canonical file with `always()` after successful
preflight, even when collect exits nonzero. Upload still precedes publication.
Only a successful qualified receipt whose uploaded artifact verifies exactly
may publish the single supersession marker; a blocked failure artifact never
creates a done marker. The existing 25-input workflow surface and the prior
field semantics of warm-archive modes are unchanged.
