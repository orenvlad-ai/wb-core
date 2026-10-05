"""Small owned rollover reader/UI fixtures, with no native evaluator or sources."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import sys
import tempfile
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.sheet_vitrina_v1_web_vitrina_gravity_table_adapter_smoke import _build_view_model_payload
from packages.application.web_vitrina_gravity_table_adapter import build_web_vitrina_gravity_table_adapter
from packages.application.web_vitrina_compact_table import compact_adapter_payload
from packages.application.web_vitrina_history_store import HistoryStore, HistoryUnavailable, import_finished_table
from packages.application.web_vitrina_history_compiler import digest
from packages.application import web_vitrina_history_http_read as reader
from packages.business_time import current_business_date_iso
from packages.adapters.registry_upload_http_entrypoint import (
    _render_sheet_vitrina_web_vitrina_ui, _handle_web_vitrina_history_snapshot_request,
)

TODAY = '2026-10-06'
DAYS = [(date.fromisoformat(TODAY) - timedelta(days=i)).isoformat() for i in range(13, 0, -1)]


def fixture_table():
    table = compact_adapter_payload(build_web_vitrina_gravity_table_adapter(_build_view_model_payload()))
    old_columns = table['columns']
    first_date = next(i for i, column in enumerate(old_columns) if column['id'].startswith('date:'))
    template = old_columns[first_date]
    table['columns'] = old_columns[:first_date] + [
        {**deepcopy(template), 'id': 'date:' + day, 'accessor_key': 'date:' + day, 'header': day} for day in DAYS]
    total = next(row for row in table['rows'] if row['row_kind'] == 'total')
    group = deepcopy(total)
    group.update(row_id='GROUP:clean|' + total['row_id'], row_kind='group', group_id='group:clean')
    table['rows'].append(group)
    for row in table['rows']:
        saved = deepcopy(next(cell for cell in row['values'] if cell[0] == first_date))
        row['values'] = [cell for cell in row['values'] if cell[0] < first_date]
        row['values'] += [[first_date + i, *saved[1:]] for i in range(len(DAYS))]
        if row['row_kind'] == 'sku':
            row['group_id'] = 'group:clean'
    table['groupings'] = []
    return table


def refuse(call, reason):
    try:
        call()
    except HistoryUnavailable as error:
        assert str(error) == reason, (str(error), reason)
    else:
        raise AssertionError('expected ' + reason)


def family(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def checks(store, table, edition):
    assert current_business_date_iso(datetime(2026, 10, 5, 19, tzinfo=timezone.utc)) == TODAY
    before = family(store.root)
    refuse(lambda: store.read(date_from=DAYS[0], date_to=TODAY), 'history_date_unavailable')
    with patch.object(reader, 'current_business_date_iso', return_value=TODAY) as today:
        total = reader.read_history_page(store, date_from=DAYS[0], date_to=TODAY, scope='total')
        assert today.call_count == 1
    marker = total['history_snapshot']
    assert marker['edition_id'] == edition and marker['date_from'] == DAYS[0] and marker['date_to'] == TODAY
    assert marker['unmaterialized_dates'] == [TODAY] and marker['availability'][TODAY] is False
    assert marker['current_preliminary'] is True
    columns = total['table_surface']['columns']
    assert [c['id'][5:] for c in columns if c['id'].startswith('date:')] == DAYS + [TODAY]
    original = store.read(date_from=DAYS[0], date_to=DAYS[-1], scope='total')
    for row in total['table_surface']['rows']:
        saved = next(r for r in original['rows'] if r['row_id'] == row['row_id'])['cells']
        for cell in row['values']:
            key = columns[cell[0]]['id']
            if key.startswith('date:'):
                if key[5:] == TODAY:
                    assert len(cell[1:]) == 16 and cell[1] is None and cell[2] == '—'
                    assert cell[6] == 'unavailable' and cell[9] != 'not_tracked'
                else:
                    assert cell[1:] == saved[key[5:]]
    with patch.object(reader, 'current_business_date_iso', return_value=TODAY):
        for scope in ('catalog', 'total', 'group', 'sku'):
            page = reader.read_history_page(store, date_from=TODAY, date_to=TODAY, scope=scope, edition_id=edition)
            assert page['table_surface']['rows'] == [] and page['history_snapshot']['availability'] == {TODAY: False}
        group = reader.read_history_page(store, date_from=DAYS[0], date_to=TODAY, scope='group', edition_id=edition, group_id='group:clean')
        sku1 = reader.read_history_page(store, date_from=DAYS[0], date_to=TODAY, scope='sku', edition_id=edition, group_id='group:clean', limit=1)
        sku2 = reader.read_history_page(store, date_from=DAYS[0], date_to=TODAY, scope='sku', edition_id=edition, group_id='group:clean', limit=1, offset=1)
        assert len(group['table_surface']['rows']) == 1
        assert sku1['history_snapshot']['next_offset'] == 1 and sku2['history_snapshot']['next_offset'] is None
        assert all(p['history_snapshot']['edition_id'] == edition for p in (group, sku1, sku2))
        refuse(lambda: reader.read_history_page(store, date_from=DAYS[0], date_to='2026-10-07'), 'history_date_unavailable')
        tiny = HistoryStore(store.root, max_reply_bytes=64)
        refuse(lambda: reader.read_history_page(tiny, date_from=DAYS[0], date_to=TODAY), 'history_reply_limit')
    assert family(store.root) == before
    # Internal gap and two-day-old edition cannot use the one-day allowance.
    source = store.edition(edition)
    for remove in (DAYS[-1], DAYS[4]):
        changed = deepcopy(source)
        del changed['days'][remove]
        changed_id = digest(changed)
        path = store.root / 'editions' / (changed_id + '.json')
        path.write_text(json.dumps(changed))
        refuse(lambda: store.read(date_from=DAYS[0], date_to=TODAY, business_today=TODAY, edition_id=changed_id), 'history_date_unavailable')
        path.unlink()
    # Corrupt existing object is still an error, never replaced by rollover cells.
    object_path = store.root / 'objects' / (source['days'][DAYS[0]] + '.sqlite3')
    raw = object_path.read_bytes()
    import sqlite3
    with sqlite3.connect(object_path) as conn:
        conn.execute("UPDATE metadata SET payload='{}'")
    try:
        try:
            store.read(date_from=DAYS[0], date_to=TODAY, business_today=TODAY)
        except (HistoryUnavailable, KeyError):
            pass
        else:
            raise AssertionError('corrupt metadata accepted')
    finally:
        object_path.write_bytes(raw)
    assert family(store.root) == before
    # Published replacement must not change old pinned reads or their saved time.
    full = deepcopy(table)
    for row in full['rows']:
        source_cell = row['values'][-1]
        row['values'].append([len(full['columns']), *source_cell[1:]])
    full['columns'].append({**deepcopy(full['columns'][-1]), 'id': 'date:' + TODAY, 'accessor_key': 'date:' + TODAY, 'header': TODAY})
    new = import_finished_table(store, full, accepted_ready={d: True for d in DAYS + [TODAY]})['edition_id']
    assert new != edition
    with patch.object(reader, 'current_business_date_iso', return_value=TODAY):
        pinned = reader.read_history_page(store, date_from=DAYS[0], date_to=TODAY, scope='total', edition_id=edition)
        assert pinned == total
        fresh = reader.read_history_page(store, date_from=DAYS[0], date_to=TODAY, scope='total')
        assert fresh['history_snapshot']['unmaterialized_dates'] == []
    return new


def browser_check(store, old_edition):
    from playwright.sync_api import sync_playwright
    reads, posts, errors = [], [], []
    config = {'status': 'ok', 'revision': 2, 'config': {}}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == '/read':
                reads.append(parse_qs(url.query))
                # Keep serving the old edition; use the actual HTTP handler.
                query = url.query
                if 'edition_id=' not in query:
                    query += '&edition_id=' + old_edition
                _handle_web_vitrina_history_snapshot_request(self, query, store.root)
                return
            if url.path == '/vitrina':
                body = _render_sheet_vitrina_web_vitrina_ui(read_path='/read', operator_path='/operator',
                    refresh_path='/refresh', job_path='/job', history_snapshots_configured=True).encode()
                mime = 'text/html; charset=utf-8'
            elif url.path.endswith('.css'):
                body = (ROOT / 'packages/adapters/templates/sheet_vitrina_v1_ui_system.css').read_bytes()
                mime = 'text/css'
            else:
                body, mime = json.dumps(config).encode(), 'application/json'
            self.send_response(200); self.send_header('Content-Type', mime)
            self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            posts.append(self.path)
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.end_headers(); self.wfile.write(json.dumps(config).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with patch.object(reader, 'current_business_date_iso', return_value=TODAY), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.clock.install(time=datetime(2026, 10, 5, 19, tzinfo=timezone.utc))
            base = 'http://127.0.0.1:' + str(server.server_port)
            page.goto(base + '/vitrina')
            page.wait_for_selector('[data-history-summary-load-ms]', state='attached', timeout=15000)
            assert page.locator('[data-table-body] tr').count() > 0
            current_cells = page.locator('[data-table-body] td[data-col-id="date:' + TODAY + '"]')
            assert current_cells.count() > 0
            assert all(text.strip() == '—' for text in current_cells.all_inner_texts())
            assert page.locator('[data-history-current-unavailable]').is_visible()
            assert 'За текущий день ещё нет сохранённых данных' in page.locator('[data-history-current-unavailable]').inner_text()
            assert page.locator('[data-table-load-status]').get_attribute('data-load-status-text') == 'текущий день ещё не сохранён'
            assert [q['scope'][0] for q in reads] == ['catalog', 'total']
            assert all(q['date_from'] == [DAYS[0]] and q['date_to'] == [TODAY] for q in reads)
            page.locator('[data-filters-toggle]').click()
            page.locator('[data-block-kind="skus"][data-block-group="group:clean"]').check()
            page.locator('[data-filters-apply]').click()
            page.wait_for_function("document.querySelector('[data-table-body]').querySelectorAll('tr').length > 1")
            assert [q['scope'][0] for q in reads] == ['catalog', 'total', 'sku']
            assert reads[-1]['edition_id'] == [old_edition] and reads[-1]['date_to'] == [TODAY]
            assert page.locator('[data-history-current-unavailable]').is_visible()
            page.goto(base + '/vitrina?history_mode=explicit&date_from=' + TODAY + '&date_to=' + TODAY)
            page.wait_for_selector('[data-history-summary-load-ms]', state='attached', timeout=15000)
            assert 'За текущий день ещё нет сохранённых данных' in page.locator('[data-state-title]').inner_text()
            assert not errors, errors
            assert not any('user-config' in path for path in posts), posts
            browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def main():
    with tempfile.TemporaryDirectory(prefix='history-rollover-') as directory:
        store = HistoryStore(Path(directory) / 'history')
        table = fixture_table()
        edition = import_finished_table(store, table, accepted_ready={d: True for d in DAYS})['edition_id']
        checks(store, table, edition)
        before = family(store.root)
        browser_check(store, edition)
        assert family(store.root) == before
    print(json.dumps({'status': 'pass', 'synthetic_read_only': True, 'canonical_rollover': True,
        'only_one_missing_today': True, 'previous_exact16': True, 'strict_default': True,
        'pinned_total_group_sku_pages': True, 'new_current_old_pin': True, 'bounds_corruption_quota': True,
        'mixed_and_today_ui': True, 'no_evaluator': True, 'no_config_write': True}))


if __name__ == '__main__':
    main()
