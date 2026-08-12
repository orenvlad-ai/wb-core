"""HTTP boundary checks for Stage 4 FBW supply FF-origin assignment."""

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
    DEFAULT_FF_POOL_WB_SUPPLY_ORIGINS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
)
from packages.application.ff_pool_surfaces import MAX_JSON_REQUEST_BYTES  # noqa: E402
from packages.application.ff_wb_supply_origins import ASSIGNMENTS_TABLE  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)
from packages.contracts.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypointConfig,
)


def main() -> None:
    with TemporaryDirectory(prefix="ff-wb-origin-http-") as directory:
        runtime_dir = Path(directory) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.list_wb_supplies()
        _seed_supply(runtime)
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
            _default_off_and_csrf(base)
            _enable_writer(runtime.db_path)
            assignment_id = _assignment_contract(base)
            _conditional_get(base)
            _prebuffer_limit(config.port)
            with sqlite3.connect(runtime.db_path) as conn:
                assert conn.execute(f"SELECT COUNT(*) FROM {ASSIGNMENTS_TABLE}").fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_movement_lines").fetchone()[0] == 0
                assert conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations").fetchone()[0] == 0
                assert conn.execute(
                    f"SELECT assignment_id FROM {ASSIGNMENTS_TABLE}"
                ).fetchone()[0] == assignment_id
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("ff_wb_supply_origins_http_smoke: OK")


def _seed_supply(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_wb_supply_rows(
        rows=[
            {
                "supply_id": "supply:42000001",
                "cache_key": "supply:42000001",
                "wb_supply_id": "42000001",
                "status_id": 3,
                "raw_list_hash": "sha256:http-list",
                "raw_goods_hash": "sha256:http-goods",
                "number_label": "42000001",
                "type_label": "Поставка",
            }
        ],
        warehouses=[],
        synced_at="2026-08-12T08:00:00Z",
    )


def _default_off_and_csrf(base: str) -> None:
    detail_url = f"{base}{DEFAULT_FF_POOL_WB_SUPPLY_ORIGINS_PATH}/42000001"
    code, detail, _headers = _json_request(detail_url)
    assert code == 200 and detail["contract_name"] == "ff_wb_supply_origin_assignments_v1"
    assert detail["reason"] == "facility_pool_feature_off"
    body = {
        "request_id": "stage4:http:origin",
        "facility_id": "facility-http",
        "expected_assignment_id": "",
    }
    missing_code, missing, _ = _json_request(detail_url, method="POST", payload=body)
    assert missing_code == 403 and missing["code"] == "csrf_failed"
    cross_code, cross, _ = _json_request(
        detail_url,
        method="POST",
        payload=body,
        headers={
            "X-WB-FF-Pool-CSRF": "1",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert cross_code == 403 and cross["code"] == "csrf_failed"
    off_code, off, _ = _json_request(
        detail_url,
        method="POST",
        payload=body,
        headers={"X-WB-FF-Pool-CSRF": "1", "Sec-Fetch-Site": "same-origin"},
    )
    assert off_code == 409 and off["code"] == "facility_pool_feature_off"


def _enable_writer(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"""INSERT INTO {FACILITIES_TABLE}(
                   facility_id,code,name,active,display_timezone,created_at,updated_at
               ) VALUES('facility-http','FF-HTTP','HTTP FF',1,'Asia/Yekaterinburg',
                        '2026-08-12T08:01:00Z','2026-08-12T08:01:00Z')"""
        )
        conn.execute(
            f"""INSERT INTO {FEATURE_EPOCHS_TABLE}(
                   epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json
               ) VALUES(1,1,0,'stage4-http-writer','2026-08-12T08:01:00Z','{{}}')"""
        )
        conn.commit()


def _assignment_contract(base: str) -> str:
    url = f"{base}{DEFAULT_FF_POOL_WB_SUPPLY_ORIGINS_PATH}/42000001"
    body = {
        "request_id": "stage4:http:origin",
        "facility_id": "facility-http",
        "expected_assignment_id": "",
        "reason": "HTTP fixture",
    }
    headers = {"X-WB-FF-Pool-CSRF": "1", "Sec-Fetch-Site": "same-origin"}
    code, payload, _ = _json_request(url, method="POST", payload=body, headers=headers)
    assert code == 200 and not payload["idempotent"]
    assert payload["assignment"]["pool"] == "FBO" and not payload["creates_pool_movement"]
    repeated_code, repeated, _ = _json_request(url, method="POST", payload=body, headers=headers)
    assert repeated_code == 200 and repeated["idempotent"]
    stale_code, stale, _ = _json_request(
        url,
        method="POST",
        payload={
            "request_id": "stage4:http:stale",
            "facility_id": "facility-http",
            "expected_assignment_id": "",
        },
        headers=headers,
    )
    assert stale_code == 409 and stale["code"] == "stale_origin_assignment"
    return str(payload["assignment"]["assignment_id"])


def _conditional_get(base: str) -> None:
    root = f"{base}{DEFAULT_FF_POOL_WB_SUPPLY_ORIGINS_PATH}"
    code, payload, headers = _json_request(root)
    assert code == 200 and payload["page"]["total"] == 1
    etag = next((value for key, value in headers.items() if key.lower() == "etag"), "")
    assert etag.startswith('"sha256:')
    not_modified, _payload, response_headers = _json_request(
        root, headers={"If-None-Match": etag}
    )
    response_etag = next(
        (value for key, value in response_headers.items() if key.lower() == "etag"), ""
    )
    assert not_modified == 304 and response_etag == etag


def _prebuffer_limit(port: int) -> None:
    path = f"{DEFAULT_FF_POOL_WB_SUPPLY_ORIGINS_PATH}/42000001"
    request_head = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Accept: application/json\r\n"
        "Content-Type: application/json\r\n"
        "X-WB-FF-Pool-CSRF: 1\r\n"
        "Sec-Fetch-Site: same-origin\r\n"
        f"Content-Length: {MAX_JSON_REQUEST_BYTES + 1}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(request_head)
        response = b""
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            response += chunk
    head, body = response.split(b"\r\n\r\n", 1)
    assert b" 413 " in head and json.loads(body.decode("utf-8"))["code"] == "request_too_large"


def _json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object], dict[str, str]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib_request.Request(url, data=body, method=method, headers=request_headers)
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
