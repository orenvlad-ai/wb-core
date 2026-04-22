"""Targeted smoke-check for closed-day source freshness gating in web-source sync."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.web_source_current_sync import (  # noqa: E402
    ClosedDaySourceState,
    ShellBackedWebSourceCurrentSync,
    WebSourceCurrentSyncConfig,
)


class _NoopMaterializeSync(ShellBackedWebSourceCurrentSync):
    def __init__(self, *, states: dict[tuple[str, str], ClosedDaySourceState]) -> None:
        super().__init__(
            config=WebSourceCurrentSyncConfig(
                mode="force",
                wb_web_bot_dir=Path("/tmp/nonexistent-wb-web-bot"),
                wb_ai_dir=Path("/tmp/nonexistent-wb-ai"),
                api_base_url="http://127.0.0.1:8000",
                timeout_sec=5,
                canonical_supplier_id="",
                canonical_supplier_label="",
            ),
            closed_day_source_state_loader=lambda source_key, snapshot_date: states.get((source_key, snapshot_date)),
        )
        self.materialized: list[tuple[str, str]] = []

    def _ensure_seller_portal_session_ready(self) -> None:
        return

    def _materialize_search_analytics(self, snapshot_date: str) -> None:
        self.materialized.append(("web_source_snapshot", snapshot_date))

    def _materialize_sales_funnel(self, snapshot_date: str) -> None:
        self.materialized.append(("seller_funnel_snapshot", snapshot_date))


def main() -> None:
    sync = _NoopMaterializeSync(
        states={
            (
                "web_source_snapshot",
                "2026-04-18",
            ): ClosedDaySourceState(
                source_key="web_source_snapshot",
                snapshot_date="2026-04-18",
                row_count=36,
                fetched_at="2026-04-17T21:11:25+00:00",
            ),
            (
                "seller_funnel_snapshot",
                "2026-04-18",
            ): ClosedDaySourceState(
                source_key="seller_funnel_snapshot",
                snapshot_date="2026-04-18",
                row_count=61,
                fetched_at="2026-04-18T20:43:31+00:00",
            ),
        }
    )

    try:
        sync.ensure_closed_day_snapshot(source_key="web_source_snapshot", snapshot_date="2026-04-18")
    except RuntimeError as exc:
        message = str(exc)
        if "closed_day_source_freshness_not_accepted" not in message:
            raise AssertionError(f"unexpected freshness failure: {message}")
        if "source_fetched_at=2026-04-17T21:11:25+00:00" not in message:
            raise AssertionError(f"freshness note must disclose source_fetched_at, got: {message}")
        if "required_after=2026-04-18T19:00:00+00:00" not in message:
            raise AssertionError(f"freshness note must disclose required_after cutoff, got: {message}")
    else:
        raise AssertionError("pre-close search snapshot must be rejected for closed-day acceptance")

    sync.ensure_closed_day_snapshot(source_key="seller_funnel_snapshot", snapshot_date="2026-04-18")

    if sync.materialized != [
        ("web_source_snapshot", "2026-04-18"),
        ("seller_funnel_snapshot", "2026-04-18"),
    ]:
        raise AssertionError(f"closed-day sync must materialize both requests, got {sync.materialized}")

    print("search_freshness_guard: ok -> pre-close fetched_at rejected for closed-day acceptance")
    print("seller_freshness_guard: ok -> post-close fetched_at accepted for closed-day acceptance")
    print("smoke-check passed")


if __name__ == "__main__":
    main()
