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
from packages.application.sheet_vitrina_v1_plan_report import SheetVitrinaV1PlanReportBlock

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 21, 1, 0, tzinfo=timezone.utc)
REFERENCE_DATE = "2026-04-20"
ACCEPTED_ROLE = "accepted_closed_day_snapshot"


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
                                "fin_buyout_rub": 1500.0,
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
                                "ads_sum": 180.0,
                            }
                        ],
                    }
                },
            )

        block = SheetVitrinaV1PlanReportBlock(runtime=runtime, now_factory=lambda: NOW)
        payload = block.build(
            period="last_30_days",
            q1_buyout_plan_rub=90000.0,
            q2_buyout_plan_rub=182000.0,
            q3_buyout_plan_rub=273000.0,
            q4_buyout_plan_rub=365000.0,
            plan_drr_pct=10.0,
        )
        if payload.get("status") != "available":
            raise AssertionError(f"plan report must be available, got {payload}")
        if payload.get("reference_date") != REFERENCE_DATE:
            raise AssertionError(f"plan report must default to the previous closed business day, got {payload}")
        if payload.get("selected_period_key") != "last_30_days":
            raise AssertionError(f"selected period key must be preserved, got {payload}")

        selected = payload["periods"]["selected_period"]
        if selected["date_from"] != "2026-03-22" or selected["date_to"] != REFERENCE_DATE or selected["day_count"] != 30:
            raise AssertionError(f"last_30_days window must cross quarter boundary truthfully, got {selected}")
        _assert_close(selected["metrics"]["buyout_rub"]["fact"], 45000.0, "selected buyout fact")
        _assert_close(selected["metrics"]["buyout_rub"]["plan"], 50000.0, "selected buyout plan")
        _assert_close(selected["metrics"]["buyout_rub"]["delta_abs"], -5000.0, "selected buyout delta")
        _assert_close(selected["metrics"]["buyout_rub"]["delta_pct"], -10.0, "selected buyout delta_pct")
        if selected["metrics"]["buyout_rub"]["status_label"] != "ниже плана":
            raise AssertionError(f"buyout status must be below-plan when fact < plan, got {selected}")
        _assert_close(selected["metrics"]["drr_pct"]["fact"], 12.0, "selected drr fact")
        _assert_close(selected["metrics"]["drr_pct"]["plan"], 10.0, "selected drr plan")
        _assert_close(selected["metrics"]["drr_pct"]["delta_pp"], 2.0, "selected drr delta_pp")
        _assert_close(selected["metrics"]["drr_pct"]["delta_pct"], 20.0, "selected drr delta_pct")
        if selected["metrics"]["drr_pct"]["status_label"] != "выше плана":
            raise AssertionError(f"drr status must disclose above-plan overspend, got {selected}")
        _assert_close(selected["metrics"]["ads_sum_rub"]["fact"], 5400.0, "selected ads fact")
        _assert_close(selected["metrics"]["ads_sum_rub"]["plan"], 5000.0, "selected ads plan")
        _assert_close(selected["metrics"]["ads_sum_rub"]["delta_abs"], 400.0, "selected ads delta")
        _assert_close(selected["metrics"]["ads_sum_rub"]["delta_pct"], 8.0, "selected ads delta_pct")

        mtd = payload["periods"]["month_to_date"]
        if mtd["date_from"] != "2026-04-01" or mtd["day_count"] != 20:
            raise AssertionError(f"MTD block must start from month start, got {mtd}")
        _assert_close(mtd["metrics"]["buyout_rub"]["plan"], 40000.0, "mtd buyout plan")
        _assert_close(mtd["metrics"]["ads_sum_rub"]["plan"], 4000.0, "mtd ads plan")

        qtd = payload["periods"]["quarter_to_date"]
        if qtd["date_from"] != "2026-04-01" or qtd["day_count"] != 20:
            raise AssertionError(f"QTD block must start from quarter start, got {qtd}")
        _assert_close(qtd["metrics"]["buyout_rub"]["fact"], 30000.0, "qtd buyout fact")

        ytd = payload["periods"]["year_to_date"]
        if ytd["date_from"] != "2026-01-01" or ytd["day_count"] != 110:
            raise AssertionError(f"YTD block must start from year start, got {ytd}")
        _assert_close(ytd["metrics"]["buyout_rub"]["plan"], 130000.0, "ytd buyout plan")
        _assert_close(ytd["metrics"]["buyout_rub"]["fact"], 165000.0, "ytd buyout fact")
        if ytd["metrics"]["buyout_rub"]["status_label"] != "выполнен":
            raise AssertionError(f"YTD buyout must be marked as fulfilled when fact >= plan, got {ytd}")

        missing_day = "2026-04-10"
        runtime.delete_temporal_source_slot_snapshots(
            source_key="ads_compact",
            date_from=missing_day,
            date_to=missing_day,
            snapshot_roles=[ACCEPTED_ROLE],
        )
        unavailable_payload = block.build(
            period="yesterday",
            q1_buyout_plan_rub=90000.0,
            q2_buyout_plan_rub=182000.0,
            q3_buyout_plan_rub=273000.0,
            q4_buyout_plan_rub=365000.0,
            plan_drr_pct=10.0,
            as_of_date=REFERENCE_DATE,
        )
        if unavailable_payload.get("status") != "unavailable":
            raise AssertionError(
                f"plan report must stay truthful when accepted snapshots are missing, got {unavailable_payload}"
            )
        coverage = unavailable_payload.get("coverage") or {}
        missing_dates_by_source = coverage.get("missing_dates_by_source") or {}
        if missing_dates_by_source.get("ads_compact") != [missing_day]:
            raise AssertionError(f"missing accepted ads snapshots must be surfaced explicitly, got {unavailable_payload}")

        print("plan_report_status: ok ->", payload["status"])
        print("plan_report_selected_period: ok ->", selected["date_from"], selected["date_to"])
        print("plan_report_cross_quarter_plan: ok ->", selected["metrics"]["buyout_rub"]["plan"])
        print("plan_report_mtd_qtd_ytd: ok ->", mtd["day_count"], qtd["day_count"], ytd["day_count"])
        print("plan_report_missing_snapshot_guard: ok ->", missing_dates_by_source)


def _iter_dates(date_from: date, date_to: date):
    current = date_from
    while current <= date_to:
        yield current
        current += timedelta(days=1)


def _assert_close(actual: float | None, expected: float, label: str) -> None:
    if actual is None or abs(actual - expected) > 1e-6:
        raise AssertionError(f"{label} must be {expected}, got {actual}")


if __name__ == "__main__":
    main()
