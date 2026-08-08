# WB Autoanswers policy addendum v5

Status: owner-approved, 2026-08-08. This addendum extends server orchestration
policy v4. It does not alter doctrine v1.0, the frozen AI bundle v1.4.2,
prompts, schemas, guards, thresholds, golden data, evaluation signature or
artifact hashes.

Policy identity: `owner-policy-2026-08-08-v5`.

## Server-owned return guard

The versioned `wb_autoanswers_owner_policy_v1` contract evaluates the complete
normalized review surfaces (`text`, `pros`, `cons`, tags) after the immutable
bundle result. It uses auditable co-occurring semantic signal groups rather
than a single regular expression or a random quota.

A frozen `wb_return` becomes `public_only` when it has no independent hard
return reason. Ordinary cracking, breaking, crumbling, shedding and edge
chipping after use are public-only regardless of whether the review says one
day, the first days, a week or no exact duration. Partial privacy effect,
subjective quality, a vague mismatch and a remedy request alone are not hard
return reasons.

The return route is preserved only for an independently evidenced condition:

- damage at receipt or before use; opened packaging or missing contents;
- a proved different item/variant or concrete model/size/fit mismatch;
- a persistent stripe/spot or uneven coating;
- a persistent sensor/camera failure;
- privacy effect fully absent rather than merely partial;
- stated device damage or injury;
- a clearly large chip or sharp/cutting edge with injury risk.

Mixed reviews record those exact independent reasons in
`server_owner_policy.hard_return_reasons`. Later ordinary cracking is never
presented as the return basis.

## Public reply semantics

Post-use breakage uses deterministic but variable templates selected from the
feedback identity. Every variant acknowledges the unpleasant experience,
states that protection reduces risk without guaranteeing absolute protection,
and explains that local point loading can damage the protective glass itself.
It never asserts an unreported impact or an intact device screen. The formula
about force, angle and contact point is selected only when the review contains
a positive, non-negated description of a fall or impact.

The exact phrase `к сожалению` is limited to one occurrence. Existing double
empathy with `Сожалеем`, `Нам жаль` or `Жаль` removes the phrase. Otherwise the
policy deterministically inserts it first into a knowledge limitation such as
`По фото, к сожалению, нельзя достоверно определить…` or
`По описанию, к сожалению, недостаточно данных…`. It is not inserted into a
CTA, neutral instruction or positive sentence. Some negative templates use it
naturally; others deliberately do not, preserving live variation.

## Existing zero-write queue activation

Deploy alone does not activate v5 in an existing v4 store. While the exact
Autoanswers worker timer and service are disabled/inactive and read-only sync
continues, the hosted
`autoanswers-policy-v5-reconciliation dry-run|apply|readback` command is the
only activation path.

- `dry-run` pins the complete deployed SHA, verified current-schema backup,
  settings/epoch, every publication/job/reply/hash projection, active cost
  boundaries, exact counts and non-target digests. SQLite is opened with
  `mode=ro` and `PRAGMA query_only=ON`.
- `apply` requires the external reviewed fingerprint and a fresh canonical
  worker-hold readback. One `BEGIN IMMEDIATE` transaction evaluates every
  unstarted publication, advances the policy epoch/version once, rewrites and
  rekeys only changed zero-attempt artifacts, rebinds unchanged artifacts, and
  appends hash-only per-row plus summary audit. It makes zero provider calls
  and zero WB POSTs.
- Any publication with `write_started_at`, an attempt row, or a
  publishing/readback/published state is excluded from mutation. Its complete
  row, linked job policy projection and all attempt evidence are protected by
  the started-publication digest.
- `readback` is query-only and requires all unstarted artifacts to carry v5 and
  the new epoch with coherent exact reply hashes, the audited fingerprint,
  exact before/after counts and unchanged settings (except policy),
  started-write, attempt, outside-scope job, reservation, cost and uncertainty
  digests. Feedback truth/version/media are a separate GET-only evidence group:
  with the canonical readonly timer still enabled/active, non-decreasing counts
  and changed digests are reported as a bounded observed delta. A count
  regression, immutable execution drift, WB-attempt delta or provider-boundary
  delta blocks. Existing flat v1 reviewed plans/audits are split compatibly and
  retain their exact applied fingerprint.

Only after a reconciled readback may feature-owned lifecycle reconciliation
restore the previously persisted `auto_all` worker intent. Replaying an applied
fingerprint is a bounded no-op.
