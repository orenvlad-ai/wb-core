"""Narrow framework failure tests; callbacks here are NOT source adapter proof."""
from __future__ import annotations

from copy import deepcopy
import json
import os
import secrets
import sqlite3
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.web_vitrina_history_compiler import CONTRACT, digest
from packages.application.web_vitrina_history_store import HistoryStore, HistoryUnavailable
from packages.application.web_vitrina_history_frozen_adapter import FrozenNativeAdapter
from datetime import datetime, timezone


DAYS = ["2026-04-18", "2026-04-19", "2026-04-20"]
ROW = "TOTAL|stock"


def setup_units():
    values = {key: [value, str(value)] + [""] * 14 for key, value in (
        ("scope_label", "ИТОГО"), ("metric_label", "Stock"), ("group", ""),
        ("section", "Stock"), ("nm_id", ""))}
    catalog = {"contract": CONTRACT, "context_epoch": "catalog-epoch", "order": [ROW],
               "columns": [{"id": "row_order"}] + [{"id": key} for key in values],
               "rows": {ROW: {"row_id": ROW, "row_kind": "total", "section_id": "stock",
                              "group_id": "all", "values": values}}}
    units = {}
    for index, day in enumerate(DAYS):
        cell = [index, str(index), "number", "number_default", "renderer:number"] + [""] * 7
        cell += [None, "", "", ""]
        units[day] = {"contract": CONTRACT, "date": day, "context_epoch": "catalog-epoch",
                      "accepted_ready_available": True, "members": [ROW], "cells": {ROW: cell}}
    return catalog, units


def vector_for(units):
    return {"coverage": "complete_frozen_native_v1", "epoch": "frozen-epoch",
            "dates": {day: digest(unit) for day, unit in units.items()}}


def expect_error(function, exception, reason=None):
    try:
        function()
    except exception as error:
        if reason is not None:
            assert reason in str(error), str(error)
    else:
        raise AssertionError("expected failure")


def check_closed_wal_refusal():
    with tempfile.TemporaryDirectory(prefix="history-frozen-wal-") as directory:
        root = Path(directory)
        db = root / "native.sqlite3"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE source(value TEXT)")
        arguments = {"db_path": db, "frozen_root": root, "files": [],
                     "now": datetime(2026, 4, 20, tzinfo=timezone.utc),
                     "date_from": DAYS[0], "date_to": DAYS[-1], "formula_epoch": "fixture"}
        adapter = FrozenNativeAdapter(**arguments)
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        # Context managers commit but leave connection open; explicitly close it.
        conn.close()
        before = sorted(path.name for path in root.iterdir())
        assert before == ["native.sqlite3"]
        expect_error(lambda: FrozenNativeAdapter(**arguments), ValueError, "persistent WAL")
        expect_error(adapter.capture, ValueError, "persistent WAL")
        assert sorted(path.name for path in root.iterdir()) == before
        # A supplied/automatically inventoried book is covered by the same guard.
        db.rename(root / "fbs-snapshot-accounting.sqlite3")
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE source(value TEXT)")
        conn.close()
        book = root / "fbs-snapshot-accounting.sqlite3"
        expect_error(lambda: FrozenNativeAdapter(**{**arguments, "files": [book]}),
                     ValueError, "persistent WAL")
        adapter = FrozenNativeAdapter(**arguments)
        before = sorted(path.name for path in root.iterdir())
        expect_error(adapter.capture, ValueError, "persistent WAL")
        assert sorted(path.name for path in root.iterdir()) == before


def main():
    check_closed_wal_refusal()
    with tempfile.TemporaryDirectory(prefix="history-state-machine-") as directory:
        catalog, units = setup_units()
        store = HistoryStore(Path(directory), max_store_bytes=32 * 1024**2)
        vector = vector_for(units)
        calls = []

        def compile_day(day):
            calls.append(day)
            return deepcopy(units[day])

        def update(**kwargs):
            return store.update(vector=vector, catalog=catalog, compile_day=compile_day,
                                revalidate=lambda: vector, **kwargs)

        partial = update(max_recomputes=1)
        assert partial["status"] == "pending" and calls == [DAYS[0]]
        expect_error(lambda: store.edition(), HistoryUnavailable, "history_not_ready")
        initial = update()
        assert initial["status"] == "published" and initial["recomputes"] == 2
        calls.clear()
        assert update()["status"] == "unchanged" and calls == []
        old = store.edition()

        # Related correction remains pending until both changed dates are complete.
        for day in DAYS[:2]:
            units[day]["cells"][ROW][10] = "semantic correction"
        vector = vector_for(units)
        assert update(max_recomputes=1)["status"] == "pending"
        assert store.edition() == old
        second = update()
        assert second["recomputes"] == 1
        assert store.edition()["days"][DAYS[2]] == old["days"][DAYS[2]]
        pinned = store.read(date_from=DAYS[0], date_to=DAYS[-1], edition_id=initial["edition_id"])
        assert pinned["rows"][0]["cells"][DAYS[0]][10] == ""
        current = store.edition()

        # A changed source proof cannot publish/consume a complete obsolete candidate.
        units[DAYS[2]]["cells"][ROW][10] = "old candidate"
        vector = vector_for(units)
        obsolete = deepcopy(vector)
        new_vector = deepcopy(vector)
        new_vector["dates"][DAYS[2]] = digest("superseding source")
        result = store.update(vector=obsolete, catalog=catalog, compile_day=compile_day,
                              revalidate=lambda: new_vector)
        assert result["status"] == "superseded" and store.edition() == current
        assert (store.root / "PENDING.json").exists()

        # Exception preserves edition and incomplete work; native reads distinguish missing/zero.
        units[DAYS[2]]["cells"][ROW][0:2] = [None, "—"]
        units[DAYS[2]]["accepted_ready_available"] = False
        vector = vector_for(units)
        def interrupted(_day):
            raise RuntimeError("interrupted before day publication")
        expect_error(lambda: store.update(vector=vector, catalog=catalog, compile_day=interrupted,
                                         revalidate=lambda: vector), RuntimeError)
        assert store.edition() == current
        third = update()
        read = store.read(date_from=DAYS[0], date_to=DAYS[-1])
        assert read["rows"][0]["cells"][DAYS[0]][0] == 0
        assert read["rows"][0]["cells"][DAYS[2]][0] is None and not read["availability"][DAYS[2]]

        # Last callback overruns deadline: completed pending object never becomes CURRENT.
        units[DAYS[1]]["cells"][ROW][10] = "deadline candidate"
        vector = vector_for(units)
        def slow(day):
            time.sleep(0.01)
            return compile_day(day)
        result = store.update(vector=vector, catalog=catalog, compile_day=slow,
                              revalidate=lambda: vector, deadline_monotonic=time.monotonic() + 0.005)
        assert result["status"] == "pending" and store._current()["current"] == third["edition_id"]
        assert update()["recomputes"] == 0  # resume completed object, publish once
        assert update(expected_base=initial["edition_id"])["status"] == "superseded"

        expect_error(lambda: store.read(date_from="2020-01-01", date_to=DAYS[-1]), ValueError)
        expect_error(lambda: store.read(date_from=DAYS[0], date_to=DAYS[-1], limit=513), ValueError)
        expect_error(lambda: store.read(date_from=DAYS[0], date_to=DAYS[-1], edition_id="../bad"),
                     HistoryUnavailable, "snapshot_expired")
        expect_error(lambda: store.update(vector={**vector, "coverage": "unknown"}, catalog=catalog,
                                         compile_day=compile_day, revalidate=lambda: vector),
                     HistoryUnavailable, "coverage_unknown")
        tiny_reader = HistoryStore(store.root, max_reply_bytes=64)
        expect_error(lambda: tiny_reader.read(date_from=DAYS[0], date_to=DAYS[-1]),
                     HistoryUnavailable, "reply_limit")
        # GC keeps current+previous even when old, and expires older pins outside GET.
        for path in (store.root / "editions").glob("*.json"):
            os.utime(path, (0, 0))
        with store._writer():
            store._collect(pin_ttl_seconds=0)
        expect_error(lambda: store.edition(initial["edition_id"]), HistoryUnavailable, "snapshot_expired")
        family = {str(p): p.stat().st_size for p in store.root.rglob("*") if p.is_file()}
        store.read(date_from=DAYS[0], date_to=DAYS[-1])
        assert family == {str(p): p.stat().st_size for p in store.root.rglob("*") if p.is_file()}

    # Actual SQLite candidate exceeds quota: no pointer and no orphan private candidate.
    with tempfile.TemporaryDirectory(prefix="history-quota-") as directory:
        catalog, units = setup_units()
        store = HistoryStore(Path(directory), max_store_bytes=32 * 1024**2)
        store.max_store_bytes = 18 * 1024**2
        payload = secrets.token_urlsafe(3072)
        day = DAYS[0]
        cell = deepcopy(units[day]["cells"][ROW])
        cell[10] = payload
        row_ids = ["TOTAL|metric_" + str(i) for i in range(6000)]
        catalog["order"] = row_ids
        catalog["rows"] = {
            rid: {"row_id": rid, "row_kind": "total", "group_id": "all", "values": {}}
            for rid in row_ids
        }
        units = {day: {**units[day], "members": row_ids,
                       "cells": {rid: cell for rid in row_ids}}}
        expect_error(lambda: store.update(vector=vector_for(units), catalog=catalog,
                                         compile_day=units.__getitem__, revalidate=lambda: vector_for(units)),
                     HistoryUnavailable, "storage_limit")
        assert store._current() is None and not list((store.root / "objects").glob(".building-*"))
    print(json.dumps({"status": "pass", "framework_only": True, "partial_resume": True,
                      "related_atomic_corrections": True, "no_change_zero": True,
                      "unchanged_ref_reuse": True, "pinned_previous": True, "superseded": True,
                      "exception_lastgood": True, "late_deadline_pending": True,
                      "quota_bounds": True, "missing_not_zero": True, "reader_readonly": True,
                      "retention_expired": True, "closed_wal_main_and_book_refused_without_sidecars": True}))


if __name__ == "__main__":
    main()
