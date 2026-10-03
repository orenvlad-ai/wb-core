"""Unwired history candidate: immutable dated SQLite objects + atomic editions.

Reads evaluate no business formulas, create no files, and open one day at a
time. Writers publish only after a complete producer proof is revalidated.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable
import fcntl
import json
import os
import re
import sqlite3
import time
import uuid
import zlib

from packages.application.web_vitrina_history_compiler import CONTRACT, digest, dates_between


class HistoryUnavailable(ValueError):
    pass


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()


def _read(path: Path):
    return json.loads(path.read_bytes())


def _atomic(path: Path, value) -> None:
    temp = path.with_name(path.name + ".writing-" + uuid.uuid4().hex)
    try:
        with temp.open("xb") as file:
            os.chmod(temp, 0o600)
            file.write(_json(value))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
        with _directory_fd(path.parent) as fd:
            os.fsync(fd)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _directory_fd(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        yield fd
    finally:
        os.close(fd)


_UNSET = object()
_HASH = re.compile(r"^[0-9a-f]{64}$")


def validate_vector(vector: dict) -> None:
    # No automatic adapter may claim no-change with incomplete coverage.
    if vector.get("coverage") != "complete_frozen_native_v1":
        raise HistoryUnavailable("source_dependency_coverage_unknown")
    if not vector.get("epoch") or not isinstance(vector.get("dates"), dict) or not vector["dates"]:
        raise HistoryUnavailable("invalid_dependency_vector")
    for day, token in vector["dates"].items():
        dates_between(day, day)
        if not isinstance(token, str) or not _HASH.fullmatch(token):
            raise HistoryUnavailable("invalid_dated_dependency")


class HistoryStore:
    def __init__(self, root: Path, *, max_days: int = 366, max_rows: int = 512,
                 max_reply_bytes: int = 8 * 1024 * 1024,
                 max_store_bytes: int = 2 * 1024**3):
        self.root = Path(root)
        self.max_days, self.max_rows = max_days, max_rows
        self.max_reply_bytes, self.max_store_bytes = max_reply_bytes, max_store_bytes

    @contextmanager
    def _writer(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in ("objects", "catalogs", "editions"):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        with (self.root / "writer.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HistoryUnavailable("history_builder_busy") from None
            try:
                # Only our private object-writing pattern; never served objects.
                for path in (self.root / "objects").glob(".building-*"):
                    if path.is_file():
                        path.unlink()
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _current(self):
        pointer = self.root / "CURRENT.json"
        return _read(pointer) if pointer.exists() else None

    def edition(self, edition_id: str | None = None) -> dict:
        pointer = self._current()
        if not pointer:
            raise HistoryUnavailable("history_not_ready")
        edition_id = edition_id or pointer["current"]
        if len(edition_id) != 64 or any(c not in "0123456789abcdef" for c in edition_id):
            raise HistoryUnavailable("snapshot_expired")
        path = self.root / "editions" / (edition_id + ".json")
        if not path.is_file():
            raise HistoryUnavailable("snapshot_expired")
        edition = _read(path)
        if digest(edition) != edition_id:
            raise HistoryUnavailable("history_edition_corrupt")
        return edition

    def _write_day(self, unit: dict) -> str:
        if unit.get("contract") != CONTRACT or not unit.get("cells"):
            raise ValueError("invalid compiler day")
        if any(len(cell) != 16 or len(_json(cell)) > 65536 for cell in unit["cells"].values()):
            raise ValueError("invalid compiler cell")
        if sum(len(_json(cell)) for cell in unit["cells"].values()) > 64 * 1024**2:
            raise ValueError("history day limit exceeded")
        object_id = digest(unit)
        target = self.root / "objects" / (object_id + ".sqlite3")
        if target.exists():
            return object_id
        temp = self.root / "objects" / (".building-" + uuid.uuid4().hex)
        try:
            with closing(sqlite3.connect(temp)) as conn:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("CREATE TABLE cells(row_id TEXT PRIMARY KEY, payload BLOB NOT NULL) WITHOUT ROWID")
                conn.execute("CREATE TABLE metadata(payload TEXT NOT NULL)")
                conn.execute("INSERT INTO metadata VALUES(?)", (_json(
                    {k: v for k, v in unit.items() if k != "cells"}).decode(),))
                conn.executemany("INSERT INTO cells VALUES(?,?)", (
                    (row_id, zlib.compress(_json(cell), 6)) for row_id, cell in unit["cells"].items()))
                conn.commit()
            if self._usage() > self.max_store_bytes:
                raise HistoryUnavailable("history_storage_limit")
            os.chmod(temp, 0o600)
            with temp.open("rb") as file:
                os.fsync(file.fileno())
            os.replace(temp, target)
            with _directory_fd(target.parent) as fd:
                os.fsync(fd)
            return object_id
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                Path(str(temp) + suffix).unlink(missing_ok=True)

    def update(self, *, vector: dict, catalog: dict, compile_day: Callable[[str], dict],
               revalidate: Callable[[], dict], max_recomputes: int = 366,
               deadline_monotonic: float | None = None, expected_base=_UNSET) -> dict:
        validate_vector(vector)
        if len(vector["dates"]) > self.max_days:
            raise ValueError("history date limit exceeded")
        if len(catalog["rows"]) > 50000 or len(_json(catalog)) > 64 * 1024**2:
            raise ValueError("history catalog limit exceeded")
        if set(catalog["order"]) != set(catalog["rows"]) or len(catalog["order"]) != len(catalog["rows"]):
            raise ValueError("invalid catalog order")
        catalog_id = digest(catalog)
        target_key = digest({"vector": vector, "catalog": catalog_id})
        with self._writer():
            current_pointer = self._current()
            current_id = current_pointer["current"] if current_pointer else None
            if expected_base is not _UNSET and expected_base != current_id:
                return {"status": "superseded", "recomputes": 0}
            current = self.edition() if current_pointer else None
            if current and current["consumed"] == vector and current["catalog"] == catalog_id:
                return {"status": "unchanged", "recomputes": 0,
                    "edition_id": current_pointer["current"]}
            pending_path = self.root / "PENDING.json"
            pending = _read(pending_path) if pending_path.exists() else {}
            if pending.get("target") != target_key or pending.get("base") != current_id:
                refs = {}
                if current and current["consumed"]["epoch"] == vector["epoch"] and current["catalog"] == catalog_id:
                    refs = {day: ref for day, ref in current["days"].items()
                        if vector["dates"].get(day) == current["consumed"]["dates"].get(day)}
                pending = {"target": target_key, "base": current_pointer["current"] if current_pointer else None,
                    "refs": refs}
                _atomic(pending_path, pending)
            self._collect()
            catalog_path = self.root / "catalogs" / (catalog_id + ".json")
            if not catalog_path.exists():
                self._reserve(len(_json(catalog)))
                _atomic(catalog_path, catalog)
            recomputes = 0
            for day in sorted(vector["dates"]):
                if day in pending["refs"]:
                    continue
                if recomputes >= max_recomputes or (
                    deadline_monotonic is not None and time.monotonic() >= deadline_monotonic):
                    return {"status": "pending", "recomputes": recomputes,
                        "completed": len(pending["refs"]), "total": len(vector["dates"])}
                usage = self._usage()
                if usage >= self.max_store_bytes - 16 * 1024**2:
                    raise HistoryUnavailable("history_storage_limit")
                unit = compile_day(day)
                if unit["date"] != day or unit["context_epoch"] != catalog["context_epoch"]:
                    raise ValueError("compiler context mismatch")
                if set(unit["cells"]) != set(catalog["rows"]):
                    raise ValueError("compiler row set mismatch")
                pending["refs"][day] = self._write_day(unit)
                recomputes += 1
                _atomic(pending_path, pending)
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                return {"status": "pending", "recomputes": recomputes,
                    "completed": len(pending["refs"]), "total": len(vector["dates"])}
            fresh = revalidate()
            validate_vector(fresh)
            if fresh != vector:
                # Pending is retained as unconsumed work, never a published watermark.
                return {"status": "superseded", "recomputes": recomputes}
            edition = {"contract": CONTRACT, "catalog": catalog_id,
                "days": pending["refs"], "consumed": vector}
            edition_id = digest(edition)
            self._reserve(len(_json(edition)) + 4096)
            _atomic(self.root / "editions" / (edition_id + ".json"), edition)
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                return {"status": "pending", "recomputes": recomputes,
                    "completed": len(pending["refs"]), "total": len(vector["dates"])}
            _atomic(self.root / "CURRENT.json", {"current": edition_id,
                "previous": current_pointer["current"] if current_pointer else None})
            pending_path.unlink(missing_ok=True)
            self._collect()
            return {"status": "published", "recomputes": recomputes, "edition_id": edition_id}

    def _usage(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def _reserve(self, additional: int) -> None:
        if self._usage() + additional > self.max_store_bytes:
            raise HistoryUnavailable("history_storage_limit")

    def _collect(self, *, pin_ttl_seconds: int = 4 * 3600) -> None:
        """Worker-only bounded retention: current/previous, TTL pins, pending refs."""
        pointer = self._current() or {}
        keep = {pointer.get("current"), pointer.get("previous")}
        refs, catalogs = set(), set()
        now = time.time()
        for path in (self.root / "editions").glob("*.json"):
            if not _HASH.fullmatch(path.stem):
                continue
            if path.stem in keep or now - path.stat().st_mtime <= pin_ttl_seconds:
                edition = _read(path)
                refs.update(edition["days"].values())
                catalogs.add(edition["catalog"])
            else:
                path.unlink()
        pending_path = self.root / "PENDING.json"
        if pending_path.exists():
            refs.update(_read(pending_path).get("refs", {}).values())
        for folder, suffix, retained in (("objects", ".sqlite3", refs), ("catalogs", ".json", catalogs)):
            for path in (self.root / folder).glob("*" + suffix):
                if _HASH.fullmatch(path.stem) and path.stem not in retained:
                    # Pending catalog is protected until its edition publishes.
                    if folder == "catalogs" and pending_path.exists():
                        continue
                    path.unlink()

    @staticmethod
    def _open_day(path: Path):
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        conn.execute("PRAGMA query_only=ON")
        return conn

    def read(self, *, date_from: str, date_to: str, scope: str = "summary",
             row_ids: list[str] | None = None, group_id: str | None = None,
             offset: int = 0, limit: int = 128, edition_id: str | None = None) -> dict:
        days = dates_between(date_from, date_to, limit=self.max_days)
        if scope not in {"summary", "sku"} or not 1 <= limit <= self.max_rows or offset < 0:
            raise ValueError("invalid history read scope")
        if row_ids is not None and len(set(row_ids)) > self.max_rows:
            raise ValueError("history row limit exceeded")
        edition = self.edition(edition_id)
        actual_id = digest(edition)
        if any(day not in edition["days"] for day in days):
            raise HistoryUnavailable("history_date_unavailable")
        catalog = _read(self.root / "catalogs" / (edition["catalog"] + ".json"))
        if digest(catalog) != edition["catalog"]:
            raise HistoryUnavailable("history_catalog_corrupt")
        members, availability = set(), {}
        # One connection at a time; metadata is bounded by the shared catalog.
        for day in days:
            with closing(self._open_day(self.root / "objects" / (edition["days"][day] + ".sqlite3"))) as conn:
                meta = json.loads(conn.execute("SELECT payload FROM metadata").fetchone()[0])
            if meta["date"] != day or meta["context_epoch"] != catalog["context_epoch"]:
                raise HistoryUnavailable("history_day_context_mismatch")
            members.update(meta["members"])
            availability[day] = meta["accepted_ready_available"]
        selected = [rid for rid in catalog["order"] if rid in members]
        order_by_id = {rid: i for i, rid in enumerate(selected, 1)}
        selected = [rid for rid in selected if (
            catalog["rows"][rid]["row_kind"] in {"total", "group"} if scope == "summary"
            else catalog["rows"][rid]["row_kind"] == "sku")]
        if row_ids is not None:
            wanted = set(row_ids)
            selected = [rid for rid in selected if rid in wanted]
        if group_id is not None:
            selected = [rid for rid in selected if catalog["rows"][rid]["group_id"] == group_id]
        total = len(selected)
        selected = selected[offset:offset + limit]
        rows = {rid: {**catalog["rows"][rid], "row_order": order_by_id[rid],
                      "cells": {}} for rid in selected}
        response_bytes = len(_json(rows))
        for day in days:
            if not selected:
                break
            with closing(self._open_day(self.root / "objects" / (edition["days"][day] + ".sqlite3"))) as conn:
                placeholders = ",".join("?" for _ in selected)
                cells = conn.execute("SELECT row_id,payload FROM cells WHERE row_id IN (" + placeholders + ")", selected)
                found = 0
                for rid, payload in cells:
                    decoder = zlib.decompressobj()
                    decoded = decoder.decompress(payload, 65537)
                    if len(decoded) > 65536 or not decoder.eof:
                        raise HistoryUnavailable("history_cell_limit")
                    cell = json.loads(decoded)
                    response_bytes += len(decoded) + len(_json(cell[1])) + 64
                    if response_bytes > self.max_reply_bytes:
                        raise HistoryUnavailable("history_reply_limit")
                    if not isinstance(cell, list) or len(cell) != 16:
                        raise HistoryUnavailable("history_cell_corrupt")
                    rows[rid]["cells"][day] = cell
                    found += 1
                if found != len(selected):
                    raise HistoryUnavailable("history_day_rows_missing")
        for row in rows.values():
            values = row["values"]
            terms = [str(values.get(k, [""])[0] or "")
                     for k in ("scope_label", "metric_label", "group", "section", "nm_id")]
            displays = [str(row["row_order"])] + [values[c["id"]][1]
                for c in catalog["columns"] if c["id"] != "row_order"]
            displays += [row["cells"][day][1] for day in days]
            row["search_text"] = " ".join(v for v in terms + displays if v not in {"", "—"})
        result = {"contract": CONTRACT, "edition_id": actual_id, "scope": scope,
            "dates": days, "availability": availability, "total_rows": total,
            "offset": offset, "rows": list(rows.values()), "next_offset": (
                offset + len(selected) if offset + len(selected) < total else None)}
        if len(_json(result)) > self.max_reply_bytes:
            raise HistoryUnavailable("history_reply_limit")
        return result


def import_finished_table(store: HistoryStore, table: dict, *, accepted_ready: dict[str, bool]) -> dict:
    """Local benchmark import of an already-finished indexed table, no evaluator.

    Membership is the supplied range's row catalog; this proves storage/read
    cost only, not native independent-day compilation or production adapters.
    """
    from packages.application.web_vitrina_history_compiler import unpack_table
    catalog, cells = unpack_table(table)
    days = [c["id"][5:] for c in table["columns"] if c["id"].startswith("date:")]
    if set(accepted_ready) != set(days):
        raise ValueError("explicit dated availability required")
    catalog["order"] = [r["row_id"] for r in table["rows"]]
    catalog["context_epoch"] = digest(catalog)
    units = {day: {"contract": CONTRACT, "date": day,
        "context_epoch": catalog["context_epoch"], "accepted_ready_available": accepted_ready[day],
        "members": list(catalog["order"]),
        "cells": {rid: dated[day] for rid, dated in cells.items()}} for day in days}
    vector = {"coverage": "complete_frozen_native_v1", "epoch": catalog["context_epoch"],
        "dates": {day: digest(unit) for day, unit in units.items()}}
    return store.update(vector=vector, catalog=catalog, compile_day=units.__getitem__,
                        revalidate=lambda: vector)
