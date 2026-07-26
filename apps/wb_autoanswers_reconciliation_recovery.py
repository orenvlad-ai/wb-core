#!/usr/bin/env python3
"""Fingerprint-bound recovery for a stalled Autoanswers policy sweep.

Dry-run/readback are query-only. Apply adds only exact per-sweep preservation
acknowledgements plus one audit event and resets the sweep's derived
cursor/progress projection. It never changes jobs, replies, publication
aggregates/attempts, provider evidence, reservations, costs or run limits.
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

from packages.application.wb_autoanswers_runtime import (  # noqa: E402
    AUTOANSWERS_DB_FILENAME,
    EVALUATION_SIGNATURE,
    PROMPT_BUNDLE_VERSION,
    SCHEMA_VERSION,
    AutoanswersRepository,
    _sha256_path,
    _verified_compressed_schema_backup_status,
)
from packages.application.sqlite_contention import connect_sqlite  # noqa: E402


CONTRACT = "wb_autoanswers_reconciliation_recovery_v1"
EVENT = "reconciliation_ack_recovery_applied"
MAX_CANDIDATES = 5_000


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _candidate_projection(
    row: Mapping[str, Any],
    *,
    excluded: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    projection = {
        key: row[key]
        for key in row.keys()
        if key not in excluded
    }
    result_json = projection.pop("result_json", None)
    projection["result_json_fingerprint"] = _fingerprint(
        str(result_json or "")
    )
    return projection


def _open(runtime_dir: Path, *, read_only: bool) -> sqlite3.Connection:
    database = (runtime_dir / AUTOANSWERS_DB_FILENAME).resolve()
    if read_only:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=15)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = connect_sqlite(
            database,
            priority="background",
            isolation_level=None,
        )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _verified_backup(runtime_dir: Path) -> dict[str, Any]:
    compressed = _verified_compressed_schema_backup_status(
        runtime_dir,
        verify_bytes=True,
    )
    if int(compressed.get("count") or 0) > 0:
        return {
            "verified": True,
            "kind": "compressed",
            "manifest": compressed["manifest_filename"],
            "sha256": compressed["snapshot_sha256"],
        }
    backup_dir = runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
    databases = sorted(backup_dir.glob("*.sqlite3"))
    if not databases:
        return {"verified": False, "kind": None}
    path = databases[-1]
    with sqlite3.connect(
        f"file:{path.resolve()}?mode=ro",
        uri=True,
        timeout=30,
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    return {
        "verified": integrity == "ok",
        "kind": "sqlite",
        "filename": path.name,
        "sha256": "sha256:" + _sha256_path(path),
    }


def _preserved_candidates(
    conn: sqlite3.Connection,
    *,
    sweep_id: str,
    transition_run_id: str,
    maximum: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH active_members AS (
            SELECT
                rs.feedback_id,
                rs.content_version_at_preview AS content_version,
                rs.content_version_hash_at_preview AS content_version_hash
            FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope rs
            WHERE rs.sweep_id=?
            UNION
            SELECT
                ra.feedback_id,
                ra.content_version,
                ra.content_version_hash
            FROM sheet_vitrina_v1_wb_autoanswers_rolling_admissions ra
            WHERE ra.transition_run_id=?
        )
        SELECT
            m.feedback_id,
            m.content_version,
            m.content_version_hash,
            j.processing_key,
            j.state AS job_state,
            j.policy_epoch AS job_policy_epoch,
            j.enable_epoch AS job_enable_epoch,
            j.transition_run_id AS job_transition_run_id,
            j.regeneration_required,
            j.last_error_code,
            j.attempts AS job_attempts,
            j.final_reply_sha256,
            j.actual_cost_usd AS job_actual_cost_usd,
            j.result_json,
            j.completed_at AS job_completed_at,
            p.publication_key,
            p.state AS publication_state,
            p.policy_epoch AS publication_policy_epoch,
            p.transition_run_id AS publication_transition_run_id,
            p.normalized_reply_sha256,
            p.attempts AS publication_attempts,
            p.write_started_at,
            p.readback_hash,
            p.last_error_code AS publication_error_code,
            (
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_publication_attempts pa
                WHERE pa.publication_key=p.publication_key
            ) AS write_attempt_count,
            (
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_cost_events ce
                WHERE ce.processing_key=j.processing_key
            ) AS cost_event_count,
            (
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events fe
                WHERE fe.processing_key=j.processing_key
            ) AS failed_cost_event_count,
            (
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswer_job_revisions jr
                WHERE jr.processing_key=j.processing_key
            ) AS revision_count,
            (
                SELECT json_object(
                    'status',br.status,
                    'reserved_usd',br.reserved_usd,
                    'actual_cost_usd',br.actual_cost_usd,
                    'provider_call_started_at',br.provider_call_started_at,
                    'released_reason',br.released_reason,
                    'settled_at',br.settled_at
                )
                FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations br
                WHERE br.processing_key=j.processing_key
            ) AS reservation_evidence,
            CASE
                WHEN p.write_started_at IS NOT NULL
                     AND COALESCE(p.state,'')<>'published'
                    THEN 'readback_preserved'
                WHEN j.state='published' AND p.state='published'
                    THEN 'published_preserved'
                WHEN j.state='skipped'
                    THEN 'skipped_preserved'
                WHEN j.state='terminal_error'
                    THEN 'terminal_error_preserved'
                WHEN j.state='needs_review'
                    THEN 'review_required_preserved'
                ELSE NULL
            END AS outcome
        FROM active_members m
        JOIN sheet_vitrina_v1_wb_feedbacks f
          ON f.feedback_id=m.feedback_id
         AND f.content_version=m.content_version
         AND f.content_version_hash=m.content_version_hash
        JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
          ON j.feedback_id=m.feedback_id
         AND j.content_version=m.content_version
         AND j.content_version_hash=m.content_version_hash
         AND j.bundle_version=?
        LEFT JOIN sheet_vitrina_v1_wb_publication_jobs p
          ON p.processing_key=j.processing_key
        WHERE COALESCE(f.answer_text,'')=''
          AND (
              j.state IN ('needs_review','terminal_error','skipped','published')
              OR p.write_started_at IS NOT NULL
          )
          AND NOT EXISTS(
              SELECT 1
              FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements a
              WHERE a.sweep_id=?
                AND a.feedback_id=m.feedback_id
                AND a.content_version=m.content_version
                AND a.content_version_hash=m.content_version_hash
          )
        ORDER BY
            CASE f.content_classification
                WHEN 'content_bearing' THEN
                    CASE WHEN f.rating BETWEEN 1 AND 5 THEN f.rating ELSE 6 END
                WHEN 'indeterminate' THEN 6
                ELSE 7
            END,
            COALESCE(f.created_at_wb,f.first_seen_at) DESC,
            f.feedback_id DESC,
            f.content_version DESC
        LIMIT ?
        """,
        (
            sweep_id,
            transition_run_id,
            PROMPT_BUNDLE_VERSION,
            sweep_id,
            int(maximum) + 1,
        ),
    ).fetchall()
    candidates = [
        _candidate_projection(row)
        for row in rows
        if row["outcome"]
    ]
    return candidates


def _non_target(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswer_jobs) AS jobs,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs) AS publications,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts) AS wb_writes,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events) AS costs,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events) AS failed_costs,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds) AS legacy_holds,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts) AS attempt_holds,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswer_job_revisions) AS revisions,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations) AS reservations,
          (SELECT COALESCE(SUM(CAST(actual_cost_usd AS REAL)),0)
           FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations) AS reservation_actual,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs
           WHERE state='published' AND readback_hash IS NOT NULL) AS published_readbacks
        """
    ).fetchone()
    return dict(row)


def _target_evidence_from_acknowledgements(
    conn: sqlite3.Connection,
    *,
    sweep_id: str,
    member_fingerprints: list[str],
) -> list[dict[str, Any]]:
    if not member_fingerprints:
        return []
    rows = conn.execute(
        """
        WITH expected(candidate_fingerprint) AS (
            SELECT CAST(value AS TEXT)
            FROM json_each(?)
        )
        SELECT
            a.candidate_fingerprint,
            a.feedback_id,
            a.content_version,
            a.content_version_hash,
            j.processing_key,
            j.state AS job_state,
            j.policy_epoch AS job_policy_epoch,
            j.enable_epoch AS job_enable_epoch,
            j.transition_run_id AS job_transition_run_id,
            j.regeneration_required,
            j.last_error_code,
            j.attempts AS job_attempts,
            j.final_reply_sha256,
            j.actual_cost_usd AS job_actual_cost_usd,
            j.result_json,
            j.completed_at AS job_completed_at,
            p.publication_key,
            p.state AS publication_state,
            p.policy_epoch AS publication_policy_epoch,
            p.transition_run_id AS publication_transition_run_id,
            p.normalized_reply_sha256,
            p.attempts AS publication_attempts,
            p.write_started_at,
            p.readback_hash,
            p.last_error_code AS publication_error_code,
            (
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_publication_attempts pa
                WHERE pa.publication_key=p.publication_key
            ) AS write_attempt_count,
            (
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_cost_events ce
                WHERE ce.processing_key=j.processing_key
            ) AS cost_event_count,
            (
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events fe
                WHERE fe.processing_key=j.processing_key
            ) AS failed_cost_event_count,
            (
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_wb_autoanswer_job_revisions jr
                WHERE jr.processing_key=j.processing_key
            ) AS revision_count,
            (
                SELECT json_object(
                    'status',br.status,
                    'reserved_usd',br.reserved_usd,
                    'actual_cost_usd',br.actual_cost_usd,
                    'provider_call_started_at',br.provider_call_started_at,
                    'released_reason',br.released_reason,
                    'settled_at',br.settled_at
                )
                FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations br
                WHERE br.processing_key=j.processing_key
            ) AS reservation_evidence,
            a.outcome
        FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements a
        JOIN expected e
          ON e.candidate_fingerprint=a.candidate_fingerprint
        JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
          ON j.feedback_id=a.feedback_id
         AND j.content_version=a.content_version
         AND j.content_version_hash=a.content_version_hash
         AND j.bundle_version=?
        LEFT JOIN sheet_vitrina_v1_wb_publication_jobs p
          ON p.processing_key=j.processing_key
        WHERE a.sweep_id=?
        ORDER BY a.candidate_fingerprint
        """,
        (
            _canonical(member_fingerprints),
            PROMPT_BUNDLE_VERSION,
            sweep_id,
        ),
    ).fetchall()
    return [
        {
            "candidate_fingerprint": str(row["candidate_fingerprint"]),
            "projection": _candidate_projection(
                row,
                excluded=frozenset({"candidate_fingerprint"}),
            ),
        }
        for row in rows
    ]


def _sweep_identity(
    conn: sqlite3.Connection,
    *,
    sweep_id: str,
    expected_policy_epoch: int,
    transition_run_id: str,
) -> dict[str, Any]:
    settings = conn.execute(
        """
        SELECT master_enabled,mode,enable_epoch,policy_epoch
        FROM sheet_vitrina_v1_wb_autoanswers_settings
        WHERE singleton=1
        """
    ).fetchone()
    sweep = conn.execute(
        """
        SELECT
            sweep_id,policy_epoch,target_mode,state,transition_run_id,
            run_max_usd,run_max_paid_reviews,created_at,updated_at
        FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
        WHERE sweep_id=?
        """,
        (sweep_id,),
    ).fetchone()
    if settings is None or sweep is None:
        raise RuntimeError("active reconciliation sweep is missing")
    if int(settings["policy_epoch"]) != int(expected_policy_epoch):
        raise RuntimeError("settings policy epoch changed")
    if int(sweep["policy_epoch"]) != int(expected_policy_epoch):
        raise RuntimeError("sweep policy epoch mismatch")
    if str(sweep["transition_run_id"] or sweep["sweep_id"]) != transition_run_id:
        raise RuntimeError("transition run identity mismatch")
    if str(sweep["state"]) not in {"queued", "processing", "retryable_error"}:
        raise RuntimeError("reconciliation sweep is not active")
    return {
        "settings": dict(settings),
        "sweep": dict(sweep),
    }


def build_plan(
    conn: sqlite3.Connection,
    *,
    runtime_dir: Path,
    sweep_id: str,
    expected_policy_epoch: int,
    transition_run_id: str,
    expected_candidates: int,
    maximum: int = MAX_CANDIDATES,
    backup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if maximum < 1 or maximum > MAX_CANDIDATES:
        raise ValueError(f"maximum must be in 1..{MAX_CANDIDATES}")
    identity = _sweep_identity(
        conn,
        sweep_id=sweep_id,
        expected_policy_epoch=expected_policy_epoch,
        transition_run_id=transition_run_id,
    )
    candidates = _preserved_candidates(
        conn,
        sweep_id=sweep_id,
        transition_run_id=transition_run_id,
        maximum=maximum,
    )
    bounded = len(candidates) <= maximum
    if not bounded:
        candidates = candidates[:maximum]
    backup_evidence = dict(backup or _verified_backup(runtime_dir))
    target_identity = {
        "contract": CONTRACT,
        "sweep_id": sweep_id,
        "policy_epoch": int(expected_policy_epoch),
        "transition_run_id": transition_run_id,
        "target_mode": identity["sweep"]["target_mode"],
        "run_max_usd": identity["sweep"]["run_max_usd"],
        "run_max_paid_reviews": identity["sweep"]["run_max_paid_reviews"],
        "sweep_state": identity["sweep"]["state"],
        "candidates": candidates,
        "schema_backup": backup_evidence,
    }
    candidate_counts: dict[str, int] = {}
    for candidate in candidates:
        outcome = str(candidate["outcome"])
        candidate_counts[outcome] = candidate_counts.get(outcome, 0) + 1
    fingerprint = _fingerprint(target_identity)
    non_target = _non_target(conn)
    return {
        **target_identity,
        "candidate_count": len(candidates),
        "candidate_counts": candidate_counts,
        "coverage_confirmed": (
            bounded and len(candidates) == int(expected_candidates)
        ),
        "bounded": bounded,
        "maximum": maximum,
        "plan_fingerprint": fingerprint,
        "pre_change_digest": _fingerprint(
            {**target_identity, "non_target_snapshot": non_target}
        ),
        "non_target_snapshot": non_target,
        "captured_at": _now(),
        "expected_affected_records": {
            "acknowledgements_inserted": len(candidates),
            "sweeps_updated": 1 if candidates else 0,
            "audit_events_appended": 1 if candidates else 0,
            "jobs_changed": 0,
            "provider_calls_created": 0,
            "cost_events_created": 0,
            "publication_jobs_created": 0,
            "wb_writes_created": 0,
        },
        "non_target_invariants": {
            "job_execution_identity_unchanged": True,
            "publication_and_readback_evidence_unchanged": True,
            "provider_cost_reservation_and_hold_evidence_unchanged": True,
            "run_identity_and_caps_unchanged": True,
        },
    }


def _prior_apply(
    conn: sqlite3.Connection,
    *,
    expected_fingerprint: str,
) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT details_json
        FROM sheet_vitrina_v1_wb_autoanswers_audit_events
        WHERE event_type=?
        ORDER BY created_at DESC
        """,
        (EVENT,),
    ).fetchall()
    for row in rows:
        try:
            details = json.loads(str(row["details_json"] or "{}"))
        except ValueError:
            continue
        if str(details.get("plan_fingerprint") or "") == expected_fingerprint:
            return details
    return None


def _rebuild_sweep_projection(
    conn: sqlite3.Connection,
    *,
    sweep_id: str,
    transition_run_id: str,
    policy_epoch: int,
    target_mode: str,
    stamp: str,
) -> dict[str, Any]:
    outcome_rows = conn.execute(
        """
        SELECT outcome,outcome_class,COUNT(*) AS count
        FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements
        WHERE sweep_id=?
        GROUP BY outcome,outcome_class
        ORDER BY outcome
        """,
        (sweep_id,),
    ).fetchall()
    progress = {str(row["outcome"]): int(row["count"]) for row in outcome_rows}
    acknowledgements = sum(int(row["count"]) for row in outcome_rows)
    action_total = sum(
        int(row["count"]) for row in outcome_rows if row["outcome_class"] == "action"
    )
    preserved_total = sum(
        int(row["count"])
        for row in outcome_rows
        if row["outcome_class"] == "preserved"
    )
    unchanged_total = sum(
        int(row["count"])
        for row in outcome_rows
        if row["outcome_class"] == "unchanged"
    )
    membership = conn.execute(
        """
        WITH active_members AS (
            SELECT
                rs.feedback_id,
                rs.content_version_at_preview AS content_version,
                rs.content_version_hash_at_preview AS content_version_hash
            FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_scope rs
            WHERE rs.sweep_id=?
            UNION
            SELECT
                ra.feedback_id,
                ra.content_version,
                ra.content_version_hash
            FROM sheet_vitrina_v1_wb_autoanswers_rolling_admissions ra
            WHERE ra.transition_run_id=?
        )
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN
                EXISTS(
                    SELECT 1
                    FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements a
                    WHERE a.sweep_id=?
                      AND a.feedback_id=m.feedback_id
                      AND a.content_version=m.content_version
                      AND a.content_version_hash=m.content_version_hash
                )
                THEN 1 ELSE 0 END
            ) AS reconciled
        FROM active_members m
        JOIN sheet_vitrina_v1_wb_feedbacks f
          ON f.feedback_id=m.feedback_id
         AND f.content_version=m.content_version
         AND f.content_version_hash=m.content_version_hash
        """,
        (
            sweep_id,
            transition_run_id,
            sweep_id,
        ),
    ).fetchone()
    total = int(membership["total"] or 0)
    reconciled = min(total, int(membership["reconciled"] or 0))
    remaining = max(0, total - reconciled)
    priority_bucket = AutoanswersRepository._automatic_priority_bucket(
        conn,
        transition_run_id=transition_run_id,
        policy_epoch=int(policy_epoch),
        target_mode=target_mode,
    )
    queue_depth = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM sheet_vitrina_v1_wb_autoanswer_jobs
            WHERE policy_epoch=?
              AND state IN ('queued','processing','retryable_error')
            """,
            (int(policy_epoch),),
        ).fetchone()[0]
    )
    sweep_state = (
        "queued"
        if remaining > 0 or priority_bucket is not None
        else "succeeded"
    )
    cursor = {
        "materialized_total": reconciled,
        "acknowledged_total": acknowledgements,
        "action_total": action_total,
        "preserved_total": preserved_total,
        "unchanged_total": unchanged_total,
        "membership_total": total,
        "reconciliation_remaining": remaining,
        "last_progress_at": stamp,
        "candidate_batch_fingerprint": None,
        "repeated_candidate_batches": 0,
        "rate_per_minute": 0.0,
        "eta_minutes": None if remaining else 0.0,
        "queue_depth": queue_depth,
        "priority_bucket": priority_bucket,
        "priority_bucket_since": stamp if priority_bucket is not None else None,
        "observed_at": stamp,
        "recovery_contract": CONTRACT,
    }
    conn.execute(
        """
        UPDATE sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
        SET state=?,cursor_json=?,progress_json=?,
            pause_reason='reconciliation_recovery_applied',
            lease_owner=NULL,lease_until=NULL,
            completed_at=CASE WHEN ?='succeeded' THEN ? ELSE NULL END,
            updated_at=?
        WHERE sweep_id=?
        """,
        (
            sweep_state,
            _canonical(cursor),
            _canonical(progress),
            sweep_state,
            stamp,
            stamp,
            sweep_id,
        ),
    )
    return {"state": sweep_state, "cursor": cursor, "progress": progress}


def apply_plan(
    runtime_dir: Path,
    *,
    sweep_id: str,
    expected_policy_epoch: int,
    transition_run_id: str,
    expected_candidates: int,
    expected_fingerprint: str,
    actor: str,
    maximum: int = MAX_CANDIDATES,
) -> dict[str, Any]:
    with closing(_open(runtime_dir, read_only=True)) as read_conn:
        prior = _prior_apply(
            read_conn,
            expected_fingerprint=expected_fingerprint,
        )
        if prior is not None:
            if (
                str(prior.get("sweep_id") or "") != sweep_id
                or int(prior.get("policy_epoch") or -1)
                != int(expected_policy_epoch)
                or str(prior.get("transition_run_id") or "")
                != transition_run_id
                or int(prior.get("acknowledgement_count") or -1)
                != int(expected_candidates)
            ):
                raise RuntimeError(
                    "prior reconciliation recovery identity does not match arguments"
                )
            confirmed = readback(
                read_conn,
                expected_fingerprint=expected_fingerprint,
            )
            if confirmed["status"] != "confirmed":
                raise RuntimeError(
                    "prior reconciliation recovery readback no longer matches"
                )
            return {
                "status": "already_reconciled",
                "idempotent": True,
                "plan_fingerprint": expected_fingerprint,
                "acknowledgement_count": int(
                    confirmed["acknowledgement_count"]
                ),
                "readback": confirmed,
            }
        preflight = build_plan(
            read_conn,
            runtime_dir=runtime_dir,
            sweep_id=sweep_id,
            expected_policy_epoch=expected_policy_epoch,
            transition_run_id=transition_run_id,
            expected_candidates=expected_candidates,
            maximum=maximum,
        )
    if preflight["plan_fingerprint"] != expected_fingerprint:
        raise RuntimeError("reconciliation recovery evidence changed; create a new plan")
    if not preflight["coverage_confirmed"]:
        raise RuntimeError("reconciliation recovery candidate coverage mismatch")
    if not preflight["schema_backup"]["verified"]:
        raise RuntimeError("verified pre-v8 schema backup is required")
    if preflight["candidate_count"] == 0:
        raise RuntimeError("reconciliation recovery has no candidates")

    conn = _open(runtime_dir, read_only=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        plan = build_plan(
            conn,
            runtime_dir=runtime_dir,
            sweep_id=sweep_id,
            expected_policy_epoch=expected_policy_epoch,
            transition_run_id=transition_run_id,
            expected_candidates=expected_candidates,
            maximum=maximum,
            backup=preflight["schema_backup"],
        )
        if plan["plan_fingerprint"] != expected_fingerprint:
            raise RuntimeError("reconciliation recovery target drifted before apply")
        before = dict(plan["non_target_snapshot"])
        stamp = _now()
        inserted = 0
        member_fingerprints: list[str] = []
        for candidate in plan["candidates"]:
            candidate_fingerprint = _fingerprint(
                {
                    "sweep_id": sweep_id,
                    "feedback_id": candidate["feedback_id"],
                    "content_version": candidate["content_version"],
                    "content_version_hash": candidate["content_version_hash"],
                    "policy_epoch": int(expected_policy_epoch),
                    "transition_run_id": transition_run_id,
                    "outcome": candidate["outcome"],
                }
            )
            cursor = conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements(
                    sweep_id,feedback_id,content_version,content_version_hash,
                    policy_epoch,transition_run_id,outcome,outcome_class,
                    candidate_fingerprint,acknowledged_at
                ) VALUES(?,?,?,?,?,?,?,'preserved',?,?)
                """,
                (
                    sweep_id,
                    candidate["feedback_id"],
                    int(candidate["content_version"]),
                    candidate["content_version_hash"],
                    int(expected_policy_epoch),
                    transition_run_id,
                    candidate["outcome"],
                    candidate_fingerprint,
                    stamp,
                ),
            )
            inserted += int(cursor.rowcount or 0)
            member_fingerprints.append(candidate_fingerprint)
        if inserted != int(expected_candidates):
            raise RuntimeError("reconciliation recovery affected-row mismatch")
        cursor_projection = _rebuild_sweep_projection(
            conn,
            sweep_id=sweep_id,
            transition_run_id=transition_run_id,
            policy_epoch=expected_policy_epoch,
            target_mode=str(plan["target_mode"]),
            stamp=stamp,
        )
        details = {
            "contract": CONTRACT,
            "plan_fingerprint": expected_fingerprint,
            "sweep_id": sweep_id,
            "transition_run_id": transition_run_id,
            "policy_epoch": int(expected_policy_epoch),
            "acknowledgement_count": inserted,
            "member_fingerprints": member_fingerprints,
            "target_evidence_digest": _fingerprint(
                sorted(
                    (
                        {
                            "candidate_fingerprint": member_fingerprint,
                            "projection": candidate,
                        }
                        for member_fingerprint, candidate in zip(
                            member_fingerprints,
                            plan["candidates"],
                            strict=True,
                        )
                    ),
                    key=lambda item: item["candidate_fingerprint"],
                )
            ),
            "run_max_usd": plan["run_max_usd"],
            "run_max_paid_reviews": plan["run_max_paid_reviews"],
            "pre_change_digest": plan["pre_change_digest"],
            "non_target_snapshot": before,
            "jobs_changed": 0,
            "provider_calls_created": 0,
            "publication_jobs_created": 0,
            "wb_writes_created": 0,
        }
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_wb_autoanswers_audit_events(
                event_id,aggregate_type,aggregate_id,event_type,
                previous_state,next_state,actor_type,actor_id,
                bundle_version,evaluation_signature,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                uuid4().hex,
                "reconciliation_sweep",
                sweep_id,
                EVENT,
                plan["sweep_state"],
                cursor_projection["state"],
                "operator",
                actor,
                PROMPT_BUNDLE_VERSION,
                EVALUATION_SIGNATURE,
                _canonical(details),
                stamp,
            ),
        )
        after = _non_target(conn)
        if after != before:
            raise RuntimeError("reconciliation recovery changed non-target evidence")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "status": "reconciled",
        "idempotent": False,
        "plan_fingerprint": expected_fingerprint,
        "pre_change_digest": preflight["pre_change_digest"],
        "acknowledgement_count": inserted,
        "cursor": cursor_projection,
        "affected_records": preflight["expected_affected_records"],
        "non_target_invariants_preserved": True,
    }


def readback(
    conn: sqlite3.Connection,
    *,
    expected_fingerprint: str,
) -> dict[str, Any]:
    prior = _prior_apply(conn, expected_fingerprint=expected_fingerprint)
    if prior is None:
        return {
            "status": "not_applied",
            "plan_fingerprint": expected_fingerprint,
        }
    expected_members = list(prior.get("member_fingerprints") or [])
    actual_members = [
        str(row["candidate_fingerprint"])
        for row in conn.execute(
            """
            SELECT candidate_fingerprint
            FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements
            WHERE sweep_id=? AND candidate_fingerprint IN (
                SELECT value FROM json_each(?)
            )
            ORDER BY candidate_fingerprint
            """,
            (
                prior["sweep_id"],
                _canonical(expected_members),
            ),
        ).fetchall()
    ]
    actual = len(actual_members)
    expected_sorted = sorted(str(value) for value in expected_members)
    target_evidence = _target_evidence_from_acknowledgements(
        conn,
        sweep_id=str(prior["sweep_id"]),
        member_fingerprints=expected_sorted,
    )
    target_evidence_matches = (
        _fingerprint(target_evidence)
        == str(prior.get("target_evidence_digest") or "")
    )
    sweep = conn.execute(
        """
        SELECT policy_epoch,transition_run_id,run_max_usd,run_max_paid_reviews
        FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
        WHERE sweep_id=?
        """,
        (prior["sweep_id"],),
    ).fetchone()
    run_identity_and_caps_match = bool(
        sweep is not None
        and int(sweep["policy_epoch"]) == int(prior["policy_epoch"])
        and str(sweep["transition_run_id"] or prior["sweep_id"])
        == str(prior["transition_run_id"])
        and sweep["run_max_usd"] == prior.get("run_max_usd")
        and sweep["run_max_paid_reviews"]
        == prior.get("run_max_paid_reviews")
    )
    confirmed = (
        actual == int(prior["acknowledgement_count"])
        and actual_members == expected_sorted
        and target_evidence_matches
        and run_identity_and_caps_match
    )
    return {
        "status": "confirmed" if confirmed else "mismatch",
        "plan_fingerprint": expected_fingerprint,
        "acknowledgement_count": actual,
        "expected_acknowledgement_count": int(prior["acknowledgement_count"]),
        "member_fingerprints_match": actual_members == expected_sorted,
        "target_execution_evidence_match": target_evidence_matches,
        "run_identity_and_caps_match": run_identity_and_caps_match,
        "non_target_invariants_preserved": True,
        "non_target_invariant_proof": "apply_transaction_before_after_match",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("dry-run", "apply", "readback"))
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--sweep-id", required=True)
    parser.add_argument("--policy-epoch", required=True, type=int)
    parser.add_argument("--transition-run-id", required=True)
    parser.add_argument("--expected-candidates", required=True, type=int)
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--maximum", type=int, default=MAX_CANDIDATES)
    parser.add_argument("--actor", default="codex-loop")
    args = parser.parse_args()
    if args.expected_candidates < 0:
        raise SystemExit("--expected-candidates must be non-negative")
    if args.command in {"apply", "readback"} and not args.expected_fingerprint:
        raise SystemExit("--expected-fingerprint is required")
    if args.command == "apply":
        payload = apply_plan(
            args.runtime_dir,
            sweep_id=args.sweep_id,
            expected_policy_epoch=args.policy_epoch,
            transition_run_id=args.transition_run_id,
            expected_candidates=args.expected_candidates,
            expected_fingerprint=args.expected_fingerprint,
            actor=args.actor,
            maximum=args.maximum,
        )
    else:
        with closing(_open(args.runtime_dir, read_only=True)) as conn:
            payload = (
                readback(
                    conn,
                    expected_fingerprint=args.expected_fingerprint,
                )
                if args.command == "readback"
                else build_plan(
                    conn,
                    runtime_dir=args.runtime_dir,
                    sweep_id=args.sweep_id,
                    expected_policy_epoch=args.policy_epoch,
                    transition_run_id=args.transition_run_id,
                    expected_candidates=args.expected_candidates,
                    maximum=args.maximum,
                )
            )
    print(_canonical(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
