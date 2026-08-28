---
title: "Модуль: baseline/diff engine реестра изменений"
doc_id: "WB-CORE-MODULE-56-CHANGE-REGISTRY-BASELINE-ENGINE"
doc_type: "module"
status: "active_internal_engine"
purpose: "Транзакционно фиксировать joint Prices+Ads checkpoints и доказанные transitions; давать module-57 projection и late-link exact module-58 writer/checkpoint proof без duplicate facts."
scope: "Один seller/account; explicit sanitized acquisition input; complete-baseline chain; append-only facts/incidents; query-only exact target/field projection."
source_basis:
  - "docs/modules/54_MODULE__CHANGE_REGISTRY_FOUNDATION.md"
  - "docs/modules/55_MODULE__CHANGE_REGISTRY_SOURCE_ACQUISITION.md"
  - "packages/application/change_registry_baseline_engine.py"
related_modules:
  - "packages/application/change_registry.py"
  - "packages/application/change_registry_source_acquisition.py"
  - "packages/application/storage_registry.py"
related_runners:
  - "apps/change_registry_baseline_engine_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "Canonical baseline/checkpoint/diff/projection engine invoked by the active module-57 observer; the engine itself owns no scheduler, HTTP/UI, writer instrumentation, manual pending or WB mutation."
---

# 1. Activation and transaction boundary

`ChangeRegistryBaselineEngine` is an internal callable used by the module-57
observer. It has no timer, scheduler, refresh hook, HTTP/UI route, writer
instrumentation or startup invocation of its own. It accepts only the canonical sanitized
`wb_change_registry_source_acquisition/v1` result for the configured exact
`seller_id + account_scope`, verifies its own and both source manifest digests,
mapping version and zero-persistence seam, then opens the StoreRegistry-selected
`operational` generation.

One explicit `ingest` is one `BEGIN IMMEDIATE`: checkpoint, normalized
observations, identity incidents, facts and checkpoint links commit together.
An optional transaction hook lets the observer add only its bounded source
summaries, terminal job/health event and lease release to that same commit.
Any validation/constraint/insert failure rolls the whole invocation back. IDs
derive from canonical proof bytes; an exact repeated acquisition returns the
same receipt bytes and cannot create a second checkpoint, observation, incident,
fact or link. A conflicting reuse fails closed.

# 2. Joint baseline chronology

Only `joint_complete=true` with independently complete Prices and Ads becomes a
complete checkpoint. The first complete checkpoint is baseline only: it may
persist checkpoint/observations/incidents and creates exactly zero facts.

Partial or failed invocations may persist typed health/observation evidence and
reference the latest earlier complete checkpoint, but they never become a
baseline and never create facts. A later complete invocation advances the
complete checkpoint chain. For each scalar it compares only the current exact
value with the latest earlier complete exact value; intervening missing, null,
inapplicable or error observations do not advance that scalar baseline.
Complete checkpoints are strictly chronological; same-time conflicting or
out-of-order complete evidence is rejected.

Complete acquisition does not make non-exact fields actionable. `missing`,
explicit `null`, `inapplicable`, `error` and exact integer zero remain distinct.
Only comparable exact integer/text/boolean values, including `exact_zero`, can
produce a fact. Unchanged values create none. A target missing from the next
complete joint snapshot gets an explicit `missing/target_disappeared`
observation; it never becomes zero, null, delete or transition fact.

# 3. Canonical observations and identity

Prices retains only accepted SKU-level `original_price_minor`, `discount_bps`
and `seller_price_minor`. The acquisition's nonuniform size representation is
stored as explicit `inapplicable`; the engine does not choose minimum, maximum
or first size.

Bid identity remains exact `nmID + advert_id + placement` with `bid_minor`.
Campaign identity remains `advert_id +` proven exact-one `nmID` with
`campaign_state`, `payment_model` and `payment_unit`. Cardinality zero/many is
stored append-only as `campaign_nm_mapping_cardinality` and produces no
campaign/bid observation or fact. Legacy count-only campaigns get a typed
non-actionable identity-health incident so complete-manifest membership remains
queryable without pretending that detail or nmID exists.

# 4. Diff and campaign creation proof

Every `checkpoint_diff` fact has exact before/after values, observation window
`last_exact.completed_at .. current.completed_at`, deterministic evidence identity
and one checkpoint link. The engine admits that link only when the linked
checkpoint contains the exact same seller/account/target/field observation.

Campaign creation uses the schema-safe existing `campaign_state` atomic field.
When the current complete Ads manifest contains a proven exact-one campaign and
the immediately previous complete manifest proves that `advert_id` absent, one
fact stores `before=absent` and `after=<exact current campaign_state>`. This
reserved internal token is never accepted as a source observation. Prior
missing/partial evidence, an identity incident, legacy presence or lack of an
actionable target is not absence. Campaign disappearance never synthesizes a
deletion fact.

# 5. Interval projection

`project_intervals` is query-only and exact target/field scoped. It walks the
complete checkpoint chain, comparable normalized observations and linked
`checkpoint_diff` facts in deterministic chronology. Stable cursors bind the
seller/account/target/field, so a cursor cannot be replayed against another
projection.

Unbound writer proof is inert for projection. After exact reconciliation the
same fact has one checkpoint link and is admitted when its writer transition
start belongs to that checkpoint interval. The projector rejects missing or
multiple checkpoint links, interval mismatch, before/after mismatch, unproven
campaign absence and missing transition proof. An explicit non-exact
observation closes the prior interval; a later exact fact starts a new interval
after the evidence gap rather than inventing continuous state or an effective
time. It does not calculate outcomes,
causality, performance, recommendations or ML features.

# 6. Excluded scope

The engine itself does not own the scheduler/timer/manual HTTP scan or public
API/UI; those belong to module 57. Excluded from the complete solution remain
additional writer surfaces beyond module 58, manual-pending activation, Balance
bridge, historical import/backfill, public API/UI, outcomes/analytics/ML,
campaign/price/bid WB writes, deletion facts and any production-data apply.

# 7. Verification

`python3 apps/change_registry_baseline_engine_smoke.py` proves partial-before,
first baseline with zero facts, partial-after, exact second diff including zero
and later campaign creation, zero/many fail-closed incidents, exact repeat,
transaction rollback/recovery, strict complete chronology, deterministic stable
cursor projection, inert unbound writer proof and exact race reconciliation in
both proof orderings. The module-57 observer smoke additionally proves explicit
disappearance observations and evidence-gap windows through the same engine;
module-58 smoke proves writer/checkpoint reconciliation.
