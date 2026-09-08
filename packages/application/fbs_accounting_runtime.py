"""Atomic publication of the admitted snapshot accounting book.

Sources are read only; the separate book retains its opening, every revision,
closed daily costs and the presentation consumed by all current readers.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import zlib

from packages.application.fbs_snapshot_cost import (
    canonical, fingerprint, initialize_candidate, evaluate_candidate,
    close_candidate_period, candidate_period_view,
)
from packages.application.fbs_snapshot_cost_sources import capture_current
from packages.application.fbs_inventory_presentation import (
    FbsInventorySnapshot, capture_retained_stages, SOURCE,
)
from packages.application.shared_sku_cost import SharedSkuCostSnapshot, build_shared_cost_day
from packages.application.shared_sku_cost_sources import capture_wb_component
from packages.application.storage_registry import StoreRegistry
from packages.business_time import current_business_date_iso

SCHEMA = "fbs_active_snapshot_accounting_v1"
TABLES = {"accounting_revisions", "accounting_current", "accounting_blobs"}


def unpack(conn, text):
    index = json.loads(text)
    def blob(key):
        row = conn.execute("SELECT payload FROM accounting_blobs WHERE digest=?", (key,)).fetchone()
        value = json.loads(zlib.decompress(row[0]))
        if fingerprint(value) != key:
            raise ValueError("fbs_accounting_blob_corrupt")
        return value
    for field in ("shared_days", "wb_days", "retained_days", "presentations"):
        index[field] = {day: blob(key) for day, key in index[field].items()}
    index["state"]["baseline"] = blob(index["state"]["baseline"])
    index["state"]["periods"] = {day: blob(key) for day, key in index["state"]["periods"].items()}
    index["state"]["observed_documents"] = blob(index["state"]["observed_documents"])
    return index


def pack(conn, book):
    index = deepcopy(book)
    def blob(value):
        key = fingerprint(value)
        conn.execute("INSERT OR IGNORE INTO accounting_blobs VALUES(?,?)", (key, zlib.compress(canonical(value).encode(), 6)))
        return key
    for field in ("shared_days", "wb_days", "retained_days", "presentations"):
        index[field] = {day: blob(value) for day, value in index[field].items()}
    index["state"]["baseline"] = blob(index["state"]["baseline"])
    index["state"]["periods"] = {day: blob(value) for day, value in index["state"]["periods"].items()}
    index["state"]["observed_documents"] = blob(index["state"]["observed_documents"])
    return canonical(index)


def path(runtime_dir):
    return Path(runtime_dir).resolve() / "fbs-snapshot-accounting.sqlite3"


def admit(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables and tables != TABLES:
        raise ValueError("not_an_isolated_fbs_accounting_book")


def load(runtime_dir, *, version=None):
    file = path(runtime_dir)
    if not file.exists():
        if version is not None:
            raise ValueError("fbs_accounting_bound_revision_missing")
        return None, None
    with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        admit(conn)
        row = (conn.execute("SELECT version,payload FROM accounting_revisions WHERE version=?", (version,)).fetchone()
               if version is not None else conn.execute("SELECT r.version,r.payload FROM accounting_current c JOIN accounting_revisions r USING(version)").fetchone())
        if row is None:
            if version is not None:
                raise ValueError("fbs_accounting_bound_revision_missing")
            return None, None
        book = unpack(conn, row[1])
        if book.get("schema") != SCHEMA or fingerprint(book) != row[0]:
            raise ValueError("fbs_accounting_book_corrupt")
        return book, row[0]


@contextmanager
def writer_lock(runtime_dir):
    Path(runtime_dir).mkdir(parents=True, exist_ok=True)
    with (Path(runtime_dir) / ".fbs-snapshot-accounting.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _save_book(runtime_dir, book, *, expected, operation_id):
    if book.get("schema") != SCHEMA:
        raise ValueError("invalid_accounting_book")
    load(runtime_dir)  # Admit an existing file before any writing connection.
    version = fingerprint(book)
    with closing(sqlite3.connect(path(runtime_dir), timeout=0)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        admit(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS accounting_revisions(version TEXT PRIMARY KEY,operation_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,previous_version TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS accounting_current(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS accounting_blobs(digest TEXT PRIMARY KEY,payload BLOB NOT NULL)")
        old = conn.execute("SELECT version FROM accounting_current").fetchone()
        if (old[0] if old else None) != expected:
            raise ValueError("fbs_accounting_compare_and_swap_failed")
        previous = unpack(conn, conn.execute("SELECT payload FROM accounting_revisions WHERE version=?", (expected,)).fetchone()[0]) if expected else None
        if previous:
            if previous["state"]["baseline"] != book["state"]["baseline"]:
                raise ValueError("accepted_opening_is_immutable")
            for day, period in previous["shared_days"].items():
                if period["status"] == "closed" and book["shared_days"].get(day) != period:
                    raise ValueError("closed_shared_day_is_immutable")
            for day, period in previous["state"]["periods"].items():
                if period["status"] == "closed" and (book["state"]["periods"].get(day) != period
                        or book["presentations"].get(day) != previous["presentations"].get(day)):
                    raise ValueError("closed_accounting_day_is_immutable")
        conn.execute("INSERT INTO accounting_revisions VALUES(?,?,?,?)", (version, operation_id, pack(conn, book), expected))
        conn.execute("INSERT INTO accounting_current VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET version=excluded.version", (version,))
    return version


def _after_book_commit():
    """Deterministic fixture boundary; production performs no additional work."""


def save(runtime_dir, book, *, expected, operation_id):
    """Book-only commit with a durable intent before the separate book commit.

    Management publication is a separate obligation and never follows latest
    book implicitly. The public save always owns the common lock order.
    """
    from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
    inputs = book.get("publication_inputs", {}) if book["active"] else {}
    with warehouse_functional_write_lock(Path(runtime_dir), timeout_seconds=5), writer_lock(runtime_dir):
        return _commit_book_intent(runtime_dir, book, expected=expected, operation_id=operation_id,
                                   inputs=inputs, kind="book_only")


def _commit_book_intent(runtime_dir, book, *, expected, operation_id, inputs, kind):
    """Private phase under the already held warehouse and book owners."""
    from packages.application.ready_publication import check_material, record_intent, complete_publication, capture_authority, check_pinned_authority
    authority = book.get("publication_authority") if kind == "book_only" and book["active"] else None
    authority = authority or capture_authority(runtime_dir)
    db = Path(authority["path"])
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(db, timeout=0)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        check_pinned_authority(conn, authority)
        if inputs:
            check_material(conn, inputs)
        if load(runtime_dir)[1] != expected:
            raise ValueError("fbs_accounting_compare_and_swap_failed")
        record_intent(conn, operation_id=operation_id, attempt_id="1", kind=kind,
            expected=None, inputs={"material": inputs, "authority": authority}, expected_book=expected, book_required=True,
            ready_required=False, created_at=now, book_operation_id=operation_id)
        conn.commit()  # Deliberately durable before the other database commit.
        conn.execute("BEGIN IMMEDIATE")
        check_pinned_authority(conn, authority)
        if inputs:
            check_material(conn, inputs)
        version = _save_book(runtime_dir, book, expected=expected, operation_id=operation_id)
        _after_book_commit()
        complete_publication(conn, operation_id=operation_id, attempt_id="1", book_version=version,
                             after_digest=None, finished_at=now)
        check_pinned_authority(conn, authority)
        conn.commit()
    return version


def _record_prepare_failure(runtime_dir, *, expected, error):
    """Invalidate open operands without replacing ready or replaying old inputs."""
    from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
    from uuid import uuid4
    with warehouse_functional_write_lock(Path(runtime_dir), timeout_seconds=5), writer_lock(runtime_dir):
        prior, version = load(runtime_dir)
        if version != expected or not prior or not prior["active"]:
            return  # A newer successful attempt/pause owns its own quality.
        failed = {**prior, "publication_error": {"reason": str(error)[:500],
                  "at": datetime.now(timezone.utc).isoformat()}}
        _commit_book_intent(runtime_dir, failed, expected=expected, operation_id="failure-" + uuid4().hex,
                            inputs={}, kind="book_quality_failure")


def prepare(runtime_dir, *, now=None, opening=False):
    now = now or datetime.now(timezone.utc)
    before, expected = load(runtime_dir)
    if opening and before is not None:
        raise ValueError("accounting_already_initialized")
    if not opening and (before is None or not before["active"]):
        return None, expected
    from packages.application.ready_publication import readonly, capture_material, capture_authority, check_pinned_authority
    authority = capture_authority(runtime_dir)
    db = Path(authority["path"])
    with readonly(db) as conn:
        check_pinned_authority(conn, authority)
        inputs = capture_material(conn)
        prepared, expected = _prepare_from_snapshot(runtime_dir, db=db, conn=conn, now=now, opening=opening,
                                                   before=before, expected=expected, inputs=inputs)
        prepared["publication_authority"] = authority
        return prepared, expected


def _prepare_from_snapshot(runtime_dir, *, db, conn, now, opening, before, expected, inputs):
    capture = capture_current(db, now=now, include_baseline=opening, connection=conn)
    day = capture["business_date"]
    state = initialize_candidate(capture, open_initial_day=True) if opening else before["state"]
    book = {"schema": SCHEMA, "active": True, "effective_date": day,
            "shared_days": {}, "wb_days": {}, "retained_days": {}, "presentations": {}} if opening else deepcopy(before)
    state = evaluate_candidate(state, capture)
    # A new-day capture first reconciles documents against the saved prior-day
    # quantity, then closes that day and starts the current one. No gap filling.
    target = state["last_attempt"].get("target_date", day)
    if target < day and target in state["periods"]:
        state = close_candidate_period(state, target, today=day)
        book["shared_days"][target] = build_shared_cost_day(state, book["wb_days"][target], target)
        book["presentations"][target] = FbsInventorySnapshot(fbs_state=state,
            wb_capture=book["wb_days"][target], retained=book["retained_days"][target], day=target).payload()
        state = evaluate_candidate(state, capture)
    view = candidate_period_view(state, day)
    if not view["available"]:
        raise ValueError("current_fbs_period_missing:" + str(state["last_attempt"]))
    ids = sorted({int(r["nm_id"]) for r in view["rows"].values()})
    wb = capture_wb_component(db, day=day, nm_ids=ids, connection=conn)
    retained = capture_retained_stages(db, day=day, wb_version_id=wb["version_id"], nm_ids=ids, connection=conn)
    presentation = FbsInventorySnapshot(fbs_state=state, wb_capture=wb, retained=retained, day=day)
    shared = build_shared_cost_day(state, wb, day)
    if opening and (view["quality"] == "incomplete" or view["pending_documents"]
                    or shared["quality"] != "complete"
                    or presentation.payload()["totals"]["total"]["capital_rub"] is None):
        raise ValueError("incomplete_accounting_activation")
    book.update(state=state, prepared_at=capture["captured_at"], source_digest=capture["source_digest"])
    book.pop("publication_error", None)
    book["wb_days"][day] = wb
    book["retained_days"][day] = retained
    book["shared_days"][day] = shared
    book["presentations"][day] = presentation.payload()
    book["publication_inputs"] = inputs
    return book, expected


def refresh(runtime_dir, *, ready_runtime=None):
    prior, prior_version = load(runtime_dir)
    if prior is None or not prior["active"]:
        return {"status": "not_active"}
    if ready_runtime is not None:
        current = ready_runtime.load_current_state()
        plan = ready_runtime.load_sheet_vitrina_ready_snapshot()
        expected_ready = ready_runtime.prepare_sheet_vitrina_ready_publication(
            bundle_version=current.bundle_version, as_of_date=plan.as_of_date)
        from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
        plan = _deserialize_sheet_vitrina_plan(expected_ready.plan_json)
    try:
        book, expected = prepare(runtime_dir)
    except Exception as exc:
        _record_prepare_failure(runtime_dir, expected=prior_version, error=exc)
        raise
    if book is None:
        return {"status": "not_active"}
    operation_id = "refresh-" + fingerprint(book)[7:]
    if ready_runtime is None:
        version = save(runtime_dir, book, expected=expected, operation_id=operation_id)
    else:
        ready_result = ready_runtime.save_sheet_vitrina_ready_snapshot(current_state=current, plan=plan,
            refreshed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            expected=expected_ready, _prepared_book=(book, expected))
        version = fingerprint(book)
        operation_id = ready_result.publication_operation_id
    return {"status": "published", "version": version, "date": max(book["shared_days"]),
            "operation_id": operation_id, "attempt_id": "1", "ready_obligation": "complete" if ready_runtime else "not_applicable",
            "fbs": book["presentations"][max(book["presentations"])]["totals"]["fbs"]}


def publish_ready(runtime):
    """Use the ordinary dated writer after the accounting book is committed."""
    book, _ = load(runtime.runtime_dir)
    if not book or not book["active"]:
        return {"status": "not_active"}
    current = runtime.load_current_state()
    plan = runtime.load_sheet_vitrina_ready_snapshot()
    expected = runtime.prepare_sheet_vitrina_ready_publication(bundle_version=current.bundle_version,
                                                            as_of_date=plan.as_of_date)
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
    if expected.plan_json is None:
        raise ValueError("fbs_ready_target_missing")
    plan = _deserialize_sheet_vitrina_plan(expected.plan_json)
    result = runtime.save_sheet_vitrina_ready_snapshot(current_state=current,
        refreshed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), plan=plan, expected=expected)
    return {"status": result.status}


def current_publication_receipt(runtime, *, now=None):
    """Verify the current owner instead of replaying retired historical costs.

    The hourly runner has already published this book and its ready cells.
    An absent/stale publication must fail, not silently complete the queue.
    Explicit historical parameter requests retain their separate legacy path.
    """
    book, version = load(runtime.runtime_dir)
    if not book or not book["active"]:
        return None
    snapshot = inventory_from_book(book, now=now)
    data = snapshot.payload()
    if data["quality"] == "unavailable":
        raise ValueError("current_accounting_publication_unavailable")
    with closing(sqlite3.connect(Path(runtime.db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute("""SELECT ready.plan_json,p.after_digest,p.operation_id,p.attempt_id
            FROM sheet_vitrina_v1_ready_publications p
            JOIN sheet_vitrina_v1_ready_snapshots ready
              ON ready.bundle_version=p.bundle_version AND ready.as_of_date=p.as_of_date
            JOIN registry_upload_current_state current ON current.bundle_version=ready.bundle_version AND current.slot=1
            WHERE p.state='complete' AND p.book_version=? AND p.ready_required=1
              AND json_extract(ready.plan_json,'$.metadata.fbs_accounting_bindings.' || ? || '.book_version')=?
            ORDER BY p.finished_at DESC,p.operation_id LIMIT 1""", (version, '"' + data["date"] + '"', version)).fetchone()
    from packages.application.ready_publication import digest
    if row is None or digest(row[0]) != row[1]:
        raise ValueError("current_accounting_ready_publication_mismatch:receipt")
    plan = json.loads(row[0]) if row else {}
    cells = plan.get("metadata", {}).get("server_cell_presentation", {})
    checked = 0
    for nm in [None, *data["rows"]]:
        scope = "TOTAL" if nm is None else "SKU:" + nm
        for metric, value in snapshot._metrics(data, nm).items():
            cell = cells.get(scope + "|" + metric, {}).get(data["date"], {})
            if (cell.get("source") != SOURCE or cell.get("candidate_only") is not False
                    or cell.get("source_version_id") != data["version_id"]
                    or cell.get("source_as_of_date") != data["date"]
                    or cell.get("management_value") != snapshot.presentation(value, data=data)["management_value"]):
                raise ValueError("current_accounting_ready_publication_mismatch:" + scope + "|" + metric)
            checked += 1
    return {"status": "published", "source": SOURCE, "business_date": data["date"],
            "operation_id": row[2], "attempt_id": row[3], "ready_digest": row[1],
            "plan_fingerprint": fingerprint({"book": version, "presentation": data["version_id"]}),
            "accounting_version": version, "checked_cell_count": checked,
            "changed_snapshot_count": 0, "database_written": False,
            "historical_replay": False, "backup_archive": None}


def published(value):
    if isinstance(value, dict):
        return {k: False if k == "candidate_only" else published(v) for k, v in value.items()}
    if isinstance(value, list):
        return [published(v) for v in value]
    return value


class ActiveSharedCostSnapshot(SharedSkuCostSnapshot):
    def metadata(self):
        return published(super().metadata())

    def resolve(self, **kwargs):
        return published(super().resolve(**kwargs))

    def closed_for(self, day):
        return day in self._days and self._days[day]["status"] == "closed"


def load_shared(runtime_dir):
    book, _ = load(runtime_dir)
    if book is None or not book["active"]:
        return None
    periods = [p for p in book["shared_days"].values() if not book.get("publication_error") or p["status"] == "closed"]
    return ActiveSharedCostSnapshot(periods, effective_date=book["effective_date"])


class ActiveInventorySnapshot(FbsInventorySnapshot):
    __slots__ = ()

    def payload(self):
        return published(super().payload())

    def presentation(self, value, *, data=None):
        return published(super().presentation(value, data=data))

    def warehouse_detail(self):
        result = published(super().warehouse_detail())
        result["warehouse"]["accounting_effective_date"] = self.payload().get("accounting_effective_date")
        result["warehouse"]["status_description"] = result["warehouse"]["status_description"].replace("Проверочный вариант перехода. ", "")
        if self.payload()["quality"] == "unavailable":
            result["warehouse"].update(status_label="Данные устарели", status_description="Нет свежего согласованного снимка текущего дня.")
        return result

    def _metrics(self, data, nm=None):
        result = super()._metrics(data, nm)
        if data["quality"] == "unavailable":
            from packages.application.web_vitrina_official_fbs import FBS_FACILITY
            prefix = "total_" if nm is None else ""
            for row in data["quantity_snapshot"]["rows"]:
                result[prefix + FBS_FACILITY + row["facility_id"]] = None
        return result

    def planning_payload(self):
        result = published(super().planning_payload())
        names = {k: v.get("facility_name", k) for k, v in self.payload()["quantity_snapshot"].get("facility_evidence", {}).items()}
        for metric in result["metrics"]:
            metric["quality"] = "preliminary" if metric["available"] else "unavailable"
            if metric["metric_key"].startswith("fbs_facility:"):
                facility = metric["metric_key"].split(":", 1)[1]
                metric["label_ru"] = "Остаток FBS: " + names.get(facility, facility)
        for facility in result["fbs"]["facilities"]:
            facility["name"] = names.get(facility["facility_id"], facility["facility_id"])
        return result


def inventory_from_book(book, *, now=None):
    now = now or datetime.now(timezone.utc)
    day = current_business_date_iso(now)
    saved = book["presentations"].get(day)
    # Keep a declared unavailable new-policy object after a missing/stale day;
    # returning None would silently reactivate the old FF source.
    payload = deepcopy(saved or book["presentations"][max(book["presentations"])])
    payload["accounting_effective_date"] = book["effective_date"]
    captured = datetime.fromisoformat(payload["quantity_snapshot"]["captured_at"].replace("Z", "+00:00"))
    if book.get("publication_error") or saved is None or not 0 <= (now - captured).total_seconds() <= 3 * 3600:
        payload["date"] = day
        payload["quality"] = "unavailable"
        def mask(value):
            if isinstance(value, dict):
                return {k: None if k in {"quantity", "capital_rub", "wac_rub", "unit_cost_rub", "wb_physical", "stock_total"} else mask(v) for k, v in value.items()}
            if isinstance(value, list):
                return [mask(v) for v in value]
            return value
        payload = mask(payload)
        # Locations drive facility totals; empty lists prevent arithmetic on
        # unknown quantities. The original source identity remains explicit.
        for row in payload["rows"].values():
            row["locations"] = []
        payload["pending_documents"] = [{"reason": "fresh_current_accounting_snapshot_missing"}]
    result = ActiveInventorySnapshot.__new__(ActiveInventorySnapshot)
    object.__setattr__(result, "_json", json.dumps(payload, ensure_ascii=False))
    return result


def load_inventory(runtime_dir, *, now=None):
    book, _ = load(runtime_dir)
    return inventory_from_book(book, now=now) if book and book["active"] else None


_UNSET = object()


def materialize(plan, *, runtime_dir, now=None, book=_UNSET, book_version=None, ready_target=None):
    """Publish dated cells via the existing ready-plan writer, preserving history."""
    if book is _UNSET:
        book, book_version = load(runtime_dir)
    if not book or not book["active"]:
        return plan
    current = inventory_from_book(book, now=now)
    metadata = deepcopy(dict(plan.metadata or {}))
    cells = metadata.setdefault("server_cell_presentation", {})
    # Only dates from this policy. Closed inventory presentations are immutable
    # in this book; dates before the cutover are never touched.
    for day, payload in book["presentations"].items():
        if day not in plan.date_columns:
            continue
        snapshot = current if day == current.date else ActiveInventorySnapshot.__new__(ActiveInventorySnapshot)
        if day != current.date:
            object.__setattr__(snapshot, "_json", json.dumps(payload))
        for nm in [None, *payload["rows"]]:
            scope = "TOTAL" if nm is None else "SKU:" + nm
            for key, value in snapshot.metrics(nm).items():
                cells.setdefault(scope + "|" + key, {})[day] = snapshot.presentation(value)
    if current.date in plan.date_columns and current.date not in book["presentations"]:
        for nm in [None, *current.payload()["rows"]]:
            scope = "TOTAL" if nm is None else "SKU:" + nm
            for key, value in current.metrics(nm).items():
                cells.setdefault(scope + "|" + key, {})[current.date] = current.presentation(value)
    sheets = []
    for sheet in plan.sheets:
        if sheet.sheet_name != "DATA_VITRINA":
            sheets.append(sheet)
            continue
        rows = [list(row) for row in sheet.rows]
        indexed = {str(row[1]): row for row in rows if len(row) > 1}
        for identity, dates in cells.items():
            own = {d: c for d, c in dates.items() if c.get("source") == SOURCE and d in plan.date_columns and d >= book["effective_date"]}
            if not own:
                continue
            if identity not in indexed:
                row = ["Товарный капитал", identity, *["" for _ in plan.date_columns]]
                rows.append(row)
                indexed[identity] = row
            for day, cell in own.items():
                value = cell["management_value"]
                indexed[identity][plan.date_columns.index(day) + 2] = float(value) if value != "" else ""
        start = re.fullmatch(r"[A-Z]+([1-9][0-9]*)", sheet.write_start_cell)
        if not start:
            raise ValueError("unsupported_data_sheet_start_cell")
        sheets.append(replace(sheet, rows=rows, row_count=len(rows), write_rect=re.sub(r"\d+$", str(len(rows) + int(start[1])), sheet.write_rect)))
    metadata["fbs_accounting"] = {"effective_date": book["effective_date"], "source": SOURCE, "candidate_only": False}
    metadata["fbs_accounting_bindings"] = {
        day: {"book_version": book_version, "presentation_version": payload["version_id"],
              "effective_date": book["effective_date"], "date": day,
              "quality": payload["quality"], "source": SOURCE,
              "ready_target": ready_target}
        for day, payload in book["presentations"].items() if day in plan.date_columns
    }
    return replace(plan, sheets=sheets, metadata=metadata)


def load_management_inventory(runtime_dir, plan, *, now):
    """Resolve only the book revision accepted by this management column.

    Legacy ready keeps its dated materialized cells; it cannot select latest.
    Standalone FF/Finance/planning continue to use their own book contracts.
    """
    current, _ = load(runtime_dir)
    if not current or not current["active"]:
        return None
    day = current_business_date_iso(now)
    binding = dict(plan.metadata or {}).get("fbs_accounting_bindings", {}).get(day)
    if not binding:
        return None
    target = dict(plan.metadata or {}).get("fbs_accounting_targets", {}).get(day)
    if target is None:
        target = dict(plan.metadata or {}).get("ready_publication_target")
    if not target or binding.get("ready_target") != target:
        raise ValueError("fbs_accounting_ready_binding_target_mismatch")
    book, version = load(runtime_dir, version=binding["book_version"])
    payload = book["presentations"].get(day)
    if (not payload or binding.get("date") != day or binding.get("source") != SOURCE
            or payload["version_id"] != binding.get("presentation_version")
            or payload["quality"] != binding.get("quality")
            or book["effective_date"] != binding.get("effective_date")):
        raise ValueError("fbs_accounting_ready_binding_mismatch")
    # Failure/pause belongs to the current activation, not to the old immutable
    # revision. Preserve dated display, but never re-enable a forbidden operand.
    if current.get("publication_error"):
        return None
    inventory = inventory_from_book(book, now=now)
    return inventory if inventory.payload().get("quality") != "unavailable" else None


def publication_status(runtime_dir, *, operation_id, attempt_id="1"):
    """Exact RO reconciliation, including a process death between the two commits."""
    from packages.application.ready_publication import publication_status as read_intent
    db = StoreRegistry(Path(runtime_dir)).resolve("operational")
    intent = read_intent(db, operation_id=operation_id, attempt_id=attempt_id)
    if intent is None:
        return None
    result = {key: intent[key] for key in (
        "operation_id", "attempt_id", "kind", "bundle_version", "as_of_date", "state",
        "expected_digest", "book_required", "ready_required", "book_version", "after_digest")}
    if intent["book_required"] and path(runtime_dir).exists():
        with closing(sqlite3.connect(path(runtime_dir).as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            admit(conn)
            row = conn.execute("SELECT version,payload FROM accounting_revisions WHERE operation_id=?",
                               (intent["book_operation_id"],)).fetchone()
            if row:
                if fingerprint(unpack(conn, row[1])) != row[0]:
                    raise ValueError("fbs_accounting_book_corrupt")
                result["book_version"] = row[0]
                if intent["state"] != "complete":
                    result["state"] = "book_committed_ready_pending" if intent["ready_required"] else "book_committed_ack_pending"
    return result
