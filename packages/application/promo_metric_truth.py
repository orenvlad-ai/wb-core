"""Canonical promo archive row parsing and candidate/eligible-set helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


HEADER_NM_ID = "Артикул WB"
HEADER_PLAN_PRICE = "Плановая цена для акции"


@dataclass(frozen=True)
class PromoWorkbookPlanRow:
    nm_id: int
    plan_price: float
    row_index: int


@dataclass(frozen=True)
class PromoCandidateRow:
    nm_id: int
    campaign_identity: str
    plan_price: float


@dataclass(frozen=True)
class PromoEligibilityEvaluation:
    price_seller_discounted: float | None
    candidate_campaign_identities: tuple[str, ...]
    candidate_plan_prices: tuple[float, ...]
    eligible_campaign_identities: tuple[str, ...]
    eligible_plan_prices: tuple[float, ...]
    promo_count_by_price: float
    promo_entry_price_best: float
    promo_participation: float


def find_workbook_data_sheet(workbook: Any) -> tuple[Any, int]:
    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        row_index = find_workbook_header_row_index(sheet)
        if row_index is not None:
            return sheet, row_index
    first_sheet = workbook[workbook.sheetnames[0]]
    row_index = find_workbook_header_row_index(first_sheet)
    return first_sheet, row_index or 1


def find_workbook_header_row_index(sheet: Any) -> int | None:
    for row_index, row in enumerate(
        sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 10), values_only=True),
        start=1,
    ):
        header = [value for value in row if value not in (None, "")]
        normalized = {str(value).strip() for value in header}
        if {HEADER_NM_ID, HEADER_PLAN_PRICE}.issubset(normalized):
            return row_index
    return None


def header_index_from_row(header: list[object]) -> dict[str, int]:
    return {
        str(name).strip(): idx
        for idx, name in enumerate(header)
        if name not in (None, "")
    }


def iter_workbook_plan_rows(
    *,
    sheet: Any,
    header_row_index: int,
    requested_nm_ids: set[int],
) -> list[PromoWorkbookPlanRow]:
    header = list(
        next(
            sheet.iter_rows(
                min_row=header_row_index,
                max_row=header_row_index,
                values_only=True,
            )
        )
    )
    header_index = header_index_from_row(header)
    for required in (HEADER_NM_ID, HEADER_PLAN_PRICE):
        if required not in header_index:
            raise ValueError(f"promo workbook missing required header: {required}")

    plan_rows: list[PromoWorkbookPlanRow] = []
    for row_index, row in enumerate(
        sheet.iter_rows(min_row=header_row_index + 1, values_only=True),
        start=header_row_index + 1,
    ):
        nm_id = parse_int(row[header_index[HEADER_NM_ID]])
        if nm_id is None or nm_id not in requested_nm_ids:
            continue
        plan_price = parse_float(row[header_index[HEADER_PLAN_PRICE]])
        if plan_price is None:
            continue
        plan_rows.append(
            PromoWorkbookPlanRow(
                nm_id=nm_id,
                plan_price=plan_price,
                row_index=row_index,
            )
        )
    return plan_rows


def evaluate_candidate_rows(
    *,
    candidate_rows: list[PromoCandidateRow],
    price_seller_discounted: float | None,
) -> PromoEligibilityEvaluation:
    ordered_rows = list(candidate_rows)
    eligible_rows = (
        []
        if price_seller_discounted is None
        else [
            row
            for row in ordered_rows
            if float(price_seller_discounted) < float(row.plan_price)
        ]
    )
    candidate_plan_prices = tuple(float(row.plan_price) for row in ordered_rows)
    eligible_plan_prices = tuple(float(row.plan_price) for row in eligible_rows)
    promo_count = float(len(eligible_rows))
    return PromoEligibilityEvaluation(
        price_seller_discounted=(
            None if price_seller_discounted is None else float(price_seller_discounted)
        ),
        candidate_campaign_identities=tuple(row.campaign_identity for row in ordered_rows),
        candidate_plan_prices=candidate_plan_prices,
        eligible_campaign_identities=tuple(row.campaign_identity for row in eligible_rows),
        eligible_plan_prices=eligible_plan_prices,
        promo_count_by_price=promo_count,
        promo_entry_price_best=max(candidate_plan_prices) if candidate_plan_prices else 0.0,
        promo_participation=1.0 if eligible_rows else 0.0,
    )


def parse_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\xa0", "").replace("%", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: object) -> int | None:
    numeric = parse_float(value)
    if numeric is None:
        return None
    return int(numeric)
