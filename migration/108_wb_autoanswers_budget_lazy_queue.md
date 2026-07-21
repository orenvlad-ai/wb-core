# WB autoanswers schema v4 — incident controls and zero-cost templates

Status: release candidate. Deployment and schema preparation run under process-local `WB_AUTOANSWERS_FORCE_OFF=true`; final production state is `master_enabled=true`, `mode=manual`, force-off false.

## Additive migration

Schema v4 adds without deleting or rewriting queue/audit/publication evidence:

- hourly, paid-review, concurrency, role-call and materialized-queue limits;
- processing kind and transition-run identity on AI jobs;
- transition-run identity on publication jobs;
- expiry, release reason and settlement timestamp on reservations;
- a provider-call-start marker that distinguishes safe pre-call lease recovery from unknown post-call cost;
- mandatory run caps and pause reason on transition previews/sweeps;
- append-only budget adjustments;
- runtime scheduler/AI/publication timestamps and stop reason.

The incident's terminal `$1.00` reservation was incorrectly settled as actual spend without provider usage evidence. Migration does not mutate that historical reservation. It appends one idempotent negative adjustment keyed by processing key, so confirmed actual spend is corrected while the original evidence remains auditable. The absolute adjusted amount is also exposed as unverified legacy cost and continues to consume the applicable safety caps; uncertainty is not converted into extra available budget.

Existing published answers, publication attempts, exact readbacks and audit events are immutable. No down migration is required. Code rollback leaves additive columns/tables inert.

## Release gates

1. Verify a coherent integrity-checked schema-v4 backup before DDL.
2. Verify deployed SHA, Node manifest and frozen identity.
3. Activate `manual`; do not resume the prior auto-all sweep.
4. Prove zero background AI claims and zero new publication attempts for five scheduler ticks.
5. Run authenticated read-only UI acceptance for queue/cost dashboard, filters and responsive dark answer box.
6. Do not call OpenAI or WB POST/PATCH.

## Recovery

Set `WB_AUTOANSWERS_FORCE_OFF=true` for emergency stop. Preserve every queue, lease, reservation, result and audit row. A possible prior write may perform readback only. Restore the verified pre-v4 backup only for demonstrated corruption; otherwise keep additive schema and roll code back.
