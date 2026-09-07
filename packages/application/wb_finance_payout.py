"""Calculated settlement and offline official reconciliation; no bank transfer claim."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

LOYALTY_KEYS = ("loyalty_points", "loyalty_fee")
SUMMARY_FIELDS = (
    "reportId",
    "dateFrom",
    "dateTo",
    "createDate",
    "currency",
    "reportType",
    "forPaySum",
    "deliveryServiceSum",
    "paidStorageSum",
    "paidAcceptanceSum",
    "deductionSum",
    "penaltySum",
    "additionalPaymentSum",
    "cashbackAmountSum",
    "cashbackCommissionChangeSum",
    "bankPaymentSum",
)


def money(value: Any) -> Decimal:
    if value is None or value == "" or isinstance(value, bool):
        raise ValueError("finance_payout_missing_amount")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("finance_payout_invalid_amount") from exc
    if not result.is_finite():
        raise ValueError("finance_payout_invalid_amount")
    return result


def text(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001")))


def loyalty(row: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    amounts = tuple(
        money(row.get(k) or "0") for k in ("cashbackAmount", "cashbackCommissionChange")
    )
    doc = str(row.get("docTypeName") or "").casefold()
    if any(amounts) and doc not in ("продажа", "возврат"):
        raise ValueError("finance_loyalty_document_sign_missing")
    sign = -1 if doc == "возврат" else 1
    return amounts[0] * sign, amounts[1] * sign


def standalone_adjustment(row: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    if str(row.get("docTypeName") or "").casefold() in ("продажа", "возврат"):
        return Decimal(0), Decimal(0)
    amount = money(row.get("additionalPayment") or "0")
    return max(-amount, Decimal(0)), max(amount, Decimal(0))


def patch_settlement(
    metrics: Mapping[str, Any],
    points: Decimal,
    fee: Decimal,
    positive: Decimal | None = None,
    corrections: Decimal | None = None,
) -> dict[str, Any]:
    """Update only loyalty-dependent amounts; preserve stored COGS and coverage."""
    result = dict(metrics)
    income_delta = Decimal(0)
    correction_delta = Decimal(0)
    if positive is not None and corrections is not None:
        income_delta = positive - money(metrics["positive_adjustments"])
        correction_delta = corrections - money(metrics["corrections"])
        result.update(
            positive_adjustments=text(positive), corrections=text(corrections)
        )
    delta = (
        correction_delta
        + points
        + fee
        - sum(money(metrics.get(k, "0")) for k in LOYALTY_KEYS)
    )
    result.update(
        loyalty_points=text(points),
        loyalty_fee=text(fee),
        payout_formula_version="wb_payout_v1",
    )
    for key in (
        "total_wb_expenses",
        "wb_expenses_without_marketing",
        "profit_period_expenses",
    ):
        result[key] = text(money(metrics[key]) + delta)
    for key in ("before_cogs_profit", "profit_after_cogs"):
        if metrics.get(key) is not None:
            result[key] = text(money(metrics[key]) - delta + income_delta)
    for key, numerator, denominator in (
        (
            "wb_expenses_without_marketing_pct",
            "wb_expenses_without_marketing",
            "net_revenue",
        ),
        ("before_cogs_margin_pct", "before_cogs_profit", "profit_revenue_covered"),
        ("final_margin_pct", "profit_after_cogs", "profit_revenue_covered"),
    ):
        den = money(metrics.get(denominator, metrics["net_revenue"]))
        result[key] = (
            text(money(result[numerator]) / den * 100)
            if den > 0 and result.get(numerator) is not None
            else None
        )
    result["calculated_payout"] = text(
        money(result["net_revenue"])
        - money(result["total_wb_expenses"])
        + money(result["positive_adjustments"])
    )
    return result


def reconcile(
    metrics: Mapping[str, Any],
    summaries: list[dict[str, Any]],
    report_ids: list[str],
    week_start: str,
    week_end: str,
) -> dict[str, Any]:
    """Publish an official amount only for the complete, matching report set."""
    result: dict[str, Any] = {
        "official_bank_payment_sum": None,
        "payout_status": "missing",
        "payout_difference": None,
    }
    if not summaries:
        return result
    try:
        ids = [str(r["reportId"]) for r in summaries]
        if len(set(ids)) != len(ids) or set(ids) != set(report_ids):
            raise ValueError("finance_payout_report_set_mismatch")
        for row in summaries:
            if (
                not (week_start <= row["dateFrom"] <= row["dateTo"] <= week_end)
                or row["currency"] != "RUB"
            ):
                raise ValueError("finance_payout_period_currency_mismatch")
        sums = {
            k: sum((money(r[k]) for r in summaries), Decimal(0))
            for k in SUMMARY_FIELDS
            if k.endswith("Sum")
        }
        mapping = {
            "forPaySum": ("to_seller",),
            "deliveryServiceSum": ("logistics",),
            "paidStorageSum": ("storage",),
            "paidAcceptanceSum": ("acceptance",),
            "penaltySum": ("penalties",),
            "cashbackAmountSum": ("loyalty_points",),
            "cashbackCommissionChangeSum": ("loyalty_fee",),
            "deductionSum": (
                "marketing",
                "transit_logistics",
                "subscriptions",
                "paid_services",
                "review_points",
                "other_deductions",
            ),
        }
        differences = {
            key: text(sum(money(metrics[k]) for k in keys) - sums[key])
            for key, keys in mapping.items()
        }
        net = (
            money(metrics["net_revenue"])
            - money(metrics["total_wb_expenses"])
            + money(metrics["positive_adjustments"])
        )
        delta = net - sums["bankPaymentSum"]
        result["payout_difference"] = text(delta)
        result["payout_component_differences"] = differences
        result["payout_status"] = (
            "ok"
            if abs(delta) <= Decimal("0.01")
            and all(abs(money(d)) <= Decimal("0.01") for d in differences.values())
            else "mismatch"
        )
        if result["payout_status"] == "ok":
            result["official_bank_payment_sum"] = text(sums["bankPaymentSum"])
    except (ValueError, KeyError, TypeError):
        result["payout_status"] = "incomplete"
    return result
