"""Smoke-check guarded promo archive GC on a temporary fixture."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.promo_campaign_archive_gc import (  # noqa: E402
    apply_gc_plan,
    build_gc_report,
    run_promo_campaign_archive_light_gc,
)
from apps.promo_campaign_archive_integrity_smoke import _write_promo_fixture  # noqa: E402
from packages.application.promo_campaign_archive import (  # noqa: E402
    load_promo_campaign_archive,
    promo_campaign_rows_archive_path,
    promo_campaign_rows_manifest_path,
    sync_promo_campaign_archive,
)


def main() -> None:
    with TemporaryDirectory(prefix="promo-campaign-archive-gc-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        old_run = runtime_dir / "promo_xlsx_collector_runs" / "2026-03-01__success-old"
        protected_dir = old_run / "promos" / "1001__2001__complete"
        logs_dir = old_run / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        protected_dir.mkdir(parents=True, exist_ok=True)
        removable = [
            logs_dir / "session.har",
            logs_dir / "states.jsonl",
            protected_dir / "card.png",
        ]
        protected = [
            old_run / "run_summary.json",
            protected_dir / "metadata.json",
            protected_dir / "card.json",
            protected_dir / "workbook.xlsx",
        ]
        (old_run / "run_summary.json").write_text('{"status":"success"}\n', encoding="utf-8")
        (protected_dir / "metadata.json").write_text("{}\n", encoding="utf-8")
        (protected_dir / "card.json").write_text("{}\n", encoding="utf-8")
        (protected_dir / "workbook.xlsx").write_bytes(b"not-deleted-by-debug-gc")
        (logs_dir / "session.har").write_bytes(b"h" * 128)
        (logs_dir / "states.jsonl").write_bytes(b"{}\n")
        (protected_dir / "card.png").write_bytes(b"png")
        old_mtime = time.time() - 45 * 86400
        for path in [*removable, *protected, logs_dir, protected_dir, old_run]:
            os.utime(path, (old_mtime, old_mtime))

        report = build_gc_report(
            runtime_dir=runtime_dir,
            include_plan=True,
            success_debug_ttl_days=14,
            failed_debug_ttl_days=30,
        )
        plan_paths = {str(Path(item["path"]).resolve()) for item in report["deletion_plan"]}
        expected_paths = {str(path.resolve()) for path in removable}
        if plan_paths != expected_paths:
            raise AssertionError(f"GC dry-run must plan only debug files: expected={expected_paths}, got={plan_paths}")

        apply_result = apply_gc_plan(runtime_dir=runtime_dir, plan=report["deletion_plan"])
        if apply_result["deleted_count"] != len(removable) or apply_result["errors"]:
            raise AssertionError(f"unexpected apply result: {apply_result}")
        for path in removable:
            if path.exists():
                raise AssertionError(f"debug file was not deleted in temp fixture: {path}")
        for path in protected:
            if not path.exists():
                raise AssertionError(f"protected file was deleted: {path}")
        print(
            "promo campaign archive GC smoke passed: "
            f"deleted_count={apply_result['deleted_count']}; deleted_size={apply_result['deleted_size']}"
        )
    _assert_light_gc_policy()


def _assert_light_gc_policy() -> None:
    with TemporaryDirectory(prefix="promo-campaign-archive-light-gc-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        _write_promo_fixture(
            runtime_dir=runtime_dir,
            run_name="2026-04-26__archive-source",
            promo_folder="2001__3001__complete",
            promo_id=2001,
            period_id=3001,
            title="Complete artifact",
            confidence="high",
            workbook_kind="valid",
        )
        sync_promo_campaign_archive(runtime_dir)
        record = load_promo_campaign_archive(runtime_dir)[0]
        archive_dir = Path(record.archive_dir)
        for path in (
            promo_campaign_rows_archive_path(archive_dir),
            promo_campaign_rows_manifest_path(archive_dir),
            archive_dir / "metadata.json",
            archive_dir / "workbook.xlsx",
        ):
            if not path.exists():
                raise AssertionError(f"normalized/protected archive artifact missing before light GC: {path}")

        runs_root = runtime_dir / "promo_xlsx_collector_runs"
        old_run = runs_root / "2026-04-20__old-success"
        current_run = runs_root / "2026-04-26__current-success"
        unknown_run = runs_root / "2026-04-19__unknown"
        for run_dir in (old_run, current_run, unknown_run):
            (run_dir / "logs").mkdir(parents=True, exist_ok=True)
        (old_run / "run_summary.json").write_text('{"status":"success"}\n', encoding="utf-8")
        (current_run / "run_summary.json").write_text('{"status":"success"}\n', encoding="utf-8")
        old_har = old_run / "logs" / "session.har"
        old_screenshot = old_run / "logs" / "screen.png"
        current_har = current_run / "logs" / "current.har"
        unknown_har = unknown_run / "logs" / "unknown.har"
        for path, payload in (
            (old_har, b"h" * 128),
            (old_screenshot, b"p" * 64),
            (current_har, b"current"),
            (unknown_har, b"unknown"),
        ):
            path.write_bytes(payload)
        old_mtime = time.time() - 5 * 86400
        for path in (
            *old_run.rglob("*"),
            *current_run.rglob("*"),
            *unknown_run.rglob("*"),
            old_run,
            current_run,
            unknown_run,
        ):
            os.utime(path, (old_mtime, old_mtime))

        summary = run_promo_campaign_archive_light_gc(
            runtime_dir=runtime_dir,
            current_run_dirs=[current_run],
            success_debug_ttl_days=3,
            failed_debug_ttl_days=14,
        )
        if summary["status"] != "success" or summary["deleted_count"] < 2:
            raise AssertionError(f"light GC must delete old successful debug traces, got {summary}")
        if old_har.exists() or old_screenshot.exists():
            raise AssertionError("old successful debug traces were not deleted")
        if not current_har.exists():
            raise AssertionError("current run debug trace must be protected")
        if not unknown_har.exists():
            raise AssertionError("unknown run debug trace must be skipped")
        for path in (
            promo_campaign_rows_archive_path(archive_dir),
            promo_campaign_rows_manifest_path(archive_dir),
            archive_dir / "metadata.json",
            archive_dir / "workbook.xlsx",
        ):
            if not path.exists():
                raise AssertionError(f"light GC deleted protected archive artifact: {path}")
        skip_reasons = summary.get("skip_reasons") or {}
        if int(skip_reasons.get("current_run_protected_skip") or 0) < 1:
            raise AssertionError(f"current run protection must be surfaced, got {summary}")
        if int(skip_reasons.get("unknown_run_status_skip") or 0) < 1:
            raise AssertionError(f"unknown run skip must be surfaced, got {summary}")
        print(
            "promo campaign archive light GC smoke passed: "
            f"deleted_count={summary['deleted_count']}; freed_bytes={summary['freed_bytes']}"
        )


if __name__ == "__main__":
    main()
