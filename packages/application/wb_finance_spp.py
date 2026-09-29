"""Bounded, source-bound SPP disclosure for official WB Finance sale rows."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
import re


SPP_FORMULA_VERSION = "wb_finance_spp_ru_gross_qty_v1"
OFFICE_CHANNEL = {
    "Склад WB": "FBO",
    "Склад поставщика - везу на склад WB": "FBS",
}


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def spp_channel(row: Mapping[str, Any]) -> str | None:
    """Use explicit Finance delivery mode or the two evidenced exact aliases."""

    raw_method = str(row.get("deliveryMethod") or "").strip().upper()
    modes = set(re.findall(r"(?<![A-Z])(?:FBO|FBW|FBS|DBS)(?![A-Z])", raw_method))
    if "DBS" in modes or len(modes) > 1:
        return None
    explicit = ("FBO" if "FBW" in modes else next(iter(modes))) if modes else None
    office = OFFICE_CHANNEL.get(str(row.get("officeName") or "").strip())
    if explicit and office and explicit != office:
        return None
    return explicit or office


def project_spp(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    numerator = {"FBO": Decimal(0), "FBS": Decimal(0)}
    denominator = {"FBO": 0, "FBS": 0}
    counts = {
        "candidate_rows": 0, "candidate_quantity": 0,
        "classified_rows": 0, "classified_quantity": 0,
        "unknown_channel_rows": 0, "unknown_channel_quantity": 0,
        "missing_spp_rows": 0, "missing_spp_quantity": 0,
        "valid_rows": 0, "valid_quantity": 0,
        "non_russian_sale_rows": 0,
    }
    for row in rows:
        if (str(row.get("docTypeName") or "").strip().casefold() != "продажа"
            or str(row.get("sellerOperName") or "").strip().casefold() != "продажа"):
            continue
        quantity_value = _decimal(row.get("quantity"))
        if (quantity_value is None or quantity_value <= 0
            or quantity_value != quantity_value.to_integral_value()):
            continue
        quantity = int(quantity_value)
        if str(row.get("country") or "").strip() != "Россия":
            counts["non_russian_sale_rows"] += 1
            continue
        counts["candidate_rows"] += 1
        counts["candidate_quantity"] += quantity
        channel = spp_channel(row)
        if channel is None:
            counts["unknown_channel_rows"] += 1
            counts["unknown_channel_quantity"] += quantity
            continue
        counts["classified_rows"] += 1
        counts["classified_quantity"] += quantity
        spp = _decimal(row.get("spp"))
        if spp is None or spp < 0 or spp > 100:
            counts["missing_spp_rows"] += 1
            counts["missing_spp_quantity"] += quantity
            continue
        counts["valid_rows"] += 1
        counts["valid_quantity"] += quantity
        numerator[channel] += spp * quantity
        denominator[channel] += quantity
    return {
        "formula_version": SPP_FORMULA_VERSION,
        "spp_fbo_pct": (
            format(numerator["FBO"] / denominator["FBO"], ".4f")
            if denominator["FBO"] else None
        ),
        "spp_fbs_pct": (
            format(numerator["FBS"] / denominator["FBS"], ".4f")
            if denominator["FBS"] else None
        ),
        "valid_fbo_quantity": denominator["FBO"],
        "valid_fbs_quantity": denominator["FBS"],
        "coverage": counts,
    }
