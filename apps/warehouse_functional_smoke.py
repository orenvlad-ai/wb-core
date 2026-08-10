#!/usr/bin/env python3
"""Focused contract smoke for functional warehouses, WAC and Proxy 3."""

from __future__ import annotations

import argparse
import ast
import copy
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import inspect
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
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
from packages.application.own_product_capital import OwnProductCapitalBlock  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.supplier_financial_documents import build_financial_summary  # noqa: E402
from packages.application.wb_supplies import validate_functional_supply_sync  # noqa: E402
from packages.application.warehouse_functional import (  # noqa: E402
    FUNCTIONAL_CUTOVER_ID,
    STAGES,
    STAGE_DISCREPANCY,
    STAGE_WB,
    WAREHOUSE_QUALITY_PRESENTATIONS,
    WarehouseFunctionalBlock,
    WarehouseFunctionalError,
    WarehouseLine,
    _build_versioned_historical_correction,
    _calculation_digest,
    _counted_cny_operation,
    _current_snapshot_effective_date,
    _daily_wb_cost_row,
    _ff_operation_replay_sort_key,
    _fingerprint,
    _functional_local_source_view,
    _guarded_local_sources,
    _historical_recovery_source_rows,
    _historical_snapshot_manifest_digest,
    _line_payload,
    _merge_historical_wb_quantity_evidence,
    _missing_pre_cutover_historical_dates,
    _nomenclature_purchase_prices,
    _ready_snapshot_recovery_rows,
    _ready_snapshot_historical_correction_rows,
    _revalidate_balance_certifications,
    load_supplier_flow_cost_state,
    _supply_downstream_component_index,
    _supply_revision,
    _supplier_allocation_with_certification,
    _supplier_cost_allocations,
    _summaries,
    _validate_historical_projection_calendar,
    _validate_historical_correction_plan,
    _validate_historical_correction_matches_derived,
    _validated_financial_expense,
    _warehouse_balance_status_presentation,
    _warehouse_human_evidence,
    _wb_snapshot_integrity,
    accepted_capital_delta,
    accepted_quantity_delta,
    allocate_capital,
    build_frozen_opening_cost_map,
    build_historical_wb_cost_projection,
    compose_supply_costs,
    moving_weighted_average,
    proportional_ff_outbound,
    reconcile_discrepancies,
    roll_periodic_wac,
    validate_cutover_ff_debit_coverage,
)
from packages.application.warehouse_functional_economics_backfill import (  # noqa: E402
    WAREHOUSE_TARGET_KEYS,
    _exact_functional_snapshot_dates,
    _transform_snapshot,
    _warehouse_input_manifest_digest,
    apply_functional_economics_backfill_plan,
    build_functional_economics_backfill_plan,
    rollback_target_scoped_functional_economics,
)
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    WarehouseRecoveryRegistry,
)
from packages.business_time import current_business_date_iso  # noqa: E402
from packages.application.wb_finance_weekly import (  # noqa: E402
    CALCULATION_REFERENCE_ROWS,
    CLASSIFIER_VERSION as WB_FINANCE_CLASSIFIER_VERSION,
    _functional_wb_cost_state,
)
from apps.warehouse_functional_runner import (  # noqa: E402
    _run,
    _recalculate_downstream_finance_cost,
    _verify_cutover_external_recheck,
)


NOW = "2026-07-18T12:00:00Z"
DRY_RUN_AT = "2026-07-18T11:55:00Z"


def main() -> None:
    _test_decimal_and_allocations()
    _test_invalid_supplier_line_fails_closed()
    _test_blocked_cny_operation_cannot_activate_supplier_flow()
    _test_zero_rub_supplier_payment_fails_closed()
    _test_26gn390_supplier_line_cost_proof()
    _test_official_wb_snapshot_integrity()
    _test_current_snapshot_business_date_gate()
    _test_accepted_source_correction()
    _test_paid_acceptance_cost_boundary()
    _test_financial_document_eligibility()
    _test_discrepancy_pool()
    _test_unavailable_cost_is_not_zero_wac()
    _test_cutover_ff_debit_coverage()
    _test_equal_timestamp_ff_receipt_ordering()
    _test_frozen_cost_map()
    _test_nomenclature_purchase_price_source()
    _test_historical_wb_projection()
    _test_historical_projection_calendar_gate()
    _test_versioned_historical_correction()
    _test_zero_quantity_without_cost_basis_consumer()
    _test_exact_historical_wb_quantity_evidence()
    _test_quality_localization_catalog()
    _test_source_mutation_removes_green_balance_status()
    _test_human_evidence_uses_source_quality_and_date()
    _test_proxy()
    _test_versioned_parameters_and_reference()
    _test_initial_settings_preserve_outer_transaction()
    _test_external_optimistic_recheck()
    _test_hourly_and_manual_cost_materialization_journal_details()
    _test_downstream_cost_refresh_recalculates_finance_before_unlock()
    _test_finance_recalculation_is_the_last_cost_writer()
    _test_semantic_digest_ignores_volatile_capture_identity()
    _test_source_capture_exposes_calculation_timestamp()
    _test_supply_refresh_completeness_gate()
    _test_incident_option_handler_is_local_read_only()
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
    remaining_qty, remaining_capital, first_outbound_wac = proportional_ff_outbound(
        quantity="500",
        capital="59011.79010127528074135875863",
        outbound_quantity="250",
    )
    remaining_qty, remaining_capital, final_outbound_wac = proportional_ff_outbound(
        quantity=remaining_qty,
        capital=remaining_capital,
        outbound_quantity="250",
    )
    _assert(
        remaining_qty == Decimal("0")
        and remaining_capital == Decimal("0")
        and first_outbound_wac == Decimal("118.0235802025505614827175173")
        and final_outbound_wac == Decimal("118.0235802025505614827175172"),
        "full FF depletion transfers all capital without a Decimal residue",
    )


def _test_downstream_cost_refresh_recalculates_finance_before_unlock() -> None:
    class FakeFinance:
        def __init__(self) -> None:
            self.date_from = None

        def recalculate_stale_cost_weeks(self, *, date_from):
            self.date_from = date_from
            return {
                "status": "applied",
                "recalculated_week_count": 2,
                "post_verify_stale_week_count": 0,
                "non_target_preserved": True,
            }

    class FakeRuntime:
        runtime_dir = Path("/tmp/fake-warehouse-runtime")

    finance = FakeFinance()
    with patch(
        "apps.warehouse_functional_runner.block_from_env",
        return_value=finance,
    ) as factory:
        result = _recalculate_downstream_finance_cost(FakeRuntime())  # type: ignore[arg-type]
    _assert(
        factory.call_args.args == (FakeRuntime.runtime_dir,),
        "Finance recalculation must use the selected runtime registry",
    )
    _assert(
        finance.date_from == date(2026, 7, 1),
        "Finance recalculation must include the 2026-06-29 boundary week",
    )
    _assert(
        result["post_verify_stale_week_count"] == 0
        and result["non_target_preserved"] is True,
        "last cost writer must expose exact Finance post-verify/non-target evidence",
    )


def _test_hourly_and_manual_cost_materialization_journal_details() -> None:
    class FakeCalculationParameters:
        def prepare_functional_economics_backup(self):
            return {"status": "ready"}

        def process_pending_targeted_recalculations(self, *, verified_backup):
            return {"status": "success", "request_count": 0}

        def publish_current_functional_economics(self, *, verified_backup):
            return {
                "plan_fingerprint": "sha256:fixture-economics",
                "changed_snapshot_count": 0,
                "database_written": False,
                "backup_archive": {},
            }

    class FakeBlock:
        def __init__(self) -> None:
            self.calculation_parameters = FakeCalculationParameters()

        def timestamp_factory(self):
            return "2026-08-03T08:00:00Z"

        def build_sync_plan(self):
            return {
                "plan_fingerprint": "sha256:fixture-plan",
                "diff": {"changed_line_count": 0},
            }

        def apply_plan(self, plan, *, confirm_fingerprint):
            return {
                "active_version": {
                    "version_id": "whfv_fixture",
                    "business_effective_date": "2026-08-03",
                },
                "recovery_policy": {"status": "ready"},
                "sync": {"status": "success"},
                "reconciliation": {"status": "success"},
            }

        def record_failed_sync(self, failure):
            raise AssertionError(f"successful fixture recorded failure: {failure}")

    class FakeRuntime:
        def __init__(self, runtime_dir: Path) -> None:
            self.runtime_dir = runtime_dir
            self.db_path = runtime_dir / "registry.sqlite3"

        def finalize_completed_wb_transit_cost_recalculations(self, *, completed_at):
            return {"status": "success", "completed_at": completed_at}

    for command, trigger_source in (
        ("hourly-sync", "hourly"),
        ("manual-sync", "manual"),
    ):
        for changed_rows in (53, 0):
            with tempfile.TemporaryDirectory(prefix="warehouse-materialization-journal-") as tmp:
                runtime_dir = Path(tmp) / "runtime"
                runtime_dir.mkdir()
                runtime = FakeRuntime(runtime_dir)
                with (
                    patch(
                        "apps.warehouse_functional_runner.RegistryUploadDbBackedRuntime",
                        return_value=runtime,
                    ),
                    patch(
                        "apps.warehouse_functional_runner.WarehouseFunctionalBlock",
                        return_value=FakeBlock(),
                    ),
                    patch(
                        "apps.warehouse_functional_runner._fresh_stocks_block",
                        return_value=object(),
                    ),
                    patch(
                        "apps.warehouse_functional_runner.warehouse_functional_write_lock",
                        return_value=nullcontext({"wait_ms": 0}),
                    ),
                    patch(
                        "apps.warehouse_functional_runner._run_bounded_recovery_retention",
                        return_value={"status": "success"},
                    ),
                    patch(
                        "apps.warehouse_functional_runner._refresh_official_supply_state",
                        return_value={"status": "success"},
                    ),
                    patch(
                        "apps.warehouse_functional_runner._collect_autonomous_transit_costs",
                        return_value={"status": "success"},
                    ),
                    patch(
                        "apps.warehouse_functional_runner._materialize_downstream_cost_layers",
                        return_value=changed_rows,
                    ),
                    patch(
                        "apps.warehouse_functional_runner.WbSuppliesBlock"
                    ) as supplies_block,
                    patch(
                        "apps.warehouse_functional_runner._recalculate_downstream_finance_cost",
                        return_value={"status": "success"},
                    ),
                ):
                    supplies_block.return_value.reconcile_functional_ff_state.return_value = {
                        "status": "success"
                    }
                    result = _run(
                        argparse.Namespace(
                            runtime_dir=str(runtime_dir),
                            command=command,
                            backup_dir=str(Path(tmp) / "backups"),
                        ),
                        sqlite_busy_timeout_ms=120_000,
                    )
                with sqlite3.connect(runtime.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    phase = conn.execute(
                        "SELECT status,item_count,details_json "
                        "FROM sheet_vitrina_v1_warehouse_update_phases "
                        "WHERE phase_key='cost_materialization'"
                    ).fetchone()
                    durable_run = conn.execute(
                        "SELECT trigger_source,status,result_json "
                        "FROM sheet_vitrina_v1_warehouse_update_runs"
                    ).fetchone()
            _assert(
                result["status"] == "success"
                and result["downstream_cost_layers_materialized"] == changed_rows,
                f"{command} preserves the scalar materialization result for {changed_rows}",
            )
            _assert(
                phase["status"] == "success"
                and phase["item_count"] == changed_rows
                and json.loads(phase["details_json"]) == {"changed_rows": changed_rows},
                f"{command} journals structured materialization evidence for {changed_rows}",
            )
            _assert(
                durable_run["trigger_source"] == trigger_source
                and durable_run["status"] == "success"
                and json.loads(durable_run["result_json"])[
                    "downstream_cost_layers_materialized"
                ]
                == changed_rows,
                f"{command} durably completes for materialization count {changed_rows}",
            )


def _test_finance_recalculation_is_the_last_cost_writer() -> None:
    tree = ast.parse(inspect.getsource(_run))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]
    recalculate_lines = sorted(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Name)
        and node.func.id == "_recalculate_downstream_finance_cost"
    )
    economics_lines = sorted(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "publish_current_functional_economics"
    )
    retention_lines = sorted(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Name)
        and node.func.id == "_run_bounded_recovery_retention"
    )
    _assert(
        len(recalculate_lines) == 2,
        "reviewed and hourly/manual sync must each recalculate Finance once",
    )
    for line in recalculate_lines:
        _assert(
            any(economics < line for economics in economics_lines),
            "Finance recalculation must follow functional-economics publication",
        )
        _assert(
            any(retention > line for retention in retention_lines),
            "Finance recalculation must finish before final retention and unlock",
        )
    manual_source = inspect.getsource(
        RegistryUploadHttpEntrypoint.handle_warehouse_manual_sync_request
    )
    _assert(
        manual_source.index("publish_current_functional_economics")
        < manual_source.index("recalculate_stale_cost_weeks")
        < manual_source.index("finalize_completed_wb_transit_cost_recalculations"),
        "operator manual sync publishes and verifies Finance before completing its dependent phase",
    )


def _test_incident_option_handler_is_local_read_only() -> None:
    source = inspect.getsource(
        RegistryUploadHttpEntrypoint.handle_wb_warehouse_exclusion_options_request
    )
    _assert(
        "warehouse_functional_block.wb_warehouse_exclusion_options" in source
        and "factory_order_supply_block.build_wb_warehouse_exclusion_options" not in source,
        "opening the WB warehouse incident panel reads the active local version without an external producer",
    )


def _test_source_mutation_removes_green_balance_status() -> None:
    with tempfile.TemporaryDirectory(prefix="warehouse-certification-recheck-") as temp_dir:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(temp_dir) / "runtime")
        balance = {
            "version_id": "whfv_active",
            "warehouse_key": "china_to_ff",
            "nm_id": 104,
            "quantity": "10",
            "capital_rub": "100",
            "wac_rub": "10",
            "cost_covered_quantity": "10",
            "quality": "certified",
            "certified": True,
            "provenance": {
                "source_records": [
                    {"shipment_id": "shipment-source-changed", "flow_quantity": "10"}
                ]
            },
        }
        stale_proof = {
            "certification": {
                "certified": False,
                "source_fingerprint_matches": False,
                "active_version_id": "whfv_active",
            }
        }
        with patch(
            "packages.application.warehouse_functional.load_supplier_line_cost_breakdown",
            return_value=stale_proof,
        ):
            [revalidated] = _revalidate_balance_certifications(
                runtime=runtime,
                balances=[balance],
                active_version_id="whfv_active",
            )
        _assert(
            revalidated["persisted_certified"] is True
            and revalidated["certified"] is False
            and revalidated["certification_revalidation_failed"] is True,
            "source mutation removes the stale persisted certification before replay",
        )
        status = _warehouse_balance_status_presentation(
            "source_changed_provisional",
            certified=bool(revalidated["certified"]),
        )
        _assert(
            status["tone"] == "warning"
            and status["label_ru"] == "Предварительная себестоимость — источники изменились",
            "source mutation is presented as an explicit yellow provisional status",
        )

        runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """CREATE TABLE sheet_vitrina_v1_warehouse_functional_active(
                       slot INTEGER PRIMARY KEY, version_id TEXT NOT NULL
                   )"""
            )
            conn.execute(
                """CREATE TABLE sheet_vitrina_v1_warehouse_functional_balances(
                       version_id TEXT NOT NULL, warehouse_key TEXT NOT NULL,
                       quantity TEXT NOT NULL, capital_rub TEXT NOT NULL,
                       certified INTEGER NOT NULL, quality TEXT NOT NULL,
                       provenance_json TEXT NOT NULL
                   )"""
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_warehouse_functional_active(slot,version_id) VALUES(1,?)",
                ("whfv_active",),
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                       version_id,warehouse_key,quantity,capital_rub,certified,quality,provenance_json
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    "whfv_active",
                    "china_to_ff",
                    "10",
                    "100",
                    1,
                    "certified",
                    json.dumps({"source_records": [{
                        "shipment_id": "shipment-source-changed",
                        "flow_quantity": "10",
                        "flow_capital_rub": "100",
                        "quality": "certified",
                        "expenses_complete_certification": True,
                    }]}),
                ),
            )
            conn.commit()
        with patch(
            "packages.application.warehouse_functional.load_supplier_line_cost_breakdown",
            return_value=stale_proof,
        ):
            supplier_registry_state = load_supplier_flow_cost_state(
                runtime=runtime,
                shipment_id="shipment-source-changed",
            )
        _assert(
            supplier_registry_state["china_to_ff"]["certified"] is False
            and "source_changed_provisional" in supplier_registry_state["china_to_ff"]["quality"],
            "supplier registry stage cell also removes stale green certification",
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_balances")
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                       version_id,warehouse_key,quantity,capital_rub,certified,quality,provenance_json
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    "whfv_active",
                    "china_to_ff",
                    "30",
                    "250",
                    0,
                    "mixed:certified,confirmed_payments_provisional_expenses",
                    json.dumps({"source_records": [
                        {
                            "shipment_id": "shipment-source-changed",
                            "flow_quantity": "10",
                            "flow_capital_rub": "100",
                            "quality": "certified",
                            "expenses_complete_certification": True,
                        },
                        {
                            "shipment_id": "sibling-provisional",
                            "flow_quantity": "20",
                            "flow_capital_rub": "150",
                            "quality": "confirmed_payments_provisional_expenses",
                            "expenses_complete_certification": False,
                        },
                    ]}),
                ),
            )
            conn.commit()
        with patch(
            "packages.application.warehouse_functional.load_supplier_line_cost_breakdown",
            return_value={
                "certification": {
                    "certified": True,
                    "source_fingerprint_matches": True,
                    "active_version_id": "whfv_active",
                }
            },
        ):
            selected_supplier_state = load_supplier_flow_cost_state(
                runtime=runtime,
                shipment_id="shipment-source-changed",
            )
        _assert(
            selected_supplier_state["china_to_ff"]["certified"] is True
            and selected_supplier_state["china_to_ff"]["quality"] == ["certified"],
            "a provisional sibling party does not downgrade the selected certified supplier shipment",
        )


def _test_invalid_supplier_line_fails_closed() -> None:
    allocation = _supplier_cost_allocations(
        {
            "shipments": [
                {
                    "shipment_id": "invalid-line-smoke",
                    "invoice_no": "INVALID-LINE",
                    "invoice_date": "2026-07-18",
                    "order_status": "active",
                    "expenses_complete": 1,
                }
            ],
            "shipment_lines": [
                {
                    "line_id": "valid-line",
                    "shipment_id": "invalid-line-smoke",
                    "line_type": "product",
                    "internal_nm_id": 101,
                    "qty": "10",
                    "unit_price": "5",
                    "amount": "50",
                },
                {
                    "line_id": "zero-quantity-line",
                    "shipment_id": "invalid-line-smoke",
                    "line_type": "product",
                    "internal_nm_id": 202,
                    "qty": "0",
                    "unit_price": "5",
                    "amount": "25",
                },
                {
                    "line_id": "explicit-zero-amount-line",
                    "shipment_id": "invalid-line-smoke",
                    "line_type": "product",
                    "internal_nm_id": 303,
                    "qty": "5",
                    "unit_price": "5",
                    "amount": "0",
                },
            ],
            "cny_operations": [
                {
                    "operation_id": "invalid-line-payment",
                    "operation_type": "supplier_payment_out",
                    "source_order_id": "invalid-line-smoke",
                    "operation_date": "2026-07-18",
                    "sequence_key": "1",
                    "cny_delta": "-75",
                    "rub_value_delta": "-750",
                    "status": "posted",
                }
            ],
            "cny_documents": [],
            "financial_documents": [],
            "financial_expense_lines": [],
        }
    )["invalid-line-smoke"]
    _assert(
        any(item["code"] == "invalid_invoice_product_line" for item in allocation["blockers"]),
        "one invalid matched product line blocks the entire supplier flow",
    )
    _assert(
        any(
            "explicit-zero-amount-line" in item["reason_ru"]
            and "стоимость строки invoice не положительная" in item["reason_ru"]
            for item in allocation["blockers"]
        ),
        "an explicit non-positive invoice amount is not reconstructed from quantity and unit price",
    )
    _assert(
        allocation["capital_rub"] is None
        and all(item["unit_cost_rub"] is None for item in allocation["lines"]),
        "a partial invoice cannot be certified or published with silent quantity omission",
    )


def _test_blocked_cny_operation_cannot_activate_supplier_flow() -> None:
    shipment_id = "blocked-cny-payment-smoke"
    allocation = _supplier_cost_allocations(
        {
            "shipments": [
                {
                    "shipment_id": shipment_id,
                    "invoice_no": "BLOCKED-CNY",
                    "invoice_date": "2026-07-18",
                    "order_status": "active",
                    "expenses_complete": 0,
                }
            ],
            "shipment_lines": [
                {
                    "line_id": "blocked-cny-line",
                    "shipment_id": shipment_id,
                    "line_type": "product",
                    "internal_nm_id": 101,
                    "qty": "10",
                    "unit_price": "5",
                    "amount": "50",
                }
            ],
            "cny_operations": [
                {
                    "operation_id": "blocked-payment",
                    "operation_type": "supplier_payment_out",
                    "source_order_id": shipment_id,
                    "operation_date": "2026-07-18",
                    "sequence_key": "1",
                    "cny_delta": "-50",
                    "rub_value_delta": "0",
                    "status": "blocked",
                    "document_status": "posted",
                    "error_reason": "insufficient_cny_balance",
                }
            ],
            "cny_documents": [],
            "financial_documents": [
                {
                    "document_id": "recognized-logistics",
                    "document_type": "logistics_invoice",
                    "document_number": "LOG-BLOCKED-CNY",
                    "document_date": "2026-07-18",
                    "parse_status": "confirmed",
                }
            ],
            "financial_expense_lines": [
                {
                    "line_id": "recognized-logistics-line",
                    "financial_document_id": "recognized-logistics",
                    "supplier_order_id": shipment_id,
                    "category": "logistics",
                    "amount_rub": "100",
                    "currency": "RUB",
                    "status": "confirmed",
                }
            ],
        }
    )[shipment_id]
    _assert(
        any(item["code"] == "confirmed_supplier_payment_unavailable" for item in allocation["blockers"]),
        "a blocked CNY payment remains an explicit missing-payment blocker",
    )
    _assert(
        allocation["capital_rub"] is None
        and not any(
            component.get("component_key") == "supplier_payment"
            for line in allocation["lines"]
            for component in line.get("components") or []
        )
        and all(item["unit_cost_rub"] is None for item in allocation["lines"]),
        "recognized downstream expenses cannot activate full invoice quantity without a posted supplier payment",
    )
    blocked_payment_control = next(
        item
        for item in allocation["document_controls"]
        if item["document_type"] == "supplier_cny_payment"
    )
    _assert(
        blocked_payment_control["eligible_component_count"] == 1
        and blocked_payment_control["allocated_component_count"] == 0
        and blocked_payment_control["conserved"] is False
        and blocked_payment_control["incomplete_reasons"],
        "a blocked CNY document remains a canonical none-status source with bounded reasons",
    )


def _test_zero_rub_supplier_payment_fails_closed() -> None:
    shipment_id = "zero-rub-payment-smoke"
    allocation = _supplier_cost_allocations(
        {
            "shipments": [
                {
                    "shipment_id": shipment_id,
                    "invoice_no": "ZERO-RUB",
                    "invoice_date": "2026-07-18",
                    "currency": "CNY",
                    "order_status": "active",
                    "expenses_complete": 1,
                }
            ],
            "shipment_lines": [
                {
                    "line_id": "zero-rub-line",
                    "shipment_id": shipment_id,
                    "line_type": "product",
                    "internal_nm_id": 101,
                    "qty": "10",
                    "unit_price": "5",
                    "amount": "50",
                }
            ],
            "cny_operations": [
                {
                    "operation_id": "zero-rub-payment",
                    "operation_type": "supplier_payment_out",
                    "source_order_id": shipment_id,
                    "operation_date": "2026-07-18",
                    "sequence_key": "1",
                    "cny_delta": "-50",
                    "rub_value_delta": "0",
                    "status": "posted",
                }
            ],
            "cny_documents": [],
            "financial_documents": [],
            "financial_expense_lines": [],
        }
    )[shipment_id]
    _assert(
        any(
            item["code"] == "supplier_payment_rub_valuation_unavailable"
            for item in allocation["blockers"]
        )
        and allocation["capital_rub"] is None
        and allocation["average_unit_cost_rub"] is None,
        "a posted CNY payment without positive RUB valuation cannot publish or certify zero cost",
    )


def _test_26gn390_supplier_line_cost_proof() -> None:
    shipment_id = "26GN390"
    financial_documents = []
    financial_expense_lines = []
    expense_specs = (
        ("log136", "logistics_invoice", "logistics", "1075030"),
        ("log121", "logistics_invoice", "logistics", "5000"),
        ("customs", "customs_declaration", "customs_fee_1010", "49240"),
        ("customs", "customs_declaration", "import_duty_2010", "622093.05"),
        ("customs", "customs_declaration", "import_vat_5010", "1505465.18"),
    )
    for document_id, document_type, category, amount in expense_specs:
        if not any(item["document_id"] == document_id for item in financial_documents):
            financial_documents.append(
                {
                    "document_id": document_id,
                    "supplier_order_id": shipment_id,
                    "document_type": document_type,
                    "document_number": document_id,
                    "document_date": "2026-07-03",
                    "parse_status": "confirmed",
                }
            )
        financial_expense_lines.append(
            {
                "line_id": f"{document_id}:{category}",
                "financial_document_id": document_id,
                "supplier_order_id": shipment_id,
                "category": category,
                "amount_rub": amount,
                "currency": "RUB",
                "status": "confirmed",
            }
        )
    financial_documents.extend(
        [
            {
                "document_id": "payment-financial",
                "supplier_order_id": shipment_id,
                "document_type": "bank_transfer_application",
                "document_number": "PAYMENT",
                "document_date": "2026-05-21",
                "parse_status": "confirmed",
            },
            {
                "document_id": "fee-financial",
                "supplier_order_id": shipment_id,
                "document_type": "bank_fee_statement",
                "document_number": "FEE",
                "document_date": "2026-05-21",
                "parse_status": "confirmed",
            },
            {
                "document_id": "packing-informational",
                "supplier_order_id": shipment_id,
                "document_type": "packing_list",
                "document_number": "PACKING",
                "document_date": "2026-05-14",
                "parse_status": "confirmed",
            },
        ]
    )
    financial_expense_lines.extend(
        [
            {
                "line_id": "fee-info-cny",
                "financial_document_id": "fee-financial",
                "supplier_order_id": shipment_id,
                "category": "bank_transfer_fee",
                "amount_rub": "120899.32",
                "currency": "CNY",
                "status": "confirmed",
            },
            {
                "line_id": "fee-info-zero",
                "financial_document_id": "fee-financial",
                "supplier_order_id": shipment_id,
                "category": "bank_transfer_fee",
                "amount_rub": "0",
                "currency": "RUB",
                "status": "confirmed",
            },
        ]
    )
    allocation_sources = {
            "shipments": [
                {
                    "shipment_id": shipment_id,
                    "invoice_no": shipment_id,
                    "invoice_date": "2026-05-14",
                    "currency": "CNY",
                    "actual_shipment_date": "2026-06-25",
                    "actual_ff_acceptance_date": "",
                    "order_status": "active",
                    "expenses_complete": 1,
                }
            ],
            "shipment_lines": [
                {"line_id": "anti16pro", "shipment_id": shipment_id, "line_type": "product", "internal_nm_id": 391660889, "qty": "4500", "unit_price": "7.5", "amount": "33750"},
                {"line_id": "anti16promax", "shipment_id": shipment_id, "line_type": "product", "internal_nm_id": 391661710, "qty": "5250", "unit_price": "7.5", "amount": "39375"},
                {"line_id": "other", "shipment_id": shipment_id, "line_type": "product", "internal_nm_id": 999999999, "qty": "70500", "unit_price": "1", "amount": "440750"},
            ],
            "cny_operations": [
                {"operation_id": "payment", "operation_type": "supplier_payment_out", "source_order_id": shipment_id, "source_document_id": "payment-doc", "operation_date": "2026-05-21", "sequence_key": "1", "cny_delta": "-541962.5", "rub_value_delta": "-5724403.57", "status": "posted"},
                {"operation_id": "fee", "operation_type": "transfer_fee", "source_order_id": shipment_id, "source_document_id": "fee-doc", "operation_date": "2026-05-21", "sequence_key": "2", "cny_delta": "-11446.4", "rub_value_delta": "-120899.32", "status": "posted"},
            ],
            "cny_documents": [
                {
                    "document_id": "payment-doc",
                    "source_order_id": shipment_id,
                    "linked_financial_document_id": "payment-financial",
                    "document_type": "supplier_cny_payment",
                    "status": "posted",
                },
                {
                    "document_id": "fee-doc",
                    "source_order_id": shipment_id,
                    "linked_financial_document_id": "fee-financial",
                    "document_type": "bank_fee",
                    "status": "posted",
                },
            ],
            "financial_documents": financial_documents,
            "financial_expense_lines": financial_expense_lines,
        }
    allocation = _supplier_cost_allocations(allocation_sources)[shipment_id]
    permuted_allocation = _supplier_cost_allocations(
        {
            **allocation_sources,
            "financial_expense_lines": list(reversed(financial_expense_lines)),
        }
    )[shipment_id]
    _assert(
        permuted_allocation["source_fingerprint"] == allocation["source_fingerprint"]
        and permuted_allocation["calculation_fingerprint"]
        == allocation["calculation_fingerprint"],
        "supplier cost fingerprints are invariant to equal-sort source row order",
    )
    changed_currency = _supplier_cost_allocations(
        {
            **allocation_sources,
            "shipments": [{**allocation_sources["shipments"][0], "currency": "USD"}],
        }
    )[shipment_id]
    _assert(
        changed_currency["source_fingerprint"] != allocation["source_fingerprint"]
        and changed_currency["calculation_fingerprint"]
        != allocation["calculation_fingerprint"],
        "invoice currency is bound into both supplier cost fingerprints",
    )
    checksum_mismatch = _supplier_cost_allocations(
        {
            **allocation_sources,
            "shipments": [
                {
                    **allocation_sources["shipments"][0],
                    "declared_invoice_total": "999999",
                    "invoice_amount_total": "513875",
                    "match_status": "checksum_error",
                }
            ],
        }
    )[shipment_id]
    _assert(
        any(
            item.get("code") == "invoice_checksum_mismatch"
            for item in checksum_mismatch["blockers"]
        )
        and checksum_mismatch["capital_rub"] is None
        and checksum_mismatch["source_fingerprint"] != allocation["source_fingerprint"],
        "invoice checksum mismatch changes the proof and fails exact cost closed",
    )
    by_nm = {int(item["nm_id"]): item for item in allocation["lines"]}
    expected = Decimal("130.4357210850608995999639293105")
    _assert(abs(Decimal(by_nm[391660889]["unit_cost_rub"]) - expected) < Decimal("1e-25"), "26GN390 Anti-Spy 16 Pro exact WAC")
    _assert(abs(Decimal(by_nm[391661710]["unit_cost_rub"]) - expected) < Decimal("1e-25"), "26GN390 Anti-Spy 16 Pro Max exact WAC")
    _assert(abs(Decimal(by_nm[391660889]["capital_rub"]) - Decimal("586960.7448827740481998376819")) < Decimal("1e-20"), "26GN390 391660889 capital")
    _assert(abs(Decimal(by_nm[391661710]["capital_rub"]) - Decimal("684787.5356965697228998106288")) < Decimal("1e-20"), "26GN390 391661710 capital")
    _assert(all(all(item.values()) for item in [allocation["controls"]]), "26GN390 conservation controls")
    document_controls = {
        str(item["document_id"]): item for item in allocation["document_controls"]
    }
    _assert(
        document_controls["payment-financial"]["document_type"]
        == "bank_transfer_application"
        and document_controls["payment-financial"]["eligible_component_count"] == 1
        and document_controls["payment-financial"]["allocated_component_count"] == 1
        and document_controls["payment-financial"]["conserved"] is True,
        "linked CNY payment is projected onto its internal financial document",
    )
    _assert(
        document_controls["fee-financial"]["eligible_component_count"] == 1
        and document_controls["fee-financial"]["allocated_component_count"] == 1
        and document_controls["fee-financial"]["conserved"] is True
        and document_controls["fee-financial"]["incomplete_reasons"] == [],
        "CNY/zero statement provenance rows do not duplicate canonical CNY-ledger fees",
    )
    _assert(
        document_controls["customs"]["eligible_component_count"] == 3
        and document_controls["customs"]["allocated_component_count"] == 3
        and document_controls["customs"]["incomplete_reasons"] == []
        and "packing-informational" not in document_controls
        and "packing_list" not in allocation["cost_affecting_document_types"],
        "canonical document controls conserve every customs component and exclude informational files",
    )
    archived_sources = copy.deepcopy(allocation_sources)
    archived_sources["financial_documents"].extend(
        [
            {
                "document_id": "bank-fee-rub-a",
                "supplier_order_id": shipment_id,
                "document_type": "bank_fee_statement",
                "document_number": "FEE-RUB-A",
                "document_date": "2026-05-21",
                "parse_status": "confirmed",
            },
            {
                "document_id": "bank-fee-rub-b",
                "supplier_order_id": shipment_id,
                "document_type": "bank_fee_statement",
                "document_number": "FEE-RUB-B",
                "document_date": "2026-05-21",
                "parse_status": "confirmed",
            },
            {
                "document_id": "log136-archive",
                "supplier_order_id": shipment_id,
                "document_type": "logistics_invoice",
                "document_number": "136",
                "document_date": "2026-07-03",
                "file_sha256": "same-as-active-log136",
                "parse_status": "excluded",
            },
        ]
    )
    archived_sources["financial_expense_lines"].extend(
        [
            {
                "line_id": "bank-fee-rub-a:fee",
                "financial_document_id": "bank-fee-rub-a",
                "supplier_order_id": shipment_id,
                "category": "bank_transfer_fee",
                "amount_rub": "100",
                "currency": "RUB",
                "status": "confirmed",
            },
            {
                "line_id": "bank-fee-rub-b:fee",
                "financial_document_id": "bank-fee-rub-b",
                "supplier_order_id": shipment_id,
                "category": "currency_control_fee",
                "amount_rub": "200",
                "currency": "RUB",
                "status": "confirmed",
            },
            {
                "line_id": "log136-archive:logistics",
                "financial_document_id": "log136-archive",
                "supplier_order_id": shipment_id,
                "category": "logistics",
                "amount_rub": "1075030",
                "currency": "RUB",
                "status": "confirmed",
            },
        ]
    )
    active_only = _supplier_cost_allocations(archived_sources)[shipment_id]
    active_component_count = sum(
        int(item["eligible_component_count"])
        for item in active_only["document_controls"]
    )
    _assert(
        active_component_count == 9
        and all(
            item["document_id"] != "log136-archive"
            for item in active_only["document_controls"]
        )
        and active_only["controls"]["document_allocation_conserved"] is True,
        "excluded duplicate invoice 136 is absent from the active 9-of-9 allocation",
    )
    stale_sources = copy.deepcopy(archived_sources)
    next(
        item
        for item in stale_sources["financial_documents"]
        if item["document_id"] == "log136-archive"
    )["parse_status"] = "confirmed"
    stale_allocation = _supplier_cost_allocations(stale_sources)[shipment_id]
    _assert(
        sum(
            int(item["eligible_component_count"])
            for item in stale_allocation["document_controls"]
        )
        == 10
        and Decimal(stale_allocation["capital_rub"])
        - Decimal(active_only["capital_rub"])
        == Decimal("1075030")
        and stale_allocation["source_fingerprint"]
        != active_only["source_fingerprint"],
        "excluding archived invoice 136 creates a semantic source revision and removes exactly 1,075,030 RUB",
    )
    stale_certification = _supplier_allocation_with_certification(
        active_only,
        active_version_id="whfv-stale-duplicate",
        active_fingerprints=(
            stale_allocation["source_fingerprint"],
            stale_allocation["calculation_fingerprint"],
        ),
    )
    fresh_certification = _supplier_allocation_with_certification(
        active_only,
        active_version_id="whfv-active-only",
        active_fingerprints=(
            active_only["source_fingerprint"],
            active_only["calculation_fingerprint"],
        ),
    )
    repeated = _supplier_cost_allocations(archived_sources)[shipment_id]
    _assert(
        stale_certification["certification"]["certified"] is False
        and fresh_certification["certification"]["certified"] is True
        and repeated["source_fingerprint"] == active_only["source_fingerprint"]
        and repeated["calculation_fingerprint"]
        == active_only["calculation_fingerprint"]
        and any(
            item["document_id"] == "log136-archive"
            for item in archived_sources["financial_documents"]
        ),
        "stale fingerprint cannot certify; replay is stable and excluded audit evidence is preserved",
    )
    partial_sources = copy.deepcopy(allocation_sources)
    partial_sources["financial_expense_lines"].append(
        {
            "line_id": "customs:needs-review",
            "financial_document_id": "customs",
            "supplier_order_id": shipment_id,
            "category": "import_duty_2010",
            "amount_rub": "10",
            "currency": "RUB",
            "status": "needs_review",
        }
    )
    partial_customs = next(
        item
        for item in _supplier_cost_allocations(partial_sources)[shipment_id]["document_controls"]
        if item["document_id"] == "customs"
    )
    _assert(
        partial_customs["eligible_component_count"] == 4
        and partial_customs["allocated_component_count"] == 3
        and partial_customs["conserved"] is False
        and any(
            reason["code"] == "financial_component_status_not_eligible"
            for reason in partial_customs["incomplete_reasons"]
        ),
        "unconfirmed customs evidence stays visible as a partial canonical document allocation",
    )


def _test_official_wb_snapshot_integrity() -> None:
    evidence = _wb_snapshot_integrity(
        {
            "snapshot_id": "wbsnap_test",
            "version_id": "whfv_test",
            "snapshot_date": "2026-07-20",
            "fetched_at": "2026-07-19T22:18:03Z",
            "pagination_complete": 1,
            "page_count": 1,
            "page_offsets_json": "[0]",
            "raw_rows_digest": "sha256:test",
            "raw_rows_json": json.dumps(
                [
                    {"nmId": 1, "chrtId": 11, "warehouseId": 101, "stockCount": 4, "inWayToClient": 2, "inWayFromClient": 1},
                    {"nmId": 1, "chrtId": 11, "warehouseId": 102, "stockCount": 6, "inWayToClient": 1, "inWayFromClient": 3},
                ]
            ),
            "items_json": json.dumps(
                [{"nm_id": 1, "quantity": 10, "in_way_to_client": 3, "in_way_from_client": 4}]
            ),
        }
    )
    _assert(evidence["raw_to_canonical_mapping_matches"] is True, "WB raw-to-canonical mapping")
    _assert(evidence["arithmetic"] == "10 + 3 + 4 = 17", "WB contour arithmetic")
    _assert(evidence["exact_duplicate_count"] == 0, "WB exact duplicate guard")
    _assert(evidence["source_key_duplicate_count"] == 0, "WB source key duplicate guard")


def _test_current_snapshot_business_date_gate() -> None:
    _assert(
        _current_snapshot_effective_date(
            captured_at="2026-07-19T22:18:00Z",
            snapshot_date="2026-07-20",
        )
        == "2026-07-20",
        "current warehouse version uses the canonical business timezone",
    )
    try:
        _current_snapshot_effective_date(
            captured_at="2026-07-20T22:18:00Z",
            snapshot_date="2026-07-20",
        )
    except WarehouseFunctionalError as exc:
        _assert(
            "stale WB snapshot date" in str(exc),
            "emergency/current-state version rejects a prior-day last-good snapshot",
        )
    else:
        raise AssertionError("a stale WB snapshot must not date current local state")
    omitted_zero_and_other = _wb_snapshot_integrity(
        {
            "snapshot_id": "wbsnap_zero_other",
            "version_id": "whfv_zero_other",
            "snapshot_date": "2026-07-20",
            "fetched_at": "2026-07-20T00:00:00Z",
            "pagination_complete": 1,
            "page_count": 1,
            "page_offsets_json": "[0]",
            "raw_rows_digest": "sha256:zero-other",
            "raw_rows_json": json.dumps(
                [
                    {
                        "nmId": 1,
                        "chrtId": 11,
                        "warehouseId": 0,
                        "warehouseName": "Остальные",
                        "regionName": "Регион А",
                        "quantity": 1,
                        "inWayToClient": 2,
                        "inWayFromClient": 3,
                    },
                    {
                        "nmId": 1,
                        "chrtId": 11,
                        "warehouseId": 0,
                        "warehouseName": "Остальные",
                        "regionName": "Регион Б",
                        "quantity": 4,
                        "inWayToClient": 5,
                        "inWayFromClient": 6,
                    },
                ]
            ),
            "items_json": json.dumps(
                [
                    {"nm_id": 1, "quantity": 5, "in_way_to_client": 7, "in_way_from_client": 9},
                    {"nm_id": 2, "quantity": 0, "in_way_to_client": 0, "in_way_from_client": 0},
                ]
            ),
        }
    )
    _assert(
        omitted_zero_and_other["raw_to_canonical_mapping_matches"] is True,
        "complete WB response may omit a requested certified-zero SKU",
    )
    _assert(
        omitted_zero_and_other["source_key_duplicate_count"] == 0,
        "WB Other bucket identity includes warehouse and region names",
    )


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
        _counted_cny_operation({"status": "posted", "document_status": "posted"}),
        "only a posted CNY operation from a posted document is eligible",
    )
    _assert(
        _counted_cny_operation(
            {
                "status": "needs_review",
                "source_document_id": "persisted-date-only",
                "error_reason": "date_only_deterministic_sequence",
            },
            document={"document_id": "persisted-date-only", "status": "posted"},
        ),
        "a persisted date-only ordering warning recovers posted status from its source document",
    )
    _assert(
        not _counted_cny_operation({"status": "needs_review", "document_status": "posted"})
        and not _counted_cny_operation(
            {
                "status": "needs_review",
                "error_reason": "date_only_deterministic_sequence",
            },
            document={"status": "needs_review"},
        )
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
    exact_balances, exact_unmatched = reconcile_discrepancies(
        discrepancies=[
            {
                "source_id": "40985996:short-receipt",
                "nm_id": 391660889,
                "quantity": "253",
                "capital": "25300",
            }
        ],
        doprinato=[
            {
                "source_id": "40985996:over-receipt-other-sku",
                "business_date": "2026-07-18",
                "nm_id": 391661710,
                "quantity": "249",
            }
        ],
        audit=[],
    )
    _assert(
        exact_balances
        and Decimal(str(exact_balances[0]["quantity"])) == Decimal("253")
        and int(exact_balances[0]["nm_id"]) == 391660889
        and exact_unmatched
        and Decimal(str(exact_unmatched[0]["quantity"])) == Decimal("249")
        and int(exact_unmatched[0]["nm_id"]) == 391661710,
        "40985996 short receipt 253 is never netted with +249 of another SKU",
    )


def _test_unavailable_cost_is_not_zero_wac() -> None:
    line = WarehouseLine(
        warehouse_key="FF_TO_WB",
        nm_id=10,
        quantity=Decimal("10"),
        capital=Decimal("0"),
        cost_covered_quantity=Decimal("0"),
        quality="physical_movement_cost_unavailable",
        provenance={"cost_blockers": ["ff_base_cost_unavailable"]},
    )
    payload = _line_payload(line)
    _assert(
        payload["quantity"] == "10"
        and payload["wac_rub"] is None
        and payload["capital_rub"] == "0"
        and payload["cost_covered_quantity"] == "0",
        "unknown base cost keeps physical quantity but never publishes a synthetic zero WAC",
    )
    balances, unmatched = reconcile_discrepancies(
        discrepancies=[
            {
                "source_id": "unknown-cost",
                "nm_id": 10,
                "quantity": "4",
                "capital": "0",
                "cost_covered_quantity": "0",
            }
        ],
        doprinato=[],
    )
    _assert(
        not unmatched
        and balances[0]["quantity"] == "4"
        and balances[0]["wac"] is None
        and balances[0]["cost_covered_quantity"] == "0",
        "unknown discrepancy cost remains unavailable instead of zero-valued",
    )


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


def _test_equal_timestamp_ff_receipt_ordering() -> None:
    created_at = "2026-07-24T10:27:06Z"
    outbound = {
        "operation_id": "ffso_97c4851b77cb4d4ab023",
        "operation_type": "auto_writeoff",
        "source_type": "wb_supply",
        "created_at": created_at,
    }
    supplier_receipt = {
        "operation_id": "ffso_bd364f578e80429c999f",
        "operation_type": "auto_receipt",
        "source_type": "supplier_shipment",
        "created_at": created_at,
    }
    ordered = sorted(
        [outbound, supplier_receipt],
        key=_ff_operation_replay_sort_key,
    )
    _assert(
        [item["operation_id"] for item in ordered]
        == ["ffso_bd364f578e80429c999f", "ffso_97c4851b77cb4d4ab023"],
        "same-second supplier receipt creates the FF cost pool before outbound",
    )
    later_receipt = {**supplier_receipt, "created_at": "2026-07-24T10:27:07Z"}
    _assert(
        sorted([outbound, later_receipt], key=_ff_operation_replay_sort_key)[0]
        == outbound,
        "semantic priority never reorders distinct operation timestamps",
    )


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


def _test_historical_projection_calendar_gate() -> None:
    complete = [
        {"as_of_date": day, "nm_id": 1, "quantity": "0", "wac_rub": "100"}
        for day in ("2026-07-01", "2026-07-02", "2026-07-03")
    ]
    calendar = _validate_historical_projection_calendar(
        complete,
        effective_date="2026-07-03",
    )
    _assert(calendar["expected_day_count"] == 3, "complete historical calendar is accepted")
    try:
        _validate_historical_projection_calendar(
            [complete[0], complete[2]],
            effective_date="2026-07-03",
        )
    except Exception as exc:
        _assert("2026-07-02" in str(exc), "missing historical day is named before activation")
    else:
        raise AssertionError("incomplete historical calendar must fail before activation")


def _test_versioned_historical_correction() -> None:
    opening = [
        {
            "nm_id": 104,
            "ff_unit_cost_rub": "10",
            "wb_unit_cost_rub": "14",
            "quality": "direct_24_06",
            "provenance": {"frozen": True},
        }
    ]
    quantities = [
        {
            "as_of_date": day,
            "nm_id": 104,
            "physical_quantity": quantity,
            "quantity_provenance": {"column_date": day},
        }
        for day, quantity in (
            ("2026-07-01", "10"),
            ("2026-07-02", "9"),
            ("2026-07-03", "8"),
        )
    ]
    full = build_historical_wb_cost_projection(
        opening_cost_map=opening,
        daily_quantity_rows=quantities,
        downstream_rows=[],
        cutover_date="2026-07-04",
    )
    snapshots = [
        {
            "bundle_version": "exact-history-v1",
            "as_of_date": "2026-07-03",
            "activated_at": "2026-07-03T23:00:00Z",
            "refreshed_at": "2026-07-03T23:00:00Z",
            "plan_json": json.dumps(
                {
                    "date_columns": ["2026-07-03"],
                    "sheets": [
                        {
                            "sheet_name": "DATA_VITRINA",
                            "rows": [["SKU", "SKU:104|stock_total", 8]],
                        }
                    ],
                }
            ),
        }
    ]
    cutover = {
        "cutover_id": FUNCTIONAL_CUTOVER_ID,
        "cutover_at": "2026-07-04T00:00:00Z",
        "plan_fingerprint": "sha256:cutover-smoke",
    }
    correction, rows = _build_versioned_historical_correction(
        cutover=cutover,
        opening_cost_map=opening,
        frozen_rows=full[:2],
        correction_quantity_rows=quantities[-1:],
        downstream_rows=[],
        ready_snapshot_rows=snapshots,
    )
    _assert(correction["required"] is True, "missing date creates a versioned correction")
    _assert(correction["missing_dates"] == ["2026-07-03"], "only absent dates are corrected")
    _assert(len(rows) == 1 and rows[0]["quantity"] == "8", "correction appends exact arithmetic row")
    _assert(
        rows[0]["provenance"]["versioned_historical_correction"][
            "supersedes_plan_fingerprint"
        ]
        == "sha256:cutover-smoke",
        "correction retains supersedes provenance",
    )
    zero_scope_snapshot = copy.deepcopy(snapshots[0])
    zero_scope_snapshot["bundle_version"] = "exact-history-with-later-zero-scope"
    zero_scope_snapshot["plan_json"] = json.dumps(
        {
            "date_columns": ["2026-07-03"],
            "sheets": [
                {
                    "sheet_name": "DATA_VITRINA",
                    "rows": [
                        ["SKU", "SKU:104|stock_total", 8],
                        ["SKU", "SKU:999|stock_total", 0],
                    ],
                }
            ],
        }
    )
    zero_scope_correction, zero_scope_rows = _build_versioned_historical_correction(
        cutover=cutover,
        opening_cost_map=opening,
        frozen_rows=full[:2],
        correction_quantity_rows=[
            quantities[-1],
            {
                "as_of_date": "2026-07-03",
                "nm_id": 999,
                "physical_quantity": "0",
                "quantity_provenance": {"column_date": "2026-07-03"},
            },
        ],
        downstream_rows=[],
        ready_snapshot_rows=[zero_scope_snapshot],
    )
    zero_scope_by_id = {int(item["nm_id"]): item for item in zero_scope_rows}
    _assert(
        zero_scope_correction["row_count"] == 2
        and zero_scope_by_id[999]["quantity"] == "0"
        and zero_scope_by_id[999]["wac_rub"] == "0"
        and zero_scope_by_id[999]["quality"] == "zero_quantity_without_cost_basis",
        "a zero-stock post-cutover SKU keeps exact coverage without inventing a cost basis",
    )
    _validate_historical_correction_plan(
        zero_scope_correction,
        rows=zero_scope_rows,
        cutover=cutover,
    )
    _validate_historical_correction_plan(correction, rows=rows, cutover=cutover)
    _validate_historical_correction_matches_derived(
        planned_correction=correction,
        planned_rows=rows,
        expected_correction=correction,
        expected_rows=rows,
    )
    injected_row = copy.deepcopy(rows[0])
    injected_row["nm_id"] = 105
    injected_row["fingerprint"] = "sha256:injected-existing-day"
    try:
        _validate_historical_correction_matches_derived(
            planned_correction=correction,
            planned_rows=[*rows, injected_row],
            expected_correction=correction,
            expected_rows=rows,
        )
    except Exception as exc:
        _assert(
            "rows differ from current persisted evidence" in str(exc),
            "apply rejects a self-consistent extra row not derived from current evidence",
        )
    else:
        raise AssertionError("non-derived historical correction row must fail closed")
    forged_manifest = copy.deepcopy(correction)
    forged_manifest["ready_snapshot_manifest"][0]["bundle_version"] = "forged"
    forged_manifest["ready_snapshot_manifest_digest"] = (
        _historical_snapshot_manifest_digest(
            forged_manifest["ready_snapshot_manifest"]
        )
    )
    try:
        _validate_historical_correction_plan(
            forged_manifest,
            rows=rows,
            cutover=cutover,
        )
    except Exception as exc:
        _assert(
            "manifest differs from row provenance" in str(exc)
            or "row source manifest digest mismatch" in str(exc),
            "correction audit manifest is cryptographically bound to row provenance",
        )
    else:
        raise AssertionError("forged correction source manifest must fail closed")
    metadata_only_snapshot = copy.deepcopy(snapshots[0])
    metadata_only_plan = json.loads(metadata_only_snapshot["plan_json"])
    metadata_only_plan["metadata"] = {"unrelated_publication_marker": "changed"}
    metadata_only_plan["sheets"][0]["rows"].append(
        ["SKU", "SKU:104|orderCount", 123]
    )
    metadata_only_snapshot["plan_json"] = json.dumps(metadata_only_plan)
    original_source_view = _functional_local_source_view(
        {
            "historical_wb_daily_quantities": [],
            "ready_snapshots": [],
            "historical_correction_missing_dates": ["2026-07-03"],
            "historical_correction_ready_snapshots": snapshots,
        }
    )
    metadata_only_source_view = _functional_local_source_view(
        {
            "historical_wb_daily_quantities": [],
            "ready_snapshots": [],
            "historical_correction_missing_dates": ["2026-07-03"],
            "historical_correction_ready_snapshots": [metadata_only_snapshot],
        }
    )
    exact_correction, exact_rows = _build_versioned_historical_correction(
        cutover=cutover,
        opening_cost_map=opening,
        frozen_rows=full[:2],
        correction_quantity_rows=original_source_view[
            "historical_correction_wb_daily_quantities"
        ],
        downstream_rows=[],
        ready_snapshot_rows=snapshots,
    )
    _assert(
        _guarded_local_sources(original_source_view)
        == _guarded_local_sources(metadata_only_source_view),
        "unrelated ready-snapshot rows and metadata do not invalidate the exact-column drift gate",
    )
    metadata_correction, metadata_rows = _build_versioned_historical_correction(
        cutover=cutover,
        opening_cost_map=opening,
        frozen_rows=full[:2],
        correction_quantity_rows=metadata_only_source_view[
            "historical_correction_wb_daily_quantities"
        ],
        downstream_rows=[],
        ready_snapshot_rows=[metadata_only_snapshot],
    )
    _assert(
        metadata_correction["ready_snapshot_manifest_digest"]
        == exact_correction["ready_snapshot_manifest_digest"]
        and [item["fingerprint"] for item in metadata_rows]
        == [item["fingerprint"] for item in exact_rows],
        "correction manifest and rows hash only the selected exact stock_total evidence",
    )
    drifted_opening = copy.deepcopy(opening)
    drifted_opening[0]["wb_unit_cost_rub"] = "15"
    try:
        _build_versioned_historical_correction(
            cutover=cutover,
            opening_cost_map=drifted_opening,
            frozen_rows=full[:2],
            correction_quantity_rows=quantities[-1:],
            downstream_rows=[],
            ready_snapshot_rows=snapshots,
        )
    except Exception as exc:
        _assert(
            "differs from existing frozen business values" in str(exc),
            "frozen overlap arithmetic drift blocks append-only correction",
        )
    else:
        raise AssertionError("historical correction must prove every overlapping frozen value")

    partial_opening = [
        *opening,
        {
            "nm_id": 105,
            "ff_unit_cost_rub": "20",
            "wb_unit_cost_rub": "24",
            "quality": "direct_24_06",
            "provenance": {"frozen": True},
        },
    ]
    partial_quantities = [
        *quantities,
        {
            "as_of_date": "2026-07-01",
            "nm_id": 105,
            "physical_quantity": "5",
            "quantity_provenance": {"column_date": "2026-07-01"},
        },
        {
            "as_of_date": "2026-07-02",
            "nm_id": 105,
            "physical_quantity": "5",
            "quantity_provenance": {"column_date": "2026-07-02"},
        },
    ]
    partial_full = build_historical_wb_cost_projection(
        opening_cost_map=partial_opening,
        daily_quantity_rows=partial_quantities,
        downstream_rows=[],
        cutover_date="2026-07-04",
    )
    partial_snapshot = copy.deepcopy(snapshots)
    partial_snapshot[0]["plan_json"] = json.dumps(
        {
            "date_columns": ["2026-07-01", "2026-07-02", "2026-07-03"],
            "sheets": [
                {
                    "sheet_name": "DATA_VITRINA",
                    "rows": [
                        ["SKU", "SKU:104|stock_total", 10, 9, 8],
                        ["SKU", "SKU:105|stock_total", 5, 5, ""],
                    ],
                }
            ],
        }
    )
    try:
        _build_versioned_historical_correction(
            cutover=cutover,
            opening_cost_map=partial_opening,
            frozen_rows=[
                item
                for item in partial_full
                if item["as_of_date"] in {"2026-07-01", "2026-07-02"}
            ],
            correction_quantity_rows=partial_quantities,
            downstream_rows=[],
            ready_snapshot_rows=partial_snapshot,
        )
    except Exception as exc:
        _assert(
            "incomplete exact stock_total evidence" in str(exc)
            and "2026-07-03:105" in str(exc),
            "a partially blank exact snapshot column fails closed with SKU identity",
        )
    else:
        raise AssertionError("partial SKU coverage must not complete a correction date")

    coherent_older_snapshot = copy.deepcopy(partial_snapshot[0])
    coherent_older_snapshot["bundle_version"] = "exact-history-older-coherent"
    coherent_older_snapshot["activated_at"] = "2026-07-03T21:00:00Z"
    coherent_older_snapshot["refreshed_at"] = "2026-07-03T21:00:00Z"
    coherent_older_snapshot["plan_json"] = json.dumps(
        {
            "date_columns": ["2026-07-01", "2026-07-02", "2026-07-03"],
            "sheets": [
                {
                    "sheet_name": "DATA_VITRINA",
                    "rows": [
                        ["SKU", "SKU:104|stock_total", 10, 9, 8],
                        ["SKU", "SKU:105|stock_total", 5, 5, 4],
                        ["SKU", "SKU:105|orderCount", 0, 0, 0],
                    ],
                }
            ],
        }
    )
    later_partial_snapshot = copy.deepcopy(partial_snapshot[0])
    later_partial_snapshot["bundle_version"] = "exact-history-later-partial"
    later_partial_snapshot["activated_at"] = "2026-07-03T23:00:00Z"
    later_partial_snapshot["refreshed_at"] = "2026-07-03T23:00:00Z"
    later_partial_snapshot["plan_json"] = json.dumps(
        {
            "date_columns": ["2026-07-01", "2026-07-02", "2026-07-03"],
            "sheets": [
                {
                    "sheet_name": "DATA_VITRINA",
                    "rows": [
                        ["SKU", "SKU:104|stock_total", 10, 9, 8],
                    ],
                }
            ],
        }
    )
    coherent_quantities = [
        *partial_quantities,
        {
            "as_of_date": "2026-07-03",
            "nm_id": 105,
            "physical_quantity": "4",
            "quantity_provenance": {"column_date": "2026-07-03"},
        },
    ]
    coherent_source_view = _functional_local_source_view(
        {
            "historical_wb_daily_quantities": [],
            "ready_snapshots": [],
            "historical_correction_missing_dates": ["2026-07-03"],
            "historical_correction_ready_snapshots": [
                coherent_older_snapshot,
                later_partial_snapshot,
            ],
        }
    )
    coherent_source_quantities = coherent_source_view[
        "historical_correction_wb_daily_quantities"
    ]
    correction, rows = _build_versioned_historical_correction(
        cutover=cutover,
        opening_cost_map=partial_opening,
        frozen_rows=[
            item
            for item in build_historical_wb_cost_projection(
                opening_cost_map=partial_opening,
                daily_quantity_rows=coherent_quantities,
                downstream_rows=[],
                cutover_date="2026-07-04",
            )
            if item["as_of_date"] in {"2026-07-01", "2026-07-02"}
        ],
        correction_quantity_rows=coherent_source_quantities,
        downstream_rows=[],
        ready_snapshot_rows=[coherent_older_snapshot, later_partial_snapshot],
    )
    _assert(
        correction["row_count"] == 2
        and {(int(item["nm_id"]), item["quantity"]) for item in rows}
        == {(104, "8"), (105, "4")},
        "a later snapshot that omits a whole SKU scope is skipped in favor of the newest coherent exact column",
    )


def _test_exact_historical_wb_quantity_evidence() -> None:
    period_plan = {
        "date_columns": ["2026-07-17", "2026-07-18", "2026-07-19"],
        "sheets": [
            {
                "sheet_name": "DATA_VITRINA",
                "rows": [
                    ["SKU", "SKU:104|stock_total", 7, 8, 99],
                    ["SKU", "SKU:105|stock_total", "", 3, 4],
                ],
            }
        ],
    }
    merged = _merge_historical_wb_quantity_evidence(
        canonical_rows=[
            {"as_of_date": "2026-07-19", "nm_id": 104, "physical_quantity": "9"},
        ],
        ready_snapshot_rows=[
            {
                "bundle_version": "period-smoke",
                "as_of_date": "2026-07-19",
                "plan_json": json.dumps(period_plan),
            }
        ],
    )
    correction_view = _functional_local_source_view(
        {
            "historical_wb_daily_quantities": [
                {"as_of_date": "2026-07-18", "nm_id": 104, "physical_quantity": "999"}
            ],
            "ready_snapshots": [],
            "historical_correction_missing_dates": ["2026-07-18"],
            "historical_correction_ready_snapshots": [
                {
                    "bundle_version": "exact-period-v1",
                    "as_of_date": "2026-07-18",
                    "activated_at": "2026-07-18T12:00:00Z",
                    "refreshed_at": "2026-07-18T12:00:00Z",
                    "plan_json": json.dumps(period_plan),
                }
            ],
        }
    )
    unrelated_day_plan = copy.deepcopy(period_plan)
    unrelated_day_plan["sheets"][0]["rows"][0][2] = 700
    unrelated_day_view = _functional_local_source_view(
        {
            "historical_wb_daily_quantities": [
                {"as_of_date": "2026-07-18", "nm_id": 104, "physical_quantity": "999"}
            ],
            "ready_snapshots": [],
            "historical_correction_missing_dates": ["2026-07-18"],
            "historical_correction_ready_snapshots": [
                {
                    "bundle_version": "exact-period-v1",
                    "as_of_date": "2026-07-18",
                    "activated_at": "2026-07-18T12:00:00Z",
                    "refreshed_at": "2026-07-18T12:00:00Z",
                    "plan_json": json.dumps(unrelated_day_plan),
                }
            ],
        }
    )
    _assert(
        _guarded_local_sources(correction_view)
        == _guarded_local_sources(unrelated_day_view),
        "exact-column drift ignores stock_total changes outside the dates being corrected",
    )
    multi_day_snapshot = {
        "bundle_version": "multi-day-later",
        "as_of_date": "2026-07-19",
        "activated_at": "2026-07-19T12:00:00Z",
        "refreshed_at": "2026-07-19T12:00:00Z",
        "plan_json": json.dumps(
            {
                "date_columns": ["2026-07-17", "2026-07-18"],
                "sheets": [
                    {
                        "sheet_name": "DATA_VITRINA",
                        "rows": [["SKU", "SKU:104|stock_total", 7, 8]],
                    }
                ],
            }
        ),
    }
    older_same_selected_day = {
        "bundle_version": "older-same-selected-day",
        "as_of_date": "2026-07-18",
        "activated_at": "2026-07-18T12:00:00Z",
        "refreshed_at": "2026-07-18T12:00:00Z",
        "plan_json": json.dumps(
            {
                "date_columns": ["2026-07-18"],
                "sheets": [
                    {
                        "sheet_name": "DATA_VITRINA",
                        "rows": [["SKU", "SKU:104|stock_total", 8]],
                    }
                ],
            }
        ),
    }
    multi_day_sources = {
        "historical_wb_daily_quantities": [],
        "ready_snapshots": [],
        "historical_correction_missing_dates": ["2026-07-17", "2026-07-18"],
        "historical_correction_ready_snapshots": [multi_day_snapshot],
    }
    multi_day_with_older = {
        **multi_day_sources,
        "historical_correction_ready_snapshots": [
            older_same_selected_day,
            multi_day_snapshot,
        ],
    }
    _assert(
        _guarded_local_sources(_functional_local_source_view(multi_day_sources))
        == _guarded_local_sources(_functional_local_source_view(multi_day_with_older)),
        "unchanged selected multi-date evidence has a stable business-date order and drift digest",
    )
    correction_quantities = {
        (item["as_of_date"], int(item["nm_id"])): item["physical_quantity"]
        for item in correction_view["historical_correction_wb_daily_quantities"]
    }
    _assert(
        correction_quantities[("2026-07-18", 104)] == "8",
        "historical correction ignores later mutable canonical rows in favor of exact columns",
    )
    by_key = {(item["as_of_date"], int(item["nm_id"])): item for item in merged}
    _assert(by_key[("2026-07-17", 104)]["physical_quantity"] == "7", "17 July uses its exact period column")
    _assert(by_key[("2026-07-18", 104)]["physical_quantity"] == "8", "18 July uses its exact period column")
    _assert(by_key[("2026-07-19", 104)]["physical_quantity"] == "9", "canonical daily evidence has priority")
    _assert(("2026-07-17", 105) not in by_key, "missing historical input is not fabricated as zero")
    _assert(
        by_key[("2026-07-18", 104)]["quantity_provenance"]["column_date"] == "2026-07-18",
        "historical provenance binds the exact business date",
    )
    older_complete = copy.deepcopy(period_plan)
    older_complete["date_columns"] = ["2026-07-18"]
    older_complete["sheets"][0]["rows"] = [
        ["SKU", "SKU:104|stock_total", 8],
        ["SKU", "SKU:105|stock_total", 3],
    ]
    later_partial = copy.deepcopy(older_complete)
    later_partial["sheets"][0]["rows"] = [["SKU", "SKU:104|stock_total", 9]]
    retained = _merge_historical_wb_quantity_evidence(
        canonical_rows=[],
        ready_snapshot_rows=[
            {
                "bundle_version": "older-complete",
                "as_of_date": "2026-07-18",
                "activated_at": "2026-07-18T20:00:00Z",
                "refreshed_at": "2026-07-18T20:00:00Z",
                "plan_json": json.dumps(older_complete),
            },
            {
                "bundle_version": "later-partial",
                "as_of_date": "2026-07-18",
                "activated_at": "2026-07-18T21:00:00Z",
                "refreshed_at": "2026-07-18T21:00:00Z",
                "plan_json": json.dumps(later_partial),
            },
        ],
    )
    retained_by_key = {
        (item["as_of_date"], int(item["nm_id"])): item["physical_quantity"]
        for item in retained
    }
    _assert(
        retained_by_key == {("2026-07-18", 104): "9", ("2026-07-18", 105): "3"},
        "ordinary historical merge never drops an older valid SKU cell",
    )


def _test_zero_quantity_without_cost_basis_consumer() -> None:
    with tempfile.TemporaryDirectory(prefix="warehouse-zero-cost-basis-") as temp_dir:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(temp_dir))
        WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: NOW)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers(
                       cutover_id,cutover_at,status,plan_fingerprint,source_watermarks_json,
                       absorbed_supply_revisions_json,backup_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    FUNCTIONAL_CUTOVER_ID,
                    "2026-07-19T00:00:00Z",
                    "posted",
                    "sha256:zero-basis-cutover",
                    "{}",
                    "{}",
                    "{}",
                    NOW,
                    NOW,
                ),
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost(
                       cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,
                       quality,provenance_json,fingerprint,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    FUNCTIONAL_CUTOVER_ID,
                    "2026-07-18",
                    999,
                    "0",
                    "0",
                    "0",
                    "zero_quantity_without_cost_basis",
                    "{}",
                    "sha256:zero-basis-row",
                    NOW,
                ),
            )
            conn.commit()
            finance_state, functional_applies = _functional_wb_cost_state(
                conn,
                as_of_date="2026-07-18",
                nm_id="999",
            )
        state = runtime.load_our_wb_cost_daily_state(as_of_date="2026-07-18")
        _assert(
            state[999]["stock_qty"] == 0.0
            and state[999]["our_wb_unit_cost_rub"] is None,
            "zero-stock historical coverage never becomes an invented zero WB unit cost",
        )
        _assert(
            functional_applies
            and finance_state is not None
            and finance_state["our_wb_unit_cost_rub"] is None,
            "weekly Finance consumes the zero-basis marker as an unknown unit cost",
        )


def _test_quality_localization_catalog() -> None:
    expected_codes = {
        "provisional",
        "mixed",
        "certified",
        "primary_documents",
        "confirmed_payments_provisional_expenses",
        "moving_weighted_average",
        "periodic_snapshot_wac",
        "periodic_snapshot_wac_provisional",
        "periodic_snapshot_wac_closed",
        "zero_quantity_without_cost_basis",
        "direct_24_06",
        "same_purchase_price",
        "interpolation",
        "extrapolation",
        "fallback_average",
        "direct_confirmed_downstream",
        "confirmed_weighted_downstream_unit_cost",
        "supply_specific_downstream_cost",
        "proportional_wac_outbound",
        "current_wac_adjustment",
        "pooled_final_acceptance_discrepancy",
        "empty",
    }
    missing = sorted(expected_codes - set(WAREHOUSE_QUALITY_PRESENTATIONS))
    _assert(not missing, f"warehouse quality localization is complete: missing={missing}")
    for code in expected_codes:
        label, description = WAREHOUSE_QUALITY_PRESENTATIONS[code]
        _assert(bool(label and description), f"warehouse quality {code} has label and explanation")
        _assert(label != code, f"warehouse quality {code} does not expose its technical token")


def _test_human_evidence_uses_source_quality_and_date() -> None:
    certified_status = _warehouse_balance_status_presentation(
        "direct_24_06",
        certified=True,
    )
    _assert(
        certified_status["tone"] == "success"
        and certified_status["label_ru"]
        == "Все расходы учтены / Подтверждено документами",
        "certified balance shows an explicit green certification caption regardless of quality code",
    )
    evidence = _warehouse_human_evidence(
        {
            "source_records": [
                {
                    "invoice_no": "CERTIFIED",
                    "invoice_date": "2026-07-01",
                    "flow_quantity": "1",
                    "flow_capital_rub": "10",
                    "expenses_complete_certification": True,
                    "payment_operation_ids": ["payment-certified"],
                    "bank_fee_source_ids": ["bank-fee-certified"],
                },
                {
                    "invoice_no": "PROVISIONAL",
                    "business_date": "2026-07-02",
                    "flow_quantity": "2",
                    "flow_capital_rub": "20",
                    "expenses_complete_certification": False,
                },
            ]
        },
        quantity="3",
        capital_rub="30",
        quality="mixed:certified,confirmed_payments_provisional_expenses",
    )
    items = {item["document"]: item for item in evidence["items"]}
    _assert(items["CERTIFIED"]["date"] == "2026-07-01", "evidence retains invoice date")
    _assert(items["PROVISIONAL"]["date"] == "2026-07-02", "evidence retains business date")
    _assert(
        items["CERTIFIED"]["confirmation_status"]
        == "Все расходы учтены / Подтверждено документами",
        "certified source keeps its own status inside a mixed SKU",
    )
    _assert(
        items["PROVISIONAL"]["confirmation_status"]
        == "Платежи подтверждены, часть расходов предварительная",
        "provisional source keeps its own status inside a mixed SKU",
    )
    _assert(
        "банковские комиссии" in items["CERTIFIED"]["cost_source"],
        "readable cost evidence retains bank-fee provenance",
    )
    supplies = _warehouse_human_evidence(
        {
            "source_records": [
                {
                    "wb_supply_id": "SUPPLY-A",
                    "business_date": "2026-07-03",
                    "packed_quantity": "12",
                    "accepted_quantity": "2",
                    "ff_wac_at_ledger_debit_rub": "5",
                    "downstream_pre_acceptance_addon_rub": "1",
                },
                {
                    "wb_supply_id": "SUPPLY-B",
                    "business_date": "2026-07-04",
                    "packed_quantity": "20",
                    "accepted_quantity": "0",
                    "ff_wac_at_ledger_debit_rub": "5",
                    "downstream_pre_acceptance_addon_rub": "1",
                },
            ]
        },
        quantity="30",
        capital_rub="180",
        quality="supply_specific_downstream_cost",
    )
    _assert(
        sum(Decimal(item["quantity_contribution"]) for item in supplies["items"]) == Decimal("30"),
        "FF to WB evidence conserves aggregate quantity across supplies",
    )
    _assert(
        sum(Decimal(item["capital_contribution_rub"]) for item in supplies["items"]) == Decimal("180"),
        "FF to WB evidence conserves aggregate capital across supplies",
    )
    ledger = _warehouse_human_evidence(
        {
            "source": "canonical_append_only_ff_ledger_replay",
            "cutover_date": "2026-07-05",
            "operations": [
                {
                    "operation_id": "op-in",
                    "created_at": "2026-07-06T10:00:00Z",
                    "quantity_delta": "5",
                    "unit_cost_rub": "10",
                },
                {
                    "operation_id": "op-out",
                    "created_at": "2026-07-07T10:00:00Z",
                    "quantity_delta": "-3",
                    "unit_cost_rub": "10",
                },
            ],
        },
        quantity="12",
        capital_rub="120",
        quality="moving_weighted_average",
    )
    _assert(
        [item["document"] for item in ledger["items"]]
        == ["Остаток FF на cutover", "Операция FF op-in", "Операция FF op-out"],
        "FF ledger evidence exposes opening and each operation",
    )
    _assert(
        sum(Decimal(item["quantity_contribution"]) for item in ledger["items"]) == Decimal("12"),
        "FF ledger evidence quantity reconciles to the balance",
    )
    _assert(
        sum(Decimal(item["capital_contribution_rub"]) for item in ledger["items"]) == Decimal("120"),
        "FF ledger evidence capital reconciles to the balance",
    )
    _assert(ledger["items"][1]["date"] == "2026-07-06", "FF ledger evidence retains operation date")
    nested_ledger = _warehouse_human_evidence(
        {"source_records": [{
            "source": "canonical_append_only_ff_ledger_replay",
            "cutover_date": "2026-07-05",
            "operations": [
                {
                    "operation_id": "op-in",
                    "created_at": "2026-07-06T10:00:00Z",
                    "quantity_delta": "5",
                    "unit_cost_rub": "10",
                },
                {
                    "operation_id": "op-out",
                    "created_at": "2026-07-07T10:00:00Z",
                    "quantity_delta": "-3",
                    "unit_cost_rub": "10",
                },
            ],
        }]},
        quantity="12",
        capital_rub="120",
        quality="moving_weighted_average",
    )
    _assert(
        [item["document"] for item in nested_ledger["items"]]
        == ["Остаток FF на cutover", "Операция FF op-in", "Операция FF op-out"],
        "persisted source_records FF provenance expands every ledger operation",
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
        week_payloads: list[tuple[str, str, dict[str, str]]] = []
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """CREATE TABLE wb_finance_weekly_aggregates(
                       seller_id TEXT,week_start TEXT,week_end TEXT,classifier_version TEXT,
                       metrics_json TEXT,report_ids_json TEXT,report_types_json TEXT,
                       unknown_reasons_json TEXT,calculated_at TEXT,
                       PRIMARY KEY(seller_id,week_start,week_end))"""
            )
            today = date.fromisoformat(current_business_date_iso())
            last_closed = today - timedelta(days=today.weekday() + 1)
            bases = (Decimal("100"), Decimal("200"), Decimal("700"))
            for week_index, revenue in enumerate(bases):
                week_end = last_closed - timedelta(days=7 * (2 - week_index))
                week_start = week_end - timedelta(days=6)
                metrics = {
                    "net_revenue": str(revenue),
                    "commission": str(revenue * Decimal("0.3396")),
                    "acquiring": str(revenue * Decimal("0.03")),
                    "logistics": str((Decimal("1"), Decimal("6"), Decimal("14"))[week_index]),
                    "storage": str(revenue * Decimal("0.01")),
                    "acceptance": str(revenue * Decimal("0.04")),
                    "capitalized_acceptance": str(revenue * Decimal("0.01")),
                    "marketing": str(revenue * Decimal("0.10")),
                    "transit_logistics": str(revenue * Decimal("0.03")),
                    "capitalized_transit_logistics": str(revenue * Decimal("0.005")),
                    "penalties": str(revenue * Decimal("0.005")),
                    "corrections": str(revenue * Decimal("0.004")),
                    "subscriptions": str(revenue * Decimal("0.003")),
                    "paid_services": str(revenue * Decimal("0.002")),
                    "review_points": str(revenue * Decimal("0.001")),
                    "other_deductions": str(revenue * Decimal("0.0005")),
                    "positive_adjustments": str(revenue * Decimal("0.02")),
                    "wb_remuneration_adjustment": str(revenue * Decimal("0.07")),
                }
                if week_index > 0:
                    metrics["agent_remuneration"] = str(
                        revenue * Decimal("0.3396")
                    )
                    metrics["commission"] = (
                        "999999" if week_index == 1 else metrics["agent_remuneration"]
                    )
                week_payloads.append(
                    (week_start.isoformat(), week_end.isoformat(), metrics)
                )
                conn.execute(
                    "INSERT INTO wb_finance_weekly_aggregates VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "seller-a",
                        week_start.isoformat(),
                        week_end.isoformat(),
                        WB_FINANCE_CLASSIFIER_VERSION,
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
        _assert(
            [(item["week_start"], item["week_end"]) for item in reference["weeks"]]
            == [(start, end) for start, end, _metrics in week_payloads],
            "reference uses the exact last three closed calendar slots",
        )
        _assert(
            reference["aggregation_rule"] == "SUM(amount) / SUM(net_revenue)"
            and reference["gross_buyout_revenue_field"] == "net_revenue",
            "reference pins one canonical denominator and SUM/SUM aggregation",
        )
        by_key = {row["key"]: row for row in reference["rows"]}
        _assert(
            list(by_key) == [str(spec["key"]) for spec in CALCULATION_REFERENCE_ROWS],
            "every canonical calculation-reference row is present once and in audited order",
        )
        _assert(
            [
                Decimal(value)
                for value in by_key["agent_remuneration"]["weekly_rate_pct"]
            ]
            == [Decimal("33.96")] * 3
            and Decimal(
                by_key["agent_remuneration"]["weighted_average_pct"]
            )
            == Decimal("33.96"),
            "canonical agent remuneration uses agent_remuneration/commission once without subtracting acquiring",
        )
        _assert(
            by_key["agent_remuneration"]["source_fields"]
            == ["agent_remuneration", "commission"]
            and by_key["agent_remuneration"]["source_mode"]
            == "first_available",
            "agent canonical field wins while commission remains only a compatible alias",
        )
        _assert(
            Decimal(by_key["acquiring"]["weighted_average_pct"])
            == Decimal("3"),
            "acquiring reference is separately classified",
        )
        _assert(
            [Decimal(value) for value in by_key["logistics"]["weekly_rate_pct"]]
            == [Decimal("1"), Decimal("3"), Decimal("2")]
            and Decimal(by_key["logistics"]["weighted_average_pct"])
            == Decimal("2.1"),
            "weighted reference is SUM(amount)/SUM(net_revenue), not mean weekly percentage",
        )
        required_atomic_rows = {
            "penalties",
            "corrections",
            "subscriptions",
            "paid_services",
            "review_points",
            "other_deductions",
            "marketing",
            "acceptance",
            "capitalized_acceptance",
            "transit_logistics",
            "capitalized_transit_logistics",
            "positive_adjustments",
            "wb_remuneration_adjustment",
        }
        _assert(
            required_atomic_rows <= set(by_key)
            and by_key["penalties"]["label"] == "Штрафы"
            and by_key["corrections"]["label"] == "Корректировки (расходы)",
            "penalties, corrections and every other canonical component stay explicit",
        )
        for key, row in by_key.items():
            _assert(
                row["denominator"] == "net_revenue"
                and row["aggregation_rule"]
                == "SUM(amount) / SUM(net_revenue)"
                and row["sign_rule"]
                and row["proxy_treatment"]
                and row["status"] == "ready",
                f"reference row audit is complete for {key}",
            )
        _assert(
            "капитализ" in by_key["acceptance"]["proxy_treatment"].casefold()
            and "не входит" in by_key["capitalized_acceptance"]["proxy_treatment"].casefold()
            and "остаток" in by_key["transit_logistics"]["proxy_treatment"].casefold(),
            "acceptance/transit disclose gross, proven capitalized share and retained residual",
        )

        latest_start, latest_end, latest_metrics = week_payloads[-1]
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "DELETE FROM wb_finance_weekly_aggregates WHERE week_start=? AND week_end=?",
                (latest_start, latest_end),
            )
            older_end = date.fromisoformat(week_payloads[0][1]) - timedelta(days=7)
            older_start = older_end - timedelta(days=6)
            conn.execute(
                "INSERT INTO wb_finance_weekly_aggregates VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "seller-a",
                    older_start.isoformat(),
                    older_end.isoformat(),
                    WB_FINANCE_CLASSIFIER_VERSION,
                    json.dumps({**week_payloads[0][2], "net_revenue": "999999999"}),
                    "[]",
                    "[]",
                    "[]",
                    NOW,
                ),
            )
            conn.commit()
        stale = block.get_payload()["reference"]
        _assert(
            stale["status"] == "partial"
            and stale["weeks"][-1]["status"] == "missing"
            and Decimal(stale["rows"][0]["weighted_average_pct"]) == Decimal("33.96")
            and stale["rows"][0]["ready_week_count"] == 2
            and stale["ready_week_count"] == 2
            and [(item["week_start"], item["week_end"]) for item in stale["weeks"]]
            == [(start, end) for start, end, _metrics in week_payloads],
            "missing latest cell stays blank while combined uses the two READY in-slot weeks",
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "INSERT INTO wb_finance_weekly_aggregates VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "seller-a",
                    latest_start,
                    latest_end,
                    WB_FINANCE_CLASSIFIER_VERSION,
                    json.dumps(latest_metrics),
                    "[]",
                    "[]",
                    "[]",
                    NOW,
                ),
            )
            conn.execute(
                "DELETE FROM wb_finance_weekly_aggregates WHERE week_start=? AND week_end=?",
                (week_payloads[1][0], week_payloads[1][1]),
            )
            conn.commit()
        partial = block.get_payload()["reference"]
        _assert(
            partial["status"] == "partial"
            and partial["weeks"][1]["status"] == "missing"
            and partial["rows"][0]["weekly_rate_pct"][1] is None
            and Decimal(partial["rows"][0]["weighted_average_pct"]) == Decimal("33.96")
            and partial["rows"][0]["ready_week_count"] == 2,
            "missing middle calendar week is excluded from a direct two-week combined",
        )


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
        "archival_estimate_active": [],
        "historical_wb_daily_quantities": [
            {
                "as_of_date": "2026-07-17",
                "nm_id": 1,
                "physical_quantity": "9",
            }
        ],
        "ready_snapshots": [
            {
                "bundle_version": "period-fixture",
                "as_of_date": "2026-07-19",
                "plan_json": json.dumps(
                    {
                        "date_columns": ["2026-07-17"],
                        "sheets": [
                            {
                                "sheet_name": "DATA_VITRINA",
                                "rows": [["stock", "SKU:1|stock_total", 10]],
                            }
                        ],
                    },
                    sort_keys=True,
                ),
            }
        ],
    }
    guarded_before_activation = _guarded_local_sources(
        _functional_local_source_view(source_rows)
    )
    activated_source_rows = copy.deepcopy(source_rows)
    activated_source_rows["archival_estimate_active"] = [
        {
            "version_id": "wbae_fixture",
            "plan_fingerprint": "sha256:archival-fixture",
            "nm_id": 259474327,
            "row_unit_cost_rub": "100.00",
            "row_quality": "business_approved_archival_estimate",
        }
    ]
    guarded_after_activation = _guarded_local_sources(
        _functional_local_source_view(activated_source_rows)
    )
    _assert(
        guarded_before_activation != guarded_after_activation
        and guarded_after_activation["archival_estimate_active"],
        "archival activation invalidates a previously built functional writer plan",
    )
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
            apply_digest = block._local_source_digest()  # noqa: SLF001
        _assert(capture["captured_at"] == NOW, "source capture exposes calculation timestamp")
        _assert(
            capture["local_source_digest"] == apply_digest,
            "dry-run and apply hash the same normalized local source view",
        )
        timestamps = iter(
            [
                "2026-07-19T18:59:59Z",
                "2026-07-19T19:00:01Z",
            ]
        )
        crossing_block = WarehouseFunctionalBlock(
            runtime=block.runtime,
            timestamp_factory=lambda: next(timestamps),
        )
        midnight_payload = {
            **wb_payload,
            "snapshot_date": "2026-07-20",
            "data": {
                **wb_payload["data"],
                "fetched_at": "2026-07-19T19:00:00Z",
            },
        }
        with patch(
            "packages.application.warehouse_functional._source_rows",
            return_value=source_rows,
        ):
            crossing_capture = crossing_block._capture_sources(  # noqa: SLF001
                captured_at=None,
                wb_payload=midnight_payload,
            )
        _assert(
            crossing_capture["captured_at"] == "2026-07-19T19:00:01Z",
            "coherent capture timestamp is sampled after the local source transaction",
        )
        _assert(
            _current_snapshot_effective_date(
                captured_at=crossing_capture["captured_at"],
                snapshot_date=crossing_capture["wb_snapshot"]["snapshot_date"],
            )
            == "2026-07-20",
            "a WB fetch crossing local midnight binds to the completed coherent capture",
        )


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
        runtime.save_nomenclature_item(
            {
                "item_id": "nom-104",
                "is_active": True,
                "our_sku": "anti-spy-smoke",
                "nm_id": 104,
                "barcode": "2052929000104",
                "vendor_code": "anti-spy-smoke",
                "nomenclature_name": "Anti-Spy smoke",
                "product_type": "anti_spy",
                "match_key": "anti_spy|smoke",
                "purchase_price_yuan": 7.2,
                "aliases": [],
                "compatible_model_keys": [],
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        block._local_source_digest = lambda **_: "sha256:local"  # type: ignore[method-assign]
        block._wb_supply_source_digest = lambda **_: "sha256:supply"  # type: ignore[method-assign]
        # This legacy guarded-publication fixture uses a hand-built plan and
        # intentionally has no complete canonical source tables. Dedicated
        # correction tests above exercise deterministic evidence re-derivation.
        block._validate_emergency_correction_against_current = (  # type: ignore[method-assign]
            lambda *_, **__: None
        )
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
                "raw_rows": [
                    {
                        "snapshot_date": NOW[:10],
                        "snapshot_ts": NOW,
                        "nmId": 104,
                        "warehouseId": 507,
                        "warehouseName": "Коледино",
                        "stockCount": 1,
                        "inWayToClient": 0,
                        "inWayFromClient": 0,
                    }
                ],
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
            "unmatched_doprinato": [
                {
                    "source_id": "unmatched-smoke",
                    "source_fingerprint": "sha256:unmatched-smoke",
                    "business_date": "2026-07-18",
                    "nm_id": 999,
                    "quantity": "1",
                    "matched_quantity": "0",
                    "reason": "smoke audit row",
                }
            ],
            "new_events": [],
            "movement_documents": [],
            "supplier_cost_states": [
                {
                    "shipment_id": "accepted-supplier-flow-smoke",
                    "source_fingerprint": "sha256:accepted-source",
                    "calculation_fingerprint": "sha256:accepted-calculation",
                    "expenses_complete": True,
                    "calculation_available": True,
                }
            ],
            "historical_wb_cost_projection": [
                {
                    "as_of_date": "2026-07-17",
                    "nm_id": 104,
                    "quantity": "1",
                    "wac_rub": "14",
                    "capital_rub": "14",
                    "quality": "periodic_snapshot_wac_closed",
                    "provenance": {"test": True, "frozen_at_cutover": True},
                    "fingerprint": "sha256:pre-cutover-daily",
                },
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
        compact_options = block.wb_warehouse_exclusion_options(
            excluded_warehouse_ids=(507, 999),
        )
        _assert(
            compact_options["active_version_id"] == applied["active_version"]["version_id"]
            and compact_options["options"][0]["warehouse_id"] == 507
            and compact_options["options"][0]["stock_quantity"] == 1.0
            and compact_options["options"][0]["selected"] is True
            and compact_options["temporarily_missing_selected_ids"] == [999],
            "incident selector reads sorted compact options from the active local version",
        )
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
            "requested_nm_ids": [104, 999998],
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
        _assert(
            replay_by_date[("2026-07-19", 104)]["quality"]
            == "periodic_snapshot_wac_provisional",
            "current functional day remains explicitly provisional",
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
                "historical_wb_cost_projection": [
                    plan["historical_wb_cost_projection"][0],
                    *daily_replay,
                ],
                "lines": [_line_payload(item) for item in candidate_lines],
                "summaries": _summaries(candidate_lines),
                "new_events": [acceptance_event],
            }
        )
        sync_plan.pop("plan_fingerprint", None)
        sync_plan["plan_fingerprint"] = _fingerprint(sync_plan)
        tampered_history_plan = copy.deepcopy(sync_plan)
        tampered_history_plan["historical_wb_cost_projection"][0]["capital_rub"] = "15"
        tampered_history_plan.pop("plan_fingerprint", None)
        tampered_history_plan["plan_fingerprint"] = _fingerprint(tampered_history_plan)
        class _CrossBusinessDateAtCommit:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self) -> str:
                self.calls += 1
                return (
                    "2026-07-19T12:05:00Z"
                    if self.calls <= 4
                    else "2026-07-20T12:05:00Z"
                )

        block.timestamp_factory = _CrossBusinessDateAtCommit()
        try:
            block.apply_plan(
                sync_plan,
                confirm_fingerprint=sync_plan["plan_fingerprint"],
            )
        except Exception as exc:
            _assert(
                "crossed the canonical business-date boundary before commit" in str(exc),
                "reviewed warehouse plan cannot commit after crossing local midnight",
            )
        else:
            raise AssertionError("cross-business-date warehouse commit must fail closed")
        block.timestamp_factory = lambda: "2026-07-19T12:05:00Z"
        try:
            block.apply_plan(
                tampered_history_plan,
                confirm_fingerprint=tampered_history_plan["plan_fingerprint"],
            )
        except Exception as exc:
            _assert(
                "differs from the frozen cutover history" in str(exc),
                "post-cutover apply rejects a rewritten frozen daily row",
            )
        else:
            raise AssertionError("post-cutover apply must not rewrite frozen daily history")
        sync_applied = block.apply_plan(
            sync_plan,
            confirm_fingerprint=sync_plan["plan_fingerprint"],
        )
        _assert(sync_applied["idempotent"] is False, "hourly daily WAC version publishes")
        frozen_reservation_state = copy.deepcopy(block.readback()["ff_reservations"])
        runtime.create_ff_stock_reservation_operation(
            operation_id="ffsr-functional-live-probe-reserve",
            source_key="functional-live-probe:reserve",
            supply_id="functional-live-probe",
            supply_revision="reserve-v1",
            operation_type="reserve",
            created_at="2026-07-19T12:06:00Z",
            diagnostics={"reason": "snapshot consistency smoke"},
            lines=[{"nm_id": 104, "quantity_delta": 1}],
            expected_current={},
        )
        _assert(
            runtime.list_ff_stock_reservations(supply_id="functional-live-probe"),
            "live reservation probe exists outside the frozen functional version",
        )
        _assert(
            block.readback()["ff_reservations"] == frozen_reservation_state,
            "active functional readback never mixes a live reservation with frozen FF quantities",
        )
        runtime.create_ff_stock_reservation_operation(
            operation_id="ffsr-functional-live-probe-release",
            source_key="functional-live-probe:release",
            supply_id="functional-live-probe",
            supply_revision="release-v1",
            operation_type="release",
            created_at="2026-07-19T12:07:00Z",
            diagnostics={"reason": "snapshot consistency smoke cleanup"},
            lines=[{"nm_id": 104, "quantity_delta": -1}],
            expected_current={104: 1},
        )
        with sqlite3.connect(runtime.db_path) as conn:
            retained_supplier_cost_state = conn.execute(
                """SELECT source_fingerprint,calculation_fingerprint,expenses_complete
                   FROM sheet_vitrina_v1_warehouse_supplier_cost_states
                   WHERE version_id=? AND shipment_id='accepted-supplier-flow-smoke'""",
                (sync_applied["active_version"]["version_id"],),
            ).fetchone()
            unmatched_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT unmatched_id FROM sheet_vitrina_v1_warehouse_unmatched_doprinato ORDER BY created_at,unmatched_id"
                ).fetchall()
            ]
        _assert(
            retained_supplier_cost_state
            == ("sha256:accepted-source", "sha256:accepted-calculation", 1),
            "supplier certification fingerprints remain versioned after goods leave transit",
        )
        _assert(len(unmatched_ids) == 2, "the same unmatched evidence is auditable in two versions")
        _assert(len(set(unmatched_ids)) == 2, "unmatched identity includes its owning version")
        daily_state = runtime.load_our_wb_cost_daily_state(as_of_date="2026-07-19")
        _assert(
            Decimal(str(daily_state[104]["our_wb_unit_cost_rub"])) == Decimal("17"),
            "canonical WB cost consumer reads replayed daily projection",
        )
        active_version_id = sync_applied["active_version"]["version_id"]
        with sqlite3.connect(runtime.db_path) as conn:
            original_certification_row = conn.execute(
                """SELECT certified,provenance_json
                   FROM sheet_vitrina_v1_warehouse_functional_balances
                   WHERE version_id=? AND warehouse_key='wb' AND nm_id=104""",
                (active_version_id,),
            ).fetchone()
            _assert(original_certification_row is not None, "active WB row for revalidation probe")
            conn.execute(
                """UPDATE sheet_vitrina_v1_warehouse_functional_balances
                   SET certified=1,provenance_json=?
                   WHERE version_id=? AND warehouse_key='wb' AND nm_id=104""",
                (
                    json.dumps({"source_records": [{"shipment_id": "accepted-supplier-flow-smoke"}]}),
                    active_version_id,
                ),
            )
            conn.commit()
        try:
            with patch(
                "packages.application.warehouse_functional.load_supplier_line_cost_breakdown",
                return_value={
                    "certification": {
                        "certified": False,
                        "source_fingerprint_matches": False,
                        "active_version_id": active_version_id,
                    }
                },
            ) as breakdown_loader:
                stale_source_state = OwnProductCapitalBlock(
                    runtime=runtime,
                    timestamp_factory=lambda: "2026-07-19T12:00:00Z",
                ).load_daily_metric_lookup(
                    "2026-07-19",
                    requested_nm_ids=[104],
                    revalidate_current_sources=True,
                )[104]
                closed_date_state = OwnProductCapitalBlock(
                    runtime=runtime,
                    timestamp_factory=lambda: "2026-07-20T12:00:00Z",
                ).load_daily_metric_lookup(
                    "2026-07-19",
                    requested_nm_ids=[104],
                    revalidate_current_sources=True,
                )[104]
                _assert(
                    breakdown_loader.call_count == 1,
                    "closed historical versions do not revalidate against mutable current sources",
                )
        finally:
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute(
                    """UPDATE sheet_vitrina_v1_warehouse_functional_balances
                       SET certified=?,provenance_json=?
                       WHERE version_id=? AND warehouse_key='wb' AND nm_id=104""",
                    (
                        original_certification_row[0],
                        original_certification_row[1],
                        active_version_id,
                    ),
                )
                conn.commit()
        _assert(
            stale_source_state["presentation_state"] == "unconfirmed"
            and "source_changed_provisional" in stale_source_state["presentation_reason"],
            "economics projection removes stale green certification before targeted replay",
        )
        _assert(
            closed_date_state["presentation_state"] == "confirmed"
            and "source_changed_provisional" not in closed_date_state["presentation_reason"],
            "closed historical certification remains frozen to its exact-date functional version",
        )
        empty_exact_state = OwnProductCapitalBlock(runtime=runtime).load_daily_metric_lookup(
            "2026-07-19",
            requested_nm_ids=[999998, 999999],
        )
        _assert(
            empty_exact_state[999998]["own_total_product_qty"] == 0.0,
            "exact functional day materializes a proved zero for an SKU requested by that snapshot",
        )
        _assert(
            999999 not in empty_exact_state,
            "exact functional day does not fabricate zero for a currently enabled SKU outside historical scope",
        )
        missing_exact_state = OwnProductCapitalBlock(runtime=runtime).load_daily_metric_lookup(
            "2026-07-20",
            requested_nm_ids=[999999],
        )
        _assert(
            missing_exact_state == {},
            "missing functional day does not materialize a zero or carry the previous day",
        )
        sync_repeated = block.apply_plan(
            sync_plan,
            confirm_fingerprint=sync_plan["plan_fingerprint"],
        )
        _assert(sync_repeated["idempotent"] is True, "exact hourly apply remains idempotent")
        stale_plan = copy.deepcopy(sync_plan)
        stale_plan["captured_at"] = "2026-07-20T12:00:00Z"
        stale_plan["effective_date"] = "2026-07-20"
        stale_plan["wb_snapshot"]["snapshot_date"] = "2026-07-20"
        stale_plan.pop("plan_fingerprint", None)
        stale_plan["plan_fingerprint"] = _fingerprint(stale_plan)
        block.timestamp_factory = lambda: "2026-07-20T12:05:00Z"
        try:
            block.apply_plan(stale_plan, confirm_fingerprint=stale_plan["plan_fingerprint"])
        except Exception as exc:
            _assert("active functional warehouse version drifted" in str(exc), "stale active guard")
        else:
            raise AssertionError("stale concurrent functional plan must not publish")
        block.timestamp_factory = lambda: "2026-07-19T12:05:00Z"
        with sqlite3.connect(runtime.db_path) as conn:
            frozen_before = conn.execute(
                """SELECT fingerprint,created_at
                   FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                   WHERE cutover_id=? AND as_of_date='2026-07-17' AND nm_id=104""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
        correction_id = "whcorr_smoke_missing_day"
        correction_snapshot_manifest = [
            {
                "business_date": "2026-07-16",
                "bundle_version": "smoke-history",
                "snapshot_as_of_date": "2026-07-16",
                "activated_at": NOW,
                "refreshed_at": NOW,
                "sku_count": 1,
                "exact_stock_total_sha256": "sha256:smoke-column",
            }
        ]
        correction_snapshot_manifest_digest = _historical_snapshot_manifest_digest(
            correction_snapshot_manifest
        )
        correction_row = _daily_wb_cost_row(
            day="2026-07-16",
            nm_id=104,
            quantity=Decimal("1"),
            wac=Decimal("14"),
            quality="direct_24_06",
            provenance={
                "quantity_evidence": {
                    "bundle_version": "smoke-history",
                    "snapshot_as_of_date": "2026-07-16",
                    "snapshot_activated_at": NOW,
                    "snapshot_refreshed_at": NOW,
                    "snapshot_exact_stock_total_sha256": "sha256:smoke-column",
                },
                "versioned_historical_correction": {
                    "correction_id": correction_id,
                    "supersedes_cutover_id": FUNCTIONAL_CUTOVER_ID,
                    "supersedes_plan_fingerprint": plan["plan_fingerprint"],
                    "ready_snapshot_manifest_digest": correction_snapshot_manifest_digest,
                }
            },
        )
        emergency_plan = copy.deepcopy(sync_plan)
        emergency_plan.update(
            {
                "kind": "emergency_rebuild",
                "base_active_version_id": sync_applied["active_version"]["version_id"],
                "historical_wb_cost_projection": [
                    correction_row,
                    *sync_plan["historical_wb_cost_projection"],
                ],
                "historical_correction": {
                    "required": True,
                    "correction_id": correction_id,
                    "missing_dates": ["2026-07-16"],
                    "row_count": 1,
                    "row_fingerprints": [correction_row["fingerprint"]],
                    "ready_snapshot_manifest": correction_snapshot_manifest,
                    "ready_snapshot_manifest_digest": correction_snapshot_manifest_digest,
                    "supersedes_cutover_id": FUNCTIONAL_CUTOVER_ID,
                    "supersedes_plan_fingerprint": plan["plan_fingerprint"],
                },
            }
        )
        emergency_plan.pop("plan_fingerprint", None)
        emergency_plan["plan_fingerprint"] = _fingerprint(emergency_plan)
        rejected_emergency_plan = copy.deepcopy(emergency_plan)
        rejected_emergency_plan["local_source_digest"] = "sha256:drifted-before-backup"
        rejected_emergency_plan.pop("plan_fingerprint", None)
        rejected_emergency_plan["plan_fingerprint"] = _fingerprint(
            rejected_emergency_plan
        )
        rejected_backup_dir = root / "rejected-emergency-backups"
        try:
            block.apply_plan(
                rejected_emergency_plan,
                confirm_fingerprint=rejected_emergency_plan["plan_fingerprint"],
                backup_dir=rejected_backup_dir,
            )
        except Exception as exc:
            _assert(
                "local sources drifted" in str(exc),
                "emergency drift is rejected before backup",
            )
        else:
            raise AssertionError("drifted emergency plan must not apply")
        _assert(
            not rejected_backup_dir.exists()
            or not list(rejected_backup_dir.iterdir()),
            "rejected emergency plan leaves no full backup",
        )
        locked_drift_plan = copy.deepcopy(emergency_plan)
        locked_drift_plan["source_watermarks"] = {"locked_drift_smoke": True}
        locked_drift_plan.pop("plan_fingerprint", None)
        locked_drift_plan["plan_fingerprint"] = _fingerprint(locked_drift_plan)
        locked_drift_backup_dir = root / "locked-drift-emergency-backups"
        with patch.object(
            block,
            "_local_source_digest",
            side_effect=["sha256:local", "sha256:locked-drift"],
        ):
            try:
                block.apply_plan(
                    locked_drift_plan,
                    confirm_fingerprint=locked_drift_plan["plan_fingerprint"],
                    backup_dir=locked_drift_backup_dir,
                )
            except Exception as exc:
                _assert(
                    "while acquiring apply lock" in str(exc),
                    "locked source drift aborts before any transaction commit",
                )
            else:
                raise AssertionError("locked source drift must abort emergency apply")
        _assert(
            not locked_drift_backup_dir.exists()
            or not list(locked_drift_backup_dir.iterdir()),
            "rolled-back emergency apply removes its unused coherent backup",
        )
        emergency_applied = block.apply_plan(
            emergency_plan,
            confirm_fingerprint=emergency_plan["plan_fingerprint"],
            backup_dir=root / "emergency-backups",
        )
        emergency_backup = emergency_applied.get("backup") or {}
        _assert(
            emergency_backup.get("tier") == "T2"
            and emergency_backup.get("lifecycle") == "retained",
            "emergency apply retains a warehouse-domain checkpoint",
        )
        emergency_checkpoint = next(
            artifact
            for artifact in emergency_backup.get("artifacts") or []
            if artifact.get("artifact_kind") == "domain_checkpoint"
        )
        _assert(
            Path(str(emergency_checkpoint["path"])).stat().st_mode & 0o777
            == 0o600,
            "emergency domain checkpoint remains owner-only",
        )
        with sqlite3.connect(runtime.db_path) as conn:
            frozen_after = conn.execute(
                """SELECT fingerprint,created_at
                   FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                   WHERE cutover_id=? AND as_of_date='2026-07-17' AND nm_id=104""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            corrected = conn.execute(
                """SELECT fingerprint FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                   WHERE cutover_id=? AND as_of_date='2026-07-16' AND nm_id=104""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            correction_audit = conn.execute(
                """SELECT correction_id,ready_snapshot_manifest_json,backup_json
                   FROM sheet_vitrina_v1_warehouse_wb_daily_cost_corrections
                   WHERE correction_id=?""",
                (correction_id,),
            ).fetchone()
        _assert(frozen_after == frozen_before, "emergency correction does not rewrite frozen rows")
        _assert(corrected[0] == correction_row["fingerprint"], "missing row is appended exactly")
        _assert(correction_audit is not None, "versioned correction audit is persisted atomically")
        _assert(
            json.loads(correction_audit[1]) == correction_snapshot_manifest,
            "correction audit retains the exact normalized source manifest",
        )
        emergency_repeated = block.apply_plan(
            emergency_plan,
            confirm_fingerprint=emergency_plan["plan_fingerprint"],
            backup_dir=root / "emergency-backups",
        )
        _assert(emergency_repeated["idempotent"] is True, "exact emergency apply is a no-op")
        entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir, runtime=runtime)
        try:
            entrypoint.handle_warehouse_emergency_apply_request(
                {
                    "confirm": True,
                    "plan": emergency_plan,
                    "fingerprint": emergency_plan["plan_fingerprint"],
                }
            )
        except ValueError as exc:
            _assert(
                "synchronous emergency apply is disabled" in str(exc),
                "HTTP/UI contour cannot start a mutation that outlives proxy timeout",
            )
        else:
            raise AssertionError("HTTP/UI emergency apply must stay preview-only")
        overview = entrypoint.handle_warehouses_overview_request()
        _assert(overview["contract_name"] == "sheet_vitrina_v1_warehouse_functional", "functional HTTP overview")
        _assert(len(overview["warehouses"]) == 6, "functional HTTP exposes six warehouses")
        wb_detail = entrypoint.handle_warehouse_detail_request("wb")
        _assert((wb_detail.get("warehouse") or {}).get("wb_contour") is not None, "WB HTTP detail exposes contour")
        _assert(
            wb_detail.get("probe_shape")
            == {
                "warehouse_key": "wb",
                "required_collections": ["balances"],
                "documents_lazy": True,
                "provenance_lazy": True,
            },
            "warehouse detail declares its compact lazy probe shape",
        )
        _assert(wb_detail.get("documents") == [], "initial warehouse response excludes documents")
        _assert(
            int(wb_detail.get("payload_bytes") or 0) < 500_000,
            "initial warehouse response stays within the compact payload budget",
        )
        documents_page = entrypoint.handle_warehouse_documents_request("wb", page=1, limit=25)
        _assert(documents_page["documents"], "warehouse documents load lazily")
        _assert(
            all(not item.get("lines") for item in documents_page["documents"]),
            "document pages exclude line/provenance payloads",
        )
        document_detail = entrypoint.handle_warehouse_document_detail_request(
            "wb",
            documents_page["documents"][0]["document_id"],
        )
        _assert(document_detail["document"].get("lines"), "expanded document exposes its lines")
        document_line = document_detail["document"]["lines"][0]
        _assert(
            document_line["quality_presentation"]["code"] != "provisional",
            "persisted document line retains its actual quality",
        )
        _assert(
            document_line["human_evidence"]["items"][0]["date"] != "—",
            "persisted document evidence has a known document date fallback",
        )
        wb_balance = next(item for item in wb_detail["balances"] if int(item["nm_id"]) == 104)
        _assert(wb_balance["nomenclature_name"] == "Anti-Spy smoke", "active nomenclature resolves exact nmID")
        _assert(wb_balance["barcode"] == "2052929000104", "active nomenclature exposes stable barcode")
        _assert(
            wb_balance["identity_source"] == "active_nomenclature_exact_nm_id",
            "warehouse identity records exact-key provenance",
        )
        _assert(wb_balance["quality_presentation"]["label_ru"], "warehouse quality has a Russian label")
        _assert(wb_balance["human_evidence"] is None, "initial balance defers provenance")
        wb_balance_detail = entrypoint.handle_warehouse_balance_detail_request("wb", 104)
        _assert(
            wb_balance_detail["human_evidence"]["items"],
            "expanded balance exposes structured human evidence",
        )
        runtime.save_nomenclature_item(
            {
                "item_id": "nom-104-conflict",
                "is_active": True,
                "our_sku": "conflicting-smoke",
                "nm_id": 104,
                "barcode": "2052929000999",
                "vendor_code": "conflicting-smoke",
                "nomenclature_name": "Conflicting smoke",
                "product_type": "anti_spy",
                "match_key": "anti_spy|conflicting-smoke",
                "purchase_price_yuan": 7.2,
                "aliases": [],
                "compatible_model_keys": [],
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        ambiguous_detail = entrypoint.handle_warehouse_detail_request("wb")
        ambiguous_balance = next(
            item for item in ambiguous_detail["balances"] if int(item["nm_id"]) == 104
        )
        _assert(
            "Неоднозначная активная номенклатура" in ambiguous_balance["identity_warning"]
            and ambiguous_balance["quality_tone"] == "warning",
            "identity-review warning cannot inherit a certified success tone",
        )
        _assert(wb_detail["warehouse"]["status_label"] != wb_detail["warehouse"]["status"], "status code is localized")
        discrepancy_detail = entrypoint.handle_warehouse_detail_request("wb_acceptance_discrepancy")
        _assert(
            (discrepancy_detail.get("unmatched_doprinato") or [])[0]["human_evidence"]["items"],
            "unmatched audit row exposes structured human evidence",
        )
        settings = entrypoint.handle_calculation_parameters_request()
        _assert(settings["status"] == "ready", "calculation parameters HTTP readback")
        _test_ready_snapshot_recovery_scan_is_bounded(runtime)
        _test_functional_economics_backfill(runtime=runtime, root=root)
        active_before_failure = block.readback()["active_version"]["version_id"]
        block.record_failed_sync(RuntimeError("injected 429 exhaustion"))
        failed = block.readback()
        _assert(failed["active_version"]["version_id"] == active_before_failure, "last good survives failure")
        _assert("429" in failed["sync"]["last_error"], "last failure is visible")
        failed_public = entrypoint.handle_warehouse_detail_request("wb")
        failed_description = str((failed_public.get("warehouse") or {}).get("status_description") or "")
        _assert("ограничил частоту" in failed_description, "public sync reason is localized and categorized")
        _assert("injected 429 exhaustion" not in failed_description, "raw sync error stays outside public reason")
        rolled_back = block.rollback_functional_cutover(
            confirm_fingerprint=plan["plan_fingerprint"],
            backup_dir=root / "rollback-backups",
        )
        _assert(rolled_back["status"] == "rolled_back", "bounded rollback")
        _assert(block.readback()["status"] == "not_initialized", "derived state removed")
        with sqlite3.connect(runtime.db_path) as conn:
            _assert(conn.execute("SELECT COUNT(*) FROM immutable_warehouse_opening_v1").fetchone()[0] == 1, "old opening audit preserved")
        _assert(
            applied["backup"]["tier"] == "T3",
            "functional schema cutover is the allowlisted full-backup tier",
        )
        backup_artifact = next(
            artifact
            for artifact in applied["backup"].get("artifacts") or []
            if artifact.get("artifact_kind") == "raw"
        )
        backup_path = Path(str(backup_artifact["path"]))
        _assert(backup_path.stat().st_mode & 0o777 == 0o600, "backup mode 0600")
        _assert(
            backup_path.name.startswith(f"{FUNCTIONAL_CUTOVER_ID}-")
            and backup_path.name != f"{FUNCTIONAL_CUTOVER_ID}.sqlite3",
            "functional cutover backup keeps the canonical timestamped recovery name",
        )


def _test_ready_snapshot_recovery_scan_is_bounded(runtime: RegistryUploadDbBackedRuntime) -> None:
    future_plan = {
        "date_columns": ["2026-07-17", "2026-08-01"],
        "sheets": [
            {
                "sheet_name": "DATA_VITRINA",
                "rows": [["SKU", "SKU:104|stock_total", 10, 99]],
            }
        ],
    }
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO registry_upload_versions(bundle_version,uploaded_at,activated_at) VALUES(?,?,?)",
            ("future-recovery-smoke", "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO registry_upload_versions(bundle_version,uploaded_at,activated_at) VALUES(?,?,?)",
            ("late-predated-recovery-smoke", "2026-07-20T00:00:00Z", "2026-07-20T00:00:00Z"),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_ready_snapshots(
                   bundle_version,activated_at,as_of_date,snapshot_id,plan_version,refreshed_at,plan_json
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                "future-recovery-smoke",
                "2026-08-01T00:00:00Z",
                "2026-08-01",
                "future-recovery-snapshot",
                "v1",
                "2026-08-01T00:00:00Z",
                json.dumps(future_plan),
            ),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_ready_snapshots(
                   bundle_version,activated_at,as_of_date,snapshot_id,plan_version,refreshed_at,plan_json
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                "late-predated-recovery-smoke",
                "2026-07-20T00:00:00Z",
                "2026-07-17",
                "late-predated-recovery-snapshot",
                "v1",
                "2026-07-20T00:00:00Z",
                json.dumps(future_plan),
            ),
        )
        conn.commit()
        snapshots = _ready_snapshot_recovery_rows(conn, recovery_boundary="2026-07-18")
        _assert(
            all(item["bundle_version"] != "future-recovery-smoke" for item in snapshots),
            "post-cutover ready snapshots are excluded from the recovery source scan",
        )
        correction_snapshots = _ready_snapshot_historical_correction_rows(
            conn,
            missing_dates=["2026-07-17"],
        )
        _assert(
            {
                item["bundle_version"]
                for item in correction_snapshots
            }
            >= {"future-recovery-smoke", "late-predated-recovery-smoke"},
            "missing-date correction admits later persisted bundles carrying the exact old column",
        )
        ready_snapshots, frozen = _historical_recovery_source_rows(
            conn,
            cutover_at=NOW,
            recovery_boundary="2026-07-18",
        )
        _assert(
            ready_snapshots == [],
            "an established cutover rejects even later-published snapshots with pre-cutover outer dates",
        )
        _assert(
            [item["as_of_date"] for item in frozen] == ["2026-07-16", "2026-07-17"]
            and frozen[1]["fingerprint"] == "sha256:pre-cutover-daily",
            "post-cutover source capture reuses original plus append-only corrected frozen rows",
        )
        for day in range(1, 16):
            business_date = f"2026-07-{day:02d}"
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost(
                       cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,quality,
                       provenance_json,fingerprint,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    FUNCTIONAL_CUTOVER_ID,
                    business_date,
                    104,
                    "1",
                    "14",
                    "14",
                    "direct_24_06",
                    "{}",
                    f"sha256:complete-calendar-{day:02d}",
                    NOW,
                ),
            )
        complete_frozen_calendar = [
            {"as_of_date": row[0]}
            for row in conn.execute(
                """SELECT DISTINCT as_of_date
                   FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                   WHERE cutover_id=? AND as_of_date<'2026-07-18'
                   ORDER BY as_of_date""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchall()
        ]
        _assert(
            _missing_pre_cutover_historical_dates(
                complete_frozen_calendar,
                cutover_date="2026-07-18",
            )
            == [],
            "complete frozen calendar gates mutable snapshots out of ordinary emergency digest",
        )
        conn.execute(
            """DELETE FROM sheet_vitrina_v1_warehouse_wb_daily_cost
               WHERE cutover_id=? AND fingerprint LIKE 'sha256:complete-calendar-%'""",
            (FUNCTIONAL_CUTOVER_ID,),
        )
        conn.execute(
            """DELETE FROM sheet_vitrina_v1_ready_snapshots
               WHERE bundle_version IN ('future-recovery-smoke','late-predated-recovery-smoke')"""
        )
        conn.execute(
            """DELETE FROM registry_upload_versions
               WHERE bundle_version IN ('future-recovery-smoke','late-predated-recovery-smoke')"""
        )
        conn.commit()


def _test_functional_economics_backfill(*, runtime: RegistryUploadDbBackedRuntime, root: Path) -> None:
    supplier_manifest_before = _warehouse_input_manifest_digest(
        runtime,
        dates=["2026-07-18", "2026-07-19"],
    )
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_supplier_shipments(
                   shipment_id,created_at,updated_at,shipment_date,match_status,warnings_json,errors_json
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                "economics-manifest-supplier-probe",
                NOW,
                NOW,
                "2026-07-18",
                "all_matched",
                "[]",
                "[]",
            ),
        )
        conn.commit()
    supplier_manifest_after = _warehouse_input_manifest_digest(
        runtime,
        dates=["2026-07-18", "2026-07-19"],
    )
    _assert(
        supplier_manifest_after != supplier_manifest_before,
        "economics optimistic manifest fingerprints mutable supplier evidence used by certification",
    )
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            "DELETE FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
            ("economics-manifest-supplier-probe",),
        )
        conn.commit()

    exact_days = _exact_functional_snapshot_dates(
        runtime,
        ["2026-07-18", "2026-07-19", "2026-07-20"],
    )
    _assert(
        {"2026-07-18", "2026-07-19"}.issubset(exact_days)
        and "2026-07-20" not in exact_days,
        "warehouse history identifies exact functional days without carrying the prior day",
    )
    exact_day_probe = {
        "bundle_version": "economics-exact-day-probe",
        "as_of_date": "2026-07-20",
        "refreshed_at": NOW,
        "plan_json": json.dumps(
            {
                "date_columns": ["2026-07-20"],
                "sheets": [
                    {
                        "sheet_name": "DATA_VITRINA",
                        "write_start_cell": "A1",
                        "header": ["Показатель", "row_id", "2026-07-20"],
                        "rows": [
                            ["SKU", "SKU:104|orderSum", 100],
                            ["SKU", "SKU:104|orderCount", 2],
                            ["SKU", "SKU:104|ads_sum", 10],
                            ["WB qty", "SKU:104|own_capital_WB_qty", 999],
                        ],
                    }
                ],
            }
        ),
    }
    parameters = {
        "2026-07-20": CalculationParametersBlock(runtime=runtime).parameters_for_date(
            "2026-07-20"
        )
    }
    probe_args = {
        "snapshot": exact_day_probe,
        "costs": {
            "2026-07-20": {
                104: {"our_wb_unit_cost_rub": 14, "stock_qty": 0}
            }
        },
        "warehouse_metrics": {"2026-07-20": {}},
        "parameters": parameters,
        "source_fingerprint": "sha256:exact-day-probe",
        "cutover_business_date": "2026-07-18",
    }
    missing_probe = _transform_snapshot(
        **probe_args,
        warehouse_exact_dates=set(),
        warehouse_covered_nm_ids={},
        warehouse_version_ids={},
    )
    missing_rows = {
        row[1]: row
        for row in json.loads(missing_probe["after_plan_json"])["sheets"][0]["rows"]
    }
    _assert(
        missing_rows["SKU:104|own_capital_WB_qty"][2] == "",
        "missing exact warehouse day stays unknown instead of carrying stale state",
    )
    missing_metadata = json.loads(missing_probe["after_plan_json"])["metadata"]
    _assert(
        "нет точной успешной функциональной версии" in missing_metadata[
            "warehouse_history_coverage"
        ]["2026-07-20"]["reason_ru"],
        "post-cutover warehouse gap reports the missing exact publication",
    )
    missing_presentation = missing_metadata["server_cell_presentation"][
        "SKU:104|own_capital_WB_qty"
    ]["2026-07-20"]
    _assert(
        missing_presentation["state"] == "unavailable"
        and "Исторические данные отсутствуют" in missing_presentation["reason"],
        "missing warehouse history reaches the server_cell_presentation contract consumed by UI",
    )
    empty_exact_probe = _transform_snapshot(
        **probe_args,
        warehouse_exact_dates={"2026-07-20"},
        warehouse_covered_nm_ids={"2026-07-20": {104}},
        warehouse_version_ids={"2026-07-20": "whfv_empty_exact"},
    )
    empty_exact_rows = {
        row[1]: row
        for row in json.loads(empty_exact_probe["after_plan_json"])["sheets"][0]["rows"]
    }
    _assert(
        empty_exact_rows["SKU:104|own_capital_WB_qty"][2] == 0.0,
        "an exact reconciled empty warehouse day publishes a proved zero",
    )
    uncovered_exact_probe = _transform_snapshot(
        **probe_args,
        warehouse_exact_dates={"2026-07-20"},
        warehouse_covered_nm_ids={"2026-07-20": {999}},
        warehouse_version_ids={"2026-07-20": "whfv_uncovered_exact"},
    )
    uncovered_exact_payload = json.loads(uncovered_exact_probe["after_plan_json"])
    uncovered_exact_rows = {
        row[1]: row for row in uncovered_exact_payload["sheets"][0]["rows"]
    }
    _assert(
        uncovered_exact_rows["SKU:104|own_capital_WB_qty"][2] == "",
        "an exact date keeps an SKU outside requested/canonical coverage unknown",
    )
    _assert(
        "не входила в requested nmID scope"
        in uncovered_exact_payload["metadata"]["server_cell_presentation"][
            "SKU:104|own_capital_WB_qty"
        ]["2026-07-20"]["reason"],
        "uncovered exact-date SKU carries a source-level UI reason",
    )
    _assert(
        uncovered_exact_rows["TOTAL|total_own_total_product_qty"][2] == ""
        and uncovered_exact_rows["TOTAL|total_own_total_product_capital_rub"][2] == "",
        "warehouse TOTAL remains unknown when any visible SKU is outside exact snapshot coverage",
    )
    uncovered_total_presentation = uncovered_exact_payload["metadata"][
        "server_cell_presentation"
    ]["TOTAL|total_own_total_product_qty"]["2026-07-20"]
    _assert(
        uncovered_total_presentation["state"] == "unavailable"
        and "Частичная сумма не публикуется" in uncovered_total_presentation["reason"],
        "partial exact coverage exposes an unavailable TOTAL instead of an understated sum",
    )
    provisional_exact_probe = _transform_snapshot(
        **{
            **probe_args,
            "warehouse_metrics": {
                "2026-07-20": {
                    104: {
                        "own_capital_WB_qty": "2",
                        "own_capital_WB_unit_cost_rub": "10",
                        "own_capital_WB_capital_rub": "20",
                        "own_total_product_qty": "2",
                        "own_total_product_capital_rub": "20",
                        "own_total_product_avg_cost_rub": "10",
                        "presentation_state": "unconfirmed",
                        "presentation_reason": "confirmed_payments_provisional_expenses",
                        "stage_presentation": {
                            "WB": {
                                "state": "unconfirmed",
                                "reason": "confirmed_payments_provisional_expenses",
                            }
                        },
                    }
                }
            },
        },
        warehouse_exact_dates={"2026-07-20"},
        warehouse_covered_nm_ids={"2026-07-20": {104}},
        warehouse_version_ids={"2026-07-20": "whfv_provisional_exact"},
    )
    provisional_payload = json.loads(provisional_exact_probe["after_plan_json"])
    provisional_presentation = provisional_payload["metadata"]["server_cell_presentation"]
    for row_id in (
        "SKU:104|own_capital_WB_qty",
        "TOTAL|total_own_capital_WB_qty",
    ):
        status = provisional_presentation[row_id]["2026-07-20"]
        _assert(
            status["state"] == "unconfirmed"
            and status["tone"] == "yellow"
            and "Платежи подтверждены, часть расходов предварительная" in status["reason"],
            f"exact-date provisional quality is published for {row_id}",
        )
    certified_exact_probe = _transform_snapshot(
        snapshot={
            **exact_day_probe,
            "plan_json": provisional_exact_probe["after_plan_json"],
        },
        costs=probe_args["costs"],
        warehouse_metrics={
            "2026-07-20": {
                104: {
                    "own_capital_WB_qty": "2",
                    "own_capital_WB_unit_cost_rub": "10",
                    "own_capital_WB_capital_rub": "20",
                    "own_total_product_qty": "2",
                    "own_total_product_capital_rub": "20",
                    "own_total_product_avg_cost_rub": "10",
                    "presentation_state": "confirmed",
                    "presentation_reason": "",
                    "stage_presentation": {
                        "WB": {"state": "confirmed", "reason": ""}
                    },
                }
            }
        },
        warehouse_exact_dates={"2026-07-20"},
        warehouse_covered_nm_ids={"2026-07-20": {104}},
        warehouse_version_ids={"2026-07-20": "whfv_certified_exact"},
        parameters=parameters,
        source_fingerprint="sha256:certified-exact-day-probe",
        cutover_business_date="2026-07-18",
    )
    certified_presentation = json.loads(certified_exact_probe["after_plan_json"])[
        "metadata"
    ].get("server_cell_presentation", {})
    _assert(
        "2026-07-20"
        not in certified_presentation.get("SKU:104|own_capital_WB_qty", {}),
        "certification removes the stale yellow exact-date presentation",
    )
    scoped_totals_probe = _transform_snapshot(
        **{
            **probe_args,
            "warehouse_metrics": {
                "2026-07-20": {
                    104: {
                        "own_capital_WB_qty": "2",
                        "own_capital_WB_capital_rub": "20",
                    },
                    999: {
                        "own_capital_WB_qty": "100",
                        "own_capital_WB_capital_rub": "1000",
                    },
                }
            },
        },
        warehouse_exact_dates={"2026-07-20"},
        warehouse_covered_nm_ids={"2026-07-20": {104, 999}},
        warehouse_version_ids={"2026-07-20": "whfv_scoped_totals"},
    )
    scoped_total_rows = {
        row[1]: row
        for row in json.loads(scoped_totals_probe["after_plan_json"])["sheets"][0]["rows"]
    }
    _assert(
        scoped_total_rows["TOTAL|total_own_capital_WB_qty"][2] == 2.0
        and scoped_total_rows["TOTAL|total_own_capital_WB_capital_rub"][2] == 20.0,
        "warehouse TOTAL rows equal the snapshot's published SKU scope and exclude hidden SKU state",
    )
    missing_cost_scope_probe = _transform_snapshot(
        snapshot={
            "bundle_version": "economics-missing-cost-scope",
            "as_of_date": "2026-06-30",
            "refreshed_at": NOW,
            "plan_json": json.dumps(
                {
                    "date_columns": ["2026-06-30"],
                    "sheets": [
                        {
                            "sheet_name": "DATA_VITRINA",
                            "write_start_cell": "A1",
                            "header": ["Показатель", "row_id", "2026-06-30"],
                            "rows": [
                                ["SKU 104", "SKU:104|orderSum", 100],
                                ["SKU 104", "SKU:104|orderCount", 2],
                                ["SKU 104", "SKU:104|ads_sum", 10],
                                ["SKU 105", "SKU:105|orderSum", 100],
                                ["SKU 105", "SKU:105|orderCount", 2],
                                ["SKU 105", "SKU:105|ads_sum", 10],
                            ],
                        }
                    ],
                }
            ),
        },
        costs={
            "2026-06-30": {
                104: {"our_wb_unit_cost_rub": 14, "stock_qty": 10}
            }
        },
        warehouse_metrics={},
        warehouse_exact_dates=set(),
        warehouse_covered_nm_ids={},
        warehouse_version_ids={},
        parameters={
            "2026-06-30": CalculationParametersBlock(
                runtime=runtime
            ).parameters_for_date("2026-07-01")
        },
        source_fingerprint="sha256:missing-cost-scope",
        cutover_business_date="2026-07-18",
    )
    missing_cost_scope_rows = {
        row[1]: row
        for row in json.loads(missing_cost_scope_probe["after_plan_json"])["sheets"][0]["rows"]
    }
    _assert(
        missing_cost_scope_rows["TOTAL|total_our_wb_unit_cost_rub"][2] == "",
        "pre-boundary TOTAL cost fails closed when any configured SKU lacks its 01.07 row",
    )
    targeted_dates = ["2026-07-19", "2026-07-20", "2026-07-21"]
    targeted_snapshot = {
        "bundle_version": "economics-targeted-probe",
        "as_of_date": "2026-07-21",
        "refreshed_at": NOW,
        "plan_json": json.dumps(
            {
                "date_columns": targeted_dates,
                "sheets": [
                    {
                        "sheet_name": "DATA_VITRINA",
                        "write_start_cell": "A1",
                        "header": [
                            "Показатель",
                            "row_id",
                            *targeted_dates,
                        ],
                        "rows": [
                            ["SKU 104", "SKU:104|orderSum", 100, 100, 100],
                            ["SKU 104", "SKU:104|orderCount", 2, 2, 2],
                            ["SKU 104", "SKU:104|ads_sum", 10, 10, 10],
                            ["SKU 105", "SKU:105|orderSum", 100, 100, 100],
                            ["SKU 105", "SKU:105|orderCount", 2, 2, 2],
                            ["SKU 105", "SKU:105|ads_sum", 10, 10, 10],
                        ],
                    }
                ],
            }
        ),
    }
    targeted_parameters = {
        day: CalculationParametersBlock(runtime=runtime).parameters_for_date(
            day
        )
        for day in targeted_dates
    }
    targeted_base_costs = {
        day: {
            104: {
                "our_wb_unit_cost_rub": 14,
                "stock_qty": 10,
            },
            105: {
                "our_wb_unit_cost_rub": 20,
                "stock_qty": 10,
            },
        }
        for day in targeted_dates
    }
    targeted_baseline = _transform_snapshot(
        snapshot=targeted_snapshot,
        costs=targeted_base_costs,
        warehouse_metrics={},
        warehouse_exact_dates=set(),
        warehouse_covered_nm_ids={},
        warehouse_version_ids={},
        parameters=targeted_parameters,
        source_fingerprint="sha256:targeted-baseline",
        cutover_business_date="2026-07-18",
    )
    targeted_costs = copy.deepcopy(targeted_base_costs)
    targeted_costs["2026-07-19"][104]["our_wb_unit_cost_rub"] = 18
    targeted_costs["2026-07-20"][104]["our_wb_unit_cost_rub"] = 18
    targeted_costs["2026-07-21"][104]["our_wb_unit_cost_rub"] = 18
    targeted_result = _transform_snapshot(
        snapshot={
            **targeted_snapshot,
            "plan_json": targeted_baseline["after_plan_json"],
        },
        costs=targeted_costs,
        warehouse_metrics={},
        warehouse_exact_dates=set(),
        warehouse_covered_nm_ids={},
        warehouse_version_ids={},
        parameters=targeted_parameters,
        source_fingerprint="sha256:targeted-change",
        cutover_business_date="2026-07-18",
        affected_nm_ids={104},
        earliest_business_date="2026-07-20",
        latest_business_date="2026-07-20",
    )
    targeted_payload = json.loads(targeted_result["after_plan_json"])
    targeted_rows = {
        row[1]: row for row in targeted_payload["sheets"][0]["rows"]
    }
    _assert(
        targeted_rows["SKU:104|our_wb_unit_cost_rub"][2:] == [14.0, 18.0, 14.0],
        "targeted economics changes the affected SKU only inside its exact date bounds",
    )
    _assert(
        targeted_rows["SKU:105|our_wb_unit_cost_rub"][2:] == [20.0, 20.0, 20.0],
        "targeted economics preserves unrelated SKU cells",
    )
    _assert(
        targeted_rows["TOTAL|total_our_wb_unit_cost_rub"][3] == 19.0,
        "targeted economics refreshes the direct dependent TOTAL cell",
    )
    _assert(
        targeted_result["non_target_before"]
        == targeted_result["non_target_after"],
        "targeted economics proves exact non-target snapshot invariance",
    )
    unrelated_drift_costs = copy.deepcopy(targeted_costs)
    unrelated_drift_costs["2026-07-20"][105][
        "our_wb_unit_cost_rub"
    ] = 21
    try:
        _transform_snapshot(
            snapshot={
                **targeted_snapshot,
                "plan_json": targeted_baseline["after_plan_json"],
            },
            costs=unrelated_drift_costs,
            warehouse_metrics={},
            warehouse_exact_dates=set(),
            warehouse_covered_nm_ids={},
            warehouse_version_ids={},
            parameters=targeted_parameters,
            source_fingerprint="sha256:targeted-unrelated-drift",
            cutover_business_date="2026-07-18",
            affected_nm_ids={104},
            earliest_business_date="2026-07-20",
            latest_business_date="2026-07-20",
        )
    except Exception as exc:
        _assert(
            "unrelated stale consumer cell" in str(exc),
            "targeted economics exposes a bounded unrelated consumer blocker",
        )
    else:
        raise AssertionError(
            "targeted economics must reject unrelated consumer drift"
        )
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
                    ["Archived coverage", "SKU:104|our_wb_cost_confirmed_share_pct", 1],
                    ["Archived proxy 2", "SKU:104|proxy_profit_2_rub", 12],
                    ["Archived paid equivalent", "SKU:104|own_total_paid_equivalent_qty", 10],
                    ["Legacy warehouse quantity", "SKU:104|own_capital_WB_qty", 999],
                    ["Legacy presentation A", 93.54754799999999, ""],
                    ["Legacy presentation B", 93.54754799999999, ""],
                ],
            }
        ],
    }
    pre_boundary_plan = {
        "date_columns": ["2026-06-30"],
        "sheets": [
            {
                "sheet_name": "DATA_VITRINA",
                "write_start_cell": "A1",
                "header": ["Показатель", "row_id", "2026-06-30"],
                "rows": [
                    ["SKU", "SKU:104|orderSum", 100],
                    ["SKU", "SKU:104|orderCount", 2],
                    ["SKU", "SKU:104|ads_sum", 10],
                    ["Legacy", "SKU:104|non_target", 555],
                    ["Archived proxy 2", "SKU:104|proxy_profit_2_rub", 11],
                ],
            }
        ],
        "metadata": {
            "preserved": True,
            "row_last_updated_at_by_row_id": {"SKU:104|proxy_profit_2_rub": NOW},
        },
    }
    untouched_pre_boundary_plan_json = json.dumps(
        {
            "date_columns": ["2026-06-29"],
            "sheets": [
                {
                    "sheet_name": "DATA_VITRINA",
                    "write_start_cell": "A1",
                    "header": ["Показатель", "row_id", "2026-06-29"],
                    "rows": [["Legacy", "SKU:104|non_target", 444]],
                }
            ],
        },
        ensure_ascii=False,
    )
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
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_ready_snapshots(
                   bundle_version,activated_at,as_of_date,snapshot_id,plan_version,refreshed_at,plan_json
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                "economics-smoke",
                NOW,
                "2026-06-30",
                "snap-economics-pre-boundary",
                "v1",
                NOW,
                json.dumps(pre_boundary_plan),
            ),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_ready_snapshots(
                   bundle_version,activated_at,as_of_date,snapshot_id,plan_version,refreshed_at,plan_json
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                "economics-smoke",
                NOW,
                "2026-06-29",
                "snap-economics-untouched-pre-boundary",
                "v1",
                NOW,
                untouched_pre_boundary_plan_json,
            ),
        )
        conn.commit()
    dry_run = build_functional_economics_backfill_plan(runtime)
    _assert(
        dry_run["changed_snapshot_count"] == 3,
        "functional economics backfill publishes canonical temporal rows on both sides of 01.07",
    )
    _assert(dry_run["archived_row_count"] == 4, "functional economics dry-run inventories archived rows")
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
    original_begin_mutation = WarehouseRecoveryRegistry.begin_mutation

    def journal_then_publish_new_daily_cost(
        registry: WarehouseRecoveryRegistry,
        operation_id: str,
        *,
        expected_source_digest: str,
    ) -> dict[str, object]:
        recovery = original_begin_mutation(
            registry,
            operation_id,
            expected_source_digest=expected_source_digest,
        )
        with sqlite3.connect(runtime.db_path) as drift_conn:
            drift_conn.execute(
                """UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost
                   SET wac_rub='15',capital_rub='150',fingerprint='sha256:daily-drift'
                   WHERE cutover_id=? AND as_of_date='2026-07-01' AND nm_id=104""",
                (FUNCTIONAL_CUTOVER_ID,),
            )
            drift_conn.commit()
        return recovery

    try:
        with patch.object(
            WarehouseRecoveryRegistry,
            "begin_mutation",
            autospec=True,
            side_effect=journal_then_publish_new_daily_cost,
        ):
            apply_functional_economics_backfill_plan(
                runtime,
                dry_run,
                confirm_fingerprint=dry_run["plan_fingerprint"],
                backup_dir=root / "economics-backups",
            )
    except Exception as exc:
        _assert(
            "warehouse/cost/settings inputs drifted" in str(exc),
            "publication during recovery journaling is rejected under the write lock",
        )
    else:
        raise AssertionError("concurrent warehouse source drift must block economics backfill")
    finally:
        with sqlite3.connect(runtime.db_path) as restore_conn:
            restore_conn.execute(
                """UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost
                   SET wac_rub='14',capital_rub='140',fingerprint='sha256:daily'
                   WHERE cutover_id=? AND as_of_date='2026-07-01' AND nm_id=104""",
                (FUNCTIONAL_CUTOVER_ID,),
            )
            restore_conn.commit()
    with sqlite3.connect(runtime.db_path) as conn:
        race_blocked_plan = conn.execute(
            """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
               WHERE bundle_version='economics-smoke' AND as_of_date='2026-07-01'"""
        ).fetchone()[0]
    _assert(
        race_blocked_plan == json.dumps(plan),
        "concurrent source drift leaves the ready snapshot unchanged",
    )
    # The heavy full-manifest validation must hold only a WAL read snapshot.
    # An interactive FF writer is allowed to commit immediately; the stale
    # background snapshot then aborts before any ready-snapshot mutation.
    from packages.application import warehouse_functional_economics_backfill as economics_module

    dry_run = build_functional_economics_backfill_plan(runtime)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ff_interactive_writer_probe(id TEXT PRIMARY KEY,created_at TEXT NOT NULL)"
        )
        conn.commit()
    validation_entered = threading.Event()
    release_validation = threading.Event()

    def pause_before_write_lock() -> None:
        validation_entered.set()
        if not release_validation.wait(timeout=5):
            raise AssertionError("concurrent economics validation probe timed out")

    background_errors: list[Exception] = []

    def apply_background() -> None:
        try:
            apply_functional_economics_backfill_plan(
                runtime,
                dry_run,
                confirm_fingerprint=dry_run["plan_fingerprint"],
                backup_dir=root / "economics-backups",
            )
        except Exception as exc:  # expected stale-snapshot abort
            background_errors.append(exc)

    with patch.object(
        economics_module,
        "_before_functional_economics_write_lock",
        side_effect=pause_before_write_lock,
    ):
        background = threading.Thread(target=apply_background, daemon=True)
        background.start()
        _assert(validation_entered.wait(timeout=5), "background economics entered deferred validation")
        interactive_started = time.monotonic()
        with sqlite3.connect(runtime.db_path, timeout=2) as interactive:
            interactive.execute(
                "INSERT INTO ff_interactive_writer_probe(id,created_at) VALUES(?,?)",
                ("ff-preview-status", NOW),
            )
            interactive.commit()
        interactive_ms = int((time.monotonic() - interactive_started) * 1000)
        release_validation.set()
        background.join(timeout=5)
    _assert(not background.is_alive(), "background economics aborts without a multi-minute writer wait")
    _assert(interactive_ms < 1_500, f"interactive writer starved for {interactive_ms}ms")
    _assert(
        background_errors
        and "changed during lock-free economics revalidation" in str(background_errors[0]),
        f"concurrent commit must fail the guarded background plan: {background_errors}",
    )
    with sqlite3.connect(runtime.db_path) as conn:
        concurrent_plan = conn.execute(
            """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
               WHERE bundle_version='economics-smoke' AND as_of_date='2026-07-01'"""
        ).fetchone()[0]
        probe_count = conn.execute(
            "SELECT COUNT(*) FROM ff_interactive_writer_probe WHERE id='ff-preview-status'"
        ).fetchone()[0]
    _assert(concurrent_plan == json.dumps(plan), "failed stale snapshot preserves last-good economics")
    _assert(probe_count == 1, "interactive FF writer commit must survive background abort")
    dry_run = build_functional_economics_backfill_plan(runtime)
    operation_business_date = str(dry_run["business_date"])
    next_business_date = (
        datetime.fromisoformat(operation_business_date) + timedelta(days=1)
    ).date().isoformat()
    with patch(
        "packages.application.warehouse_functional_economics_backfill.current_business_date_iso",
        side_effect=[operation_business_date, next_business_date],
    ):
        try:
            apply_functional_economics_backfill_plan(
                runtime,
                dry_run,
                confirm_fingerprint=dry_run["plan_fingerprint"],
                backup_dir=root / "economics-backups",
            )
        except Exception as exc:
            _assert(
                "business-date boundary" in str(exc),
                "economics apply fails closed when its pinned business date changes",
            )
        else:
            raise AssertionError("economics apply must not cross the canonical business-date boundary")
    with sqlite3.connect(runtime.db_path) as conn:
        midnight_blocked_plan = conn.execute(
            """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
               WHERE bundle_version='economics-smoke' AND as_of_date='2026-07-01'"""
        ).fetchone()[0]
    _assert(
        midnight_blocked_plan == json.dumps(plan),
        "business-date drift is rejected before any ready-snapshot mutation",
    )
    dry_run = build_functional_economics_backfill_plan(runtime)
    applied = apply_functional_economics_backfill_plan(
        runtime,
        dry_run,
        confirm_fingerprint=dry_run["plan_fingerprint"],
        backup_dir=root / "economics-backups",
        target_scoped_undo=True,
    )
    _assert(applied["database_written"] is True, "functional economics backfill applies atomically")
    _assert(
        applied["backup"]["full_database_copy"] is False
        and applied["backup"]["copy_bytes"] == 0,
        "targeted economics publication records exact before-images without a database copy",
    )
    rolled_back = rollback_target_scoped_functional_economics(
        runtime,
        manifest_digest=applied["rollback_manifest_digest"],
    )
    _assert(
        rolled_back["rolled_back"] is True,
        "targeted economics before-images restore exactly",
    )
    restored_plan = build_functional_economics_backfill_plan(runtime)
    _assert(
        restored_plan["plan_fingerprint"] == dry_run["plan_fingerprint"],
        "targeted economics rollback restores the reviewed source revision",
    )
    applied = apply_functional_economics_backfill_plan(
        runtime,
        restored_plan,
        confirm_fingerprint=restored_plan["plan_fingerprint"],
        backup_dir=root / "economics-backups",
    )
    repeated = build_functional_economics_backfill_plan(runtime)
    _assert(repeated["changed_snapshot_count"] == 0, "functional economics backfill is idempotent")
    with sqlite3.connect(runtime.db_path) as conn:
        coverage_row = conn.execute(
            """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
               WHERE bundle_version='economics-smoke' AND as_of_date='2026-07-01'"""
        ).fetchone()
        coverage_plan = json.loads(coverage_row[0])
        coverage_plan["metadata"].pop("warehouse_history_coverage", None)
        conn.execute(
            """UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?
               WHERE bundle_version='economics-smoke' AND as_of_date='2026-07-01'""",
            (json.dumps(coverage_plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")),),
        )
        conn.commit()
    coverage_repair = build_functional_economics_backfill_plan(runtime)
    _assert(
        coverage_repair["changed_snapshot_count"] == 1
        and coverage_repair["changed_cell_count"] == 0
        and coverage_repair["coverage_change_count"] == 1,
        "coverage-only semantic repair is persisted without manufacturing a cell change",
    )
    apply_functional_economics_backfill_plan(
        runtime,
        coverage_repair,
        confirm_fingerprint=coverage_repair["plan_fingerprint"],
        backup_dir=root / "economics-backups",
    )
    coverage_repeated = build_functional_economics_backfill_plan(runtime)
    _assert(
        coverage_repeated["changed_snapshot_count"] == 0
        and coverage_repeated["coverage_change_count"] == 0,
        "coverage-only repair is idempotent",
    )
    targeted_repeated = build_functional_economics_backfill_plan(
        runtime,
        affected_nm_ids=[104],
        earliest_business_date="2026-07-01",
    )
    _assert(
        targeted_repeated["changed_snapshot_count"] == 0
        and targeted_repeated["target_scope"]["affected_nm_ids"] == [104]
        and targeted_repeated["target_scope"]["earliest_business_date"]
        == "2026-07-01",
        "targeted economics plan carries the exact SKU/date closure",
    )
    targeted_noop = apply_functional_economics_backfill_plan(
        runtime,
        targeted_repeated,
        confirm_fingerprint=targeted_repeated["plan_fingerprint"],
        backup_dir=root / "economics-backups",
        target_scoped_undo=True,
    )
    _assert(
        targeted_noop["idempotent"] is True
        and targeted_noop["database_written"] is False,
        "targeted economics exact repeat is a no-op",
    )
    with sqlite3.connect(runtime.db_path) as conn:
        stored = json.loads(conn.execute(
            """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
               WHERE bundle_version='economics-smoke' AND as_of_date='2026-07-01'"""
        ).fetchone()[0])
        pre_boundary_stored = json.loads(conn.execute(
            """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
               WHERE bundle_version='economics-smoke' AND as_of_date='2026-06-30'"""
        ).fetchone()[0])
        untouched_pre_boundary_stored = json.loads(conn.execute(
            """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
               WHERE bundle_version='economics-smoke' AND as_of_date='2026-06-29'"""
        ).fetchone()[0])
    rows = {row[1]: row for row in stored["sheets"][0]["rows"]}
    profit = Decimal(str(rows["SKU:104|proxy_profit_3_rub"][2]))
    margin = Decimal(str(rows["SKU:104|proxy_margin_3_pct"][2]))
    _assert(profit == Decimal("15.48"), "Proxy 3 default settings formula")
    _assert(abs(margin - profit / Decimal("91")) < Decimal("0.0000005"), "Proxy 3 margin uses expected buyout revenue")
    _assert(rows["SKU:104|non_target"][2] == 777, "functional economics backfill preserves non-target cells")
    _assert(rows["SKU:104|own_capital_WB_qty"][2] == "", "unproved pre-cutover warehouse value is cleared, not zeroed")
    _assert(
        stored["metadata"]["warehouse_history_coverage"]["2026-07-01"]["status"] == "unavailable",
        "warehouse history gap carries an explicit source-level reason",
    )
    _assert(
        "до функционального cutover" in stored["metadata"]["warehouse_history_coverage"][
            "2026-07-01"
        ]["reason_ru"],
        "pre-cutover warehouse gap retains the immutable-opening explanation",
    )
    untouched_pre_rows = {
        row[1]: row for row in untouched_pre_boundary_stored["sheets"][0]["rows"]
    }
    _assert(
        Decimal(str(untouched_pre_rows["SKU:104|our_wb_unit_cost_rub"][2]))
        == Decimal("14"),
        "pre-boundary Vitrina cost projects exact same-nmID 01.07 value",
    )
    _assert(
        untouched_pre_rows["SKU:104|proxy_profit_3_rub"][2] == "",
        "missing pre-boundary Proxy operand remains blank instead of zero",
    )
    for archived_key in (
        "SKU:104|our_wb_cost_confirmed_share_pct",
        "SKU:104|proxy_profit_2_rub",
        "SKU:104|own_total_paid_equivalent_qty",
    ):
        _assert(archived_key not in rows, f"archived public row removed: {archived_key}")
    _assert(
        [row for row in stored["sheets"][0]["rows"] if row[0].startswith("Legacy presentation")]
        == [
            ["Legacy presentation A", 93.54754799999999, ""],
            ["Legacy presentation B", 93.54754799999999, ""],
        ],
        "functional economics ignores and preserves legacy non-key presentation rows",
    )
    pre_boundary_rows = {row[1]: row for row in pre_boundary_stored["sheets"][0]["rows"]}
    _assert("SKU:104|proxy_profit_2_rub" not in pre_boundary_rows, "pre-boundary archived row is removed")
    _assert(pre_boundary_rows["SKU:104|non_target"][2] == 555, "pre-boundary non-target row is preserved")
    _assert(
        Decimal(str(pre_boundary_rows["SKU:104|our_wb_unit_cost_rub"][2]))
        == Decimal("14"),
        "30.06 uses exact same-nmID 01.07 canonical cost",
    )
    _assert(
        Decimal(str(pre_boundary_rows["SKU:104|proxy_profit_3_rub"][2]))
        == Decimal("15.48"),
        "30.06 computes true Proxy 3 instead of Proxy 2 substitution",
    )
    _assert(
        "SKU:104|proxy_profit_2_rub"
        not in pre_boundary_stored["metadata"].get("row_last_updated_at_by_row_id", {}),
        "pre-boundary archived timestamp is removed",
    )
    for payload, label in (
        (pre_boundary_stored, "30.06"),
        (untouched_pre_boundary_stored, "29.06"),
    ):
        payload_rows = {
            row[1]: row for row in payload["sheets"][0]["rows"]
        }
        inserted_warehouse_rows = [
            row_id
            for row_id in payload_rows
            if "|" in row_id
            and row_id.split("|", 1)[1] in WAREHOUSE_TARGET_KEYS
        ]
        if inserted_warehouse_rows:
            raise AssertionError(
                f"pre-boundary {label} gained blank six-stage warehouse rows: "
                f"{inserted_warehouse_rows}"
            )
        timestamped_warehouse_rows = [
            row_id
            for row_id in payload.get("metadata", {})
            .get("row_last_updated_at_by_row_id", {})
            if "|" in row_id
            and row_id.split("|", 1)[1] in WAREHOUSE_TARGET_KEYS
        ]
        if timestamped_warehouse_rows:
            raise AssertionError(
                f"pre-boundary {label} stamped warehouse rows: "
                f"{timestamped_warehouse_rows}"
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
            """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
               WHERE bundle_version='economics-smoke' AND as_of_date='2026-07-01'"""
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
