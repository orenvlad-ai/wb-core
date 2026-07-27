"""Integration smoke-check for audit-only COST_PRICE after public retirement."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from typing import Any
from urllib import error, parse as urllib_parse, request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_COST_PRICE_UPLOAD_PATH,
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
ACTIVATED_AT = "2026-04-16T11:00:00Z"
REFRESHED_AT = "2026-04-16T11:05:00Z"
AS_OF_DATE = "2026-04-15"
TODAY_CURRENT_DATE = "2026-04-16"
ORDER_SUM = 1000.0
ORDER_COUNT = 10.0
ADS_SUM = 50.0


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    if not any(item["enabled"] for item in bundle["config_v2"]):
        raise AssertionError("fixture bundle must keep enabled config groups")
    _check_empty_cost_price_state(bundle)
    _check_cost_price_audit_state_and_public_retirement(bundle)
    print("smoke-check passed")


def _check_empty_cost_price_state(bundle: dict[str, Any]) -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-cost-price-empty-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        refresh_payload, plan_payload, _status_payload = _run_scenario(
            bundle=bundle,
            runtime_dir=runtime_dir,
            cost_price_payload=None,
        )
        if refresh_payload["status"] != "success":
            raise AssertionError("refresh must keep ready snapshot materialized when COST_PRICE state is missing")

        yesterday_status = _find_status_row(plan_payload, "cost_price[yesterday_closed]")
        today_status = _find_status_row(plan_payload, "cost_price[today_current]")
        if yesterday_status["kind"] != "missing" or today_status["kind"] != "missing":
            raise AssertionError("missing COST_PRICE current state must surface as missing in both slots")
        if "not materialized" not in str(today_status["note"]):
            raise AssertionError("missing COST_PRICE current state must explain the gap in STATUS")

        _assert_no_legacy_public_rows(plan_payload)
        print("cost-price-empty: ok -> source status remains truthful without public legacy rows")


def _check_cost_price_audit_state_and_public_retirement(bundle: dict[str, Any]) -> None:
    cost_price_payload = {
        "dataset_version": "sheet_vitrina_v1_cost_price_overlay__2026-04-16T11:00:00Z",
        "uploaded_at": "2026-04-16T11:00:00Z",
        "cost_price_rows": [
            {"group": "Clean", "cost_price_rub": 20.0, "effective_from": "2026-04-14"},
            {"group": "Clean", "cost_price_rub": 40.0, "effective_from": "2026-04-16"},
            {"group": "Anti-Spy", "cost_price_rub": 30.0, "effective_from": "2026-04-01"},
            {"group": "Matte", "cost_price_rub": 50.0, "effective_from": "2026-04-15"},
            {"group": "Matte", "cost_price_rub": 60.0, "effective_from": "2026-04-16"},
        ],
    }
    with TemporaryDirectory(prefix="sheet-vitrina-cost-price-overlay-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        refresh_payload, plan_payload, _status_payload = _run_scenario(
            bundle=bundle,
            runtime_dir=runtime_dir,
            cost_price_payload=cost_price_payload,
        )
        if refresh_payload["status"] != "success":
            raise AssertionError("refresh must succeed with persisted audit-only COST_PRICE")

        yesterday_status = _find_status_row(plan_payload, "cost_price[yesterday_closed]")
        today_status = _find_status_row(plan_payload, "cost_price[today_current]")
        for row in (yesterday_status, today_status):
            if row["kind"] != "success":
                raise AssertionError(f"COST_PRICE coverage must be success for full group fixture, got {row}")
            if row["requested_count"] != 3 or row["covered_count"] != 3:
                raise AssertionError(f"COST_PRICE status counts mismatch: {row}")
            if cost_price_payload["dataset_version"] not in str(row["note"]):
                raise AssertionError("COST_PRICE status note must expose the dataset_version in use")
            if "resolution_rule=latest_effective_from<=slot_date" not in str(row["note"]):
                raise AssertionError("COST_PRICE status note must expose effective_from resolution rule")

        state = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir).load_cost_price_current_state()
        if state.dataset_version != cost_price_payload["dataset_version"]:
            raise AssertionError(f"audit dataset version was not preserved: {state}")
        persisted_rows = {
            (row.group, row.effective_from, row.cost_price_rub)
            for row in state.cost_price_rows
        }
        expected_rows = {
            (
                str(row["group"]),
                str(row["effective_from"]),
                float(row["cost_price_rub"]),
            )
            for row in cost_price_payload["cost_price_rows"]
        }
        if persisted_rows != expected_rows:
            raise AssertionError(
                f"historical COST_PRICE audit rows changed: {persisted_rows}"
            )
        _assert_no_legacy_public_rows(plan_payload)
        print("cost-price-audit: ok -> raw group history persists while public Proxy1 rows stay retired")


def _assert_no_legacy_public_rows(plan_payload: dict[str, Any]) -> None:
    retired = {
        "cost_price_rub",
        "avg_cost_price_rub",
        "profit_proxy_rub",
        "proxy_profit_rub",
        "total_proxy_profit_rub",
        "proxy_margin_pct",
        "proxy_margin_pct_total",
    }
    leaked = {
        metric_key
        for metric_key in retired
        if _find_data_rows(plan_payload, metric_key)
    }
    if leaked:
        raise AssertionError(f"legacy COST_PRICE/Proxy1 rows leaked into active plan: {sorted(leaked)}")


def _run_scenario(
    *,
    bundle: dict[str, Any],
    runtime_dir: Path,
    cost_price_payload: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    entrypoint = _build_entrypoint(runtime_dir=runtime_dir)
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
        cost_price_upload_path=DEFAULT_COST_PRICE_UPLOAD_PATH,
    )
    server = build_registry_upload_http_server(config, entrypoint=entrypoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        upload_url = f"http://127.0.0.1:{config.port}{config.upload_path}"
        cost_price_url = f"http://127.0.0.1:{config.port}{config.cost_price_upload_path}"
        refresh_url = f"http://127.0.0.1:{config.port}{config.sheet_refresh_path}"
        plan_url = (
            f"http://127.0.0.1:{config.port}{config.sheet_plan_path}"
            f"?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}"
        )
        status_url = (
            f"http://127.0.0.1:{config.port}{config.sheet_status_path}"
            f"?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}"
        )

        upload_status, upload_payload = _post_json(upload_url, bundle)
        if upload_status != 200 or upload_payload["status"] != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {upload_status} {upload_payload}")

        if cost_price_payload is not None:
            cost_status, cost_payload = _post_json(cost_price_url, cost_price_payload)
            if cost_status != 200 or cost_payload["status"] != "accepted":
                raise AssertionError(f"COST_PRICE fixture must be accepted, got {cost_status} {cost_payload}")

        refresh_status, refresh_payload = _post_json(refresh_url, {"as_of_date": AS_OF_DATE})
        if refresh_status != 200:
            raise AssertionError(f"refresh endpoint must return 200, got {refresh_status}")

        plan_status, plan_payload = _get_json(plan_url)
        if plan_status != 200:
            raise AssertionError(f"plan endpoint must return 200 after refresh, got {plan_status}")
        status_code, status_payload = _get_json(status_url)
        if status_code != 200:
            raise AssertionError(f"status endpoint must return 200 after refresh, got {status_code}")

        return refresh_payload, plan_payload, status_payload
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _build_entrypoint(*, runtime_dir: Path) -> RegistryUploadHttpEntrypoint:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=runtime_dir,
        runtime=runtime,
        activated_at_factory=lambda: ACTIVATED_AT,
        refreshed_at_factory=lambda: REFRESHED_AT,
    )
    entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
        runtime=runtime,
        web_source_block=_EmptySuccessBlock("web_source_snapshot"),
        seller_funnel_block=_EmptySuccessBlock("seller_funnel_snapshot"),
        sales_funnel_history_block=_HistorySuccessBlock(),
        prices_snapshot_block=_EmptySuccessBlock("prices_snapshot"),
        sf_period_block=_EmptySuccessBlock("sf_period"),
        spp_block=_EmptySuccessBlock("spp"),
        ads_bids_block=_EmptySuccessBlock("ads_bids"),
        stocks_block=_EmptySuccessBlock("stocks"),
        ads_compact_block=_AdsCompactSuccessBlock(),
        fin_report_daily_block=_EmptySuccessBlock("fin_report_daily"),
        now_factory=lambda: datetime(2026, 4, 16, 9, 0, tzinfo=timezone.utc),
    )
    return entrypoint


class _EmptySuccessBlock:
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


class _HistorySuccessBlock:
    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        items = []
        for nm_id in list(getattr(request, "nm_ids", []) or []):
            items.extend(
                [
                    SimpleNamespace(nm_id=int(nm_id), metric="orderCount", date=request_date, value=ORDER_COUNT),
                    SimpleNamespace(nm_id=int(nm_id), metric="orderSum", date=request_date, value=ORDER_SUM),
                ]
            )
        payload = SimpleNamespace(
            kind="success",
            items=items,
            snapshot_date=request_date,
            date=request_date,
            date_from=request_date,
            date_to=request_date,
            detail=f"sales_funnel_history synthetic success for {request_date}",
        )
        return SimpleNamespace(result=payload)


class _AdsCompactSuccessBlock:
    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        items = [
            SimpleNamespace(nm_id=int(nm_id), ads_sum=ADS_SUM)
            for nm_id in list(getattr(request, "nm_ids", []) or [])
        ]
        payload = SimpleNamespace(
            kind="success",
            items=items,
            snapshot_date=request_date,
            date=request_date,
            date_from=request_date,
            date_to=request_date,
            detail=f"ads_compact synthetic success for {request_date}",
        )
        return SimpleNamespace(result=payload)


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    raise AssertionError("synthetic source request must carry a date field")


def _find_status_row(plan_payload: dict[str, Any], source_key: str) -> dict[str, Any]:
    status_sheet = _find_sheet(plan_payload, "STATUS")
    for row in status_sheet["rows"]:
        if row and row[0] == source_key:
            return dict(zip(status_sheet["header"], row))
    raise AssertionError(f"STATUS row for {source_key!r} is missing")


def _find_data_rows(plan_payload: dict[str, Any], metric_key: str) -> list[list[Any]]:
    data_sheet = _find_sheet(plan_payload, "DATA_VITRINA")
    return [row for row in data_sheet["rows"] if len(row) >= 3 and str(row[1]).endswith(f"|{metric_key}")]


def _find_sheet(plan_payload: dict[str, Any], sheet_name: str) -> dict[str, Any]:
    for sheet in plan_payload["sheets"]:
        if sheet["sheet_name"] == sheet_name:
            return sheet
    raise AssertionError(f"sheet {sheet_name!r} is missing")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _post_json(url: str, payload: object) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_json(url: str) -> tuple[int, dict[str, Any]]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
