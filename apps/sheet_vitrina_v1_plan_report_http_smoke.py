"""HTTP integration smoke-check for the sheet_vitrina_v1 plan-report operator surface."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import parse as urllib_parse
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_DAILY_REPORT_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_PLAN_REPORT_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_STOCK_REPORT_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 21, 1, 0, tzinfo=timezone.utc)
REFERENCE_DATE = "2026-04-20"
ACCEPTED_ROLE = "accepted_closed_day_snapshot"


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-plan-report-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        port = _reserve_free_port()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-04-21T01:00:00Z",
            now_factory=lambda: NOW,
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            upload_status, upload_payload = _post_json(f"{base_url}{DEFAULT_UPLOAD_PATH}", bundle)
            if upload_status != 200 or upload_payload.get("status") != "accepted":
                raise AssertionError(f"bundle upload must be accepted, got {upload_status} {upload_payload}")

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
                            "items": [{"nm_id": primary_nm_id, "fin_buyout_rub": 1500.0}],
                            "storage_total": {"nm_id": 0, "fin_storage_fee_total": 0.0},
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
                            "items": [{"nm_id": primary_nm_id, "ads_sum": 180.0}],
                        }
                    },
                )

            operator_status, operator_html = _get_text(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}")
            if operator_status != 200:
                raise AssertionError(f"operator page must return 200, got {operator_status}")
            for expected in (
                "Отчёты",
                "Ежедневные отчёты",
                "Отчёт по остаткам",
                "Выполнение плана",
                'data-report-section-button="plan"',
                'data-report-section-panel="plan" hidden',
                'id="planReportPeriodSelect"',
                'id="planReportQ1Input"',
                'id="planReportQ4Input"',
                'id="planReportDrrInput"',
                'id="planReportApplyButton"',
                DEFAULT_SHEET_PLAN_REPORT_PATH,
            ):
                if expected not in operator_html:
                    raise AssertionError(f"operator HTML must expose plan-report token {expected!r}")

            missing_query_status, missing_query_payload = _get_json(f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}")
            if missing_query_status != 400 or "period query parameter is required" not in str(missing_query_payload.get("error", "")):
                raise AssertionError(
                    f"plan report route must reject missing required query params, got {missing_query_status} {missing_query_payload}"
                )

            query = urllib_parse.urlencode(
                {
                    "period": "last_30_days",
                    "q1_buyout_plan_rub": "90000",
                    "q2_buyout_plan_rub": "182000",
                    "q3_buyout_plan_rub": "273000",
                    "q4_buyout_plan_rub": "365000",
                    "plan_drr_pct": "10",
                }
            )
            report_status, report_payload = _get_json(f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}?{query}")
            if report_status != 200 or report_payload.get("status") != "available":
                raise AssertionError(f"plan report route must return available JSON, got {report_status} {report_payload}")
            if report_payload.get("reference_date") != REFERENCE_DATE:
                raise AssertionError(f"plan report must default to previous closed day, got {report_payload}")
            if report_payload.get("selected_period_key") != "last_30_days":
                raise AssertionError(f"plan report must keep requested period key, got {report_payload}")
            source_of_truth = report_payload.get("source_of_truth") or {}
            if source_of_truth.get("read_model") != "persisted_temporal_source_slot_snapshots":
                raise AssertionError(f"plan report must disclose its read model, got {report_payload}")
            if source_of_truth.get("snapshot_role") != ACCEPTED_ROLE:
                raise AssertionError(f"plan report must disclose accepted slot role, got {report_payload}")
            selected = (report_payload.get("periods") or {}).get("selected_period") or {}
            if selected.get("date_from") != "2026-03-22" or selected.get("day_count") != 30:
                raise AssertionError(f"plan report selected window must cross Q1/Q2 boundary, got {report_payload}")
            buyout_metric = (selected.get("metrics") or {}).get("buyout_rub") or {}
            if buyout_metric.get("plan") != 50000.0 or buyout_metric.get("fact") != 45000.0:
                raise AssertionError(f"plan report buyout contract must keep server-side plan/fact math, got {report_payload}")
            drr_metric = (selected.get("metrics") or {}).get("drr_pct") or {}
            if drr_metric.get("fact") != 12.0 or drr_metric.get("delta_pp") != 2.0:
                raise AssertionError(f"plan report drr block must stay percentage-based, got {report_payload}")
            ads_metric = (selected.get("metrics") or {}).get("ads_sum_rub") or {}
            if ads_metric.get("plan") != 5000.0 or ads_metric.get("fact") != 5400.0:
                raise AssertionError(f"ads spend plan must derive from buyout plan * planned DRR, got {report_payload}")
            if "month_to_date" not in report_payload.get("periods", {}) or "quarter_to_date" not in report_payload.get("periods", {}) or "year_to_date" not in report_payload.get("periods", {}):
                raise AssertionError(f"plan report must always expose MTD/QTD/YTD blocks, got {report_payload}")

            missing_day = "2026-04-10"
            runtime.delete_temporal_source_slot_snapshots(
                source_key="ads_compact",
                date_from=missing_day,
                date_to=missing_day,
                snapshot_roles=[ACCEPTED_ROLE],
            )
            partial_query = urllib_parse.urlencode(
                {
                    "period": "current_month",
                    "q1_buyout_plan_rub": "90000",
                    "q2_buyout_plan_rub": "182000",
                    "q3_buyout_plan_rub": "273000",
                    "q4_buyout_plan_rub": "365000",
                    "plan_drr_pct": "10",
                    "as_of_date": REFERENCE_DATE,
                }
            )
            partial_status, partial_payload = _get_json(f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}?{partial_query}")
            if partial_status != 200 or partial_payload.get("status") != "partial":
                raise AssertionError(
                    f"plan report route must return partial JSON instead of 500 when coverage is incomplete, got {partial_status} {partial_payload}"
                )
            partial_coverage = partial_payload.get("coverage") or {}
            if (partial_coverage.get("missing_dates_by_source") or {}).get("ads_compact") != [missing_day]:
                raise AssertionError(f"plan report route must expose missing coverage details, got {partial_payload}")

            print("operator_plan_report_html: ok ->", DEFAULT_SHEET_OPERATOR_UI_PATH)
            print("plan_report_missing_query_guard: ok ->", missing_query_status)
            print("plan_report_route: ok ->", report_payload["reference_date"], report_payload["selected_period_key"])
            print("plan_report_source: ok ->", source_of_truth["read_model"], source_of_truth["snapshot_role"])
            print("plan_report_blocks: ok ->", ", ".join(sorted(report_payload["periods"].keys())))
            print("plan_report_partial_route: ok ->", partial_payload["status"], partial_coverage["missing_dates_by_source"])
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _iter_dates(date_from: date, date_to: date):
    current = date_from
    while current <= date_to:
        yield current
        current += timedelta(days=1)


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_request.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_json(url: str) -> tuple[int, dict]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib_request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_request.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    request = urllib_request.Request(url, headers={"Accept": "text/html"})
    try:
        with urllib_request.urlopen(request) as response:
            return response.status, response.read().decode("utf-8")
    except urllib_request.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


if __name__ == "__main__":
    main()
