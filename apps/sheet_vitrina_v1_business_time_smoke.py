"""Targeted smoke-check for EKT business date semantics in sheet_vitrina_v1."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from urllib import error, request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
BOUNDARY_NOW = datetime(2026, 4, 16, 21, 30, tzinfo=timezone.utc)
EXPECTED_AS_OF_DATE = "2026-04-16"
EXPECTED_CURRENT_DATE = "2026-04-17"
ACTIVATED_AT = "2026-04-16T21:30:00Z"
REFRESHED_AT = "2026-04-16T21:35:00Z"


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)

    with TemporaryDirectory(prefix="sheet-vitrina-business-time-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        accepted = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted.status}")

        plan_block = _build_live_plan(runtime)
        plan = plan_block.build_plan()
        if plan.as_of_date != EXPECTED_AS_OF_DATE:
            raise AssertionError(f"default as_of_date must use EKT yesterday, got {plan.as_of_date}")
        if plan.date_columns != [EXPECTED_AS_OF_DATE, EXPECTED_CURRENT_DATE]:
            raise AssertionError(f"date_columns must use EKT current day, got {plan.date_columns}")
        status_rows = _status_row_map(plan)
        current_state_row = status_rows["registry_upload_current_state"]
        if current_state_row["freshness"] != EXPECTED_CURRENT_DATE:
            raise AssertionError(
                "registry_upload_current_state freshness must be rendered in EKT business date"
            )
        print(f"plan_default_date: ok -> {plan.as_of_date} / {plan.date_columns[-1]}")

        port = _reserve_free_port()
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            refreshed_at_factory=lambda: REFRESHED_AT,
        )
        entrypoint.sheet_plan_block = plan_block
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            refresh_url = f"http://127.0.0.1:{port}{config.sheet_refresh_path}"
            status_url = f"http://127.0.0.1:{port}{config.sheet_status_path}"
            plan_url = f"http://127.0.0.1:{port}{config.sheet_plan_path}"

            refresh_status, refresh_payload = _post_json(refresh_url, {})
            if refresh_status != 200:
                raise AssertionError(f"default refresh must return 200, got {refresh_status}")
            if refresh_payload["as_of_date"] != EXPECTED_AS_OF_DATE:
                raise AssertionError(
                    f"default refresh must materialize EKT yesterday, got {refresh_payload['as_of_date']}"
                )
            if refresh_payload["date_columns"] != [EXPECTED_AS_OF_DATE, EXPECTED_CURRENT_DATE]:
                raise AssertionError(
                    f"default refresh must materialize EKT date columns, got {refresh_payload['date_columns']}"
                )

            plan_status, plan_payload = _get_json(plan_url)
            if plan_status != 200:
                raise AssertionError(f"plan endpoint must return 200 after refresh, got {plan_status}")
            if plan_payload["date_columns"] != [EXPECTED_AS_OF_DATE, EXPECTED_CURRENT_DATE]:
                raise AssertionError("persisted plan must keep EKT date columns")

            status_status, status_payload = _get_json(status_url)
            if status_status != 200:
                raise AssertionError(f"status endpoint must return 200 after refresh, got {status_status}")
            if status_payload["as_of_date"] != EXPECTED_AS_OF_DATE:
                raise AssertionError("status endpoint must expose EKT default as_of_date")

            print(f"refresh_default_date: ok -> {refresh_payload['snapshot_id']}")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    print("smoke-check passed")


def _build_live_plan(runtime: RegistryUploadDbBackedRuntime) -> SheetVitrinaV1LivePlanBlock:
    return SheetVitrinaV1LivePlanBlock(
        runtime=runtime,
        web_source_block=_SyntheticSuccessBlock("web_source_snapshot"),
        seller_funnel_block=_SyntheticSuccessBlock("seller_funnel_snapshot"),
        sales_funnel_history_block=_SyntheticSuccessBlock("sales_funnel_history"),
        prices_snapshot_block=_SyntheticSuccessBlock("prices_snapshot"),
        sf_period_block=_SyntheticSuccessBlock("sf_period"),
        spp_block=_SyntheticSuccessBlock("spp"),
        ads_bids_block=_SyntheticSuccessBlock("ads_bids"),
        stocks_block=_SyntheticSuccessBlock("stocks"),
        ads_compact_block=_SyntheticSuccessBlock("ads_compact"),
        fin_report_daily_block=_SyntheticSuccessBlock("fin_report_daily"),
        now_factory=lambda: BOUNDARY_NOW,
    )


def _status_row_map(plan: object) -> dict[str, dict[str, object]]:
    status_sheet = next(sheet for sheet in plan.sheets if sheet.sheet_name == "STATUS")
    return {
        str(row[0]): dict(zip(status_sheet.header, row))
        for row in status_sheet.rows
        if row
    }


class _SyntheticSuccessBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        payload = SimpleNamespace(
            kind="success",
            items=[],
            snapshot_date=request_date,
            date=request_date,
            date_from=request_date,
            date_to=request_date,
            detail=f"{self.source_key} synthetic success for {request_date}",
            storage_total=None,
        )
        return SimpleNamespace(result=payload)


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    raise AssertionError("synthetic source request must carry a date field")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _post_json(url: str, payload: object) -> tuple[int, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib_request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_json(url: str) -> tuple[int, object]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
