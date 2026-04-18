"""Targeted smoke-check for rejecting zero-filled seller funnel payloads in DATA_VITRINA."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from urllib import error, parse as urllib_parse, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
AS_OF_DATE = "2026-04-18"
CURRENT_DATE = "2026-04-19"
ACTIVATED_AT = "2026-04-19T00:00:00Z"
REFRESHED_AT = "2026-04-19T00:05:00Z"


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    requested_nm_ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]]
    probe_nm_id = requested_nm_ids[0]

    with TemporaryDirectory(prefix="sheet-vitrina-seller-funnel-zero-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            refreshed_at_factory=lambda: REFRESHED_AT,
        )
        entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            seller_funnel_block=_ZeroFilledSellerFunnelBlock(requested_nm_ids=requested_nm_ids),
            current_web_source_sync=_NoopCurrentWebSourceSync(),
            now_factory=lambda current_date=CURRENT_DATE: datetime.fromisoformat(f"{current_date}T08:00:00+00:00"),
            **_build_synthetic_blocks(),
        )

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
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            upload_url = f"http://127.0.0.1:{config.port}{config.upload_path}"
            refresh_url = f"http://127.0.0.1:{config.port}{config.sheet_refresh_path}"
            plan_url = (
                f"http://127.0.0.1:{config.port}{config.sheet_plan_path}"
                f"?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}"
            )

            upload_status, upload_payload = _post_json(upload_url, bundle)
            if upload_status != 200 or upload_payload["status"] != "accepted":
                raise AssertionError(f"fixture upload must be accepted, got {upload_status} {upload_payload}")

            refresh_status, refresh_payload = _post_json(refresh_url, {"as_of_date": AS_OF_DATE})
            if refresh_status != 200 or refresh_payload["status"] != "success":
                raise AssertionError(f"refresh must succeed, got {refresh_status} {refresh_payload}")

            plan_status, plan_payload = _get_json(plan_url)
            if plan_status != 200:
                raise AssertionError(f"plan endpoint must return 200, got {plan_status}")

            status_sheet = next(sheet for sheet in plan_payload["sheets"] if sheet["sheet_name"] == "STATUS")
            status_rows = {row[0]: row for row in status_sheet["rows"]}
            seller_status = status_rows["seller_funnel_snapshot[today_current]"]
            if seller_status[1] != "error":
                raise AssertionError(f"zero-filled seller_funnel payload must be rejected, got {seller_status}")
            if "invalid_exact_snapshot=zero_filled_seller_funnel_snapshot" not in str(seller_status[10]):
                raise AssertionError(f"seller_funnel status note must explain zero-filled rejection, got {seller_status}")

            data_sheet = next(sheet for sheet in plan_payload["sheets"] if sheet["sheet_name"] == "DATA_VITRINA")
            data_rows = {row[1]: row for row in data_sheet["rows"]}
            probe_row = data_rows[f"SKU:{probe_nm_id}|view_count"]
            if probe_row[2] != 300.0 or probe_row[3] != "":
                raise AssertionError(f"seller_funnel zero payload must blank today value, got {probe_row}")

            print(f"refresh: ok -> {refresh_payload['snapshot_id']}")
            print(f"status: ok -> {seller_status[10]}")
            print("smoke-check passed")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


class _NoopCurrentWebSourceSync:
    def ensure_snapshot(self, snapshot_date: str) -> None:
        return


class _ZeroFilledSellerFunnelBlock:
    def __init__(self, *, requested_nm_ids: list[int]) -> None:
        self.requested_nm_ids = requested_nm_ids

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        zero = request_date == CURRENT_DATE
        base = 300
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                items=[
                    SimpleNamespace(
                        nm_id=nm_id,
                        name=f"NM {nm_id}",
                        vendor_code=f"VC-{nm_id}",
                        view_count=0 if zero else base + index,
                        open_card_count=0 if zero else 30 + index,
                        ctr=0 if zero else 40 + index,
                    )
                    for index, nm_id in enumerate(self.requested_nm_ids)
                ],
                date=request_date,
                count=len(self.requested_nm_ids),
            )
        )


class _SyntheticSuccessBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                items=[],
                snapshot_date=request_date,
                date=request_date,
                date_from=request_date,
                date_to=request_date,
                detail=f"{self.source_key} synthetic success for {request_date}",
                storage_total=None,
            )
        )


def _build_synthetic_blocks() -> dict[str, object]:
    return {
        "web_source_block": _SyntheticSuccessBlock("web_source_snapshot"),
        "sales_funnel_history_block": _SyntheticSuccessBlock("sales_funnel_history"),
        "prices_snapshot_block": _SyntheticSuccessBlock("prices_snapshot"),
        "sf_period_block": _SyntheticSuccessBlock("sf_period"),
        "spp_block": _SyntheticSuccessBlock("spp"),
        "ads_bids_block": _SyntheticSuccessBlock("ads_bids"),
        "stocks_block": _SyntheticSuccessBlock("stocks"),
        "ads_compact_block": _SyntheticSuccessBlock("ads_compact"),
        "fin_report_daily_block": _SyntheticSuccessBlock("fin_report_daily"),
    }


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    raise AssertionError("synthetic request must carry a date field")


def _post_json(url: str, payload: object) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
