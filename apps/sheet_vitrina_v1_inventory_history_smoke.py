#!/usr/bin/env python3
"""Contract, supersession and realistic-window smoke for inventory history."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import perf_counter


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_inventory_history import (  # noqa: E402
    CAPTURES_TABLE,
    COMPONENTS_TABLE,
    FINALIZATIONS_TABLE,
    append_inventory_history_capture,
    append_inventory_history_finalization,
    ensure_inventory_history_schema,
    read_inventory_history_window,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)


BUNDLE_FIXTURE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "registry_upload_bundle__fixture.json"
)


def main() -> int:
    _capture_and_supersession_smoke()
    _realistic_window_smoke()
    print("sheet_vitrina_v1_inventory_history_smoke: OK")
    return 0


def _capture_and_supersession_smoke() -> None:
    with TemporaryDirectory(prefix="inventory-history-capture-") as raw:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw) / "runtime")
        bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        bundle["bundle_version"] = "inventory-history-capture-v1"
        bundle["uploaded_at"] = "2026-08-21T08:00:00Z"
        assert runtime.ingest_bundle(
            bundle, activated_at="2026-08-21T08:00:00Z"
        ).status == "accepted"
        state = runtime.load_current_state()
        nm_ids = [int(item.nm_id) for item in state.config_v2 if item.enabled][:2]
        archived_nm_id = 999_991
        missing_nm_id = 999_992

        first = _ready_plan(
            current_date="2026-08-21",
            previous_date=None,
            snapshot_id="capture-1",
            total=30,
            first=10,
            second=20,
            nm_ids=nm_ids,
            archived_nm_id=archived_nm_id,
            missing_nm_id=missing_nm_id,
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=state,
            refreshed_at="2026-08-21T18:00:00Z",
            plan=first,
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=state,
            refreshed_at="2026-08-21T18:00:00Z",
            plan=first,
        )
        correction = _ready_plan(
            current_date="2026-08-21",
            previous_date=None,
            snapshot_id="capture-2",
            total=31,
            first=11,
            second=20,
            nm_ids=nm_ids,
            archived_nm_id=archived_nm_id,
            missing_nm_id=missing_nm_id,
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=state,
            refreshed_at="2026-08-21T19:00:00Z",
            plan=correction,
        )
        close_once = _ready_plan(
            current_date="2026-08-22",
            previous_date="2026-08-21",
            snapshot_id="close-1",
            total=40,
            first=15,
            second=25,
            nm_ids=nm_ids,
            archived_nm_id=archived_nm_id,
            missing_nm_id=missing_nm_id,
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=state,
            refreshed_at="2026-08-22T18:00:00Z",
            plan=close_once,
        )

        late = _ready_plan(
            current_date="2026-08-21",
            previous_date=None,
            snapshot_id="capture-3-late",
            total=32,
            first=12,
            second=20,
            nm_ids=nm_ids,
            archived_nm_id=archived_nm_id,
            missing_nm_id=missing_nm_id,
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=state,
            refreshed_at="2026-08-22T19:00:00Z",
            plan=late,
        )
        close_twice = _ready_plan(
            current_date="2026-08-22",
            previous_date="2026-08-21",
            snapshot_id="close-2",
            total=40,
            first=15,
            second=25,
            nm_ids=nm_ids,
            archived_nm_id=archived_nm_id,
            missing_nm_id=missing_nm_id,
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=state,
            refreshed_at="2026-08-22T20:00:00Z",
            plan=close_twice,
        )

        history = read_inventory_history_window(
            runtime.db_path,
            dates=["2026-08-21", "2026-08-22"],
            current_date="2026-08-22",
        )
        closed = history["dates"]["2026-08-21"]
        assert closed["scopes"]["TOTAL"]["total"] == 32
        assert closed["scopes"]["TOTAL"]["quality"] == "full"
        assert closed["scopes"][f"SKU:{archived_nm_id}"]["wb"]["state"] == "exact_zero"
        assert closed["scopes"][f"SKU:{archived_nm_id}"]["total"] == 0
        assert closed["scopes"][f"SKU:{missing_nm_id}"]["wb"]["state"] == "missing"
        assert closed["scopes"][f"SKU:{missing_nm_id}"]["total"] is None
        assert closed["scopes"][f"SKU:{missing_nm_id}"]["quality"] == "unavailable"
        assert history["dates"]["2026-08-22"]["scopes"]["TOTAL"]["total"] == 40
        with sqlite3.connect(runtime.db_path) as conn:
            day_captures = conn.execute(
                f"SELECT COUNT(*) FROM {CAPTURES_TABLE} WHERE business_date='2026-08-21'"
            ).fetchone()[0]
            finalizations = conn.execute(
                f"SELECT finalization_digest,supersedes_finalization_digest "
                f"FROM {FINALIZATIONS_TABLE} WHERE business_date='2026-08-21' "
                "ORDER BY finalization_sequence"
            ).fetchall()
            assert day_captures == 3, "identical refresh must be idempotent"
            assert len(finalizations) == 2
            assert finalizations[1][1] == finalizations[0][0]


def _realistic_window_smoke() -> None:
    with TemporaryDirectory(prefix="inventory-history-window-") as raw:
        db_path = Path(raw) / "warehouse.sqlite3"
        conn = sqlite3.connect(db_path)
        ensure_inventory_history_schema(conn)
        roster = [
            {
                "facility_id": f"facility-{index}",
                "code": f"FBS-{index}",
                "name": f"Facility {index}",
                "active": index < 4,
                "applicable": True,
                "effective_from": "2026-03-01",
                "display_order": index,
            }
            for index in range(1, 5)
        ]
        dates = [
            (date(2026, 3, 1) + timedelta(days=offset)).isoformat()
            for offset in range(174)
        ]
        started = perf_counter()
        for date_index, business_date in enumerate(dates):
            components = []
            for scope_index in range(34):
                scope_kind = "TOTAL" if scope_index == 0 else "SKU"
                nm_id = None if scope_index == 0 else 40_000_000 + scope_index
                scope_key = "TOTAL" if nm_id is None else f"SKU:{nm_id}"
                components.append(
                    _component(scope_kind, scope_key, nm_id, "WB", "WB", date_index + scope_index)
                )
                for facility_index in range(1, 5):
                    state = (
                        "inapplicable"
                        if date_index < facility_index
                        else "missing"
                        if date_index == 10 and facility_index == 2
                        else "exact_zero"
                        if (date_index + scope_index + facility_index) % 17 == 0
                        else "exact"
                    )
                    value = (
                        None
                        if state in {"missing", "inapplicable"}
                        else 0
                        if state == "exact_zero"
                        else facility_index
                    )
                    components.append(
                        _component(
                            scope_kind,
                            scope_key,
                            nm_id,
                            "FBS_FACILITY",
                            f"facility-{facility_index}",
                            value,
                            state=state,
                        )
                    )
            capture = append_inventory_history_capture(
                conn,
                business_date=business_date,
                capture_kind="historical_backfill",
                formula_version="inventory_planning_v1",
                facility_roster=roster,
                source_manifest={"date": business_date, "revision": date_index},
                components=components,
                captured_at=business_date + "T20:00:00Z",
            )
            append_inventory_history_finalization(
                conn,
                business_date=business_date,
                capture_id=capture["capture_id"],
                finalization_identity=f"performance:{business_date}",
                finalized_at=business_date + "T21:00:00Z",
                provenance={"fixture": "realistic-window"},
            )
        conn.commit()
        write_seconds = perf_counter() - started
        conn.close()

        started = perf_counter()
        history = read_inventory_history_window(
            db_path,
            dates=dates,
            current_date="2026-08-23",
        )
        read_seconds = perf_counter() - started
        assert len(history["dates"]) == 174
        assert len(history["facilities"]) == 4
        assert write_seconds < 10.0, write_seconds
        assert read_seconds < 5.0, read_seconds
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {CAPTURES_TABLE}").fetchone()[0] == 174
            assert conn.execute(f"SELECT COUNT(*) FROM {COMPONENTS_TABLE}").fetchone()[0] == 29_580
            columns = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({CAPTURES_TABLE})").fetchall()
            }
            assert "plan_json" not in columns and "rendered_snapshot_json" not in columns
            try:
                conn.execute(f"DELETE FROM {CAPTURES_TABLE}")
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("indefinite-retention delete guard is missing")


def _ready_plan(
    *,
    current_date: str,
    previous_date: str | None,
    snapshot_id: str,
    total: int,
    first: int,
    second: int,
    nm_ids: list[int],
    archived_nm_id: int,
    missing_nm_id: int,
) -> SheetVitrinaV1Envelope:
    dates = [item for item in (previous_date, current_date) if item]
    values = [total for _ in dates]
    rows = [
        ["Остатки", "TOTAL|total_stock_total", *values],
        ["Остатки", f"SKU:{nm_ids[0]}|stock_total", *[first for _ in dates]],
        ["Остатки", f"SKU:{nm_ids[1]}|stock_total", *[second for _ in dates]],
        ["Остатки", f"SKU:{archived_nm_id}|stock_total", *[0 for _ in dates]],
        ["Остатки", f"SKU:{missing_nm_id}|stock_total", *[None for _ in dates]],
    ]
    slots = []
    if previous_date:
        slots.append(
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="Вчера",
                column_date=previous_date,
            )
        )
    slots.append(
        SheetVitrinaV1TemporalSlot(
            slot_key="today_current",
            slot_label="Сегодня",
            column_date=current_date,
        )
    )
    return SheetVitrinaV1Envelope(
        plan_version="inventory-history-capture-plan-v1",
        snapshot_id=snapshot_id,
        as_of_date=current_date,
        date_columns=dates,
        temporal_slots=slots,
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect=f"A1:{chr(66 + len(dates))}{len(rows) + 1}",
                clear_range="A:Z",
                write_mode="values",
                partial_update_allowed=False,
                header=["label", "key", *dates],
                rows=rows,
                row_count=len(rows),
                column_count=2 + len(dates),
            )
        ],
    )


def _component(
    scope_kind: str,
    scope_key: str,
    nm_id: int | None,
    component_kind: str,
    component_id: str,
    quantity: int | None,
    *,
    state: str | None = None,
) -> dict[str, object]:
    effective_state = state or ("exact_zero" if quantity == 0 else "exact")
    return {
        "scope_kind": scope_kind,
        "scope_key": scope_key,
        "nm_id": nm_id,
        "component_kind": component_kind,
        "component_id": component_id,
        "component_label": component_id,
        "state": effective_state,
        "quantity": quantity,
        "source_revision": "fixture-v1",
        "source_digest": "sha256:fixture",
        "source_watermark": "fixture-watermark",
        "provenance": {"fixture": True},
    }


if __name__ == "__main__":
    raise SystemExit(main())
