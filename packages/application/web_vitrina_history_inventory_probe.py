"""Inventory-only compatibility probe on explicitly extracted source records.

This does not certify a partial DB/book or run the production page evaluator.
The caller supplies native lifecycle outputs for untyped closed captures.
"""
from dataclasses import asdict
import json
import sqlite3

from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope, SheetVitrinaWriteTarget, SheetVitrinaV1TemporalSlot,
)
from packages.application.sheet_vitrina_v1_inventory_history import (
    COMPONENTS_TABLE, _materialize_captures, ensure_inventory_history_schema,
)
from packages.application.management_inventory_history import legacy_wb_operands, verify_capture_content
from packages.application.sheet_vitrina_v1_inventory_planning import (
    extend_rows_with_inventory_planning, restore_finalized_inventory_history,
)
from packages.application.sheet_vitrina_v1_web_vitrina import _normalize_rows
from packages.application.web_vitrina_view_model import _build_cell
from packages.application.web_vitrina_gravity_table_adapter import _renderer_id
from packages.application.web_vitrina_compact_table import CELL_FIELDS
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item
from packages.contracts.web_vitrina_view_model import WebVitrinaViewModelColumn


def extracted_inventory_history(records: dict, quality: dict) -> dict:
    """Only own in-memory fixture is provisioned; original records are untouched."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_inventory_history_schema(conn)
        selected = {}
        for day, record in records.items():
            capture = record["capture"]
            if not capture.get("finalization_id"):
                raise ValueError("probe requires a closed selected finalization")
            selected[day] = capture
            for component in record["components"]:
                columns = list(component)
                conn.execute("INSERT INTO " + COMPONENTS_TABLE + " (" + ",".join(columns)
                             + ") VALUES(" + ",".join("?" for _ in columns) + ")",
                             tuple(component.values()))
            if not verify_capture_content(conn, capture):
                raise ValueError("extracted capture content mismatch")
        def resolver(day, _nm_ids):
            result = quality[day]
            if result.get("contract") != "fbs_lifecycle_quality_coverage_v1" or result["as_of_date"] != day:
                raise ValueError("missing exact native lifecycle proof")
            return result
        return _materialize_captures(conn, selected, lifecycle_quality_resolver=resolver)
    finally:
        conn.close()


def probe_inventory_day(inputs: dict, *, day: str, history: dict, context: dict) -> dict:
    """Native narrow versus canonical inventory normalization, all 16 fields."""
    payload = inputs["dates"][day]["selected_ready_plan"]
    # A single inventory-only DATA slice is intentionally not a full ready plan.
    plan = SheetVitrinaV1Envelope(
        payload["plan_version"], payload["snapshot_id"], payload["as_of_date"],
        payload["date_columns"], [SheetVitrinaV1TemporalSlot(**v) for v in payload["temporal_slots"]],
        payload["source_temporal_policies"], [SheetVitrinaWriteTarget(**v) for v in payload["sheets"]],
        payload.get("metadata", {}))
    configs = [ConfigV2Item(int(r["nm_id"]), bool(r["enabled"]), r["display_name"],
                           r["group_name"], r["display_order"]) for r in inputs["enabled_config"]]
    metrics = [MetricV2Item(r["metric_key"], bool(r["enabled"]), r["scope"], r["label_ru"],
                           r["calc_type"], r["calc_ref"], bool(r["show_in_data"]),
                           r["format_name"], r["display_order"], r["section_name"])
               for r in inputs["metrics"]]
    index = plan.date_columns.index(day) + 2
    raw = [[r[0], r[1], r[index]] for r in plan.sheets[0].rows]
    presentation = json.loads(json.dumps(plan.metadata.get("server_cell_presentation", {})))
    for row_id, dated in legacy_wb_operands(plan).items():
        if day in dated:
            presentation.setdefault(row_id, {}).setdefault(day, {})["legacy_wb_operand"] = dated[day]
    rows = _normalize_rows(raw, date_columns=[day], config_by_nm_id={c.nm_id: c for c in configs},
                           metrics_by_key={m.metric_key: m for m in metrics},
                           row_updated_at_by_id=plan.metadata.get("row_last_updated_at_by_row_id", {}),
                           server_cell_presentation=presentation)
    dated_history = {**history, "dates": {day: history["dates"][day]}}
    natural = extend_rows_with_inventory_planning(rows, planning={}, history=dated_history,
                                                  date_columns=[day], enabled_config=configs)
    canonical = extend_rows_with_inventory_planning(rows, planning={},
        history={**dated_history, "facilities": context["facilities"]},
        date_columns=[day], enabled_config=configs, force_catalog=True,
        legacy_wb_history_present=context["legacy_wb_present"],
        catalog_scope_keys=context["history_scope_keys"],
        catalog_scope_identities=context["history_scope_identities"])
    column = WebVitrinaViewModelColumn("date:" + day, day, "date", "number", "end", "",
                                      None, True, False, None, None)
    def serialize(selected):
        selected = restore_finalized_inventory_history(selected, history=dated_history, current_date="")
        result = {}
        for row in selected:
            cell = asdict(_build_cell(column, asdict(row)))
            cell["renderer_id"] = _renderer_id(cell_kind=cell["cell_kind"], formatter_id=cell["formatter_id"])
            result[row.row_id] = {"cell": [cell[f] for f in CELL_FIELDS],
                                  "scope_label": row.scope_label, "group": row.group}
        return result
    return {"natural": serialize(natural), "canonical": serialize(canonical)}
