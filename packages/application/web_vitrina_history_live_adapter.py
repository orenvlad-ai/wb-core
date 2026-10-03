"""Opt-in live RO bridge. No scheduler, HTTP, source writes or raw Finance scan.

Native transactional revisions are alarms, not all-history dependency epochs.
The private cache stores proofs/row headers only, never operational DB copies.
Resource rejection keeps the previous edition; partially warmed headers can be
reused by the next explicit candidate run.
"""
from __future__ import annotations

from contextlib import closing, nullcontext, ExitStack
from dataclasses import asdict
from datetime import datetime, date, timedelta
from pathlib import Path
import json
import sqlite3
import time
import zlib

from packages.application.ready_publication import (
    MATERIAL_TABLES, INVENTORY_PREPARATION_TABLES, ensure_publication_schema,
    capture_authority, check_pinned_authority,
)
from packages.application.web_vitrina_history_compiler import NativeDatedCompiler, digest, dates_between
from packages.application.web_vitrina_history_store import HistoryStore, HistoryUnavailable, _atomic, _read
from packages.application.web_vitrina_window_read_context import window_read_context, active_window_read_context
from packages.application.web_vitrina_window_v3 import (
    _ReadyHeader, _select_bindings, _row_header_keys, _current_supplier_certification_material,
)
from packages.application.sheet_vitrina_v1_web_vitrina import default_business_as_of_date
from packages.application.sheet_vitrina_v1_buyout_percent import (
    SALES_FUNNEL_HISTORY_SOURCE_KEY, BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY,
)
from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
from packages.application.calculation_parameters import PROXY_BLOCK_KEY
from packages.application.calculation_parameters_v4 import PROXY_V4_BLOCK_KEY
from packages.application import ff_pool_fbs_lifecycle as lifecycle
from packages.application.ff_pool_fbs_lifecycle import fbs_lifecycle_quality_coverage
from packages.business_time import current_business_date_iso

PREFIX = "sheet_vitrina_v1_"
DATED_TABLES = {
    PREFIX + "warehouse_business_projection_current_rows": "as_of_date",
    PREFIX + "inventory_history_captures": "business_date",
    PREFIX + "inventory_history_finalizations": "business_date",
    PREFIX + "warehouse_wb_daily_cost": "as_of_date",
    PREFIX + "canonical_cost_daily_state": "as_of_date",
    PREFIX + "wb_cost_daily_state": "as_of_date",
}
STATIC_TABLES = {
    "registry_upload_config_v2", "registry_upload_metrics_v2", "registry_upload_formulas_v2",
    PREFIX + "nomenclature_items", PREFIX + "sku_groups", PREFIX + "ff_facilities",
    PREFIX + "ff_facility_profiles", PREFIX + "warehouse_functional_cutovers",
}
PARAMETERS = (PREFIX + "calculation_parameter_versions", PREFIX + "proxy_v4_parameter_versions")
POLICIES = (".auto-updates-policy.json", ".web-vitrina-fbs-lifecycle-last-good.json")
SOURCES = (SALES_FUNNEL_HISTORY_SOURCE_KEY, BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY)


class LiveSourceUnavailable(HistoryUnavailable):
    pass


def _require_live_sqlite_family(path: Path) -> None:
    """Reject sources whose RO open would need recovery or create WAL sidecars.

    Persistent WAL is allowed only with existing readable WAL/SHM. This is a
    preflight, not permission to switch source journal mode or repair its files.
    """
    try:
        with path.open("rb") as source:
            header = source.read(100)
        if header[:16] != b"SQLite format 3\x00" or len(header) != 100:
            raise LiveSourceUnavailable("live_sqlite_header_unknown:" + path.name)
        journal = Path(str(path) + "-journal")
        if journal.exists() and journal.stat().st_size:
            raise LiveSourceUnavailable("live_sqlite_journal_unknown:" + path.name)
        versions = tuple(header[18:20])
        if versions == (1, 1):
            return
        if versions != (2, 2):
            raise LiveSourceUnavailable("live_sqlite_versions_unknown:" + path.name)
        with Path(str(path) + "-wal").open("rb") as wal:
            wal_header = wal.read(32)
        with Path(str(path) + "-shm").open("rb") as shm:
            shm_header = shm.read(32768)
        if (len(shm_header) != 32768 or
                (wal_header and (len(wal_header) != 32 or
                 wal_header[:4] not in (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83")))):
            raise LiveSourceUnavailable("live_sqlite_wal_family_unknown:" + path.name)
    except OSError as exc:
        raise LiveSourceUnavailable("live_sqlite_family_unavailable:" + path.name) from exc


def _quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_normal(sql: str) -> str:
    return "".join(sql.lower().split()).replace('"', '')


def _verify_revision_triggers(conn, tables: set[str]) -> None:
    """Compare actual alarm trigger bodies to today's native producer, not names."""
    with closing(sqlite3.connect(":memory:")) as expected:
        names = {*MATERIAL_TABLES, *INVENTORY_PREPARATION_TABLES,
                 "sheet_vitrina_v1_ready_snapshots", "temporal_source_snapshots",
                 "temporal_source_slot_snapshots"} & tables
        for name in sorted(names):
            columns = [r[1] for r in conn.execute("PRAGMA table_info(" + _quoted(name) + ")")]
            expected.execute("CREATE TABLE " + _quoted(name) + " (" +
                             ",".join(_quoted(c) + " TEXT" for c in columns) + ")")
        ensure_publication_schema(expected)  # Own in-memory schema ONLY.
        for name, sql in expected.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"):
            actual = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
            if not actual or _sql_normal(actual[0]) != _sql_normal(sql):
                raise LiveSourceUnavailable("native_revision_trigger_unknown:" + name)


class LiveNativeAdapter:
    def __init__(self, *, db_path: Path, runtime_dir: Path, cache_dir: Path,
                 now: datetime, date_from: str, date_to: str, formula_epoch: str,
                 max_read_bytes: int = 32 * 1024**2, max_capture_seconds: float = 20,
                 max_cache_bytes: int = 128 * 1024**2):
        self.db_path = Path(db_path).resolve()
        self.runtime_dir, self.cache_dir = Path(runtime_dir).resolve(), Path(cache_dir).resolve()
        if self.cache_dir.is_relative_to(self.runtime_dir) or not formula_epoch or now.tzinfo is None:
            raise ValueError("separate private cache, formula epoch and explicit timezone required")
        self.now, self.days, self.formula_epoch = now, dates_between(date_from, date_to), formula_epoch
        self.max_read_bytes, self.max_capture_seconds = max_read_bytes, max_capture_seconds
        self.max_cache_bytes = max_cache_bytes
        self.context = self.availability = None
        self.fence = None
        self.stats = {}
        self.capture_calls = 0

    def _rows(self, conn, sql, args=()):
        rows = []
        for row in conn.execute(sql, args):
            value = list(row)
            self.stats["bytes"] += len(json.dumps(value, ensure_ascii=False, default=str).encode())
            if self.stats["bytes"] > self.max_read_bytes or time.monotonic() >= self.deadline:
                raise LiveSourceUnavailable("live_source_resource_limit")
            rows.append(value)
        self.stats["queries"] += 1
        return rows

    def capture(self) -> dict:
        self.capture_calls += 1
        self.stats = {"bytes": 0, "queries": 0, "plans_loaded": 0, "reasons": []}
        self.deadline = time.monotonic() + self.max_capture_seconds
        self.require_source_families()
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._cache_reserve(0)
        cache_file = self.cache_dir / "source-proofs.json"
        if cache_file.exists() and cache_file.stat().st_size > self.max_cache_bytes:
            raise LiveSourceUnavailable("source_cache_resource_limit")
        cache = _read(cache_file) if cache_file.exists() else {}
        self.used_slices, self.used_book = set(), set()
        authority = capture_authority(self.runtime_dir, db_path=self.db_path)
        stat = self.db_path.stat()
        identity = [str(self.db_path), stat.st_dev, stat.st_ino, self.formula_epoch]
        if cache.get("source") != identity:
            cache = {"source": identity, "headers": {}, "slices": {}, "book": {}}
        conn = None
        try:
            active = active_window_read_context()
            with (nullcontext(active) if active else window_read_context(self.db_path, runtime_dir=self.runtime_dir)) as context:
                conn = context.borrow(self.db_path)
                check_pinned_authority(conn, authority)
                if tuple(context.operational_generation[-2:]) != (stat.st_dev, stat.st_ino):
                    raise LiveSourceUnavailable("live_source_replaced_while_pinning")
                conn.set_progress_handler(lambda: int(time.monotonic() >= self.deadline), 1000)
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                _verify_revision_triggers(conn, tables)
                alarms = dict(self._rows(conn, "SELECT source_table,revision FROM sheet_vitrina_v1_ready_input_revisions"))
                for name in {*MATERIAL_TABLES, *INVENTORY_PREPARATION_TABLES} & tables:
                    if name not in alarms:
                        raise LiveSourceUnavailable("native_source_revision_missing:" + name)
                current = self._rows(conn, "SELECT bundle_version FROM registry_upload_current_state WHERE slot=1")
                if len(current) != 1:
                    raise LiveSourceUnavailable("current_bundle_missing")
                identities = self._rows(conn, """SELECT s.bundle_version,s.as_of_date,s.snapshot_id,
                    s.activated_at,s.refreshed_at,r.revision FROM sheet_vitrina_v1_ready_snapshots s
                    LEFT JOIN sheet_vitrina_v1_ready_revisions r USING(bundle_version,as_of_date)
                    ORDER BY s.activated_at DESC,s.refreshed_at DESC,s.as_of_date DESC,s.bundle_version DESC""")
                self.fence = digest({"alarms": alarms, "ready": identities, "authority": authority})
                headers = []
                for item in identities:
                    if item[-1] is None:
                        raise LiveSourceUnavailable("ready_revision_missing")
                    key = digest(item)
                    if key not in cache["headers"]:
                        size = conn.execute("SELECT length(CAST(plan_json AS BLOB)) FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?", item[:2]).fetchone()[0]
                        if size > self.max_read_bytes - self.stats["bytes"]:
                            raise LiveSourceUnavailable("live_ready_bootstrap_pending")
                        encoded = self._rows(conn, "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?", item[:2])[0][0]
                        plan = json.loads(encoded)
                        native_plan = _deserialize_sheet_vitrina_plan(encoded)
                        raw = [r for sheet in native_plan.sheets if sheet.sheet_name == "DATA_VITRINA" for r in sheet.rows if len(r) >= 2]
                        labels = [r[:2] for r in raw]
                        label_id = digest(labels)
                        label_path = self.cache_dir / (label_id + ".rows.json")
                        if not label_path.exists():
                            self._cache_reserve(len(json.dumps(labels, ensure_ascii=False).encode()))
                            _atomic(label_path, labels)
                        cache["headers"][key] = {"dates": native_plan.date_columns, "book": plan.get("metadata", {}).get("fbs_accounting_bindings", {}),
                            "labels": label_id, "legacy": any(str(r[1]).endswith(("|stock_total", "|total_stock_total")) and any(v not in (None, "") for v in r[2:]) for r in raw)}
                        self.stats["plans_loaded"] += 1
                    node = cache["headers"][key]
                    headers.append(_ReadyHeader(*item[:5], tuple(node["dates"]), json.dumps(node["book"]), item[5]))
                business_day = current_business_date_iso(self.now)
                bindings, default = _select_bindings(conn, headers, self.days, current[0][0], default_business_as_of_date(self.now))
                by_key = {h.key: h for h in headers}
                nodes = {h.key: cache["headers"][digest([h.bundle_version, h.as_of_date, h.snapshot_id, h.activated_at, h.refreshed_at, h.revision])] for h in headers}
                labels, seen = [], set()
                keys = _row_header_keys(bindings, default)
                for key in keys:
                    for row in _read(self.cache_dir / (nodes[key]["labels"] + ".rows.json")):
                        if row[1] not in seen:
                            labels.append(row)
                            seen.add(row[1])
                per_day = {day: {} for day in self.days}
                for binding in bindings:
                    per_day[binding.date]["ready"] = asdict(by_key[binding.source_key]) if binding.source_key else None
                    if binding.source_key and PREFIX + "ready_publications" in tables:
                        per_day[binding.date]["publications"] = digest(self._rows(conn,
                            "SELECT * FROM " + PREFIX + "ready_publications WHERE bundle_version=? AND as_of_date=? AND state='complete' AND ready_required=1 ORDER BY operation_id,attempt_id",
                            binding.source_key))
                # All selected material slices retain semantic fields, not just numbers.
                source_dates = sorted(set(self.days) | {"2026-07-01"})
                for name, column in DATED_TABLES.items():
                    rows = self._slice(conn, tables, cache, alarms, name, column, source_dates)
                    for day in self.days:
                        mapped = "2026-07-01" if day < "2026-07-01" and "cost" in name else day
                        per_day[day][name] = rows.get(mapped, digest([]))
                components = PREFIX + "inventory_history_components"
                # Components are immutable under the supported native producer.
                # A changed component alarm forces exact affected capture content proof.
                captures = self._rows(conn, "SELECT capture_id,business_date,facility_roster_json,source_manifest_json,source_digest FROM " + PREFIX + "inventory_history_captures WHERE business_date>=? AND business_date<=? ORDER BY capture_sequence", (self.days[0], self.days[-1])) if components in tables else []
                facilities, scope_ids, history_identities = {}, set(), {}
                for capture_id, day, roster, manifest, source_digest in captures:
                    for facility in json.loads(roster):
                        facilities[facility["facility_id"]] = facility
                    proof_key = components + ":" + capture_id + ":" + source_digest
                    self.used_slices.add(proof_key)
                    if proof_key not in cache["slices"]:
                        rows = self._rows(conn, "SELECT * FROM " + components + " WHERE capture_id=? ORDER BY scope_key,component_kind,component_id", (capture_id,))
                        columns = [r[1] for r in conn.execute("PRAGMA table_info(" + components + ")")]
                        scopes = []
                        for row in rows:
                            value = dict(zip(columns, row))
                            provenance = json.loads(value["provenance_json"])
                            scopes.append([value["scope_key"], provenance.get("identity", {}), value["component_kind"]])
                        cache["slices"][proof_key] = {"proof": digest(rows), "scopes": scopes}
                    proof = cache["slices"][proof_key]
                    per_day[day].setdefault("components", []).append([capture_id, proof["proof"]])
                    if json.loads(manifest).get("contract") == "bound_inventory_quantity_v1":
                        for scope, identity_value, kind in proof["scopes"]:
                            scope_ids.add(scope)
                            if identity_value and kind == "WB":
                                history_identities[scope] = identity_value
                for name, block in zip(PARAMETERS, (PROXY_BLOCK_KEY, PROXY_V4_BLOCK_KEY)):
                    for day in self.days:
                        applicable = name in tables and (name != PARAMETERS[1] or day >= "2026-08-01")
                        rows = self._rows(conn, "SELECT * FROM " + name + " WHERE block_key=? AND effective_date<=? ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1", (block, day)) if applicable else []
                        per_day[day][name] = digest(rows)
                self._temporal(conn, tables, cache, per_day, business_day)
                self._finance(conn, tables, per_day)
                self._archival(conn, tables, per_day)
                self._breakglass(conn, tables, per_day)
                self._book(context, nodes, bindings, cache, per_day, business_day)
                current_alarms = {k: v for k, v in alarms.items() if k not in STATIC_TABLES | set(DATED_TABLES) | set(PARAMETERS) | {components} and not k.startswith("registry_upload_")}
                yesterday = (date.fromisoformat(business_day) - timedelta(days=1)).isoformat()
                current_headers, planning_day = self._current_evidence(conn, tables)
                supplier = _current_supplier_certification_material(conn, tables,
                    business_date=business_day, dates=self.days)
                for day in self.days:
                    age = (date.fromisoformat(business_day) - date.fromisoformat(day)).days
                    per_day[day]["clock"] = {"current": age == 0, "yesterday": age == 1, "mature": age >= 6}
                    if day in {business_day, yesterday, planning_day}:
                        per_day[day]["current_sources"] = current_alarms
                        per_day[day]["current_evidence"] = current_headers
                        per_day[day]["clock"]["bucket"] = self.now.strftime("%Y-%m-%dT%H")
                    if day == business_day:
                        per_day[day]["supplier_certification"] = supplier
                # Untyped inventory actually depends on unresolved lifecycle quality.
                self._quality(conn, cache, captures, per_day, alarms)
                static = {name: digest(self._rows(conn, "SELECT * FROM " + name +
                    (" WHERE bundle_version=? ORDER BY 1" if name.startswith("registry_upload_") else " ORDER BY 1"),
                    (current[0][0],) if name.startswith("registry_upload_") else ())) for name in sorted(STATIC_TABLES & tables)}
                policies = {name: digest((context.read_file_once(self.runtime_dir / name) or b"").hex()) for name in POLICIES}
                epoch = digest({"formula": self.formula_epoch, "static": static, "policies": policies,
                                "labels": labels, "facilities": facilities, "identities": history_identities,
                                "scopes": sorted(scope_ids), "legacy": any(nodes[k]["legacy"] for k in keys)})
                self.context = {"template_rows": labels, "facilities": list(facilities.values()),
                    "history_scope_keys": sorted(scope_ids), "history_scope_identities": history_identities,
                    "inventory_catalog_present": bool(captures), "legacy_wb_present": any(nodes[k]["legacy"] for k in keys),
                    "static_config": {"proof": static, "dependency_epoch": epoch}}
                self.availability = {b.date: b.coverage != "missing" for b in bindings}
                vector = {"coverage": "complete_live_native_v1", "epoch": epoch,
                          "dates": {day: digest(per_day[day]) for day in self.days}}
                self.stats["seconds"] = self.max_capture_seconds - (self.deadline - time.monotonic())
                if self.stats["bytes"] > self.max_read_bytes or time.monotonic() >= self.deadline:
                    raise LiveSourceUnavailable("live_source_resource_limit")
                cache["headers"] = {digest(item): cache["headers"][digest(item)] for item in identities}
                cache["slices"] = {k: v for k, v in cache["slices"].items() if k in self.used_slices}
                cache["book"] = {k: v for k, v in cache["book"].items() if k in self.used_book}
                active_labels = {node["labels"] for node in cache["headers"].values()}
                for path in self.cache_dir.glob("*.rows.json"):
                    key = path.name.removesuffix(".rows.json")
                    if len(key) == 64 and all(c in "0123456789abcdef" for c in key) and key not in active_labels:
                        path.unlink()
                conn.set_progress_handler(None, 0)
                check_pinned_authority(conn, authority)
                return vector
        except sqlite3.OperationalError as exc:
            raise LiveSourceUnavailable("live_source_read_incomplete") from exc
        finally:
            if conn is not None:
                try:
                    conn.set_progress_handler(None, 0)
                except sqlite3.ProgrammingError:
                    pass  # A standalone context has already closed its connection.
            # Cache progress is NOT a consumed watermark or a published edition.
            self._cache_reserve(len(json.dumps(cache, ensure_ascii=False).encode()), replacing=cache_file)
            _atomic(cache_file, cache)

    def _cache_reserve(self, size, *, replacing=None):
        usage = sum(p.stat().st_size for p in self.cache_dir.iterdir() if p.is_file() and p != replacing)
        if usage + size > self.max_cache_bytes:
            raise LiveSourceUnavailable("source_cache_resource_limit")

    def require_source_families(self):
        _require_live_sqlite_family(self.db_path)
        book = self.runtime_dir / "fbs-snapshot-accounting.sqlite3"
        if book.exists():
            _require_live_sqlite_family(book)

    def _slice(self, conn, tables, cache, alarms, name, column, days):
        if name not in tables:
            return {}
        key = digest([name, alarms.get(name), days])
        self.used_slices.add(key)
        # Sources with no native revision are always read, never silently cached.
        if name not in alarms or key not in cache["slices"]:
            columns = [r[1] for r in conn.execute("PRAGMA table_info(" + name + ")")]
            grouped = {}
            rows = self._rows(conn, "SELECT * FROM " + name + " WHERE " + column + " IN (" + ",".join("?" for _ in days) + ") ORDER BY " + ",".join(_quoted(c) for c in columns), days)
            for row in rows:
                grouped.setdefault(row[columns.index(column)], []).append(row)
            result = {day: digest(grouped.get(day, [])) for day in days}
            if name in alarms:
                cache["slices"][key] = result
            return result
        return cache["slices"][key]

    def _temporal(self, conn, tables, cache, per_day, business_day):
        if "sheet_vitrina_v1_ready_temporal_revisions" not in tables:
            raise LiveSourceUnavailable("temporal_revision_schema_missing")
        for table, role in (("temporal_source_snapshots", "''"), ("temporal_source_slot_snapshots", "t.snapshot_role")):
            if table not in tables:
                continue
            rows = self._rows(conn, "SELECT t.source_key,t.snapshot_date," + role + ",t.captured_at,COALESCE(r.revision,0) FROM " + table + " t LEFT JOIN sheet_vitrina_v1_ready_temporal_revisions r ON r.source_key=t.source_key AND r.snapshot_date=t.snapshot_date AND r.snapshot_role=" + role + " WHERE t.source_key IN (?,?) AND t.snapshot_date>=? AND t.snapshot_date<=? ORDER BY 1,2,3", (*SOURCES, self.days[0], self.days[-1]))
            for source, day, snapshot_role, captured, revision in rows:
                key = digest([table, source, day, snapshot_role, captured, revision])
                self.used_slices.add(key)
                if key not in cache["slices"]:
                    query = "SELECT payload_json FROM " + table + " WHERE source_key=? AND snapshot_date=?"
                    args = [source, day]
                    if snapshot_role:
                        query += " AND snapshot_role=?"
                        args.append(snapshot_role)
                    cache["slices"][key] = digest(self._rows(conn, query, args))
                per_day[day].setdefault("temporal", []).append([source, snapshot_role, captured, revision, cache["slices"][key]])

    def _finance(self, conn, tables, per_day):
        name = "wb_finance_weekly_sku_aggregates"
        if name not in tables:
            return
        rows = self._rows(conn, "SELECT nm_id,week_start,week_end,calculated_at,coverage_json FROM " + name + " WHERE nm_id<>'__account__' AND week_start<=? AND week_end>=? ORDER BY calculated_at,nm_id", (min(self.days[-1], "2026-08-21"), self.days[0]))
        for nm_id, start, end, calculated, encoded in rows:
            try:
                payload = json.loads(encoded)
            except (ValueError, TypeError):
                continue  # Exact native resolver ignores malformed coverage.
            matched = set()
            for item in payload.get("daily_rows", []):
                day = item.get("operation_date")
                if day in per_day and day < "2026-08-22" and day not in matched and start <= day <= end:
                    per_day[day].setdefault("finance_daily_coverage", {})[str(nm_id)] = item
                    matched.add(day)  # Native selects FIRST matching daily_rows entry.

    def _archival(self, conn, tables, per_day):
        from packages.application.warehouse_archival_estimate import active_archival_estimates
        estimates = active_archival_estimates(conn)
        self.stats["bytes"] += len(json.dumps(estimates, ensure_ascii=False, default=str).encode())
        first = {}
        name = PREFIX + "warehouse_functional_events"
        if estimates and name in tables:
            ids = sorted(estimates)
            first = dict(self._rows(conn, "SELECT nm_id,MIN(business_date) FROM " + name + " WHERE event_type='wb_final_acceptance' AND nm_id IN (" + ",".join("?" for _ in ids) + ") AND business_date IS NOT NULL AND business_date!='' AND (CAST(quantity AS REAL)!=0 OR CAST(capital_rub AS REAL)!=0) GROUP BY nm_id", ids))
        for day in self.days:
            per_day[day]["archival_cost"] = {str(nm): row for nm, row in estimates.items()
                if row["effective_date"] <= max(day, "2026-07-01") < first.get(nm, "9999-12-31")}

    def _breakglass(self, conn, tables, per_day):
        from packages.application.sheet_vitrina_v1_breakglass_last_good import (
            CELLS_TABLE, OPERATIONS_TABLE, REVOCATIONS_TABLE,
        )
        if not {CELLS_TABLE, OPERATIONS_TABLE, REVOCATIONS_TABLE} <= tables:
            return
        operation = self._rows(conn, "SELECT o.* FROM " + OPERATIONS_TABLE + " o LEFT JOIN " + REVOCATIONS_TABLE + " r USING(operation_id) WHERE r.operation_id IS NULL ORDER BY o.applied_at DESC,o.operation_id DESC LIMIT 1")
        if not operation:
            return
        op_columns = [r[1] for r in conn.execute("PRAGMA table_info(" + OPERATIONS_TABLE + ")")]
        op_id = operation[0][op_columns.index("operation_id")]
        rows = self._rows(conn, "SELECT * FROM " + CELLS_TABLE + " WHERE operation_id=? ORDER BY row_id", (op_id,))
        columns = [r[1] for r in conn.execute("PRAGMA table_info(" + CELLS_TABLE + ")")]
        index = columns.index("target_date_from")
        for day in self.days:
            per_day[day]["breakglass"] = digest([operation, [r for r in rows if r[index] <= day]])

    def _book(self, context, nodes, bindings, cache, per_day, business_day):
        conn = context.borrow_book(self.runtime_dir / "fbs-snapshot-accounting.sqlite3")
        if conn is None:
            return
        current = self._rows(conn, "SELECT version FROM accounting_current WHERE singleton=1")
        versions = {}
        for binding in bindings:
            value = nodes[binding.source_key]["book"].get(binding.date, {}) if binding.source_key else {}
            version = value.get("book_version")
            if binding.date == business_day and current:
                version = current[0][0]
            if version:
                versions.setdefault(version, []).append(binding.date)
        from packages.application.fbs_snapshot_cost import fingerprint
        for version, days in versions.items():
            self.used_book.add(version)
            if not conn.execute("SELECT 1 FROM accounting_revisions WHERE version=?", (version,)).fetchone():
                raise LiveSourceUnavailable("bound_book_version_missing")
            if version not in cache["book"]:
                index = json.loads(self._rows(conn, "SELECT payload FROM accounting_revisions WHERE version=?", (version,))[0][0])
                proofs = {}
                for field in ("shared_days", "wb_days", "retained_days", "presentations"):
                    proofs[field] = dict(index.get(field, {}))
                cache["book"][version] = {"index": proofs, "verified": []}
            record = cache["book"][version]
            for day in days:
                refs = sorted({mapping[day] for mapping in record["index"].values() if day in mapping})
                for ref in refs:
                    if not conn.execute("SELECT 1 FROM accounting_blobs WHERE digest=?", (ref,)).fetchone():
                        raise LiveSourceUnavailable("bound_book_blob_missing")
                    if ref not in record["verified"]:
                        row = conn.execute("SELECT payload FROM accounting_blobs WHERE digest=?", (ref,)).fetchone()
                        self.stats["bytes"] += len(row[0])
                        if self.stats["bytes"] > self.max_read_bytes:
                            raise LiveSourceUnavailable("book_source_resource_limit")
                        decoder = zlib.decompressobj()
                        payload = decoder.decompress(row[0], self.max_read_bytes + 1)
                        if (len(payload) > self.max_read_bytes or not decoder.eof
                                or decoder.unconsumed_tail or fingerprint(json.loads(payload)) != ref):
                            raise LiveSourceUnavailable("book_blob_content_unknown")
                        record["verified"].append(ref)
                per_day[day]["book"] = [version, refs]

    def _quality(self, conn, cache, captures, per_day, alarms):
        quality_sources = {*INVENTORY_PREPARATION_TABLES,
                           lifecycle.WAREHOUSE_MAPPINGS_TABLE, lifecycle.FACILITIES_TABLE}
        alarm = digest({k: alarms.get(k) for k in quality_sources})
        days = sorted({day for _, day, _, manifest, _ in captures if json.loads(manifest).get("contract") != "bound_inventory_quantity_v1"})
        if not days:
            return
        floor_key = "quality_floor:" + alarm
        self.used_slices.add(floor_key)
        if floor_key not in cache["slices"]:
            cache["slices"][floor_key] = self._quality_floor(conn)
        floor = cache["slices"][floor_key]
        for day in days:
            if floor is not None and day < floor:
                # Exact native early-skip: no unresolved status can affect this date.
                material = {"contract": "fbs_lifecycle_quality_coverage_v1",
                    "as_of_date": day, "status": "exact", "groups": []}
                per_day[day]["lifecycle_quality"] = digest({**material, "digest": lifecycle._fingerprint(material)})
                continue
            key = "quality:" + day + ":" + alarm
            self.used_slices.add(key)
            if key not in cache["slices"]:
                cache["slices"][key] = digest(fbs_lifecycle_quality_coverage(conn, as_of_date=day))
            per_day[day]["lifecycle_quality"] = cache["slices"][key]

    def _quality_floor(self, conn):
        """Bounded native joins, without resolving each order's identity scopes.

        A floor proves ONLY exact empty coverage for earlier dates. It does not
        replace native partial/group semantics for dates at/after the floor.
        The native pre-filter overflow means no floor can be asserted.
        """
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {lifecycle.IDENTITY_PENDING_TABLE, lifecycle.IDENTITY_PENDING_RESOLUTIONS_TABLE,
            lifecycle.STATUS_OBSERVATIONS_TABLE, lifecycle.OBSERVATIONS_TABLE, lifecycle.IDENTITY_EVIDENCE_TABLE,
            lifecycle.WAREHOUSE_MAPPINGS_TABLE, lifecycle.IDENTITY_MAPPINGS_TABLE, lifecycle.FACILITIES_TABLE,
            PREFIX + "ff_pool_cutover_manifests"}
        if not required <= tables:
            return None  # Native not_applicable is evaluated normally, never assumed exact.
        row = conn.execute("SELECT manifest_json FROM " + PREFIX + "ff_pool_cutover_manifests ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1").fetchone()
        if not row:
            return None
        cutover = json.loads(row[0]).get("cutover_id")
        if not cutover:
            return None
        cursor = lifecycle._lifecycle_quality_cursor(conn, cutover_id=cutover, tables=tables)
        join = " JOIN " + lifecycle.STATUS_OBSERVATIONS_TABLE + " status ON status.observation_sequence=pending.source_status_observation_sequence LEFT JOIN " + lifecycle.OBSERVATIONS_TABLE + " source ON source.order_id=status.order_id AND source.source_revision=status.order_revision LEFT JOIN " + lifecycle.IDENTITY_PENDING_RESOLUTIONS_TABLE + " resolution ON resolution.pending_id=pending.pending_id"
        pending = self._rows(conn, "SELECT source.source_created_at,status.observed_at FROM " + lifecycle.IDENTITY_PENDING_TABLE + " pending" + join + " WHERE pending.cutover_id=? AND resolution.pending_id IS NULL ORDER BY status.observation_sequence LIMIT 100001", (cutover,))
        new = self._rows(conn, "SELECT source.source_created_at,status.observed_at FROM " + lifecycle.STATUS_OBSERVATIONS_TABLE + " status LEFT JOIN " + lifecycle.OBSERVATIONS_TABLE + " source ON source.order_id=status.order_id AND source.source_revision=status.order_revision LEFT JOIN " + lifecycle.IDENTITY_PENDING_TABLE + " pending ON pending.cutover_id=? AND pending.source_status_observation_sequence=status.observation_sequence WHERE status.observation_sequence>? AND pending.pending_id IS NULL ORDER BY status.observation_sequence LIMIT 100001", (cutover, cursor))
        if len(pending) > 100000 or len(new) > 100000:
            return None
        return min((lifecycle.current_business_date(str(created or observed)) for created, observed in [*pending, *new]), default="9999-12-31")

    def _current_evidence(self, conn, tables):
        # These evidence owners are outside MATERIAL_TABLES. Exact bounded
        # current records avoid a false no-change after a new official readback.
        from packages.application.inventory_planning_read_model import (
            INCIDENT_MANIFESTS_TABLE, INCIDENT_LINES_TABLE, SELLER_STOCK_READBACKS_TABLE,
            SELLER_STOCK_LINES_TABLE, INCIDENT_POLICY_TABLE, FUNCTIONAL_ACTIVE_TABLE,
            WB_SNAPSHOTS_TABLE,
        )
        result = {name: digest(self._rows(conn, "SELECT * FROM " + name + " ORDER BY 1,2"))
            for name in (INCIDENT_MANIFESTS_TABLE, INCIDENT_LINES_TABLE, SELLER_STOCK_READBACKS_TABLE,
                         SELLER_STOCK_LINES_TABLE, INCIDENT_POLICY_TABLE) if name in tables}
        if PREFIX + "user_configs" in tables:
            result["legacy_wb_warehouse_exclusions"] = digest(self._rows(conn,
                "SELECT user_key,revision,updated_at,payload_json FROM " + PREFIX + "user_configs WHERE config_key='wb_warehouse_exclusions' ORDER BY user_key"))
        planning_day = ""
        if {FUNCTIONAL_ACTIVE_TABLE, WB_SNAPSHOTS_TABLE} <= tables:
            row = self._rows(conn, "SELECT snapshot.snapshot_date FROM " + FUNCTIONAL_ACTIVE_TABLE + " active JOIN " + WB_SNAPSHOTS_TABLE + " snapshot ON snapshot.version_id=active.version_id WHERE active.slot=1 ORDER BY snapshot.created_at DESC,snapshot.snapshot_id DESC LIMIT 1")
            planning_day = row[0][0] if row else ""
        return result, planning_day


def update_live_history(*, adapter: LiveNativeAdapter, runtime, store: HistoryStore,
                        max_recomputes: int = 31, deadline_monotonic=None) -> dict:
    if Path(runtime.db_path).resolve() != adapter.db_path or Path(runtime.runtime_dir).resolve() != adapter.runtime_dir:
        raise ValueError("runtime differs from live source bridge")
    if store.root.resolve().is_relative_to(adapter.runtime_dir):
        raise ValueError("derived store must be separate from native runtime")
    calls_before = adapter.capture_calls
    vector = adapter.capture()
    initial_fence = adapter.fence
    pointer = store._current()
    old = store.edition() if pointer else None
    if old:
        for day, available in adapter.availability.items():
            if not available and day in old["days"]:
                with closing(store._open_day(store.root / "objects" / (old["days"][day] + ".sqlite3"))) as conn:
                    unit = json.loads(conn.execute("SELECT payload FROM metadata").fetchone()[0])
                if unit["accepted_ready_available"]:
                    # No producer intent distinguishes retention from deletion.
                    # Preserve accepted derived history until that intent exists.
                    raise LiveSourceUnavailable("accepted_ready_source_disappeared:" + day)
    if old and old["consumed"] == vector:
        return {"status": "unchanged", "recomputes": 0, "compiler_constructed": False,
                "edition_id": pointer["current"], "source_reads": adapter.stats}
    catalog = _read(store.root / "catalogs" / (old["catalog"] + ".json")) if old else None
    with ExitStack() as batch:
        adapter.require_source_families()
        batch.enter_context(window_read_context(runtime.db_path, runtime_dir=runtime.runtime_dir))
        if adapter.capture() != vector or adapter.fence != initial_fence:
            return {"status": "superseded", "recomputes": 0, "compiler_constructed": False}
        compiler = NativeDatedCompiler(runtime, adapter.now, adapter.days[0], adapter.days[-1],
            existing_catalog=catalog, prepared_context=adapter.context,
            prepared_availability=adapter.availability)
        def revalidate():
            # Close the entire bounded portion before obtaining a fresh fence.
            # Pending/error paths also close through ExitStack's finally.
            batch.close()
            current = adapter.capture()
            return current if adapter.fence == initial_fence else {**current, "publication_fence": "changed"}
        result = store.update(vector=vector, catalog=compiler.catalog, compile_day=compiler.compile,
            revalidate=revalidate, max_recomputes=max_recomputes,
            deadline_monotonic=deadline_monotonic, expected_base=pointer["current"] if pointer else None)
    return {**result, "compiler_constructed": True, "source_reads": adapter.stats,
            "capture_calls": adapter.capture_calls - calls_before}
