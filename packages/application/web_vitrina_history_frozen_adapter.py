"""Concrete LOCAL frozen-native adapter; intentionally not a production hook.

Covers the entire explicitly frozen database and supplied side-input files by
content proof. Ready rows have dated impact; all other native tables are a
conservative global epoch. This is correct but deliberately not a production
incremental detector: live finance/book publication, effective interval impact
and clock buckets require separately accepted producer contracts.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from pathlib import Path
import hashlib
import json
import sqlite3

from packages.application.web_vitrina_history_store import HistoryStore, _read
from packages.application.web_vitrina_history_compiler import NativeDatedCompiler
from packages.application.web_vitrina_window_read_context import window_read_context

from packages.application.web_vitrina_history_compiler import digest, dates_between
from packages.business_time import current_business_date_iso


def require_rollback_sqlite(path: Path) -> None:
    """Reject persistent WAL without an open capable of creating sidecars.

    Immutable PRAGMA journal_mode reports delete even for a WAL main. Native
    window reads later use mode=ro; both SQLite header versions must be 1.
    """
    with path.open("rb") as file:
        header = file.read(20)
    if header[:16] == b"SQLite format 3\x00" and header[18:20] != b"\x01\x01":
        raise ValueError("frozen SQLite persistent WAL/unsupported header")


class FrozenNativeAdapter:
    def __init__(self, *, db_path: Path, frozen_root: Path, files: list[Path],
                 now: datetime, date_from: str, date_to: str,
                 formula_epoch: str, max_source_bytes: int = 128 * 1024**2):
        self.db_path, self.frozen_root = Path(db_path).resolve(), Path(frozen_root).resolve()
        self.runtime_dir = self.db_path.parent
        self.files = sorted({Path(p).resolve() for p in files})
        self.now, self.days = now, dates_between(date_from, date_to)
        self.formula_epoch, self.max_source_bytes = formula_epoch, max_source_bytes
        if not formula_epoch or now.tzinfo is None:
            raise ValueError("explicit formula epoch and frozen timezone-aware clock required")
        for path in [self.db_path, *self.files]:
            if not path.is_relative_to(self.frozen_root) or not path.is_file():
                raise ValueError("adapter only accepts explicit private frozen native files")
            require_rollback_sqlite(path)
        for suffix in ("-wal", "-journal"):
            if Path(str(self.db_path) + suffix).exists():
                raise ValueError("frozen database must be closed and clean")

    def capture(self) -> dict:
        runtime_files = {p.resolve() for p in self.runtime_dir.rglob("*") if p.is_file()}
        runtime_files.discard(self.db_path)
        if any(p.name.endswith(("-wal", "-journal")) for p in runtime_files):
            raise ValueError("frozen runtime SQLite inputs must be closed and clean")
        paths = sorted({self.db_path, *self.files, *runtime_files})
        if any(not p.is_relative_to(self.frozen_root) for p in paths):
            raise ValueError("unscoped frozen side input")
        for path in paths:
            require_rollback_sqlite(path)
        if sum(p.stat().st_size for p in paths) > self.max_source_bytes:
            raise ValueError("frozen source proof byte limit exceeded")
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        global_tables, ready = {}, []
        with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            tables = sorted(row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            for name in tables:
                quoted = '"' + name.replace('"', '""') + '"'
                columns = [r[1] for r in conn.execute("PRAGMA table_info(" + quoted + ")")]
                rows = list(conn.execute("SELECT * FROM " + quoted))
                normalized = [list(row) for row in rows]
                normalized.sort(key=lambda row: json.dumps(row, sort_keys=True, default=str))
                if name == "sheet_vitrina_v1_ready_snapshots":
                    for row in normalized:
                        item = dict(zip(columns, row))
                        plan = json.loads(item["plan_json"])
                        ready.append({"dates": set(plan.get("date_columns", [])) | {item["as_of_date"]},
                            "proof": digest(item)})
                elif name not in {"sheet_vitrina_v1_ready_revisions"}:
                    # Unknown/new native tables automatically broaden invalidation.
                    global_tables[name] = digest({"columns": columns, "rows": normalized})
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        if before != after:
            raise ValueError("frozen inputs changed during dependency capture")
        file_proofs = {str(p.relative_to(self.frozen_root)): before[str(p)] for p in paths if p != self.db_path}
        ready_catalog = []
        # Default/current templates are used even outside the requested history.
        # Identity/label/binding changes therefore conservatively dirty all days.
        with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            for bundle, day, encoded in conn.execute(
                "SELECT bundle_version,as_of_date,plan_json FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date"):
                plan = json.loads(encoded)
                ready_catalog.append({"bundle": bundle, "date": day,
                    "date_columns": plan.get("date_columns"),
                    "rows": [[row[:2] for row in sheet.get("rows", [])] for sheet in plan.get("sheets", [])]})
        if before != {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}:
            raise ValueError("frozen inputs changed during catalog capture")
        epoch = digest({"tables": global_tables, "files": file_proofs, "ready_catalog": ready_catalog,
            "formula": self.formula_epoch, "business_clock": current_business_date_iso(self.now)})
        return {"coverage": "complete_frozen_native_v1", "epoch": epoch,
            "dates": {day: digest(sorted(item["proof"] for item in ready if day in item["dates"]))
                for day in self.days}}


def update_frozen_history(*, adapter: FrozenNativeAdapter, runtime, store: HistoryStore,
                          max_recomputes: int = 366, deadline_monotonic=None) -> dict:
    """Cheap no-change check precedes ALL native evaluator/catalog construction."""
    if Path(runtime.db_path).resolve() != adapter.db_path or Path(runtime.runtime_dir).resolve() != adapter.runtime_dir:
        raise ValueError("runtime differs from frozen adapter source")
    if store.root.resolve().is_relative_to(adapter.runtime_dir):
        raise ValueError("derived store must be outside frozen native runtime")
    vector = adapter.capture()
    pointer = store._current()
    old = store.edition() if pointer else None
    if old and old["consumed"] == vector:
        return {"status": "unchanged", "recomputes": 0, "compiler_constructed": False,
                "edition_id": pointer["current"]}
    catalog = _read(store.root / "catalogs" / (old["catalog"] + ".json")) if old else None
    with window_read_context(runtime.db_path, runtime_dir=runtime.runtime_dir):
        compiler = NativeDatedCompiler(runtime, adapter.now, adapter.days[0], adapter.days[-1],
                                      existing_catalog=catalog, dependency_epoch=vector["epoch"])
        result = store.update(vector=vector, catalog=compiler.catalog,
            compile_day=compiler.compile, revalidate=adapter.capture,
            max_recomputes=max_recomputes, deadline_monotonic=deadline_monotonic,
            expected_base=pointer["current"] if pointer else None)
    return {**result, "compiler_constructed": True}
