"""Smoke-check web-vitrina group metric coverage and other_sources formulas."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    WEB_VITRINA_SOURCE_GROUPS,
    WEB_VITRINA_SOURCE_METRIC_KEYS,
    RegistryUploadHttpEntrypoint,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
STATUS_HEADER = [
    "source_key",
    "kind",
    "freshness",
    "snapshot_date",
    "date",
    "date_from",
    "date_to",
    "requested_count",
    "covered_count",
    "missing_nm_ids",
    "note",
]
OLD_REFRESHED_AT = "2026-04-20T10:00:00Z"
NEW_REFRESHED_AT = "2026-04-20T11:00:00Z"
DERIVED_OTHER_SOURCE_METRICS = {
    "proxy_margin_pct_total",
    "total_proxy_profit_rub",
    "proxy_margin_pct",
    "proxy_profit_rub",
}


def main() -> None:
    _assert_group_metric_coverage()
    _assert_other_sources_recomputes_derived_metrics()


def _assert_group_metric_coverage() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    visible_metric_keys = [
        str(item["metric_key"])
        for item in sorted(bundle["metrics_v2"], key=lambda row: int(row.get("display_order") or 0))
        if item.get("enabled") and item.get("show_in_data")
    ]
    metric_to_groups: dict[str, list[str]] = {metric_key: [] for metric_key in visible_metric_keys}
    for group_id, group in WEB_VITRINA_SOURCE_GROUPS.items():
        group_metric_keys: set[str] = set()
        for source_key in group["source_keys"]:
            group_metric_keys.update(WEB_VITRINA_SOURCE_METRIC_KEYS.get(source_key, ()))
        for metric_key in visible_metric_keys:
            if metric_key in group_metric_keys:
                metric_to_groups[metric_key].append(group_id)

    missing = [metric_key for metric_key, group_ids in metric_to_groups.items() if not group_ids]
    duplicate = {metric_key: group_ids for metric_key, group_ids in metric_to_groups.items() if len(group_ids) > 1}
    other_source_metrics = {
        metric_key
        for source_key in WEB_VITRINA_SOURCE_GROUPS["other_sources"]["source_keys"]
        for metric_key in WEB_VITRINA_SOURCE_METRIC_KEYS.get(source_key, ())
    }
    missing_derived = sorted(DERIVED_OTHER_SOURCE_METRICS - other_source_metrics)
    if missing or duplicate or missing_derived:
        raise AssertionError(
            "web-vitrina loading group metric coverage failed: "
            f"missing={missing}, duplicate={duplicate}, missing_derived={missing_derived}"
        )
    print(
        "web_vitrina_group_metric_coverage: ok ->",
        len(visible_metric_keys),
        {group_id: sum(1 for groups in metric_to_groups.values() if groups == [group_id]) for group_id in WEB_VITRINA_SOURCE_GROUPS},
    )


def _assert_other_sources_recomputes_derived_metrics() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-group-coverage-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-20T09:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")
        current_state = runtime.load_current_state()
        nm_id = next(item.nm_id for item in current_state.config_v2 if item.enabled)
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at=OLD_REFRESHED_AT,
            plan=_build_previous_plan(nm_id=nm_id),
        )

        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=Path(tmp),
            runtime=runtime,
            activated_at_factory=lambda: NEW_REFRESHED_AT,
            refreshed_at_factory=lambda: NEW_REFRESHED_AT,
            now_factory=lambda: datetime(2026, 4, 21, 15, 0, tzinfo=timezone.utc),
        )
        captured: dict[str, object] = {}

        def build_partial_plan(**kwargs: object) -> SheetVitrinaV1Envelope:
            captured["source_keys"] = list(kwargs.get("source_keys") or [])
            captured["metric_keys"] = list(kwargs.get("metric_keys") or [])
            return _build_partial_other_sources_plan(nm_id=nm_id)

        entrypoint.sheet_plan_block.build_plan = build_partial_plan  # type: ignore[method-assign]
        job = entrypoint.start_sheet_source_group_refresh_job(
            source_group_id="other_sources",
            as_of_date="2026-04-21",
        )
        job_snapshot = _wait_job(entrypoint, str(job["job_id"]))
        if job_snapshot["status"] != "success":
            raise AssertionError(f"other_sources group refresh must succeed, got {job_snapshot}")
        for metric_key in DERIVED_OTHER_SOURCE_METRICS:
            if metric_key not in captured["metric_keys"]:
                raise AssertionError(f"other_sources must include derived metric {metric_key}, got {captured}")

        merged = runtime.load_sheet_vitrina_ready_snapshot(as_of_date="2026-04-20")
        data_rows = {row[1]: row for row in _sheet(merged, "DATA_VITRINA").rows}
        expected_proxy_profit = 45.6
        expected_margin = 0.0456
        assert_close(data_rows[f"SKU:{nm_id}|cost_price_rub"][3], 200.0, "cost price selected date")
        assert_close(data_rows[f"SKU:{nm_id}|cost_price_rub"][2], 100.0, "cost price unselected date")
        assert_close(data_rows[f"SKU:{nm_id}|proxy_profit_rub"][3], expected_proxy_profit, "sku proxy profit")
        assert_close(data_rows[f"SKU:{nm_id}|proxy_profit_rub"][2], 0.0, "sku proxy profit unselected date")
        assert_close(data_rows[f"SKU:{nm_id}|proxy_margin_pct"][3], expected_margin, "sku proxy margin")
        assert_close(data_rows["TOTAL|total_proxy_profit_rub"][3], expected_proxy_profit, "total proxy profit")
        assert_close(data_rows["TOTAL|proxy_margin_pct_total"][3], expected_margin, "total proxy margin")

        metadata = dict(getattr(merged, "metadata", {}) or {})
        row_updated_at = metadata.get("row_last_updated_at_by_row_id") or {}
        for row_id in (
            f"SKU:{nm_id}|cost_price_rub",
            f"SKU:{nm_id}|proxy_profit_rub",
            f"SKU:{nm_id}|proxy_margin_pct",
            "TOTAL|total_proxy_profit_rub",
            "TOTAL|proxy_margin_pct_total",
        ):
            if row_updated_at.get(row_id) != NEW_REFRESHED_AT:
                raise AssertionError(f"processed other_sources row timestamp must advance for {row_id}: {row_updated_at}")

        result = job_snapshot["result"]
        updated_cells = result["merge_summary"]["updated_cells"]
        highlighted = {(cell["row_id"], cell["as_of_date"], cell["status"]) for cell in updated_cells}
        for row_id in (
            f"SKU:{nm_id}|cost_price_rub",
            f"SKU:{nm_id}|proxy_profit_rub",
            f"SKU:{nm_id}|proxy_margin_pct",
            "TOTAL|total_proxy_profit_rub",
            "TOTAL|proxy_margin_pct_total",
        ):
            if (row_id, "2026-04-21", "updated") not in highlighted:
                raise AssertionError(f"updated_cells must include green highlight for {row_id}, got {updated_cells}")

        log_text, _ = entrypoint.handle_sheet_operator_job_text_request(str(job["job_id"]))
        for expected in (
            "source_group_id=other_sources",
            "as_of_date=2026-04-21",
            "updated_cells=5",
            "latest_confirmed_cells=0",
        ):
            if expected not in log_text:
                raise AssertionError(f"other_sources log missing {expected!r}: {log_text}")
        print(
            "web_vitrina_other_sources_derived_refresh: ok ->",
            result["merge_summary"]["updated_cell_count"],
            sorted(DERIVED_OTHER_SOURCE_METRICS),
        )


def _build_previous_plan(*, nm_id: int) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="previous-other-sources-snapshot",
        as_of_date="2026-04-20",
        date_columns=["2026-04-20", "2026-04-21"],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(slot_key="yesterday_closed", slot_label="Вчера", column_date="2026-04-20"),
            SheetVitrinaV1TemporalSlot(slot_key="today_current", slot_label="Сегодня", column_date="2026-04-21"),
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:D10",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", "2026-04-20", "2026-04-21"],
                rows=[
                    ["Итого: сумма заказов", "TOTAL|total_orderSum", 900, 1000],
                    ["Итого: прибыль прокси", "TOTAL|total_proxy_profit_rub", 0, 0],
                    ["Итого: маржинальность", "TOTAL|proxy_margin_pct_total", 0, 0],
                    ["SKU: сумма заказов", f"SKU:{nm_id}|orderSum", 900, 1000],
                    ["SKU: количество заказов", f"SKU:{nm_id}|orderCount", 1, 2],
                    ["SKU: реклама", f"SKU:{nm_id}|ads_sum", 50, 100],
                    ["SKU: себестоимость", f"SKU:{nm_id}|cost_price_rub", 100, 100],
                    ["SKU: прибыль прокси", f"SKU:{nm_id}|proxy_profit_rub", 0, 0],
                    ["SKU: маржинальность", f"SKU:{nm_id}|proxy_margin_pct", 0, 0],
                ],
                row_count=9,
                column_count=4,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K3",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[
                    _status_row("cost_price[yesterday_closed]", "success", "old cost yesterday"),
                    _status_row("cost_price[today_current]", "success", "old cost today"),
                ],
                row_count=2,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


def _build_partial_other_sources_plan(*, nm_id: int) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="partial-other-sources-snapshot",
        as_of_date="2026-04-20",
        date_columns=["2026-04-20", "2026-04-21"],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(slot_key="yesterday_closed", slot_label="Вчера", column_date="2026-04-20"),
            SheetVitrinaV1TemporalSlot(slot_key="today_current", slot_label="Сегодня", column_date="2026-04-21"),
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:D6",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", "2026-04-20", "2026-04-21"],
                rows=[
                    ["SKU: себестоимость", f"SKU:{nm_id}|cost_price_rub", 111, 200],
                    ["SKU: прибыль прокси", f"SKU:{nm_id}|proxy_profit_rub", 999, 999],
                    ["SKU: маржинальность", f"SKU:{nm_id}|proxy_margin_pct", 999, 999],
                    ["Итого: прибыль прокси", "TOTAL|total_proxy_profit_rub", 999, 999],
                    ["Итого: маржинальность", "TOTAL|proxy_margin_pct_total", 999, 999],
                ],
                row_count=5,
                column_count=4,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[_status_row("cost_price[today_current]", "success", "new cost today")],
                row_count=1,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


def _status_row(source_key: str, kind: str, note: str) -> list[object]:
    return [
        source_key,
        kind,
        "2026-04-21",
        "2026-04-21",
        "2026-04-21",
        "",
        "",
        1,
        1 if kind == "success" else 0,
        "",
        note,
    ]


def _sheet(plan: SheetVitrinaV1Envelope, sheet_name: str) -> SheetVitrinaWriteTarget:
    for sheet in plan.sheets:
        if sheet.sheet_name == sheet_name:
            return sheet
    raise AssertionError(f"missing sheet {sheet_name}")


def _wait_job(entrypoint: RegistryUploadHttpEntrypoint, job_id: str) -> dict[str, object]:
    for _ in range(80):
        snapshot = entrypoint.operator_jobs.get(job_id)
        if snapshot["status"] != "running":
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def assert_close(actual: object, expected: float, label: str) -> None:
    if abs(float(actual) - expected) > 0.000001:
        raise AssertionError(f"{label}: expected {expected}, got {actual}")


if __name__ == "__main__":
    main()
