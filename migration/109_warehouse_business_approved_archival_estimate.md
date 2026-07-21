# Migration 109 — business-approved archival WB cost estimate

## Scope and business decision

This recovery is limited to the exact 18 legacy `nmId` values in
`migration/data/warehouse_business_approved_archival_estimate_20260701.json`.
The manifest pins seller article, the exact canonical nomenclature name, a
separate human-readable Finance product description, the production Finance dry-run
SHA-256 `dc4802b590a3540a9357f52a8bf04ae1a7e043573813321a61104f7604cfe6da`,
the former `fallback_average` value `113.8716125306441197313055472`, effective
date `2026-07-01`, quality `business_approved_archival_estimate`, and the
owner-approved value `100.00 RUB/unit`.

The target set is disjoint from the 33 active Vitrina SKU pinned in the same
manifest. Their factual costs and every primary source remain non-target.
There is no `nmId` branch in Finance: eligibility comes only from the active
versioned manifest rows.

## Canonical semantics

- A Finance sale/return before 01.07 uses the same-SKU 100 RUB effective basis
  from 01.07.
- On and after 01.07, the estimate is the canonical last valid WB WAC while no
  factual accepted-cost layer exists, including zero stock and later returns.
- A future factual accepted layer uses ordinary Decimal moving WAC. With zero
  preceding quantity the factual layer replaces the estimate; with remaining
  quantity it blends with that remaining capital.
- The estimate creates no quantity, stock, capital, supply or movement. The
  guarded apply only rewrites already materialized target daily cost rows,
  preserving each exact quantity and enforcing `capital = quantity × WAC`.
- Immutable `sheet_vitrina_v1_warehouse_opening_cost_map` is not updated. A
  versioned overlay records `supersedes`, owner approval, manifest/source
  digests, effective date and calculation/row fingerprints.
- `fallback_average`, cross-SKU substitution, legacy `COST_PRICE` and silent
  zero remain forbidden.

## Guarded production sequence

Repository delivery and source correction are separate gates. After deploying
the exact recovery SHA, build a fresh plan outside Git:

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  warehouse-archival-estimate-dry-run \
  --output /ABSOLUTE/OUTSIDE-REPO/warehouse-archival-estimate-plan.json
```

Review requires exactly 18 targets, empty intersection with the exact 33,
matching canonical nomenclature identity and former fallback evidence, no target factual
post-effective acceptance event or factual inbound evidence already embedded
in daily provenance, zero primary/opening/movement writes, target
before/after rows, full primary/non-target/opening digests, and
`apply_allowed=true`. Apply requires a newly approved exact fingerprint:

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  warehouse-archival-estimate-apply \
  --plan-file /ABSOLUTE/OUTSIDE-REPO/warehouse-archival-estimate-plan.json \
  --fingerprint 'sha256:EXACT_REVIEWED_FINGERPRINT' \
  --approval-reference 'OWNER_APPROVAL_REFERENCE'
```

The runner creates a coherent mode-0600 SQLite backup after the free-space
gate, verifies SHA-256 and `integrity_check=ok`, re-plans under `BEGIN
IMMEDIATE`, applies the exact target set, and checks primary, immutable opening
and non-target digests before commit. Every functional apply/rollback enters
the same re-entrant `.warehouse-functional-sync.lock` common boundary as the
archival apply/rollback. The active archival version and exact row lineage are
also part of the functional local-source digest, so a warehouse plan built
before archival activation is rejected after it acquires the lock. Exact
repeat is a no-op without another backup. A fresh post-apply dry-run is marked
`status=no_op`, `apply_allowed=false`; even if submitted, it returns an inert
readback and cannot append another version. The entire dry-run is read from one
explicit SQLite snapshot. If factual acceptance lands before daily replay,
consumers and archival readback reject an already materialized estimate row
until the ordinary WAC projection succeeds; an exact idempotent retry also
fails closed instead of relabeling that blocked readback as success. Every
target must have exactly one matching nomenclature row. Identity is proved by
`nmId + seller article + canonical nomenclature name`; the descriptive Finance
name is retained only as lineage and is never compared as a nomenclature key.
The dry-run publishes expected and actual identity fields for all 18 rows, and
any duplicate, identity conflict or factual purchase price blocks apply. Readback is:

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  warehouse-archival-estimate-readback
```

Rollback is version/audit preserving and restores only the exact pre-apply
derived daily rows after a new coherent backup. Its consumed plan fingerprint
remains append-only audit and is deliberately non-reusable; a new business
decision would require a new manifest/version identity:

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  warehouse-archival-estimate-rollback \
  --fingerprint 'sha256:APPLIED_FINGERPRINT' \
  --reason 'EXACT_REASON'
```

The parent functional cutover cannot be rolled back while this overlay is
active: archival rollback/deactivation must run first, preventing orphaned
lineage or deleted daily rows below an active version.

Only after successful source readback may the all-history Finance dry-run be
generated. Its separate fingerprint still requires the Finance human gate in
migration 108.

## Verification

```bash
python3 apps/warehouse_archival_estimate_smoke.py
python3 apps/warehouse_functional_smoke.py
python3 apps/wb_finance_weekly_business_approved_backfill_smoke.py
python3 apps/wb_finance_weekly_canonical_scale_smoke.py
python3 apps/partner_report_smoke.py
python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py
```

The dedicated smoke proves exact 18/33 disjointness, 51/51 resolver coverage,
100 RUB before/on/after 01.07, unchanged active costs, normal future WAC,
verified backup, shared common-boundary writer serialization, atomic apply, inert fresh
no-op plans, factual-layer overwrite rejection, stale-estimate fail-closed,
single-version idempotency and audited non-reusable rollback.
