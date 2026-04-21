"""Targeted smoke for one-off historical web-vitrina ready snapshot completion."""

from __future__ import annotations

from datetime import datetime, timezone
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.application.web_vitrina_historical_ready_snapshot_import import (
    HistoricalArtifact,
    HistoricalArtifactRow,
    compare_historical_artifact_against_runtime,
    materialize_historical_ready_snapshots,
)
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)


BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 21, 15, 0, tzinfo=timezone.utc)
STATUS_HEADER = [
    "source_key",
    "kind",
    "freshness",
    "snapshot_date",
    "date",
    "date_from",
    "date_to",
    "requested_count",
    "covered_count",
    "missing_nm_ids",
    "note",
]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="web-vitrina-historical-completion-") as tempdir:
        runtime_dir = Path(tempdir) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        bundle = __import__("json").loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-21T15:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-21T15:05:00Z",
            plan=_build_daily_plan(
                first_nm_id=enabled[0].nm_id,
                second_nm_id=enabled[1].nm_id,
            ),
        )

        artifact = _build_artifact(
            first_nm_id=enabled[0].nm_id,
            second_nm_id=enabled[1].nm_id,
        )
        compare_summary = compare_historical_artifact_against_runtime(
            runtime=runtime,
            artifact=artifact,
            date_from="2026-03-01",
            date_to="2026-03-03",
        )
        if compare_summary["ready_snapshot_coverage"]["missing_count"] != 3:
            raise AssertionError(f"historical compare must report three missing dates, got {compare_summary}")
        if not compare_summary["template_summary"]["same_row_id_set"]:
            raise AssertionError(f"artifact row universe must match DATA_VITRINA template, got {compare_summary}")

        import_summary = materialize_historical_ready_snapshots(
            runtime=runtime,
            artifact=artifact,
            captured_at="2026-04-21T16:00:00Z",
            date_from="2026-03-01",
            date_to="2026-03-03",
            replace_existing=True,
        )
        if import_summary["ready_snapshot_coverage_after"]["missing_count"] != 0:
            raise AssertionError(f"historical import must close the requested window, got {import_summary}")

        imported_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date="2026-03-02")
        if imported_plan.date_columns != ["2026-03-02"]:
            raise AssertionError(f"historical snapshot must materialize a one-date column set, got {imported_plan.date_columns}")
        if [slot.slot_key for slot in imported_plan.temporal_slots] != ["historical_import"]:
            raise AssertionError(f"historical snapshot slot must be historical_import, got {imported_plan.temporal_slots}")
        if imported_plan.source_temporal_policies != {}:
            raise AssertionError(f"historical import must not invent source policy lineage, got {imported_plan.source_temporal_policies}")
        imported_data_sheet = next(sheet for sheet in imported_plan.sheets if sheet.sheet_name == "DATA_VITRINA")
        imported_values = {
            str(row[1]): row[2]
            for row in imported_data_sheet.rows
        }
        if imported_values[f"TOTAL|total_view_count"] != 101:
            raise AssertionError(f"historical import must keep row values for the requested day, got {imported_values}")

        block = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        )
        default_contract = block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
        )
        if default_contract.meta.as_of_date != "2026-04-20":
            raise AssertionError(
                "default no-query read must stay on current daily default as_of_date after historical import, "
                f"got {default_contract.meta.as_of_date}"
            )
        historical_contract = block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            as_of_date="2026-03-02",
        )
        if historical_contract.meta.as_of_date != "2026-03-02":
            raise AssertionError(f"historical read must resolve the imported as_of_date, got {historical_contract.meta.as_of_date}")
        if historical_contract.meta.date_columns != ["2026-03-02"]:
            raise AssertionError(f"historical read must expose a one-date matrix, got {historical_contract.meta.date_columns}")

        print("web_vitrina_historical_compare: ok ->", compare_summary["ready_snapshot_coverage"]["missing_count"], "missing before import")
        print("web_vitrina_historical_import: ok ->", import_summary["saved_snapshot_count"], "saved")
        print("web_vitrina_historical_one_date_snapshot: ok ->", imported_plan.as_of_date, imported_plan.date_columns[0])
        print("web_vitrina_historical_default_daily_mode: ok ->", default_contract.meta.as_of_date)
        print("web_vitrina_historical_read_route: ok ->", historical_contract.meta.as_of_date, historical_contract.meta.row_count)


def _build_artifact(*, first_nm_id: int, second_nm_id: int) -> HistoricalArtifact:
    return HistoricalArtifact(
        artifact_name="web_vitrina_historical_ready_snapshot_artifact",
        artifact_version="v1",
        source_file="/tmp/synthetic.xlsx",
        sheet_name="DATA_VITRINA",
        date_from="2026-03-01",
        date_to="2026-03-03",
        dates=["2026-03-01", "2026-03-02", "2026-03-03"],
        rows=[
            HistoricalArtifactRow(
                row_id="TOTAL|total_view_count",
                label="Показы в воронке",
                values_by_date={"2026-03-01": 100, "2026-03-02": 101, "2026-03-03": 102},
            ),
            HistoricalArtifactRow(
                row_id="TOTAL|total_orderSum",
                label="Сумма заказов",
                values_by_date={"2026-03-01": 1000, "2026-03-02": 1010, "2026-03-03": 1020},
            ),
            HistoricalArtifactRow(
                row_id=f"SKU:{first_nm_id}|avg_price_seller_discounted",
                label="SKU A: Цена продавца",
                values_by_date={"2026-03-01": 990, "2026-03-02": 992, "2026-03-03": 994},
            ),
            HistoricalArtifactRow(
                row_id=f"SKU:{first_nm_id}|avg_addToCartConversion",
                label="SKU A: Конверсия в корзину",
                values_by_date={"2026-03-01": 11.5, "2026-03-02": 11.7, "2026-03-03": 11.9},
            ),
            HistoricalArtifactRow(
                row_id=f"SKU:{second_nm_id}|avg_price_seller_discounted",
                label="SKU B: Цена продавца",
                values_by_date={"2026-03-01": 1090, "2026-03-02": 1092, "2026-03-03": 1094},
            ),
            HistoricalArtifactRow(
                row_id=f"SKU:{second_nm_id}|avg_addToCartConversion",
                label="SKU B: Конверсия в корзину",
                values_by_date={"2026-03-01": 10.5, "2026-03-02": 10.7, "2026-03-03": 10.9},
            ),
        ],
    )


def _build_daily_plan(*, first_nm_id: int, second_nm_id: int) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="web-vitrina-historical-fixture",
        as_of_date="2026-04-20",
        date_columns=["2026-04-20", "2026-04-21"],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="Yesterday closed",
                column_date="2026-04-20",
            ),
            SheetVitrinaV1TemporalSlot(
                slot_key="today_current",
                slot_label="Today current",
                column_date="2026-04-21",
            ),
        ],
        source_temporal_policies={
            "seller_funnel_snapshot": "dual_day_capable",
            "prices_snapshot": "accepted_current_rollover",
        },
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:D7",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", "2026-04-20", "2026-04-21"],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 140, 150],
                    ["Итого: Сумма заказов", "TOTAL|total_orderSum", 1200, 1300],
                    [f"SKU A: Цена продавца", f"SKU:{first_nm_id}|avg_price_seller_discounted", 1110, 1120],
                    [f"SKU A: Конверсия в корзину", f"SKU:{first_nm_id}|avg_addToCartConversion", 13.0, 13.2],
                    [f"SKU B: Цена продавца", f"SKU:{second_nm_id}|avg_price_seller_discounted", 1210, 1220],
                    [f"SKU B: Конверсия в корзину", f"SKU:{second_nm_id}|avg_addToCartConversion", 12.0, 12.2],
                ],
                row_count=6,
                column_count=4,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[
                    [
                        "seller_funnel_snapshot",
                        "success",
                        "fresh",
                        "2026-04-21",
                        "2026-04-21",
                        "2026-04-21",
                        "2026-04-21",
                        2,
                        2,
                        "",
                        "",
                    ]
                ],
                row_count=1,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


if __name__ == "__main__":
    main()
