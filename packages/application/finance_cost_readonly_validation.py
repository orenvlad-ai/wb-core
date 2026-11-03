"""Offline native Finance validation; never an apply or posting receipt.

The caller supplies a complete isolated runtime copy and the exact expected
book, generation and forward-ingress week identities. No admission, schema,
ingestion, finalizer or supplier operation is performed here.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Sequence

from packages.application import fbs_accounting_runtime as accounting
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock


def _file_identity(path: str | Path) -> list[Any]:
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    return [str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _book_version(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT version FROM accounting_current").fetchall()
    if len(rows) != 1 or not rows[0][0]:
        raise ValueError("finance_validation_book_missing")
    return str(rows[0][0])


def _business_images(images: dict) -> dict:
    """Keep all business columns; ignore only native recalculation timestamps."""
    result = {}
    for table, image in images.items():
        columns = image["columns"]
        keep = [(n, name) for n, name in enumerate(columns)
                if name not in {"calculated_at", "checked_at"}]
        result[table] = {"columns": [name for _, name in keep], "rows": [
            [json.loads(row[n]) if name.endswith("_json") and row[n] is not None
             else row[n] for n, name in keep] for row in image["rows"]]}
    return result


def _raw_evidence(block, conn, start: str, end: str) -> dict:
    args = (block.seller_id, start, end)
    rows = conn.execute("SELECT report_id,rrd_id,row_hash,raw_json FROM "
        "wb_finance_weekly_raw_rows WHERE seller_id=? AND week_start=? "
        "AND week_end=? ORDER BY report_id,rrd_id", args).fetchall()
    reports = conn.execute("SELECT report_id,row_count,content_hash FROM "
        "wb_finance_weekly_reports WHERE seller_id=? AND week_start=? "
        "AND week_end=? ORDER BY report_id", args).fetchall()
    sync = conn.execute("SELECT raw_row_count,report_count,content_hash,status,last_error "
        "FROM wb_finance_weekly_sync WHERE seller_id=? AND week_start=? "
        "AND week_end=?", args).fetchone()
    if not rows or not reports or sync is None:
        raise ValueError("finance_validation_week_evidence_missing")
    by_report: dict[str, list[str]] = {}
    for row in rows:
        raw = json.loads(row["raw_json"])
        if (str(raw.get("reportId")) != str(row["report_id"])
                or str(raw.get("rrdId")) != str(row["rrd_id"])):
            raise ValueError("finance_validation_raw_identity_mismatch")
        if block._row_hash(raw) != row["row_hash"]:
            raise ValueError("finance_validation_raw_hash_mismatch")
        by_report.setdefault(str(row["report_id"]), []).append(row["row_hash"])
    def content_hash(hashes):
        return hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()
    if set(by_report) != {str(row["report_id"]) for row in reports}:
        raise ValueError("finance_validation_report_scope_mismatch")
    for report in reports:
        hashes = by_report[str(report["report_id"])]
        if (report["row_count"] != len(hashes)
                or report["content_hash"] != content_hash(hashes)):
            raise ValueError("finance_validation_report_incomplete")
    if (sync["raw_row_count"] != len(rows) or sync["report_count"] != len(reports)
            or sync["content_hash"] != content_hash([r["row_hash"] for r in rows])
            or sync["status"] not in {"completed", "incomplete_cost", "loaded_preliminary"}
            or sync["last_error"]):
        raise ValueError("finance_validation_sync_incomplete")
    return {"raw_row_count": len(rows), "report_count": len(reports),
            "sync_status": sync["status"],
            "raw_digest": block._json_digest([list(r) for r in rows]),
            "report_digest": block._json_digest([list(r) for r in reports])}


def validate_finance_cost_weeks_readonly(
    runtime_dir: Path, *, expected_book_version: str,
    expected_storage_manifest_sha256: str,
    expected_weeks: Sequence[tuple[str, str]], seller_id: str = "canonical",
) -> dict[str, Any]:
    """Recompute every forward candidate week on a complete offline copy.

    Read-only operations intentionally do not acquire heavy admission or write
    admission state. This separate offline entry is not a runtime finalizer.
    Any missing evidence or concurrent commit aborts without a receipt.
    """
    weeks = list(expected_weeks)
    if not weeks or len(set(weeks)) != len(weeks):
        raise ValueError("finance_validation_explicit_nonempty_weeks_required")
    if not expected_book_version or not expected_storage_manifest_sha256:
        raise ValueError("finance_validation_pinned_provenance_required")
    for start, end in weeks:
        if (date.fromisoformat(end) - date.fromisoformat(start)).days != 6:
            raise ValueError("finance_validation_week_identity_invalid")
    block = WbFinanceWeeklyBlock(Path(runtime_dir), seller_id=seller_id)
    manifest = block.store_registry.load(require_files=True)
    if manifest.manifest_sha256 != expected_storage_manifest_sha256:
        raise ValueError("finance_validation_storage_changed")
    book_path = accounting.path(runtime_dir)
    book_identity = _file_identity(book_path)
    with ExitStack() as stack:
        book_conn = stack.enter_context(closing(sqlite3.connect(
            book_path.as_uri() + "?mode=ro", uri=True)))
        book_conn.execute("PRAGMA query_only=ON")
        book_token = book_conn.execute("PRAGMA data_version").fetchone()[0]
        if _book_version(book_conn) != expected_book_version:
            raise ValueError("finance_validation_book_changed")
        guard = stack.enter_context(closing(block.store_registry.connect(
            "operational", mode="ro", operation="offline_finance_validation_guard",
            manifest=manifest)))
        guard_token = block._sqlite_data_version_token(guard)
        guard_identity = [_file_identity(row[2]) for row in guard.execute(
            "PRAGMA database_list") if row[1] != "temp"]
        authority = []
        conn = stack.enter_context(closing(block._connect_stale_cost_plan(
            storage_authority=authority)))
        if authority[0].manifest_sha256 != expected_storage_manifest_sha256:
            raise ValueError("finance_validation_storage_changed")
        block._assert_readonly_plan_connection(conn)
        token = block._sqlite_data_version_token(conn)
        identities = [_file_identity(row[2]) for row in conn.execute(
            "PRAGMA database_list") if row[1] != "temp"]
        if block.shared_cost_snapshot is None:
            raise ValueError("finance_validation_active_book_required")
        boundary = block.shared_cost_snapshot.effective_date
        # Union catches an expected week whose raw or derived source vanished.
        candidates = set()
        for table in ("wb_finance_weekly_raw_rows", "wb_finance_weekly_reports",
                      "wb_finance_weekly_sync"):
            candidates.update((r[0], r[1]) for r in conn.execute(
                f"SELECT DISTINCT week_start,week_end FROM {table} "
                "WHERE seller_id=? AND week_end>=?", (seller_id, boundary)))
        if candidates != set(weeks):
            raise ValueError("finance_validation_week_scope_mismatch")
        keys = {(seller_id, start, end) for start, end in candidates}
        source = block._finance_source_dependency_fingerprint(
            conn, target_keys=keys, scoped_daily_dependencies=True, force_reload=True)
        target_before = block._finance_target_images(conn, keys)
        results = []
        for start, end in sorted(candidates):
            evidence = _raw_evidence(block, conn, start, end)
            projection = block._build_week_target_projection(
                conn, week_start=date.fromisoformat(start), week_end=date.fromisoformat(end))
            coverage = projection["coverage"]
            quality_blockers = []
            if evidence["sync_status"] == "loaded_preliminary":
                quality_blockers.append("native_source_preliminary")
            if (coverage.get("profit_coverage_status") != "complete"
                    or coverage.get("unmatched_units") != 0
                    or coverage.get("estimated_units") != 0
                    or projection["metrics"].get("cogs") is None):
                quality_blockers.append("native_cost_partial_or_estimated")
            shared_days = []
            for day, nm in block._finance_shared_handoff_scope(conn, {(seller_id, start, end)}):
                if not block.shared_cost_snapshot.applies_to(date.fromisoformat(day)):
                    continue
                period = block.shared_cost_snapshot._days.get(day)
                resolution = block.shared_cost_snapshot.resolve(
                    nm_id=nm, operation_date=date.fromisoformat(day))
                item = {"date": day, "nm_id": nm,
                    "period_status": period.get("status") if period else None,
                    "period_quality": period.get("quality") if period else None,
                    "resolution_status": resolution.get("status"),
                    "resolution_quality": resolution.get("quality"),
                    "reason": resolution.get("reason")}
                shared_days.append(item)
                if (item["period_status"] != "closed"
                        or item["period_quality"] != "complete"
                        or item["resolution_status"] != "resolved"):
                    quality_blockers.append("shared_day_missing_unclosed_or_incomplete")
            actual = block._finance_target_images(conn, {(seller_id, start, end)})
            expected = block._canonicalize_finance_target_images(conn, projection["images"])
            before_business = _business_images(actual)
            expected_business = _business_images(expected)
            changed = [table for table in expected_business
                       if before_business.get(table) != expected_business[table]]
            results.append({"week_start": start, "week_end": end, **evidence,
                "stale": bool(changed), "changed_tables": changed,
                "stored_business_digest": block._json_digest(before_business),
                "expected_business_digest": block._json_digest(expected_business),
                "expected_cost_state_hash": coverage["cost_state_hash"],
                "expected_metrics_digest": block._json_digest(projection["metrics"]),
                "cost_quality": {"profit_coverage_status": coverage["profit_coverage_status"],
                    "unmatched_units": coverage.get("unmatched_units"),
                    "estimated_units": coverage.get("estimated_units"),
                    "quality": coverage.get("quality"), "shared_days": shared_days,
                    "blockers": sorted(set(quality_blockers))}})
        source_after = block._finance_source_dependency_fingerprint(
            conn, target_keys=keys, scoped_daily_dependencies=True, force_reload=True)
        target_after = block._finance_target_images(conn, keys)
        if (source_after != source or target_after != target_before
                or block._sqlite_data_version_token(conn) != token
                or block._sqlite_data_version_token(guard) != guard_token
                or [_file_identity(row[2]) for row in conn.execute("PRAGMA database_list")
                    if row[1] != "temp"] != identities
                or [_file_identity(row[2]) for row in guard.execute("PRAGMA database_list")
                    if row[1] != "temp"] != guard_identity):
            raise ValueError("finance_validation_sqlite_drift")
        if (book_conn.execute("PRAGMA data_version").fetchone()[0] != book_token
                or _book_version(book_conn) != expected_book_version
                or _file_identity(book_path) != book_identity):
            raise ValueError("finance_validation_book_changed")
        if block.store_registry.load(require_files=True).manifest_sha256 != expected_storage_manifest_sha256:
            raise ValueError("finance_validation_storage_changed")
        cost_complete = all(not row["cost_quality"]["blockers"] for row in results)
        receipt = {"schema_version": "wb_finance_cost_readonly_validation_v1",
            "status": "validated_readonly" if cost_complete else "validation_incomplete",
            "offline_copy_only": True, "cost_evidence_complete": cost_complete,
            "eligible_for_completion": False,
            "runtime_mutation": False, "apply_allowed": False, "posting_proof": False,
            "seller_id": seller_id, "accounting_version": expected_book_version,
            "storage_manifest_sha256": expected_storage_manifest_sha256,
            "scope": {"date_from": boundary, "date_to": None,
                      "expected_weeks": [list(w) for w in sorted(weeks)]},
            "shared_cost": block.shared_cost_snapshot.metadata(),
            "source_dependency": source, "target_digest": block._json_digest(target_before),
            "sqlite_identities": identities, "sqlite_data_versions": token,
            "checked_week_count": len(results),
            "stale_week_count": sum(r["stale"] for r in results), "weeks": results}
        receipt["fingerprint"] = block._json_digest(receipt)
        return receipt
