"""Targeted smoke-check for the server-driven web-vitrina page composition."""

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
from packages.application.web_vitrina_page_composition import (
    build_web_vitrina_page_composition,
    build_web_vitrina_page_error_composition,
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
NOW = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
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
    with TemporaryDirectory(prefix="sheet-vitrina-web-vitrina-page-composition-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-21T12:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        if len(enabled) < 2:
            raise AssertionError("fixture must expose at least two enabled SKU rows")

        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-21T12:05:00Z",
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
        composition = build_web_vitrina_page_composition(
            contract=contract,
            view_model=view_model,
            adapter=adapter,
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            operator_route="/sheet-vitrina-v1/operator",
        )

        if composition["composition_name"] != "web_vitrina_page_composition" or composition["composition_version"] != "v1":
            raise AssertionError(f"page composition identity mismatch, got {composition}")
        if composition["meta"]["current_state"] != "ready":
            raise AssertionError(f"page composition state mismatch, got {composition['meta']}")
        if composition["meta"]["source_adapter_name"] != "web_vitrina_gravity_table_adapter":
            raise AssertionError(f"page composition source chain mismatch, got {composition['meta']}")
        if composition["meta"]["state_namespace"] != "wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1":
            raise AssertionError(f"page composition namespace mismatch, got {composition['meta']}")
        if composition["meta"]["browser_state_persistence"] != "none":
            raise AssertionError(f"browser state persistence mismatch, got {composition['meta']}")

        controls = {item["control_id"]: item for item in composition["filter_surface"]["controls"]}
        for required in ("search", "section", "group", "scope_kind", "metric"):
            if required not in controls:
                raise AssertionError(f"missing filter control {required!r}: {controls}")
        if composition["filter_surface"]["default_sort_value"] != "row_order::asc":
            raise AssertionError(f"default sort mismatch, got {composition['filter_surface']}")
        if not composition["table_surface"]["columns"] or not composition["table_surface"]["rows"]:
            raise AssertionError(f"table surface is empty, got {composition['table_surface']}")
        if composition["status_badge"]["tone"] != "success":
            raise AssertionError(f"status badge mismatch, got {composition['status_badge']}")

        error_payload = build_web_vitrina_page_error_composition(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            operator_route="/sheet-vitrina-v1/operator",
            as_of_date="2026-04-21",
            error_message="sheet_vitrina_v1 ready snapshot missing: fixture",
        )
        if error_payload["meta"]["current_state"] != "error":
            raise AssertionError(f"error composition state mismatch, got {error_payload['meta']}")
        if error_payload["table_surface"]["state_surface"]["current_state"] != "error":
            raise AssertionError(f"error table state mismatch, got {error_payload['table_surface']}")

        print("web_vitrina_page_composition_identity: ok ->", composition["composition_name"], composition["composition_version"])
        print("web_vitrina_page_composition_state: ok ->", composition["meta"]["current_state"], composition["status_badge"]["tone"])
        print("web_vitrina_page_composition_filters: ok ->", ",".join(sorted(controls)))
        print("web_vitrina_page_composition_table: ok ->", len(composition["table_surface"]["columns"]), len(composition["table_surface"]["rows"]))
        print("web_vitrina_page_composition_error: ok ->", error_payload["meta"]["current_state"])


def _build_plan(
    *,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="web-vitrina-page-composition-fixture",
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
