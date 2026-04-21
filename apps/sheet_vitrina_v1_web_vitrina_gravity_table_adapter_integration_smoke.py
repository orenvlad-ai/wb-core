"""Integration smoke-check for contract -> view_model -> Gravity-table adapter."""

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
from packages.application.web_vitrina_gravity_table_adapter import (
    build_web_vitrina_gravity_table_adapter,
)
from packages.application.web_vitrina_view_model import build_web_vitrina_view_model
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc)
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
    with TemporaryDirectory(prefix="sheet-vitrina-gravity-table-adapter-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-21T10:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        if len(enabled) < 2:
            raise AssertionError("fixture must expose at least two enabled SKU rows")

        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-21T10:05:00Z",
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
        adapter = build_web_vitrina_gravity_table_adapter(view_model)

        if adapter.meta.library_name != "@gravity-ui/table":
            raise AssertionError(f"adapter library identity mismatch, got {adapter.meta}")
        if adapter.meta.source_view_model_name != "web_vitrina_view_model":
            raise AssertionError(f"source view_model mismatch, got {adapter.meta}")
        if adapter.use_table_options.get_row_id_key != "row_id" or adapter.use_table_options.grouping_mode != "flat":
            raise AssertionError(f"useTable options mismatch, got {adapter.use_table_options}")

        row_ids = [row.row_id for row in adapter.rows]
        expected_row_ids = [
            "TOTAL|total_view_count",
            f"GROUP:{enabled[0].group}|view_count",
            f"SKU:{enabled[0].nm_id}|avg_price_seller_discounted",
            f"SKU:{enabled[1].nm_id}|avg_addToCartConversion",
        ]
        if row_ids != expected_row_ids:
            raise AssertionError(f"adapter row order mismatch, got {row_ids}")

        columns = {column.id: column for column in adapter.columns}
        group_row = next(row for row in adapter.rows if row.row_id == f"GROUP:{enabled[0].group}|view_count")
        money_row = next(row for row in adapter.rows if row.row_id == f"SKU:{enabled[0].nm_id}|avg_price_seller_discounted")
        percent_row = next(row for row in adapter.rows if row.row_id == f"SKU:{enabled[1].nm_id}|avg_addToCartConversion")
        if columns["scope_label"].meta.pin != "left":
            raise AssertionError(f"sticky pin mismatch, got {columns['scope_label']}")
        if not (72 <= int(columns["row_order"].size or 0) < 96):
            raise AssertionError(f"row_order width must stay compact, got {columns['row_order']}")
        if not (156 <= int(columns["scope_label"].size or 0) < 280):
            raise AssertionError(f"scope_label width must stay content-driven, got {columns['scope_label']}")
        if not (96 <= int(columns["group"].size or 0) < 160):
            raise AssertionError(f"group width must stay compact, got {columns['group']}")
        if not (104 <= int(columns["date:2026-04-21"].size or 0) <= 120):
            raise AssertionError(f"date column width must stay narrow and readable, got {columns['date:2026-04-21']}")
        if money_row.values["date:2026-04-21"].renderer_id != "renderer:money:money_rub":
            raise AssertionError(f"money renderer mismatch, got {money_row.values['date:2026-04-21']}")
        if percent_row.values["date:2026-04-21"].renderer_id != "renderer:percent:percent_default":
            raise AssertionError(f"percent renderer mismatch, got {percent_row.values['date:2026-04-21']}")
        if group_row.filter_tokens["group"] != [enabled[0].group]:
            raise AssertionError(f"group filter tokens mismatch, got {group_row.filter_tokens}")
        if adapter.state_surface.current_state != "ready":
            raise AssertionError(f"state surface mismatch, got {adapter.state_surface}")

        print("web_vitrina_contract_to_gravity_adapter: ok ->", adapter.meta.snapshot_id)
        print("web_vitrina_gravity_adapter_columns: ok ->", adapter.meta.column_count, columns["scope_label"].meta.pin)
        print("web_vitrina_gravity_adapter_rows: ok ->", row_ids[-2], row_ids[-1])
        print("web_vitrina_gravity_adapter_state: ok ->", adapter.state_surface.current_state)


def _build_plan(
    *,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="web-vitrina-gravity-adapter-integration",
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
                    [f"SKU B: Конверсия в корзину", f"SKU:{second_nm_id}|avg_addToCartConversion", 11.5, 13.0],
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
