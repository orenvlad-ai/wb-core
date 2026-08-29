"""Smoke-check operator-facing funnel metrics in web-vitrina payloads."""

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
from packages.application.web_vitrina_gravity_table_adapter import build_web_vitrina_gravity_table_adapter
from packages.application.web_vitrina_page_composition import build_web_vitrina_page_composition
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
DATE_COLUMNS = ["2026-04-18", "2026-04-19", "2026-04-20"]
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
    with TemporaryDirectory(prefix="sheet-vitrina-web-vitrina-funnel-metrics-") as tmp:
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
            available_snapshot_dates=runtime.list_sheet_vitrina_ready_snapshot_dates(descending=True),
            selected_as_of_date=None,
            selected_date_from=None,
            selected_date_to=None,
        )

        contract_rows = {row.row_id: row for row in contract.rows}
        for forbidden in ("TOTAL|total_openCount", f"SKU:{enabled[0].nm_id}|openCount"):
            if forbidden in contract_rows:
                raise AssertionError(f"funnel duplicate metric must be hidden from contract: {forbidden}")
        if contract_rows["TOTAL|total_open_card_count"].metric_label != "Открытия карточки в воронке":
            raise AssertionError(f"TOTAL open-card label mismatch: {contract_rows['TOTAL|total_open_card_count']}")
        if contract_rows[f"SKU:{enabled[0].nm_id}|open_card_count"].metric_label != "Открытия карточки в воронке":
            raise AssertionError(f"SKU open-card label mismatch: {contract_rows[f'SKU:{enabled[0].nm_id}|open_card_count']}")
        if contract_rows["TOTAL|total_views_current"].metric_label != "Показы в поиске всего":
            raise AssertionError("search total views label must remain untouched")
        if contract_rows[f"SKU:{enabled[0].nm_id}|views_current"].metric_label != "Показы в поиске":
            raise AssertionError("search views label must remain untouched")

        _assert_values(
            contract_rows["TOTAL|ctr"].values_by_date,
            expected_first=0.2,
            row_id="TOTAL|ctr",
        )
        _assert_values(
            contract_rows[f"SKU:{enabled[0].nm_id}|ctr"].values_by_date,
            expected_first=0.2,
            row_id=f"SKU:{enabled[0].nm_id}|ctr",
        )
        _assert_values(
            contract_rows[f"SKU:{enabled[1].nm_id}|ctr"].values_by_date,
            expected_first=0.5,
            row_id=f"SKU:{enabled[1].nm_id}|ctr",
        )

        table_rows = {row["row_id"]: row for row in composition["table_surface"]["rows"]}
        total_funnel_order = _funnel_metric_labels(composition["table_surface"]["rows"], scope_kind="TOTAL")
        if total_funnel_order != [
            "Показы в воронке",
            "CTR в воронке",
            "Открытия карточки в воронке",
            "Процент выкупа",
        ]:
            raise AssertionError(f"TOTAL funnel order mismatch, got {total_funnel_order}")

        first_sku_order = [
            row["row_id"]
            for row in composition["table_surface"]["rows"]
            if row["row_id"].startswith(f"SKU:{enabled[0].nm_id}|")
            and row["values"]["section"]["value"] == "Воронка"
        ]
        if first_sku_order != [
            f"SKU:{enabled[0].nm_id}|view_count",
            f"SKU:{enabled[0].nm_id}|ctr",
            f"SKU:{enabled[0].nm_id}|open_card_count",
            f"SKU:{enabled[0].nm_id}|buyoutPercent",
        ]:
            raise AssertionError(f"SKU funnel order mismatch, got {first_sku_order}")

        all_funnel_labels = _funnel_metric_labels(composition["table_surface"]["rows"])
        if "Просмотры" in all_funnel_labels or "Открытия карточки" in all_funnel_labels:
            raise AssertionError(f"funnel labels must not expose duplicate/original labels, got {all_funnel_labels}")
        if table_rows[f"SKU:{enabled[0].nm_id}|ctr"]["values"]["date:2026-04-18"]["value"] != 0.2:
            raise AssertionError("CTR must be calculated from open_card_count/view_count, not copied from source ctr")
        for row_id in ("TOTAL|ctr", f"SKU:{enabled[0].nm_id}|ctr"):
            for column_id in ("date:2026-04-19", "date:2026-04-20"):
                cell = table_rows[row_id]["values"][column_id]
                if cell["value"] != "" or cell["display_text"] != "—" or cell["cell_kind"] != "empty":
                    raise AssertionError(f"zero/null denominator must render empty for {row_id} {column_id}: {cell}")
                if str(cell["display_text"]) in {"0%", "Infinity", "NaN"}:
                    raise AssertionError(f"CTR must not render fake numeric text for {row_id} {column_id}: {cell}")

        print("web_vitrina_funnel_duplicate_hidden: ok -> total_openCount/openCount")
        print("web_vitrina_funnel_order: ok ->", " / ".join(total_funnel_order))
        print("web_vitrina_funnel_ctr_values: ok ->", contract_rows["TOTAL|ctr"].values_by_date)
        print("web_vitrina_funnel_denominator_empty: ok -> zero/null")
        print("web_vitrina_search_metrics_untouched: ok -> total_views_current/views_current")


def _assert_values(values_by_date: dict[str, object], *, expected_first: float, row_id: str) -> None:
    if values_by_date["2026-04-18"] != expected_first:
        raise AssertionError(f"{row_id} CTR first day mismatch: {values_by_date}")
    if values_by_date["2026-04-19"] != "" or values_by_date["2026-04-20"] != "":
        raise AssertionError(f"{row_id} zero/null denominator must stay blank: {values_by_date}")


def _funnel_metric_labels(rows: list[dict[str, object]], *, scope_kind: str | None = None) -> list[str]:
    labels: list[str] = []
    for row in rows:
        values = row["values"]  # type: ignore[index]
        if scope_kind is not None and values["scope_kind"]["value"] != scope_kind:  # type: ignore[index]
            continue
        if values["section"]["value"] != "Воронка":  # type: ignore[index]
            continue
        labels.append(str(values["metric_label"]["value"]))  # type: ignore[index]
    return labels


def _build_plan(*, first_nm_id: int, second_nm_id: int) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="web-vitrina-funnel-metrics-fixture",
        as_of_date="2026-04-20",
        date_columns=list(DATE_COLUMNS),
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(slot_key="d1", slot_label="D1", column_date="2026-04-18"),
            SheetVitrinaV1TemporalSlot(slot_key="d2", slot_label="D2", column_date="2026-04-19"),
            SheetVitrinaV1TemporalSlot(slot_key="d3", slot_label="D3", column_date="2026-04-20"),
        ],
        source_temporal_policies={
            "seller_funnel_snapshot": "dual_day_capable",
            "sales_funnel_history": "dual_day_capable",
            "web_source_snapshot": "dual_day_capable",
        },
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:E14",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", *DATE_COLUMNS],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100, 0, None],
                    ["Итого: Открытия карточки", "TOTAL|total_open_card_count", 20, 1, 1],
                    ["Итого: Просмотры", "TOTAL|total_openCount", 20, 1, 1],
                    ["Итого: Показы в поиске всего", "TOTAL|total_views_current", 900, 910, 920],
                    ["SKU A: Показы в воронке", f"SKU:{first_nm_id}|view_count", 50, 0, None],
                    ["SKU A: CTR source should be ignored", f"SKU:{first_nm_id}|ctr", 0.99, 0.99, 0.99],
                    ["SKU A: Открытия карточки", f"SKU:{first_nm_id}|open_card_count", 10, 1, 1],
                    ["SKU A: Просмотры", f"SKU:{first_nm_id}|openCount", 10, 1, 1],
                    ["SKU A: Показы в поиске", f"SKU:{first_nm_id}|views_current", 1000, 1001, 1002],
                    ["SKU B: Показы в воронке", f"SKU:{second_nm_id}|view_count", 40, 0, None],
                    ["SKU B: Открытия карточки", f"SKU:{second_nm_id}|open_card_count", 20, 1, 1],
                    ["SKU B: Просмотры", f"SKU:{second_nm_id}|openCount", 20, 1, 1],
                    ["SKU B: Показы в поиске", f"SKU:{second_nm_id}|views_current", 2000, 2001, 2002],
                ],
                row_count=13,
                column_count=5,
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
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
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
