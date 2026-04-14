"""Smoke-check для sheet_vitrina_v1 registry upload trigger."""

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

from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_PLAN_PATH, DEFAULT_UPLOAD_PATH
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

ARTIFACTS_DIR = ROOT / "artifacts" / "sheet_vitrina_v1_registry_upload_trigger"
INPUT_BUNDLE_FIXTURE = ARTIFACTS_DIR / "input" / "registry_upload_bundle__fixture.json"
TARGET_DIR = ARTIFACTS_DIR / "target"
ACTIVATED_AT = "2026-04-13T12:00:04Z"


def main() -> None:
    bundle_fixture = _load_json(INPUT_BUNDLE_FIXTURE)
    expected_bundle = _load_json(TARGET_DIR / "bundle_from_sheets__fixture.json")
    expected_accepted = _load_json(TARGET_DIR / "upload_response__accepted__fixture.json")
    expected_duplicate = _load_json(TARGET_DIR / "upload_response__duplicate_bundle_version__fixture.json")
    expected_current_state = _load_json(TARGET_DIR / "current_state__fixture.json")

    with TemporaryDirectory(prefix="sheet-vitrina-registry-upload-trigger-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        port = _reserve_free_port()
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            runtime_dir=runtime_dir,
        )
        env = os.environ.copy()
        env.update(
            {
                "REGISTRY_UPLOAD_HTTP_HOST": config.host,
                "REGISTRY_UPLOAD_HTTP_PORT": str(config.port),
                "REGISTRY_UPLOAD_HTTP_PATH": config.upload_path,
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
            base_url = f"http://127.0.0.1:{config.port}{config.upload_path}"
            _wait_until_reachable(base_url)

            harness_result = json.loads(
                subprocess.check_output(
                    [
                        "node",
                        str(ROOT / "apps" / "sheet_vitrina_v1_registry_upload_trigger_harness.js"),
                        "--scriptPath",
                        str(ROOT / "gas" / "sheet_vitrina_v1" / "RegistryUploadTrigger.gs"),
                        "--fixturePath",
                        str(INPUT_BUNDLE_FIXTURE),
                        "--endpointUrl",
                        base_url,
                        "--bundleVersion",
                        bundle_fixture["bundle_version"],
                        "--uploadedAt",
                        bundle_fixture["uploaded_at"],
                    ],
                    cwd=ROOT,
                    text=True,
                )
            )

            ensure_result = harness_result["ensure_result"]
            if ensure_result["sheet_names"] != ["CONFIG", "METRICS", "FORMULAS"]:
                raise AssertionError(f"unexpected sheet names: {ensure_result['sheet_names']}")

            if harness_result["built_bundle"] != expected_bundle:
                raise AssertionError("sheet-built bundle differs from target fixture")
            if len(harness_result["built_bundle"]["metrics_v2"]) != len(bundle_fixture["metrics_v2"]):
                raise AssertionError("sheet-built bundle must preserve all metrics_v2 rows from the input fixture")
            accepted_response = harness_result["accepted_response"]
            if accepted_response.get("ok") != "success":
                raise AssertionError("accepted trigger response must be marked as success")
            accepted_subset = {
                "http_status": accepted_response["http_status"],
                "upload_result": accepted_response["upload_result"],
            }
            if accepted_subset != expected_accepted:
                raise AssertionError("accepted trigger response differs from target fixture")
            if accepted_response["upload_result"]["accepted_counts"]["metrics_v2"] != len(bundle_fixture["metrics_v2"]):
                raise AssertionError("accepted upload must persist all metrics_v2 rows from the sheet bundle")
            duplicate_response = harness_result["duplicate_response"]
            if duplicate_response.get("ok") != "success":
                raise AssertionError("duplicate trigger response must be marked as success")
            duplicate_subset = {
                "http_status": duplicate_response["http_status"],
                "upload_result": duplicate_response["upload_result"],
            }
            if duplicate_subset != expected_duplicate:
                raise AssertionError("duplicate trigger response differs from target fixture")

            status_block = harness_result["status_block"]
            if status_block["endpoint_url"] != base_url:
                raise AssertionError("status block endpoint_url mismatch")
            if status_block["last_bundle_version"] != bundle_fixture["bundle_version"]:
                raise AssertionError("status block last_bundle_version mismatch")
            if status_block["last_status"] != "rejected":
                raise AssertionError("status block last_status must reflect duplicate rejection")
            if status_block["last_http_status"] != "409":
                raise AssertionError("status block last_http_status must reflect duplicate rejection")
            if "bundle_version already accepted" not in status_block["last_validation_errors"]:
                raise AssertionError("status block last_validation_errors must reflect duplicate rejection")

            runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
            current_state = asdict(runtime.load_current_state())
            if current_state != expected_current_state:
                raise AssertionError("runtime current state differs from target fixture")

            print("sheet layout: ok -> CONFIG, METRICS, FORMULAS")
            print(f"bundle_from_sheets: ok -> {bundle_fixture['bundle_version']}")
            print(f"accepted status: ok -> {accepted_response['upload_result']['status']}")
            print(f"duplicate status: ok -> {duplicate_response['upload_result']['status']}")
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
    probe_payload = {"bundle_version": "", "uploaded_at": "", "config_v2": [], "metrics_v2": [], "formulas_v2": []}
    deadline = time.time() + 10
    while True:
        try:
            _post_json(url, probe_payload)
            return
        except error.URLError:
            if time.time() >= deadline:
                raise AssertionError("HTTP entrypoint did not become reachable")
            time.sleep(0.1)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
