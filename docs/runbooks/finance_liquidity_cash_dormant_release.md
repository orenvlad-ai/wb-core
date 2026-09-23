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
  This candidate requires schema version 2. Bootstrap is only for a verified
  absent target; any existing earlier schema is refused and needs a separately
  accepted migration rather than in-place use.

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
   schema. Require schema version 2, empty business tables, canonical resolved
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
