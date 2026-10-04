"""Actual frozen native compiler -> dated objects -> requested range parity."""
from __future__ import annotations

import hashlib
import gc
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from packages.application import sheet_vitrina_v1_inventory_history as history
from packages.application.sheet_vitrina_v1_inventory_planning import extend_rows_with_inventory_planning
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow
from packages.application.inventory_quantity import CONTRACT as INVENTORY_CONTRACT
from packages.application.web_vitrina_history_compiler import (
    NativeDatedCompiler, unpack_table, assert_numeric_compatibility, DatedCompilerUnsupported, digest,
)
from packages.application.web_vitrina_history_frozen_adapter import FrozenNativeAdapter, update_frozen_history
from packages.application.web_vitrina_history_store import HistoryStore
from packages.application.web_vitrina_window_read_context import window_read_context


def capture_inventory(conn, day: str, facility: str, *, revision: str = "original"):
    roster = [{"facility_id": facility, "name": facility, "active": True, "applicable": True}]
    components = []
    for kind, key, nm_id in (("TOTAL", "TOTAL", None), ("SKU", "SKU:999", 999), ("SKU", "SKU:777", 777)):
        for component, identity, quantity in (("WB", "WB", 7), ("FBS_FACILITY", facility, 3)):
            semantic = "wb_physical_stock_qty" if component == "WB" else "fbs_available_qty"
            components.append({
                "scope_kind": kind, "scope_key": key, "nm_id": nm_id,
                "component_kind": component, "component_id": identity,
                "state": "exact", "quantity": quantity,
                "source_revision": revision, "source_watermark": day + "T12:00:00Z",
                "provenance": {"contract": INVENTORY_CONTRACT, "semantic_kind": semantic, "identity": {"name": "HistoricalOnlyName"}
                               if nm_id == 777 else {}},
            })
    capture = history.append_inventory_history_capture(
        conn, business_date=day, capture_kind="historical_backfill",
        formula_version=INVENTORY_CONTRACT, facility_roster=roster,
        source_manifest={"contract": INVENTORY_CONTRACT, "case": "native-history-fixture", "revision": revision},
        components=components, captured_at=day + "T12:00:00Z",
    )
    history.append_inventory_history_finalization(
        conn, business_date=day, capture_id=capture["capture_id"],
        finalization_identity="fixture-" + revision, finalized_at=day + "T12:00:01Z",
        provenance={},
    )


def install_adversarial_source(runtime):
    with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
        history.ensure_inventory_history_schema(conn)
        capture_inventory(conn, "2026-04-15", "old")
        capture_inventory(conn, "2026-04-19", "new")
        row_id, encoded = conn.execute(
            "SELECT rowid,plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-04-15'"
        ).fetchone()
        plan = json.loads(encoded)
        sheet = plan["sheets"][0]
        sheet["rows"].append(["Historical SKU: stock", "SKU:999|stock_total", 17])
        sheet["row_count"] += 1
        plan.setdefault("metadata", {}).setdefault("server_cell_presentation", {})[
            "SKU:999|stock_total"
        ] = {"2026-04-15": {"source": "legacy_fixture", "quality_state": "partial",
                               "quality_reason": "dated legacy source"}}
        conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE rowid=?",
                     (json.dumps(plan), row_id))


def finalize_owned_frozen_fixture(runtime_dir: Path) -> list[dict]:
    """Finalize ONLY this synthetic temp fixture before its frozen byte proof.

    Native fixture entrypoint also creates auxiliary supplier/autoanswers WAL
    databases. Production source policy is untouched; this test owns all files.
    """
    root = runtime_dir.resolve()
    if root.name != "runtime" or not root.parent.name.startswith("sheet-vitrina-web-vitrina-browser-"):
        raise ValueError("not the owned synthetic fixture directory")
    gc.collect()  # Release fixture setup's committed but unclosed SQLite handles.
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        with path.open("rb") as file:
            header = file.read(20)
        if header[:16] != b"SQLite format 3\x00":
            continue
        with closing(sqlite3.connect(path)) as conn:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            assert checkpoint[0] == 0, (path.name, "fixture checkpoint busy")
            mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            assert mode == "delete", (path.name, mode)
        with path.open("rb") as file:
            finalized = file.read(20)
        assert finalized[18:20] == b"\x01\x01", path.name
        assert not any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")), path.name
        files.append({"name": path.name, "before_header_versions": list(header[18:20]),
                      "frozen_header_versions": list(finalized[18:20])})
    return files


def check_ranges(compiler, store, now):
    comparisons = []
    for count in (1, 3, 14, 31, 180):
        start = (now.date() - timedelta(days=count - 1)).isoformat()
        # Independently use the native range's exact row-set, never common keys.
        natural = compiler._table(compiler.natural_block, start, now.date().isoformat())
        expected_members = {row["row_id"] for row in natural["rows"]}
        # Canonical identity/order is explicit; requested membership remains independently native.
        expected = compiler._table(compiler.block, start, now.date().isoformat(), members=expected_members)
        expected_catalog, expected_cells = unpack_table(expected)
        got = {}
        for scope in ("summary", "sku"):
            read = store.read(date_from=start, date_to=now.date().isoformat(), scope=scope, limit=512)
            got.update({row["row_id"]: row for row in read["rows"]})
        assert set(got) == expected_members, (count, set(got) ^ expected_members)
        for row_id, row in got.items():
            actual_static = {key: value for key, value in row.items()
                             if key not in {"cells", "row_order", "search_text"}}
            assert actual_static == expected_catalog["rows"][row_id], (count, row_id, "static fields")
            for day, cell in row["cells"].items():
                assert cell == expected_cells[row_id][day], (count, row_id, day)
        for native_row in expected["rows"]:
            assert got[native_row["row_id"]]["search_text"] == native_row["search_text"], (
                count, native_row["row_id"])
        comparisons.append({"days": count, "exact_rows": len(got), "cells": len(got) * count, "all16": True})
    return comparisons


def check_numeric_loss_guard(store):
    day = "2026-04-01"
    row_id = "TOTAL|total_inventory_wb_total_qty_v1"
    row = WebVitrinaContractRow(
        row_id=row_id, row_order=1, scope_kind="TOTAL", scope_key="TOTAL", scope_label="ИТОГО",
        metric_key="total_inventory_wb_total_qty_v1", metric_label="WB", row_last_updated_at="",
        section="Stock", group=None, nm_id=None, format="number", values_by_date={day: 42},
    )
    arguments = {"planning": {}, "history": {}, "date_columns": [day], "enabled_config": []}
    natural = extend_rows_with_inventory_planning([row], **arguments)
    canonical = extend_rows_with_inventory_planning([row], legacy_wb_history_present=True, **arguments)
    natural_value = next(r for r in natural if r.row_id == row_id).values_by_date[day]
    canonical_value = next(r for r in canonical if r.row_id == row_id).values_by_date[day]
    assert natural_value == 42 and canonical_value == ""
    current = store.edition()
    vector = json.loads(json.dumps(current["consumed"]))
    from packages.application.web_vitrina_history_compiler import digest
    vector["dates"][day] = digest("unsupported native numeric loss")
    catalog = json.loads((store.root / "catalogs" / (current["catalog"] + ".json")).read_text())
    def unsupported(_day):
        assert_numeric_compatibility({row_id: [canonical_value, str(canonical_value)]},
                                     {row_id: [natural_value, str(natural_value)]}, day)
    try:
        store.update(vector=vector, catalog=catalog, compile_day=unsupported, revalidate=lambda: vector)
    except DatedCompilerUnsupported as error:
        assert "numeric_context_mismatch" in str(error)
    else:
        raise AssertionError("unsupported numeric loss published")
    assert store.edition() == current


def check_cached_presentation_stability(compiler):
    """A compiler-chosen day and observed clock must not dirty structural metadata."""
    table = compiler._table(compiler.block, "2026-04-20", "2026-04-20")
    original, _ = unpack_table(table)
    changed = json.loads(json.dumps(table, ensure_ascii=False).replace("2026-04-20", "2026-04-21"))
    changed.setdefault("meta", {}).update(generated_at="2099-01-01T00:00:00Z",
                                         snapshot_id="new-observation", row_count=999999)
    for row in changed["rows"]:
        for cell in row["values"]:
            if changed["columns"][cell[0]]["id"].startswith("date:"):
                cell[1] = 123456
                cell[2] = "123456"
    updated, _ = unpack_table(changed)
    assert digest(original) == digest(updated), "presentation contains day/clock/value volatility"
    cached = {key: original[key] for key in ("presentation", "date_column_template", "row_order_cell_template")}
    text = json.dumps(cached, ensure_ascii=False)
    assert "2026-04-20" not in text and "generated_at" not in text and "snapshot_id" not in text
    # Same stable catalog plus exactly one changed dated token reuses all other day objects.
    with tempfile.TemporaryDirectory(prefix="history-presentation-stability-") as directory:
        local = HistoryStore(Path(directory))
        catalog = {**original, "order": [row["row_id"] for row in table["rows"]], "context_epoch": "fixture"}
        _, cells = unpack_table(table)
        days = ["2026-04-19", "2026-04-20"]
        units = {day: {"contract": "web_vitrina_dated_history_v1", "date": day,
            "context_epoch": "fixture", "accepted_ready_available": True,
            "members": catalog["order"], "cells": {rid: values["2026-04-20"] for rid, values in cells.items()}} for day in days}
        # Use the existing compiler contract rather than any source revision claim.
        from packages.application.web_vitrina_history_compiler import CONTRACT
        for unit in units.values(): unit["contract"] = CONTRACT
        vector = {"coverage": "complete_frozen_native_v1", "epoch": "fixture", "dates": {d: digest(units[d]) for d in days}}
        local.update(vector=vector, catalog=catalog, compile_day=units.__getitem__, revalidate=lambda: vector)
        old = local.edition()
        unchanged = local.update(vector=vector, catalog=catalog,
            compile_day=lambda _: (_ for _ in ()).throw(AssertionError("unchanged compiled")), revalidate=lambda: vector)
        assert unchanged["status"] == "unchanged" and unchanged["recomputes"] == 0
        units[days[-1]] = json.loads(json.dumps(units[days[-1]]))
        row = catalog["order"][0]
        units[days[-1]]["cells"][row][0:2] = [123456, "123456"]
        vector["dates"][days[-1]] = digest(units[days[-1]])
        changed_result = local.update(vector=vector, catalog=catalog, compile_day=units.__getitem__, revalidate=lambda: vector)
        assert changed_result["status"] == "published" and changed_result["recomputes"] == 1
        assert local.edition()["days"][days[0]] == old["days"][days[0]]
    return {"stable_catalog_on_clock_day_values": True, "unchanged_recomputes": 0,
            "single_dated_change_recomputes": 1, "source_adapter_invalidation_not_claimed": True}


def main():
    now = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=31, now=now)
    with fixture, tempfile.TemporaryDirectory(prefix="vitrina-dated-history-") as store_dir:
        runtime = fixture.entrypoint.runtime
        install_adversarial_source(runtime)
        frozen_fixture = finalize_owned_frozen_fixture(runtime.runtime_dir)
        before = hashlib.sha256(runtime.db_path.read_bytes()).hexdigest()
        adapter = FrozenNativeAdapter(
            db_path=runtime.db_path, frozen_root=fixture.runtime_dir.parent, files=[], now=now,
            date_from="2025-10-23", date_to="2026-04-20", formula_epoch="native-canonical-inventory-context-v1",
        )
        vector = adapter.capture()
        store = HistoryStore(Path(store_dir), max_rows=512, max_reply_bytes=32 * 1024**2)
        with window_read_context(runtime.db_path, runtime_dir=runtime.runtime_dir):
            compiler = NativeDatedCompiler(runtime, now, adapter.days[0], adapter.days[-1],
                                            dependency_epoch=vector["epoch"])
            presentation_stability = check_cached_presentation_stability(compiler)
            result = store.update(vector=vector, catalog=compiler.catalog, compile_day=compiler.compile,
                                  revalidate=adapter.capture)
            assert result["status"] == "published", result
            comparisons = check_ranges(compiler, store, now)
            historic = store.read(date_from="2026-04-15", date_to="2026-04-15", scope="sku",
                                  row_ids=["SKU:777|inventory_wb_total_qty_v1"], limit=512)
            assert historic["rows"][0]["values"]["scope_label"][0] == "HistoricalOnlyName"
            assert "HistoricalOnlyName" in historic["rows"][0]["search_text"]
            old_cell = compiler.compile("2026-04-15")["cells"]["TOTAL|total_view_count"]
        assert before == hashlib.sha256(runtime.db_path.read_bytes()).hexdigest()
        no_change = update_frozen_history(adapter=adapter, runtime=runtime, store=store)
        assert no_change["status"] == "unchanged" and not no_change["compiler_constructed"]
        family_before = {str(p): p.stat().st_size for p in store.root.rglob("*") if p.is_file()}
        store.read(date_from="2026-04-07", date_to="2026-04-20", scope="summary")
        assert family_before == {str(p): p.stat().st_size for p in store.root.rglob("*") if p.is_file()}

        # Semantic-only correction uses actual capture/finalization producer writes.
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            capture_inventory(conn, "2026-04-15", "old", revision="corrected-same-quantity")
            row_id, encoded = conn.execute(
                "SELECT rowid,plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-04-15'"
            ).fetchone()
            plan = json.loads(encoded)
            plan.setdefault("metadata", {}).setdefault("server_cell_presentation", {})[
                "TOTAL|total_view_count"
            ] = {"2026-04-15": {"quality_state": "partial", "quality_reason": "corrected source proof"}}
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE rowid=?",
                         (json.dumps(plan), row_id))
        corrected = adapter.capture()
        assert corrected["epoch"] != vector["epoch"]  # unknown exact impact -> conservative epoch
        with window_read_context(runtime.db_path, runtime_dir=runtime.runtime_dir):
            compiler = NativeDatedCompiler(runtime, now, adapter.days[0], adapter.days[-1],
                                            existing_catalog=compiler.catalog,
                                            dependency_epoch=corrected["epoch"])
            new_cell = compiler.compile("2026-04-15")["cells"]["TOTAL|total_view_count"]
        assert old_cell[0] == new_cell[0] and old_cell[10] != new_cell[10] and new_cell[10] == "corrected source proof", (old_cell, new_cell)
        pending = update_frozen_history(adapter=adapter, runtime=runtime, store=store, max_recomputes=2)
        assert pending["status"] == "pending" and store._current()["current"] == result["edition_id"]
        assert store.edition()["consumed"] == vector  # correction not silently consumed

        # Current catalog-only source correction cannot reuse previous static labels.
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute("UPDATE registry_upload_config_v2 SET display_name=display_name || ' corrected'")
        renamed = adapter.capture()
        assert renamed["epoch"] != corrected["epoch"]
        with window_read_context(runtime.db_path, runtime_dir=runtime.runtime_dir):
            renamed_compiler = NativeDatedCompiler(runtime, now, adapter.days[0], adapter.days[-1],
                                                   existing_catalog=compiler.catalog,
                                                   dependency_epoch=renamed["epoch"])
        assert renamed_compiler.catalog is not compiler.catalog
        assert any("corrected" in row["values"].get("scope_label", [""])[0]
                   for row in renamed_compiler.catalog["rows"].values())

        # Native ready semantic update/delete/re-date on an OLD date, content proof.
        for statement in (
            "UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='corrected-proof' WHERE as_of_date='2026-03-21'",
            "UPDATE sheet_vitrina_v1_ready_snapshots SET as_of_date='2026-03-20' WHERE as_of_date='2026-03-21'",
            "DELETE FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-03-20'",
        ):
            previous = adapter.capture()
            with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
                assert conn.execute(statement).rowcount == 1
            assert adapter.capture() != previous

        # A default template outside a historical requested range is a dependency.
        historical = FrozenNativeAdapter(
            db_path=runtime.db_path, frozen_root=fixture.runtime_dir.parent, files=[], now=now,
            date_from="2026-03-22", date_to="2026-03-24", formula_epoch="test",
        )
        previous = historical.capture()
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            row_id, encoded = conn.execute(
                "SELECT rowid,plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-04-20'"
            ).fetchone()
            plan = json.loads(encoded)
            plan["sheets"][0]["rows"][0][0] += " label correction"
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE rowid=?",
                         (json.dumps(plan), row_id))
        assert historical.capture()["epoch"] != previous["epoch"]

        check_numeric_loss_guard(store)

        # Actual business TZ rollover: UTC date unchanged, D-6 maturity advances.
        adapter.now = datetime(2026, 4, 20, 18, 59, tzinfo=timezone.utc)
        before_rollover = adapter.capture()
        adapter.now = datetime(2026, 4, 20, 19, 1, tzinfo=timezone.utc)
        assert adapter.capture()["epoch"] != before_rollover["epoch"]
        (runtime.runtime_dir / "new-side-input.json").write_text("{}")
        assert adapter.capture() != before_rollover
        print(json.dumps({"status": "pass", "comparisons": comparisons, "no_change": no_change,
                          "presentation_stability": presentation_stability,
                          "frozen_owned_fixture": frozen_fixture,
                          "native_semantic_correction": True, "pending_unconsumed": True,
                          "config_and_outside_range_catalog": True, "retro_update_delete_redate": True,
                          "business_timezone_rollover": True, "numeric_loss_fails_closed": True, "operational_read_unchanged": True,
                          "read_creates_no_files": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
