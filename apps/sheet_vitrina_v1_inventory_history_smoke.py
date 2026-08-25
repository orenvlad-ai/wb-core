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
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
import packages.application.sheet_vitrina_v1_inventory_history as inventory_history  # noqa: E402
from packages.application.inventory_planning_read_model import (  # noqa: E402
    _active_wb_snapshot,
    _wb_items,
)
from packages.application.sheet_vitrina_v1_inventory_history import (  # noqa: E402
    CAPTURES_TABLE,
    COMPONENTS_TABLE,
    FINALIZATIONS_TABLE,
    append_inventory_history_capture,
    append_inventory_history_finalization,
    capture_inventory_history_from_ready_plan,
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
    _late_closed_date_ready_evidence_smoke()
    _current_ui_wb_operand_smoke()
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
            previous_total=31,
            previous_first=11,
            previous_second=20,
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
            previous_total=32,
            previous_first=12,
            previous_second=20,
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
            assert day_captures == 5, "distinct accepted source revisions must append"
            assert len(finalizations) == 2
            assert finalizations[1][1] == finalizations[0][0]


def _late_closed_date_ready_evidence_smoke() -> None:
    with TemporaryDirectory(prefix="inventory-history-late-closed-") as raw:
        db_path = Path(raw) / "warehouse.sqlite3"
        conn = sqlite3.connect(db_path)
        ensure_inventory_history_schema(conn)
        roster = [
            {
                "facility_id": "moscow",
                "code": "FBS-MOSCOW",
                "name": "Москва",
                "active": True,
                "applicable": True,
                "effective_from": "2026-08-14",
                "display_order": 1,
            },
            {
                "facility_id": "orenburg",
                "code": "FBS-ORENBURG",
                "name": "Оренбург",
                "active": True,
                "applicable": True,
                "effective_from": "2026-08-20",
                "display_order": 2,
            },
        ]
        first_nm_id, second_nm_id = 100_001, 100_002
        base_components = [
            _component("TOTAL", "TOTAL", None, "WB", "WB", None, state="missing"),
            _component("TOTAL", "TOTAL", None, "FBS_FACILITY", "moscow", 82_900),
            _component("TOTAL", "TOTAL", None, "FBS_FACILITY", "orenburg", 26_697),
            _component(
                "SKU", f"SKU:{first_nm_id}", first_nm_id, "WB", "WB", None, state="missing"
            ),
            _component(
                "SKU", f"SKU:{first_nm_id}", first_nm_id, "FBS_FACILITY", "moscow", 40_000
            ),
            _component(
                "SKU",
                f"SKU:{first_nm_id}",
                first_nm_id,
                "FBS_FACILITY",
                "orenburg",
                None,
                state="missing",
            ),
            _component(
                "SKU", f"SKU:{second_nm_id}", second_nm_id, "WB", "WB", None, state="missing"
            ),
            _component(
                "SKU", f"SKU:{second_nm_id}", second_nm_id, "FBS_FACILITY", "moscow", 42_900
            ),
            _component(
                "SKU", f"SKU:{second_nm_id}", second_nm_id, "FBS_FACILITY", "orenburg", 26_697
            ),
        ]
        base = append_inventory_history_capture(
            conn,
            business_date="2026-08-23",
            capture_kind="accepted_refresh",
            formula_version="inventory_planning_v1",
            facility_roster=roster,
            source_manifest={"revision": "initial-wb-missing", "business_date": "2026-08-23"},
            components=base_components,
            captured_at="2026-08-23T18:00:00Z",
        )
        append_inventory_history_finalization(
            conn,
            business_date="2026-08-23",
            capture_id=base["capture_id"],
            finalization_identity="initial-close:2026-08-23",
            finalized_at="2026-08-24T00:00:00Z",
            provenance={"fixture": "initial-wb-missing"},
        )
        # A newer current-day balance must never be copied into 2026-08-23.
        append_inventory_history_capture(
            conn,
            business_date="2026-08-24",
            capture_kind="accepted_refresh",
            formula_version="inventory_planning_v1",
            facility_roster=roster,
            source_manifest={"revision": "current-fbs-must-not-retrocopy"},
            components=[
                _component("TOTAL", "TOTAL", None, "WB", "WB", 1),
                _component("TOTAL", "TOTAL", None, "FBS_FACILITY", "moscow", 999_999),
                _component("TOTAL", "TOTAL", None, "FBS_FACILITY", "orenburg", 888_888),
            ],
            captured_at="2026-08-24T10:00:00Z",
        )
        conn.commit()
        conn.close()

        initial = read_inventory_history_window(
            db_path,
            dates=["2026-08-23"],
            current_date="2026-08-24",
        )["dates"]["2026-08-23"]["scopes"]["TOTAL"]
        assert initial["wb"]["state"] == "missing"
        assert initial["total"] == 109_597
        assert initial["quality"] == "partial"
        assert initial["missing_components"] == ["WB"]

        late_plan = _closed_ready_plan(
            snapshot_id="closed-wb-revision-1",
            business_date="2026-08-23",
            total=50_162,
            first=30_000,
            second=20_162,
            nm_ids=[first_nm_id, second_nm_id],
        )
        with sqlite3.connect(db_path) as conn:
            first_result = capture_inventory_history_from_ready_plan(
                conn,
                plan=late_plan,
                bundle_version="late-ready-bundle-v1",
                refreshed_at="2026-08-24T10:29:19Z",
                generation_identity="generation-v1",
            )
            conn.commit()
        assert first_result["closed_capture_inserted"] is True
        corrected = read_inventory_history_window(
            db_path,
            dates=["2026-08-23"],
            current_date="2026-08-24",
        )["dates"]["2026-08-23"]
        total = corrected["scopes"]["TOTAL"]
        assert total["wb"] == {
            "state": "exact",
            "value": 50_162,
            "source_revision": "ready:late-ready-bundle-v1:closed-wb-revision-1:2026-08-23",
            "source_digest": total["wb"]["source_digest"],
            "source_watermark": "closed-wb-revision-1",
        }
        assert total["facilities"]["moscow"]["value"] == 82_900
        assert total["facilities"]["orenburg"]["value"] == 26_697
        assert total["total"] == 159_759
        assert total["quality"] == "full"
        first_sku = corrected["scopes"][f"SKU:{first_nm_id}"]
        assert first_sku["total"] == 70_000
        assert first_sku["quality"] == "partial"
        assert first_sku["missing_components"] == ["Оренбург"]
        second_sku = corrected["scopes"][f"SKU:{second_nm_id}"]
        assert second_sku["total"] == 89_759
        assert second_sku["quality"] == "full"

        with sqlite3.connect(db_path) as conn:
            component_kinds = {
                str(row[0])
                for row in conn.execute(
                    f"SELECT DISTINCT component_kind FROM {COMPONENTS_TABLE} "
                    "WHERE capture_id=?",
                    (corrected["capture_id"],),
                ).fetchall()
            }
            assert component_kinds == {"WB", "FBS_FACILITY"}
            assert conn.execute(
                f"SELECT COUNT(*) FROM {COMPONENTS_TABLE} WHERE capture_id=?",
                (corrected["capture_id"],),
            ).fetchone()[0] == 9, "WB plus two FBS operands per scope; no double-count"
            before_counts = (
                conn.execute(
                    f"SELECT COUNT(*) FROM {CAPTURES_TABLE} WHERE business_date='2026-08-23'"
                ).fetchone()[0],
                conn.execute(
                    f"SELECT COUNT(*) FROM {FINALIZATIONS_TABLE} WHERE business_date='2026-08-23'"
                ).fetchone()[0],
            )
            repeated = capture_inventory_history_from_ready_plan(
                conn,
                plan=late_plan,
                bundle_version="late-ready-bundle-v1",
                refreshed_at="2026-08-24T10:31:37Z",
                generation_identity="generation-v1",
            )
            conn.commit()
            after_repeat_counts = (
                conn.execute(
                    f"SELECT COUNT(*) FROM {CAPTURES_TABLE} WHERE business_date='2026-08-23'"
                ).fetchone()[0],
                conn.execute(
                    f"SELECT COUNT(*) FROM {FINALIZATIONS_TABLE} WHERE business_date='2026-08-23'"
                ).fetchone()[0],
            )
            assert repeated["closed_capture_inserted"] is False
            assert after_repeat_counts == before_counts

            revised_plan = _closed_ready_plan(
                snapshot_id="closed-wb-revision-2",
                business_date="2026-08-23",
                total=50_162,
                first=30_000,
                second=20_162,
                nm_ids=[first_nm_id, second_nm_id],
            )
            revised = capture_inventory_history_from_ready_plan(
                conn,
                plan=revised_plan,
                bundle_version="late-ready-bundle-v1",
                refreshed_at="2026-08-24T10:35:00Z",
                generation_identity="generation-v1",
            )
            conn.commit()
            assert revised["closed_capture_inserted"] is True
            assert conn.execute(
                f"SELECT COUNT(*) FROM {CAPTURES_TABLE} WHERE business_date='2026-08-23'"
            ).fetchone()[0] == before_counts[0] + 1
            revised_wb = conn.execute(
                f"""SELECT source_revision,provenance_json FROM {COMPONENTS_TABLE}
                    WHERE capture_id=? AND scope_key='TOTAL' AND component_kind='WB'""",
                (revised["closed_capture_id"],),
            ).fetchone()
            assert revised_wb is not None
            assert str(revised_wb[0]).endswith(":closed-wb-revision-2:2026-08-23")
            assert json.loads(str(revised_wb[1]))["ready_snapshot_id"] == "closed-wb-revision-2"
            finalizations = conn.execute(
                f"SELECT finalization_digest,supersedes_finalization_digest "
                f"FROM {FINALIZATIONS_TABLE} WHERE business_date='2026-08-23' "
                "ORDER BY finalization_sequence"
            ).fetchall()
            assert finalizations[-1][1] == finalizations[-2][0]


def _current_ui_wb_operand_smoke() -> None:
    with TemporaryDirectory(prefix="inventory-history-current-ui-") as raw:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw) / "runtime")
        bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        bundle["bundle_version"] = "inventory-history-current-ui-v1"
        bundle["uploaded_at"] = "2026-08-24T08:00:00Z"
        assert runtime.ingest_bundle(
            bundle, activated_at="2026-08-24T08:00:00Z"
        ).status == "accepted"
        state = runtime.load_current_state()
        nm_ids = [int(item.nm_id) for item in state.config_v2 if item.enabled][:2]
        with sqlite3.connect(runtime.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE sheet_vitrina_v1_warehouse_functional_active(
                    slot INTEGER PRIMARY KEY,version_id TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE sheet_vitrina_v1_warehouse_wb_snapshots(
                    snapshot_id TEXT PRIMARY KEY,version_id TEXT NOT NULL,fetched_at TEXT NOT NULL,
                    snapshot_date TEXT NOT NULL,requested_nm_ids_json TEXT NOT NULL,
                    pagination_complete INTEGER NOT NULL,page_count INTEGER NOT NULL,
                    page_offsets_json TEXT NOT NULL,raw_row_count INTEGER NOT NULL,
                    raw_rows_digest TEXT NOT NULL,raw_rows_json TEXT NOT NULL,
                    items_json TEXT NOT NULL,created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
                       snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,
                       pagination_complete,page_count,page_offsets_json,raw_row_count,
                       raw_rows_digest,raw_rows_json,items_json,created_at
                   ) VALUES(?,?,?,?,?,1,1,'[0]',2,?,'[]',?,?)""",
                (
                    "ui-wb-snapshot-1",
                    "ui-wb-version-1",
                    "2026-08-24T09:00:00Z",
                    "2026-08-24",
                    json.dumps(nm_ids),
                    "sha256:ui-wb-snapshot-1",
                    json.dumps(
                        [
                            {"nm_id": nm_ids[0], "quantity": 30_000},
                            {"nm_id": nm_ids[1], "quantity": 20_162},
                        ]
                    ),
                    "2026-08-24T09:00:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_warehouse_functional_active("
                "slot,version_id,updated_at) "
                "VALUES(1,'ui-wb-version-1','2026-08-24T09:00:00Z')"
            )
            conn.commit()
            conn.row_factory = sqlite3.Row
            ui_snapshot = _active_wb_snapshot(conn)
            assert ui_snapshot is not None
            ui_total = sum(item["quantity"] for item in _wb_items(ui_snapshot))
        ready = _ready_plan(
            current_date="2026-08-24",
            previous_date=None,
            snapshot_id="ready-current-column-differs",
            total=1,
            first=1,
            second=0,
            nm_ids=nm_ids,
            archived_nm_id=999_991,
            missing_nm_id=999_992,
        )
        empty_fbs = {
            "facilities": [],
            "formula_epoch": {},
            "updated_at": "",
        }
        with patch.object(inventory_history, "_fbs_facilities", return_value=empty_fbs):
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=state,
                refreshed_at="2026-08-24T09:01:00Z",
                plan=ready,
            )
        assert ui_total == 50_162
        current = read_inventory_history_window(
            runtime.db_path,
            dates=["2026-08-24"],
            current_date="2026-08-24",
        )["dates"]["2026-08-24"]["scopes"]
        assert current["TOTAL"]["wb"]["value"] == ui_total == 50_162
        assert current[f"SKU:{nm_ids[0]}"]["wb"]["value"] == 30_000
        assert current[f"SKU:{nm_ids[1]}"]["wb"]["value"] == 20_162
        assert current["TOTAL"]["wb"]["source_revision"] == "wb_snapshot:ui-wb-snapshot-1"


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
    previous_total: int | None = None,
    previous_first: int | None = None,
    previous_second: int | None = None,
) -> SheetVitrinaV1Envelope:
    dates = [item for item in (previous_date, current_date) if item]
    total_values = (
        [previous_total if previous_total is not None else total, total]
        if previous_date
        else [total]
    )
    first_values = (
        [previous_first if previous_first is not None else first, first]
        if previous_date
        else [first]
    )
    second_values = (
        [previous_second if previous_second is not None else second, second]
        if previous_date
        else [second]
    )
    rows = [
        ["Остатки", "TOTAL|total_stock_total", *total_values],
        ["Остатки", f"SKU:{nm_ids[0]}|stock_total", *first_values],
        ["Остатки", f"SKU:{nm_ids[1]}|stock_total", *second_values],
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


def _closed_ready_plan(
    *,
    snapshot_id: str,
    business_date: str,
    total: int,
    first: int,
    second: int,
    nm_ids: list[int],
) -> SheetVitrinaV1Envelope:
    rows = [
        ["Остатки", "TOTAL|total_stock_total", total],
        ["Остатки", f"SKU:{nm_ids[0]}|stock_total", first],
        ["Остатки", f"SKU:{nm_ids[1]}|stock_total", second],
    ]
    return SheetVitrinaV1Envelope(
        plan_version="inventory-history-closed-ready-plan-v1",
        snapshot_id=snapshot_id,
        as_of_date="2026-08-24",
        date_columns=[business_date],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="Вчера",
                column_date=business_date,
            )
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect=f"A1:C{len(rows) + 1}",
                clear_range="A:C",
                write_mode="values",
                partial_update_allowed=False,
                header=["label", "key", business_date],
                rows=rows,
                row_count=len(rows),
                column_count=3,
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
        "component_label": {
            "moscow": "Москва",
            "orenburg": "Оренбург",
        }.get(component_id, component_id),
        "state": effective_state,
        "quantity": quantity,
        "source_revision": "fixture-v1",
        "source_digest": "sha256:fixture",
        "source_watermark": "fixture-watermark",
        "provenance": {"fixture": True},
    }


if __name__ == "__main__":
    raise SystemExit(main())
