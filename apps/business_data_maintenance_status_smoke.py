#!/usr/bin/env python3
"""Focused monitoring-status smoke without HTTP/Playwright dependencies."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.business_data_maintenance import (  # noqa: E402
    _autoanswers_budget_monitor_state,
    _with_operator_process_status,
)


def _assert_statuses() -> None:
    healthy = {
        "process_key": "autoanswers",
        "desired": True,
        "actual": True,
        "drift_status": "matched",
        "lifecycle_state": "running",
        "fresh_scheduler_tick": True,
        "last_error": "",
        "stop_reason": "",
    }
    assert _with_operator_process_status(
        healthy, master_desired=True
    )["operator_status"] == "Работает штатно"
    cases = (
        ({"desired": True, "lifecycle_state": "starting"}, True, "Запускается"),
        ({"desired": False, "lifecycle_state": "off"}, True, "Приостановлено пользователем"),
        (healthy, False, "Приостановлено общей паузой"),
        ({"desired": True, "drift_status": "drift"}, True, "Есть расхождение"),
        (
            {"desired": True, "lifecycle_state": "error", "last_error": "boom"},
            True,
            "Ошибка процесса",
        ),
        (
            {
                "process_key": "autoanswers",
                "desired": True,
                "lifecycle_state": "error",
                "fresh_scheduler_tick": False,
                "stop_reason": "worker_unavailable",
            },
            True,
            "Нет свежего подтверждения",
        ),
        ({"desired": None}, True, "Состояние неизвестно"),
    )
    for partial, master_desired, expected in cases:
        payload = {
            "process_key": "fixture",
            "desired": True,
            "actual": False,
            "drift_status": "unknown",
            "lifecycle_state": "",
            "last_error": "",
            "stop_reason": "",
            **partial,
        }
        assert _with_operator_process_status(
            payload,
            master_desired=master_desired,
        )["operator_status"] == expected


def _assert_budget_hold() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE sheet_vitrina_v1_wb_autoanswers_budget_reservations(
            processing_key TEXT, transition_run_id TEXT, provider_call_started_at TEXT,
            released_reason TEXT, created_at TEXT, updated_at TEXT, status TEXT,
            actual_cost_usd TEXT, reserved_usd TEXT
        );
        CREATE TABLE sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds(
            processing_key TEXT, upper_bound_usd TEXT, created_at TEXT
        );
        CREATE TABLE sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts(
            processing_key TEXT, attempt_number INTEGER, upper_bound_usd TEXT,
            created_at TEXT
        );
        CREATE TABLE sheet_vitrina_v1_wb_autoanswers_cost_events(
            processing_key TEXT, actual_cost_usd TEXT, incurred_at TEXT
        );
        CREATE TABLE sheet_vitrina_v1_wb_autoanswers_failed_cost_events(
            processing_key TEXT, actual_cost_usd TEXT, incurred_at TEXT
        );
        CREATE TABLE sheet_vitrina_v1_wb_autoanswers_budget_adjustments(
            amount_usd TEXT, effective_at TEXT
        );
        CREATE TABLE sheet_vitrina_v1_wb_autoanswer_jobs(
            processing_key TEXT, last_error_code TEXT, attempts INTEGER
        );
        INSERT INTO sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds
        VALUES ('a','0.10','2026-07-24T10:00:00Z'),('b','0.10','2026-07-24T10:01:00Z');
        INSERT INTO sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts
        VALUES ('c',1,'0.10','2026-07-24T10:01:30Z');
        INSERT INTO sheet_vitrina_v1_wb_autoanswers_cost_events
        VALUES ('paid','0.03','2026-07-24T10:02:00Z');
        """
    )
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    budget = _autoanswers_budget_monitor_state(conn, tables=tables)
    assert budget["budget_state"] == "conservative_unverified"
    assert budget["confirmed_actual_usd"] == 0.03
    assert budget["uncertainty_hold_usd"] == 0.3
    assert budget["uncertainty_hold_count"] == 3
    assert budget["unresolved_uncertainty_count"] == 0
    assert "не подтверждённое списание" in budget["hold_explanation"]
    legacy_budget = _autoanswers_budget_monitor_state(
        conn,
        tables=tables
        - {"sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts"},
    )
    assert legacy_budget["uncertainty_hold_usd"] == 0.2
    assert legacy_budget["uncertainty_hold_count"] == 2


def _assert_ui_copy() -> None:
    template = (
        ROOT / "packages/adapters/templates/sheet_vitrina_v1_settings.html"
    ).read_text(encoding="utf-8")
    backend = (
        ROOT / "apps/business_data_maintenance.py"
    ).read_text(encoding="utf-8")
    rendered_contract = backend + "\n" + template
    for expected in (
        "Работает штатно",
        "Запускается",
        "Приостановлено пользователем",
        "Приостановлено общей паузой",
        "Есть расхождение",
        "Ошибка процесса",
        "Нет свежего подтверждения",
        "Состояние неизвестно",
        "Консервативные holds",
        "не подтверждённый расход",
        "Только мониторинг",
    ):
        assert expected in rendered_contract, expected


def main() -> int:
    _assert_statuses()
    _assert_budget_hold()
    _assert_ui_copy()
    print("business data maintenance status smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
