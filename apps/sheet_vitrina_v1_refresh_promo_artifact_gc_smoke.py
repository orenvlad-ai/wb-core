"""Smoke-check refresh-integrated promo artifact light GC."""

from __future__ import annotations

from pathlib import Path
import sys
import time
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.promo_campaign_archive_integrity_smoke import _write_promo_fixture  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.promo_campaign_archive import (  # noqa: E402
    load_promo_campaign_archive,
    promo_campaign_rows_archive_path,
    promo_campaign_rows_manifest_path,
    sync_promo_campaign_archive,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)


STATUS_HEADER = [
    "source_key",
    "kind",
    "freshness",
    "snapshot_date",
    "date",
    "date_from",
    "date_to",
    "requested_count",
    "covered_count",
    "missing_nm_ids",
    "note",
]


def main() -> None:
    _assert_refresh_runs_light_gc_after_snapshot_save()
    _assert_refresh_surfaces_gc_failure_as_warning()
    print("sheet vitrina refresh promo artifact GC smoke passed")


def _assert_refresh_runs_light_gc_after_snapshot_save() -> None:
    with TemporaryDirectory(prefix="sheet-refresh-promo-gc-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.ingest_bundle_from_path(
            ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json",
            activated_at="2026-04-30T00:00:00Z",
        )
        _write_promo_fixture(
            runtime_dir=runtime_dir,
            run_name="2026-04-26__archive-source",
            promo_folder="3001__4001__complete",
            promo_id=3001,
            period_id=4001,
            title="Complete artifact",
            confidence="high",
            workbook_kind="valid",
        )
        sync_promo_campaign_archive(runtime_dir)
        record = load_promo_campaign_archive(runtime_dir)[0]
        archive_dir = Path(record.archive_dir)
        current_run = runtime_dir / "promo_xlsx_collector_runs" / "2026-04-29__current"
        old_run = runtime_dir / "promo_xlsx_collector_runs" / "2026-04-20__old-success"
        unknown_run = runtime_dir / "promo_xlsx_collector_runs" / "2026-04-19__unknown"
        old_har, current_har, unknown_har = _write_gc_fixture_runs(
            old_run=old_run,
            current_run=current_run,
            unknown_run=unknown_run,
        )

        entrypoint = _entrypoint(runtime_dir=runtime_dir, runtime=runtime)
        entrypoint.sheet_plan_block = _FakePlanBlock(current_run_dir=str(current_run))
        log_lines: list[str] = []
        result = entrypoint._run_sheet_refresh(  # noqa: SLF001 - fixture-level integration smoke
            as_of_date="2026-04-29",
            log=log_lines.append,
        )
        gc_summary = (result.get("refresh_diagnostics") or {}).get("promo_artifact_gc") or {}
        if gc_summary.get("status") != "success":
            raise AssertionError(f"refresh-integrated light GC must succeed in fixture, got {gc_summary}")
        if int(gc_summary.get("deleted_count") or 0) < 1:
            raise AssertionError(f"light GC must delete at least the old successful HAR, got {gc_summary}")
        if old_har.exists():
            raise AssertionError("old successful HAR was not deleted by refresh-integrated light GC")
        if not current_har.exists():
            raise AssertionError("current run HAR must be protected by refresh-integrated light GC")
        if not unknown_har.exists():
            raise AssertionError("unknown run HAR must be skipped by refresh-integrated light GC")
        for path in (
            promo_campaign_rows_archive_path(archive_dir),
            promo_campaign_rows_manifest_path(archive_dir),
            archive_dir / "metadata.json",
            archive_dir / "workbook.xlsx",
        ):
            if not path.exists():
                raise AssertionError(f"refresh-integrated light GC deleted protected archive artifact: {path}")
        if not any("promo_artifact_gc_finish" in line for line in log_lines):
            raise AssertionError(f"GC summary must be surfaced in refresh job log, got {log_lines}")
        persisted = runtime.load_sheet_vitrina_ready_snapshot(as_of_date="2026-04-29")
        persisted_gc = ((persisted.metadata or {}).get("refresh_diagnostics") or {}).get("promo_artifact_gc") or {}
        if persisted_gc.get("policy_name") != "promo_refresh_light_gc_v1":
            raise AssertionError(f"GC summary must be persisted in ready snapshot diagnostics, got {persisted_gc}")


def _assert_refresh_surfaces_gc_failure_as_warning() -> None:
    with TemporaryDirectory(prefix="sheet-refresh-promo-gc-failure-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.ingest_bundle_from_path(
            ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json",
            activated_at="2026-04-30T00:00:00Z",
        )
        current_run = runtime_dir / "promo_xlsx_collector_runs" / "2026-04-29__current"
        current_run.mkdir(parents=True, exist_ok=True)

        def failing_gc_runner(**_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("fixture gc failure")

        entrypoint = _entrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            promo_artifact_gc_runner=failing_gc_runner,
        )
        entrypoint.sheet_plan_block = _FakePlanBlock(current_run_dir=str(current_run))
        result = entrypoint._run_sheet_refresh(  # noqa: SLF001 - fixture-level integration smoke
            as_of_date="2026-04-29",
            log=lambda _line: None,
        )
        gc_summary = (result.get("refresh_diagnostics") or {}).get("promo_artifact_gc") or {}
        if result.get("status") != "success":
            raise AssertionError(f"GC warning must not fail a successful refresh, got {result}")
        if gc_summary.get("status") != "warning" or "fixture gc failure" not in str(gc_summary.get("warning")):
            raise AssertionError(f"GC failure must be surfaced as warning diagnostics, got {gc_summary}")


def _write_gc_fixture_runs(*, old_run: Path, current_run: Path, unknown_run: Path) -> tuple[Path, Path, Path]:
    for run_dir in (old_run, current_run, unknown_run):
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (old_run / "run_summary.json").write_text('{"status":"success"}\n', encoding="utf-8")
    (current_run / "run_summary.json").write_text('{"status":"success"}\n', encoding="utf-8")
    old_har = old_run / "logs" / "session.har"
    current_har = current_run / "logs" / "current.har"
    unknown_har = unknown_run / "logs" / "unknown.har"
    for path, payload in (
        (old_har, b"old"),
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
        time_tuple = (old_mtime, old_mtime)
        path.touch(exist_ok=True)
        __import__("os").utime(path, time_tuple)
    return old_har, current_har, unknown_har


def _entrypoint(
    *,
    runtime_dir: Path,
    runtime: RegistryUploadDbBackedRuntime,
    promo_artifact_gc_runner=None,
) -> RegistryUploadHttpEntrypoint:
    return RegistryUploadHttpEntrypoint(
        runtime_dir=runtime_dir,
        runtime=runtime,
        activated_at_factory=lambda: "2026-04-30T00:00:00Z",
        refreshed_at_factory=lambda: "2026-04-30T00:01:00Z",
        now_factory=lambda: datetime(2026, 4, 30, 0, 0, 0, tzinfo=timezone.utc),
        promo_artifact_gc_runner=promo_artifact_gc_runner,
    )


class _FakePlanBlock:
    def __init__(self, *, current_run_dir: str) -> None:
        self.current_run_dir = current_run_dir

    def build_plan(self, *, as_of_date: str | None, log, execution_mode: str) -> SheetVitrinaV1Envelope:
        if log is not None:
            log("fake_plan_build source=promo_by_price")
        effective_date = str(as_of_date or "2026-04-29")
        refreshed_at = "2026-04-30T00:01:00Z"
        return SheetVitrinaV1Envelope(
            plan_version="delivery_contract_v1__sheet_scaffold_v1",
            snapshot_id=f"{effective_date}__fixture",
            as_of_date=effective_date,
            date_columns=[effective_date, "2026-04-30"],
            temporal_slots=[
                SheetVitrinaV1TemporalSlot(
                    slot_key="yesterday_closed",
                    slot_label="Yesterday closed",
                    column_date=effective_date,
                ),
                SheetVitrinaV1TemporalSlot(
                    slot_key="today_current",
                    slot_label="Today current",
                    column_date="2026-04-30",
                ),
            ],
            source_temporal_policies={"promo_by_price": "dual_day_capable"},
            sheets=[
                SheetVitrinaWriteTarget(
                    sheet_name="DATA_VITRINA",
                    write_start_cell="A1",
                    write_rect="A1:D2",
                    clear_range="A:Z",
                    write_mode="overwrite",
                    partial_update_allowed=False,
                    header=["label", "key", effective_date, "2026-04-30"],
                    rows=[["SKU: Promo", "SKU:210183919|promo_participation", 1.0, 1.0]],
                    row_count=1,
                    column_count=4,
                ),
                SheetVitrinaWriteTarget(
                    sheet_name="STATUS",
                    write_start_cell="A1",
                    write_rect="A1:K2",
                    clear_range="A:Z",
                    write_mode="overwrite",
                    partial_update_allowed=False,
                    header=STATUS_HEADER,
                    rows=[
                        [
                            "promo_by_price",
                            "success",
                            "fresh",
                            effective_date,
                            effective_date,
                            effective_date,
                            effective_date,
                            1,
                            1,
                            "",
                            "archive_mode=fixture",
                        ]
                    ],
                    row_count=1,
                    column_count=len(STATUS_HEADER),
                ),
            ],
            metadata={
                "refresh_diagnostics": {
                    "schema_version": "refresh_diagnostics_v1",
                    "execution_mode": execution_mode,
                    "as_of_date": effective_date,
                    "bundle_version": "fixture",
                    "started_at": refreshed_at,
                    "finished_at": refreshed_at,
                    "duration_ms": 1,
                    "source_slots": [
                        {
                            "source_key": "promo_by_price",
                            "slot_kind": "today_current",
                            "requested_date": "2026-04-30",
                            "status": "success",
                            "semantic_status": "success",
                            "origin": "upstream_fetch",
                            "promo_diagnostics": {
                                "context": {
                                    "current_run_dir": self.current_run_dir,
                                }
                            },
                        }
                    ],
                    "source_summary": [],
                    "phase_summary": [],
                }
            },
        )


if __name__ == "__main__":
    main()
