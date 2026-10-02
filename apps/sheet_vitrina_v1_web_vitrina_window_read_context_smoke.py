"""A window reader pins one query-only SQLite snapshot across nested readers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ready_publication_fixture import save_ready_fixture
from apps.sheet_vitrina_v1_web_vitrina_page_composition_smoke import BUNDLE_FIXTURE, _build_plan
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.own_product_capital import OwnProductCapitalBlock
from packages.application.sheet_vitrina_v1_web_vitrina import (
    SheetVitrinaV1WebVitrinaBlock, _resolve_period_date_bindings,
)
from packages.application.web_vitrina_management_history import FACT_GROUPS, FACT_SOURCE
from packages.application.calculation_parameters_v4 import (
    PROXY_V4_BLOCK_KEY, ensure_proxy_v4_schema, load_proxy_v4_parameters_for_date,
)
from apps.fbs_snapshot_cost_smoke import capture
from apps.fbs_inventory_presentation_smoke import retained
from packages.application.fbs_snapshot_cost import initialize_candidate
from packages.application.fbs_inventory_presentation import FbsInventorySnapshot
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application.fbs_accounting_runtime import SCHEMA, _save_book, load as load_book
from packages.application.web_vitrina_window_read_context import (
    WindowReadContextError,
    borrowed_operational_connection,
    window_read_context,
)


def main() -> None:
    with TemporaryDirectory(prefix="vitrina-window-read-context-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        runtime.ingest_bundle(json.loads(BUNDLE_FIXTURE.read_text()), activated_at="2026-04-21T12:00:00Z")
        state = runtime.load_current_state()
        enabled = [item for item in state.config_v2 if item.enabled]
        save_ready_fixture(
            runtime,
            current_state=state,
            refreshed_at="2026-04-20T12:00:00Z",
            plan=_build_plan(
                as_of_date="2026-04-20",
                first_nm_id=enabled[0].nm_id,
                second_nm_id=enabled[1].nm_id,
                first_group=enabled[0].group,
            ),
        )
        with sqlite3.connect(runtime.db_path) as setup:
            setup.execute("PRAGMA journal_mode=WAL")
        book_path = Path(tmp) / "fbs-snapshot-accounting.sqlite3"
        with sqlite3.connect(book_path) as setup:
            setup.execute("PRAGMA journal_mode=WAL")
            setup.execute("CREATE TABLE accounting_revisions(version TEXT PRIMARY KEY,payload TEXT)")
            setup.execute("CREATE TABLE accounting_current(singleton INTEGER PRIMARY KEY,version TEXT)")
            setup.execute("CREATE TABLE accounting_blobs(digest TEXT PRIMARY KEY,payload BLOB)")
            setup.execute("INSERT INTO accounting_current VALUES(1,'v1')")
        policy_path = Path(tmp) / ".auto-updates-policy.json"
        policy_path.write_bytes(b'{"revision":1}')
        with window_read_context(runtime.db_path, runtime_dir=Path(tmp)) as context:
            borrowed = borrowed_operational_connection(runtime.db_path)
            assert borrowed is not None
            book = context.borrow_book(book_path)
            assert book is not None
            if book.execute("SELECT version FROM accounting_current").fetchone()[0] != "v1":
                raise AssertionError("FBS book was not pinned")
            if context.read_file_once(policy_path) != b'{"revision":1}':
                raise AssertionError("policy bytes were not captured")
            before = borrowed.execute(
                "SELECT refreshed_at FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?",
                ("2026-04-20",),
            ).fetchone()[0]
            if before != "2026-04-20T12:00:00Z":
                raise AssertionError(before)
            try:
                borrowed.commit()
            except WindowReadContextError:
                pass
            else:
                raise AssertionError("nested reader committed a pinned snapshot")
            # Both legacy runtime reads must borrow rather than release the pin.
            runtime.load_current_state()
            runtime.load_sheet_vitrina_ready_snapshot(as_of_date="2026-04-20")
            borrowed.close()
            with sqlite3.connect(runtime.db_path) as writer:
                writer.execute(
                    "UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=? WHERE as_of_date=?",
                    ("2026-04-20T13:00:00Z", "2026-04-20"),
                )
            with sqlite3.connect(book_path) as writer:
                writer.execute("UPDATE accounting_current SET version='v2'")
            policy_path.write_bytes(b'{"revision":2}')
            during = borrowed.execute(
                "SELECT refreshed_at FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?",
                ("2026-04-20",),
            ).fetchone()[0]
            if during != before:
                raise AssertionError("nested reader lost its pinned SQLite snapshot")
            if book.execute("SELECT version FROM accounting_current").fetchone()[0] != "v1":
                raise AssertionError("nested FBS reader lost its pinned SQLite snapshot")
            if context.read_file_once(policy_path) != b'{"revision":1}':
                raise AssertionError("policy changed inside pinned window step")
            try:
                borrowed.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='bad'")
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("window read context allowed an SQLite write")
        with sqlite3.connect(runtime.db_path) as reader:
            after = reader.execute(
                "SELECT refreshed_at FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?",
                ("2026-04-20",),
            ).fetchone()[0]
        if after != "2026-04-20T13:00:00Z":
            raise AssertionError(f"writer commit was not visible after read context: {after}")
        book_path.unlink()
        policy_path.unlink()
        OwnProductCapitalBlock(runtime=runtime)
        save_ready_fixture(
            runtime,
            current_state=state,
            refreshed_at="2026-04-21T12:00:00Z",
            plan=_build_plan(
                as_of_date="2026-04-21",
                first_nm_id=enabled[0].nm_id,
                second_nm_id=enabled[1].nm_id,
                first_group=enabled[0].group,
            ),
        )
        extra_connections: list[str] = []
        audit_active = [False]
        def audit(event: str, args: tuple[object, ...]) -> None:
            if audit_active[0] and event == "sqlite3.connect":
                extra_connections.append(str(args[0]))
        sys.addaudithook(audit)
        with window_read_context(runtime.db_path, runtime_dir=Path(tmp)):
            audit_active[0] = True
            contract = SheetVitrinaV1WebVitrinaBlock(
                runtime=runtime,
                now_factory=lambda: datetime(2026, 4, 21, tzinfo=timezone.utc),
            ).build(
                page_route="/sheet-vitrina-v1/vitrina",
                read_route="/v1/sheet-vitrina-v1/web-vitrina",
                date_from="2026-04-20",
                date_to="2026-04-21",
            )
            audit_active[0] = False
        if not contract.rows or extra_connections:
            raise AssertionError(f"window build escaped pinned readers: {extra_connections}")
        with sqlite3.connect(runtime.db_path) as setup:
            ensure_proxy_v4_schema(setup)
        for day in ("2026-10-01", "2026-10-02"):
            save_ready_fixture(
                runtime,
                current_state=state,
                refreshed_at=f"{day}T12:00:00Z",
                plan=_build_plan(
                    as_of_date=day,
                    first_nm_id=enabled[0].nm_id,
                    second_nm_id=enabled[1].nm_id,
                    first_group=enabled[0].group,
                ),
            )
        extra_connections.clear()
        with window_read_context(runtime.db_path, runtime_dir=Path(tmp)):
            if load_proxy_v4_parameters_for_date(runtime=runtime, effective_date="2026-10-02") is not None:
                raise AssertionError("unexpected Proxy V4 parameters before concurrent publish")
            old_covering = runtime.load_sheet_vitrina_ready_snapshot_covering_date_any_bundle(
                column_date="2026-10-02"
            ).snapshot_id
            with sqlite3.connect(runtime.db_path) as writer:
                writer.execute(
                    """INSERT INTO sheet_vitrina_v1_proxy_v4_parameter_versions
                    (version_id,block_key,revision,effective_date,source_window_from,source_window_to,
                     source_window_fingerprint,parameters_json,fingerprint,version_kind,created_by,created_at)
                    VALUES('race-v1',?,1,'2026-10-02','2026-09-01','2026-09-30',
                           'race-source','{}','race-fingerprint','manual','fixture','2026-10-02T12:00:00Z')""",
                    (PROXY_V4_BLOCK_KEY,),
                )
                writer.execute(
                    """UPDATE sheet_vitrina_v1_ready_snapshots
                    SET plan_json=json_set(plan_json,'$.snapshot_id','race-covering-v2'),
                        refreshed_at='2026-10-02T13:00:00Z'
                    WHERE as_of_date='2026-10-02'"""
                )
            audit_active[0] = True
            try:
                contract = SheetVitrinaV1WebVitrinaBlock(
                    runtime=runtime,
                    now_factory=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
                ).build(
                    page_route="/sheet-vitrina-v1/vitrina",
                    read_route="/v1/sheet-vitrina-v1/web-vitrina",
                    date_from="2026-10-01",
                    date_to="2026-10-02",
                )
                if load_proxy_v4_parameters_for_date(runtime=runtime, effective_date="2026-10-02") is not None:
                    raise AssertionError("Proxy V4 resolver escaped pinned snapshot")
                pinned_covering = runtime.load_sheet_vitrina_ready_snapshot_covering_date_any_bundle(
                    column_date="2026-10-02"
                ).snapshot_id
            finally:
                audit_active[0] = False
            if pinned_covering != old_covering:
                raise AssertionError("covering-date read escaped pinned snapshot")
        if not contract.rows or extra_connections:
            raise AssertionError(f"October window escaped pinned readers: {extra_connections}")
        fresh_covering = runtime.load_sheet_vitrina_ready_snapshot_covering_date_any_bundle(
            column_date="2026-10-02"
        ).snapshot_id
        if fresh_covering != "race-covering-v2":
            raise AssertionError("post-transaction covering update was not visible")
        with sqlite3.connect(runtime.db_path) as setup:
            row = setup.execute(
                "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-10-02'"
            ).fetchone()
            covering_plan = json.loads(row[0])
            covering_plan["date_columns"].append("2026-10-03")
            covering_plan["temporal_slots"].append({
                "slot_key": "historical_import", "slot_label": "Historical import",
                "column_date": "2026-10-03",
            })
            data_sheet = next(
                item for item in covering_plan["sheets"] if item["sheet_name"] == "DATA_VITRINA"
            )
            data_sheet["header"].append("2026-10-03")
            for data_row in data_sheet["rows"]:
                data_row.append(None)
            data_sheet["column_count"] = 4
            data_sheet["write_rect"] = "A1:D8"
            covering_plan.setdefault("metadata", {}).setdefault(
                "server_cell_presentation", {}
            )["SKU:fixture|view_count"] = {
                "2026-10-03": {
                    "source": FACT_SOURCE,
                    "source_as_of_date": "2026-10-03",
                    "complete_source_groups": sorted(FACT_GROUPS),
                }
            }
            setup.execute(
                "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date='2026-10-02'",
                (json.dumps(covering_plan),),
            )
        with window_read_context(runtime.db_path, runtime_dir=Path(tmp)):
            first_binding = _resolve_period_date_bindings(
                runtime=runtime, date_from="2026-10-03", date_to="2026-10-03",
                default_visible_snapshot=None,
            )[0]
            if first_binding.missing or first_binding.covering_snapshot is None:
                raise AssertionError(f"fixture did not enter the covering-date branch: {first_binding}")
            with sqlite3.connect(runtime.db_path) as writer:
                writer.execute(
                    """UPDATE sheet_vitrina_v1_ready_snapshots
                    SET plan_json=json_set(plan_json,'$.snapshot_id','covered-race-v3')
                    WHERE as_of_date='2026-10-02'"""
                )
            pinned_binding = _resolve_period_date_bindings(
                runtime=runtime, date_from="2026-10-03", date_to="2026-10-03",
                default_visible_snapshot=None,
            )[0]
            if pinned_binding.covering_snapshot.snapshot_id != first_binding.covering_snapshot.snapshot_id:
                raise AssertionError("covering-date selection escaped pinned operational store")
        fresh_binding = _resolve_period_date_bindings(
            runtime=runtime, date_from="2026-10-03", date_to="2026-10-03",
            default_visible_snapshot=None,
        )[0]
        if fresh_binding.covering_snapshot.snapshot_id != "covered-race-v3":
            raise AssertionError("covering-date writer was not visible after RO transaction")
        day = "2026-10-02"
        source = capture(day, extra=[{
            "nm_id": enabled[1].nm_id, "facility_id": "ff-1", "quantity": "0",
        }])
        source["quantity_snapshot"]["rows"][0]["nm_id"] = enabled[0].nm_id
        source["baseline_costs"]["rows"][0]["nm_id"] = enabled[0].nm_id
        fbs_state = initialize_candidate(source)
        wb = {
            "contract": "shared_sku_cost_wb_source_v1", "business_date": day,
            "complete": True, "authority_complete": True, "version_id": "wb-test",
            "source_digest": "fixture", "rows": [
                {"nm_id": enabled[0].nm_id, "quantity": "500", "capital_rub": "100000",
                 "status": "available", "components": {"physical": 500}},
                {"nm_id": enabled[1].nm_id, "quantity": "0", "capital_rub": "0",
                 "status": "available", "components": {"physical": 0}},
            ],
        }
        presentation = FbsInventorySnapshot(
            fbs_state=fbs_state, wb_capture=wb, retained=retained(wb), day=day,
        )
        active_book = {
            "schema": SCHEMA, "active": True, "effective_date": day,
            "state": fbs_state,
            "shared_days": {day: build_shared_cost_day(fbs_state, wb, day)},
            "wb_days": {day: wb}, "retained_days": {day: retained(wb)},
            "presentations": {day: presentation.payload()},
            "prepared_at": source["captured_at"], "source_digest": source["source_digest"],
        }
        active_version = _save_book(
            runtime.runtime_dir, active_book, expected=None, operation_id="window-active-book",
        )
        with sqlite3.connect(book_path) as setup:
            setup.execute("PRAGMA journal_mode=WAL")
        with window_read_context(runtime.db_path, runtime_dir=Path(tmp)):
            pinned_book, pinned_version = load_book(runtime.runtime_dir)
            if pinned_version != active_version or not pinned_book["active"]:
                raise AssertionError("active FBS book was not bound to window read")
            audit_active[0] = True
            try:
                active_contract = SheetVitrinaV1WebVitrinaBlock(
                    runtime=runtime,
                    now_factory=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
                ).build(
                    page_route="/sheet-vitrina-v1/vitrina",
                    read_route="/v1/sheet-vitrina-v1/web-vitrina",
                    date_from=day,
                    date_to=day,
                )
            finally:
                audit_active[0] = False
            if not active_contract.rows or extra_connections:
                raise AssertionError(f"active FBS build escaped pin: {extra_connections}")
            with sqlite3.connect(book_path) as writer:
                writer.execute("UPDATE accounting_current SET version='temporarily-moved'")
            pinned_again, version_again = load_book(runtime.runtime_dir)
            if version_again != active_version or not pinned_again["active"]:
                raise AssertionError("active FBS book moved during pinned window read")
        with sqlite3.connect(book_path) as writer:
            writer.execute("UPDATE accounting_current SET version=?", (active_version,))
        print("window_read_context: ok -> pinned, query-only, nested context safe")


if __name__ == "__main__":
    main()
