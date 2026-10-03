"""Local, opt-in dated compiler using the existing native evaluator.

No HTTP/timer wiring. A caller must pin native source inputs in a short window
read context and supply one frozen clock and an explicit history boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
import hashlib
import json

from packages.application.sheet_vitrina_v1_web_vitrina import (
    SheetVitrinaV1WebVitrinaBlock, _build_period_snapshot,
    _load_default_visible_snapshot, default_business_as_of_date,
)
from packages.application.management_inventory_history import read_management_inventory_history
from packages.application.web_vitrina_compact_table import (
    CELL_FIELDS, CELL_DEFAULTS, compact_adapter_payload,
)
from packages.application.web_vitrina_gravity_table_adapter import build_web_vitrina_gravity_table_adapter
from packages.application.web_vitrina_view_model import build_web_vitrina_view_model
from packages.application.web_vitrina_window_read_context import active_window_read_context

CONTRACT = "web_vitrina_dated_cells_canonical_inventory_context_v1"


class DatedCompilerUnsupported(ValueError):
    pass


def assert_numeric_compatibility(canonical: dict, natural: dict, day: str) -> None:
    """Global catalog context must never silently replace an existing fact."""
    for row_id, cell in natural.items():
        if row_id not in canonical or canonical[row_id][:2] != cell[:2]:
            raise DatedCompilerUnsupported(
                f"dated_compiler_numeric_context_mismatch:{day}:{row_id}")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def dates_between(start: str, end: str, *, limit: int = 366) -> list[str]:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    count = (b - a).days + 1
    if not 1 <= count <= limit:
        raise ValueError("history date limit exceeded")
    return [(a + timedelta(days=i)).isoformat() for i in range(count)]


def unpack_table(table: dict) -> tuple[dict, dict[str, dict[str, list]]]:
    """Split immutable structural catalog from all sixteen dated fields."""
    columns = table["columns"]
    static_columns = [c for c in columns if not c["id"].startswith("date:")]
    rows, dated = {}, {}
    for row in table["rows"]:
        structural = {k: v for k, v in row.items() if k not in {"values", "search_text"}}
        structural["values"] = {}
        dated[row["row_id"]] = {}
        for packed in row["values"]:
            field = columns[packed[0]]["id"]
            cell = packed[1:] + list(CELL_DEFAULTS[len(packed)-1:])
            if len(cell) != len(CELL_FIELDS):
                raise ValueError("invalid dated cell width")
            if field.startswith("date:"):
                dated[row["row_id"]][field[5:]] = cell
            elif field != "row_order":
                structural["values"][field] = cell
        rows[row["row_id"]] = structural
    return {"contract": CONTRACT, "columns": static_columns, "rows": rows}, dated


@dataclass
class NativeDatedCompiler:
    runtime: Any
    now: datetime
    date_from: str
    date_to: str
    existing_catalog: dict | None = None
    dependency_epoch: str = ""

    def __post_init__(self) -> None:
        if self.now.tzinfo is None or active_window_read_context() is None:
            raise ValueError("dated compiler requires a pinned read context and timezone-aware clock")
        self.dates = dates_between(self.date_from, self.date_to)
        default = _load_default_visible_snapshot(runtime=self.runtime,
            default_as_of_date=default_business_as_of_date(self.now))
        plan, bindings = _build_period_snapshot(runtime=self.runtime,
            date_from=self.date_from, date_to=self.date_to, default_visible_snapshot=default)
        history = read_management_inventory_history(self.runtime.db_path,
            runtime_dir=self.runtime.runtime_dir, plan=plan, current_date="")
        identities = {}
        for day in reversed(sorted(history.get("dates", {}))):
            for scope_key, scope in history["dates"][day].get("scopes", {}).items():
                identity = scope.get("wb", {}).get("provenance", {}).get("identity", {})
                if identity and scope_key not in identities:
                    identities[scope_key] = identity
        self.context = {
            "template_rows": [list(row[:2]) for row in plan.sheets[0].rows],
            "facilities": list(history.get("facilities") or []),
            "history_scope_identities": identities,
            "inventory_catalog_present": bool(history.get("dates")),
            "history_scope_keys": sorted({key for dated in history.get("dates", {}).values()
                for key, scope in dated.get("scopes", {}).items()
                if scope.get("typed_quantity") or scope.get("diagnostic")}),
            "legacy_wb_present": any(
                str(row[1]).endswith(("|stock_total", "|total_stock_total"))
                and any(v not in (None, "") for v in row[2:]) for row in plan.sheets[0].rows),
        }
        from dataclasses import asdict
        state = self.runtime.load_current_state()
        self.context["static_config"] = {
            "config": [asdict(item) for item in state.config_v2],
            "metrics": [asdict(item) for item in state.metrics_v2],
            "dependency_epoch": self.dependency_epoch,
        }
        self.context_epoch = digest(self.context)
        self.availability = {b.requested_date: not b.missing for b in bindings}
        self.block = SheetVitrinaV1WebVitrinaBlock(runtime=self.runtime,
            now_factory=lambda: self.now, dated_cell_context=self.context)
        self.natural_block = SheetVitrinaV1WebVitrinaBlock(runtime=self.runtime,
            now_factory=lambda: self.now)
        self.cached_tables = {}
        if self.existing_catalog and self.existing_catalog["context_epoch"] == self.context_epoch:
            self.catalog = self.existing_catalog
        else:
            # Canonical raw row/facility union materializes the catalog in ONE day.
            first = next(day for day in reversed(self.dates) if self.availability[day])
            table = self._table(self.block, first, first)
            self.cached_tables[first] = table
            self.catalog, _ = unpack_table(table)
            self.catalog["order"] = [row["row_id"] for row in table["rows"]]
            self.catalog["context_epoch"] = self.context_epoch

    @staticmethod
    def _table(block, start: str, end: str, *, members: set[str] | None = None) -> dict:
        contract = block.build(page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina", date_from=start, date_to=end)
        if members is not None:
            from dataclasses import replace
            contract = replace(contract, rows=[row for row in contract.rows if row.row_id in members])
        return compact_adapter_payload(build_web_vitrina_gravity_table_adapter(
            build_web_vitrina_view_model(contract)))

    def compile(self, day: str) -> dict:
        if active_window_read_context() is None or day not in self.availability:
            raise ValueError("compile outside pinned declared history")
        table = self.cached_tables.pop(day, None)
        if table is None:
            table = self._table(self.block, day, day)
        _, cells = unpack_table(table)
        try:
            natural = self._table(self.natural_block, day, day)
            membership = [row["row_id"] for row in natural["rows"]]
            _, natural_cells = unpack_table(natural)
            assert_numeric_compatibility(
                {rid: by_date[day] for rid, by_date in cells.items()},
                {rid: by_date[day] for rid, by_date in natural_cells.items()}, day)
        except ValueError as exc:
            if "no materialized row template" not in str(exc):
                raise
            membership = []
        return {"contract": CONTRACT, "date": day,
            "context_epoch": self.context_epoch,
            "accepted_ready_available": self.availability[day],
            "members": membership,
            "cells": {row_id: by_date[day] for row_id, by_date in cells.items()}}
