"""Dated order-price formula, coverage and ordinary publication regression."""
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_weighted_seller_price_smoke import _evaluator, _validated_metrics, main as legacy_smoke
from apps.sheet_vitrina_v1_web_vitrina_group_refresh_smoke import BUNDLE_FIXTURE, STATUS_HEADER, _wait_job
from apps.ready_publication_fixture import save_ready_fixture
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import (
    RegistryUploadHttpEntrypoint, _metric_keys_for_source_keys, _with_full_refresh_metadata,
    _merge_source_group_ready_snapshot, _load_existing_ready_snapshot_for_preservation,
)
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.application.sheet_vitrina_v1_weighted_seller_price import (
    ORDER_PRICE_EFFECTIVE_FROM, WEIGHTED_PRICE_ROW_ID, WEIGHTED_PRICE_CALC_REF,
    WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY as KEY,
    extend_metrics_with_weighted_seller_price, index_daily_order_price,
    observed_order_price, weighted_price_presentation,
)
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaV1TemporalSlot, SheetVitrinaWriteTarget

DAY = ORDER_PRICE_EFFECTIVE_FROM
DATES = [(date.fromisoformat(DAY) - timedelta(days=i)).isoformat() for i in (2, 1, 0)]
NOW = datetime.fromisoformat(DAY).replace(hour=12, tzinfo=timezone.utc)


def evaluator_for(lookup, day=DAY, prices=None):
    evaluator = _evaluator(_validated_metrics(), slots={"day": (prices or {1001: 524, 1002: 524}, {1001: 50, 1002: 50})})
    slot = evaluator.live_sources.slot_lookups["day"]
    slot.column_date = day
    slot.order_price_lookup = lookup
    return evaluator


def check_formula():
    paired = {1001: {"orderSum": 25100, "orderCount": 50}, 1002: {"orderSum": 26200, "orderCount": 50}}
    before = evaluator_for(paired, DATES[1])
    after = evaluator_for(paired)
    assert before.resolve_total(KEY, "day") == 524
    assert after.resolve_total(KEY, "day") == 513
    # Prices, legacy arithmetic and unrelated operands keep their original meaning.
    for key in ("price_seller_discounted", "orderCount"):
        for nm_id in (1001, 1002):
            assert before.resolve_sku(key, nm_id, "day") == after.resolve_sku(key, nm_id, "day")
    assert before.resolve_total("avg_price_seller_discounted", "day") == after.resolve_total("avg_price_seller_discounted", "day")
    changed = evaluator_for(paired, prices={1001: 9999, 1002: None})
    changed.live_sources.slot_lookups["day"].ads_compact_lookup = {1001: SimpleNamespace(ads_sum=999)}
    changed.live_sources.slot_lookups["day"].stocks_lookup = {1001: SimpleNamespace(qty=0)}
    assert changed.resolve_total(KEY, "day") == 513
    value, _, _ = observed_order_price({1001: {"orderSum": 2500, "orderCount": 5}, 1002: {"orderSum": 10000, "orderCount": 10}}, [1001, 1002])
    assert abs(value - 833.3333333333334) < 1e-9
    invalid_pairs = [
        {"orderCount": 5}, {"orderSum": 2500}, {},
        {"orderSum": None, "orderCount": 5},
        {"orderSum": -1, "orderCount": 5}, {"orderSum": 1, "orderCount": -1},
        {"orderSum": float("nan"), "orderCount": 5},
        {"orderSum": 1, "orderCount": float("inf")},
        {"orderSum": 1, "orderCount": 0}, {"orderSum": 1, "orderCount": .5},
    ]
    for pair in invalid_pairs:
        assert evaluator_for({1001: paired[1001], 1002: pair}).resolve_total(KEY, "day") is None, pair
    assert observed_order_price({1: {"orderSum": 0, "orderCount": 0}}, [1])[0] is None
    assert observed_order_price({1: {"orderSum": 0, "orderCount": 1}}, [1])[0] == 0
    assert observed_order_price({**paired, 777: {}}, [1001, 1002])[0] == 513
    assert observed_order_price({1001: paired[1001]}, [1001, 1002]) == (502, [1002], [])

    # Exact-date extraction never combines two days or conflicting duplicates.
    def item(nm_id, metric, value, day=DAY):
        return SimpleNamespace(nm_id=nm_id, metric=metric, value=value, date=day)
    payload = SimpleNamespace(items=[item(1001, "orderSum", 25100), item(1001, "orderCount", 50, DATES[1])])
    assert evaluator_for(index_daily_order_price(payload, DAY)).resolve_total(KEY, "day") is None
    payload.items = [item(1001, "orderSum", 25100), item(1001, "orderCount", 50), item(1001, "orderSum", 26200)]
    assert evaluator_for(index_daily_order_price(payload, DAY)).resolve_total(KEY, "day") is None
    payload.items = [item(1001, "cartCount", 3)]
    assert evaluator_for(index_daily_order_price(payload, DAY)).resolve_total(KEY, "day") is None

    # Existing saved definitions must also receive the effective dated definition.
    metrics = _validated_metrics()
    stale = [replace(m, calc_ref="aggregate:positive_weight_fail_closed:price_seller_discounted:orderCount", label_ru="Custom") if m.metric_key == KEY else m for m in metrics]
    overlaid = extend_metrics_with_weighted_seller_price(stale)
    target = next(m for m in overlaid if m.metric_key == KEY)
    assert target.calc_ref == WEIGHTED_PRICE_CALC_REF and target.label_ru == "Custom"
    for day, source in ((DATES[1], "prices_snapshot"), (DAY, "sales_funnel_history")):
        assert KEY in _metric_keys_for_source_keys(metrics, source_keys=[source], column_date=day)
        other = "prices_snapshot" if day == DAY else "sales_funnel_history"
        assert KEY not in _metric_keys_for_source_keys(metrics, source_keys=[other], column_date=day)


def check_coverage():
    evaluator = evaluator_for({i: {"orderSum": 513, "orderCount": 1} for i in range(1, 59)})
    evaluator.enabled_config = [replace(evaluator.enabled_config[0], nm_id=i) for i in range(1, 93)]
    assert evaluator.resolve_total(KEY, "day") == 513
    slots = [SheetVitrinaV1TemporalSlot("day", "day", DAY)]
    cell = weighted_price_presentation(slots=slots, evaluator=evaluator, current_date=DAY)[WEIGHTED_PRICE_ROW_ID][DAY]
    assert cell["completeness_state"] == "partial" and cell["missing_sku_count"] == 34
    assert len(cell["metric_scope_evidence"]["applicable_scope"]) == 92
    assert len(cell["metric_scope_evidence"]["missing_scope"]) == 34
    older = weighted_price_presentation(slots=slots, evaluator=evaluator, current_date="2099-01-01")[WEIGHTED_PRICE_ROW_ID][DAY]
    assert older["completeness_state"] == "unknown_scope"
    return cell


def plan(prices, *, presentation=None, source="sales_funnel_history", kind="success"):
    rows = [["Price", WEIGHTED_PRICE_ROW_ID, *prices], ["SKU price", "SKU:1001|price_seller_discounted", 524, 524, 524], ["SPP", "SKU:1001|spp", .2, .2, .2]]
    statuses = [[f"{source}[{d}]", kind, "fresh", d, d, d, d, 92, 58, "", ""] for d in DATES]
    def sheet(name, header, data):
        return SheetVitrinaWriteTarget(name, "A1", "A1:K9", "A:Z", "overwrite", False, header, data, len(data), len(header))
    return SheetVitrinaV1Envelope("cutover-smoke", "cutover-smoke", DATES[1], DATES,
        [SheetVitrinaV1TemporalSlot(d, d, d) for d in DATES], {},
        [sheet("DATA_VITRINA", ["label", "key", *DATES], rows), sheet("STATUS", STATUS_HEADER, statuses)],
        {"server_cell_presentation": {WEIGHTED_PRICE_ROW_ID: presentation or {}}})


def values(snapshot):
    return next(r[2:] for s in snapshot.sheets if s.sheet_name == "DATA_VITRINA" for r in s.rows if r[1] == WEIGHTED_PRICE_ROW_ID)


def check_publication(cell):
    old_cell = {"quality_reason": "Published historical evidence"}
    previous = plan(["", 524, 524], presentation={DATES[1]: old_cell})
    candidate = plan([999, 999, 513], presentation={DAY: cell})
    full = _with_full_refresh_metadata(candidate, refreshed_at=NOW.isoformat().replace("+00:00", "Z"), previous_plan=previous, business_date=DAY)
    assert values(full) == ["", 524, 513]
    assert full.metadata["server_cell_presentation"][WEIGHTED_PRICE_ROW_ID][DATES[1]] == old_cell
    assert full.sheets[0].rows[1:] == candidate.sheets[0].rows[1:]
    # A fresh price snapshot cannot overwrite the new ratio when order source failed.
    failed = plan([999, 999, 999], source="sales_funnel_history", kind="error")
    retained = _with_full_refresh_metadata(failed, refreshed_at=NOW.isoformat().replace("+00:00", "Z"), previous_plan=full, business_date=DAY)
    assert values(retained) == ["", 524, 513]
    recovered, summary = _merge_source_group_ready_snapshot(previous_plan=retained,
        partial_plan=plan([999, 999, 514], presentation={DAY: cell}),
        source_group_id="wb_api", source_keys=["sales_funnel_history"], metric_keys=[KEY],
        refreshed_at=DAY + "T13:00:00Z", previous_refreshed_at=DAY + "T12:00:00Z",
        selected_as_of_date=DAY, business_date=DAY)
    assert values(recovered) == ["", 524, 514]
    assert len(summary["updated_cells"]) == 1
    assert summary["updated_cells"][0]["source_key"] == "sales_funnel_history"

    with TemporaryDirectory(prefix="wbc0080-cutover-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        assert runtime.ingest_bundle(json.loads(BUNDLE_FIXTURE.read_text()), activated_at=NOW.isoformat().replace("+00:00", "Z")).status == "accepted"
        state = runtime.load_current_state()
        save_ready_fixture(runtime, current_state=state, refreshed_at=NOW.isoformat().replace("+00:00", "Z"), plan=previous)
        entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=Path(tmp), runtime=runtime,
            now_factory=lambda: NOW, refreshed_at_factory=lambda: NOW.isoformat().replace("+00:00", "Z"))
        captured = {}
        def build(**kwargs):
            captured.update(kwargs)
            return candidate
        entrypoint.sheet_plan_block.build_plan = build
        job = entrypoint.start_sheet_source_group_refresh_job(source_group_id="wb_api", as_of_date=DAY)
        result = _wait_job(entrypoint, str(job["job_id"]))
        assert result["status"] == "success", result
        assert KEY in captured["metric_keys"]
        saved = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=DATES[1])
        assert values(saved) == ["", 524, 513]
        assert saved.metadata["server_cell_presentation"][WEIGHTED_PRICE_ROW_ID][DAY] == cell
        assert saved.metadata["server_cell_presentation"][WEIGHTED_PRICE_ROW_ID][DATES[1]] == old_cell
        target_updates = [c for c in result["result"]["updated_cells"] if c["metric_key"] == KEY]
        assert len(target_updates) == 1 and target_updates[0]["source_key"] == "sales_funnel_history"
        # A targeted historical refresh must preserve both historical number and blank.
        for day in DATES[:2]:
            if day != DATES[1]:
                save_ready_fixture(runtime, current_state=state, refreshed_at=NOW.isoformat().replace("+00:00", "Z"), plan=replace(saved, as_of_date=day))
            historical = entrypoint.start_sheet_source_group_refresh_job(source_group_id="wb_api", as_of_date=day)
            outcome = _wait_job(entrypoint, str(historical["job_id"]))
            assert outcome["status"] == "success", outcome
            assert not any(c["metric_key"] == KEY for c in outcome["result"]["updated_cells"])
            saved = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=day)
            assert values(saved) == ["", 524, 513]
        # The public grid contract supplies API/UI; this table has exportable=False.
        contract = SheetVitrinaV1WebVitrinaBlock(runtime=runtime, now_factory=lambda: NOW).build(
            page_route="/sheet-vitrina-v1/vitrina", read_route="/v1/sheet-vitrina-v1/web-vitrina", as_of_date=DATES[1])
        row = next(r for r in contract.rows if r.row_id == WEIGHTED_PRICE_ROW_ID)
        assert [row.values_by_date[d] for d in DATES] == ["", 524, 513]
        encoded = json.loads(json.dumps(asdict(row)))
        assert encoded["values_by_date"][DATES[1]] == 524 and encoded["values_by_date"][DAY] == 513
        assert encoded["presentation_by_date"][DAY]["completeness_state"] == "partial"


def check_rollover_and_closed_scope(cell):
    next_day = (date.fromisoformat(DAY) + timedelta(days=1)).isoformat()
    past_cell = {**cell, "completeness_state": "unknown_scope", "missing_sku_count": None}
    past_cell.pop("metric_scope_evidence", None)
    previous = plan(["", 524, 513], presentation={DAY: cell})
    candidate = plan([999, 999, 514], presentation={DAY: past_cell})
    closed = _with_full_refresh_metadata(candidate, refreshed_at=next_day + "T12:00:00Z", previous_plan=previous, business_date=next_day)
    assert closed.metadata["server_cell_presentation"][WEIGHTED_PRICE_ROW_ID][DAY]["missing_sku_count"] == 34
    grouped, _ = _merge_source_group_ready_snapshot(previous_plan=previous, partial_plan=candidate,
        source_group_id="wb_api", source_keys=["sales_funnel_history"], metric_keys=[KEY],
        refreshed_at=next_day + "T12:00:00Z", previous_refreshed_at=DAY + "T12:00:00Z",
        selected_as_of_date=DAY, business_date=next_day)
    assert grouped.metadata["server_cell_presentation"][WEIGHTED_PRICE_ROW_ID][DAY]["missing_sku_count"] == 34
    changed_scope = {**past_cell, "calculation_scope": ["SKU:999"]}
    changed = _with_full_refresh_metadata(plan([999, 999, 514], presentation={DAY: changed_scope}),
        refreshed_at=next_day + "T12:00:00Z", previous_plan=previous, business_date=next_day)
    assert changed.metadata["server_cell_presentation"][WEIGHTED_PRICE_ROW_ID][DAY]["completeness_state"] == "unknown_scope"
    with TemporaryDirectory(prefix="wbc0080-rollover-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        bundle = json.loads(BUNDLE_FIXTURE.read_text())
        assert runtime.ingest_bundle(bundle, activated_at=DATES[0] + "T12:00:00Z").status == "accepted"
        save_ready_fixture(runtime, current_state=runtime.load_current_state(), refreshed_at=DATES[1] + "T12:00:00Z", plan=replace(previous, as_of_date=DATES[0]))
        assert _load_existing_ready_snapshot_for_preservation(runtime, as_of_date=DATES[1])[0] is None
        for new_bundle in (False, True):
            if new_bundle:
                bundle["bundle_version"] = "wbc0080-cutover-next-bundle"
                assert runtime.ingest_bundle(bundle, activated_at=DAY + "T12:00:00Z").status == "accepted"
            restored = _with_full_refresh_metadata(candidate, refreshed_at=DAY + "T12:00:00Z", business_date=DAY, runtime=runtime)
            assert values(restored) == ["", 524, 514], new_bundle
            assert restored.sheets[0].rows[1:] == candidate.sheets[0].rows[1:]


if __name__ == "__main__":
    legacy_smoke()
    check_formula()
    cell = check_coverage()
    check_publication(cell)
    check_rollover_and_closed_scope(cell)
    print("order_price_cutover: formula, paired coverage, exact date, saved definition, full/group refresh and public contract OK")
