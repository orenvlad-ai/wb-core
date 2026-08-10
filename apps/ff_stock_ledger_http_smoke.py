"""HTTP smoke-check for the server-owned Остатки ФФ ledger routes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import time
from urllib import error, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_FF_INVENTORY_CONFIRM_PATH,
    DEFAULT_FF_INVENTORY_PREVIEW_PATH,
    DEFAULT_FF_INVENTORY_STATUS_PATH,
    DEFAULT_FF_INVENTORY_TEMPLATE_PATH,
    DEFAULT_FF_OVERHEAD_CONFIRM_PATH,
    DEFAULT_FF_OVERHEAD_PREVIEW_PATH,
    DEFAULT_FF_OVERHEAD_STATUS_PATH,
    DEFAULT_FF_STOCKS_CONFIRM_PATH,
    DEFAULT_FF_STOCKS_EXPORT_PATH,
    DEFAULT_FF_STOCKS_PATH,
    DEFAULT_FF_STOCKS_PREVIEW_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.ff_document_workflow import (
    FfDocumentWorkflow,
    mark_ff_replay_economics,
)
from packages.application.ff_inventory_reconciliation import FfInventoryReconciliation
from packages.application.ff_overhead_allocation import FfOverheadAllocation
from packages.application.ff_stock_ledger import (
    FF_STOCK_OPERATION_AUTO_WRITEOFF,
    FF_STOCK_OPERATION_CORRECTION_RECEIPT,
    FF_STOCK_OPERATION_MANUAL_RECEIPT,
    FF_STOCK_SOURCE_MANUAL_EXCEL,
    FF_STOCK_SOURCE_RUNTIME_REPAIR,
    FF_STOCK_SOURCE_WB_SUPPLY,
)
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.application.warehouse_update_journal import WarehouseUpdateJournal
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-18T09:00:00Z"
NOW = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="ff-stock-ledger-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        active_nm_ids = [int(item.nm_id) for item in runtime.load_current_state().config_v2 if item.enabled]
        probe_nm_id = active_nm_ids[0]
        _seed_nomenclature(runtime, active_nm_ids)

        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            now_factory=lambda: NOW,
        )
        def reject_synchronous_replay(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("FF confirm must not execute functional/economics replay in HTTP")

        entrypoint._replay_ff_document_queue = reject_synchronous_replay  # type: ignore[method-assign]
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"

            inventory_counts_before_page = _inventory_document_counts(runtime)
            page_code, page_bytes, _ = _get_bytes(
                f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}"
            )
            _assert(page_code == 200 and page_bytes, "operator page must render")
            _assert(
                _inventory_document_counts(runtime) == inventory_counts_before_page,
                "opening the operator page must not create inventory preview/document state",
            )

            status_code, status_payload = _get_json(f"{base_url}{DEFAULT_FF_STOCKS_PATH}")
            _assert(status_code == 200, f"status route must return 200, got {status_code}")
            _assert(status_payload["contract_name"] == "sheet_vitrina_v1_ff_stock_ledger", "status contract changed")
            _assert(status_payload["registry"]["summary"]["sku_count"] == len(active_nm_ids), "status must include active SKU registry")

            export_code, export_bytes, export_headers = _get_bytes(f"{base_url}{DEFAULT_FF_STOCKS_EXPORT_PATH}")
            _assert(export_code == 200, f"export route must return 200, got {export_code}")
            _assert(export_headers.get("Content-Type", "").startswith(XLSX_TYPE), "export must be XLSX")
            export_rows = read_first_sheet_rows(export_bytes)
            _assert(export_rows[0] == ["barcode", "nmId", "SKU/название/комментарий", "группа", "количество"], "export headers changed")

            upload_bytes = _operation_xlsx(
                [[f"000460{probe_nm_id}", probe_nm_id, "Probe", "Clear", 12]]
            )
            preview_code, preview_payload = _post_multipart(
                f"{base_url}{DEFAULT_FF_STOCKS_PREVIEW_PATH}",
                upload_bytes,
                filename="receipt.xlsx",
                fields={"operation_type": "manual_receipt"},
            )
            _assert(preview_code == 200, f"preview route must return 200, got {preview_code} {preview_payload}")
            _assert(preview_payload["apply_allowed"] is True, "preview must be applicable")
            _assert(preview_payload["preview"]["summary"]["sku_count"] == 1, "preview SKU count changed")

            confirm_code, confirm_payload = _post_json(
                f"{base_url}{DEFAULT_FF_STOCKS_CONFIRM_PATH}",
                {"preview_id": preview_payload["preview"]["preview_id"]},
            )
            _assert(confirm_code == 200, f"confirm route must return 200, got {confirm_code} {confirm_payload}")
            operation = confirm_payload["operation"]
            _assert(operation["operation_type"] == "manual_receipt", "confirm must create manual receipt")
            _assert(operation["file_available"] is True, "manual operation must expose source file")

            status_after_code, status_after_payload = _get_json(f"{base_url}{DEFAULT_FF_STOCKS_PATH}")
            _assert(status_after_code == 200, "status after confirm must return 200")
            probe_row = next(item for item in status_after_payload["registry"]["rows"] if int(item["nm_id"]) == probe_nm_id)
            _assert(probe_row["current_stock_ff"] == 12.0, "confirmed receipt must affect computed balance")

            inventory_template_code, inventory_template, inventory_headers = _get_bytes(
                f"{base_url}{DEFAULT_FF_INVENTORY_TEMPLATE_PATH}?business_date=2026-04-18"
            )
            _assert(inventory_template_code == 200, "FF inventory template route must return 200")
            _assert(inventory_headers.get("Content-Type", "").startswith(XLSX_TYPE), "FF inventory template must be XLSX")
            inventory_rows = read_first_sheet_rows(inventory_template)
            _assert(len(inventory_rows) == len(active_nm_ids) + 1, "FF inventory template must contain every active SKU")
            _assert(
                inventory_rows[0]
                == ["nmId", "Штрихкод", "Комментарий SKU", "Остаток ФФ", "Дата остатка"],
                "FF inventory template headers must expose barcode identity",
            )
            probe_template_row = next(
                row for row in inventory_rows[1:] if int(row[0]) == probe_nm_id
            )
            _assert(
                probe_template_row[1] == f"000460{probe_nm_id}",
                "FF inventory template must preserve a leading-zero barcode",
            )
            _assert(
                _inventory_document_counts(runtime) == inventory_counts_before_page,
                "template download must not create inventory preview/document state",
            )
            barcode_only_inventory = build_single_sheet_workbook_bytes(
                "Инвентаризация FF",
                [
                    list(inventory_rows[0]),
                    *[
                        [None, row[1], row[2], row[3], row[4]]
                        for row in inventory_rows[1:]
                    ],
                ],
            )
            _post_multipart_disconnect(
                config.port,
                DEFAULT_FF_INVENTORY_PREVIEW_PATH,
                barcode_only_inventory,
                filename="inventory-barcode-only.xlsx",
                fields={"business_date": "2026-04-18", "request_id": "ffi_http_inventory_0001"},
            )
            inventory_preview = _poll_workflow(
                f"{base_url}{DEFAULT_FF_INVENTORY_STATUS_PATH}?request_id=ffi_http_inventory_0001",
                expected={"ready"},
            )
            _assert(inventory_preview["confirm_allowed"] is True, "complete inventory preview must be confirmable")
            repeat_started = time.monotonic()
            repeat_code, repeat_preview = _post_multipart(
                f"{base_url}{DEFAULT_FF_INVENTORY_PREVIEW_PATH}",
                barcode_only_inventory,
                filename="inventory-barcode-only.xlsx",
                fields={"business_date": "2026-04-18", "request_id": "ffi_http_inventory_retry"},
            )
            _assert(repeat_code == 202 and repeat_preview["preview_id"] == inventory_preview["preview_id"], "exact inventory repeat must reuse one preview")
            _assert((time.monotonic() - repeat_started) < 1.0, "exact inventory repeat must be a fast T0 readback")
            _, alias_readback = _get_json(
                f"{base_url}{DEFAULT_FF_INVENTORY_STATUS_PATH}?request_id=ffi_http_inventory_retry"
            )
            _assert(alias_readback["preview_id"] == inventory_preview["preview_id"], "inventory retry request id must recover the exact existing preview")
            _post_json_disconnect(
                config.port,
                DEFAULT_FF_INVENTORY_CONFIRM_PATH,
                {"confirm": True, "preview_id": inventory_preview["preview_id"], "fingerprint": inventory_preview["fingerprint"]},
            )
            inventory_status = _poll_workflow(
                f"{base_url}{DEFAULT_FF_INVENTORY_STATUS_PATH}?preview_id={inventory_preview['preview_id']}",
                expected={"applied", "replay_complete"},
            )
            _assert(inventory_status["document"]["status"] == "applied", "inventory response loss must recover durable commit")
            inventory_retry_code, inventory_retry = _post_json(
                f"{base_url}{DEFAULT_FF_INVENTORY_CONFIRM_PATH}",
                {"confirm": True, "preview_id": inventory_preview["preview_id"], "fingerprint": inventory_preview["fingerprint"]},
            )
            _assert(inventory_retry_code == 200 and inventory_retry["idempotent"] is True, "inventory double click must be T0")
            with sqlite3.connect(runtime.db_path) as conn:
                _assert(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ff_inventory_reconciliations WHERE source_sha256=?", (inventory_preview["source"]["sha256"],)).fetchone()[0] == 1, "inventory retry duplicated reconciliation")
                _assert(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ff_workflow_events WHERE identity=? AND stage='file_accepted'", (inventory_preview["preview_id"],)).fetchone()[0] == 1, "inventory retry duplicated acceptance telemetry")
                stable_inventory_source = "ff_inventory:" + str(inventory_status["document"]["document_id"])
                conn.execute(
                    "DELETE FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE stable_source_id=?",
                    (stable_inventory_source,),
                )
                conn.commit()
            repaired_retry_code, repaired_retry = _post_json(
                f"{base_url}{DEFAULT_FF_INVENTORY_CONFIRM_PATH}",
                {"confirm": True, "preview_id": inventory_preview["preview_id"], "fingerprint": inventory_preview["fingerprint"]},
            )
            _assert(repaired_retry_code == 200 and repaired_retry["idempotent"] is True, "exact retry must recover a legacy commit-to-enqueue gap")
            with sqlite3.connect(runtime.db_path) as conn:
                _assert(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE stable_source_id=?", (stable_inventory_source,)).fetchone()[0] == 1, "inventory queue recovery must create exactly one replay row")

            mismatch_inventory = build_single_sheet_workbook_bytes(
                "Инвентаризация FF",
                [list(inventory_rows[0]), *[[row[0], row[1], row[2], row[3], "2026-04-17"] for row in inventory_rows[1:]]],
            )
            mismatch_code, mismatch_payload = _post_multipart(
                f"{base_url}{DEFAULT_FF_INVENTORY_PREVIEW_PATH}",
                mismatch_inventory,
                filename="inventory-date-mismatch.xlsx",
                fields={"business_date": "2026-04-18", "request_id": "ffi_http_inventory_mismatch"},
            )
            _assert(mismatch_code == 422, f"date mismatch must be a controlled 422: {mismatch_payload}")
            _assert("Дата в файле: 17.04.2026; дата в форме: 18.04.2026" in mismatch_payload["validation"]["message_ru"], "date mismatch must be grouped and localized")
            _assert(mismatch_payload["validation"]["affected_count"] == len(active_nm_ids), "date mismatch affected row count changed")
            _assert(mismatch_payload["validation"]["examples"][0]["message_ru"], "row examples must be localized")
            mismatch_repeat_code, mismatch_repeat = _post_multipart(
                f"{base_url}{DEFAULT_FF_INVENTORY_PREVIEW_PATH}",
                mismatch_inventory,
                filename="inventory-date-mismatch.xlsx",
                fields={"business_date": "2026-04-18", "request_id": "ffi_http_inventory_mismatch_retry"},
            )
            _assert(mismatch_repeat_code == 422 and mismatch_repeat["preview_id"] == mismatch_payload["preview_id"], "blocked exact inventory repeat must remain a controlled T0 422")

            _post_json_disconnect(
                config.port,
                DEFAULT_FF_OVERHEAD_PREVIEW_PATH,
                {"request_id": "ffo_http_overhead_disconnect", "business_date": "2026-04-18", "amount_rub": "14.00", "reason": "Disconnected preview"},
            )
            disconnected_overhead = _poll_workflow(
                f"{base_url}{DEFAULT_FF_OVERHEAD_STATUS_PATH}?request_id=ffo_http_overhead_disconnect",
                expected={"ready"},
            )
            _assert(disconnected_overhead["preview_id"], "overhead preview must recover after response loss")

            overhead_accept_started = time.monotonic()
            overhead_preview_code, overhead_preview = _post_json(
                f"{base_url}{DEFAULT_FF_OVERHEAD_PREVIEW_PATH}",
                {"request_id": "ffo_http_overhead_0001", "business_date": "2026-04-18", "amount_rub": "12.00", "reason": "HTTP fixture overhead"},
            )
            _assert(overhead_preview_code == 202, f"overhead accept failed: {overhead_preview}")
            _assert((time.monotonic() - overhead_accept_started) < 1.0, "overhead accept must not wait for planning/replay")
            overhead_preview = _poll_workflow(
                f"{base_url}{DEFAULT_FF_OVERHEAD_STATUS_PATH}?request_id=ffo_http_overhead_0001",
                expected={"ready"},
            )
            _, unknown_overhead = _get_json(
                f"{base_url}{DEFAULT_FF_OVERHEAD_STATUS_PATH}?request_id=ffo_http_unknown"
            )
            _assert(
                unknown_overhead["state"] == "not_found",
                "unknown exact overhead request must not fall through to the latest document",
            )
            overhead_repeat_code, overhead_repeat = _post_json(
                f"{base_url}{DEFAULT_FF_OVERHEAD_PREVIEW_PATH}",
                {"request_id": "ffo_http_overhead_retry", "business_date": "2026-04-18", "amount_rub": "12.00", "reason": "HTTP fixture overhead"},
            )
            _assert(overhead_repeat_code == 202 and overhead_repeat["preview_id"] == overhead_preview["preview_id"], "overhead double click must reuse one durable preview")
            _, overhead_alias = _get_json(
                f"{base_url}{DEFAULT_FF_OVERHEAD_STATUS_PATH}?request_id=ffo_http_overhead_retry"
            )
            _assert(overhead_alias["preview_id"] == overhead_preview["preview_id"], "overhead retry request id must recover the exact preview")
            double_results: list[tuple[int, object]] = []
            start_double = threading.Barrier(3)

            def post_double(request_id: str) -> None:
                start_double.wait(timeout=3)
                double_results.append(
                    _post_json(
                        f"{base_url}{DEFAULT_FF_OVERHEAD_PREVIEW_PATH}",
                        {"request_id": request_id, "business_date": "2026-04-18", "amount_rub": "13.00", "reason": "Concurrent double click"},
                    )
                )

            double_threads = [
                threading.Thread(target=post_double, args=(request_id,), daemon=True)
                for request_id in ("ffo_http_double_0001", "ffo_http_double_0002")
            ]
            for double_thread in double_threads:
                double_thread.start()
            start_double.wait(timeout=3)
            for double_thread in double_threads:
                double_thread.join(timeout=5)
            _assert(len(double_results) == 2 and all(item[0] == 202 for item in double_results), f"concurrent double click must be accepted: {double_results}")
            _assert(len({str(item[1]["preview_id"]) for item in double_results}) == 1, "concurrent double click must resolve to one preview id")
            with sqlite3.connect(runtime.db_path) as conn:
                _assert(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ff_overhead_previews WHERE request_identity=(SELECT request_identity FROM sheet_vitrina_v1_ff_overhead_previews WHERE preview_id=?)", (double_results[0][1]["preview_id"],)).fetchone()[0] == 1, "concurrent double click duplicated overhead preview")
                _assert(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ff_workflow_events WHERE identity=? AND stage='data_accepted'", (double_results[0][1]["preview_id"],)).fetchone()[0] == 1, "concurrent double click duplicated overhead acceptance telemetry")
            _post_json_disconnect(
                config.port,
                DEFAULT_FF_OVERHEAD_CONFIRM_PATH,
                {"confirm": True, "preview_id": overhead_preview["preview_id"], "fingerprint": overhead_preview["fingerprint"]},
            )
            overhead_status = _poll_workflow(
                f"{base_url}{DEFAULT_FF_OVERHEAD_STATUS_PATH}?preview_id={overhead_preview['preview_id']}",
                expected={"applied", "replay_complete"},
            )
            _assert(overhead_status["details"]["physical_quantity_unchanged"] is True, "overhead status must state quantity unchanged")
            with sqlite3.connect(runtime.db_path) as conn:
                queue = conn.execute(
                    "SELECT queue_id FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE stable_source_id=?",
                    ("ff_overhead:" + overhead_status["document"]["document_id"],),
                ).fetchone()
                conn.execute(
                    "UPDATE sheet_vitrina_v1_warehouse_targeted_recalc_queue SET status='complete',finished_at=? WHERE queue_id=?",
                    (ACTIVATED_AT, queue[0]),
                )
                conn.commit()
            _, functional_only_status = _get_json(
                f"{base_url}{DEFAULT_FF_OVERHEAD_STATUS_PATH}?preview_id={overhead_preview['preview_id']}"
            )
            _assert(functional_only_status["state"] == "applied", "functional completion alone must not produce final green")
            marked_error = mark_ff_replay_economics(
                runtime,
                queue_ids=[queue[0]],
                status="error",
                occurred_at=ACTIVATED_AT,
                error="injected economics failure",
            )
            _assert(marked_error == 1, "economics failure must be persisted once")
            _, replay_error_status = _get_json(
                f"{base_url}{DEFAULT_FF_OVERHEAD_STATUS_PATH}?preview_id={overhead_preview['preview_id']}"
            )
            _assert(replay_error_status["state"] == "replay_error", "applied document with failed economics must be an explicit error, not false pending/green")
            mark_ff_replay_economics(
                runtime,
                queue_ids=[queue[0]],
                status="complete",
                occurred_at=ACTIVATED_AT,
            )
            _, fully_replayed_status = _get_json(
                f"{base_url}{DEFAULT_FF_OVERHEAD_STATUS_PATH}?preview_id={overhead_preview['preview_id']}"
            )
            _assert(fully_replayed_status["state"] == "replay_complete", "final green requires durable economics completion")
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute(
                    "UPDATE sheet_vitrina_v1_warehouse_targeted_recalc_queue "
                    "SET economics_status='',economics_started_at='',economics_finished_at='',economics_error='' "
                    "WHERE queue_id=?",
                    (queue[0],),
                )
                conn.commit()
            legacy_journal = WarehouseUpdateJournal(
                db_path=runtime.db_path,
                timestamp_factory=lambda: "2026-04-18T09:01:00Z",
            )
            legacy_run_id = legacy_journal.start(trigger_source="hourly")
            legacy_journal.phase_started(legacy_run_id, "dependent_replay_economics")
            legacy_journal.phase_finished(
                legacy_run_id,
                "dependent_replay_economics",
                status="success",
            )
            legacy_journal.finish(legacy_run_id, status="success")
            _, legacy_replayed_status = _get_json(
                f"{base_url}{DEFAULT_FF_OVERHEAD_STATUS_PATH}?preview_id={overhead_preview['preview_id']}"
            )
            _assert(
                legacy_replayed_status["state"] == "replay_complete"
                and legacy_replayed_status["replay"]["evidence"] == "warehouse_update_phase:" + legacy_run_id,
                "legacy applied overhead must recover final replay state from a later successful durable economics phase",
            )
            mark_ff_replay_economics(
                runtime,
                queue_ids=[queue[0]],
                status="complete",
                occurred_at=ACTIVATED_AT,
            )
            _assert(
                mark_ff_replay_economics(runtime, queue_ids=[queue[0]], status="complete", occurred_at=ACTIVATED_AT) == 0,
                "repeat economics completion must be a telemetry/queue T0",
            )
            repeat_confirm_code, repeat_confirm = _post_json(
                f"{base_url}{DEFAULT_FF_OVERHEAD_CONFIRM_PATH}",
                {"confirm": True, "preview_id": overhead_preview["preview_id"], "fingerprint": overhead_preview["fingerprint"]},
            )
            _assert(repeat_confirm_code == 200 and repeat_confirm["idempotent"] is True, "overhead exact retry must be T0")
            with sqlite3.connect(runtime.db_path) as conn:
                _assert(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ff_overhead_documents").fetchone()[0] == 1, "overhead retry duplicated document")
                _assert(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE stable_source_id LIKE 'ff_overhead:%'").fetchone()[0] == 1, "overhead retry duplicated replay")
                _assert(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ff_workflow_events WHERE action_type='overhead' AND stage='document_committed'").fetchone()[0] == 1, "overhead confirm retry duplicated commit telemetry")

            documents_code, documents_payload = _get_json(
                f"{base_url}/v1/sheet-vitrina-v1/warehouses/ff/documents?effect=cost_only&reason=overhead&business_date_from=2026-04-18&business_date_to=2026-04-18&search=ffoh&include_technical=false&page=1&limit=25"
            )
            _assert(documents_code == 200, f"FF business registry failed: {documents_payload}")
            _assert(documents_payload["page"]["total_count"] == 1, "all document filters must operate server-side")
            _assert(documents_payload["documents"][0]["document_type_label"] == "Распределение накладных расходов FF", "overhead localization changed")

            source_path = f"{DEFAULT_FF_STOCKS_PATH}/operations/{operation['operation_id']}/file"
            file_code, file_bytes, file_headers = _get_bytes(f"{base_url}{source_path}")
            _assert(file_code == 200, f"source-file route must return 200, got {file_code}")
            _assert(file_bytes == upload_bytes, "source-file download must preserve original XLSX bytes")
            _assert("receipt.xlsx" in file_headers.get("Content-Disposition", ""), "source-file filename missing")

            balance_summary_before_pagination = dict(status_after_payload["registry"]["summary"])
            runtime.save_ff_stock_wb_auto_writeoff_checkpoint(
                checkpoint_id="ffswc_http_smoke",
                created_at=ACTIVATED_AT,
                created_by="smoke",
                reason="http pagination smoke",
            )
            _seed_operation_journal_pagination_fixture(runtime)
            page_1_code, page_1_payload = _get_json(
                f"{base_url}{DEFAULT_FF_STOCKS_PATH}?operations_limit=50&operations_page=1&show_technical_archive=0"
            )
            page_2_code, page_2_payload = _get_json(
                f"{base_url}{DEFAULT_FF_STOCKS_PATH}?operations_limit=50&operations_page=2&show_technical_archive=0"
            )
            _assert(page_1_code == 200 and page_2_code == 200, "paginated status routes must return 200")
            _assert(page_1_payload["operations_page"]["total_count"] >= 60, "paginated status must return total_count")
            _assert(page_1_payload["operations_page"]["has_next"] is True, "first operations page must report has_next")
            _assert(page_2_payload["operations_page"]["current_page"] == 2, "second operations page must be reachable")
            _assert(page_2_payload["operations"], "second operations page must return rows")
            _assert(
                page_1_payload["operations_page"]["hidden_archive_count"] >= 2,
                f"archive-off status must report hidden rows, got {page_1_payload['operations_page']}",
            )
            archive_code, archive_payload = _get_json(
                f"{base_url}{DEFAULT_FF_STOCKS_PATH}?operations_limit=200&operations_page=1&show_technical_archive=1"
            )
            _assert(archive_code == 200, "archive-on status must return 200")
            _assert(
                any(item["source_type"] == FF_STOCK_SOURCE_RUNTIME_REPAIR for item in archive_payload["operations"]),
                "archive-on status must expose runtime_repair rows",
            )
            _assert(
                any(item["source_type"] == FF_STOCK_SOURCE_WB_SUPPLY for item in archive_payload["operations"]),
                "archive-on status must expose old WB auto_writeoff rows",
            )
            _assert(
                page_1_payload["registry"]["summary"] == balance_summary_before_pagination,
                "archive-off pagination must not change FF balance summary",
            )
            _assert_preview_worker_restart_resume(
                runtime,
                inventory_rows=inventory_rows,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    print("ff_stock_ledger_http_smoke: ok")


def _operation_xlsx(rows: list[list[object]]) -> bytes:
    return build_single_sheet_workbook_bytes(
        "Остатки ФФ",
        [["barcode", "nmId", "SKU/название/комментарий", "группа", "количество"], *rows],
    )


def _assert_preview_worker_restart_resume(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    inventory_rows: list[list[object]],
) -> None:
    source = build_single_sheet_workbook_bytes(
        "Инвентаризация FF",
        [
            list(inventory_rows[0]),
            *[
                [row[0], row[1], str(row[2]) + " restart", row[3], row[4]]
                for row in inventory_rows[1:]
            ],
        ],
    )
    inventory = FfInventoryReconciliation(runtime=runtime, timestamp_factory=lambda: ACTIVATED_AT)
    overhead = FfOverheadAllocation(runtime=runtime, timestamp_factory=lambda: ACTIVATED_AT)
    stopped = FfDocumentWorkflow(
        runtime=runtime,
        inventory=inventory,
        overhead=overhead,
        timestamp_factory=lambda: ACTIVATED_AT,
        start_workers=False,
    )
    accepted = stopped.accept_inventory(
        source_bytes=source,
        source_filename="inventory-worker-restart.xlsx",
        business_date="2026-04-18",
        request_id="ffi_http_worker_restart",
        actor="restart-smoke",
    )
    _assert(accepted["state"] == "accepted", "stopped worker must leave a durable accepted job")
    restarted = FfDocumentWorkflow(
        runtime=runtime,
        inventory=inventory,
        overhead=overhead,
        timestamp_factory=lambda: ACTIVATED_AT,
        start_workers=True,
    )
    deadline = time.monotonic() + 10
    status: dict[str, object] = {}
    while time.monotonic() < deadline:
        status = restarted.inventory_status(request_id="ffi_http_worker_restart")
        if status["state"] == "ready":
            break
        time.sleep(0.05)
    _assert(status.get("state") == "ready", f"restart must resume the exact durable preview job: {status}")
    stopped_overhead = FfDocumentWorkflow(
        runtime=runtime,
        inventory=inventory,
        overhead=overhead,
        timestamp_factory=lambda: ACTIVATED_AT,
        start_workers=False,
    )
    accepted_overhead = stopped_overhead.accept_overhead(
        business_date="2026-04-18",
        amount_rub="17.00",
        reason="worker restart overhead",
        request_id="ffo_http_worker_restart",
        actor="restart-smoke",
    )
    _assert(accepted_overhead["state"] == "accepted", "stopped worker must leave durable overhead input")
    resumed_overhead = FfDocumentWorkflow(
        runtime=runtime,
        inventory=inventory,
        overhead=overhead,
        timestamp_factory=lambda: ACTIVATED_AT,
        start_workers=True,
    )
    deadline = time.monotonic() + 10
    overhead_status: dict[str, object] = {}
    while time.monotonic() < deadline:
        overhead_status = resumed_overhead.overhead_status(request_id="ffo_http_worker_restart")
        if overhead_status["state"] == "ready":
            break
        time.sleep(0.05)
    _assert(overhead_status.get("state") == "ready", f"restart must resume overhead planning: {overhead_status}")
    with sqlite3.connect(runtime.db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_inventory_previews WHERE request_id=?",
            ("ffi_http_worker_restart",),
        ).fetchone()[0]
    _assert(count == 1, "restart resume must not duplicate preview rows")


def _inventory_document_counts(
    runtime: RegistryUploadDbBackedRuntime,
) -> tuple[int, int]:
    with sqlite3.connect(runtime.db_path) as conn:
        preview_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_inventory_previews"
            ).fetchone()[0]
        )
        reconciliation_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_inventory_reconciliations"
            ).fetchone()[0]
        )
    return preview_count, reconciliation_count


def _seed_nomenclature(runtime: RegistryUploadDbBackedRuntime, active_nm_ids: list[int]) -> None:
    runtime.save_sku_group(
        {
            "group_key": "clear",
            "label": "Clear",
            "is_active": True,
            "is_system": False,
            "created_at": ACTIVATED_AT,
            "updated_at": ACTIVATED_AT,
        }
    )
    runtime.save_nomenclature_items_atomic(
        [
            {
                "item_id": f"nom_{nm_id}",
                "is_active": True,
                "is_hidden": False,
                "our_sku": f"SKU-{index}",
                "nm_id": nm_id,
                "barcode": f"000460{nm_id}",
                "barcodes": [f"000460{nm_id}", f"990{nm_id}"],
                "nomenclature_name": f"SKU name {index}",
                "product_type": "clear",
                "match_key": f"sku-{index}",
                "comment": f"comment {index}",
                "created_at": ACTIVATED_AT,
                "updated_at": ACTIVATED_AT,
            }
            for index, nm_id in enumerate(active_nm_ids, start=1)
        ]
    )


def _seed_operation_journal_pagination_fixture(runtime: RegistryUploadDbBackedRuntime) -> None:
    for index in range(60):
        runtime.create_ff_stock_operation(
            operation_id=f"ffso_http_page_visible_{index:03d}",
            operation_type=FF_STOCK_OPERATION_MANUAL_RECEIPT,
            source_type=FF_STOCK_SOURCE_MANUAL_EXCEL,
            source_key=f"manual_excel:http-page-visible:{index:03d}",
            source_object_id=f"http-page-visible-{index:03d}",
            source_object_label=f"HTTP pagination visible {index:03d}",
            created_at=f"2026-04-18T09:10:{index:02d}Z",
            created_by="smoke",
            lines=[],
        )
    runtime.create_ff_stock_operation(
        operation_id="ffso_http_page_repair_archive",
        operation_type=FF_STOCK_OPERATION_CORRECTION_RECEIPT,
        source_type=FF_STOCK_SOURCE_RUNTIME_REPAIR,
        source_key="runtime_repair:http-page-archive",
        source_object_id="repair-http-page-archive",
        source_object_label="runtime_repair HTTP archive",
        created_at="2026-04-18T09:59:59Z",
        created_by="smoke",
        diagnostics={"reason": "http pagination smoke"},
        lines=[],
    )
    runtime.create_ff_stock_operation(
        operation_id="ffso_http_page_old_wb_archive",
        operation_type=FF_STOCK_OPERATION_AUTO_WRITEOFF,
        source_type=FF_STOCK_SOURCE_WB_SUPPLY,
        source_key="wb_supply:http-page-old-auto-writeoff",
        source_object_id="http-page-old-auto-writeoff",
        source_object_label="old HTTP WB auto_writeoff",
        created_at="2026-04-18T08:59:59Z",
        created_by="system",
        diagnostics={"cache_key": "http-page-old-auto-writeoff"},
        lines=[],
    )


def _post_json(url: str, payload: object) -> tuple[int, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_multipart(
    url: str,
    file_bytes: bytes,
    *,
    filename: str,
    fields: dict[str, str] | None = None,
) -> tuple[int, object]:
    body, content_type = _multipart_body(
        file_bytes,
        filename=filename,
        fields=fields,
    )
    req = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Accept": "application/json"},
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _multipart_body(
    file_bytes: bytes,
    *,
    filename: str,
    fields: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    boundary = "----ffStockLedgerSmokeBoundary"
    parts: list[bytes] = []
    for key, value in (fields or {}).items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {XLSX_TYPE}\r\n\r\n"
        ).encode("utf-8")
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _post_json_disconnect(port: int, path: str, payload: object) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    _post_disconnect(port, path, body, content_type="application/json")


def _post_multipart_disconnect(
    port: int,
    path: str,
    file_bytes: bytes,
    *,
    filename: str,
    fields: dict[str, str] | None = None,
) -> None:
    body, content_type = _multipart_body(
        file_bytes,
        filename=filename,
        fields=fields,
    )
    _post_disconnect(port, path, body, content_type=content_type)


def _post_disconnect(port: int, path: str, body: bytes, *, content_type: str) -> None:
    request_bytes = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Content-Type: {content_type}\r\n"
        "Accept: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body
    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        connection.sendall(request_bytes)
        connection.shutdown(socket.SHUT_WR)


def _get_json(url: str) -> tuple[int, object]:
    req = urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _poll_workflow(url: str, *, expected: set[str]) -> dict[str, object]:
    deadline = time.monotonic() + 10
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        code, payload = _get_json(url)
        last = dict(payload)
        if code == 200 and str(last.get("state") or "") in expected:
            return last
        if str(last.get("state") or "") in {"blocked", "error"}:
            break
        time.sleep(0.05)
    raise AssertionError(f"workflow did not reach {sorted(expected)}: {last}")


def _get_bytes(url: str) -> tuple[int, bytes, dict[str, str]]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return response.status, response.read(), dict(response.headers.items())
    except error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
