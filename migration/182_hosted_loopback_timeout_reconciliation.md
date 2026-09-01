# Migration 182: hosted loopback timeout reconciliation

An exact runtime deployment can finish while a legitimate operational SQLite
writer makes the remote loopback surface exceed its bounded transport timeout.
That timeout is now reconciled without repeating rsync, dependencies, service
restart or any business-data mutation.

The verifier requires canonical exact-SHA deployment metadata, runtime marker,
active process, auth boundary and light probes through read-only transport
reconciliation. Only after that complete binding may it repeat the query-only
loopback surface once. SSH exit 255 retains its existing repair-capable lane;
ordinary HTTP and semantic failures remain terminal.

Verification:

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py
python3 apps/hosted_runtime_transport_reconcile_smoke.py
```
