"""Isolated quantity-invariant, allocation, replay and storno checks for FF overhead."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_overhead_allocation import (  # noqa: E402
    FfOverheadAllocation,
    FfOverheadAllocationError,
)
from packages.application.ff_warehouse_documents import FfWarehouseDocumentView  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_functional import (  # noqa: E402
    WarehouseFunctionalError,
    _ff_cost_adjusted_state,
    _ff_ledger_line_cost_adjustment,
    ensure_warehouse_functional_schema,
)


NOW = "2026-08-02T09:00:00Z"
BUSINESS_DATE = "2026-07-31"


def main() -> None:
    with TemporaryDirectory(prefix="ff-overhead-allocation-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_nomenclature(runtime)
        _seed_opening(runtime)
        _seed_active_cost(runtime)
        block = FfOverheadAllocation(runtime=runtime, timestamp_factory=lambda: NOW)

        preview = block.build_plan(
            business_date=BUSINESS_DATE,
            amount_rub="10.00",
            reason="Аренда зоны приёмки FF",
        )
        assert preview["status"] == "ready" and preview["apply_allowed"] is True
        manifest = preview["manifest"]
        assert manifest["denominator_quantity"] == "4"
        allocations = {int(item["nm_id"]): item for item in manifest["allocations"]}
        assert allocations[101]["allocation_rub"] == "7.5"
        assert allocations[102]["allocation_rub"] == "2.5"
        assert sum(Decimal(item["allocation_rub"]) for item in allocations.values()) == Decimal("10")
        tiny = block.build_plan(
            business_date=BUSINESS_DATE,
            amount_rub="0.01",
            reason="Проверка deterministic remainder",
        )
        assert sum(
            Decimal(item["allocation_rub"])
            for item in tiny["manifest"]["allocations"]
        ) == Decimal("0.01")
        assert len(tiny["manifest"]["affected_nm_ids"]) == 1
        stale_preview = block.build_plan(
            business_date=BUSINESS_DATE,
            amount_rub="11.00",
            reason="Предварительный конфликт revision",
        )

        before = _physical(runtime)
        applied = block.apply_plan(
            business_date=BUSINESS_DATE,
            amount_rub="10.00",
            reason="Аренда зоны приёмки FF",
            confirmation_fingerprint=preview["fingerprint"],
            created_by="smoke",
        )
        assert applied["status"] == "applied" and applied["readback"]["physical_quantity_unchanged"] is True
        assert _physical(runtime) == before
        try:
            block.apply_plan(
                business_date=BUSINESS_DATE,
                amount_rub="11.00",
                reason="Предварительный конфликт revision",
                confirmation_fingerprint=stale_preview["fingerprint"],
                created_by="smoke",
            )
        except FfOverheadAllocationError as exc:
            assert exc.code == "stale_or_invalid_fingerprint"
        else:
            raise AssertionError("stale overhead preview must fail closed")

        with sqlite3.connect(runtime.db_path) as conn:
            rows = conn.execute(
                """
                SELECT line.nm_id,line.quantity_delta,line.raw_json
                FROM sheet_vitrina_v1_ff_stock_operation_lines line
                WHERE line.operation_id=? ORDER BY line.nm_id
                """,
                (applied["operation_id"],),
            ).fetchall()
        assert [int(row[1]) for row in rows] == [0, 0]
        parsed = [_ff_ledger_line_cost_adjustment({"quantity_delta": row[1], "raw_json": row[2]}) for row in rows]
        assert [item["capital_delta_rub"] for item in parsed] == [Decimal("7.5"), Decimal("2.5")]

        capital_101, wac_101 = _ff_cost_adjusted_state(
            current_quantity=Decimal("3"),
            current_capital=Decimal("300"),
            adjustment=parsed[0],
            operation_id=applied["operation_id"],
            nm_id=101,
        )
        assert capital_101 == Decimal("307.5") and wac_101 == Decimal("102.5")
        assert Decimal("3") == Decimal(allocations[101]["physical_quantity"])

        repeated = block.apply_plan(
            business_date=BUSINESS_DATE,
            amount_rub="10.00",
            reason="Аренда зоны приёмки FF",
            confirmation_fingerprint=preview["fingerprint"],
            created_by="smoke",
        )
        assert repeated["idempotent"] is True

        page = FfWarehouseDocumentView(db_path=runtime.db_path).page(
            effect="cost_only",
            reason="overhead",
        )
        assert page["page"]["total_count"] == 1
        assert page["documents"][0]["document_type_label"] == "Распределение накладных расходов FF"
        assert page["documents"][0]["total_quantity"] == "0"
        assert page["documents"][0]["total_expense_rub"] == "10"

        reversal_preview = block.build_reversal_plan(
            document_id=applied["document_id"],
            reason="Исправление документа",
        )
        reversed_result = block.apply_reversal(
            document_id=applied["document_id"],
            reason="Исправление документа",
            confirmation_fingerprint=reversal_preview["fingerprint"],
            created_by="smoke",
        )
        assert reversed_result["status"] == "reversed"
        assert _physical(runtime) == before
        with sqlite3.connect(runtime.db_path) as conn:
            reversal_rows = conn.execute(
                "SELECT raw_json FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=? ORDER BY nm_id",
                (reversed_result["reversal_operation_id"],),
            ).fetchall()
        reversal_allocations = [
            Decimal(json.loads(row[0])["cost_adjustment"]["capital_delta_rub"])
            for row in reversal_rows
        ]
        assert reversal_allocations == [Decimal("-7.5"), Decimal("-2.5")]
        correction = FfWarehouseDocumentView(db_path=runtime.db_path).page(
            effect="cost_only",
            reason="correction",
        )["documents"][0]
        assert "ffop:" + applied["operation_id"] in correction["linked_document_ids"]
        restored_capital, restored_wac = _ff_cost_adjusted_state(
            current_quantity=Decimal("3"),
            current_capital=capital_101,
            adjustment={"allocation_basis_quantity": Decimal("3"), "capital_delta_rub": Decimal("-7.5")},
            operation_id=reversed_result["reversal_operation_id"],
            nm_id=101,
        )
        assert restored_capital == Decimal("300") and restored_wac == Decimal("100")
        assert block.apply_reversal(
            document_id=applied["document_id"],
            reason="Исправление документа",
            confirmation_fingerprint=reversal_preview["fingerprint"],
            created_by="smoke",
        )["idempotent"] is True

        try:
            _ff_cost_adjusted_state(
                current_quantity=Decimal("2"),
                current_capital=Decimal("200"),
                adjustment=parsed[0],
                operation_id="stale-fixture",
                nm_id=101,
            )
        except WarehouseFunctionalError as exc:
            assert "basis changed" in str(exc)
        else:
            raise AssertionError("stale replay basis must fail closed")

    with TemporaryDirectory(prefix="ff-overhead-empty-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_nomenclature(runtime)
        block = FfOverheadAllocation(runtime=runtime, timestamp_factory=lambda: NOW)
        try:
            block.build_plan(
                business_date=BUSINESS_DATE,
                amount_rub="1",
                reason="Нет остатков",
            )
        except FfOverheadAllocationError as exc:
            assert exc.code == "positive_denominator_missing"
        else:
            raise AssertionError("overhead without positive denominator must be blocked")

    print("ff_overhead_allocation_smoke: OK")


def _seed_nomenclature(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_sku_group(
        {"group_key": "overhead-smoke", "label": "Overhead smoke", "is_active": True, "is_system": False, "created_at": NOW, "updated_at": NOW}
    )
    runtime.save_nomenclature_items_atomic(
        [
            {
                "item_id": f"overhead-{nm_id}",
                "is_active": True,
                "is_hidden": False,
                "our_sku": f"OH-{nm_id}",
                "nm_id": nm_id,
                "barcode": f"460{nm_id}",
                "barcodes": [f"460{nm_id}"],
                "nomenclature_name": f"Overhead SKU {nm_id}",
                "product_type": "overhead-smoke",
                "match_key": f"overhead-{nm_id}",
                "comment": "",
                "created_at": NOW,
                "updated_at": NOW,
            }
            for nm_id in (101, 102)
        ]
    )


def _seed_opening(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.create_ff_stock_operation(
        operation_id="ffso-overhead-opening",
        operation_type="manual_receipt",
        source_type="manual_excel",
        source_key="overhead-smoke:opening",
        source_object_id="overhead-smoke",
        source_object_label="Overhead smoke opening",
        created_at="2026-07-30T09:00:00Z",
        business_effective_date="2026-07-30",
        created_by="smoke",
        warnings=[],
        diagnostics={"opening": True},
        lines=[{"nm_id": 101, "quantity_delta": 3}, {"nm_id": 102, "quantity_delta": 1}],
    )


def _seed_active_cost(runtime: RegistryUploadDbBackedRuntime) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                version_id,cutover_id,version_kind,effective_at,status,
                plan_fingerprint,local_source_digest,source_watermarks_json,
                created_at,business_effective_date,published_at
            ) VALUES('overhead-cost-v1','warehouse_functional_cutover_v1','fixture',
                     '2026-07-31T08:00:00Z','good','sha256:overhead-cost-v1',
                     'sha256:overhead-source','{}','2026-07-31T08:00:00Z',
                     '2026-07-31','2026-07-31T08:00:00Z')
            """
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_warehouse_functional_active(slot,version_id,updated_at) VALUES(1,'overhead-cost-v1',?)",
            (NOW,),
        )
        for nm_id, quantity in ((101, 3), (102, 1)):
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                    version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                    cost_covered_quantity,quality,certified,wb_quantity,
                    wb_in_way_to_client,wb_in_way_from_client,provenance_json
                ) VALUES('overhead-cost-v1','ff',?,?,'100',?,?,'certified',1,'0','0','0','{}')
                """,
                (nm_id, str(quantity), str(quantity * 100), str(quantity)),
            )
        conn.commit()


def _physical(runtime: RegistryUploadDbBackedRuntime) -> dict[int, int]:
    with sqlite3.connect(runtime.db_path) as conn:
        return {
            int(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT nm_id,SUM(quantity_delta) FROM sheet_vitrina_v1_ff_stock_operation_lines GROUP BY nm_id ORDER BY nm_id"
            ).fetchall()
        }


if __name__ == "__main__":
    main()
