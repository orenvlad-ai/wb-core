"""Targeted smoke-check for web-vitrina activity reason sanitization."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_http_entrypoint import _build_endpoint_summary_item


def main() -> None:
    preserved_warning = _build_endpoint_summary_item(
        source_key="stocks",
        record={
            "tone": "warning",
            "status_label": "Внимание",
            "detail": (
                "вчера: resolution_rule=accepted_closed_preserved_after_invalid_attempt; accepted_at=2026-04-21T20:39:44Z"
                " · сегодня: historical stocks http 429; current_day_web_source_sync_failed=search_analytics current-day sync"
                " 2026-04-22 failed (rc=1): stderr=ue raise exception"
                " playwright._impl._errors.TimeoutError: Timeout 30000ms exceeded while waiting for event \"response\""
                " Traceback (most recent call last): RuntimeError: Template request to search-report/report was not captured"
            ),
            "note": "",
        },
        source_order=0,
    )
    if "источник временно ограничил запросы" not in str(preserved_warning.get("reason_ru") or ""):
        raise AssertionError(f"rate-limit reason must be humanized, got {preserved_warning}")
    if "дополнительная синхронизация завершилась по таймауту" not in str(preserved_warning.get("reason_ru") or ""):
        raise AssertionError(f"sync-timeout reason must be humanized, got {preserved_warning}")
    if "использована последняя подтверждённая версия" not in str(preserved_warning.get("reason_ru") or ""):
        raise AssertionError(f"preserved-version reason must be kept human-readable, got {preserved_warning}")
    _assert_reason_is_sanitized(str(preserved_warning.get("reason_ru") or ""))
    _assert_reason_is_sanitized(str(preserved_warning.get("detail") or ""))

    zero_warning = _build_endpoint_summary_item(
        source_key="fin_report_daily",
        record={
            "tone": "warning",
            "status_label": "Внимание",
            "detail": "вчера: fin_storage_fee_total=0.0; invalid_exact_snapshot · сегодня: fin_storage_fee_total=0.0; invalid_exact_snapshot",
            "note": "",
        },
        source_order=1,
    )
    if zero_warning.get("reason_ru") != "вчера источник вернул нулевые данные, обновление не подтверждено; сегодня источник вернул нулевые данные, обновление не подтверждено":
        raise AssertionError(f"zero-data warning must stay human and deterministic, got {zero_warning}")
    _assert_reason_is_sanitized(str(zero_warning.get("reason_ru") or ""))

    success_marker_warning = _build_endpoint_summary_item(
        source_key="prices_snapshot",
        record={
            "tone": "warning",
            "status_label": "Внимание",
            "detail": (
                "вчера: использован ранее принятый current snapshot предыдущего дня"
                " · сегодня: resolution_rule=accepted_current_current_attempt; accepted_at=2026-04-21T20:40:16Z"
            ),
            "note": "",
        },
        source_order=2,
    )
    if success_marker_warning.get("reason_ru") != "использована последняя подтверждённая версия":
        raise AssertionError(f"success markers must not leak into warning reason, got {success_marker_warning}")
    _assert_reason_is_sanitized(str(success_marker_warning.get("reason_ru") or ""))
    if "prices_snapshot" not in str(success_marker_warning.get("technical_text") or ""):
        raise AssertionError(f"technical line must stay available, got {success_marker_warning}")

    print("web_vitrina_reason_sanitization: ok ->", preserved_warning["reason_ru"])
    print("web_vitrina_zero_warning_reason: ok ->", zero_warning["reason_ru"])
    print("web_vitrina_success_marker_sanitization: ok ->", success_marker_warning["reason_ru"])


def _assert_reason_is_sanitized(text: str) -> None:
    markers = (
        "{",
        "}",
        "Traceback",
        "traceback",
        "requestId",
        "statusText",
        "resolution_rule=",
        "accepted_at=",
        "current_day_web_source_sync_failed=",
    )
    for marker in markers:
        if marker in text:
            raise AssertionError(f"visible reason/detail must not leak {marker!r}: {text}")
    if len(text) > 220:
        raise AssertionError(f"visible reason/detail must stay short, got {len(text)} chars: {text}")


if __name__ == "__main__":
    main()
