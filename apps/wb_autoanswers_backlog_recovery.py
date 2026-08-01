#!/usr/bin/env python3
"""Fingerprint-bound recovery for an exact WB unanswered T0 cohort.

Dry-run and readback perform only official WB GETs plus SQLite ``mode=ro`` /
``query_only`` reads.  Apply requires an exact plan fingerprint, a verified
schema-v10 backup and the original machine-readable T0 manifest.  It activates
the versioned safe-public policy, reuses immutable completed AI/publication
evidence, and queues only the exact still-unanswered cohort.  This runner never
performs a WB POST; the ordinary publication worker retains the mandatory
POST-then-detail-GET state machine.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
from typing import Any, Mapping
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_autoanswers_rolling_recovery import _verified_backup  # noqa: E402
from packages.adapters.wb_autoanswers import (  # noqa: E402
    HttpBackedWbAutoanswersReadAdapter,
    WbFeedbackReadPort,
)
from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV  # noqa: E402
from packages.application.wb_autoanswers_runtime import (  # noqa: E402
    AUTOANSWERS_DB_FILENAME,
    DEFAULT_POLICY_VERSION,
    EVALUATION_SIGNATURE,
    PREVIOUS_POLICY_VERSION,
    PROCESSING_KIND_FROZEN_AI,
    PROCESSING_KIND_RATING_ONLY_TEMPLATE,
    PROCESSING_KIND_SAFE_PUBLIC_TEMPLATE,
    PROMPT_BUNDLE_VERSION,
    STATE_APPROVED,
    STATE_QUEUED,
    AutoanswersRepository,
    canonical_json,
    content_projection,
    final_reply_hash,
    iso_utc,
    processing_key,
    safe_public_template,
    sha256_text,
)


CONTRACT = "wb_autoanswers_backlog_recovery_v1"
T0_CONTRACT = "wb_autoanswers_t0_manifest_v1"
DATABASE_FILENAME = AUTOANSWERS_DB_FILENAME
EXTERNAL_GATE_ENV = "WB_AUTOANSWERS_EXTERNAL_IO_ENABLED"
SAFE_ENV_KEYS = frozenset(
    {
        DEFAULT_WB_API_TOKEN_ENV,
        "WB_FEEDBACKS_API_BASE_URL",
        "OFFICIAL_API_TIMEOUT_SECONDS",
    }
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_safe_env_file(path: Path) -> None:
    """Load only GET-adapter configuration without evaluating shell syntax."""

    if not path.is_file():
        raise ValueError(f"environment file does not exist: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in SAFE_ENV_KEYS:
            continue
        lexer = shlex.shlex(raw_value, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            os.environ[key] = " ".join(lexer)
        except ValueError as exc:
            raise ValueError(f"invalid value for environment key {key}") from exc


def _deployed_runtime_evidence(expected_sha: str) -> dict[str, Any]:
    expected = str(expected_sha).strip().lower()
    if len(expected) != 40 or any(character not in "0123456789abcdef" for character in expected):
        raise ValueError("expected deployed SHA must be 40 lowercase hexadecimal characters")
    runtime_sha_path = ROOT / ".wb-core-runtime-sha"
    metadata_path = ROOT / ".wb-core-deploy.json"
    if not runtime_sha_path.is_file() or not metadata_path.is_file():
        raise RuntimeError("canonical deployed-SHA evidence is missing")
    runtime_sha = runtime_sha_path.read_text(encoding="utf-8").strip().lower()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata_sha = str(metadata.get("commit") or "").strip().lower()
    if (
        runtime_sha != expected
        or metadata_sha != expected
        or metadata.get("deployment_complete") is not True
    ):
        raise RuntimeError("canonical deployed-SHA evidence does not match the expected release")
    return {
        "runtime_sha": runtime_sha,
        "deploy_metadata_sha": metadata_sha,
        "deployment_complete": True,
        "deployed_at": metadata.get("deployed_at"),
    }


def _open(runtime_dir: Path, *, read_only: bool) -> sqlite3.Connection:
    database = (runtime_dir / DATABASE_FILENAME).resolve()
    if not read_only:
        raise ValueError("backlog recovery opens direct connections read-only")
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def validate_manifest(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("contract") != T0_CONTRACT:
        raise ValueError("an exact wb_autoanswers_t0_manifest_v1 is required")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("T0 manifest contains no items")
    ids = [str(item.get("feedback_id") or "").strip() for item in items]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("T0 feedback IDs must be non-empty and unique")
    supplied = str(payload.get("manifest_sha256") or "")
    calculated = _fingerprint(
        {key: value for key, value in payload.items() if key != "manifest_sha256"}
    )
    if supplied != calculated:
        raise ValueError("T0 manifest fingerprint mismatch")
    return payload


def load_manifest(path: Path) -> dict[str, Any]:
    return validate_manifest(json.loads(path.read_text(encoding="utf-8")))


def _answer_text(detail: Mapping[str, Any] | None) -> str:
    if not detail:
        return ""
    answer = detail.get("answer")
    if isinstance(answer, Mapping):
        return str(answer.get("text") or "").strip()
    return str(answer or "").strip()


def _content_hash(detail: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(content_projection(detail)))


def _fetch_full_unanswered(
    source: WbFeedbackReadPort,
    *,
    captured_at: datetime,
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    skip = 0
    while True:
        page = source.fetch_feedbacks_page(
            date_from_ts=0,
            date_to_ts=int(captured_at.timestamp()),
            is_answered=False,
            take=5000,
            skip=skip,
        )
        rows.extend(page.rows)
        if not page.has_more:
            break
        skip += page.take
    list_ids = [str(row.get("id") or "").strip() for row in rows]
    if any(not feedback_id for feedback_id in list_ids):
        raise RuntimeError("full WB unanswered list contains an empty feedback ID")
    if len(list_ids) != len(set(list_ids)):
        raise RuntimeError("full WB unanswered list contains duplicate feedback IDs")
    return rows


def capture_t0_manifest(source: WbFeedbackReadPort) -> dict[str, Any]:
    """Capture a complete, count-matched unanswered cohort using official GETs."""

    captured_at = _now()
    rows = _fetch_full_unanswered(source, captured_at=captured_at)
    if not rows:
        raise RuntimeError("full WB unanswered list is already empty")
    items: list[dict[str, Any]] = []
    for feedback_id in sorted(str(row["id"]).strip() for row in rows):
        detail = source.fetch_detail(feedback_id)
        if detail is None:
            raise RuntimeError(f"WB detail is missing for {feedback_id}")
        if _answer_text(detail):
            raise RuntimeError(f"WB detail became answered during T0 capture: {feedback_id}")
        items.append(
            {
                "feedback_id": feedback_id,
                "wb_detail_content_hash": _content_hash(detail),
            }
        )
    repeated_rows = _fetch_full_unanswered(source, captured_at=captured_at)
    if sorted(str(row["id"]).strip() for row in repeated_rows) != sorted(
        str(row["id"]).strip() for row in rows
    ):
        raise RuntimeError("WB unanswered list changed during T0 capture")
    count_endpoint = int(source.count_unanswered())
    if count_endpoint != len(repeated_rows):
        raise RuntimeError("WB unanswered count changed during T0 capture")
    manifest: dict[str, Any] = {
        "contract": T0_CONTRACT,
        "captured_at": iso_utc(captured_at),
        "items": items,
    }
    manifest["manifest_sha256"] = _fingerprint(manifest)
    return manifest


def fetch_remote_evidence(
    source: WbFeedbackReadPort,
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]]]:
    now = _now()
    rows = _fetch_full_unanswered(source, captured_at=now)
    list_ids = [str(row.get("id") or "").strip() for row in rows]
    list_id_set = set(list_ids)
    details: dict[str, Mapping[str, Any]] = {}
    detail_evidence: list[dict[str, Any]] = []
    for item in manifest["items"]:
        feedback_id = str(item["feedback_id"])
        detail = source.fetch_detail(feedback_id)
        if detail is None:
            raise RuntimeError(f"WB detail is missing for {feedback_id}")
        details[feedback_id] = detail
        answer = _answer_text(detail)
        content_hash = _content_hash(detail)
        expected_hash = str(item.get("wb_detail_content_hash") or "")
        if not answer and expected_hash and content_hash != expected_hash:
            raise RuntimeError(f"T0 content changed for {feedback_id}")
        detail_evidence.append(
            {
                "feedback_id": feedback_id,
                "content_hash": content_hash,
                "answer_present": bool(answer),
                "answer_sha256": final_reply_hash(answer) if answer else None,
                "listed_unanswered": feedback_id in list_id_set,
            }
        )
    repeated_rows = _fetch_full_unanswered(source, captured_at=now)
    repeated_ids = [str(row.get("id") or "").strip() for row in repeated_rows]
    if sorted(repeated_ids) != sorted(list_ids):
        raise RuntimeError("full WB unanswered list changed during evidence capture")
    count_endpoint = int(source.count_unanswered())
    return (
        {
            "captured_at": iso_utc(now),
            "count_endpoint": count_endpoint,
            "full_list_count": len(rows),
            "full_list_distinct_count": len(set(list_ids)),
            "full_list_stable": True,
            "count_matches_list": count_endpoint == len(repeated_rows),
            "full_list_ids_sha256": _fingerprint(sorted(list_ids)),
            "t0_details": detail_evidence,
            "non_t0_unanswered_count": len(list_id_set - {str(item["feedback_id"]) for item in manifest["items"]}),
        },
        details,
    )


def _target_rows(conn: sqlite3.Connection, feedback_ids: list[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in feedback_ids)
    rows = conn.execute(
        f"""
        SELECT
          f.feedback_id,f.content_version,f.content_version_hash,
          f.content_classification,f.rating,f.answer_text,
          j.processing_key,j.state AS processing_state,j.processing_kind,
          j.policy_epoch AS job_policy_epoch,j.policy_version AS job_policy_version,
          j.final_route,j.final_reply_sha256,j.result_json,j.hard_gates_passed,
          j.node_contract_valid,j.fallback_used,j.media_uncertain,
          j.regeneration_required,j.last_error_code,j.actual_cost_usd,
          p.publication_key,p.state AS publication_state,p.policy_epoch AS publication_policy_epoch,
          p.normalized_reply_sha256,p.write_started_at,p.attempts AS publication_attempts,
          r.status AS reservation_status,r.actual_cost_usd AS reservation_actual,
          r.provider_call_started_at,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts a
           WHERE a.publication_key=p.publication_key) AS write_attempt_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events c
           WHERE c.processing_key=j.processing_key) AS archived_cost_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events c
           WHERE c.processing_key=j.processing_key) AS failed_cost_count,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts u
           WHERE u.processing_key=j.processing_key) AS uncertainty_count
        FROM sheet_vitrina_v1_wb_feedbacks f
        LEFT JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
          ON j.feedback_id=f.feedback_id
         AND j.content_version=f.content_version
         AND j.bundle_version=?
        LEFT JOIN sheet_vitrina_v1_wb_publication_jobs p ON p.processing_key=j.processing_key
        LEFT JOIN sheet_vitrina_v1_wb_autoanswers_budget_reservations r ON r.processing_key=j.processing_key
        WHERE f.feedback_id IN ({placeholders})
        ORDER BY f.feedback_id
        """,
        [PROMPT_BUNDLE_VERSION, *feedback_ids],
    ).fetchall()
    return [dict(row) for row in rows]


def _non_target_snapshot(conn: sqlite3.Connection, feedback_ids: list[str]) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in feedback_ids)
    settings = conn.execute(
        """
        SELECT master_enabled,mode,enable_epoch,daily_cap_usd,monthly_cap_usd,
               hourly_cap_usd,max_paid_reviews_per_hour,
               global_paid_review_concurrency,max_inflight_role_calls,
               max_materialized_processing_jobs,warning_ratio,
               max_reservation_per_review_usd
        FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1
        """
    ).fetchone()
    counts = conn.execute(
        f"""
        SELECT
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswer_jobs
           WHERE feedback_id NOT IN ({placeholders})) AS jobs,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs
           WHERE feedback_id NOT IN ({placeholders})) AS publications,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts a
           JOIN sheet_vitrina_v1_wb_publication_jobs p USING(publication_key)
           WHERE p.feedback_id NOT IN ({placeholders})) AS wb_writes,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events c
           JOIN sheet_vitrina_v1_wb_autoanswer_jobs j USING(processing_key)
           WHERE j.feedback_id NOT IN ({placeholders})) AS costs,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events c
           JOIN sheet_vitrina_v1_wb_autoanswer_jobs j USING(processing_key)
           WHERE j.feedback_id NOT IN ({placeholders})) AS failed_costs,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations r
           JOIN sheet_vitrina_v1_wb_autoanswer_jobs j USING(processing_key)
           WHERE j.feedback_id NOT IN ({placeholders})) AS reservations
        """,
        [*feedback_ids, *feedback_ids, *feedback_ids, *feedback_ids, *feedback_ids, *feedback_ids],
    ).fetchone()
    return {"settings": dict(settings), "counts": dict(counts)}


def _mutation_safety_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    runtime = conn.execute(
        """
        SELECT stop_reason,stop_details_json
        FROM sheet_vitrina_v1_wb_autoanswers_runtime_state
        WHERE singleton=1
        """
    ).fetchone()
    unresolved = AutoanswersRepository._budget_uncertainty_candidates(conn)
    active_reservations = [
        str(row["processing_key"])
        for row in conn.execute(
            """
            SELECT processing_key
            FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
            WHERE status='reserved'
            ORDER BY processing_key
            """
        ).fetchall()
    ]
    return {
        "runtime_stop_reason": str(runtime["stop_reason"] or "") if runtime else "",
        "runtime_stop_details_json": (
            str(runtime["stop_details_json"] or "{}") if runtime else "{}"
        ),
        "unresolved_provider_cost_processing_keys": sorted(
            str(item["processing_key"]) for item in unresolved
        ),
        "active_reservation_processing_keys": active_reservations,
    }


def _classify_action(
    row: Mapping[str, Any] | None,
    *,
    answer_present: bool,
    audited_complete: bool,
) -> str:
    if answer_present:
        return "preexisting_external_answer"
    if row is None or not row.get("processing_key"):
        return "ingest_and_generate"
    if row.get("publication_key"):
        if row.get("write_started_at") or int(row.get("write_attempt_count") or 0):
            return "readback_only"
        return "rebind_publication"
    if str(row.get("final_route") or "") == "seller_chat":
        return "safe_public_transform"
    if audited_complete:
        return "recover_audited_generation"
    if row.get("final_reply_sha256") and row.get("hard_gates_passed") and row.get("node_contract_valid"):
        return "enqueue_existing_generation"
    return "safe_public_recovery"


def build_plan(
    conn: sqlite3.Connection,
    *,
    runtime_dir: Path,
    manifest: Mapping[str, Any],
    remote: Mapping[str, Any],
) -> dict[str, Any]:
    feedback_ids = sorted(str(item["feedback_id"]) for item in manifest["items"])
    rows = {str(row["feedback_id"]): row for row in _target_rows(conn, feedback_ids)}
    settings = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
    ).fetchone()
    if settings is None:
        raise RuntimeError("Autoanswers settings are missing")
    actions: list[dict[str, Any]] = []
    details_by_id = {str(item["feedback_id"]): item for item in remote["t0_details"]}
    if set(details_by_id) != set(feedback_ids):
        raise RuntimeError("remote detail evidence does not match the exact T0 manifest")
    for feedback_id in feedback_ids:
        row = rows.get(feedback_id)
        completed_evidence: dict[str, Any] | None = None
        audited_complete = False
        if row is not None and row.get("processing_key"):
            completed_evidence = AutoanswersRepository._completed_node_evidence(
                conn, str(row["processing_key"])
            )
            audited_complete = bool(
                completed_evidence is not None
                and completed_evidence.get("outcome") == "ready"
            )
        action = _classify_action(
            row,
            answer_present=bool(details_by_id[feedback_id]["answer_present"]),
            audited_complete=audited_complete,
        )
        if action == "readback_only":
            # A possible prior POST can never be repeated by this runner.
            if not details_by_id[feedback_id]["answer_present"]:
                raise RuntimeError(
                    f"ambiguous prior WB write requires readback resolution: {feedback_id}"
                )
        actions.append(
            {
                "feedback_id": feedback_id,
                "action": action,
                "remote_content_hash": details_by_id[feedback_id]["content_hash"],
                "content_version": row.get("content_version") if row else None,
                "content_version_hash": row.get("content_version_hash") if row else None,
                "content_classification": row.get("content_classification") if row else None,
                "rating": row.get("rating") if row else None,
                "processing_key": row.get("processing_key") if row else None,
                "processing_state": row.get("processing_state") if row else None,
                "processing_kind": row.get("processing_kind") if row else None,
                "job_policy_epoch": row.get("job_policy_epoch") if row else None,
                "job_policy_version": row.get("job_policy_version") if row else None,
                "final_route": row.get("final_route") if row else None,
                "final_reply_sha256": row.get("final_reply_sha256") if row else None,
                "result_json_sha256": (
                    "sha256:" + sha256_text(str(row.get("result_json") or ""))
                    if row
                    else None
                ),
                "hard_gates_passed": row.get("hard_gates_passed") if row else None,
                "node_contract_valid": row.get("node_contract_valid") if row else None,
                "fallback_used": row.get("fallback_used") if row else None,
                "media_uncertain": row.get("media_uncertain") if row else None,
                "regeneration_required": row.get("regeneration_required") if row else None,
                "last_error_code": row.get("last_error_code") if row else None,
                "publication_key": row.get("publication_key") if row else None,
                "publication_state": row.get("publication_state") if row else None,
                "publication_policy_epoch": (
                    row.get("publication_policy_epoch") if row else None
                ),
                "publication_reply_sha256": (
                    row.get("normalized_reply_sha256") if row else None
                ),
                "publication_write_started_at": (
                    row.get("write_started_at") if row else None
                ),
                "publication_attempts": (
                    int(row.get("publication_attempts") or 0) if row else 0
                ),
                "reply_sha256": row.get("normalized_reply_sha256") if row else None,
                "actual_cost_usd": row.get("actual_cost_usd") if row else None,
                "reservation_status": row.get("reservation_status") if row else None,
                "reservation_actual_usd": row.get("reservation_actual") if row else None,
                "provider_call_started_at": (
                    row.get("provider_call_started_at") if row else None
                ),
                "write_attempt_count": int(row.get("write_attempt_count") or 0) if row else 0,
                "archived_cost_count": (
                    int(row.get("archived_cost_count") or 0) if row else 0
                ),
                "failed_cost_count": (
                    int(row.get("failed_cost_count") or 0) if row else 0
                ),
                "uncertainty_count": (
                    int(row.get("uncertainty_count") or 0) if row else 0
                ),
                "audited_complete": audited_complete,
                "audited_evidence": completed_evidence,
            }
        )
    backup = _verified_backup(runtime_dir)
    stable_remote = {
        key: value for key, value in remote.items() if key != "captured_at"
    }
    non_target_invariants = _non_target_snapshot(conn, feedback_ids)
    mutation_safety = _mutation_safety_snapshot(conn)
    pre_change_digest = _fingerprint(
        {
            "targets": actions,
            "non_target": non_target_invariants,
        }
    )
    identity = {
        "contract": CONTRACT,
        "manifest_sha256": manifest["manifest_sha256"],
        "expected_feedback_count": len(feedback_ids),
        "current_policy": {
            "policy_version": str(settings["policy_version"]),
            "policy_epoch": int(settings["policy_epoch"]),
            "mode": str(settings["mode"]),
            "master_enabled": bool(settings["master_enabled"]),
        },
        "target_actions": actions,
        "remote": stable_remote,
        "schema_backup": backup,
        "pre_change_digest": pre_change_digest,
        "non_target_invariants": non_target_invariants,
        "mutation_safety": mutation_safety,
    }
    return {
        **identity,
        "plan_fingerprint": _fingerprint(identity),
        "action_counts": {
            name: sum(1 for item in actions if item["action"] == name)
            for name in sorted({item["action"] for item in actions})
        },
        "coverage_confirmed": len(actions) == len(feedback_ids)
        and bool(remote.get("full_list_stable"))
        and bool(remote.get("count_matches_list"))
        and int(remote.get("non_t0_unanswered_count") or 0) == 0
        and all(
            bool(item.get("answer_present")) or bool(item.get("listed_unanswered"))
            for item in details_by_id.values()
        )
        and bool(backup.get("verified"))
        and mutation_safety["runtime_stop_reason"] != "budget_state_unknown"
        and not mutation_safety["unresolved_provider_cost_processing_keys"]
        and not mutation_safety["active_reservation_processing_keys"]
        and str(settings["mode"]) == "auto_all"
        and bool(settings["master_enabled"])
        and str(settings["policy_version"]) in {PREVIOUS_POLICY_VERSION, DEFAULT_POLICY_VERSION},
        "external_io": "official_wb_get_only",
        "wb_post_count": 0,
        "provider_call_count": 0,
    }


def _validate_planned_resume(
    *,
    persisted_plan: Mapping[str, Any],
    current_plan: Mapping[str, Any],
    manifest: Mapping[str, Any],
    details: Mapping[str, Mapping[str, Any]],
    expected_fingerprint: str,
) -> dict[str, Any]:
    """Accept only a deterministic prefix of this runner's own detail upserts."""

    plan = dict(persisted_plan)
    if (
        plan.get("contract") != CONTRACT
        or plan.get("plan_fingerprint") != expected_fingerprint
        or plan.get("manifest_sha256") != manifest["manifest_sha256"]
        or plan.get("coverage_confirmed") is not True
        or plan.get("current_policy") != current_plan.get("current_policy")
        or plan.get("schema_backup") != current_plan.get("schema_backup")
        or plan.get("remote") != current_plan.get("remote")
        or plan.get("mutation_safety") != current_plan.get("mutation_safety")
        or plan.get("non_target_invariants")
        != current_plan.get("non_target_invariants")
    ):
        raise RuntimeError("persisted recovery plan or its immutable evidence changed")
    original_actions = {
        str(item["feedback_id"]): dict(item)
        for item in plan.get("target_actions") or []
    }
    current_actions = {
        str(item["feedback_id"]): dict(item)
        for item in current_plan.get("target_actions") or []
    }
    if set(original_actions) != set(current_actions) or set(original_actions) != set(details):
        raise RuntimeError("persisted recovery target set changed")
    for feedback_id, original in original_actions.items():
        current = current_actions[feedback_id]
        if current == original:
            continue
        expected_content_hash = sha256_text(
            canonical_json(content_projection(details[feedback_id]))
        )
        if (
            str(original.get("remote_content_hash") or "")
            != expected_content_hash
            or str(current.get("content_version_hash") or "")
            != expected_content_hash
            or current.get("processing_key") is not None
            or current.get("publication_key") is not None
            or current.get("action") != "ingest_and_generate"
        ):
            raise RuntimeError(
                f"persisted recovery target changed outside its detail-upsert prefix: {feedback_id}"
            )
    return plan


def _archive_original_job(
    conn: sqlite3.Connection,
    job: Mapping[str, Any],
    *,
    reason: str,
    at: datetime,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO sheet_vitrina_v1_wb_autoanswer_job_revisions(
            revision_id,processing_key,media_processing_version,previous_state,
            result_json,final_route,final_reply,final_reply_sha256,media_uncertain,
            actual_cost_usd,reason,archived_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            uuid4().hex,
            job["processing_key"],
            int(job["media_processing_version"] or 1),
            str(job["state"]),
            job["result_json"],
            job["final_route"],
            job["final_reply"],
            job["final_reply_sha256"],
            int(bool(job["media_uncertain"])),
            str(job["actual_cost_usd"] or "0"),
            reason,
            iso_utc(at),
        ),
    )


def _set_generated_and_enqueue(
    repo: AutoanswersRepository,
    conn: sqlite3.Connection,
    *,
    job: Mapping[str, Any],
    feedback: Mapping[str, Any],
    route: str,
    reply: str,
    policy_epoch: int,
    actor: str,
    at: datetime,
    source: str,
) -> None:
    selected_route = route
    selected_reply = reply
    transform: dict[str, Any] | None = None
    if route == "seller_chat":
        selected = safe_public_template(str(job["feedback_id"]), int(feedback["rating"] or 0))
        selected_route = str(selected["route"])
        selected_reply = str(selected["reply"])
        transform = {
            "contract": "wb_autoanswers_safe_public_policy_v1",
            "source_route": route,
            "source_reply_sha256": final_reply_hash(reply),
            "template_id": selected["template_id"],
            "operator_handoff": False,
            "model_calls": 0,
        }
        _archive_original_job(
            conn,
            job,
            reason="seller_chat_safe_public_policy_v4",
            at=at,
        )
    digest = final_reply_hash(selected_reply)
    result = {
        "final_route": selected_route,
        "final_reply": selected_reply,
        "case_code": None,
        "hard_gates_passed": True,
        "fallback_used": False,
        "media_uncertain": False,
        "node_contract_valid": True,
        "pipeline_result": {
            "route": selected_route,
            "publication_action": "draft",
            "recovery_source": source,
        },
    }
    if transform:
        result["server_policy_transform"] = transform
    conn.execute(
        """
        UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
        SET state=?,policy_epoch=?,policy_version=?,final_route=?,case_code=NULL,
            final_reply=?,final_reply_sha256=?,result_json=?,hard_gates_passed=1,
            fallback_used=0,media_uncertain=0,node_contract_valid=1,
            regeneration_required=0,regeneration_reason=NULL,
            review_reasons_json='[]',last_error_code=NULL,lease_owner=NULL,
            lease_until=NULL,retry_stage=NULL,completed_at=?,updated_at=?
        WHERE processing_key=?
        """,
        (
            STATE_APPROVED,
            policy_epoch,
            DEFAULT_POLICY_VERSION,
            selected_route,
            selected_reply,
            digest,
            canonical_json(result),
            iso_utc(at),
            iso_utc(at),
            job["processing_key"],
        ),
    )
    adopted = dict(job)
    adopted.update(
        {
            "policy_epoch": policy_epoch,
            "policy_version": DEFAULT_POLICY_VERSION,
            "final_route": selected_route,
            "final_reply": selected_reply,
            "final_reply_sha256": digest,
        }
    )
    repo._create_publication_job(
        conn,
        job=adopted,
        reply=selected_reply,
        reply_sha=digest,
        request_source="automatic",
        requested_by=None,
        mode_at_enqueue="auto_all",
        manual_edit_revision=None,
        at=at,
    )
    repo._audit(
        conn,
        aggregate_type="processing_job",
        aggregate_id=str(job["processing_key"]),
        event_type="backlog_recovery_generation_adopted",
        actor_type="recovery",
        actor_id=actor,
        details={
            "source": source,
            "route": selected_route,
            "reply_sha256": digest,
            "provider_calls": 0,
            "operator_handoff": False,
        },
        at=at,
        previous_state=str(job["state"]),
        next_state=STATE_APPROVED,
    )


def apply_plan(
    runtime_dir: Path,
    *,
    manifest: Mapping[str, Any],
    remote: Mapping[str, Any],
    details: Mapping[str, Mapping[str, Any]],
    expected_fingerprint: str,
    actor: str,
    approval_reference: str,
) -> dict[str, Any]:
    if not str(approval_reference).strip():
        raise ValueError("backlog recovery apply requires an exact human approval reference")
    if not str(actor).strip():
        raise ValueError("backlog recovery apply requires an explicit actor")
    repo = AutoanswersRepository(runtime_dir=runtime_dir)
    feedback_ids = sorted(str(item["feedback_id"]) for item in manifest["items"])
    if set(details) != set(feedback_ids):
        raise RuntimeError("apply details do not match the exact T0 manifest")
    recovery_id = "backlog-recovery:" + expected_fingerprint.removeprefix("sha256:")
    with closing(repo._connect()) as conn:
        persisted = conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_wb_autoanswers_backlog_recovery_runs
            WHERE plan_fingerprint=?
            """,
            (expected_fingerprint,),
        ).fetchone()
    if persisted is not None and str(persisted["state"]) == "applied":
        return {
            "contract": CONTRACT,
            "status": str(persisted["state"]),
            "idempotent": True,
            "recovery_id": str(persisted["recovery_id"]),
            "plan_fingerprint": expected_fingerprint,
        }
    with _open(runtime_dir, read_only=True) as conn:
        current_plan = build_plan(
            conn,
            runtime_dir=runtime_dir,
            manifest=manifest,
            remote=remote,
        )
    if persisted is not None:
        persisted_evidence = json.loads(str(persisted["evidence_json"] or "{}"))
        persisted_plan = dict(persisted_evidence.get("plan") or {})
        plan = _validate_planned_resume(
            persisted_plan=persisted_plan,
            current_plan=current_plan,
            manifest=manifest,
            details=details,
            expected_fingerprint=expected_fingerprint,
        )
    else:
        plan = current_plan
        if plan["plan_fingerprint"] != expected_fingerprint:
            raise RuntimeError("recovery plan fingerprint changed")
        if not plan["coverage_confirmed"]:
            raise RuntimeError("recovery coverage or backup precondition failed")
        with repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_autoanswers_backlog_recovery_runs(
                    recovery_id,contract,manifest_sha256,plan_fingerprint,
                    pre_change_digest,expected_feedback_count,state,evidence_json,
                    actor_id,created_at
                ) VALUES(?,?,?,?,?,?,'planned',?,?,?)
                """,
                (
                    recovery_id,
                    CONTRACT,
                    manifest["manifest_sha256"],
                    expected_fingerprint,
                    plan["pre_change_digest"],
                    len(feedback_ids),
                    canonical_json({"plan": plan}),
                    actor,
                    iso_utc(_now()),
                ),
            )

    # Persist every exact T0 detail only inside explicit apply.  This makes a
    # pre-existing external answer part of DB/API reconciliation while keeping
    # dry-run and readback strictly query-only.
    for action in plan["target_actions"]:
        repo.upsert_feedback(
            details[action["feedback_id"]],
            source_stream="t0_backlog_recovery",
            run_kind="reconciliation",
        )

    at = _now()
    with repo.transaction() as conn:
        existing = conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_wb_autoanswers_backlog_recovery_runs
            WHERE plan_fingerprint=?
            """,
            (expected_fingerprint,),
        ).fetchone()
        if existing is not None and str(existing["state"]) == "applied":
            return {
                "contract": CONTRACT,
                "status": str(existing["state"]),
                "idempotent": True,
                "recovery_id": str(existing["recovery_id"]),
                "plan_fingerprint": expected_fingerprint,
            }
        if _mutation_safety_snapshot(conn) != plan["mutation_safety"]:
            raise RuntimeError("mutation safety gates changed after the reviewed recovery plan")
        settings = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1"
        ).fetchone()
        if settings is None or not bool(settings["master_enabled"]) or str(settings["mode"]) != "auto_all":
            raise RuntimeError("Autoanswers must be effectively configured for auto_all")
        if str(settings["policy_version"]) not in {PREVIOUS_POLICY_VERSION, DEFAULT_POLICY_VERSION}:
            raise RuntimeError("unexpected current Autoanswers policy version")
        current_policy = {
            "policy_version": str(settings["policy_version"]),
            "policy_epoch": int(settings["policy_epoch"]),
            "mode": str(settings["mode"]),
            "master_enabled": bool(settings["master_enabled"]),
        }
        if current_policy != plan["current_policy"]:
            raise RuntimeError("Autoanswers policy changed after the reviewed recovery plan")
        non_target_before = _non_target_snapshot(conn, feedback_ids)
        if non_target_before != plan["non_target_invariants"]:
            raise RuntimeError("non-target invariants changed after the reviewed recovery plan")
        next_epoch = int(settings["policy_epoch"]) + int(
            str(settings["policy_version"]) != DEFAULT_POLICY_VERSION
        )
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_wb_autoanswers_settings
            SET policy_epoch=?,policy_version=?,updated_at=? WHERE singleton=1
            """,
            (next_epoch, DEFAULT_POLICY_VERSION, iso_utc(at)),
        )
        applied_actions: list[dict[str, Any]] = []
        for planned in plan["target_actions"]:
            feedback_id = str(planned["feedback_id"])
            if _answer_text(details[feedback_id]):
                applied_actions.append({"feedback_id": feedback_id, "action": "preexisting_external_answer"})
                continue
            feedback = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_feedbacks WHERE feedback_id=?",
                (feedback_id,),
            ).fetchone()
            if feedback is None:
                raise RuntimeError(f"target feedback was not materialized: {feedback_id}")
            job = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_wb_autoanswer_jobs
                WHERE feedback_id=? AND content_version=? AND bundle_version=?
                """,
                (feedback_id, feedback["content_version"], PROMPT_BUNDLE_VERSION),
            ).fetchone()
            if job is None:
                key = processing_key(feedback_id, int(feedback["content_version"]))
                kind = (
                    PROCESSING_KIND_RATING_ONLY_TEMPLATE
                    if str(feedback["content_classification"]) == "rating_only"
                    else PROCESSING_KIND_FROZEN_AI
                )
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_autoanswer_jobs(
                        processing_key,feedback_id,content_version,content_version_hash,
                        state,trigger_source,bundle_version,evaluation_signature,
                        policy_version,enable_epoch,policy_epoch,processing_kind,
                        transition_run_id,available_at,attempts,created_at,updated_at
                    ) VALUES(?,?,?,?,?,'t0_backlog_recovery',?,?,?,?,?,?,NULL,?,0,?,?)
                    """,
                    (
                        key,
                        feedback_id,
                        feedback["content_version"],
                        feedback["content_version_hash"],
                        STATE_QUEUED,
                        PROMPT_BUNDLE_VERSION,
                        EVALUATION_SIGNATURE,
                        DEFAULT_POLICY_VERSION,
                        settings["enable_epoch"],
                        next_epoch,
                        kind,
                        iso_utc(at),
                        iso_utc(at),
                        iso_utc(at),
                    ),
                )
                repo._audit(
                    conn,
                    aggregate_type="processing_job",
                    aggregate_id=key,
                    event_type="t0_missing_feedback_queued",
                    actor_type="recovery",
                    actor_id=actor,
                    details={"processing_kind": kind, "manifest_sha256": manifest["manifest_sha256"]},
                    at=at,
                    previous_state="synced",
                    next_state=STATE_QUEUED,
                )
                applied_actions.append({"feedback_id": feedback_id, "action": "generation_queued"})
                continue
            publication = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_publication_jobs WHERE processing_key=?",
                (job["processing_key"],),
            ).fetchone()
            if publication is not None:
                attempts = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts WHERE publication_key=?",
                        (publication["publication_key"],),
                    ).fetchone()[0]
                )
                if publication["write_started_at"] or attempts:
                    raise RuntimeError(f"target has ambiguous WB write evidence: {feedback_id}")
                adopted_route = str(job["final_route"] or "")
                if adopted_route == "rating_only_template" and str(feedback["content_classification"]) != "rating_only":
                    adopted_route = "public_only"
                if adopted_route == "seller_chat":
                    raise RuntimeError(f"seller_chat publication cannot be rebound: {feedback_id}")
                try:
                    adopted_result = json.loads(str(job["result_json"] or "{}"))
                except json.JSONDecodeError:
                    adopted_result = {}
                if not isinstance(adopted_result, dict):
                    adopted_result = {}
                if adopted_route != str(job["final_route"] or ""):
                    adopted_result["server_policy_rebind"] = {
                        "contract": CONTRACT,
                        "source_route": str(job["final_route"] or ""),
                        "source_reply_sha256": str(job["final_reply_sha256"] or ""),
                        "publication_route": adopted_route,
                        "reason": "current_content_classification_is_not_rating_only",
                        "operator_handoff": False,
                        "model_calls": 0,
                    }
                    adopted_result["final_route"] = adopted_route
                    adopted_result["final_reply"] = str(job["final_reply"] or "")
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET state=?,policy_epoch=?,policy_version=?,final_route=?,result_json=?,
                        regeneration_required=0,regeneration_reason=NULL,
                        review_reasons_json='[]',last_error_code=NULL,updated_at=?
                    WHERE processing_key=?
                    """,
                    (
                        STATE_APPROVED,
                        next_epoch,
                        DEFAULT_POLICY_VERSION,
                        adopted_route,
                        canonical_json(adopted_result),
                        iso_utc(at),
                        job["processing_key"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_publication_jobs
                    SET state=?,policy_epoch=?,mode_at_enqueue='auto_all',
                        last_error_code=NULL,available_at=?,lease_owner=NULL,
                        lease_until=NULL,updated_at=? WHERE publication_key=?
                    """,
                    (
                        STATE_APPROVED,
                        next_epoch,
                        iso_utc(at),
                        iso_utc(at),
                        publication["publication_key"],
                    ),
                )
                repo._audit(
                    conn,
                    aggregate_type="publication_job",
                    aggregate_id=str(publication["publication_key"]),
                    event_type="t0_publication_rebound_without_new_ai_or_post",
                    actor_type="recovery",
                    actor_id=actor,
                    details={
                        "reply_sha256": publication["normalized_reply_sha256"],
                        "provider_calls": 0,
                        "wb_posts": 0,
                        "policy_epoch": next_epoch,
                    },
                    at=at,
                    previous_state=str(publication["state"]),
                    next_state=STATE_APPROVED,
                )
                applied_actions.append({"feedback_id": feedback_id, "action": "publication_rebound"})
                continue
            evidence = AutoanswersRepository._completed_node_evidence(
                conn, str(job["processing_key"])
            )
            if str(job["final_route"] or "") == "seller_chat":
                _set_generated_and_enqueue(
                    repo,
                    conn,
                    job=job,
                    feedback=feedback,
                    route="seller_chat",
                    reply=str(job["final_reply"] or ""),
                    policy_epoch=next_epoch,
                    actor=actor,
                    at=at,
                    source="seller_chat_safe_public_transform",
                )
                applied_actions.append({"feedback_id": feedback_id, "action": "safe_public_transformed"})
            elif evidence is not None:
                _set_generated_and_enqueue(
                    repo,
                    conn,
                    job=job,
                    feedback=feedback,
                    route=str(evidence["route"]),
                    reply=str(evidence["reply"]),
                    policy_epoch=next_epoch,
                    actor=actor,
                    at=at,
                    source="append_only_node_audit",
                )
                applied_actions.append({"feedback_id": feedback_id, "action": "audited_generation_recovered"})
            elif job["final_reply"] and job["hard_gates_passed"] and job["node_contract_valid"]:
                _set_generated_and_enqueue(
                    repo,
                    conn,
                    job=job,
                    feedback=feedback,
                    route=str(job["final_route"] or "public_only"),
                    reply=str(job["final_reply"]),
                    policy_epoch=next_epoch,
                    actor=actor,
                    at=at,
                    source="existing_valid_generation",
                )
                applied_actions.append({"feedback_id": feedback_id, "action": "existing_generation_enqueued"})
            else:
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_autoanswer_jobs
                    SET state=?,processing_kind=?,policy_epoch=?,policy_version=?,
                        trigger_source='t0_safe_public_recovery',available_at=?,
                        lease_owner=NULL,lease_until=NULL,retry_stage=NULL,
                        regeneration_required=0,regeneration_reason=NULL,
                        completed_at=NULL,updated_at=? WHERE processing_key=?
                    """,
                    (
                        STATE_QUEUED,
                        PROCESSING_KIND_SAFE_PUBLIC_TEMPLATE,
                        next_epoch,
                        DEFAULT_POLICY_VERSION,
                        iso_utc(at),
                        iso_utc(at),
                        job["processing_key"],
                    ),
                )
                repo._audit(
                    conn,
                    aggregate_type="processing_job",
                    aggregate_id=str(job["processing_key"]),
                    event_type="t0_safe_public_recovery_queued",
                    actor_type="recovery",
                    actor_id=actor,
                    details={"source_error_code": job["last_error_code"], "provider_calls": 0},
                    at=at,
                    previous_state=str(job["state"]),
                    next_state=STATE_QUEUED,
                )
                applied_actions.append({"feedback_id": feedback_id, "action": "safe_public_queued"})
        non_target_after = _non_target_snapshot(conn, feedback_ids)
        if non_target_after != non_target_before:
            raise RuntimeError("non-target invariants changed during apply")
        evidence = {
            "manifest_sha256": manifest["manifest_sha256"],
            "approval_reference": str(approval_reference).strip(),
            "policy_epoch_before": int(settings["policy_epoch"]),
            "policy_epoch_after": next_epoch,
            "policy_version_before": str(settings["policy_version"]),
            "policy_version_after": DEFAULT_POLICY_VERSION,
            "applied_actions": applied_actions,
            "non_target_invariants": non_target_after,
            "wb_posts": 0,
            "provider_calls": 0,
        }
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_wb_autoanswers_backlog_recovery_runs
            SET state='applied',evidence_json=?,actor_id=?,applied_at=?
            WHERE recovery_id=?
            """,
            (
                canonical_json(evidence),
                actor,
                iso_utc(at),
                recovery_id,
            ),
        )
    return {
        "contract": CONTRACT,
        "status": "applied",
        "idempotent": False,
        "recovery_id": recovery_id,
        "plan_fingerprint": expected_fingerprint,
        "manifest_sha256": manifest["manifest_sha256"],
        "approval_reference": str(approval_reference).strip(),
        "applied_count": len(feedback_ids),
        "wb_posts_by_runner": 0,
        "provider_calls_by_runner": 0,
    }


def _local_zero_tail(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS local_unanswered,
          SUM(CASE WHEN j.processing_key IS NULL THEN 1 ELSE 0 END) AS not_materialized,
          SUM(CASE WHEN j.state='needs_review' THEN 1 ELSE 0 END) AS needs_review,
          SUM(CASE WHEN j.state='terminal_error' OR p.state='terminal_error' THEN 1 ELSE 0 END)
            AS terminal_error,
          SUM(CASE WHEN j.last_error_code='policy_epoch_stale'
                        OR p.last_error_code='policy_epoch_stale' THEN 1 ELSE 0 END)
            AS policy_epoch_stale,
          SUM(CASE WHEN j.final_route='seller_chat'
                        AND COALESCE(p.state,'')<>'published' THEN 1 ELSE 0 END)
            AS seller_chat_unpublished,
          SUM(CASE WHEN p.state IN ('publishing','publish_pending_readback')
                        OR (p.state='retryable_error' AND p.retry_stage='readback')
                        OR (
                          COALESCE(p.state,'')<>'published'
                          AND (
                            p.write_started_at IS NOT NULL
                            OR EXISTS(
                              SELECT 1
                              FROM sheet_vitrina_v1_wb_publication_attempts a
                              WHERE a.publication_key=p.publication_key
                            )
                          )
                        ) THEN 1 ELSE 0 END) AS ambiguous_write_tail,
          SUM(CASE WHEN j.state IN ('queued','processing','retryable_error','approved')
                        OR p.state IN ('approved','publishing','publish_pending_readback','retryable_error')
                   THEN 1 ELSE 0 END) AS active_pipeline_tail
        FROM sheet_vitrina_v1_wb_feedbacks f
        LEFT JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
          ON j.feedback_id=f.feedback_id
         AND j.content_version=f.content_version
         AND j.bundle_version=?
        LEFT JOIN sheet_vitrina_v1_wb_publication_jobs p
          ON p.processing_key=j.processing_key
        WHERE COALESCE(f.answer_text,'')=''
        """,
        (PROMPT_BUNDLE_VERSION,),
    ).fetchone()
    result = {key: int(row[key] or 0) for key in row.keys()}
    runtime = conn.execute(
        "SELECT stop_reason FROM sheet_vitrina_v1_wb_autoanswers_runtime_state WHERE singleton=1"
    ).fetchone()
    result["budget_state_unknown"] = int(
        runtime is not None and str(runtime["stop_reason"] or "") == "budget_state_unknown"
    )
    result["unresolved_provider_cost"] = len(
        AutoanswersRepository._budget_uncertainty_candidates(conn)
    )
    result["active_reservations"] = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
            WHERE status='reserved'
            """
        ).fetchone()[0]
    )
    return result


def reconcile_readback(
    runtime_dir: Path,
    *,
    manifest: Mapping[str, Any],
    remote: Mapping[str, Any],
    details: Mapping[str, Mapping[str, Any]],
    actor: str,
) -> dict[str, Any]:
    items = list(remote["t0_details"])
    unresolved = [item for item in items if not item["answer_present"]]
    feedback_ids = sorted(str(item["feedback_id"]) for item in manifest["items"])
    if set(details) != set(feedback_ids) or {
        str(item["feedback_id"]) for item in items
    } != set(feedback_ids):
        raise RuntimeError("readback details do not match the exact T0 manifest")
    with _open(runtime_dir, read_only=True) as conn:
        local = _target_rows(conn, feedback_ids)
        local_by_id = {str(item["feedback_id"]): item for item in local}
        unconfirmed_local = [
            feedback_id
            for feedback_id in feedback_ids
            if not bool((local_by_id.get(feedback_id) or {}).get("answer_text"))
        ]
        zero_tail = _local_zero_tail(conn)
        recovery = conn.execute(
            """
            SELECT recovery_id,state,plan_fingerprint,applied_at
            FROM sheet_vitrina_v1_wb_autoanswers_backlog_recovery_runs
            WHERE manifest_sha256=? ORDER BY created_at DESC LIMIT 1
            """,
            (manifest["manifest_sha256"],),
        ).fetchone()
    remote_zero = (
        int(remote["full_list_count"]) == 0
        and int(remote["count_endpoint"]) == 0
        and bool(remote.get("full_list_stable"))
        and bool(remote.get("count_matches_list"))
        and int(remote.get("non_t0_unanswered_count") or 0) == 0
    )
    reconciled = (
        not unresolved
        and remote_zero
        and not unconfirmed_local
        and not any(zero_tail.values())
        and recovery is not None
        and str(recovery["state"]) == "applied"
    )
    return {
        "contract": CONTRACT,
        "status": "reconciled" if reconciled else "pending",
        "manifest_sha256": manifest["manifest_sha256"],
        "t0_count": len(feedback_ids),
        "t0_answered_detail_get": len(feedback_ids) - len(unresolved),
        "t0_unresolved": [item["feedback_id"] for item in unresolved],
        "local_unconfirmed": unconfirmed_local,
        "local_zero_tail": zero_tail,
        "full_unanswered_count": int(remote["full_list_count"]),
        "count_endpoint": int(remote["count_endpoint"]),
        "count_matches_list": bool(remote.get("count_matches_list")),
        "full_list_stable": bool(remote.get("full_list_stable")),
        "full_unanswered_zero": remote_zero,
        "detail_get_count": len(items),
        "recovery_run": dict(recovery) if recovery is not None else None,
        "read_only": True,
        "actor": str(actor),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("capture", "dry-run", "apply", "readback"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    manifest_group = parser.add_mutually_exclusive_group()
    manifest_group.add_argument("--manifest", type=Path)
    manifest_group.add_argument("--manifest-stdin", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--expected-deployed-sha")
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--approval-reference")
    parser.add_argument("--actor", default="codex-backlog-recovery")
    args = parser.parse_args()
    if args.env_file is not None:
        _load_safe_env_file(args.env_file.resolve())
    if str(os.environ.get(EXTERNAL_GATE_ENV) or "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise RuntimeError("external IO gate is OFF")
    deployed_evidence = (
        _deployed_runtime_evidence(str(args.expected_deployed_sha))
        if args.expected_deployed_sha
        else None
    )
    if args.operation == "apply" and deployed_evidence is None:
        parser.error("apply requires --expected-deployed-sha")
    source = HttpBackedWbAutoanswersReadAdapter()
    if args.operation == "capture":
        if args.manifest is not None or args.manifest_stdin:
            parser.error("capture does not accept a manifest")
        payload = {
            "contract": CONTRACT,
            "status": "captured",
            "manifest": capture_t0_manifest(source),
            "external_io": "official_wb_get_only",
            "deployed_runtime": deployed_evidence,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    if args.manifest_stdin:
        manifest = validate_manifest(json.load(sys.stdin))
    elif args.manifest is not None:
        manifest = load_manifest(args.manifest)
    else:
        parser.error(f"{args.operation} requires --manifest or --manifest-stdin")
    remote, details = fetch_remote_evidence(source, manifest)
    if args.operation == "dry-run":
        with _open(args.runtime_dir, read_only=True) as conn:
            payload = build_plan(
                conn,
                runtime_dir=args.runtime_dir,
                manifest=manifest,
                remote=remote,
            )
    elif args.operation == "apply":
        if not args.expected_fingerprint:
            parser.error("apply requires --expected-fingerprint")
        payload = apply_plan(
            args.runtime_dir,
            manifest=manifest,
            remote=remote,
            details=details,
            expected_fingerprint=str(args.expected_fingerprint),
            actor=str(args.actor),
            approval_reference=str(args.approval_reference or ""),
        )
    else:
        payload = reconcile_readback(
            args.runtime_dir,
            manifest=manifest,
            remote=remote,
            details=details,
            actor=str(args.actor),
        )
    if deployed_evidence is not None:
        payload["deployed_runtime"] = deployed_evidence
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if payload.get("coverage_confirmed", True) and payload.get("status") != "pending" else 2


if __name__ == "__main__":
    raise SystemExit(main())
