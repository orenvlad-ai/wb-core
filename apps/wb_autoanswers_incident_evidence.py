#!/usr/bin/env python3
"""Bounded, read-only production evidence for the Autoanswers lifecycle.

The helper deliberately avoids ``AutoanswersRepository`` because constructing
that repository owns additive schema migration.  It opens the canonical
runtime database with SQLite ``mode=ro`` and emits only operational aggregates,
transition metadata and audit records needed for incident diagnosis.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from packages.application.wb_autoanswers_runtime import AUTOANSWERS_DB_FILENAME

DATABASE_FILENAME = AUTOANSWERS_DB_FILENAME


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _json(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return value


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sum(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Decimal:
    return _money(conn.execute(sql, params).fetchone()[0])


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def collect_evidence(runtime_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    captured_at = now or datetime.now(timezone.utc)
    database_path = runtime_dir / DATABASE_FILENAME
    uri = f"file:{database_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        table_names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        settings = _row(
            conn.execute(
                """
                SELECT master_enabled,mode,enable_epoch,policy_epoch,enabled_at,
                       daily_cap_usd,monthly_cap_usd,hourly_cap_usd,
                       max_paid_reviews_per_hour,max_materialized_processing_jobs,
                       max_reservation_per_review_usd,updated_at
                FROM sheet_vitrina_v1_wb_autoanswers_settings
                WHERE singleton=1
                """
            ).fetchone()
        )
        runtime = _row(
            conn.execute(
                """
                SELECT stop_reason,stop_details_json,last_scheduler_tick_at,
                       last_successful_ai_call_at,last_confirmed_publication_at,
                       updated_at
                FROM sheet_vitrina_v1_wb_autoanswers_runtime_state
                WHERE singleton=1
                """
            ).fetchone()
        )
        if runtime is not None:
            runtime["stop_details"] = _json(runtime.pop("stop_details_json"))

        latest_preview = _row(
            conn.execute(
                """
                SELECT preview_id,target_selector_state,scope_from,scope_to,
                       snapshot_sha256,counts_json,estimated_cost_usd,budget_json,
                       enable_epoch,policy_epoch,created_by,created_at,expires_at,
                       consumed_at,run_max_usd,run_max_paid_reviews,
                       estimated_unit_cost_usd
                FROM sheet_vitrina_v1_wb_autoanswers_transition_previews
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        )
        if latest_preview is not None:
            latest_preview["counts"] = _json(latest_preview.pop("counts_json"))
            latest_preview["budget"] = _json(latest_preview.pop("budget_json"))

        latest_run = _row(
            conn.execute(
                """
                SELECT sweep_id,preview_id,policy_epoch,target_mode,scope_from,
                       scope_to,state,cursor_json,totals_json,progress_json,
                       lease_owner,lease_until,last_error_code,created_by,
                       created_at,updated_at,completed_at,transition_run_id,
                       run_max_usd,run_max_paid_reviews,pause_reason
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        )
        transition_run_id = ""
        sweep_id = ""
        run_created_at = ""
        if latest_run is not None:
            transition_run_id = str(
                latest_run.get("transition_run_id") or latest_run.get("sweep_id") or ""
            )
            sweep_id = str(latest_run.get("sweep_id") or "")
            run_created_at = str(latest_run.get("created_at") or "")
            latest_run["cursor"] = _json(latest_run.pop("cursor_json"))
            latest_run["totals"] = _json(latest_run.pop("totals_json"))
            latest_run["progress"] = _json(latest_run.pop("progress_json"))
            latest_run["membership_count"] = _count(
                conn,
                """
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope
                WHERE sweep_id=?
                """,
                (sweep_id,),
            )
            latest_run["membership_distinct_feedbacks"] = _count(
                conn,
                """
                SELECT COUNT(DISTINCT feedback_id)
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope
                WHERE sweep_id=?
                """,
                (sweep_id,),
            )
            latest_run["eligible_unanswered_at_readback"] = _count(
                conn,
                """
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope rs
                JOIN sheet_vitrina_v1_wb_feedbacks f
                  ON f.feedback_id=rs.feedback_id
                 AND f.content_version=rs.content_version_at_preview
                WHERE rs.sweep_id=? AND COALESCE(f.answer_text,'')=''
                """,
                (sweep_id,),
            )

        audit = _rows(
            conn.execute(
                """
                SELECT event_type,actor_type,actor_id,previous_state,next_state,
                       details_json,created_at
                FROM sheet_vitrina_v1_wb_autoanswers_audit_events
                WHERE aggregate_type='settings' OR event_type LIKE 'mode_transition%'
                ORDER BY created_at DESC
                LIMIT 30
                """
            ).fetchall()
        )
        for item in audit:
            item["details"] = _json(item.pop("details_json"))

        ai_jobs = _rows(
            conn.execute(
                """
                SELECT state,COUNT(*) AS count
                FROM sheet_vitrina_v1_wb_autoanswer_jobs
                WHERE transition_run_id=?
                GROUP BY state ORDER BY state
                """,
                (transition_run_id,),
            ).fetchall()
        )
        publication_jobs = _rows(
            conn.execute(
                """
                SELECT state,COUNT(*) AS count
                FROM sheet_vitrina_v1_wb_publication_jobs
                WHERE transition_run_id=?
                GROUP BY state ORDER BY state
                """,
                (transition_run_id,),
            ).fetchall()
        )
        sweeps = _rows(
            conn.execute(
                """
                SELECT state,COUNT(*) AS count
                FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
                GROUP BY state ORDER BY state
                """
            ).fetchall()
        )
        reservations = _row(
            conn.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN status='reserved' THEN 1 ELSE 0 END) AS active,
                  COALESCE(SUM(CASE WHEN status='reserved'
                                    THEN CAST(reserved_usd AS REAL) ELSE 0 END),0)
                    AS active_reserved_usd,
                  SUM(CASE WHEN status='reserved' AND provider_call_started_at IS NOT NULL
                           THEN 1 ELSE 0 END) AS provider_started_active,
                  COALESCE(SUM(CAST(actual_cost_usd AS REAL)),0) AS recorded_actual_usd,
                  MAX(updated_at) AS last_updated_at
                FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
                WHERE transition_run_id=?
                """,
                (transition_run_id,),
            ).fetchone()
        )
        reservation_evidence = _rows(
            conn.execute(
                """
                SELECT r.status,COALESCE(r.released_reason,'') AS released_reason,
                       CASE WHEN r.provider_call_started_at IS NULL THEN 0 ELSE 1 END
                         AS provider_call_started,
                       COUNT(*) AS count,
                       COALESCE(SUM(CAST(r.reserved_usd AS REAL)),0) AS reserved_usd,
                       COALESCE(SUM(CAST(r.actual_cost_usd AS REAL)),0) AS actual_usd,
                       MAX(r.provider_call_started_at) AS last_provider_call_started_at,
                       MAX(r.updated_at) AS last_updated_at
                FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations r
                GROUP BY r.status,COALESCE(r.released_reason,''),
                         CASE WHEN r.provider_call_started_at IS NULL THEN 0 ELSE 1 END
                ORDER BY last_updated_at DESC
                """
            ).fetchall()
        )
        latest_provider_started = _rows(
            conn.execute(
                """
                SELECT r.status,COALESCE(r.released_reason,'') AS released_reason,
                       r.reserved_usd,r.actual_cost_usd,r.transition_run_id,
                       r.provider_call_started_at,r.settled_at,r.updated_at,
                       j.state AS job_state,j.last_error_code,j.started_at,
                       j.completed_at,
                       (SELECT COUNT(*)
                        FROM sheet_vitrina_v1_wb_autoanswers_cost_events e
                        WHERE e.processing_key=r.processing_key) AS cost_event_count,
                       (SELECT COUNT(*)
                        FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events e
                        WHERE e.processing_key=r.processing_key) AS failed_cost_event_count
                FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations r
                LEFT JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
                  ON j.processing_key=r.processing_key
                WHERE r.provider_call_started_at IS NOT NULL
                ORDER BY r.updated_at DESC
                LIMIT 12
                """
            ).fetchall()
        )

        adjustments = _row(
            conn.execute(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(CAST(amount_usd AS REAL)),0) AS net_usd,
                       COALESCE(SUM(CASE WHEN CAST(amount_usd AS REAL)<0
                                         THEN -CAST(amount_usd AS REAL) ELSE 0 END),0)
                         AS unverified_legacy_usd,
                       MAX(effective_at) AS last_effective_at
                FROM sheet_vitrina_v1_wb_autoanswers_budget_adjustments
                """
            ).fetchone()
        )
        if (
            "sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds"
            in table_names
        ):
            uncertainty_holds = _rows(
                conn.execute(
                    """
                    SELECT hold_id,transition_run_id,upper_bound_usd,effective_at,
                           reason,created_by,created_at
                    FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds
                    ORDER BY effective_at,hold_id
                    """
                ).fetchall()
            )
        else:
            uncertainty_holds = []
        if (
            "sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts"
            in table_names
        ):
            provider_uncertainty_attempts = _rows(
                conn.execute(
                    """
                    SELECT uncertainty_id,transition_run_id,attempt_number,
                           upper_bound_usd,effective_at,error_code,created_at
                    FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts
                    ORDER BY effective_at,uncertainty_id
                    """
                ).fetchall()
            )
        else:
            provider_uncertainty_attempts = []
        run_failed_actual = _sum(
            conn,
            """
            SELECT COALESCE(SUM(CAST(actual_cost_usd AS REAL)),0)
            FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events
            WHERE transition_run_id=?
            """,
            (transition_run_id,),
        )
        run_success_actual = _sum(
            conn,
            """
            SELECT COALESCE(SUM(CAST(e.actual_cost_usd AS REAL)),0)
            FROM sheet_vitrina_v1_wb_autoanswers_cost_events e
            JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
              ON j.processing_key=e.processing_key
            WHERE j.transition_run_id=?
            """,
            (transition_run_id,),
        )
        calls_after_run = _row(
            conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events
                   WHERE incurred_at>=?) AS successful_cost_events,
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events
                   WHERE incurred_at>=?) AS failed_cost_events,
                  (SELECT MAX(incurred_at) FROM sheet_vitrina_v1_wb_autoanswers_cost_events
                   WHERE incurred_at>=?) AS last_successful_cost_at,
                  (SELECT MAX(incurred_at) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events
                   WHERE incurred_at>=?) AS last_failed_cost_at
                """,
                (run_created_at, run_created_at, run_created_at, run_created_at),
            ).fetchone()
        )
        writes_after_run = _row(
            conn.execute(
                """
                SELECT COUNT(*) AS attempts,
                       SUM(CASE WHEN write_finished_at IS NOT NULL THEN 1 ELSE 0 END)
                         AS transport_finished,
                       SUM(CASE WHEN readback_outcome='confirmed' THEN 1 ELSE 0 END)
                         AS confirmed,
                       SUM(CASE WHEN write_finished_at IS NULL THEN 1 ELSE 0 END)
                         AS ambiguous,
                       MAX(write_started_at) AS last_write_started_at
                FROM sheet_vitrina_v1_wb_publication_attempts
                WHERE write_started_at>=?
                """,
                (run_created_at,),
            ).fetchone()
        )
        sync = _row(
            conn.execute(
                """
                SELECT MAX(last_success_at) AS last_success_at,
                       MAX(updated_at) AS last_updated_at
                FROM sheet_vitrina_v1_wb_sync_state
                """
            ).fetchone()
        )
        recent_sync_runs = _rows(
            conn.execute(
                """
                SELECT run_kind,source_stream,state,error_code,started_at,finished_at,
                       discovered_count,upserted_count
                FROM sheet_vitrina_v1_wb_sync_runs
                ORDER BY started_at DESC LIMIT 12
                """
            ).fetchall()
        )

        hour_start = _iso(captured_at - timedelta(hours=1))
        day = captured_at.date().isoformat()
        month = day[:7]
        budget_reservation = _row(
            conn.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN substr(COALESCE(settled_at,updated_at),1,10)=?
                                    THEN CAST(actual_cost_usd AS REAL) ELSE 0 END),0)
                    AS daily_actual,
                  COALESCE(SUM(CASE WHEN substr(COALESCE(settled_at,updated_at),1,7)=?
                                    THEN CAST(actual_cost_usd AS REAL) ELSE 0 END),0)
                    AS monthly_actual,
                  COALESCE(SUM(CASE WHEN COALESCE(settled_at,updated_at)>=?
                                    THEN CAST(actual_cost_usd AS REAL) ELSE 0 END),0)
                    AS hourly_actual,
                  COALESCE(SUM(CASE WHEN status='reserved'
                                    THEN CAST(reserved_usd AS REAL) ELSE 0 END),0)
                    AS active_reserved
                FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
                """,
                (day, month, hour_start),
            ).fetchone()
        )
        budget_events = _row(
            conn.execute(
                """
                SELECT
                  (SELECT COALESCE(SUM(CASE WHEN substr(incurred_at,1,10)=?
                                             THEN CAST(actual_cost_usd AS REAL) ELSE 0 END),0)
                   FROM sheet_vitrina_v1_wb_autoanswers_cost_events)
                  +(SELECT COALESCE(SUM(CASE WHEN substr(incurred_at,1,10)=?
                                              THEN CAST(actual_cost_usd AS REAL) ELSE 0 END),0)
                    FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events)
                    AS daily_actual,
                  (SELECT COALESCE(SUM(CASE WHEN substr(incurred_at,1,7)=?
                                             THEN CAST(actual_cost_usd AS REAL) ELSE 0 END),0)
                   FROM sheet_vitrina_v1_wb_autoanswers_cost_events)
                  +(SELECT COALESCE(SUM(CASE WHEN substr(incurred_at,1,7)=?
                                              THEN CAST(actual_cost_usd AS REAL) ELSE 0 END),0)
                    FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events)
                    AS monthly_actual,
                  (SELECT COALESCE(SUM(CASE WHEN incurred_at>=?
                                             THEN CAST(actual_cost_usd AS REAL) ELSE 0 END),0)
                   FROM sheet_vitrina_v1_wb_autoanswers_cost_events)
                  +(SELECT COALESCE(SUM(CASE WHEN incurred_at>=?
                                              THEN CAST(actual_cost_usd AS REAL) ELSE 0 END),0)
                    FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events)
                    AS hourly_actual
                """,
                (day, day, month, month, hour_start, hour_start),
            ).fetchone()
        )
        budget_adjustments = _row(
            conn.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN substr(effective_at,1,10)=?
                                    THEN CAST(amount_usd AS REAL) ELSE 0 END),0)
                    AS daily_net,
                  COALESCE(SUM(CASE WHEN substr(effective_at,1,7)=?
                                    THEN CAST(amount_usd AS REAL) ELSE 0 END),0)
                    AS monthly_net,
                  COALESCE(SUM(CASE WHEN effective_at>=?
                                    THEN CAST(amount_usd AS REAL) ELSE 0 END),0)
                    AS hourly_net,
                  COALESCE(SUM(CASE WHEN substr(effective_at,1,10)=?
                                      AND CAST(amount_usd AS REAL)<0
                                    THEN -CAST(amount_usd AS REAL) ELSE 0 END),0)
                    AS daily_unverified,
                  COALESCE(SUM(CASE WHEN substr(effective_at,1,7)=?
                                      AND CAST(amount_usd AS REAL)<0
                                    THEN -CAST(amount_usd AS REAL) ELSE 0 END),0)
                    AS monthly_unverified,
                  COALESCE(SUM(CASE WHEN effective_at>=?
                                      AND CAST(amount_usd AS REAL)<0
                                    THEN -CAST(amount_usd AS REAL) ELSE 0 END),0)
                    AS hourly_unverified
                FROM sheet_vitrina_v1_wb_autoanswers_budget_adjustments
                """,
                (day, month, hour_start, day, month, hour_start),
            ).fetchone()
        )
        budget = {
            "active_reserved_usd": float(_money(budget_reservation["active_reserved"])),
            "hourly_actual_usd": float(
                _money(budget_reservation["hourly_actual"])
                + _money(budget_events["hourly_actual"])
                + _money(budget_adjustments["hourly_net"])
            ),
            "daily_actual_usd": float(
                _money(budget_reservation["daily_actual"])
                + _money(budget_events["daily_actual"])
                + _money(budget_adjustments["daily_net"])
            ),
            "monthly_actual_usd": float(
                _money(budget_reservation["monthly_actual"])
                + _money(budget_events["monthly_actual"])
                + _money(budget_adjustments["monthly_net"])
            ),
            "hourly_unverified_legacy_usd": float(
                _money(budget_adjustments["hourly_unverified"])
            ),
            "daily_unverified_legacy_usd": float(
                _money(budget_adjustments["daily_unverified"])
            ),
            "monthly_unverified_legacy_usd": float(
                _money(budget_adjustments["monthly_unverified"])
            ),
        }
        all_uncertainty_holds = [
            *uncertainty_holds,
            *provider_uncertainty_attempts,
        ]
        budget.update(
            {
                "hourly_uncertainty_hold_usd": float(
                    sum(
                        (
                            _money(item.get("upper_bound_usd"))
                            for item in all_uncertainty_holds
                            if str(item.get("effective_at") or "") >= hour_start
                        ),
                        Decimal(0),
                    )
                ),
                "daily_uncertainty_hold_usd": float(
                    sum(
                        (
                            _money(item.get("upper_bound_usd"))
                            for item in all_uncertainty_holds
                            if str(item.get("effective_at") or "")[:10] == day
                        ),
                        Decimal(0),
                    )
                ),
                "monthly_uncertainty_hold_usd": float(
                    sum(
                        (
                            _money(item.get("upper_bound_usd"))
                            for item in all_uncertainty_holds
                            if str(item.get("effective_at") or "")[:7] == month
                        ),
                        Decimal(0),
                    )
                ),
                "all_time_uncertainty_hold_usd": float(
                    sum(
                        (
                            _money(item.get("upper_bound_usd"))
                            for item in all_uncertainty_holds
                        ),
                        Decimal(0),
                    )
                ),
                "uncertainty_hold_count": len(all_uncertainty_holds),
            }
        )

    return {
        "contract": "wb_autoanswers_incident_evidence_v2",
        "captured_at": _iso(captured_at),
        "database_open_mode": "ro",
        "settings": settings,
        "runtime": runtime,
        "latest_transition_preview": latest_preview,
        "latest_transition_run": latest_run,
        "settings_audit": audit,
        "reconciliation_sweeps": sweeps,
        "run_jobs": {
            "ai": ai_jobs,
            "publication": publication_jobs,
        },
        "run_budget": {
            "reservations": reservations,
            "successful_actual_usd": float(run_success_actual),
            "failed_actual_usd": float(run_failed_actual),
            "total_actual_usd": float(run_success_actual + run_failed_actual),
            "uncertainty_hold_usd": float(
                sum(
                    (
                        _money(item.get("upper_bound_usd"))
                        for item in all_uncertainty_holds
                        if str(item.get("transition_run_id") or "")
                        == transition_run_id
                    ),
                    Decimal(0),
                )
            ),
        },
        "reservation_evidence": reservation_evidence,
        "latest_provider_started_reservations": latest_provider_started,
        "budget": budget,
        "budget_adjustments": adjustments,
        "budget_uncertainty_holds": {
            "schema_available": (
                "sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds"
                in table_names
            ),
            "count": len(uncertainty_holds),
            "upper_bound_usd": float(
                sum(
                    (_money(item.get("upper_bound_usd")) for item in uncertainty_holds),
                    Decimal(0),
                )
            ),
            "holds": uncertainty_holds,
        },
        "provider_uncertainty_attempts": {
            "schema_available": (
                "sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts"
                in table_names
            ),
            "count": len(provider_uncertainty_attempts),
            "upper_bound_usd": float(
                sum(
                    (
                        _money(item.get("upper_bound_usd"))
                        for item in provider_uncertainty_attempts
                    ),
                    Decimal(0),
                )
            ),
            "attempts": provider_uncertainty_attempts,
        },
        "calls_after_run_created": calls_after_run,
        "wb_writes_after_run_created": writes_after_run,
        "sync": sync,
        "recent_sync_runs": recent_sync_runs,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True)
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    payload = collect_evidence(Path(args.runtime_dir))
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
