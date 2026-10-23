"""Focused regression smoke for re-fetching only the calendar-yesterday web sources."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.web_source_current_sync import ClosedDaySourceState, _serving_item_digest  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    TEMPORAL_ROLE_ACCEPTED_CLOSED,
    TEMPORAL_SLOT_YESTERDAY_CLOSED,
    SheetVitrinaV1LivePlanBlock,
)
from packages.contracts.seller_funnel_snapshot_block import (  # noqa: E402
    SellerFunnelSnapshotItem,
    SellerFunnelSnapshotSuccess,
)
from packages.contracts.web_source_snapshot_block import (  # noqa: E402
    WebSourceSnapshotItem,
    WebSourceSnapshotSuccess,
)


YESTERDAY = "2026-10-22"
OLDER_DAY = "2026-10-21"
NOW = datetime.fromisoformat("2026-10-23T02:00:00+05:00")
REQUESTED = [101, 202]
SOURCES = ("seller_funnel_snapshot", "web_source_snapshot")


def main() -> None:
    for source_key in SOURCES:
        _assert_yesterday_refresh_and_fail_closed(source_key)
        _assert_older_day_and_stocks_unchanged(source_key)
    print("yesterday strict-source refetch, rejected-candidate preservation, older-history and stocks: ok")


def _assert_yesterday_refresh_and_fail_closed(source_key: str) -> None:
    with TemporaryDirectory(prefix="vitrina-yesterday-refetch-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        clock = SimpleNamespace(value=NOW)
        plan = SheetVitrinaV1LivePlanBlock.__new__(SheetVitrinaV1LivePlanBlock)
        plan.runtime = runtime
        plan.now_factory = lambda: clock.value
        sync = _ClosedSync()
        plan.closed_day_web_source_sync = sync
        plan.current_web_source_sync = SimpleNamespace(observed_states={})

        first = _valid_payload(source_key, YESTERDAY, signal=10)
        sync.payload = first
        status, payload = _capture(plan, source_key, YESTERDAY, lambda: first)
        assert status.kind == "success", status
        assert _signal(source_key, payload) == 10

        # The same accepted closed date is still calendar-yesterday. A newer
        # complete observation must replace its accepted value.
        clock.value = datetime.fromisoformat("2026-10-23T03:00:00+05:00")
        newer = _valid_payload(source_key, YESTERDAY, signal=35)
        sync.payload = newer
        calls = []
        status, payload = _capture(plan, source_key, YESTERDAY, lambda: calls.append(1) or newer)
        assert calls == [1], f"yesterday candidate was not fetched again: {source_key}"
        assert status.kind == "success", status
        assert _signal(source_key, payload) == 35
        accepted, _ = runtime.load_temporal_source_slot_snapshot(
            source_key=source_key, snapshot_date=YESTERDAY,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        )
        assert _signal(source_key, accepted) == 35

        # Wrong date, transport error, and a valid-date but unusable empty
        # result must leave the last accepted values intact.
        rejected = (
            ("wrong_date", _valid_payload(source_key, "2026-10-21", signal=99)),
            ("error", RuntimeError("synthetic transport failure")),
            ("completeness_regression", _empty_payload(source_key, YESTERDAY)),
            ("coverage_regression", _partial_payload(source_key, YESTERDAY, signal=45)),
            ("clock_regression", _valid_payload(
                source_key, YESTERDAY, signal=44, fetched_at="2026-10-22T21:00:00Z")),
        )
        expected_signal = 35
        for label, candidate in rejected:
            if not isinstance(candidate, Exception) and label != "clock_regression":
                candidate = replace(candidate, source_fetched_at=_fresh_stamp(clock.value))
            sync.payload = candidate if not isinstance(candidate, Exception) else newer
            def load(candidate=candidate):
                if isinstance(candidate, Exception):
                    raise candidate
                return candidate
            status, payload = _capture(plan, source_key, YESTERDAY, load)
            accepted, _ = runtime.load_temporal_source_slot_snapshot(
                source_key=source_key, snapshot_date=YESTERDAY,
                snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
            )
            assert _signal(source_key, accepted) == expected_signal, (source_key, label, status, accepted)
            assert _signal(source_key, payload) == expected_signal, (source_key, label, status, payload)
            closure = runtime.load_temporal_source_closure_state(
                source_key=source_key, target_date=YESTERDAY,
                slot_kind=TEMPORAL_SLOT_YESTERDAY_CLOSED,
            )
            assert (closure is not None and closure.state not in {"success", "closure_exhausted"}
                    and closure.next_retry_at), (source_key, label, closure)

            # A fresh cycle inside the saved backoff must preserve the last
            # accepted snapshot without contacting the source.
            calls_inside_backoff = []
            status, payload = _capture(
                plan, source_key, YESTERDAY,
                lambda: calls_inside_backoff.append(1) or _valid_payload(
                    source_key, YESTERDAY, signal=60, fetched_at=_fresh_stamp(clock.value)),
            )
            assert calls_inside_backoff == [], (source_key, label, "loader called before next_retry_at")
            assert _signal(source_key, payload) == expected_signal, (source_key, label, status, payload)
            accepted, _ = runtime.load_temporal_source_slot_snapshot(
                source_key=source_key, snapshot_date=YESTERDAY,
                snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
            )
            assert _signal(source_key, accepted) == expected_signal

            # A later fresh cycle must retry after the rejected attempt rather
            # than treating the preserved payload as final for yesterday.
            retry_after = datetime.fromisoformat(closure.next_retry_at.replace("Z", "+00:00"))
            clock.value = retry_after.astimezone(NOW.tzinfo) + timedelta(seconds=1)
            retry = _valid_payload(source_key, YESTERDAY, signal=50, fetched_at=_fresh_stamp(clock.value))
            sync.payload = retry
            retry_calls = []
            status, payload = _capture(plan, source_key, YESTERDAY, lambda: retry_calls.append(1) or retry)
            assert retry_calls == [1], (source_key, label, "retry suppressed")
            assert status.kind == "success" and _signal(source_key, payload) == 50
            accepted, _ = runtime.load_temporal_source_slot_snapshot(
                source_key=source_key, snapshot_date=YESTERDAY,
                snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
            )
            assert _signal(source_key, accepted) == 50
            expected_signal = 50


def _assert_older_day_and_stocks_unchanged(source_key: str) -> None:
    with TemporaryDirectory(prefix="vitrina-yesterday-preserve-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        plan = SheetVitrinaV1LivePlanBlock.__new__(SheetVitrinaV1LivePlanBlock)
        plan.runtime = runtime
        plan.now_factory = lambda: NOW
        sync = _ClosedSync()
        plan.closed_day_web_source_sync = sync
        plan.current_web_source_sync = SimpleNamespace(observed_states={})

        old = _valid_payload(source_key, OLDER_DAY, signal=7, fetched_at="2026-10-22T20:00:00Z")
        runtime.save_temporal_source_slot_snapshot(
            source_key=source_key, snapshot_date=OLDER_DAY,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
            captured_at="2026-10-22T20:00:00Z", payload=old,
        )
        stock = {"kind": "success", "snapshot_date": YESTERDAY,
                 "items": [{"nm_id": 101, "stock_total": 17}]}
        runtime.save_temporal_source_slot_snapshot(
            source_key="stocks", snapshot_date=YESTERDAY,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
            captured_at="2026-10-23T00:10:00Z", payload=stock,
        )

        calls = []
        sync.payload = _valid_payload(source_key, OLDER_DAY, signal=88)
        status, payload = _capture(plan, source_key, OLDER_DAY, lambda: calls.append(1) or sync.payload)
        assert calls == [], f"older accepted history was fetched again: {source_key}"
        assert status.kind == "success" and _signal(source_key, payload) == 7
        old_after, _ = runtime.load_temporal_source_slot_snapshot(
            source_key=source_key, snapshot_date=OLDER_DAY,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        )
        assert _signal(source_key, old_after) == 7
        stock_after, stock_at = runtime.load_temporal_source_slot_snapshot(
            source_key="stocks", snapshot_date=YESTERDAY,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        )
        assert (stock_after.kind == "success" and stock_after.snapshot_date == YESTERDAY
                and stock_after.items[0].nm_id == 101 and stock_after.items[0].stock_total == 17
                and stock_at == "2026-10-23T00:10:00Z"), (stock_after, stock_at)


def _capture(plan, source_key: str, day: str, loader):
    return plan._capture_temporal_source_with_acceptance(
        source_key=source_key,
        temporal_slot=TEMPORAL_SLOT_YESTERDAY_CLOSED,
        temporal_policy="dual_day_capable",
        column_date=day,
        requested_nm_ids=REQUESTED,
        loader=loader,
        execution_mode="auto_daily",
        accepted_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        allow_persisted_retry=True,
        current_web_source_sync_note=None,
    )


class _ClosedSync:
    def __init__(self) -> None:
        self.payload = None

    def ensure_closed_day_snapshot(self, *, source_key: str, snapshot_date: str):
        items = getattr(self.payload, "items", [])
        return ClosedDaySourceState(
            source_key=source_key,
            snapshot_date=snapshot_date,
            row_count=len(items),
            fetched_at=getattr(self.payload, "source_fetched_at", None),
            item_digests={str(item.nm_id): _serving_item_digest(item) for item in items},
        )


def _valid_payload(source_key: str, day: str, *, signal: int, fetched_at: str | None = None):
    fetched_at = fetched_at or ("2026-10-22T20:30:00Z" if signal == 10 else "2026-10-22T21:30:00Z")
    if source_key == "seller_funnel_snapshot":
        return SellerFunnelSnapshotSuccess(
            "success", day, len(REQUESTED),
            [SellerFunnelSnapshotItem(nm, "item", f"sku-{nm}", signal, signal // 2, 10)
             for nm in REQUESTED],
            fetched_at,
        )
    return WebSourceSnapshotSuccess(
        "success", day, day, len(REQUESTED),
        [WebSourceSnapshotItem(nm, signal, 10, signal // 3, 5) for nm in REQUESTED],
        fetched_at,
    )


def _empty_payload(source_key: str, day: str):
    if source_key == "seller_funnel_snapshot":
        return SellerFunnelSnapshotSuccess("success", day, 0, [], "2026-10-22T21:30:00Z")
    return WebSourceSnapshotSuccess("success", day, day, 0, [], "2026-10-22T21:30:00Z")


def _partial_payload(source_key: str, day: str, *, signal: int):
    complete = _valid_payload(source_key, day, signal=signal)
    return replace(complete, count=1, items=complete.items[:1])


def _fresh_stamp(now: datetime) -> str:
    return (now.astimezone(timezone.utc) - timedelta(minutes=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _signal(source_key: str, payload) -> int | None:
    items = getattr(payload, "items", [])
    if not items:
        return None
    return items[0].view_count if source_key == "seller_funnel_snapshot" else items[0].views_current


if __name__ == "__main__":
    main()
