"""Opt-in, lossless table-cell wire representation for page composition v2.

The table remains the sole authoritative business read in stage 1. This wire
encoding only removes repeated object keys; it does not change row scope,
search text, values, quality, provenance, or missing/null semantics.
"""

from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any

from packages.contracts.web_vitrina_gravity_table_adapter import WebVitrinaGravityTableAdapterV1

TABLE_WIRE_FORMAT = "indexed_cells_v2"
CELL_FIELDS = (
    "value", "display_text", "cell_kind", "formatter_id", "renderer_id",
    "presentation_state", "presentation_tone", "presentation_reason",
    "quality_state", "quality_label", "quality_reason", "completeness_state",
    "missing_sku_count", "quantity_semantic_kind", "quantity_source_observed_at",
    "inventory_finalization_digest",
)
CELL_DEFAULTS: tuple[Any, ...] = (
    None, "", "", None, "", "", "", "", "", "", "", "", None, "", "", "",
)


def compact_adapter_payload(adapter: WebVitrinaGravityTableAdapterV1) -> dict[str, Any]:
    """Serialize adapter rows directly to wire cells, never making dense dict cells."""
    columns = [item.id for item in adapter.columns]
    index_by_column = {column_id: index for index, column_id in enumerate(columns)}
    payload = {
        field.name: asdict(getattr(adapter, field.name))
        if hasattr(getattr(adapter, field.name), "__dataclass_fields__")
        else [asdict(item) for item in getattr(adapter, field.name)]
        if isinstance(getattr(adapter, field.name), list)
        else getattr(adapter, field.name)
        for field in fields(adapter)
        if field.name != "rows"
    }
    rows = []
    for row in adapter.rows:
        packed = []
        for column_id, cell in row.values.items():
            if column_id not in index_by_column:
                raise ValueError(f"compact table has unknown column {column_id!r}")
            cell_fields = [getattr(cell, field) for field in CELL_FIELDS]
            while len(cell_fields) > 2 and cell_fields[-1] == CELL_DEFAULTS[len(cell_fields) - 1]:
                cell_fields.pop()
            packed.append([index_by_column[column_id], *cell_fields])
        rows.append({
            "row_id": row.row_id,
            "row_kind": row.row_kind,
            "section_id": row.section_id,
            "group_id": row.group_id,
            "depth": row.depth,
            "parent_id": row.parent_id,
            "search_text": row.search_text,
            "filter_tokens": row.filter_tokens,
            "values": packed,
        })
    payload["rows"] = rows
    return payload
