# Migration 154 — Finance query-only target CAS

## Incident and invariant

The first ordinary refresh after migration 153 published the Warehouse version
but held the operational SQLite writer transaction for minutes and rolled back
with `Finance target CAS readback differs from snapshot`. Two production-scale
defects were present:

- the exact source-dependency scan still ran after `BEGIN IMMEDIATE`;
- projected SKU rows used numeric nomenclature order while SQLite readback used
  the textual target primary key, so a semantically identical multi-SKU image
  could hash differently.

Warehouse last-good, the WB+FF blend, sale-time FBS frozen WAC, Finance/Partner
covered COGS, historical ready dates and the six mutually exclusive capital
stages are not changed by this correction.

## Corrected execution contract

Schema readiness remains a separate short preflight. Heavy stale-week planning,
canonical channel/location resolution, exact dependency fingerprints and all
target after-images then run on the registry-selected operational database in
SQLite `mode=ro` with `query_only=ON`. They do not open an explicit or implicit
data transaction. In split storage, both persistent files remain read-only; the
connection-local compatibility view is prepared before query-only planning and
does not open a persistent writer transaction.

The exact dependency is calculated once in the dry-run plan and again after
after-image construction on the same query-only contour. Drift fails before a
writer is requested. The read-only observer then captures `data_version` for
every persistent attached database. After `BEGIN IMMEDIATE`, the writer performs
only:

1. the observer's per-database handoff-token comparison;
2. an indexed exact target-before image comparison;
3. scoped delete/insert of reviewed week identities;
4. exact target readback and commit.

The writer does not recalculate a source fingerprint, rebuild a week, scan a
non-target surface or construct an after-image. A commit in the millisecond
snapshot-to-writer gap aborts fail-closed. Unrelated commits made during the
long query projection are accepted when the exact dependency and target image
are unchanged.

Before hashing or applying, every target table is normalized to its declared
SQLite affinity and sorted by its persisted primary key. Duplicate identities,
schema/row-width drift, invalid INTEGER values and any readback mismatch abort
the transaction. Non-target evidence and post-commit dependency/readback remain
query-only. No blind retry, service restart, manual refresh, backfill or
historical rewrite is introduced.

## Acceptance

The focused safety smoke proves query-only/autocommit planning, exact dependency
drift rejection, unrelated interactive-write availability, atomic rollback,
non-target preservation, normalized readback and repeat no-op. The dedicated
production-scale smoke uses `295919` Finance raw rows across 26 weeks, includes
mixed-width numeric SKU identities, commits an interactive status write while
projection is paused, requires projection time above five seconds, writer-lock
time below 1.5 seconds and a zero-stale repeat plan.
