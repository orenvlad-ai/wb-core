"""A one-row window never materializes a >2,000-row ready contract."""

from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ready_publication_fixture import save_ready_fixture
from apps.sheet_vitrina_v1_web_vitrina_page_composition_smoke import BUNDLE_FIXTURE, _build_plan
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application import sheet_vitrina_v1_web_vitrina as vitrina
from packages.application.web_vitrina_window_read_context import window_read_context
from packages.application.web_vitrina_management_history import (
    corrected_proxy_dates, recalculate_corrected_unit_margin_rows,
)
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow
from packages.application.calculation_parameters_v4 import ensure_proxy_v4_schema
from apps.web_vitrina_management_inventory_history_smoke import ManagementInventoryTests
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.web_vitrina_official_fbs import (
    apply_current_official_fbs_estimate, build_current_official_fbs_estimate,
)
from apps.web_vitrina_official_fbs_smoke import fixture as official_fbs_fixture
from packages.application.fbs_accounting_runtime import _save_book
from packages.application.web_vitrina_window_v3 import (
    WindowV3Service, _book_material, _catalog_surface, _digest, _input_material,
)
from packages.application.calculation_parameters import DEFAULT_PROXY_PARAMETERS
from packages.application.vitrina_economics import METRICS as CURRENT_PROXY_METRICS
from packages.application.sheet_vitrina_v1_proxy_v4 import PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY


DAY = "2026-04-20"


def mixed_canonical_legacy_proxy_dates() -> None:
    """Compact canonical evidence outside the recursive legacy range terminates."""
    canonical, legacy = "2026-09-30", "2026-10-01"
    metric = SimpleNamespace(label_ru="Маржа", section="Экономика", format="rub")
    rows = vitrina._include_proxy_v4_unit_margin_rows(
        [], runtime=SimpleNamespace(load_our_wb_cost_daily_state=lambda as_of_date: {}),
        date_columns=[canonical, legacy], enabled_config=[], sku_metric=metric,
        total_metric=metric, parameters_for_date=lambda day: None,
        window_operands={f"SKU:1|{PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY}": {
            "presentation_by_date": {canonical: {
                "calculation_contract": "catalog_economics_v1",
                "source_as_of_date": canonical,
            }},
        }},
    )
    assert len(rows) == 1 and rows[0].values_by_date == {legacy: ""}


def corrected_hidden_sku_parity() -> None:
    corrected = {"calculation_contract": "dated_proxy_recalculation_v1", "source": "fixture"}

    def row(row_id: str, value: object, cell: dict | None = None) -> WebVitrinaContractRow:
        scope, metric = row_id.split("|", 1)
        return WebVitrinaContractRow(
            row_id=row_id, row_order=1, scope_kind="SKU" if scope.startswith("SKU:") else "TOTAL",
            scope_key=scope, scope_label="", metric_key=metric, metric_label="",
            row_last_updated_at="", section="", group=None, nm_id=None, format=None,
            values_by_date={DAY: value}, presentation_by_date={DAY: cell or {}},
        )

    full = [
        row("SKU:1|proxy_profit_4_rub", 100, corrected),
        row("SKU:1|orderCount", 10), row("SKU:1|orderSum", 1000),
        row("SKU:1|proxy_margin_per_unit_rub", ""),
        row("SKU:2|proxy_profit_4_rub", 500, corrected),
        row("SKU:2|orderCount", 30), row("SKU:2|orderSum", 3000),
        row("SKU:2|proxy_margin_per_unit_rub", ""),
        row("TOTAL|total_proxy_profit_4_rub", 600, corrected),
        row("TOTAL|proxy_margin_per_unit_rub_total", ""),
    ]
    p4 = SimpleNamespace(buyout_rate=Decimal("0.2"))
    dates = corrected_proxy_dates(full)
    expected = {item.row_id: item for item in recalculate_corrected_unit_margin_rows(
        full, parameters={day: (None, p4) for day in dates},
    )}
    selected = [full[-1]]
    compact = {
        item.row_id: {"values_by_date": item.values_by_date,
                      "presentation_by_date": item.presentation_by_date}
        for item in full[:-1]
    }
    scoped_dates = corrected_proxy_dates(selected, compact_operands=compact)
    actual = recalculate_corrected_unit_margin_rows(
        selected, parameters={day: (None, p4) for day in scoped_dates},
        compact_operands=compact,
    )
    assert scoped_dates == dates == [DAY]
    assert len(actual) == 1
    assert (actual[0].values_by_date, actual[0].presentation_by_date) == (
        expected[actual[0].row_id].values_by_date,
        expected[actual[0].row_id].presentation_by_date,
    )


def active_bound_book_parity() -> None:
    fixture = ManagementInventoryTests("test_1_d_dplus1_dplus2_keep_exact_source_and_preliminary")
    fixture.setUp()
    try:
        hidden = fixture.nms[1]
        book = fixture.book
        for day, payload in book["presentations"].items():
            for item in payload["quantity_snapshot"]["rows"]:
                if item["nm_id"] == hidden and item["facility_id"] == "moscow":
                    item["quantity"] = 7
            for item in book["wb_days"][day]["rows"]:
                if item["nm_id"] == hidden:
                    item["quantity"] = item["components"]["physical"] = 11
            payload["rows"][str(hidden)].update(wb_physical=11, stock_total=18)
            payload["totals"]["wb_physical"] += 11
            payload["totals"]["stock_total"] += 18
            payload["version_id"] = fingerprint(payload)
            book["state"]["periods"][day]["snapshot"] = json.loads(json.dumps(payload["quantity_snapshot"]))
        fixture.seed(book)
        with sqlite3.connect(fixture.runtime.db_path) as setup:
            ensure_proxy_v4_schema(setup)
        block = vitrina.SheetVitrinaV1WebVitrinaBlock(
            runtime=fixture.runtime,
            now_factory=lambda: datetime(2026, 9, 13, 10, tzinfo=timezone.utc),
        )
        requested = frozenset({
            "TOTAL|total_stock_total", "TOTAL|total_inventory_wb_total_qty_v1",
            "TOTAL|total_inventory_fbs_total_qty_v1",
            "TOTAL|total_inventory_fbs_facility_available_qty_v1:moscow",
        })
        with window_read_context(fixture.runtime.db_path, runtime_dir=fixture.runtime.runtime_dir):
            full = block.build(page_route="/", read_route="/", date_from=fixture.target["as_of_date"],
                               date_to=fixture.target["as_of_date"])
        with window_read_context(fixture.runtime.db_path, runtime_dir=fixture.runtime.runtime_dir):
            scoped = block.build(page_route="/", read_route="/", date_from=fixture.target["as_of_date"],
                                 date_to=fixture.target["as_of_date"], output_row_ids=requested)
        for field in (
            "refresh_status", "refresh_status_label", "refresh_status_tone",
            "refresh_status_reason", "refreshed_at", "refresh_outcome_counts",
            "load_window_status", "source_status_snapshot_as_of_date",
        ):
            assert getattr(scoped.status_summary, field) == getattr(full.status_summary, field), field
        expected = {item.row_id: item for item in full.rows if item.row_id in requested}
        actual = {item.row_id: item for item in scoped.rows}
        assert actual.keys() == expected.keys(), (actual.keys(), expected.keys())
        assert len(scoped.rows) == len(requested)
        for row_id, row in expected.items():
            assert (actual[row_id].values_by_date, actual[row_id].presentation_by_date) == (
                row.values_by_date, row.presentation_by_date,
            ), row_id
        assert actual["TOTAL|total_stock_total"].values_by_date[fixture.target["as_of_date"]] == 167207
        assert actual["TOTAL|total_inventory_wb_total_qty_v1"].values_by_date[fixture.target["as_of_date"]] == 37856
        owner = ("fixture-user", "fixture", "operator", ("vitrina",))
        service = WindowV3Service(block)
        try:
            manifest = service._build_manifest([fixture.target["as_of_date"]], owner, Event())
            historical_session = service._sessions[manifest.session_id]
            target_index = historical_session.row_ids.index("TOTAL|total_stock_total")
            seek = service._seek(historical_session, {
                "date_index": "0", "row_start": str(target_index), "row_count": "1",
            }, owner)
            cursor = service._decode_cursor(seek["cursor"], historical_session, owner)

            def material_token(dates: list[str]) -> str:
                with window_read_context(fixture.runtime.db_path, runtime_dir=fixture.runtime.runtime_dir) as context:
                    material, _, _, _ = _input_material(
                        context.borrow(fixture.runtime.db_path), fixture.runtime.runtime_dir,
                        dates, business_date="2026-09-13", default_as_of_date=fixture.target["as_of_date"],
                        context=context, header_cache=service._header_cache,
                    )
                return _digest(material)

            historical_token_before = material_token([fixture.target["as_of_date"]])
            current_token_before = material_token(["2026-09-13"])
            with window_read_context(fixture.runtime.db_path, runtime_dir=fixture.runtime.runtime_dir) as context:
                historical_before = _book_material(
                    context, fixture.runtime.runtime_dir, [fixture.version], include_current=False,
                )
                current_before = _book_material(
                    context, fixture.runtime.runtime_dir, [fixture.version], include_current=True,
                )
            successor = deepcopy(book)
            successor["state"]["observed_documents"]["later-current-only"] = {"source": "fixture"}
            _save_book(fixture.runtime.runtime_dir, successor, expected=fixture.version,
                       operation_id="window-false-stale-fixture")
            with window_read_context(fixture.runtime.db_path, runtime_dir=fixture.runtime.runtime_dir) as context:
                historical_after = _book_material(
                    context, fixture.runtime.runtime_dir, [fixture.version], include_current=False,
                )
                current_after = _book_material(
                    context, fixture.runtime.runtime_dir, [fixture.version], include_current=True,
                )
            assert historical_before == historical_after, "unrelated current book changed bound historical material"
            assert current_before != current_after, "current-day book change was not detected"
            assert material_token([fixture.target["as_of_date"]]) == historical_token_before
            assert material_token(["2026-09-13"]) != current_token_before
            service._check_input_version(historical_session)
            chunk = service._build_chunk(historical_session, cursor, Event())
            assert chunk.kind == "chunk" and chunk.session_id == historical_session.session_id
        finally:
            service.close()
    finally:
        fixture.doCleanups()


def official_compact_cost_parity() -> None:
    with TemporaryDirectory(prefix="window-official-cost-") as folder:
        db_path = Path(folder) / "official.sqlite3"
        connection = official_fbs_fixture(db_path)
        connection.close()
        day = "2026-09-05"
        model = build_current_official_fbs_estimate(
            db_path, nm_ids=[1, 2], now=datetime(2026, 9, 5, 10, tzinfo=timezone.utc),
        )
        assert model["available"]
        cost_row = WebVitrinaContractRow(
            row_id="SKU:2|our_wb_unit_cost_rub", row_order=1, scope_kind="SKU",
            scope_key="SKU:2", scope_label="", metric_key="our_wb_unit_cost_rub",
            metric_label="", row_last_updated_at="", section="", group=None, nm_id=2,
            format=None, values_by_date={day: 777}, presentation_by_date={},
        )
        expected = apply_current_official_fbs_estimate([cost_row], estimate=model)[0]
        compact = {cost_row.row_id: {
            "values_by_date": dict(cost_row.values_by_date), "presentation_by_date": {},
        }}
        assert apply_current_official_fbs_estimate([], estimate=model, compact_operands=compact) == []
        assert compact[cost_row.row_id]["values_by_date"][day] == expected.values_by_date[day]
        assert compact[cost_row.row_id]["presentation_by_date"][day] == expected.presentation_by_date[day]


def current_proxy_catalog_parity() -> None:
    day = "2026-10-02"
    with TemporaryDirectory(prefix="window-current-proxy-") as folder:
        root = Path(folder)
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=root)
        runtime.ingest_bundle(json.loads(BUNDLE_FIXTURE.read_text()), activated_at=day + "T08:00:00Z")
        state = runtime.load_current_state()
        enabled = [item for item in state.config_v2 if item.enabled]
        nm_id = enabled[0].nm_id
        plan = _build_plan(as_of_date=day, first_nm_id=nm_id,
                           second_nm_id=enabled[1].nm_id, first_group=enabled[0].group)
        source = [
            ["Заказы", f"SKU:{nm_id}|orderSum", 1000],
            ["Количество", f"SKU:{nm_id}|orderCount", 10],
            ["Реклама", f"SKU:{nm_id}|ads_sum", 100],
            ["Себестоимость", f"SKU:{nm_id}|our_wb_unit_cost_rub", 10],
        ]
        data = plan.sheets[0]
        data = replace(data, rows=[*data.rows, *source], row_count=len(data.rows) + len(source),
                       write_rect=f"A1:C{len(data.rows) + len(source) + 1}")
        plan = replace(plan, sheets=[data, *plan.sheets[1:]])
        save_ready_fixture(runtime, current_state=state, refreshed_at=day + "T09:00:00Z", plan=plan)
        with sqlite3.connect(runtime.db_path) as setup:
            ensure_proxy_v4_schema(setup)
        now = datetime(2026, 10, 2, 10, tzinfo=timezone.utc)
        block = vitrina.SheetVitrinaV1WebVitrinaBlock(runtime=runtime, now_factory=lambda: now)
        p4 = SimpleNamespace(buyout_rate=Decimal(".8"), included_expense_rate=Decimal(".3"),
                             retained_share=Decimal(".7"), version_id="fixture-v4")
        item = {"cost": Decimal("10"), "stock_quantity": Decimal("0"),
                "fbs_quantity": Decimal("0"), "facilities": {}}
        estimate = {
            "available": True, "date": day, "generation_id": "fixture-generation",
            "generation_digest": "sha256:fixture", "captured_at": day + "T09:30:00Z",
            "functional_version_id": "fixture-functional", "skus": {nm_id: item}, "total": item,
        }
        with patch.object(vitrina, "build_current_official_fbs_estimate", return_value=estimate), \
                patch.object(vitrina, "dated_parameters", return_value=(DEFAULT_PROXY_PARAMETERS, p4)):
            # This local fixture still needs normal legacy lazy-schema setup.
            block.build(page_route="/", read_route="/", date_from=day, date_to=day)
            with window_read_context(runtime.db_path, runtime_dir=root):
                full = block.build(page_route="/", read_route="/", date_from=day, date_to=day)
            expected = {row.row_id: row for row in full.rows if row.row_id.startswith(f"SKU:{nm_id}|")
                        and row.metric_key in CURRENT_PROXY_METRICS}
            assert len(expected) == len(CURRENT_PROXY_METRICS), expected.keys()
            assert any(row.values_by_date[day] not in (None, "") for row in expected.values())
            assert any(row.presentation_by_date.get(day) for row in expected.values())
            service = WindowV3Service(block)
            try:
                owner = ("fixture-user", "fixture", "operator", ("vitrina",))
                manifest = service._build_manifest([day], owner, Event())
                session = service._sessions[manifest.session_id]
                wire_rows = json.loads(manifest.json_bytes)["rows"]
                full_static = _catalog_surface(
                    list(full.rows), dates=[day], metric_catalog=[], business_date=day,
                )["rows"]
                assert session.row_ids == [row["row_id"] for row in full_static]
                for actual, original in zip(wire_rows, full_static, strict=True):
                    for key in ("row_id", "section_id", "group_id", "row_order", "static_cells"):
                        assert actual[key] == original[key], (key, actual["row_id"])
                for row_id, original in expected.items():
                    assert row_id in session.row_ids, f"manifest omitted {row_id}"
                    row_index = session.row_ids.index(row_id)
                    days, actual = service._slice_rows(session, 0, Event(), [row_index])
                    assert days == [day] and row_id in actual
                    assert (actual[row_id].values_by_date, actual[row_id].presentation_by_date) == (
                        original.values_by_date, original.presentation_by_date,
                    ), row_id
                    seek = service._seek(session, {"date_index": "0", "row_start": str(row_index),
                                                   "row_count": "1"}, owner)
                    cursor = service._decode_cursor(seek["cursor"], session, owner)
                    chunk = service._build_chunk(session, cursor, Event())
                    payload = json.loads(chunk.json_bytes)
                    expected_cell = service._date_cell(service._date_view_columns([day])[0], original)
                    assert payload["rows"][0]["values"][0] == [0, *expected_cell], row_id
            finally:
                service.close()
            print("window_v3_current_proxy_catalog:", {
                row.metric_key: row.values_by_date[day] for row in expected.values()
            })


def main() -> None:
    mixed_canonical_legacy_proxy_dates()
    corrected_hidden_sku_parity()
    active_bound_book_parity()
    official_compact_cost_parity()
    current_proxy_catalog_parity()
    with TemporaryDirectory(prefix="window-row-scope-") as folder:
        root = Path(folder)
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=root)
        runtime.ingest_bundle(json.loads(BUNDLE_FIXTURE.read_text()), activated_at="2026-04-21T12:00:00Z")
        config = [item for item in runtime.load_current_state().config_v2 if item.enabled]
        plan = _build_plan(
            as_of_date=DAY, first_nm_id=config[0].nm_id,
            second_nm_id=config[1].nm_id, first_group=config[0].group,
        )
        synthetic = [
            [f"Synthetic {item.nm_id} {metric}", f"SKU:{item.nm_id}|synthetic_metric_{metric:03}", metric]
            for item in config for metric in range(70)
        ]
        synthetic.extend([
            ["First discount", f"SKU:{config[0].nm_id}|effective_nonwallet_discount", 0.2],
            ["Second discount", f"SKU:{config[1].nm_id}|effective_nonwallet_discount", ""],
            ["Third discount", f"SKU:{config[2].nm_id}|effective_nonwallet_discount", 0.4],
        ])
        data = plan.sheets[0]
        data = replace(data, rows=[*data.rows, *synthetic],
                       row_count=len(data.rows) + len(synthetic),
                       write_rect=f"A1:C{len(data.rows) + len(synthetic) + 1}")
        plan = replace(plan, sheets=[data, *plan.sheets[1:]])
        save_ready_fixture(
            runtime, current_state=runtime.load_current_state(),
            refreshed_at="2026-04-20T12:00:00Z", plan=plan,
        )
        block = vitrina.SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: datetime(2026, 4, 21, 12, tzinfo=timezone.utc),
        )
        with window_read_context(runtime.db_path, runtime_dir=root):
            full = block.build(page_route="/", read_route="/", date_from=DAY, date_to=DAY)
        if len(full.rows) <= 2000:
            raise AssertionError(f"fixture too small: {len(full.rows)}")

        requested = frozenset({
            f"SKU:{config[-1].nm_id}|synthetic_metric_069",
            "TOTAL|total_orderSum",
            "TOTAL|avg_effective_nonwallet_discount",
        })
        materialized: list[int] = []
        patches = []
        for name in (
            "_normalize_rows", "_include_proxy_v4_unit_margin_rows",
            "_include_buyout_percent_rows", "extend_rows_with_inventory_planning",
            "recalculate_current_rows",
        ):
            original = getattr(vitrina, name)

            def measure(*args, _original=original, **kwargs):
                result = _original(*args, **kwargs)
                materialized.append(len(result))
                return result

            patches.append(patch.object(vitrina, name, measure))
        for item in patches:
            item.start()
        try:
            with window_read_context(runtime.db_path, runtime_dir=root):
                scoped = block.build(
                    page_route="/", read_route="/", date_from=DAY, date_to=DAY,
                    output_row_ids=requested,
                )
        finally:
            for item in reversed(patches):
                item.stop()
        expected = {row.row_id: row for row in full.rows if row.row_id in requested}
        actual = {row.row_id: row for row in scoped.rows}
        if expected.keys() != actual.keys():
            raise AssertionError((expected.keys(), actual.keys()))
        for row_id, row in expected.items():
            selected = actual[row_id]
            if (row.values_by_date, row.presentation_by_date) != (
                selected.values_by_date, selected.presentation_by_date,
            ):
                raise AssertionError(f"row-scope changed {row_id}")
        if abs(actual["TOTAL|avg_effective_nonwallet_discount"].values_by_date[DAY] - 0.3) > 1e-12:
            raise AssertionError("TOTAL mean did not include both hidden SKU operands")
        if not materialized or max(materialized) > 2000:
            raise AssertionError(f"row-scope materialized too many contract rows: {materialized}")
        print(f"window_v3_row_scope: full={len(full.rows)} scoped={len(scoped.rows)} "
              f"max_materialized={max(materialized)} parity=ok")


if __name__ == "__main__":
    main()
