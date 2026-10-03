# Finance Liquidity cash: dormant release and isolated TEST pilot

## Current state

The dormant artifact remains the default-off reference. The separately reviewed
pilot candidate uses only the dedicated TEST store
`/opt/wb-core-runtime/state/finance-liquidity-pilot/finance-liquidity-pilot.sqlite3`,
unit `wb-core-finance-liquidity-pilot.service`, loopback port `8767`, and the
existing `/finance/` and `/v1/finance/` proxy renderer. It never uses the
reserved production cash path
`/opt/wb-core-runtime/state/finance-liquidity/finance-liquidity.sqlite3`.

The single non-secret access source is
`artifacts/finance_liquidity_cash/pilot/finance-liquidity-pilot-access.json`.
It binds the canonical env-bootstrap username `owner`, explicit
`finance_admin`, the exact pilot store, stable store id, and the visible
`ТЕСТОВАЯ БАЗА · ИЗОЛИРОВАННЫЕ ДАННЫЕ` label. Main WebCore and the sidecar read
that same file on every authorization check. Missing, revoked, malformed,
unknown-capability or username-mismatched content grants nothing. The sidecar
still validates the signed unexpired admin session and the pinned operational
store before admitting this exact env principal; ordinary runtime users retain
their existing SQLite-grant path. No password, password hash or session secret
is copied into the pilot files.

## Dormant code deploy

Use the ordinary release train without adding the candidate unit or fragment to
the active managed target.  Verify the deployed code remains default-off:
`FINANCE_LIQUIDITY_ENABLED`, `FINANCE_LIQUIDITY_READ_ENABLED` and
`FINANCE_LIQUIDITY_WRITE_ENABLED` are not `1`; `/finance/` and `/v1/finance/`
are not published; no Finance store exists.  This check must not bootstrap a
store or start the sidecar.

## Technical safeguards required at activation

Dormant code approval does not approve monetary operation. The accepted
candidate must retain all of these safeguards before the sidecar is installed
or started, routes are published, a store is bootstrapped, access is granted,
or money is entered:

- Every monetary read and effect-bearing replay validates exact receipt
  membership, count and digest, the expected business cardinality, and complete
  posted-document ownership. Zero opening has zero transactions;
  post/send/complete/cancel has one; completed-transfer reversal matches its
  exact two original phases; opening replacement matches its zero/nonzero
  reversal and replacement sides.
- Allowed terminal document transitions preserve every semantic field. The
  transition is operation-bound and requires its effect manifest plus sealed
  transaction before status/state changes. Entries, transactions, seals,
  operations, audit events and opening anchors remain append-only.
- Reconciliation remains a non-ledger observation. Record and resolution rows
  are bound to their exact operation scope. SQL rejects NULL resolution kinds,
  self/cross-account/earlier/nonmatched links, empty explanations and receipt
  owner mismatch; the service additionally restricts override to an admin.
- Operational authorization reads current grants with a read-only APSW
  connection. `SQLITE_FCNTL_HAS_MOVED` is checked on that exact grant-reading
  connection before and after the read, together with current manifest,
  pathname and generation identity revalidation. Missing APSW, unsupported
  file-control, descriptor movement or identity drift fails closed.
- An isolated cash store must report the exact current Finance schema version.
  The directory candidate requires schema version 3. Bootstrap is only for a
  verified absent target; GET and service start never seed or migrate. An
  existing v2 TEST store needs the explicit offline procedure below before v3
  code is released. No REAL store is created or activated by this change.

## Explicit v2 TEST migration gate for directory code

This code change does not run the migration. Freeze the exact candidate and
obtain normal owner approval before changing the existing TEST store. Stop TEST
writes and sidecar; verify its exact canonical database path, v2 schema,
`PRAGMA integrity_check`, `PRAGMA foreign_key_check`, balances, document count
and ledger receipts. Rehearse on an isolated copy first. For the actual stopped
TEST store, provide a new, absent backup pathname on the protected volume:

`apps/finance_liquidity_http.py --db <exact verified TEST sqlite path> --migrate-v2 --backup <new protected backup sqlite path>`

The CLI writes an SQLite backup before one transactional schema change. It
refuses a non-v2 source, existing backup, symlink source, or second run. Retain
backup and evidence. Before starting v3 code, read back schema v3, the three
seeded RUB cashboxes with uninitialized balances, 23 seeded articles, zero
seeded counterparties, unchanged existing document/ledger counts and balances,
`integrity_check`, `foreign_key_check`, and read-only Finance API results. The
seed keeps edited labels and deleted tombstones.
For pre-v3 posted documents, also verify the append-only migration snapshot of
each referenced article name: it is the name known *at migration time*, not a
reconstructed historical name. Existing expense/income articles receive
neutral legacy analytic classes; no posted document row is updated.

If migration fails before v3 writes, keep the sidecar stopped; the transaction
rolls back and the v2 backup remains. Return to v2 after successful migration
only before any v3 document or directory write: stop the service, verify this
condition, restore the retained backup by a separately reviewed file
replacement, and read back schema v2 and original balances. After v3 writes,
do not restore an older backup over new facts; use a reviewed forward repair.
This runbook does not itself authorize TEST or REAL migration.

## Additive category-group extension for an existing v3 TEST store

This extension keeps the core `schema_version=3` and adds only group display
metadata. Code start, GET, and the canonical deploy do not install it. The new
code accepts either a fully absent extension (group editing disabled) or a
complete `category_groups` version-1 marker with its tables, indexes, column
and foreign key. Partial/mismatched extension fails closed. A fresh bootstrap
and an explicit v2 migration install it transactionally; an existing v3 TEST
store needs the separate command below. No REAL store is in scope.

1. Let the usual PR Gate, canonical prepare-deploy checks and Finance probe run
   with the existing v3 store unchanged; new code reads it and ordinary money
   operations remain available. Deploy the reviewed code through the standard
   Release Runner. Do not mutate the database to make an intermediate Gate
   state pass.
2. In the agreed Finance TEST window, hold only the Finance TEST writer/sidecar
   briefly. Recheck the exact canonical TEST database path, schema v3, complete
   absence of the extension, `integrity_check`, `foreign_key_check`, latest
   document/operation counts, and balances. Select a new absent backup path
   on the protected volume. Do not use a stale count from the code-release
   moment: TEST operations may have continued meanwhile.
3. Run exactly once against that verified stopped store:
   `apps/finance_liquidity_http.py --db <exact TEST sqlite path> --install-category-groups --backup <new protected backup sqlite path>`.
   The command validates the backup, then installs schema features, seed
   groups, assignments by stable seed codes, and the completion marker in one
   transaction. It leaves posted documents, ledger rows and balances untouched.
4. Read back marker version 1, six seed groups, active and unassigned articles,
   unchanged documents/operations/balances and the same integrity checks;
   confirm Finance HTTP readback and resume the TEST sidecar. Keep the exact
   backup and pre/post evidence. A repeat invocation with a complete marker is
   read-only; a partial extension is never auto-repaired.

If installation fails before commit, keep the sidecar held and inspect the
original v3 store; its transaction rolls back. If the extension committed but
new code must be rolled back, the previous v3 Finance code can read and perform
ordinary fixture operations against the additive schema (verified in a local
old-code compatibility test); it ignores groups. Prefer this code rollback
without restoring an older database. Restoring the backup requires a separate
review of exact paths and proof that no newer TEST operation would be lost;
never overwrite later user operations with the pre-extension backup.

Before activation, rerun the synthetic cash, auth, HTTP and browser checks and
verify the sanctioned live signed session, explicit grants, supplier denial
and revocation against the selected operational owner.

## Reviewed isolated TEST pilot activation

The approved pilot contains synthetic data only. Do not enter real cash
accounts, opening balances, dates or documents. Before publication, freeze and
independently review the exact source candidate, including the access JSON,
shared flags file, unit, hosted target and route manifest.

1. Recheck the active host and completed deployed SHA. Prove read-only that the
   exact pilot path and its resolved path are identical and absent, no existing
   parent component is a symlink, the reserved production cash path remains
   absent, `8767` is unused, the pilot unit is absent and both public routes are
   `404`. Stop on any mismatch. An absent new pilot store needs no data backup.
2. Immediately before the governed release, materialize only the reviewed pilot
   path once with the existing explicit CLI:
   `apps/finance_liquidity_http.py --db /opt/wb-core-runtime/state/finance-liquidity-pilot/finance-liquidity-pilot.sqlite3 --bootstrap`.
   Bootstrap is never part of GET, import, service start or the unit. If the
   command result is ambiguous, do not send it again: read the same path and
   schema. Require schema version 3, three uninitialized seed cashboxes, 23
   seed articles, no counterparties or monetary documents, canonical resolved
   path, and an unchanged absent production cash path.
3. Run the ordinary release for the independently accepted SHA. It installs the
   pilot unit and the shared flags file as the final EnvironmentFile for both
   main and sidecar. The hosted deploy publishes the two existing-renderer
   routes before it restarts and reconciles the managed services, so the TEST
   route may briefly return `502` during startup. The flags bind the public
   origin and access JSON; no secret values are present. A failed deploy stage
   blocks completion rather than triggering another bootstrap.
4. After the release completes, read back the effective unit arguments, exact
   PID, listener, route map, loopback backend, actual canonical database path,
   TEST label and store id. Do not perform any UI write before every readback
   succeeds.
   With the current signed `owner` cookie, require `/v1/finance/capabilities`
   to report `finance`, `finance_operate`, `finance_admin`, write enabled, the
   exact TEST label and `store_id=finance-liquidity-pilot`. Require `/finance/`
   to show the TEST banner before every money action. A supplier, another admin,
   an expired session, a mismatched owner name and a revoked access file remain
   denied. The canonical operational auth reader remains query-only.
5. Run only the accepted synthetic scenario and reconcile its final balances.
   Retain the pilot store and evidence after the check; do not delete them as
   cleanup.

Restore is a reviewed source change through the same release train: set the
access file to `enabled=false`, set all three Finance flags to `0`, remove both
public routes, remove the pilot unit from `managed_systemd_units`, add its exact
name to `retired_systemd_units`, and remove the pilot flags EnvironmentFile from
the main unit. Read back no Finance link/capability, `404` routes, no listener
and an inactive/absent pilot unit. Keep the synthetic database in place for
evidence unless a later reviewed retention decision says otherwise. Do not
alter the operational user table, shared auth secrets or the production cash
path during activation or restore.

If any check is ambiguous, stop writes and use readback of the same operation;
do not repeat bootstrap or substitute a direct unmanaged config edit.

## Offline retirement of isolated TEST cash accounts

This separate operation hides selected TEST cash accounts and their document
and reconciliation history from ordinary Finance lists. Posted documents,
ledger entries, seals, opening anchors, reconciliations and prior audit events
remain in the store. The backup is the recovery copy. It refuses seeded cash
accounts and stores outside `isolated_test` mode.

1. Identify the exact store, access configuration, account IDs and intended
   backup path. Stop the Finance sidecar and independently verify that its
   service is inactive. Keep it stopped until readback is complete.
2. Run `preview` with the exact access configuration, database path, store ID
   and one `--account-id` per selected TEST cash account. Review the names,
   counts and `fingerprint`. A document or ledger edge to an unselected
   account rejects the candidate.
3. Run `apply` once with the same arguments, an absent absolute `--backup`
   path, the preview `--fingerprint`, a fresh unique `--operation-id`, the
   operator `--actor`, and `--service-stopped`. The command creates and verifies
   a read-only backup before the transaction. A source change after preview or
   during backup rejects the write.
4. If the apply response is missing or ambiguous, run `readback` with the
   exact same account IDs, fingerprint, operation ID and actor. Readback
   verifies the durable receipt, tombstones, audits, ledger integrity and
   database integrity. Do not submit a new operation ID to resolve an
   ambiguous response.
5. Check ordinary account, document and reconciliation lists, then restart
   the sidecar. Retain the backup and operation receipt under task evidence.

The CLI is `python3 apps/finance_liquidity_test_retirement.py {preview|apply|readback}`.
Use `--help` for exact flags. Account IDs,
fingerprint, backup path and operation ID are operator inputs and are not
stored in this runbook or source code.
