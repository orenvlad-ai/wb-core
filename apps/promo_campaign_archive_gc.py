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
EXACT_GC_CONTRACT_NAME = "promo_campaign_archive_exact_gc_v1"
EXACT_GC_AUDIT_DIRNAME = "promo-campaign-archive-gc"
LIGHT_GC_POLICY_NAME = "promo_refresh_light_gc_v1"
LIGHT_GC_SUCCESS_DEBUG_TTL_DAYS = 3.0
LIGHT_GC_FAILED_DEBUG_TTL_DAYS = 14.0
LIGHT_GC_MAX_DURATION_SECONDS = 20.0
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
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--deployed-sha", default="")
    parser.add_argument(
        "--deployed-sha-file",
        default=str(ROOT / ".wb-core-runtime-sha"),
    )
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
        report["apply_result"] = apply_exact_gc_plan(
            runtime_dir=runtime_dir,
            report=report,
            fingerprint=args.fingerprint,
            deployed_sha=args.deployed_sha,
            deployed_sha_file=Path(args.deployed_sha_file),
        )

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
                protected_run_dirs=(),
                skipped=skipped,
            )
        )
        deletion_plan.extend(
            _plan_duplicate_workbook_files(
                runtime_dir=runtime_dir,
                runs_root=runs_root,
                archive_root=archive_root,
                safe_archive_hashes=safe_archive_hashes,
                protected_run_dirs=(),
                skipped=skipped,
            )
        )
        deletion_plan = _enrich_exact_identities(deletion_plan)

    plan_material = {
        "contract_name": EXACT_GC_CONTRACT_NAME,
        "runtime_dir": str(runtime_dir),
        "archive_root": str(archive_root),
        "runs_root": str(runs_root),
        "success_debug_ttl_days": float(success_debug_ttl_days),
        "failed_debug_ttl_days": float(failed_debug_ttl_days),
        "deletion_plan": deletion_plan,
        "non_target_digest": _candidate_run_non_target_digest(
            runs_root=runs_root,
            deletion_plan=deletion_plan,
        )
        if include_plan
        else "",
    }

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
        "plan_material": plan_material if include_plan else {},
        "fingerprint": _stable_hash(plan_material) if include_plan else "",
        "non_target_digest": plan_material["non_target_digest"],
        "skipped": skipped[:200],
    }


def run_promo_campaign_archive_light_gc(
    *,
    runtime_dir: Path,
    current_run_dirs: list[str | Path] | tuple[str | Path, ...] = (),
    success_debug_ttl_days: float = LIGHT_GC_SUCCESS_DEBUG_TTL_DAYS,
    failed_debug_ttl_days: float = LIGHT_GC_FAILED_DEBUG_TTL_DAYS,
    max_duration_seconds: float = LIGHT_GC_MAX_DURATION_SECONDS,
    policy_name: str = LIGHT_GC_POLICY_NAME,
) -> dict[str, Any]:
    started = time.perf_counter()
    deadline = started + max(0.001, float(max_duration_seconds))
    runtime_dir = Path(runtime_dir).expanduser().resolve()
    try:
        report = build_light_gc_report(
            runtime_dir=runtime_dir,
            current_run_dirs=current_run_dirs,
            success_debug_ttl_days=success_debug_ttl_days,
            failed_debug_ttl_days=failed_debug_ttl_days,
            deadline=deadline,
        )
        apply_result = apply_gc_plan(runtime_dir=runtime_dir, plan=report["deletion_plan"])
        status = "success" if not apply_result.get("errors") else "warning"
        warning = "" if status == "success" else "one_or_more_candidates_failed_to_delete"
        return _light_gc_summary(
            policy_name=policy_name,
            status=status,
            warning=warning,
            started=started,
            report=report,
            apply_result=apply_result,
        )
    except Exception as exc:
        return _light_gc_summary(
            policy_name=policy_name,
            status="warning",
            warning=f"{type(exc).__name__}: {exc}",
            started=started,
            report=None,
            apply_result={"deleted_count": 0, "deleted_size": 0, "errors": []},
        )


def build_light_gc_report(
    *,
    runtime_dir: Path,
    current_run_dirs: list[str | Path] | tuple[str | Path, ...],
    success_debug_ttl_days: float,
    failed_debug_ttl_days: float,
    deadline: float | None,
) -> dict[str, Any]:
    _ensure_before_deadline(deadline)
    archive_root = promo_campaign_archive_root(runtime_dir)
    runs_root = runtime_dir / PROMO_RUNS_DIRNAME
    protected_run_dirs = _normalize_protected_run_dirs(current_run_dirs)
    records = load_promo_campaign_archive(runtime_dir)
    normalized_records = [record for record in records if promo_campaign_has_normalized_rows(record)]
    safe_archive_hashes = {
        str(record.workbook_fingerprint)
        for record in normalized_records
        if str(record.workbook_fingerprint or "").strip()
    }
    skipped: list[dict[str, Any]] = []
    runs = _collect_run_reports(runs_root, deadline=deadline)
    deletion_plan: list[dict[str, Any]] = []
    if not normalized_records:
        skipped.append({"path": str(archive_root), "reason": "normalized_archive_not_ready_skip_light_gc"})
    else:
        deletion_plan.extend(
            _plan_old_debug_files(
                runtime_dir=runtime_dir,
                runs=runs,
                success_debug_ttl_days=success_debug_ttl_days,
                failed_debug_ttl_days=failed_debug_ttl_days,
                protected_run_dirs=protected_run_dirs,
                skipped=skipped,
                deadline=deadline,
            )
        )
        deletion_plan.extend(
            _plan_duplicate_workbook_files(
                runtime_dir=runtime_dir,
                runs_root=runs_root,
                archive_root=archive_root,
                safe_archive_hashes=safe_archive_hashes,
                protected_run_dirs=protected_run_dirs,
                skipped=skipped,
                deadline=deadline,
            )
        )
    _ensure_before_deadline(deadline)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "runtime_dir": str(runtime_dir),
        "archive_root": str(archive_root),
        "runs_root": str(runs_root),
        "protected_run_dirs": [str(path) for path in protected_run_dirs],
        "mode_is_read_only": False,
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


def apply_gc_plan(
    *,
    runtime_dir: Path,
    plan: list[dict[str, Any]],
    allow_missing: bool = False,
    fail_fast: bool = False,
) -> dict[str, Any]:
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
                if allow_missing and not resolved.exists():
                    deleted_count += 1
                    deleted_size += int(item.get("size") or 0)
                    continue
                raise ValueError("candidate is not a regular file")
            file_stat = resolved.stat()
            size = file_stat.st_size
            if int(item.get("size") or -1) != int(size):
                raise ValueError("candidate size drifted from exact plan")
            if item.get("inode") is not None and int(item["inode"]) != int(
                file_stat.st_ino
            ):
                raise ValueError("candidate inode drifted from exact plan")
            if item.get("mtime_ns") is not None and int(
                item["mtime_ns"]
            ) != int(file_stat.st_mtime_ns):
                raise ValueError("candidate mtime drifted from exact plan")
            if item.get("sha256") and str(item["sha256"]) != _sha256_path(
                resolved
            ):
                raise ValueError("candidate SHA-256 drifted from exact plan")
            resolved.unlink()
            _fsync_directory(resolved.parent)
            deleted_count += 1
            deleted_size += size
        except Exception as exc:
            errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            if fail_fast:
                break
    return {
        "deleted_count": deleted_count,
        "deleted_size": deleted_size,
        "errors": errors,
    }


def apply_exact_gc_plan(
    *,
    runtime_dir: Path,
    report: dict[str, Any],
    fingerprint: str,
    deployed_sha: str,
    deployed_sha_file: Path,
) -> dict[str, Any]:
    approved = str(fingerprint or "").strip()
    if not approved:
        raise ValueError("promo GC apply requires the exact dry-run fingerprint")
    _verify_deployed_sha(
        deployed_sha=deployed_sha,
        deployed_sha_file=deployed_sha_file,
    )
    audit_path = _exact_gc_audit_path(
        runtime_dir=runtime_dir,
        fingerprint=approved,
    )
    resumed = audit_path.is_file()
    if resumed:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if str(audit.get("fingerprint") or "") != approved:
            raise ValueError("promo GC audit fingerprint mismatch")
        if str(audit.get("status") or "") == "applied":
            return {
                **dict(audit["result"]),
                "status": "applied",
                "applied": False,
                "idempotent": True,
                "audit_path": str(audit_path),
            }
        plan_material = dict(audit["plan_material"])
    else:
        if approved != str(report.get("fingerprint") or ""):
            raise ValueError(
                "promo GC apply requires the exact current dry-run fingerprint"
            )
        plan_material = dict(report.get("plan_material") or {})
        _write_private_json(
            audit_path,
            {
                "contract_name": EXACT_GC_CONTRACT_NAME,
                "fingerprint": approved,
                "status": "applying",
                "deployed_sha": deployed_sha,
                "started_at": _now(),
                "plan_material": plan_material,
            },
        )
    result = apply_gc_plan(
        runtime_dir=runtime_dir,
        plan=list(plan_material.get("deletion_plan") or []),
        allow_missing=resumed,
        fail_fast=True,
    )
    non_target_after = _candidate_run_non_target_digest(
        runs_root=Path(str(plan_material["runs_root"])),
        deletion_plan=list(plan_material.get("deletion_plan") or []),
    )
    if non_target_after != str(plan_material.get("non_target_digest") or ""):
        raise ValueError("promo GC non-target digest changed during apply")
    result = {
        **result,
        "status": "applied" if not result["errors"] else "partial_failure",
        "applied": not bool(result["errors"]),
        "idempotent": False,
        "fingerprint": approved,
        "non_target_digest_before": plan_material["non_target_digest"],
        "non_target_digest_after": non_target_after,
        "audit_path": str(audit_path),
    }
    _write_private_json(
        audit_path,
        {
            "contract_name": EXACT_GC_CONTRACT_NAME,
            "fingerprint": approved,
            "status": result["status"],
            "deployed_sha": deployed_sha,
            "completed_at": _now(),
            "plan_material": plan_material,
            "result": result,
        },
    )
    return result


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


def _collect_run_reports(runs_root: Path, *, deadline: float | None = None) -> list[dict[str, Any]]:
    if not runs_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    now = time.time()
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        _ensure_before_deadline(deadline)
        size = 0
        file_count = 0
        mtime_max = run_dir.stat().st_mtime
        by_category: dict[str, int] = defaultdict(int)
        for path in _iter_files(run_dir, deadline=deadline):
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
    protected_run_dirs: tuple[Path, ...],
    skipped: list[dict[str, Any]],
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for run in runs:
        _ensure_before_deadline(deadline)
        run_dir = Path(str(run["run_dir"])).resolve()
        if _is_protected_path(run_dir, protected_run_dirs):
            skipped.append({"path": str(run_dir), "reason": "current_run_protected_skip", "status": str(run["status"])})
            continue
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
        for path in _iter_files(run_dir, deadline=deadline):
            _ensure_before_deadline(deadline)
            if not _is_relative_to(path, runtime_dir):
                continue
            if _is_protected_path(path, protected_run_dirs):
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
    protected_run_dirs: tuple[Path, ...],
    skipped: list[dict[str, Any]],
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    if not safe_archive_hashes:
        skipped.append({"path": str(runs_root), "reason": "no_normalized_archive_hashes_for_duplicate_workbook_gc"})
        return plan
    for path in _iter_files(runs_root, deadline=deadline):
        _ensure_before_deadline(deadline)
        if path.suffix.lower() != ".xlsx":
            continue
        if _is_relative_to(path, archive_root):
            continue
        if _is_protected_path(path, protected_run_dirs):
            skipped.append({"path": str(path), "reason": "current_run_protected_skip"})
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


def _iter_files(root: Path, *, deadline: float | None = None):
    if not root.exists():
        return
    for current, _dirs, files in os.walk(root):
        _ensure_before_deadline(deadline)
        for filename in files:
            _ensure_before_deadline(deadline)
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


def _enrich_exact_identities(
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    exact = []
    for item in plan:
        path = Path(str(item["path"]))
        if path.is_symlink() or not path.is_file():
            raise ValueError("promo GC exact candidate is not a regular file")
        file_stat = path.stat()
        exact.append(
            {
                **item,
                "size": int(file_stat.st_size),
                "inode": int(file_stat.st_ino),
                "mtime_ns": int(file_stat.st_mtime_ns),
                "mode": oct(file_stat.st_mode & 0o777),
                "sha256": _sha256_path(path),
            }
        )
    return exact


def _candidate_run_non_target_digest(
    *,
    runs_root: Path,
    deletion_plan: list[dict[str, Any]],
) -> str:
    runs_root = runs_root.resolve()
    excluded = {
        Path(str(item["path"])).resolve() for item in deletion_plan
    }
    run_dirs: set[Path] = set()
    for path in excluded:
        try:
            relative = path.relative_to(runs_root)
        except ValueError as exc:
            raise ValueError("promo GC target escaped runs root") from exc
        if not relative.parts:
            raise ValueError("promo GC target cannot be the runs root")
        run_dirs.add(runs_root / relative.parts[0])
    rows = []
    for run_dir in sorted(run_dirs, key=str):
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise ValueError("promo GC candidate run directory drifted")
        for path in sorted(_iter_files(run_dir), key=str):
            resolved = path.resolve()
            if resolved in excluded:
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError("promo GC non-target is not a regular file")
            file_stat = path.stat()
            rows.append(
                {
                    "path": str(path.relative_to(runs_root)),
                    "size": int(file_stat.st_size),
                    "inode": int(file_stat.st_ino),
                    "mtime_ns": int(file_stat.st_mtime_ns),
                    "sha256": _sha256_path(path),
                }
            )
    return _stable_hash(rows)


def _verify_deployed_sha(
    *,
    deployed_sha: str,
    deployed_sha_file: Path,
) -> None:
    approved = str(deployed_sha or "").strip().lower()
    if len(approved) != 40 or any(
        character not in "0123456789abcdef" for character in approved
    ):
        raise ValueError("promo GC apply requires an exact deployed SHA")
    marker_input = deployed_sha_file.expanduser()
    if marker_input.is_symlink():
        raise ValueError("promo GC deployed SHA marker must not be a symlink")
    marker = marker_input.resolve()
    if not marker.is_file():
        raise ValueError("promo GC deployed SHA marker is unavailable")
    actual = marker.read_text(encoding="utf-8").strip().lower()
    if actual != approved:
        raise ValueError(
            f"promo GC deployed SHA mismatch: expected={approved}, actual={actual}"
        )


def _exact_gc_audit_path(*, runtime_dir: Path, fingerprint: str) -> Path:
    digest = fingerprint.removeprefix("sha256:")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest.lower()
    ):
        raise ValueError("promo GC fingerprint is invalid")
    path = (
        runtime_dir.resolve()
        / EXACT_GC_AUDIT_DIRNAME
        / f"{digest}.json"
    )
    if path.parent.is_symlink():
        raise ValueError("promo GC audit directory must not be a symlink")
    return path


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    if path.parent.is_symlink():
        raise ValueError("private JSON directory must not be a symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        with temp.open("xb") as handle:
            os.chmod(temp, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _normalize_protected_run_dirs(current_run_dirs: list[str | Path] | tuple[str | Path, ...]) -> tuple[Path, ...]:
    normalized: list[Path] = []
    for raw_path in current_run_dirs:
        text = str(raw_path or "").strip()
        if not text:
            continue
        normalized.append(Path(text).expanduser().resolve())
    return tuple(normalized)


def _is_protected_path(path: Path, protected_roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(_is_relative_to(resolved, root) or resolved == root.resolve() for root in protected_roots)


def _skip_reason_counts(skipped: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for item in skipped:
        reason = str(item.get("reason") or "unknown")
        counts[reason] += 1
    return dict(sorted(counts.items()))


def _light_gc_summary(
    *,
    policy_name: str,
    status: str,
    warning: str,
    started: float,
    report: dict[str, Any] | None,
    apply_result: dict[str, Any],
) -> dict[str, Any]:
    skipped = list((report or {}).get("skipped") or [])
    plan_summary = (report or {}).get("deletion_plan_summary") or {"count": 0, "size": 0, "by_reason": {}}
    errors = list(apply_result.get("errors") or [])
    summary = {
        "policy_name": policy_name,
        "status": status,
        "warning": warning,
        "duration_ms": max(0, int(round((time.perf_counter() - started) * 1000))),
        "deleted_count": int(apply_result.get("deleted_count") or 0),
        "freed_bytes": int(apply_result.get("deleted_size") or 0),
        "skipped_count": len(skipped),
        "skip_reasons": _skip_reason_counts(skipped),
        "deletion_plan_summary": plan_summary,
        "apply_error_count": len(errors),
        "errors": errors[:20],
    }
    if report is not None:
        summary["archive"] = report.get("archive") or {}
        summary["protected_run_dirs"] = report.get("protected_run_dirs") or []
    return summary


def _ensure_before_deadline(deadline: float | None) -> None:
    if deadline is not None and time.perf_counter() > deadline:
        raise TimeoutError("promo archive light GC deadline exceeded")


def _emit_report(report: dict[str, Any], report_path: str) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if report_path:
        Path(report_path).write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
