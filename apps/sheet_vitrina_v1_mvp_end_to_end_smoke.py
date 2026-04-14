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

from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_PLAN_PATH, DEFAULT_UPLOAD_PATH
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
            runtime_dir=runtime_dir,
        )
        env = os.environ.copy()
        env.update(
            {
                "REGISTRY_UPLOAD_HTTP_HOST": config.host,
                "REGISTRY_UPLOAD_HTTP_PORT": str(config.port),
                "REGISTRY_UPLOAD_HTTP_PATH": config.upload_path,
                "SHEET_VITRINA_HTTP_PATH": config.sheet_plan_path,
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

            load_result = harness_result["load_result"]
            if load_result["http_status"] != 200:
                raise AssertionError(f"load endpoint must return 200, got {load_result['http_status']}")
            sheet_plan = load_result["sheet_plan"]
            if sheet_plan["snapshot_id"] != expected_summary["snapshot_id"]:
                raise AssertionError("snapshot_id mismatch")
            if sheet_plan["sheets"][1]["row_count"] != expected_summary["status_row_count"]:
                raise AssertionError("STATUS row_count mismatch")

            sheets = harness_result["sheets"]
            sheet_state = harness_result["sheet_state"]
            data_values = sheets["DATA_VITRINA"]["values"]
            status_values = sheets["STATUS"]["values"]
            if data_values[0] != expected_summary["data_header"]:
                raise AssertionError("DATA_VITRINA header mismatch")
            if status_values[0] != expected_summary["status_header"]:
                raise AssertionError("STATUS header mismatch")

            status_keys = [row[0] for row in status_values[1:]]
            if status_keys != expected_summary["status_keys"]:
                raise AssertionError("STATUS source keys mismatch")

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

            enabled_config = [item for item in current_state["config_v2"] if item["enabled"]]
            expected_metric_keys = [
                item["metric_key"]
                for item in sorted(
                    [item for item in current_state["metrics_v2"] if item["enabled"] and item["show_in_data"]],
                    key=lambda row: row["display_order"],
                )
            ]
            expected_group_count = len({item["group"] for item in enabled_config})
            expected_data_row_count = len(expected_metric_keys) * (1 + expected_group_count + len(enabled_config))
            if sheet_plan["sheets"][0]["row_count"] != expected_data_row_count:
                raise AssertionError("DATA_VITRINA row_count mismatch")

            displayed_metric_keys = _extract_displayed_metric_keys(data_values)
            if displayed_metric_keys != expected_metric_keys:
                raise AssertionError("DATA_VITRINA must materialize the full authoritative metric key set")

            data_state = next(sheet for sheet in sheet_state["sheets"] if sheet["sheet_name"] == "DATA_VITRINA")
            if data_state["metric_key_count"] != len(expected_metric_keys):
                raise AssertionError("getSheetVitrinaV1State metric_key_count mismatch")
            if data_state["metric_keys"] != expected_metric_keys:
                raise AssertionError("getSheetVitrinaV1State metric_keys mismatch")
            if data_state["section_row_counts"] != {
                "TOTAL": len(expected_metric_keys),
                "GROUP": len(expected_metric_keys) * expected_group_count,
                "SKU": len(expected_metric_keys) * len(enabled_config),
                "OTHER": 0,
            }:
                raise AssertionError("getSheetVitrinaV1State section_row_counts mismatch")

            total_view_count_row = _find_row_by_key(data_values, "TOTAL|view_count")
            if not isinstance(total_view_count_row[2], (int, float)):
                raise AssertionError("supported live metric rows must keep numeric values")

            total_stock_row = _find_row_by_key(data_values, "TOTAL|stock_total")
            if total_stock_row[:2] != ["Итого: Остаток, шт", "TOTAL|stock_total"]:
                raise AssertionError("authoritative metric rows must include newly unlocked stock_total")

            live_summary_row = _find_status_row(status_values, "sheet_vitrina_v1_mvp")
            live_summary_note = str(live_summary_row[10] or "")
            if f"displayed_metrics={','.join(expected_metric_keys)}" not in live_summary_note:
                raise AssertionError("STATUS live summary must expose the full displayed metric key set")
            if "live_value_supported_metrics=view_count,ctr,open_card_count,views_current,ctr_current,orders_current,position_avg" not in live_summary_note:
                raise AssertionError("STATUS live summary must expose current live numeric coverage")

            print(f"prepare seed: ok -> {prepare_result['seeded_counts']}")
            print(f"upload accepted: ok -> {accepted['bundle_version']}")
            print(f"displayed metrics: ok -> {len(expected_metric_keys)}")
            print(f"load DATA_VITRINA: ok -> {sheet_plan['sheets'][0]['write_rect']}")
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


def _extract_displayed_metric_keys(values: list[list[object]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in values[1:]:
        if len(row) < 2:
            continue
        key = str(row[1] or "")
        if "|" not in key:
            continue
        metric_key = key.split("|")[-1]
        if metric_key not in seen:
            seen.add(metric_key)
            out.append(metric_key)
    return out


def _find_row_by_key(values: list[list[object]], key: str) -> list[object]:
    for row in values[1:]:
        if len(row) >= 2 and str(row[1] or "") == key:
            return row
    raise AssertionError(f"unable to find DATA_VITRINA row by key: {key}")


def _find_status_row(values: list[list[object]], source_key: str) -> list[object]:
    for row in values[1:]:
        if row and str(row[0] or "") == source_key:
            return row
    raise AssertionError(f"unable to find STATUS row by source_key: {source_key}")


if __name__ == "__main__":
    main()
