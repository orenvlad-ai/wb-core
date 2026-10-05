"""Offline GROUP-only repair of saved history; no native evaluation or HTTP wiring.

The trusted transform receives stored cells and returns an exact allowlisted
patch plus pinned auxiliary digests. Native metadata and every other cell BLOB
remain byte-identical. Only explicit preview/CAS publication changes CURRENT.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
import zlib

from packages.application.web_vitrina_history_compiler import CONTRACT, dates_between, digest
from packages.application.web_vitrina_history_store import (
    HistoryStore, HistoryUnavailable, _HASH, _atomic, _directory_fd, _json, _read,
)

REPAIR_CONTRACT = "stored_sku_group_only_repair_v1"
PENDING = "GROUP_REPAIR_PENDING.json"
READY = "GROUP_REPAIR_READY.json"


def _deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise HistoryUnavailable("history_group_repair_deadline")


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise HistoryUnavailable("history_group_repair_invalid_hash")
    return value


def _separate(source, candidate):
    a, b = source.root.resolve(), candidate.root.resolve()
    if a == b or a in b.parents or b in a.parents:
        raise HistoryUnavailable("history_group_repair_overlapping_roots")


def _catalog(store, catalog_id, deadline, cache):
    _deadline(deadline)
    _hash(catalog_id)
    if catalog_id in cache:
        return cache[catalog_id][0]
    path = store.root / "catalogs" / (catalog_id + ".json")
    size=path.stat().st_size
    if size > 64 * 1024**2:
        raise HistoryUnavailable("history_group_repair_catalog_limit")
    catalog = _read(path)
    if digest(catalog) != catalog_id or len(catalog["rows"]) > 50000:
        raise HistoryUnavailable("history_group_repair_catalog_corrupt")
    _deadline(deadline)
    # Per-call immutable catalog reuse, bounded by encoded bytes and count.
    while cache and (sum(item[1] for item in cache.values())+size>64 * 1024**2 or len(cache)>=4):
        _deadline(deadline)
        del cache[next(iter(cache))]
    cache[catalog_id]=(catalog,size)
    return catalog


def _unit(store, path, object_id, day, catalog, deadline):
    cells, blobs, size = {}, {}, 0
    with closing(store._open_day(path)) as conn:
        records = conn.execute("SELECT payload FROM metadata").fetchall()
        if len(records) != 1 or len(records[0][0].encode()) > 8 * 1024**2:
            raise HistoryUnavailable("history_group_repair_metadata_corrupt")
        raw_metadata = records[0][0]
        metadata = json.loads(raw_metadata)
        for row_id, compressed in conn.execute("SELECT row_id,payload FROM cells ORDER BY row_id"):
            _deadline(deadline)
            decoder = zlib.decompressobj()
            raw = decoder.decompress(compressed, 65537)
            size += len(raw)
            if not decoder.eof or decoder.unused_data or len(raw) > 65536 or size > 64 * 1024**2:
                raise HistoryUnavailable("history_cell_limit")
            cell = json.loads(raw)
            if not isinstance(cell, list) or len(cell) != 16:
                raise HistoryUnavailable("history_cell_corrupt")
            cells[row_id], blobs[row_id] = cell, compressed
            if len(cells) > 50000:
                raise HistoryUnavailable("history_group_repair_row_limit")
    unit = {**metadata, "cells": cells}
    if (metadata.get("contract") != CONTRACT or metadata.get("date") != day
            or metadata.get("context_epoch") != catalog["context_epoch"]
            or not set(cells) <= set(catalog["rows"])
            or not set(metadata.get("members", [])) <= set(cells)
            or digest(unit) != object_id):
        raise HistoryUnavailable("history_group_repair_object_corrupt")
    _deadline(deadline)
    return unit, blobs, raw_metadata


def _plan(source, candidate, expected_base, allowlisted_group_row_ids, metadata, deadline=None):
    _deadline(deadline)
    _separate(source, candidate)
    _hash(expected_base)
    pointer = (source.root / "CURRENT.json").read_bytes()
    if (source._current() or {}).get("current") != expected_base:
        raise HistoryUnavailable("history_group_repair_superseded")
    old = source.edition(expected_base)
    days = sorted(allowlisted_group_row_ids)
    if not days or len(days) > source.max_days or not set(days) <= set(old["days"]):
        raise HistoryUnavailable("history_group_repair_dates_invalid")
    allowlist = {}
    for day in days:
        _deadline(deadline)
        dates_between(day, day)
        rows = allowlisted_group_row_ids[day]
        if not rows or len(rows) > 50000 or len(set(rows)) != len(rows) or any(not isinstance(r, str) for r in rows):
            raise HistoryUnavailable("history_group_repair_allowlist_invalid")
        allowlist[day] = sorted(rows)
    if set(metadata) != {"transform_code_hash", "auxiliary_digests"} or set(metadata["auxiliary_digests"]) != set(days):
        raise HistoryUnavailable("history_group_repair_proof_invalid")
    _hash(metadata["transform_code_hash"])
    for proofs in metadata["auxiliary_digests"].values():
        _deadline(deadline)
        if not isinstance(proofs, dict) or any(not isinstance(k, str) or not k for k in proofs):
            raise HistoryUnavailable("history_group_repair_proof_invalid")
        for value in proofs.values():
            _hash(value)
    claim = {"contract": REPAIR_CONTRACT, "base_edition_id": expected_base,
             "base_pointer_sha256": hashlib.sha256(pointer).hexdigest(),
             "base_objects": {d: _hash(old["days"][d]) for d in days},
             "allowlisted_group_row_ids": allowlist, **deepcopy(metadata)}
    if len(_json(claim)) > 4 * 1024**2:
        raise HistoryUnavailable("history_group_repair_proof_limit")
    return old, claim


def _copy(source_path, target_path, store, deadline):
    with source_path.open("rb") as src, target_path.open("xb") as dst:
        os.chmod(target_path, 0o600)
        while True:
            _deadline(deadline)
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            store._reserve(len(chunk))
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())


def _same_file(left, right, deadline):
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as a, right.open("rb") as b:
        while True:
            _deadline(deadline)
            block=a.read(1024 * 1024)
            if block != b.read(1024 * 1024):
                return False
            if not block:
                return True


def _patched_object(source, candidate, old_id, unit, patch, deadline):
    cells = {**unit["cells"], **patch}
    size=0
    for cell in cells.values():
        _deadline(deadline)
        size+=len(_json(cell))
        if size>64 * 1024**2:
            raise HistoryUnavailable("history_cell_limit")
    new_id = digest({**unit, "cells": cells})
    if new_id == old_id:
        return old_id
    target = candidate.root / "objects" / (new_id + ".sqlite3")
    if target.exists():
        return new_id
    temp = target.with_name(".building-" + uuid.uuid4().hex)
    try:
        candidate._reserve((source.root / "objects" / (old_id + ".sqlite3")).stat().st_size
                           + sum(len(_json(c)) for c in patch.values()) + 65536)
        _copy(source.root / "objects" / (old_id + ".sqlite3"), temp, candidate, deadline)
        with closing(sqlite3.connect(temp)) as conn, conn:
            conn.execute("PRAGMA journal_mode=DELETE")
            for row_id, cell in patch.items():
                _deadline(deadline)
                if conn.execute("UPDATE cells SET payload=? WHERE row_id=?",
                                (zlib.compress(_json(cell), 6), row_id)).rowcount != 1:
                    raise HistoryUnavailable("history_group_repair_patch_invalid")
        _deadline(deadline)
        candidate._reserve(0)
        with temp.open("rb") as file:
            os.fsync(file.fileno())
        os.replace(temp, target)
        with _directory_fd(target.parent) as fd:
            os.fsync(fd)
        return new_id
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(temp) + suffix).unlink(missing_ok=True)


def build_group_repair_candidate(source: HistoryStore, candidate: HistoryStore, *,
        expected_base: str, allowlisted_group_row_ids: dict[str, list[str]], metadata: dict,
        transform_day, max_days_per_call: int = 1, deadline_monotonic=None) -> dict:
    """Resume an offline candidate; CURRENT is never written in either store."""
    if max_days_per_call < 1:
        raise ValueError("max_days_per_call must be positive")
    old, claim = _plan(source, candidate, expected_base, allowlisted_group_row_ids, metadata, deadline_monotonic)
    plan_id = digest(claim)
    with candidate._writer():
        if candidate._current() is not None or (candidate.root / "PENDING.json").exists():
            raise HistoryUnavailable("history_group_repair_candidate_in_use")
        pending_path = candidate.root / PENDING
        pending = _read(pending_path) if pending_path.exists() else {"claim": claim, "refs": {}}
        if pending.get("claim") != claim or not set(pending.get("refs", {})) <= set(claim["base_objects"]):
            raise HistoryUnavailable("history_group_repair_resume_conflict")
        for value in pending["refs"].values():
            _hash(value)
        candidate._reserve(len(_json(pending)))
        _atomic(pending_path, pending)
        completed = 0
        catalog_cache={}
        try:
            for day, allowed in claim["allowlisted_group_row_ids"].items():
                _deadline(deadline_monotonic)
                if day in pending["refs"]:
                    continue
                if completed >= max_days_per_call:
                    break
                catalog = _catalog(source, source.day_catalogs(old)[day], deadline_monotonic, catalog_cache)
                if any(catalog["rows"].get(r, {}).get("row_kind") != "group" for r in allowed):
                    raise HistoryUnavailable("history_group_repair_non_group_allowlist")
                old_id = claim["base_objects"][day]
                unit, _, _ = _unit(source, source.root / "objects" / (old_id + ".sqlite3"),
                                               old_id, day, catalog, deadline_monotonic)
                if not set(allowed) <= set(unit["cells"]):
                    raise HistoryUnavailable("history_group_repair_patch_missing_row")
                transformed = transform_day(day, deepcopy(catalog), deepcopy(unit["cells"]))
                _deadline(deadline_monotonic)
                if (set(transformed) != {"patch", "auxiliary_digests"}
                        or transformed["auxiliary_digests"] != claim["auxiliary_digests"][day]
                        or set(transformed["patch"]) != set(allowed)):
                    raise HistoryUnavailable("history_group_repair_patch_or_proof_invalid")
                for cell in transformed["patch"].values():
                    _deadline(deadline_monotonic)
                    if not isinstance(cell, list) or len(cell) != 16 or len(_json(cell)) > 65536:
                        raise HistoryUnavailable("history_cell_limit")
                new_id = _patched_object(source, candidate, old_id, unit, transformed["patch"], deadline_monotonic)
                pending["refs"][day] = new_id
                candidate._reserve(len(_json(pending)))
                _atomic(pending_path, pending)
                completed += 1
            _deadline(deadline_monotonic)
        except HistoryUnavailable as error:
            if str(error) != "history_group_repair_deadline":
                raise
            return {"status":"pending", "plan_id":plan_id, "completed":len(pending["refs"]),
                    "total":len(claim["base_objects"]), "processed":completed}
        if len(pending["refs"]) != len(claim["base_objects"]):
            return {"status": "pending", "plan_id": plan_id, "completed": len(pending["refs"]),
                    "total": len(claim["base_objects"]), "processed": completed}
        if hashlib.sha256((source.root / "CURRENT.json").read_bytes()).hexdigest() != claim["base_pointer_sha256"]:
            raise HistoryUnavailable("history_group_repair_superseded")
        edition = {**deepcopy(old), "days": {**old["days"], **pending["refs"]}, "group_repair": claim}
        edition_id = digest(edition)
        try:
            candidate._reserve(len(_json(edition)) + 4096)
            _deadline(deadline_monotonic)
            _atomic(candidate.root / "editions" / (edition_id + ".json"), edition)
            _deadline(deadline_monotonic)
            _atomic(candidate.root / READY, {"edition_id": edition_id, "plan_id": plan_id})
        except HistoryUnavailable as error:
            if str(error) != "history_group_repair_deadline":
                raise
            return {"status":"pending", "plan_id":plan_id, "completed":len(pending["refs"]),
                    "total":len(claim["base_objects"]), "processed":completed}
        return {"status": "ready", "edition_id": edition_id, "plan_id": plan_id,
                "completed": len(pending["refs"]), "total": len(pending["refs"])}


def preview_group_repair(source: HistoryStore, candidate: HistoryStore, *,
        expected_base: str, expected_candidate: str, deadline_monotonic=None) -> dict:
    """Read-only full comparison of the patch and byte-preserved source fields."""
    _separate(source, candidate)
    _hash(expected_base); _hash(expected_candidate)
    edition = _read(candidate.root / "editions" / (expected_candidate + ".json"))
    if digest(edition) != expected_candidate or _read(candidate.root / READY)["edition_id"] != expected_candidate:
        raise HistoryUnavailable("history_group_repair_candidate_corrupt")
    claim = edition.get("group_repair", {})
    old, regenerated = _plan(source, candidate, expected_base, claim.get("allowlisted_group_row_ids", {}),
                            {k: claim.get(k) for k in ("transform_code_hash", "auxiliary_digests")}, deadline_monotonic)
    if claim != regenerated or digest(claim) != _read(candidate.root / READY)["plan_id"]:
        raise HistoryUnavailable("history_group_repair_claim_mismatch")
    if {k: v for k, v in edition.items() if k not in {"days", "group_repair"}} != {k: v for k, v in old.items() if k not in {"days", "group_repair"}}:
        raise HistoryUnavailable("history_group_repair_native_metadata_changed")
    if set(edition["days"]) != set(old["days"]):
        raise HistoryUnavailable("history_group_repair_dates_invalid")
    changed = 0
    catalog_cache={}
    for day, old_id in old["days"].items():
        _deadline(deadline_monotonic)
        if day not in claim["base_objects"]:
            if edition["days"][day] != old_id:
                raise HistoryUnavailable("history_group_repair_outside_scope")
            continue
        catalog = _catalog(source, source.day_catalogs(old)[day], deadline_monotonic, catalog_cache)
        allowed = set(claim["allowlisted_group_row_ids"][day])
        if any(catalog["rows"].get(r, {}).get("row_kind") != "group" for r in allowed):
            raise HistoryUnavailable("history_group_repair_non_group_allowlist")
        before, old_blobs, old_metadata = _unit(source, source.root / "objects" / (old_id + ".sqlite3"),
                                               old_id, day, catalog, deadline_monotonic)
        new_id = _hash(edition["days"][day])
        path = (source if new_id == old_id else candidate).root / "objects" / (new_id + ".sqlite3")
        after, new_blobs, new_metadata = _unit(candidate, path, new_id, day, catalog, deadline_monotonic)
        if old_metadata != new_metadata or set(old_blobs) != set(new_blobs) or not allowed <= set(new_blobs):
            raise HistoryUnavailable("history_group_repair_native_metadata_changed")
        for row_id, blob in old_blobs.items():
            _deadline(deadline_monotonic)
            if blob != new_blobs[row_id]:
                if row_id not in allowed:
                    raise HistoryUnavailable("history_group_repair_non_group_cell_changed")
                changed += 1
    token_claim = {"source_root": str(source.root.resolve()), "candidate_root": str(candidate.root.resolve()),
                   "expected_current": expected_base, "candidate_edition": expected_candidate, "repair_claim": claim}
    return {"status": "preview", "preview_token": digest(token_claim),
            "repair_claim": claim, "changed_group_cells": changed, "days": len(claim["base_objects"])}


def publish_group_repair(source: HistoryStore, candidate: HistoryStore, *,
        expected_base: str, expected_candidate: str, preview_token: str,
        revalidate, deadline_monotonic=None) -> dict:
    """One CAS submission; an already-published candidate is read back only."""
    _separate(source, candidate)
    _hash(expected_base); _hash(expected_candidate); _hash(preview_token)
    if (source._current() or {}).get("current") == expected_candidate:
        edition = source.edition(expected_candidate)
        if edition.get("group_repair", {}).get("base_edition_id") != expected_base:
            raise HistoryUnavailable("history_group_repair_claim_mismatch")
        return {"status": "already_published", "edition_id": expected_candidate}
    try:
        with candidate._writer(), source._writer():
            preview = preview_group_repair(source, candidate, expected_base=expected_base,
                                          expected_candidate=expected_candidate, deadline_monotonic=deadline_monotonic)
            if preview["preview_token"] != preview_token:
                raise HistoryUnavailable("history_group_repair_preview_mismatch")
            claim = preview["repair_claim"]
            if revalidate() != claim:
                return {"status": "superseded", "last_good_retained": True}
            edition = _read(candidate.root / "editions" / (expected_candidate + ".json"))
            paths = [("objects", _hash(edition["days"][day]) + ".sqlite3") for day, old_id in claim["base_objects"].items()
                     if edition["days"][day] != old_id]
            paths.append(("editions", expected_candidate + ".json"))
            for folder, name in dict.fromkeys(paths):
                _deadline(deadline_monotonic)
                src, dst = candidate.root / folder / name, source.root / folder / name
                if dst.exists():
                    if not _same_file(src, dst, deadline_monotonic):
                        raise HistoryUnavailable("history_group_repair_object_conflict")
                    continue
                temp = dst.with_name(".building-" + uuid.uuid4().hex)
                try:
                    _copy(src, temp, source, deadline_monotonic)
                    os.replace(temp, dst)
                    with _directory_fd(dst.parent) as fd:
                        os.fsync(fd)
                finally:
                    temp.unlink(missing_ok=True)
            _deadline(deadline_monotonic)
            if revalidate() != claim or hashlib.sha256((source.root / "CURRENT.json").read_bytes()).hexdigest() != claim["base_pointer_sha256"]:
                return {"status": "superseded", "last_good_retained": True}
            _deadline(deadline_monotonic)
            source._reserve(4096)
            _deadline(deadline_monotonic)
            _atomic(source.root / "CURRENT.json", {"current": expected_candidate, "previous": expected_base})
            return {"status": "published", "edition_id": expected_candidate, "previous_edition_id": expected_base}
    except HistoryUnavailable as error:
        if str(error) != "history_group_repair_deadline":
            raise
        return {"status":"pending", "last_good_retained":True}
