"""Owned SQLite fixtures; compare complete coverage against original 0c76 native.

The tracked source-only oracle works in shallow checkouts and source archives;
never constructs runtime/source services or reads production business data.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application import ff_pool_fbs_lifecycle as native

BASE = "0c761f9910608057e024d831930ca07841976dda"
PATH = "packages/application/ff_pool_fbs_lifecycle.py"
BUDGET = 128 * 1024**2
ORACLE = Path(__file__).with_name("fixtures") / "ff_pool_fbs_lifecycle_quality_native_0c76.json"


def _ast_digest(node):
    def normalized(value):
        if isinstance(value, ast.AST):
            # Python3.12 added empty type_params to FunctionDef. These original
            # helpers have no type parameters; retain all other semantic fields.
            return [type(value).__name__, [[name, normalized(item)] for name, item in ast.iter_fields(value)
                                          if name != "type_params" or item]]
        if isinstance(value, list):
            return [normalized(item) for item in value]
        if value is Ellipsis:
            return ["literal", "Ellipsis"]
        return value
    return hashlib.sha256(json.dumps(normalized(node), ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def original():
    oracle = json.loads(ORACLE.read_text())
    assert oracle["contract"] == "original_lifecycle_quality_oracle_v1"
    assert oracle["baseline_revision"] == BASE and oracle["source_path"] == PATH
    text = oracle["function_source"]
    assert hashlib.sha256(text.encode()).hexdigest() == oracle["function_source_sha256"]
    old = {n.name: n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)}
    assert set(old) == {"fbs_lifecycle_quality_coverage"}
    current = {n.name: n for n in ast.parse(Path(native.__file__).read_text()).body
               if isinstance(n, ast.FunctionDef)}
    for name in ("_lifecycle_quality_scopes", "_map_order", "_canonical_mapping_for_identity",
                 "_lifecycle_quality_cursor", "_fingerprint"):
        assert _ast_digest(current[name]) == oracle["canonical_ast_sha256"][name], name + " formula changed"
    namespace = dict(vars(native))
    exec(compile(ast.Module(body=[old["fbs_lifecycle_quality_coverage"]], type_ignores=[]),
                 "original-0c76-native", "exec"), namespace)
    return namespace["fbs_lifecycle_quality_coverage"]


def fixture():
    conn = sqlite3.connect(":memory:")
    schemas = {
        native.IDENTITY_PENDING_TABLE: "pending_id TEXT,cutover_id TEXT,source_status_observation_sequence INTEGER,deferred_identity_evidence_sequence INTEGER",
        native.IDENTITY_PENDING_RESOLUTIONS_TABLE: "pending_id TEXT",
        native.STATUS_OBSERVATIONS_TABLE: "observation_sequence INTEGER PRIMARY KEY,order_id INTEGER,order_revision TEXT,status_digest TEXT,supplier_status TEXT,wb_status TEXT,positive_quantity INTEGER,observed_at TEXT",
        native.OBSERVATIONS_TABLE: "observation_sequence INTEGER,observation_id TEXT,order_id INTEGER,source_revision TEXT,source_created_at TEXT,observed_at TEXT,warehouse_id INTEGER,nm_id INTEGER,chrt_id INTEGER,skus_json TEXT,office_id INTEGER,seller_sku TEXT",
        native.DRAIN_STATE_TABLE: "cutover_id TEXT,last_status_observation_sequence INTEGER,run_count INTEGER",
        "sheet_vitrina_v1_ff_pool_cutover_manifests": "cutover_id TEXT,cutover_at TEXT,manifest_json TEXT",
        native.IDENTITY_EVIDENCE_TABLE: "evidence_sequence INTEGER PRIMARY KEY,evidence_id TEXT,order_id INTEGER,order_revision TEXT,outcome TEXT,warehouse_id INTEGER,nm_id INTEGER,chrt_id INTEGER,warehouse_mapping_id TEXT,identity_mapping_id TEXT,barcode TEXT,seller_sku TEXT",
        native.WAREHOUSE_MAPPINGS_TABLE: "mapping_id TEXT,seller_warehouse_id INTEGER,facility_id TEXT,active INTEGER",
        native.IDENTITY_MAPPINGS_TABLE: "mapping_id TEXT,source_nm_id INTEGER,source_chrt_id INTEGER,source_barcode TEXT,source_sku TEXT,target_nm_id INTEGER,active INTEGER",
        native.FACILITIES_TABLE: "facility_id TEXT,active INTEGER",
        native.MAPPING_EXTENSIONS_TABLE: "extension_id TEXT,cutover_id TEXT,seller_warehouse_id INTEGER,official_office_id INTEGER,facility_id TEXT,warehouse_mapping_id TEXT",
        native.MAPPING_EXTENSION_ALLOCATIONS_TABLE: "extension_id TEXT,nm_id INTEGER,quantity INTEGER",
        native.BALANCES_TABLE: "facility_id TEXT,pool TEXT,nm_id INTEGER,projection_epoch INTEGER,quantity INTEGER,wac_rub TEXT",
        native.NOMENCLATURE_TABLE: "nm_id INTEGER,is_active INTEGER,is_hidden INTEGER",
    }
    for table, columns in schemas.items():
        conn.execute(f"CREATE TABLE {table}({columns})")
    conn.execute(f"CREATE INDEX evidence_orders ON {native.IDENTITY_EVIDENCE_TABLE}(order_id,order_revision)")
    conn.execute("INSERT INTO sheet_vitrina_v1_ff_pool_cutover_manifests VALUES('cut','2026-08-01',?)",
                 (json.dumps({"cutover_id": "cut", "feature_epoch": 7}),))
    conn.execute(f"INSERT INTO {native.DRAIN_STATE_TABLE} VALUES('cut',0,1)")
    conn.execute(f"INSERT INTO {native.FACILITIES_TABLE} VALUES('ff',1)")
    conn.execute(f"INSERT INTO {native.WAREHOUSE_MAPPINGS_TABLE} VALUES('wh',10,'ff',1)")
    for order, nm, day in ((1, 111, 15), (2, 222, 16), (3, 333, 17), (4, 444, 18), (5, 555, 19)):
        add_order(conn, order, nm, f"2026-08-{day:02}")
    for name, source, target in (("m1", 111, 101), ("m2", 222, 102), ("m4a", 444, 201), ("m4b", 444, 202)):
        conn.execute(f"INSERT INTO {native.IDENTITY_MAPPINGS_TABLE} VALUES(?,?,1,'barcode','sku',?,1)",
                     (name, source, target))
    conn.executemany(f"INSERT INTO {native.IDENTITY_MAPPINGS_TABLE} VALUES(?,555,1,'barcode','sku',?,1)",
                     [(f"many-{n}", 300 + n) for n in range(65)])
    for sequence, order, revision, nm, mapping in ((1, 1, "r1", 111, "m1"),
            (2, 2, "r2", 222, "m2"), (10, 2, "r2", 222, "m2"), (11, 2, "later", 222, "m2")):
        add_identity(conn, sequence, order, revision, nm, mapping)
    conn.execute(f"INSERT INTO {native.IDENTITY_PENDING_TABLE} VALUES('pending','cut',2,9)")
    for target in (101, 102):
        conn.execute(f"INSERT INTO {native.BALANCES_TABLE} VALUES('ff','FBS',?,7,42,'1')", (target,))
        conn.execute(f"INSERT INTO {native.NOMENCLATURE_TABLE} VALUES(?,1,0)", (target,))
    conn.commit()
    return conn


def add_order(conn, order, nm, day, sequence=None):
    seq = order if sequence is None else sequence
    conn.execute(f"INSERT INTO {native.STATUS_OBSERVATIONS_TABLE} VALUES(?,?,?,'status','new','new',1,?)",
                 (seq, order, f"r{order}", day + "T12:00:00Z"))
    conn.execute(f"INSERT INTO {native.OBSERVATIONS_TABLE} VALUES(?,?,?, ?,?,?,10,?,1,'[\"barcode\"]',20,'sku')",
                 (seq, f"obs{seq}", order, f"r{order}", day + "T00:00:00Z", day + "T12:00:00Z", nm))


def add_identity(conn, seq, order, revision, nm, mapping):
    conn.execute(f"INSERT INTO {native.IDENTITY_EVIDENCE_TABLE} VALUES(?,?,?,?, 'matched',10,?,1,'wh',?,'barcode','sku')",
                 (seq, f"ev{seq}", order, revision, nm, mapping))


@contextmanager
def pinned(conn):
    conn.commit()
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.rollback()
        conn.execute("PRAGMA query_only=OFF")


def parity(conn, oracle, cache, *, dates=None):
    dates = dates or ["2026-08-14", "2026-08-16", "2026-08-19", "2026-09-01", "2026-09-03"]
    with pinned(conn):
        resolver = native.reusable_fbs_lifecycle_quality_resolver(
            conn, scope_cache=cache, max_cache_bytes=BUDGET)
        expected = {(day, str(requested)): oracle(conn, as_of_date=day, requested_nm_ids=requested)
                    for day in dates for requested in (None, [], [101], [102, 201], [999])}
        for day in dates:
            for requested in (None, [], [101], [102, 201], [999]):
                actual = resolver(as_of_date=day, requested_nm_ids=requested)
                assert actual == expected[(day, str(requested))], (day, requested, actual)
                assert native.fbs_lifecycle_quality_coverage(conn, as_of_date=day, requested_nm_ids=requested) == actual
        # Repeat just the reusable path to obtain its independent native call count.
        resolver = native.reusable_fbs_lifecycle_quality_resolver(
            conn, scope_cache=cache, max_cache_bytes=BUDGET)
        with patch.object(native, "_lifecycle_quality_scopes", wraps=native._lifecycle_quality_scopes) as scopes:
            for day in dates:
                resolver(as_of_date=day)
            assert scopes.call_count == 0


def check_calls(conn, cache, expected, dates=None):
    with pinned(conn):
        queries = []
        conn.set_trace_callback(queries.append)
        resolver = native.reusable_fbs_lifecycle_quality_resolver(conn, scope_cache=cache, max_cache_bytes=BUDGET)
        with patch.object(native, "_lifecycle_quality_scopes", wraps=native._lifecycle_quality_scopes) as scopes:
            for day in dates or ["2026-08-19", "2026-09-01", "2026-09-03"]:
                for requested in (None, [101], [999]):
                    resolver(day, requested)
            assert scopes.call_count == expected, (scopes.call_count, expected)
        conn.set_trace_callback(None)
        assert sum(query.startswith("WITH requested(") for query in queries) == 1
        return resolver


class OverflowConnection:
    def __init__(self, conn, branch):
        self.conn, self.branch = conn, branch
        self.new_queries = 0

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, args=()):
        pending = "resolution.pending_id IS NULL" in sql
        new = "pending.pending_id IS NULL" in sql
        if new:
            self.new_queries += 1
        if (self.branch == "pending" and pending) or (self.branch == "new" and new):
            class Rows:
                def fetchall(self):
                    return [None] * 100001
            return Rows()
        return self.conn.execute(sql, args)


def check_read_hook(oracle):
    conn, cache, queries = fixture(), {}, []
    charged = 0
    with pinned(conn):
        changes = conn.total_changes

        def read(sql, args=()):
            nonlocal charged
            queries.append(sql)
            rows = []
            for row in conn.execute(sql, args):
                value = list(row)
                charged += len(json.dumps(value, ensure_ascii=False, default=str).encode())
                rows.append(value)
            return rows

        resolver = native.reusable_fbs_lifecycle_quality_resolver(
            conn, scope_cache=cache, max_cache_bytes=BUDGET, read_rows=read)
        for day in ("2026-08-16", "2026-08-19", "2026-09-01"):
            assert resolver(day, [101, 102]) == oracle(conn, as_of_date=day, requested_nm_ids=[101, 102])
        assert charged > 0 and conn.total_changes == changes
        assert sum(sql.startswith("WITH requested(") for sql in queries) == 1
        assert any("resolution.pending_id IS NULL" in sql for sql in queries)
        assert any("pending.pending_id IS NULL" in sql for sql in queries)
        assert any("SELECT manifest_json" in sql for sql in queries)
        assert any("SELECT name FROM sqlite_master" in sql for sql in queries)
        assert any("SELECT facility_id,nm_id,projection_epoch" in sql for sql in queries)
        # Same streaming accounting style as adapter._rows: abort the read
        # immediately when its caller byte cap is crossed.
        def capped(sql, args=()):
            rows, size = [], 0
            for row in conn.execute(sql, args):
                size += len(json.dumps(list(row)).encode())
                if size > 1:
                    raise RuntimeError("owned_fixture_capture_byte_cap")
                rows.append(row)
            return rows
        untouched = {}
        try:
            native.reusable_fbs_lifecycle_quality_resolver(
                conn, scope_cache=untouched, max_cache_bytes=BUDGET, read_rows=capped)
            raise AssertionError("capture cap was ignored")
        except RuntimeError as exc:
            assert str(exc) == "owned_fixture_capture_byte_cap"
        assert untouched == {}

        def deadline(sql, args=()):
            if sql.startswith("WITH requested("):
                raise TimeoutError("owned_fixture_proof_deadline")
            return [list(row) for row in conn.execute(sql, args)]
        resolver = native.reusable_fbs_lifecycle_quality_resolver(
            conn, scope_cache=untouched, max_cache_bytes=BUDGET, read_rows=deadline)
        try:
            resolver("2026-09-01")
            raise AssertionError("proof deadline was ignored")
        except TimeoutError as exc:
            assert str(exc) == "owned_fixture_proof_deadline"
        assert untouched == {} and conn.total_changes == changes
    conn.close()


def main():
    oracle = original()
    check_read_hook(oracle)
    conn, cache = fixture(), {}
    check_calls(conn, cache, 5)
    parity(conn, oracle, cache)
    check_calls(conn, cache, 0)
    # Fresh status/cursor diagnostic churn and future/current identities must
    # not invalidate closed scopes. Membership is still reloaded each portion.
    add_order(conn, 9, 999, "2026-10-04", sequence=90)
    conn.execute(f"UPDATE {native.DRAIN_STATE_TABLE} SET run_count=run_count+1")
    add_identity(conn, 90, 9, "r9", 999, "future")
    check_calls(conn, cache, 0)
    parity(conn, oracle, cache)
    add_identity(conn, 12, 1, "r1", 111, "m1")
    check_calls(conn, cache, 1)
    parity(conn, oracle, cache)
    conn.execute(f"UPDATE {native.BALANCES_TABLE} SET quantity=123,wac_rub='99'")
    check_calls(conn, cache, 0)
    conn.execute(f"DELETE FROM {native.BALANCES_TABLE} WHERE nm_id=101")
    check_calls(conn, cache, 1)
    parity(conn, oracle, cache)
    conn.execute(f"UPDATE {native.IDENTITY_MAPPINGS_TABLE} SET active=0 WHERE mapping_id='m1'")
    check_calls(conn, cache, 1)
    parity(conn, oracle, cache)
    conn.execute(f"UPDATE {native.NOMENCLATURE_TABLE} SET is_hidden=1 WHERE nm_id=102")
    check_calls(conn, cache, 1)
    parity(conn, oracle, cache)
    conn.execute(f"UPDATE {native.IDENTITY_PENDING_TABLE} SET deferred_identity_evidence_sequence=11")
    check_calls(conn, cache, 1)
    parity(conn, oracle, cache)
    conn.execute(f"INSERT INTO {native.IDENTITY_PENDING_RESOLUTIONS_TABLE} VALUES('pending')")
    check_calls(conn, cache, 0)  # Resolved pending disappears, it does not become new.
    parity(conn, oracle, cache)
    conn.execute(f"UPDATE {native.DRAIN_STATE_TABLE} SET last_status_observation_sequence=5")
    parity(conn, oracle, cache)
    admission, admission_cache = fixture(), {}
    check_calls(admission, admission_cache, 5)
    # Native admission uses cardinality==1, not mere membership. Deleting a
    # balance and then admitting via an allocation must refresh the proof.
    admission.execute(f"DELETE FROM {native.BALANCES_TABLE} WHERE nm_id=102")
    check_calls(admission, admission_cache, 1)
    admission.execute(f"INSERT INTO {native.MAPPING_EXTENSIONS_TABLE} VALUES('ext','cut',10,20,'ff','wh')")
    admission.execute(f"INSERT INTO {native.MAPPING_EXTENSION_ALLOCATIONS_TABLE} VALUES('ext',102,1)")
    check_calls(admission, admission_cache, 5)
    parity(admission, oracle, admission_cache)
    admission.execute(f"INSERT INTO {native.MAPPING_EXTENSION_ALLOCATIONS_TABLE} VALUES('ext',102,1)")
    check_calls(admission, admission_cache, 1)
    parity(admission, oracle, admission_cache)
    admission.execute(f"INSERT INTO {native.BALANCES_TABLE} VALUES('ff','FBS',101,7,0,'0')")
    check_calls(admission, admission_cache, 1)
    parity(admission, oracle, admission_cache)
    admission.close()
    for branch in ("pending", "new"):
        fresh = fixture()
        with pinned(fresh):
            wrapper = OverflowConnection(fresh, branch)
            old = oracle(wrapper, as_of_date="2020-01-01", requested_nm_ids=[999])
            new = native.fbs_lifecycle_quality_coverage(wrapper, as_of_date="2020-01-01", requested_nm_ids=[999])
            resolver = native.reusable_fbs_lifecycle_quality_resolver(wrapper, scope_cache={}, max_cache_bytes=BUDGET)
            assert resolver(as_of_date="2020-01-01", requested_nm_ids=[999]) == new == old
            assert ("status_sequence_count" in old["groups"][0]) == (branch == "new")
            if branch == "pending":
                assert wrapper.new_queries == 0
        fresh.close()
    # No required tables and malformed/absent manifest retain native semantics.
    empty = sqlite3.connect(":memory:")
    parity(empty, oracle, {})
    for manifest in (None, "[]", '{"cutover_id":""}'):
        fresh = fixture()
        fresh.execute("DELETE FROM sheet_vitrina_v1_ff_pool_cutover_manifests")
        if manifest is not None:
            fresh.execute("INSERT INTO sheet_vitrina_v1_ff_pool_cutover_manifests VALUES('cut','now',?)", (manifest,))
        parity(fresh, oracle, {})
        fresh.close()
    with pinned(conn):
        resolver = native.reusable_fbs_lifecycle_quality_resolver(conn, scope_cache={}, max_cache_bytes=0)
        assert resolver(as_of_date="2026-09-01") == oracle(conn, as_of_date="2026-09-01")
    empty.close()
    conn.close()
    print(json.dumps({"status": "PASS", "baseline": BASE,
        "full_coverage_and_digest_parity": True, "cold_scope_calls": 5,
        "warm_scope_calls": 0, "current_only_churn_scope_calls": 0,
        "selective_identity_and_mapping_invalidation": True,
        "extension_allocation_and_balance_cardinality": True,
        "bulk_identity_queries_per_portion": 1,
        "bulk_read_hook_parity_byte_cap_and_deadline_propagation": True,
        "native_pending_and_new_100001_overflow": True,
        "scope": "owned fixtures only; no production latency claim"}))


if __name__ == "__main__":
    main()
