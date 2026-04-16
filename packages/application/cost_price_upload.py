"""Application-слой отдельного COST_PRICE upload contour."""

from __future__ import annotations

from datetime import date, datetime
import math
import re
from typing import Any, Iterable, Mapping

from packages.contracts.cost_price_upload import CostPriceRow, CostPriceUploadPayload

_DDMMYYYY_RE = re.compile(r"^(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>\d{4})$")


class CostPriceUploadBlock:
    """Минимальный parser/validator для authoritative COST_PRICE dataset."""

    def validate_dataset(self, dataset: CostPriceUploadPayload) -> None:
        _validate_uploaded_at(dataset.uploaded_at)
        _require_unique(
            "cost_price_rows.(group,effective_from)",
            ((row.group, row.effective_from) for row in dataset.cost_price_rows),
        )


def parse_cost_price_upload_payload(payload: Any) -> CostPriceUploadPayload:
    mapping = _require_mapping_value(payload, "cost price upload payload")
    return CostPriceUploadPayload(
        dataset_version=_require_str(mapping, "dataset_version"),
        uploaded_at=_require_str(mapping, "uploaded_at"),
        cost_price_rows=[
            _parse_cost_price_row(item)
            for item in _require_list(mapping, "cost_price_rows")
        ],
    )


def canonicalize_effective_from(raw_value: Any) -> str:
    if isinstance(raw_value, str):
        value = raw_value.strip()
        if not value:
            raise ValueError("effective_from must be a non-empty string")
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass

        match = _DDMMYYYY_RE.fullmatch(value)
        if match is not None:
            return date(
                year=int(match.group("year")),
                month=int(match.group("month")),
                day=int(match.group("day")),
            ).isoformat()

        try:
            return _parse_datetime_to_date(value).isoformat()
        except ValueError as exc:
            raise ValueError(
                "effective_from must be a valid date in YYYY-MM-DD, DD.MM.YYYY or ISO datetime form"
            ) from exc

    raise ValueError("effective_from must be a string date value")


def _parse_cost_price_row(raw: Any) -> CostPriceRow:
    row = _require_mapping_value(raw, "cost price row")
    return CostPriceRow(
        group=_require_str(row, "group").strip(),
        cost_price_rub=_require_float_like(row, "cost_price_rub"),
        effective_from=canonicalize_effective_from(row.get("effective_from")),
    )


def _parse_datetime_to_date(value: str) -> date:
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    return datetime.fromisoformat(normalized).date()


def _validate_uploaded_at(value: str) -> None:
    if not value.endswith("Z"):
        raise ValueError("uploaded_at must be an ISO 8601 UTC timestamp ending with Z")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("uploaded_at must be a valid ISO 8601 timestamp") from exc


def _require_mapping_value(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _require_list(document: Mapping[str, Any], key: str) -> list[Any]:
    value = document.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _require_str(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _require_float_like(document: Mapping[str, Any], key: str) -> float:
    value = document.get(key)
    if value == "" or value is None or isinstance(value, bool):
        raise ValueError(f"{key} must be a numeric value")
    if isinstance(value, (int, float)):
        number_value = float(value)
    elif isinstance(value, str):
        normalized = value.strip().replace(" ", "").replace(",", ".")
        try:
            number_value = float(normalized)
        except ValueError as exc:
            raise ValueError(f"{key} must be a numeric value") from exc
    else:
        raise ValueError(f"{key} must be a numeric value")
    if not math.isfinite(number_value):
        raise ValueError(f"{key} must be a finite numeric value")
    return number_value


def _require_unique(label: str, values: Iterable[Any]) -> None:
    seen: set[Any] = set()
    duplicates: set[Any] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        rendered = [f"{group}|{effective_from}" for group, effective_from in sorted(duplicates)]
        raise ValueError(f"{label} contains duplicates: {rendered}")
