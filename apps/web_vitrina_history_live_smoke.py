"""Owned native fixture: live RO bridge impact and bounded publication."""
from contextlib import closing, ExitStack
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import time
import fcntl
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from apps.web_vitrina_history_store_smoke import setup_units, vector_for
from apps.ff_pool_fbs_lifecycle_quality_reuse_smoke import fixture as quality_fixture
from packages.application.web_vitrina_window_read_context import window_read_context
from packages.application.web_vitrina_fbs_lifecycle_last_good import load_owner_paused_fallback, build_and_publish_cache
from packages.application.storage_registry import MONOLITH_FILENAME
from packages.application.ready_publication import ensure_publication_schema
from packages.application.web_vitrina_history_live_adapter import (
    LiveNativeAdapter, LiveSourceUnavailable, update_live_history,
    _require_live_sqlite_family, SOURCES,
)
from packages.application.web_vitrina_history_store import HistoryStore
from packages.application.web_vitrina_history_compiler import NativeDatedCompiler
from packages.application.calculation_parameters import ensure_calculation_parameters_schema, PROXY_BLOCK_KEY
from apps.web_vitrina_finished_snapshot_build import bounded_worker
from apps import web_vitrina_history_candidate_build as command
from packages.application import ff_pool_fbs_lifecycle as lifecycle
from packages.application.web_vitrina_history_compiler import digest
from packages.application.web_vitrina_window_v3 import _DateBinding


def changed(old, new):
    return {d for d in old["dates"] if old["dates"][d] != new["dates"][d]}


def check_publication_projection(root):
    """Fresh consumed proofs, including shared bindings, under unchanged caps."""
    adapter = LiveNativeAdapter(db_path=root / "native.sqlite3", runtime_dir=root / "runtime",
        cache_dir=root / "proofs", now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        date_from="2026-01-01", date_to="2026-06-29", formula_epoch="publication-proof",
        max_read_bytes=4096)
    first_key, second_key = ("bundle", "2026-06-28"), ("bundle", "2026-06-29")
    bindings = [_DateBinding(day, "covered", first_key[1], first_key)
                for day in adapter.days[:-1]]
    bindings.append(_DateBinding(adapter.days[-1], "exact", second_key[1], second_key))
    tables = {"sheet_vitrina_v1_ready_publications"}
    with closing(sqlite3.connect(":memory:")) as conn:
        ensure_publication_schema(conn)
        ordinary_inputs = json.dumps({"unconsumed": "i" * 192 * 1024})
        diagnostics = json.dumps({"unconsumed": "d" * 192 * 1024})
        retention_inputs = '{"contract":"accepted_inventory_retention_v1","dates":{}}'
        for operation, kind, key, inputs in (
                ("ordinary", "book_ready", first_key, ordinary_inputs),
                ("retention", "inventory_retention", second_key, retention_inputs)):
            conn.execute("""INSERT INTO sheet_vitrina_v1_ready_publications
                (operation_id,attempt_id,kind,bundle_version,as_of_date,expected_digest,
                 inputs_json,book_required,ready_required,state,book_version,after_digest,
                 created_at,finished_at,diagnostics_json)
                VALUES(?,?,?,?,?,'expected',?,1,1,'complete','book','after','created','finished',?)""",
                (operation, "attempt", kind, *key, inputs, diagnostics))

        def snapshot():
            adapter.stats = {"bytes": 0, "queries": 0, "plans_loaded": 0}
            adapter.deadline = time.monotonic() + 4
            proofs = adapter._publication_proofs(conn, tables, bindings)
            assert adapter.stats["queries"] == 2  # 180 days, only two selected groups.
            return proofs, {b.date: proofs[b.source_key] for b in bindings}

        proofs, baseline = snapshot()
        expected = [["ordinary", "attempt", "book_ready", *first_key,
                     "book", "finished", "after", None]]
        assert proofs[first_key] == digest(expected)
        expected = [["retention", "attempt", "inventory_retention", *second_key,
                     "book", "finished", "after", retention_inputs]]
        assert proofs[second_key] == digest(expected)
        measured = dict(adapter.stats)
        assert measured["bytes"] < 4096
        # These supported finalize changes are not business renderer dependencies.
        conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json='{}'")
        conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET inputs_json='{}' WHERE operation_id='ordinary'")
        assert snapshot()[1] == baseline
        affected = set(adapter.days[:-1])
        for column, value in (("operation_id", "changed-operation"), ("attempt_id", "changed-attempt"),
                              ("kind", "other-kind"), ("book_version", "changed-book"),
                              ("after_digest", "changed-digest"), ("finished_at", "changed-time"),
                              ("state", "prepared"), ("ready_required", 0)):
            old = conn.execute("SELECT " + column + " FROM sheet_vitrina_v1_ready_publications WHERE kind<>'inventory_retention'").fetchone()[0]
            conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET " + column + "=? WHERE kind<>'inventory_retention'", (value,))
            current = snapshot()[1]
            assert {day for day in baseline if current[day] != baseline[day]} == affected, column
            conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET " + column + "=? WHERE kind<>'inventory_retention'", (old,))
        conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET inputs_json=? WHERE kind='inventory_retention'",
                     ('{"corrected":true}',))
        corrected = snapshot()[1]
        assert {day for day in baseline if corrected[day] != baseline[day]} == {adapter.days[-1]}
        conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET inputs_json=? WHERE kind='inventory_retention'", (retention_inputs,))
        conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET as_of_date=? WHERE operation_id='ordinary'", (second_key[1],))
        assert {day for day, proof in snapshot()[1].items() if proof != baseline[day]} == set(adapter.days)
        conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET as_of_date=? WHERE operation_id='ordinary'", (first_key[1],))
        conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET bundle_version='other' WHERE operation_id='ordinary'")
        assert {day for day, proof in snapshot()[1].items() if proof != baseline[day]} == affected
        conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET bundle_version=? WHERE operation_id='ordinary'", (first_key[0],))
        conn.execute("DELETE FROM sheet_vitrina_v1_ready_publications WHERE operation_id='ordinary'")
        assert {day for day, proof in snapshot()[1].items() if proof != baseline[day]} == affected
        return {"days": len(bindings), "unique_groups": 2, "stats": measured,
                "fresh_consumed_mutation_delete_redate": True, "retention_inputs": True}


def check_dated_slice_progress(root):
    adapter = LiveNativeAdapter(db_path=root / "native.sqlite3", runtime_dir=root / "runtime",
        cache_dir=root / "proofs", now=datetime(2026, 4, 20, tzinfo=timezone.utc),
        date_from="2026-04-14", date_to="2026-04-18", formula_epoch="dated-proof",
        max_read_bytes=2400)
    table = "owned_dated_source"
    days = adapter.days
    cache = {"slices": {}}
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE owned_dated_source(day TEXT,row_id INTEGER,payload TEXT)")
        conn.executemany("INSERT INTO owned_dated_source VALUES(?,?,?)",
                         [(day, i, "x" * 1000) for i, day in enumerate(days)])

        def begin():
            adapter.stats = {"bytes": 0, "queries": 0, "plans_loaded": 0,
                             "dated_days_loaded": 0}
            adapter.deadline = time.monotonic() + 4
            adapter.used_slices = set()

        def expected():
            grouped = {day: [] for day in days}
            for row in conn.execute("SELECT * FROM owned_dated_source ORDER BY day,row_id,payload"):
                grouped[row[0]].append(list(row))
            return {day: digest(rows) for day, rows in grouped.items()}

        baseline = expected()
        passes = 0
        while True:
            begin()
            previous = dict(cache["slices"])
            passes += 1
            try:
                full = adapter._slice(conn, {table}, cache, {table: 1}, table, "day", days)
            except LiveSourceUnavailable as exc:
                assert str(exc) == "live_dated_bootstrap_pending"
                assert len(cache["slices"]) > len(previous)
                assert adapter.stats["dated_days_loaded"] == 2
                # The partial third day has no reusable proof.
                assert len(cache["slices"]) == 2 * passes
                assert all(proof == baseline[day] for day in days
                           for key, proof in cache["slices"].items()
                           if key == digest([table, 1, day]))
                assert passes < 3
            else:
                assert full == baseline and passes == 3
                assert adapter.stats["queries"] == 1
                assert adapter.stats["dated_days_loaded"] == 1
                break
        begin()
        assert adapter._slice(conn, {table}, cache, {table: 1}, table, "day", days) == baseline
        assert adapter.stats["queries"] == 0 and adapter.stats["bytes"] == 0
        # A changed native alarm invalidates the old keys. Every field remains
        # in the independently ordered digest, including semantic-only payload.
        adapter.max_read_bytes = 16384
        conn.execute("UPDATE owned_dated_source SET payload='semantic-correction' WHERE day=?", (days[0],))
        begin()
        corrected = adapter._slice(conn, {table}, cache, {table: 2}, table, "day", days)
        assert corrected == expected() and adapter.stats["queries"] == len(days)
        assert {day for day in days if corrected[day] != baseline[day]} == {days[0]}
        conn.execute("UPDATE owned_dated_source SET day=? WHERE day=?", (days[3], days[1]))
        begin()
        redated = adapter._slice(conn, {table}, cache, {table: 3}, table, "day", days)
        assert redated == expected() and adapter.stats["queries"] == len(days)
        assert {day for day in days if redated[day] != corrected[day]} == {days[1], days[3]}
        assert redated[days[1]] == digest([])  # Missing source evidence, not a numeric zero.
        conn.execute("DELETE FROM owned_dated_source WHERE day=?", (days[2],))
        begin()
        deleted = adapter._slice(conn, {table}, cache, {table: 4}, table, "day", days)
        assert deleted == expected()
        assert {day for day in days if deleted[day] != redated[day]} == {days[2]}
        # No revision means no cache reuse, even within the same adapter.
        unversioned = {"slices": {}}
        begin()
        first = adapter._slice(conn, {table}, unversioned, {}, table, "day", days)
        conn.execute("UPDATE owned_dated_source SET payload='fresh-unversioned' WHERE day=?", (days[0],))
        begin()
        second = adapter._slice(conn, {table}, unversioned, {}, table, "day", days)
        assert first != second and second == expected() and unversioned["slices"] == {}
        assert adapter.stats["queries"] == len(days)
        # An over-cap first day must not produce endless warming outcomes.
        adapter.max_read_bytes = 100
        oversized = {"slices": {}}
        begin()
        try:
            adapter._slice(conn, {table}, oversized, {table: 5}, table, "day", [days[3]])
        except LiveSourceUnavailable as exc:
            assert str(exc) == "live_source_resource_limit"
        else:
            raise AssertionError("one over-cap day must be refused")
        assert oversized["slices"] == {} and adapter.stats["dated_days_loaded"] == 0
        return {"passes": passes, "completed_day_proofs": len(days),
                "unchanged_queries": 0, "exact_ordered_digest": True,
                "alarm_mutation_delete_redate": True, "unversioned_fresh": True,
                "over_cap_day_terminal": True}


def check_ready_binding_serialization(root):
    """Fresh native headers and persisted headers consume identical book proof."""
    now = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)
    day = "2026-04-20"
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=1, now=now)
    with fixture:
        runtime = fixture.entrypoint.runtime
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")  # Owned fixture provisions live RO family.
            ensure_publication_schema(conn)
            encoded = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (day,)).fetchone()[0]
            plan = json.loads(encoded)
            # Deliberately noncanonical at BOTH nesting levels. The native
            # source is unchanged between cold and pinned/warm captures.
            plan.setdefault("metadata", {})["fbs_accounting_bindings"] = {
                day: {"z_owned": {"z_nested": 2, "a_nested": 1}, "a_owned": "proof"}}
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?", (json.dumps(plan), day))
        with closing(sqlite3.connect(runtime.db_path)) as keeper:
            keeper.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            adapter = LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir,
                cache_dir=root / "proofs", now=now, date_from=day, date_to=day,
                formula_epoch="owned-ready-bindings")
            cold = adapter.capture()
            assert adapter.stats["plans_loaded"] == 1
            fence, context = adapter.fence, deepcopy(adapter.context)
            with window_read_context(runtime.db_path, runtime_dir=runtime.runtime_dir):
                assert adapter.capture() == cold
                assert adapter.fence == fence and adapter.context == context
                assert adapter.stats["plans_loaded"] == 0
            assert adapter.capture() == cold and adapter.fence == fence
            # A genuine binding change still changes the dated proof and the
            # native revision fence. Its NEW header must immediately be stable.
            with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
                plan["metadata"]["fbs_accounting_bindings"][day]["z_owned"]["z_nested"] = 3
                conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?", (json.dumps(plan), day))
            corrected = adapter.capture()
            assert adapter.stats["plans_loaded"] == 1
            assert changed(cold, corrected) == {day} and corrected["epoch"] == cold["epoch"]
            assert adapter.fence != fence
            corrected_fence = adapter.fence
            with window_read_context(runtime.db_path, runtime_dir=runtime.runtime_dir):
                assert adapter.capture() == corrected and adapter.fence == corrected_fence
                assert adapter.stats["plans_loaded"] == 0
            assert adapter.max_read_bytes == 32 * 1024**2 and adapter.max_capture_seconds == 20
    return {"cold_warm_pinned_equal": True, "new_ready_header_equal": True,
            "binding_change_dated_dirty": 1, "binding_change_fence_changed": True}


def check_pending_rollover(root):
    store = HistoryStore(root)
    catalog, units = setup_units()
    old = vector_for(units)
    first = store.update(vector=old, catalog=catalog, compile_day=units.__getitem__,
                         revalidate=lambda: old, max_recomputes=1)
    assert first["status"] == "pending"
    refs = json.loads((root / "PENDING.json").read_text())["refs"]
    new = deepcopy(old)
    new["dates"]["2026-04-20"] = "f" * 64
    calls = []
    def compile_day(day):
        calls.append(day)
        return units[day]
    final = store.update(vector=new, catalog=catalog, compile_day=compile_day,
                         revalidate=lambda: new)
    assert final["status"] == "published" and calls == ["2026-04-19", "2026-04-20"]
    assert store.edition()["days"]["2026-04-18"] == refs["2026-04-18"]


def check_initial_warming_failures(adapter, runtime, store):
    """Control-flow failures only; actual native warming is checked in main."""
    previous = store.edition()
    configured = adapter.max_capture_seconds
    for reason, progress, deadline in (
            ("live_source_resource_limit", 1, time.monotonic() + 1),
            ("live_dated_bootstrap_pending", 0, time.monotonic() + 1),
            ("live_temporal_bootstrap_pending", 0, time.monotonic() + 1),
            ("live_temporal_bootstrap_pending", 1, None),
            ("live_ready_bootstrap_pending", 1, None)):
        def refused():
            adapter.stats = {"plans_loaded": progress, "dated_days_loaded": 0,
                             "bytes": 1, "queries": 1}
            raise LiveSourceUnavailable(reason)
        with patch.object(adapter, "capture", side_effect=refused) as capture:
            try:
                update_live_history(adapter=adapter, runtime=runtime, store=store,
                                    deadline_monotonic=deadline)
            except LiveSourceUnavailable as exc:
                assert str(exc) == reason
            else:
                raise AssertionError("terminal/no-progress/unbounded call must not retry")
            assert capture.call_count == 1
        assert adapter.max_capture_seconds == configured and store.edition() == previous
    seen_seconds = []
    def expired():
        seen_seconds.append(adapter.max_capture_seconds)
        adapter.stats = {"plans_loaded": 0, "dated_days_loaded": 0, "temporal_proofs_loaded": 1,
                         "bytes": 1, "queries": 1}
        time.sleep(0.02)
        raise LiveSourceUnavailable("live_temporal_bootstrap_pending")
    with patch.object(adapter, "capture", side_effect=expired) as capture:
        try:
            update_live_history(adapter=adapter, runtime=runtime, store=store,
                                deadline_monotonic=time.monotonic() + 0.01)
        except LiveSourceUnavailable as exc:
            assert str(exc) == "live_bootstrap_deadline"
        else:
            raise AssertionError("deadline must terminate initial warming")
        assert capture.call_count == 1 and 0 < seen_seconds[0] <= 0.01
    assert adapter.max_capture_seconds == configured and store.edition() == previous


def check_quality_floor(root):
    adapter = LiveNativeAdapter(db_path=root / "source.sqlite3", runtime_dir=root / "source",
        cache_dir=root / "proofs", now=datetime(2026, 7, 2, tzinfo=timezone.utc),
        date_from="2026-07-01", date_to="2026-07-01", formula_epoch="floor-fixture")
    adapter.stats, adapter.deadline = {"bytes": 0, "queries": 0}, time.monotonic() + 4
    with closing(sqlite3.connect(":memory:")) as conn:
        schemas = {
            lifecycle.IDENTITY_PENDING_TABLE: "pending_id TEXT,cutover_id TEXT,source_status_observation_sequence INTEGER,deferred_identity_evidence_sequence INTEGER",
            lifecycle.IDENTITY_PENDING_RESOLUTIONS_TABLE: "pending_id TEXT",
            lifecycle.STATUS_OBSERVATIONS_TABLE: "observation_sequence INTEGER,order_id INTEGER,order_revision TEXT,status_digest TEXT,supplier_status TEXT,wb_status TEXT,positive_quantity INTEGER,observed_at TEXT",
            lifecycle.OBSERVATIONS_TABLE: "observation_sequence INTEGER,observation_id TEXT,order_id INTEGER,source_revision TEXT,source_created_at TEXT,observed_at TEXT,warehouse_id INTEGER,nm_id INTEGER,chrt_id INTEGER,skus_json TEXT,office_id INTEGER,seller_sku TEXT",
            lifecycle.DRAIN_STATE_TABLE: "cutover_id TEXT,last_status_observation_sequence INTEGER",
            "sheet_vitrina_v1_ff_pool_cutover_manifests": "cutover_id TEXT,cutover_at TEXT,manifest_json TEXT",
        }
        for name in (lifecycle.IDENTITY_EVIDENCE_TABLE, lifecycle.WAREHOUSE_MAPPINGS_TABLE,
                     lifecycle.IDENTITY_MAPPINGS_TABLE, lifecycle.FACILITIES_TABLE):
            schemas[name] = "unused TEXT"
        for table, columns in schemas.items():
            conn.execute("CREATE TABLE " + table + "(" + columns + ")")
        conn.execute("INSERT INTO sheet_vitrina_v1_ff_pool_cutover_manifests VALUES('cutover','2026-06-01',?)", (json.dumps({"cutover_id": "cutover"}),))
        conn.execute("INSERT INTO " + lifecycle.DRAIN_STATE_TABLE + " VALUES('cutover',4)")
        conn.execute("INSERT INTO " + lifecycle.STATUS_OBSERVATIONS_TABLE + " VALUES(5,1,'rev','status','new','new',1,'2026-07-02T01:00:00Z')")
        conn.execute("INSERT INTO " + lifecycle.OBSERVATIONS_TABLE + " VALUES(1,'obs',1,'rev','2026-07-01T22:00:00Z','2026-07-02T01:00:00Z',0,0,0,'[]',0,'')")
        assert adapter._quality_floor(conn) == "2026-07-02"  # Actual business TZ, not UTC substring.
        native = lifecycle.fbs_lifecycle_quality_coverage(conn, as_of_date="2026-07-01")
        assert native["status"] == "exact" and native["groups"] == []
        conn.execute("UPDATE " + lifecycle.OBSERVATIONS_TABLE + " SET source_created_at='2026-06-25T00:00:00Z'")
        assert adapter._quality_floor(conn) == "2026-06-25"
        conn.execute("UPDATE " + lifecycle.DRAIN_STATE_TABLE + " SET last_status_observation_sequence=5")
        assert adapter._quality_floor(conn) == "9999-12-31"


def check_cli_guards(root):
    source = root / "runtime"
    source.mkdir()
    lock = source / ".web-vitrina-finished-builder.lock"
    lock.write_bytes(b"owned fixture lock")
    before = (lock.read_bytes(), lock.stat().st_mtime_ns)
    arguments = ["candidate", "--runtime-dir", str(source), "--candidate-root", str(root / "candidate"),
                 "--date-from", "2026-04-18", "--date-to", "2026-04-20", "--formula-epoch", "fixture"]
    with patch.object(sys, "argv", arguments), patch.object(command, "deadline_seconds", side_effect=[10, 8]), \
            patch.object(command, "admission", return_value="idle"), \
            patch.object(command, "bounded_worker", return_value={"status": "fixture_only"}) as worker, redirect_stdout(StringIO()):
        command.main()
        assert worker.call_args.args[1] == 8 and worker.call_args.args[0][-1] == "--worker"
    with lock.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.object(sys, "argv", arguments), patch.object(command, "deadline_seconds", return_value=10), \
                patch.object(command, "admission", return_value="idle"), \
                patch.object(command, "bounded_worker") as worker, redirect_stdout(StringIO()):
            command.main()
            assert not worker.called
    with patch.object(sys, "argv", [*arguments, "--worker"]), \
            patch.object(command, "deadline_seconds", return_value=0), \
            patch.object(command, "StoreRegistry") as registry, redirect_stdout(StringIO()):
        command.main()
        assert not registry.called
    manual = [*arguments, "--manual", "--budget-seconds", "240"]
    with patch.object(sys, "argv", manual), \
            patch.object(command, "deadline_seconds", side_effect=AssertionError("manual uses no calendar")), \
            patch.object(command, "admission", return_value="idle"), \
            patch.object(command, "bounded_worker", return_value={"status": "fixture_only"}) as worker, redirect_stdout(StringIO()):
        command.main()
        assert worker.call_args.args[1] == 180
        assert "--manual" in worker.call_args.args[0] and worker.call_args.args[0][-1] == "--worker"
    with patch.object(sys, "argv", manual), \
            patch.object(command, "admission", return_value="busy"), \
            patch.object(command, "bounded_worker") as worker, redirect_stdout(StringIO()):
        command.main()
        assert not worker.called
    with lock.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.object(sys, "argv", manual), \
                patch.object(command, "admission", return_value="idle"), \
                patch.object(command, "bounded_worker") as worker, redirect_stdout(StringIO()):
            command.main()
            assert not worker.called
    started = time.monotonic()
    with patch.object(sys, "argv", [*manual, "--worker"]), \
            patch.object(command, "deadline_seconds", side_effect=AssertionError("manual worker uses no calendar")), \
            patch.object(command, "StoreRegistry"), \
            patch.object(command, "RegistryUploadDbBackedRuntime"), \
            patch.object(command, "LiveNativeAdapter"), patch.object(command, "HistoryStore"), \
            patch.object(command, "update_live_history", return_value={"status": "fixture_only"}) as update, redirect_stdout(StringIO()):
        command.main()
        assert started + 180 <= update.call_args.kwargs["deadline_monotonic"] <= time.monotonic() + 180
    for message, expected in (("live_source_resource_limit:owned-private-detail", "live_source_resource_limit"),
                              ("unknown-owned-private-detail", "live_source_unavailable")):
        output = StringIO()
        with patch.object(sys, "argv", [*manual, "--worker"]), \
                patch.object(command, "StoreRegistry"), \
                patch.object(command, "RegistryUploadDbBackedRuntime"), \
                patch.object(command, "LiveNativeAdapter") as adapter, patch.object(command, "HistoryStore"), \
                patch.object(command, "update_live_history", side_effect=LiveSourceUnavailable(message)), \
                redirect_stdout(output):
            adapter.return_value.stats = {"bytes": 123, "queries": 4, "raw_source": "owned-private-detail"}
            assert command.main() == 1  # Failure remains nonzero, never a successful data update.
        failure = json.loads(output.getvalue())
        assert failure["reason_code"] == expected and failure["source_reads"] == {"bytes": 123, "queries": 4}
        assert "owned-private-detail" not in output.getvalue()
    assert (lock.read_bytes(), lock.stat().st_mtime_ns) == before


def check_safe_failure_parent():
    failure = command.source_failure_result(LiveSourceUnavailable("live_source_resource_limit:private"),
                                            {"bytes": 123, "queries": 4})
    def run(payload, opted_in=True):
        script = "import sys;print(" + repr(json.dumps(payload)) + ");print('owned-private-stderr',file=sys.stderr);sys.exit(3)"
        return bounded_worker([sys.executable, "-c", script], 2,
            allowed_failure_reasons=command.SAFE_SOURCE_FAILURE_REASONS if opted_in else ())
    accepted = run(failure)
    assert accepted == {**failure, "exit_code": 3}
    assert "reason_code" not in run(failure, opted_in=False)
    for malformed in ([failure], {**failure, "reason_code": "untrusted"},
                      {**failure, "source_reads": {"bytes": True}},
                      {**failure, "source_reads": {"raw_source": "private"}},
                      {**failure, "raw_source": "private"}):
        refused = run(malformed)
        assert refused == {"status": "build_failed", "exit_code": 3, "last_good_retained": True}
    return {"validated_reason_and_counters": True, "nonzero_preserved": True,
            "stderr_raw_fields_not_forwarded": True, "ordinary_caller_unchanged": True}


def check_source_guards(root):
    root.mkdir()
    source = root / "closed-wal.sqlite3"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE owned(value TEXT)")
        conn.commit()
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    try:
        _require_live_sqlite_family(source)
    except LiveSourceUnavailable as exc:
        assert "family_unavailable" in str(exc)
    else:
        raise AssertionError("closed persistent WAL must be refused before RO open")
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}
    # The same preflight applies to the optional book before any window opens.
    runtime_dir = root / "runtime"
    runtime_dir.mkdir()
    operational = runtime_dir / "native.sqlite3"
    with closing(sqlite3.connect(operational)) as conn:
        conn.execute("CREATE TABLE owned(value TEXT)")
        conn.commit()
    book = runtime_dir / "fbs-snapshot-accounting.sqlite3"
    book.write_bytes(source.read_bytes())
    adapter = LiveNativeAdapter(db_path=operational, runtime_dir=runtime_dir,
        cache_dir=root / "proofs", now=datetime(2026, 4, 20, tzinfo=timezone.utc),
        date_from="2026-04-20", date_to="2026-04-20", formula_epoch="guard")
    before = {p.name: p.read_bytes() for p in runtime_dir.iterdir()}
    try:
        adapter.capture()
    except LiveSourceUnavailable as exc:
        assert book.name in str(exc)
    else:
        raise AssertionError("closed WAL book must be refused before operational open")
    assert before == {p.name: p.read_bytes() for p in runtime_dir.iterdir()}


def check_quality_scope_bridge(root):
    root.mkdir()
    runtime_dir, cache_dir = root / "runtime", root / "proofs"
    runtime_dir.mkdir()
    cache_dir.mkdir()
    source = root / "source.sqlite3"
    with closing(quality_fixture()) as fixture_conn, closing(sqlite3.connect(source)) as output:
        fixture_conn.backup(output)  # Only owned synthetic source, never business DB.
        output.execute("CREATE TABLE registry_upload_current_state(unused TEXT)")
        output.execute("CREATE TABLE sheet_vitrina_v1_ready_snapshots(unused TEXT)")
        output.commit()
    adapter = LiveNativeAdapter(db_path=source, runtime_dir=runtime_dir, cache_dir=cache_dir,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc), date_from="2026-08-15",
        date_to="2026-08-20", formula_epoch="quality-bridge")
    cache = {"source": "owned-fixture", "quality_scopes": {}}
    adapter._quality_cache = cache
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    days = ("2026-08-01", "2026-08-15", "2026-08-18", "2026-08-20")
    with window_read_context(source, runtime_dir=runtime_dir) as context:
        conn = context.borrow(source)
        expected = {(day, requested): lifecycle.fbs_lifecycle_quality_coverage(
            conn, as_of_date=day, requested_nm_ids=requested)
            for day in days for requested in (None, (101, 102))}
    calls = lifecycle._lifecycle_quality_scopes
    portion_reads = []
    with patch.object(lifecycle, "_lifecycle_quality_scopes", wraps=calls) as scopes:
        for portion, expected_calls in ((0, 5), (1, 0)):
            scopes.reset_mock()
            adapter.stats = {"bytes": 0, "queries": 0}
            adapter.deadline = time.monotonic() + 20
            with window_read_context(source, runtime_dir=runtime_dir) as context, \
                    patch.object(adapter, "_cache_reserve", wraps=adapter._cache_reserve) as quota_checks:
                conn = context.borrow(source)
                adapter._bind_quality_resolver(conn, context, cache)
                resolver = adapter.lifecycle_quality_resolver
                assert resolver(days[0]) == expected[(days[0], None)]
                assert adapter._quality_native is None  # Native proven floor skips loader.
                for day in days:
                    for requested in (None, (101, 102)):
                        assert resolver(day, requested) == expected[(day, requested)]
                assert scopes.call_count == expected_calls, (portion, scopes.call_count)
                assert quota_checks.call_count == 0, "whole-cache checks in per-date hot path"
                portion_reads.append(dict(adapter.stats))
                assert adapter.stats["bytes"] > 0 and adapter.stats["queries"] > 0
                adapter.finish_quality_portion(prune=True)
                assert quota_checks.call_count == 1
            try:
                resolver("2026-08-20")
            except LiveSourceUnavailable as exc:
                assert str(exc) == "lifecycle_quality_pin_closed"
            else:
                raise AssertionError("closed pinned resolver reused")
    # New bulk loader reads are charged to the original source byte/time caps.
    for byte_cap, deadline, reason in ((1, time.monotonic() + 20, "live_source_resource_limit"),
            (32 * 1024**2, time.monotonic() - 1, "live_source_resource_limit")):
        adapter.max_read_bytes, adapter.deadline = byte_cap, deadline
        adapter.stats = {"bytes": 0, "queries": 0}
        with window_read_context(source, runtime_dir=runtime_dir) as context:
            adapter._bind_quality_resolver(context.borrow(source), context, cache)
            try:
                adapter.lifecycle_quality_resolver("2026-08-20")
            except LiveSourceUnavailable as exc:
                assert str(exc) == reason
            else:
                raise AssertionError("unaccounted quality bulk reads")
    adapter.max_read_bytes, adapter.deadline = 32 * 1024**2, time.monotonic() + 20
    adapter.max_cache_bytes = 1
    adapter.stats = {"bytes": 0, "queries": 0}
    try:
        adapter.finish_quality_portion()
    except LiveSourceUnavailable as exc:
        assert str(exc) == "source_cache_resource_limit"
    else:
        raise AssertionError("unbounded whole proof cache")
    adapter.max_cache_bytes = 128 * 1024**2
    # Real pinned policy files change the selected resolver, without native reads.
    policy = runtime_dir / ".auto-updates-policy.json"
    variants = ("{}", json.dumps({"schema_version": "auto_updates_owner_policy_v2",
        "revision": 1, "processes": {"fbs_shadow": {"desired": True}}}))
    results = []
    for value in variants:
        policy.write_text(value)
        adapter.stats, adapter.deadline = {"bytes": 0, "queries": 0}, time.monotonic() + 20
        with window_read_context(source, runtime_dir=runtime_dir) as context:
            adapter._bind_quality_resolver(context.borrow(source), context, cache)
            expected = load_owner_paused_fallback(runtime_dir).resolve("2026-08-20")
            actual = adapter.lifecycle_quality_resolver("2026-08-20")
            assert actual == expected and adapter._quality_native is None
            assert adapter.stats == {"bytes": 0, "queries": 0}
            results.append(actual)
    assert results[0] != results[1]
    policy.unlink()
    with window_read_context(source, runtime_dir=runtime_dir) as context:
        conn = context.borrow(source)
        adapter._bind_quality_resolver(conn, context, cache)
        assert adapter.lifecycle_quality_resolver("2026-08-20") == lifecycle.fbs_lifecycle_quality_coverage(
            conn, as_of_date="2026-08-20")
        adapter.finish_quality_portion(prune=True)
    assert before == {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    # Actual supported last-good writer on a separate owned fully drained source.
    fallback_root = root / "last-good"
    fallback_root.mkdir()
    fallback_db = fallback_root / MONOLITH_FILENAME
    with closing(quality_fixture()) as fixture_conn, closing(sqlite3.connect(fallback_db)) as output:
        fixture_conn.backup(output)
        output.execute("CREATE TABLE registry_upload_current_state(unused TEXT)")
        output.execute("CREATE TABLE sheet_vitrina_v1_ready_snapshots(unused TEXT)")
        output.execute("DELETE FROM " + lifecycle.IDENTITY_PENDING_TABLE)
        output.execute("DELETE FROM " + lifecycle.STATUS_OBSERVATIONS_TABLE)
        output.execute("UPDATE " + lifecycle.DRAIN_STATE_TABLE + " SET last_status_observation_sequence=5")
        output.commit()
    (fallback_root / ".auto-updates-policy.json").write_text(json.dumps({
        "schema_version": "auto_updates_owner_policy_v2", "revision": 1,
        "processes": {"fbs_shadow": {"desired": False}}}))
    fallback_adapter = LiveNativeAdapter(db_path=fallback_db, runtime_dir=fallback_root,
        cache_dir=cache_dir, now=adapter.now, date_from="2026-08-20", date_to="2026-08-20",
        formula_epoch="last-good-fixture")
    results, cache_proofs = [], []
    for timestamp in ("2026-08-20T00:00:00Z", "2026-08-20T01:00:00Z"):
        build_and_publish_cache(fallback_root, db_path=fallback_db,
            generated_at=timestamp, source_as_of_date="2026-08-20")
        fallback_adapter.stats = {"bytes": 0, "queries": 0}
        fallback_adapter.deadline = time.monotonic() + 20
        with window_read_context(fallback_db, runtime_dir=fallback_root) as context:
            fallback_adapter._bind_quality_resolver(context.borrow(fallback_db), context, {})
            result = fallback_adapter.lifecycle_quality_resolver("2026-08-20")
            assert result["source_mode"] == "last_good_owner_paused"
            assert result["last_good_at"] == timestamp
            assert fallback_adapter._quality_native is None
            assert fallback_adapter.stats == {"bytes": 0, "queries": 0}
            cache_proofs.append(digest(context.read_file_once(
                fallback_root / ".web-vitrina-fbs-lifecycle-last-good.json").hex()))
            results.append(digest(result))
    assert results[0] != results[1] and cache_proofs[0] != cache_proofs[1]
    return {"cold_native_scope_calls": 5, "warm_native_scope_calls": 0, "portion_reads": portion_reads,
            "all_native_coverage_fields": True, "fresh_pin_and_closed_refusal": True,
            "native_floor_skip": True, "bulk_byte_deadline_and_whole_cache_caps": True,
            "owner_policy_switch_and_lastgood_content_change": True}


def check_quality_cache_and_temporal(root):
    adapter = LiveNativeAdapter(db_path=root / "native.sqlite3", runtime_dir=root / "runtime",
        cache_dir=root / "proofs", now=datetime(2026, 4, 20, tzinfo=timezone.utc),
        date_from="2026-04-18", date_to="2026-04-18", formula_epoch="selectors")
    adapter.stats, adapter.deadline = {"bytes": 0, "queries": 0}, time.monotonic() + 4
    adapter.used_slices = set()
    cache = {"slices": {}}
    captures = [("capture", "2026-04-18", "[]", "{}", "proof")]
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE temporal_source_snapshots(source_key TEXT,snapshot_date TEXT,captured_at TEXT,payload_json TEXT)")
        conn.execute("CREATE TABLE sheet_vitrina_v1_ready_temporal_revisions(source_key TEXT,snapshot_date TEXT,snapshot_role TEXT,revision INTEGER)")
        conn.execute("INSERT INTO temporal_source_snapshots VALUES(?,?,?,?)", (SOURCES[1], "2026-04-18", "capture", '{"quality":"old"}'))
        conn.execute("INSERT INTO sheet_vitrina_v1_ready_temporal_revisions VALUES(?,?,?,?)", (SOURCES[1], "2026-04-18", "", 1))
        tables = {"temporal_source_snapshots", "sheet_vitrina_v1_ready_temporal_revisions"}
        first = {"2026-04-18": {}}
        adapter._temporal(conn, tables, cache, first, "2026-04-20")
        assert first["2026-04-18"]["temporal"][0][0] == "sales_funnel_history_buyout_confirmation_v1"
        conn.execute("UPDATE temporal_source_snapshots SET payload_json='{}'")
        conn.execute("UPDATE sheet_vitrina_v1_ready_temporal_revisions SET revision=2")
        second = {"2026-04-18": {}}
        adapter._temporal(conn, tables, cache, second, "2026-04-20")
        assert first != second


def check_temporal_progress(root):
    adapter = LiveNativeAdapter(db_path=root / "native.sqlite3", runtime_dir=root / "runtime",
        cache_dir=root / "proofs", now=datetime(2026, 4, 20, tzinfo=timezone.utc),
        date_from="2026-04-14", date_to="2026-04-18", formula_epoch="temporal-proof")
    tables = {"temporal_source_snapshots", "sheet_vitrina_v1_ready_temporal_revisions"}
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE temporal_source_snapshots(source_key TEXT,snapshot_date TEXT,captured_at TEXT,payload_json TEXT)")
        conn.execute("CREATE TABLE sheet_vitrina_v1_ready_temporal_revisions(source_key TEXT,snapshot_date TEXT,snapshot_role TEXT,revision INTEGER)")
        conn.executemany("INSERT INTO temporal_source_snapshots VALUES(?,?,?,?)",
            [(SOURCES[0], day, "captured", json.dumps({"padding": "x" * 600})) for day in adapter.days])
        conn.executemany("INSERT INTO sheet_vitrina_v1_ready_temporal_revisions VALUES(?,?,?,?)",
            [(SOURCES[0], day, "", 1) for day in adapter.days])

        def capture(cache, limit=2400):
            adapter.max_read_bytes = limit
            adapter.stats = {"bytes": 0, "queries": 0, "temporal_proofs_loaded": 0}
            adapter.deadline = time.monotonic() + 4
            adapter.used_slices = set()
            vector = {day: {} for day in adapter.days}
            adapter._temporal(conn, tables, cache, vector, adapter.days[-1])
            return vector

        reference = capture({"slices": {}}, 32000)
        cache, passes = {"slices": {}}, 0
        while True:
            before = deepcopy(cache["slices"])
            passes += 1
            assert passes <= len(adapter.days)
            try:
                vector = capture(cache)
            except LiveSourceUnavailable as exc:
                assert str(exc) == "live_temporal_bootstrap_pending"
                assert adapter.stats["temporal_proofs_loaded"] > 0
                assert len(cache["slices"]) > len(before)
            else:
                assert vector == reference
                break
            assert all(cache["slices"][key] == value for key, value in before.items())
        assert passes > 1 and len(cache["slices"]) == len(adapter.days)
        assert capture(cache) == reference and adapter.stats["temporal_proofs_loaded"] == 0
        conn.execute("UPDATE temporal_source_snapshots SET payload_json='{}' WHERE snapshot_date=?", (adapter.days[0],))
        conn.execute("UPDATE sheet_vitrina_v1_ready_temporal_revisions SET revision=2 WHERE snapshot_date=?", (adapter.days[0],))
        corrected = capture(cache)
        assert {day for day in adapter.days if reference[day] != corrected[day]} == {adapter.days[0]}
        conn.execute("DELETE FROM temporal_source_snapshots WHERE snapshot_date=?", (adapter.days[1],))
        deleted = capture(cache)
        assert {day for day in adapter.days if corrected[day] != deleted[day]} == {adapter.days[1]}
        conn.execute("UPDATE temporal_source_snapshots SET snapshot_date=? WHERE snapshot_date=?",
                     (adapter.days[1], adapter.days[2]))
        conn.execute("UPDATE sheet_vitrina_v1_ready_temporal_revisions SET revision=3 WHERE snapshot_date=?", (adapter.days[1],))
        redated = capture(cache)
        assert {day for day in adapter.days if deleted[day] != redated[day]} == {adapter.days[1], adapter.days[2]}
        conn.execute("UPDATE temporal_source_snapshots SET payload_json=? WHERE snapshot_date=?",
                     (json.dumps({"oversized": "x" * 8000}), adapter.days[0]))
        conn.execute("UPDATE sheet_vitrina_v1_ready_temporal_revisions SET revision=4 WHERE snapshot_date=?", (adapter.days[0],))
        fresh = {"slices": {}}
        try:
            capture(fresh)
        except LiveSourceUnavailable as exc:
            assert str(exc) == "live_source_resource_limit"
        else:
            raise AssertionError("one oversized temporal key must stay terminal")
        assert fresh["slices"] == {} and adapter.stats["temporal_proofs_loaded"] == 0
        rows = adapter._rows
        def expired_rows(connection, sql, args=()):
            if sql.startswith("SELECT payload_json"):
                adapter.stats["plans_loaded"] = 1  # Prior progress cannot turn time expiry into retry.
                adapter.deadline = time.monotonic() - 1
            return rows(connection, sql, args)
        with patch.object(adapter, "_rows", side_effect=expired_rows):
            try:
                capture(fresh)
            except LiveSourceUnavailable as exc:
                assert str(exc) == "live_source_resource_limit"
            else:
                raise AssertionError("expired temporal read must stay terminal despite prior progress")
        assert fresh["slices"] == {} and adapter.stats["temporal_proofs_loaded"] == 0
        return {"passes": passes, "exact_fullproof_vector": True, "cache_hit_new_progress": 0,
                "correction_delete_redate": True, "oversized_key_terminal": True, "time_expiry_terminal": True}


def main():
    now = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=7, now=now)
    with fixture, tempfile.TemporaryDirectory(prefix="vitrina-live-candidate-") as temp, ExitStack() as resources:
        root = Path(temp)
        runtime = fixture.entrypoint.runtime
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")  # Owned synthetic fixture only.
            ensure_publication_schema(conn)
            bundle = conn.execute("SELECT bundle_version FROM registry_upload_current_state WHERE slot=1").fetchone()[0]
            conn.execute("""INSERT INTO sheet_vitrina_v1_ready_publications
                (operation_id,attempt_id,kind,bundle_version,as_of_date,expected_digest,
                 inputs_json,book_required,ready_required,state,after_digest,created_at,finished_at)
                VALUES('bridge-diagnostics','attempt','book_ready',?,'2026-04-20',
                       'expected','{}',0,1,'complete','after','created','finished')""", (bundle,))
        keeper = resources.enter_context(closing(sqlite3.connect(runtime.db_path)))
        keeper.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()  # Own fixture provisions live WAL sidecars.
        adapter = LiveNativeAdapter(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir,
            cache_dir=root / "cache", now=now, date_from="2026-04-14", date_to="2026-04-20",
            formula_epoch="native-fixture-live-v1")
        vector = adapter.capture()
        assert adapter.stats["plans_loaded"] == 7
        assert adapter.capture() == vector and adapter.stats["plans_loaded"] == 0
        # Actual native source + compiler + store: bulk exceeds one capture,
        # while each completed source day remains inside the same budget.
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_business_projection_current_rows(
                as_of_date TEXT NOT NULL,nm_id INTEGER NOT NULL,revision_id TEXT NOT NULL,
                metrics_json TEXT NOT NULL,presentation_json TEXT NOT NULL,provenance_json TEXT NOT NULL,
                row_fingerprint TEXT NOT NULL,published_at TEXT NOT NULL,PRIMARY KEY(as_of_date,nm_id))""")
            conn.executemany("""INSERT INTO sheet_vitrina_v1_warehouse_business_projection_current_rows
                VALUES(?,999,'proof-fixture','{}','{}',?,'proof-fixture','2026-04-20')""",
                [(day, json.dumps({"unconsumed_fixture_padding": "x" * 128 * 1024}))
                 for day in ("2026-04-18", "2026-04-19")])
            ensure_publication_schema(conn)
        for day in ("2026-04-16", "2026-04-17"):
            runtime.save_temporal_source_snapshot(source_key=SOURCES[1], snapshot_date=day,
                captured_at=day + "T12:00:00Z",
                payload={"unconsumed_fixture_padding": "x" * 128 * 1024})
        adapter.max_read_bytes = 224 * 1024
        store = HistoryStore(root / "history", max_reply_bytes=32 * 1024**2)
        built = update_live_history(adapter=adapter, runtime=runtime, store=store,
                                    deadline_monotonic=time.monotonic() + 30)
        assert built["status"] == "published", built
        assert built["bootstrap_source_reads"]["captures"] >= 3, built
        assert built["capture_calls"] == built["bootstrap_source_reads"]["captures"] + 2, built
        assert built["bootstrap_source_reads"]["temporal_proofs_loaded"] >= 2, built
        assert store._current()["current"] == built["edition_id"]
        warming = built["bootstrap_source_reads"]
        vector = store.edition()["consumed"]
        check_initial_warming_failures(adapter, runtime, store)
        adapter.max_read_bytes = 32 * 1024**2
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=?,inputs_json=? WHERE operation_id='bridge-diagnostics'",
                         ('{"diagnostic_only":"updated"}', '{"ordinary_intent":"not_rendered"}'))
        assert adapter.capture() == vector
        nochange = update_live_history(adapter=adapter, runtime=runtime, store=store)
        assert nochange["status"] == "unchanged" and not nochange["compiler_constructed"]
        previous = store.edition()
        context = deepcopy(adapter.context)
        adapter.now += timedelta(hours=1)
        advanced = adapter.capture()
        assert changed(vector, advanced) == {"2026-04-19", "2026-04-20"}
        assert advanced["epoch"] == vector["epoch"] and adapter.context == context
        current_calls = []
        compile_original = NativeDatedCompiler.compile
        def current_compile(compiler, day):
            current_calls.append(day)
            return compile_original(compiler, day)
        with patch.object(NativeDatedCompiler, "compile", current_compile):
            current_update = update_live_history(adapter=adapter, runtime=runtime, store=store)
        assert current_update["status"] == "published" and current_calls == ["2026-04-19", "2026-04-20"]
        current = store.edition()
        assert current["catalog"] == previous["catalog"]
        assert all(current["days"][day] == previous["days"][day] for day in adapter.days[:-2])
        vector = advanced
        # A historic semantic-only correction is a dated dependency.
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            encoded = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-04-15'").fetchone()[0]
            plan = json.loads(encoded)
            plan.setdefault("metadata", {})["bridge_semantic_receipt"] = "corrected"
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date='2026-04-15'", (json.dumps(plan),))
        correction = adapter.capture()
        assert vector["epoch"] == correction["epoch"]
        assert changed(vector, correction) == {"2026-04-15"}
        previous = store.edition()
        compile_original = NativeDatedCompiler.compile
        writes = []
        def source_aba(compiler, day):
            unit = compile_original(compiler, day)
            if not writes:
                with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
                    before = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-04-14'").fetchone()[0]
                    altered = json.loads(before)
                    altered.setdefault("metadata", {})["bridge_concurrent_change"] = True
                    conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date='2026-04-14'", (json.dumps(altered),))
                    conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date='2026-04-14'", (before,))
                writes.append(day)
            return unit
        with patch.object(NativeDatedCompiler, "compile", source_aba):
            superseded = update_live_history(adapter=adapter, runtime=runtime, store=store)
        assert superseded["status"] == "superseded" and store.edition() == previous
        correction = adapter.capture()
        # Moving an old key is detected even when native UPDATE only bumps NEW.
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute("DELETE FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-04-16'")
        deleted = adapter.capture()
        assert changed(correction, deleted) == {"2026-04-16"}
        try:
            update_live_history(adapter=adapter, runtime=runtime, store=store)
        except LiveSourceUnavailable as exc:
            assert "accepted_ready_source_disappeared:2026-04-16" in str(exc)
        else:
            raise AssertionError("ambiguous source GC must retain accepted derived history")
        assert store.edition() == previous
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            row = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-04-15'").fetchone()[0]
            plan = json.loads(row)
            plan["date_columns"] = ["2026-04-16"]
            plan["as_of_date"] = "2026-04-16"
            for sheet in plan["sheets"]:
                if sheet["sheet_name"] == "DATA_VITRINA":
                    sheet["header"][2:] = ["2026-04-16"]
            for slot in plan["temporal_slots"]:
                slot["column_date"] = "2026-04-16"
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET as_of_date='2026-04-16',plan_json=? WHERE as_of_date='2026-04-15'", (json.dumps(plan),))
        # Use a vacant key: preserve the original plan's dated column unchanged.
        redated = adapter.capture()
        assert changed(deleted, redated) == {"2026-04-15", "2026-04-16"}
        # Real native effective selector: a later unrelated block cannot hide winner changes.
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            ensure_calculation_parameters_schema(conn)
            conn.execute("INSERT INTO sheet_vitrina_v1_calculation_parameter_versions VALUES(?,?,?,?,?,?,?,?,?)",
                ("bridge-param", PROXY_BLOCK_KEY, 999, "2026-04-18", "{}", "p", "manual", "fixture", "2026-04-18T00:00:00Z"))
            conn.execute("INSERT INTO sheet_vitrina_v1_calculation_parameter_versions VALUES(?,?,?,?,?,?,?,?,?)",
                ("unrelated", "OTHER", 1, "2026-04-20", "{}", "p", "manual", "fixture", "2026-04-20T00:00:00Z"))
        parameters = adapter.capture()
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_calculation_parameter_versions SET rates_json='{} ' WHERE version_id='bridge-param'")
        param_corrected = adapter.capture()
        assert changed(parameters, param_corrected) == {"2026-04-18", "2026-04-19", "2026-04-20"}
        # Native Jul1 mapping makes a single dated cost correction affect pre-Jul1 days.
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_wb_daily_cost(cutover_id TEXT,as_of_date TEXT,nm_id INTEGER,quantity TEXT,wac_rub TEXT,capital_rub TEXT,quality TEXT,provenance_json TEXT,fingerprint TEXT,created_at TEXT)")
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost(cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,quality,provenance_json,fingerprint,created_at) VALUES('warehouse_functional_cutover_v1','2026-07-01',999,'1','42','42','certified','{}','cost-v1','2026-07-01T00:00:00Z')")
            ensure_publication_schema(conn)
        cost = adapter.capture()
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost SET wac_rub='43' WHERE as_of_date='2026-07-01'")
        cost_corrected = adapter.capture()
        assert changed(cost, cost_corrected) == set(adapter.days)
        # Exact consumed projected Finance semantics: FIRST duplicate daily item.
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS wb_finance_weekly_sku_aggregates(nm_id TEXT,week_start TEXT,week_end TEXT,calculated_at TEXT,coverage_json TEXT)")
            conn.execute("INSERT INTO wb_finance_weekly_sku_aggregates(seller_id,nm_id,week_start,week_end,calculated_at,coverage_json,formula_version,metrics_json,raw_source_digest,week_content_hash,cost_state_hash,raw_row_count) VALUES('fixture','999','2026-04-14','2026-04-20','2026-04-20T00:00:00Z',?,'fixture','{}','source','week','cost',2)",
                (json.dumps({"daily_rows": [{"operation_date": "2026-04-18", "profit": 1}, {"operation_date": "2026-04-18", "profit": 2}]}),))
        finance = adapter.capture()
        with closing(sqlite3.connect(runtime.db_path)) as conn, conn:
            conn.execute("UPDATE wb_finance_weekly_sku_aggregates SET coverage_json=? WHERE nm_id='999'",
                (json.dumps({"daily_rows": [{"operation_date": "2026-04-18", "profit": 3}, {"operation_date": "2026-04-18", "profit": 2}]}),))
        finance_corrected = adapter.capture()
        assert changed(finance, finance_corrected) == {"2026-04-18"}
        ready_binding_serialization = check_ready_binding_serialization(root / "ready-binding-test")
        check_pending_rollover(root / "pending-test")
        check_quality_floor(root / "quality-test")
        check_source_guards(root / "family-test")
        check_quality_cache_and_temporal(root / "cache-test")
        quality_bridge = check_quality_scope_bridge(root / "quality-bridge-test")
        publication_projection = check_publication_projection(root / "publication-test")
        dated_slice_progress = check_dated_slice_progress(root / "dated-proof-test")
        temporal_progress = check_temporal_progress(root / "temporal-proof-test")
        cli_root = root / "cli-test"
        cli_root.mkdir()
        check_cli_guards(cli_root)
        safe_failure = check_safe_failure_parent()
        started = time.monotonic()
        killed = bounded_worker([sys.executable, "-c", "import time;time.sleep(5)"], 0.1)
        assert killed["status"] == "skipped_deadline" and time.monotonic() - started < 2
        print(json.dumps({"status": "pass", "nochange_before_compiler": True,
                          "ready_binding_serialization": ready_binding_serialization,
                          "current_clock_dirty_dates": 2, "historic_semantic_dirty_dates": 1,
                          "historic_delete_dirty_dates": 1, "pending_rollover_reused": True,
                          "source_aba_superseded": True, "parameter_suffix_days": 3,
                          "cost_july_backward_days": 7, "finance_first_duplicate_days": 1,
                          "native_quality_floor": True, "reusable_quality_bridge": quality_bridge, "portion_capture_calls": 3,
                          "publication_projection": publication_projection,
                          "dated_slice_progress": dated_slice_progress,
                          "temporal_progress": temporal_progress,
                          "native_initial_warming": warming,
                          "warming_no_progress_terminal_deadline_retains_lastgood": True,
                          "publication_diagnostic_nochange_before_compiler": True,
                          "cli_guards": True, "safe_failure": safe_failure,
                          "existing_process_wrapper_kill": True}))


if __name__ == "__main__":
    main()
