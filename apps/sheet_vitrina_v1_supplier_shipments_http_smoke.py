"""HTTP smoke-check for supplier invoice shipment parse/storage/API routes."""

from __future__ import annotations

from io import BytesIO
import hashlib
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, request as urllib_request
from uuid import uuid4

from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_NOMENCLATURE_EXPORT_PATH,
    DEFAULT_NOMENCLATURE_IMPORT_PATH,
    DEFAULT_NOMENCLATURE_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.supplier_shipments import SupplierShipmentsBlock  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def _assert_price_conformity_application_smoke() -> None:
    timestamp_counter = {"value": 0}

    def next_timestamp() -> str:
        timestamp_counter["value"] += 1
        return f"2026-05-30T09:{timestamp_counter['value']:02d}:00Z"

    with TemporaryDirectory(prefix="supplier-price-conformity-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        block = SupplierShipmentsBlock(runtime=runtime, timestamp_factory=next_timestamp)
        first = block.create_nomenclature_item(
            {
                "is_active": True,
                "nm_id": 501001,
                "nomenclature_name": "Clear iPhone 14 Pro",
                "product_type": "clear",
                "match_key": "clear|iphone_14_pro",
                "purchase_price_yuan": "1",
            }
        )["item"]
        block.create_nomenclature_item(
            {
                "is_active": True,
                "nm_id": 501002,
                "nomenclature_name": "Anti-Spy iPhone 14",
                "product_type": "anti_spy",
                "match_key": "anti_spy|iphone_14",
                "purchase_price_yuan": "3",
            }
        )
        block.create_nomenclature_item(
            {
                "is_active": True,
                "nm_id": 501003,
                "nomenclature_name": "Clear iPhone 15",
                "product_type": "clear",
                "match_key": "clear|iphone_15",
                "purchase_price_yuan": None,
            }
        )
        block.create_nomenclature_item(
            {
                "is_active": True,
                "nm_id": 501004,
                "nomenclature_name": "Matte iPhone 16",
                "product_type": "matte",
                "match_key": "matte|iphone_16",
                "purchase_price_yuan": "5",
            }
        )

        parsed = block.parse_upload(_build_price_conformity_invoice_fixture(), uploaded_filename="price-check.xlsx")
        statuses = [line.get("price_conformity_status") for line in parsed.get("lines", []) if line.get("line_type") == "product"]
        expected = ["matched", "mismatched", "sku_not_found", "reference_price_missing", "invoice_price_missing"]
        if statuses != expected:
            raise AssertionError(f"price conformity parse statuses changed: {statuses}")
        detail = block.create_shipment(
            {
                "upload_id": parsed["upload_id"],
                "shipment_date": "2026-05-30",
                "payload": parsed,
            }
        )
        shipment_id = detail["shipment_id"]
        if detail["product_lines"][0].get("price_conformity_check_mode") != "initial_parse":
            raise AssertionError("initial create must persist initial_parse price check mode")
        block.update_nomenclature_item(str(first["item_id"]), {**first, "purchase_price_yuan": "1.25"})
        ordinary_open = block.get_shipment(shipment_id)
        if (
            ordinary_open["product_lines"][0].get("price_conformity_status") != "matched"
            or ordinary_open["product_lines"][0].get("reference_purchase_price_yuan_snapshot") != 1.0
        ):
            raise AssertionError("ordinary detail open must not auto-recalculate price conformity")
        rechecked = block.recheck_shipment_prices(
            shipment_id,
            actor="operator:smoke",
            context={"source": "application_smoke"},
        )
        first_line = rechecked["product_lines"][0]
        if (
            first_line.get("price_conformity_status") != "mismatched"
            or first_line.get("reference_purchase_price_yuan_snapshot") != 1.25
            or first_line.get("price_conformity_check_mode") != "manual_recheck"
            or first_line.get("price_conformity_actor") != "operator:smoke"
            or first_line.get("price_conformity_context", {}).get("source") != "application_smoke"
        ):
            raise AssertionError(f"manual recheck must update persisted price metadata, got {first_line}")

        runtime.save_supplier_shipment(
            header={
                "shipment_id": "sup_legacy_price_check",
                "created_at": "2026-05-30T08:10:00Z",
                "updated_at": "2026-05-30T08:10:00Z",
                "shipment_date": "2026-05-16",
                "invoice_no": "LEGACY-PRICE",
                "invoice_date": "2026-05-15",
                "contract_no": "",
                "contract_date": "",
                "supplier_name": "",
                "customer_name": "",
                "currency": "RMB",
                "product_qty_total": 1,
                "product_amount_total": 1,
                "extras_amount_total": 0,
                "invoice_amount_total": 1,
                "declared_invoice_total": 1,
                "match_status": "all_matched",
                "source_filename": "legacy.xlsx",
                "source_file_sha256": "",
                "source_file_path": "",
                "parser_version": "legacy",
                "warnings": [],
                "errors": [],
            },
            lines=[
                {
                    "line_id": "ln_legacy_price",
                    "line_type": "product",
                    "sort_order": 1,
                    "product_type": "anti_spy",
                    "model_raw": "iPhone 14",
                    "model_normalized": "iphone_14",
                    "match_key": "anti_spy|iphone_14",
                    "internal_nm_id": 501002,
                    "internal_name": "Anti-Spy iPhone 14",
                    "qty": 1,
                    "unit_price": 3,
                    "amount": 3,
                    "currency": "RMB",
                    "match_status": "matched",
                    "manual_override": False,
                    "raw": {"preserve": True},
                }
            ],
        )
        backfill = block.backfill_price_conformity_checks()
        if backfill.get("processed_shipments") != 1 or backfill.get("matched_count") != 1:
            raise AssertionError(f"backfill must fill only missing legacy line, got {backfill}")
        legacy = block.get_shipment("sup_legacy_price_check")
        if (
            legacy["product_lines"][0].get("price_conformity_check_mode") != "migration_backfill"
            or legacy["product_lines"][0].get("raw", {}).get("preserve") is not True
        ):
            raise AssertionError(f"backfill must preserve unrelated line fields, got {legacy['product_lines'][0]}")
        second_backfill = block.backfill_price_conformity_checks()
        if second_backfill.get("processed_shipments") != 0 or second_backfill.get("updated_line_count") != 0:
            raise AssertionError(f"backfill must be idempotent, got {second_backfill}")


def main() -> None:
    _assert_price_conformity_application_smoke()
    workbook_bytes = _build_invoice_fixture()
    workbook_sha256 = hashlib.sha256(workbook_bytes).hexdigest()
    with TemporaryDirectory(prefix="supplier-shipments-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
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
            runtime=runtime,
            activated_at_factory=lambda: "2026-05-30T08:00:00Z",
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            list_status, list_payload = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
            if list_status != 200 or list_payload.get("shipments") != []:
                raise AssertionError(f"empty registry must load, got {list_status} {list_payload}")

            unsupported_status, unsupported_payload = _post_multipart(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH}",
                b"not an invoice",
                filename="invoice.txt",
                content_type="text/plain",
            )
            if unsupported_status != 400 or "xlsx" not in str(unsupported_payload.get("error", "")).lower():
                raise AssertionError(f"unsupported file type must return controlled JSON 400, got {unsupported_status} {unsupported_payload}")

            nomenclature_status, nomenclature_payload = _get_json(f"{base_url}{DEFAULT_NOMENCLATURE_PATH}")
            if nomenclature_status != 200 or nomenclature_payload.get("items") != []:
                raise AssertionError(f"empty nomenclature must load, got {nomenclature_status} {nomenclature_payload}")
            create_nom_status, create_nom_payload = _post_json(
                f"{base_url}{DEFAULT_NOMENCLATURE_PATH}",
                {
                    "is_active": True,
                    "our_sku": "SKU-CLEAR-14P",
                    "nm_id": 210183919,
                    "nomenclature_name": "Clear iPhone 14 Pro",
                    "product_type": "clear",
                    "match_key": "clear|iphone_14_pro",
                    "purchase_price_yuan": "1,0",
                    "aliases": ["iPhone 14 Pro"],
                    "compatible_models_text": "iPhone 14 Pro",
                    "comment": "smoke",
                },
            )
            if create_nom_status != 200 or create_nom_payload.get("item", {}).get("nm_id") != 210183919:
                raise AssertionError(f"nomenclature create must persist item, got {create_nom_status} {create_nom_payload}")
            if create_nom_payload.get("item", {}).get("purchase_price_yuan") != 1.0:
                raise AssertionError("nomenclature create must normalize purchase_price_yuan decimal comma")
            if create_nom_payload.get("item", {}).get("compatible_model_keys") != ["iphone_14_pro"]:
                raise AssertionError("nomenclature create must normalize compatible model keys")
            duplicate_nom_status, duplicate_nom_payload = _post_json(
                f"{base_url}{DEFAULT_NOMENCLATURE_PATH}",
                {
                    "is_active": True,
                    "nomenclature_name": "Duplicate",
                    "product_type": "clear",
                    "match_key": "clear|iphone_14_pro",
                },
            )
            if duplicate_nom_status != 400 or "duplicate" not in str(duplicate_nom_payload.get("error", "")).lower():
                raise AssertionError(f"duplicate active match_key must be rejected, got {duplicate_nom_status} {duplicate_nom_payload}")
            compat_nom_status, compat_nom_payload = _post_json(
                f"{base_url}{DEFAULT_NOMENCLATURE_PATH}",
                {
                    "is_active": True,
                    "our_sku": "SKU-AS-141313P",
                    "nm_id": 391662410,
                    "nomenclature_name": "anti-spy iPhone 14 / 13 / 13Pro",
                    "product_type": "anti_spy",
                    "match_key": "anti_spy|iphone_14_13_13pro",
                    "purchase_price_yuan": "3",
                    "compatible_models_text": "iPhone 14, iPhone 13, iPhone 13 Pro",
                    "comment": "compatibility smoke",
                },
            )
            if compat_nom_status != 200 or compat_nom_payload.get("item", {}).get("compatible_model_keys") != [
                "iphone_14",
                "iphone_13",
                "iphone_13_pro",
            ]:
                raise AssertionError(f"compatible nomenclature item must save normalized keys, got {compat_nom_status} {compat_nom_payload}")
            nomenclature_import_bytes = _build_nomenclature_import_fixture(
                first_item_id=str(create_nom_payload["item"]["item_id"]),
                compat_item_id=str(compat_nom_payload["item"]["item_id"]),
            )
            export_status, export_bytes, export_headers = _get_bytes(f"{base_url}{DEFAULT_NOMENCLATURE_EXPORT_PATH}")
            if export_status != 200 or "spreadsheetml.sheet" not in str(export_headers.get("Content-Type", "")):
                raise AssertionError(f"nomenclature export must return XLSX, got {export_status} {export_headers}")
            exported = load_workbook(BytesIO(export_bytes), data_only=True)
            exported_headers = [cell.value for cell in exported.active[1]]
            expected_headers = [
                "ID строки",
                "Включено",
                "Скрыто",
                "nmId",
                "ШК / barcode",
                "Все ШК",
                "Источник ШК",
                "Статус ШК",
                "Артикул продавца WB / vendorCode",
                "Название WB",
                "WB subject",
                "WB updatedAt",
                "Статус WB sync",
                "Номенклатура",
                "Группа",
                "Match key",
                "Цена закупки, ¥",
                "Совместимые модели",
                "Ключи совместимости",
                "Обновлено",
            ]
            if exported_headers != expected_headers:
                raise AssertionError(f"nomenclature export headers changed unexpectedly: {exported_headers}")
            if {"Наш SKU", "Aliases", "Комментарий"} & set(exported_headers):
                raise AssertionError("nomenclature export must not expose hidden legacy fields by default")
            dry_run_status, dry_run_payload = _post_multipart(
                f"{base_url}{DEFAULT_NOMENCLATURE_IMPORT_PATH}?dry_run=1",
                nomenclature_import_bytes,
                filename="nomenclature.xlsx",
            )
            if (
                dry_run_status != 200
                or dry_run_payload.get("dry_run") is not True
                or dry_run_payload.get("created_count") != 1
                or dry_run_payload.get("updated_count") != 1
                or dry_run_payload.get("deactivated_count") != 1
            ):
                raise AssertionError(f"nomenclature import dry-run must validate counts, got {dry_run_status} {dry_run_payload}")
            after_dry_run_status, after_dry_run_payload = _get_json(f"{base_url}{DEFAULT_NOMENCLATURE_PATH}")
            after_dry_run_items = {item["item_id"]: item for item in after_dry_run_payload.get("items", [])}
            if (
                after_dry_run_status != 200
                or after_dry_run_items[str(create_nom_payload["item"]["item_id"])].get("purchase_price_yuan") != 1.0
                or after_dry_run_items[str(compat_nom_payload["item"]["item_id"])].get("is_active") is not True
            ):
                raise AssertionError("nomenclature import dry-run must not mutate runtime DB")

            parse_status, parse_payload = _post_multipart(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH}",
                workbook_bytes,
                filename="PI-test 26GN390 (14.5.2026).xlsx",
            )
            if parse_status != 200 or not parse_payload.get("upload_id"):
                raise AssertionError(f"parse route must stage upload and return editable payload, got {parse_status} {parse_payload}")
            if parse_payload.get("source_file_sha256") != workbook_sha256:
                raise AssertionError("parse route must expose sha256 of original upload")
            if parse_payload.get("metadata", {}).get("supplier_name") != "HanShang Technology":
                raise AssertionError("parse route must default supplier_name to HanShang Technology")
            if parse_payload.get("metadata", {}).get("customer_name") not in {"", None}:
                raise AssertionError("parse route must not require or persist parsed customer_name")
            if (
                parse_payload.get("metadata", {}).get("contract_no") != "CNT-2026-0513"
                or parse_payload.get("metadata", {}).get("contract_date") != "2026-05-13"
            ):
                raise AssertionError(f"parse route must expose contract no/date, got {parse_payload.get('metadata')}")
            product_lines = [item for item in parse_payload.get("lines", []) if item.get("line_type") == "product"]
            if product_lines[0].get("internal_nm_id") != 210183919 or product_lines[0].get("match_status") != "matched":
                raise AssertionError("parse route must resolve active nomenclature match_key into nmId/name")
            if (
                product_lines[1].get("match_status") != "matched_by_compatibility"
                or product_lines[1].get("internal_nm_id") != 391662410
            ):
                raise AssertionError(f"parse route must resolve compatible model overlap, got {product_lines[1]}")
            if product_lines[2].get("match_status") != "unmatched":
                raise AssertionError("unknown product match_key must remain visible and unmatched")
            price_statuses = [line.get("price_conformity_status") for line in product_lines]
            if price_statuses != ["matched", "mismatched", "sku_not_found"]:
                raise AssertionError(f"parse route must attach price conformity statuses, got {price_statuses}")
            if (
                product_lines[0].get("invoice_price_yuan_snapshot") != 1.0
                or product_lines[0].get("reference_purchase_price_yuan_snapshot") != 1.0
                or product_lines[0].get("price_conformity_check_mode") != "initial_parse"
            ):
                raise AssertionError(f"parse route must expose price snapshots/mode, got {product_lines[0]}")

            missing_date_status, missing_date_payload = _post_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}",
                {"upload_id": parse_payload["upload_id"], "payload": parse_payload},
            )
            if missing_date_status != 400 or "shipment_date" not in str(missing_date_payload.get("error", "")):
                raise AssertionError("create must reject missing shipment_date")

            invalid_actual_status, invalid_actual_payload = _post_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}",
                {
                    "upload_id": parse_payload["upload_id"],
                    "shipment_date": "2026-05-14",
                    "actual_shipment_date": "2026/05/16",
                    "payload": parse_payload,
                },
            )
            if invalid_actual_status != 400 or "actual_shipment_date" not in str(invalid_actual_payload.get("error", "")):
                raise AssertionError(f"create must reject invalid actual_shipment_date, got {invalid_actual_status} {invalid_actual_payload}")

            invalid_rate_status, invalid_rate_payload = _post_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}",
                {
                    "upload_id": parse_payload["upload_id"],
                    "shipment_date": "2026-05-14",
                    "approx_yuan_rate": "-1",
                    "payload": parse_payload,
                },
            )
            if invalid_rate_status != 400 or "approx_yuan_rate" not in str(invalid_rate_payload.get("error", "")):
                raise AssertionError(f"create must reject non-positive approx_yuan_rate, got {invalid_rate_status} {invalid_rate_payload}")

            create_status, detail = _post_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}",
                {
                    "upload_id": parse_payload["upload_id"],
                    "shipment_date": "2026-05-14",
                    "actual_shipment_date": "2026-05-16",
                    "approx_yuan_rate": "13,2",
                    "payload": parse_payload,
                },
            )
            if create_status != 200 or not detail.get("shipment_id"):
                raise AssertionError(f"create route must persist shipment, got {create_status} {detail}")
            shipment_id = detail["shipment_id"]
            if detail.get("shipment_date") != "2026-05-14" or detail.get("match_status") != "has_unmatched":
                raise AssertionError("created shipment must keep date and unmatched status")
            if (
                detail.get("planned_shipment_date") != "2026-05-14"
                or detail.get("actual_shipment_date") != "2026-05-16"
                or detail.get("actual_ff_acceptance_date") not in {"", None}
            ):
                raise AssertionError(f"created shipment must expose planned/fact dates, got {detail}")
            if detail.get("order_status") != "production":
                raise AssertionError(f"created shipment must default order_status=production, got {detail.get('order_status')}")
            if detail.get("approx_yuan_rate") != 13.2 or detail.get("approx_invoice_cost_rub") != 435.6:
                raise AssertionError(f"created shipment must expose approx yuan rate and invoice RUB cost, got {detail}")
            if detail.get("approx_landed_cost_per_unit_rub") is not None:
                raise AssertionError(f"created shipment without factual expenses must not expose approximate landed cost, got {detail}")
            if detail.get("supplier_name") != "HanShang Technology" or detail.get("metadata", {}).get("supplier_name") != "HanShang Technology":
                raise AssertionError("created shipment must persist fixed supplier_name")
            if detail.get("customer_name") not in {"", None} or detail.get("metadata", {}).get("customer_name") not in {"", None}:
                raise AssertionError("created shipment must not require customer_name")
            if detail.get("contract_no") != "CNT-2026-0513" or detail.get("contract_date") != "2026-05-13":
                raise AssertionError("created shipment must persist contract no/date")
            if len(detail.get("product_lines", [])) != 3 or len(detail.get("extra_lines", [])) != 1:
                raise AssertionError("detail must split product and extra lines")
            if detail["product_lines"][0].get("internal_name") != "Clear iPhone 14 Pro":
                raise AssertionError("created shipment must persist nomenclature auto-match")
            if detail["product_lines"][0].get("price_conformity_status") != "matched":
                raise AssertionError("created shipment must persist price conformity status")

            _seed_supplier_factual_expense(runtime, shipment_id, amount_rub=48.0)

            detail_status, loaded_detail = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}")
            if detail_status != 200 or loaded_detail.get("shipment_id") != shipment_id:
                raise AssertionError("detail route must return persisted card payload")
            if (
                loaded_detail.get("approx_yuan_rate") != 13.2
                or loaded_detail.get("approx_invoice_cost_rub") != 435.6
                or loaded_detail.get("approx_landed_cost_per_unit_rub") != 25.45
            ):
                raise AssertionError(f"detail route must expose approximate landed cost with factual expenses, got {loaded_detail}")
            post_expense_list_status, post_expense_list = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
            listed_created = _shipment_by_id(post_expense_list, shipment_id)
            if (
                post_expense_list_status != 200
                or listed_created.get("approx_yuan_rate") != 13.2
                or listed_created.get("approx_invoice_cost_rub") != 435.6
                or listed_created.get("approx_landed_cost_per_unit_rub") != 25.45
            ):
                raise AssertionError(f"list route must expose approximate landed cost with factual expenses, got {post_expense_list_status} {post_expense_list}")
            if loaded_detail.get("order_status") != "production":
                raise AssertionError("detail route must expose default order_status")
            if (
                loaded_detail.get("planned_shipment_date") != "2026-05-14"
                or loaded_detail.get("actual_shipment_date") != "2026-05-16"
                or loaded_detail.get("actual_ff_acceptance_date") not in {"", None}
            ):
                raise AssertionError("detail route must expose planned/fact shipment dates")
            if loaded_detail.get("product_lines", [{}])[0].get("price_conformity_checked_at") != "2026-05-30T08:00:00Z":
                raise AssertionError("detail route must expose persisted price conformity metadata without recalculation")
            price_check_status, price_checked = _post_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}/price-check",
                {"context": {"source": "http_smoke"}},
            )
            first_price_checked_line = price_checked.get("product_lines", [{}])[0]
            if (
                price_check_status != 200
                or first_price_checked_line.get("price_conformity_check_mode") != "manual_recheck"
                or not str(first_price_checked_line.get("price_conformity_actor") or "").startswith("webcore_user_")
                or first_price_checked_line.get("price_conformity_context", {}).get("source") != "http_smoke"
            ):
                raise AssertionError(f"manual price-check route must persist actor/context/mode, got {price_check_status} {price_checked}")
            if price_checked.get("approx_yuan_rate") != 13.2 or price_checked.get("approx_landed_cost_per_unit_rub") != 25.45:
                raise AssertionError("manual price-check must not erase approximate cost fields")
            loaded_detail = price_checked

            edited = json.loads(json.dumps(loaded_detail, ensure_ascii=False))
            edited["lines"][0]["internal_sku"] = "SKU-MANUAL"
            edited["lines"][0]["internal_nm_id"] = 123456
            edited["lines"][0]["internal_name"] = "Manual SKU"
            edited["lines"][0]["match_status"] = "matched"
            edited["lines"][0]["manual_override"] = True
            edited["lines"][0]["amount"] = 12
            edited["metadata"]["declared_invoice_total"] = 35
            patch_status, patched = _patch_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                {
                    "shipment_date": "2026-05-15",
                    "actual_shipment_date": "2026-05-17",
                    "approx_yuan_rate": "14.5",
                    "payload": edited,
                },
            )
            if patch_status != 200 or patched.get("shipment_date") != "2026-05-15":
                raise AssertionError(f"patch route must update shipment date, got {patch_status} {patched}")
            if (
                patched.get("planned_shipment_date") != "2026-05-15"
                or patched.get("actual_shipment_date") != "2026-05-17"
                or patched.get("actual_ff_acceptance_date") not in {"", None}
            ):
                raise AssertionError(f"patch route must update fact dates, got {patched}")
            if patched.get("match_status") != "manual_override" or patched.get("summary", {}).get("product_amount_total") != 30.0:
                raise AssertionError("patch route must mark manual_override and recalculate totals server-side")
            if patched.get("order_status") != "production":
                raise AssertionError("full patch must preserve existing order_status")
            if patched.get("approx_yuan_rate") != 14.5 or patched.get("approx_invoice_cost_rub") != 507.5:
                raise AssertionError(f"patch route must update approx_yuan_rate and derived invoice cost, got {patched}")
            if patched.get("approx_landed_cost_per_unit_rub") != 29.24:
                raise AssertionError(f"patch route must recalculate approximate landed cost, got {patched}")

            status_patch_status, status_patched = _patch_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                {"order_status": "in_transit"},
            )
            if status_patch_status != 200 or status_patched.get("order_status") != "in_transit":
                raise AssertionError(f"status-only patch must persist in_transit, got {status_patch_status} {status_patched}")
            if (
                len(status_patched.get("product_lines", [])) != 3
                or status_patched.get("source_file_sha256") != workbook_sha256
                or status_patched.get("invoice_no") != "26GN390"
                or status_patched.get("actual_shipment_date") != "2026-05-17"
                or status_patched.get("actual_ff_acceptance_date") not in {"", None}
                or status_patched.get("approx_yuan_rate") != 14.5
                or status_patched.get("approx_landed_cost_per_unit_rub") != 29.24
            ):
                raise AssertionError("status-only patch must not erase lines, metadata, source file, fact dates, or approx yuan rate")
            invalid_status, invalid_payload = _patch_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                {"order_status": "delivered_to_mars"},
            )
            if invalid_status != 400 or "order_status" not in str(invalid_payload.get("error", "")):
                raise AssertionError(f"invalid order_status must be rejected, got {invalid_status} {invalid_payload}")
            accepted_status, accepted_patched = _patch_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                {"actual_ff_acceptance_date": "2026-05-30"},
            )
            if accepted_status != 200 or accepted_patched.get("order_status") != "accepted_ff":
                raise AssertionError(f"actual FF acceptance patch must persist accepted_ff, got {accepted_status} {accepted_patched}")
            if accepted_patched.get("actual_ff_acceptance_date") != "2026-05-30":
                raise AssertionError(f"actual FF acceptance patch must keep acceptance date, got {accepted_patched}")
            ff_stock_keys = [str(item.get("source_key") or "") for item in runtime.list_ff_stock_operations()]
            if ff_stock_keys.count(f"supplier_shipment_acceptance:{shipment_id}") != 1:
                raise AssertionError(f"actual FF acceptance must create one idempotent ФФ stock operation, got {ff_stock_keys}")

            second_nom_status, second_nom_payload = _post_json(
                f"{base_url}{DEFAULT_NOMENCLATURE_PATH}",
                {
                    "is_active": True,
                    "our_sku": "SKU-AS-14PM",
                    "nm_id": 210184534,
                    "nomenclature_name": "Anti-Spy iPhone 14 Pro Max",
                    "product_type": "anti_spy",
                    "match_key": "anti_spy|iphone_14_pro_max",
                    "comment": "rematch smoke",
                },
            )
            if second_nom_status != 200 or second_nom_payload.get("item", {}).get("nm_id") != 210184534:
                raise AssertionError("second nomenclature item must save for rematch")
            rematch_status, rematched = _post_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}/rematch",
                {"overwrite_manual": False},
            )
            if rematch_status != 200:
                raise AssertionError(f"rematch route must return updated detail, got {rematch_status} {rematched}")
            rematched_products = rematched.get("product_lines", [])
            if rematched_products[0].get("internal_sku") != "SKU-MANUAL":
                raise AssertionError("rematch must not overwrite manual_override rows by default")
            if rematched_products[2].get("internal_nm_id") != 210184534:
                raise AssertionError("rematch must fill previously unmatched rows from nomenclature")
            if rematched.get("approx_yuan_rate") != 14.5 or rematched.get("approx_landed_cost_per_unit_rub") != 29.24:
                raise AssertionError("rematch must not erase approximate cost fields")

            import_status, import_payload = _post_multipart(
                f"{base_url}{DEFAULT_NOMENCLATURE_IMPORT_PATH}",
                nomenclature_import_bytes,
                filename="nomenclature.xlsx",
            )
            if (
                import_status != 200
                or import_payload.get("created_count") != 1
                or import_payload.get("updated_count") != 1
                or import_payload.get("deactivated_count") != 1
                or import_payload.get("error_count") != 0
            ):
                raise AssertionError(f"nomenclature import must apply batch changes, got {import_status} {import_payload}")
            imported_list_status, imported_list_payload = _get_json(f"{base_url}{DEFAULT_NOMENCLATURE_PATH}")
            imported_items = {item["item_id"]: item for item in imported_list_payload.get("items", [])}
            if imported_list_status != 200:
                raise AssertionError(f"nomenclature list after import failed: {imported_list_status} {imported_list_payload}")
            first_item = imported_items[str(create_nom_payload["item"]["item_id"])]
            if (
                first_item.get("purchase_price_yuan") != 13.75
                or first_item.get("our_sku") != "SKU-CLEAR-14P"
                or first_item.get("aliases") != ["iPhone 14 Pro"]
                or first_item.get("comment") != "smoke"
            ):
                raise AssertionError(f"nomenclature import must preserve hidden fields while updating visible fields, got {first_item}")
            compat_item = imported_items[str(compat_nom_payload["item"]["item_id"])]
            if compat_item.get("is_active") is not False:
                raise AssertionError("nomenclature import must support soft-disable through Включено=нет")
            created_import_items = [item for item in imported_items.values() if item.get("match_key") == "matte|iphone_15"]
            if len(created_import_items) != 1 or created_import_items[0].get("purchase_price_yuan") != 9.5:
                raise AssertionError(f"nomenclature import must create new active rows, got {created_import_items}")
            invalid_status, invalid_import_payload = _post_multipart(
                f"{base_url}{DEFAULT_NOMENCLATURE_IMPORT_PATH}",
                _build_invalid_nomenclature_import_fixture(),
                filename="nomenclature-invalid.xlsx",
            )
            if (
                invalid_status != 400
                or invalid_import_payload.get("status") != "error"
                or invalid_import_payload.get("error_count", 0) < 2
            ):
                raise AssertionError(f"invalid nomenclature import must return row-level errors, got {invalid_status} {invalid_import_payload}")
            after_invalid_status, after_invalid_payload = _get_json(f"{base_url}{DEFAULT_NOMENCLATURE_PATH}")
            if after_invalid_status != 200 or len(after_invalid_payload.get("items", [])) != len(imported_list_payload.get("items", [])):
                raise AssertionError("invalid nomenclature import must not partially mutate rows")
            runtime.save_nomenclature_item(
                {
                    "item_id": "nom_duplicate_a",
                    "is_active": True,
                    "our_sku": "",
                    "nm_id": 700001,
                    "nomenclature_name": "Duplicate A",
                    "product_type": "clear",
                    "match_key": "clear|duplicate",
                    "purchase_price_yuan": None,
                    "aliases": [],
                    "compatible_models_text": "",
                    "compatible_model_keys": [],
                    "comment": "",
                    "created_at": "2026-05-30T08:20:00Z",
                    "updated_at": "2026-05-30T08:20:00Z",
                }
            )
            runtime.save_nomenclature_item(
                {
                    "item_id": "nom_duplicate_b",
                    "is_active": True,
                    "our_sku": "",
                    "nm_id": 700002,
                    "nomenclature_name": "Duplicate B",
                    "product_type": "clear",
                    "match_key": "clear|duplicate",
                    "purchase_price_yuan": None,
                    "aliases": [],
                    "compatible_models_text": "",
                    "compatible_model_keys": [],
                    "comment": "",
                    "created_at": "2026-05-30T08:20:00Z",
                    "updated_at": "2026-05-30T08:20:00Z",
                }
            )
            ambiguous_status, ambiguous_payload = _post_multipart(
                f"{base_url}{DEFAULT_NOMENCLATURE_IMPORT_PATH}",
                _build_ambiguous_nomenclature_import_fixture(),
                filename="nomenclature-ambiguous.xlsx",
            )
            if ambiguous_status != 400 or "неоднозначен" not in json.dumps(ambiguous_payload, ensure_ascii=False):
                raise AssertionError(f"ambiguous match_key import must return row-level error, got {ambiguous_status} {ambiguous_payload}")

            registry_status, registry_payload = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
            if registry_status != 200 or len(registry_payload.get("shipments", [])) != 1:
                raise AssertionError("list route must expose saved shipment")
            if registry_payload["shipments"][0].get("supplier_name") != "HanShang Technology":
                raise AssertionError("list route must expose fixed supplier_name")
            if registry_payload["shipments"][0].get("order_status") != "accepted_ff":
                raise AssertionError("list route must expose persisted order_status")
            if (
                registry_payload["shipments"][0].get("planned_shipment_date") != "2026-05-15"
                or registry_payload["shipments"][0].get("actual_shipment_date") != "2026-05-17"
                or registry_payload["shipments"][0].get("actual_ff_acceptance_date") != "2026-05-30"
            ):
                raise AssertionError(f"list route must expose planned/fact dates, got {registry_payload}")
            shipment_registry_status, shipment_registry = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_PATH}")
            if (
                shipment_registry_status != 200
                or shipment_registry.get("contract_name") != "sheet_vitrina_v1_supplier_shipment_registry"
                or shipment_registry.get("meta", {}).get("shipment_count") != 1
            ):
                raise AssertionError(f"shipment registry matrix route mismatch: {shipment_registry_status} {shipment_registry}")
            shipment_registry_json = json.dumps(shipment_registry, ensure_ascii=False)
            if "NaN" in shipment_registry_json or "Infinity" in shipment_registry_json:
                raise AssertionError(f"shipment registry matrix must not expose invalid numbers: {shipment_registry}")
            section_ids = [section.get("section_id") for section in shipment_registry.get("sections", [])]
            expected_section_ids = ["passport", "quote_logistics", "lead_times", "cargo_physics", "cargo_value", "fact_expenses", "fact_normalized", "documents"]
            if section_ids != expected_section_ids:
                raise AssertionError(f"shipment registry matrix missing sections: {section_ids}")
            if _registry_cell_display(shipment_registry, "quote_logistics", "quote_total_rub_per_unit", shipment_id) != "—":
                raise AssertionError(f"shipment registry without financial docs must keep quote ₽/шт unavailable: {shipment_registry}")
            if _registry_cell_display(shipment_registry, "fact_expenses", "fact_total_rub_per_unit", shipment_id) != "—":
                raise AssertionError(f"shipment registry without financial docs must keep fact ₽/шт unavailable: {shipment_registry}")
            if _registry_cell_display(shipment_registry, "lead_times", "shipment_date", shipment_id) != "2026-05-15":
                raise AssertionError(f"shipment registry must expose planned shipment date: {shipment_registry}")
            if _registry_cell_display(shipment_registry, "lead_times", "actual_shipment_date", shipment_id) != "2026-05-17":
                raise AssertionError(f"shipment registry must expose actual shipment date: {shipment_registry}")
            if _registry_cell_display(shipment_registry, "lead_times", "actual_ff_acceptance_date", shipment_id) != "2026-05-30":
                raise AssertionError(f"shipment registry must expose actual FF acceptance date: {shipment_registry}")
            if _registry_cell_display(shipment_registry, "lead_times", "actual_delivery_days", shipment_id) != "13 дн.":
                raise AssertionError(f"shipment registry must calculate actual delivery days: {shipment_registry}")
            invoice_path = registry_payload["shipments"][0].get("invoice_download_path")
            invoice_status, invoice_bytes, invoice_headers = _get_bytes(f"{base_url}{invoice_path}")
            if invoice_status != 200 or hashlib.sha256(invoice_bytes).hexdigest() != workbook_sha256:
                raise AssertionError("invoice download must preserve original XLSX bytes")
            if "attachment" not in str(invoice_headers.get("Content-Disposition", "")):
                raise AssertionError("invoice download must be an attachment")
            delete_status, delete_payload = _delete_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}")
            if delete_status != 200 or delete_payload.get("deleted") is not True:
                raise AssertionError(f"delete route must remove shipment, got {delete_status} {delete_payload}")
            after_delete_status, after_delete_payload = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
            if after_delete_status != 200 or after_delete_payload.get("shipments") != []:
                raise AssertionError("deleted supplier order must disappear from registry")
            deleted_invoice_status, _, _ = _get_bytes(f"{base_url}{invoice_path}")
            if deleted_invoice_status != 404:
                raise AssertionError("deleted supplier invoice must not remain downloadable")

            runtime.save_supplier_shipment(
                header={
                    "shipment_id": "sup_legacy_missing_supplier",
                    "created_at": "2026-05-30T08:10:00Z",
                    "updated_at": "2026-05-30T08:10:00Z",
                    "shipment_date": "2026-05-16",
                    "invoice_no": "LEGACY-1",
                    "invoice_date": "2026-05-15",
                    "contract_no": "",
                    "contract_date": "",
                    "supplier_name": "",
                    "customer_name": "",
                    "currency": "RMB",
                    "product_qty_total": 0,
                    "product_amount_total": 0,
                    "extras_amount_total": 0,
                    "invoice_amount_total": 0,
                    "declared_invoice_total": 0,
                    "match_status": "all_matched",
                    "source_filename": "legacy.xlsx",
                    "source_file_sha256": "",
                    "source_file_path": "",
                    "parser_version": "legacy",
                    "warnings": [],
                    "errors": [],
                },
                lines=[],
            )
            legacy_list_status, legacy_list_payload = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
            if (
                legacy_list_status != 200
                or legacy_list_payload.get("shipments", [{}])[0].get("supplier_name") != "HanShang Technology"
            ):
                raise AssertionError(f"legacy missing supplier must list with default fallback, got {legacy_list_payload}")
            if legacy_list_payload.get("shipments", [{}])[0].get("order_status") != "production":
                raise AssertionError("legacy missing order_status must list with production fallback")
            if (
                legacy_list_payload.get("shipments", [{}])[0].get("actual_shipment_date") != ""
                or legacy_list_payload.get("shipments", [{}])[0].get("actual_ff_acceptance_date") != ""
            ):
                raise AssertionError("legacy rows without fact dates must expose empty fact dates")
            legacy_no_rate = legacy_list_payload.get("shipments", [{}])[0]
            if (
                legacy_no_rate.get("approx_yuan_rate") is not None
                or legacy_no_rate.get("approx_invoice_cost_rub") is not None
                or legacy_no_rate.get("approx_landed_cost_per_unit_rub") is not None
            ):
                raise AssertionError(f"legacy rows without approx_yuan_rate must expose empty approximate fields, got {legacy_no_rate}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("sheet_vitrina_v1_supplier_shipments_http_smoke: OK")


def _build_invoice_fixture() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Invoice"
    sheet.append(["Invoice No:", "26GN390"])
    sheet.append(["Invoice Date:", "14.5.2026"])
    sheet.append(["Contract No.", "CNT-2026-0513"])
    sheet.append(["Date of Contract", "2026.5.13"])
    sheet.append(["Supplier:", "Zhejiang Supplier", "", "Currency:", "RMB"])
    sheet.append(["Invoice Total:", 33])
    sheet.append(["NO.", "NAME & SPECIFICATION", "MODELS", "QTY", "U.PRICE", "AMOUNT", "COMMENT"])
    sheet.append([1, "高清膜 smk", "iPhone 14 Pro", 10, 1, 10, ""])
    sheet.append([2, "防窥膜 (Anti-Spy)", "iPhone 17e / 16e /14 / 13 / 13Pro", 4, 2, 8, ""])
    sheet.append([3, "防窥膜 (Anti-Spy)", "iPhone 14 Pro Max", 5, 2, 10, ""])
    sheet.append([4, "OPP bag packets", "", 100, 0.05, 5, "OPP packets"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _shipment_by_id(payload: dict, shipment_id: str) -> dict:
    for shipment in payload.get("shipments", []):
        if shipment.get("shipment_id") == shipment_id:
            return shipment
    return {}


def _seed_supplier_factual_expense(
    runtime: RegistryUploadDbBackedRuntime,
    shipment_id: str,
    *,
    amount_rub: float,
) -> None:
    runtime.save_supplier_financial_document(
        document={
            "document_id": f"fdoc_{shipment_id}_logistics",
            "supplier_order_id": shipment_id,
            "document_type": "logistics_invoice",
            "original_filename": "factual-logistics.pdf",
            "stored_file_path": "",
            "file_content_type": "application/pdf",
            "file_sha256": "",
            "uploaded_at": "2026-05-30T08:00:00Z",
            "updated_at": "2026-05-30T08:00:00Z",
            "parse_status": "parsed",
            "vendor": "Smoke Logistics",
            "document_number": "SMOKE-EXPENSE",
            "document_date": "2026-05-20",
            "currency": "RUB",
            "total_amount": amount_rub,
            "total_amount_rub": amount_rub,
            "normalized_parse": {"document_type": "logistics_invoice", "amount_rub": amount_rub},
            "raw_parse": {},
            "parser_version": "smoke",
            "warnings": [],
            "errors": [],
        },
        expense_lines=[
            {
                "line_id": f"fline_{shipment_id}_logistics",
                "financial_document_id": f"fdoc_{shipment_id}_logistics",
                "supplier_order_id": shipment_id,
                "sort_order": 1,
                "category": "domestic_transport",
                "stage": "fact",
                "description": "Factual smoke logistics expense",
                "amount": amount_rub,
                "currency": "RUB",
                "amount_rub": amount_rub,
                "vat_rate": None,
                "vat_amount_rub": None,
                "included_in_logistics_efficiency": True,
                "included_in_customs_total": False,
                "status": "parsed",
                "confidence": 1.0,
                "raw": {},
            }
        ],
    )


def _build_price_conformity_invoice_fixture() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Invoice"
    sheet.append(["Invoice No:", "PRICE-CHECK"])
    sheet.append(["Invoice Date:", "30.5.2026"])
    sheet.append(["Contract No.", "CNT-PRICE"])
    sheet.append(["Date of Contract", "2026.5.30"])
    sheet.append(["Supplier:", "Zhejiang Supplier", "", "Currency:", "RMB"])
    sheet.append(["Invoice Total:", 16])
    sheet.append(["NO.", "NAME & SPECIFICATION", "MODELS", "QTY", "U.PRICE", "AMOUNT", "COMMENT"])
    sheet.append([1, "高清膜 smk", "iPhone 14 Pro", 1, 1, 1, "matched"])
    sheet.append([2, "防窥膜 (Anti-Spy)", "iPhone 14", 1, 2, 2, "mismatched"])
    sheet.append([3, "高清膜 smk", "iPhone 99", 1, 4, 4, "sku missing"])
    sheet.append([4, "高清膜 smk", "iPhone 15", 1, 4, 4, "reference price missing"])
    sheet.append([5, "磨砂膜 Matte", "iPhone 16", 1, "", "", "invoice price missing"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _build_nomenclature_import_fixture(*, first_item_id: str, compat_item_id: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Номенклатура"
    sheet.append(
        [
            "ID строки",
            "Включено",
            "nmId",
            "Номенклатура",
            "Тип",
            "Match key",
            "Цена закупки, ¥",
            "Совместимые модели",
            "Ключи совместимости",
            "Обновлено",
        ]
    )
    sheet.append(
        [
            first_item_id,
            "да",
            210183919,
            "Clear iPhone 14 Pro",
            "Прозрачное",
            "clear|iphone_14_pro",
            "13,75",
            "iPhone 14 Pro",
            "",
            "",
        ]
    )
    sheet.append(
        [
            compat_item_id,
            "нет",
            391662410,
            "anti-spy iPhone 14 / 13 / 13Pro",
            "anti_spy",
            "anti_spy|iphone_14_13_13pro",
            "",
            "iPhone 14, iPhone 13, iPhone 13 Pro",
            "iphone_14; iphone_13; iphone_13_pro",
            "",
        ]
    )
    sheet.append(
        [
            "",
            "yes",
            500001,
            "Matte iPhone 15",
            "Матовое",
            "matte|iphone_15",
            "9,5",
            "iPhone 15",
            "",
            "",
        ]
    )
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _build_invalid_nomenclature_import_fixture() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Номенклатура"
    sheet.append(["Включено", "nmId", "Номенклатура", "Тип", "Match key", "Цена закупки, ¥"])
    sheet.append(["да", 900001, "Invalid type", "глянцевое", "clear|invalid_type", 1])
    sheet.append(["да", 900002, "Invalid price", "clear", "clear|invalid_price", "-1"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _build_ambiguous_nomenclature_import_fixture() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Номенклатура"
    sheet.append(["Включено", "nmId", "Номенклатура", "Тип", "Match key", "Цена закупки, ¥"])
    sheet.append(["да", 700003, "Duplicate import", "clear", "clear|duplicate", 2])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _post_multipart(
    url: str,
    workbook_bytes: bytes,
    *,
    filename: str,
    content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
) -> tuple[int, dict[str, object]]:
    boundary = "----wbcore-supplier" + uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            workbook_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    request = urllib_request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"},
        method="POST",
    )
    return _open_json(request)


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    return _open_json(request)


def _patch_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="PATCH",
    )
    return _open_json(request)


def _delete_json(url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="DELETE")
    return _open_json(request)


def _registry_cell_display(registry, section_id: str, row_id: str, shipment_id: str) -> str:
    for section in registry.get("sections", []):
        if section.get("section_id") != section_id:
            continue
        for row in section.get("rows", []):
            if row.get("row_id") == row_id:
                return str((row.get("cells", {}).get(shipment_id) or {}).get("display") or "")
    return ""


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="GET")
    return _open_json(request)


def _open_json(request: urllib_request.Request) -> tuple[int, dict[str, object]]:
    try:
        with urllib_request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_bytes(url: str) -> tuple[int, bytes, dict[str, str]]:
    request = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib_error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
