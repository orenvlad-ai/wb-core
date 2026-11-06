"""Deterministic resilience fixtures plus real spawn/urllib deadline checks."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import multiprocessing
from multiprocessing.reduction import ForkingPickler
import os
from pathlib import Path
import ssl
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib import error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import packages.adapters.stocks_block as stocks
from packages.contracts.stocks_block import StocksRequest

TOKEN_ENV = "WB_STOCKS_RESILIENCE_SMOKE_TOKEN"
REAL_WORKER = stocks._stocks_inventory_worker


def _slow_parse_worker(connection, config, kwargs, expires_at):
    original = stocks.json.loads

    def slow_parse(*args, **kw):
        time.sleep(5)
        return original(*args, **kw)

    stocks.json.loads = slow_parse
    REAL_WORKER(connection, config, kwargs, expires_at)


def _crash_worker(connection, config, kwargs, expires_at):
    os._exit(7)


def _result_fixture_worker(connection, config, kwargs, expires_at):
    deadline = stocks._StocksDeadline(expires_at)
    stock = item()
    if kwargs.get("_large_record"):
        stock["warehouseName"] = "Я" * 4000
    records = [
        {"type": "header", "snapshot_date": "2026-11-06", "data": {"pagination_complete": True, "raw_row_count": 1, "page_count": 1, "batch_count": 1, "normalization": {"normalized_row_count": 1}}},
        {"type": "raw_rows", "item": stock},
        {"type": "rows", "item": stock},
        {"type": "offset", "offset": 0},
        {"type": "complete"},
    ]
    try:
        for record in records:
            stocks._stocks_ipc_send(connection, record, deadline)
    finally:
        connection.close()


def _large_result_worker(connection, config, kwargs, expires_at):
    kwargs["_large_record"] = True
    _result_fixture_worker(connection, config, kwargs, expires_at)


def _partial_record_worker(connection, config, kwargs, expires_at):
    connection.send(b'C{"type":')
    time.sleep(5)


def _oversize_frame_worker(connection, config, kwargs, expires_at):
    connection.send(b"F" + b" " * stocks._STOCKS_IPC_FRAME_BYTES)
    time.sleep(5)


def _oversize_record_worker(connection, config, kwargs, expires_at):
    for _ in range(20):
        connection.send(b"C" + b" " * (stocks._STOCKS_IPC_FRAME_BYTES - 1))
        connection.recv(2)
    time.sleep(5)


def item(chrt=10):
    return dict(nmId=1, chrtId=chrt, warehouseId=1, warehouseName="Test", regionName="Test", quantity=1, inWayToClient=0, inWayFromClient=0)


def body(items=None):
    return json.dumps({"data": {"items": items or []}}).encode()


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self, responses, clock):
        self.responses = iter(responses)
        self.clock = clock
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((self.clock(), json.loads(request.data), timeout))
        outcome = next(self.responses)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome if hasattr(outcome, "read") else Response(outcome)


def status(code, headers=None, response=None):
    return error.HTTPError("http://fixture", code, "fixture", headers or {}, response or Response(b"fixture"))


@contextmanager
def server(*, status_code=200, trickle=False):
    entered = threading.Event()
    disconnected = threading.Event()
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status_code.pop(0) if isinstance(status_code, list) else status_code)
            data = body()
            if not trickle:
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            entered.set()
            try:
                if trickle:
                    # Each byte arrives well inside the 2s socket timeout;
                    # a socket-only deadline would never stop this stream.
                    while not stop.is_set():
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        stop.wait(0.03)
                else:
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", entered, disconnected
    finally:
        stop.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(1)


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {TOKEN_ENV: "fixture-token"})
        self.env.start()
        with stocks.HttpBackedStocksSource._shared_lock:
            stocks.HttpBackedStocksSource._single_flights.clear()
            stocks.HttpBackedStocksSource._request_cache.clear()
            stocks.HttpBackedStocksSource._rate_limit_states.clear()
        self.request = StocksRequest(snapshot_type="stocks", snapshot_date="2026-11-06", nm_ids=[1])
        self.clock = Clock()
        self.source_serial = 0

    def tearDown(self):
        self.assertFalse(stocks.HttpBackedStocksSource._single_flights)
        self.env.stop()

    def source(self, responses, **kwargs):
        opener = Opener(responses, self.clock)
        self.source_serial += 1
        config = dict(base_url=f"https://fixture-{self.source_serial}", token_env_var=TOKEN_ENV, base_url_env_var="", opener=opener, monotonic_fn=self.clock, sleep_fn=self.clock.sleep, jitter_fn=lambda: 0, min_request_interval_seconds=0, total_timeout_seconds=120)
        config.update(kwargs)
        return stocks.HttpBackedStocksSource(**config), opener

    def assert_incomplete(self, source):
        with self.assertRaises(stocks.StocksFetchIncompleteError):
            source.fetch(self.request)
        self.assertFalse(source._request_cache)

    def test_500_success_preserves_request_and_reserves_every_send(self):
        source, opener = self.source([status(500), status(502), body()], min_request_interval_seconds=20)
        self.assertTrue(source.fetch(self.request)["data"]["pagination_complete"])
        self.assertEqual([call[0] for call in opener.calls], [0, 20, 40])
        self.assertEqual([call[1] for call in opener.calls], [dict(nmIds=[1], chrtIds=[], limit=250000, offset=0)] * 3)

    def test_complete_pagination_and_remaining_socket_timeout(self):
        source, opener = self.source([body([item()]), body()], page_limit=1, min_request_interval_seconds=20, total_timeout_seconds=25, timeout_seconds=30)
        result = source.fetch(self.request)
        self.assertEqual(result["data"]["page_offsets"], [0, 1])
        self.assertEqual(result["data"]["raw_rows"], [item()])
        self.assertEqual([call[2] for call in opener.calls], [25, 5])

    def test_aggregate_source_limits_apply_across_pages_before_accumulation(self):
        source, _ = self.source([body([item()]), body([item(20)])], page_limit=1)
        with patch.object(stocks, "_STOCKS_SOURCE_TOTAL_ROWS", 1):
            self.assert_incomplete(source)
        source, _ = self.source([body([item()]), body()], page_limit=1)
        with patch.object(stocks, "_STOCKS_HTTP_TOTAL_BYTES", len(body([item()]))):
            self.assert_incomplete(source)

    def test_concurrent_distinct_keys_reserve_distinct_rate_slots(self):
        source = stocks.HttpBackedStocksSource(token_env_var=TOKEN_ENV, min_request_interval_seconds=0.03)
        deadline = stocks._StocksDeadline(time.monotonic() + 1)
        barrier = threading.Barrier(3)
        grants = []

        def reserve():
            barrier.wait()
            source._wait_for_request_slot("shared-seller", deadline=deadline)
            grants.append(time.monotonic())

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(1)
        self.assertEqual(len(grants), 2)
        self.assertGreaterEqual(max(grants) - min(grants), 0.025)

    def test_cancelled_wait_wakes_promptly(self):
        source = stocks.HttpBackedStocksSource(token_env_var=TOKEN_ENV)
        source._mark_next_request_after("cancelled-seller", 60)
        cancelled = threading.Event()
        failures = []

        def reserve():
            try:
                source._wait_for_request_slot("cancelled-seller", deadline=stocks._StocksDeadline(time.monotonic() + 120), cancelled=cancelled)
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=reserve)
        thread.start()
        cancelled.set()
        thread.join(0.5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(failures[0], stocks.StocksFetchIncompleteError)

    def test_combined_retry_limit_and_final_429_cooldown(self):
        source, opener = self.source([status(429, {"Retry-After": "5"}), status(500), status(429, {"Retry-After": "9"}), body()])
        with self.assertRaisesRegex(RuntimeError, "status 429"):
            source.fetch(self.request)
        self.assertEqual(len(opener.calls), 3)
        self.assertFalse(source._request_cache)
        # Final 429 still applies to the next fetch, even after exhaustion.
        source.fetch(self.request)
        self.assertEqual(opener.calls[3][0], 16)

    def test_transport_timeout_reset_recover_tls_config_auth_do_not_retry(self):
        for exc in [error.URLError(TimeoutError()), ConnectionResetError()]:
            source, opener = self.source([exc, body()], reuse_ttl_seconds=0)
            source.fetch(self.request)
            self.assertEqual(len(opener.calls), 2)
        for exc in [error.URLError(ssl.SSLError("fixture")), error.URLError("bad config"), status(401), status(403), status(400)]:
            source, opener = self.source([exc, body()], reuse_ttl_seconds=0)
            with self.assertRaises(RuntimeError):
                source.fetch(self.request)
            self.assertEqual(len(opener.calls), 1)

    def test_wait_that_cannot_fit_fails_without_send(self):
        source, opener = self.source([status(429, {"Retry-After": "50"}), body()], total_timeout_seconds=10)
        self.assert_incomplete(source)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.clock(), 0)

    def test_500_to_budget_and_last_page_timeout_publish_nothing(self):
        source, opener = self.source([status(500), status(500), body()], total_timeout_seconds=2)
        self.assert_incomplete(source)
        self.assertEqual(len(opener.calls), 2)
        self.clock.now = 0
        source, opener = self.source([body([item()]), error.URLError(TimeoutError()), body()], page_limit=1, total_timeout_seconds=1)
        self.assert_incomplete(source)
        self.assertEqual([call[1]["offset"] for call in opener.calls], [0, 1])

    def test_body_size_and_malformed_payload_not_retried(self):
        for response, options in [(body([item()]), {"max_success_body_bytes": 5}), (status(500), {"max_error_body_bytes": 2}), (b"not json", {}), (b'{"data":{}}', {}), (body([item(), item(20)]), {"page_limit": 1})]:
            source, opener = self.source([response, body()], **options)
            with self.assertRaises((RuntimeError, ValueError)):
                source.fetch(self.request)
            self.assertEqual(len(opener.calls), 1)
            self.assertFalse(source._request_cache)

    def test_last_chunk_parse_validation_and_cache_boundary_expiry(self):
        clock = self.clock

        class LastChunk(Response):
            def read1(self, size=-1):
                result = super().read1(size)
                clock.now = 10
                return result

        source, _ = self.source([LastChunk(body())], total_timeout_seconds=10)
        self.assert_incomplete(source)
        self.clock.now = 0
        source, _ = self.source([body()], total_timeout_seconds=10)
        real_loads = json.loads

        def late_parse(*args, **kwargs):
            result = real_loads(*args, **kwargs)
            if isinstance(args[0], str):
                clock.now = 10
            return result

        with patch.object(stocks.json, "loads", late_parse):
            self.assert_incomplete(source)
        self.clock.now = 0
        source, _ = self.source([body([item()])], total_timeout_seconds=10)
        validate = source._validate_official_item

        def late_validate(*args, **kwargs):
            result = validate(*args, **kwargs)
            clock.now = 10
            return result

        source._validate_official_item = late_validate
        self.assert_incomplete(source)
        self.clock.now = 0
        source, _ = self.source([body()], total_timeout_seconds=10)
        store = source._store_cached_payload

        def late_store(*args, **kwargs):
            clock.now = 10
            return store(*args, **kwargs)

        source._store_cached_payload = late_store
        self.assert_incomplete(source)

    def test_duplicate_pages_remain_incomplete(self):
        source, opener = self.source([body([item()]), body([item()])], page_limit=1)
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            source.fetch(self.request)
        self.assertEqual(len(opener.calls), 2)
        self.assertFalse(source._request_cache)

    def test_leader_interruption_releases_waiters_with_error(self):
        source, _ = self.source([KeyboardInterrupt()])
        flights = []
        enter = source._enter_single_flight

        def capture(key):
            result = enter(key)
            flights.append(result[1])
            return result

        source._enter_single_flight = capture
        with self.assertRaises(KeyboardInterrupt):
            source.fetch(self.request)
        self.assertTrue(flights[0].event.is_set())
        self.assertIsInstance(flights[0].error, stocks.StocksFetchIncompleteError)
        self.assertFalse(source._request_cache)

    def test_follower_rejects_mismatched_result_identity(self):
        source, _ = self.source([])
        flight = stocks._StocksSingleFlight(payload={"data": {"pagination_complete": True}}, request_key=("wrong-seller", "2026-11-06", (1,)))
        flight.event.set()
        source._enter_single_flight = lambda _key: (False, flight)
        with self.assertRaisesRegex(RuntimeError, "without payload"):
            source.fetch(self.request)

    def real_source(self, url, budget=0.8):
        return stocks.HttpBackedStocksSource(base_url=url, base_url_env_var="", token_env_var=TOKEN_ENV, timeout_seconds=2, min_request_interval_seconds=0, total_timeout_seconds=budget)

    def test_real_trickle_200_and_500_cancel_and_reap_worker(self):
        before = {child.pid for child in multiprocessing.active_children()}
        for code in [200, 500]:
            with server(status_code=code, trickle=True) as (url, entered, disconnected):
                source = self.real_source(url)
                started = time.monotonic()
                self.assert_incomplete(source)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertTrue(entered.is_set())
                self.assertTrue(disconnected.wait(0.5), "terminated worker left network stream alive")
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)

    def test_real_parse_is_cancellable(self):
        with server() as (url, _, _):
            source = self.real_source(url)
            started = time.monotonic()
            with patch.object(stocks, "_stocks_inventory_worker", _slow_parse_worker):
                self.assert_incomplete(source)
            self.assertLess(time.monotonic() - started, 1.5)

    def test_real_500_retry_parent_slot_protocol_and_complete_result(self):
        codes = [500, 200]
        with server(status_code=codes) as (url, _, _):
            result = self.real_source(url, budget=4).fetch(self.request)
            self.assertTrue(result["data"]["pagination_complete"])
            self.assertEqual(codes, [])

    def test_worker_crash_has_explicit_incomplete_outcome(self):
        source = self.real_source("http://127.0.0.1:1")
        with patch.object(stocks, "_stocks_inventory_worker", _crash_worker):
            self.assert_incomplete(source)

    def test_result_does_not_unpickle_or_create_receiver_threads(self):
        source = self.real_source("http://127.0.0.1:1", budget=0.4)
        before = {thread.ident for thread in threading.enumerate()}
        real_loads = ForkingPickler.loads

        def slow_unpickle(*args, **kwargs):
            # Regression for the former unkillable daemon relay.
            time.sleep(2)
            return real_loads(*args, **kwargs)

        started = time.monotonic()
        with patch.object(ForkingPickler, "loads", slow_unpickle), patch.object(stocks, "_stocks_inventory_worker", _result_fixture_worker):
            result = source.fetch(self.request)
        self.assertTrue(result["data"]["pagination_complete"])
        self.assertLess(time.monotonic() - started, 0.7)
        self.assertEqual({thread.ident for thread in threading.enumerate()}, before)

    def test_multiframe_unicode_record_reconstructs_exactly(self):
        source = self.real_source("http://127.0.0.1:1")
        with patch.object(stocks, "_stocks_inventory_worker", _large_result_worker):
            result = source.fetch(self.request)
        self.assertEqual(result["data"]["raw_rows"][0]["warehouseName"], "Я" * 4000)
        self.assertEqual(result["data"]["rows"][0]["warehouseName"], "Я" * 4000)

    def test_incomplete_frames_and_aggregate_caps_leave_no_receiver_thread(self):
        before = {thread.ident for thread in threading.enumerate()}
        children = {child.pid for child in multiprocessing.active_children()}
        for worker, limit_patch in [
            (_partial_record_worker, {}),
            (_oversize_frame_worker, {}),
            (_oversize_record_worker, {}),
            (_result_fixture_worker, {"_STOCKS_IPC_RESULT_BYTES": 128}),
            (_result_fixture_worker, {"_STOCKS_IPC_RESULT_ITEMS": 1}),
        ]:
            source = self.real_source("http://127.0.0.1:1", budget=0.3)
            with patch.object(stocks, "_stocks_inventory_worker", worker), (patch.multiple(stocks, **limit_patch) if limit_patch else nullcontext()):
                self.assert_incomplete(source)
            self.assertEqual({thread.ident for thread in threading.enumerate()}, before)
            self.assertEqual({child.pid for child in multiprocessing.active_children()}, children)

    def test_late_bounded_parent_decode_rejects_result_and_leaves_no_thread(self):
        source = self.real_source("http://127.0.0.1:1", budget=0.3)
        before = {thread.ident for thread in threading.enumerate()}
        original_loads = json.loads
        decoded_sizes = []

        def late_decode(encoded, *args, **kwargs):
            if isinstance(encoded, bytearray):
                decoded_sizes.append(len(encoded))
                time.sleep(0.4)
            return original_loads(encoded, *args, **kwargs)

        with patch.object(stocks.json, "loads", late_decode), patch.object(stocks, "_stocks_inventory_worker", _result_fixture_worker):
            self.assert_incomplete(source)
        self.assertTrue(decoded_sizes)
        self.assertLessEqual(max(decoded_sizes), stocks._STOCKS_IPC_RECORD_BYTES)
        self.assertEqual({thread.ident for thread in threading.enumerate()}, before)

    def test_expired_follower_does_not_cancel_leader_and_inflight_is_cleared(self):
        with server(trickle=True) as (url, entered, _):
            leader = self.real_source(url, budget=1.2)
            follower = self.real_source(url, budget=0.15)
            failures = []

            def run():
                try:
                    leader.fetch(self.request)
                except Exception as exc:
                    failures.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assert_incomplete(follower)
            self.assertTrue(thread.is_alive())
            self.assertEqual(len(leader._single_flights), 1)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], stocks.StocksFetchIncompleteError)
            self.assertFalse(leader._request_cache)

    def test_success_follower_has_explicit_payload_even_with_zero_cache_ttl(self):
        entered, release = threading.Event(), threading.Event()

        def opener(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(1))
            return Response(body())

        source = stocks.HttpBackedStocksSource(token_env_var=TOKEN_ENV, opener=opener, min_request_interval_seconds=0, reuse_ttl_seconds=0)
        outcomes = []
        threads = [threading.Thread(target=lambda: outcomes.append(source.fetch(self.request))) for _ in range(2)]
        threads[0].start()
        self.assertTrue(entered.wait(1))
        threads[1].start()
        time.sleep(0.05)
        release.set()
        for thread in threads:
            thread.join(1)
        self.assertEqual(len(outcomes), 2)
        self.assertIs(outcomes[0], outcomes[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
