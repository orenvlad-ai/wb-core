"""Targeted smoke-check для отдельного COST_PRICE HTTP upload contour."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
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
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.cost_price_upload import CostPriceCurrentState, CostPriceRow
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

ACTIVATED_AT = "2026-04-16T10:10:01Z"


def main() -> None:
    accepted_payload = {
        "dataset_version": "sheet_vitrina_v1_cost_price__2026-04-16T10:10:00Z",
        "uploaded_at": "2026-04-16T10:10:00Z",
        "cost_price_rows": [
            {"group": "Clean", "cost_price_rub": 123.45, "effective_from": "14.04.2026"},
            {"group": "Anti-Spy", "cost_price_rub": "234,50", "effective_from": "2026-04-15T00:00:00Z"},
            {"group": "Clean", "cost_price_rub": 150, "effective_from": "2026-04-16"},
        ],
    }
    rejected_payload = {
        "dataset_version": "sheet_vitrina_v1_cost_price__duplicate-key__2026-04-16T10:10:00Z",
        "uploaded_at": "2026-04-16T10:10:00Z",
        "cost_price_rows": [
            {"group": "Clean", "cost_price_rub": 100, "effective_from": "16.04.2026"},
            {"group": "Clean", "cost_price_rub": 120, "effective_from": "2026-04-16"},
        ],
    }

    with TemporaryDirectory(prefix="cost-price-http-entrypoint-") as tmp:
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
            cost_price_upload_path=DEFAULT_COST_PRICE_UPLOAD_PATH,
        )
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir),
            activated_at_factory=lambda: ACTIVATED_AT,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{config.port}{config.cost_price_upload_path}"
            accepted_status, accepted_result = _post_json(url, accepted_payload)
            if accepted_status != 200:
                raise AssertionError(f"accepted cost price upload must return 200, got {accepted_status}")
            if accepted_result["status"] != "accepted":
                raise AssertionError("accepted cost price upload must return accepted status")
            if accepted_result["dataset_version"] != accepted_payload["dataset_version"]:
                raise AssertionError("accepted cost price upload must preserve dataset_version")
            if accepted_result["accepted_counts"]["cost_price_rows"] != 3:
                raise AssertionError("accepted cost price upload must persist factual row count")

            runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
            current_state = runtime.load_cost_price_current_state()
            expected_state = CostPriceCurrentState(
                dataset_version=accepted_payload["dataset_version"],
                activated_at=ACTIVATED_AT,
                cost_price_rows=[
                    CostPriceRow(group="Clean", cost_price_rub=123.45, effective_from="2026-04-14"),
                    CostPriceRow(group="Anti-Spy", cost_price_rub=234.5, effective_from="2026-04-15"),
                    CostPriceRow(group="Clean", cost_price_rub=150.0, effective_from="2026-04-16"),
                ],
            )
            if asdict(current_state) != asdict(expected_state):
                raise AssertionError("cost price current state must canonicalize dates and preserve row order")

            persisted_result = runtime.load_persisted_cost_price_upload_result(accepted_payload["dataset_version"])
            if asdict(persisted_result) != accepted_result:
                raise AssertionError("persisted cost price upload result must match HTTP result")

            duplicate_status, duplicate_result = _post_json(url, accepted_payload)
            if duplicate_status != 409:
                raise AssertionError(f"duplicate dataset_version must return 409, got {duplicate_status}")
            if duplicate_result["status"] != "rejected":
                raise AssertionError("duplicate dataset_version must be rejected")
            if "dataset_version already accepted" not in "; ".join(duplicate_result["validation_errors"]):
                raise AssertionError("duplicate dataset_version must surface canonical validation error")

            rejected_status, rejected_result = _post_json(url, rejected_payload)
            if rejected_status != 422:
                raise AssertionError(f"duplicate cost key payload must return 422, got {rejected_status}")
            if rejected_result["status"] != "rejected":
                raise AssertionError("duplicate cost key payload must be rejected")
            if "cost_price_rows.(group,effective_from) contains duplicates" not in "; ".join(
                rejected_result["validation_errors"]
            ):
                raise AssertionError("duplicate cost key payload must surface canonical duplicate-key error")

            print(f"cost_price_path: ok -> {config.cost_price_upload_path}")
            print(f"accepted_rows: ok -> {accepted_result['accepted_counts']['cost_price_rows']}")
            print(f"current_dataset_version: ok -> {current_state.dataset_version}")
            print(f"duplicate_status: ok -> {duplicate_result['status']}")
            print("smoke-check passed")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


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


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    main()
