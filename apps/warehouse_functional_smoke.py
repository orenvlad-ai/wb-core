#!/usr/bin/env python3
"""Focused contract smoke for functional warehouses, WAC and Proxy 3."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.calculation_parameters import (  # noqa: E402
    CalculationParametersBlock,
    DEFAULT_PROXY_PARAMETERS,
    aggregate_proxy_3,
    calculate_proxy_3,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.supplier_financial_documents import build_financial_summary  # noqa: E402
from packages.application.wb_supplies import validate_functional_supply_sync  # noqa: E402
from packages.application.warehouse_functional import (  # noqa: E402
    FUNCTIONAL_CUTOVER_ID,
    STAGES,
    STAGE_DISCREPANCY,
    STAGE_WB,
    WarehouseFunctionalBlock,
    WarehouseLine,
    _calculation_digest,
    _counted_cny_operation,
    _fingerprint,
    _guarded_local_sources,
    _line_payload,
    _nomenclature_purchase_prices,
    _supply_downstream_component_index,
    _supply_revision,
    _summaries,
    _validated_financial_expense,
    accepted_capital_delta,
    accepted_quantity_delta,
    allocate_capital,
    build_frozen_opening_cost_map,
    build_historical_wb_cost_projection,
    compose_supply_costs,
    moving_weighted_average,
    reconcile_discrepancies,
    roll_periodic_wac,
    validate_cutover_ff_debit_coverage,
)
from packages.application.warehouse_functional_economics_backfill import (  # noqa: E402
    apply_functional_economics_backfill_plan,
    build_functional_economics_backfill_plan,
)
from apps.warehouse_functional_runner import _verify_cutover_external_recheck  # noqa: E402


NOW = "2026-07-18T12:00:00Z"
DRY_RUN_AT = "2026-07-18T11:55:00Z"


def main() -> None:
    _test_decimal_and_allocations()
    _test_accepted_source_correction()
    _test_paid_acceptance_cost_boundary()
    _test_financial_document_eligibility()
    _test_discrepancy_pool()
    _test_cutover_ff_debit_coverage()
    _test_frozen_cost_map()
    _test_nomenclature_purchase_price_source()
    _test_historical_wb_projection()
    _test_proxy()
    _test_versioned_parameters_and_reference()
    _test_initial_settings_preserve_outer_transaction()
    _test_external_optimistic_recheck()
    _test_semantic_digest_ignores_volatile_capture_identity()
    _test_source_capture_exposes_calculation_timestamp()
    _test_supply_refresh_completeness_gate()
    _test_guarded_publication()
    print("warehouse functional smoke: ok")


def _test_decimal_and_allocations() -> None:
    qty, capital, wac = moving_weighted_average(
        quantity="3", capital="30", inbound_quantity="2", inbound_capital="30"
    )
    _assert((qty, capital, wac) == (Decimal("5"), Decimal("60"), Decimal("12")), "moving WAC")
    value = allocate_capital(
        [
            {"nm_id": 1, "quantity": "100", "invoice_value": "10"},
            {"nm_id": 2, "quantity": "1", "invoice_value": "30"},
        ],
        total_capital="80",
        method="invoice_value",
    )
    _assert(value == {1: Decimal("20"), 2: Decimal("60")}, "invoice-value allocation")
    quantity = allocate_capital(
        [
            {"nm_id": 1, "quantity": "3", "invoice_value": "10"},
            {"nm_id": 2, "quantity": "1", "invoice_value": "30"},
        ],
        total_capital="1",
        method="quantity",
    )
    _assert(sum(quantity.values(), Decimal("0")) == Decimal("1"), "allocation conserves exact capital")


def _test_accepted_source_correction() -> None:
    _assert(
        accepted_quantity_delta(packed="31500", accepted="31477", previously_posted="0") == Decimal("31477"),
        "first final acceptance posts cumulative accepted quantity",
    )
    _assert(
        accepted_quantity_delta(packed="31500", accepted="31477", previously_posted="31477") == Decimal("0"),
        "unchanged source revision is idempotent",
    )
    correction = accepted_quantity_delta(packed="31500", accepted="31470", previously_posted="31477")
    _assert(correction == Decimal("-7"), "accepted regression posts only correction delta")
    _assert(
        accepted_capital_delta(
            packed="100",
            accepted="100",
            unit_cost="125",
            previously_posted_capital="12000",
        )
        == Decimal("500"),
        "late accepted cost evidence posts a capital-only correction",
    )
    qty, capital, wac = roll_periodic_wac(
        quantity="100", capital="12000", quantity_delta=correction, capital_delta=correction * Decimal("120")
    )
    _assert((qty, capital, wac) == (Decimal("93"), Decimal("11160"), Decimal("120")), "correction reverses capital once")
    try:
        roll_periodic_wac(quantity="1", capital="120", quantity_delta="-2", capital_delta="-240")
    except Exception:
        pass
    else:
        raise AssertionError("correction must not create a negative WB cost pool")


def _test_paid_acceptance_cost_boundary() -> None:
    seed = build_frozen_opening_cost_map(
        target_nm_ids=[1],
        primary_rows=[{"nm_id": 1, "quantity": "1", "purchase_price_cny": "1", "ff_unit_cost_rub": "80"}],
        purchase_price_by_nm={1: "1"},
        downstream_rows=[_downstream_row("supply-1", 1, quantity="1", transit="20", acceptance="20")],
        primary_identity={"shipment_id": "baseline"},
    )
    rows = [
        {
            "wb_supply_id": "supply-1",
            "nm_id": 1,
            "transit_cost_status": "transit_confirmed",
            "transit_per_unit_rub": "20",
            "ff_services_per_unit_rub": "0",
            "ff_storage_per_unit_rub": "0",
            "wb_acceptance_per_accepted_unit_rub": "20",
            "inputs_hash": "layer-v1",
        }
    ]
    component = _supply_downstream_component_index(rows)[("supply-1", 1)]
    pre_acceptance, accepted = compose_supply_costs(
        outbound_ff_wac=seed[1].ff_unit_cost,
        pre_acceptance_addon=component["pre_acceptance_addon"],
        acceptance_addon=component["acceptance_addon"],
    )
    _assert(accepted == Decimal("120"), "accepted WB inbound includes paid acceptance")
    _assert(pre_acceptance == Decimal("100"), "FF-to-WB and discrepancy exclude paid acceptance")


def _test_financial_document_eligibility() -> None:
    parsed = {"document_id": "parsed", "document_type": "logistics_invoice", "parse_status": "parsed"}
    review = {"document_id": "review", "document_type": "logistics_invoice", "parse_status": "needs_review"}
    confirmed = {"document_id": "confirmed", "document_type": "bank_fee_statement", "parse_status": "confirmed"}
    failed_line = {"financial_document_id": "parsed", "status": "failed"}
    _assert(
        _validated_financial_expense(document=parsed, expense={"status": "parsed"}),
        "validated parsed source participates",
    )
    _assert(
        not _validated_financial_expense(document=review, expense={"status": "parsed"})
        and not _validated_financial_expense(document=parsed, expense=failed_line),
        "review and failed sources are excluded",
    )
    summary = build_financial_summary(
        [parsed, review, confirmed],
        [
            {
                "financial_document_id": "parsed",
                "status": "parsed",
                "category": "logistics_invoice",
                "amount_rub": "100",
                "currency": "RUB",
            },
            {
                "financial_document_id": "review",
                "status": "parsed",
                "category": "logistics_invoice",
                "amount_rub": "900",
                "currency": "RUB",
            },
            {
                "financial_document_id": "confirmed",
                "status": "parsed",
                "category": "bank_transfer_fee",
                "amount": "20",
                "amount_rub": "20",
                "currency": "RUB",
            },
        ],
        shipment={
            "header": {
                "cny_payment_currency_rub_cost": "1000",
                "cny_calculation_status": "ok",
                "cny_bank_fee_rub": "0",
            },
            "lines": [{"line_type": "product", "qty": "10"}],
        },
    )
    _assert(summary["invoices"]["fact_rub"] == 100.0, "needs-review logistics is not capitalized")
    _assert(summary["per_unit"]["exact_bank_fees_rub"] == 20.0, "confirmed RUB bank fee is included once")
    _assert(summary["per_unit"]["exact_landed_cost_total_rub"] == 1120.0, "exact cost uses eligible sources only")
    _assert(
        _counted_cny_operation({"status": "needs_review", "document_status": "posted"}),
        "counted date-only CNY ordering warning remains eligible",
    )
    _assert(
        not _counted_cny_operation({"status": "needs_review", "document_status": "needs_review"})
        and not _counted_cny_operation({"status": "blocked", "document_status": "posted"}),
        "review documents and blocked CNY operations remain excluded",
    )


def _test_discrepancy_pool() -> None:
    audit: list[dict[str, object]] = []
    balances, unmatched = reconcile_discrepancies(
        discrepancies=[
            {"source_id": "s1", "nm_id": 10, "quantity": "5", "capital": "100"},
            {"source_id": "s2", "nm_id": 11, "quantity": "2", "capital": "60"},
        ],
        doprinato=[
            {"source_id": "d1", "business_date": "2026-07-18", "nm_id": 10, "quantity": "7"},
            {"source_id": "d2", "business_date": "2026-07-18", "nm_id": 99, "quantity": "1"},
        ],
        audit=audit,
    )
    _assert({item["nm_id"] for item in balances} == {11}, "doprinato does not consume another SKU")
    unmatched_by_nm = {int(item["nm_id"]): Decimal(str(item["quantity"])) for item in unmatched}
    _assert(unmatched_by_nm == {10: Decimal("2"), 99: Decimal("1")}, "unmatched is quarantined")
    _assert(all(Decimal(str(item["quantity"])) >= 0 for item in balances), "no negative discrepancy")
    _assert({item["source_id"] for item in audit} == {"d1", "d2"}, "doprinato movement audit is complete")


def _test_cutover_ff_debit_coverage() -> None:
    supply = {
        "supply_id": "wb-new",
        "wb_supply_id": "wb-new",
        "status_id": 4,
        "fact_date": "2026-07-18",
        "raw_goods_json": json.dumps([{"nmID": 1, "quantity": 10, "acceptedQuantity": 0}]),
    }
    capture = {
        "wb_supplies": [supply],
        "ff_operations": [
            {"source_type": "wb_supply", "source_object_id": "wb-new"},
        ],
        "ff_auto_writeoff_checkpoint": [
            {
                "checkpoint_id": "checkpoint",
                "created_at": "2026-07-01T00:00:00Z",
                "baseline_supply_ids_json": "[]",
            }
        ],
    }
    result = validate_cutover_ff_debit_coverage(capture)
    _assert(result["covered_supply_count"] == 1, "gated supply has one exact FF debit coverage")
    try:
        validate_cutover_ff_debit_coverage({**capture, "ff_operations": []})
    except Exception as exc:
        _assert("without FF debit" in str(exc), "missing post-checkpoint FF debit blocks cutover")
    else:
        raise AssertionError("uncovered post-checkpoint WB supply must block cutover")


def _test_frozen_cost_map() -> None:
    result = build_frozen_opening_cost_map(
        target_nm_ids=[1, 2, 3, 4, 5],
        primary_rows=[
            {"nm_id": 1, "quantity": "10", "purchase_price_cny": "10", "ff_unit_cost_rub": "100"},
            {"nm_id": 2, "quantity": "20", "purchase_price_cny": "20", "ff_unit_cost_rub": "180"},
        ],
        purchase_price_by_nm={2: "20", 3: "15", 4: "30"},
        downstream_rows=[
            _downstream_row("wb-1", 1, quantity="10", transit="20"),
            _downstream_row("wb-2", 2, quantity="20", transit="36"),
        ],
        primary_identity={"shipment_id": "24.06"},
    )
    _assert(result[1].quality == "direct_24_06", "direct opening quality")
    _assert(result[2].quality == "direct_24_06", "direct takes precedence over same band")
    _assert(result[3].quality == "interpolation", "interpolation quality")
    _assert(result[4].quality == "extrapolation", "extrapolation quality")
    _assert(result[5].quality == "fallback_average", "fallback quality")
    _assert(all(item.wb_unit_cost > item.ff_unit_cost > 0 for item in result.values()), "downstream WB cost coverage")


def _test_nomenclature_purchase_price_source() -> None:
    prices = _nomenclature_purchase_prices(
        [
            {"item_id": "a", "nm_id": 1, "purchase_price_yuan": "10"},
            {"item_id": "b", "nm_id": 1, "purchase_price_yuan": "10.0"},
            {"item_id": "c", "nm_id": 2, "purchase_price_yuan": None},
        ]
    )
    _assert(prices == {1: Decimal("10")}, "opening price bands use active nomenclature")
    try:
        _nomenclature_purchase_prices(
            [
                {"item_id": "a", "nm_id": 1, "purchase_price_yuan": "10"},
                {"item_id": "b", "nm_id": 1, "purchase_price_yuan": "11"},
            ]
        )
    except Exception as exc:
        _assert("conflicting CNY purchase prices" in str(exc), "nomenclature conflict fails closed")
    else:
        raise AssertionError("conflicting active nomenclature prices must block cutover")


def _test_historical_wb_projection() -> None:
    opening = [
        {
            "nm_id": 1,
            "ff_unit_cost_rub": "80",
            "wb_unit_cost_rub": "100",
            "quality": "direct_24_06",
            "provenance": {"frozen": True},
        }
    ]
    projection = build_historical_wb_cost_projection(
        opening_cost_map=opening,
        daily_quantity_rows=[
            {"as_of_date": "2026-07-01", "nm_id": 1, "physical_quantity": "10"},
            {"as_of_date": "2026-07-02", "nm_id": 1, "physical_quantity": "0"},
            {"as_of_date": "2026-07-03", "nm_id": 1, "physical_quantity": "4"},
        ],
        downstream_rows=[
            _downstream_row("wb-1", 1, quantity="4", accepted_date="2026-07-03", transit="40")
        ],
        cutover_date="2026-07-18",
    )
    by_date = {item["as_of_date"]: item for item in projection}
    _assert(by_date["2026-07-01"]["wac_rub"] == "100", "historical opening uses frozen WAC")
    _assert(by_date["2026-07-02"]["wac_rub"] == "100", "zero stock retains last valid WAC")
    _assert(by_date["2026-07-03"]["wac_rub"] == "120", "known downstream inbound rolls daily WAC")
    corrected = build_historical_wb_cost_projection(
        opening_cost_map=opening,
        daily_quantity_rows=[
            {"as_of_date": "2026-07-01", "nm_id": 1, "physical_quantity": "10"},
            {"as_of_date": "2026-07-02", "nm_id": 1, "physical_quantity": "0"},
            {"as_of_date": "2026-07-03", "nm_id": 1, "physical_quantity": "4"},
        ],
        downstream_rows=[
            _downstream_row("wb-1", 1, quantity="4", accepted_date="2026-07-03", transit="50")
        ],
        cutover_date="2026-07-18",
    )
    _assert(
        {item["as_of_date"]: item for item in corrected}["2026-07-03"]["wac_rub"] == "130",
        "late confirmed downstream expense replays historical WAC from its effective date",
    )


def _test_proxy() -> None:
    row = calculate_proxy_3(
        order_sum="1000",
        order_count="10",
        canonical_wb_wac="20",
        ads_sum="50",
        parameters=DEFAULT_PROXY_PARAMETERS,
    )
    expected = Decimal("1000") * Decimal("0.5096") - Decimal("10") * Decimal("0.91") * Decimal("20") - Decimal("50")
    _assert(row["proxy_profit_3"] == expected, "defaults preserve Proxy profit coefficient")
    _assert(row["proxy_margin_3"] == expected / Decimal("910"), "margin denominator is expected buyout revenue")
    missing = calculate_proxy_3(
        order_sum="1000", order_count="10", canonical_wb_wac=None, ads_sum="0", parameters=DEFAULT_PROXY_PARAMETERS
    )
    _assert(missing["proxy_profit_3"] is None, "missing operand is not zero")
    zero = calculate_proxy_3(
        order_sum="0", order_count="0", canonical_wb_wac="20", ads_sum="0", parameters=DEFAULT_PROXY_PARAMETERS
    )
    _assert(zero["proxy_margin_3"] is None, "zero denominator is null")
    total = aggregate_proxy_3(
        [
            {"proxy_profit_3": "10", "expected_buyout_revenue": "100"},
            {"proxy_profit_3": "30", "expected_buyout_revenue": "300"},
        ]
    )
    _assert(total["proxy_margin_3"] == Decimal("0.1"), "TOTAL sums before division")


def _test_versioned_parameters_and_reference() -> None:
    with tempfile.TemporaryDirectory(prefix="calculation-parameters-") as temp_dir:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(temp_dir) / "runtime")
        block = CalculationParametersBlock(runtime=runtime)
        _assert(block.get_payload()["status"] == "awaiting_functional_cutover", "settings wait for cutover")
        payload = {
            "effective_date": "2026-07-15",
            "buyout_rate": "0.8",
            "tax_rate": "0.05",
            "wb_agent_and_other_rate": "0.3",
            "acquiring_rate": "0.01",
            "wb_logistics_rate": "0",
            "wb_storage_rate": "0",
            "penalties_adjustments_rate": "0",
            "other_expense_rate": "0",
        }
        pending_preview = block.preview_version(payload)
        try:
            block.create_version(
                payload,
                preview_fingerprint=pending_preview["preview_fingerprint"],
                created_by="smoke",
            )
        except ValueError as exc:
            _assert("before the functional cutover initial version" in str(exc), "pre-cutover settings save is blocked")
        else:
            raise AssertionError("operator settings must not preempt the cutover initial version")
        created = block.ensure_initial_version(created_at=NOW)
        _assert(created["parameters"]["buyout_rate_pct"] == "91", "default buyout 91%")
        _assert(created["parameters"]["included_expense_rate_pct"] == "44", "default expenses 44%")
        _assert(created["parameters"]["retained_share_pct"] == "56", "default retained share 56%")
        preview = block.preview_version(payload)
        saved = block.create_version(
            payload,
            preview_fingerprint=preview["preview_fingerprint"],
            created_by="smoke",
        )
        _assert(len(saved["history"]) == 2, "settings save appends a version")
        _assert(block.parameters_for_date("2026-07-14").buyout_rate == Decimal("0.91"), "effective date keeps earlier version")
        _assert(block.parameters_for_date("2026-07-15").buyout_rate == Decimal("0.8"), "effective version activates on date")
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """CREATE TABLE wb_finance_weekly_aggregates(
                       seller_id TEXT,week_start TEXT,week_end TEXT,classifier_version TEXT,
                       metrics_json TEXT,report_ids_json TEXT,report_types_json TEXT,
                       unknown_reasons_json TEXT,calculated_at TEXT,
                       PRIMARY KEY(seller_id,week_start,week_end))"""
            )
            today = datetime.now(timezone.utc).date()
            last_closed = today - timedelta(days=today.weekday() + 1)
            for week_index in range(3):
                week_end = last_closed - timedelta(days=7 * (2 - week_index))
                week_start = week_end - timedelta(days=6)
                for seller_id, revenue, commission, acquiring in (
                    ("seller-a", Decimal("100"), Decimal("30"), Decimal("5")),
                    ("seller-b", Decimal("50"), Decimal("10"), Decimal("2")),
                ):
                    metrics = {
                        "net_revenue": str(revenue),
                        "commission": str(commission),
                        "acquiring": str(acquiring),
                        "logistics": "3",
                        "storage": "2",
                        "acceptance": "1",
                        "penalties": "0",
                    }
                    conn.execute(
                        "INSERT INTO wb_finance_weekly_aggregates VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            seller_id,
                            week_start.isoformat(),
                            week_end.isoformat(),
                            "v1",
                            json.dumps(metrics),
                            "[]",
                            "[]",
                            "[]",
                            NOW,
                        ),
                    )
            conn.commit()
        reference = block.get_payload()["reference"]
        _assert(reference["status"] == "ready" and len(reference["weeks"]) == 3, "three closed weeks")
        by_key = {row["key"]: row for row in reference["rows"]}
        _assert(by_key["wb_agent_and_other"]["weighted_average_pct"] == "22", "weighted reference uses sums and excludes acquiring duplication")
        _assert(by_key["acquiring"]["weighted_average_pct"] == "4.666666666666666666666666667", "acquiring reference is separately classified")
        _assert(by_key["acceptance"]["included_in_proxy_by_default"] is False, "paid acceptance is reference-only")


def _test_initial_settings_preserve_outer_transaction() -> None:
    with tempfile.TemporaryDirectory(prefix="warehouse-atomic-settings-") as temp_dir:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(temp_dir))
        block = WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers(
                       cutover_id,cutover_at,status,plan_fingerprint,source_watermarks_json,
                       absorbed_supply_revisions_json,backup_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "atomicity_probe",
                    NOW,
                    "posted",
                    "sha256:atomicity-probe",
                    "{}",
                    "{}",
                    "{}",
                    NOW,
                    NOW,
                ),
            )
            block.calculation_parameters.ensure_initial_version(connection=conn, created_at=NOW)
            conn.rollback()
        with sqlite3.connect(runtime.db_path) as conn:
            cutovers = conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id='atomicity_probe'"
            ).fetchone()[0]
            settings = conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_calculation_parameter_versions"
            ).fetchone()[0]
        _assert(cutovers == 0 and settings == 0, "initial settings stay inside outer cutover transaction")


def _test_external_optimistic_recheck() -> None:
    source = {
        "local_source_digest": "sha256:local",
        "wb_supply_source_digest": "sha256:supply",
        "calculation_digest": "sha256:calculation",
        "wb_snapshot": {
            "raw_rows_digest": "sha256:stock",
            "requested_nm_ids": [1, 2],
            "raw_row_count": 2,
            "pagination_complete": True,
        },
    }
    _verify_cutover_external_recheck(source, json.loads(json.dumps(source)))
    drifted = json.loads(json.dumps(source))
    drifted["wb_snapshot"]["raw_rows_digest"] = "sha256:changed"
    try:
        _verify_cutover_external_recheck(source, drifted)
    except RuntimeError:
        pass
    else:
        raise AssertionError("official WB source drift must block functional cutover apply")
    calculation_drifted = json.loads(json.dumps(source))
    calculation_drifted["calculation_digest"] = "sha256:changed-calculation"
    try:
        _verify_cutover_external_recheck(source, calculation_drifted)
    except RuntimeError:
        pass
    else:
        raise AssertionError("derived semantic drift must block functional cutover apply")
    guarded = _guarded_local_sources(
        {
            "wb_supplies": [{"revision": "official"}],
            "downstream_cost_rows": [{"inputs_hash": "derived"}],
            "fulfillment_service_uploads": [{"file_sha256": "primary"}],
        }
    )
    _assert("wb_supplies" not in guarded, "disposable official supply rows use their own digest")
    _assert("downstream_cost_rows" not in guarded, "disposable derived layers are not a production local guard")
    _assert(
        "fulfillment_service_uploads" in guarded,
        "FF service primary evidence remains in the local source guard",
    )
    revision_source = {
        "supply_id": "wb-1",
        "status_id": 4,
        "normalized_row_json": json.dumps(
            {
                "packed_quantity": 100,
                "accepted_quantity": 90,
                "synced_at": "2026-07-18T12:00:00Z",
                "last_list_synced_at": "2026-07-18T12:00:00Z",
                "last_enriched_at": "2026-07-18T12:00:00Z",
            },
            sort_keys=True,
        ),
        "raw_goods_hash": "goods",
        "raw_goods_json": "[]",
        "updated_date": "2026-07-18",
        "last_enriched_at": "2026-07-18T12:00:00Z",
    }
    later_refresh = {
        **revision_source,
        "normalized_row_json": json.dumps(
            {
                "packed_quantity": 100,
                "accepted_quantity": 90,
                "synced_at": "2026-07-18T13:00:00Z",
                "last_list_synced_at": "2026-07-18T13:00:00Z",
                "last_enriched_at": "2026-07-18T13:00:00Z",
            },
            sort_keys=True,
        ),
        "last_enriched_at": "2026-07-18T13:00:00Z",
    }
    _assert(
        _supply_revision(revision_source) == _supply_revision(later_refresh),
        "volatile enrichment timestamp does not cause false cutover drift",
    )
    _assert(
        _supply_revision(revision_source)
        != _supply_revision({**revision_source, "status_id": 5}),
        "business supply state change invalidates the reviewed plan",
    )
    accepted_correction = {
        **revision_source,
        "normalized_row_json": json.dumps(
            {
                "packed_quantity": 100,
                "accepted_quantity": 91,
                "synced_at": "2026-07-18T13:00:00Z",
                "last_list_synced_at": "2026-07-18T13:00:00Z",
                "last_enriched_at": "2026-07-18T13:00:00Z",
            },
            sort_keys=True,
        ),
    }
    _assert(
        _supply_revision(revision_source) != _supply_revision(accepted_correction),
        "accepted quantity correction invalidates the reviewed plan",
    )


def _test_semantic_digest_ignores_volatile_capture_identity() -> None:
    plan = {
        "opening_cost_map": [],
        "historical_wb_cost_projection": [
            {
                "as_of_date": "2026-07-18",
                "nm_id": 1,
                "quantity": "12",
                "wac_rub": "100",
                "capital_rub": "1200",
                "quality": "periodic_snapshot_wac",
                "fingerprint": "sha256:first",
                "provenance": {"snapshot_id": "snapshot_first"},
            }
        ],
        "lines": [
            {
                "warehouse_key": "wb",
                "nm_id": 1,
                "quantity": "12",
                "wac_rub": "100",
                "capital_rub": "1200",
                "cost_covered_quantity": "12",
                "coverage_share": "1",
                "quality": "periodic_snapshot_wac",
                "certified": False,
                "wb_quantity": "10",
                "wb_in_way_to_client": "1",
                "wb_in_way_from_client": "1",
                "provenance": {"source_records": [{"snapshot_id": "snapshot_first"}]},
            }
        ],
        "summaries": {"wb": {"quantity": "12", "capital_rub": "1200"}},
        "unmatched_doprinato": [],
        "new_events": [],
        "movement_documents": [],
        "invariants": {"negative_balance_count": 0},
    }
    refetched = copy.deepcopy(plan)
    refetched["historical_wb_cost_projection"][0]["fingerprint"] = "sha256:second"
    refetched["historical_wb_cost_projection"][0]["provenance"]["snapshot_id"] = "snapshot_second"
    refetched["lines"][0]["provenance"]["source_records"][0]["snapshot_id"] = "snapshot_second"
    _assert(
        _calculation_digest(plan) == _calculation_digest(refetched),
        "fresh capture identity does not cause false semantic calculation drift",
    )
    changed = copy.deepcopy(refetched)
    changed["lines"][0]["wb_in_way_to_client"] = "2"
    _assert(
        _calculation_digest(plan) != _calculation_digest(changed),
        "business quantity change invalidates semantic calculation digest",
    )


def _test_source_capture_exposes_calculation_timestamp() -> None:
    source_rows = {
        "wb_supplies": [],
        "shipments": [],
        "cny_operations": [],
        "financial_documents": [],
        "nomenclature_purchase_prices": [],
        "fulfillment_service_uploads": [],
        "ff_operations": [],
        "ff_auto_writeoff_checkpoint": [],
    }
    with tempfile.TemporaryDirectory(prefix="warehouse-capture-") as temp_dir:
        block = WarehouseFunctionalBlock(
            runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(temp_dir)),
            timestamp_factory=lambda: NOW,
        )
        wb_payload = {
            "snapshot_date": NOW[:10],
            "requested_nm_ids": [],
            "canonical_items": [],
            "data": {
                "raw_rows": [],
                "fetched_at": NOW,
                "pagination_complete": True,
                "page_count": 1,
                "page_offsets": [0],
                "raw_rows_digest": "sha256:empty",
            },
        }
        with patch(
            "packages.application.warehouse_functional._source_rows",
            return_value=source_rows,
        ):
            capture = block._capture_sources(captured_at=NOW, wb_payload=wb_payload)  # noqa: SLF001
        _assert(capture["captured_at"] == NOW, "source capture exposes calculation timestamp")


def _test_supply_refresh_completeness_gate() -> None:
    complete = {
        "status": "ok",
        "active_reconciliation_complete": True,
        "partial_status_slices": False,
        "failed_enrich": 0,
    }
    validate_functional_supply_sync(complete)
    for patch in (
        {"active_reconciliation_complete": False},
        {"partial_status_slices": True},
    ):
        try:
            validate_functional_supply_sync({**complete, **patch})
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"partial official supply refresh must fail closed: {patch}")
    try:
        validate_functional_supply_sync(
            {
                **complete,
                "failed_enrich": 1,
                "enrichment_failures": [
                    {
                        "lookup_id": "40422317",
                        "warnings": ["goods fetch failed for 40422317: status 429"],
                    }
                ],
            }
        )
    except RuntimeError as exc:
        _assert(
            "40422317[goods fetch failed for 40422317: status 429]" in str(exc),
            "functional supply gate exposes bounded supply-specific enrichment evidence",
        )
    else:
        raise AssertionError("failed official enrichment must fail closed with diagnostics")


def _test_guarded_publication() -> None:
    with tempfile.TemporaryDirectory(prefix="warehouse-functional-") as temp_dir:
        root = Path(temp_dir)
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=root / "runtime")
        block = WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        block._local_source_digest = lambda **_: "sha256:local"  # type: ignore[method-assign]
        block._wb_supply_source_digest = lambda **_: "sha256:supply"  # type: ignore[method-assign]
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute("CREATE TABLE immutable_warehouse_opening_v1(id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO immutable_warehouse_opening_v1 VALUES('audit')")
            conn.commit()
        lines = [
            WarehouseLine(
                warehouse_key=stage,
                nm_id=100 + index,
                quantity=Decimal("1"),
                capital=Decimal(str(10 + index)),
                cost_covered_quantity=Decimal("1"),
                quality="direct_24_06",
                provenance={"test": True},
                certified=True,
                wb_quantity=Decimal("1") if stage == STAGE_WB else Decimal("0"),
            )
            for index, stage in enumerate(STAGES)
            if stage != STAGE_DISCREPANCY
        ]
        summaries = _summaries(lines)
        plan = {
            "contract_name": "sheet_vitrina_v1_warehouse_functional",
            "contract_version": "v2",
            "status": "dry_run_ready",
            "kind": "functional_cutover",
            "cutover_id": FUNCTIONAL_CUTOVER_ID,
            "captured_at": DRY_RUN_AT,
            "effective_date": DRY_RUN_AT[:10],
            "base_active_version_id": "",
            "local_source_digest": "sha256:local",
            "wb_supply_source_digest": "sha256:supply",
            "source_watermarks": {"test": True},
            "absorbed_supply_revisions": {},
            "wb_snapshot": {
                "snapshot_id": "wbsnap_test",
                "fetched_at": NOW,
                "snapshot_date": NOW[:10],
                "requested_nm_ids": [104],
                "pagination_complete": True,
                "page_count": 1,
                "page_offsets": [0],
                "raw_row_count": 1,
                "raw_rows_digest": "sha256:rows",
                "raw_rows": [{"nmId": 104}],
                "items": [
                    {
                        "nm_id": 104,
                        "quantity": "1",
                        "in_way_to_client": "0",
                        "in_way_from_client": "0",
                        "wb_contour_quantity": "1",
                    }
                ],
            },
            "opening_cost_map": [
                {
                    "nm_id": item.nm_id,
                    "ff_unit_cost_rub": "10",
                    "wb_unit_cost_rub": "12",
                    "quality": "direct_24_06",
                    "provenance": {"test": True},
                    "fingerprint": f"sha256:{item.nm_id}",
                }
                for item in lines
            ],
            "lines": [_line_payload(item) for item in lines],
            "summaries": summaries,
            "unmatched_doprinato": [],
            "new_events": [],
            "movement_documents": [],
            "historical_wb_cost_projection": [
                {
                    "as_of_date": NOW[:10],
                    "nm_id": 104,
                    "quantity": "1",
                    "wac_rub": "14",
                    "capital_rub": "14",
                    "quality": "periodic_snapshot_wac_provisional",
                    "provenance": {"test": True},
                    "fingerprint": "sha256:cutover-daily",
                }
            ],
            "diff": {"changed_line_count": len(lines), "lines": []},
            "invariants": {
                "warehouse_count": 6,
                "negative_balance_count": 0,
                "positive_cost_gap_count": 0,
                "wb_quantity_source": "official_snapshot_only",
                "discrepancy_opening_zero": True,
            },
        }
        plan["plan_fingerprint"] = _fingerprint(plan)
        applied = block.apply_plan(
            plan,
            confirm_fingerprint=plan["plan_fingerprint"],
            backup_dir=root / "backups",
        )
        _assert(applied["status"] == "ready", "functional publication ready")
        _assert(applied["reconciliation"]["warehouse_count"] == 6, "six warehouses")
        _assert(applied["reconciliation"]["positive_cost_gap_count"] == 0, "positive cost coverage")
        _assert(applied["cutover"]["cutover_at"] == NOW, "cutover timestamp is atomic apply time")
        _assert(applied["active_version"]["effective_at"] == NOW, "opening version starts at apply time")
        repeated = block.apply_plan(
            plan,
            confirm_fingerprint=plan["plan_fingerprint"],
            backup_dir=root / "backups",
        )
        _assert(repeated["idempotent"] is True, "exact repeated apply is idempotent")
        acceptance_event = {
            "event_id": "whfe_daily_replay",
            "event_type": "wb_final_acceptance",
            "source_id": "supply-104:104",
            "source_fingerprint": "sha256:supply-104",
            "business_date": "2026-07-19",
            "nm_id": 104,
            "quantity": "1",
            "capital_rub": "20",
            "provenance": {"test": True},
        }
        candidate_lines = [
            (
                WarehouseLine(
                    warehouse_key=item.warehouse_key,
                    nm_id=item.nm_id,
                    quantity=Decimal("2"),
                    capital=Decimal("34"),
                    cost_covered_quantity=Decimal("2"),
                    quality="periodic_snapshot_wac_provisional",
                    provenance={"test": True},
                    certified=True,
                    wb_quantity=Decimal("2"),
                )
                if item.warehouse_key == STAGE_WB
                else item
            )
            for item in lines
        ]
        next_snapshot = {
            **plan["wb_snapshot"],
            "snapshot_id": "wbsnap_next",
            "fetched_at": "2026-07-19T12:00:00Z",
            "snapshot_date": "2026-07-19",
            "raw_rows_digest": "sha256:rows-next",
            "items": [
                {
                    "nm_id": 104,
                    "quantity": "2",
                    "in_way_to_client": "0",
                    "in_way_from_client": "0",
                    "wb_contour_quantity": "2",
                }
            ],
        }
        daily_replay = block._build_post_cutover_daily_cost_projection(  # noqa: SLF001
            captured_at="2026-07-19T12:00:00Z",
            candidate_lines=candidate_lines,
            candidate_snapshot=next_snapshot,
            new_events=[acceptance_event],
            opening_cost_map=plan["opening_cost_map"],
            cutover_mode=False,
        )
        replay_by_date = {
            (item["as_of_date"], int(item["nm_id"])): item for item in daily_replay
        }
        _assert(
            replay_by_date[("2026-07-18", 104)]["quality"]
            == "periodic_snapshot_wac_closed",
            "prior functional day closes as a versioned daily WAC",
        )
        _assert(
            replay_by_date[("2026-07-19", 104)]["wac_rub"] == "17",
            "accepted capital replays daily WAC from effective date",
        )
        sync_plan = copy.deepcopy(plan)
        sync_plan.update(
            {
                "kind": "hourly_wb_sync",
                "captured_at": "2026-07-19T12:00:00Z",
                "effective_date": "2026-07-19",
                "base_active_version_id": applied["active_version"]["version_id"],
                "wb_snapshot": next_snapshot,
                "opening_cost_map": [],
                "historical_wb_cost_projection": daily_replay,
                "lines": [_line_payload(item) for item in candidate_lines],
                "summaries": _summaries(candidate_lines),
                "new_events": [acceptance_event],
            }
        )
        sync_plan.pop("plan_fingerprint", None)
        sync_plan["plan_fingerprint"] = _fingerprint(sync_plan)
        sync_applied = block.apply_plan(
            sync_plan,
            confirm_fingerprint=sync_plan["plan_fingerprint"],
        )
        _assert(sync_applied["idempotent"] is False, "hourly daily WAC version publishes")
        daily_state = runtime.load_our_wb_cost_daily_state(as_of_date="2026-07-19")
        _assert(
            Decimal(str(daily_state[104]["our_wb_unit_cost_rub"])) == Decimal("17"),
            "canonical WB cost consumer reads replayed daily projection",
        )
        sync_repeated = block.apply_plan(
            sync_plan,
            confirm_fingerprint=sync_plan["plan_fingerprint"],
        )
        _assert(sync_repeated["idempotent"] is True, "exact hourly apply remains idempotent")
        stale_plan = copy.deepcopy(sync_plan)
        stale_plan["captured_at"] = "2026-07-20T12:00:00Z"
        stale_plan["effective_date"] = "2026-07-20"
        stale_plan.pop("plan_fingerprint", None)
        stale_plan["plan_fingerprint"] = _fingerprint(stale_plan)
        try:
            block.apply_plan(stale_plan, confirm_fingerprint=stale_plan["plan_fingerprint"])
        except Exception as exc:
            _assert("active functional warehouse version drifted" in str(exc), "stale active guard")
        else:
            raise AssertionError("stale concurrent functional plan must not publish")
        entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir, runtime=runtime)
        overview = entrypoint.handle_warehouses_overview_request()
        _assert(overview["contract_name"] == "sheet_vitrina_v1_warehouse_functional", "functional HTTP overview")
        _assert(len(overview["warehouses"]) == 6, "functional HTTP exposes six warehouses")
        wb_detail = entrypoint.handle_warehouse_detail_request("wb")
        _assert((wb_detail.get("warehouse") or {}).get("wb_contour") is not None, "WB HTTP detail exposes contour")
        _assert((wb_detail.get("documents") or [])[0].get("lines"), "warehouse documents persist their own lines")
        settings = entrypoint.handle_calculation_parameters_request()
        _assert(settings["status"] == "ready", "calculation parameters HTTP readback")
        _test_functional_economics_backfill(runtime=runtime, root=root)
        active_before_failure = block.readback()["active_version"]["version_id"]
        block.record_failed_sync(RuntimeError("injected 429 exhaustion"))
        failed = block.readback()
        _assert(failed["active_version"]["version_id"] == active_before_failure, "last good survives failure")
        _assert("429" in failed["sync"]["last_error"], "last failure is visible")
        rolled_back = block.rollback_functional_cutover(
            confirm_fingerprint=plan["plan_fingerprint"],
            backup_dir=root / "rollback-backups",
        )
        _assert(rolled_back["status"] == "rolled_back", "bounded rollback")
        _assert(block.readback()["status"] == "not_initialized", "derived state removed")
        with sqlite3.connect(runtime.db_path) as conn:
            _assert(conn.execute("SELECT COUNT(*) FROM immutable_warehouse_opening_v1").fetchone()[0] == 1, "old opening audit preserved")
        backup_path = Path(str(applied["backup"]["path"]))
        _assert(backup_path.stat().st_mode & 0o777 == 0o600, "backup mode 0600")


def _test_functional_economics_backfill(*, runtime: RegistryUploadDbBackedRuntime, root: Path) -> None:
    plan = {
        "date_columns": ["2026-07-01"],
        "sheets": [
            {
                "sheet_name": "DATA_VITRINA",
                "write_start_cell": "A1",
                "header": ["Показатель", "row_id", "2026-07-01"],
                "rows": [
                    ["SKU", "SKU:104|orderSum", 100],
                    ["SKU", "SKU:104|orderCount", 2],
                    ["SKU", "SKU:104|ads_sum", 10],
                    ["Legacy", "SKU:104|non_target", 777],
                    ["Legacy presentation A", 93.54754799999999, ""],
                    ["Legacy presentation B", 93.54754799999999, ""],
                ],
            }
        ],
        "metadata": {"preserved": True},
    }
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost(
                   cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,quality,
                   provenance_json,fingerprint,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (FUNCTIONAL_CUTOVER_ID, "2026-07-01", 104, "10", "14", "140", "direct_24_06", "{}", "sha256:daily", NOW),
        )
        conn.execute(
            "INSERT INTO registry_upload_versions(bundle_version,uploaded_at,activated_at) VALUES(?,?,?)",
            ("economics-smoke", NOW, NOW),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_ready_snapshots(
                   bundle_version,activated_at,as_of_date,snapshot_id,plan_version,refreshed_at,plan_json
               ) VALUES(?,?,?,?,?,?,?)""",
            ("economics-smoke", NOW, "2026-07-01", "snap-economics", "v1", NOW, json.dumps(plan)),
        )
        conn.commit()
    dry_run = build_functional_economics_backfill_plan(runtime)
    _assert(dry_run["changed_snapshot_count"] == 1, "functional economics backfill finds target snapshot")
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            """UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=?
               WHERE bundle_version='economics-smoke' AND as_of_date='2026-07-01'""",
            ("2026-07-18T12:00:01Z",),
        )
        conn.commit()
    try:
        apply_functional_economics_backfill_plan(
            runtime,
            dry_run,
            confirm_fingerprint=dry_run["plan_fingerprint"],
            backup_dir=root / "economics-backups",
        )
    except Exception as exc:
        _assert("drifted" in str(exc), "ready snapshot manifest drift blocks exact backfill")
    else:
        raise AssertionError("ready snapshot manifest drift must block exact backfill")
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            """UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=?
               WHERE bundle_version='economics-smoke' AND as_of_date='2026-07-01'""",
            (NOW,),
        )
        conn.commit()
    dry_run = build_functional_economics_backfill_plan(runtime)
    applied = apply_functional_economics_backfill_plan(
        runtime,
        dry_run,
        confirm_fingerprint=dry_run["plan_fingerprint"],
        backup_dir=root / "economics-backups",
    )
    _assert(applied["database_written"] is True, "functional economics backfill applies atomically")
    repeated = build_functional_economics_backfill_plan(runtime)
    _assert(repeated["changed_snapshot_count"] == 0, "functional economics backfill is idempotent")
    with sqlite3.connect(runtime.db_path) as conn:
        stored = json.loads(conn.execute(
            "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version='economics-smoke'"
        ).fetchone()[0])
    rows = {row[1]: row for row in stored["sheets"][0]["rows"]}
    profit = Decimal(str(rows["SKU:104|proxy_profit_3_rub"][2]))
    margin = Decimal(str(rows["SKU:104|proxy_margin_3_pct"][2]))
    _assert(profit == Decimal("15.48"), "Proxy 3 default settings formula")
    _assert(abs(margin - profit / Decimal("91")) < Decimal("0.0000005"), "Proxy 3 margin uses expected buyout revenue")
    _assert(rows["SKU:104|non_target"][2] == 777, "functional economics backfill preserves non-target cells")
    _assert(
        [row for row in stored["sheets"][0]["rows"] if row[0].startswith("Legacy presentation")]
        == [
            ["Legacy presentation A", 93.54754799999999, ""],
            ["Legacy presentation B", 93.54754799999999, ""],
        ],
        "functional economics ignores and preserves legacy non-key presentation rows",
    )
    parameters = CalculationParametersBlock(runtime=runtime)
    changed_payload = {
        "effective_date": "2026-07-01",
        "buyout_rate": "0.9",
        "tax_rate": "0.1",
        "wb_agent_and_other_rate": "0.3",
        "acquiring_rate": "0",
        "wb_logistics_rate": "0",
        "wb_storage_rate": "0",
        "penalties_adjustments_rate": "0",
        "other_expense_rate": "0",
    }
    preview = parameters.preview_version(changed_payload)
    saved = parameters.create_version(
        changed_payload,
        preview_fingerprint=preview["preview_fingerprint"],
        created_by="smoke",
    )
    _assert(saved["targeted_recalculation"]["status"] == "complete", "settings save executes targeted Proxy recalculation")
    with sqlite3.connect(runtime.db_path) as conn:
        updated = json.loads(conn.execute(
            "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version='economics-smoke'"
        ).fetchone()[0])
    updated_rows = {row[1]: row for row in updated["sheets"][0]["rows"]}
    _assert(
        Decimal(str(updated_rows["SKU:104|proxy_profit_3_rub"][2])) == Decimal("18.8"),
        "settings effective date republishes Proxy 3 without physical warehouse rebuild",
    )


def _downstream_row(
    supply_id: str,
    nm_id: int,
    *,
    quantity: str,
    transit: str,
    acceptance: str = "0",
    accepted_date: str = "2026-07-02",
) -> dict[str, object]:
    return {
        "accepted_date": accepted_date,
        "wb_supply_id": supply_id,
        "nm_id": nm_id,
        "quantity": quantity,
        "transit_cost_status": "transit_confirmed",
        "transit_per_unit_rub": transit,
        "ff_services_per_unit_rub": "0",
        "ff_storage_per_unit_rub": "0",
        "wb_acceptance_per_accepted_unit_rub": acceptance,
        "inputs_hash": f"{supply_id}:{nm_id}:{transit}:{acceptance}",
    }


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    main()
