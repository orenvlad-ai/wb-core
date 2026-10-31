"""Exact dated physical WB observations, independent of inventory valuation."""
from datetime import datetime
from decimal import Decimal
import json
import sqlite3

from packages.business_time import current_business_date_iso
from packages.application.fbs_current_snapshot_policy import resolve_current_snapshot_policy
from packages.application.warehouse_functional import _hash, _wb_snapshot_integrity


def _quantity(value):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError('invalid_wb_physical_quantity')
    return int(number)


def read_official_wb_physical_snapshot(conn: sqlite3.Connection, *, universe: list[int],
                                       day: str, now: datetime, snapshot_id: str | None = None) -> dict:
    """Read retained same-date evidence; current observations retain age policy.

    Historical dates remain observations at their saved time, never a claim
    of accounting closure or end-of-day stock. No financial table is consulted.
    """
    ids = sorted(set(universe))
    if not ids or len(ids) != len(universe) or any(type(nm) is not int or nm <= 0 for nm in ids):
        raise ValueError('invalid_wb_physical_scope')
    row = conn.execute(
        'SELECT s.*,v.status AS version_status,v.business_effective_date,v.source_watermarks_json '
        'FROM sheet_vitrina_v1_warehouse_wb_snapshots s '
        'JOIN sheet_vitrina_v1_warehouse_functional_versions v USING(version_id) '
        'WHERE s.snapshot_date=? AND (? IS NULL OR s.snapshot_id=?) '
        'ORDER BY s.fetched_at DESC,s.snapshot_id DESC LIMIT 1', (day,snapshot_id,snapshot_id)).fetchone()
    if row is None:
        raise ValueError('same_date_wb_physical_snapshot_missing')
    snapshot = dict(row)
    observed = datetime.fromisoformat(snapshot['fetched_at'].replace('Z','+00:00'))
    if (snapshot['version_status'] != 'good' or snapshot['business_effective_date'] != day
            or current_business_date_iso(observed) != day or observed > now):
        raise ValueError('wb_physical_date_version_mismatch')
    if day == current_business_date_iso(now):
        age = (now-observed).total_seconds()
        if not 0 <= age <= resolve_current_snapshot_policy(connection=conn).official_max_age_seconds:
            raise ValueError('wb_physical_snapshot_stale')
    requested = json.loads(snapshot['requested_nm_ids_json'])
    items = json.loads(snapshot['items_json'])
    raw = json.loads(snapshot['raw_rows_json'])
    offsets = json.loads(snapshot['page_offsets_json'])
    watermark = json.loads(snapshot['source_watermarks_json']).get('wb_snapshot',{})
    if (not isinstance(requested,list) or any(type(nm) is not int for nm in requested)
            or sorted(requested) != ids or len(set(requested)) != len(requested)
            or not isinstance(items,list) or not isinstance(raw,list)
            or not offsets or offsets[0] != 0 or len(offsets) != snapshot['page_count']
            or any(type(v) is not int or v < 0 for v in offsets) or offsets != sorted(set(offsets))
            or snapshot['pagination_complete'] != 1 or watermark.get('pagination_complete') is not True
            or not watermark.get('snapshot_id') or watermark.get('requested_count') != len(ids)
            or watermark.get('raw_row_count') != len(raw) or snapshot['raw_row_count'] != len(raw)
            or watermark.get('fetched_at') != snapshot['fetched_at']):
        raise ValueError('wb_physical_scope_or_pagination_incomplete')
    digest = 'sha256:' + _hash(sorted(raw,key=lambda item:json.dumps(item,ensure_ascii=False,sort_keys=True,separators=(',',':'))))
    if not digest == snapshot['raw_rows_digest'] == watermark.get('digest'):
        raise ValueError('wb_physical_source_digest_mismatch')
    quantities = {}
    for item in items:
        nm = item['nm_id']
        if type(nm) is not int or nm not in ids or str(nm) in quantities:
            raise ValueError('wb_physical_dense_identity_mismatch')
        physical,to_client,from_client = (_quantity(item[k]) for k in ('quantity','in_way_to_client','in_way_from_client'))
        if physical+to_client+from_client != _quantity(item['wb_contour_quantity']):
            raise ValueError('wb_physical_contour_mismatch')
        quantities[str(nm)] = physical
    if set(quantities) != {str(nm) for nm in ids}:
        raise ValueError('wb_physical_dense_identity_mismatch')
    for item in raw:
        if type(item.get('nmId')) is not int or item['nmId'] not in ids:
            raise ValueError('wb_physical_raw_identity_mismatch')
        for key in ('quantity','inWayToClient','inWayFromClient'):
            _quantity(item[key])
    integrity = _wb_snapshot_integrity(snapshot)
    if (not integrity['raw_to_canonical_mapping_matches'] or integrity['exact_duplicate_count']
            or integrity['source_key_duplicate_count']):
        raise ValueError('wb_physical_raw_mapping_mismatch')
    return dict(date=day,snapshot_id=snapshot['snapshot_id'],source_digest=digest,
        version_id=snapshot['version_id'],observed_at=snapshot['fetched_at'],
        quantity_basis='physical_quantity_only',skus=quantities,total=sum(quantities.values()))
