# Supplier reconciliation and vitrina publication chain

This deploy adds the schema-only table `sheet_vitrina_v1_supplier_publication_chain_jobs`. No supplier header, historical signal, canonical row or ready snapshot is changed by schema initialization.

The table persists the exact chain fingerprint, component supplier/publication fingerprints, actor, phase, status, timestamps, terminal report and sanitized error. It is execution audit/progress, not a second source of supplier or vitrina truth. Supplier truth remains in its header/audit rows, canonical truth remains in canonical tables, and published table truth remains in `sheet_vitrina_v1_ready_snapshots`.

`apps/supplier_shipment_publication_chain.py` first creates one disposable supplier candidate, plans ready-snapshot publication against that expected post-correction database and hashes the strict order and intermediate digests. Dry-run is read-only. Apply requires the unchanged supplier, publication and chain fingerprints plus explicit fresh backup roots. If the supplier stage fails, its transaction rolls back. If a later publication stage fails after supplier commit, the runner restores and verifies the fresh pre-supplier backup, then records terminal failure. Wildcards, force, partial canonical rebuild and reuse of a proof backup as the fresh apply backup are unsupported.
