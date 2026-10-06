"""Owned mixed-context day fixtures; no production/native throughput claim."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import time
import sqlite3
from contextlib import closing
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_history_store_smoke import setup_units, expect_error
from packages.application.web_vitrina_history_store import HistoryStore, HistoryUnavailable, _read
from packages.application.web_vitrina_history_compiler import digest, dates_between
from packages.application.web_vitrina_history_http_read import read_history_page
from packages.business_time import current_business_date_iso


def fixture():
    catalog, _ = setup_units()
    catalog["date_column_template"] = {"id": "date:{date}", "header": "{date}"}
    catalog["row_order_cell_template"] = [1, "1"] + [""] * 14
    catalog["presentation"] = {"renderers": []}
    old = catalog["rows"].pop("TOTAL|stock")
    catalog["rows"] = {}
    for rid, kind in (("TOTAL|stock", "total"), ("SKU:7|stock", "sku")):
        row = deepcopy(old)
        row.update(row_id=rid, row_kind=kind)
        row["values"]["metric_key"] = ["stock", "stock"] + [""] * 14
        row["values"]["nm_id"] = [7, "7"] + [""] * 14 if kind == "sku" else [None, ""] + [""] * 14
        catalog["rows"][rid] = row
    catalog["columns"].append({"id": "metric_key"})
    catalog["order"] = list(catalog["rows"])
    return catalog


def unit(catalog, day, available=True):
    # Preserve every field, not just the numeric/display operands.
    cells = {rid: [len(day) + i, str(len(day) + i), "number", "number_default",
        "renderer:number", "normal", "neutral", day, "exact", "Exact", rid,
        "complete", None, "quantity", day + "T00:00:00Z", digest([rid, day])]
        for i, rid in enumerate(catalog["order"])}
    if not available:
        for cell in cells.values():
            cell[0:2] = [None, "—"]
            cell[8:12] = ["missing", "Missing", "No accepted observation", "missing"]
    return {"contract": catalog["contract"], "date": day,
        "context_epoch": catalog["context_epoch"], "accepted_ready_available": available,
        "members": catalog["order"] if available else [], "cells": cells}


def run(root):
    store = HistoryStore(root)
    old_catalog = fixture()
    old_days = dates_between("2026-09-01", "2026-10-19")
    old_units = {day: unit(old_catalog, day) for day in old_days}
    old_vector = {"coverage": "complete_frozen_native_v1", "epoch": "original-formula",
                  "dates": {day: digest(["source", day]) for day in old_days}}
    result = store.update(vector=old_vector, catalog=old_catalog,
        compile_day=old_units.__getitem__, revalidate=lambda: old_vector)
    old_id = result["edition_id"]
    old_edition = store.edition()
    old_refs = dict(old_edition["days"])
    old_proofs = store.day_proofs(old_edition)
    fresh = deepcopy(old_catalog)
    fresh["context_epoch"] = "new-catalog-context"
    for rid, kind in (("TOTAL|rating", "total"), ("SKU:7|rating", "sku")):
        row = deepcopy(fresh["rows"]["TOTAL|stock" if kind == "total" else "SKU:7|stock"])
        row["row_id"] = rid
        row["values"]["metric_key"] = ["rating", "rating"] + [""] * 14
        row["values"]["metric_label"] = ["Rating", "Rating"] + [""] * 14
        fresh["rows"][rid] = row
        fresh["order"].append(rid)
    days = dates_between(old_days[0], "2026-10-20")
    vector = {**old_vector, "epoch": "reviewed-new-formula",
              "dates": {day: digest(["source", day]) for day in days}}
    calls = []
    def compile_day(day):
        calls.append(day)
        return unit(fresh, day)
    def update(**kwargs):
        return store.update(vector=vector, catalog=fresh, compile_day=compile_day,
            revalidate=lambda: vector, business_date="2026-10-20",
            metric_start_dates={"rating": "2026-10-10"}, **kwargs)
    first = update(max_recomputes=3)
    assert first["status"] == "pending" and first["recomputes"] == 3
    assert store._current()["current"] == old_id
    pending = _read(root / "PENDING.json")
    assert pending["refs"]["2026-09-01"] == old_refs["2026-09-01"]
    store._collect(pin_ttl_seconds=0)
    assert (root / "catalogs" / (old_edition["catalog"] + ".json")).is_file()
    finished = update(max_recomputes=14)
    assert finished["status"] == "published" and finished["recomputes"] == 11
    assert calls == dates_between("2026-10-07", "2026-10-20")
    current = store.edition()
    for day in dates_between(old_days[0], "2026-10-06"):
        assert current["days"][day] == old_refs[day]
        assert current["day_catalogs"][day] == old_edition["catalog"]
        assert current["day_proofs"][day] == old_proofs[day]
    assert set(old_days) <= set(current["days"])
    assert "2026-09-01" not in current["consumed"]["dates"]
    # Existing archived cells keep all16 original fields across the mixed range.
    page = store.read(date_from="2026-09-01", date_to="2026-10-20", scope="summary")
    stock = next(row for row in page["rows"] if row["row_id"] == "TOTAL|stock")
    assert stock["cells"]["2026-09-01"] == old_units["2026-09-01"]["cells"]["TOTAL|stock"]
    rating = next(row for row in page["rows"] if row["row_id"] == "TOTAL|rating")
    assert rating["cells"]["2026-09-01"][0] is None and rating["cells"]["2026-09-01"][8] == "not_tracked"
    # A declared later start may not erase a saved earlier observation.
    for day in ("2026-10-07", "2026-10-09"):
        assert rating["cells"][day] == unit(fresh, day)["cells"]["TOTAL|rating"]
    assert rating["cells"]["2026-10-10"][0] is not None
    edition = finished["edition_id"]
    summary = read_history_page(store, date_from="2026-09-01", date_to="2026-10-20")
    sku = read_history_page(store, date_from="2026-09-01", date_to="2026-10-20",
                            scope="sku", edition_id=edition, limit=1)
    assert summary["history_snapshot"]["edition_id"] == sku["history_snapshot"]["edition_id"] == edition
    assert sku["history_snapshot"]["next_offset"] == 1
    sku2 = read_history_page(store, date_from="2026-09-01", date_to="2026-10-20",
                             scope="sku", edition_id=edition, offset=1, limit=1)
    assert sku2["history_snapshot"]["edition_id"] == edition
    calls.clear()
    assert update()["status"] == "unchanged" and calls == []
    original_archive = deepcopy(store.edition())
    declaration = store.update(vector=vector, catalog=fresh, compile_day=compile_day,
        revalidate=lambda: vector, business_date="2026-10-20",
        metric_start_dates={"stock": "2026-10-10"})
    assert declaration["status"] == "published" and declaration["recomputes"] == 0 and calls == []
    assert store.edition()["days"] == original_archive["days"]
    assert store.edition()["day_proofs"] == original_archive["day_proofs"]
    archived = store.read(date_from="2026-09-01", date_to="2026-09-01")["rows"][0]
    assert archived["cells"]["2026-09-01"] == old_units["2026-09-01"]["cells"]["TOTAL|stock"]
    assert archived["cells"]["2026-09-01"][0] == 10
    # Known archived mutation is reported, while the fresh day still publishes.
    vector["dates"]["2026-09-03"] = digest("old-date correction")
    vector["dates"]["2026-10-20"] = digest("fresh correction")
    result = update()
    assert calls == ["2026-10-20"] and result["backfill_required"] == ["2026-09-03"]
    assert store.edition()["days"]["2026-09-03"] == old_refs["2026-09-03"]
    assert update()["backfill_required"] == ["2026-09-03"]
    # A new global context still schedules only14 and keeps the archive warning.
    calls.clear()
    vector["epoch"] = "another-reviewed-formula"
    another = deepcopy(fresh)
    another["context_epoch"] = "third-day-context"
    fresh = another
    result = update()
    assert calls == dates_between("2026-10-07", "2026-10-20")
    assert result["archive_not_reevaluated"] and result["backfill_required"] == ["2026-09-03"]
    assert store.edition()["days"]["2026-09-01"] == old_refs["2026-09-01"]
    # Only an explicit backfill boundary grants archive compute.
    calls.clear()
    result = update(backfill_dates=["2026-09-03"])
    assert calls == ["2026-09-03"] and result["backfill_required"] == []
    assert store.edition()["day_proofs"]["2026-09-03"]["token"] == vector["dates"]["2026-09-03"]
    # Midnight rollover is based on canonical business time, not latest READY.
    before = datetime(2026, 10, 20, 18, 59, tzinfo=timezone.utc)
    after = datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc)
    assert current_business_date_iso(before) == "2026-10-20"
    today = current_business_date_iso(after)
    assert today == "2026-10-21"
    vector["dates"][today] = digest("no accepted current ready")
    vector["dates"]["2026-10-20"] = digest("previous-day clock change")
    calls.clear()
    def rollover_compile(day):
        calls.append(day)
        return unit(fresh, day, available=day != today)
    roll = store.update(vector=vector, catalog=fresh, compile_day=rollover_compile,
        revalidate=lambda: vector, business_date=today)
    assert roll["window_from"] == "2026-10-08" and calls == ["2026-10-20", today]
    current_page = store.read(date_from=today, date_to=today)
    assert current_page["availability"] == {today: False} and current_page["rows"] == []
    vector["dates"]["2026-10-22"] = digest("future")
    expect_error(lambda: store.update(vector=vector, catalog=fresh, compile_day=compile_day,
        revalidate=lambda: vector, business_date=today), HistoryUnavailable, "future")
    del vector["dates"]["2026-10-22"]
    # An expired budget cannot replace last-good; complete refs remain resumable.
    before_id = store._current()["current"]
    vector["dates"]["2026-10-18"] = digest("deadline source")
    late = store.update(vector=vector, catalog=fresh, compile_day=compile_day,
        revalidate=lambda: vector, business_date=today, deadline_monotonic=time.monotonic() - 1)
    assert late["status"] == "pending" and store._current()["current"] == before_id
    # Superseded source fence cannot publish partial/current data.
    before_id = store._current()["current"]
    vector["dates"]["2026-10-19"] = digest("new source")
    result = store.update(vector=vector, catalog=fresh, compile_day=compile_day,
        revalidate=lambda: {**vector, "publication_fence": "changed"}, business_date=today)
    assert result["status"] == "superseded" and store._current()["current"] == before_id
    result = store.update(vector=vector, catalog=fresh, compile_day=compile_day,
        revalidate=lambda: vector, business_date=today)
    assert result["status"] == "published" and result["recomputes"] == 0
    store._collect(pin_ttl_seconds=0)
    # GC must keep original catalogs for archives even after previous editions expire.
    assert (root / "catalogs" / (old_edition["catalog"] + ".json")).is_file()
    assert store.read(date_from="2026-09-01", date_to="2026-09-01")["rows"][0]["cells"]["2026-09-01"] == old_units["2026-09-01"]["cells"]["TOTAL|stock"]
    # Removed current SKU rows remain readable in the archive union, without
    # requiring those retired rows in a newly compiled day object.
    retired = deepcopy(fresh)
    retired["rows"].pop("SKU:7|stock")
    retired["order"].remove("SKU:7|stock")
    retired["context_epoch"] = "retired-native-context"
    vector["epoch"] = "retired-formula-context"
    result = store.update(vector=vector, catalog=retired, compile_day=lambda day: unit(retired, day),
        revalidate=lambda: vector, business_date=today)
    assert result["status"] == "published" and result["recomputes"] == 14
    old_sku = store.read(date_from="2026-09-01", date_to=today, scope="sku")
    old_stock = next(row for row in old_sku["rows"] if row["row_id"] == "SKU:7|stock")
    assert old_stock["cells"]["2026-09-01"] == old_units["2026-09-01"]["cells"]["SKU:7|stock"]
    assert old_stock["cells"][today][0] is None
    incompatible = deepcopy(fresh)
    incompatible["rows"]["TOTAL|stock"]["row_kind"] = "sku"
    vector["epoch"] = "incompatible"
    expect_error(lambda: store.update(vector=vector, catalog=incompatible, compile_day=compile_day,
        revalidate=lambda: vector, business_date=today), HistoryUnavailable, "identity_incompatible")
    return {"status": "PASS", "migration_recomputes": 14, "preserved_archive_days": 36,
            "rolling_window_days": 14, "native_compute": "fixture callbacks only"}


def metadata_status(root):
    store = HistoryStore(root)
    catalog = fixture()
    days = dates_between("2026-09-01", "2026-10-20")
    vector = {"coverage": "complete_frozen_native_v1", "epoch": "same-formula",
              "dates": {day: digest(day) for day in days}}
    store.update(vector=vector, catalog=catalog, compile_day=lambda day: unit(catalog, day),
                 revalidate=lambda: vector)
    before = store.edition()
    vector["dates"]["2026-09-02"] = digest("archive correction")
    def must_not_compile(day):
        raise AssertionError("archive-only status must not compile")
    result = store.update(vector=vector, catalog=catalog, compile_day=must_not_compile,
        revalidate=lambda: vector, business_date="2026-10-20")
    assert result["status"] == "published" and result["metadata_only"] and result["recomputes"] == 0
    after = store.edition()
    assert after["days"] == before["days"] and after["catalog"] == before["catalog"]
    assert after["consumed"] == before["consumed"]
    assert store.day_proofs(after) == store.day_proofs(before)
    assert store.read(date_from="2026-09-02", date_to="2026-09-02")["archive_status"]["backfill_required"] == ["2026-09-02"]
    unchanged = store.update(vector=vector, catalog=catalog, compile_day=must_not_compile,
        revalidate=lambda: (_ for _ in ()).throw(AssertionError("unchanged must not publish")),
        business_date="2026-10-20")
    assert unchanged["status"] == "unchanged" and unchanged["edition_id"] == result["edition_id"]
    vector["dates"]["2026-09-03"] = digest("another archive correction")
    stable_id = store._current()["current"]
    def status_update(**kwargs):
        return store.update_rolling_status(vector=vector, business_date="2026-10-20",
            expected_base=stable_id, **kwargs)
    superseded = status_update(revalidate=lambda: {**vector, "publication_fence": "changed"})
    assert superseded["status"] == "superseded" and store._current()["current"] == stable_id
    expired = status_update(revalidate=lambda: vector, deadline_monotonic=time.monotonic() - 1)
    assert expired["status"] == "pending" and store._current()["current"] == stable_id
    stale = store.update_rolling_status(vector=vector, business_date="2026-10-20",
        expected_base="0" * 64, revalidate=lambda: vector)
    assert stale["status"] == "superseded" and store._current()["current"] == stable_id
    # An explicit backfill clears the corresponding durable reader status.
    calls = []
    def backfill(day):
        calls.append(day)
        return unit(catalog, day)
    fixed = store.update(vector=vector, catalog=catalog, compile_day=backfill,
        revalidate=lambda: vector, business_date="2026-10-20",
        backfill_dates=["2026-09-02", "2026-09-03"])
    assert fixed["status"] == "published" and calls == ["2026-09-02", "2026-09-03"]
    assert store.read(date_from="2026-09-02", date_to="2026-09-03")["archive_status"]["backfill_required"] == []
    return {"archive_only_metadata_published": True, "dayrefs_proofs_preserved": True,
            "fence_CAS_deadline_retains_lastgood": True, "explicit_backfill_clears_reader_status": True}


def native_bridge(root):
    from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
    from packages.application.ready_publication import ensure_publication_schema
    from packages.application.web_vitrina_window_read_context import window_read_context
    from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter, update_live_history
    from packages.application.web_vitrina_history_compiler import NativeDatedCompiler
    root.mkdir()
    now = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)
    fixture_server = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=3, now=now)
    with fixture_server:
        runtime = fixture_server.entrypoint.runtime
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            ensure_publication_schema(conn)
        with closing(sqlite3.connect(runtime.db_path)) as keeper:
            keeper.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            adapter = LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir,
                cache_dir=root / "proofs", now=now, date_from="2026-04-18", date_to="2026-04-20",
                formula_epoch="native-rolling14-fixture")
            vector = adapter.capture()
            with window_read_context(runtime.db_path, runtime_dir=runtime.runtime_dir):
                adapter.capture()
                compiler = NativeDatedCompiler(runtime, now, adapter.days[0], adapter.days[-1],
                    prepared_context=adapter.context, prepared_availability=adapter.availability,
                    lifecycle_quality_resolver=adapter.lifecycle_quality_resolver)
                template = compiler.compile("2026-04-20")
                catalog = compiler.catalog
                adapter.finish_quality_portion()
            store = HistoryStore(root / "history")
            # Synthetic archive with an original context; the recent updates below
            # really go through source capture + native compiler + fresh fence.
            baseline_catalog = deepcopy(catalog)
            baseline_catalog["context_epoch"] = "original-archive-context"
            baseline_days = dates_between("2026-04-01", "2026-04-20")
            baseline = {"coverage": "complete_frozen_native_v1", "epoch": "original-archive",
                        "dates": {day: digest(["owned baseline", day]) for day in baseline_days}}
            def baseline_unit(day):
                return {**deepcopy(template), "date": day, "context_epoch": baseline_catalog["context_epoch"]}
            store.update(vector=baseline, catalog=baseline_catalog, compile_day=baseline_unit,
                         revalidate=lambda: baseline)
            before = store.edition()
            compile_original = NativeDatedCompiler.compile
            calls = []
            def counted(compiler, day):
                calls.append(day)
                return compile_original(compiler, day)
            with patch.object(NativeDatedCompiler, "compile", counted):
                result = update_live_history(adapter=adapter, runtime=runtime, store=store,
                    rolling14=True, deadline_monotonic=time.monotonic() + 30)
            assert result["status"] == "published" and calls == adapter.days, result
            after = store.edition()
            assert all(after["days"][day] == before["days"][day] for day in baseline_days[:-3])
            assert result["archive_not_reevaluated"]
            with patch("packages.application.web_vitrina_history_live_adapter.NativeDatedCompiler",
                       side_effect=AssertionError("nochange must not construct compiler")):
                stable = update_live_history(adapter=adapter, runtime=runtime, store=store, rolling14=True)
            assert stable["status"] == "unchanged" and not stable["compiler_constructed"]
            assert stable["edition_id"] == result["edition_id"]
            # Adapter control-flow: inject one owned archived proof into real
            # captures, while the actual native recent source remains unchanged.
            capture = adapter.capture
            correction = {"2026-04-01": digest("owned archive correction")}
            def captured():
                return {**capture(), "dates": {**vector["dates"], **correction}}
            unchanged_refs = deepcopy(store.edition()["days"])
            with patch.object(adapter, "capture", side_effect=captured), \
                    patch("packages.application.web_vitrina_history_live_adapter.NativeDatedCompiler",
                          side_effect=AssertionError("metadata update constructed compiler")):
                status = update_live_history(adapter=adapter, runtime=runtime, store=store, rolling14=True)
            assert status["status"] == "published" and status["metadata_only"]
            assert not status["compiler_constructed"] and status["recomputes"] == 0
            assert store.edition()["days"] == unchanged_refs
            assert store.read(date_from="2026-04-01", date_to="2026-04-01")["archive_status"]["backfill_required"] == ["2026-04-01"]
            return {"native_recomputes": len(calls), "nochange_compiler_constructed": False,
                    "adapter_metadata_without_compiler": True,
                    "archive_refs_preserved": len(baseline_days) - len(calls)}


def cli_worker(root):
    (root/"native").mkdir(parents=True)
    from apps import web_vitrina_history_candidate_build as command
    args = SimpleNamespace(runtime_dir=root / "native", candidate_root=root / "derived",
        captured_now="2026-10-20T19:00:00+00:00", date_to="business-today",
        date_from="2026-09-01", backfill_from="2026-09-03", backfill_to="2026-09-04",
        formula_epoch="fixture", budget_seconds=180, max_recomputes=31,
        manual=True, worker=True, runtime_contract=None)
    from contextlib import contextmanager
    from packages.application import owned_history_worker as delegation
    captured={}
    args.worker=False; args.maintenance_window_id=''; args.runtime_contract=root/'contract.json'
    @contextmanager
    def fixed_worker(*, runtime, config):
        def complete(now, **kwargs):
            captured.update(config=config,kwargs=kwargs)
            return {'status':'fixture_only'}
        yield SimpleNamespace(complete=complete)
    with patch.object(command, "StoreRegistry"), patch.object(command, "RegistryUploadDbBackedRuntime"), \
         patch.object(command,'runtime_storage_admission'), \
         patch.object(delegation,'standalone_history_worker',fixed_worker),redirect_stdout(StringIO()):
        command.run_admitted(args)
    assert captured['kwargs']['source_range']==('2026-09-01','2026-10-21')
    assert captured['kwargs']['backfill_dates']==('2026-09-03','2026-09-04')
    assert captured['kwargs']['max_portions']==1 and captured['config'].max_recomputes==31
    args.backfill_to = None
    expect_error(lambda: command.run_admitted(args), ValueError, "both explicit")
    return {"rolling14_default": True, "explicit_backfill_propagated": True,
            "canonical_business_today": "2026-10-21"}


if __name__ == "__main__":
    with TemporaryDirectory(prefix="history-rolling14-") as directory:
        result = run(Path(directory) / "state")
        result["metadata_status"] = metadata_status(Path(directory) / "metadata")
        result["native_bridge"] = native_bridge(Path(directory) / "native")
        result["cli"] = cli_worker(Path(directory) / "cli")
        print(json.dumps(result))
