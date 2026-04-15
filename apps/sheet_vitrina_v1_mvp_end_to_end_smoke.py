"""Интеграционный smoke-check первого end-to-end MVP sheet_vitrina_v1."""

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

ARTIFACTS_DIR = ROOT / "artifacts" / "sheet_vitrina_v1_mvp_end_to_end"
TARGET_DIR = ARTIFACTS_DIR / "target"
ACTIVATED_AT = "2026-04-13T14:00:00Z"
BUNDLE_VERSION = "sheet_vitrina_v1_mvp_e2e__2026-04-13T14:00:00Z"
UPLOADED_AT = "2026-04-13T14:00:00Z"
AS_OF_DATE = "2026-04-12"


def main() -> None:
    expected_summary = _load_json(TARGET_DIR / "mvp_summary__fixture.json")

    with TemporaryDirectory(prefix="sheet-vitrina-mvp-e2e-") as tmp:
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
                "SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK": "1",
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
                        "mvp_end_to_end",
                        "--scriptPath",
                        str(ROOT / "gas" / "sheet_vitrina_v1" / "RegistryUploadTrigger.gs"),
                        "--endpointUrl",
                        base_url,
                        "--refreshUrl",
                        f"http://127.0.0.1:{config.port}{config.sheet_refresh_path}",
                        "--bundleVersion",
                        BUNDLE_VERSION,
                        "--uploadedAt",
                        UPLOADED_AT,
                        "--asOfDate",
                        AS_OF_DATE,
                    ],
                    cwd=ROOT,
                    text=True,
                )
            )

            prepare_result = harness_result["prepare_result"]
            if prepare_result["seed_version"] != expected_summary["seed_version"]:
                raise AssertionError("unexpected seed_version")
            if prepare_result["seeded_counts"] != expected_summary["seeded_counts"]:
                raise AssertionError("seeded_counts mismatch")

            accepted = harness_result["accepted_response"]["upload_result"]
            if accepted["status"] != "accepted":
                raise AssertionError("upload path must return accepted")
            if accepted["accepted_counts"] != expected_summary["accepted_counts"]:
                raise AssertionError("accepted_counts mismatch")
            built_bundle = harness_result["built_bundle"]
            if len(built_bundle["metrics_v2"]) <= 64:
                raise AssertionError("integration smoke must stay above legacy 64-row metrics cap")
            if accepted["accepted_counts"]["config_v2"] != len(built_bundle["config_v2"]):
                raise AssertionError("upload path must accept every config_v2 row built from sheets")
            if accepted["accepted_counts"]["metrics_v2"] != len(built_bundle["metrics_v2"]):
                raise AssertionError("upload path must accept every metrics_v2 row built from sheets")
            if accepted["accepted_counts"]["formulas_v2"] != len(built_bundle["formulas_v2"]):
                raise AssertionError("upload path must accept every formulas_v2 row built from sheets")

            refresh_payload = harness_result["refresh_response"]
            if refresh_payload["status"] != "success":
                raise AssertionError("refresh endpoint must return success")
            if refresh_payload["as_of_date"] != AS_OF_DATE:
                raise AssertionError("refresh endpoint as_of_date mismatch")

            load_result = harness_result["load_result"]
            if load_result["http_status"] != 200:
                raise AssertionError(f"load endpoint must return 200, got {load_result['http_status']}")
            sheet_plan = load_result["sheet_plan"]
            if sheet_plan["snapshot_id"] != expected_summary["snapshot_id"]:
                raise AssertionError("snapshot_id mismatch")
            if sheet_plan["sheets"][1]["row_count"] != expected_summary["status_row_count"]:
                raise AssertionError("STATUS row_count mismatch")
            write_result = load_result["write_result"]
            if write_result["sheets"][0]["row_count"] != expected_summary["data_row_count"]:
                raise AssertionError("DATA_VITRINA row_count mismatch")
            if write_result["sheets"][0]["displayed_metric_count"] != expected_summary["metric_key_count"]:
                raise AssertionError("DATA_VITRINA displayed_metric_count mismatch")
            if write_result["sheets"][0]["source_row_count"] != expected_summary["source_row_count"]:
                raise AssertionError("DATA_VITRINA source_row_count mismatch")
            if write_result["sheets"][0]["source_metric_key_count"] != expected_summary["source_metric_key_count"]:
                raise AssertionError("DATA_VITRINA source_metric_key_count mismatch")
            if write_result["sheets"][0]["rendered_block_count"] != expected_summary["block_key_count"]:
                raise AssertionError("DATA_VITRINA rendered_block_count mismatch")
            if write_result["sheets"][0]["rendered_metric_row_count"] != expected_summary["metric_row_count"]:
                raise AssertionError("DATA_VITRINA rendered_metric_row_count mismatch")
            if write_result["sheets"][0]["rendered_data_row_count"] != expected_summary["data_row_count"]:
                raise AssertionError("DATA_VITRINA rendered_data_row_count mismatch")

            sheets = harness_result["sheets"]
            data_values = sheets["DATA_VITRINA"]["values"]
            status_values = sheets["STATUS"]["values"]
            if data_values[0] != expected_summary["data_header"]:
                raise AssertionError("DATA_VITRINA header mismatch")
            if status_values[0] != expected_summary["status_header"]:
                raise AssertionError("STATUS header mismatch")
            if data_values[1][:3] != expected_summary["first_block_row"]:
                raise AssertionError("unexpected TOTAL block header row")
            if data_values[2][:3] != expected_summary["first_metric_row"]:
                raise AssertionError("unexpected first DATA_VITRINA metric row")
            if data_values[3][:3] != expected_summary["second_metric_row"]:
                raise AssertionError("unexpected second DATA_VITRINA metric row")
            if data_values[expected_summary["first_sku_header_index"]][:2] != expected_summary["first_sku_header"]:
                raise AssertionError("unexpected first SKU DATA_VITRINA block header")
            if data_values[expected_summary["first_sku_header_index"] + 1][:2] != expected_summary["first_sku_metric_row"]:
                raise AssertionError("unexpected first SKU DATA_VITRINA metric row")

            sheet_state = harness_result["sheet_state"]
            data_state = next(item for item in sheet_state["sheets"] if item["sheet_name"] == "DATA_VITRINA")
            status_state = next(item for item in sheet_state["sheets"] if item["sheet_name"] == "STATUS")
            if data_state["layout_mode"] != "date_matrix":
                raise AssertionError("DATA_VITRINA must materialize date_matrix layout")
            if data_state["metric_key_count"] != expected_summary["metric_key_count"]:
                raise AssertionError("DATA_VITRINA metric_key_count mismatch")
            if data_state["metric_key_count"] <= 7:
                raise AssertionError("DATA_VITRINA must keep the full current-truth metric set")
            if data_state["data_row_count"] != expected_summary["data_row_count"]:
                raise AssertionError("DATA_VITRINA data_row_count mismatch")
            if data_state["block_key_count"] != expected_summary["block_key_count"]:
                raise AssertionError("DATA_VITRINA block_key_count mismatch")
            if data_state["date_column_count"] != expected_summary["date_column_count"]:
                raise AssertionError("DATA_VITRINA date_column_count mismatch")
            if data_state["scope_block_counts"] != expected_summary["scope_block_counts"]:
                raise AssertionError("DATA_VITRINA scope_block_counts mismatch")
            if data_state["section_row_count"] != expected_summary["section_row_count"]:
                raise AssertionError("DATA_VITRINA section_row_count mismatch")
            if data_state["separator_row_count"] != expected_summary["separator_row_count"]:
                raise AssertionError("DATA_VITRINA separator_row_count mismatch")
            if data_state["metric_row_count"] != expected_summary["metric_row_count"]:
                raise AssertionError("DATA_VITRINA metric_row_count mismatch")
            if data_state["non_empty_metric_row_count"] != expected_summary["non_empty_metric_row_count"]:
                raise AssertionError("DATA_VITRINA non_empty_metric_row_count mismatch")
            if data_state["rendered_block_count"] != expected_summary["block_key_count"]:
                raise AssertionError("DATA_VITRINA rendered_block_count mismatch")
            if data_state["rendered_date_column_count"] != expected_summary["date_column_count"]:
                raise AssertionError("DATA_VITRINA rendered_date_column_count mismatch")
            if data_state["rendered_data_row_count"] != expected_summary["data_row_count"]:
                raise AssertionError("DATA_VITRINA rendered_data_row_count mismatch")
            if status_state["status_row_count"] != expected_summary["status_row_count"]:
                raise AssertionError("STATUS status_row_count mismatch")

            status_keys = [row[0] for row in status_values[1:]]
            if status_keys != expected_summary["status_keys"]:
                raise AssertionError("STATUS source keys mismatch")
            if status_state["source_keys"] != expected_summary["status_keys"]:
                raise AssertionError("STATUS state summary source keys mismatch")

            data_presentation = next(
                item for item in harness_result["presentation_snapshot"]["sheets"] if item["sheet_name"] == "DATA_VITRINA"
            )
            if data_presentation["frozen_columns"] != 2:
                raise AssertionError("DATA_VITRINA frozen_columns mismatch")
            if data_presentation["header_style"]["background"] != "#ffffff":
                raise AssertionError("DATA_VITRINA header must not keep dark fill")
            if data_presentation["samples"]["section"] is None:
                raise AssertionError("DATA_VITRINA matrix view must keep section rows")
            if data_presentation["samples"]["percent"]["number_format"] != "0.0%":
                raise AssertionError("DATA_VITRINA percent format mismatch")
            if data_presentation["samples"]["decimal"]["number_format"] != "#,##0.00":
                raise AssertionError("DATA_VITRINA decimal format mismatch")
            if data_presentation["samples"]["integer"]["number_format"] != "#,##0":
                raise AssertionError("DATA_VITRINA integer format mismatch")

            status_block = harness_result["status_block"]
            if status_block["endpoint_url"] != base_url:
                raise AssertionError("CONFIG!I2 endpoint_url must be preserved")
            if status_block["last_status"] != "accepted":
                raise AssertionError("CONFIG!I4 last_status must remain accepted after load")

            runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
            current_state = asdict(runtime.load_current_state())
            if current_state["bundle_version"] != BUNDLE_VERSION:
                raise AssertionError("runtime current_state bundle_version mismatch")
            if len(current_state["config_v2"]) != expected_summary["accepted_counts"]["config_v2"]:
                raise AssertionError("runtime config_v2 count mismatch")
            if len(current_state["metrics_v2"]) != expected_summary["accepted_counts"]["metrics_v2"]:
                raise AssertionError("runtime metrics_v2 count mismatch")
            if len(current_state["formulas_v2"]) != expected_summary["accepted_counts"]["formulas_v2"]:
                raise AssertionError("runtime formulas_v2 count mismatch")

            print(f"prepare seed: ok -> {prepare_result['seeded_counts']}")
            print(f"upload accepted: ok -> {accepted['bundle_version']}")
            print(f"refresh snapshot: ok -> {refresh_payload['snapshot_id']}")
            print(f"load DATA_VITRINA: ok -> {write_result['sheets'][0]['write_rect']}")
            print(f"load STATUS: ok -> {sheet_plan['sheets'][1]['write_rect']}")
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
