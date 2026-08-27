# Migration 159 terminal addendum: WBC0008 block 024

This is the authoritative repo-only terminal-receipt addendum to
`159_root_storage_warm_archive_wbc0008_006.md`. It does not change the archive
worker contract or authorize a new production effect.

## Existing submitted operation

The mode `warm-archive-receipt-reconciliation` is valid only for the already
submitted exact-six operation whose immutable Production Apply receipt is
exactly `blocked/post-submit-readback-not-reconciled`, with one submit and a
succeeded attempt-1 detached job. It binds the exact source PR/run/artifact
name and SHA-256, authorization and blocked comment ids, release/readiness/
operation/job and qualified-manifest identities, deployed SHA, plus the new
merged trusted-main `repo_only/done` Release receipt. No new owner authorization
is required: under the authorization router this is same-operation query-only
`AUTO_CONTINUE`, not a new production effect.

Exactly one bounded SSH probe is permitted after GitHub-only preflight. It runs
with `PYTHONDONTWRITEBYTECODE=1` and only reads immutable journal/job/manifest
records, the six retained archive/manifest pairs and their saved proof digests,
direct source/destination/non-target/StoreRegistry/journald state, current
capacity, natural monitor and systemd show/config output. Readiness, a second
submit, apply/job/archive execution, the existing `readback_batch`, decompression
or full restore, temporary files, lock acquisition, service/timer action,
SQL/file writes and unlink are absent.

Any source/job/journal/hash/proof drift, source presence, missing/foreign/temp
destination object, active job/lock, unstable or below-floor capacity, stale or
non-normal monitor, 27/12/service, journald/non-target/StoreRegistry drift, or
nonzero Promo/business/non-target effect blocks `done`.

## Receipt and terminal acceptance

Full canonical evidence is uploaded as one immutable artifact first. Only then
may the Actions bot append one distinct compact supersession marker to the
original operation PR, binding the untouched source blocked comment/artifact,
new release/artifact/evidence digests and
`done/reconciled_existing_operation`. An exact existing marker is verified back
through its artifact and returns `already_terminal` without SSH or publication;
duplicate, foreign or different existing evidence fails closed. This block's
production mutation count is structurally and observably zero.

`COMPLETE` requires that immutable `done` artifact and compact marker for the
same existing operation, plus every terminal fact in migration 159: six absent
sources, six retained archives and manifests with current hashes and saved
stream/full-restore/SQLite proof digests, six completed unlink intents and exact
reclaimed bytes, stable capacity above root and Finance floors, fresh normal
natural monitor, healthy 27 units/12 pairs, no active sanitation job or held
lock, preserved journald/non-target/StoreRegistry state, and zero Promo,
business-data, non-target or block-024 production mutation.

Required additional check:

- `python3 apps/wbc0008_warm_archive_receipt_reconciliation_probe_smoke.py`
