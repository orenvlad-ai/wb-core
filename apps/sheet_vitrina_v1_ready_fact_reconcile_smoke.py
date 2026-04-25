"""Targeted smoke for ready-snapshot fact reconciliation into accepted source slots."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_plan_report import SheetVitrinaV1PlanReportBlock
from packages.application.web_vitrina_ready_fact_reconcile import (
    TEMPORAL_ROLE_ACCEPTED_CLOSED,
    apply_ready_fact_reconcile,
    dry_run_ready_fact_reconcile,
)
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)


BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 3, 4, 9, 0, tzinfo=timezone.utc)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ready-fact-reconcile-") as tempdir:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tempdir) / "runtime")
        accepted = runtime.ingest_bundle(
            json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8")),
            activated_at="2026-03-04T09:00:00Z",
        )
        if accepted.status != "accepted":
            raise AssertionError(f"bundle fixture must be accepted, got {accepted}")
        current_state = runtime.load_current_state()
        active_nm_ids = [int(item.nm_id) for item in current_state.config_v2 if item.enabled][:2]

        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-03-04T09:05:00Z",
            plan=_ready_plan("2026-03-01", active_nm_ids, buyout_values=[100.0, 200.0], ads_values=[10.0, 20.0]),
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-03-04T09:06:00Z",
            plan=_ready_plan("2026-03-02", active_nm_ids, buyout_values=["", ""], ads_values=[30.0, 40.0]),
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-03-04T09:07:00Z",
            plan=_ready_plan("2026-03-03", active_nm_ids, buyout_values=[300.0, 400.0], ads_values=[50.0, 60.0]),
        )
        _seed_accepted(runtime=runtime, source_key="fin_report_daily", metric_key="fin_buyout_rub", date="2026-03-03", active_nm_ids=active_nm_ids, values=[300.0, 400.0])
        _seed_accepted(runtime=runtime, source_key="ads_compact", metric_key="ads_sum", date="2026-03-03", active_nm_ids=active_nm_ids, values=[50.0, 60.0])

        dry_run = dry_run_ready_fact_reconcile(
            runtime=runtime,
            date_from="2026-03-01",
            date_to="2026-03-03",
        )
        if dry_run["action_counts"].get("insert_missing_accepted_snapshot") != 3:
            raise AssertionError(f"dry-run must plan three inserts, got {dry_run['action_counts']}")
        if dry_run["action_counts"].get("skip_no_ready_metric_values") != 1:
            raise AssertionError(f"dry-run must skip one blank source metric, got {dry_run['action_counts']}")
        if dry_run["action_counts"].get("skip_existing_matches_ready_snapshot") != 2:
            raise AssertionError(f"dry-run must skip two existing matching snapshots, got {dry_run['action_counts']}")

        applied = apply_ready_fact_reconcile(
            runtime=runtime,
            date_from="2026-03-01",
            date_to="2026-03-03",
            captured_at="2026-03-04T09:10:00Z",
        )
        if applied["applied_insert_count"] != 3:
            raise AssertionError(f"apply must insert three snapshots, got {applied}")

        fin_missing, _ = runtime.load_temporal_source_slot_snapshot(
            source_key="fin_report_daily",
            snapshot_date="2026-03-02",
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        )
        if fin_missing is not None:
            raise AssertionError("blank ready buyout metric must not fabricate an accepted fin snapshot")

        report = SheetVitrinaV1PlanReportBlock(runtime=runtime, now_factory=lambda: NOW).build(
            period="current_year",
            h1_buyout_plan_rub=18100.0,
            h2_buyout_plan_rub=18400.0,
            plan_drr_pct=10.0,
            as_of_date="2026-03-03",
        )
        ytd = report["periods"]["year_to_date"]
        if ytd["status"] != "partial":
            raise AssertionError(f"source-specific missing metric must keep the block partial, got {ytd}")
        if ytd["metrics"]["buyout_rub"]["fact"] != 1000.0:
            raise AssertionError(f"buyout fact must use available buyout dates only, got {ytd['metrics']['buyout_rub']}")
        if ytd["metrics"]["ads_sum_rub"]["fact"] != 210.0:
            raise AssertionError(f"ads fact must use available ads dates only, got {ytd['metrics']['ads_sum_rub']}")
        if ytd["coverage"]["buyout_daily_covered_day_count"] != 2 or ytd["coverage"]["ads_daily_covered_day_count"] != 3:
            raise AssertionError(f"coverage must expose per-source daily counts, got {ytd['coverage']}")

        print("ready_fact_reconcile_dry_run: ok ->", dry_run["action_counts"])
        print("ready_fact_reconcile_apply: ok ->", applied["applied_insert_count"])
        print("ready_fact_reconcile_no_fake_daily: ok ->", fin_missing)
        print("ready_fact_reconcile_plan_report_source_specific: ok ->", ytd["metrics"]["buyout_rub"]["fact"], ytd["metrics"]["ads_sum_rub"]["fact"])


def _ready_plan(
    snapshot_date: str,
    active_nm_ids: list[int],
    *,
    buyout_values: list[float | str],
    ads_values: list[float | str],
) -> SheetVitrinaV1Envelope:
    rows = []
    for nm_id, buyout_value, ads_value in zip(active_nm_ids, buyout_values, ads_values, strict=True):
        rows.append([f"SKU {nm_id}: Выкуп", f"SKU:{nm_id}|fin_buyout_rub", buyout_value])
        rows.append([f"SKU {nm_id}: Реклама", f"SKU:{nm_id}|ads_sum", ads_value])
    return SheetVitrinaV1Envelope(
        plan_version="sheet_vitrina_v1_historical_import_v1__sheet_scaffold_v1",
        snapshot_id=f"{snapshot_date}__ready_fact_reconcile_smoke",
        as_of_date=snapshot_date,
        date_columns=[snapshot_date],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="historical_import",
                slot_label="Historical import",
                column_date=snapshot_date,
            )
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect=f"A1:C{len(rows) + 1}",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", snapshot_date],
                rows=rows,
                row_count=len(rows),
                column_count=3,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K1",
                clear_range="A:K",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=[
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
                ],
                rows=[],
                row_count=0,
                column_count=11,
            ),
        ],
    )


def _seed_accepted(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    source_key: str,
    metric_key: str,
    date: str,
    active_nm_ids: list[int],
    values: list[float],
) -> None:
    runtime.save_temporal_source_slot_snapshot(
        source_key=source_key,
        snapshot_date=date,
        snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        captured_at=f"{date}T12:00:00Z",
        payload={
            "kind": "success",
            "snapshot_date": date,
            "count": len(active_nm_ids),
            "items": [
                {"nm_id": nm_id, metric_key: value}
                for nm_id, value in zip(active_nm_ids, values, strict=True)
            ],
        },
    )


if __name__ == "__main__":
    main()
