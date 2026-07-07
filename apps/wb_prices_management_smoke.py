"""Smoke-check WB prices management block without live WB mutations."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from typing import Any, Mapping, Sequence
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_PRICES_GOODS_PATH,
    DEFAULT_SHEET_PRICES_PREVIEW_PATH,
    DEFAULT_SHEET_PRICES_QUARANTINE_PATH,
    DEFAULT_SHEET_PRICES_UPLOAD_TASK_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.wb_prices_management import (  # noqa: E402
    WbPricesManagementBlock,
    WbPricesManagementError,
    WbPricesSafetyConfig,
    map_upload_status,
    normalize_goods_payload,
    normalize_upload_good,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402

BUNDLE_FIXTURE = ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
NOW = datetime(2026, 7, 7, 7, 0, tzinfo=timezone.utc)
PRIMARY_NM = 210183919
SIZE_PRICE_NM = 210184534


class FakePricesSource:
    def __init__(self) -> None:
        self.upload_payloads: list[list[dict[str, Any]]] = []
        self.status_code = 5

    def fetch_goods(self, *, limit: int, offset: int, filter_nm_id: int | None = None) -> Mapping[str, Any]:
        goods = _goods_payload()["data"]["listGoods"]
        if filter_nm_id is not None:
            goods = [item for item in goods if int(item["nmID"]) == int(filter_nm_id)]
        return {"data": {"listGoods": goods[offset : offset + limit]}, "error": False, "errorText": ""}

    def fetch_goods_by_nm_ids(self, nm_ids: Sequence[int]) -> Mapping[str, Any]:
        wanted = {int(value) for value in nm_ids}
        goods = [item for item in _goods_payload()["data"]["listGoods"] if int(item["nmID"]) in wanted]
        return {"data": {"listGoods": goods}, "error": False, "errorText": ""}

    def upload_task(self, goods: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        copied = [dict(item) for item in goods]
        self.upload_payloads.append(copied)
        return {"data": {"id": 987654321, "alreadyExists": False}, "error": False, "errorText": "", "_http_status": 200}

    def fetch_upload_status(self, upload_id: int) -> Mapping[str, Any]:
        return {
            "data": {
                "uploadID": int(upload_id),
                "status": self.status_code,
                "uploadDate": "2026-07-07T07:00:00+00:00",
                "activationDate": "2026-07-07T07:00:00+00:00",
                "overAllGoodsNumber": 2,
                "successGoodsNumber": 1,
            },
            "error": False,
            "errorText": "",
        }

    def fetch_upload_goods(self, *, upload_id: int, limit: int, offset: int) -> Mapping[str, Any]:
        rows = [
            {
                "nmID": PRIMARY_NM,
                "vendorCode": "VC-PRIMARY",
                "sizeID": 11,
                "techSizeName": "0",
                "price": 900,
                "currencyIsoCode4217": "RUB",
                "discount": 10,
                "clubDiscount": 5,
                "status": 1,
                "errorText": "",
            },
            {
                "nmID": SIZE_PRICE_NM,
                "vendorCode": "VC-SIZE",
                "sizeID": 22,
                "techSizeName": "42",
                "price": 1200,
                "currencyIsoCode4217": "RUB",
                "discount": 20,
                "clubDiscount": 5,
                "status": 1,
                "errorText": "price is blocked by size-based pricing",
            },
        ]
        return {"data": {"uploadID": int(upload_id), "historyGoods": rows[offset : offset + limit]}, "error": False, "errorText": ""}

    def fetch_quarantine_goods(self, *, limit: int, offset: int) -> Mapping[str, Any]:
        rows = [
            {
                "nmID": PRIMARY_NM,
                "sizeID": None,
                "techSizeName": "",
                "currencyIsoCode4217": "RUB",
                "newPrice": 100,
                "oldPrice": 1000,
                "newDiscount": 50,
                "oldDiscount": 10,
                "priceDiff": -850,
            }
        ]
        return {"data": {"quarantineGoods": rows[offset : offset + limit]}, "error": False, "errorText": ""}


def main() -> None:
    _run_unit_checks()
    _run_http_smoke(write_enabled=False)
    _run_http_smoke(write_enabled=True)
    print("wb_prices_management_smoke: OK")


def _run_unit_checks() -> None:
    goods = normalize_goods_payload(_goods_payload())
    if len(goods) != 2:
        raise AssertionError("normalization must parse two goods")
    primary = goods[0].to_dict()
    if primary["price"] != 1000 or primary["discountedPrice"] != 900 or primary["clubDiscountedPrice"] != 855:
        raise AssertionError(f"normalization must use min size prices, got {primary}")
    if not goods[1].editable_size_price or goods[1].is_bad_turnover is not True:
        raise AssertionError("normalization must preserve editableSizePrice/isBadTurnover")
    if map_upload_status(3) != "success" or map_upload_status(5) != "partial_error" or map_upload_status(999) != "unknown":
        raise AssertionError("upload status mapping mismatch")
    normalized_error = normalize_upload_good({"nmID": 1, "status": 1, "errorText": "bad row"})
    if normalized_error["status"] != "processing" or normalized_error["errorText"] != "bad row":
        raise AssertionError(f"upload row normalization mismatch: {normalized_error}")
    with TemporaryDirectory(prefix="wb-prices-unit-") as tmp:
        runtime = _seed_runtime(Path(tmp) / "runtime")
        block = _build_block(runtime, Path(tmp) / "runtime", FakePricesSource(), write_enabled=False)
        preview = block.preview_changes({"changes": [{"nmID": PRIMARY_NM, "price": 200, "discount": 10}]})
        row = preview["preview"]["rows"][0]
        if row["new"]["discountedPrice"] != 180 or "quarantine_risk" not in ",".join(row["warnings"]):
            raise AssertionError(f"preview quarantine risk mismatch: {row}")
        size_preview = block.preview_changes({"changes": [{"nmID": SIZE_PRICE_NM, "price": 1000}]})
        if size_preview["preview"]["summary"]["valid"] != 0 or "size-based" not in size_preview["preview"]["rows"][0]["errors"][0]:
            raise AssertionError(f"editable size price must block ordinary price edit: {size_preview}")
        try:
            block.upload_task(preview["confirmation_payload"], actor="smoke")
        except WbPricesManagementError as exc:
            if exc.http_status != 403:
                raise
        else:
            raise AssertionError("write-disabled upload must be guarded")


def _run_http_smoke(*, write_enabled: bool) -> None:
    with TemporaryDirectory(prefix="wb-prices-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        source = FakePricesSource()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            now_factory=lambda: NOW,
            activated_at_factory=lambda: "2026-07-07T07:00:00Z",
            prices_block=_build_block(runtime, runtime_dir, source, write_enabled=write_enabled),
        )
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
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            ui_status, ui_html = _get_text(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}")
            if ui_status != 200:
                raise AssertionError(f"operator shell route must return 200, got {ui_status}")
            for expected in (
                'data-unified-tab-button="prices"',
                'data-unified-tab-panel="prices"',
                'data-prices-body',
                'data-prices-modal',
                f'"prices_goods_path": "{DEFAULT_SHEET_PRICES_GOODS_PATH}"',
                f'"prices_preview_path": "{DEFAULT_SHEET_PRICES_PREVIEW_PATH}"',
                f'"prices_upload_task_path": "{DEFAULT_SHEET_PRICES_UPLOAD_TASK_PATH}"',
                f'"prices_quarantine_path": "{DEFAULT_SHEET_PRICES_QUARANTINE_PATH}"',
            ):
                if expected not in ui_html:
                    raise AssertionError(f"prices UI must contain {expected!r}")
            if "discounts-prices-api.wildberries.ru" in _prices_script_slice(ui_html):
                raise AssertionError("frontend prices code must not call WB upstream directly")

            status, goods = _get_json(f"{base_url}{DEFAULT_SHEET_PRICES_GOODS_PATH}")
            if status != 200 or goods.get("contract_name") != "sheet_vitrina_v1_prices_goods":
                raise AssertionError(f"goods route mismatch: {status} {goods}")
            status, preview = _post_json(
                f"{base_url}{DEFAULT_SHEET_PRICES_PREVIEW_PATH}",
                {"changes": [{"nmID": PRIMARY_NM, "price": 900, "discount": 10}]},
            )
            if status != 200 or preview.get("contract_name") != "sheet_vitrina_v1_prices_preview":
                raise AssertionError(f"preview route mismatch: {status} {preview}")
            status, commit = _post_json(
                f"{base_url}{DEFAULT_SHEET_PRICES_UPLOAD_TASK_PATH}",
                preview["confirmation_payload"],
            )
            if write_enabled:
                if status != 200 or commit.get("uploadID") != 987654321:
                    raise AssertionError(f"enabled commit route mismatch: {status} {commit}")
                if source.upload_payloads != [[{"nmID": PRIMARY_NM, "price": 900, "discount": 10}]]:
                    raise AssertionError(f"upload payload shape mismatch: {source.upload_payloads}")
            else:
                if status != 403 or "disabled" not in str(commit.get("error")):
                    raise AssertionError(f"disabled commit guard mismatch: {status} {commit}")
            status, task = _get_json(f"{base_url}{DEFAULT_SHEET_PRICES_UPLOAD_TASK_PATH}/987654321")
            if status != 200 or task.get("status") != "partial_error" or not task.get("goods_errors"):
                raise AssertionError(f"status route mismatch: {status} {task}")
            status, details = _get_json(f"{base_url}{DEFAULT_SHEET_PRICES_UPLOAD_TASK_PATH}/987654321/goods")
            if status != 200 or details.get("rows", [])[1].get("errorText") != "price is blocked by size-based pricing":
                raise AssertionError(f"details route mismatch: {status} {details}")
            status, quarantine = _get_json(f"{base_url}{DEFAULT_SHEET_PRICES_QUARANTINE_PATH}")
            if status != 200 or quarantine.get("rows", [])[0].get("nmID") != PRIMARY_NM:
                raise AssertionError(f"quarantine route mismatch: {status} {quarantine}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def _build_block(
    runtime: RegistryUploadDbBackedRuntime,
    runtime_dir: Path,
    source: FakePricesSource,
    *,
    write_enabled: bool,
) -> WbPricesManagementBlock:
    return WbPricesManagementBlock(
        runtime=runtime,
        runtime_dir=runtime_dir,
        source=source,
        now_factory=lambda: NOW,
        timestamp_factory=lambda: "2026-07-07T07:00:00Z",
        safety_config=WbPricesSafetyConfig(write_enabled=write_enabled, preview_ttl_seconds=300),
    )


def _seed_runtime(runtime_dir: Path) -> RegistryUploadDbBackedRuntime:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    result = runtime.ingest_bundle(bundle, activated_at="2026-07-07T07:00:00Z")
    if result.status != "accepted":
        raise AssertionError(f"bundle fixture must be accepted, got {result}")
    for nm_id, name, vendor in (
        (PRIMARY_NM, "Primary prices SKU", "VC-PRIMARY"),
        (SIZE_PRICE_NM, "Size price SKU", "VC-SIZE"),
    ):
        runtime.save_nomenclature_item(
            {
                "item_id": f"prices-{nm_id}",
                "is_active": True,
                "our_sku": f"OUR-{nm_id}",
                "nm_id": nm_id,
                "barcode": f"4600000{nm_id}",
                "barcodes": [f"4600000{nm_id}"],
                "barcode_source": "manual",
                "barcode_status": "ready",
                "vendor_code": vendor,
                "wb_title": name,
                "wb_subject_name": "Smoke",
                "wb_sync_status": "ready",
                "nomenclature_name": name,
                "product_type": "smoke",
                "match_key": f"prices-{nm_id}",
                "aliases": [],
                "compatible_models_text": "",
                "compatible_model_keys": [],
                "comment": "",
                "created_at": "2026-07-07T07:00:00Z",
                "updated_at": "2026-07-07T07:00:00Z",
            }
        )
    return runtime


def _goods_payload() -> dict[str, Any]:
    return {
        "data": {
            "listGoods": [
                {
                    "nmID": PRIMARY_NM,
                    "vendorCode": "VC-PRIMARY",
                    "sizes": [
                        {
                            "sizeID": 11,
                            "price": 1000,
                            "discountedPrice": 900,
                            "clubDiscountedPrice": 855,
                            "techSizeName": "0",
                        },
                        {
                            "sizeID": 12,
                            "price": 1200,
                            "discountedPrice": 1080,
                            "clubDiscountedPrice": 1026,
                            "techSizeName": "1",
                        },
                    ],
                    "currencyIsoCode4217": "RUB",
                    "discount": 10,
                    "clubDiscount": 5,
                    "editableSizePrice": False,
                    "wholesaleDiscountThreshold": [{"minQuantity": 10, "wholesaleDiscount": 3, "level": 1}],
                    "isBadTurnover": False,
                },
                {
                    "nmID": SIZE_PRICE_NM,
                    "vendorCode": "VC-SIZE",
                    "sizes": [
                        {
                            "sizeID": 22,
                            "price": 1500,
                            "discountedPrice": 1200,
                            "clubDiscountedPrice": 1140,
                            "techSizeName": "42",
                        }
                    ],
                    "currencyIsoCode4217": "RUB",
                    "discount": 20,
                    "clubDiscount": 5,
                    "editableSizePrice": True,
                    "wholesaleDiscountThreshold": [],
                    "isBadTurnover": True,
                },
            ]
        },
        "error": False,
        "errorText": "",
    }


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str) -> tuple[int, dict[str, Any]]:
    status, text = _get_text(url)
    return status, json.loads(text)


def _get_text(url: str) -> tuple[int, str]:
    try:
        with request.urlopen(url, timeout=10) as response:
            return int(response.status), response.read().decode("utf-8")
    except error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8")


def _post_json(url: str, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return int(exc.code), json.loads(exc.read().decode("utf-8"))


def _prices_script_slice(html: str) -> str:
    marker = "function ensurePricesLoaded"
    index = html.find(marker)
    return html[index : index + 9000] if index >= 0 else html


if __name__ == "__main__":
    main()
