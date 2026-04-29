"""Guarded promo archive artifact retention runner."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.promo_campaign_archive import (  # noqa: E402
    ARCHIVE_NORMALIZED_ROWS_FILENAME,
    ARCHIVE_NORMALIZED_ROWS_MANIFEST_FILENAME,
    ARCHIVE_RECORD_FILENAME,
    ARCHIVE_METADATA_FILENAME,
    load_promo_campaign_archive,
    promo_campaign_archive_root,
    promo_campaign_has_normalized_rows,
)


PROMO_RUNS_DIRNAME = "promo_xlsx_collector_runs"
REPORT_SCHEMA_VERSION = "promo_campaign_archive_gc_report_v1"
DELETE_DEBUG_EXTENSIONS = {".har", ".png", ".jpg", ".jpeg", ".webp", ".jsonl", ".log", ".txt", ".out", ".err"}
PROTECTED_FILENAMES = {
    ARCHIVE_RECORD_FILENAME,
    ARCHIVE_METADATA_FILENAME,
    ARCHIVE_NORMALIZED_ROWS_FILENAME,
    ARCHIVE_NORMALIZED_ROWS_MANIFEST_FILENAME,
    "metadata.json",
    "card.json",
    "manifest_card.json",
    "timeline_card.json",
    "archive_reuse.json",
    "workbook_inspection.json",
    "run_summary.json",
    "derived_promo_live_source.json",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("audit", "dry-run", "apply"))
    parser.add_argument(
        "--runtime-dir",
        default=os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", "/opt/wb-core-runtime/state"),
    )
    parser.add_argument("--success-debug-ttl-days", type=float, default=7.0)
    parser.add_argument("--failed-debug-ttl-days", type=float, default=14.0)
    parser.add_argument("--report-path", default="")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--apply", dest="apply_flag", action="store_true")
    args = parser.parse_args()

    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    include_plan = args.mode in {"dry-run", "apply"}
    report = build_gc_report(
        runtime_dir=runtime_dir,
        include_plan=include_plan,
        success_debug_ttl_days=args.success_debug_ttl_days,
        failed_debug_ttl_days=args.failed_debug_ttl_days,
    )

    if args.mode == "apply":
        if not (args.confirm or args.apply_flag):
            report["apply_error"] = "apply mode requires explicit --confirm or --apply"
            _emit_report(report, args.report_path)
            raise SystemExit(2)
        report["apply_result"] = apply_gc_plan(runtime_dir=runtime_dir, plan=report["deletion_plan"])

    _emit_report(report, args.report_path)


def build_gc_report(
    *,
    runtime_dir: Path,
    include_plan: bool,
    success_debug_ttl_days: float,
    failed_debug_ttl_days: float,
) -> dict[str, Any]:
    archive_root = promo_campaign_archive_root(runtime_dir)
    runs_root = runtime_dir / PROMO_RUNS_DIRNAME
    records = load_promo_campaign_archive(runtime_dir)
    normalized_records = [record for record in records if promo_campaign_has_normalized_rows(record)]
    safe_archive_hashes = {
        str(record.workbook_fingerprint)
        for record in normalized_records
        if str(record.workbook_fingerprint or "").strip()
    }
    scan = _scan_runtime(runtime_dir)
    runs = _collect_run_reports(runs_root)
    deletion_plan: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    if include_plan:
        deletion_plan.extend(
            _plan_old_debug_files(
                runtime_dir=runtime_dir,
                runs=runs,
                success_debug_ttl_days=success_debug_ttl_days,
                failed_debug_ttl_days=failed_debug_ttl_days,
                skipped=skipped,
            )
        )
        deletion_plan.extend(
            _plan_duplicate_workbook_files(
                runtime_dir=runtime_dir,
                runs_root=runs_root,
                archive_root=archive_root,
                safe_archive_hashes=safe_archive_hashes,
                skipped=skipped,
            )
        )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "runtime_dir": str(runtime_dir),
        "archive_root": str(archive_root),
        "runs_root": str(runs_root),
        "mode_is_read_only": not include_plan,
        "totals": scan,
        "archive": {
            "record_count": len(records),
            "normalized_row_archive_count": len(normalized_records),
            "raw_workbook_record_count": sum(1 for record in records if record.workbook_path),
            "safe_archive_hash_count": len(safe_archive_hashes),
        },
        "runs": {
            "count": len(runs),
            "status_counts": _status_counts(runs),
            "top": sorted(runs, key=lambda item: int(item["size"]), reverse=True)[:20],
        },
        "deletion_plan": deletion_plan,
        "deletion_plan_summary": _plan_summary(deletion_plan),
        "skipped": skipped[:200],
    }


def apply_gc_plan(*, runtime_dir: Path, plan: list[dict[str, Any]]) -> dict[str, Any]:
    deleted_count = 0
    deleted_size = 0
    errors: list[dict[str, Any]] = []
    archive_root = promo_campaign_archive_root(runtime_dir)
    for item in plan:
        path = Path(str(item.get("path") or ""))
        try:
            resolved = path.resolve()
            if not _is_relative_to(resolved, runtime_dir):
                raise ValueError("candidate is outside runtime_dir")
            reason = str(item.get("reason") or "")
            allow_workbook = (
                reason == "duplicate_workbook_copy_after_hash_and_normalized_rows_proof"
                and not _is_relative_to(resolved, archive_root)
            )
            if not _is_allowed_delete_candidate(resolved, allow_workbook=allow_workbook):
                raise ValueError("candidate is protected by filename guard")
            if not resolved.is_file():
                raise ValueError("candidate is not a regular file")
            size = resolved.stat().st_size
            resolved.unlink()
            deleted_count += 1
            deleted_size += size
        except Exception as exc:
            errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    return {
        "deleted_count": deleted_count,
        "deleted_size": deleted_size,
        "errors": errors,
    }


def _scan_runtime(runtime_dir: Path) -> dict[str, Any]:
    by_category: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "size": 0})
    total_size = 0
    file_count = 0
    for path in _iter_files(runtime_dir):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        file_count += 1
        total_size += size
        category = _category_for(path)
        by_category[category]["count"] += 1
        by_category[category]["size"] += size
    return {
        "total_size": total_size,
        "file_count": file_count,
        "by_category": dict(sorted(by_category.items())),
    }


def _collect_run_reports(runs_root: Path) -> list[dict[str, Any]]:
    if not runs_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    now = time.time()
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        size = 0
        file_count = 0
        mtime_max = run_dir.stat().st_mtime
        by_category: dict[str, int] = defaultdict(int)
        for path in _iter_files(run_dir):
            try:
                stat = path.stat()
            except OSError:
                continue
            file_count += 1
            size += stat.st_size
            mtime_max = max(mtime_max, stat.st_mtime)
            by_category[_category_for(path)] += stat.st_size
        status = _run_status(run_dir)
        rows.append(
            {
                "run_dir": str(run_dir),
                "name": run_dir.name,
                "status": status,
                "age_days": round(max(0.0, (now - mtime_max) / 86400), 2),
                "size": size,
                "file_count": file_count,
                "by_category": dict(sorted(by_category.items())),
            }
        )
    return rows


def _plan_old_debug_files(
    *,
    runtime_dir: Path,
    runs: list[dict[str, Any]],
    success_debug_ttl_days: float,
    failed_debug_ttl_days: float,
    skipped: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for run in runs:
        status = str(run["status"])
        age_days = float(run["age_days"])
        if status == "success":
            ttl = success_debug_ttl_days
        elif status in {"partial", "blocked"}:
            ttl = failed_debug_ttl_days
        else:
            skipped.append({"path": run["run_dir"], "reason": "unknown_run_status_skip", "status": status})
            continue
        if age_days < ttl:
            skipped.append({"path": run["run_dir"], "reason": "ttl_not_reached", "status": status, "age_days": age_days, "ttl_days": ttl})
            continue
        for path in _iter_files(Path(run["run_dir"])):
            if not _is_relative_to(path, runtime_dir):
                continue
            if _is_debug_trace_file(path) and _is_allowed_delete_candidate(path):
                plan.append(
                    _plan_item(
                        path=path,
                        reason=f"old_{status}_debug_trace_after_ttl",
                        safety_evidence=f"run_status={status}; age_days={age_days}; ttl_days={ttl}; summary_and_metadata_preserved=true",
                    )
                )
    return plan


def _plan_duplicate_workbook_files(
    *,
    runtime_dir: Path,
    runs_root: Path,
    archive_root: Path,
    safe_archive_hashes: set[str],
    skipped: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    if not safe_archive_hashes:
        skipped.append({"path": str(runs_root), "reason": "no_normalized_archive_hashes_for_duplicate_workbook_gc"})
        return plan
    for path in _iter_files(runs_root):
        if path.suffix.lower() != ".xlsx":
            continue
        if _is_relative_to(path, archive_root):
            continue
        if not _is_allowed_delete_candidate(path, allow_workbook=True):
            continue
        digest = _sha256_path(path)
        if digest not in safe_archive_hashes:
            skipped.append({"path": str(path), "reason": "workbook_hash_not_proven_duplicate"})
            continue
        plan.append(
            _plan_item(
                path=path,
                reason="duplicate_workbook_copy_after_hash_and_normalized_rows_proof",
                safety_evidence=f"sha256={digest}; normalized_archive_hash_match=true; archive_metadata_preserved=true",
            )
        )
    return plan


def _run_status(run_dir: Path) -> str:
    summary_path = run_dir / "run_summary.json"
    if not summary_path.exists():
        return "unknown"
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    status = str(payload.get("status") or "").strip()
    return status or "unknown"


def _plan_item(*, path: Path, reason: str, safety_evidence: str) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "size": stat.st_size,
        "category": _category_for(path),
        "reason": reason,
        "safety_evidence": safety_evidence,
    }


def _plan_summary(plan: list[dict[str, Any]]) -> dict[str, Any]:
    by_reason: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "size": 0})
    for item in plan:
        reason = str(item.get("reason") or "unknown")
        by_reason[reason]["count"] += 1
        by_reason[reason]["size"] += int(item.get("size") or 0)
    return {
        "count": len(plan),
        "size": sum(int(item.get("size") or 0) for item in plan),
        "by_reason": dict(sorted(by_reason.items())),
    }


def _status_counts(runs: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "size": 0})
    for run in runs:
        status = str(run.get("status") or "unknown")
        counts[status]["count"] += 1
        counts[status]["size"] += int(run.get("size") or 0)
    return dict(sorted(counts.items()))


def _iter_files(root: Path):
    if not root.exists():
        return
    for current, _dirs, files in os.walk(root):
        for filename in files:
            yield Path(current) / filename


def _is_debug_trace_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in DELETE_DEBUG_EXTENSIONS:
        return False
    lowered_parts = {part.lower() for part in path.parts}
    lowered_name = path.name.lower()
    return (
        "logs" in lowered_parts
        or "artifacts" in lowered_parts
        or "screenshots" in lowered_parts
        or "failure" in lowered_name
        or "screenshot" in lowered_name
        or suffix in {".har", ".png", ".jpg", ".jpeg", ".webp"}
    )


def _is_allowed_delete_candidate(path: Path, *, allow_workbook: bool = False) -> bool:
    if path.name == "workbook.xlsx" and not allow_workbook:
        return False
    if path.name in PROTECTED_FILENAMES:
        return False
    return path.is_file()


def _category_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return "xlsx_workbooks"
    if suffix == ".har":
        return "har"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "screenshots_images"
    if suffix == ".jsonl":
        return "jsonl"
    if suffix == ".json":
        return "json"
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        return "sqlite_runtime"
    if suffix in {".log", ".txt", ".out", ".err"}:
        return "logs"
    return "other"


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _emit_report(report: dict[str, Any], report_path: str) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if report_path:
        Path(report_path).write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
