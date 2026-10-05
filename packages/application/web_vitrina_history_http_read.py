"""Lossless presentation of finished daystore pages; no business runtime dependency."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import time
import json

from packages.business_time import current_business_date_iso
from packages.application.web_vitrina_compact_table import CELL_DEFAULTS, CELL_FIELDS
from packages.application.web_vitrina_history_compiler import digest
from packages.application.web_vitrina_history_store import HistoryUnavailable, _read, _json
from packages.application.web_vitrina_view_model import _FORMATTER_LIBRARY
from packages.application.web_vitrina_page_composition import (
    WEB_VITRINA_PAGE_STATE_NAMESPACE, _build_metric_options, _count_metric_rows,
)


def _history_metric_label(row):
    cell = row.get("values", {}).get("metric_label", [])
    metric = row.get("values", {}).get("metric_key", [])
    key = metric[0] if metric else ""
    if key not in {"inventory_fbs_total_qty_v1", "total_inventory_fbs_total_qty_v1"}:
        return cell
    label = (cell[1] or cell[0]) if len(cell) >= 2 else cell[0] if cell else ""
    if str(label or "").strip() not in {"", key}:
        return cell
    # Retained compatibility aggregate rows predate the public inventory order.
    # Repair only their missing/raw caption; every dated cell stays untouched.
    return ["Остаток FBS: всего", "Остаток FBS: всего", *cell[2:]]


def read_history_page(store, *, date_from, date_to, scope="summary", edition_id=None,
                      offset=0, limit=64, group_id=None):
    today = current_business_date_iso()
    page = store.read(date_from=date_from, date_to=date_to, scope=scope,
        edition_id=edition_id, offset=offset, limit=limit, group_id=group_id,
        deadline_monotonic=time.monotonic() + 10, business_today=today)
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
            elif key == "metric_label":
                cell = _history_metric_label(row)
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
    saved_at = datetime.fromtimestamp((store.root / "editions" /
        (page["edition_id"] + ".json")).stat().st_mtime, timezone.utc).isoformat()
    marker = {k: page[k] for k in ("edition_id", "scope", "offset", "next_offset", "total_rows", "availability", "scope_totals", "sku_group_totals", "unmaterialized_dates")}
    group_labels = {}
    for row in catalog["rows"].values():
        catalog_group_id = row.get("group_id", "")
        if catalog_group_id in page["sku_group_totals"] and catalog_group_id not in group_labels:
            cell = row.get("values", {}).get("group", [])
            label = (cell[1] or cell[0]) if len(cell) >= 2 else ""
            if label:
                group_labels[catalog_group_id] = str(label)
    marker["sku_group_labels"] = group_labels
    registry_groups = catalog.get("reporting_groups")
    # Old editions remain readable: derive their available identities, without
    # relabelling any stored row or claiming native group totals exist.
    if registry_groups is None:
        registry_groups = [{"group_key": key.removeprefix("group:"), "label": label,
            "display_order": i, "is_active": True} for i, (key, label) in enumerate(group_labels.items())]
    marker["reporting_groups"] = [{**g, "group_id": "group:" + g["group_key"],
        "sku_rows": page["sku_group_totals"].get("group:" + g["group_key"], 0),
        "total_available": any(r["row_kind"] == "group" and r.get("group_id") == "group:" + g["group_key"]
            for r in catalog["rows"].values())} for g in registry_groups]
    marker["archive_status"] = page.get("archive_status", {})
    marker.update(date_from=date_from, date_to=date_to, group_id=group_id or "",
                  current_preliminary=today in page["dates"], saved_at=saved_at)
    dates = sorted(edition["days"])
    presentation = deepcopy(catalog["presentation"])
    # Gravity adapter catalogs carry renderer IDs, but not formatter rules.
    # Resolve presentation rules without evaluating or changing stored cells.
    formatters = {item["formatter_id"]: item for item in presentation.get("formatters", [])}
    for renderer in presentation.get("renderers", []):
        formatter_id = renderer.get("formatter_id")
        if formatter_id not in formatters and formatter_id in _FORMATTER_LIBRARY:
            formatters[formatter_id] = asdict(_FORMATTER_LIBRARY[formatter_id])
    presentation["formatters"] = list(formatters.values())
    # Full structural metric metadata keeps normal TOTAL/SKU logical IDs stable
    # before any lazy SKU cells are requested.
    metric_rows = []
    for row in catalog["rows"].values():
        values = {}
        for key in ("metric_key", "metric_label", "section"):
            cell = _history_metric_label(row) if key == "metric_label" else row.get("values", {}).get(key, [])
            values[key] = {"value": cell[0] if cell else None,
                           "display_text": cell[1] if len(cell) > 1 else ""}
        metric_rows.append({"row_kind": "total" if row["row_kind"] == "group" else row["row_kind"], "section_id": row.get("section_id", ""),
                            "values": values})
    metric_options = _build_metric_options(_count_metric_rows(metric_rows), sections=[])
    payload = {
        "composition_name": "web_vitrina_page_composition", "response_schema_version": 2,
        "history_snapshot": marker,
        "meta": {"current_state": "ready", "state_message": "Готовая история; качество указано в ячейках.",
                 "today_current_date": today, "state_namespace": WEB_VITRINA_PAGE_STATE_NAMESPACE,
                 "browser_state_persistence": "local", "history_snapshot": True},
        "summary_cards": [{"card_id": "period", "detail": date_from + " — " + date_to},
            {"card_id": "page_refresh", "value": saved_at, "updated_at": saved_at}],
        "filter_surface": {"controls": [{"control_id": "metric", "options": metric_options}],
                           "sort_options": [], "default_sort_value": ""},
        "historical_access": {"options": [{"value": d, "label": d} for d in reversed(dates)],
            "available_date_min": dates[0], "available_date_max": dates[-1],
            "current_mode": "period", "selected_date_from": date_from, "selected_date_to": date_to,
            "default_as_of_date": today, "default_date_from": date_from, "default_date_to": date_to,
            "status_text": (("За текущий день ещё нет сохранённых данных. Текущий день предварительный."
                if date_from == date_to else
                "За текущий день ещё нет сохранённых данных. Прошлые дни показаны из готовой истории.")
                if page["unmaterialized_dates"] else
                "Готовая история. Качество и полнота указаны в ячейках; текущий день предварительный."),
            "preset_options": [], "supported_query_mode": "history_mode_explicit_date_window"},
        "table_surface": {**presentation, "columns": columns, "rows": rows,
            "groupings": groupings, "total_row_count": page["total_rows"], "returned_row_count": len(rows),
            "table_data_state": "included", "value_encoding": {
                "format": "indexed_cells_v2", "fields": list(CELL_FIELDS), "defaults": list(CELL_DEFAULTS)}},
    }
    # Includes the presentation envelope, which the storage reader's guard excludes.
    if len(_json(payload)) > store.max_reply_bytes:
        raise HistoryUnavailable("history_reply_limit")
    return payload
