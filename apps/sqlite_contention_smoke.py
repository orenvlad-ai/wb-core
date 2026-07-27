#!/usr/bin/env python3
"""Deterministic concurrency contract for interactive SQLite writers."""

from __future__ import annotations

from http import HTTPStatus
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import _write_json_response
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
    registry_runtime_sqlite_busy_timeout,
)
from packages.application.supplier_financial_documents import (
    SupplierFinancialDocumentsBlock,
)
from packages.application.sqlite_contention import (
    SQLiteContentionExhausted,
    connect_sqlite,
    sqlite_operation_context,
)
from packages.application.wb_autoanswers_runtime import AutoanswersRepository


STAMP = "2026-07-26T08:00:00Z"


class _ResponseProbe:
    def __init__(self) -> None:
        self.wfile = BytesIO()
        self.path = "/v1/sheet-vitrina-v1/supply/wb-regional/calculate"
        self.command = "POST"
        self.status = 0
        self.headers: dict[str, str] = {}
        self._sqlite_request_started_at = time.monotonic()

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.headers[name] = value

    def end_headers(self) -> None:
        return


def _seed_shipment(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_supplier_shipment(
        header={
            "shipment_id": "contention-shipment",
            "created_at": STAMP,
            "updated_at": STAMP,
            "shipment_date": "2026-07-26",
            "invoice_no": "CONTENTION-1",
            "currency": "CNY",
        },
        lines=[],
    )


def _bank_confirm_write(runtime: RegistryUploadDbBackedRuntime) -> None:
    operation_id = "bankop_contention_atomic"
    runtime.save_supplier_financial_document(
        document={
            "document_id": "fdoc_contention",
            "supplier_order_id": "contention-shipment",
            "document_type": "bank_fee_statement",
            "original_filename": "contention.pdf",
            "stored_file_path": "supplier_financial_sources/contention.pdf",
            "file_content_type": "application/pdf",
            "file_sha256": "b" * 64,
            "uploaded_at": STAMP,
            "updated_at": STAMP,
            "parse_status": "confirmed",
            "normalized_parse": {},
            "raw_parse": {},
            "parser_version": "contention-fixture-v1",
        },
        expense_lines=[
            {
                "line_id": "feline_contention",
                "sort_order": 1,
                "category": "bank_transfer_fee",
                "amount": 100,
                "currency": "RUB",
                "amount_rub": 100,
                "status": "parsed",
                "raw": {
                    "semantic_operation_id": operation_id,
                    "logical_fee_id": "bankfee_contention",
                },
            }
        ],
        bank_operation_assignments=[
            {
                "semantic_operation_id": operation_id,
                "logical_fee_id": "bankfee_contention",
            }
        ],
    )


def _run_parallel_wait_contract(runtime: RegistryUploadDbBackedRuntime) -> None:
    financial_documents = SupplierFinancialDocumentsBlock(
        runtime=runtime,
        timestamp_factory=lambda: STAMP,
        pdf_text_extractor=lambda _body, _filename: (
            "Счёт на оплату № 1 от 26.07.2026\nИтого: 100,00 RUB",
            {"method": "contention_fixture"},
            [],
        ),
    )
    blocker = sqlite3.connect(runtime.db_path, timeout=1, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    outcomes: dict[str, str] = {}
    failures: dict[str, str] = {}

    def run(name: str, action: object) -> None:
        try:
            with sqlite_operation_context(
                endpoint=name,
                operation="POST",
                priority="interactive",
                owner="contention-smoke",
            ):
                action()  # type: ignore[operator]
            outcomes[name] = "ok"
        except Exception as exc:  # pragma: no cover - reported below
            failures[name] = f"{type(exc).__name__}:{exc}"

    actions = {
        "wb-regional/calculate": lambda: runtime.save_wb_regional_supply_result_state(
            calculated_at=STAMP,
            payload={
                "status": "success",
                "calculation_id": "regional-contention",
                "calculated_at": STAMP,
                "report_date": "2026-07-26",
                "settings": {},
                "summary": {},
                "districts": [],
            },
        ),
        "factory-order/calculate": lambda: runtime.save_factory_order_result_state(
            calculated_at=STAMP,
            payload={
                "status": "success",
                "calculation_id": "factory-contention",
                "calculated_at": STAMP,
                "report_date": "2026-07-26",
            },
        ),
        "supplier-financial/confirm": lambda: _bank_confirm_write(runtime),
        "settings/document-write": lambda: runtime.save_nomenclature_item(
            {
                "item_id": "contention-setting",
                "is_active": True,
                "is_hidden": False,
                "nm_id": 260726,
                "nomenclature_name": "Contention fixture",
                "created_at": STAMP,
                "updated_at": STAMP,
            }
        ),
    }
    threads = [
        threading.Thread(target=run, args=(name, action), daemon=True)
        for name, action in actions.items()
    ]
    for thread in threads:
        thread.start()

    preview_started = time.monotonic()
    preview = financial_documents.preview_document_upload(
        "contention-shipment",
        file_bytes=b"%PDF-1.4\ncontention fixture\n",
        uploaded_filename="contention-invoice.pdf",
        uploaded_content_type="application/pdf",
    )
    if time.monotonic() - preview_started >= 2:
        raise AssertionError(
            "supplier upload/parse/durable preview was blocked by the main SQLite writer"
        )
    token = str(preview.get("confirmation_token") or "")
    durable_preview = runtime.load_supplier_confirmation_preview(token)
    staging_path = runtime.runtime_dir / str(
        (durable_preview or {}).get("payload", {}).get("staging_path") or ""
    )
    if not token or durable_preview is None or not staging_path.is_file():
        raise AssertionError("supplier upload/parse/durable preview readback failed")

    autoanswers = AutoanswersRepository(runtime_dir=runtime.runtime_dir, env={})
    isolated_started = time.monotonic()
    autoanswers.update_settings(
        master_enabled=False,
        actor_id="contention-smoke",
    )
    if time.monotonic() - isolated_started >= 2:
        raise AssertionError("isolated autoanswers store was blocked by main SQLite")

    # This exceeds the former five-second wait and proves bounded recovery.
    time.sleep(6.2)
    blocker.commit()
    blocker.close()
    for thread in threads:
        thread.join(timeout=35)
    if any(thread.is_alive() for thread in threads):
        raise AssertionError("interactive writer exceeded the bounded wait")
    if failures or set(outcomes) != set(actions):
        raise AssertionError(
            f"parallel writers failed after recoverable contention: {failures}"
        )
    document = runtime.load_supplier_financial_document(
        supplier_order_id="contention-shipment",
        document_id="fdoc_contention",
    )
    if document is None or len(document.get("expense_lines") or []) != 1:
        raise AssertionError("bank confirm did not commit its document and row atomically")
    if len(runtime.list_supplier_bank_operation_assignments()) != 1:
        raise AssertionError("bank operation assignment readback is incomplete")
    autoanswers_blocker = sqlite3.connect(
        autoanswers.db_path,
        timeout=1,
        isolation_level=None,
    )
    autoanswers_blocker.execute("BEGIN IMMEDIATE")
    main_write_started = time.monotonic()
    runtime.save_factory_order_result_state(
        calculated_at=STAMP,
        payload={
            "status": "success",
            "calculation_id": "factory-during-autoanswers-writer",
            "calculated_at": STAMP,
            "report_date": "2026-07-26",
        },
    )
    if time.monotonic() - main_write_started >= 2:
        raise AssertionError("autoanswers writer blocked the main runtime store")
    autoanswers_blocker.rollback()
    autoanswers_blocker.close()
    runtime.save_supplier_shipment(
        header={
            "shipment_id": "contention-shipment-other",
            "created_at": STAMP,
            "updated_at": STAMP,
            "shipment_date": "2026-07-26",
            "invoice_no": "CONTENTION-2",
            "currency": "CNY",
        },
        lines=[],
    )
    try:
        runtime.save_supplier_financial_document(
            document={
                **dict(document),
                "document_id": "fdoc_contention_conflict",
                "supplier_order_id": "contention-shipment-other",
                "updated_at": STAMP,
            },
            expense_lines=[
                {
                    **dict(document["expense_lines"][0]),
                    "line_id": "feline_contention_conflict",
                    "supplier_order_id": "contention-shipment-other",
                    "financial_document_id": "fdoc_contention_conflict",
                }
            ],
            bank_operation_assignments=[
                {
                    "semantic_operation_id": "bankop_contention_atomic",
                    "logical_fee_id": "bankfee_contention",
                }
            ],
        )
    except ValueError as exc:
        if "другой поставке" not in str(exc):
            raise
    else:
        raise AssertionError("one atomic bank row was assigned to two shipments")
    if runtime.load_supplier_financial_document(
        supplier_order_id="contention-shipment-other",
        document_id="fdoc_contention_conflict",
    ):
        raise AssertionError("assignment conflict left a partial financial document")


def _run_controlled_exhaustion_contract(runtime_dir: Path) -> None:
    path = runtime_dir / "controlled-contention.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE values_table(value TEXT NOT NULL)")
    blocker = sqlite3.connect(path, timeout=1, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO values_table(value) VALUES('owner')")
    failure: SQLiteContentionExhausted | None = None
    with sqlite_operation_context(
        endpoint="/v1/sheet-vitrina-v1/supply/wb-regional/calculate",
        operation="POST",
        priority="interactive",
        owner="contention-smoke",
    ):
        conn = connect_sqlite(path, timeout_ms=600, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
        except SQLiteContentionExhausted as exc:
            failure = exc
        finally:
            conn.close()
        if failure is None:
            raise AssertionError("bounded contention exhaustion was not raised")
        response = _ResponseProbe()
        _write_json_response(
            response,  # type: ignore[arg-type]
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {"error": str(failure)},
        )
    blocker.rollback()
    blocker.close()
    body = json.loads(response.wfile.getvalue())
    if (
        response.status != HTTPStatus.SERVICE_UNAVAILABLE
        or body.get("code") != "sqlite_write_busy"
        or body.get("retryable") is not True
        or int(body.get("retry_count") or 0) < 1
        or "database is locked" in json.dumps(body).lower()
        or "Повторите действие" not in str(body.get("message") or "")
        or response.headers.get("Retry-After") != "2"
    ):
        raise AssertionError(f"controlled retry response changed: {body}")


def _run_executemany_atomic_retry_contract(runtime_dir: Path) -> None:
    path = runtime_dir / "executemany-contention.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE values_table(value TEXT PRIMARY KEY)")
    first_written = threading.Event()
    blocker_acquired = threading.Event()

    def hold_between_parameter_sets() -> None:
        if not first_written.wait(timeout=2):
            return
        with sqlite3.connect(path, timeout=1, isolation_level=None) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            blocker_acquired.set()
            time.sleep(0.6)
            blocker.commit()

    blocker_thread = threading.Thread(
        target=hold_between_parameter_sets,
        daemon=True,
    )
    blocker_thread.start()

    def parameter_sets() -> object:
        yield ("first",)
        first_written.set()
        if not blocker_acquired.wait(timeout=2):
            raise AssertionError("executemany blocker did not acquire its lock")
        yield ("second",)

    with connect_sqlite(path, timeout_ms=2_000, isolation_level=None) as conn:
        conn.executemany(
            "INSERT INTO values_table(value) VALUES(?)",
            parameter_sets(),
        )
    blocker_thread.join(timeout=2)
    with sqlite3.connect(path) as conn:
        values = [
            str(row[0])
            for row in conn.execute(
                "SELECT value FROM values_table ORDER BY value"
            ).fetchall()
        ]
    if values != ["first", "second"]:
        raise AssertionError(
            f"executemany retry duplicated or lost a parameter set: {values}"
        )


def main() -> None:
    with TemporaryDirectory(prefix="sqlite-contention-") as directory:
        (Path(directory) / "runtime").mkdir()
        runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(directory) / "runtime"
        )
        runtime.list_supplier_shipments()
        _seed_shipment(runtime)
        _run_parallel_wait_contract(runtime)
        _run_controlled_exhaustion_contract(runtime.runtime_dir)
        _run_executemany_atomic_retry_contract(runtime.runtime_dir)
        with registry_runtime_sqlite_busy_timeout(120_000):
            runtime.list_supplier_shipments()
    print("sqlite_contention_smoke: OK")


if __name__ == "__main__":
    main()
