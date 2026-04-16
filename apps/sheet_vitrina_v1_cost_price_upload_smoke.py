"""Integration smoke-check для sheet-side COST_PRICE upload contour."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib import error, request as urllib_request

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
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

ACTIVATED_AT = "2026-04-16T10:10:02Z"


def main() -> None:
    fixture_payload = {
        "dataset_version": "sheet_vitrina_v1_cost_price_sheet_fixture__2026-04-16T10:10:00Z",
        "uploaded_at": "2026-04-16T10:10:00Z",
        "cost_price_rows": [
            {"group": "Clean", "cost_price_rub": 123.45, "effective_from": "14.04.2026"},
            {"group": "Anti-Spy", "cost_price_rub": "234,50", "effective_from": "2026-04-15"},
        ],
    }
    expected_built_payload = {
        "dataset_version": fixture_payload["dataset_version"],
        "uploaded_at": fixture_payload["uploaded_at"],
        "cost_price_rows": [
            {"group": "Clean", "cost_price_rub": 123.45, "effective_from": "14.04.2026"},
            {"group": "Anti-Spy", "cost_price_rub": 234.5, "effective_from": "2026-04-15"},
        ],
    }

    with TemporaryDirectory(prefix="sheet-vitrina-cost-price-upload-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        fixture_path = Path(tmp) / "cost_price_fixture.json"
        fixture_path.write_text(json.dumps(fixture_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
        env = os.environ.copy()
        env.update(
            {
                "REGISTRY_UPLOAD_HTTP_HOST": config.host,
                "REGISTRY_UPLOAD_HTTP_PORT": str(config.port),
                "REGISTRY_UPLOAD_HTTP_PATH": config.upload_path,
                "COST_PRICE_UPLOAD_HTTP_PATH": config.cost_price_upload_path,
                "SHEET_VITRINA_HTTP_PATH": config.sheet_plan_path,
                "SHEET_VITRINA_REFRESH_HTTP_PATH": config.sheet_refresh_path,
                "SHEET_VITRINA_STATUS_HTTP_PATH": config.sheet_status_path,
                "SHEET_VITRINA_OPERATOR_UI_PATH": config.sheet_operator_ui_path,
                "REGISTRY_UPLOAD_RUNTIME_DIR": str(config.runtime_dir),
                "REGISTRY_UPLOAD_ACTIVATED_AT_OVERRIDE": ACTIVATED_AT,
            }
        )

        process = subprocess.Popen(
            [sys.executable, str(ROOT / "apps" / "registry_upload_http_entrypoint_live.py")],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            base_url = f"http://127.0.0.1:{config.port}{config.cost_price_upload_path}"
            _wait_until_reachable(base_url)

            harness_result = json.loads(
                subprocess.check_output(
                    [
                        "node",
                        str(ROOT / "apps" / "sheet_vitrina_v1_registry_upload_trigger_harness.js"),
                        "--mode",
                        "cost_price_upload",
                        "--scriptPath",
                        str(ROOT / "gas" / "sheet_vitrina_v1" / "RegistryUploadTrigger.gs"),
                        "--fixturePath",
                        str(fixture_path),
                        "--endpointUrl",
                        base_url,
                        "--datasetVersion",
                        fixture_payload["dataset_version"],
                        "--uploadedAt",
                        fixture_payload["uploaded_at"],
                    ],
                    cwd=ROOT,
                    text=True,
                )
            )

            prepare_result = harness_result["prepare_result"]
            if prepare_result["sheet_name"] != "COST_PRICE":
                raise AssertionError("prepareCostPriceSheet must materialize COST_PRICE sheet")
            if prepare_result["summary"]["header"] != ["group", "cost_price_rub", "effective_from"]:
                raise AssertionError("COST_PRICE sheet must expose canonical headers")

            if harness_result["built_payload"] != expected_built_payload:
                raise AssertionError("Apps Script must build the expected separate cost price payload")

            accepted_response = harness_result["accepted_response"]
            if accepted_response.get("ok") != "success":
                raise AssertionError("accepted COST_PRICE upload response must be marked as success")
            if accepted_response["upload_result"]["status"] != "accepted":
                raise AssertionError("accepted COST_PRICE upload must return accepted result")
            if accepted_response["upload_result"]["accepted_counts"]["cost_price_rows"] != 2:
                raise AssertionError("accepted COST_PRICE upload must persist factual row count")

            duplicate_response = harness_result["duplicate_response"]
            if duplicate_response.get("ok") != "success":
                raise AssertionError("duplicate COST_PRICE upload response must be marked as success")
            if duplicate_response["http_status"] != 409:
                raise AssertionError("duplicate COST_PRICE upload must return HTTP 409")
            if duplicate_response["upload_result"]["status"] != "rejected":
                raise AssertionError("duplicate COST_PRICE upload must be rejected")

            status_block = harness_result["status_block"]
            if status_block["endpoint_url"] != base_url:
                raise AssertionError("COST_PRICE status block endpoint_url mismatch")
            if status_block["last_dataset_version"] != fixture_payload["dataset_version"]:
                raise AssertionError("COST_PRICE status block must keep dataset_version")
            if status_block["last_status"] != "rejected":
                raise AssertionError("COST_PRICE status block last_status must reflect duplicate rejection")
            if status_block["last_http_status"] != "409":
                raise AssertionError("COST_PRICE status block last_http_status must reflect duplicate rejection")
            if "dataset_version already accepted" not in status_block["last_validation_errors"]:
                raise AssertionError("COST_PRICE status block must show duplicate rejection message")

            runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
            current_state = asdict(runtime.load_cost_price_current_state())
            expected_current_state = {
                "dataset_version": fixture_payload["dataset_version"],
                "activated_at": ACTIVATED_AT,
                "cost_price_rows": [
                    {"group": "Clean", "cost_price_rub": 123.45, "effective_from": "2026-04-14"},
                    {"group": "Anti-Spy", "cost_price_rub": 234.5, "effective_from": "2026-04-15"},
                ],
            }
            if current_state != expected_current_state:
                raise AssertionError("runtime current cost price state must persist canonicalized rows")

            print("cost_price_sheet: ok -> COST_PRICE")
            print(f"cost_price_upload: ok -> {accepted_response['upload_result']['status']}")
            print(f"cost_price_rows: ok -> {accepted_response['upload_result']['accepted_counts']['cost_price_rows']}")
            print(f"cost_price_duplicate: ok -> {duplicate_response['upload_result']['status']}")
            print("smoke-check passed")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


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


def _wait_until_reachable(url: str) -> None:
    probe_payload = {"dataset_version": "", "uploaded_at": "", "cost_price_rows": []}
    deadline = time.time() + 10
    while True:
        try:
            _post_json(url, probe_payload)
            return
        except error.URLError:
            if time.time() >= deadline:
                raise AssertionError("COST_PRICE HTTP entrypoint did not become reachable")
            time.sleep(0.1)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    main()
