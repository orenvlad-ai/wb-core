"""Targeted integrity smoke for factory-order XLSX generation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.factory_order_supply import FactoryOrderSupplyBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.contracts.factory_order_supply import (
    DATASET_INBOUND_FACTORY_TO_FF,
    DATASET_INBOUND_FF_TO_WB,
    DATASET_STOCK_FF,
)

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
ACTIVATED_AT = "2026-04-18T09:00:00Z"
EXPECTED_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "docProps/app.xml",
    "docProps/core.xml",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
    "xl/styles.xml",
    "xl/theme/theme1.xml",
    "xl/worksheets/sheet1.xml",
}


class FakeStocksBlock:
    def __init__(self, nm_ids: list[int]) -> None:
        self.nm_ids = list(nm_ids)

    def execute(self, request_obj: object) -> SimpleNamespace:
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                items=[SimpleNamespace(nm_id=nm_id, stock_total=0.0) for nm_id in self.nm_ids],
            )
        )


class FakeSalesHistoryBlock:
    def execute(self, request_obj: object) -> SimpleNamespace:
        del request_obj
        return SimpleNamespace(result=SimpleNamespace(kind="success", items=[]))


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="factory-order-xlsx-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        active_nm_ids = [item.nm_id for item in runtime.load_current_state().config_v2 if item.enabled]
        block = FactoryOrderSupplyBlock(
            runtime=runtime,
            stocks_block=FakeStocksBlock(active_nm_ids),
            sales_funnel_history_block=FakeSalesHistoryBlock(),
            now_factory=lambda: NOW,
            timestamp_factory=lambda: ACTIVATED_AT,
        )

        stock_template, _ = block.build_template(DATASET_STOCK_FF)
        inbound_factory_template, _ = block.build_template(DATASET_INBOUND_FACTORY_TO_FF)
        inbound_ff_to_wb_template, _ = block.build_template(DATASET_INBOUND_FF_TO_WB)

        stock_rows = read_first_sheet_rows(stock_template)
        for row in stock_rows[1:]:
            row[2] = 0
        stock_rows[1][2] = 12
        block.upload_dataset(
            DATASET_STOCK_FF,
            build_single_sheet_workbook_bytes("Остатки ФФ", stock_rows),
            uploaded_filename="stock.xlsx",
        )
        result = block.calculate(
            {
                "prod_lead_time_days": 3,
                "lead_time_factory_to_ff_days": 2,
                "lead_time_ff_to_wb_days": 1,
                "safety_days_mp": 0,
                "safety_days_ff": 0,
                "order_batch_qty": 10,
                "report_date_override": "2026-04-18",
                "sales_avg_period_days": 1,
            }
        )
        recommendation_xlsx, _ = block.download_recommendation()

        for label, workbook_bytes in [
            ("stock_ff_template", stock_template),
            ("inbound_factory_template", inbound_factory_template),
            ("inbound_ff_to_wb_template", inbound_ff_to_wb_template),
            ("recommendation", recommendation_xlsx),
        ]:
            _assert_workbook_integrity(label, workbook_bytes)

        if result.summary.total_qty != 0:
            raise AssertionError("integrity smoke must keep the recommendation workbook readable after calculation")

        print("xlsx_integrity_stock_ff_template: ok")
        print("xlsx_integrity_inbound_factory_template: ok")
        print("xlsx_integrity_inbound_ff_to_wb_template: ok")
        print("xlsx_integrity_recommendation: ok")


def _assert_workbook_integrity(label: str, workbook_bytes: bytes) -> None:
    with ZipFile(__import__("io").BytesIO(workbook_bytes), "r") as archive:
        parts = set(archive.namelist())
        missing = sorted(EXPECTED_PARTS - parts)
        if missing:
            raise AssertionError(f"{label} is missing OOXML parts: {missing}")
        workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
        rels_xml = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        content_types_xml = archive.read("[Content_Types].xml").decode("utf-8")
        if "theme/theme1.xml" not in rels_xml:
            raise AssertionError(f"{label} workbook rels must include theme relationship")
        if "/xl/theme/theme1.xml" not in content_types_xml:
            raise AssertionError(f"{label} content types must include theme override")
        if "<calcPr" not in workbook_xml:
            raise AssertionError(f"{label} workbook must include calcPr for Excel-friendly open")
        if "<sheetViews>" not in archive.read("xl/worksheets/sheet1.xml").decode("utf-8"):
            raise AssertionError(f"{label} worksheet must include sheetViews for Excel-friendly open")
        try:
            from openpyxl import load_workbook

            workbook = load_workbook(filename=__import__("io").BytesIO(workbook_bytes), read_only=True, data_only=False)
            workbook.close()
        except Exception as exc:  # pragma: no cover - optional local aid
            raise AssertionError(f"{label} must stay readable for a standard XLSX reader: {exc}") from exc


if __name__ == "__main__":
    main()
