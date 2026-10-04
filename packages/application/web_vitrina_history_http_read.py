"""Lossless presentation of finished daystore pages; no business runtime dependency."""
from copy import deepcopy
from datetime import datetime, timezone
import time
import json

from packages.business_time import current_business_date_iso
from packages.application.web_vitrina_compact_table import CELL_DEFAULTS, CELL_FIELDS
from packages.application.web_vitrina_history_compiler import digest
from packages.application.web_vitrina_history_store import HistoryUnavailable, _read, _json


def read_history_page(store, *, date_from, date_to, scope="summary", edition_id=None,
                      offset=0, limit=64, group_id=None):
    page = store.read(date_from=date_from, date_to=date_to, scope=scope,
        edition_id=edition_id, offset=offset, limit=limit, group_id=group_id,
        deadline_monotonic=time.monotonic() + 10)
    edition = store.edition(page["edition_id"])
    catalog = _read(store.root / "catalogs" / (edition["catalog"] + ".json"))
    if digest(catalog) != edition["catalog"]:
        raise HistoryUnavailable("history_catalog_corrupt")
    if any(key not in catalog for key in ("presentation", "date_column_template", "row_order_cell_template")):
        raise HistoryUnavailable("history_presentation_unavailable")
    columns = deepcopy(catalog["columns"])
    for day in page["dates"]:
        column = json.loads(json.dumps(catalog["date_column_template"], ensure_ascii=False).replace("{date}", day))
        column.update(id="date:" + day, accessor_key="date:" + day, header=day)
        columns.append(column)
    rows = []
    for row in page["rows"]:
        values = []
        for index, column in enumerate(columns):
            key = column["id"]
            if key == "row_order":
                cell = [row["row_order"], str(row["row_order"]), *catalog["row_order_cell_template"][2:]]
            elif key.startswith("date:"):
                cell = row["cells"][key[5:]]
            else:
                cell = row["values"][key]
            values.append([index, *cell])
        rows.append({k: v for k, v in row.items() if k not in {"values", "cells", "row_order"}}
                    | {"values": values})
    groups = {}
    for row in rows:
        key = (row.get("section_id", ""), row.get("group_id", ""))
        groups.setdefault(key, []).append(row["row_id"])
    groupings = [{"grouping_id": section + "|" + group, "section_id": section,
                  "group_id": group, "row_ids": ids} for (section, group), ids in groups.items()]
    today = current_business_date_iso()
    saved_at = datetime.fromtimestamp((store.root / "editions" /
        (page["edition_id"] + ".json")).stat().st_mtime, timezone.utc).isoformat()
    marker = {k: page[k] for k in ("edition_id", "scope", "offset", "next_offset", "total_rows", "availability", "scope_totals", "sku_group_totals")}
    marker.update(date_from=date_from, date_to=date_to, group_id=group_id or "",
                  current_preliminary=today in page["dates"], saved_at=saved_at)
    dates = sorted(edition["days"])
    payload = {
        "composition_name": "web_vitrina_page_composition", "response_schema_version": 2,
        "history_snapshot": marker,
        "meta": {"current_state": "ready", "state_message": "Готовая история; качество указано в ячейках.",
                 "today_current_date": today, "state_namespace": "web_vitrina_history",
                 "browser_state_persistence": "local", "history_snapshot": True},
        "summary_cards": [{"card_id": "period", "detail": date_from + " — " + date_to},
            {"card_id": "page_refresh", "value": saved_at, "updated_at": saved_at}],
        "historical_access": {"options": [{"value": d, "label": d} for d in dates],
            "current_mode": "period", "selected_date_from": date_from, "selected_date_to": date_to,
            "default_as_of_date": today, "default_date_from": date_from, "default_date_to": date_to,
            "status_text": "Готовая история. Качество и полнота указаны в ячейках; текущий день предварительный.",
            "preset_options": [], "supported_query_mode": "history_mode_explicit_date_window"},
        "table_surface": {**catalog["presentation"], "columns": columns, "rows": rows,
            "groupings": groupings, "total_row_count": page["total_rows"], "returned_row_count": len(rows),
            "table_data_state": "included", "value_encoding": {
                "format": "indexed_cells_v2", "fields": list(CELL_FIELDS), "defaults": list(CELL_DEFAULTS)}},
    }
    # Includes the presentation envelope, which the storage reader's guard excludes.
    if len(_json(payload)) > store.max_reply_bytes:
        raise HistoryUnavailable("history_reply_limit")
    return payload
