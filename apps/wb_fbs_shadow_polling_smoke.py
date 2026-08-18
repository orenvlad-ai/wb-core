"""Stage 7B cadence, transition, concurrency and readiness checks."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
import random
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
from unittest import mock
from urllib import error


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.official_api_rate_budget import (  # noqa: E402
    FileBackedOfficialApiRateBudget,
)
from packages.adapters.wb_fbs_orders import (  # noqa: E402
    HttpBackedWbFbsOrdersSource,
    WbFbsOrderStatus,
    WbFbsOrdersPage,
    WbFbsOrdersTransportError,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    POLL_RUNS_TABLE,
    STATUS_CURRENT_TABLE,
    STATUS_TRANSITIONS_TABLE,
    WbFbsOrdersCollector,
)
from packages.application.wb_fbs_shadow_polling import (  # noqa: E402
    FBS_LIFECYCLE_BATCH_LIMIT,
    LOCK_FILENAME,
    WbFbsShadowPollingService,
    build_readiness_report,
    fbs_shadow_poll_lock,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)


class _Clock:
    def __init__(self, epoch: int = 1_786_608_000) -> None:
        self.epoch = epoch
        self.monotonic = 1000.0

    def timestamp(self) -> str:
        return datetime.fromtimestamp(self.epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    def unix(self) -> float:
        return float(self.epoch)

    def monotonic_now(self) -> float:
        self.monotonic += 0.001
        return self.monotonic

    def advance(self, seconds: int = 300) -> None:
        self.epoch += seconds


class _PagedSource:
    def __init__(self, *, order_count: int = 3) -> None:
        self.order_count = order_count
        self.wb_status = "waiting"
        self.order_cursors: list[int] = []
        self.status_calls = 0
        self.telemetry = {
            "request_count": 0,
            "retry_count": 0,
            "rate_limited_count": 0,
            "server_error_count": 0,
            "transport_error_count": 0,
            "rate_budget_wait_ms": 0,
            "retry_wait_ms": 0,
        }

    def reset_telemetry(self) -> None:
        for key in self.telemetry:
            self.telemetry[key] = 0

    def telemetry_snapshot(self) -> dict[str, int]:
        return dict(self.telemetry)

    def list_orders(
        self,
        *,
        limit: int,
        next_cursor: int,
        date_from: int | None,
        date_to: int | None,
    ) -> WbFbsOrdersPage:
        self.telemetry["request_count"] += 1
        self.order_cursors.append(next_cursor)
        if next_cursor == 0:
            rows = [_order(10_000 + item) for item in range(self.order_count)]
            next_value = 700 if self.order_count > 3 else 0
        elif next_cursor == 700:
            rows = [_order(20_000 + item) for item in range(2)]
            next_value = 0
        else:
            raise AssertionError(f"unexpected cursor {next_cursor}")
        return WbFbsOrdersPage(
            orders=rows,
            next_cursor=next_value,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
        )

    def list_statuses(self, order_ids: list[int]) -> list[WbFbsOrderStatus]:
        self.telemetry["request_count"] += 1
        self.status_calls += 1
        return [
            WbFbsOrderStatus(
                order_id=order_id,
                supplier_status="complete",
                wb_status=self.wb_status,
            )
            for order_id in order_ids
        ]


def main() -> None:
    _rate_budget_concurrency()
    _http_retry_and_duplicate_contract()
    _failed_cycle_observability()
    _poll_resume_single_flight_and_readiness()
    _transition_sequence_property()
    print("wb_fbs_shadow_polling_smoke: OK")


class _Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class _Budget:
    def __init__(self) -> None:
        self.acquires = 0
        self.defers: list[float] = []

    def acquire(self) -> dict[str, float]:
        self.acquires += 1
        return {"wait_seconds": 0.0}

    def defer(self, seconds: float) -> None:
        self.defers.append(float(seconds))


def _http_retry_and_duplicate_contract() -> None:
    budget = _Budget()
    calls = 0

    def retrying_opener(request: object, *, timeout: float) -> _Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error.HTTPError(
                request.full_url,
                429,
                "rate limited",
                {"Content-Type": "application/json", "Retry-After": "0.05"},
                io.BytesIO(b'{"error":"bounded"}'),
            )
        return _Response({"next": 0, "orders": []})

    prior_token = os.environ.get("WB_API_TOKEN")
    prior_base = os.environ.get("WB_FBS_API_BASE_URL")
    os.environ["WB_API_TOKEN"] = "test-only-token"
    os.environ["WB_FBS_API_BASE_URL"] = "https://fbs.test"
    try:
        source = HttpBackedWbFbsOrdersSource(
            opener=retrying_opener,
            rate_budget=budget,
            max_retries=1,
            random_fn=lambda: 0.0,
        )
        page = source.list_orders(limit=1, next_cursor=0, date_from=1, date_to=2)
        assert page.next_cursor == 0 and calls == 2 and budget.acquires == 2
        assert budget.defers == [0.5]  # Exponential minimum is intentionally conservative.
        telemetry = source.telemetry_snapshot()
        assert telemetry["retry_count"] == 1 and telemetry["rate_limited_count"] == 1

        duplicate_source = HttpBackedWbFbsOrdersSource(
            opener=lambda _request, timeout: _Response(
                {
                    "orders": [
                        {"id": 42, "supplierStatus": "complete", "wbStatus": "waiting"},
                        {"id": 42, "supplierStatus": "complete", "wbStatus": "sorted"},
                    ]
                }
            ),
            max_retries=0,
        )
        try:
            duplicate_source.list_statuses([42])
            raise AssertionError("duplicate official status rows must fail closed")
        except WbFbsOrdersTransportError as exc:
            assert "duplicated" in str(exc)
    finally:
        _restore_env("WB_API_TOKEN", prior_token)
        _restore_env("WB_FBS_API_BASE_URL", prior_base)


def _rate_budget_concurrency() -> None:
    with TemporaryDirectory(prefix="fbs-rate-budget-") as directory:
        root = Path(directory)
        budget = FileBackedOfficialApiRateBudget(
            runtime_dir=root,
            family="wb_fbs_orders",
            min_interval_seconds=0.001,
        )
        reservations: list[float] = []
        guard = threading.Lock()

        def reserve() -> None:
            result = budget.acquire()
            with guard:
                reservations.append(float(result["reserved_at_epoch"]))

        threads = [threading.Thread(target=reserve) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(reservations) == 12
        ordered = sorted(reservations)
        assert all(ordered[index] > ordered[index - 1] for index in range(1, len(ordered)))
        payload = json.loads((root / "official_api_rate_budgets" / "wb_fbs_orders.json").read_text(encoding="utf-8"))
        assert payload["reservation_sequence"] == 12
        assert not set(payload).intersection({"token", "authorization", "cookie", "raw_payload"})


def _failed_cycle_observability() -> None:
    class FailsAfterFirst(_PagedSource):
        def list_orders(self, **kwargs):
            if int(kwargs["next_cursor"]) == 700:
                self.telemetry["transport_error_count"] += 1
                raise RuntimeError("simulated bounded second-page failure")
            return super().list_orders(**kwargs)

    with TemporaryDirectory(prefix="fbs-shadow-failed-cycle-") as directory:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(directory) / "runtime")
        runtime.list_wb_supplies()
        clock = _Clock()
        service = _service(runtime, clock, FailsAfterFirst(order_count=5), max_pages=10)
        try:
            service.poll_once()
            raise AssertionError("second-page failure must remain visible")
        except RuntimeError as exc:
            assert "second-page" in str(exc)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT * FROM {POLL_RUNS_TABLE} ORDER BY run_sequence DESC LIMIT 1"
            ).fetchone()
            assert row["status"] == "failed"
            assert row["page_count"] == 1 and row["next_cursor"] == 700
            assert row["transport_error_count"] == 1


def _poll_resume_single_flight_and_readiness() -> None:
    with TemporaryDirectory(prefix="fbs-shadow-poll-") as directory:
        root = Path(directory)
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=root / "runtime")
        runtime.list_wb_supplies()
        clock = _Clock()
        source = _PagedSource(order_count=5)
        disabled = WbFbsShadowPollingService(
            runtime_dir=runtime.runtime_dir,
            db_path=runtime.db_path,
            source=source,
            timestamp_factory=clock.timestamp,
            unix_time_factory=clock.unix,
            monotonic_factory=clock.monotonic_now,
            enabled=False,
        ).poll_once()
        assert disabled["status"] == "disabled" and source.order_cursors == []
        service = _service(runtime, clock, source, max_pages=1)

        # Collection remains independent, but the lifecycle suffix never
        # overlaps the short warehouse confirm/publication window.  The timer
        # reports a held drain instead of consuming progress; its next normal
        # cycle can therefore resume from the unchanged durable sequence.
        writer_entered = threading.Event()
        writer_release = threading.Event()

        def hold_warehouse_writer() -> None:
            with warehouse_functional_write_lock(runtime.runtime_dir):
                writer_entered.set()
                assert writer_release.wait(timeout=5)

        writer = threading.Thread(target=hold_warehouse_writer)
        writer.start()
        assert writer_entered.wait(timeout=5)
        held_lifecycle = service._process_lifecycle_after_poll()
        assert held_lifecycle == {
            "status": "held",
            "reason": "warehouse_functional_writer_active",
            "mutates_wb": False,
        }
        writer_release.set()
        writer.join(timeout=5)
        assert not writer.is_alive()

        # Catch up a production-shaped suffix promptly without taking the
        # domain primitive's 100k maximum in one warehouse transaction.
        with mock.patch(
            "packages.application.wb_fbs_shadow_polling.process_post_t_fbs_lifecycle"
        ) as lifecycle_processor:
            lifecycle_processor.return_value = {
                "status": "caught_up",
                "mutates_wb": False,
            }
            assert service._process_lifecycle_after_poll()["status"] == "caught_up"
            lifecycle_processor.assert_called_once()
            assert (
                lifecycle_processor.call_args.kwargs["limit"]
                == FBS_LIFECYCLE_BATCH_LIMIT
                == 10_000
            )

        first = service.poll_once()
        assert first["status"] == "bounded_partial"
        assert first["next_cursor"] == 700 and source.order_cursors == [0]
        clock.advance()
        second = service.poll_once()
        assert second["status"] == "success"
        assert second["start_cursor"] == 700 and source.order_cursors[-1] == 700

        source.order_count = 3
        source.wb_status = "waiting"
        clock.advance()
        waiting = service.poll_once()
        assert waiting["status"] == "success"
        source.wb_status = "sorted"
        clock.advance()
        sorted_result = service.poll_once()
        assert sorted_result["transition_count"] == 3
        clock.advance()
        stable = service.poll_once()
        assert stable["transition_count"] == 0

        calls_before_busy = len(source.order_cursors)
        with fbs_shadow_poll_lock(runtime.runtime_dir):
            busy = service.poll_once()
        assert busy["status"] == "single_flight_skipped"
        assert len(source.order_cursors) == calls_before_busy
        assert (runtime.runtime_dir / LOCK_FILENAME).stat().st_mode & 0o777 == 0o600

        with sqlite3.connect(runtime.db_path) as conn:
            transition_count = int(conn.execute(f"SELECT COUNT(*) FROM {STATUS_TRANSITIONS_TABLE}").fetchone()[0])
            assert transition_count == 3
            assert conn.execute(
                f"SELECT COUNT(*) FROM {STATUS_TRANSITIONS_TABLE} WHERE source_timestamp_available<>0 OR previous_source_observed_at<>'' OR current_source_observed_at<>''"
            ).fetchone()[0] == 0
            assert conn.execute(f"SELECT COUNT(*) FROM {STATUS_CURRENT_TABLE}").fetchone()[0] == 7
            before = conn.total_changes
        report = build_readiness_report(
            db_path=runtime.db_path,
            runtime_dir=runtime.runtime_dir,
            now_unix=clock.epoch,
        )
        assert report["query_only"] is True
        handoff = report["transition_evidence"]["complete_waiting_to_complete_sorted"]
        assert handoff["distinct_order_count"] == 3
        assert handoff["candidate_evidence_sufficient"] is True
        assert report["physical_handoff"]["supplier_status_complete_debit_trigger"] is False
        assert report["physical_handoff"]["automatic_trigger_selected"] is False
        assert report["portal_lane_diagnostics"]["seller_portal_scraped"] is False
        assert report["go_no_go"] == "NO_GO"  # No fixture mappings/opening acceptance decision.
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.total_changes == before == 0
            try:
                conn.execute(f"UPDATE {STATUS_TRANSITIONS_TABLE} SET current_wb_status='x'")
                raise AssertionError("transition evidence must be immutable")
            except sqlite3.IntegrityError:
                pass
            try:
                conn.execute(f"DELETE FROM {POLL_RUNS_TABLE}")
                raise AssertionError("poll-run evidence must be append-only")
            except sqlite3.IntegrityError:
                pass


def _transition_sequence_property() -> None:
    with TemporaryDirectory(prefix="fbs-transition-property-") as directory:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(directory) / "runtime")
        runtime.list_wb_supplies()
        clock = _Clock()
        rng = random.Random(137)
        pairs = [
            ("new", "waiting"),
            ("confirm", "waiting"),
            ("complete", "waiting"),
            ("complete", "sorted"),
        ]
        sequence = [pairs[0]] + [rng.choice(pairs) for _ in range(79)]
        source = _SequenceSource(sequence)
        expected_transitions = sum(
            sequence[index] != sequence[index - 1] for index in range(1, len(sequence))
        )
        for _pair in sequence:
            WbFbsOrdersCollector(
                db_path=runtime.db_path,
                timestamp_factory=clock.timestamp,
                unix_time_factory=clock.unix,
                source=source,
                enabled=True,
            ).collect_default_window()
            clock.advance(300)
        with sqlite3.connect(runtime.db_path) as conn:
            actual = int(conn.execute(f"SELECT COUNT(*) FROM {STATUS_TRANSITIONS_TABLE}").fetchone()[0])
            episodes = int(conn.execute(f"SELECT episode_sequence FROM {STATUS_CURRENT_TABLE} WHERE order_id=99001").fetchone()[0])
            assert actual == expected_transitions
            assert episodes == expected_transitions + 1
            assert conn.execute(
                f"SELECT COUNT(*) FROM {STATUS_TRANSITIONS_TABLE} WHERE transition_id<>('fbs_transition_' || substr(transition_digest,8,32))"
            ).fetchone()[0] == 0


class _SequenceSource:
    def __init__(self, sequence: list[tuple[str, str]]) -> None:
        self.sequence = iter(sequence)
        self.current = ("", "")

    def list_orders(self, *, limit: int, next_cursor: int, date_from: int | None, date_to: int | None) -> WbFbsOrdersPage:
        self.current = next(self.sequence)
        return WbFbsOrdersPage(
            orders=[_order(99001)], next_cursor=0, limit=limit,
            date_from=date_from, date_to=date_to,
        )

    def list_statuses(self, order_ids: list[int]) -> list[WbFbsOrderStatus]:
        return [WbFbsOrderStatus(order_id=99001, supplier_status=self.current[0], wb_status=self.current[1])]


def _service(
    runtime: RegistryUploadDbBackedRuntime,
    clock: _Clock,
    source: _PagedSource,
    *,
    max_pages: int,
) -> WbFbsShadowPollingService:
    return WbFbsShadowPollingService(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
        source=source,
        timestamp_factory=clock.timestamp,
        unix_time_factory=clock.unix,
        monotonic_factory=clock.monotonic_now,
        enabled=True,
        max_pages_per_cycle=max_pages,
    )


def _order(order_id: int) -> dict[str, object]:
    return {
        "id": order_id,
        "supplyId": "WB-GI-9001",
        "deliveryType": "fbs",
        "createdAt": "2026-08-13T12:00:00Z",
        "warehouseId": 507,
        "officeId": 123,
        "nmId": 140557512,
        "chrtId": 987654321,
        "article": "SKU-140557512",
        "skus": ["0001234567890"],
        "cargoType": 1,
        "crossBorderType": 0,
        "isZeroOrder": False,
        "address": {"fullAddress": "must not persist"},
        "comment": "must not persist",
    }


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    main()
