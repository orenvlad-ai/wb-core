"""Read-only, same-SKU last-known cost estimates for bounded Vitrina history.

Only immutable, closed shared-cost days in the active accounting book are
eligible. The projection adds dated management-estimate cells to a ready plan;
it never projects quantities, capital, or TOTAL weighted unit cost.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any

from packages.application.fbs_accounting_runtime import load as load_book
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.shared_sku_cost import _validate as validate_shared_day
from packages.application.web_vitrina_management_history import SOURCE, data_sheet, digest

COST = "our_wb_unit_cost_rub"


def _business_date(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("last_known_cost_date_invalid")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("last_known_cost_date_invalid")
    return value


def _positive_finite(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def capture_source(runtime_dir: str | Path, *, dates: list[str], bundle_version: str) -> dict[str, Any]:
    """Capture same-SKU costs as of each requested date from closed book days.

    The canonical loader verifies the full revision and immutable blob digests
    in the isolated accounting store. The enclosing book version is not
    included in source identity, so an unrelated current-day revision cannot
    change the digest of unchanged historical choices.
    """
    requested = sorted(set(_business_date(day) for day in dates))
    if not requested or len(requested) > 31 or not isinstance(bundle_version, str) or not bundle_version:
        raise ValueError("last_known_cost_scope_invalid")
    book, _current_book_version = load_book(runtime_dir)
    if book is None or book.get('active') is not True:
        raise ValueError('last_known_cost_accounting_book_inactive')
    max_day = requested[-1]
    error = book.get('publication_error') or {}
    if not isinstance(error, dict):
        raise ValueError('last_known_cost_publication_error_invalid')
    invalid_from = error.get('invalid_from_date')
    if invalid_from is not None:
        _business_date(invalid_from)
    effective_date = _business_date(book['effective_date'])
    shared_days: dict[str, tuple[str, dict[str, Any]]] = {}
    for raw_day, period in sorted(book['shared_days'].items()):
        day = _business_date(raw_day)
        if day > max_day or day < effective_date or (invalid_from and day >= invalid_from):
            continue
        if period.get('business_date') != day or period.get('status') != 'closed':
            continue
        try:
            validate_shared_day(period)
        except Exception as exc:
            raise ValueError('last_known_cost_shared_day_invalid') from exc
        shared_days[day] = (fingerprint(period), period)

    costs_by_date: dict[str, dict[str, Any]] = {}
    choices: dict[str, Any] = {}
    for target_day in requested:
        available_days = [day for day in shared_days if day <= target_day]
        sku_ids = sorted({str(nm) for day in available_days
                          for nm in shared_days[day][1].get("rows", {})}, key=int)
        chosen: dict[str, Any] = {}
        for nm_id in sku_ids:
            for source_day in reversed(available_days):
                blob_digest, period = shared_days[source_day]
                row = period.get("rows", {}).get(nm_id)
                if not isinstance(row, dict) or row.get("status") != "resolved":
                    continue
                # Treat malformed per-row evidence as unusable; never chain an
                # estimate from this module or substitute another SKU.
                expected_row_digest = fingerprint({k: v for k, v in row.items() if k != "source_digest"})
                if row.get("source_digest") != expected_row_digest:
                    continue
                unit_cost = _positive_finite(row.get("unit_cost_rub"))
                if unit_cost is None:
                    continue
                candidate = {
                    "management_value": format(unit_cost, "f"),
                    "source_as_of_date": source_day,
                    "source_day_version_id": period.get("version_id"),
                    "source_digest": row["source_digest"],
                    "source_book_blob_digest": blob_digest,
                    "source_row_status": "resolved",
                }
                chosen[f"SKU:{nm_id}|{COST}"] = candidate
                break
        costs_by_date[target_day] = chosen
        choices[target_day] = chosen

    source_identity = {"source_kind": "last_known_as_of", "dates": requested,
                       "bundle_version": bundle_version, "choices": choices}
    return {**source_identity,
            "snapshot_id": "last-known-cost:" + digest(source_identity),
            "costs_by_date": costs_by_date}


def project_costs(plan: dict[str, Any], *, dates: list[str], source: dict[str, Any],
                  operation_id: str) -> dict[str, Any]:
    """Fill only blank SKU cost cells with their captured same-SKU source."""
    requested = sorted(set(_business_date(day) for day in dates))
    if (not requested or source.get("source_kind") != "last_known_as_of"
            or source.get("dates") != requested or not isinstance(operation_id, str) or not operation_id):
        raise ValueError("last_known_cost_source_scope_invalid")
    working = json.loads(json.dumps(plan, ensure_ascii=False))
    sheet = data_sheet(working)
    rows = {str(row[1]): row for row in sheet.get("rows", []) if isinstance(row, list) and len(row) > 1}
    costs_by_date = source.get("costs_by_date")
    if not isinstance(costs_by_date, dict):
        raise ValueError("last_known_cost_choices_invalid")
    presentation = working.setdefault("metadata", {}).setdefault("server_cell_presentation", {})
    changes, remaining = [], []
    for day in requested:
        if day not in sheet.get("header", []):
            continue
        index = sheet["header"].index(day)
        choices = costs_by_date.get(day, {})
        if not isinstance(choices, dict):
            raise ValueError("last_known_cost_choices_invalid")
        for row_id in sorted(key for key in rows if key.startswith("SKU:") and key.endswith("|" + COST)):
            row = rows[row_id]
            before = row[index] if len(row) > index else ""
            if before not in ("", None):
                continue
            candidate = choices.get(row_id)
            if candidate is None:
                remaining.append({"date": day, "row_id": row_id, "reason": "no_prior_or_same_closed_resolved_cost"})
                continue
            source_day = _business_date(candidate.get("source_as_of_date"))
            number = _positive_finite(candidate.get("management_value"))
            if source_day > day or number is None:
                remaining.append({"date": day, "row_id": row_id, "reason": "invalid_last_known_cost_candidate"})
                continue
            value = float(number)
            while len(row) <= index:
                row.append("")
            reason = (f"Управленческая оценка: последняя известная себестоимость этого SKU "
                      f"из закрытого дня {source_day}; складские остатки не оценивались.")
            provenance = {
                "source": SOURCE,
                "state": "unconfirmed",
                "tone": "warning",
                "quality_state": "management_estimate",
                "quality_label": "Управленческая оценка",
                "reason": reason,
                "quality_reason": reason,
                "management_value": format(number, "f"),
                "source_as_of_date": source_day,
                "target_date": day,
                "source_snapshot_id": source.get("snapshot_id"),
                "source_bundle_version": source.get("bundle_version"),
                "source_day_version_id": candidate.get("source_day_version_id"),
                "source_digest": candidate.get("source_digest"),
                "source_book_blob_digest": candidate.get("source_book_blob_digest"),
                "operation_id": operation_id,
                "evidence": {"selection_method": "latest_same_sku_closed_resolved_on_or_before_target",
                             "source_row_status": "resolved"},
            }
            row[index] = value
            presentation.setdefault(row_id, {})[day] = provenance
            changes.append({"date": day, "row_id": row_id, "before": before,
                            "after": value, "provenance": provenance})
    return {"plan": working, "changes": changes, "remaining": remaining}


class ApprovedEstimateCostSnapshot:
    """Pinned reporting overlay; never writes or invents inventory/book facts."""

    def __init__(self, exact, cells):
        self.exact = exact
        self.cells = json.loads(json.dumps(cells))
        self.version_id = fingerprint({'exact': exact.version_id, 'approved_estimates': self.cells})

    def __getattr__(self, name):
        return getattr(self.exact, name)

    def metadata(self):
        return {**self.exact.metadata(), 'version_id': self.version_id,
                'management_estimate_policy': 'explicit_applied_same_sku_closed_cost_v1'}

    def resolve(self, *, nm_id, operation_date):
        resolved = self.exact.resolve(nm_id=nm_id, operation_date=operation_date)
        if resolved.get('status') == 'resolved':
            return resolved
        # Do not override invalid identity, invalidated books or unresolved
        # exact-day components. Only a genuinely absent day/SKU is eligible.
        if resolved.get('reason') not in {'shared_cost_exact_date_missing', 'shared_cost_sku_missing'}:
            return resolved
        cell = self.cells.get(str(nm_id), {}).get(operation_date.isoformat())
        if cell is None:
            return resolved
        return {**resolved, 'status': 'resolved', 'reason': '',
                'unit_cost_rub': cell['management_value'],
                'quality': 'management_estimate', 'projection_quality': 'management_estimate',
                'selection_method': 'explicit_applied_same_sku_closed_cost',
                'canonical_source_date': cell['source_as_of_date'],
                'canonical_source_version': cell['source_day_version_id'],
                'canonical_source_identity': cell['operation_id'] + ':' + fingerprint(cell),
                'source_digest': fingerprint(cell)}


def approved_reporting_snapshot(exact, conn):
    """Read only applied Vitrina estimates, and revalidate their closed source."""
    if exact is None:
        return None
    from packages.application.web_vitrina_management_history import load_presentations
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'registry_upload_current_state', 'sheet_vitrina_v1_ready_snapshots'} <= tables:
        return exact
    state = conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()
    if state is None or not exact._days:
        return exact
    cells = load_presentations(conn, bundle_version=state[0], dates=None, cost_only=True)
    approved = {}
    for key, by_date in cells.items():
        if not key.startswith('SKU:') or not key.endswith('|' + COST):
            continue
        nm = key[4:].split('|')[0]
        for day, cell in by_date.items():
            invalid_from = getattr(exact, 'invalid_from_date', None)
            if invalid_from and day >= invalid_from:
                continue
            source_day = cell.get('source_as_of_date', '')
            if (cell.get('quality_state') != 'management_estimate'
                    or cell.get('target_date') != day or not cell.get('operation_id')
                    or not str(cell.get('source_snapshot_id', '')).startswith('last-known-cost:')
                    or cell.get('evidence', {}).get('selection_method') != 'latest_same_sku_closed_resolved_on_or_before_target'
                    or source_day > day or source_day not in exact._days):
                continue
            period = exact._days[source_day]
            row = period['rows'].get(nm)
            amount = _positive_finite(cell.get('management_value'))
            if (period['status'] != 'closed' or row is None or row['status'] != 'resolved'
                    or cell.get('source_day_version_id') != period['version_id']
                    or cell.get('source_digest') != row['source_digest']
                    or amount is None or amount != _positive_finite(row['unit_cost_rub'])):
                continue
            approved.setdefault(nm, {})[day] = cell
    return ApprovedEstimateCostSnapshot(exact, approved) if approved else exact
