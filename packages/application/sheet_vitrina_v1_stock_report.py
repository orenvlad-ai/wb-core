"""Read-only closed-day stock report for the sheet_vitrina_v1 operator page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    current_business_date_iso,
    default_business_as_of_date,
)
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope

TEMPORAL_SLOT_YESTERDAY_CLOSED = "yesterday_closed"
STOCK_ALERT_THRESHOLD = 50.0
EPS = 1e-9
STOCK_REPORT_DISTRICTS = (
    ("stock_ru_central", "Центральный ФО"),
    ("stock_ru_northwest", "Северо-Западный ФО"),
    ("stock_ru_volga", "Приволжский ФО"),
    ("stock_ru_ural", "Уральский ФО"),
    ("stock_ru_south_caucasus", "Юг и СКФО"),
)
REPORT_NOTES = (
    "По умолчанию отчёт использует previous closed business day через persisted ready snapshot и slot yesterday_closed.",
    "При explicit as_of_date route остаётся server-owned и читает именно requested closed business day, без upstream fetch.",
    "В список попадают только SKU, у которых хотя бы по одному supported district stock меньше 50 единиц.",
    "Merged bucket `ДВ и Сибирь` целиком исключён из текущего report contour: current truth не делит его на отдельный Дальний Восток и Сибирь.",
    "Короткий label `Юг и СКФО` остаётся truthful к current merged bucket и не разрезается искусственно.",
)


@dataclass(frozen=True)
class SnapshotSlotView:
    as_of_date: str
    slot_date: str
    sku_values: dict[int, dict[str, float | None]]


class SheetVitrinaV1StockReportBlock:
    """Build a compact operator-facing closed-day stock report from the ready snapshot."""

    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime = runtime
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def build(self, *, as_of_date: str | None = None) -> dict[str, Any]:
        business_date = date.fromisoformat(current_business_date_iso(self.now_factory()))
        current_business_date = business_date.isoformat()
        requested_as_of_date = as_of_date or default_business_as_of_date(self.now_factory())
        date.fromisoformat(requested_as_of_date)
        base_payload = {
            "status": "unavailable",
            "reason": "",
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "current_business_date": current_business_date,
            "report_date": requested_as_of_date,
            "threshold_lt": int(STOCK_ALERT_THRESHOLD),
            "notes": list(REPORT_NOTES),
            "districts": [
                {
                    "metric_key": metric_key,
                    "label": label,
                }
                for metric_key, label in STOCK_REPORT_DISTRICTS
            ],
            "source_of_truth": {
                "read_model": "persisted_ready_snapshot",
                "sheet_name": "DATA_VITRINA",
                "snapshot_as_of_date": requested_as_of_date,
                "temporal_slot": TEMPORAL_SLOT_YESTERDAY_CLOSED,
                "slot_date": requested_as_of_date,
            },
        }

        try:
            current_state = self.runtime.load_current_state()
        except ValueError as exc:
            return {
                **base_payload,
                "reason": f"Отчёт по остаткам пока недоступен: {exc}",
            }

        try:
            snapshot = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=requested_as_of_date)
        except ValueError as exc:
            return {
                **base_payload,
                "reason": f"Отчёт по остаткам пока недоступен: отсутствует ready snapshot для {requested_as_of_date} ({exc})",
            }

        try:
            closed_view = _extract_closed_slot_view(snapshot, expected_closed_date=requested_as_of_date)
        except ValueError as exc:
            return {
                **base_payload,
                "reason": f"Отчёт по остаткам пока недоступен: {exc}",
            }

        rows: list[dict[str, Any]] = []
        for config_item in sorted(current_state.config_v2, key=lambda item: item.display_order):
            if not config_item.enabled:
                continue
            sku_values = closed_view.sku_values.get(config_item.nm_id, {})
            breached_districts = []
            for metric_key, label in STOCK_REPORT_DISTRICTS:
                stock_value = sku_values.get(metric_key)
                if stock_value is None or stock_value >= STOCK_ALERT_THRESHOLD - EPS:
                    continue
                breached_districts.append(
                    {
                        "metric_key": metric_key,
                        "label": label,
                        "stock": float(stock_value),
                    }
                )
            if not breached_districts:
                continue
            stock_total = sku_values.get("stock_total")
            rows.append(
                {
                    "nm_id": config_item.nm_id,
                    "display_name": config_item.display_name,
                    "identity_label": f"{config_item.display_name} · nmId {config_item.nm_id}",
                    "stock_total": None if stock_total is None else float(stock_total),
                    "breached_districts": breached_districts,
                    "breached_district_count": len(breached_districts),
                    "min_breached_stock": min(item["stock"] for item in breached_districts),
                }
            )

        rows.sort(
            key=lambda item: (
                float(item["min_breached_stock"]),
                -int(item["breached_district_count"]),
                _sort_stock_total(item.get("stock_total")),
                str(item["identity_label"]),
            )
        )

        return {
            **base_payload,
            "status": "available",
            "report_date": closed_view.slot_date,
            "row_count": len(rows),
            "rows": rows,
            "source_of_truth": {
                **base_payload["source_of_truth"],
                "slot_date": closed_view.slot_date,
            },
        }


def _extract_closed_slot_view(
    plan: SheetVitrinaV1Envelope,
    *,
    expected_closed_date: str,
) -> SnapshotSlotView:
    slot_index = None
    slot_date = ""
    for index, slot in enumerate(plan.temporal_slots):
        if slot.slot_key == TEMPORAL_SLOT_YESTERDAY_CLOSED:
            slot_index = index
            slot_date = slot.column_date
            break
    if slot_index is None:
        raise ValueError(f"ready snapshot {plan.as_of_date} does not contain yesterday_closed slot")
    if slot_date != expected_closed_date:
        raise ValueError(
            f"ready snapshot {plan.as_of_date} points yesterday_closed to {slot_date}, expected {expected_closed_date}"
        )

    data_sheet = next((item for item in plan.sheets if item.sheet_name == "DATA_VITRINA"), None)
    if data_sheet is None:
        raise ValueError(f"ready snapshot {plan.as_of_date} does not contain DATA_VITRINA")

    value_index = 2 + slot_index
    sku_values: dict[int, dict[str, float | None]] = {}
    for row in data_sheet.rows:
        if len(row) <= value_index:
            continue
        key = str(row[1] or "")
        value = _coerce_numeric(row[value_index])
        if not key.startswith("SKU:") or "|" not in key:
            continue
        scope_token, metric_key = key.split("|", 1)
        try:
            nm_id = int(scope_token.split(":", 1)[1])
        except (IndexError, ValueError):
            continue
        sku_values.setdefault(nm_id, {})[metric_key] = value

    return SnapshotSlotView(
        as_of_date=plan.as_of_date,
        slot_date=slot_date,
        sku_values=sku_values,
    )


def _coerce_numeric(value: Any) -> float | None:
    if value in ("", None):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _sort_stock_total(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return float("inf")
