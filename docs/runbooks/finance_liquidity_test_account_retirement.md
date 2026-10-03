# Offline retirement of isolated TEST cash accounts

This operation hides selected TEST cash accounts and their document and reconciliation history from ordinary Finance lists. Posted documents, ledger entries, seals, opening anchors, reconciliations and prior audit events remain in the store. The backup is the recovery copy. Do not use this route for seeded cash accounts or a store outside `isolated_test` mode.

1. Identify the exact store, access configuration, account IDs and intended backup path. Stop the Finance sidecar and independently verify that its service is inactive. Keep it stopped until readback is complete.
2. Run `preview` with the exact access configuration, database path, store ID and one `--account-id` per selected TEST cash account. Review the names, counts and `fingerprint`. Any document or ledger edge to an unselected account rejects the candidate.
3. Run `apply` once with the same arguments, an absent absolute `--backup` path, the preview `--fingerprint`, a fresh unique `--operation-id`, the operator `--actor`, and `--service-stopped`. The command creates and verifies a read-only backup before the transaction. A source change after preview or during backup rejects the write.
4. If the apply response is missing or ambiguous, run `readback` with the exact same account IDs, fingerprint, operation ID and actor. Readback verifies the durable receipt, tombstones, audits, ledger integrity and database integrity. Do not submit a new operation ID to resolve an ambiguous response.
5. Check ordinary account, document and reconciliation lists, then restart the sidecar. Retain the backup and operation receipt under the task evidence location.

The CLI is `python3 apps/finance_liquidity_test_retirement.py {preview|apply|readback}`. Use `--help` for exact flags. The account IDs, fingerprint, backup path and operation ID are operator inputs; they are not stored in this runbook or source code.
