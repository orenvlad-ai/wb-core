#!/usr/bin/env python3
"""Restore proven prefilter skips corrupted by policy-epoch requeue.

Dry-run and readback are query-only. Apply is fingerprint-bound, uses
``BEGIN IMMEDIATE``, restores only the job projection, appends audit evidence,
and never changes reservations, provider cost, uncertainty holds or WB writes.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_autoanswers_runtime import (
    EVALUATION_SIGNATURE,
    PROMPT_BUNDLE_VERSION,
)  # noqa: E402


DATABASE_FILENAME = "registry_upload_runtime.sqlite3"
CONTRACT = "wb_autoanswers_prefilter_skip_recovery_v1"
RESTORE_EVENT = "prefilter_skip_state_restored"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _open_ro(runtime_dir: Path) -> sqlite3.Connection:
    database = (runtime_dir / DATABASE_FILENAME).resolve()
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _open_rw(runtime_dir: Path) -> sqlite3.Connection:
    database = (runtime_dir / DATABASE_FILENAME).resolve()
    conn = sqlite3.connect(database, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _audit_events(
    conn: sqlite3.Connection,
    processing_key: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT event_type,previous_state,next_state,details_json,created_at
        FROM sheet_vitrina_v1_wb_autoanswers_audit_events
        WHERE aggregate_type='processing_job' AND aggregate_id=?
        ORDER BY created_at,event_id
        """,
        (processing_key,),
    ).fetchall()


def _details(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(str(row["details_json"] or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _candidate_rows(
    conn: sqlite3.Connection,
    *,
    transition_run_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          j.processing_key,j.feedback_id,j.content_version,j.content_version_hash,
          j.state,j.attempts,j.last_error_code,j.policy_epoch,j.transition_run_id,
          j.completed_at,j.updated_at,j.regeneration_required,j.media_uncertain,
          j.manual_started,
          r.status AS reservation_status,r.actual_cost_usd,
          r.provider_call_started_at,r.settled_at,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events c
           WHERE c.processing_key=j.processing_key) AS cost_event_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events f
           WHERE f.processing_key=j.processing_key) AS failed_cost_event_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs p
           WHERE p.processing_key=j.processing_key) AS publication_count
        FROM sheet_vitrina_v1_wb_autoanswer_jobs j
        JOIN sheet_vitrina_v1_wb_autoanswers_budget_reservations r
          ON r.processing_key=j.processing_key
        WHERE j.transition_run_id=?
          AND (
            (j.state='terminal_error' AND j.last_error_code='reservation_missing')
            OR (j.state='queued' AND COALESCE(j.last_error_code,'')='')
          )
          AND COALESCE(j.regeneration_required,0)=0
          AND COALESCE(j.media_uncertain,0)=0
          AND COALESCE(j.manual_started,0)=0
          AND r.status='settled'
          AND CAST(COALESCE(r.actual_cost_usd,'0') AS REAL)=0
          AND r.provider_call_started_at IS NOT NULL
        ORDER BY j.processing_key
        """,
        (transition_run_id,),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if (
            int(row["cost_event_count"] or 0)
            or int(row["failed_cost_event_count"] or 0)
            or int(row["publication_count"] or 0)
        ):
            continue
        original_skip: sqlite3.Row | None = None
        terminal_error: sqlite3.Row | None = None
        for event in _audit_events(conn, str(row["processing_key"])):
            details = _details(event)
            if (
                str(event["event_type"]) == "prefilter_skipped"
                and str(details.get("reason") or "") == "empty_five_star"
                and str(event["next_state"] or "") == "skipped"
            ):
                original_skip = event
            elif (
                str(event["event_type"]) == "processing_terminal_error"
                and str(details.get("error_code") or "") == "reservation_missing"
                and str(event["next_state"] or "") == "terminal_error"
            ):
                terminal_error = event
        policy_requeue = None
        for event in conn.execute(
            """
            SELECT details_json,created_at
            FROM sheet_vitrina_v1_wb_autoanswers_audit_events
            WHERE aggregate_type='feedback' AND aggregate_id=?
              AND event_type='policy_reconciled'
            ORDER BY created_at,event_id
            """,
            (str(row["feedback_id"]),),
        ).fetchall():
            details = _details(event)
            if (
                str(details.get("outcome") or "") == "generation_queued"
                and str(details.get("transition_run_id") or "")
                == transition_run_id
            ):
                policy_requeue = event
        current_state = str(row["state"])
        if (
            original_skip is None
            or policy_requeue is None
            or (current_state == "terminal_error" and terminal_error is None)
        ):
            continue
        candidates.append(
            {
                "processing_key": str(row["processing_key"]),
                "feedback_id": str(row["feedback_id"]),
                "content_version": int(row["content_version"]),
                "content_version_hash": str(row["content_version_hash"]),
                "policy_epoch": int(row["policy_epoch"] or 0),
                "transition_run_id": str(row["transition_run_id"] or ""),
                "attempts": int(row["attempts"] or 0),
                "current_state": current_state,
                "current_last_error_code": str(
                    row["last_error_code"] or ""
                ),
                "reservation_status": str(row["reservation_status"]),
                "reservation_actual_cost_usd": str(row["actual_cost_usd"] or "0"),
                "provider_call_started_at": str(
                    row["provider_call_started_at"] or ""
                ),
                "reservation_settled_at": str(row["settled_at"] or ""),
                "original_skipped_at": str(original_skip["created_at"] or ""),
                "policy_requeued_at": str(policy_requeue["created_at"] or ""),
                "terminal_error_at": (
                    str(terminal_error["created_at"] or "")
                    if terminal_error is not None
                    else ""
                ),
            }
        )
    return candidates


def _non_target_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations)
            AS reservations,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
           WHERE provider_call_started_at IS NOT NULL) AS provider_boundaries,
          (SELECT COALESCE(SUM(CAST(actual_cost_usd AS REAL)),0)
           FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations)
            AS reservation_actual_usd,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events)
            AS cost_events,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events)
            AS failed_cost_events,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds)
            AS uncertainty_holds,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs)
            AS publication_jobs,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts)
            AS wb_write_attempts
        """
    ).fetchone()
    return {
        "reservations": int(row["reservations"] or 0),
        "provider_boundaries": int(row["provider_boundaries"] or 0),
        "reservation_actual_usd": str(row["reservation_actual_usd"] or 0),
        "cost_events": int(row["cost_events"] or 0),
        "failed_cost_events": int(row["failed_cost_events"] or 0),
        "uncertainty_holds": int(row["uncertainty_holds"] or 0),
        "publication_jobs": int(row["publication_jobs"] or 0),
        "wb_write_attempts": int(row["wb_write_attempts"] or 0),
    }


def build_plan(
    conn: sqlite3.Connection,
    *,
    transition_run_id: str,
    expected_rows: int,
) -> dict[str, Any]:
    candidates = _candidate_rows(
        conn,
        transition_run_id=transition_run_id,
    )
    non_target_snapshot = _non_target_counts(conn)
    identity = {
        "contract": CONTRACT,
        "transition_run_id": transition_run_id,
        "expected_rows": int(expected_rows),
        "candidates": candidates,
        "non_target_snapshot": non_target_snapshot,
    }
    digest = _fingerprint(identity)
    return {
        **identity,
        "captured_at": _utc_now(),
        "candidate_count": len(candidates),
        "coverage_confirmed": len(candidates) == int(expected_rows),
        "plan_fingerprint": digest,
        "pre_change_digest": digest,
        "expected_affected_records": {
            "processing_jobs_updated": len(candidates),
            "audit_events_appended": len(candidates),
            "reservations_updated": 0,
            "provider_calls_created": 0,
            "cost_events_created": 0,
            "wb_writes_created": 0,
        },
        "non_target_invariants": {
            "reservation_evidence_unchanged": True,
            "provider_calls_unchanged": True,
            "cost_events_unchanged": True,
            "uncertainty_holds_unchanged": True,
            "publication_jobs_and_writes_unchanged": True,
        },
        "reversibility": {
            "kind": "projection_restore_with_append_only_audit",
            "backup_required": False,
            "reason": (
                "The original skip, terminal error and reservation remain in "
                "immutable audit/evidence; apply restores only the job state "
                "and appends a fingerprint-bound recovery event."
            ),
        },
    }


def _prior_apply(
    conn: sqlite3.Connection,
    *,
    transition_run_id: str,
    fingerprint: str,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT aggregate_id,details_json
        FROM sheet_vitrina_v1_wb_autoanswers_audit_events
        WHERE aggregate_type='processing_job' AND event_type=?
        ORDER BY aggregate_id
        """,
        (RESTORE_EVENT,),
    ).fetchall()
    keys: list[str] = []
    for row in rows:
        details = _details(row)
        if (
            str(details.get("plan_fingerprint") or "") == fingerprint
            and str(details.get("transition_run_id") or "") == transition_run_id
        ):
            keys.append(str(row["aggregate_id"]))
    if not keys:
        return []
    placeholders = ",".join("?" for _ in keys)
    confirmed = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM sheet_vitrina_v1_wb_autoanswer_jobs j
            JOIN sheet_vitrina_v1_wb_autoanswers_budget_reservations r
              ON r.processing_key=j.processing_key
            WHERE j.processing_key IN ({placeholders})
              AND j.state='skipped' AND j.last_error_code='empty_five_star'
              AND r.status='settled'
              AND CAST(COALESCE(r.actual_cost_usd,'0') AS REAL)=0
              AND NOT EXISTS(
                SELECT 1 FROM sheet_vitrina_v1_wb_autoanswers_cost_events c
                WHERE c.processing_key=j.processing_key
              )
              AND NOT EXISTS(
                SELECT 1 FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events f
                WHERE f.processing_key=j.processing_key
              )
              AND NOT EXISTS(
                SELECT 1 FROM sheet_vitrina_v1_wb_publication_jobs p
                WHERE p.processing_key=j.processing_key
              )
            """,
            keys,
        ).fetchone()[0]
    )
    return keys if confirmed == len(keys) else []


def _append_audit(
    conn: sqlite3.Connection,
    *,
    processing_key: str,
    actor: str,
    now: str,
    fingerprint: str,
    transition_run_id: str,
    original_skipped_at: str,
    policy_requeued_at: str,
    terminal_error_at: str,
    previous_state: str,
) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_wb_autoanswers_audit_events(
          event_id,aggregate_type,aggregate_id,event_type,previous_state,next_state,
          actor_type,actor_id,bundle_version,evaluation_signature,details_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            uuid4().hex,
            "processing_job",
            processing_key,
            RESTORE_EVENT,
            previous_state,
            "skipped",
            "operator",
            actor,
            PROMPT_BUNDLE_VERSION,
            EVALUATION_SIGNATURE,
            _canonical(
                {
                    "reason": "empty_five_star",
                    "source_error": "reservation_missing",
                    "plan_fingerprint": fingerprint,
                    "transition_run_id": transition_run_id,
                    "original_skipped_at": original_skipped_at,
                    "policy_requeued_at": policy_requeued_at,
                    "terminal_error_at": terminal_error_at,
                }
            ),
            now,
        ),
    )


def apply_plan(
    runtime_dir: Path,
    *,
    transition_run_id: str,
    expected_rows: int,
    expected_fingerprint: str,
    actor: str,
) -> dict[str, Any]:
    if expected_rows <= 0:
        raise ValueError("prefilter skip recovery requires positive expected rows")
    if not actor.strip():
        raise ValueError("prefilter skip recovery requires a non-empty actor")
    conn = _open_rw(runtime_dir)
    try:
        conn.execute("BEGIN IMMEDIATE")
        plan = build_plan(
            conn,
            transition_run_id=transition_run_id,
            expected_rows=expected_rows,
        )
        if str(plan["plan_fingerprint"]) != expected_fingerprint:
            prior = (
                _prior_apply(
                    conn,
                    transition_run_id=transition_run_id,
                    fingerprint=expected_fingerprint,
                )
                if not plan["candidates"]
                else []
            )
            if prior:
                conn.rollback()
                return {
                    "status": "already_reconciled",
                    "idempotent": True,
                    "plan_fingerprint": expected_fingerprint,
                    "affected_records": {
                        key: 0
                        for key in plan["expected_affected_records"]
                    },
                    "non_target_invariants_preserved": True,
                    "restored_processing_keys": prior,
                }
            raise RuntimeError(
                "prefilter skip recovery evidence changed; create a new plan"
            )
        if not plan["coverage_confirmed"]:
            raise RuntimeError(
                "prefilter skip recovery coverage does not match expected rows"
            )
        if not plan["candidates"]:
            raise RuntimeError("prefilter skip recovery has no candidates")
        now = _utc_now()
        non_target_before = dict(plan["non_target_snapshot"])
        for candidate in plan["candidates"]:
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                SET state='skipped',last_error_code='empty_five_star',
                    lease_owner=NULL,lease_until=NULL,completed_at=?,updated_at=?
                WHERE processing_key=? AND transition_run_id=?
                  AND state=?
                  AND COALESCE(last_error_code,'')=?
                """,
                (
                    candidate["original_skipped_at"],
                    now,
                    candidate["processing_key"],
                    transition_run_id,
                    candidate["current_state"],
                    candidate["current_last_error_code"],
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise RuntimeError(
                    "prefilter skip recovery affected-row mismatch"
                )
            _append_audit(
                conn,
                processing_key=str(candidate["processing_key"]),
                actor=actor,
                now=now,
                fingerprint=expected_fingerprint,
                transition_run_id=transition_run_id,
                original_skipped_at=str(candidate["original_skipped_at"]),
                policy_requeued_at=str(candidate["policy_requeued_at"]),
                terminal_error_at=str(candidate["terminal_error_at"]),
                previous_state=str(candidate["current_state"]),
            )
        non_target_after = _non_target_counts(conn)
        if non_target_after != non_target_before:
            raise RuntimeError(
                "prefilter skip recovery changed non-target evidence"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    with closing(_open_ro(runtime_dir)) as readback:
        remaining = _candidate_rows(
            readback,
            transition_run_id=transition_run_id,
        )
        prior = _prior_apply(
            readback,
            transition_run_id=transition_run_id,
            fingerprint=expected_fingerprint,
        )
    if remaining or len(prior) != int(expected_rows):
        raise RuntimeError("prefilter skip recovery readback failed")
    return {
        "status": "reconciled",
        "idempotent": False,
        "plan_fingerprint": expected_fingerprint,
        "affected_records": dict(plan["expected_affected_records"]),
        "non_target_invariants_preserved": True,
        "non_target_readback": {
            "before": non_target_before,
            "after": non_target_after,
        },
        "restored_processing_keys": prior,
        "remaining_candidates": 0,
    }


def readback(
    runtime_dir: Path,
    *,
    transition_run_id: str,
    expected_rows: int,
) -> dict[str, Any]:
    with closing(_open_ro(runtime_dir)) as conn:
        plan = build_plan(
            conn,
            transition_run_id=transition_run_id,
            expected_rows=expected_rows,
        )
        restored = conn.execute(
            """
            SELECT aggregate_id,details_json,created_at
            FROM sheet_vitrina_v1_wb_autoanswers_audit_events
            WHERE aggregate_type='processing_job' AND event_type=?
            ORDER BY created_at,aggregate_id
            """,
            (RESTORE_EVENT,),
        ).fetchall()
    evidence = []
    for row in restored:
        details = _details(row)
        if str(details.get("transition_run_id") or "") == transition_run_id:
            evidence.append(
                {
                    "processing_key": str(row["aggregate_id"]),
                    "plan_fingerprint": str(
                        details.get("plan_fingerprint") or ""
                    ),
                    "created_at": str(row["created_at"] or ""),
                }
            )
    return {
        "status": (
            "confirmed"
            if not plan["candidates"] and len(evidence) == int(expected_rows)
            else "pending"
        ),
        "transition_run_id": transition_run_id,
        "expected_rows": int(expected_rows),
        "remaining_candidates": len(plan["candidates"]),
        "restored": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        choices=("dry-run", "apply", "readback"),
        default="dry-run",
    )
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--transition-run-id", required=True)
    parser.add_argument("--expected-rows", type=int, required=True)
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--actor", default="repo_owned_cli")
    args = parser.parse_args()
    runtime_dir = args.runtime_dir.expanduser().resolve()
    if int(args.expected_rows) <= 0:
        raise ValueError("prefilter skip recovery requires positive --expected-rows")
    if args.action == "dry-run":
        with closing(_open_ro(runtime_dir)) as conn:
            result = build_plan(
                conn,
                transition_run_id=str(args.transition_run_id),
                expected_rows=int(args.expected_rows),
            )
    elif args.action == "apply":
        if not str(args.fingerprint or "").strip():
            raise ValueError("apply requires --fingerprint")
        result = apply_plan(
            runtime_dir,
            transition_run_id=str(args.transition_run_id),
            expected_rows=int(args.expected_rows),
            expected_fingerprint=str(args.fingerprint),
            actor=str(args.actor),
        )
    else:
        result = readback(
            runtime_dir,
            transition_run_id=str(args.transition_run_id),
            expected_rows=int(args.expected_rows),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
