"""Application-слой блока fin report daily."""

from collections import defaultdict
from typing import Any, Mapping

from packages.adapters.fin_report_daily_block import FinReportDailySource
from packages.contracts.fin_report_daily_block import (
    FinReportDailyEnvelope,
    FinReportDailyItem,
    FinReportDailyRequest,
    FinReportDailyStorageTotal,
    FinReportDailySuccess,
)


FIN_FIELDS = (
    "fin_delivery_rub",
    "fin_storage_fee",
    "fin_deduction",
    "fin_commission",
    "fin_penalty",
    "fin_additional_payment",
    "fin_buyout_rub",
    "fin_commission_wb_portal",
    "fin_acquiring_fee",
    "fin_loyalty_rub",
)


def transform_legacy_payload(payload: Mapping[str, Any]) -> FinReportDailyEnvelope:
    snapshot_date = _require_str(payload, "snapshot_date")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("legacy payload must contain data object")

    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("legacy payload must contain data.rows list")

    grouped: dict[int, dict[str, float]] = defaultdict(lambda: {field: 0.0 for field in FIN_FIELDS})
    storage_total = 0.0

    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("legacy row must be object")
        if _require_str(row, "snapshot_date") != snapshot_date:
            continue

        nm_id = _require_int(row, "nmId")
        if nm_id == 0:
            storage_total += _require_float(row, "fin_storage_fee")
            continue

        acc = grouped[nm_id]
        for field in FIN_FIELDS:
            acc[field] += _require_float(row, field)

    items = [
        FinReportDailyItem(
            nm_id=nm_id,
            fin_delivery_rub=grouped[nm_id]["fin_delivery_rub"],
            fin_storage_fee=grouped[nm_id]["fin_storage_fee"],
            fin_deduction=grouped[nm_id]["fin_deduction"],
            fin_commission=grouped[nm_id]["fin_commission"],
            fin_penalty=grouped[nm_id]["fin_penalty"],
            fin_additional_payment=grouped[nm_id]["fin_additional_payment"],
            fin_buyout_rub=grouped[nm_id]["fin_buyout_rub"],
            fin_commission_wb_portal=grouped[nm_id]["fin_commission_wb_portal"],
            fin_acquiring_fee=grouped[nm_id]["fin_acquiring_fee"],
            fin_loyalty_rub=grouped[nm_id]["fin_loyalty_rub"],
        )
        for nm_id in sorted(grouped)
    ]

    return FinReportDailyEnvelope(
        result=FinReportDailySuccess(
            kind="success",
            snapshot_date=snapshot_date,
            count=len(items),
            items=items,
            storage_total=FinReportDailyStorageTotal(
                nm_id=0,
                fin_storage_fee_total=storage_total,
            ),
        )
    )


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be string")
    return value


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise ValueError(f"{key} must be int")
    return value


def _require_float(payload: Mapping[str, Any], key: str) -> float:
    value = payload.get(key)
    if not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    return float(value)


class FinReportDailyBlock:
    def __init__(self, source: FinReportDailySource) -> None:
        self._source = source

    def execute(self, request: FinReportDailyRequest) -> FinReportDailyEnvelope:
        payload = self._source.fetch(request)
        return transform_legacy_payload(payload)
