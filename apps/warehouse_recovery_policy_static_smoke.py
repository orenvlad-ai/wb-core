#!/usr/bin/env python3
"""Static fail-closed reachability gate for warehouse recovery entrypoints."""

from __future__ import annotations

import ast
from pathlib import Path
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


FORBIDDEN_CALLS = frozenset(
    {
        "backup",
        "backup_database",
        "coherent_backup_size_bytes",
        "_sqlite_backup",
        "_create_sqlite_backup",
        "create_verified_sqlite_backup",
        "restore_verified_supplier_backup",
    }
)
FORBIDDEN_TEXT = (
    "PRAGMA integrity_check",
    "PRAGMA quick_check",
)

# These are the current executable bounded and T2 production entrypoints from
# migration/123. T3 is intentionally absent: the central allowlist test owns it.
ENTRYPOINTS = (
    ("apps/warehouse_cost_queue_replay.py", "", "apply_plan"),
    (
        "packages/application/warehouse_targeted_replay.py",
        "WarehouseTargetedSupplierReplay",
        "apply",
    ),
    (
        "packages/application/supplier_shipment_factual_correction.py",
        "SupplierShipmentFactualCorrectionBlock",
        "apply",
    ),
    (
        "packages/application/warehouse_functional_economics_backfill.py",
        "",
        "apply_functional_economics_backfill_plan",
    ),
    (
        "packages/application/warehouse_functional.py",
        "WarehouseFunctionalBlock",
        "apply_plan",
    ),
    (
        "packages/application/warehouse_functional.py",
        "",
        "enqueue_warehouse_targeted_recalculation",
    ),
    (
        "packages/application/calculation_parameters.py",
        "CalculationParametersBlock",
        "create_version",
    ),
    (
        "packages/application/calculation_parameters.py",
        "CalculationParametersBlock",
        "preflight_fresh_economics_backup_capacity",
    ),
    (
        "packages/application/ff_stock_ledger.py",
        "FfStockLedgerBlock",
        "confirm_manual_operation",
    ),
    (
        "packages/application/ff_stock_ledger.py",
        "FfStockLedgerBlock",
        "record_supplier_acceptance",
    ),
    (
        "packages/application/ff_stock_ledger.py",
        "FfStockLedgerBlock",
        "record_wb_supply_debits",
    ),
    (
        "packages/application/supplier_shipments.py",
        "SupplierShipmentsBlock",
        "create_shipment",
    ),
    (
        "packages/application/supplier_shipments.py",
        "SupplierShipmentsBlock",
        "update_shipment",
    ),
    (
        "packages/application/supplier_shipments.py",
        "SupplierShipmentsBlock",
        "update_expenses_complete",
    ),
    (
        "packages/application/wb_supplies.py",
        "WbSuppliesBlock",
        "sync_supplies",
    ),
    (
        "packages/application/warehouse_archival_estimate.py",
        "",
        "apply_archival_estimate_plan",
    ),
    (
        "packages/application/warehouse_archival_estimate.py",
        "",
        "rollback_archival_estimate",
    ),
    (
        "packages/application/warehouse_supplier_cost_state_replay.py",
        "",
        "apply_supplier_cost_state_replay_plan",
    ),
    (
        "packages/application/warehouse_supplier_cost_state_replay.py",
        "",
        "rollback_supplier_cost_state_replay",
    ),
    (
        "apps/canonical_cost_engine_vitrina_publication.py",
        "",
        "apply_publication",
    ),
    ("apps/canonical_cost_engine_backfill.py", "", "run"),
    ("apps/ff_stock_targeted_reconciliation.py", "", "main"),
    (
        "packages/application/warehouse_stocks.py",
        "WarehouseStocksBlock",
        "apply_opening_plan",
    ),
    (
        "packages/application/warehouse_stocks.py",
        "WarehouseStocksBlock",
        "rollback_opening_cutover",
    ),
    ("apps/wb_finance_weekly.py", "", "main"),
    (
        "apps/sheet_vitrina_v1_proxy_margin_3_historical_backfill.py",
        "",
        "run_backfill",
    ),
    ("apps/supplier_shipment_publication_chain.py", "", "apply_chain"),
    ("apps/supplier_26gn390_recovery.py", "", "apply_plan"),
    ("apps/supplier_cny_payment_10_recovery.py", "", "apply_plan"),
    ("apps/ff_reservations_transit_cost_recovery.py", "", "apply_plan"),
)


def main() -> int:
    failures: list[str] = []
    for relative, class_name, function_name in ENTRYPOINTS:
        analyzer = ReachabilityAnalyzer(ROOT / relative)
        calls, text = analyzer.reachable(class_name, function_name)
        forbidden = sorted(FORBIDDEN_CALLS & calls)
        forbidden_text = sorted(
            marker for marker in FORBIDDEN_TEXT if marker.lower() in text.lower()
        )
        if forbidden or forbidden_text:
            failures.append(
                f"{relative}:{class_name + '.' if class_name else ''}{function_name} "
                f"reaches calls={forbidden} scans={forbidden_text}"
            )

    central_source = (
        ROOT / "packages/application/warehouse_recovery_policy.py"
    ).read_text(encoding="utf-8")
    if central_source.count("runtime.backup_database(") != 1:
        failures.append(
            "the central policy must contain exactly one T3 full-backup call"
        )
    for relative in (
        "apps/warehouse_cost_queue_replay.py",
        "apps/ff_stock_targeted_reconciliation.py",
        "apps/warehouse_functional_runner.py",
        "apps/canonical_cost_engine_vitrina_publication.py",
        "packages/application/warehouse_stocks.py",
        "packages/application/warehouse_archival_estimate.py",
        "packages/application/warehouse_supplier_cost_state_replay.py",
        "packages/application/warehouse_targeted_replay.py",
        "packages/application/warehouse_functional_economics_backfill.py",
        "packages/application/calculation_parameters.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        if ".backup_database(" in source:
            failures.append(f"{relative} contains a direct full-backup call")

    if failures:
        raise AssertionError("\n".join(failures))
    print(
        "warehouse_recovery_policy_static_smoke: ok "
        f"({len(ENTRYPOINTS)} entrypoints, T3 central-only)"
    )
    return 0


class ReachabilityAnalyzer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.functions: dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[("", node.name)] = node
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.functions[(node.name, item.name)] = item

    def reachable(self, class_name: str, function_name: str) -> tuple[set[str], str]:
        pending = [(class_name, function_name)]
        visited: set[tuple[str, str]] = set()
        calls: set[str] = set()
        text_parts: list[str] = []
        while pending:
            identity = pending.pop()
            if identity in visited:
                continue
            visited.add(identity)
            function = self.functions.get(identity)
            if function is None:
                raise AssertionError(
                    f"static recovery entrypoint is missing: "
                    f"{self.path.relative_to(ROOT)}:{identity}"
                )
            nodes, _ = _reachable_suite(function.body)
            for node in nodes:
                if isinstance(node, ast.Call):
                    call_name = _call_name(node.func)
                    if call_name:
                        calls.add(call_name)
                        local = (
                            (identity[0], call_name)
                            if isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id in {"self", "cls"}
                            else ("", call_name)
                        )
                        if local in self.functions and local not in visited:
                            pending.append(local)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    text_parts.append(node.value)
        return calls, "\n".join(text_parts)


def _reachable_suite(statements: Iterable[ast.stmt]) -> tuple[list[ast.AST], bool]:
    nodes: list[ast.AST] = []
    for statement in statements:
        statement_nodes, terminates = _reachable_statement(statement)
        nodes.extend(statement_nodes)
        if terminates:
            return nodes, True
    return nodes, False


def _reachable_statement(statement: ast.stmt) -> tuple[list[ast.AST], bool]:
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [], False
    if isinstance(statement, (ast.Return, ast.Raise)):
        return list(ast.walk(statement)), True
    if isinstance(statement, ast.If):
        nodes = _expression_nodes(statement.test)
        body, body_terminates = _reachable_suite(statement.body)
        otherwise, otherwise_terminates = _reachable_suite(statement.orelse)
        return (
            [*nodes, *body, *otherwise],
            bool(statement.orelse) and body_terminates and otherwise_terminates,
        )
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        nodes = [
            child
            for item in statement.items
            for child in _expression_nodes(item.context_expr)
        ]
        body, terminates = _reachable_suite(statement.body)
        return [*nodes, *body], terminates
    if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
        nodes = _expression_nodes(statement)
        body, _ = _reachable_suite(statement.body)
        otherwise, _ = _reachable_suite(statement.orelse)
        return [*nodes, *body, *otherwise], False
    if isinstance(statement, (ast.Try, ast.TryStar)):
        body, body_terminates = _reachable_suite(statement.body)
        handlers: list[ast.AST] = []
        handler_termination: list[bool] = []
        for handler in statement.handlers:
            handler_nodes, handler_terminates = _reachable_suite(handler.body)
            handlers.extend(handler_nodes)
            handler_termination.append(handler_terminates)
        otherwise, otherwise_terminates = _reachable_suite(statement.orelse)
        final, final_terminates = _reachable_suite(statement.finalbody)
        terminates = final_terminates or (
            body_terminates
            and all(handler_termination)
            and (not statement.orelse or otherwise_terminates)
        )
        return [*body, *handlers, *otherwise, *final], terminates
    return list(ast.walk(statement)), False


def _expression_nodes(node: ast.AST) -> list[ast.AST]:
    excluded = (ast.stmt, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return [child for child in ast.walk(node) if not isinstance(child, excluded)]


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
