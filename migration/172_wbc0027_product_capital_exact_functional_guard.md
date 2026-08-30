# Migration 172 — WBC0027 exact-functional product-capital guard

## Scope

- account-level CNY documents without an exact shipment identity become a
  durable warehouse no-op;
- mutable own-capital events become replay signals only;
- exact functional publication is the sole post-cutover product-capital
  writer, with date/version/digest/publication provenance;
- both product-capital readers reject unbound or mismatched projection rows;
- warehouse projection health exhaustively reconciles the 42-key allowlist;
- one production-mutation manifest prepares two separate T1 recovery
  operations: product capital, then qualified cost/Proxy.

## Historical production qualification

- 17–29 August: 936 rows / 19,656 cells; 7,655 mismatches, including
  7,639 event-path cells and 16 separately qualified exact-zero cells for SKU
  `497413772` on 21 August;
- 13, 14 and 16 August: 216 rows and 1,791 mismatches;
- 15 August: `EVIDENCE_BLOCKED`, because no immutable same-date functional
  version exists;
- 30 August and later: hard non-target, exact at qualification;
- cost/Proxy: 298 logical / 472 persisted source-proven repairs on 26 and
  29 August; twelve 26-August protected/dependent cells remain explicit
  evidence-blocked rather than guessed.

The PR #1126 static manifest, `awaiting_apply` release operation, approval
comment and recovery IDs are historical superseded evidence and are not
reusable.

## Consolidated JIT correction

The active `product-capital-qualified-economics` profile is released as
`live_runtime`, never as another static `production_mutation` retry. It derives
deployed SHA only from `live_runtime/done`, binds the exact StoreRegistry
generation and creates fresh product/economics operation IDs in one new
scope-goal namespace. Each phase requires two consecutive normalized material
witnesses and permits at most three regenerations before its first submit.

Product target is 1,152 rows / 24,192 cells / 9,446 mismatches. Economics is
fresh only after retained exact product readback and remains 298 logical / 472
persisted repairs with twelve explicit 26-August gaps and 29-August missing
count zero. Exact target before images are re-read under the shared writer
lock. Mutable full ready envelopes, events, outbox and timestamps are
audit-only; unrelated 21-August Proxy V4, Finance, 30 August and later, and SKU
`428853741` cost `117.537167` are preserved.

Plans are private, admitted and durable (0700 directory, 0600 file, O_EXCL,
file fsync, atomic no-overwrite publish, directory fsync). Product and
economics have at most one submit each. After submit or transport ambiguity no
candidate is regenerated and only same-operation query-only readback is
allowed. Release/deployed dry-run uses `--no-create` and has zero business-data
mutations. Production Apply remains default-off and requires one new immutable
OWNER/MEMBER passport.
