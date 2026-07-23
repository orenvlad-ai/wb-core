#!/usr/bin/env python3
"""Canonical Our WB Cost policy and capitalization regression smoke."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock  # noqa: E402
from packages.application.canonical_wb_cost_resolver import (  # noqa: E402
    load_canonical_wb_cost_lookup,
)


def main() -> None:
    with TemporaryDirectory(prefix="wb-finance-canonical-cost-") as tmp:
        block = WbFinanceWeeklyBlock(
            Path(tmp),
            seller_id="seller-1",
            now_factory=lambda: datetime(2026, 7, 27, tzinfo=timezone.utc),
        )
        block.ensure_schema()
        _seed_sources(block.db_path)
        _assert_all_history_policy(block)
        _assert_missing_and_forbidden_costs_block(block)
        _assert_standalone_remuneration_adjustment_once(block)
        _assert_capitalization_requires_lineage(block)
        _assert_capitalization_cap_is_global_across_weeks(block)
        _assert_supply_layer_change_invalidates_profit(block)
        _assert_canonical_change_invalidates_projection(block)
        _assert_legacy_runner_is_revoked(block)
    print(
        "wb_finance_weekly_cost_cutover: ok -> all-history canonical policy, "
        "mixed boundary, signed returns, blockers, lineage-capped capitalization, invalidation"
    )


def _assert_all_history_policy(block: WbFinanceWeeklyBlock) -> None:
    january = block.ingest_week(
        date(2026, 1, 5),
        date(2026, 1, 11),
        [_row(1, "2026-01-06", quantity=2)],
    )
    if january["aggregate"]["cogs"] != "200.0000":
        raise AssertionError(f"January must project the 01.07 canonical value: {january}")
    quality = _week(block, "2026-01-05")["cost_coverage"]["quality"]
    if quality["projected_units"] != 2 or quality["fallback_average_created"]:
        raise AssertionError(f"historical projection quality mismatch: {quality}")

    mixed = block.ingest_week(
        date(2026, 6, 29),
        date(2026, 7, 5),
        [
            _row(2, "2026-06-30", quantity=2),
            _row(3, "2026-07-01", quantity=3),
            _row(4, "2026-07-02", quantity=1, returned=True),
        ],
    )
    # 2 * 100 (projected) + 3 * 100 (exact 01.07) - 1 * 120 (return date).
    if mixed["aggregate"]["cogs"] != "380.0000":
        raise AssertionError(f"30.06/01.07 mixed-week COGS mismatch: {mixed}")
    mixed_quality = _week(block, "2026-06-29")["cost_coverage"]["quality"]
    if mixed_quality["source_units"] != {
        "projected_from_2026_07_01": 2,
        "canonical_exact_date": 4,
    }:
        raise AssertionError(f"mixed temporal source split mismatch: {mixed_quality}")
    if mixed["aggregate"]["commission_control_reconciliation_rub"] != "0.0000":
        raise AssertionError(f"agent/acquiring split does not reconcile: {mixed}")
    if (
        mixed["aggregate"]["agent_remuneration"] != "200.0000"
        or mixed["aggregate"]["acquiring"] != "40.0000"
        or mixed["aggregate"]["combined_commission_control"] != "240.0000"
        or mixed["aggregate"]["wb_remuneration_adjustment"] != "7.0000"
    ):
        raise AssertionError(f"agent/acquiring signed split mismatch: {mixed}")

    with sqlite3.connect(block.db_path) as conn:
        # Historical evidence may remain, but this deliberately wrong fixed value
        # must have no influence on the live Finance calculation.
        conn.execute(
            """INSERT INTO wb_finance_retro_cost_map VALUES(
               'seller-1','101','9999','2026-07-01','obsolete','{}','sha256:old',
               'sha256:old-calculation','obsolete','obsolete','obsolete','2026-07-01T00:00:00Z')"""
        )
        conn.commit()
    recalculated = block.recalculate_week(date(2026, 1, 5), date(2026, 1, 11))
    if recalculated["cogs"] != "200.0000":
        raise AssertionError("obsolete retro map became a second business cost source")


def _assert_missing_and_forbidden_costs_block(block: WbFinanceWeeklyBlock) -> None:
    missing = block.ingest_week(
        date(2026, 2, 2),
        date(2026, 2, 8),
        [_row(20, "2026-02-03", nm_id=102)],
    )
    problem = _week(block, "2026-02-02")["cost_coverage"]["problem_skus"][0]
    if (
        missing["aggregate"]["cogs"] is not None
        or problem["nm_id"] != "102"
        or problem["reason"] != "canonical_cost_exact_date_missing"
    ):
        raise AssertionError(f"missing canonical 01.07 cost did not block: {missing}")

    forbidden = block.ingest_week(
        date(2026, 2, 9),
        date(2026, 2, 15),
        [_row(21, "2026-02-10", nm_id=103)],
    )
    forbidden_problem = _week(block, "2026-02-09")["cost_coverage"]["problem_skus"][0]
    if forbidden_problem["reason"] != "canonical_cost_forbidden_fallback_quality":
        raise AssertionError(f"fallback_average must be rejected: {forbidden}")
    with sqlite3.connect(block.db_path) as conn:
        conn.row_factory = sqlite3.Row
        blocked_lookup = load_canonical_wb_cost_lookup(
            conn,
            as_of_date=date(2026, 7, 1),
        )[103]
    if (
        blocked_lookup["stock_qty"] != 10.0
        or blocked_lookup["our_wb_unit_cost_rub"] is not None
        or blocked_lookup["source_reason"]
        != "canonical_cost_forbidden_fallback_quality"
    ):
        raise AssertionError(
            f"blocked positive-quantity cost lost quantity/blocker evidence: {blocked_lookup}"
        )


def _assert_standalone_remuneration_adjustment_once(
    block: WbFinanceWeeklyBlock,
) -> None:
    result = block.ingest_week(
        date(2026, 3, 2),
        date(2026, 3, 8),
        [
            _remuneration_adjustment_row(22, "2026-03-03", "12"),
            _remuneration_adjustment_row(23, "2026-03-03", "-5"),
        ],
    )["aggregate"]
    if (
        result["wb_remuneration_adjustment"] != "7.0000"
        or result["positive_adjustments"] != "12.0000"
        or result["corrections"] != "5.0000"
        or result["profit_period_expenses"] != "5.0000"
        or result["profit_after_cogs"] != "7.0000"
        or result["agent_remuneration"] != "0.0000"
        or result["acquiring"] != "0.0000"
    ):
        raise AssertionError(f"standalone WB remuneration adjustment was not applied once: {result}")


def _assert_capitalization_requires_lineage(block: WbFinanceWeeklyBlock) -> None:
    rows = [
        _row(30, "2026-07-14"),
        _expense_row(31, "2026-07-14", supply_id="77", acceptance="30"),
        _expense_row(32, "2026-07-14", supply_id="77", deduction="20", transit=True),
        _expense_row(33, "2026-07-14", supply_id="88", acceptance="10"),
    ]
    result = block.ingest_week(date(2026, 7, 13), date(2026, 7, 19), rows)
    metrics = result["aggregate"]
    reconciliation = metrics["capitalization_reconciliation"]
    if (
        metrics["capitalized_acceptance"] != "25.0000"
        or metrics["capitalized_transit_logistics"] != "15.0000"
        or metrics["profit_period_expenses"] != "80.0000"
    ):
        raise AssertionError(f"lineage-capped addback mismatch: {metrics}")
    lineage = reconciliation["lineage"]
    if not any(
        item["reason"] == "matched_but_global_cap_exhausted_or_capped"
        for item in lineage
    ):
        raise AssertionError(f"capped lineage not disclosed: {reconciliation}")
    if not any(item["reason"] == "canonical_supply_cost_layer_missing" for item in lineage):
        raise AssertionError(f"unmatched acceptance was silently added back: {reconciliation}")


def _assert_capitalization_cap_is_global_across_weeks(
    block: WbFinanceWeeklyBlock,
) -> None:
    later = block.ingest_week(
        date(2026, 7, 20),
        date(2026, 7, 26),
        [
            _expense_row(34, "2026-07-21", supply_id="77", acceptance="10"),
            _expense_row(35, "2026-07-21", supply_id="77", deduction="10", transit=True),
        ],
    )["aggregate"]
    if (
        later["capitalized_acceptance"] != "0.0000"
        or later["capitalized_transit_logistics"] != "0.0000"
        or later["profit_period_expenses"] != "20.0000"
    ):
        raise AssertionError(f"canonical cost-layer cap was reused in a later week: {later}")
    reasons = {
        item["reason"] for item in later["capitalization_reconciliation"]["lineage"]
    }
    if reasons != {"matched_but_global_cap_exhausted_or_capped"}:
        raise AssertionError(f"global cap exhaustion was not disclosed: {later}")

    first = _week(block, "2026-07-13")["metrics"]
    if (
        first["capitalized_acceptance"] != "25.0000"
        or first["capitalized_transit_logistics"] != "15.0000"
    ):
        raise AssertionError("later Finance rows changed the chronological first allocation")


def _assert_supply_layer_change_invalidates_profit(block: WbFinanceWeeklyBlock) -> None:
    with sqlite3.connect(block.db_path) as conn:
        conn.execute(
            """UPDATE sheet_vitrina_v1_wb_supply_cost_layers
               SET wb_acceptance_amount_total='35',transit_amount_total='20',
                   inputs_hash='sha256:layer-77-v2',version=2
               WHERE wb_supply_cost_layer_id='layer-77' AND is_current=1"""
        )
        conn.commit()
    plan = block.plan_stale_cost_weeks()
    stale_starts = {item["week_start"] for item in plan["weeks"]}
    if not {"2026-07-13", "2026-07-20"}.issubset(stale_starts):
        raise AssertionError(f"supply-layer profit drift was not detected: {plan}")
    block.apply_stale_cost_weeks(expected_fingerprint=plan["fingerprint"])
    first = _week(block, "2026-07-13")["metrics"]
    later = _week(block, "2026-07-20")["metrics"]
    if (
        first["capitalized_acceptance"] != "30.0000"
        or first["capitalized_transit_logistics"] != "20.0000"
        or later["capitalized_acceptance"] != "5.0000"
        or later["capitalized_transit_logistics"] != "0.0000"
    ):
        raise AssertionError("supply-layer correction did not rebuild global allocation")


def _assert_canonical_change_invalidates_projection(block: WbFinanceWeeklyBlock) -> None:
    before = _week(block, "2026-01-05")["cost_coverage"]["cost_state_hash"]
    with sqlite3.connect(block.db_path) as conn:
        conn.execute(
            """UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost
               SET wac_rub='105',capital_rub='1050',fingerprint='sha256:cost-101-jul1-v2'
               WHERE as_of_date='2026-07-01' AND nm_id=101"""
        )
        conn.commit()
    plan = block.plan_stale_cost_weeks()
    stale_starts = {item["week_start"] for item in plan["weeks"]}
    if "2026-01-05" not in stale_starts:
        raise AssertionError("all-history stale detection missed a January projection")
    block.apply_stale_cost_weeks(expected_fingerprint=plan["fingerprint"])
    recalculated = _week(block, "2026-01-05")["metrics"]
    after = _week(block, "2026-01-05")["cost_coverage"]["cost_state_hash"]
    if recalculated["cogs"] != "210.0000" or before == after:
        raise AssertionError("canonical correction did not rebuild historical projection")


def _assert_legacy_runner_is_revoked(block: WbFinanceWeeklyBlock) -> None:
    for action in (
        lambda: block.plan_business_approved_backfill(),
        lambda: block.apply_business_approved_backfill(
            expected_fingerprint="sha256:any",
            approval_reference="must-not-run",
        ),
    ):
        try:
            action()
        except ValueError as exc:
            if "revoked" not in str(exc):
                raise
        else:
            raise AssertionError("obsolete retro-cost workflow remained executable")


def _seed_sources(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                is_active INTEGER,nm_id INTEGER,vendor_code TEXT,barcode TEXT,
                barcodes_json TEXT,product_type TEXT
            );
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES
                (1,101,'VC101','BAR101','["BAR101"]','other'),
                (1,102,'VC102','BAR102','["BAR102"]','other'),
                (1,103,'VC103','BAR103','["BAR103"]','other');
            CREATE TABLE sheet_vitrina_v1_warehouse_functional_cutovers(
                cutover_id TEXT PRIMARY KEY,cutover_at TEXT,status TEXT,
                plan_fingerprint TEXT,source_watermarks_json TEXT,
                absorbed_supply_revisions_json TEXT,backup_json TEXT,
                created_at TEXT,updated_at TEXT
            );
            INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers VALUES(
                'warehouse_functional_cutover_v1','2026-07-01T00:00:00Z','posted',
                'sha256:cutover','{}','[]','{}','2026-07-01T00:00:00Z','2026-07-01T00:00:00Z'
            );
            CREATE TABLE sheet_vitrina_v1_warehouse_wb_daily_cost(
                cutover_id TEXT,as_of_date TEXT,nm_id INTEGER,quantity TEXT,wac_rub TEXT,
                capital_rub TEXT,quality TEXT,provenance_json TEXT,fingerprint TEXT,
                created_at TEXT,PRIMARY KEY(cutover_id,as_of_date,nm_id)
            );
            INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost VALUES
                ('warehouse_functional_cutover_v1','2026-07-01',101,'10','100','1000','certified','{}','sha256:cost-101-jul1','2026-07-01T00:00:00Z'),
                ('warehouse_functional_cutover_v1','2026-07-02',101,'10','120','1200','certified','{}','sha256:cost-101-jul2','2026-07-02T00:00:00Z'),
                ('warehouse_functional_cutover_v1','2026-07-14',101,'10','140','1400','certified','{}','sha256:cost-101-jul14','2026-07-14T00:00:00Z'),
                ('warehouse_functional_cutover_v1','2026-07-21',101,'10','140','1400','certified','{}','sha256:cost-101-jul21','2026-07-21T00:00:00Z'),
                ('warehouse_functional_cutover_v1','2026-07-01',103,'10','50','500','fallback_average','{}','sha256:forbidden','2026-07-01T00:00:00Z');
            CREATE TABLE sheet_vitrina_v1_wb_supply_cost_layers(
                wb_supply_cost_layer_id TEXT,wb_supply_id TEXT,nm_id TEXT,accepted_qty TEXT,
                transit_cost_status TEXT,transit_amount_total TEXT,
                wb_acceptance_amount_total TEXT,source_status TEXT,
                component_status_json TEXT,inputs_hash TEXT,version INTEGER,is_current INTEGER
            );
            INSERT INTO sheet_vitrina_v1_wb_supply_cost_layers VALUES(
                'layer-77','77','101','10','confirmed','15','25','confirmed','{}',
                'sha256:layer-77',1,1
            );
            CREATE TABLE wb_finance_retro_cost_map(
                seller_id TEXT,nm_id TEXT,unit_cost_rub TEXT,source_date TEXT,
                source_table TEXT,source_row_json TEXT,source_row_sha256 TEXT,
                source_calculation_fingerprint TEXT,selection_method TEXT,
                formula_version TEXT,status TEXT,created_at TEXT
            );
            """
        )
        conn.commit()


def _row(
    rrd_id: int,
    operation_date: str,
    *,
    nm_id: int = 101,
    quantity: int = 1,
    returned: bool = False,
) -> dict:
    revenue = 200 * quantity
    for_pay = 140 * quantity
    acquiring = 10 * quantity
    return {
        "dateFrom": operation_date,
        "dateTo": operation_date,
        "reportId": rrd_id,
        "reportType": 1,
        "rrdId": rrd_id,
        "nmId": nm_id,
        "vendorCode": f"VC{nm_id}",
        "sku": f"BAR{nm_id}",
        "rrDate": operation_date,
        "saleDt": operation_date,
        "docTypeName": "Возврат" if returned else "Продажа",
        "sellerOperName": "Возврат" if returned else "Продажа",
        "quantity": quantity,
        "retailPriceWithDisc": str(revenue),
        "forPay": str(for_pay),
        "acquiringFee": str(acquiring),
        "additionalPayment": "7",
    }


def _expense_row(
    rrd_id: int,
    operation_date: str,
    *,
    supply_id: str,
    acceptance: str = "0",
    deduction: str = "0",
    transit: bool = False,
) -> dict:
    return {
        **_row(rrd_id, operation_date),
        "docTypeName": "",
        "sellerOperName": "Удержание",
        "quantity": 0,
        "retailPriceWithDisc": "0",
        "forPay": "0",
        "acquiringFee": "0",
        "additionalPayment": "0",
        "giId": supply_id,
        "paidAcceptance": acceptance,
        "deduction": deduction,
        "bonusTypeName": "Услуги доставки транзитных поставок" if transit else "",
    }


def _remuneration_adjustment_row(
    rrd_id: int,
    operation_date: str,
    amount: str,
) -> dict:
    return {
        **_expense_row(rrd_id, operation_date, supply_id=""),
        "sellerOperName": "Корректировка Вознаграждения Вайлдберриз",
        "additionalPayment": amount,
    }


def _week(block: WbFinanceWeeklyBlock, week_start: str) -> dict:
    return next(item for item in block.build_payload()["weeks"] if item["week_start"] == week_start)


if __name__ == "__main__":
    main()
