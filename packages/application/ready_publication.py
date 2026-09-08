"""Exact, connection-owned publication of the existing dated ready row.

Publication helpers never open a writer, commit, retry or relax a domain approval.
Additive schema bootstrap alone completes its own setup transaction if needed.
Recovery callers retain their source guards and receipt in their own transaction.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

TABLE = "sheet_vitrina_v1_ready_snapshots"


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class ReadyPublicationConflict(ValueError):
    """The prepared target or a consumed input is no longer current."""


@dataclass(frozen=True)
class ExpectedReady:
    bundle_version: str
    as_of_date: str
    plan_json: str | None

    @property
    def exists(self) -> bool:
        return self.plan_json is not None

    @property
    def fingerprint(self) -> str:
        return digest(canonical([self.bundle_version, self.as_of_date, self.exists, self.plan_json]))


@contextmanager
def readonly(db_path):
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn


def capture_expected(conn, *, bundle_version: str, as_of_date: str) -> ExpectedReady:
    row = conn.execute(f"SELECT plan_json FROM {TABLE} WHERE bundle_version=? AND as_of_date=?",
                       (bundle_version, as_of_date)).fetchone()
    return ExpectedReady(bundle_version, as_of_date, row[0] if row else None)


def check_expected(conn, expected: ExpectedReady) -> None:
    if not isinstance(expected, ExpectedReady):
        raise TypeError("exact_expected_ready_required")
    if capture_expected(conn, bundle_version=expected.bundle_version,
                        as_of_date=expected.as_of_date) != expected:
        raise ReadyPublicationConflict("ready_target_changed:" + expected.bundle_version + ":" + expected.as_of_date)


def replace_ready(conn, *, expected: ExpectedReady, plan_json: str,
                  activated_at: str | None = None, snapshot_id: str | None = None,
                  plan_version: str | None = None, refreshed_at: str | None = None):
    """CAS exact bytes, preserving the surrounding domain transaction."""
    if not conn.in_transaction:
        raise ValueError("ready_publication_requires_owner_transaction")
    check_expected(conn, expected)
    values = {"plan_json": plan_json, "activated_at": activated_at, "snapshot_id": snapshot_id,
              "plan_version": plan_version, "refreshed_at": refreshed_at}
    if expected.exists:
        fields = {key: value for key, value in values.items() if value is not None}
        result = conn.execute(f"UPDATE {TABLE} SET " + ",".join(key + "=?" for key in fields)
            + " WHERE bundle_version=? AND as_of_date=? AND plan_json=?",
            (*fields.values(), expected.bundle_version, expected.as_of_date, expected.plan_json))
        if result.rowcount != 1:
            raise ReadyPublicationConflict("ready_compare_and_swap_failed")
    else:
        if any(value is None for value in values.values()):
            raise ValueError("ready_first_publication_requires_complete_identity")
        try:
            result = conn.execute(f"INSERT INTO {TABLE}(bundle_version,as_of_date," + ",".join(values)
                + ") VALUES(?,?,?,?,?,?,?)", (expected.bundle_version, expected.as_of_date, *values.values()))
        except sqlite3.IntegrityError as exc:
            raise ReadyPublicationConflict("ready_first_publication_conflict") from exc
    return result


def delete_ready(conn, *, expected: ExpectedReady):
    """Exact undo of a reviewed inserted row, not a historical purge API."""
    if not conn.in_transaction or not expected.exists:
        raise ValueError("ready_delete_requires_owner_transaction_and_after_image")
    check_expected(conn, expected)
    result = conn.execute(f"DELETE FROM {TABLE} WHERE bundle_version=? AND as_of_date=? AND plan_json=?",
                         (expected.bundle_version, expected.as_of_date, expected.plan_json))
    if result.rowcount != 1:
        raise ReadyPublicationConflict("ready_delete_compare_and_swap_failed")
    return result


# Compact, source-owned revisions of the material tables consumed by FBS and
# ready transformations. A commit to a job/log/other module is not input drift.
# Triggers change revisions in the source transaction, including non-HTTP paths.
MATERIAL_TABLES = (
    "registry_upload_current_state", "registry_upload_config_v2", "registry_upload_metrics_v2",
    "registry_upload_formulas_v2", "cost_price_current_state", "cost_price_upload_rows",
    *["sheet_vitrina_v1_" + name for name in (
        "nomenclature_items", "sku_groups", "ff_facilities", "ff_facility_profiles",
        "wb_supplies_fbs_warehouse_facility_mappings", "wb_fbs_warehouse_registry_runs", "wb_fbs_warehouse_registry_rows",
        "wb_fbs_stock_snapshot_runs", "wb_fbs_stock_snapshot_rows", "warehouse_business_operations",
        "ff_pool_movement_lines", "ff_pool_documents", "ff_pool_document_lines",
        "ff_pool_document_expense_lines", "ff_pool_document_relations", "ff_pool_balances",
        "warehouse_functional_active", "warehouse_functional_versions", "warehouse_functional_balances",
        "warehouse_functional_cutovers", "warehouse_functional_events", "warehouse_wb_daily_cost",
        "warehouse_archival_estimate_versions", "warehouse_archival_estimate_rows", "warehouse_archival_estimate_active",
        "warehouse_functional_read_models", "warehouse_wb_snapshots", "warehouse_business_projection_current_rows",
        "calculation_parameter_versions", "proxy_v4_parameter_versions",
        "sku_action_events",
        "inventory_history_captures", "inventory_history_components", "inventory_history_finalizations",
    )],
)
REVISIONS = "sheet_vitrina_v1_ready_input_revisions"
# The existing immutable inventory capture still consumes legacy FBS quality.
# Pin these only at its RO preparation, not before unrelated live API collection.
INVENTORY_PREPARATION_TABLES = tuple("sheet_vitrina_v1_" + name for name in (
    "ff_pool_cutover_manifests", "ff_pool_feature_epochs", "ff_pool_fbs_lifecycle_current",
    "ff_pool_fbs_applicability_events", "ff_pool_fbs_identity_pending",
    "ff_pool_fbs_identity_pending_resolutions", "ff_pool_fbs_drain_state",
    "ff_pool_fbs_forward_generations", "ff_pool_fbs_forward_state",
    "ff_pool_fbs_mapping_extensions", "ff_pool_fbs_mapping_extension_allocations",
    "wb_supplies_fbs_order_observations", "wb_supplies_fbs_status_observations",
    "wb_supplies_fbs_identity_mappings", "wb_supplies_fbs_identity_evidence",
))
PARAMETER_TABLES = tuple(table for table in MATERIAL_TABLES if "parameter_versions" in table)
_CAPTURE = ContextVar("ready_publication_input_capture", default=None)


@contextmanager
def capture_build_inputs(db_path):
    with readonly(db_path) as conn:
        inputs = {"material": capture_material(conn, tables=tuple(
            table for table in MATERIAL_TABLES if table not in PARAMETER_TABLES)),
            "history": [list(row) for row in conn.execute("SELECT * FROM sheet_vitrina_v1_ready_revisions ORDER BY bundle_version,as_of_date")],
            "sources": {}, "consumed": {}, "conflicts": []}
    token = _CAPTURE.set(inputs)
    try:
        yield inputs
    finally:
        _CAPTURE.reset(token)


def pin_parameters(db_path):
    inputs = _CAPTURE.get()
    if inputs is not None:
        with readonly(db_path) as conn:
            captured = capture_material(conn, tables=PARAMETER_TABLES)
            if any(table in inputs["material"] and inputs["material"][table] != value for table, value in captured.items()):
                inputs["conflicts"].append("parameters")
            inputs["material"].update(captured)


def record_source(*, source_key, snapshot_date, snapshot_role=None, row=None, own_write=False):
    """Pin exact persisted input at its read/acceptance boundary, never at finalize."""
    inputs = _CAPTURE.get()
    if inputs is None:
        return
    key = canonical([source_key, snapshot_date, snapshot_role])
    item = {"source_key": source_key, "snapshot_date": snapshot_date, "snapshot_role": snapshot_role,
            "digest": digest(canonical(list(row) if row is not None else None))}
    previous = inputs["consumed"].get(key)
    if previous is not None and previous != item:
        inputs["conflicts"].append(key)
    inputs["sources"][key] = item


def consume_source(*, source_key, snapshot_date):
    inputs = _CAPTURE.get()
    if inputs is None:
        return
    for key, item in inputs["sources"].items():
        if item["source_key"] == source_key and item["snapshot_date"] == snapshot_date:
            previous = inputs["consumed"].get(key)
            if previous is not None and previous != item:
                inputs["conflicts"].append(key)
            inputs["consumed"][key] = item


def check_build_inputs(conn, inputs):
    if not inputs:
        return
    if inputs.get("conflicts"):
        raise ReadyPublicationConflict("ready_source_changed_during_build")
    check_material(conn, inputs["material"])
    if [list(row) for row in conn.execute("SELECT * FROM sheet_vitrina_v1_ready_revisions ORDER BY bundle_version,as_of_date")] != inputs["history"]:
        raise ReadyPublicationConflict("ready_history_changed_during_build")
    for item in inputs["consumed"].values():
        role = item["snapshot_role"]
        table = "temporal_source_snapshots" if role is None else "temporal_source_slot_snapshots"
        row = conn.execute(f"SELECT captured_at,payload_json FROM {table} WHERE source_key=? AND snapshot_date=?"
            + (" AND snapshot_role=?" if role is not None else ""),
            (item["source_key"], item["snapshot_date"], role) if role is not None
            else (item["source_key"], item["snapshot_date"])).fetchone()
        if digest(canonical(list(row) if row is not None else None)) != item["digest"]:
            raise ReadyPublicationConflict("ready_source_changed:" + item["source_key"] + ":" + item["snapshot_date"])


def ensure_publication_schema(conn) -> None:
    """Additive bootstrap only; never called by a read or inside an apply."""
    ensure_material_revisions(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ready_temporal_revisions(
        source_key TEXT NOT NULL,snapshot_date TEXT NOT NULL,snapshot_role TEXT NOT NULL,
        revision INTEGER NOT NULL,PRIMARY KEY(source_key,snapshot_date,snapshot_role))""")
    for table, role in (("temporal_source_snapshots", "''"), ("temporal_source_slot_snapshots", "{row}.snapshot_role")):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        for event in ("INSERT", "UPDATE", "DELETE"):
            row = "old" if event == "DELETE" else "new"
            when = " WHEN old.captured_at IS NOT new.captured_at OR old.payload_json IS NOT new.payload_json" if event == "UPDATE" else ""
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS ready_temporal_{table}_{event.lower()} AFTER {event} ON {table}{when} BEGIN "
                f"INSERT INTO sheet_vitrina_v1_ready_temporal_revisions VALUES({row}.source_key,{row}.snapshot_date,{role.format(row=row)},1) "
                "ON CONFLICT(source_key,snapshot_date,snapshot_role) DO UPDATE SET revision=revision+1; END")
    conn.execute("""CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ready_publications(
        operation_id TEXT NOT NULL,attempt_id TEXT NOT NULL,kind TEXT NOT NULL,
        bundle_version TEXT,as_of_date TEXT,expected_digest TEXT NOT NULL,
        inputs_json TEXT NOT NULL,expected_book TEXT,book_operation_id TEXT,
        book_required INTEGER NOT NULL,ready_required INTEGER NOT NULL,
        state TEXT NOT NULL,book_version TEXT,after_digest TEXT,
        created_at TEXT NOT NULL,finished_at TEXT,diagnostics_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY(operation_id,attempt_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ready_revisions(
        bundle_version TEXT NOT NULL,as_of_date TEXT NOT NULL,revision INTEGER NOT NULL,
        PRIMARY KEY(bundle_version,as_of_date))""")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return
    conn.execute(f"INSERT OR IGNORE INTO sheet_vitrina_v1_ready_revisions SELECT bundle_version,as_of_date,0 FROM {TABLE}")
    for event in ("INSERT", "UPDATE", "DELETE"):
        row = "old" if event == "DELETE" else "new"
        when = " WHEN " + " OR ".join(f"old.{column} IS NOT new.{column}" for column in (
            "bundle_version", "as_of_date", "plan_json", "snapshot_id", "plan_version", "refreshed_at", "activated_at")) if event == "UPDATE" else ""
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS ready_history_{event.lower()} AFTER {event} ON {TABLE}{when} BEGIN "
            f"INSERT INTO sheet_vitrina_v1_ready_revisions VALUES({row}.bundle_version,{row}.as_of_date,1) "
            "ON CONFLICT(bundle_version,as_of_date) DO UPDATE SET revision=revision+1; END")


def ensure_material_revisions(conn) -> None:
    """Also called by the three lazy source-schema owners before their first write."""
    was_in_transaction = conn.in_transaction
    conn.execute(f"CREATE TABLE IF NOT EXISTS {REVISIONS}(source_table TEXT PRIMARY KEY,revision INTEGER NOT NULL)")
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in (*MATERIAL_TABLES, *INVENTORY_PREPARATION_TABLES):
        if table not in existing:
            continue
        conn.execute(f"INSERT OR IGNORE INTO {REVISIONS} VALUES(?,0)", (table,))
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
        different = " OR ".join(f'old."{column}" IS NOT new."{column}"' for column in columns)
        for event in ("INSERT", "UPDATE", "DELETE"):
            trigger = "ready_input_" + table + "_" + event.lower()
            when = " WHEN " + different if event == "UPDATE" else ""
            conn.execute(f'CREATE TRIGGER IF NOT EXISTS "{trigger}" AFTER {event} ON "{table}"{when} '
                f"BEGIN UPDATE {REVISIONS} SET revision=revision+1 WHERE source_table='{table}'; END")
    if not was_in_transaction:
        conn.commit()  # Complete only this bootstrap; never commit its owner's write.


def capture_history(conn, bundle_version=None):
    if bundle_version is None:
        return [list(row) for row in conn.execute("SELECT * FROM sheet_vitrina_v1_ready_revisions ORDER BY bundle_version,as_of_date")]
    return dict(conn.execute("SELECT as_of_date,revision FROM sheet_vitrina_v1_ready_revisions WHERE bundle_version=?",
                             (bundle_version,)))


def capture_material(conn, *, tables=MATERIAL_TABLES) -> dict[str, int | None]:
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    revisions = dict(conn.execute(f"SELECT source_table,revision FROM {REVISIONS}"))
    result = {}
    for table in tables:
        if table in names and table not in revisions:
            raise ValueError("ready_source_revision_bootstrap_required:" + table)
        result[table] = revisions.get(table) if table in names else None
    return result


def check_material(conn, expected) -> None:
    current = capture_material(conn, tables=tuple(expected))
    changed = [name for name in expected if current[name] != expected[name]]
    if changed:
        raise ReadyPublicationConflict("ready_material_input_changed:" + ",".join(changed))


def pin_queries(conn, queries):
    """Bounded source reads for reviewed repair adapters without epoch owners.

    SQL and scope are constants supplied by the source adapter. Capture before
    dependent preparation; recheck on its existing writer connection. No writes,
    arbitrary table-wide data_version gate or independent transaction is added.
    """
    result = []
    for sql, params in queries:
        hashed = hashlib.sha256()
        for row in conn.execute(sql, params):
            hashed.update(canonical(list(row)).encode())
            hashed.update(b"\n")
        result.append((sql, tuple(params), "sha256:" + hashed.hexdigest()))
    return result


def check_queries(conn, expected):
    if pin_queries(conn, [(sql, params) for sql, params, _ in expected]) != expected:
        raise ReadyPublicationConflict("ready_repair_source_changed")


def operational_authority(runtime_dir):
    from packages.application.storage_registry import StoreRegistry
    registry = StoreRegistry(Path(runtime_dir))
    manifest = registry.load()
    return registry.resolve("operational", manifest=manifest), manifest.manifest_sha256


def check_authority(runtime_dir, expected):
    if operational_authority(runtime_dir) != expected:
        raise ReadyPublicationConflict("ready_storage_authority_changed")


def pin_proxy_repair_sources(conn):
    """Exact compact source boundary before the existing bounded V4 rebuild."""
    tables = ("registry_upload_current_state", "registry_upload_config_v2",
        "sheet_vitrina_v1_calculation_parameter_versions", "sheet_vitrina_v1_proxy_v4_parameter_versions",
        "wb_finance_weekly_aggregates", "wb_finance_weekly_sync")
    placeholders = ",".join("?" for _ in tables)
    queries = [(f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders}) ORDER BY name", tables),
        ("SELECT * FROM sheet_vitrina_v1_ready_revisions ORDER BY bundle_version,as_of_date", ()),
        ("SELECT * FROM sheet_vitrina_v1_ready_temporal_revisions WHERE source_key='sales_funnel_history' ORDER BY snapshot_date,snapshot_role", ())]
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in tables:
        if table in existing:
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            queries.append((f'SELECT * FROM "{table}" ORDER BY ' + ",".join('"' + c + '"' for c in columns), ()))
    return pin_queries(conn, queries)


def record_intent(conn, *, operation_id, attempt_id, kind, expected, inputs,
                  expected_book, book_required, ready_required, created_at, book_operation_id=None):
    conn.execute("""INSERT INTO sheet_vitrina_v1_ready_publications(
        operation_id,attempt_id,kind,bundle_version,as_of_date,expected_digest,inputs_json,
        expected_book,book_operation_id,book_required,ready_required,state,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        operation_id, attempt_id, kind, expected.bundle_version if expected else None,
        expected.as_of_date if expected else None, expected.fingerprint if expected else digest("null"),
        canonical(inputs), expected_book, (book_operation_id or operation_id + ":" + attempt_id) if book_required else None,
        int(book_required), int(ready_required), "prepared", created_at))


def complete_publication(conn, *, operation_id, attempt_id, book_version, after_digest, finished_at):
    result = conn.execute("""UPDATE sheet_vitrina_v1_ready_publications
        SET state='complete',book_version=?,after_digest=?,finished_at=?
        WHERE operation_id=? AND attempt_id=? AND state='prepared'""",
        (book_version, after_digest, finished_at, operation_id, attempt_id))
    if result.rowcount != 1:
        raise ReadyPublicationConflict("publication_intent_claim_changed")


def publication_status(db_path, *, operation_id, attempt_id):
    with readonly(db_path) as conn:
        row = conn.execute("SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?",
                           (operation_id, attempt_id)).fetchone()
        return dict(row) if row else None
