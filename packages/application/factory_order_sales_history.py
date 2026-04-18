"""Server-owned orderCount history seam for factory-order plus one-time DATA_VITRINA reconciliation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock
from packages.business_time import current_business_date_iso
from packages.contracts.sales_funnel_history_block import (
    SalesFunnelHistoryItem,
    SalesFunnelHistoryRequest,
    SalesFunnelHistorySuccess,
)


SALES_HISTORY_SOURCE_KEY = "sales_funnel_history"


@dataclass(frozen=True)
class FactoryOrderSalesHistoryCoverage:
    earliest_available_date: str | None
    latest_available_date: str | None
    exact_date_snapshot_count: int


@dataclass(frozen=True)
class DataVitrinaOrderCountWindow:
    date_from: str
    date_to: str
    sku_count: int
    day_count: int
    item_count: int
    total_row_mismatch_count: int
    exact_date_payloads: dict[str, SalesFunnelHistorySuccess]


class FactoryOrderAuthoritativeSalesHistory:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        sales_funnel_history_block: SalesFunnelHistoryBlock,
        now_factory: callable,
        timestamp_factory: callable,
    ) -> None:
        self.runtime = runtime
        self.sales_funnel_history_block = sales_funnel_history_block
        self.now_factory = now_factory
        self.timestamp_factory = timestamp_factory

    def describe_coverage(self) -> FactoryOrderSalesHistoryCoverage:
        return describe_runtime_sales_history_coverage(self.runtime)

    def build_operator_note(self, base_note: str) -> str:
        coverage = self.describe_coverage()
        if not coverage.earliest_available_date or not coverage.latest_available_date:
            return (
                f"{base_note} История продаж на сервере пока не materialized; "
                "расчёт допускается только в пределах реально покрытого authoritative window."
            )
        return (
            f"{base_note} История продаж на сервере: "
            f"{coverage.earliest_available_date}..{coverage.latest_available_date} "
            f"(exact-date snapshots: {coverage.exact_date_snapshot_count}). "
            "Расчёт допускается на любую глубину внутри этого покрытия."
        )

    def load_order_count_samples(
        self,
        *,
        date_from: str,
        date_to: str,
        nm_ids: list[int],
    ) -> dict[int, list[float]]:
        self._fill_missing_recent_snapshots(date_from=date_from, date_to=date_to, nm_ids=nm_ids)
        payloads = load_runtime_sales_history_payloads(
            runtime=self.runtime,
            date_from=date_from,
            date_to=date_to,
        )
        return _collect_required_order_count_samples(
            payloads=payloads,
            date_from=date_from,
            date_to=date_to,
            nm_ids=nm_ids,
            coverage=self.describe_coverage(),
        )

    def _fill_missing_recent_snapshots(
        self,
        *,
        date_from: str,
        date_to: str,
        nm_ids: list[int],
    ) -> None:
        missing_dates = _find_missing_dates(
            payloads=load_runtime_sales_history_payloads(
                runtime=self.runtime,
                date_from=date_from,
                date_to=date_to,
            ),
            date_from=date_from,
            date_to=date_to,
            nm_ids=nm_ids,
        )
        if not missing_dates:
            return

        current_business_date = date.fromisoformat(current_business_date_iso(self.now_factory()))
        min_fetchable_date = current_business_date - timedelta(days=7)
        fetchable_dates = [
            item
            for item in missing_dates
            if date.fromisoformat(item) >= min_fetchable_date
        ]
        if not fetchable_dates:
            return

        captured_at = self.timestamp_factory()
        for batch_from, batch_to in _group_contiguous_dates(fetchable_dates):
            result = self.sales_funnel_history_block.execute(
                SalesFunnelHistoryRequest(
                    snapshot_type="sales_funnel_history",
                    date_from=batch_from,
                    date_to=batch_to,
                    nm_ids=nm_ids,
                )
            ).result
            persist_sales_history_result_exact_dates(
                runtime=self.runtime,
                payload=result,
                captured_at=captured_at,
            )


def describe_runtime_sales_history_coverage(
    runtime: RegistryUploadDbBackedRuntime,
) -> FactoryOrderSalesHistoryCoverage:
    dates = runtime.list_temporal_source_snapshot_dates(source_key=SALES_HISTORY_SOURCE_KEY)
    if not dates:
        return FactoryOrderSalesHistoryCoverage(
            earliest_available_date=None,
            latest_available_date=None,
            exact_date_snapshot_count=0,
        )
    return FactoryOrderSalesHistoryCoverage(
        earliest_available_date=dates[0],
        latest_available_date=dates[-1],
        exact_date_snapshot_count=len(dates),
    )


def persist_sales_history_result_exact_dates(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    payload: Any,
    captured_at: str,
) -> int:
    saved = 0
    for snapshot_date, exact_payload in split_sales_history_payload_by_date(payload).items():
        runtime.save_temporal_source_snapshot(
            source_key=SALES_HISTORY_SOURCE_KEY,
            snapshot_date=snapshot_date,
            captured_at=captured_at,
            payload=exact_payload,
        )
        saved += 1
    return saved


def replace_runtime_sales_history_window(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    date_from: str,
    date_to: str,
    exact_date_payloads: Mapping[str, SalesFunnelHistorySuccess],
    captured_at: str,
) -> dict[str, int]:
    deleted = runtime.delete_temporal_source_snapshots(
        source_key=SALES_HISTORY_SOURCE_KEY,
        date_from=date_from,
        date_to=date_to,
    )
    saved = 0
    for snapshot_date in sorted(exact_date_payloads):
        runtime.save_temporal_source_snapshot(
            source_key=SALES_HISTORY_SOURCE_KEY,
            snapshot_date=snapshot_date,
            captured_at=captured_at,
            payload=exact_date_payloads[snapshot_date],
        )
        saved += 1
    return {"deleted_snapshot_count": deleted, "saved_snapshot_count": saved}


def load_runtime_sales_history_payloads(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    payloads: dict[str, Any] = {}
    for snapshot_date in _iter_iso_dates(date_from, date_to):
        payload, _ = runtime.load_temporal_source_snapshot(
            source_key=SALES_HISTORY_SOURCE_KEY,
            snapshot_date=snapshot_date,
        )
        if payload is not None:
            payloads[snapshot_date] = payload
    return payloads


def split_sales_history_payload_by_date(payload: Any) -> dict[str, SalesFunnelHistorySuccess]:
    if str(getattr(payload, "kind", "")) != "success":
        return {}
    items = list(getattr(payload, "items", []) or [])
    by_date: dict[str, list[SalesFunnelHistoryItem]] = {}
    for item in items:
        snapshot_date = str(getattr(item, "date", "") or "")
        nm_id = getattr(item, "nm_id", None)
        metric = str(getattr(item, "metric", "") or "")
        value = getattr(item, "value", None)
        if not snapshot_date or not isinstance(nm_id, int) or metric == "" or not isinstance(value, (int, float)):
            continue
        by_date.setdefault(snapshot_date, []).append(
            SalesFunnelHistoryItem(
                date=snapshot_date,
                nm_id=nm_id,
                metric=metric,
                value=float(value),
            )
        )
    return {
        snapshot_date: SalesFunnelHistorySuccess(
            kind="success",
            date_from=snapshot_date,
            date_to=snapshot_date,
            count=len(items_for_date),
            items=sorted(items_for_date, key=lambda item: (item.nm_id, item.metric)),
        )
        for snapshot_date, items_for_date in sorted(by_date.items())
    }


def extract_data_vitrina_order_count_window(
    values: list[list[Any]],
    *,
    date_from: str,
    date_to: str,
) -> DataVitrinaOrderCountWindow:
    if not values:
        raise ValueError("DATA_VITRINA sheet is empty")
    header = values[0]
    if len(header) < 3 or str(header[0]).strip() != "дата" or str(header[1]).strip() != "key":
        raise ValueError("DATA_VITRINA header must start with ['дата', 'key']")

    date_columns: list[tuple[int, str]] = []
    for index, raw_value in enumerate(header[2:], start=2):
        parsed = _coerce_sheet_header_date(raw_value)
        if parsed is None:
            continue
        if date_from <= parsed <= date_to:
            date_columns.append((index, parsed))
    if not date_columns:
        raise ValueError(
            f"DATA_VITRINA does not expose requested date window {date_from}..{date_to}"
        )

    total_order_count_by_date: dict[str, float] = {}
    items_by_date: dict[str, list[SalesFunnelHistoryItem]] = {
        snapshot_date: []
        for _, snapshot_date in date_columns
    }
    seen_nm_ids: set[int] = set()
    current_nm_id: int | None = None

    for row in values[1:]:
        key = str(row[1]).strip() if len(row) > 1 else ""
        if key == "total_orderCount":
            total_order_count_by_date = _collect_row_values_by_date(row, date_columns)
            continue
        if key.startswith("SKU:"):
            current_nm_id = _parse_sheet_nm_id(key)
            seen_nm_ids.add(current_nm_id)
            continue
        if key != "orderCount" or current_nm_id is None:
            continue
        row_values = _collect_row_values_by_date(row, date_columns)
        for snapshot_date, value in row_values.items():
            items_by_date[snapshot_date].append(
                SalesFunnelHistoryItem(
                    date=snapshot_date,
                    nm_id=current_nm_id,
                    metric="orderCount",
                    value=value,
                )
            )

    if not seen_nm_ids:
        raise ValueError("DATA_VITRINA does not contain SKU:* sections for orderCount extraction")

    exact_date_payloads: dict[str, SalesFunnelHistorySuccess] = {}
    total_row_mismatch_count = 0
    for snapshot_date in sorted(items_by_date):
        items = sorted(items_by_date[snapshot_date], key=lambda item: item.nm_id)
        if len(items) != len(seen_nm_ids):
            raise ValueError(
                f"DATA_VITRINA orderCount rows for {snapshot_date} do not cover all SKU blocks: "
                f"expected {len(seen_nm_ids)}, got {len(items)}"
            )
        total_value = round(sum(item.value for item in items), 6)
        expected_total = total_order_count_by_date.get(snapshot_date)
        if expected_total is not None and round(expected_total, 6) != total_value:
            total_row_mismatch_count += 1
        exact_date_payloads[snapshot_date] = SalesFunnelHistorySuccess(
            kind="success",
            date_from=snapshot_date,
            date_to=snapshot_date,
            count=len(items),
            items=items,
        )

    return DataVitrinaOrderCountWindow(
        date_from=date_columns[0][1],
        date_to=date_columns[-1][1],
        sku_count=len(seen_nm_ids),
        day_count=len(date_columns),
        item_count=sum(len(payload.items) for payload in exact_date_payloads.values()),
        total_row_mismatch_count=total_row_mismatch_count,
        exact_date_payloads=exact_date_payloads,
    )


def _collect_required_order_count_samples(
    *,
    payloads: Mapping[str, Any],
    date_from: str,
    date_to: str,
    nm_ids: list[int],
    coverage: FactoryOrderSalesHistoryCoverage,
) -> dict[int, list[float]]:
    by_nm_id: dict[int, list[float]] = {nm_id: [] for nm_id in nm_ids}
    missing_dates: list[str] = []
    missing_pairs = 0
    for snapshot_date in _iter_iso_dates(date_from, date_to):
        payload = payloads.get(snapshot_date)
        if payload is None or str(getattr(payload, "kind", "")) != "success":
            missing_dates.append(snapshot_date)
            missing_pairs += len(nm_ids)
            continue
        order_counts = _collect_order_count_map(payload)
        missing_nm_ids = sorted(set(nm_ids) - set(order_counts))
        if missing_nm_ids:
            missing_dates.append(snapshot_date)
            missing_pairs += len(missing_nm_ids)
            continue
        for nm_id in nm_ids:
            by_nm_id[nm_id].append(order_counts[nm_id])
    if missing_dates:
        earliest = coverage.earliest_available_date or "coverage is empty"
        latest = coverage.latest_available_date or "coverage is empty"
        raise ValueError(
            "Запрошенный период усреднения продаж не покрывается текущим authoritative sales history source: "
            f"нужен диапазон {date_from}..{date_to}, "
            f"а на сервере сейчас доступно {earliest}..{latest}; "
            f"missing_dates={','.join(missing_dates)}; missing_pairs={missing_pairs}."
        )
    return by_nm_id


def _find_missing_dates(
    *,
    payloads: Mapping[str, Any],
    date_from: str,
    date_to: str,
    nm_ids: list[int],
) -> list[str]:
    missing: list[str] = []
    for snapshot_date in _iter_iso_dates(date_from, date_to):
        payload = payloads.get(snapshot_date)
        if payload is None or str(getattr(payload, "kind", "")) != "success":
            missing.append(snapshot_date)
            continue
        order_counts = _collect_order_count_map(payload)
        if set(order_counts) < set(nm_ids):
            missing.append(snapshot_date)
    return missing


def _collect_order_count_map(payload: Any) -> dict[int, float]:
    out: dict[int, float] = {}
    for item in list(getattr(payload, "items", []) or []):
        metric = str(getattr(item, "metric", "") or "")
        nm_id = getattr(item, "nm_id", None)
        value = getattr(item, "value", None)
        if metric != "orderCount" or not isinstance(nm_id, int) or not isinstance(value, (int, float)):
            continue
        out[nm_id] = float(value)
    return out


def _collect_row_values_by_date(
    row: list[Any],
    date_columns: list[tuple[int, str]],
) -> dict[str, float]:
    out: dict[str, float] = {}
    for index, snapshot_date in date_columns:
        if index >= len(row):
            raise ValueError(f"DATA_VITRINA row is shorter than requested date column {snapshot_date}")
        raw_value = row[index]
        if raw_value in ("", None):
            raise ValueError(f"DATA_VITRINA has empty orderCount cell for {snapshot_date}")
        out[snapshot_date] = float(raw_value)
    return out


def _parse_sheet_nm_id(value: str) -> int:
    prefix, _, raw_nm_id = value.partition(":")
    if prefix != "SKU" or not raw_nm_id.strip():
        raise ValueError(f"Unexpected DATA_VITRINA SKU key: {value!r}")
    return int(raw_nm_id.strip())


def _coerce_sheet_header_date(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        serial_days = int(value)
        return (_excel_epoch() + timedelta(days=serial_days)).isoformat()
    text = str(value or "").strip()
    if not text:
        return None
    for parser in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, parser).date().isoformat()
        except ValueError:
            continue
    return None


def _group_contiguous_dates(snapshot_dates: list[str]) -> list[tuple[str, str]]:
    if not snapshot_dates:
        return []
    ordered = sorted(snapshot_dates)
    groups: list[tuple[str, str]] = []
    start = ordered[0]
    previous = ordered[0]
    for current in ordered[1:]:
        if date.fromisoformat(current) == date.fromisoformat(previous) + timedelta(days=1):
            previous = current
            continue
        groups.append((start, previous))
        start = current
        previous = current
    groups.append((start, previous))
    return groups


def _iter_iso_dates(date_from: str, date_to: str) -> list[str]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("date_to must be >= date_from")
    out: list[str] = []
    current = start
    while current <= end:
        out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def _excel_epoch() -> date:
    return date(1899, 12, 30)
