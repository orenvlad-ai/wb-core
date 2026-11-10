"""Synthetic, credential-free bounded search scope proof regression checks."""
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.adapters.seller_portal_web_source_collector import (
    CollectorError, _search_scoped_items, validate_scoped_search_evidence,
)


def row(nm, views=1, opens=1, carts=1, orders=1):
    return {'nmId': nm, 'views': {'current': views}, 'openCard': {'current': opens},
            'addToCart': {'current': carts}, 'orders': {'current': orders},
            'ctr': {'current': 0}, 'avgPosition': {'current': 0}}


def report(rows, count=None, all_rows=None):
    totals = rows if all_rows is None else all_rows
    return {'error': False, 'data': {'commonInfo': {'totalProducts': len(rows) if count is None else count},
        'groups': [{'items': deepcopy(rows), 'metrics': {key: {'current': sum(r[key]['current'] for r in totals)}
            for key in ('views', 'openCard', 'addToCart', 'orders')}}]}}


def rejected(global_report, batches, scope, expected):
    try:
        validate_scoped_search_evidence(report=global_report, batches=batches, scope_nm_ids=scope)
    except CollectorError as exc:
        assert str(exc) == expected, (str(exc), expected)
    else:
        raise AssertionError('invalid evidence accepted: ' + expected)


def main():
    rows = [row(nm) for nm in range(1, 71)] + [row(71, views=6, opens=0, carts=0, orders=0)]
    rows[59] = row(60, views=0, opens=0, carts=0, orders=0)
    global_report = report(rows[:50], count=70, all_rows=rows)
    scope = list(range(1, 73))  # 72 is absent, not a synthetic zero row.

    class Response:
        status = 200
        def __init__(self, data): self.data = data
        def json(self): return self.data

    class Client:
        def __init__(self): self.requests = []
        def post(self, url, **kwargs):
            nms = json.loads(kwargs['data'])['nmIds']; self.requests.append(nms)
            selected = [r for r in rows if r['nmId'] in nms]
            return Response(report(selected, count=len(selected) - int(71 in nms)))

    client = Client()
    items, batches, certificate = _search_scoped_items(client, 'https://example.test', {}, {}, global_report, scope)
    assert [len(nms) for nms in client.requests] == [40, 32]
    assert certificate == {'mode': 'bounded_nm_scope_v1', 'requested_nm_ids': scope,
        'returned_nm_ids': list(range(1, 72)), 'missing_nm_ids': [72],
        'reported_count': 70, 'observed_count': 71, 'count_difference': 1}
    assert len(items) == 71 and items[59]['views_current'] == 0
    assert all(item['nm_id'] != 72 for item in items)
    assert validate_scoped_search_evidence(report=global_report, batches=batches, scope_nm_ids=scope) == (items, certificate)

    singleton = report([rows[-1]], count=0)
    singleton_items, singleton_certificate = validate_scoped_search_evidence(report=singleton,
        batches=[{'requested_nm_ids': [71], 'response': singleton}], scope_nm_ids=[71])
    assert singleton_items[0]['views_current'] == 6 and singleton_certificate['count_difference'] == 1

    # Missing zero rows are explicitly missing even when all four sums match.
    missing_zero = deepcopy(batches)
    missing_zero[1]['response']['data']['groups'][0]['items'] = [r for r in missing_zero[1]['response']['data']['groups'][0]['items'] if r['nmId'] != 60]
    _, missing_certificate = validate_scoped_search_evidence(report=global_report, batches=missing_zero, scope_nm_ids=scope)
    assert missing_certificate['missing_nm_ids'] == [60, 72]

    bad = deepcopy(batches); bad[0]['response']['data']['groups'][0]['items'].pop()
    rejected(global_report, bad, scope, 'search_scope_batch_incomplete_or_unbound')
    bad = deepcopy(batches); bad[1]['response']['data']['groups'][0]['items'].pop()
    rejected(global_report, bad, scope, 'search_scope_additive_totals_incomplete')
    bad = deepcopy(batches); bad[0]['response']['data']['groups'][0]['items'].append(deepcopy(rows[0]))
    rejected(global_report, bad, scope, 'search_scope_duplicate_or_invalid_id')
    bad = deepcopy(batches); bad[0]['response']['data']['groups'][0]['items'][0]['nmId'] = 999
    rejected(global_report, bad, scope, 'search_scope_batch_incomplete_or_unbound')
    bad = deepcopy(batches); bad[1]['requested_nm_ids'].insert(0, 40)
    rejected(global_report, bad, scope, 'search_scope_batch_filter_invalid')
    rejected(global_report, batches[:1], scope, 'search_scope_requests_incomplete')
    bad = deepcopy(batches); bad[1]['requested_nm_ids'].remove(72)
    rejected(global_report, bad, scope, 'search_scope_requests_incomplete')
    rejected(global_report, [{'requested_nm_ids': scope, 'response': report(rows, count=70)}], scope,
        'search_scope_batch_filter_invalid')
    for metric in ('views', 'openCard', 'addToCart', 'orders'):
        bad = deepcopy(batches); bad[0]['response']['data']['groups'][0]['metrics'][metric]['current'] += 1
        rejected(global_report, bad, scope, 'search_scope_additive_totals_incomplete')
        bad_report = deepcopy(global_report); bad_report['data']['groups'][0]['metrics'][metric]['current'] += 1
        rejected(bad_report, batches, scope, 'search_scope_additive_totals_incomplete')
    for invalid in (-1, True, 1.2):
        bad = deepcopy(batches); bad[0]['response']['data']['commonInfo']['totalProducts'] = invalid
        rejected(global_report, bad, scope, 'search_scope_reported_count_invalid')
    bad_report = deepcopy(global_report); bad_report['data']['commonInfo']['totalProducts'] = 72
    rejected(bad_report, batches, scope, 'search_scope_global_count_incomplete')
    bad = deepcopy(batches); bad[0]['response']['additionalErrors'] = {'errors': ['partial']}
    rejected(global_report, bad, scope, 'search_scope_response_invalid')
    for invalid in (float('inf'), float('nan'), -1, True):
        bad = deepcopy(batches); bad[0]['response']['data']['groups'][0]['items'][0]['views']['current'] = invalid
        rejected(global_report, bad, scope, 'search_scope_metric_invalid')
    for metric in ('ctr', 'avgPosition'):
        for invalid in (None, float('nan'), float('inf'), 1.0, 1.5, -1, True):
            bad = deepcopy(batches); bad[0]['response']['data']['groups'][0]['items'][0][metric] = {'current': invalid}
            rejected(global_report, bad, scope, 'search_scope_metric_invalid')
        bad = deepcopy(batches); del bad[0]['response']['data']['groups'][0]['items'][0][metric]
        rejected(global_report, bad, scope, 'search_scope_metric_invalid')
    # Aggregate-preserving movement must not allow preview value drift.
    bad = deepcopy(batches)
    bad[0]['response']['data']['groups'][0]['items'][0]['openCard']['current'] = 0
    bad[0]['response']['data']['groups'][0]['items'][1]['openCard']['current'] = 2
    rejected(global_report, bad, scope, 'search_scope_preview_missing_or_drifted')
    print('seller_portal_scoped_search_collector_smoke: ok')


if __name__ == '__main__':
    main()
