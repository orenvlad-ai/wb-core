"""Canonical promo workbook row parsing and eligibility helpers."""

from __future__ import annotations

from typing import Any


HEADER_NM_ID = "Артикул WB"
HEADER_PLAN_PRICE = "Плановая цена для акции"
HEADER_CURRENT_PRICE = "Текущая розничная цена"
HEADER_CURRENT_DISCOUNT = "Текущая скидка на сайте, %"
HEADER_UPLOADABLE_DISCOUNT = "Загружаемая скидка для участия в акции"


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
        if {HEADER_NM_ID, HEADER_PLAN_PRICE, HEADER_CURRENT_PRICE}.issubset(normalized):
            return row_index
    return None


def header_index_from_row(header: list[object]) -> dict[str, int]:
    return {
        str(name).strip(): idx
        for idx, name in enumerate(header)
        if name not in (None, "")
    }


def iter_eligible_workbook_rows(
    *,
    sheet: Any,
    header_row_index: int,
    requested_nm_ids: set[int],
) -> list[tuple[int, float]]:
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
    for required in (HEADER_NM_ID, HEADER_PLAN_PRICE, HEADER_CURRENT_PRICE):
        if required not in header_index:
            raise ValueError(f"promo workbook missing required header: {required}")

    eligible: list[tuple[int, float]] = []
    for row in sheet.iter_rows(min_row=header_row_index + 1, values_only=True):
        nm_id = parse_int(row[header_index[HEADER_NM_ID]])
        if nm_id is None or nm_id not in requested_nm_ids:
            continue
        plan_price = parse_float(row[header_index[HEADER_PLAN_PRICE]])
        if plan_price is None:
            continue
        seller_discounted_price = resolve_row_seller_discounted_price(
            row=row,
            header_index=header_index,
        )
        if seller_discounted_price is None:
            continue
        if seller_discounted_price < plan_price:
            eligible.append((nm_id, plan_price))
    return eligible


def resolve_row_seller_discounted_price(
    *,
    row: tuple[object, ...],
    header_index: dict[str, int],
) -> float | None:
    current_price = _optional_numeric(row, header_index, HEADER_CURRENT_PRICE)
    if current_price is None:
        return None
    uploadable_discount = _optional_numeric(row, header_index, HEADER_UPLOADABLE_DISCOUNT)
    current_discount = _optional_numeric(row, header_index, HEADER_CURRENT_DISCOUNT)
    effective_discount = uploadable_discount if uploadable_discount is not None else current_discount
    if effective_discount is None:
        return current_price
    return current_price * (100.0 - effective_discount) / 100.0


def _optional_numeric(
    row: tuple[object, ...],
    header_index: dict[str, int],
    header_name: str,
) -> float | None:
    index = header_index.get(header_name)
    if index is None:
        return None
    return parse_float(row[index])


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
