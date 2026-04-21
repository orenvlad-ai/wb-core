"""Integration smoke-check for the web_vitrina_contract -> view_model seam."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.application.web_vitrina_view_model import build_web_vitrina_view_model
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 21, 8, 0, tzinfo=timezone.utc)
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
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-web-vitrina-view-model-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-21T08:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        if len(enabled) < 2:
            raise AssertionError("fixture must expose at least two enabled SKU rows")

        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-21T08:05:00Z",
            plan=_build_plan(
                first_nm_id=enabled[0].nm_id,
                second_nm_id=enabled[1].nm_id,
                first_group=enabled[0].group,
            ),
        )

        contract = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
        )
        view_model = build_web_vitrina_view_model(contract)

        if view_model.meta.source_contract_name != "web_vitrina_contract":
            raise AssertionError(f"source contract identity mismatch, got {view_model.meta}")
        if view_model.meta.snapshot_id != "web-vitrina-view-model-integration" or view_model.meta.row_count != 4:
            raise AssertionError(f"view_model meta mismatch, got {view_model.meta}")
        if len(view_model.columns) != 11 or len(view_model.groups) != 2 or len(view_model.sections) != 2:
            raise AssertionError(f"view_model schema counts mismatch, got {view_model}")

        rows = {row.row_id: row for row in view_model.rows}
        money_row = rows[f"SKU:{enabled[0].nm_id}|avg_price_seller_discounted"]
        percent_row = rows[f"SKU:{enabled[1].nm_id}|avg_addToCartConversion"]
        total_row = rows["TOTAL|total_view_count"]
        if total_row.group_id != "group:overview" or total_row.row_kind != "total":
            raise AssertionError(f"TOTAL mapping mismatch, got {total_row}")
        if _cell(money_row, "date:2026-04-20").cell_kind != "money":
            raise AssertionError(f"money mapping mismatch, got {money_row}")
        if _cell(percent_row, "date:2026-04-20").cell_kind != "percent":
            raise AssertionError(f"percent mapping mismatch, got {percent_row}")
        if money_row.filter_tokens["group"] != [enabled[0].group]:
            raise AssertionError(f"group filter tokens mismatch, got {money_row.filter_tokens}")
        if view_model.state_model.current_state != "ready":
            raise AssertionError(f"state model mismatch, got {view_model.state_model}")

        print("web_vitrina_contract_to_view_model: ok ->", view_model.meta.snapshot_id)
        print("web_vitrina_view_model_counts: ok ->", view_model.meta.column_count, view_model.meta.group_count, view_model.meta.section_count)
        print("web_vitrina_view_model_cell_kinds: ok ->", _cell(money_row, "date:2026-04-20").cell_kind, _cell(percent_row, "date:2026-04-20").cell_kind)
        print("web_vitrina_view_model_state: ok ->", view_model.state_model.current_state)


def _cell(row: object, column_id: str) -> object:
    return next(cell for cell in row.cells if cell.column_id == column_id)


def _build_plan(
    *,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="web-vitrina-view-model-integration",
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
            "cost_price": "manual_overlay",
        },
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:D5",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", "2026-04-20", "2026-04-21"],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100, 140],
                    [f"Группа {first_group}: Показы в воронке", f"GROUP:{first_group}|view_count", 40, 55],
                    [f"SKU A: Цена продавца", f"SKU:{first_nm_id}|avg_price_seller_discounted", 990, 1110],
                    [f"SKU B: Конверсия в корзину", f"SKU:{second_nm_id}|avg_addToCartConversion", 0.115, 0.13],
                ],
                row_count=4,
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
