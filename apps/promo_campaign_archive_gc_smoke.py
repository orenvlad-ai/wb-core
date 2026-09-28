"""Smoke-check guarded promo archive GC on a temporary fixture."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.promo_campaign_archive_gc import (  # noqa: E402
    EXACT_GC_AUDIT_DIRNAME,
    LIGHT_GC_LOCK_FILENAME,
    LIGHT_GC_STATE_FILENAME,
    _load_light_gc_state,
    _plan_incremental_light_gc_batch,
    _write_private_json,
    apply_exact_gc_plan,
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

        deployed_sha = "b" * 40
        deployed_sha_file = runtime_dir / "deployed-sha"
        deployed_sha_file.write_text(deployed_sha, encoding="utf-8")
        apply_result = apply_exact_gc_plan(
            runtime_dir=runtime_dir,
            report=report,
            fingerprint=report["fingerprint"],
            deployed_sha=deployed_sha,
            deployed_sha_file=deployed_sha_file,
        )
        if apply_result["deleted_count"] != len(removable) or apply_result["errors"]:
            raise AssertionError(f"unexpected apply result: {apply_result}")
        repeated = apply_exact_gc_plan(
            runtime_dir=runtime_dir,
            report=report,
            fingerprint=report["fingerprint"],
            deployed_sha=deployed_sha,
            deployed_sha_file=deployed_sha_file,
        )
        if not repeated["idempotent"] or repeated["applied"]:
            raise AssertionError(f"exact promo GC apply must be idempotent: {repeated}")
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
    _assert_incremental_backlog_and_lock()
    _assert_pending_batch_resume_and_drift()


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


def _normalized_runtime(runtime_dir: Path) -> None:
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


def _old_run(runtime_dir: Path, name: str, status: str, filenames: tuple[str, ...]) -> list[Path]:
    run_dir = runtime_dir / "promo_xlsx_collector_runs" / name
    logs = run_dir / "logs"
    logs.mkdir(parents=True)
    summary = run_dir / "run_summary.json"
    summary.write_text(json.dumps({"status": status}) + "\n", encoding="utf-8")
    paths = []
    for filename in filenames:
        path = logs / filename
        path.write_bytes(b"trace")
        paths.append(path)
    old = time.time() - 20 * 86400
    for path in [summary, *paths]:
        os.utime(path, (old, old))
    return paths


def _assert_incremental_backlog_and_lock() -> None:
    with TemporaryDirectory(prefix="promo-light-gc-backlog-") as tmp:
        runtime_dir = (Path(tmp) / "runtime").resolve()
        _normalized_runtime(runtime_dir)
        targets = [
            _old_run(runtime_dir, f"2026-08-{i:03d}__partial", "partial", ("trace.har",))[0]
            for i in range(120)
        ]
        running = _old_run(runtime_dir, "2026-08-999__running", "running", ("trace.har",))[0]
        unknown = _old_run(runtime_dir, "2026-08-998__unknown", "unknown", ("trace.har",))[0]
        current = _old_run(runtime_dir, "2026-08-997__current", "partial", ("trace.har",))[0]
        recent = _old_run(runtime_dir, "2026-08-995__recent", "partial", ("trace.har",))[0]
        os.utime(recent, None)
        protected_run = runtime_dir / "promo_xlsx_collector_runs" / "2026-08-996__unsafe"
        _old_run(runtime_dir, protected_run.name, "partial", ())
        protected_metadata = protected_run / "metadata.json"
        protected_metadata.write_text("{}\n", encoding="utf-8")
        protected_workbook = protected_run / "workbook.xlsx"
        protected_workbook.write_bytes(b"canonical-copy")
        hardlink_target = protected_run / "hardlink-target"
        hardlink_target.write_bytes(b"trace")
        hardlink = protected_run / "logs" / "hardlink.har"
        os.link(hardlink_target, hardlink)
        symlink = protected_run / "logs" / "symlink.har"
        symlink.symlink_to(protected_metadata)
        old = time.time() - 20 * 86400
        for path in (protected_metadata, protected_workbook, hardlink_target, hardlink, symlink):
            os.utime(path, (old, old), follow_symlinks=False)

        gc_dir = runtime_dir / EXACT_GC_AUDIT_DIRNAME
        gc_dir.mkdir()
        lock_fd = os.open(gc_dir / LIGHT_GC_LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = run_promo_campaign_archive_light_gc(runtime_dir=runtime_dir)
            if locked["status"] != "skipped" or locked["deleted_count"]:
                raise AssertionError(f"concurrent GC must skip: {locked}")
        finally:
            os.close(lock_fd)

        deleted = 0
        for _ in range(6):
            summary = run_promo_campaign_archive_light_gc(
                runtime_dir=runtime_dir, current_run_dirs=[current.parent.parent],
                max_duration_seconds=0.25, max_files=1, max_runs=5,
            )
            deleted += summary["deleted_count"]
        if deleted < 4:
            raise AssertionError(f"bounded GC did not progress through backlog: deleted={deleted}")
        if sum(path.exists() for path in targets) != len(targets) - deleted:
            raise AssertionError("bounded GC did not delete exactly its reported targets")
        if not all(path.exists() for path in (
            running, unknown, current, recent, hardlink, symlink,
            protected_metadata, protected_workbook,
        )):
            raise AssertionError("bounded GC crossed protected status or file boundary")


def _assert_pending_batch_resume_and_drift() -> None:
    with TemporaryDirectory(prefix="promo-light-gc-resume-") as tmp:
        runtime_dir = (Path(tmp) / "runtime").resolve()
        _normalized_runtime(runtime_dir)
        first, second = _old_run(
            runtime_dir, "2026-08-001__partial", "partial", ("first.har", "second.har"),
        )
        gc_dir = runtime_dir / EXACT_GC_AUDIT_DIRNAME
        gc_dir.mkdir()
        state_path = gc_dir / LIGHT_GC_STATE_FILENAME
        state = _load_light_gc_state(state_path, runtime_dir)
        batch = _plan_incremental_light_gc_batch(
            runtime_dir=runtime_dir, state=state, protected_run_dirs=(),
            success_debug_ttl_days=3, failed_debug_ttl_days=14,
            deadline=time.perf_counter() + 5, max_files=2,
            max_bytes=1024, max_runs=5,
        )
        if len(batch["plan"]) != 2:
            raise AssertionError(f"test fixture did not make a two-file batch: {batch}")
        state["pending"] = batch
        _write_private_json(state_path, state)
        first.unlink()
        second.write_bytes(b"other")
        old = time.time() - 20 * 86400
        os.utime(second, (old, old))
        summary = run_promo_campaign_archive_light_gc(runtime_dir=runtime_dir)
        if summary["already_missing_count"] != 1 or summary["apply_error_count"] != 1:
            raise AssertionError(f"pending resume/drift was not accounted for: {summary}")
        if not second.exists():
            raise AssertionError("drifted candidate was deleted")
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        if saved["pending"] is not None or saved["sequence"] != 1:
            raise AssertionError("completed pending batch did not commit durable progress")
        receipt = gc_dir / "light-gc-receipt-00000001.json"
        if not receipt.is_file():
            raise AssertionError("completed batch lacks durable receipt")
        # Simulate a crash after the receipt fsync but before the state update.
        _write_private_json(state_path, state)
        recovered = run_promo_campaign_archive_light_gc(runtime_dir=runtime_dir)
        if recovered["deleted_count"] or recovered["status"] != "success":
            raise AssertionError(f"receipt recovery replayed a completed batch: {recovered}")
        if json.loads(state_path.read_text(encoding="utf-8"))["sequence"] != 1:
            raise AssertionError("receipt recovery did not restore completed sequence")
        again = run_promo_campaign_archive_light_gc(
            runtime_dir=runtime_dir, max_files=1, max_runs=2,
        )
        if again["already_missing_count"]:
            raise AssertionError(f"completed batch was replayed: {again}")


if __name__ == "__main__":
    main()
