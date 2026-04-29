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

from apps.promo_campaign_archive_gc import apply_gc_plan, build_gc_report  # noqa: E402


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
        plan_paths = {item["path"] for item in report["deletion_plan"]}
        expected_paths = {str(path) for path in removable}
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


if __name__ == "__main__":
    main()
