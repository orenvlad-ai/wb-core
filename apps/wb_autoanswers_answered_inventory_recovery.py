#!/usr/bin/env python3
"""Reconcile stale local observations from the full WB processed inventory.

Capture, dry-run and readback use only official WB GETs and query-only SQLite.
Apply requires an exact deployed SHA, manifest, reviewed plan fingerprint and
human approval reference.  It updates only feedback observations proven either
answered or officially processed without an answer by WB, performs no provider
call or WB POST, and is resumable through the schema-v10 recovery ledger.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_autoanswers_backlog_recovery import (  # noqa: E402
    EXTERNAL_GATE_ENV,
    RecoveryPacedReadPort,
    _answer_text,
    _content_hash,
    _deployed_runtime_evidence,
    _fingerprint,
    _load_safe_env_file,
    _local_zero_tail,
    _now,
    _open,
    _verified_backup,
)
from packages.adapters.wb_autoanswers import (  # noqa: E402
    HttpBackedWbAutoanswersReadAdapter,
    WbFeedbackReadPort,
)
from packages.application.wb_autoanswers_runtime import (  # noqa: E402
    OFFICIAL_PROCESSED_STATE,
    AutoanswersRepository,
    canonical_json,
    final_reply_hash,
    iso_utc,
)
from packages.application.wb_autoanswers_lifecycle import (  # noqa: E402
    AutoanswersLifecycle,
)


CONTRACT = "wb_autoanswers_answered_inventory_recovery_v2"
MANIFEST_CONTRACT = "wb_autoanswers_processed_inventory_manifest_v2"
SOURCE_STREAM = "answered_inventory_recovery"
PAGE_SIZE = 5000
QUERY_CHUNK = 400
RESOLUTION_ANSWER_OBSERVED = "answer_observed"
RESOLUTION_PROCESSED_WITHOUT_ANSWER = "processed_without_answer"


def _chunks(values: list[str], size: int = QUERY_CHUNK) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _manifest_item(row: Mapping[str, Any]) -> dict[str, str]:
    feedback_id = str(row.get("id") or "").strip()
    answer = _answer_text(row)
    if not feedback_id:
        raise RuntimeError("WB answered inventory contains an empty feedback ID")
    state = str(row.get("state") or "").strip()
    if answer:
        resolution_kind = RESOLUTION_ANSWER_OBSERVED
        answer_sha256 = final_reply_hash(answer)
    elif state == OFFICIAL_PROCESSED_STATE:
        resolution_kind = RESOLUTION_PROCESSED_WITHOUT_ANSWER
        answer_sha256 = ""
    else:
        raise RuntimeError(
            "WB processed inventory row has neither an answer nor the canonical "
            f"processed state: {feedback_id}"
        )
    return {
        "feedback_id": feedback_id,
        "content_hash": _content_hash(row),
        "resolution_kind": resolution_kind,
        "answer_sha256": answer_sha256,
    }


def _fetch_full_answered(
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
            is_answered=True,
            take=PAGE_SIZE,
            skip=skip,
        )
        rows.extend(page.rows)
        if not page.has_more:
            break
        skip += page.take
    ids = [str(row.get("id") or "").strip() for row in rows]
    if any(not feedback_id for feedback_id in ids):
        raise RuntimeError("WB answered inventory contains an empty feedback ID")
    if len(ids) != len(set(ids)):
        raise RuntimeError("WB answered inventory contains duplicate feedback IDs")
    return rows


def _projection(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, str]], dict[str, Mapping[str, Any]]]:
    by_id: dict[str, Mapping[str, Any]] = {}
    items: list[dict[str, str]] = []
    for row in rows:
        item = _manifest_item(row)
        feedback_id = item["feedback_id"]
        if feedback_id in by_id:
            raise RuntimeError("WB answered inventory contains duplicate feedback IDs")
        by_id[feedback_id] = row
        items.append(item)
    items.sort(key=lambda item: item["feedback_id"])
    return items, by_id


def capture_manifest(source: WbFeedbackReadPort) -> dict[str, Any]:
    captured_at = _now()
    first, _ = _projection(
        _fetch_full_answered(source, captured_at=captured_at)
    )
    second, _ = _projection(
        _fetch_full_answered(source, captured_at=captured_at)
    )
    if first != second:
        raise RuntimeError("full WB answered inventory changed during capture")
    if not first:
        raise RuntimeError("full WB answered inventory is empty")
    manifest: dict[str, Any] = {
        "contract": MANIFEST_CONTRACT,
        "captured_at": iso_utc(captured_at),
        "items": first,
    }
    manifest["manifest_sha256"] = _fingerprint(manifest)
    return manifest


def validate_manifest(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("contract") != MANIFEST_CONTRACT:
        raise ValueError("an exact processed-inventory manifest is required")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("processed-inventory manifest contains no items")
    ids: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("processed-inventory manifest item is not an object")
        feedback_id = str(item.get("feedback_id") or "").strip()
        content_hash = str(item.get("content_hash") or "").strip()
        resolution_kind = str(item.get("resolution_kind") or "").strip()
        answer_sha256 = str(item.get("answer_sha256") or "").strip()
        content_hash_valid = len(content_hash) == 64 and not any(
            character not in "0123456789abcdef" for character in content_hash
        )
        answer_hash_valid = len(answer_sha256) == 64 and not any(
            character not in "0123456789abcdef" for character in answer_sha256
        )
        resolution_valid = (
            resolution_kind == RESOLUTION_ANSWER_OBSERVED and answer_hash_valid
        ) or (
            resolution_kind == RESOLUTION_PROCESSED_WITHOUT_ANSWER
            and not answer_sha256
        )
        if not feedback_id or not content_hash_valid or not resolution_valid:
            raise ValueError("processed-inventory manifest item is incomplete")
        ids.append(feedback_id)
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise ValueError("processed-inventory manifest IDs must be sorted and unique")
    supplied = str(payload.get("manifest_sha256") or "")
    calculated = _fingerprint(
        {key: value for key, value in payload.items() if key != "manifest_sha256"}
    )
    if supplied != calculated:
        raise ValueError("processed-inventory manifest fingerprint mismatch")
    return payload


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    manifest = payload.get("manifest") if isinstance(payload, Mapping) else None
    return validate_manifest(manifest if manifest is not None else payload)


def fetch_remote_evidence(
    source: WbFeedbackReadPort,
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]]]:
    captured_at = _now()
    first, first_by_id = _projection(
        _fetch_full_answered(source, captured_at=captured_at)
    )
    second, _ = _projection(
        _fetch_full_answered(source, captured_at=captured_at)
    )
    if first != second:
        raise RuntimeError("full WB answered inventory changed during evidence capture")
    expected = {
        str(item["feedback_id"]): {
            "content_hash": str(item["content_hash"]),
            "resolution_kind": str(item["resolution_kind"]),
            "answer_sha256": str(item["answer_sha256"]),
        }
        for item in manifest["items"]
    }
    current = {
        item["feedback_id"]: {
            "content_hash": item["content_hash"],
            "resolution_kind": item["resolution_kind"],
            "answer_sha256": item["answer_sha256"],
        }
        for item in first
    }
    missing = sorted(set(expected) - set(current))
    changed = sorted(
        feedback_id
        for feedback_id in set(expected) & set(current)
        if expected[feedback_id] != current[feedback_id]
    )
    coverage_confirmed = not missing and not changed
    return (
        {
            "captured_at": iso_utc(captured_at),
            "manifest_processed_count": len(expected),
            "manifest_answer_observed_count": sum(
                item["resolution_kind"] == RESOLUTION_ANSWER_OBSERVED
                for item in expected.values()
            ),
            "manifest_processed_without_answer_count": sum(
                item["resolution_kind"] == RESOLUTION_PROCESSED_WITHOUT_ANSWER
                for item in expected.values()
            ),
            "current_processed_count": len(current),
            "current_processed_ids_sha256": _fingerprint(sorted(current)),
            "full_processed_stable": True,
            "manifest_subset_confirmed": coverage_confirmed,
            "missing_manifest_count": len(missing),
            "changed_manifest_count": len(changed),
            "missing_manifest_examples": missing[:20],
            "changed_manifest_examples": changed[:20],
        },
        {feedback_id: first_by_id[feedback_id] for feedback_id in expected if feedback_id in first_by_id},
    )


def _local_rows(
    conn: sqlite3.Connection,
    feedback_ids: list[str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(feedback_ids):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"""
            SELECT feedback_id,content_version,content_version_hash,answer_text,
                   wb_observation_hash,source_stream,last_seen_at,raw_json
            FROM sheet_vitrina_v1_wb_feedbacks
            WHERE feedback_id IN ({placeholders})
            """,
            chunk,
        ).fetchall():
            result[str(row["feedback_id"])] = dict(row)
    return result


def _core_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_feedbacks) AS feedbacks,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswer_jobs) AS jobs,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_jobs) AS publications,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_publication_attempts) AS wb_writes,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations) AS reservations,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_cost_events) AS costs,
          (SELECT COUNT(*) FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events) AS failed_costs
        """
    ).fetchone()
    settings = conn.execute(
        """
        SELECT master_enabled,mode,enable_epoch,policy_epoch,policy_version,
               daily_cap_usd,monthly_cap_usd,hourly_cap_usd,
               max_paid_reviews_per_hour,global_paid_review_concurrency,
               max_inflight_role_calls,max_materialized_processing_jobs,
               warning_ratio,max_reservation_per_review_usd
        FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1
        """
    ).fetchone()
    return {
        "counts": {key: int(counts[key] or 0) for key in counts.keys()},
        "settings": {key: settings[key] for key in settings.keys()},
    }


def _mutation_safety(conn: sqlite3.Connection) -> dict[str, Any]:
    active = conn.execute(
        """
        SELECT processing_key FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
        WHERE status='reserved' ORDER BY processing_key
        """
    ).fetchall()
    unresolved = AutoanswersRepository._budget_uncertainty_candidates(conn)
    runtime = conn.execute(
        """
        SELECT stop_reason,stop_details_json
        FROM sheet_vitrina_v1_wb_autoanswers_runtime_state WHERE singleton=1
        """
    ).fetchone()
    return {
        "active_reservation_processing_keys": [str(row[0]) for row in active],
        "unresolved_provider_cost_processing_keys": sorted(
            str(item["processing_key"]) for item in unresolved
        ),
        "runtime_stop_reason": str(runtime["stop_reason"] or "") if runtime else "",
        "runtime_stop_details_json": str(runtime["stop_details_json"] or "{}") if runtime else "{}",
    }


def _lifecycle_pause_snapshot(runtime_dir: Path) -> dict[str, Any]:
    repository = AutoanswersRepository(runtime_dir=runtime_dir)
    lifecycle = AutoanswersLifecycle(
        runtime_dir=runtime_dir,
        repository=repository,
    ).status(suspended_by_master=True)
    components: dict[str, Any] = {}
    for key in ("readonly_sync", "worker"):
        item = dict((lifecycle.get("components") or {}).get(key) or {})
        service = dict(item.get("service") or {})
        timer = dict(item.get("timer") or {})
        components[key] = {
            "desired": bool(item.get("desired")),
            "actual": bool(item.get("actual")),
            "drift_status": str(item.get("drift_status") or ""),
            "service_active": str(service.get("is_active") or ""),
            "timer_active": str(timer.get("is_active") or ""),
            "timer_enabled": str(timer.get("is_enabled") or ""),
        }
    confirmed = (
        str(lifecycle.get("lifecycle_state") or "") == "suspended_by_master"
        and str(lifecycle.get("drift_status") or "") == "matched"
        and not bool(lifecycle.get("actual"))
        and not bool(lifecycle.get("service_in_progress"))
        and all(
            not item["desired"]
            and not item["actual"]
            and item["drift_status"] == "matched"
            and item["service_active"] not in {"active", "activating", "reloading"}
            and item["timer_active"] == "inactive"
            and item["timer_enabled"] == "disabled"
            for item in components.values()
        )
    )
    return {
        "confirmed": confirmed,
        "lifecycle_state": str(lifecycle.get("lifecycle_state") or ""),
        "drift_status": str(lifecycle.get("drift_status") or ""),
        "actual": bool(lifecycle.get("actual")),
        "service_in_progress": bool(lifecycle.get("service_in_progress")),
        "policy_epoch": int(lifecycle.get("policy_epoch") or 0),
        "transition_run_id": str(lifecycle.get("transition_run_id") or ""),
        "components": components,
    }


def build_plan(
    conn: sqlite3.Connection,
    *,
    runtime_dir: Path,
    manifest: Mapping[str, Any],
    remote: Mapping[str, Any],
    deployed_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    feedback_ids = [str(item["feedback_id"]) for item in manifest["items"]]
    local = _local_rows(conn, feedback_ids)
    remote_items = {str(item["feedback_id"]): item for item in manifest["items"]}
    actions: list[dict[str, Any]] = []
    for feedback_id in feedback_ids:
        row = local.get(feedback_id)
        local_answer = str((row or {}).get("answer_text") or "")
        local_answer_sha256 = final_reply_hash(local_answer) if local_answer else None
        remote_item = remote_items[feedback_id]
        resolution_kind = str(remote_item["resolution_kind"])
        expected_answer_sha256 = str(remote_item["answer_sha256"])
        content_matches = (
            row is not None
            and str(row["content_version_hash"])
            == str(remote_item["content_hash"])
        )
        try:
            local_raw = json.loads(str((row or {}).get("raw_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            local_raw = {}
        local_processed_without_answer = (
            not local_answer
            and isinstance(local_raw, Mapping)
            and str(local_raw.get("state") or "").strip() == OFFICIAL_PROCESSED_STATE
        )
        resolution_matches = (
            content_matches
            and resolution_kind == RESOLUTION_ANSWER_OBSERVED
            and local_answer_sha256 == expected_answer_sha256
        ) or (
            content_matches
            and resolution_kind == RESOLUTION_PROCESSED_WITHOUT_ANSWER
            and local_processed_without_answer
        )
        if resolution_matches:
            continue
        actions.append(
            {
                "feedback_id": feedback_id,
                "action": (
                    "insert_processed_observation"
                    if row is None
                    else "refresh_processed_observation"
                ),
                "resolution_kind": resolution_kind,
                "local_exists": row is not None,
                "local_content_version": int(row["content_version"]) if row is not None else None,
                "local_content_version_hash": str(row["content_version_hash"]) if row is not None else None,
                "local_answer_sha256": local_answer_sha256,
                "remote_content_hash": str(remote_items[feedback_id]["content_hash"]),
                "remote_answer_sha256": expected_answer_sha256 or None,
            }
        )
    snapshot = _core_snapshot(conn)
    safety = _mutation_safety(conn)
    lifecycle_pause = _lifecycle_pause_snapshot(runtime_dir)
    backup = _verified_backup(runtime_dir, 10)
    identity = {
        "contract": CONTRACT,
        "manifest_sha256": manifest["manifest_sha256"],
        "deployed_sha": str(deployed_runtime["runtime_sha"]),
        "target_actions": actions,
        "non_target_invariants": snapshot,
        "mutation_safety": safety,
        "lifecycle_pause": lifecycle_pause,
        "schema_backup": backup,
    }
    coverage_confirmed = bool(remote.get("manifest_subset_confirmed")) and bool(
        backup.get("verified")
    ) and not safety["active_reservation_processing_keys"] and not safety[
        "unresolved_provider_cost_processing_keys"
    ] and bool(lifecycle_pause["confirmed"])
    plan = {
        **identity,
        "coverage_confirmed": coverage_confirmed,
        "manifest_processed_count": len(feedback_ids),
        "manifest_answer_observed_count": sum(
            item["resolution_kind"] == RESOLUTION_ANSWER_OBSERVED
            for item in manifest["items"]
        ),
        "manifest_processed_without_answer_count": sum(
            item["resolution_kind"] == RESOLUTION_PROCESSED_WITHOUT_ANSWER
            for item in manifest["items"]
        ),
        "expected_local_updates": len(actions),
        "expected_local_inserts": sum(not action["local_exists"] for action in actions),
        "remote": dict(remote),
        "pre_change_digest": _fingerprint(
            {
                "target_actions": actions,
                "non_target_invariants": snapshot,
                "mutation_safety": safety,
                "lifecycle_pause": lifecycle_pause,
            }
        ),
        "provider_call_count": 0,
        "wb_post_count": 0,
    }
    plan["plan_fingerprint"] = _fingerprint(identity)
    return plan


def _stored_run(repo: AutoanswersRepository, fingerprint: str) -> sqlite3.Row | None:
    with closing(repo._connect()) as conn:
        return conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_wb_autoanswers_backlog_recovery_runs
            WHERE plan_fingerprint=?
            """,
            (fingerprint,),
        ).fetchone()


def _verify_target_resolutions(
    conn: sqlite3.Connection,
    actions: list[Mapping[str, Any]],
) -> list[str]:
    expected = {
        str(action["feedback_id"]): action
        for action in actions
    }
    local = _local_rows(conn, sorted(expected))
    unconfirmed: list[str] = []
    for feedback_id, action in expected.items():
        row = local.get(feedback_id)
        if row is None:
            unconfirmed.append(feedback_id)
            continue
        if str(row["content_version_hash"]) != str(action["remote_content_hash"]):
            unconfirmed.append(feedback_id)
            continue
        resolution_kind = str(action["resolution_kind"])
        answer = str(row.get("answer_text") or "")
        if resolution_kind == RESOLUTION_ANSWER_OBSERVED:
            if (
                not answer
                or final_reply_hash(answer) != str(action["remote_answer_sha256"])
            ):
                unconfirmed.append(feedback_id)
            continue
        try:
            raw = json.loads(str(row.get("raw_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
        if (
            answer
            or not isinstance(raw, Mapping)
            or str(raw.get("state") or "").strip() != OFFICIAL_PROCESSED_STATE
        ):
            unconfirmed.append(feedback_id)
    return sorted(unconfirmed)


def apply_plan(
    runtime_dir: Path,
    *,
    manifest: Mapping[str, Any],
    remote: Mapping[str, Any],
    remote_rows: Mapping[str, Mapping[str, Any]],
    deployed_runtime: Mapping[str, Any],
    expected_fingerprint: str,
    actor: str,
    approval_reference: str,
) -> dict[str, Any]:
    if not str(actor).strip() or not str(approval_reference).strip():
        raise ValueError("answered-inventory apply requires actor and human approval reference")
    repo = AutoanswersRepository(runtime_dir=runtime_dir)
    recovery_id = "answered-inventory:" + expected_fingerprint.removeprefix("sha256:")
    persisted = _stored_run(repo, expected_fingerprint)
    if persisted is not None and str(persisted["state"]) == "applied":
        return {
            "contract": CONTRACT,
            "status": "applied",
            "idempotent": True,
            "recovery_id": str(persisted["recovery_id"]),
            "plan_fingerprint": expected_fingerprint,
        }
    if persisted is None:
        with _open(runtime_dir, read_only=True) as conn:
            plan = build_plan(
                conn,
                runtime_dir=runtime_dir,
                manifest=manifest,
                remote=remote,
                deployed_runtime=deployed_runtime,
            )
        if plan["plan_fingerprint"] != expected_fingerprint:
            raise RuntimeError("answered-inventory recovery plan fingerprint changed")
        if not plan["coverage_confirmed"]:
            raise RuntimeError("answered-inventory recovery is not apply-ready")
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
                    int(plan["expected_local_updates"]),
                    canonical_json({"plan": plan, "approval_reference": approval_reference}),
                    str(actor).strip(),
                    iso_utc(_now()),
                ),
            )
    else:
        evidence = json.loads(str(persisted["evidence_json"] or "{}"))
        plan = dict(evidence.get("plan") or {})
        if (
            str(persisted["contract"]) != CONTRACT
            or str(persisted["manifest_sha256"]) != str(manifest["manifest_sha256"])
            or str(plan.get("plan_fingerprint") or "") != expected_fingerprint
            or str(plan.get("deployed_sha") or "") != str(deployed_runtime["runtime_sha"])
        ):
            raise RuntimeError("persisted answered-inventory recovery identity is invalid")

    actions = list(plan["target_actions"])
    current_pause = _lifecycle_pause_snapshot(runtime_dir)
    if not current_pause["confirmed"] or current_pause != plan["lifecycle_pause"]:
        raise RuntimeError(
            "Autoanswers lifecycle is not in the exact reviewed suspended/drained state"
        )
    for action in actions:
        feedback_id = str(action["feedback_id"])
        row = remote_rows.get(feedback_id)
        if row is None:
            raise RuntimeError(f"approved answered row is unavailable: {feedback_id}")
        repo.upsert_feedback(
            row,
            source_stream=SOURCE_STREAM,
            run_kind="reconciliation",
        )

    with repo.transaction() as conn:
        unconfirmed = _verify_target_resolutions(conn, actions)
        if unconfirmed:
            raise RuntimeError(
                "processed-inventory local readback is incomplete: "
                + ",".join(unconfirmed[:20])
            )
        after = _core_snapshot(conn)
        before = dict(plan["non_target_invariants"])
        expected_feedbacks = int(before["counts"]["feedbacks"]) + int(
            plan["expected_local_inserts"]
        )
        if int(after["counts"]["feedbacks"]) != expected_feedbacks:
            raise RuntimeError("answered-inventory feedback count invariant changed")
        for key in ("jobs", "publications", "wb_writes", "reservations", "costs", "failed_costs"):
            if int(after["counts"][key]) != int(before["counts"][key]):
                raise RuntimeError(f"answered-inventory non-target count changed: {key}")
        if after["settings"] != before["settings"]:
            raise RuntimeError("answered-inventory settings changed")
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_wb_autoanswers_backlog_recovery_runs
            SET state='applied',applied_at=? WHERE plan_fingerprint=?
            """,
            (iso_utc(_now()), expected_fingerprint),
        )
    return {
        "contract": CONTRACT,
        "status": "applied",
        "idempotent": False,
        "recovery_id": recovery_id,
        "manifest_sha256": manifest["manifest_sha256"],
        "plan_fingerprint": expected_fingerprint,
        "applied_count": len(actions),
        "inserted_count": int(plan["expected_local_inserts"]),
        "approval_reference": str(approval_reference).strip(),
        "provider_calls_by_runner": 0,
        "wb_posts_by_runner": 0,
    }


def _full_unanswered_ids(source: WbFeedbackReadPort) -> tuple[list[str], int]:
    captured_at = _now()
    ids: list[str] = []
    skip = 0
    while True:
        page = source.fetch_feedbacks_page(
            date_from_ts=0,
            date_to_ts=int(captured_at.timestamp()),
            is_answered=False,
            take=PAGE_SIZE,
            skip=skip,
        )
        ids.extend(str(row.get("id") or "").strip() for row in page.rows)
        if not page.has_more:
            break
        skip += page.take
    repeated: list[str] = []
    skip = 0
    while True:
        page = source.fetch_feedbacks_page(
            date_from_ts=0,
            date_to_ts=int(captured_at.timestamp()),
            is_answered=False,
            take=PAGE_SIZE,
            skip=skip,
        )
        repeated.extend(str(row.get("id") or "").strip() for row in page.rows)
        if not page.has_more:
            break
        skip += page.take
    if sorted(ids) != sorted(repeated) or len(ids) != len(set(ids)):
        raise RuntimeError("full WB unanswered inventory changed during readback")
    count = int(source.count_unanswered())
    if count != len(repeated):
        raise RuntimeError("WB unanswered count changed during readback")
    return sorted(ids), count


def reconcile_readback(
    runtime_dir: Path,
    *,
    manifest: Mapping[str, Any],
    remote: Mapping[str, Any],
    source: WbFeedbackReadPort,
) -> dict[str, Any]:
    with _open(runtime_dir, read_only=True) as conn:
        recovery = conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_wb_autoanswers_backlog_recovery_runs
            WHERE contract=? AND manifest_sha256=? ORDER BY created_at DESC LIMIT 1
            """,
            (CONTRACT, manifest["manifest_sha256"]),
        ).fetchone()
        actions: list[Mapping[str, Any]] = []
        if recovery is not None:
            evidence = json.loads(str(recovery["evidence_json"] or "{}"))
            actions = list((evidence.get("plan") or {}).get("target_actions") or [])
        target_unconfirmed = _verify_target_resolutions(conn, actions)
        local_ids = [
            str(row[0])
            for row in conn.execute(
                """
                SELECT feedback_id FROM sheet_vitrina_v1_wb_feedbacks
                WHERE COALESCE(answer_text,'')=''
                  AND COALESCE(json_extract(raw_json,'$.state'),'')<>'wbRu'
                ORDER BY feedback_id
                """
            ).fetchall()
        ]
        zero_tail = _local_zero_tail(conn)
    official_ids, count_endpoint = _full_unanswered_ids(source)
    stale_local = sorted(set(local_ids) - set(official_ids))
    missing_local = sorted(set(official_ids) - set(local_ids))
    local_official_match = not stale_local and not missing_local
    reconciled = (
        bool(remote.get("manifest_subset_confirmed"))
        and recovery is not None
        and str(recovery["state"]) == "applied"
        and not target_unconfirmed
        and local_official_match
    )
    return {
        "contract": CONTRACT,
        "status": "reconciled" if reconciled else "pending",
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest_processed_count": len(manifest["items"]),
        "target_count": len(actions),
        "target_unconfirmed_count": len(target_unconfirmed),
        "target_unconfirmed_examples": target_unconfirmed[:20],
        "official_unanswered_count": len(official_ids),
        "count_endpoint": count_endpoint,
        "local_unanswered_count": len(local_ids),
        "local_official_match": local_official_match,
        "stale_local_unanswered_count": len(stale_local),
        "stale_local_unanswered_examples": stale_local[:20],
        "missing_local_unanswered_count": len(missing_local),
        "missing_local_unanswered_examples": missing_local[:20],
        "local_zero_tail": zero_tail,
        "recovery_run": (
            {
                "recovery_id": str(recovery["recovery_id"]),
                "state": str(recovery["state"]),
                "plan_fingerprint": str(recovery["plan_fingerprint"]),
                "applied_at": recovery["applied_at"],
            }
            if recovery is not None
            else None
        ),
        "remote": dict(remote),
        "read_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("capture", "dry-run", "apply", "readback"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--expected-deployed-sha", required=True)
    manifest_group = parser.add_mutually_exclusive_group()
    manifest_group.add_argument("--manifest", type=Path)
    manifest_group.add_argument("--manifest-stdin", action="store_true")
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--actor", default="repo_owned_cli")
    args = parser.parse_args()
    if args.env_file is not None:
        _load_safe_env_file(args.env_file)
    if str(os.environ.get(EXTERNAL_GATE_ENV) or "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise RuntimeError("external IO gate is OFF")
    deployed = _deployed_runtime_evidence(str(args.expected_deployed_sha))
    source = RecoveryPacedReadPort(HttpBackedWbAutoanswersReadAdapter())
    if args.operation == "capture":
        if args.manifest is not None or args.manifest_stdin:
            parser.error("capture does not accept a manifest")
        payload = {
            "contract": CONTRACT,
            "status": "captured",
            "manifest": capture_manifest(source),
            "external_io": "official_wb_get_only",
            "deployed_runtime": deployed,
        }
    else:
        if args.manifest_stdin:
            manifest = validate_manifest(json.load(sys.stdin))
        elif args.manifest is not None:
            manifest = load_manifest(args.manifest)
        else:
            parser.error(f"{args.operation} requires --manifest or --manifest-stdin")
        remote, rows = fetch_remote_evidence(source, manifest)
        if args.operation == "dry-run":
            with _open(args.runtime_dir, read_only=True) as conn:
                payload = build_plan(
                    conn,
                    runtime_dir=args.runtime_dir,
                    manifest=manifest,
                    remote=remote,
                    deployed_runtime=deployed,
                )
        elif args.operation == "apply":
            if not args.expected_fingerprint:
                parser.error("apply requires --expected-fingerprint")
            payload = apply_plan(
                args.runtime_dir,
                manifest=manifest,
                remote=remote,
                remote_rows=rows,
                deployed_runtime=deployed,
                expected_fingerprint=str(args.expected_fingerprint),
                actor=str(args.actor),
                approval_reference=str(args.approval_reference),
            )
        else:
            payload = reconcile_readback(
                args.runtime_dir,
                manifest=manifest,
                remote=remote,
                source=source,
            )
        payload["deployed_runtime"] = deployed
        payload["external_io"] = "official_wb_get_only"
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if payload.get("coverage_confirmed", True) and payload.get("status") != "pending" else 2


if __name__ == "__main__":
    raise SystemExit(main())
