# Finance Liquidity cash: dormant rollout and future activation

## Current state

This release is code-only.  The candidate unit and nginx fragment under
`artifacts/finance_liquidity_cash/dormant/` are not in the hosted target's
managed-unit list or nginx manifest.  No service, timer, route, database,
schema, user grant, cash account, opening balance or financial document is
created by deploy, import, startup or GET.  `8766` remains Data MCP; the future
sidecar uses loopback `127.0.0.1:8767`.

## Code deploy

Use the ordinary release train without adding the candidate unit or fragment to
the active managed target.  Verify the deployed code remains default-off:
`FINANCE_LIQUIDITY_ENABLED`, `FINANCE_LIQUIDITY_READ_ENABLED` and
`FINANCE_LIQUIDITY_WRITE_ENABLED` are not `1`; `/finance/` and `/v1/finance/`
are not published; no Finance store exists.  This check must not bootstrap a
store or start the sidecar.

## Separate business activation (requires a new decision)

Before any money data, the business owner chooses and records the cash accounts,
responsible people, currencies, opening amounts and dates, evidence, and exact
explicit Finance grants. Roles, bootstrap admin and supplier never imply a
Finance grant. Record the recovery owner and successful restore/readback
procedure. For a new empty cash-store path, prove that the target is absent
before bootstrap; no backup of that absent target is needed. Before changing
existing irrecoverable data, including existing auth grants, take and verify an
appropriate backup and recovery plan.

After that approval, in a controlled change:

1. Recheck the absent new cash-store target and the recovery procedure. If the
   activation changes existing auth or money data, take and verify its backup
   before that change.
2. Materialize the isolated store exactly once with
   `apps/finance_liquidity_http.py --db <isolated-path> --bootstrap`; do not use
   main, supply, CNY, weekly Finance or an HTTP GET for bootstrap.
3. Install the candidate unit and create explicit post-unit activation drop-ins
   for both `wb-core-finance-liquidity.service` and
   `wb-core-registry-http.service`. Set
   a dedicated flags-only `EnvironmentFile` as the last environment file in
   both drop-ins. Its explicit `FINANCE_LIQUIDITY_ENABLED=1` and
   `FINANCE_LIQUIDITY_READ_ENABLED=1` let the sidecar read and the main shell
   reveal its guarded link. `EnvironmentFile` values override `Environment=`;
   later environment files override earlier files. Verify the effective flags
   without logging the shared environment or its secrets. Initially set
   `FINANCE_LIQUIDITY_WRITE_ENABLED=0` explicitly in the sidecar activation
   file. Keep the signed-session secret in
   `/opt/wb-ai/.env`; do not copy it into the flags-only file. The candidate
   unit's zero values are defaults, not protection against conflicting values
   in the shared environment. Dormant code release is protected by leaving
   this unit and these routes outside the active target.
   In the sidecar file also set `FINANCE_LIQUIDITY_ORIGIN` to the exact public
   HTTPS origin used by the existing signed-session website, without a path
   or trailing slash. The loopback default is for local testing; it rejects
   browser writes through the public reverse proxy. Verify that another
   origin is rejected and the intended origin passes the CSRF check.
4. Reload systemd after installing the unit and drop-ins, then start the
   isolated loopback sidecar and read back `/v1/finance/capabilities`
   with an explicit grant, a supplier session, and a no-grant session. Confirm
   that canonical auth is read-only and there are no Finance writes or
   synchronous dependencies from main, supply or CNY.
5. Restart the main registry HTTP service through the ordinary governed process
   so the main process receives its activation
   flags. Only then add the candidate nginx routes through the governed
   hosted-route process and verify `/finance/` end-to-end. After those checks,
   within the already authorized activation scope, set
   `FINANCE_LIQUIDITY_WRITE_ENABLED=1` in the sidecar activation file and
   restart that sidecar through the ordinary governed process. Read back its
   write capability before entering the approved business data.

If any check is ambiguous, leave routes unpublished and the sidecar stopped.
