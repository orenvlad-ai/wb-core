#!/usr/bin/env python3
"""Fingerprint-bound recovery for content-bearing skips and opaque exits.

Dry-run/readback use SQLite ``mode=ro`` plus ``query_only``.  Apply requires
the verified pre-v7 schema backup, changes only exact unpublished candidates,
archives their prior projection, preserves cost/uncertainty/audit evidence,
and creates no provider call, cost event, publication row or WB write.  The
approval fingerprint binds only target evidence plus the verified backup;
mutable unrelated aggregates are captured separately and protected by an
apply-time transaction-local before/after invariant.
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
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_autoanswers_runtime import (  # noqa: E402
    AUTOANSWERS_DB_FILENAME,
    EVALUATION_SIGNATURE,
    PROMPT_BUNDLE_VERSION,
    SCHEMA_VERSION,
    _sha256_path,
    _verified_compressed_schema_backup_status,
)
from packages.application.sqlite_contention import connect_sqlite  # noqa: E402


CONTRACT = "wb_autoanswers_rolling_recovery_v1"
EVENT = "rolling_runtime_recovery_applied"
DATABASE_FILENAME = AUTOANSWERS_DB_FILENAME


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _open(runtime_dir: Path, *, read_only: bool) -> sqlite3.Connection:
    database = (runtime_dir / DATABASE_FILENAME).resolve()
    if read_only:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = connect_sqlite(
            database,
            timeout_ms=30_000,
            priority="background",
            isolation_level=None,
        )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _verified_backup(runtime_dir: Path) -> dict[str, Any]:
    backup_dir = runtime_dir / "backups" / f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
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
    databases = sorted(backup_dir.glob("*.sqlite3"))
    if databases:
        path = databases[-1]
        with sqlite3.connect(
            f"file:{path.resolve()}?mode=ro", uri=True, timeout=30
        ) as conn:
            conn.execute("PRAGMA query_only=ON")
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        return {
            "verified": integrity == "ok",
            "kind": "sqlite",
            "filename": path.name,
            "sha256": "sha256:" + _sha256_path(path),
        }
    return {"verified": False, "kind": None}


def _candidates(
    conn: sqlite3.Connection,
    *,
    transition_run_id: str,
) -> dict[str, list[dict[str, Any]]]:
    empty_rows = conn.execute(
        """
        SELECT
          j.processing_key,j.feedback_id,j.content_version,j.content_version_hash,
          j.state,j.last_error_code,j.attempts,j.media_processing_version,
          j.policy_epoch,j.transition_run_id,j.result_json,j.final_route,
          j.final_reply,j.final_reply_sha256,j.media_uncertain,j.actual_cost_usd,
          r.status AS reservation_status,r.actual_cost_usd AS reservation_actual,
          r.provider_call_started_at,
          f.content_classification,f.rating,f.content_json,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_feedback_media m
           WHERE m.feedback_id=j.feedback_id
             AND m.content_version=j.content_version) AS media_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs p
           WHERE p.processing_key=j.processing_key) AS publication_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events c
           WHERE c.processing_key=j.processing_key) AS cost_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events c
           WHERE c.processing_key=j.processing_key) AS failed_cost_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswer_job_revisions x
           WHERE x.processing_key=j.processing_key
             AND x.media_processing_version=j.media_processing_version) AS revision_conflict
        FROM sheet_vitrina_v1_wb_autoanswer_jobs j
        JOIN sheet_vitrina_v1_wb_feedbacks f
          ON f.feedback_id=j.feedback_id
         AND f.content_version=j.content_version
         AND f.content_version_hash=j.content_version_hash
        JOIN sheet_vitrina_v1_wb_autoanswers_budget_reservations r
          ON r.processing_key=j.processing_key
        WHERE j.transition_run_id=?
          AND j.state='skipped'
          AND j.last_error_code='empty_five_star'
          AND f.content_classification='content_bearing'
          AND f.rating=5
          AND COALESCE(f.answer_text,'')=''
          AND COALESCE(json_extract(f.content_json,'$.text'),'')=''
          AND COALESCE(json_extract(f.content_json,'$.pros'),'')=''
          AND COALESCE(json_extract(f.content_json,'$.cons'),'')=''
          AND (
            json_array_length(COALESCE(json_extract(f.content_json,'$.tags'),'[]'))>0
            OR EXISTS(
              SELECT 1 FROM sheet_vitrina_v1_wb_feedback_media m
              WHERE m.feedback_id=j.feedback_id
                AND m.content_version=j.content_version
            )
          )
          AND r.status='settled'
          AND CAST(COALESCE(r.actual_cost_usd,'0') AS REAL)=0
        ORDER BY j.processing_key
        """,
        (transition_run_id,),
    ).fetchall()
    node_rows = conn.execute(
        """
        SELECT
          j.processing_key,j.feedback_id,j.content_version,j.content_version_hash,
          j.state,j.last_error_code,j.attempts,j.media_processing_version,
          j.policy_epoch,j.transition_run_id,j.result_json,j.final_route,
          j.final_reply,j.final_reply_sha256,j.media_uncertain,j.actual_cost_usd,
          r.status AS reservation_status,r.actual_cost_usd AS reservation_actual,
          r.provider_call_started_at,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs p
           WHERE p.processing_key=j.processing_key) AS publication_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events c
           WHERE c.processing_key=j.processing_key) AS cost_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events c
           WHERE c.processing_key=j.processing_key) AS failed_cost_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds h
           WHERE h.processing_key=j.processing_key) AS legacy_hold_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswer_job_revisions x
           WHERE x.processing_key=j.processing_key
             AND x.media_processing_version=j.media_processing_version) AS revision_conflict
        FROM sheet_vitrina_v1_wb_autoanswer_jobs j
        JOIN sheet_vitrina_v1_wb_feedbacks f
          ON f.feedback_id=j.feedback_id
         AND f.content_version=j.content_version
         AND f.content_version_hash=j.content_version_hash
        JOIN sheet_vitrina_v1_wb_autoanswers_budget_reservations r
          ON r.processing_key=j.processing_key
        WHERE j.transition_run_id=?
          AND j.state='terminal_error'
          AND j.last_error_code='node_process_exit_1'
          AND j.attempts=1
          AND COALESCE(f.answer_text,'')=''
          AND r.status='released'
          AND CAST(COALESCE(r.actual_cost_usd,'0') AS REAL)=0
        ORDER BY j.processing_key
        """,
        (transition_run_id,),
    ).fetchall()

    def eligible(row: sqlite3.Row, *, node: bool) -> bool:
        if any(
            int(row[name] or 0)
            for name in ("publication_count", "cost_count", "failed_cost_count", "revision_conflict")
        ):
            return False
        return not node or int(row["legacy_hold_count"] or 0) == 1

    return {
        "empty_five_star": [
            dict(row) for row in empty_rows if eligible(row, node=False)
        ],
        "node_process_exit_1": [
            dict(row) for row in node_rows if eligible(row, node=True)
        ],
    }


def _non_target(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs) AS publications,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts) AS wb_writes,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events) AS costs,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events) AS failed_costs,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds) AS legacy_holds,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts) AS attempt_holds,
          (SELECT COALESCE(SUM(CAST(actual_cost_usd AS REAL)),0)
           FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations) AS reservation_actual
        """
    ).fetchone()
    return dict(row)


def build_plan(
    conn: sqlite3.Connection,
    *,
    runtime_dir: Path,
    transition_run_id: str,
    expected_empty: int,
    expected_node: int,
) -> dict[str, Any]:
    candidates = _candidates(conn, transition_run_id=transition_run_id)
    backup = _verified_backup(runtime_dir)
    target_identity = {
        "contract": CONTRACT,
        "transition_run_id": transition_run_id,
        "expected": {
            "empty_five_star": int(expected_empty),
            "node_process_exit_1": int(expected_node),
        },
        "candidates": candidates,
        "schema_backup": backup,
    }
    non_target_snapshot = _non_target(conn)
    fingerprint = _fingerprint(target_identity)
    pre_change_digest = _fingerprint(
        {
            **target_identity,
            "non_target_snapshot": non_target_snapshot,
        }
    )
    counts = {key: len(rows) for key, rows in candidates.items()}
    return {
        **target_identity,
        "non_target_snapshot": non_target_snapshot,
        "captured_at": _now(),
        "candidate_counts": counts,
        "coverage_confirmed": counts == target_identity["expected"],
        "plan_fingerprint": fingerprint,
        "pre_change_digest": pre_change_digest,
        "expected_affected_records": {
            "processing_jobs_requeued": sum(counts.values()),
            "job_revisions_appended": sum(counts.values()),
            "reservations_released_for_reuse": counts["empty_five_star"],
            "audit_events_appended": sum(counts.values()),
            "provider_calls_created": 0,
            "cost_events_created": 0,
            "publication_jobs_created": 0,
            "wb_writes_created": 0,
        },
        "non_target_invariants": {
            "provider_and_cost_evidence_preserved": True,
            "legacy_uncertainty_holds_preserved": True,
            "publication_aggregates_and_writes_unchanged": True,
            "content_version_and_frozen_identity_unchanged": True,
        },
        "reversibility": {
            "kind": "verified_backup_plus_archived_job_projection",
            "backup_required": True,
            "backup_verified": bool(backup["verified"]),
        },
    }


def _prior_apply(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT aggregate_id,details_json
        FROM sheet_vitrina_v1_wb_autoanswers_audit_events
        WHERE aggregate_type='processing_job' AND event_type=?
        ORDER BY aggregate_id
        """,
        (EVENT,),
    ).fetchall()
    result: list[str] = []
    for row in rows:
        try:
            details = json.loads(str(row["details_json"] or "{}"))
        except ValueError:
            continue
        if str(details.get("plan_fingerprint") or "") == fingerprint:
            result.append(str(row["aggregate_id"]))
    return result


def apply_plan(
    runtime_dir: Path,
    *,
    transition_run_id: str,
    expected_empty: int,
    expected_node: int,
    expected_fingerprint: str,
    actor: str,
) -> dict[str, Any]:
    conn = _open(runtime_dir, read_only=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        plan = build_plan(
            conn,
            runtime_dir=runtime_dir,
            transition_run_id=transition_run_id,
            expected_empty=expected_empty,
            expected_node=expected_node,
        )
        if plan["plan_fingerprint"] != expected_fingerprint:
            prior = _prior_apply(conn, fingerprint=expected_fingerprint)
            if prior and not any(plan["candidates"].values()):
                conn.rollback()
                return {
                    "status": "already_reconciled",
                    "idempotent": True,
                    "plan_fingerprint": expected_fingerprint,
                    "processing_keys": prior,
                }
            raise RuntimeError("rolling recovery evidence changed; create a new plan")
        if not plan["coverage_confirmed"]:
            raise RuntimeError("rolling recovery candidate coverage mismatch")
        if not plan["schema_backup"]["verified"]:
            raise RuntimeError("verified pre-v7 schema backup is required")
        if not any(plan["candidates"].values()):
            raise RuntimeError("rolling recovery has no candidates")
        before = dict(plan["non_target_snapshot"])
        stamp = _now()
        recovered: list[str] = []
        for kind, rows in plan["candidates"].items():
            for row in rows:
                key = str(row["processing_key"])
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_autoanswer_job_revisions(
                      revision_id,processing_key,media_processing_version,
                      previous_state,result_json,final_route,final_reply,
                      final_reply_sha256,media_uncertain,actual_cost_usd,
                      reason,archived_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        uuid4().hex,
                        key,
                        int(row["media_processing_version"]),
                        row["state"],
                        row["result_json"],
                        row["final_route"],
                        row["final_reply"],
                        row["final_reply_sha256"],
                        row["media_uncertain"],
                        str(row["actual_cost_usd"] or "0"),
                        kind + "_rolling_recovery_v1",
                        stamp,
                    ),
                )
                cursor = conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET state='queued',processing_kind='frozen_ai',
                        media_processing_version=media_processing_version+1,
                        regeneration_required=0,regeneration_reason=NULL,
                        result_json=NULL,final_route=NULL,case_code=NULL,
                        final_reply=NULL,final_reply_sha256=NULL,
                        review_reasons_json=NULL,hard_gates_passed=NULL,
                        fallback_used=NULL,media_uncertain=NULL,
                        node_contract_valid=NULL,available_at=?,
                        lease_owner=NULL,lease_until=NULL,retry_stage=NULL,
                        last_error_code=NULL,completed_at=NULL,updated_at=?
                    WHERE processing_key=? AND transition_run_id=?
                      AND state=? AND last_error_code=?
                    """,
                    (
                        stamp,
                        stamp,
                        key,
                        transition_run_id,
                        row["state"],
                        row["last_error_code"],
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise RuntimeError("rolling recovery affected-row mismatch")
                if kind == "empty_five_star":
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_wb_autoanswers_budget_reservations
                        SET status='released',released_reason='content_bearing_recovery',
                            settled_at=NULL,updated_at=?
                        WHERE processing_key=? AND status='settled'
                          AND CAST(COALESCE(actual_cost_usd,'0') AS REAL)=0
                        """,
                        (stamp, key),
                    )
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
                        "processing_job",
                        key,
                        EVENT,
                        row["state"],
                        "queued",
                        "operator",
                        actor,
                        PROMPT_BUNDLE_VERSION,
                        EVALUATION_SIGNATURE,
                        _canonical(
                            {
                                "kind": kind,
                                "plan_fingerprint": expected_fingerprint,
                                "transition_run_id": transition_run_id,
                                "content_version_hash": row["content_version_hash"],
                                "frozen_bundle_changed": False,
                            }
                        ),
                        stamp,
                    ),
                )
                recovered.append(key)
        after = _non_target(conn)
        if after != before:
            raise RuntimeError("rolling recovery changed non-target evidence")
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
        "pre_change_digest": plan["pre_change_digest"],
        "processing_keys": recovered,
        "affected_records": plan["expected_affected_records"],
        "non_target_invariants_preserved": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("dry-run", "apply", "readback"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--transition-run-id", required=True)
    parser.add_argument("--expected-empty", type=int, required=True)
    parser.add_argument("--expected-node", type=int, required=True)
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--actor", default="codex-loop")
    args = parser.parse_args()
    if args.expected_empty < 0 or args.expected_node < 0:
        raise SystemExit("expected counts must be non-negative")
    if args.command == "apply":
        if not args.expected_fingerprint:
            raise SystemExit("--expected-fingerprint is required for apply")
        payload = apply_plan(
            args.runtime_dir,
            transition_run_id=args.transition_run_id,
            expected_empty=args.expected_empty,
            expected_node=args.expected_node,
            expected_fingerprint=args.expected_fingerprint,
            actor=args.actor,
        )
    else:
        with closing(_open(args.runtime_dir, read_only=True)) as conn:
            plan = build_plan(
                conn,
                runtime_dir=args.runtime_dir,
                transition_run_id=args.transition_run_id,
                expected_empty=args.expected_empty,
                expected_node=args.expected_node,
            )
            prior = (
                _prior_apply(conn, fingerprint=args.expected_fingerprint)
                if args.expected_fingerprint
                else []
            )
        payload = (
            {
                "status": (
                    "confirmed"
                    if prior and not any(plan["candidates"].values())
                    else "pending"
                ),
                "processing_keys": prior,
                "plan": plan,
            }
            if args.command == "readback"
            else plan
        )
    print(_canonical(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
