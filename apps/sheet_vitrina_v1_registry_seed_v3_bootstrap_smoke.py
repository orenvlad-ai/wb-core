"""Smoke-check для compact v3 bootstrap в sheet_vitrina_v1."""

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
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_UPLOAD_PATH,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

ARTIFACTS_DIR = ROOT / "artifacts" / "sheet_vitrina_v1_registry_seed_v3_bootstrap"
TARGET_DIR = ARTIFACTS_DIR / "target"
ACTIVATED_AT = "2026-04-13T12:10:04Z"
BUNDLE_VERSION = "sheet_vitrina_v1_registry_seed_v3__2026-04-13T12:10:00Z"
UPLOADED_AT = "2026-04-13T12:10:00Z"


def main() -> None:
    expected_metrics_seed = _load_json(ARTIFACTS_DIR / "input" / "metrics_v3_seed__fixture.json")
    expected_formulas_seed = _load_json(ARTIFACTS_DIR / "input" / "formulas_v3_seed__fixture.json")
    expected_prepare = _load_json(TARGET_DIR / "prepare_result__fixture.json")
    expected_preserved = _load_json(TARGET_DIR / "preserved_control_block__fixture.json")
    expected_bundle = _load_json(TARGET_DIR / "bundle_from_seed_v3__fixture.json")
    expected_accepted = _load_json(TARGET_DIR / "upload_response__accepted__fixture.json")
    expected_status = _load_json(TARGET_DIR / "status_block__after_upload__fixture.json")
    expected_current_state = _load_json(TARGET_DIR / "current_state__fixture.json")

    with TemporaryDirectory(prefix="sheet-vitrina-registry-seed-v3-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
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
        env = os.environ.copy()
        env.update(
            {
                "REGISTRY_UPLOAD_HTTP_HOST": config.host,
                "REGISTRY_UPLOAD_HTTP_PORT": str(config.port),
                "REGISTRY_UPLOAD_HTTP_PATH": config.upload_path,
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
            base_url = f"http://127.0.0.1:{config.port}{config.upload_path}"
            _wait_until_reachable(base_url)

            harness_result = json.loads(
                subprocess.check_output(
                    [
                        "node",
                        str(ROOT / "apps" / "sheet_vitrina_v1_registry_upload_trigger_harness.js"),
                        "--mode",
                        "seed_bootstrap",
                        "--scriptPath",
                        str(ROOT / "gas" / "sheet_vitrina_v1" / "RegistryUploadTrigger.gs"),
                        "--endpointUrl",
                        base_url,
                        "--bundleVersion",
                        BUNDLE_VERSION,
                        "--uploadedAt",
                        UPLOADED_AT,
                    ],
                    cwd=ROOT,
                    text=True,
                )
            )

            initial_prepare = harness_result["prepare_result"]
            if initial_prepare["sheet_names"] != ["CONFIG", "METRICS", "FORMULAS"]:
                raise AssertionError(f"unexpected sheet names: {initial_prepare['sheet_names']}")
            if not all(item["created"] for item in initial_prepare["sheets"]):
                raise AssertionError("initial prepare must create all sheets in empty harness spreadsheet")

            if harness_result["prepare_result_after_reprepare"] != expected_prepare:
                raise AssertionError("prepare result after reprepare differs from target fixture")
            if harness_result["prepare_result_after_reprepare"]["seeded_counts"]["metrics_v2"] != len(
                expected_metrics_seed["items"]
            ):
                raise AssertionError("prepare must materialize full authoritative metrics set")
            if harness_result["prepare_result_after_reprepare"]["seeded_counts"]["formulas_v2"] != len(
                expected_formulas_seed["items"]
            ):
                raise AssertionError("prepare must materialize formula set required by authoritative metrics")
            preserved_control_block = harness_result["preserved_control_block"]
            if preserved_control_block["endpoint_url"] != base_url:
                raise AssertionError("preserved control block endpoint_url mismatch")
            preserved_subset = {
                "last_bundle_version": preserved_control_block["last_bundle_version"],
                "last_status": preserved_control_block["last_status"],
                "last_activated_at": preserved_control_block["last_activated_at"],
                "last_http_status": preserved_control_block["last_http_status"],
                "last_validation_errors": preserved_control_block["last_validation_errors"],
            }
            if preserved_subset != expected_preserved:
                raise AssertionError("preserved control block differs from target fixture")
            if harness_result["built_bundle"] != expected_bundle:
                raise AssertionError("bundle from seeded sheets differs from target fixture")
            built_metric_keys = [item["metric_key"] for item in harness_result["built_bundle"]["metrics_v2"]]
            expected_metric_keys = [item["metric_key"] for item in expected_metrics_seed["items"]]
            if built_metric_keys != expected_metric_keys:
                raise AssertionError("bundle must contain full authoritative metrics_v2 set in canonical order")
            built_formula_ids = [item["formula_id"] for item in harness_result["built_bundle"]["formulas_v2"]]
            expected_formula_ids = [item["formula_id"] for item in expected_formulas_seed["items"]]
            if built_formula_ids != expected_formula_ids:
                raise AssertionError("bundle formulas_v2 must stay aligned with authoritative upload metrics")

            accepted_response = harness_result["accepted_response"]
            accepted_subset = {
                "http_status": accepted_response["http_status"],
                "upload_result": accepted_response["upload_result"],
            }
            if accepted_subset != expected_accepted:
                raise AssertionError("accepted response differs from target fixture")
            if accepted_response["upload_result"]["accepted_counts"]["metrics_v2"] != len(expected_metric_keys):
                raise AssertionError("accepted upload must persist every authoritative metrics_v2 row")
            if accepted_response["endpoint_url"] != base_url:
                raise AssertionError("accepted response endpoint_url mismatch")
            status_block = harness_result["status_block"]
            if status_block["endpoint_url"] != base_url:
                raise AssertionError("status block endpoint_url mismatch")
            status_subset = {
                "last_bundle_version": status_block["last_bundle_version"],
                "last_status": status_block["last_status"],
                "last_activated_at": status_block["last_activated_at"],
                "last_http_status": status_block["last_http_status"],
                "last_validation_errors": status_block["last_validation_errors"],
            }
            if status_subset != expected_status:
                raise AssertionError("status block after upload differs from target fixture")

            runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
            current_state = asdict(runtime.load_current_state())
            if current_state != expected_current_state:
                raise AssertionError("runtime current state differs from target fixture")

            print("prepare compact v3: ok -> CONFIG, METRICS, FORMULAS")
            print("control block preservation: ok")
            print(f"bundle_from_seed: ok -> {BUNDLE_VERSION}")
            print(f"accepted status: ok -> {accepted_response['upload_result']['status']}")
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
