# Migration 107 — superseded Finance retro-cost / Partner package contract

## Status

`SUPERSEDED / APPLY PERMANENTLY REVOKED`

This file preserves the historical identity of the revision deployed through PRs `#698` and `#707`. It is not an executable production migration contract.

The former plan fingerprint

`sha256:621323d6f03759cb8685dfffe20639fa18a16c7b5f6a5b1685205a579c6bbf2d`

is permanently revoked. It cannot be used for approval, apply, readback or recovery. The former `business-approved-backfill` CLI fails closed, and hosted `finance-retro-*` commands are no longer exposed.

The following superseded assumptions must not return:

- an independently valued `wb_finance_retro_cost_map` business source;
- legacy `COST_PRICE` in current management COGS;
- first-later-date/average/other-SKU cost substitution for a missing 01.07 value;
- blanket paid-acceptance/transit addback without supply/cost-layer lineage;
- combined agent remuneration/acquiring presentation;
- root `ads_compact` payload treated as missing;
- synchronous Partner preview scan of all raw Finance rows;
- Partner finalization, raw Finance export or evidence ZIP in the active UI scope.

Current implementation and production operations are governed only by:

- `migration/108_finance_canonical_cost_partner_ui_recovery.md`;
- module 44 Finance contract;
- module 50 Partner Report contract.

No production business data was changed by the revoked plan before this supersession.
