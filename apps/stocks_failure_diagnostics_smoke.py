"""Real spawn/loopback faults through warehouse SQLite and cycle receipt storage."""
from __future__ import annotations

from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import errno
import json
import os
from pathlib import Path
import socket
import ssl
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps import sheet_vitrina_v1_cycle_smoke as cycle_fixture
from apps.stocks_resilience_smoke import Clock, Opener, body, item
from packages.adapters import stocks_block as stocks
from packages.application.sheet_vitrina_v1_cycle import (
    CycleReceiptStore, CycleHistoryConfig, run_cycle, cycle_error_diagnostic,
)
from packages.application.stocks_block import transform_legacy_payload
from packages.application.business_data_procedure_admission import initialize_admission
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional import ensure_warehouse_functional_schema
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.warehouse_update_journal import WarehouseUpdateJournal, phase_error_details
from packages.contracts.stocks_block import StocksRequest
from packages.contracts.stocks_failure_diagnostics import stocks_failure_diagnostic, sanitize_stocks_failure

SECRET = 'private-stocks-token-body-header-url-userdata'
TOKEN_ENV = 'WB_STOCKS_DIAGNOSTICS_SMOKE_TOKEN'
PHASE = 'official_complete_wb_stocks'
REAL_WORKER = stocks._stocks_inventory_worker


def _program_error_worker(connection, config, kwargs, expires_at):
    def broken(_self, _payload):
        raise KeyError(SECRET)
    stocks.HttpBackedStocksSource._extract_items = broken
    REAL_WORKER(connection, config, kwargs, expires_at)


def _tls_worker(connection, config, kwargs, expires_at):
    original = stocks.HttpBackedStocksSource.__init__
    def init(self, **options):
        original(self, **options)
        def fail(*_args, **_kwargs):
            exc = ssl.SSLCertVerificationError(1, SECRET)
            exc.verify_code = 20
            raise URLError(exc)
        self._opener = fail
    stocks.HttpBackedStocksSource.__init__ = init
    REAL_WORKER(connection, config, kwargs, expires_at)


def _diagnostic_fault_worker(connection, config, kwargs, expires_at):
    def broken(*_args):
        raise ValueError(SECRET)
    stocks._StocksDeadline.failure = broken
    REAL_WORKER(connection, config, kwargs, expires_at)


@contextmanager
def loopback(responses):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            code, data = responses[min(len(received) - 1, len(responses) - 1)]
            self.send_response(code)
            self.send_header('X-Private', SECRET)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/{SECRET}', received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {TOKEN_ENV: SECRET})
        self.env.start()
        self.addCleanup(self.env.stop)
        for state in (stocks.HttpBackedStocksSource._single_flights,
                stocks.HttpBackedStocksSource._request_cache,
                stocks.HttpBackedStocksSource._rate_limit_states):
            state.clear()
        self.request = StocksRequest(snapshot_type='stocks', snapshot_date='2026-11-06', nm_ids=[1])

    def source(self, url, **kwargs):
        return stocks.HttpBackedStocksSource(base_url=url, base_url_env_var='', token_env_var=TOKEN_ENV,
            min_request_interval_seconds=0, total_timeout_seconds=20, timeout_seconds=2, **kwargs)

    def persist_failure(self, source, expected=RuntimeError):
        """Actual run_cycle -> phase failure -> reload both durable stores."""
        with TemporaryDirectory(prefix='stocks-failure-') as tmp:
            root = Path(tmp)
            initialize_admission(root)
            db_path = root / 'journal.sqlite3'
            with closing(sqlite3.connect(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                ensure_warehouse_functional_schema(conn)
                conn.commit()
            journal = WarehouseUpdateJournal(db_path=db_path, runtime_dir=root,
                timestamp_factory=lambda: cycle_fixture.STAMP)
            contract = root / 'contract.json'
            contract.write_text('{}')
            config = CycleHistoryConfig(root / 'history', contract, 'epoch')
            store = CycleReceiptStore(root, lambda: cycle_fixture.STAMP)
            fake = cycle_fixture.CycleFake(root)
            captured = []
            def warehouse(*_args):
                with warehouse_functional_job_lock(root):
                    run_id = journal.start(trigger_source='hourly')
                    journal.phase_started(run_id, PHASE)
                    try:
                        source.fetch(self.request)
                    except Exception as exc:
                        captured.append(exc)
                        journal.phase_finished(run_id, PHASE, status='failed',
                            error=type(exc).__name__, details=phase_error_details(exc, PHASE, run_id))
                        journal.finish(run_id, status='failed', error=type(exc).__name__)
                        raise
                self.fail('fixture unexpectedly succeeded')
            fake._cycle_warehouse = warehouse
            with patch('packages.application.sheet_vitrina_v1_cycle.process_identity',
                    return_value=cycle_fixture.IDENTITY), patch(
                    'packages.application.sheet_vitrina_v1_cycle.ClosedBacklog', cycle_fixture.EmptyClosedFixture):
                receipt, lease = store.accept(request_key='stocks-failure', slot_utc=cycle_fixture.SLOT,
                    config=config, now=cycle_fixture.NOW)
                try:
                    with heavy_admitted(root, operation='cycle'), self.assertRaises(expected) as raised:
                        run_cycle(fake, store, receipt, config, lambda _: None)
                finally:
                    lease.close()
                saved = CycleReceiptStore(root, lambda: cycle_fixture.STAMP).read(receipt['cycle_id'])
            self.assertIs(raised.exception, captured[0])
            status = WarehouseUpdateJournal(db_path=db_path, runtime_dir=root).public_status()
            phase = next(value for value in status['phases'] if value['phase_key'] == PHASE)
            evidence = phase['details']['error_diagnostic']['exceptions']['0']['stocks_failure']
            self.assertEqual(evidence, saved['error_diagnostic']['exceptions'][0]['stocks_failure'])
            self.assertEqual(saved['status'], 'failed')
            self.assertEqual(saved['summary']['publication']['status'], 'not_started')
            self.assertEqual(phase['status'], 'failed')
            self.assertFalse(source._request_cache)
            self.assertFalse(source._single_flights)
            self.assertNotIn(SECRET, json.dumps({'warehouse': status, 'cycle': saved}))
            with closing(sqlite3.connect(db_path)) as conn:
                self.assertNotIn(SECRET, '\n'.join(conn.iterdump()))
            for path in store.root.iterdir():
                if path.is_file():
                    self.assertNotIn(SECRET.encode(), path.read_bytes())
            self.assertLess(len(json.dumps(evidence)), 1024)
            self.assertGreaterEqual(evidence['total_elapsed_ms'] or 0, evidence['attempt_elapsed_ms'] or 0)
            return evidence

    def test_nonretry_http_and_exhausted_http_keep_cause_status_and_actual_sends(self):
        for code, attempts in ((403, 1), (503, 3), (429, 3)):
            with self.subTest(code=code), loopback([(code, SECRET.encode())]) as (url, received):
                evidence = self.persist_failure(self.source(url))
                self.assertEqual(len(received), attempts)
                self.assertEqual(evidence['attempt'], attempts)
                self.assertEqual(evidence['origin_type'], 'RuntimeError')
                self.assertEqual(evidence['cause_type'], '_StocksHttpStatusError')
                self.assertEqual(evidence['root_cause_type'], 'HTTPError')
                self.assertEqual(evidence['http_status'], code)
                self.assertEqual(evidence['reason'], 'http_status')
                self.assertEqual(evidence['stage'], 'request')
                self.assertIsNotNone(evidence['attempt_elapsed_ms'])

    def test_connection_refused_preserves_numeric_transport_reason(self):
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            port = reserved.getsockname()[1]
        evidence = self.persist_failure(self.source(f'http://127.0.0.1:{port}/{SECRET}'))
        self.assertEqual(evidence['attempt'], 1)
        self.assertEqual(evidence['cause_type'], 'URLError')
        self.assertEqual(evidence['transport_reason_type'], 'ConnectionRefusedError')
        self.assertEqual(evidence['errno'], errno.ECONNREFUSED)
        self.assertIsNone(evidence['http_status'])
        self.assertEqual((evidence['stage'], evidence['reason']), ('request', 'transport'))

    def test_exact_aggregate_regions_succeed_through_real_spawn(self):
        for name, region in (('Склад WB', 'Склад WB'), ('Склад WB', 'Россия'),
                             ('Склад WB РФ', 'Россия')):
            row = {**item(), 'warehouseId': -999999, 'warehouseName': name,
                'regionName': region, 'quantity': 19, 'inWayToClient': 4, 'inWayFromClient': 2}
            with self.subTest(name=name, region=region), loopback([(200, body([row]))]) as (url, received):
                source = self.source(url)
                payload = source.fetch(self.request)
                data = payload['data']
                self.assertEqual(len(received), 1)
                self.assertEqual(data['raw_rows'], [row])
                self.assertFalse(data['warehouse_granularity_complete'])
                self.assertEqual(data['normalization']['aggregate_sentinel_row_count'], 1)
                normalized = data['rows'][0]
                self.assertEqual((normalized['warehouseId'], normalized['warehouseName']), (0, 'Остальные'))
                self.assertEqual((normalized['sourceWarehouseId'], normalized['sourceRegionName']), (-999999, region))
                self.assertEqual(normalized['sourceWarehouseName'], name)
                self.assertEqual(normalized['normalizationReason'], 'exact_wb_aggregate_sentinel')
                result = transform_legacy_payload(payload).result
                self.assertEqual(result.kind, 'success')
                self.assertFalse(result.warehouse_granularity_complete)
                self.assertEqual(result.raw_rows_digest, data['raw_rows_digest'])
                stock = result.items[0]
                self.assertEqual((stock.stock_total, stock.in_way_to_client,
                    stock.in_way_from_client, stock.wb_contour_total), (19, 4, 2, 25))
                self.assertFalse(source._single_flights)

    def test_aggregate_aliases_cannot_double_count_one_size(self):
        current = {**item(), 'warehouseId': -999999, 'warehouseName': 'Склад WB РФ',
                   'regionName': 'Россия'}
        for region in ('Склад WB', 'Россия'):
            old = {**current, 'warehouseName': 'Склад WB', 'regionName': region}
            with self.subTest(region=region), loopback([(200, body([old, current]))]) as (url, received):
                evidence = self.persist_failure(self.source(url))
                self.assertEqual(evidence['reason'], 'duplicate_row')
                self.assertEqual(len(received), 1)

    def test_rf_aggregate_acceptance_is_an_exact_tuple(self):
        current = {**item(), 'warehouseId': -999999, 'warehouseName': 'Склад WB РФ',
                   'regionName': 'Россия'}
        variants = [{'warehouseId': '-999999'}, {'warehouseId': -999999.0},
                    {'warehouseId': -999998}, {'warehouseId': True},
                    {'warehouseName': 'Склад WB РФ неизвестный'},
                    {'regionName': 'Склад WB'}, {'regionName': 'Беларусь'}]
        for changed in variants:
            with self.subTest(changed=changed), loopback([(200, body([{**current, **changed}]))]) as (url, received):
                evidence = self.persist_failure(self.source(url))
                self.assertEqual(evidence['reason'], 'invalid_warehouse_id')
                self.assertEqual(len(received), 1)

    def test_decode_and_validation_errors_have_safe_codes_and_no_provider_data(self):
        invalid_row = {**item(), 'warehouseId': SECRET, 'warehouseName': SECRET}
        invalid_aggregate = {**item(), 'warehouseId': -999999, 'warehouseName': 'Склад WB', 'regionName': SECRET}
        cases = [(b'{'+SECRET.encode(), ValueError, 'JSONDecodeError', 'decode', 'invalid_json'),
            (b'\xff'+SECRET.encode(), ValueError, 'UnicodeDecodeError', 'decode', 'invalid_encoding'),
            (json.dumps({'data': {'items': [invalid_row]}}).encode(), RuntimeError,
                '_StocksValidationError', 'validation', 'invalid_warehouse_id'),
            (json.dumps({'data': {'items': [invalid_aggregate]}}).encode(), RuntimeError,
                '_StocksValidationError', 'validation', 'invalid_warehouse_id'),
            (json.dumps({'data': SECRET}).encode(), RuntimeError, '_StocksValidationError', 'validation', 'invalid_data')]
        for data, kind, origin, stage, reason in cases:
            with self.subTest(reason=reason), loopback([(200, data)]) as (url, received):
                evidence = self.persist_failure(self.source(url), kind)
                self.assertEqual(len(received), 1)
                self.assertEqual((evidence['origin_type'], evidence['stage'], evidence['reason']), (origin, stage, reason))
                self.assertEqual(evidence['http_status'], 200)
                self.assertEqual(evidence['attempt'], 1)

    def test_attempt_count_resets_for_second_page(self):
        with loopback([(200, body([item()])), (403, SECRET.encode())]) as (url, received):
            evidence = self.persist_failure(self.source(url, page_limit=1))
            self.assertEqual([value['offset'] for value in received], [0, 1])
            self.assertEqual(evidence['attempt'], 1)

    def test_status_is_recorded_before_error_body_read_fails(self):
        with loopback([(403, SECRET.encode())]) as (url, received):
            evidence = self.persist_failure(self.source(url, max_error_body_bytes=1), stocks.StocksFetchIncompleteError)
            self.assertEqual(len(received), 1)
            self.assertEqual(evidence['attempt'], 1)
            self.assertEqual(evidence['http_status'], 403)
            self.assertEqual((evidence['stage'], evidence['reason']), ('body_read', 'resource_limit'))

    def test_program_error_and_diagnostic_fault_do_not_hide_original_classification(self):
        for worker, reason in ((_program_error_worker, 'program_error'), (_diagnostic_fault_worker, 'program_error')):
            with self.subTest(worker=worker.__name__), loopback([(200, body())] if worker is _program_error_worker else
                    [(403, SECRET.encode())]) as (url, received), patch.object(stocks, '_stocks_inventory_worker', worker):
                evidence = self.persist_failure(self.source(url))
                self.assertEqual(len(received), 1)
                self.assertEqual(evidence['origin_type'], 'KeyError' if worker is _program_error_worker else 'RuntimeError')
                self.assertEqual(evidence['reason'], reason)
                self.assertEqual(evidence['attempt'], 1 if worker is _program_error_worker else None)

    def test_tls_verification_and_absent_verify_code_are_safe(self):
        with patch.object(stocks, '_stocks_inventory_worker', _tls_worker):
            evidence = self.persist_failure(self.source('http://127.0.0.1:1/'+SECRET))
        self.assertEqual(evidence['attempt'], 1)
        self.assertEqual(evidence['transport_reason_type'], 'SSLCertVerificationError')
        self.assertEqual(evidence['tls_verify_code'], 20)
        self.assertEqual(evidence['reason'], 'transport')
        missing = stocks._StocksDeadline(1, clock=lambda: 0).failure(ssl.SSLCertVerificationError(1, SECRET))
        self.assertIsNone(missing['tls_verify_code'])

    def test_wait_before_first_send_is_zero_and_unsent_time_is_unknown(self):
        clock = Clock()
        opener = Opener([], clock)
        source = self.source('https://loopback-unused', opener=opener, monotonic_fn=clock,
            sleep_fn=clock.sleep, jitter_fn=lambda: 0)
        seller_key = stocks._seller_cache_key(base_url='https://loopback-unused', token=SECRET)
        source._mark_next_request_after(seller_key, 100)
        with self.assertRaises(stocks.StocksFetchIncompleteError) as raised:
            source.fetch(self.request)
        evidence = stocks_failure_diagnostic(raised.exception)
        self.assertEqual(evidence['attempt'], 0)
        self.assertIsNone(evidence['attempt_elapsed_ms'])
        self.assertEqual(evidence['stage'], 'rate_limit')
        self.assertEqual(evidence['reason'], 'deadline_exhausted')
        self.assertEqual(opener.calls, [])

    def test_exact_allowlist_rejects_unknown_values_and_untyped_attributes(self):
        raw = {key: SECRET for key in ('origin_type', 'cause_type', 'root_cause_type', 'transport_reason_type',
            'stage', 'reason', 'http_status', 'errno', 'tls_verify_code', 'attempt', 'attempt_elapsed_ms', 'total_elapsed_ms')}
        raw.update(body=SECRET, url=SECRET, headers={'Authorization': SECRET}, arbitrary=SECRET)
        cleaned = sanitize_stocks_failure(raw)
        self.assertNotIn(SECRET, json.dumps(cleaned))
        exc = RuntimeError(SECRET)
        exc._stocks_failure = raw
        self.assertNotIn('stocks_failure', cycle_error_diagnostic(exc, {})['exceptions'][0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
