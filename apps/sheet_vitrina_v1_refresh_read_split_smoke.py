"""Integration smoke-check для split refresh/read в sheet_vitrina_v1."""

from __future__ import annotations

import json
from pathlib import Path
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from urllib import error, parse as urllib_parse, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
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
ACTIVATED_AT = "2026-04-13T12:00:03Z"
REFRESHED_AT = "2026-04-13T12:05:00Z"
AS_OF_DATE = "2026-04-12"


class CountingBlock:
    def __init__(self, source_key: str, as_of_date: str) -> None:
        self.source_key = source_key
        self.as_of_date = as_of_date
        self.calls = 0

    def execute(self, request: object) -> SimpleNamespace:
        self.calls += 1
        payload = SimpleNamespace(
            kind="success",
            items=[],
            snapshot_date=self.as_of_date,
            date=self.as_of_date,
            date_from=self.as_of_date,
            date_to=self.as_of_date,
            detail=f"{self.source_key} synthetic success",
            storage_total=None,
        )
        return SimpleNamespace(result=payload)


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    with TemporaryDirectory(prefix="sheet-vitrina-refresh-read-split-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            refreshed_at_factory=lambda: REFRESHED_AT,
        )
        counters = _build_counting_blocks(AS_OF_DATE)
        entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(runtime=runtime, **counters)

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
            plan_url = f"http://127.0.0.1:{config.port}{config.sheet_plan_path}"
            operator_ui_url = f"http://127.0.0.1:{config.port}{config.sheet_operator_ui_path}"

            operator_ui_status, operator_ui_html = _get_text(operator_ui_url)
            if operator_ui_status != 200:
                raise AssertionError(f"operator UI must return 200, got {operator_ui_status}")
            if "Обновление данных витрины" not in operator_ui_html or "Загрузить данные" not in operator_ui_html:
                raise AssertionError("operator UI must expose the expected operator controls")
            operator_ui_config = _extract_operator_ui_config(operator_ui_html)
            if operator_ui_config["refresh_path"] != config.sheet_refresh_path:
                raise AssertionError("operator UI must point to the existing refresh endpoint")
            if operator_ui_config["status_path"] != config.sheet_status_path:
                raise AssertionError("operator UI must point to the cheap status endpoint")
            refresh_url = f"http://127.0.0.1:{config.port}{operator_ui_config['refresh_path']}"
            status_url = (
                f"http://127.0.0.1:{config.port}{operator_ui_config['status_path']}"
                f"?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}"
            )

            upload_status, upload_payload = _post_json(upload_url, bundle)
            if upload_status != 200 or upload_payload["status"] != "accepted":
                raise AssertionError(f"fixture upload must be accepted, got {upload_status} {upload_payload}")

            missing_load = _run_load_harness(upload_url, as_of_date=AS_OF_DATE)
            if "ready snapshot missing" not in missing_load["load_error"]:
                raise AssertionError(f"load must surface ready-snapshot miss, got {missing_load['load_error']!r}")
            if any(block.calls for block in counters.values()):
                raise AssertionError("missing snapshot read path must not trigger heavy source blocks")

            missing_status, missing_payload = _get_json(f"{plan_url}?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}")
            if missing_status != 422 or "ready snapshot missing" not in str(missing_payload.get("error", "")):
                raise AssertionError("cheap read endpoint must report missing ready snapshot before refresh")

            pre_refresh_status, pre_refresh_payload = _get_json(status_url)
            if pre_refresh_status != 422 or "ready snapshot missing" not in str(pre_refresh_payload.get("error", "")):
                raise AssertionError("cheap status endpoint must report missing ready snapshot before refresh")

            refresh_status, refresh_payload = _post_json(refresh_url, {"as_of_date": AS_OF_DATE})
            if refresh_status != 200:
                raise AssertionError(f"refresh endpoint must return 200, got {refresh_status}")
            if refresh_payload["status"] != "success":
                raise AssertionError("refresh endpoint must report success")
            if refresh_payload["refreshed_at"] != REFRESHED_AT:
                raise AssertionError("refresh_result refreshed_at mismatch")
            if refresh_payload["as_of_date"] != AS_OF_DATE:
                raise AssertionError("refresh_result as_of_date mismatch")
            if not all(block.calls == 1 for block in counters.values()):
                raise AssertionError("refresh endpoint must call each heavy source block exactly once")

            status_after_refresh, status_payload = _get_json(status_url)
            if status_after_refresh != 200:
                raise AssertionError(f"status endpoint must return 200 after refresh, got {status_after_refresh}")
            if status_payload["snapshot_id"] != refresh_payload["snapshot_id"]:
                raise AssertionError("status endpoint must return persisted refresh metadata")
            if status_payload["refreshed_at"] != REFRESHED_AT:
                raise AssertionError("status endpoint refreshed_at mismatch")
            if status_payload["sheet_row_counts"] != refresh_payload["sheet_row_counts"]:
                raise AssertionError("status endpoint row counts must match refresh result")

            plan_status, plan_payload = _get_json(f"{plan_url}?{urllib_parse.urlencode({'as_of_date': AS_OF_DATE})}")
            if plan_status != 200:
                raise AssertionError(f"plan read endpoint must return 200 after refresh, got {plan_status}")
            if plan_payload["snapshot_id"] != refresh_payload["snapshot_id"]:
                raise AssertionError("read endpoint must return the persisted ready snapshot")
            if not all(block.calls == 1 for block in counters.values()):
                raise AssertionError("cheap read endpoint must not trigger heavy source blocks after refresh")

            ready_load = _run_load_harness(upload_url, as_of_date=AS_OF_DATE)
            if ready_load["load_error"]:
                raise AssertionError(f"sheet-side load must succeed after refresh, got {ready_load['load_error']!r}")
            if ready_load["load_result"]["http_status"] != 200:
                raise AssertionError("sheet-side load must receive 200 from cheap read endpoint")
            if ready_load["sheets"]["DATA_VITRINA"]["values"][0] != ["дата", "key", AS_OF_DATE]:
                raise AssertionError("sheet-side load must materialize ready snapshot date header")
            if ready_load["sheets"]["STATUS"]["values"][1][0] != "registry_upload_current_state":
                raise AssertionError("STATUS sheet must be materialized from ready snapshot")
            if not all(block.calls == 1 for block in counters.values()):
                raise AssertionError("sheet-side load must not trigger heavy source blocks after refresh")

            print(f"missing_snapshot: ok -> {missing_load['load_error']}")
            print(f"operator_page: ok -> {config.sheet_operator_ui_path}")
            print(f"refresh_endpoint: ok -> {refresh_payload['snapshot_id']}")
            print(f"status_endpoint: ok -> {status_payload['snapshot_id']}")
            print(f"cheap_read_endpoint: ok -> {plan_payload['snapshot_id']}")
            print("sheet_load: ok -> ready snapshot only")
            print("smoke-check passed")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def _build_counting_blocks(as_of_date: str) -> dict[str, CountingBlock]:
    return {
        "web_source_block": CountingBlock("web_source_snapshot", as_of_date),
        "seller_funnel_block": CountingBlock("seller_funnel_snapshot", as_of_date),
        "sales_funnel_history_block": CountingBlock("sales_funnel_history", as_of_date),
        "prices_snapshot_block": CountingBlock("prices_snapshot", as_of_date),
        "sf_period_block": CountingBlock("sf_period", as_of_date),
        "spp_block": CountingBlock("spp", as_of_date),
        "ads_bids_block": CountingBlock("ads_bids", as_of_date),
        "stocks_block": CountingBlock("stocks", as_of_date),
        "ads_compact_block": CountingBlock("ads_compact", as_of_date),
        "fin_report_daily_block": CountingBlock("fin_report_daily", as_of_date),
    }


def _run_load_harness(endpoint_url: str, as_of_date: str) -> dict[str, object]:
    return json.loads(
        subprocess.check_output(
            [
                "node",
                str(ROOT / "apps" / "sheet_vitrina_v1_registry_upload_trigger_harness.js"),
                "--mode",
                "load_only",
                "--scriptPath",
                str(ROOT / "gas" / "sheet_vitrina_v1" / "RegistryUploadTrigger.gs"),
                "--endpointUrl",
                endpoint_url,
                "--asOfDate",
                as_of_date,
            ],
            cwd=ROOT,
            text=True,
        )
    )


def _post_json(url: str, payload: object) -> tuple[int, object]:
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


def _get_json(url: str) -> tuple[int, object]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req) as response:
            return response.status, response.read().decode("utf-8")
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _extract_operator_ui_config(html: str) -> dict[str, object]:
    match = re.search(
        r'<script id="sheet-vitrina-v1-operator-config" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError("operator UI config script is missing")
    return json.loads(match.group(1))


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
