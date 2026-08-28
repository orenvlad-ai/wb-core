#!/usr/bin/env python3
"""Deterministic acceptance checks for the shared daily Finance transport."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.fin_report_daily_block import HttpBackedFinReportDailySource  # noqa: E402
from packages.adapters.wb_finance_api import (  # noqa: E402
    FinanceApiError,
    FinanceHttpResult,
    FinanceRateLimited,
    WbFinanceApiClient,
    parse_finance_retry_hint_seconds,
)
from packages.application.fin_report_daily_block import FinReportDailyBlock  # noqa: E402
from packages.contracts.fin_report_daily_block import FinReportDailyRequest  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    TemporalSourceClosureState,
)
from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    CLOSURE_STATE_EXHAUSTED,
    _closure_attempt_is_due,
    _closure_next_attempt_count,
    _next_closure_retry,
)


class Clock:
    def __init__(self) -> None:
        self.value = 1_000_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.value += float(seconds)


def _client(
    runtime: Path,
    clock: Clock,
    request,
    *,
    deadline_seconds: float = 240.0,
) -> WbFinanceApiClient:
    return WbFinanceApiClient(
        "test-token",
        request=request,
        rate_gate_root=runtime,
        wall_time=clock.time,
        monotonic=clock.time,
        sleep=clock.sleep,
        deadline_seconds=deadline_seconds,
    )


def _row(rrd_id: int, nm_id: int, *, doc: str = "Продажа", acquiring: str = "3") -> dict:
    return {
        "rrdId": rrd_id,
        "rrDate": "2026-08-27",
        "nmId": nm_id,
        "docTypeName": doc,
        "sellerOperName": doc,
        "retailPriceWithDisc": "100",
        "commissionPercent": 10,
        "deliveryService": "4",
        "paidStorage": "2",
        "deduction": "1",
        "ppvzSalesCommission": "5",
        "penalty": "0",
        "additionalPayment": "0",
        "acquiringFee": acquiring,
        "cashbackAmount": "1",
    }


def _assert_terminal_pagination_and_mapping(runtime: Path) -> None:
    clock = Clock()
    calls: list[tuple[float, int]] = []
    responses = [
        FinanceHttpResult(200, [_row(10, 101)], {"X-RateLimit-Retry": "90000"}),
        FinanceHttpResult(200, [_row(20, 102, doc="Возврат", acquiring="7")], {}),
        FinanceHttpResult(204, [], {}),
    ]

    def request(payload: dict) -> FinanceHttpResult:
        calls.append((clock.time(), int(payload["rrdId"])))
        return responses.pop(0)

    block = FinReportDailyBlock(
        HttpBackedFinReportDailySource(client=_client(runtime, clock, request))
    )
    result = asdict(
        block.execute(
            FinReportDailyRequest(
                snapshot_type="fin_report_daily",
                snapshot_date="2026-08-27",
                nm_ids=[101, 102],
            )
        ).result
    )
    if [cursor for _at, cursor in calls] != [0, 10, 20]:
        raise AssertionError(f"multi-page cursor sequence mismatch: {calls}")
    if calls[1][0] - calls[0][0] != 90.0 or calls[2][0] - calls[1][0] != 60.0:
        raise AssertionError(f"server-hinted/minimum cadence mismatch: {calls}")
    if result["diagnostics"]["pagination"] != {
        "pages": 2,
        "rrdid_start": 0,
        "rrdid_end": 20,
        "terminal_status": 204,
        "complete": True,
    }:
        raise AssertionError(f"terminal pagination evidence mismatch: {result}")
    by_nm = {item["nm_id"]: item for item in result["items"]}
    if by_nm[101]["fin_buyout_rub"] != 100.0 or by_nm[102]["fin_buyout_rub"] != -100.0:
        raise AssertionError("sale/return buyout rule drifted")
    if by_nm[102]["fin_acquiring_fee"] != 7.0:
        raise AssertionError("return acquiringFee must be additive as delivered")


def _assert_account_storage_total(runtime: Path) -> None:
    clock = Clock()
    unrelated = _row(11, 999999)
    unrelated["paidStorage"] = "9"
    responses = [
        FinanceHttpResult(200, [_row(10, 101), unrelated], {}),
        FinanceHttpResult(204, [], {}),
    ]
    block = FinReportDailyBlock(
        HttpBackedFinReportDailySource(
            client=_client(runtime, clock, lambda _payload: responses.pop(0))
        )
    )
    result = block.execute(
        FinReportDailyRequest(
            snapshot_type="fin_report_daily",
            snapshot_date="2026-08-27",
            nm_ids=[101],
        )
    ).result
    if result.storage_total.fin_storage_fee_total != 11.0:
        raise AssertionError("storage TOTAL must include non-target account rows")


def _expect_error(client: WbFinanceApiClient, expected: str, error_type=FinanceApiError) -> FinanceApiError:
    try:
        client.fetch_report(
            date_from="2026-08-27", date_to="2026-08-27", period="daily"
        )
    except error_type as exc:
        if exc.code != expected:
            raise AssertionError(f"expected {expected}, got {exc}")
        return exc
    raise AssertionError(f"expected typed Finance error {expected}")


def _assert_failures_are_typed_and_nonpublishing(root: Path) -> None:
    clock = Clock()
    first_429 = _client(
        root / "first-429",
        clock,
        lambda _payload: FinanceHttpResult(429, [], {"Retry-After": "75"}),
    )
    limited = _expect_error(first_429, "rate_limited", FinanceRateLimited)
    if (
        limited.pages != 0
        or limited.retry_after_seconds != 75.0
        or not limited.next_retry_at
        or limited.header_hints != {"retry-after": "75"}
    ):
        raise AssertionError(f"first-page 429 evidence incomplete: {limited}")

    clock = Clock()
    responses = [
        FinanceHttpResult(200, [_row(10, 101)], {}),
        FinanceHttpResult(429, [], {"X-Ratelimit-Retry": "120000"}),
    ]
    mid = _expect_error(
        _client(root / "mid-429", clock, lambda _payload: responses.pop(0)),
        "rate_limited",
        FinanceRateLimited,
    )
    if mid.pages != 1 or mid.cursor != 10:
        raise AssertionError(f"mid-page 429 lost partial cursor evidence: {mid}")

    clock = Clock()
    _expect_error(
        _client(root / "transport", clock, lambda _payload: (_ for _ in ()).throw(OSError("x"))),
        "transport_error",
    )
    clock = Clock()
    _expect_error(
        _client(
            root / "partial",
            clock,
            lambda _payload: FinanceHttpResult(200, [], {}),
        ),
        "partial_report",
    )
    clock = Clock()
    _expect_error(
        _client(
            root / "stuck",
            clock,
            lambda _payload: FinanceHttpResult(200, [_row(0, 101)], {}),
        ),
        "stuck_cursor",
    )
    clock = Clock()

    def slow(_payload: dict) -> FinanceHttpResult:
        clock.value += 2.0
        return FinanceHttpResult(200, [_row(10, 101)], {})

    deadline = _expect_error(
        _client(root / "deadline", clock, slow, deadline_seconds=1.0),
        "deadline",
    )
    if deadline.pages != 1 or deadline.cursor != 10:
        raise AssertionError(f"deadline lost completed-page evidence: {deadline}")


def _assert_common_gate_across_clients(runtime: Path) -> None:
    clock = Clock()
    request_times: list[float] = []

    def terminal(_payload: dict) -> FinanceHttpResult:
        request_times.append(clock.time())
        return FinanceHttpResult(204, [], {})

    first = _client(runtime, clock, terminal)
    second = _client(runtime, clock, terminal)
    first.fetch_report(date_from="2026-08-26", date_to="2026-08-26", period="daily")
    second.fetch_report(date_from="2026-08-27", date_to="2026-08-27", period="daily")
    if request_times[1] - request_times[0] != 60.0:
        raise AssertionError(f"clients bypassed common interprocess gate: {request_times}")


def _assert_next_day_retry_eligibility() -> None:
    state = TemporalSourceClosureState(
        source_key="fin_report_daily",
        target_date="2026-08-26",
        slot_kind="yesterday_closed",
        state=CLOSURE_STATE_EXHAUSTED,
        attempt_count=6,
        next_retry_at=None,
        last_reason="rate_limited",
        last_attempt_at="2026-08-27T10:00:00Z",
        last_success_at=None,
        accepted_at=None,
    )
    same_day = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    next_day = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    if _closure_attempt_is_due(state, same_day):
        raise AssertionError("same-day exhausted Finance closure reopened early")
    if not _closure_attempt_is_due(state, next_day):
        raise AssertionError("missing Finance date was terminally forgotten next day")
    if _closure_next_attempt_count(state, next_day) != 1:
        raise AssertionError("next-day Finance retry budget was not renewed")
    hinted_retry, _ = _next_closure_retry(
        same_day,
        1,
        "rate_limited",
        server_next_retry_at="2026-08-27T13:00:00Z",
    )
    if hinted_retry != "2026-08-27T13:00:00Z":
        raise AssertionError("closure retry discarded the later server next-at hint")


def main() -> None:
    if parse_finance_retry_hint_seconds(
        {"Retry-After": "30", "X-Ratelimit-Retry": "120000"}, now_epoch=0
    ) != 120.0:
        raise AssertionError("Retry-After/X-RateLimit hint parsing drifted")
    if parse_finance_retry_hint_seconds(
        {"X-RateLimit-Reset": "60000"}, now_epoch=1_000_000
    ) != 60.0:
        raise AssertionError("relative millisecond reset hint parsing drifted")
    with TemporaryDirectory(prefix="fin-daily-transport-") as tmp:
        root = Path(tmp)
        _assert_terminal_pagination_and_mapping(root / "mapping")
        _assert_account_storage_total(root / "storage")
        _assert_failures_are_typed_and_nonpublishing(root / "failures")
        _assert_common_gate_across_clients(root / "shared")
        _assert_next_day_retry_eligibility()
    print(
        "fin_report_daily_finance_transport: ok -> POST daily, terminal 204, shared 60s gate, typed failures, no partial publish"
    )


if __name__ == "__main__":
    main()
