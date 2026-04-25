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
    DEFAULT_SHEET_PLAN_REPORT_BASELINE_STATUS_PATH,
    DEFAULT_SHEET_PLAN_REPORT_BASELINE_TEMPLATE_PATH,
    DEFAULT_SHEET_PLAN_REPORT_BASELINE_UPLOAD_PATH,
    DEFAULT_SHEET_PLAN_REPORT_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_STOCK_REPORT_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_plan_report import BASELINE_TEMPLATE_HEADERS
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
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
            _seed_daily_snapshots(
                runtime,
                primary_nm_id=primary_nm_id,
                date_from=date(2026, 3, 1),
                date_to=date.fromisoformat(REFERENCE_DATE),
            )

            operator_status, operator_html = _get_text(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}?embedded_tab=reports")
            if operator_status != 200:
                raise AssertionError(f"operator page must return 200, got {operator_status}")
            for expected in (
                "Отчёты",
                "Ежедневные отчёты",
                "Отчёт по остаткам",
                "Выполнение плана",
                "За первый квартал",
                "За второй квартал",
                "За третий квартал",
                "За четвертый квартал",
                "За первое полугодие",
                "За второе полугодие",
                'data-report-section-button="plan"',
                'data-report-section-panel="plan" hidden',
                'id="planReportPeriodSelect"',
                'id="planReportH1Input"',
                'id="planReportH2Input"',
                'id="planReportDrrInput"',
                'id="planReportContractStartCheckbox"',
                'id="planReportContractStartDateInput"',
                "С учётом даты подписания",
                "Дата подписания",
                'id="planReportApplyButton"',
                'id="planReportBaselineTemplateButton"',
                'id="planReportBaselineFileInput"',
                'id="planReportBaselineStatus"',
                DEFAULT_SHEET_PLAN_REPORT_PATH,
                DEFAULT_SHEET_PLAN_REPORT_BASELINE_TEMPLATE_PATH,
                DEFAULT_SHEET_PLAN_REPORT_BASELINE_UPLOAD_PATH,
                DEFAULT_SHEET_PLAN_REPORT_BASELINE_STATUS_PATH,
            ):
                if expected not in operator_html:
                    raise AssertionError(f"operator HTML must expose plan-report token {expected!r}")

            missing_query_status, missing_query_payload = _get_json(f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}")
            if missing_query_status != 400 or "period query parameter is required" not in str(missing_query_payload.get("error", "")):
                raise AssertionError(
                    f"plan report route must reject missing required query params, got {missing_query_status} {missing_query_payload}"
                )

            baseline_status_code, baseline_status_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_BASELINE_STATUS_PATH}"
            )
            if baseline_status_code != 200 or baseline_status_payload.get("status") != "missing":
                raise AssertionError(
                    f"baseline status route must return missing before upload, got {baseline_status_code} {baseline_status_payload}"
                )
            template_status, template_bytes = _get_bytes(f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_BASELINE_TEMPLATE_PATH}")
            if template_status != 200:
                raise AssertionError(f"baseline template route must return 200, got {template_status}")
            template_rows = read_first_sheet_rows(template_bytes)
            if template_rows[0] != BASELINE_TEMPLATE_HEADERS:
                raise AssertionError(f"baseline template route must expose expected headers, got {template_rows}")

            query = urllib_parse.urlencode(
                {
                    "period": "last_30_days",
                    "h1_buyout_plan_rub": "271500",
                    "h2_buyout_plan_rub": "552000",
                    "plan_drr_pct": "10",
                }
            )
            report_status, report_payload = _get_json(f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}?{query}")
            if report_status != 200 or report_payload.get("status") != "partial":
                raise AssertionError(f"plan report route must return partial top-level JSON, got {report_status} {report_payload}")
            if report_payload.get("reference_date") != REFERENCE_DATE:
                raise AssertionError(f"plan report must default to previous closed day, got {report_payload}")
            if report_payload.get("effective_as_of_date") != REFERENCE_DATE:
                raise AssertionError(f"plan report must disclose previous closed effective date, got {report_payload}")
            if report_payload.get("selected_period_key") != "last_30_days":
                raise AssertionError(f"plan report must keep requested period key, got {report_payload}")
            source_of_truth = report_payload.get("source_of_truth") or {}
            if source_of_truth.get("read_model") != "persisted_temporal_source_slot_snapshots_plus_plan_report_monthly_baseline":
                raise AssertionError(f"plan report must disclose its read model, got {report_payload}")
            if source_of_truth.get("snapshot_role") != ACCEPTED_ROLE:
                raise AssertionError(f"plan report must disclose accepted slot role, got {report_payload}")
            selected = (report_payload.get("periods") or {}).get("selected_period") or {}
            if selected.get("status") != "available":
                raise AssertionError(f"selected period must stay available while YTD is partial, got {report_payload}")
            if selected.get("date_from") != "2026-03-22" or selected.get("day_count") != 30:
                raise AssertionError(f"plan report selected window must span expected dates, got {report_payload}")
            buyout_metric = (selected.get("metrics") or {}).get("buyout_rub") or {}
            if buyout_metric.get("plan") != 45000.0 or buyout_metric.get("fact") != 45000.0:
                raise AssertionError(f"plan report buyout contract must keep server-side plan/fact math, got {report_payload}")
            drr_metric = (selected.get("metrics") or {}).get("drr_pct") or {}
            if drr_metric.get("fact") != 12.0 or drr_metric.get("delta_pp") != 2.0:
                raise AssertionError(f"plan report drr block must stay percentage-based, got {report_payload}")
            ads_metric = (selected.get("metrics") or {}).get("ads_sum_rub") or {}
            if ads_metric.get("plan") != 4500.0 or ads_metric.get("fact") != 5400.0:
                raise AssertionError(f"ads spend plan must derive from buyout plan * planned DRR, got {report_payload}")
            if not selected.get("source_breakdown"):
                raise AssertionError(f"plan report must expose per-block source breakdown, got {report_payload}")
            if "month_to_date" not in report_payload.get("periods", {}) or "quarter_to_date" not in report_payload.get("periods", {}) or "year_to_date" not in report_payload.get("periods", {}):
                raise AssertionError(f"plan report must always expose MTD/QTD/YTD blocks, got {report_payload}")
            ytd = (report_payload.get("periods") or {}).get("year_to_date") or {}
            if ytd.get("status") != "partial":
                raise AssertionError(f"YTD must stay local partial before baseline, got {report_payload}")
            fixed_periods = {
                "yesterday": ("За вчера", "2026-04-20", "2026-04-20", 1, "closed_day_window"),
                "last_7_days": ("За последние 7 дней", "2026-04-14", "2026-04-20", 7, "closed_day_window"),
                "last_30_days": ("За последние 30 дней", "2026-03-22", "2026-04-20", 30, "closed_day_window"),
                "current_month": ("За текущий месяц", "2026-04-01", "2026-04-20", 20, "closed_day_window"),
                "current_quarter": ("За текущий квартал", "2026-04-01", "2026-04-20", 20, "closed_day_window"),
                "current_year": ("За текущий год", "2026-01-01", "2026-04-20", 110, "closed_day_window"),
                "first_quarter": ("За первый квартал", "2026-01-01", "2026-03-31", 90, "completed"),
                "second_quarter": ("За второй квартал", "2026-04-01", "2026-04-20", 20, "in_progress"),
                "third_quarter": ("За третий квартал", "2026-07-01", "2026-09-30", 0, "not_started"),
                "fourth_quarter": ("За четвертый квартал", "2026-10-01", "2026-12-31", 0, "not_started"),
                "first_half": ("За первое полугодие", "2026-01-01", "2026-04-20", 110, "in_progress"),
                "second_half": ("За второе полугодие", "2026-07-01", "2026-12-31", 0, "not_started"),
            }
            for period_key, (label, date_from, date_to, day_count, period_state) in fixed_periods.items():
                period_query = urllib_parse.urlencode(
                    {
                        "period": period_key,
                        "h1_buyout_plan_rub": "271500",
                        "h2_buyout_plan_rub": "552000",
                        "plan_drr_pct": "10",
                    }
                )
                period_status, period_payload = _get_json(f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}?{period_query}")
                period_selected = (period_payload.get("periods") or {}).get("selected_period") or {}
                if period_status != 200:
                    raise AssertionError(f"period {period_key} must return 200, got {period_status} {period_payload}")
                if (
                    period_payload.get("selected_period_key") != period_key
                    or period_selected.get("label") != label
                    or period_selected.get("date_from") != date_from
                    or period_selected.get("date_to") != date_to
                    or period_selected.get("day_count") != day_count
                    or period_selected.get("period_state") != period_state
                    or period_selected.get("effective_as_of_date") != REFERENCE_DATE
                ):
                    raise AssertionError(f"period {period_key} selected block is wrong, got {period_payload}")
                if period_state == "not_started" and period_selected.get("metrics", {}).get("buyout_rub", {}).get("plan") is not None:
                    raise AssertionError(f"future period {period_key} must not fabricate a plan, got {period_payload}")
            contract_query = urllib_parse.urlencode(
                {
                    "period": "first_quarter",
                    "h1_buyout_plan_rub": "155379879",
                    "h2_buyout_plan_rub": "294620120",
                    "plan_drr_pct": "6",
                    "as_of_date": "2026-04-24",
                    "use_contract_start_date": "true",
                    "contract_start_date": "2026-02-01",
                }
            )
            contract_status, contract_payload = _get_json(f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}?{contract_query}")
            contract_selected = (contract_payload.get("periods") or {}).get("selected_period") or {}
            if (
                contract_status != 200
                or contract_selected.get("date_from") != "2026-02-01"
                or contract_selected.get("date_to") != "2026-03-31"
                or contract_selected.get("day_count") != 59
                or not contract_selected.get("contract_start_applied")
            ):
                raise AssertionError(f"contract start route must trim first_quarter to Feb+Mar, got {contract_status} {contract_payload}")
            expected_contract_plan = 155379879.0 / 181.0 * 59.0
            actual_contract_plan = (contract_selected.get("metrics") or {}).get("buyout_rub", {}).get("plan")
            if actual_contract_plan is None or abs(actual_contract_plan - expected_contract_plan) > 1e-3:
                raise AssertionError(
                    f"contract start route must keep H1 denominator for plan, got {actual_contract_plan}"
                )
            invalid_contract_query = urllib_parse.urlencode(
                {
                    "period": "first_quarter",
                    "h1_buyout_plan_rub": "155379879",
                    "h2_buyout_plan_rub": "294620120",
                    "plan_drr_pct": "6",
                    "use_contract_start_date": "true",
                    "contract_start_date": "not-a-date",
                }
            )
            invalid_contract_status, invalid_contract_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}?{invalid_contract_query}"
            )
            if invalid_contract_status != 400 or "contract_start_date" not in str(invalid_contract_payload.get("error", "")):
                raise AssertionError(
                    f"invalid contract_start_date must be a controlled 400, got {invalid_contract_status} {invalid_contract_payload}"
                )
            legacy_query = urllib_parse.urlencode(
                {
                    "period": "last_30_days",
                    "q1_buyout_plan_rub": "90000",
                    "q2_buyout_plan_rub": "181500",
                    "q3_buyout_plan_rub": "276000",
                    "q4_buyout_plan_rub": "276000",
                    "plan_drr_pct": "10",
                }
            )
            legacy_status, legacy_payload = _get_json(f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}?{legacy_query}")
            legacy_inputs = legacy_payload.get("inputs") or {}
            if legacy_status != 200 or legacy_inputs.get("input_model") != "legacy_quarter_params_summed_to_half_year":
                raise AssertionError(f"legacy Q1-Q4 params must stay transitional only, got {legacy_status} {legacy_payload}")

            baseline_workbook = build_single_sheet_workbook_bytes(
                "План факт месяцы",
                [
                    BASELINE_TEMPLATE_HEADERS,
                    ["2026-01", 31000.0, 3100.0],
                    ["2026-02", 28000.0, 2800.0],
                ],
            )
            baseline_upload_status, baseline_upload_payload = _post_multipart_file(
                f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_BASELINE_UPLOAD_PATH}",
                baseline_workbook,
                filename="baseline.xlsx",
            )
            if baseline_upload_status != 200 or baseline_upload_payload.get("accepted_months") != ["2026-01", "2026-02"]:
                raise AssertionError(
                    f"baseline upload route must accept valid Jan/Feb XLSX, got {baseline_upload_status} {baseline_upload_payload}"
                )
            uploaded_status_code, uploaded_status_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_BASELINE_STATUS_PATH}"
            )
            if uploaded_status_code != 200 or uploaded_status_payload.get("status") != "uploaded":
                raise AssertionError(
                    f"baseline status route must return uploaded after upload, got {uploaded_status_code} {uploaded_status_payload}"
                )
            ytd_status, ytd_payload = _get_json(f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}?{query}")
            ytd_block = (ytd_payload.get("periods") or {}).get("year_to_date") or {}
            if ytd_status != 200 or ytd_block.get("status") != "available":
                raise AssertionError(f"YTD must become available after Jan/Feb baseline, got {ytd_status} {ytd_payload}")

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
                    "h1_buyout_plan_rub": "271500",
                    "h2_buyout_plan_rub": "552000",
                    "plan_drr_pct": "10",
                    "as_of_date": REFERENCE_DATE,
                }
            )
            partial_status, partial_payload = _get_json(f"{base_url}{DEFAULT_SHEET_PLAN_REPORT_PATH}?{partial_query}")
            if partial_status != 200 or partial_payload.get("status") != "partial":
                raise AssertionError(
                    f"plan report route must return partial JSON instead of 500 when coverage is incomplete, got {partial_status} {partial_payload}"
                )
            partial_selected = (partial_payload.get("periods") or {}).get("selected_period") or {}
            partial_coverage = partial_selected.get("coverage") or {}
            if (partial_coverage.get("missing_dates_by_source") or {}).get("ads_compact") != [missing_day]:
                raise AssertionError(f"plan report route must expose missing coverage details, got {partial_payload}")

            print("operator_plan_report_html: ok ->", DEFAULT_SHEET_OPERATOR_UI_PATH)
            print("plan_report_missing_query_guard: ok ->", missing_query_status)
            print("plan_report_route: ok ->", report_payload["reference_date"], report_payload["selected_period_key"])
            print("plan_report_source: ok ->", source_of_truth["read_model"], source_of_truth["snapshot_role"])
            print("plan_report_blocks: ok ->", ", ".join(sorted(report_payload["periods"].keys())))
            print("plan_report_contract_start: ok ->", contract_selected.get("date_from"), contract_selected.get("date_to"))
            print("plan_report_baseline_routes: ok ->", template_status, baseline_upload_payload["accepted_months"])
            print("plan_report_ytd_after_baseline: ok ->", ytd_block["status"])
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


def _seed_daily_snapshots(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    primary_nm_id: int,
    date_from: date,
    date_to: date,
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


def _post_multipart_file(url: str, workbook_bytes: bytes, *, filename: str) -> tuple[int, dict]:
    boundary = "----wbcore-plan-report-baseline-smoke"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n"
                "\r\n"
            ).encode("utf-8"),
            workbook_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    request = urllib_request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
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


def _get_bytes(url: str) -> tuple[int, bytes]:
    request = urllib_request.Request(url)
    try:
        with urllib_request.urlopen(request) as response:
            return response.status, response.read()
    except urllib_request.HTTPError as exc:
        return exc.code, exc.read()


def _get_text(url: str) -> tuple[int, str]:
    request = urllib_request.Request(url, headers={"Accept": "text/html"})
    try:
        with urllib_request.urlopen(request) as response:
            return response.status, response.read().decode("utf-8")
    except urllib_request.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


if __name__ == "__main__":
    main()
