"""Targeted smoke for web-vitrina grouped refresh partial-update safety."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.contracts.sheet_vitrina_v1 import (
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


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-group-refresh-") as tmp:
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
            return _build_partial_wb_api_plan(nm_id=nm_id)

        entrypoint.sheet_plan_block.build_plan = build_partial_plan  # type: ignore[method-assign]
        job = entrypoint.start_sheet_source_group_refresh_job(
            source_group_id="wb_api",
            as_of_date="2026-04-21",
        )
        job_snapshot = _wait_job(entrypoint, str(job["job_id"]))
        if job_snapshot["status"] != "success":
            raise AssertionError(f"group refresh job must succeed, got {job_snapshot}")
        result = job_snapshot["result"]
        merge_summary = result["merge_summary"]
        if merge_summary["rows_updated"] != 1 or merge_summary["rows_preserved"] != 2:
            raise AssertionError(f"group refresh must update only selected rows, got {merge_summary}")
        if result.get("updated_cells") != merge_summary["updated_cells"]:
            raise AssertionError("group refresh result must expose updated_cells at the top level")
        if result.get("updated_cell_count") != merge_summary["updated_cell_count"]:
            raise AssertionError("group refresh result must expose updated_cell_count at the top level")
        if result.get("latest_confirmed_cell_count") != merge_summary["latest_confirmed_cell_count"]:
            raise AssertionError("group refresh result must expose latest_confirmed_cell_count at the top level")
        if "price_seller_discounted" not in captured["metric_keys"]:
            raise AssertionError(f"wb_api refresh must select wb_api metric keys, got {captured}")

        merged = runtime.load_sheet_vitrina_ready_snapshot(as_of_date="2026-04-20")
        data_rows = {row[1]: row for row in _sheet(merged, "DATA_VITRINA").rows}
        price_row_id = f"SKU:{nm_id}|price_seller_discounted"
        seller_row_id = f"SKU:{nm_id}|view_count"
        other_row_id = f"SKU:{nm_id}|cost_price_rub"
        if data_rows[price_row_id][2] != 100 or data_rows[price_row_id][3] != 999:
            raise AssertionError(f"selected wb_api row must update only selected date, got {data_rows}")
        if data_rows[seller_row_id][2] != 20 or data_rows[seller_row_id][3] != 21:
            raise AssertionError(f"unrelated seller row must stay untouched, got {data_rows}")
        if data_rows[other_row_id][2] != 70 or data_rows[other_row_id][3] != 71:
            raise AssertionError(f"unrelated groups must stay untouched, got {data_rows}")
        status_rows = {row[0]: row for row in _sheet(merged, "STATUS").rows}
        if status_rows["prices_snapshot[yesterday_closed]"][10] != "old price yesterday":
            raise AssertionError(f"date-scoped refresh must preserve unselected status slot, got {status_rows}")
        if status_rows["prices_snapshot[today_current]"][10] != "new price today":
            raise AssertionError(f"date-scoped refresh must update selected status slot, got {status_rows}")
        metadata = dict(getattr(merged, "metadata", {}) or {})
        row_updated_at = metadata.get("row_last_updated_at_by_row_id") or {}
        group_updated_at = metadata.get("source_group_last_updated_at") or {}
        if row_updated_at.get(price_row_id) != NEW_REFRESHED_AT:
            raise AssertionError(f"selected row timestamp must advance, got {row_updated_at}")
        if row_updated_at.get(seller_row_id) != OLD_REFRESHED_AT or row_updated_at.get(other_row_id) != OLD_REFRESHED_AT:
            raise AssertionError(f"unrelated row timestamps must not advance, got {row_updated_at}")
        if group_updated_at.get("wb_api") != NEW_REFRESHED_AT:
            raise AssertionError(f"selected group timestamp must advance, got {group_updated_at}")
        if group_updated_at.get("seller_portal_bot") != OLD_REFRESHED_AT or group_updated_at.get("other_sources") != OLD_REFRESHED_AT:
            raise AssertionError(f"unrelated group timestamps must not advance, got {group_updated_at}")

        web_contract = SheetVitrinaV1WebVitrinaBlock(runtime=runtime).build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            as_of_date="2026-04-20",
        )
        row_timestamps = {row.row_id: row.row_last_updated_at for row in web_contract.rows}
        if row_timestamps.get(price_row_id) != NEW_REFRESHED_AT:
            raise AssertionError(f"web contract must surface per-row update timestamp, got {row_timestamps}")
        if row_timestamps.get(seller_row_id) != OLD_REFRESHED_AT:
            raise AssertionError(f"web contract must preserve unrelated row timestamp, got {row_timestamps}")

        try:
            entrypoint.start_sheet_source_group_refresh_job(
                source_group_id="wb_api",
                as_of_date="2026-04-19",
            )
        except ValueError as exc:
            if "Дата 2026-04-19 недоступна для обновления группы" not in str(exc):
                raise AssertionError(f"unsupported backend date must be human-readable, got {exc}") from exc
        else:
            raise AssertionError("unsupported group refresh date must fail before job creation")

        def failing_build_plan(**_: object) -> SheetVitrinaV1Envelope:
            raise RuntimeError("seller portal session invalid")

        entrypoint.sheet_plan_block.build_plan = failing_build_plan  # type: ignore[method-assign]
        failed_job = entrypoint.start_sheet_source_group_refresh_job(source_group_id="seller_portal_bot")
        failed_snapshot = _wait_job(entrypoint, str(failed_job["job_id"]))
        if failed_snapshot["status"] != "error" or "failed at source_fetch" not in str(failed_snapshot.get("error") or ""):
            raise AssertionError(f"group refresh failures must be stage-aware, got {failed_snapshot}")

        log_text, _ = entrypoint.handle_sheet_operator_job_text_request(str(job["job_id"]))
        for expected in (
            "event=group_refresh_start",
            "as_of_date=2026-04-21",
            "stage=source_fetch",
            "stage=prepare_materialize",
            "stage=load_group_to_vitrina",
            "event=group_refresh_finish status=success",
        ):
            if expected not in log_text:
                raise AssertionError(f"group refresh log missing {expected!r}: {log_text}")

        print("web_vitrina_group_refresh_partial_update: ok ->", merge_summary)
        print("web_vitrina_group_refresh_unsupported_date: ok -> 2026-04-19")
        print("web_vitrina_group_refresh_timestamps: ok ->", row_updated_at[price_row_id], row_updated_at[seller_row_id])
        print("web_vitrina_group_refresh_stage_failure: ok ->", failed_snapshot["error"])


def _build_previous_plan(*, nm_id: int) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="previous-full-snapshot",
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
                write_rect="A1:D4",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", "2026-04-20", "2026-04-21"],
                rows=[
                    ["SKU: цена со скидкой", f"SKU:{nm_id}|price_seller_discounted", 100, 101],
                    ["SKU: показы", f"SKU:{nm_id}|view_count", 20, 21],
                    ["SKU: себестоимость", f"SKU:{nm_id}|cost_price_rub", 70, 71],
                ],
                row_count=3,
                column_count=4,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K4",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[
                    _status_row("prices_snapshot[yesterday_closed]", "success", "old price yesterday"),
                    _status_row("prices_snapshot[today_current]", "success", "old price today"),
                    _status_row("seller_funnel_snapshot[today_current]", "success", "old seller"),
                    _status_row("cost_price[today_current]", "success", "old cost"),
                ],
                row_count=4,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


def _build_partial_wb_api_plan(*, nm_id: int) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="partial-wb-api-snapshot",
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
                write_rect="A1:D2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", "2026-04-20", "2026-04-21"],
                rows=[["SKU: цена со скидкой", f"SKU:{nm_id}|price_seller_discounted", 111, 999]],
                row_count=1,
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
                rows=[
                    _status_row("prices_snapshot[yesterday_closed]", "success", "new price yesterday"),
                    _status_row("prices_snapshot[today_current]", "success", "new price today"),
                ],
                row_count=2,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


def _status_row(source_key: str, kind: str, note: str) -> list[object]:
    return [
        source_key,
        kind,
        "2026-04-20",
        "2026-04-20",
        "2026-04-20",
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
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        snapshot = entrypoint.handle_sheet_operator_job_request(job_id)
        if snapshot["status"] != "running":
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


if __name__ == "__main__":
    main()
