"""Targeted smoke-check for server-owned nomenclature WB barcodes."""

from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, request as urllib_request

from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.official_api_runtime import OfficialApiRuntimeError  # noqa: E402
from packages.adapters.wb_content import HttpBackedWbContentSource, WbContentHttpStatusError  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_NOMENCLATURE_BARCODE_SYNC_PATH,
    DEFAULT_NOMENCLATURE_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.supplier_shipments import SupplierShipmentsBlock  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


class FakeBarcodeSource:
    def __init__(self, mapping: dict[int, list[str]] | None = None, exc: Exception | None = None) -> None:
        self.mapping = mapping or {}
        self.exc = exc
        self.calls: list[list[int]] = []

    def fetch_barcodes_by_nm_ids(self, nm_ids: list[int]):
        self.calls.append(list(nm_ids))
        if self.exc is not None:
            raise self.exc
        return {
            int(nm_id): {
                "nm_id": int(nm_id),
                "barcodes": list(self.mapping.get(int(nm_id), [])),
                "cards_found": 1 if self.mapping.get(int(nm_id)) else 0,
                "pages_fetched": 1,
                "endpoint": "/content/v2/get/cards/list",
            }
            for nm_id in nm_ids
        }


class FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class RecordingOpener:
    def __init__(self) -> None:
        self.requests = []
        self.next_payload = b"{}"
        self.next_error = None

    def __call__(self, req, timeout):
        self.requests.append((req, timeout))
        if self.next_error is not None:
            raise self.next_error
        return FakeResponse(self.next_payload)


def main() -> None:
    _adapter_smoke()
    _application_runtime_xlsx_smoke()
    _http_route_smoke()
    print("nomenclature_barcode_smoke: OK")


def _adapter_smoke() -> None:
    with _patched_env({"WB_API_TOKEN": "content-smoke-token", "WB_CONTENT_API_BASE_URL": "https://content-api.example.test"}):
        opener = RecordingOpener()
        opener.next_payload = json.dumps(
            {
                "cards": [
                    {
                        "nmID": 501001,
                        "sizes": [
                            {"skus": ["2000000000001", "2000000000002"]},
                            {"skus": ["2000000000001"]},
                        ],
                    }
                ],
                "cursor": {"total": 1},
            }
        ).encode("utf-8")
        source = HttpBackedWbContentSource(opener=opener)
        result = source.fetch_barcodes_by_nm_ids([501001])
        resolution = result[501001]
        if resolution.barcodes != ["2000000000001", "2000000000002"]:
            raise AssertionError(f"content adapter must extract size skus, got {resolution}")
        req, timeout = opener.requests[-1]
        if req.get_method() != "POST" or req.full_url != "https://content-api.example.test/content/v2/get/cards/list":
            raise AssertionError(f"content adapter endpoint changed: {req.get_method()} {req.full_url}")
        if req.get_header("Authorization") != "content-smoke-token":
            raise AssertionError("content adapter must use canonical WB_API_TOKEN")
        body = json.loads(req.data.decode("utf-8"))
        if body["settings"]["filter"]["textSearch"] != "501001" or body["settings"]["cursor"]["limit"] != 100:
            raise AssertionError(f"content adapter body changed unexpectedly: {body}")
        if timeout <= 0:
            raise AssertionError("content adapter must pass timeout")

        opener.next_error = urllib_error.HTTPError(
            "https://content-api.example.test/content/v2/get/cards/list",
            401,
            "Unauthorized",
            hdrs={"Content-Type": "application/json"},
            fp=BytesIO(b'{"error":"bad token"}'),
        )
        try:
            source.fetch_barcodes_by_nm_ids([501001])
        except WbContentHttpStatusError as exc:
            if exc.status_code != 401:
                raise AssertionError(f"content adapter must preserve status, got {exc.status_code}")
            if "content-smoke-token" in str(exc):
                raise AssertionError("content adapter error must not print token")
        else:
            raise AssertionError("content adapter HTTP errors must raise")


def _application_runtime_xlsx_smoke() -> None:
    with TemporaryDirectory(prefix="nomenclature-barcode-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        _seed_old_schema_row(runtime_dir)
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        old_rows = runtime.list_nomenclature_items()
        old_row = next(item for item in old_rows if item["item_id"] == "old_nom")
        if old_row.get("barcode") != "" or old_row.get("barcode_status") != "missing":
            raise AssertionError(f"old schema row must survive barcode migration as missing, got {old_row}")

        source = FakeBarcodeSource({501001: ["3000000000002", "3000000000001"]})
        block = SupplierShipmentsBlock(runtime=runtime, barcode_source=source, timestamp_factory=_clock())
        manual = block.create_nomenclature_item(
            {
                "is_active": True,
                "nm_id": 501000,
                "barcode": " 1000000000001 ",
                "nomenclature_name": "Manual Clear",
                "product_type": "clear",
                "match_key": "clear|iphone_14",
                "purchase_price_yuan": "1.2",
            }
        )["item"]
        if manual["barcode"] != "1000000000001" or manual["barcode_source"] != "manual" or manual["barcode_status"] != "manual":
            raise AssertionError(f"manual barcode must be authoritative, got {manual}")
        block.sync_nomenclature_item_barcode(manual["item_id"])
        preserved = runtime.load_nomenclature_item(manual["item_id"])
        if preserved["barcode"] != "1000000000001" or source.calls:
            raise AssertionError("row sync must not overwrite/call WB for manual barcode")

        synced = block.create_nomenclature_item(
            {
                "is_active": True,
                "nm_id": 501001,
                "nomenclature_name": "WB Clear",
                "product_type": "clear",
                "match_key": "clear|iphone_15",
                "purchase_price_yuan": "1.5",
            }
        )["item"]
        if (
            synced["barcode"] != "3000000000001"
            or synced["barcodes"] != ["3000000000001", "3000000000002"]
            or synced["barcode_source"] != "wb_content"
            or synced["barcode_status"] != "multiple"
        ):
            raise AssertionError(f"WB sync must pick deterministic primary and keep all barcodes, got {synced}")

        patched = block.update_nomenclature_item(synced["item_id"], {"barcode": "4000000000001"})["item"]
        if patched["barcode_source"] != "manual" or patched["barcode_status"] != "manual":
            raise AssertionError(f"patching barcode must become manual override, got {patched}")

        missing_source = FakeBarcodeSource(exc=OfficialApiRuntimeError("required env WB_API_TOKEN is not set"))
        token_block = SupplierShipmentsBlock(runtime=runtime, barcode_source=missing_source, timestamp_factory=_clock(start=30))
        token_item = token_block.create_nomenclature_item(
            {
                "is_active": True,
                "nm_id": 501002,
                "nomenclature_name": "Token Missing",
                "product_type": "clear",
                "match_key": "clear|iphone_16",
                "purchase_price_yuan": "1.0",
            }
        )["item"]
        if token_item["barcode_status"] != "token_missing" or token_item["barcode"] != "":
            raise AssertionError(f"token missing must be diagnostic, not save blocker, got {token_item}")

        listed = block.list_nomenclature()
        summary = listed.get("summary") or {}
        if summary.get("active_rows_with_barcode", 0) < 2 or summary.get("manual_barcode_count", 0) < 2:
            raise AssertionError(f"list response must expose barcode readiness summary, got {summary}")

        workbook_bytes, _, _ = block.export_nomenclature_xlsx()
        workbook = load_workbook(BytesIO(workbook_bytes), data_only=True)
        headers = [cell.value for cell in next(workbook.active.iter_rows(min_row=1, max_row=1))]
        if "ШК / barcode" not in headers or "Все ШК" not in headers:
            raise AssertionError(f"export must include barcode columns, got {headers}")

        imported = block.import_nomenclature_xlsx(_build_import_workbook(), uploaded_filename="nomenclature.xlsx")
        if imported["created_count"] != 1:
            raise AssertionError(f"barcode import must create one row, got {imported}")
        imported_row = next(item for item in runtime.list_nomenclature_items() if item["match_key"] == "matte|iphone_17")
        if imported_row["barcode"] != "5000000000001" or imported_row["barcode_source"] != "manual":
            raise AssertionError(f"imported barcode must be manual, got {imported_row}")

        old_import = block.import_nomenclature_xlsx(_build_old_import_workbook(), uploaded_filename="old.xlsx")
        if old_import["created_count"] != 1:
            raise AssertionError(f"old import workbook without barcode must remain valid, got {old_import}")


def _http_route_smoke() -> None:
    with TemporaryDirectory(prefix="nomenclature-barcode-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
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
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            activated_at_factory=lambda: "2026-06-27T10:00:00Z",
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        original_token = os.environ.get("WB_API_TOKEN")
        os.environ.pop("WB_API_TOKEN", None)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            manual_status, manual_payload = _post_json(
                f"{base_url}{DEFAULT_NOMENCLATURE_PATH}",
                {
                    "is_active": True,
                    "nm_id": 601001,
                    "barcode": "6000000000001",
                    "nomenclature_name": "HTTP Manual",
                    "product_type": "clear",
                    "match_key": "clear|http_manual",
                    "purchase_price_yuan": "1",
                },
            )
            if manual_status != 200 or manual_payload.get("item", {}).get("barcode_source") != "manual":
                raise AssertionError(f"HTTP manual barcode create failed: {manual_status} {manual_payload}")
            auto_status, auto_payload = _post_json(
                f"{base_url}{DEFAULT_NOMENCLATURE_PATH}",
                {
                    "is_active": True,
                    "nm_id": 601002,
                    "nomenclature_name": "HTTP Token Missing",
                    "product_type": "clear",
                    "match_key": "clear|http_token_missing",
                    "purchase_price_yuan": "1",
                },
            )
            if auto_status != 200 or auto_payload.get("item", {}).get("barcode_status") != "token_missing":
                raise AssertionError(f"HTTP auto-sync token warning must not block save: {auto_status} {auto_payload}")
            sync_status, sync_payload = _post_json(
                f"{base_url}{DEFAULT_NOMENCLATURE_BARCODE_SYNC_PATH}",
                {"active_only": True, "only_missing": True, "limit": 10},
            )
            if sync_status != 200 or sync_payload.get("token_missing", 0) < 1:
                raise AssertionError(f"HTTP batch barcode sync must expose token_missing count: {sync_status} {sync_payload}")
            list_status, list_payload = _get_json(f"{base_url}{DEFAULT_NOMENCLATURE_PATH}")
            summary = list_payload.get("summary") or {}
            if list_status != 200 or summary.get("active_rows_missing_barcode", 0) < 1:
                raise AssertionError(f"HTTP list must expose barcode summary: {list_status} {list_payload}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if original_token is None:
                os.environ.pop("WB_API_TOKEN", None)
            else:
                os.environ["WB_API_TOKEN"] = original_token


def _seed_old_schema_row(runtime_dir: Path) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    db_path = runtime_dir / "registry_upload_runtime.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sheet_vitrina_v1_nomenclature_items (
                item_id TEXT PRIMARY KEY,
                is_active INTEGER NOT NULL,
                our_sku TEXT,
                nm_id INTEGER,
                nomenclature_name TEXT NOT NULL,
                product_type TEXT NOT NULL,
                match_key TEXT NOT NULL,
                purchase_price_yuan REAL,
                aliases_json TEXT NOT NULL,
                compatible_models_text TEXT NOT NULL DEFAULT '',
                compatible_model_keys_json TEXT NOT NULL DEFAULT '[]',
                comment TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_nomenclature_items(
                item_id, is_active, our_sku, nm_id, nomenclature_name, product_type, match_key,
                purchase_price_yuan, aliases_json, compatible_models_text, compatible_model_keys_json,
                comment, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old_nom",
                1,
                "",
                500999,
                "Old Clear",
                "clear",
                "clear|iphone_13",
                1.0,
                "[]",
                "",
                "[]",
                "",
                "2026-06-01T00:00:00Z",
                "2026-06-01T00:00:00Z",
            ),
        )
        conn.commit()


def _build_import_workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Включено", "nmId", "ШК / barcode", "Номенклатура", "Тип", "Match key", "Цена закупки, ¥"])
    sheet.append(["да", 501017, "5000000000001", "Matte iPhone 17", "Матовое", "matte|iphone_17", 2])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _build_old_import_workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Включено", "nmId", "Номенклатура", "Тип", "Match key", "Цена закупки, ¥"])
    sheet.append(["да", 501018, "Clear iPhone 18", "Прозрачное", "clear|iphone_18", 2])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _clock(start: int = 0):
    state = {"value": start}

    def tick() -> str:
        state["value"] += 1
        return f"2026-06-27T09:{state['value']:02d}:00Z"

    return tick


def _get_json(url: str) -> tuple[int, dict]:
    req = urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return int(exc.code), json.loads(exc.read().decode("utf-8"))


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return int(exc.code), json.loads(exc.read().decode("utf-8"))


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _patched_env(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    main()
