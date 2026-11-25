"""Apply verified spend to native dated rows while preserving other quality reasons."""
from dataclasses import replace

from packages.application.ads_spend_verification import snapshot_amount
from packages.application.vitrina_economics import METRICS, SOURCE

REASONS = {"no_product_row": "В дневной выгрузке WB нет строки по этой карточке.",
           "spend_mismatch": "Расход в дневном отчёте WB расходится с данными витрины.",
           "conflicting_exports": "Дневные выгрузки WB содержат разные расходы по кампаниям."}


def same_spend(value, decision):
    try:
        return snapshot_amount(value) == decision.amount
    except ValueError:
        return False


def verified_presentation(rows, proofs):
    if not proofs:
        return rows
    updated = []
    for row in rows:
        scope, _, metric = row.row_id.partition("|")
        if not scope.startswith("SKU:") or metric not in ("ads_sum", *METRICS):
            updated.append(row)
            continue
        cells = dict(row.presentation_by_date)
        for day, decisions in proofs.items():
            decision = decisions.get(scope[4:])
            cell = cells.get(day) or {}
            if not decision or cell.get("source_as_of_date") != day or cell.get("quality_state") != "partial":
                continue
            evidence = dict(cell.get("evidence") or {})
            if metric == "ads_sum":
                if cell.get("source") != "ads_partial_observed_v1":
                    continue
                observed_value = row.values_by_date.get(day)
            else:
                if (cell.get("source") != SOURCE or cell.get("calculation_contract") != "catalog_economics_v1"
                        or cell.get("state") != "unconfirmed" or evidence.get("operand_date") != day
                        or evidence.get("partial_operands") != ["ads_sum"]
                        or (evidence.get("forward_cost_segments") or {}).get("precision_pending")
                        or row.values_by_date.get(day) in (None, "")):
                    continue
                observed_value = (evidence.get("operands") or {}).get("ads_sum")
            if decision.status in REASONS:
                reason = REASONS[decision.status]
                cells[day] = {**cell, "quality_reason": reason, "reason": reason}
                continue
            if not same_spend(observed_value, decision):
                continue
            evidence["spend_receipts"] = decision.receipts
            evidence.pop("partial_operands", None)
            reason = "Расход карточки за день сверен с товарной статистикой WB."
            detail = {**cell, "evidence": evidence, "reason": reason, "quality_reason": reason,
                      "completeness_state": "complete", "missing_sku_count": 0}
            if metric == "ads_sum":
                detail.update(source="wb_verified_daily_spend", quality_state="confirmed",
                              quality_label="Расход подтверждён")
            else:
                detail.update(quality_state="management_estimate", quality_label="Управленческая оценка")
            cells[day] = detail
        updated.append(replace(row, presentation_by_date=cells))
    return updated
