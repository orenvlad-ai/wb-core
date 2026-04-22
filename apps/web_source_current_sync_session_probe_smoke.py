"""Targeted smoke-check for explicit seller-portal session invalid surfacing."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.web_source_current_sync import (  # noqa: E402
    ShellBackedWebSourceCurrentSync,
    WebSourceCurrentSyncConfig,
)


class _InvalidSessionSync(ShellBackedWebSourceCurrentSync):
    def __init__(self) -> None:
        super().__init__(
            config=WebSourceCurrentSyncConfig(
                mode="force",
                wb_web_bot_dir=Path("/tmp/nonexistent-wb-web-bot"),
                wb_ai_dir=Path("/tmp/nonexistent-wb-ai"),
                api_base_url="http://127.0.0.1:8000",
                timeout_sec=5,
            )
        )
        self.probe_calls = 0
        self.materialize_calls: list[tuple[str, str]] = []

    def _has_search_analytics_snapshot(self, snapshot_date: str) -> bool:
        return False

    def _has_sales_funnel_snapshot(self, snapshot_date: str) -> bool:
        return True

    def _ensure_seller_portal_session_ready(self) -> None:
        self.probe_calls += 1
        raise RuntimeError(
            "seller_portal_session_invalid: "
            "final_url=https://seller-auth.wildberries.ru/ru/?redirect_url=...; "
            "title=Вход на Портал поставщиков Wildberries; "
            "manual_relogin_required=login_and_save_state"
        )

    def _materialize_search_analytics(self, snapshot_date: str) -> None:
        self.materialize_calls.append(("web_source_snapshot", snapshot_date))
        raise AssertionError("search materialization must not start after an invalid session probe")

    def _materialize_sales_funnel(self, snapshot_date: str) -> None:
        self.materialize_calls.append(("seller_funnel_snapshot", snapshot_date))
        raise AssertionError("seller materialization must not start after an invalid session probe")


def main() -> None:
    sync = _InvalidSessionSync()

    _assert_invalid_session(
        action=lambda: sync.ensure_snapshot("2026-04-22"),
        expected_fragment="seller_portal_session_invalid",
    )
    if sync.probe_calls != 1:
        raise AssertionError(f"current-day sync must probe session exactly once, got {sync.probe_calls}")

    _assert_invalid_session(
        action=lambda: sync.ensure_closed_day_snapshot(
            source_key="web_source_snapshot",
            snapshot_date="2026-04-21",
        ),
        expected_fragment="manual_relogin_required=login_and_save_state",
    )
    if sync.probe_calls != 2:
        raise AssertionError(f"closed-day sync must re-probe session, got {sync.probe_calls}")
    if sync.materialize_calls:
        raise AssertionError(f"materialization must not start on invalid session, got {sync.materialize_calls}")

    print("current_day_session_probe: ok -> explicit invalid-session blocker surfaced before bot run")
    print("closed_day_session_probe: ok -> explicit invalid-session blocker surfaced before bot run")
    print("smoke-check passed")


def _assert_invalid_session(*, action, expected_fragment: str) -> None:
    try:
        action()
    except RuntimeError as exc:
        message = str(exc)
        if expected_fragment not in message:
            raise AssertionError(f"unexpected invalid-session message: {message}")
        if "seller-auth.wildberries.ru" not in message:
            raise AssertionError(f"invalid-session message must mention login redirect, got {message}")
    else:
        raise AssertionError("invalid seller portal session must abort before bot materialization")


if __name__ == "__main__":
    main()
