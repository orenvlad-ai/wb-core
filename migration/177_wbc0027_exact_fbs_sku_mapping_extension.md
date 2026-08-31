# Migration 177 — WBC0027 exact canonical FBS SKU mapping extension

## Bounded diagnosis

The immutable source diagnosis remains bound to runtime
`999c53285ca684bd3b1d2caa5992594f8870ffc7`, StoreRegistry generation
`operational-c54072027f14f90b374b`, manifest
`sha256:8cdd437b7357042092a8be2e1fdce028af2444c81a464465dbadd557b57a2ffb`,
schema revision `987` and cutover `ffcut_d2816d894a75390dcaa6514c0a96`.
Its external source-snapshot identity
`sha256:ca2117e1c33a81df62d9de68c0f6e7f652d755fef828a91a88a8592ae69db6f7`
is an accepted opaque binding. Its unavailable preimage is not reconstructed or
claimed to be computed by this release.

The independently verifiable exact mapping tuple is canonical JSON with sorted
keys, UTF-8 and no insignificant whitespace:

```json
{"contract":"wbc0027_exact_fbs_sku_tuple/v1","source_barcode":"2044193046047","source_chrt_id":610113487,"source_nm_id":428855758,"source_sku":"(Matte) iPhone 14 Pro","target_nm_id":428855758}
```

Its digest is
`sha256:680a220d3bb88741723956ba90d84a12ce57b44ec17d2dc1c2233c4c54c38968`.
Both the opaque diagnosis digest and this tuple digest are mandatory and
independently checked. Current admission requires exactly one source tuple, one
active non-hidden nomenclature owner, zero active/all canonical mappings and
target `428855758`. Roster membership, direct `nm_id`, fuzzy/name matching and
inference are not fallback sources. Existing, duplicated, inactive, ambiguous,
foreign-facility or foreign-target mappings fail closed; no row is overwritten.

## Typed recovery blockers

The lifecycle recovery planner no longer discards a mapping-resolution failure
with `continue`. It retains privacy-minimized typed facility × SKU rows with
identity and mapping error codes, external/tuple digests, distinct order/status
cardinality and digests of the exact order/status identity sets. The accepted
blocked source is:

- Moscow `fff_d67e8c823d5f81dd988d00dbfea6 / 428855758`: 213 orders,
  1,094 statuses;
- Orenburg `fff_2579bb2741ed4ab23b11bb4c4183 / 428855758`: 8 orders,
  41 statuses.

`exact_four_group_coverage_missing` remains blocking while the coverage object
retains both missing groups alongside the two already resolvable groups. Missing
typed evidence is itself a blocker.

After an exact mapping exists, the separately gated lifecycle recovery may
append deterministic matched re-evidence inside its own one-submit transaction
before canonical replay. This is not part of the mapping operation. It uses the
original immutable unmatched tuple plus the current exact mapping; it does not
use a roster/direct-`nm_id` fallback.

## Query-only hypothetical rehearsal

`mapping-rehearsal` opens the production store `mode=ro` with
`PRAGMA query_only=ON`. It overlays the exact candidate only inside the existing
ephemeral coherent preview, removes the scratch after preview and emits no
durable plan. The rehearsal must resolve the original four groups, preserve all
source status identities and build all 15 same-date history captures. It reports
zero production mapping inserts, recovery writes and history writes. Missing
blocker evidence or any tuple/owner/mapping/storage/cutover drift blocks before
the rehearsal can qualify.

## Separate mapping-only Apply

`apps/wbc0027_fbs_mapping_extension.py` is dry-run by default. The default-off
Production Apply profile obtains two consecutive identical material witnesses.
Each candidate is stored as a private mode-0600 file under the operation-owned
mode-0700 evidence directory. Apply repeats the exact material CAS before the
writer lock and under `BEGIN IMMEDIATE`, persists an exclusive-create private
before-image, then enables a SQLite authorizer that permits only one `INSERT`
into `sheet_vitrina_v1_wb_supplies_fbs_identity_mappings`. `total_changes` and
exact row readback must both equal one.

The operation has no lifecycle debit, balance, recovery/history, public, outbox
or WB write primitive. The mutation command is issued at most once; ambiguous
transport permits only query-only readback, never a blind retry. Lifecycle and
15-date history recovery remain a different later Production Apply profile and
passport.

## Release boundary

This PR is `live_runtime`. Trusted Release Runner deploys inert code and performs
zero business-data mutation. It does not publish an OWNER passport and does not
dispatch either mapping Apply or lifecycle recovery Apply. The prior blocked
operation `production-goal-v1-431ee99b802a77a448f1bce71c9221aa` and its
run/artifact/comment identities remain terminal audit evidence and are never
reused.
