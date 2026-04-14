"""Targeted smoke-check for sales funnel history batching and 429 retry handling."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.sales_funnel_history_block import (
    HttpBackedSalesFunnelHistorySource,
    _SalesFunnelHistoryHttpStatusError,
)
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryRequest


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += float(seconds)


class RecordingSalesFunnelHistorySource(HttpBackedSalesFunnelHistorySource):
    def __init__(self, *, responses: list[object], clock: FakeClock, **kwargs: object) -> None:
        super().__init__(
            base_url="https://example.invalid",
            token_env_var="WB_TOKEN",
            **kwargs,
        )
        self._responses = list(responses)
        self._clock = clock
        self.calls: list[dict[str, object]] = []

    def _post_history_once(
        self,
        *,
        base_url: str,
        token: str,
        date_from: str,
        date_to: str,
        nm_ids: list[int],
        timeout_seconds: float,
    ) -> object:
        self.calls.append(
            {
                "base_url": base_url,
                "token": token,
                "date_from": date_from,
                "date_to": date_to,
                "nm_ids": list(nm_ids),
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self._responses:
            raise AssertionError("unexpected extra batch request")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def _sleep(self, seconds: float) -> None:
        self._clock.sleep(seconds)

    def _monotonic(self) -> float:
        return self._clock.now


def _build_payload(nm_ids: list[int], *, date: str) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for nm_id in nm_ids:
        payload.append(
            {
                "product": {"nmId": nm_id},
                "history": [{"date": date, "openCount": nm_id}],
            }
        )
    return payload


def _check_chunking_and_merge() -> None:
    clock = FakeClock()
    nm_ids = list(range(210183900, 210183933))
    source = RecordingSalesFunnelHistorySource(
        responses=[
            _build_payload(nm_ids[:20], date="2026-04-12"),
            _build_payload(nm_ids[20:], date="2026-04-12"),
        ],
        clock=clock,
    )
    block = SalesFunnelHistoryBlock(source)
    result = asdict(
        block.execute(
            SalesFunnelHistoryRequest(
                snapshot_type="sales_funnel_history",
                date_from="2026-04-12",
                date_to="2026-04-12",
                nm_ids=nm_ids,
            )
        )
    )
    if len(source.calls) != 2:
        raise AssertionError(f"expected 2 batch requests, got {len(source.calls)}")
    if len(source.calls[0]["nm_ids"]) != 20 or len(source.calls[1]["nm_ids"]) != 13:
        raise AssertionError(f"unexpected batch sizes: {[len(call['nm_ids']) for call in source.calls]}")
    if any(len(call["nm_ids"]) > 20 for call in source.calls):
        raise AssertionError("batch request exceeded 20 nmIds")
    if clock.sleeps:
        raise AssertionError(f"<=3 batches must not add pacing sleep, got {clock.sleeps}")
    if result["result"]["kind"] != "success":
        raise AssertionError(f"unexpected result kind: {result['result']['kind']}")
    if result["result"]["count"] != len(nm_ids):
        raise AssertionError(f"unexpected merged count: {result['result']['count']}")
    merged_nm_ids = {item["nm_id"] for item in result["result"]["items"]}
    if merged_nm_ids != set(nm_ids):
        raise AssertionError("merged result lost rows from one of the batches")
    print("chunking-33: ok -> 2 serial POST, 20/13, merged result preserved")


def _check_rate_limit_pacing() -> None:
    clock = FakeClock()
    nm_ids = list(range(210184000, 210184061))
    source = RecordingSalesFunnelHistorySource(
        responses=[
            _build_payload(nm_ids[:20], date="2026-04-12"),
            _build_payload(nm_ids[20:40], date="2026-04-12"),
            _build_payload(nm_ids[40:60], date="2026-04-12"),
            _build_payload(nm_ids[60:], date="2026-04-12"),
        ],
        clock=clock,
    )
    block = SalesFunnelHistoryBlock(source)
    result = asdict(
        block.execute(
            SalesFunnelHistoryRequest(
                snapshot_type="sales_funnel_history",
                date_from="2026-04-12",
                date_to="2026-04-12",
                nm_ids=nm_ids,
            )
        )
    )
    if len(source.calls) != 4:
        raise AssertionError(f"expected 4 batch requests, got {len(source.calls)}")
    if clock.sleeps != [60.0]:
        raise AssertionError(f"expected one 60-second pacing sleep, got {clock.sleeps}")
    if result["result"]["count"] != len(nm_ids):
        raise AssertionError("rate-limit pacing path lost merged rows")
    print("pacing-61: ok -> 4th request delayed to stay within 3 requests / 60 seconds")


def _check_retry_after_429() -> None:
    clock = FakeClock()
    source = RecordingSalesFunnelHistorySource(
        responses=[
            _SalesFunnelHistoryHttpStatusError(429, "too many requests"),
            _build_payload([210183919], date="2026-04-12"),
        ],
        clock=clock,
        max_retries_on_429=1,
        retry_backoff_seconds=5.0,
    )
    block = SalesFunnelHistoryBlock(source)
    result = asdict(
        block.execute(
            SalesFunnelHistoryRequest(
                snapshot_type="sales_funnel_history",
                date_from="2026-04-12",
                date_to="2026-04-12",
                nm_ids=[210183919],
            )
        )
    )
    if len(source.calls) != 2:
        raise AssertionError(f"expected retry path to make 2 requests, got {len(source.calls)}")
    if clock.sleeps != [5.0]:
        raise AssertionError(f"expected bounded retry backoff, got {clock.sleeps}")
    if result["result"]["kind"] != "success":
        raise AssertionError("retry path must preserve success shape after recovery")
    print("retry-429: ok -> bounded retry/backoff recovers without contract change")


def _check_retry_exhaustion() -> None:
    clock = FakeClock()
    source = RecordingSalesFunnelHistorySource(
        responses=[
            _SalesFunnelHistoryHttpStatusError(429, "too many requests"),
            _SalesFunnelHistoryHttpStatusError(429, "too many requests"),
            _SalesFunnelHistoryHttpStatusError(429, "too many requests"),
        ],
        clock=clock,
        max_retries_on_429=2,
        retry_backoff_seconds=5.0,
    )
    block = SalesFunnelHistoryBlock(source)
    try:
        block.execute(
            SalesFunnelHistoryRequest(
                snapshot_type="sales_funnel_history",
                date_from="2026-04-12",
                date_to="2026-04-12",
                nm_ids=[210183919],
            )
        )
    except RuntimeError as exc:
        if "status 429" not in str(exc):
            raise AssertionError(f"unexpected retry exhaustion error: {exc}") from exc
    else:
        raise AssertionError("retry exhaustion must not hide 429 failure")
    if len(source.calls) != 3:
        raise AssertionError(f"expected 3 total attempts before exhaustion, got {len(source.calls)}")
    if clock.sleeps != [5.0, 5.0]:
        raise AssertionError(f"unexpected retry sleeps after exhaustion: {clock.sleeps}")
    print("retry-429-exhausted: ok -> 429 is surfaced after bounded retries")


def main() -> None:
    os.environ.setdefault("WB_TOKEN", "stub-token")
    _check_chunking_and_merge()
    _check_rate_limit_pacing()
    _check_retry_after_429()
    _check_retry_exhaustion()
    print("smoke-check passed")


if __name__ == "__main__":
    main()
