"""Targeted fixture smoke for WebCore-owned product capital."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.own_product_capital_backfill import run as run_backfill  # noqa: E402
from packages.application.canonical_cost_engine import ensure_canonical_cost_schema  # noqa: E402
from packages.application.cny_ledger import CnyLedgerBlock  # noqa: E402
from packages.application.own_product_capital import (  # noqa: E402
    STAGE_FF,
    STAGE_FF_TO_WB,
    STAGE_PRODUCTION,
    STAGE_PRODUCTION_TO_FF,
    STAGE_WB,
    TARGETED_ORPHAN_DOPRINATO_EVENT_ID,
    TARGETED_ORPHAN_DOPRINATO_REASON,
    OwnProductCapitalBlock,
    _plan_targeted_orphan_doprinato_classification,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _connect,
)
from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    SlotLookups,
    TemporalLiveSources,
    _MetricEvaluator,
)
from packages.application.sheet_vitrina_v1_archived_metrics import (  # noqa: E402
    filter_archived_public_metrics,
)
from packages.application.sheet_vitrina_v1_onec_stocks import (  # noqa: E402
    ONEC_STOCKS_ARCHIVED_METRIC_KEYS,
    ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY,
    extend_metrics_with_onec_stock_metrics,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (  # noqa: E402
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    extend_metrics_with_our_wb_cost_metrics,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (  # noqa: E402
    OWN_AVG_COST_RUB_METRIC_KEY,
    OWN_AVG_COST_RUB_TOTAL_METRIC_KEY,
    OWN_PRODUCT_CAPITAL_SECTION_RU,
    OWN_PRODUCT_CAPITAL_ARCHIVED_METRIC_KEYS,
    OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
    OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY,
    OWN_TOTAL_QTY_TOTAL_METRIC_KEY,
    extend_metrics_with_own_product_capital_metrics,
    own_stage_metric_key,
)
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item  # noqa: E402
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot  # noqa: E402


NOW = "2026-07-12T08:00:00Z"
LINES = [
    {"line_id": "line-a", "nm_id": 101, "qty": "100", "unit_price": "100", "match_status": "matched"},
    {"line_id": "line-b", "nm_id": 202, "qty": "50", "unit_price": "200", "match_status": "matched_by_compatibility"},
]
PAYMENT_TEXT_WITHOUT_DATE = """Заявление на перевод № 1
Please debit our account with you): 40802156616580000008
Валюта Currency Code CNY
Сумма перевода Amount of transfer 15000,00
50 Ordering Customer
ООО Тест
56 Банк-посредник
BANK CNY
57 Банк получателя
ABCNCNBJXXX BANK OF CHINA
59 Получатель
12345678901234567890 TEST SUPPLIER LTD
Назначение платежа Details of payment 70
PAYMENT UNDER CONTRACT CN-1
INVOICE INV-1
Расходы и комиссии OUR
"""


def main() -> None:
    with TemporaryDirectory(prefix="own-product-capital-smoke-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        block = OwnProductCapitalBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        _assert_partial_payment_layers(block)
        _assert_history_and_stage_boundaries(block)
        _assert_moving_average_and_wb_reconciliation(block)
        _assert_confirmation_transition(block)
        _assert_official_wb_stock_override(block)
        _assert_fail_closed_guards(block)
        _assert_metric_identities(block)
        _assert_canonical_fallback_uses_physical_wac(runtime, block)
        _assert_backfill_runner(runtime, block)
    _assert_payment_document_hard_gate()
    _assert_late_boundary_correction()
    _assert_partial_acceptance_state_machine()
    _assert_historical_doprinato_paid_boundary()
    _assert_targeted_orphan_doprinato_classification()
    _assert_persisted_expense_events()
    print("own product capital smoke: OK")


def _assert_partial_payment_layers(block: OwnProductCapitalBlock) -> None:
    first = block.record_supplier_payment(
        payment_id="payment-15",
        shipment_id="shipment-main",
        effective_date="2026-07-01",
        invoice_total_cny="100000",
        paid_cny="15000",
        paid_rub="150000",
        product_lines=LINES,
        provenance={"payment_date_provenance": {"source": "parsed_document"}},
    )
    second = block.record_supplier_payment(
        payment_id="payment-85",
        shipment_id="shipment-main",
        effective_date="2026-07-05",
        invoice_total_cny="100000",
        paid_cny="85000",
        paid_rub="1020000",
        product_lines=LINES,
        actual_shipment_date="2026-07-03",
        provenance={
            "payment_date_provenance": {
                "source": "manual_operator_confirmation",
                "actor": "operator-smoke",
                "confirmed_at": NOW,
            }
        },
    )
    _eq(first["incremental_paid_share"], "0.15", "15% incremental share")
    _eq(second["incremental_paid_share"], "0.85", "85% incremental share")
    _eq(second["cumulative_paid_share"], "1", "full cumulative share")
    _eq(first["stage"], STAGE_PRODUCTION, "first payment physical stage")
    _eq(second["stage"], STAGE_PRODUCTION_TO_FF, "later payment physical stage")
    allocations = first["allocations"]
    _dec_eq(allocations[0]["paid_equivalent_qty"], "15", "all-SKU proportional qty A")
    _dec_eq(allocations[1]["paid_equivalent_qty"], "7.5", "all-SKU proportional qty B")
    _dec_eq(sum((item["allocated_rub"] for item in allocations), Decimal("0")), "150000", "payment allocation closes")
    if not block.record_supplier_payment(
        payment_id="payment-15",
        shipment_id="shipment-main",
        effective_date="2026-07-01",
        invoice_total_cny="100000",
        paid_cny="15000",
        paid_rub="150000",
        product_lines=LINES,
        provenance={"payment_date_provenance": {"source": "parsed_document"}},
    )["idempotent"]:
        raise AssertionError("same payment must be idempotent")
    block.record_order_level_cost_payment(
        document_id="logistics-paid-1",
        shipment_id="shipment-main",
        effective_date="2026-07-06",
        capital_rub="20000",
        product_lines=LINES,
        component="logistics",
        actual_shipment_date="2026-07-03",
        expenses_complete=False,
        provenance={"payment_date_provenance": {"source": "parsed_document"}},
    )


def _assert_history_and_stage_boundaries(block: OwnProductCapitalBlock) -> None:
    block.materialize_supplier_boundaries(
        shipment_id="shipment-main",
        actual_shipment_date="2026-07-03",
        actual_ff_acceptance_date="2026-07-08",
        expenses_complete=False,
    )
    before_later_payment = block.load_daily_metric_lookup("2026-07-02")[101]
    _dec_eq(before_later_payment[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY], "75000", "later payment not backdated")
    in_transit = block.load_daily_metric_lookup("2026-07-06")[101]
    _dec_eq(in_transit[own_stage_metric_key(STAGE_PRODUCTION_TO_FF, "qty")], "100", "transit qty")
    _dec_eq(in_transit[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY], "595000", "two rate layers plus paid logistics retain actual RUB")
    on_ff = block.load_daily_metric_lookup("2026-07-09")[101]
    _dec_eq(on_ff[own_stage_metric_key(STAGE_FF, "qty")], "100", "actual FF acceptance boundary")
    if on_ff["presentation_state"] != "unconfirmed":
        raise AssertionError(f"expenses_complete=false must be yellow/unconfirmed: {on_ff}")


def _assert_moving_average_and_wb_reconciliation(block: OwnProductCapitalBlock) -> None:
    block.record_supplier_payment(
        payment_id="payment-second-receipt",
        shipment_id="shipment-second",
        effective_date="2026-07-07",
        invoice_total_cny="10000",
        paid_cny="10000",
        paid_rub="200000",
        product_lines=[{"line_id": "line-c", "nm_id": 101, "qty": "100", "unit_price": "100", "match_status": "matched"}],
        actual_shipment_date="2026-07-06",
        actual_ff_acceptance_date="2026-07-09",
    )
    block.materialize_supplier_boundaries(
        shipment_id="shipment-second",
        actual_shipment_date="2026-07-06",
        actual_ff_acceptance_date="2026-07-09",
        expenses_complete=True,
    )
    wb = block.record_ordinary_wb_supply_final(
        supply_id="wb-ordinary-1",
        writeoff_date="2026-07-10",
        acceptance_date="2026-07-11",
        sent_quantities_by_nm={101: "120"},
        accepted_quantities_by_nm={101: "80"},
        warehouse="Коледино",
        destination="ЦФО",
        known_nm_ids=[101, 202],
        expenses_complete=True,
    )
    snapshot_cost = Decimal(wb["writeoff"]["lines"][0]["unit_cost_rub"])
    expected = (Decimal("595000") + Decimal("200000")) / Decimal("200")
    _dec_eq(snapshot_cost, expected, "FF moving weighted snapshot")
    day11 = block.load_daily_metric_lookup("2026-07-11")[101]
    _dec_eq(day11[own_stage_metric_key(STAGE_FF_TO_WB, "qty")], "40", "partial acceptance leaves sent-accepted")
    _dec_eq(day11[own_stage_metric_key(STAGE_WB, "qty")], "80", "accepted part moves once")
    total_before = day11[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY]
    _must_fail(
        lambda: block.reconcile_doprinato(
            reconciliation_supply_id="wb-doprin-before-original",
            effective_date="2026-07-10",
            quantities_by_nm={101: "1"},
            warehouse="Коледино",
            destination="ЦФО",
            original_supply_id="wb-ordinary-1",
        ),
        "Допринято before original final acceptance",
    )
    direct = block.reconcile_doprinato(
        reconciliation_supply_id="wb-doprin-direct",
        effective_date="2026-07-12",
        quantities_by_nm={101: "15"},
        warehouse="Коледино",
        destination="ЦФО",
        original_supply_id="wb-ordinary-1",
    )
    if direct["closures"][0]["original_supply_id"] != "wb-ordinary-1":
        raise AssertionError(f"direct Допринято link was not honored: {direct}")
    if not block.reconcile_doprinato(
        reconciliation_supply_id="wb-doprin-direct",
        effective_date="2026-07-12",
        quantities_by_nm={101: "15"},
        warehouse="Коледино",
        destination="ЦФО",
        original_supply_id="wb-ordinary-1",
    )["idempotent"]:
        raise AssertionError("Допринято replay must be idempotent")
    day12 = block.load_daily_metric_lookup("2026-07-12")[101]
    _dec_eq(day12[own_stage_metric_key(STAGE_FF_TO_WB, "qty")], "25", "direct reconciliation closes outstanding")
    _dec_eq(day12[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY], total_before, "reconciliation preserves total capital")
    with _connect(block.runtime.db_path) as conn:
        writeoffs = conn.execute(
            """
            SELECT COUNT(*) FROM sheet_vitrina_v1_own_capital_events
            WHERE supply_id = 'wb-doprin-direct' AND stage_from = 'FF'
            """
        ).fetchone()[0]
    if writeoffs != 0:
        raise AssertionError("Допринято must not create a duplicate FF writeoff")
    with _connect(block.runtime.db_path) as conn:
        conn.execute("DELETE FROM sheet_vitrina_v1_own_capital_daily_state WHERE as_of_date = '2026-07-11'")
        conn.commit()
    block.recalculate(date_from="2026-07-11", date_to="2026-07-11")
    rebuilt_day11 = block.load_daily_metric_lookup("2026-07-11")[101]
    _dec_eq(
        rebuilt_day11[own_stage_metric_key(STAGE_FF_TO_WB, "qty")],
        "40",
        "bounded rebuild carries state from events before date_from",
    )
    if not any("Недопринято WB: 40" in reason for reason in rebuilt_day11["presentation_reasons"]):
        raise AssertionError(f"historical outstanding must not be reduced by later reconciliation: {rebuilt_day11}")

    # Two additional ordinary supplies prove warehouse+SKU oldest-first fallback.
    block.record_ordinary_wb_supply_final(
        supply_id="wb-ordinary-2",
        writeoff_date="2026-07-12",
        acceptance_date="2026-07-12",
        sent_quantities_by_nm={101: "20"},
        accepted_quantities_by_nm={101: "10"},
        warehouse="Коледино",
        destination="ЦФО",
        known_nm_ids=[101],
        expenses_complete=True,
    )
    fifo = block.reconcile_doprinato(
        reconciliation_supply_id="wb-doprin-fifo",
        effective_date="2026-07-12",
        quantities_by_nm={101: "30"},
        warehouse="Коледино",
        destination="ЦФО",
    )
    if [item["original_supply_id"] for item in fifo["closures"]] != ["wb-ordinary-1", "wb-ordinary-2"]:
        raise AssertionError(f"warehouse+SKU fallback must be FIFO: {fifo}")


def _assert_confirmation_transition(block: OwnProductCapitalBlock) -> None:
    capital_before = block.load_daily_metric_lookup("2026-07-09")[101][OWN_TOTAL_CAPITAL_RUB_METRIC_KEY]
    block.set_expenses_certification(
        shipment_id="shipment-main",
        expenses_complete=True,
        actor="operator-smoke",
    )
    after = block.load_daily_metric_lookup("2026-07-09")[101]
    _dec_eq(after[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY], capital_before, "confirmation must not add future unpaid cost")
    if after["presentation_state"] != "confirmed":
        raise AssertionError(f"certification must remove yellow state without changing capital: {after}")
    block.set_expenses_certification(shipment_id="shipment-main", expenses_complete=False)
    reset = block.load_daily_metric_lookup("2026-07-09")[101]
    if reset["presentation_state"] != "unconfirmed":
        raise AssertionError("cost-affecting mutation reset must restore yellow")


def _assert_official_wb_stock_override(block: OwnProductCapitalBlock) -> None:
    with _connect(block.runtime.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_wb_cost_daily_state (
                as_of_date, nm_id, stock_qty, our_wb_unit_cost_rub,
                confirmed_qty, estimated_qty, fallback_qty, confirmed_share_pct,
                source_status, component_status_json, calculated_at, inputs_hash
            ) VALUES ('2026-07-13', 101, 10, 500, 5, 5, 0, 0.5,
                      'estimated', '{"transit":"pending"}', ?, 'official-wb-smoke')
            """,
            (NOW,),
        )
        conn.commit()
    block.recalculate(date_to="2026-07-13")
    wb = block.load_daily_metric_lookup("2026-07-13")[101]
    _dec_eq(wb[own_stage_metric_key(STAGE_WB, "qty")], "10", "official WB stock quantity override")
    _dec_eq(wb[own_stage_metric_key(STAGE_WB, "capital_rub")], "5000", "existing our_wb_unit_cost reuse")
    _dec_eq(wb[own_stage_metric_key(STAGE_WB, "confirmed_share_pct")], "0.5", "existing WB confirmed bucket reuse")


def _assert_fail_closed_guards(block: OwnProductCapitalBlock) -> None:
    _must_fail(
        lambda: block.record_supplier_payment(
            payment_id="overpayment",
            shipment_id="shipment-main",
            effective_date="2026-07-06",
            invoice_total_cny="100000",
            paid_cny="1",
            paid_rub="12",
            product_lines=LINES,
        ),
        "overpayment",
    )
    _must_fail(
        lambda: block.record_supplier_payment(
            payment_id="unmatched",
            shipment_id="bad",
            effective_date="2026-07-01",
            invoice_total_cny="100",
            paid_cny="10",
            paid_rub="100",
            product_lines=[{"line_id": "x", "nm_id": 303, "qty": 1, "unit_price": 100, "match_status": "unmatched"}],
        ),
        "atomic unmatched SKU gate",
    )
    _must_fail(
        lambda: block.record_ff_writeoff(
            supply_id="unknown-nmid",
            effective_date="2026-07-12",
            sent_quantities_by_nm={999: 1},
            warehouse="Коледино",
            destination="ЦФО",
            known_nm_ids=[101, 202],
        ),
        "unknown WB nmID",
    )
    _must_fail(
        lambda: block.reconcile_doprinato(
            reconciliation_supply_id="surplus",
            effective_date="2026-07-12",
            quantities_by_nm={101: 999},
            warehouse="Коледино",
            destination="ЦФО",
        ),
        "unmatched Допринято surplus",
    )


def _assert_metric_identities(block: OwnProductCapitalBlock) -> None:
    lookup = block.load_daily_metric_lookup("2026-07-12")
    base_metrics = [
        MetricV2Item(
            metric_key="sentinel_1c_metric",
            enabled=True,
            scope="SKU",
            label_ru="1С sentinel",
            calc_type="metric",
            calc_ref="sentinel_1c_metric",
            show_in_data=True,
            format="rub",
            display_order=1,
            section="1С",
        )
    ]
    metrics = extend_metrics_with_own_product_capital_metrics(
        extend_metrics_with_our_wb_cost_metrics(extend_metrics_with_onec_stock_metrics(base_metrics))
    )
    if metrics[0] != base_metrics[0]:
        raise AssertionError("existing 1C metric must be preserved unchanged")
    if not any(item.section == OWN_PRODUCT_CAPITAL_SECTION_RU for item in metrics):
        raise AssertionError("own product capital section is missing")
    configs = [_config(101, 1), _config(202, 2)]
    slot = "today_current"
    lookups = SlotLookups(
        seller_funnel_lookup={}, history_lookup={}, web_lookup={}, prices_lookup={}, sf_period_lookup={},
        spp_lookup={}, ads_bids_lookup={}, stocks_lookup={}, onec_stocks_lookup={}, ads_compact_lookup={},
        fin_lookup={}, fin_storage_fee_total=None, cost_price_lookup={}, promo_lookup={},
        own_product_capital_lookup=lookup, column_date="2026-07-12",
    )
    evaluator = _MetricEvaluator(
        enabled_config=configs,
        metrics_by_key={item.metric_key: item for item in metrics},
        formulas_by_id={},
        live_sources=TemporalLiveSources(
            temporal_slots=[SheetVitrinaV1TemporalSlot(slot_key=slot, slot_label="Сегодня", column_date="2026-07-12")],
            statuses=[], slot_lookups={slot: lookups}, source_temporal_policies={},
        ),
    )
    evaluator.sku_cache[(slot, 101, OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY)] = 1000.0
    evaluator.sku_cache[(slot, 202, OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY)] = 500.0
    total_capital = evaluator.resolve_total(OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY, slot)
    total_qty = evaluator.resolve_total(OWN_TOTAL_QTY_TOTAL_METRIC_KEY, slot)
    total_wac = evaluator.resolve_total(OWN_AVG_COST_RUB_TOTAL_METRIC_KEY, slot)
    _dec_eq(total_capital, sum(Decimal(str(row[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY])) for row in lookup.values()), "TOTAL capital identity")
    _dec_eq(total_qty, sum(Decimal(str(row["own_total_product_qty"])) for row in lookup.values()), "TOTAL quantity identity")
    _dec_eq(total_wac, Decimal(str(total_capital)) / Decimal(str(total_qty)), "TOTAL WAC identity")
    metric_keys = {item.metric_key for item in filter_archived_public_metrics(metrics)}
    archived_keys = set(OWN_PRODUCT_CAPITAL_ARCHIVED_METRIC_KEYS) | set(ONEC_STOCKS_ARCHIVED_METRIC_KEYS)
    leaked_archived = sorted(metric_keys & archived_keys)
    if leaked_archived:
        raise AssertionError(f"archived capital metrics leaked into active catalog: {leaked_archived}")
    if ONEC_PROXY_PROFIT_2_RUB_METRIC_KEY in metric_keys:
        raise AssertionError("proxy2 metric must be archived")
    if OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY not in {item.metric_key for item in metrics}:
        raise AssertionError("proxy3 metric was lost")


def _assert_canonical_fallback_uses_physical_wac(
    runtime: RegistryUploadDbBackedRuntime,
    block: OwnProductCapitalBlock,
) -> None:
    with _connect(runtime.db_path) as conn:
        ensure_canonical_cost_schema(conn)
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_canonical_cost_daily_state(
                as_of_date,nm_id,stage,physical_quantity,paid_equivalent_quantity,
                recognized_capital_rub,paid_capital_rub,cost_covered_quantity,
                confirmed_quantity,recognized_unit_cost_rub,paid_unit_cost_rub,
                underaccepted_quantity,underaccepted_recognized_capital_rub,
                underaccepted_paid_capital_rub,source_quality,diagnostics_json,
                calculated_at,fingerprint
            ) VALUES('2026-07-20',909,'PRODUCTION','100','15','150','150','15','15',
                     '1.5','10','0','0','0','confirmed_payment','{}',?,'physical-wac-smoke')
            """,
            (NOW,),
        )
        conn.commit()
    row = block.load_daily_metric_lookup("2026-07-20")[909]
    _dec_eq(row[own_stage_metric_key("PRODUCTION", "qty")], "100", "canonical fallback physical qty")
    _dec_eq(row[own_stage_metric_key("PRODUCTION", "capital_rub")], "150", "canonical fallback capital")
    _dec_eq(row[own_stage_metric_key("PRODUCTION", "unit_cost_rub")], "1.5", "canonical fallback stage WAC")
    _dec_eq(row[OWN_AVG_COST_RUB_METRIC_KEY], "1.5", "canonical fallback SKU WAC")


def _assert_backfill_runner(runtime: RegistryUploadDbBackedRuntime, block: OwnProductCapitalBlock) -> None:
    args = argparse.Namespace(
        runtime_dir=str(runtime.runtime_dir), date_from="2026-07-01", date_to="2026-07-12",
        apply=True, fingerprint="legacy-disabled", backup_dir=str(runtime.runtime_dir / "backups"),
    )
    try:
        run_backfill(args)
    except ValueError as exc:
        if "audit/dry-run only" not in str(exc):
            raise
    else:
        raise AssertionError("legacy own-capital runner must not remain apply-capable")
    block.recalculate(date_to="2026-07-12")


def _assert_payment_document_hard_gate() -> None:
    with TemporaryDirectory(prefix="own-capital-payment-gate-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        block = CnyLedgerBlock(
            runtime=runtime,
            timestamp_factory=lambda: NOW,
            pdf_text_extractor=lambda body, filename: (PAYMENT_TEXT_WITHOUT_DATE, {}, []),
        )
        preview = block.upload_document(
            file_bytes=b"fixture",
            uploaded_filename="payment.pdf",
        )
        if not preview.get("preview_required") or preview.get("durable_saved") is not False:
            raise AssertionError(f"missing payment date must return non-durable preview: {preview}")
        if runtime.list_cny_documents():
            raise AssertionError("parse-preview missing date must not durably save the document")
        saved = block.upload_document(
            file_bytes=b"fixture",
            uploaded_filename="payment.pdf",
            manual_payment_date="2026-05-13",
            manual_payment_date_actor="operator-smoke",
        )
        provenance = (saved.get("parsed_payload") or {}).get("payment_date_provenance") or {}
        if provenance.get("source") != "manual_operator_confirmation" or provenance.get("actor") != "operator-smoke":
            raise AssertionError(f"manual payment date provenance missing: {saved}")
        if saved.get("operation_date") != "2026-05-13":
            raise AssertionError(f"manual payment date was not effective: {saved}")
        OwnProductCapitalBlock(runtime=runtime, timestamp_factory=lambda: NOW).record_supplier_payment(
            payment_id=str(saved["document_id"]),
            shipment_id="payment-gate-shipment",
            effective_date="2026-05-13",
            invoice_total_cny="100000",
            paid_cny="15000",
            paid_rub="150000",
            product_lines=LINES,
        )
        archived = block.delete_document(str(saved["document_id"]))
        if archived.get("archived") is not True or archived.get("audit_record_retained") is not True:
            raise AssertionError(f"recognized payment must use audited archive/replay: {archived}")
        if any(
            str(item.get("source_document_id") or "") == str(saved["document_id"])
            for item in runtime.list_cny_ledger_operations()
        ):
            raise AssertionError("archived payment must be removed by deterministic CNY replay")
        broken = CnyLedgerBlock(
            runtime=runtime,
            timestamp_factory=lambda: NOW,
            pdf_text_extractor=lambda body, filename: ("Заявление на перевод Валюта Currency Code CNY", {}, []),
        )
        _must_fail(
            lambda: broken.upload_document(file_bytes=b"broken", uploaded_filename="broken.pdf"),
            "missing amount/currency required fields",
        )


def _assert_late_boundary_correction() -> None:
    with TemporaryDirectory(prefix="own-capital-boundary-correction-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        block = OwnProductCapitalBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        block.record_supplier_payment(
            payment_id="late-boundary-payment",
            shipment_id="late-boundary-shipment",
            effective_date="2026-07-04",
            invoice_total_cny="100",
            paid_cny="100",
            paid_rub="1000",
            product_lines=[
                {"line_id": "late-line", "nm_id": 303, "qty": "10", "unit_price": "10", "match_status": "matched"}
            ],
        )
        block.materialize_supplier_boundaries(
            shipment_id="late-boundary-shipment",
            actual_shipment_date="2026-07-01",
            actual_ff_acceptance_date="2026-07-02",
            expenses_complete=True,
        )
        if 303 in block.load_daily_metric_lookup("2026-07-03"):
            raise AssertionError("payment capital must not exist before its effective payment date")
        payment_day = block.load_daily_metric_lookup("2026-07-04")[303]
        _dec_eq(
            payment_day[own_stage_metric_key(STAGE_FF, "qty")],
            "10",
            "late factual boundaries place the paid layer in its payment-date physical stage",
        )


def _assert_partial_acceptance_state_machine() -> None:
    with TemporaryDirectory(prefix="own-capital-partial-acceptance-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        block = OwnProductCapitalBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        block.record_supplier_payment(
            payment_id="partial-payment",
            shipment_id="partial-shipment",
            effective_date="2026-07-01",
            invoice_total_cny="1000",
            paid_cny="1000",
            paid_rub="10000",
            product_lines=[
                {
                    "line_id": "partial-line",
                    "nm_id": 101,
                    "qty": "100",
                    "unit_price": "10",
                    "match_status": "matched",
                }
            ],
            actual_shipment_date="2026-06-29",
            actual_ff_acceptance_date="2026-06-30",
            expenses_complete=True,
        )
        _must_fail(
            lambda: block.record_ordinary_wb_supply_acceptance(
                supply_id="acceptance-before-writeoff",
                writeoff_date="2026-07-03",
                acceptance_date="2026-07-02",
                sent_quantities_by_nm={101: 1},
                accepted_quantities_by_nm={101: 1},
                warehouse="Коледино",
                destination="ЦФО",
                known_nm_ids=[101],
                expenses_complete=True,
                final=True,
            ),
            "acceptance date before writeoff date",
        )
        with _connect(runtime.db_path) as conn:
            invalid_events = conn.execute(
                """
                SELECT COUNT(*) FROM sheet_vitrina_v1_own_capital_events
                WHERE supply_id='acceptance-before-writeoff'
                """
            ).fetchone()[0]
        _eq(invalid_events, 0, "invalid event ordering must be atomic before writeoff")
        block.record_ff_writeoff(
            supply_id="partial-wb",
            effective_date="2026-07-02",
            sent_quantities_by_nm={101: 100},
            warehouse="Коледино",
            destination="ЦФО",
            known_nm_ids=[101],
            expenses_complete=True,
        )
        first_partial = block.record_ordinary_wb_supply_acceptance(
            supply_id="partial-wb",
            writeoff_date="2026-07-02",
            acceptance_date="2026-07-03",
            sent_quantities_by_nm={101: 100},
            accepted_quantities_by_nm={101: 40},
            warehouse="Коледино",
            destination="ЦФО",
            known_nm_ids=[101],
            expenses_complete=True,
            final=False,
        )
        _dec_eq(
            first_partial["lines"][0]["accepted_delta"],
            "40",
            "status=4 first accepted delta",
        )
        repeated_partial = block.record_ordinary_wb_supply_acceptance(
            supply_id="partial-wb",
            writeoff_date="2026-07-02",
            acceptance_date="2026-07-03",
            sent_quantities_by_nm={101: 100},
            accepted_quantities_by_nm={101: 40},
            warehouse="Коледино",
            destination="ЦФО",
            known_nm_ids=[101],
            expenses_complete=True,
            final=False,
        )
        _dec_eq(
            repeated_partial["lines"][0]["accepted_delta"],
            "0",
            "status=4 repeat accepted delta",
        )
        block.record_ordinary_wb_supply_acceptance(
            supply_id="partial-wb",
            writeoff_date="2026-07-02",
            acceptance_date="2026-07-04",
            sent_quantities_by_nm={101: 100},
            accepted_quantities_by_nm={101: 70},
            warehouse="Коледино",
            destination="ЦФО",
            known_nm_ids=[101],
            expenses_complete=True,
            final=False,
        )
        block.record_ordinary_wb_supply_acceptance(
            supply_id="partial-wb",
            writeoff_date="2026-07-02",
            acceptance_date="2026-07-05",
            sent_quantities_by_nm={101: 100},
            accepted_quantities_by_nm={101: 80},
            warehouse="Коледино",
            destination="ЦФО",
            known_nm_ids=[101],
            expenses_complete=True,
            final=True,
        )
        final_repeat = block.record_ordinary_wb_supply_acceptance(
            supply_id="partial-wb",
            writeoff_date="2026-07-02",
            acceptance_date="2026-07-05",
            sent_quantities_by_nm={101: 100},
            accepted_quantities_by_nm={101: 80},
            warehouse="Коледино",
            destination="ЦФО",
            known_nm_ids=[101],
            expenses_complete=True,
            final=True,
        )
        _dec_eq(
            final_repeat["lines"][0]["accepted_delta"],
            "0",
            "status=5 repeat accepted delta",
        )
        day3 = block.load_daily_metric_lookup("2026-07-03")[101]
        _dec_eq(
            day3[own_stage_metric_key(STAGE_FF_TO_WB, "qty")],
            "60",
            "status=4 outstanding remains in FF to WB",
        )
        _dec_eq(
            day3[own_stage_metric_key(STAGE_WB, "qty")],
            "40",
            "status=4 accepted quantity moves to WB",
        )
        day5 = block.load_daily_metric_lookup("2026-07-05")[101]
        _dec_eq(
            day5[own_stage_metric_key(STAGE_FF_TO_WB, "qty")],
            "20",
            "status=5 retains final underacceptance",
        )
        _dec_eq(
            day5[own_stage_metric_key(STAGE_WB, "qty")],
            "80",
            "status 3 to 4 to 5 cumulative acceptance",
        )
        with _connect(runtime.db_path) as conn:
            ff_debits = conn.execute(
                """
                SELECT COUNT(*) FROM sheet_vitrina_v1_own_capital_events
                WHERE supply_id='partial-wb' AND stage_from='FF' AND stage_to='FF_TO_WB'
                """
            ).fetchone()[0]
            accepted = conn.execute(
                """
                SELECT SUM(CAST(quantity AS REAL))
                FROM sheet_vitrina_v1_own_capital_events
                WHERE supply_id='partial-wb' AND event_type='wb_acceptance'
                """
            ).fetchone()[0]
            outstanding = conn.execute(
                """
                SELECT open_quantity FROM sheet_vitrina_v1_own_capital_wb_outstanding
                WHERE original_supply_id='partial-wb' AND nm_id=101
                """
            ).fetchone()[0]
        _eq(ff_debits, 1, "one FF debit across status transitions")
        _dec_eq(accepted, "80", "accepted capital moved exactly once")
        _dec_eq(outstanding, "20", "final outstanding persisted once")
        _must_fail(
            lambda: block.record_ordinary_wb_supply_acceptance(
                supply_id="partial-wb",
                writeoff_date="2026-07-02",
                acceptance_date="2026-07-06",
                sent_quantities_by_nm={101: 100},
                accepted_quantities_by_nm={101: 79},
                warehouse="Коледино",
                destination="ЦФО",
                known_nm_ids=[101],
                expenses_complete=True,
                final=True,
            ),
            "accepted quantity regression",
        )


def _assert_historical_doprinato_paid_boundary() -> None:
    with TemporaryDirectory(prefix="own-capital-doprinato-paid-boundary-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        block = OwnProductCapitalBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        block.record_supplier_payment(
            payment_id="bounded-payment",
            shipment_id="bounded-shipment",
            effective_date="2026-07-01",
            invoice_total_cny=5,
            paid_cny=5,
            paid_rub=500,
            product_lines=[
                {
                    "line_id": "bounded-line",
                    "nm_id": 101,
                    "qty": 5,
                    "unit_price": 1,
                    "match_status": "matched",
                }
            ],
            actual_ff_acceptance_date="2026-06-30",
            expenses_complete=True,
        )
        block.record_ordinary_wb_supply_acceptance(
            supply_id="bounded-wb",
            writeoff_date="2026-07-02",
            acceptance_date="2026-07-03",
            sent_quantities_by_nm={101: 5},
            accepted_quantities_by_nm={101: 2},
            physical_sent_quantities_by_nm={101: 10},
            physical_accepted_quantities_by_nm={101: 4},
            warehouse="Коледино",
            destination="ЦФО",
            known_nm_ids=[101],
            expenses_complete=True,
            final=True,
        )
        diagnostic_scope = block.matching_wb_outstanding_quantities(
            effective_date="2026-07-04",
            quantities_by_nm={101: 6},
            warehouse="Коледино",
            destination="ЦФО",
            original_supply_id="bounded-wb",
        )
        _eq(
            diagnostic_scope["tracked_available_by_nm"]["101"],
            "3",
            "bounded diagnostic tracked quantity",
        )
        _eq(
            diagnostic_scope["physical_available_by_nm"]["101"],
            "6",
            "bounded diagnostic physical quantity",
        )
        _eq(
            diagnostic_scope["candidates"][0]["original_supply_id"],
            "bounded-wb",
            "bounded diagnostic candidate identity",
        )
        reconciliation = block.reconcile_doprinato(
            reconciliation_supply_id="bounded-doprinato",
            effective_date="2026-07-04",
            quantities_by_nm={101: 6},
            warehouse="Коледино",
            destination="ЦФО",
            original_supply_id="bounded-wb",
        )
        _eq(len(reconciliation["closures"]), 1, "bounded Допринято closure count")
        _dec_eq(
            reconciliation["closures"][0]["quantity"],
            "3",
            "Допринято moves only tracked paid capital",
        )
        _dec_eq(
            reconciliation["closures"][0]["physical_quantity"],
            "6",
            "Допринято consumes exact physical outstanding",
        )
        _dec_eq(
            reconciliation["closures"][0]["untracked_physical_quantity"],
            "3",
            "unpaid physical remainder creates no capital",
        )
        with _connect(runtime.db_path) as conn:
            outstanding = conn.execute(
                """
                SELECT open_quantity, physical_open_quantity
                FROM sheet_vitrina_v1_own_capital_wb_outstanding
                WHERE original_supply_id='bounded-wb' AND nm_id=101
                """
            ).fetchone()
        _dec_eq(outstanding["open_quantity"], "0", "tracked outstanding closed")
        _dec_eq(
            outstanding["physical_open_quantity"],
            "0",
            "physical outstanding closed",
        )
        repeated = block.reconcile_doprinato(
            reconciliation_supply_id="bounded-doprinato",
            effective_date="2026-07-04",
            quantities_by_nm={101: 6},
            warehouse="Коледино",
            destination="ЦФО",
            original_supply_id="bounded-wb",
        )
        if not repeated["idempotent"]:
            raise AssertionError("bounded Допринято repeat must be idempotent")
        _must_fail(
            lambda: block.reconcile_doprinato(
                reconciliation_supply_id="bounded-physical-surplus",
                effective_date="2026-07-05",
                quantities_by_nm={101: 1},
                warehouse="Коледино",
                destination="ЦФО",
                original_supply_id="bounded-wb",
            ),
            "Допринято exceeding physical outstanding",
        )


def _assert_targeted_orphan_doprinato_classification() -> None:
    with TemporaryDirectory(prefix="own-capital-targeted-orphan-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        block = OwnProductCapitalBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        block.record_supplier_payment(
            payment_id="targeted-orphan-payment",
            shipment_id="targeted-orphan-shipment",
            effective_date="2026-07-01",
            invoice_total_cny=2,
            paid_cny=2,
            paid_rub=200,
            product_lines=[
                {
                    "line_id": "targeted-normal-line",
                    "nm_id": 391660889,
                    "qty": 1,
                    "unit_price": 1,
                    "match_status": "matched",
                },
                {
                    "line_id": "targeted-orphan-line",
                    "nm_id": 391663632,
                    "qty": 1,
                    "unit_price": 1,
                    "match_status": "matched",
                },
            ],
            actual_ff_acceptance_date="2026-06-30",
            expenses_complete=True,
        )
        block.record_ordinary_wb_supply_acceptance(
            supply_id="40433285",
            writeoff_date="2026-07-02",
            acceptance_date="2026-07-03",
            sent_quantities_by_nm={391660889: 1, 391663632: 1},
            accepted_quantities_by_nm={391660889: 0, 391663632: 1},
            warehouse="Склад Шушары",
            destination="Склад Шушары",
            known_nm_ids=[391660889, 391663632],
            expenses_complete=True,
            final=True,
        )
        before_lookup = block.load_daily_metric_lookup("2026-07-05")
        before_total = sum(
            (
                Decimal(str(before_lookup[nm_id][OWN_TOTAL_CAPITAL_RUB_METRIC_KEY]))
                for nm_id in (391660889, 391663632)
            ),
            Decimal("0"),
        )
        before_orphan_wb_qty = before_lookup[391663632][
            own_stage_metric_key(STAGE_WB, "qty")
        ]
        ff_operations_before = runtime.count_ff_stock_operations()
        result = block.reconcile_doprinato(
            reconciliation_supply_id="40654176",
            effective_date="2026-07-06",
            quantities_by_nm={391660889: 1, 391663632: 1},
            warehouse="Склад Шушары",
            destination="Склад Шушары",
            original_supply_id=None,
        )
        _eq(len(result["closures"]), 1, "targeted document ordinary closure count")
        _eq(result["closures"][0]["nm_id"], 391660889, "ordinary SKU identity")
        _dec_eq(result["closures"][0]["quantity"], "1", "ordinary SKU tracked transfer")
        _eq(len(result["classifications"]), 1, "targeted orphan classification count")
        classification = result["classifications"][0]
        _eq(classification["event_id"], TARGETED_ORPHAN_DOPRINATO_EVENT_ID, "orphan audit identity")
        _eq(classification["reason"], TARGETED_ORPHAN_DOPRINATO_REASON, "orphan audit reason")
        _eq(classification["nm_id"], 391663632, "orphan SKU identity")
        _dec_eq(classification["quantity"], "1", "orphan physical quantity")
        _dec_eq(classification["capital_rub"], "0", "orphan capital")
        after_lookup = block.load_daily_metric_lookup("2026-07-06")
        after_total = sum(
            (
                Decimal(str(after_lookup[nm_id][OWN_TOTAL_CAPITAL_RUB_METRIC_KEY]))
                for nm_id in (391660889, 391663632)
            ),
            Decimal("0"),
        )
        _dec_eq(after_total, before_total, "orphan classification preserves total capital")
        _dec_eq(
            after_lookup[391660889][own_stage_metric_key(STAGE_WB, "qty")],
            "1",
            "ordinary SKU reaches WB",
        )
        _dec_eq(
            after_lookup[391663632][own_stage_metric_key(STAGE_WB, "qty")],
            before_orphan_wb_qty,
            "orphan does not increase paid-capital WB quantity",
        )
        _eq(
            runtime.count_ff_stock_operations(),
            ff_operations_before,
            "orphan classification leaves FF quantity ledger unchanged",
        )
        with _connect(runtime.db_path) as conn:
            audit = conn.execute(
                """
                SELECT quantity, capital_rub, confirmed_quantity, payload_json
                FROM sheet_vitrina_v1_own_capital_events
                WHERE event_id = ?
                """,
                (TARGETED_ORPHAN_DOPRINATO_EVENT_ID,),
            ).fetchone()
        _dec_eq(audit["quantity"], "0", "orphan audit paid quantity")
        _dec_eq(audit["capital_rub"], "0", "orphan audit capital persisted")
        _dec_eq(audit["confirmed_quantity"], "0", "orphan audit confirmed quantity")
        if TARGETED_ORPHAN_DOPRINATO_REASON not in str(audit["payload_json"]):
            raise AssertionError(f"orphan audit reason missing: {dict(audit)}")
        repeated = block.reconcile_doprinato(
            reconciliation_supply_id="40654176",
            effective_date="2026-07-06",
            quantities_by_nm={391660889: 1, 391663632: 1},
            warehouse="Склад Шушары",
            destination="Склад Шушары",
            original_supply_id=None,
        )
        if not repeated["idempotent"]:
            raise AssertionError("targeted orphan classification repeat must be idempotent")
        with _connect(runtime.db_path) as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_own_capital_events
                SET capital_rub='1'
                WHERE event_id=?
                """,
                (TARGETED_ORPHAN_DOPRINATO_EVENT_ID,),
            )
            conn.commit()
        _must_fail(
            lambda: block.reconcile_doprinato(
                reconciliation_supply_id="40654176",
                effective_date="2026-07-06",
                quantities_by_nm={391660889: 1, 391663632: 1},
                warehouse="Склад Шушары",
                destination="Склад Шушары",
                original_supply_id=None,
            ),
            "targeted orphan corrupted repeat state",
        )
        with _connect(runtime.db_path) as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_own_capital_events
                SET capital_rub='0'
                WHERE event_id=?
                """,
                (TARGETED_ORPHAN_DOPRINATO_EVENT_ID,),
            )
            conn.commit()

        exact_candidate = [
            {
                "original_supply_id": "40433285",
                "open_quantity": "0",
                "physical_open_quantity": "0",
            }
        ]
        helper_kwargs = {
            "reconciliation_supply_id": "40654176",
            "effective_date": "2026-07-06",
            "requested": {391660889: Decimal("1"), 391663632: Decimal("1")},
            "nm_id": 391663632,
            "quantity": Decimal("1"),
            "warehouse": "Склад Шушары",
            "original_supply_id": None,
            "candidates": exact_candidate,
        }
        guard_mutations = [
            {"quantity": Decimal("2")},
            {"candidates": [{**exact_candidate[0], "original_supply_id": "40433286"}]},
            {"candidates": [{**exact_candidate[0], "open_quantity": "1"}]},
            {"candidates": [{**exact_candidate[0], "physical_open_quantity": "1"}]},
            {"candidates": []},
        ]
        for mutation in guard_mutations:
            kwargs = {**helper_kwargs, **mutation}
            _must_fail(
                lambda kwargs=kwargs: _plan_targeted_orphan_doprinato_classification(
                    **kwargs
                ),
                f"targeted orphan guard mutation {mutation}",
            )
        document_guard_mutations = [
            {"effective_date": "2026-07-07"},
            {"quantities_by_nm": {391660889: 1, 391663632: 2}},
            {"quantities_by_nm": {391660889: 1, 391663633: 1}},
            {"warehouse": "Другой склад"},
            {"original_supply_id": "40433285"},
        ]
        for mutation in document_guard_mutations:
            call = {
                "reconciliation_supply_id": "40654176",
                "effective_date": "2026-07-06",
                "quantities_by_nm": {391660889: 1, 391663632: 1},
                "warehouse": "Склад Шушары",
                "destination": "Склад Шушары",
                "original_supply_id": None,
                **mutation,
            }
            _must_fail(
                lambda call=call: block.reconcile_doprinato(**call),
                f"targeted orphan document guard mutation {mutation}",
            )
        _must_fail(
            lambda: block.reconcile_doprinato(
                reconciliation_supply_id="40654177",
                effective_date="2026-07-06",
                quantities_by_nm={391660889: 1, 391663632: 1},
                warehouse="Склад Шушары",
                destination="Склад Шушары",
                original_supply_id=None,
            ),
            "non-target Допринято remains fail closed",
        )


def _assert_persisted_expense_events() -> None:
    with TemporaryDirectory(prefix="own-capital-expense-events-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        header = {
            "shipment_id": "expense-shipment",
            "created_at": NOW,
            "updated_at": NOW,
            "shipment_date": "2026-07-01",
            "actual_shipment_date": "2026-07-02",
            "actual_ff_acceptance_date": "2026-07-08",
            "order_status": "accepted_ff",
            "expenses_complete": False,
            "invoice_amount_total": 200,
            "match_status": "matched",
        }
        shipment_lines = [
            {
                "line_id": "expense-line-a",
                "line_type": "product",
                "sort_order": 1,
                "internal_nm_id": 101,
                "qty": 10,
                "unit_price": 10,
                "amount": 100,
                "currency": "CNY",
                "match_status": "matched",
            },
            {
                "line_id": "expense-line-b",
                "line_type": "product",
                "sort_order": 2,
                "internal_nm_id": 202,
                "qty": 5,
                "unit_price": 20,
                "amount": 100,
                "currency": "CNY",
                "match_status": "matched",
            },
        ]
        runtime.save_supplier_shipment(header=header, lines=shipment_lines)
        _save_expense_document(
            runtime,
            document_id="expense-logistics",
            document_type="logistics_invoice",
            document_date="2026-07-05",
            lines=[
                {
                    "line_id": "expense-logistics-line",
                    "category": "delivery_cost",
                    "amount_rub": 1000,
                }
            ],
        )
        _save_expense_document(
            runtime,
            document_id="expense-customs",
            document_type="customs_declaration",
            document_date="2026-07-07",
            lines=[
                {
                    "line_id": "customs-fee",
                    "category": "customs_fee_1010",
                    "amount_rub": 200,
                    "included_in_customs_total": True,
                },
                {
                    "line_id": "customs-duty",
                    "category": "import_duty_2010",
                    "amount_rub": 300,
                    "included_in_customs_total": True,
                },
                {
                    "line_id": "customs-vat",
                    "category": "import_vat_5010",
                    "amount_rub": 500,
                    "included_in_customs_total": True,
                },
            ],
        )
        _save_expense_document(
            runtime,
            document_id="expense-bank-fee",
            document_type="bank_fee_statement",
            document_date="2026-07-10",
            parse_status="confirmed",
            lines=[
                {
                    "line_id": "bank-rub-line",
                    "category": "bank_fee",
                    "amount": 50,
                    "currency": "RUB",
                    "amount_rub": 50,
                    "raw": {"row": {"operation_date": "2026-07-09"}},
                },
                {
                    "line_id": "bank-cny-line",
                    "category": "bank_fee",
                    "amount": 10,
                    "currency": "CNY",
                    "amount_rub": 120,
                    "raw": {"row": {"operation_date": "2026-07-09"}},
                },
            ],
        )
        _save_expense_document(
            runtime,
            document_id="expense-bank-fee-cny-only",
            document_type="bank_fee_statement",
            document_date="2026-07-10",
            parse_status="confirmed",
            lines=[
                {
                    "line_id": "bank-cny-only-line",
                    "category": "bank_fee",
                    "amount": 10,
                    "currency": "CNY",
                    "amount_rub": 120,
                    "raw": {"row": {"operation_date": "2026-07-09"}},
                }
            ],
        )
        block = OwnProductCapitalBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        block.record_supplier_payment(
            payment_id="expense-base-payment",
            shipment_id="expense-shipment",
            effective_date="2026-07-01",
            invoice_total_cny=200,
            paid_cny=200,
            paid_rub=2000,
            product_lines=LINES,
            actual_shipment_date="2026-07-02",
            actual_ff_acceptance_date="2026-07-08",
            expenses_complete=False,
        )
        materialized = block.materialize_persisted_expense_events()
        _eq(materialized["blocker_count"], 0, "expense event blockers")
        _eq(materialized["created_event_group_count"], 3, "dated expense event groups")
        _eq(
            materialized["skipped_cny_ledger_only_document_count"],
            1,
            "CNY-only bank statement stays in CNY capital contour",
        )
        repeated = block.materialize_persisted_expense_events()
        _eq(repeated["created_event_group_count"], 0, "expense event dedupe")
        _eq(repeated["idempotent_event_group_count"], 3, "expense event idempotency")
        before_logistics = block.load_daily_metric_lookup("2026-07-04")[101]
        logistics_day = block.load_daily_metric_lookup("2026-07-05")[101]
        _dec_eq(
            Decimal(str(logistics_day[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY]))
            - Decimal(str(before_logistics[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY])),
            "500",
            "late logistics document starts on its effective date",
        )
        with _connect(runtime.db_path) as conn:
            rows = conn.execute(
                """
                SELECT effective_date, SUM(CAST(capital_rub AS REAL)) AS capital,
                       COUNT(*) AS row_count
                FROM sheet_vitrina_v1_own_capital_events
                WHERE event_type='cost_payment'
                  AND event_id LIKE 'cost_payment:financial_expense:%'
                GROUP BY effective_date ORDER BY effective_date
                """
            ).fetchall()
        by_date = {
            str(row["effective_date"]): (Decimal(str(row["capital"])), int(row["row_count"]))
            for row in rows
        }
        _dec_eq(by_date["2026-07-05"][0], "1000", "logistics capital once")
        _dec_eq(by_date["2026-07-07"][0], "1000", "customs duty/tax/VAT capital once")
        _dec_eq(by_date["2026-07-09"][0], "50", "direct RUB bank fee capital once")
        if "2026-07-10" in by_date:
            raise AssertionError("bank fee must use statement operation date, not document date")
        block.record_order_level_cost_payment(
            document_id="sfd_literal_aaaaaaaa",
            shipment_id="expense-shipment",
            effective_date="2026-07-10",
            capital_rub=10,
            product_lines=LINES,
            component="literal_identity_regression",
        )
        if block.has_cost_payment_event("sfd_literal_bbbbbbbb"):
            raise AssertionError("SQLite LIKE wildcards must not alias distinct financial document IDs")
        _save_expense_document(
            runtime,
            document_id="expense-bank-fee-rub-missing",
            document_type="bank_fee_statement",
            document_date="2026-07-10",
            parse_status="confirmed",
            lines=[
                {
                    "line_id": "bank-rub-missing-line",
                    "category": "bank_fee",
                    "amount": 50,
                    "currency": "RUB",
                    "amount_rub": None,
                    "raw": {"row": {"operation_date": "2026-07-09"}},
                }
            ],
        )
        missing_rub = block.materialize_persisted_expense_events()
        _eq(missing_rub["blocker_count"], 1, "missing direct-RUB amount stays fail closed")
        _eq(
            missing_rub["blockers"][0]["document_id"],
            "expense-bank-fee-rub-missing",
            "missing direct-RUB blocker identity",
        )


def _assert_historical_source_backfill() -> None:
    with TemporaryDirectory(prefix="own-capital-history-backfill-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        runtime.save_nomenclature_item(
            {
                "item_id": "history-item",
                "is_active": True,
                "our_sku": "HISTORY-101",
                "nm_id": 101,
                "vendor_code": "HISTORY-101",
                "barcode": "101",
                "nomenclature_name": "History fixture",
                "product_type": "other",
                "match_key": "other|history_fixture",
                "purchase_price_yuan": 10,
                "aliases": [],
                "compatible_models_text": "",
                "compatible_model_keys": [],
                "comment": "",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        runtime.save_supplier_shipment(
            header={
                "shipment_id": "history-shipment",
                "created_at": NOW,
                "updated_at": NOW,
                "shipment_date": "2026-07-01",
                "actual_shipment_date": "2026-07-02",
                "actual_ff_acceptance_date": "2026-07-03",
                "order_status": "accepted_ff",
                "expenses_complete": True,
                "invoice_amount_total": 100,
                "match_status": "matched",
            },
            lines=[
                {
                    "line_id": "history-line",
                    "line_type": "product",
                    "sort_order": 1,
                    "internal_nm_id": 101,
                    "qty": 10,
                    "unit_price": 10,
                    "amount": 100,
                    "currency": "CNY",
                    "match_status": "matched",
                }
            ],
        )
        runtime.save_cny_document(
            {
                "document_id": "history-payment",
                "document_type": "supplier_cny_payment",
                "source": "history_fixture",
                "source_order_id": "history-shipment",
                "uploaded_at": NOW,
                "created_at": NOW,
                "updated_at": NOW,
                "operation_date": "2026-07-01",
                "status": "posted",
                "currency": "CNY",
                "cny_amount": "100",
                "rub_amount": "1000",
                "parsed_payload": {
                    "payment_date_provenance": {"source": "persisted_fixture"}
                },
            }
        )
        runtime.replace_cny_ledger_operations(
            [
                {
                    "operation_id": "history-payment-op",
                    "operation_type": "supplier_payment_out",
                    "source_document_id": "history-payment",
                    "source_order_id": "history-shipment",
                    "operation_date": "2026-07-01",
                    "sequence_key": "00000001",
                    "cny_delta": "-100",
                    "rub_value_delta": "-1000",
                    "status": "posted",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ]
        )
        runtime.create_ff_stock_operation(
            operation_id="history-wb-debit",
            operation_type="auto_writeoff",
            source_type="wb_supply",
            source_key="wb_supply_debit:supply:history-wb",
            source_object_id="history-wb",
            source_object_label="history-wb",
            created_at="2026-07-04T10:00:00Z",
            created_by="system",
            lines=[{"nm_id": 101, "quantity_delta": -4}],
        )
        runtime.create_ff_stock_operation(
            operation_id="history-overaccepted-wb-debit",
            operation_type="auto_writeoff",
            source_type="wb_supply",
            source_key="wb_supply_debit:supply:history-overaccepted",
            source_object_id="history-overaccepted",
            source_object_label="history-overaccepted",
            created_at="2026-07-05T10:00:00Z",
            created_by="system",
            lines=[{"nm_id": 101, "quantity_delta": -10}],
        )
        runtime.save_wb_supply_rows(
            rows=[
                {
                    "supply_id": "history-wb",
                    "cache_key": "supply:history-wb",
                    "wb_supply_id": "history-wb",
                    "preorder_id": "history-preorder",
                    "number_label": "history-wb",
                    "status_id": 3,
                    "status_label": "Отгрузка разрешена",
                    "warehouse_name": "Коледино",
                    "supply_date": "2026-07-04T10:00:00Z",
                    "source_created_at": "2026-07-04T10:00:00Z",
                    "raw_list": {
                        "supplyID": "history-wb",
                        "statusID": 3,
                        "supplyDate": "2026-07-04T10:00:00Z",
                    },
                    "raw_goods": [{"nmID": 101, "quantity": 4}],
                    "raw_package": [],
                },
                {
                    "supply_id": "history-overaccepted",
                    "cache_key": "supply:history-overaccepted",
                    "wb_supply_id": "history-overaccepted",
                    "preorder_id": "history-overaccepted-preorder",
                    "number_label": "history-overaccepted",
                    "status_id": 5,
                    "status_label": "Принято",
                    "warehouse_name": "Коледино",
                    "supply_date": "2026-07-05T10:00:00Z",
                    "source_created_at": "2026-07-05T10:00:00Z",
                    "actual_acceptance_date": "2026-07-05T12:00:00Z",
                    "raw_list": {
                        "supplyID": "history-overaccepted",
                        "statusID": 5,
                        "supplyDate": "2026-07-05T10:00:00Z",
                    },
                    "raw_goods": [
                        {"nmID": 101, "quantity": 10, "acceptedQuantity": 12}
                    ],
                    "raw_package": [],
                },
                {
                    "supply_id": "history-untracked-doprinato",
                    "cache_key": "supply:history-untracked-doprinato",
                    "wb_supply_id": "history-untracked-doprinato",
                    "preorder_id": "history-untracked-doprinato-preorder",
                    "number_label": "history-untracked-doprinato",
                    "status_id": 5,
                    "status_label": "Принято",
                    "virtual_type_id": 5,
                    "type_label": "Допринято",
                    "warehouse_name": "Электросталь",
                    "source_created_at": "2026-07-05T13:00:00Z",
                    "actual_acceptance_date": "2026-07-05T13:00:00Z",
                    "raw_list": {
                        "supplyID": "history-untracked-doprinato",
                        "statusID": 5,
                        "createDate": "2026-07-05T13:00:00Z",
                    },
                    "raw_goods": [
                        {"nmID": 101, "quantity": 1, "acceptedQuantity": 1}
                    ],
                    "raw_package": [],
                },
                {
                    "supply_id": "history-old-doprinato",
                    "cache_key": "supply:history-old-doprinato",
                    "wb_supply_id": "history-old-doprinato",
                    "preorder_id": "history-old-doprinato-preorder",
                    "number_label": "history-old-doprinato",
                    "status_id": 5,
                    "status_label": "Принято",
                    "virtual_type_id": 5,
                    "type_label": "Допринято",
                    "warehouse_name": "Коледино",
                    "source_created_at": "2025-01-17T10:00:00Z",
                    "actual_acceptance_date": "2025-01-17T10:00:00Z",
                    "raw_list": {
                        "supplyID": "history-old-doprinato",
                        "statusID": 5,
                        "createDate": "2025-01-17T10:00:00Z",
                    },
                    "raw_goods": [
                        {"nmID": 101, "quantity": 1, "acceptedQuantity": 1}
                    ],
                    "raw_package": [],
                }
            ],
            warehouses=[],
            synced_at=NOW,
        )
        OwnProductCapitalBlock(runtime=runtime, timestamp_factory=lambda: NOW).status()
        args = argparse.Namespace(
            runtime_dir=str(runtime.runtime_dir),
            date_from="2026-07-01",
            date_to="2026-07-05",
            apply=False,
            fingerprint="",
            backup_dir=str(runtime.runtime_dir / "backups"),
        )
        dry = run_backfill(args)
        _eq(dry["cny_materialization"]["persisted_operation_count"], 1, "CNY history source count")
        _eq(dry["wb_materialization"]["persisted_supply_count"], 3, "WB history source count")
        _eq(
            dry["wb_materialization"]["skipped_before_paid_ownership_count"],
            1,
            "pre-ownership WB history skip count",
        )
        _eq(
            dry["wb_materialization"]["skipped_doprinato_without_tracked_outstanding_count"],
            1,
            "post-ownership untracked Допринято skip count",
        )
        diagnostic_codes = {
            str(item.get("code") or "")
            for item in dry["wb_materialization"]["bounded_paid_quantity_diagnostics"]
            if str(item.get("supply_id") or "") == "history-overaccepted"
        }
        if diagnostic_codes != {
            "physical_wb_quantity_partially_outside_paid_capital",
            "physical_accepted_quantity_partially_outside_paid_capital",
            "physical_accepted_quantity_exceeds_sent_layer",
        }:
            raise AssertionError(f"historical paid-capital diagnostics mismatch: {diagnostic_codes}")
        if dry["candidate_preflight"]["unresolved_blocker_count"]:
            raise AssertionError(f"historical source backfill unexpectedly blocked: {dry}")
        args.apply = True
        args.fingerprint = dry["fingerprint"]
        applied = run_backfill(args)
        if not applied["applied"]:
            raise AssertionError(f"historical source backfill did not apply: {applied}")
        with _connect(runtime.db_path) as conn:
            payment = conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_own_capital_payment_layers WHERE payment_id='history-payment'"
            ).fetchone()[0]
            wb = conn.execute(
                """
                SELECT quantity FROM sheet_vitrina_v1_own_capital_events
                WHERE event_id='stage_transfer:wb_supply:history-wb:101'
                """
            ).fetchone()
            overaccepted = conn.execute(
                """
                SELECT quantity FROM sheet_vitrina_v1_own_capital_events
                WHERE event_id='stage_transfer:wb_supply:history-overaccepted:101'
                """
            ).fetchone()
        _eq(payment, 1, "persisted CNY payment layer restored")
        _dec_eq(wb["quantity"], "4", "persisted WB movement restored")
        _dec_eq(
            overaccepted["quantity"],
            "6",
            "historical physical movement is bounded to paid FF capital",
        )
        second = run_backfill(
            argparse.Namespace(
                runtime_dir=str(runtime.runtime_dir),
                date_from="2026-07-01",
                date_to="2026-07-05",
                apply=False,
                fingerprint="",
                backup_dir="",
            )
        )
        if second["would_change"]:
            raise AssertionError(f"historical source backfill repeat changed rows: {second}")


def _save_expense_document(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    document_id: str,
    document_type: str,
    document_date: str,
    lines: list[dict],
    parse_status: str = "parsed",
) -> None:
    runtime.save_supplier_financial_document(
        document={
            "document_id": document_id,
            "supplier_order_id": "expense-shipment",
            "document_type": document_type,
            "original_filename": f"{document_id}.pdf",
            "uploaded_at": NOW,
            "updated_at": NOW,
            "parse_status": parse_status,
            "document_date": document_date,
            "currency": "RUB",
            "warnings": [],
            "errors": [],
        },
        expense_lines=[
            {
                "financial_document_id": document_id,
                "supplier_order_id": "expense-shipment",
                "sort_order": index,
                "stage": "expense",
                "description": str(line.get("category") or "expense"),
                "amount": line.get("amount") or line.get("amount_rub"),
                "currency": line.get("currency") or "RUB",
                "amount_rub": line.get("amount_rub"),
                "status": "parsed",
                "confidence": 1,
                "included_in_logistics_efficiency": bool(
                    document_type == "logistics_invoice"
                ),
                **line,
            }
            for index, line in enumerate(lines, start=1)
        ],
    )


def _config(nm_id: int, order: int) -> ConfigV2Item:
    return ConfigV2Item(
        nm_id=nm_id,
        enabled=True,
        display_name=f"SKU {nm_id}",
        group="group",
        display_order=order,
    )


def _must_fail(callable_obj, label: str) -> None:
    try:
        callable_obj()
    except ValueError:
        return
    raise AssertionError(f"{label} must fail closed")


def _eq(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: {actual!r} != {expected!r}")


def _dec_eq(actual, expected, label: str) -> None:
    if abs(Decimal(str(actual)) - Decimal(str(expected))) > Decimal("0.00001"):
        raise AssertionError(f"{label}: {actual!r} != {expected!r}")


if __name__ == "__main__":
    main()
