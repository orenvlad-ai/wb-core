"""Focused smoke for archived legacy Google Sheets contour guards."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from urllib import error, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_LOAD_PATH,
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
ACTIVATED_AT = "2026-04-24T12:00:00Z"
REFRESHED_AT = "2026-04-24T12:05:00Z"
AS_OF_DATE = "2026-04-23"
SERVER_NOW = datetime(2026, 4, 24, 8, 0, tzinfo=timezone.utc)
ARCHIVE_MESSAGE = "Legacy Google Sheets contour is archived"


class CountingBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key

    def execute(self, request: object) -> SimpleNamespace:
        request_date = str(getattr(request, "date", "") or getattr(request, "snapshot_date", "") or AS_OF_DATE)
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


class NoopWebSourceSync:
    def ensure_current_snapshot(self, *, source_key: str, snapshot_date: str) -> None:
        return

    def ensure_closed_day_snapshot(self, *, source_key: str, snapshot_date: str) -> None:
        return


def main() -> None:
    gas_guard = json.loads(
        subprocess.check_output(
            [
                "node",
                str(ROOT / "apps" / "sheet_vitrina_v1_registry_upload_trigger_harness.js"),
                "--mode",
                "archived_guard",
                "--scriptPath",
                str(ROOT / "gas" / "sheet_vitrina_v1" / "RegistryUploadTrigger.gs"),
            ],
            cwd=ROOT,
            text=True,
        )
    )
    if gas_guard["status"]["active"] is not False:
        raise AssertionError(f"GAS archive status must be inactive: {gas_guard}")
    if gas_guard["sheet_count"] != 0:
        raise AssertionError("GAS archived guard must not create/write sheets")
    for probe in gas_guard["blocked"]:
        if not probe["blocked"] or ARCHIVE_MESSAGE not in probe["message"]:
            raise AssertionError(f"GAS function must fail fast as archived: {probe}")

    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-legacy-gs-archive-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            refreshed_at_factory=lambda: REFRESHED_AT,
            now_factory=lambda: SERVER_NOW,
        )
        entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            now_factory=lambda: SERVER_NOW,
            current_web_source_sync=NoopWebSourceSync(),
            closed_day_web_source_sync=NoopWebSourceSync(),
            **_build_counting_blocks(),
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
            base_url = f"http://127.0.0.1:{config.port}"
            upload_status, upload_payload = _post_json(f"{base_url}{config.upload_path}", bundle)
            if upload_status != 200 or upload_payload.get("status") != "accepted":
                raise AssertionError(f"fixture upload must be accepted: {upload_status} {upload_payload}")

            operator_status, operator_html = _get_text(f"{base_url}{config.sheet_operator_ui_path}?embedded_tab=vitrina")
            if operator_status != 200:
                raise AssertionError(f"operator UI must return 200, got {operator_status}")
            if "Legacy Google Sheets contour архивирован" not in operator_html:
                raise AssertionError("operator UI must mark legacy Google Sheets as archived")
            if ">Отправить данные</button>" in operator_html:
                raise AssertionError("operator UI must not expose legacy sheet send as an active action")

            auto_status, auto_payload = _post_json(
                f"{base_url}{config.sheet_refresh_path}",
                {"as_of_date": AS_OF_DATE, "auto_load": True},
            )
            if auto_status != 400 or "auto_load targets the archived legacy Google Sheets contour" not in str(auto_payload):
                raise AssertionError(f"auto_load must be denied as archived: {auto_status} {auto_payload}")

            refresh_status, refresh_payload = _post_json(
                f"{base_url}{config.sheet_refresh_path}",
                {"as_of_date": AS_OF_DATE},
            )
            if refresh_status != 200 or refresh_payload.get("status") not in {"success", "warning", "error"}:
                raise AssertionError(f"server-side refresh must remain available: {refresh_status} {refresh_payload}")

            load_status, load_payload = _post_json(
                f"{base_url}{DEFAULT_SHEET_LOAD_PATH}",
                {"as_of_date": AS_OF_DATE},
            )
            if load_status != 410 or load_payload.get("status") != "archived":
                raise AssertionError(f"legacy load endpoint must return 410 archived: {load_status} {load_payload}")
            if ARCHIVE_MESSAGE not in str(load_payload.get("error", "")):
                raise AssertionError(f"legacy load endpoint must explain archive state: {load_payload}")

            status_code, status_payload = _get_json(f"{base_url}{config.sheet_status_path}?as_of_date={AS_OF_DATE}")
            if status_code != 200:
                raise AssertionError(f"status endpoint must remain available, got {status_code}")
            archive_context = (status_payload.get("load_context") or {}).get("legacy_google_sheets_contour") or {}
            if archive_context.get("active") is not False or archive_context.get("load_enabled") is not False:
                raise AssertionError(f"status must expose archived legacy load context: {archive_context}")

            print("gas_archive_guard: ok")
            print("auto_load_denied: ok")
            print("legacy_load_endpoint: ok -> 410 archived")
            print("web_operator_refresh: ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _post_json(url: str, payload: object) -> tuple[int, dict[str, object]]:
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


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    try:
        with urllib_request.urlopen(url) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    try:
        with urllib_request.urlopen(url) as response:
            return response.status, response.read().decode("utf-8")
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _build_counting_blocks() -> dict[str, CountingBlock]:
    return {
        "seller_funnel_block": CountingBlock("seller_funnel_snapshot"),
        "web_source_block": CountingBlock("web_source_snapshot"),
        "sales_funnel_history_block": CountingBlock("sales_funnel_history"),
        "prices_snapshot_block": CountingBlock("prices_snapshot"),
        "sf_period_block": CountingBlock("sf_period"),
        "spp_block": CountingBlock("spp"),
        "stocks_block": CountingBlock("stocks"),
        "ads_bids_block": CountingBlock("ads_bids"),
        "ads_compact_block": CountingBlock("ads_compact"),
        "fin_report_daily_block": CountingBlock("fin_report_daily"),
        "promo_live_source_block": CountingBlock("promo_by_price"),
    }


if __name__ == "__main__":
    main()
