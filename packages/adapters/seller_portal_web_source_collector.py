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
from urllib.parse import urlsplit

PORTAL = "https://seller.wildberries.ru"
ROUTES = {
    "seller_funnel_snapshot": ("/content-analytics/interactive-report/main", "/sales-funnel/report"),
    "web_source_snapshot": ("/search-analytics/my-search-queries", "/search-report/report"),
}
MAX_DETAIL_PAGES = 200


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


def collect_web_source(*, source_key: str, snapshot_date: str, storage_state_path: str,
                       canonical_supplier_id: str) -> dict:
    """Read one exact date; callers own the shared browser lock and any writes."""
    from playwright.sync_api import sync_playwright
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
                raw_rows = [row for group in groups for row in group.get('items', [])]
                items = _search_items(raw_rows)
            items = _dedupe(items)
            if not items:
                raise CollectorError('seller_report_empty_unqualified')
            if source_key == 'web_source_snapshot':
                reported_count = data.get('commonInfo', {}).get('totalProducts')
                if not isinstance(reported_count, int) or reported_count != len(items):
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
                    'header_names': sorted(headers), 'pages': pages, 'reported_count': reported_count, 'completeness': 'complete',
                    'items': items, 'raw_report': report, 'detail_pages':detail_pages}
        except CollectorError as exc:
            raise exc from None
        except Exception as exc:
            raise CollectorError('seller_portal_collection_error:' + type(exc).__name__) from None
        finally:
            try:
                browser.close()
            except Exception:
                pass
