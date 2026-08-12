"""Protected GET-only HTTP boundary checks for Stage 5 FBS observations."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_FF_POOL_FBS_ORDERS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    _is_ff_pool_mutation_path,
    build_registry_upload_http_server,
)
from packages.adapters.wb_fbs_orders import WbFbsOrdersPage  # noqa: E402
from packages.application.ff_wb_supply_origins import ASSIGNMENTS_TABLE  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    OBSERVATIONS_TABLE,
    WbFbsOrdersCollector,
)
from packages.contracts.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypointConfig,
)


class _Source:
    def list_orders(
        self,
        *,
        limit: int,
        next_cursor: int,
        date_from: int | None,
        date_to: int | None,
    ) -> WbFbsOrdersPage:
        assert next_cursor == 0
        return WbFbsOrdersPage(
            orders=[
                {
                    "id": 55000001,
                    "supplyId": "WB-GI-55000001",
                    "deliveryType": "fbs",
                    "createdAt": "2026-08-12T08:00:00Z",
                    "warehouseId": 507,
                    "officeId": 123,
                    "nmId": 140557512,
                    "chrtId": 987654321,
                    "skus": ["0001234567890"],
                    "cargoType": 1,
                    "crossBorderType": 0,
                    "isZeroOrder": False,
                    "address": {"fullAddress": "must not cross HTTP"},
                    "comment": "must not cross HTTP",
                }
            ],
            next_cursor=0,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
        )


def main() -> None:
    assert not _is_ff_pool_mutation_path(DEFAULT_FF_POOL_FBS_ORDERS_PATH)
    assert not _is_ff_pool_mutation_path(f"{DEFAULT_FF_POOL_FBS_ORDERS_PATH}/55000001")
    with TemporaryDirectory(prefix="wb-fbs-orders-http-") as directory:
        runtime_dir = Path(directory) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.list_wb_supplies()
        WbFbsOrdersCollector(
            db_path=runtime.db_path,
            timestamp_factory=lambda: "2026-08-12T09:00:00Z",
            unix_time_factory=lambda: 1_786_522_400,
            source=_Source(),
            enabled=True,
        ).collect_default_window()
        before = _target_counts(runtime.db_path)
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(
            config,
            entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir, runtime=runtime),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{config.port}"
            root = f"{base}{DEFAULT_FF_POOL_FBS_ORDERS_PATH}"
            code, payload, headers = _json_request(root)
            assert code == 200 and payload["contract_name"] == "wb_fbs_orders_readonly_shadow_v1"
            assert payload["page"]["total"] == 1 and len(payload["rows"]) == 1
            assert payload["policy"]["upstream_get_only"] is True
            assert payload["policy"]["creates_movement"] is False
            assert payload["policy"]["assigns_ff_origin"] is False
            assert "address" not in payload["rows"][0] and "comment" not in payload["rows"][0]
            etag = next((value for key, value in headers.items() if key.lower() == "etag"), "")
            assert etag.startswith('"sha256:')
            not_modified, empty, response_headers = _json_request(
                root, headers={"If-None-Match": etag}
            )
            assert not_modified == 304 and empty == {}
            assert next(
                value for key, value in response_headers.items() if key.lower() == "etag"
            ) == etag

            detail_code, detail, _ = _json_request(f"{root}/55000001")
            assert detail_code == 200 and detail["current"]["order_id"] == 55000001
            assert detail["current"]["supply_id"] == "WB-GI-55000001"
            filtered_code, filtered, _ = _json_request(
                f"{root}?nm_id=140557512&supply_id=WB-GI-55000001&limit=1"
            )
            assert filtered_code == 200 and filtered["page"]["total"] == 1
            missing_code, missing, _ = _json_request(f"{root}/99999999")
            assert missing_code == 404 and missing["code"] == "fbs_order_not_found"
            invalid_code, invalid, _ = _json_request(f"{root}?search=%25")
            assert invalid_code == 422 and invalid["code"] == "invalid_search"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        assert _target_counts(runtime.db_path) == before
    print("wb_fbs_orders_http_smoke: OK")


def _target_counts(db_path: Path) -> tuple[int, int, int, int, int]:
    with sqlite3.connect(db_path) as conn:
        return (
            int(conn.execute(f"SELECT COUNT(*) FROM {OBSERVATIONS_TABLE}").fetchone()[0]),
            int(conn.execute(f"SELECT COUNT(*) FROM {ASSIGNMENTS_TABLE}").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_movement_lines").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations").fetchone()[0]),
        )


def _json_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object], dict[str, str]]:
    request = urllib_request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", **(headers or {})},
    )
    try:
        response = urllib_request.urlopen(request, timeout=20)
        raw = response.read()
        return response.status, json.loads(raw.decode("utf-8")) if raw else {}, dict(response.headers)
    except urllib_error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw.decode("utf-8")) if raw else {}, dict(exc.headers)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


if __name__ == "__main__":
    main()
