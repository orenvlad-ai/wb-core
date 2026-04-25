"""Targeted smoke-check for the sheet_vitrina_v1 plan-report builder."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_plan_report import (
    BASELINE_TEMPLATE_HEADERS,
    MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
    SheetVitrinaV1PlanReportBlock,
)
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 21, 1, 0, tzinfo=timezone.utc)
REFERENCE_DATE = "2026-04-20"
ACCEPTED_ROLE = "accepted_closed_day_snapshot"
H1_PLAN_RUB = 271500.0
H2_PLAN_RUB = 552000.0


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-plan-report-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        result = runtime.ingest_bundle(bundle, activated_at="2026-04-21T01:00:00Z")
        if result.status != "accepted":
            raise AssertionError(f"bundle ingest must be accepted, got {result}")

        current_state = runtime.load_current_state()
        active_nm_ids = [item.nm_id for item in current_state.config_v2 if item.enabled]
        if not active_nm_ids:
            raise AssertionError("fixture must expose at least one active SKU")

        primary_nm_id = active_nm_ids[0]
        for snapshot_day in _iter_dates(date(2026, 1, 1), date.fromisoformat(REFERENCE_DATE)):
            snapshot_date = snapshot_day.isoformat()
            fin_result_payload = {
                "kind": "success",
                "snapshot_date": snapshot_date,
                "count": 1,
                "items": [
                    {
                        "nm_id": primary_nm_id,
                        "fin_buyout_rub": 1500.0,
                    }
                ],
                "storage_total": {
                    "nm_id": 0,
                    "fin_storage_fee_total": 0.0,
                },
            }
            runtime.save_temporal_source_slot_snapshot(
                source_key="fin_report_daily",
                snapshot_date=snapshot_date,
                snapshot_role=ACCEPTED_ROLE,
                captured_at=f"{snapshot_date}T12:00:00Z",
                payload=(
                    fin_result_payload
                    if snapshot_date == REFERENCE_DATE
                    else {"result": fin_result_payload}
                ),
            )
            ads_result_payload = {
                "kind": "success",
                "snapshot_date": snapshot_date,
                "count": 1,
                "items": [
                    {
                        "nm_id": primary_nm_id,
                        "ads_sum": 180.0,
                    }
                ],
            }
            runtime.save_temporal_source_slot_snapshot(
                source_key="ads_compact",
                snapshot_date=snapshot_date,
                snapshot_role=ACCEPTED_ROLE,
                captured_at=f"{snapshot_date}T12:05:00Z",
                payload=(
                    ads_result_payload
                    if snapshot_date == REFERENCE_DATE
                    else {"result": ads_result_payload}
                ),
            )
        runtime.save_temporal_source_slot_snapshot(
            source_key="fin_report_daily",
            snapshot_date="2026-04-21",
            snapshot_role=ACCEPTED_ROLE,
            captured_at="2026-04-21T12:00:00Z",
            payload={
                "result": {
                    "kind": "success",
                    "snapshot_date": "2026-04-21",
                    "count": 1,
                    "items": [{"nm_id": primary_nm_id, "fin_buyout_rub": 999999.0}],
                }
            },
        )
        runtime.save_temporal_source_slot_snapshot(
            source_key="ads_compact",
            snapshot_date="2026-04-21",
            snapshot_role=ACCEPTED_ROLE,
            captured_at="2026-04-21T12:05:00Z",
            payload={
                "result": {
                    "kind": "success",
                    "snapshot_date": "2026-04-21",
                    "count": 1,
                    "items": [{"nm_id": primary_nm_id, "ads_sum": 99999.0}],
                }
            },
        )

        block = SheetVitrinaV1PlanReportBlock(runtime=runtime, now_factory=lambda: NOW)
        payload = block.build(
            period="last_30_days",
            h1_buyout_plan_rub=H1_PLAN_RUB,
            h2_buyout_plan_rub=H2_PLAN_RUB,
            plan_drr_pct=10.0,
        )
        if payload.get("status") != "available":
            raise AssertionError(f"plan report must be available, got {payload}")
        if payload.get("reference_date") != REFERENCE_DATE:
            raise AssertionError(f"plan report must default to the previous closed business day, got {payload}")
        if payload.get("effective_as_of_date") != REFERENCE_DATE:
            raise AssertionError(f"plan report must expose effective previous closed day, got {payload}")
        if payload.get("selected_period_key") != "last_30_days":
            raise AssertionError(f"selected period key must be preserved, got {payload}")

        selected = payload["periods"]["selected_period"]
        if selected["date_from"] != "2026-03-22" or selected["date_to"] != REFERENCE_DATE or selected["day_count"] != 30:
            raise AssertionError(f"last_30_days window must span the expected calendar range, got {selected}")
        _assert_close(selected["metrics"]["buyout_rub"]["fact"], 45000.0, "selected buyout fact")
        _assert_close(selected["metrics"]["buyout_rub"]["plan"], 45000.0, "selected buyout plan")
        _assert_close(selected["metrics"]["buyout_rub"]["delta_abs"], 0.0, "selected buyout delta")
        _assert_close(selected["metrics"]["buyout_rub"]["delta_pct"], 0.0, "selected buyout delta_pct")
        if selected["metrics"]["buyout_rub"]["status_label"] != "выполнен":
            raise AssertionError(f"buyout status must be fulfilled when fact equals plan, got {selected}")
        _assert_close(selected["metrics"]["drr_pct"]["fact"], 12.0, "selected drr fact")
        _assert_close(selected["metrics"]["drr_pct"]["plan"], 10.0, "selected drr plan")
        _assert_close(selected["metrics"]["drr_pct"]["delta_pp"], 2.0, "selected drr delta_pp")
        _assert_close(selected["metrics"]["drr_pct"]["delta_pct"], 20.0, "selected drr delta_pct")
        if selected["metrics"]["drr_pct"]["status_label"] != "выше плана":
            raise AssertionError(f"drr status must disclose above-plan overspend, got {selected}")
        _assert_close(selected["metrics"]["ads_sum_rub"]["fact"], 5400.0, "selected ads fact")
        _assert_close(selected["metrics"]["ads_sum_rub"]["plan"], 4500.0, "selected ads plan")
        _assert_close(selected["metrics"]["ads_sum_rub"]["delta_abs"], 900.0, "selected ads delta")
        _assert_close(selected["metrics"]["ads_sum_rub"]["delta_pct"], 20.0, "selected ads delta_pct")

        mtd = payload["periods"]["month_to_date"]
        if mtd["date_from"] != "2026-04-01" or mtd["day_count"] != 20:
            raise AssertionError(f"MTD block must start from month start, got {mtd}")
        _assert_close(mtd["metrics"]["buyout_rub"]["plan"], 30000.0, "mtd buyout plan")
        _assert_close(mtd["metrics"]["ads_sum_rub"]["plan"], 3000.0, "mtd ads plan")

        qtd = payload["periods"]["quarter_to_date"]
        if qtd["date_from"] != "2026-04-01" or qtd["day_count"] != 20:
            raise AssertionError(f"QTD block must start from quarter start, got {qtd}")
        _assert_close(qtd["metrics"]["buyout_rub"]["fact"], 30000.0, "qtd buyout fact")

        ytd = payload["periods"]["year_to_date"]
        if ytd["date_from"] != "2026-01-01" or ytd["day_count"] != 110:
            raise AssertionError(f"YTD block must start from year start, got {ytd}")
        _assert_close(ytd["metrics"]["buyout_rub"]["plan"], 165000.0, "ytd buyout plan")
        _assert_close(ytd["metrics"]["buyout_rub"]["fact"], 165000.0, "ytd buyout fact")
        if ytd["metrics"]["buyout_rub"]["status_label"] != "выполнен":
            raise AssertionError(f"YTD buyout must be marked as fulfilled when fact >= plan, got {ytd}")
        current_day_guard = block.build(
            period="yesterday",
            h1_buyout_plan_rub=H1_PLAN_RUB,
            h2_buyout_plan_rub=H2_PLAN_RUB,
            plan_drr_pct=10.0,
        )
        current_day_selected = current_day_guard["periods"]["selected_period"]
        if current_day_selected["date_to"] != REFERENCE_DATE:
            raise AssertionError(f"default report must exclude current business day, got {current_day_guard}")
        _assert_close(current_day_selected["metrics"]["buyout_rub"]["fact"], 1500.0, "current-day exclusion fact")

        fixed_expectations = {
            "first_quarter": ("2026-01-01", "2026-03-31", 90, "completed", "available", 135000.0),
            "second_quarter": ("2026-04-01", "2026-04-20", 20, "in_progress", "available", 30000.0),
            "third_quarter": ("2026-07-01", "2026-09-30", 0, "not_started", "unavailable", None),
            "fourth_quarter": ("2026-10-01", "2026-12-31", 0, "not_started", "unavailable", None),
            "first_half": ("2026-01-01", "2026-04-20", 110, "in_progress", "available", 165000.0),
            "second_half": ("2026-07-01", "2026-12-31", 0, "not_started", "unavailable", None),
        }
        for period_key, (date_from, date_to, day_count, period_state, status, expected_plan) in fixed_expectations.items():
            fixed_payload = block.build(
                period=period_key,
                h1_buyout_plan_rub=H1_PLAN_RUB,
                h2_buyout_plan_rub=H2_PLAN_RUB,
                plan_drr_pct=10.0,
                as_of_date=REFERENCE_DATE,
            )
            fixed_selected = fixed_payload["periods"]["selected_period"]
            if (
                fixed_selected["date_from"] != date_from
                or fixed_selected["date_to"] != date_to
                or fixed_selected["day_count"] != day_count
                or fixed_selected["period_state"] != period_state
                or fixed_selected["status"] != status
            ):
                raise AssertionError(f"fixed period {period_key} has wrong range/status, got {fixed_selected}")
            if expected_plan is None:
                if fixed_selected["metrics"]["buyout_rub"]["plan"] is not None:
                    raise AssertionError(f"future period {period_key} must not fabricate a plan, got {fixed_selected}")
            else:
                _assert_close(
                    fixed_selected["metrics"]["buyout_rub"]["plan"],
                    expected_plan,
                    f"{period_key} fixed-period plan",
                )

        contract_payload = block.build(
            period="first_quarter",
            h1_buyout_plan_rub=155379879.0,
            h2_buyout_plan_rub=294620120.0,
            plan_drr_pct=6.0,
            as_of_date="2026-04-24",
            use_contract_start_date=True,
            contract_start_date="2026-02-01",
        )
        contract_selected = contract_payload["periods"]["selected_period"]
        if (
            contract_selected["date_from"] != "2026-02-01"
            or contract_selected["date_to"] != "2026-03-31"
            or contract_selected["day_count"] != 59
            or not contract_selected.get("contract_start_applied")
        ):
            raise AssertionError(f"contract start must trim Q1 to Feb+Mar, got {contract_selected}")
        _assert_close(
            contract_selected["metrics"]["buyout_rub"]["fact"],
            59.0 * 1500.0,
            "contract-trimmed q1 buyout fact",
        )
        _assert_close(
            contract_selected["metrics"]["ads_sum_rub"]["fact"],
            59.0 * 180.0,
            "contract-trimmed q1 ads fact",
        )
        _assert_close(
            contract_selected["metrics"]["buyout_rub"]["plan"],
            155379879.0 / 181.0 * 59.0,
            "contract-trimmed q1 h1-denominator buyout plan",
        )
        if "2026-01-31" in (contract_selected.get("coverage") or {}).get("covered_dates", []):
            raise AssertionError(f"contract start must exclude January coverage, got {contract_selected}")
        before_contract_payload = block.build(
            period="first_quarter",
            h1_buyout_plan_rub=155379879.0,
            h2_buyout_plan_rub=294620120.0,
            plan_drr_pct=6.0,
            as_of_date="2026-04-24",
            use_contract_start_date=True,
            contract_start_date="2026-04-01",
        )
        before_contract_selected = before_contract_payload["periods"]["selected_period"]
        if (
            before_contract_selected.get("status") != "unavailable"
            or before_contract_selected.get("period_state") != "not_started"
            or before_contract_selected["metrics"]["buyout_rub"]["plan"] is not None
        ):
            raise AssertionError(
                f"period fully before contract start must be unavailable without fake plan, got {before_contract_selected}"
            )

        missing_day = "2026-04-10"
        runtime.delete_temporal_source_slot_snapshots(
            source_key="ads_compact",
            date_from=missing_day,
            date_to=missing_day,
            snapshot_roles=[ACCEPTED_ROLE],
        )
        unavailable_payload = block.build(
            period="current_month",
            h1_buyout_plan_rub=H1_PLAN_RUB,
            h2_buyout_plan_rub=H2_PLAN_RUB,
            plan_drr_pct=10.0,
            as_of_date=REFERENCE_DATE,
        )
        if unavailable_payload.get("status") != "partial":
            raise AssertionError(
                f"plan report must surface partial coverage when one accepted snapshot is missing, got {unavailable_payload}"
            )
        coverage = unavailable_payload.get("coverage") or {}
        missing_dates_by_source = coverage.get("missing_dates_by_source") or {}
        if missing_dates_by_source.get("ads_compact") != [missing_day]:
            raise AssertionError(f"missing accepted ads snapshots must be surfaced explicitly, got {unavailable_payload}")
        partial_mtd = (unavailable_payload.get("periods") or {}).get("month_to_date") or {}
        if partial_mtd.get("status") != "partial":
            raise AssertionError(f"MTD block must disclose partial coverage, got {unavailable_payload}")

        empty_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "empty")
        empty_result = empty_runtime.ingest_bundle(bundle, activated_at="2026-04-21T01:00:00Z")
        if empty_result.status != "accepted":
            raise AssertionError(f"empty runtime bundle ingest must be accepted, got {empty_result}")
        empty_block = SheetVitrinaV1PlanReportBlock(runtime=empty_runtime, now_factory=lambda: NOW)
        empty_payload = empty_block.build(
            period="yesterday",
            h1_buyout_plan_rub=H1_PLAN_RUB,
            h2_buyout_plan_rub=H2_PLAN_RUB,
            plan_drr_pct=10.0,
            as_of_date=REFERENCE_DATE,
        )
        if empty_payload.get("status") != "unavailable":
            raise AssertionError(f"plan report must be unavailable when no usable snapshots exist, got {empty_payload}")

        partial_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "daily-from-march")
        partial_result = partial_runtime.ingest_bundle(bundle, activated_at="2026-04-21T01:00:00Z")
        if partial_result.status != "accepted":
            raise AssertionError(f"partial runtime bundle ingest must be accepted, got {partial_result}")
        _seed_daily_snapshots(
            partial_runtime,
            primary_nm_id=primary_nm_id,
            date_from=date(2026, 3, 1),
            date_to=date.fromisoformat(REFERENCE_DATE),
        )
        partial_block = SheetVitrinaV1PlanReportBlock(runtime=partial_runtime, now_factory=lambda: NOW)
        partial_payload = partial_block.build(
            period="last_30_days",
            h1_buyout_plan_rub=H1_PLAN_RUB,
            h2_buyout_plan_rub=H2_PLAN_RUB,
            plan_drr_pct=10.0,
            as_of_date=REFERENCE_DATE,
        )
        partial_selected = partial_payload["periods"]["selected_period"]
        partial_ytd = partial_payload["periods"]["year_to_date"]
        if partial_payload.get("status") != "partial":
            raise AssertionError(f"global YTD gaps may make top-level status partial, got {partial_payload}")
        if partial_selected.get("status") != "available":
            raise AssertionError(f"selected period must stay available despite missing Jan/Feb YTD, got {partial_payload}")
        if partial_ytd.get("status") != "partial":
            raise AssertionError(f"YTD must disclose missing Jan/Feb without baseline, got {partial_payload}")
        _assert_close(partial_selected["metrics"]["buyout_rub"]["fact"], 45000.0, "partial selected buyout fact")

        partial_runtime.save_plan_report_monthly_baseline(
            rows=[
                {"month": "2026-01", "fin_buyout_rub": 31000.0, "ads_sum": 3100.0},
                {"month": "2026-02", "fin_buyout_rub": 28000.0, "ads_sum": 2800.0},
            ],
            uploaded_at="2026-04-21T01:00:00Z",
            source_kind=MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            uploaded_filename="baseline.xlsx",
            uploaded_content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            workbook_checksum="test-checksum",
        )
        mixed_payload = partial_block.build(
            period="current_year",
            h1_buyout_plan_rub=H1_PLAN_RUB,
            h2_buyout_plan_rub=H2_PLAN_RUB,
            plan_drr_pct=10.0,
            as_of_date=REFERENCE_DATE,
        )
        mixed_ytd = mixed_payload["periods"]["year_to_date"]
        if mixed_ytd.get("status") != "available":
            raise AssertionError(f"YTD must become available after Jan/Feb baseline plus March+ daily, got {mixed_payload}")
        source_mix = mixed_ytd.get("source_mix") or {}
        baseline_mix = source_mix.get("manual_monthly_plan_report_baseline") or {}
        daily_mix = source_mix.get("daily_accepted_snapshots") or {}
        if baseline_mix.get("months") != ["2026-01", "2026-02"] or daily_mix.get("day_count") != 51:
            raise AssertionError(f"mixed YTD source mix must disclose Jan/Feb baseline and March+ daily, got {mixed_payload}")
        _assert_close(mixed_ytd["metrics"]["buyout_rub"]["fact"], 135500.0, "mixed ytd buyout fact")
        _assert_close(mixed_ytd["metrics"]["ads_sum_rub"]["fact"], 15080.0, "mixed ytd ads fact")
        _assert_close(mixed_ytd["metrics"]["ads_sum_rub"]["plan"], 16500.0, "mixed ytd ads plan")
        _assert_close(mixed_ytd["metrics"]["drr_pct"]["fact"], 15080.0 / 135500.0 * 100.0, "mixed ytd drr fact")

        wb_plan_payload = block.build(
            period="current_year",
            h1_buyout_plan_rub=155379879.0,
            h2_buyout_plan_rub=294620120.0,
            plan_drr_pct=6.0,
            as_of_date="2026-04-24",
        )
        wb_ytd = wb_plan_payload["periods"]["year_to_date"]
        wb_mtd = wb_plan_payload["periods"]["month_to_date"]
        wb_qtd = wb_plan_payload["periods"]["quarter_to_date"]
        expected_ytd_plan = 155379879.0 / 181.0 * 114.0
        expected_mtd_qtd_plan = 155379879.0 / 181.0 * 24.0
        _assert_close(wb_ytd["metrics"]["buyout_rub"]["plan"], expected_ytd_plan, "2026-04-24 ytd h1/h2 plan")
        _assert_close(wb_mtd["metrics"]["buyout_rub"]["plan"], expected_mtd_qtd_plan, "2026-04-24 mtd h1/h2 plan")
        _assert_close(wb_qtd["metrics"]["buyout_rub"]["plan"], expected_mtd_qtd_plan, "2026-04-24 qtd h1/h2 plan")

        split_payload = block.build(
            period="last_30_days",
            h1_buyout_plan_rub=181000.0,
            h2_buyout_plan_rub=368000.0,
            plan_drr_pct=10.0,
            as_of_date="2026-07-10",
        )
        split_selected = split_payload["periods"]["selected_period"]
        _assert_close(
            split_selected["metrics"]["buyout_rub"]["plan"],
            (181000.0 / 181.0 * 20.0) + (368000.0 / 184.0 * 10.0),
            "h1/h2 split plan across Jun/Jul",
        )

        reconcile_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "reconciliation-controls")
        reconcile_result = reconcile_runtime.ingest_bundle(bundle, activated_at="2026-04-21T01:00:00Z")
        if reconcile_result.status != "accepted":
            raise AssertionError(f"reconciliation runtime bundle ingest must be accepted, got {reconcile_result}")
        reconcile_runtime.save_plan_report_monthly_baseline(
            rows=[
                {"month": "2026-01", "fin_buyout_rub": 27444563.5, "ads_sum": 2783637.0},
                {"month": "2026-02", "fin_buyout_rub": 27444563.5, "ads_sum": 2783637.0},
            ],
            uploaded_at="2026-04-21T01:00:00Z",
            source_kind=MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            uploaded_filename="baseline.xlsx",
            uploaded_content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            workbook_checksum="test-checksum",
        )
        _seed_daily_snapshots(
            reconcile_runtime,
            primary_nm_id=primary_nm_id,
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
            buyout_rub=41917072.0 / 31.0,
            ads_sum=4814777.0 / 31.0,
        )
        _seed_daily_snapshots(
            reconcile_runtime,
            primary_nm_id=primary_nm_id,
            date_from=date(2026, 4, 1),
            date_to=date(2026, 4, 24),
            buyout_rub=39920319.0 / 24.0,
            ads_sum=3826937.0 / 24.0,
        )
        reconcile_block = SheetVitrinaV1PlanReportBlock(runtime=reconcile_runtime, now_factory=lambda: NOW)
        q1_payload = reconcile_block.build(
            period="current_year",
            h1_buyout_plan_rub=155379879.0,
            h2_buyout_plan_rub=294620120.0,
            plan_drr_pct=6.0,
            as_of_date="2026-03-31",
        )
        q1_ytd = q1_payload["periods"]["year_to_date"]
        _assert_close(q1_ytd["metrics"]["buyout_rub"]["fact"], 96806199.0, "q1 manager buyout control")
        _assert_close(q1_ytd["metrics"]["ads_sum_rub"]["fact"], 10382051.0, "q1 manager ads control")
        april_payload = reconcile_block.build(
            period="current_month",
            h1_buyout_plan_rub=155379879.0,
            h2_buyout_plan_rub=294620120.0,
            plan_drr_pct=6.0,
            as_of_date="2026-04-24",
        )
        april_mtd = april_payload["periods"]["month_to_date"]
        _assert_close(april_mtd["metrics"]["buyout_rub"]["fact"], 39920319.0, "apr1-24 buyout control")
        _assert_close(april_mtd["metrics"]["ads_sum_rub"]["fact"], 3826937.0, "apr1-24 ads control")
        contract_control_payload = reconcile_block.build(
            period="first_quarter",
            h1_buyout_plan_rub=155379879.0,
            h2_buyout_plan_rub=294620120.0,
            plan_drr_pct=6.0,
            as_of_date="2026-04-24",
            use_contract_start_date=True,
            contract_start_date="2026-02-01",
        )
        contract_control_selected = contract_control_payload["periods"]["selected_period"]
        _assert_close(
            contract_control_selected["metrics"]["buyout_rub"]["fact"],
            27444563.5 + 41917072.0,
            "contract control Feb+Mar buyout fact",
        )
        _assert_close(
            contract_control_selected["metrics"]["ads_sum_rub"]["fact"],
            2783637.0 + 4814777.0,
            "contract control Feb+Mar ads fact",
        )
        _assert_close(
            contract_control_selected["metrics"]["buyout_rub"]["plan"],
            155379879.0 / 181.0 * 59.0,
            "contract control q1 h1-denominator buyout plan",
        )

        no_double_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "no-double-count")
        no_double_result = no_double_runtime.ingest_bundle(bundle, activated_at="2026-04-21T01:00:00Z")
        if no_double_result.status != "accepted":
            raise AssertionError(f"no-double runtime bundle ingest must be accepted, got {no_double_result}")
        _seed_daily_snapshots(
            no_double_runtime,
            primary_nm_id=primary_nm_id,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
            buyout_rub=100.0,
            ads_sum=10.0,
        )
        no_double_runtime.save_plan_report_monthly_baseline(
            rows=[{"month": "2026-01", "fin_buyout_rub": 999999.0, "ads_sum": 99999.0}],
            uploaded_at="2026-04-21T01:00:00Z",
            source_kind=MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            uploaded_filename="baseline.xlsx",
            uploaded_content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            workbook_checksum="test-checksum",
        )
        no_double_payload = SheetVitrinaV1PlanReportBlock(
            runtime=no_double_runtime,
            now_factory=lambda: NOW,
        ).build(
            period="current_year",
            h1_buyout_plan_rub=18100.0,
            h2_buyout_plan_rub=H2_PLAN_RUB,
            plan_drr_pct=10.0,
            as_of_date="2026-01-31",
        )
        no_double_ytd = no_double_payload["periods"]["year_to_date"]
        if (no_double_ytd.get("source_mix") or {}).get("manual_monthly_plan_report_baseline", {}).get("months"):
            raise AssertionError(f"baseline must not double-count a month with daily facts, got {no_double_payload}")
        _assert_close(no_double_ytd["metrics"]["buyout_rub"]["fact"], 3100.0, "no-double ytd buyout fact")
        _assert_close(no_double_ytd["metrics"]["ads_sum_rub"]["fact"], 310.0, "no-double ytd ads fact")

        partial_overlap_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "baseline-partial-overlap")
        partial_overlap_result = partial_overlap_runtime.ingest_bundle(bundle, activated_at="2026-04-21T01:00:00Z")
        if partial_overlap_result.status != "accepted":
            raise AssertionError(f"partial-overlap runtime bundle ingest must be accepted, got {partial_overlap_result}")
        _seed_daily_snapshots(
            partial_overlap_runtime,
            primary_nm_id=primary_nm_id,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 1),
            buyout_rub=100.0,
            ads_sum=10.0,
        )
        partial_overlap_runtime.save_plan_report_monthly_baseline(
            rows=[{"month": "2026-01", "fin_buyout_rub": 999999.0, "ads_sum": 99999.0}],
            uploaded_at="2026-04-21T01:00:00Z",
            source_kind=MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            uploaded_filename="baseline.xlsx",
            uploaded_content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            workbook_checksum="test-checksum",
        )
        partial_overlap_payload = SheetVitrinaV1PlanReportBlock(
            runtime=partial_overlap_runtime,
            now_factory=lambda: NOW,
        ).build(
            period="current_year",
            h1_buyout_plan_rub=18100.0,
            h2_buyout_plan_rub=H2_PLAN_RUB,
            plan_drr_pct=10.0,
            as_of_date="2026-01-31",
        )
        partial_overlap_ytd = partial_overlap_payload["periods"]["year_to_date"]
        partial_overlap_source = partial_overlap_ytd.get("source_breakdown") or {}
        if partial_overlap_source.get("baseline_months") != ["2026-01"]:
            raise AssertionError(f"baseline must cover a partial-overlap full month, got {partial_overlap_payload}")
        if partial_overlap_source.get("daily_excluded_by_monthly_baseline_dates") != ["2026-01-01"]:
            raise AssertionError(f"daily overlap must be excluded when monthly baseline covers the month, got {partial_overlap_payload}")
        _assert_close(partial_overlap_ytd["metrics"]["buyout_rub"]["fact"], 999999.0, "partial-overlap ytd buyout fact")

        template_bytes, template_filename = block.build_baseline_template()
        template_rows = read_first_sheet_rows(template_bytes)
        if template_filename != "sheet-vitrina-v1-plan-report-baseline-template.xlsx":
            raise AssertionError(f"baseline template filename must be stable, got {template_filename}")
        if (
            template_rows[0] != BASELINE_TEMPLATE_HEADERS
            or not template_rows[1]
            or template_rows[1][0] != "2026-01"
            or not template_rows[2]
            or template_rows[2][0] != "2026-02"
        ):
            raise AssertionError(f"baseline template must expose Russian headers and Jan/Feb rows, got {template_rows}")

        xlsx_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "baseline-xlsx")
        xlsx_block = SheetVitrinaV1PlanReportBlock(runtime=xlsx_runtime, now_factory=lambda: NOW)
        valid_workbook = build_single_sheet_workbook_bytes(
            "План факт месяцы",
            [
                BASELINE_TEMPLATE_HEADERS,
                ["2026-01", 31000.0, 3100.0],
                ["2026-02", 28000.0, 2800.0],
            ],
        )
        upload_payload = xlsx_block.upload_baseline(valid_workbook, uploaded_filename="baseline.xlsx")
        if upload_payload.get("status") != "accepted" or upload_payload.get("accepted_months") != ["2026-01", "2026-02"]:
            raise AssertionError(f"valid Jan/Feb baseline upload must be accepted, got {upload_payload}")
        _expect_value_error(
            lambda: xlsx_block.upload_baseline(
                build_single_sheet_workbook_bytes("План факт месяцы", [BASELINE_TEMPLATE_HEADERS, ["2026-13", 1, 1]])
            ),
            "invalid month",
        )
        _expect_value_error(
            lambda: xlsx_block.upload_baseline(
                build_single_sheet_workbook_bytes("План факт месяцы", [BASELINE_TEMPLATE_HEADERS, ["2026-01", -1, 1]])
            ),
            "negative value",
        )
        _expect_value_error(
            lambda: xlsx_block.upload_baseline(build_single_sheet_workbook_bytes("План факт месяцы", [BASELINE_TEMPLATE_HEADERS])),
            "empty baseline workbook",
        )

        print("plan_report_status: ok ->", payload["status"])
        print("plan_report_selected_period: ok ->", selected["date_from"], selected["date_to"])
        print("plan_report_half_year_plan: ok ->", selected["metrics"]["buyout_rub"]["plan"])
        print("plan_report_mtd_qtd_ytd: ok ->", mtd["day_count"], qtd["day_count"], ytd["day_count"])
        print("plan_report_partial_coverage_guard: ok ->", missing_dates_by_source)
        print("plan_report_unavailable_coverage_guard: ok ->", empty_payload["status"])
        print("plan_report_selected_independent_from_ytd: ok ->", partial_selected["status"], partial_ytd["status"])
        print("plan_report_mixed_baseline_ytd: ok ->", mixed_ytd["status"], baseline_mix["months"])
        print("plan_report_no_double_count: ok ->", no_double_ytd["metrics"]["buyout_rub"]["fact"])
        print("plan_report_partial_overlap_baseline: ok ->", partial_overlap_source["baseline_months"])
        print("plan_report_reconciliation_controls: ok ->", q1_ytd["metrics"]["buyout_rub"]["fact"], april_mtd["metrics"]["buyout_rub"]["fact"])
        print("plan_report_contract_start: ok ->", contract_selected["date_from"], contract_selected["date_to"])
        print("plan_report_baseline_xlsx: ok ->", upload_payload["accepted_months"])


def _iter_dates(date_from: date, date_to: date):
    current = date_from
    while current <= date_to:
        yield current
        current += timedelta(days=1)


def _seed_daily_snapshots(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    primary_nm_id: int,
    date_from: date,
    date_to: date,
    buyout_rub: float = 1500.0,
    ads_sum: float = 180.0,
) -> None:
    for snapshot_day in _iter_dates(date_from, date_to):
        snapshot_date = snapshot_day.isoformat()
        runtime.save_temporal_source_slot_snapshot(
            source_key="fin_report_daily",
            snapshot_date=snapshot_date,
            snapshot_role=ACCEPTED_ROLE,
            captured_at=f"{snapshot_date}T12:00:00Z",
            payload={
                "result": {
                    "kind": "success",
                    "snapshot_date": snapshot_date,
                    "count": 1,
                    "items": [
                        {
                            "nm_id": primary_nm_id,
                            "fin_buyout_rub": buyout_rub,
                        }
                    ],
                    "storage_total": {
                        "nm_id": 0,
                        "fin_storage_fee_total": 0.0,
                    },
                }
            },
        )
        runtime.save_temporal_source_slot_snapshot(
            source_key="ads_compact",
            snapshot_date=snapshot_date,
            snapshot_role=ACCEPTED_ROLE,
            captured_at=f"{snapshot_date}T12:05:00Z",
            payload={
                "result": {
                    "kind": "success",
                    "snapshot_date": snapshot_date,
                    "count": 1,
                    "items": [
                        {
                            "nm_id": primary_nm_id,
                            "ads_sum": ads_sum,
                        }
                    ],
                }
            },
        )


def _assert_close(actual: float | None, expected: float, label: str) -> None:
    if actual is None or abs(actual - expected) > 1e-3:
        raise AssertionError(f"{label} must be {expected}, got {actual}")


def _expect_value_error(callback, label: str) -> None:
    try:
        callback()
    except ValueError:
        return
    raise AssertionError(f"{label} must be rejected")


if __name__ == "__main__":
    main()
