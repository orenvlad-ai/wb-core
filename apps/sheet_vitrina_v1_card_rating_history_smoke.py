"""Offline edition transition and HTTP/UI proof, not production throughput."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from apps.web_vitrina_snapshot_pilot_smoke import _build, _dense
from apps.web_vitrina_history_http_smoke import _read, _must_not_evaluate
from packages.application.sheet_vitrina_v1_card_rating import include_card_rating_catalog_presentation
from packages.application.web_vitrina_history_compiler import CONTRACT, digest, unpack_table
from packages.application.web_vitrina_history_store import HistoryStore, _atomic
from packages.application.web_vitrina_gravity_table_adapter import _renderer_id
from packages.application.web_vitrina_view_model import _build_cell
from packages.application.web_vitrina_window_v3 import WindowV3Service
from packages.application.web_vitrina_compact_table import CELL_FIELDS

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
DAYS = [(date(2026, 3, 1) + timedelta(days=i)).isoformat() for i in range(219)]
VALUE = 4.75123456789123


def rating_cell(day, value, quality):
    column = WindowV3Service._date_view_columns([day])[0]
    cell = _build_cell(column, {
        'format': 'rating', 'values_by_date': {day: value},
        'presentation_by_date': {day: {'quality_state': quality,
            'reason': 'Saved observation from ' + day}},
    })
    return [_renderer_id(cell_kind=cell.cell_kind, formatter_id=cell.formatter_id) if field == 'renderer_id' else getattr(cell, field) for field in CELL_FIELDS]


def vector(units, epoch):
    # These callback units test the publication state machine, not native proof.
    return {'coverage': 'complete_frozen_native_v1', 'epoch': epoch,
            'dates': {day: digest(unit) for day, unit in units.items()}}


def browser(base, *, edition, rating_expected):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        instance = p.chromium.launch(headless=True)
        page = instance.new_page(timezone_id='Asia/Tbilisi')
        page.clock.install(time=NOW)
        requests, errors = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url)
                if '/v1/sheet-vitrina-v1/web-vitrina?' in request.url else None)
        page.goto(base + '/sheet-vitrina-v1/vitrina', wait_until='domcontentloaded')
        page.wait_for_selector('[data-history-summary-load-ms]', timeout=30000)
        assert page.locator('[data-history-summary-load-ms]').get_attribute('data-history-summary-edition') == edition
        assert requests and all(parse_qs(urlsplit(url).query).get('history_snapshot') == ['1'] for url in requests), requests
        selector = '[data-table-body] td[data-row-id="TOTAL|avg_card_rating"][data-col-id="date:2026-10-04"]'
        if rating_expected:
            assert page.locator(selector).inner_text() == '4,75123'
            assert page.locator('[data-table-body] td[data-row-id="TOTAL|avg_card_rating"][data-col-id="date:2026-10-05"]').inner_text() == '—'
            page.locator('[data-metrics-settings-open]').click()
            # Pairing is a catalog assertion, independent of the modal list filter.
            page.locator('[data-metric-list-filter]').select_option('all')
            logical = page.locator('[data-metric-config-row]').evaluate_all('rows => rows.map(row => ({sku:row.dataset.skuMetricKey,total:row.dataset.totalMetricKey}))')
            assert any(item == {'sku': 'card_rating', 'total': 'avg_card_rating'} for item in logical), logical
        else:
            assert page.locator(selector).count() == 0
        assert not errors, errors
        instance.close()


def main():
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=2,
                                          history_snapshot=True, now=NOW)
    with fixture as base:
        table = _build(fixture, '2026-04-19', '2026-04-20', compact=True)['table_surface']
        new_catalog, dated = unpack_table(table)
        new_catalog['order'] = [row['row_id'] for row in table['rows']]
        new_catalog['context_epoch'] = 'new-reviewed-catalog'
        include_card_rating_catalog_presentation(new_catalog)
        rating_ids = {rid for rid in new_catalog['rows'] if rid.endswith(('|card_rating', '|avg_card_rating'))}
        assert 'TOTAL|avg_card_rating' in rating_ids
        old_catalog = deepcopy(new_catalog)
        old_catalog['context_epoch'] = 'previous-reviewed-catalog'
        old_catalog['order'] = [rid for rid in old_catalog['order'] if rid not in rating_ids]
        old_catalog['rows'] = {rid: row for rid, row in old_catalog['rows'].items() if rid not in rating_ids}
        for key, field in [('formatters', 'formatter_id'), ('renderers', 'formatter_id')]:
            old_catalog['presentation'][key] = [item for item in old_catalog['presentation'].get(key, []) if item.get(field) != 'rating']
        old_units, new_units = {}, {}
        for index, day in enumerate(DAYS):
            cells = {rid: deepcopy(dated[rid]['2026-04-20']) for rid in old_catalog['order']}
            # Distinct dated values and source times detect copying today's fact.
            probe = cells['TOTAL|total_view_count']
            probe[0:2] = [index + 100, str(index + 100)]
            probe[14] = day + 'T12:00:00Z'
            old_units[day] = {'contract': CONTRACT, 'date': day,
                'context_epoch': old_catalog['context_epoch'], 'accepted_ready_available': True,
                'members': list(old_catalog['order']), 'cells': cells}
            new_cells = deepcopy(cells)
            new_cells.update({rid: rating_cell(day, '', 'missing') for rid in rating_ids})
            if day == '2026-10-04':
                for rid in rating_ids:
                    new_cells[rid] = rating_cell(day, VALUE, 'exact')
            new_units[day] = {**old_units[day], 'context_epoch': new_catalog['context_epoch'],
                'members': list(new_catalog['order']), 'cells': new_cells}
        store = HistoryStore(fixture.history_snapshot_store)
        old_vector, new_vector = vector(old_units, 'old-reviewed-formula'), vector(new_units, 'new-reviewed-formula')
        previous = store.update(vector=old_vector, catalog=old_catalog,
            compile_day=old_units.__getitem__, revalidate=lambda: old_vector)['edition_id']
        old_refs = deepcopy(store.edition()['days'])
        fixture.entrypoint.handle_sheet_web_vitrina_page_composition_request = _must_not_evaluate
        fixture.entrypoint.handle_sheet_web_vitrina_shell_metadata_request = _must_not_evaluate
        before_files = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in store.root.rglob('*') if p.is_file()}
        status, before = _read(base, date_from=DAYS[0], date_to=DAYS[-1], limit=128)
        assert status == 200 and before['history_snapshot']['edition_id'] == previous
        browser(base, edition=previous, rating_expected=False)
        assert before_files == {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in store.root.rglob('*') if p.is_file()}

        result = store.update(vector=new_vector, catalog=new_catalog,
            compile_day=new_units.__getitem__, revalidate=lambda: new_vector, max_recomputes=31)
        assert result['status'] == 'pending' and result['completed'] == 31
        assert store._current()['current'] == previous and store.edition()['days'] == old_refs
        def fail(day):
            raise RuntimeError('injected compiler failure')
        try:
            store.update(vector=new_vector, catalog=new_catalog, compile_day=fail, revalidate=lambda: new_vector, max_recomputes=31)
            raise AssertionError('failure published')
        except RuntimeError as error:
            assert str(error) == 'injected compiler failure'
        assert store._current()['current'] == previous
        portions = 1
        while result['status'] == 'pending':
            result = store.update(vector=new_vector, catalog=new_catalog,
                compile_day=new_units.__getitem__, revalidate=lambda: new_vector, max_recomputes=31)
            portions += 1
            assert result['recomputes'] <= 31
            if result['status'] == 'pending':
                assert store._current()['current'] == previous
        assert portions == 8 and result['status'] == 'published'
        assert len(store.edition()['days']) == 219 and store._current()['previous'] == previous
        assert store.edition(previous)['days'] == old_refs
        # Every old row's sixteen dated fields, including timestamps, survives.
        for start in range(0, len(DAYS), 31):
            days = DAYS[start:start + 31]
            for scope in ('summary', 'sku'):
                old_page = store.read(date_from=days[0], date_to=days[-1], scope=scope, edition_id=previous, limit=512)
                new_page = store.read(date_from=days[0], date_to=days[-1], scope=scope, limit=512)
                new_rows = {row['row_id']: row for row in new_page['rows']}
                for row in old_page['rows']:
                    assert row['cells'] == new_rows[row['row_id']]['cells'], row['row_id']
        status, after = _read(base, date_from='2026-10-03', date_to='2026-10-05', limit=128)
        assert status == 200
        formatters = {item['formatter_id']: item for item in after['table_surface']['formatters']}
        assert formatters['rating']['decimals'] == 2
        assert any(item['renderer_id'] == 'renderer:number:rating' for item in after['table_surface']['renderers'])
        rows = {row['row_id']: row for row in _dense(after['table_surface']['rows'], after['table_surface']['columns'])}
        assert rows['TOTAL|avg_card_rating']['values']['date:2026-10-04']['value'] == VALUE
        assert rows['TOTAL|avg_card_rating']['values']['date:2026-10-03']['value'] == ''
        assert rows['TOTAL|avg_card_rating']['values']['date:2026-10-05']['quality_state'] == 'missing'
        browser(base, edition=result['edition_id'], rating_expected=True)
        status, pinned = _read(base, date_from=DAYS[0], date_to=DAYS[-1], edition_id=previous, limit=128)
        assert status == 200 and pinned['history_snapshot']['edition_id'] == previous
        assert all(row['row_id'] not in rating_ids for row in pinned['table_surface']['rows'])
        # An owned atomic pointer rollback after publication retains both
        # immutable editions. The production plan must bind this exact CAS.
        target = store._current()
        assert target == {'current': result['edition_id'], 'previous': previous}
        with store._writer():
            assert store._current() == target
            _atomic(store.root / 'CURRENT.json', {'current': previous, 'previous': result['edition_id']})
        browser(base, edition=previous, rating_expected=False)
        assert store.edition()['days'] == old_refs
        assert len(store.edition(result['edition_id'])['days']) == 219
    print('card rating: old CURRENT/default UI without native fallback; 219-day bounded pending/failure/atomic transition; exact16/times/pins/rollback; missing-template numeric renderer/precision/settings PASS')


if __name__ == '__main__':
    main()
