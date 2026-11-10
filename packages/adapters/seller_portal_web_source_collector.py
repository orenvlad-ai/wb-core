"""Dated Seller Portal reports, collected without writing source storage.

The authenticated browser request owns the replay header names and values.
Cookies and authorization remain in its context and are never returned.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from urllib.parse import urlsplit

PORTAL = "https://seller.wildberries.ru"
ROUTES = {
    "seller_funnel_snapshot": ("/content-analytics/interactive-report/main", "/sales-funnel/report"),
    "web_source_snapshot": ("/search-analytics/my-search-queries", "/search-report/report"),
}
MAX_DETAIL_PAGES = 200
SEARCH_BATCH_SIZE = 40
MAX_SEARCH_UNIVERSE = 400
SEARCH_SCOPED_MODE = 'bounded_nm_scope_v1'


class CollectorError(RuntimeError):
    """Safe source failure code; never a provider exception message."""


def _post_report(client, url, **kwargs):
    try:
        return client.post(url, **kwargs)
    except Exception as exc:
        raise CollectorError('seller_report_transport_error:' + type(exc).__name__) from None



def replay_headers(headers: dict) -> dict:
    normalized = {key.lower(): value for key, value in headers.items()}
    result = {key: normalized[key] for key in (
        "content-type", "authorizev3", "seller-lk", "wb-seller-lk", "accept", "root-version"
    ) if normalized.get(key)}
    missing = [key for key in ("content-type", "authorizev3") if key not in result]
    if not (result.get("seller-lk") or result.get("wb-seller-lk")):
        missing.append("seller-lk|wb-seller-lk")
    if missing:
        raise CollectorError("template_required_headers_missing: " + ",".join(missing))
    if result.get("seller-lk") and result.get("wb-seller-lk") and result['seller-lk'] != result['wb-seller-lk']:
        raise CollectorError("template_supplier_headers_conflict")
    return result


def _supplier_identity(context, page, expected: str) -> str:
    if not expected:
        raise CollectorError("canonical_supplier_id_required")
    ids = {c['value'] for c in context.cookies([PORTAL])
           if c['name'] in {'x-supplier-id', 'x-supplier-id-external'} and c.get('value')}
    encoded = page.evaluate("() => window.localStorage.getItem('analytics-external-data') || ''")
    if encoded:
        try:
            supplier = json.loads(base64.b64decode(encoded).decode()).get('idSupplier')
        except (ValueError, UnicodeError) as exc:
            raise CollectorError("supplier_identity_invalid") from exc
        if supplier:
            ids.add(str(supplier))
    if ids != {expected}:
        raise CollectorError("seller_portal_wrong_or_unconfirmed_supplier")
    return hashlib.sha256(expected.encode()).hexdigest()


def _response_json(response):
    if response.status != 200:
        raise CollectorError(f"seller_report_http_{response.status}")
    payload = response.json()
    additional = payload.get('additionalErrors') if isinstance(payload, dict) else None
    errors = additional.get('errors') if isinstance(additional, dict) and set(additional) == {'errors'} else additional
    if not isinstance(payload, dict) or payload.get('error') or errors:
        raise CollectorError("seller_report_error_or_partial_response")
    if not isinstance(payload.get('data'), (dict, list)):
        raise CollectorError("seller_report_data_missing")
    return payload


def _current(value):
    return value.get('current') if isinstance(value, dict) else value


def _funnel_items(rows):
    return [{'nm_id': row.get('nmId'), 'name': row.get('name'), 'vendor_code': row.get('vendorCode'),
             'view_count': _current(row.get('viewCount')), 'open_card_count': _current(row.get('openCard')),
             'ctr': _current(row.get('viewToOpen'))} for row in rows]


def _search_items(rows):
    return [{'nm_id': row.get('nmId'), 'views_current': _current(row.get('views')),
             'ctr_current': _current(row.get('ctr')), 'orders_current': _current(row.get('orders')),
             'position_avg': _current(row.get('avgPosition'))} for row in rows]


def _dedupe(rows):
    indexed = {}
    for row in rows:
        nm = row.get('nm_id')
        if not isinstance(nm, int) or isinstance(nm, bool):
            raise CollectorError('seller_report_invalid_nm_id')
        if nm in indexed and indexed[nm] != row:
            raise CollectorError('seller_report_conflicting_duplicate')
        indexed[nm] = row
    return [indexed[nm] for nm in sorted(indexed)]


def _search_group_totals_match(groups, rows):
    """Where WB supplies group metrics, require item sums for additive fields."""
    if not groups:
        return not rows
    if not all(isinstance(g.get('metrics'), dict) for g in groups):
        return False
    for group_key, row_key in (('views', 'views'), ('orders', 'orders')):
        totals = [_current(g['metrics'].get(group_key)) for g in groups]
        values = [_current(r.get(row_key)) for r in rows]
        if (any(type(v) not in (int, float) for v in totals + values)
            or sum(totals) != sum(values)):
            return False
    return True


def _scoped_search_rows(report):
    """Validate the raw evidence used by bounded-scope certification."""
    if not isinstance(report, dict):
        raise CollectorError('search_scope_response_invalid')
    additional = report.get('additionalErrors')
    errors = additional.get('errors') if isinstance(additional, dict) and set(additional) == {'errors'} else additional
    data = report.get('data')
    if report.get('error') or errors or not isinstance(data, dict):
        raise CollectorError('search_scope_response_invalid')
    groups = data.get('groups')
    if (not isinstance(groups, list) or any(not isinstance(g, dict)
        or not isinstance(g.get('items'), list) or not isinstance(g.get('metrics'), dict) for g in groups)):
        raise CollectorError('search_scope_groups_invalid')
    rows = [row for group in groups for row in group['items']]
    if any(not isinstance(row, dict) for row in rows):
        raise CollectorError('search_scope_rows_invalid')
    items = _dedupe(_search_items(rows))
    if len(items) != len(rows) or any(item['nm_id'] <= 0 for item in items):
        raise CollectorError('search_scope_duplicate_or_invalid_id')
    # The accepted search payload and PostgreSQL columns require integers for
    # these four fields. Reject incompatible values before any source write.
    if any(type(item[key]) is not int or item[key] < 0
           for item in items for key in ('views_current', 'ctr_current', 'orders_current', 'position_avg')):
        raise CollectorError('search_scope_metric_invalid')
    common = data.get('commonInfo')
    count = common.get('totalProducts') if isinstance(common, dict) else None
    if type(count) is not int or count < 0:
        raise CollectorError('search_scope_reported_count_invalid')
    return groups, rows, items, count


def _scoped_search_totals(groups, rows):
    """Require every supplied additive current metric, including zero rows."""
    for key in ('views', 'openCard', 'addToCart', 'orders'):
        totals = [_current(group['metrics'].get(key)) for group in groups]
        values = [_current(row.get(key)) for row in rows]
        if (any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                for value in totals + values) or sum(totals) != sum(values)):
            raise CollectorError('search_scope_additive_totals_incomplete')


def validate_scoped_search_evidence(*, report, batches, scope_nm_ids):
    """Reconstruct bounded SKU-scope evidence, never account-wide completeness.

    WB can return a real row with totalProducts=0. Its count is retained as a
    diagnostic, with a conservative count>rows alarm; it is not row cardinality.
    Missing requested IDs remain missing, even when every additive sum matches.
    The caller must independently bind this scope to its reviewed catalog.
    """
    if (not isinstance(scope_nm_ids, list) or not 1 <= len(scope_nm_ids) <= MAX_SEARCH_UNIVERSE
        or any(type(nm) is not int or nm <= 0 for nm in scope_nm_ids)
        or scope_nm_ids != sorted(set(scope_nm_ids))):
        raise CollectorError('search_scope_universe_invalid')
    groups, preview_rows, preview, reported = _scoped_search_rows(report)
    if not isinstance(batches, list) or not batches or len(batches) > MAX_SEARCH_UNIVERSE:
        raise CollectorError('search_scope_batches_invalid')
    scope = set(scope_nm_ids)
    if not {item['nm_id'] for item in preview}.issubset(scope):
        raise CollectorError('search_scope_preview_outside_scope')
    requested = set()
    indexed = {}
    raw_indexed = {}
    complete_rows = []
    for batch in batches:
        if not isinstance(batch, dict):
            raise CollectorError('search_scope_batch_invalid')
        nms = batch.get('requested_nm_ids')
        if (not isinstance(nms, list) or not 1 <= len(nms) <= SEARCH_BATCH_SIZE
            or any(type(nm) is not int or nm <= 0 for nm in nms)
            or nms != sorted(set(nms)) or requested.intersection(nms) or not set(nms).issubset(scope)):
            raise CollectorError('search_scope_batch_filter_invalid')
        requested.update(nms)
        batch_groups, rows, items, count = _scoped_search_rows(batch.get('response'))
        if count > len(items) or any(item['nm_id'] not in nms for item in items):
            raise CollectorError('search_scope_batch_incomplete_or_unbound')
        _scoped_search_totals(batch_groups, rows)
        for row, item in zip(rows, _search_items(rows)):
            nm = item['nm_id']
            if nm in indexed:
                raise CollectorError('search_scope_batch_overlap')
            indexed[nm] = item
            raw_indexed[nm] = row
        complete_rows.extend(rows)
    if requested != scope:
        raise CollectorError('search_scope_requests_incomplete')
    for row, item in zip(preview_rows, _search_items(preview_rows)):
        nm = item['nm_id']
        if (indexed.get(nm) != item or any(_current(row.get(key)) != _current(raw_indexed[nm].get(key))
            for key in ('views', 'openCard', 'addToCart', 'orders'))):
            raise CollectorError('search_scope_preview_missing_or_drifted')
    if reported > len(indexed):
        raise CollectorError('search_scope_global_count_incomplete')
    _scoped_search_totals(groups, complete_rows)
    returned = sorted(indexed)
    certificate = {'mode': SEARCH_SCOPED_MODE, 'requested_nm_ids': list(scope_nm_ids),
                   'returned_nm_ids': returned, 'missing_nm_ids': sorted(scope - set(returned)),
                   'reported_count': reported, 'observed_count': len(returned),
                   'count_difference': len(returned) - reported}
    return [indexed[nm] for nm in returned], certificate


def _search_scoped_items(client, url, headers, body, report, candidate_nm_ids):
    _, _, preview, _ = _scoped_search_rows(report)
    candidates = list(candidate_nm_ids or ())
    if any(type(nm) is not int or nm <= 0 for nm in candidates):
        raise CollectorError('search_scope_universe_invalid')
    scope = sorted(set(candidates) | {item['nm_id'] for item in preview})
    if not scope or len(scope) > MAX_SEARCH_UNIVERSE:
        raise CollectorError('search_scope_universe_invalid')
    batches = []
    for start in range(0, len(scope), SEARCH_BATCH_SIZE):
        requested = scope[start:start + SEARCH_BATCH_SIZE]
        response = _response_json(_post_report(client, url, headers=headers,
            data=json.dumps({**body, 'nmIds': requested}), timeout=60000))
        batches.append({'requested_nm_ids': requested, 'response': response})
    items, certificate = validate_scoped_search_evidence(report=report, batches=batches, scope_nm_ids=scope)
    return items, batches, certificate


def _search_complete_items(client, url, headers, body, report, candidate_nm_ids):
    """Read the UI's bounded nmIds filter; bind every batch to the global count."""
    data = report['data']
    groups = data.get('groups') if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise CollectorError('seller_report_groups_missing')
    preview_rows = [row for group in groups for row in group.get('items', [])]
    preview = _dedupe(_search_items(preview_rows))
    if len(preview_rows) != len(preview):
        raise CollectorError('search_report_product_count_incomplete')
    reported = data.get('commonInfo', {}).get('totalProducts')
    if not isinstance(reported, int) or isinstance(reported, bool) or reported <= 0:
        raise CollectorError('search_report_product_count_invalid')
    if len(preview) == reported:
        if not _search_group_totals_match(groups, preview_rows):
            raise CollectorError('search_report_group_totals_incomplete')
        return preview, []
    if len(preview) > reported:
        raise CollectorError('search_report_product_count_incomplete')
    universe = sorted(set(candidate_nm_ids or ()) | {item['nm_id'] for item in preview})
    if (not universe or len(universe) > MAX_SEARCH_UNIVERSE
        or any(type(nm) is not int or nm <= 0 for nm in universe)):
        raise CollectorError('search_report_universe_invalid')
    all_items = {item['nm_id']: item for item in preview}
    seen = set()
    batches = []
    complete_rows = []
    for start in range(0, len(universe), SEARCH_BATCH_SIZE):
        requested = universe[start:start + SEARCH_BATCH_SIZE]
        response = _response_json(_post_report(client, url, headers=headers,
            data=json.dumps({**body, 'nmIds': requested}), timeout=60000))
        filtered_data = response['data']
        filtered_groups = filtered_data.get('groups') if isinstance(filtered_data, dict) else None
        if not isinstance(filtered_groups, list):
            raise CollectorError('search_report_batch_groups_missing')
        rows = [row for group in filtered_groups for row in group.get('items', [])]
        if not _search_group_totals_match(filtered_groups, rows):
            raise CollectorError('search_report_batch_group_totals_incomplete')
        items = _dedupe(_search_items(rows))
        filtered_count = filtered_data.get('commonInfo', {}).get('totalProducts')
        if (type(filtered_count) is not int or filtered_count != len(items)
            or len(rows) != len(items) or any(item['nm_id'] not in requested for item in items)):
            raise CollectorError('search_report_batch_incomplete_or_unbound')
        for item in items:
            nm = item['nm_id']
            if nm in seen or (nm in all_items and all_items[nm] != item):
                raise CollectorError('search_report_batch_overlap_or_value_drift')
            all_items[nm] = item
            seen.add(nm)
        complete_rows.extend(rows)
        batches.append({'requested_nm_ids': requested, 'response': response})
    if len(all_items) != reported or not {item['nm_id'] for item in preview}.issubset(seen):
        raise CollectorError('search_report_product_count_incomplete')
    # The unfiltered report's metrics describe all products, while its items
    # may be only a preview. Require the global sums after bounded completion.
    if not _search_group_totals_match(groups, complete_rows):
        raise CollectorError('search_report_group_totals_incomplete')
    return [all_items[nm] for nm in sorted(all_items)], batches


def collect_web_source(*, source_key: str, snapshot_date: str, storage_state_path: str,
                       canonical_supplier_id: str, search_candidate_nm_ids=None,
                       search_completeness_mode: str = 'strict') -> dict:
    """Read one exact date; callers own the shared browser lock and any writes."""
    from playwright.sync_api import sync_playwright
    if search_completeness_mode not in ('strict', SEARCH_SCOPED_MODE):
        raise ValueError('search_completeness_mode_invalid')
    day = date.fromisoformat(snapshot_date)
    if day.isoformat() != snapshot_date:
        raise ValueError('canonical_date_required')
    portal_path, endpoint = ROUTES[source_key]
    started_at = datetime.now(timezone.utc).isoformat()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=storage_state_path)
            page = context.new_page()
            with page.expect_response(lambda r: endpoint in r.url and '/details' not in r.url
                                      and r.request.method == 'POST', timeout=35000) as info:
                page.goto(PORTAL + portal_path, wait_until='domcontentloaded', timeout=35000)
            template = info.value
            _response_json(template)
            parsed = urlsplit(template.url)
            if parsed.scheme != 'https' or parsed.hostname != 'seller-content.wildberries.ru' or parsed.query:
                raise CollectorError('seller_report_unexpected_endpoint')
            supplier_hash = _supplier_identity(context, page, canonical_supplier_id)
            headers = replay_headers(template.request.all_headers())
            body = deepcopy(template.request.post_data_json)
            previous_key = 'prevPeriod' if source_key == 'seller_funnel_snapshot' else 'pastPeriod'
            if not isinstance(body, dict) or not isinstance(body.get('currentPeriod'), dict) or not isinstance(body.get(previous_key), dict):
                raise CollectorError('seller_report_period_contract_missing')
            body['currentPeriod'] = {'start': snapshot_date, 'end': snapshot_date}
            previous = (day - timedelta(days=1)).isoformat()
            body[previous_key] = {'start': previous, 'end': previous}
            for key in ('subjects', 'brands', 'tagIds', 'nms', 'subjectIds', 'brandNames', 'nmIds'):
                if key in body:
                    body[key] = []
            response = _post_report(context.request, template.url, headers=headers, data=json.dumps(body), timeout=60000)
            report = _response_json(response)
            data = report['data']
            groups = data.get('groups') if isinstance(data, dict) else None
            if not isinstance(groups, list):
                raise CollectorError('seller_report_groups_missing')
            pages = 1
            detail_pages = []
            search_batches = []
            search_scope = None
            if source_key == 'seller_funnel_snapshot':
                raw_rows = [row for group in groups for row in group.get('itemsGroup', [])]
                items = _funnel_items(raw_rows)
                # Read all detail pages from offset zero. Group previews need
                # not contain exactly the first 50 rows across all groups.
                seen = set()
                for page_index in range(MAX_DETAIL_PAGES):
                    details_body = {key: body[key] for key in ('subjects','brands','tagIds','nms','currentPeriod','prevPeriod','orderBy','skipDeletedNm') if key in body}
                    details_body.update(limit=50, offset=page_index * 50)
                    detail = _response_json(_post_report(context.request, template.url + '/details', headers=headers, data=json.dumps(details_body), timeout=60000))
                    detail_pages.append({'offset':page_index*50,'limit':50,'response':detail})
                    rows = detail['data']
                    if not isinstance(rows, list):
                        raise CollectorError('seller_funnel_details_shape_invalid')
                    pages += 1
                    nm_ids = {row.get('nmId') for row in rows}
                    if rows and nm_ids <= seen:
                        raise CollectorError('seller_funnel_pagination_not_advancing')
                    seen.update(nm_ids)
                    items.extend(_funnel_items(rows))
                    if len(rows) < 50:
                        break
                else:
                    raise CollectorError('seller_funnel_pagination_bound_exceeded')
            else:
                if search_completeness_mode == SEARCH_SCOPED_MODE:
                    items, search_batches, search_scope = _search_scoped_items(context.request,
                        template.url,headers,body,report,search_candidate_nm_ids)
                else:
                    items, search_batches = _search_complete_items(context.request,template.url,headers,
                        body,report,search_candidate_nm_ids)
                pages += len(search_batches)
            items = _dedupe(items)
            if not items:
                raise CollectorError('seller_report_empty_unqualified')
            if source_key == 'web_source_snapshot':
                reported_count = data.get('commonInfo', {}).get('totalProducts')
                if search_scope is None and (not isinstance(reported_count, int) or reported_count != len(items)):
                    raise CollectorError('search_report_product_count_incomplete')
            else:
                reported_count = len(items)
                for source_metric, item_metric in [('viewCount','view_count'), ('openCard','open_card_count')]:
                    totals = [_current(group.get(source_metric)) for group in groups]
                    values = [row.get(item_metric) for row in items]
                    if any(not isinstance(v,(int,float)) for v in totals + values) or sum(totals) != sum(values):
                        raise CollectorError('funnel_report_group_totals_incomplete')
            return {'contract': 'seller_portal_dated_observation_v1', 'source_key': source_key,
                    'snapshot_date': snapshot_date, 'source_fetched_at': started_at,
                    'collection_finished_at': datetime.now(timezone.utc).isoformat(),
                    'supplier_identity_sha256': supplier_hash, 'request_period': body['currentPeriod'],
                    'header_names': sorted(headers), 'pages': pages, 'reported_count': reported_count,
                    'completeness': 'scoped_complete' if search_scope is not None else 'complete',
                    'items': items, 'raw_report': report, 'detail_pages':detail_pages,
                    'search_batches':search_batches, **({'search_scope': search_scope} if search_scope is not None else {})}
        except CollectorError as exc:
            raise exc from None
        except Exception as exc:
            raise CollectorError('seller_portal_collection_error:' + type(exc).__name__) from None
        finally:
            try:
                browser.close()
            except Exception:
                pass
