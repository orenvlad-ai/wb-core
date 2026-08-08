#!/usr/bin/env python3
"""Fingerprint-bound activation/rebind for WB Autoanswers owner-policy v5.

Dry-run and readback are query-only. Apply is one SQLite transaction, requires
an externally reviewed fingerprint plus a canonical worker hold, rewrites only
zero-write publication artifacts, and preserves every started write/readback.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_autoanswers_backlog_recovery import _deployed_runtime_evidence  # noqa: E402
from apps.wb_autoanswers_rolling_recovery import _verified_backup  # noqa: E402
from packages.application.sqlite_contention import connect_sqlite  # noqa: E402
from packages.application.wb_autoanswers_owner_policy import (  # noqa: E402
    OWNER_POLICY_CONTRACT,
    OWNER_POLICY_VERSION,
    apply_owner_policy,
)
from packages.application.wb_autoanswers_runtime import (  # noqa: E402
    AUTOANSWERS_DB_FILENAME,
    EVALUATION_SIGNATURE,
    PREVIOUS_POLICY_VERSION,
    PROMPT_BUNDLE_VERSION,
    AutoanswersRepository,
    canonical_json,
    final_reply_hash,
    iso_utc,
)
from packages.contracts.wb_autoanswers import publication_key  # noqa: E402


CONTRACT = "wb_autoanswers_policy_v5_reconciliation_v1"
APPLIED_EVENT = "owner_policy_v5_reconciliation_applied"
ROW_EVENT = "owner_policy_v5_publication_rebound"
STARTED_STATES = frozenset({"publishing", "publish_pending_readback", "published"})


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _open(runtime_dir: Path, *, read_only: bool) -> sqlite3.Connection:
    database = (runtime_dir / AUTOANSWERS_DB_FILENAME).resolve()
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


def _row_digest(rows: Iterable[Sequence[Any]]) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        count += 1
        for value in row:
            encoded = b"N" if value is None else f"{type(value).__name__}:{value}".encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return {"count": count, "sha256": "sha256:" + digest.hexdigest()}


def _query_digest(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> dict[str, Any]:
    return _row_digest(conn.execute(sql, tuple(params)).fetchall())


def _settings_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    excluded = {"policy_epoch", "policy_version", "updated_at"}
    return {key: row[key] for key in row.keys() if key not in excluded}


def _non_target_invariants(conn: sqlite3.Connection) -> dict[str, Any]:
    settings = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
    ).fetchone()
    if settings is None:
        raise RuntimeError("Autoanswers settings are missing")
    return {
        "settings_except_policy": _settings_projection(settings),
        "started_publications": _query_digest(
            conn,
            """
            SELECT p.*,j.final_route,j.final_reply_sha256,j.policy_epoch,j.policy_version
            FROM sheet_vitrina_v1_wb_publication_jobs p
            JOIN sheet_vitrina_v1_wb_autoanswer_jobs j USING(processing_key)
            WHERE p.write_started_at IS NOT NULL
               OR p.state IN ('publishing','publish_pending_readback','published')
               OR EXISTS(
                    SELECT 1 FROM sheet_vitrina_v1_wb_publication_attempts a
                    WHERE a.publication_key=p.publication_key
               )
            ORDER BY p.publication_key
            """,
        ),
        "publication_attempts": _query_digest(
            conn,
            "SELECT * FROM sheet_vitrina_v1_wb_publication_attempts ORDER BY publication_key,attempt_number",
        ),
        "jobs_outside_zero_write_scope": _query_digest(
            conn,
            """
            SELECT j.* FROM sheet_vitrina_v1_wb_autoanswer_jobs j
            WHERE NOT EXISTS(
                SELECT 1 FROM sheet_vitrina_v1_wb_publication_jobs p
                WHERE p.processing_key=j.processing_key
                  AND p.write_started_at IS NULL
                  AND p.attempts=0
                  AND p.state NOT IN ('publishing','publish_pending_readback','published')
                  AND NOT EXISTS(
                      SELECT 1 FROM sheet_vitrina_v1_wb_publication_attempts a
                      WHERE a.publication_key=p.publication_key
                  )
            )
            ORDER BY j.processing_key
            """,
        ),
        "feedback_truth": _query_digest(
            conn,
            """
            SELECT feedback_id,content_version,content_version_hash,content_json,
                   content_classification,rating,answer_text,wb_observation_hash
            FROM sheet_vitrina_v1_wb_feedbacks ORDER BY feedback_id
            """,
        ),
        "feedback_versions": _query_digest(
            conn,
            "SELECT * FROM sheet_vitrina_v1_wb_feedback_versions ORDER BY feedback_id,content_version",
        ),
        "feedback_media": _query_digest(
            conn,
            "SELECT * FROM sheet_vitrina_v1_wb_feedback_media ORDER BY feedback_id,content_version,kind,ordinal",
        ),
        "cost_events": _query_digest(
            conn,
            "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_cost_events ORDER BY event_id",
        ),
        "failed_cost_events": _query_digest(
            conn,
            "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events ORDER BY event_id",
        ),
        "reservations": _query_digest(
            conn,
            "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations ORDER BY processing_key",
        ),
        "uncertainty": _query_digest(
            conn,
            "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts ORDER BY processing_key,attempt_number",
        ),
    }


def _mutation_safety(conn: sqlite3.Connection) -> dict[str, Any]:
    runtime = conn.execute(
        "SELECT stop_reason FROM sheet_vitrina_v1_wb_autoanswers_runtime_state WHERE singleton=1"
    ).fetchone()
    return {
        "active_reservations": int(
            conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations WHERE status='reserved'"
            ).fetchone()[0]
        ),
        "unresolved_provider_boundaries": len(
            AutoanswersRepository._budget_uncertainty_candidates(conn)
        ),
        "runtime_stop_reason": str(runtime["stop_reason"] or "") if runtime else "",
    }


def _publication_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
              p.*,j.state AS job_state,j.policy_epoch AS job_policy_epoch,
              j.policy_version AS job_policy_version,j.final_route,j.case_code,
              j.final_reply,j.final_reply_sha256,j.result_json,j.hard_gates_passed,
              j.fallback_used,j.media_uncertain,j.node_contract_valid,
              f.rating,f.content_json,f.content_version AS current_content_version,
              f.content_version_hash AS current_content_version_hash,
              (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts a
               WHERE a.publication_key=p.publication_key) AS write_attempt_count
            FROM sheet_vitrina_v1_wb_publication_jobs p
            JOIN sheet_vitrina_v1_wb_autoanswer_jobs j USING(processing_key)
            JOIN sheet_vitrina_v1_wb_feedbacks f ON f.feedback_id=p.feedback_id
            ORDER BY p.publication_key
            """
        ).fetchall()
    ]


def _is_started(row: Mapping[str, Any]) -> bool:
    return bool(
        row.get("write_started_at")
        or int(row.get("write_attempt_count") or 0)
        or str(row.get("state") or "") in STARTED_STATES
    )


def _planned_action(row: Mapping[str, Any], *, next_policy_epoch: int) -> dict[str, Any]:
    if int(row["content_version"]) != int(row["current_content_version"]):
        raise RuntimeError("publication content version is no longer current")
    if str(row["content_version_hash"]) != str(row["current_content_version_hash"]):
        raise RuntimeError("publication content hash is no longer current")
    if int(row.get("attempts") or 0) != 0 or int(row.get("write_attempt_count") or 0) != 0:
        raise RuntimeError("unstarted publication has write-attempt evidence")
    exact_reply = str(row.get("exact_reply") or "")
    source_hash = final_reply_hash(exact_reply)
    if (
        source_hash != str(row.get("normalized_reply_sha256") or "")
        or source_hash != str(row.get("final_reply_sha256") or "")
        or exact_reply != str(row.get("final_reply") or "")
    ):
        raise RuntimeError("publication and generation reply evidence diverged")
    try:
        result = json.loads(str(row.get("result_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("generation result JSON is invalid") from exc
    if not isinstance(result, Mapping):
        raise RuntimeError("generation result JSON is not an object")
    result = dict(result)
    result["final_route"] = str(row.get("final_route") or "")
    result["final_reply"] = exact_reply
    transformed = apply_owner_policy(
        feedback_id=str(row["feedback_id"]),
        rating=int(row.get("rating") or 0),
        content_json=row.get("content_json"),
        result=result,
    )
    reply = str(transformed["final_reply"])
    route = str(transformed["final_route"])
    reply_sha = final_reply_hash(reply)
    next_key = publication_key(str(row["feedback_id"]), int(row["content_version"]), reply_sha)
    owner = dict(transformed.get("server_owner_policy") or {})
    return {
        "processing_key": str(row["processing_key"]),
        "publication_key": str(row["publication_key"]),
        "next_publication_key": next_key,
        "feedback_id": str(row["feedback_id"]),
        "content_version": int(row["content_version"]),
        "source_policy_epoch": int(row.get("policy_epoch") or 0),
        "source_policy_version": str(row.get("job_policy_version") or ""),
        "next_policy_epoch": int(next_policy_epoch),
        "source_route": str(row.get("final_route") or ""),
        "next_route": route,
        "source_reply_sha256": source_hash,
        "next_reply_sha256": reply_sha,
        "next_reply": reply,
        "next_result_json": canonical_json(transformed),
        "next_case_code": transformed.get("case_code"),
        "next_fallback_used": int(bool(transformed.get("fallback_used"))),
        "reason": str(owner.get("reason") or ""),
        "hard_return_reasons": list(owner.get("hard_return_reasons") or []),
        "template_id": owner.get("template_id"),
        "unfortunately_action": owner.get("unfortunately_action"),
    }


def _projection(action: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: action[key]
        for key in (
            "processing_key",
            "publication_key",
            "next_publication_key",
            "feedback_id",
            "content_version",
            "source_policy_epoch",
            "source_policy_version",
            "next_policy_epoch",
            "source_route",
            "next_route",
            "source_reply_sha256",
            "next_reply_sha256",
            "reason",
            "hard_return_reasons",
            "template_id",
            "unfortunately_action",
        )
    } | {"next_result_json_sha256": _fingerprint(action["next_result_json"])}


def build_plan(
    conn: sqlite3.Connection,
    *,
    runtime_dir: Path,
    deployed_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    settings = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
    ).fetchone()
    if settings is None:
        raise RuntimeError("Autoanswers settings are missing")
    policy_version = str(settings["policy_version"] or "")
    if policy_version not in {PREVIOUS_POLICY_VERSION, OWNER_POLICY_VERSION}:
        raise RuntimeError("unexpected Autoanswers owner-policy version")
    next_policy_epoch = int(settings["policy_epoch"]) + int(
        policy_version != OWNER_POLICY_VERSION
    )
    rows = _publication_rows(conn)
    started = [row for row in rows if _is_started(row)]
    unstarted = [row for row in rows if not _is_started(row)]
    actions = [_planned_action(row, next_policy_epoch=next_policy_epoch) for row in unstarted]
    action_projection = [_projection(action) for action in actions]
    counts = {
        "publication_total": len(rows),
        "unstarted_evaluated": len(actions),
        "started_preserved": len(started),
        "route_changed": sum(
            action["source_route"] != action["next_route"] for action in actions
        ),
        "reply_changed": sum(
            action["source_reply_sha256"] != action["next_reply_sha256"]
            for action in actions
        ),
        "metadata_only_rebound": sum(
            action["source_route"] == action["next_route"]
            and action["source_reply_sha256"] == action["next_reply_sha256"]
            for action in actions
        ),
        "wb_return_before": sum(action["source_route"] == "wb_return" for action in actions),
        "wb_return_after": sum(action["next_route"] == "wb_return" for action in actions),
        "ordinary_post_use_breakage": sum(
            action["reason"] == "ordinary_post_use_breakage" for action in actions
        ),
        "soft_without_hard_signal": sum(
            action["reason"] == "no_independent_hard_return_signal" for action in actions
        ),
        "hard_return_preserved": sum(
            action["reason"] == "independent_hard_return_preserved" for action in actions
        ),
        "unfortunately_inserted": sum(
            action["unfortunately_action"] == "inserted_limitation" for action in actions
        ),
        "double_empathy_removed": sum(
            action["unfortunately_action"] == "removed_double_empathy" for action in actions
        ),
    }
    safety = _mutation_safety(conn)
    backup = _verified_backup(runtime_dir)
    non_target = _non_target_invariants(conn)
    identity = {
        "contract": CONTRACT,
        "owner_policy_contract": OWNER_POLICY_CONTRACT,
        "source_policy_version": policy_version,
        "target_policy_version": OWNER_POLICY_VERSION,
        "source_policy_epoch": int(settings["policy_epoch"]),
        "target_policy_epoch": next_policy_epoch,
        "mode": str(settings["mode"]),
        "master_enabled": bool(settings["master_enabled"]),
        "deployed_runtime": dict(deployed_runtime),
        "backup": backup,
        "counts": counts,
        "target_projection": action_projection,
        "target_projection_sha256": _fingerprint(action_projection),
        "non_target_invariants": non_target,
        "mutation_safety": safety,
    }
    plan_fingerprint = _fingerprint(identity)
    return {
        **identity,
        "plan_fingerprint": plan_fingerprint,
        "pre_change_digest": _fingerprint(
            {
                "target_projection": action_projection,
                "non_target_invariants": non_target,
            }
        ),
        "coverage_confirmed": bool(
            str(settings["mode"]) == "auto_all"
            and bool(settings["master_enabled"])
            and backup.get("verified") is True
            and safety["active_reservations"] == 0
            and safety["unresolved_provider_boundaries"] == 0
            and safety["runtime_stop_reason"] != "budget_state_unknown"
            and all(int(row.get("write_attempt_count") or 0) == 0 for row in unstarted)
        ),
        "_actions": actions,
    }


def public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in plan.items()
        if key not in {"_actions", "target_projection"}
    }


def _applied_evidence(conn: sqlite3.Connection, fingerprint: str) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT details_json FROM sheet_vitrina_v1_wb_autoanswers_audit_events
        WHERE event_type=? ORDER BY rowid DESC
        """,
        (APPLIED_EVENT,),
    ).fetchall()
    for row in rows:
        try:
            details = json.loads(str(row["details_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        if str(details.get("plan_fingerprint") or "") == fingerprint:
            return dict(details)
    return None


def apply_plan(
    runtime_dir: Path,
    *,
    expected_fingerprint: str,
    deployed_runtime: Mapping[str, Any],
    actor: str,
    worker_hold_confirmed: bool,
) -> dict[str, Any]:
    if not worker_hold_confirmed:
        raise RuntimeError("canonical Autoanswers worker hold is required")
    with closing(_open(runtime_dir, read_only=False)) as conn:
        existing = _applied_evidence(conn, expected_fingerprint)
        if existing is not None:
            settings = conn.execute(
                "SELECT policy_epoch,policy_version FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
            ).fetchone()
            if (
                settings is None
                or str(settings["policy_version"]) != OWNER_POLICY_VERSION
                or int(settings["policy_epoch"])
                != int(existing.get("target_policy_epoch") or -1)
                or _non_target_invariants(conn)
                != existing.get("non_target_invariants")
            ):
                raise RuntimeError(
                    "applied policy fingerprint no longer matches its protected invariants"
                )
            return {
                "contract": CONTRACT,
                "status": "applied",
                "idempotent": True,
                "plan_fingerprint": expected_fingerprint,
                "counts": existing.get("counts") or {},
            }
        conn.execute("BEGIN IMMEDIATE")
        try:
            plan = build_plan(
                conn,
                runtime_dir=runtime_dir,
                deployed_runtime=deployed_runtime,
            )
            if plan["plan_fingerprint"] != expected_fingerprint:
                raise RuntimeError("policy reconciliation plan changed after review")
            if plan["coverage_confirmed"] is not True:
                raise RuntimeError("policy reconciliation preconditions are not confirmed")
            if plan["source_policy_version"] != PREVIOUS_POLICY_VERSION:
                raise RuntimeError("policy v5 activation requires the exact v4 source policy")
            now = _now()
            activation_id = uuid4().hex
            next_epoch = int(plan["target_policy_epoch"])
            for action in plan["_actions"]:
                old_key = str(action["publication_key"])
                next_key = str(action["next_publication_key"])
                if next_key != old_key:
                    collision = conn.execute(
                        "SELECT 1 FROM sheet_vitrina_v1_wb_publication_jobs WHERE publication_key=?",
                        (next_key,),
                    ).fetchone()
                    if collision is not None:
                        raise RuntimeError("policy reconciliation publication key collision")
                job_cursor = conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET policy_epoch=?,policy_version=?,final_route=?,case_code=?,
                        final_reply=?,final_reply_sha256=?,result_json=?,fallback_used=?,updated_at=?
                    WHERE processing_key=? AND policy_version=?
                    """,
                    (
                        next_epoch,
                        OWNER_POLICY_VERSION,
                        action["next_route"],
                        action["next_case_code"],
                        action["next_reply"],
                        action["next_reply_sha256"],
                        action["next_result_json"],
                        action["next_fallback_used"],
                        iso_utc(now),
                        action["processing_key"],
                        PREVIOUS_POLICY_VERSION,
                    ),
                )
                if int(job_cursor.rowcount or 0) != 1:
                    raise RuntimeError("policy reconciliation lost its processing job")
                pub_cursor = conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_publication_jobs
                    SET publication_key=?,exact_reply=?,normalized_reply_sha256=?,
                        policy_epoch=?,updated_at=?
                    WHERE publication_key=? AND write_started_at IS NULL AND attempts=0
                      AND NOT EXISTS(
                          SELECT 1 FROM sheet_vitrina_v1_wb_publication_attempts a
                          WHERE a.publication_key=sheet_vitrina_v1_wb_publication_jobs.publication_key
                      )
                    """,
                    (
                        next_key,
                        action["next_reply"],
                        action["next_reply_sha256"],
                        next_epoch,
                        iso_utc(now),
                        old_key,
                    ),
                )
                if int(pub_cursor.rowcount or 0) != 1:
                    raise RuntimeError("policy reconciliation lost its zero-write publication")
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_autoanswers_audit_events(
                        event_id,aggregate_type,aggregate_id,event_type,actor_type,actor_id,
                        bundle_version,evaluation_signature,details_json,
                        previous_state,next_state,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        uuid4().hex,
                        "publication_job",
                        next_key,
                        ROW_EVENT,
                        "policy",
                        actor,
                        PROMPT_BUNDLE_VERSION,
                        EVALUATION_SIGNATURE,
                        canonical_json(
                            {
                                "activation_id": activation_id,
                                "contract": CONTRACT,
                                "policy_version": OWNER_POLICY_VERSION,
                                "source_publication_key_sha256": _fingerprint(old_key),
                                "source_reply_sha256": action["source_reply_sha256"],
                                "reply_sha256": action["next_reply_sha256"],
                                "source_route": action["source_route"],
                                "publication_route": action["next_route"],
                                "reason": action["reason"],
                                "hard_return_reasons": action["hard_return_reasons"],
                                "template_id": action["template_id"],
                                "unfortunately_action": action["unfortunately_action"],
                                "wb_posts": 0,
                                "provider_calls": 0,
                            }
                        ),
                        "approved",
                        "approved",
                        iso_utc(now),
                    ),
                )
            settings_cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_autoanswers_settings
                SET policy_epoch=?,policy_version=?,updated_at=?
                WHERE singleton=1 AND policy_epoch=? AND policy_version=?
                """,
                (
                    next_epoch,
                    OWNER_POLICY_VERSION,
                    iso_utc(now),
                    plan["source_policy_epoch"],
                    PREVIOUS_POLICY_VERSION,
                ),
            )
            if int(settings_cursor.rowcount or 0) != 1:
                raise RuntimeError("policy reconciliation lost its settings epoch")
            conn.execute(
                """
                    INSERT INTO sheet_vitrina_v1_wb_autoanswers_audit_events(
                        event_id,aggregate_type,aggregate_id,event_type,actor_type,actor_id,
                    bundle_version,evaluation_signature,details_json,
                    previous_state,next_state,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    uuid4().hex,
                    "settings",
                    "singleton",
                    APPLIED_EVENT,
                    "policy",
                    actor,
                    PROMPT_BUNDLE_VERSION,
                    EVALUATION_SIGNATURE,
                    canonical_json(
                        {
                            "activation_id": activation_id,
                            "contract": CONTRACT,
                            "plan_fingerprint": expected_fingerprint,
                            "pre_change_digest": plan["pre_change_digest"],
                            "source_policy_version": PREVIOUS_POLICY_VERSION,
                            "target_policy_version": OWNER_POLICY_VERSION,
                            "source_policy_epoch": plan["source_policy_epoch"],
                            "target_policy_epoch": next_epoch,
                            "counts": plan["counts"],
                            "non_target_invariants": plan["non_target_invariants"],
                            "deployed_runtime": dict(deployed_runtime),
                            "worker_hold_confirmed": True,
                            "wb_posts": 0,
                            "provider_calls": 0,
                        }
                    ),
                    PREVIOUS_POLICY_VERSION,
                    OWNER_POLICY_VERSION,
                    iso_utc(now),
                ),
            )
            if _non_target_invariants(conn) != plan["non_target_invariants"]:
                raise RuntimeError("policy reconciliation changed a non-target invariant")
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise RuntimeError("policy reconciliation violated SQLite foreign keys")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "contract": CONTRACT,
        "status": "applied",
        "idempotent": False,
        "plan_fingerprint": expected_fingerprint,
        "policy_version": OWNER_POLICY_VERSION,
        "policy_epoch": next_epoch,
        "counts": plan["counts"],
        "non_target_invariants": plan["non_target_invariants"],
        "wb_post_count": 0,
        "provider_call_count": 0,
    }


def readback(
    conn: sqlite3.Connection,
    *,
    reviewed_plan: Mapping[str, Any],
    expected_fingerprint: str,
) -> dict[str, Any]:
    settings = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
    ).fetchone()
    if settings is None:
        raise RuntimeError("Autoanswers settings are missing")
    evidence = _applied_evidence(conn, expected_fingerprint)
    rows = _publication_rows(conn)
    unstarted = [row for row in rows if not _is_started(row)]
    blockers: list[str] = []
    if str(settings["policy_version"]) != OWNER_POLICY_VERSION:
        blockers.append("settings_policy_version_not_v5")
    if int(settings["policy_epoch"]) != int(reviewed_plan.get("target_policy_epoch") or -1):
        blockers.append("settings_policy_epoch_mismatch")
    if evidence is None:
        blockers.append("activation_audit_missing")
    if _non_target_invariants(conn) != reviewed_plan.get("non_target_invariants"):
        blockers.append("non_target_invariants_changed")
    stale = 0
    incoherent = 0
    metadata_stale = 0
    for row in unstarted:
        if (
            str(row.get("job_policy_version") or "") != OWNER_POLICY_VERSION
            or int(row.get("job_policy_epoch") or -1) != int(settings["policy_epoch"])
            or int(row.get("policy_epoch") or -1) != int(settings["policy_epoch"])
        ):
            stale += 1
        exact_reply = str(row.get("exact_reply") or "")
        if (
            final_reply_hash(exact_reply) != str(row.get("normalized_reply_sha256") or "")
            or exact_reply != str(row.get("final_reply") or "")
            or str(row.get("final_reply_sha256") or "")
            != str(row.get("normalized_reply_sha256") or "")
        ):
            incoherent += 1
        try:
            result = json.loads(str(row.get("result_json") or "{}"))
        except json.JSONDecodeError:
            result = {}
        owner = dict(result.get("server_owner_policy") or {}) if isinstance(result, Mapping) else {}
        if (
            owner.get("contract") != OWNER_POLICY_CONTRACT
            or owner.get("policy_version") != OWNER_POLICY_VERSION
            or str(result.get("final_route") or "") != str(row.get("final_route") or "")
            or str(result.get("final_reply") or "") != exact_reply
        ):
            metadata_stale += 1
    if stale:
        blockers.append("unstarted_policy_binding_stale")
    if incoherent:
        blockers.append("unstarted_reply_evidence_incoherent")
    if metadata_stale:
        blockers.append("unstarted_owner_policy_metadata_stale")
    actual_counts = {
        "publication_total": len(rows),
        "unstarted_evaluated": len(unstarted),
        "started_preserved": sum(_is_started(row) for row in rows),
        "wb_return_after": sum(
            str(row.get("final_route") or "") == "wb_return" for row in unstarted
        ),
        "stale_unstarted": stale,
        "incoherent_unstarted": incoherent,
        "metadata_stale_unstarted": metadata_stale,
    }
    expected_counts = dict(reviewed_plan.get("counts") or {})
    for key in ("publication_total", "unstarted_evaluated", "started_preserved", "wb_return_after"):
        if actual_counts[key] != int(expected_counts.get(key, -1)):
            blockers.append(f"count_mismatch:{key}")
    return {
        "contract": CONTRACT,
        "status": "reconciled" if not blockers else "blocked",
        "plan_fingerprint": expected_fingerprint,
        "policy_version": str(settings["policy_version"]),
        "policy_epoch": int(settings["policy_epoch"]),
        "expected_counts": expected_counts,
        "actual_counts": actual_counts,
        "non_target_invariants": _non_target_invariants(conn),
        "activation_audit_confirmed": evidence is not None,
        "blockers": blockers,
        "wb_post_count": 0,
        "provider_call_count": 0,
    }


def _reviewed_plan_from_stdin() -> dict[str, Any]:
    value = json.load(sys.stdin)
    result = value.get("result") if isinstance(value, Mapping) else None
    if isinstance(result, Mapping):
        value = result
    if not isinstance(value, Mapping):
        raise ValueError("reviewed policy plan must be a JSON object")
    return dict(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("dry-run", "apply", "readback"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--expected-deployed-sha", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--reviewed-plan-stdin", action="store_true")
    parser.add_argument("--worker-hold-confirmed", action="store_true")
    parser.add_argument("--actor", default="release-train")
    args = parser.parse_args()
    runtime_dir = args.runtime_dir.expanduser().resolve()
    deployed = _deployed_runtime_evidence(str(args.expected_deployed_sha))
    reviewed = _reviewed_plan_from_stdin() if args.reviewed_plan_stdin else None
    if args.action == "dry-run":
        with closing(_open(runtime_dir, read_only=True)) as conn:
            payload = public_plan(
                build_plan(conn, runtime_dir=runtime_dir, deployed_runtime=deployed)
            )
    elif args.action == "apply":
        fingerprint = str(args.expected_fingerprint).strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None:
            raise ValueError("apply requires --expected-fingerprint")
        if reviewed is None or reviewed.get("plan_fingerprint") != fingerprint:
            raise ValueError("apply requires the exact reviewed plan on stdin")
        payload = apply_plan(
            runtime_dir,
            expected_fingerprint=fingerprint,
            deployed_runtime=deployed,
            actor=str(args.actor),
            worker_hold_confirmed=bool(args.worker_hold_confirmed),
        )
    else:
        fingerprint = str(args.expected_fingerprint).strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None:
            raise ValueError("readback requires --expected-fingerprint")
        if reviewed is None or reviewed.get("plan_fingerprint") != fingerprint:
            raise ValueError("readback requires the exact reviewed plan on stdin")
        with closing(_open(runtime_dir, read_only=True)) as conn:
            payload = readback(
                conn,
                reviewed_plan=reviewed,
                expected_fingerprint=fingerprint,
            )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("status") not in {"blocked"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
