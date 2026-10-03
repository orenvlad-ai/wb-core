"""Explicit, local pilot of a finished Web Vitrina table snapshot.

The writer accepts the *finished* page composition from the existing business
evaluator.  The reader only opens this separate store in SQLite read-only mode;
it never consults the operational database or runs business calculations.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from packages.application.web_vitrina_compact_table import CELL_DEFAULTS, CELL_FIELDS, TABLE_WIRE_FORMAT


SCHEMA_VERSION = 1
MAX_PERIOD_DAYS = 31  # Explicit 14-day/month pilot, not a general history limit.
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024

VALUE_ENCODING = {"format": TABLE_WIRE_FORMAT, "fields": list(CELL_FIELDS),
                  "defaults": list(CELL_DEFAULTS)}


class SnapshotPilotError(ValueError):
    """A pilot artifact or generation cannot be served safely."""


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _period(start: str, end: str) -> None:
    try:
        count = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    except ValueError as exc:
        raise SnapshotPilotError("invalid_period") from exc
    if not 1 <= count <= MAX_PERIOD_DAYS:
        raise SnapshotPilotError("pilot_period_out_of_scope")


def provision_store(path: Path) -> None:
    """Manual, explicit local setup; never called from a request path."""
    if not path.parent.is_dir():
        raise SnapshotPilotError("store_directory_missing")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS web_vitrina_pilot_generations(
                generation_id TEXT PRIMARY KEY,
                date_from TEXT NOT NULL,
                date_to TEXT NOT NULL,
                assembled_at TEXT NOT NULL,
                summary_json BLOB NOT NULL,
                sku_json BLOB NOT NULL,
                summary_digest TEXT NOT NULL,
                sku_digest TEXT NOT NULL,
                summary_rows INTEGER NOT NULL CHECK(summary_rows>0),
                sku_rows INTEGER NOT NULL CHECK(sku_rows>=0)
            );
            CREATE TRIGGER IF NOT EXISTS web_vitrina_pilot_generations_no_update
            BEFORE UPDATE ON web_vitrina_pilot_generations
            BEGIN SELECT RAISE(ABORT,'pilot generation immutable'); END;
            CREATE TRIGGER IF NOT EXISTS web_vitrina_pilot_generations_no_delete
            BEFORE DELETE ON web_vitrina_pilot_generations
            BEGIN SELECT RAISE(ABORT,'pilot generation retained'); END;
            CREATE TABLE IF NOT EXISTS web_vitrina_pilot_pointers(
                date_from TEXT NOT NULL,
                date_to TEXT NOT NULL,
                generation_id TEXT NOT NULL REFERENCES web_vitrina_pilot_generations(generation_id),
                published_at TEXT NOT NULL,
                PRIMARY KEY(date_from,date_to)
            );
        """)


def _partition(composition: Mapping[str, Any], *, date_from: str, date_to: str) -> tuple[bytes, bytes, int, int]:
    _period(date_from, date_to)
    version = composition.get("response_schema_version")
    if composition.get("composition_name") != "web_vitrina_page_composition" or version not in {1, 2}:
        raise SnapshotPilotError("finished_composition_required")
    meta = composition.get("meta") or {}
    surface = composition.get("table_surface") or {}
    if meta.get("current_state") != "ready" or (surface.get("state_surface") or {}).get("current_state") != "ready":
        raise SnapshotPilotError("finished_ready_table_required")
    if surface.get("table_data_state") != "included":
        raise SnapshotPilotError("full_cells_required")
    compact_input = version == 2
    if (compact_input and surface.get("value_encoding") != VALUE_ENCODING) or (
        not compact_input and surface.get("value_encoding")
    ):
        raise SnapshotPilotError("cell_encoding_invalid")
    expected_dates = [(date.fromisoformat(date_from).toordinal() + offset) for offset in
                      range((date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1)]
    days = [date.fromordinal(ordinal).isoformat() for ordinal in expected_dates]
    if list(meta.get("visible_date_columns") or []) != days:
        raise SnapshotPilotError("date_coverage_mismatch")
    columns = [str(item.get("id") or "") for item in surface.get("columns") or []]
    if not columns or any(not column for column in columns) or len(set(columns)) != len(columns):
        raise SnapshotPilotError("column_catalog_invalid")
    if [item for item in columns if item.startswith("date:")] != ["date:" + day for day in days]:
        raise SnapshotPilotError("date_columns_mismatch")
    rows = surface.get("rows")
    if not isinstance(rows, list) or not rows:
        raise SnapshotPilotError("table_rows_missing")
    if any(type(surface.get(key)) is not int or surface[key] != len(rows)
           for key in ("total_row_count", "returned_row_count")):
        raise SnapshotPilotError("table_row_count_mismatch")
    summary_rows: list[dict[str, Any]] = []
    sku_rows: list[dict[str, Any]] = []
    ids: set[str] = set()
    for row in rows:
        row_id = str(row.get("row_id") or "")
        kind = str(row.get("row_kind") or "").lower()
        values = row.get("values")
        if not row_id or row_id in ids or kind not in {"total", "group", "sku"}:
            raise SnapshotPilotError("row_catalog_invalid")
        ids.add(row_id)
        if compact_input:
            if not isinstance(values, list) or len(values) != len(columns):
                raise SnapshotPilotError("cell_fields_incomplete")
            indexes: set[int] = set()
            for entry in values:
                if not isinstance(entry, list) or not 3 <= len(entry) <= len(CELL_FIELDS) + 1:
                    raise SnapshotPilotError("cell_fields_incomplete")
                index = entry[0]
                if type(index) is not int or index < 0 or index >= len(columns) or index in indexes:
                    raise SnapshotPilotError("cell_fields_incomplete")
                indexes.add(index)
            packed = values
        else:
            if not isinstance(values, dict) or set(values) != set(columns):
                raise SnapshotPilotError("cell_fields_incomplete")
            packed = []
            for index, column in enumerate(columns):
                cell = values[column]
                if not isinstance(cell, dict) or set(cell) != set(CELL_FIELDS):
                    raise SnapshotPilotError("cell_fields_incomplete")
                fields = [cell[name] for name in CELL_FIELDS]
                while len(fields) > 2 and fields[-1] == CELL_DEFAULTS[len(fields) - 1]:
                    fields.pop()
                packed.append([index, *fields])
        (sku_rows if kind == "sku" else summary_rows).append({**row, "values": packed})
    if not any(str(row.get("row_kind") or "").lower() == "total" for row in summary_rows):
        raise SnapshotPilotError("summary_rows_missing")
    groupings = surface.get("groupings")
    if not isinstance(groupings, list) or not groupings:
        raise SnapshotPilotError("grouping_catalog_invalid")
    grouping_ids: set[str] = set()
    grouped_rows: set[str] = set()
    for group in groupings:
        if not isinstance(group, dict) or not str(group.get("grouping_id") or "") \
                or not isinstance(group.get("row_ids"), list):
            raise SnapshotPilotError("grouping_catalog_invalid")
        grouping_id = str(group["grouping_id"])
        row_ids = group["row_ids"]
        if grouping_id in grouping_ids or any(
            not isinstance(row_id, str) or row_id not in ids or row_id in grouped_rows
            for row_id in row_ids
        ):
            raise SnapshotPilotError("grouping_catalog_invalid")
        grouping_ids.add(grouping_id)
        grouped_rows.update(row_ids)
    if grouped_rows != ids:
        raise SnapshotPilotError("grouping_catalog_incomplete")
    # All other page metadata is kept from the same finished evaluator result.
    summary = dict(composition)
    summary["response_schema_version"] = 2
    summary["meta"] = {**meta, "snapshot_pilot": True}
    summary_ids = {str(row["row_id"]) for row in summary_rows}
    summary_groupings = [
        {**group, "row_ids": [row_id for row_id in group.get("row_ids", []) if row_id in summary_ids]}
        for group in groupings
    ]
    summary["table_surface"] = {**surface, "rows": summary_rows,
                                "groupings": summary_groupings,
                                "total_row_count": len(summary_rows),
                                "returned_row_count": len(summary_rows),
                                "value_encoding": VALUE_ENCODING, "table_data_state": "included"}
    sku = {"snapshot_pilot_schema_version": SCHEMA_VERSION, "rows": sku_rows,
           "catalog_order": [str(row["row_id"]) for row in rows], "groupings": groupings,
           "value_encoding": VALUE_ENCODING}
    summary_bytes, sku_bytes = _encoded(summary), _encoded(sku)
    if len(summary_bytes) + len(sku_bytes) > MAX_ARTIFACT_BYTES:
        raise SnapshotPilotError("artifact_resource_limit")
    return summary_bytes, sku_bytes, len(summary_rows), len(sku_rows)


def publish_finished(path: Path, composition: Mapping[str, Any], *, date_from: str,
                     date_to: str, fail_before_pointer: bool = False) -> str:
    """Manual publish; a failed build/validation/transaction retains last good."""
    summary, sku, summary_count, sku_count = _partition(composition, date_from=date_from, date_to=date_to)
    generation = "pilot_" + _digest(summary + b"\n" + sku)
    assembled_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if not path.is_file():
        raise SnapshotPilotError("store_not_provisioned")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        required = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"web_vitrina_pilot_generations", "web_vitrina_pilot_pointers"} <= required:
            raise SnapshotPilotError("store_schema_missing")
        conn.execute("""INSERT OR IGNORE INTO web_vitrina_pilot_generations
            (generation_id,date_from,date_to,assembled_at,summary_json,sku_json,
             summary_digest,sku_digest,summary_rows,sku_rows) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (generation, date_from, date_to, assembled_at, summary, sku, _digest(summary),
             _digest(sku), summary_count, sku_count))
        existing = conn.execute("SELECT summary_digest,sku_digest FROM web_vitrina_pilot_generations "
                                "WHERE generation_id=?", (generation,)).fetchone()
        if existing != (_digest(summary), _digest(sku)):
            raise SnapshotPilotError("generation_digest_collision")
        if fail_before_pointer:
            raise SnapshotPilotError("injected_publish_failure")
        conn.execute("""INSERT INTO web_vitrina_pilot_pointers(date_from,date_to,generation_id,published_at)
            VALUES(?,?,?,?) ON CONFLICT(date_from,date_to) DO UPDATE SET
            generation_id=excluded.generation_id,published_at=excluded.published_at""",
            (date_from, date_to, generation, assembled_at))
    return generation


def read_finished(path: Path, *, date_from: str, date_to: str, part: str,
                  generation_id: str = "") -> dict[str, Any]:
    """Strict read-only, short transaction on the explicitly configured store."""
    _period(date_from, date_to)
    if part not in {"summary", "sku"} or (part == "sku" and not generation_id):
        raise SnapshotPilotError("pilot_read_parameters_invalid")
    if not path.is_file():
        raise SnapshotPilotError("store_unavailable")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        pointer = connection.execute("SELECT generation_id FROM web_vitrina_pilot_pointers "
                                     "WHERE date_from=? AND date_to=?", (date_from, date_to)).fetchone()
        if pointer is None:
            raise SnapshotPilotError("finished_snapshot_unavailable")
        selected = generation_id or str(pointer[0])
        column = "summary_json" if part == "summary" else "sku_json"
        digest_column = "summary_digest" if part == "summary" else "sku_digest"
        row = connection.execute(f"SELECT {column},{digest_column},assembled_at FROM web_vitrina_pilot_generations "
            "WHERE generation_id=? AND date_from=? AND date_to=?",
            (selected, date_from, date_to)).fetchone()
        if row is None:
            raise SnapshotPilotError("generation_unavailable")
        content = bytes(row[0])
        if _digest(content) != str(row[1]):
            raise SnapshotPilotError("generation_corrupt")
        payload = json.loads(content)
        payload["snapshot_pilot"] = {"schema_version": SCHEMA_VERSION,
            "generation_id": selected, "part": part, "date_from": date_from, "date_to": date_to,
            "latest_generation_id": str(pointer[0]), "assembled_at": str(row[2])}
        return payload
    finally:
        connection.close()
