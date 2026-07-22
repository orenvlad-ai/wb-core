# Partner marketing single-count recovery

## Scope and production diagnosis

This is the formula/classifier recovery of LOOP root #731 after the read-only
stage in migration 111. The production diagnostic selected server-owned
`nmId=245720334`, 29 Finance weeks and streamed 2,414,082 immutable rows. It
found no logical duplicates or stored/raw identity mismatches.

The former Partner residual was built from `account_metrics.profit_period_expenses`
after removing only transit, WB Jam and paid services. Finance marketing was
therefore already recognized correctly but lost at the next Partner allocation
stage. Account-level Finance marketing of 28,806,787 ₽ produced an allocated
Partner contribution of 1,154,641.4092 ₽. The visible weekly values included
40,004.4148 ₽ on 2026-06-08, 43,881.4837 ₽ on 2026-06-15,
44,950.7537 ₽ on 2026-06-22, 40,719.3405 ₽ on 2026-06-29,
40,572.4751 ₽ on 2026-07-06 and 44,903.0582 ₽ on 2026-07-13.
Those values explain the observed material `Прочие удержания` amount: the
same economic marketing expense was deducted once through `ads_compact` and
again through the Finance residual.

The remaining production operations are named review-point charges/refunds,
storage and one signed WB adjustment. Three apparent `cpm` candidates were
false positives caused by opaque identifiers embedded in review-point names;
no production operation justified broadening the marketing classifier.
Three negative deduction rows total −862,009.2000 ₽. The former `abs()` path
would report +862,009.2000 ₽ and overstate expense by 1,724,018.4000 ₽.

## Active formula contracts

- Finance classifier: `wb_finance_weekly_classifier_v3_signed_review_points`;
- Finance per-SKU projection: `wb_finance_weekly_sku_aggregate_v4`;
- Finance profit semantics: `wb_finance_profit_attributed_capitalization_v3_signed_deductions`;
- Partner schema/formula: `partner_report_v4` /
  `partner_report_profitability_ui_first_v4`;
- Partner provenance: `partner_report_provenance_v3`.

Finance continues to show marketing as its own row and continues to expose
expenses with and without marketing. Deduction values retain their official
sign. Production review-point names (`Баллы за отзывы`, `Списание за отзыв`)
use a deterministic `review_points` category. A negative transit deduction or
acceptance charge is not eligible to become a positive capitalization
candidate.

Partner uses accepted closed-day `ads_compact/fullstats` at exact
`date + nmId` as the only marketing expense. Direct and account-level Finance
marketing contribute zero to Partner expenses and margin. Account expenses are
allocated by the unchanged revenue ratio and routed explicitly:

- agent remuneration, acquiring, logistics, storage, non-capitalized
  acceptance and penalties/corrections go to existing main rows;
- non-capitalized transit, WB Jam, paid services, review points and genuine
  `other_deductions` go to named subrows;
- the explicit categories must reconcile with
  `profit_period_expenses − positive_adjustments − marketing` at 0.0001 ₽.

There is no balancing residual. UI and XLSX receive the same ordered category
definitions and omit every category whose exact selected-period total is zero.
In particular, a marketing-only period has no zero `Прочие удержания` row.
Internal provenance retains direct/allocated amounts, allocation coefficient,
source digests and the excluded Finance-marketing amount.

## Source and release boundary

The code deploy does not itself mutate production data. Existing derived
Finance projections become stale because the classifier/formula versions
changed. They must be rebuilt only by the canonical
`finance-canonical-dry-run/apply/readback` workflow after a fresh exact human
approval. The 61 missing accepted ads dates must be recovered only through
the migration-111 historical ads plan/apply/readback workflow; no missing SKU
or date may be synthesized as zero. Partner settings are non-target.

The canonical temporal cost policy stays `canonical_our_wb_cost_temporal_policy_v4`.
Vitrina and Proxy 3 are readback invariants, and TOTAL margin remains
`SUM(profit) / SUM(revenue)`, never an average of SKU margins.

## Verification

Regression coverage includes Finance marketing disclosure, signed review
refunds, classifier false-positive resistance, Partner single-count ads,
direct/account marketing exclusion, explicit main/subrow allocation,
marketing-only zero-row omission, cent conservation, shared UI/XLSX category
definitions, desktop/narrow/keyboard tooltip behavior, and XLSX rejection of
hidden sheets, external links and macros. Production acceptance remains
fail-closed until projections and ads sources are read back, preview is ready,
the workbook is downloaded/opened/reconciled, and the latest LOOP gate is the
one accepted.
