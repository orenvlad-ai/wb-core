"""Server-owned Web Vitrina health, shadow evaluation and recovery planning."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Iterable, Mapping

from packages.business_time import current_business_date_iso
from packages.application.sheet_vitrina_v1_source_groups import (
    WEB_VITRINA_SOURCE_GROUPS,
    active_source_expectations,
)
from packages.application.sheet_vitrina_v1_archived_metrics import ARCHIVED_ONLY_SOURCE_KEYS
from packages.application.sheet_vitrina_v1_temporal_policy import (
    TEMPORAL_POLICY_ACCEPTED_CURRENT_ROLLOVER,
    TEMPORAL_POLICY_DUAL_DAY_INTRADAY_TOLERANT,
    TEMPORAL_SLOT_TODAY_CURRENT,
    TEMPORAL_SLOT_YESTERDAY_CLOSED,
    effective_source_temporal_policy,
    is_tolerated_intraday_current_outcome,
    source_policy_supports_slot,
)


HEALTH_CONTRACT = "sheet_vitrina_v1_web_health/v1"
GOOD_EXPECTATION_STATES = {"complete", "exact_zero", "inapplicable", "no_events", "accepted_fallback"}
BOT_GROUP_IDS = {"seller_portal_bot", "wb_public_card_bot"}
HISTORICAL_RECOVERY_SOURCE_KEYS = {
    "seller_funnel_snapshot",
    "sales_funnel_history",
    "web_source_snapshot",
    "sf_period",
    "stocks",
    "onec_stocks",
    "ads_compact",
    "fin_report_daily",
    "cost_price",
    "promo_by_price",
    "own_product_capital",
    "sku_action_events",
}
BOT_HISTORICAL_ROLES = {
    "seller_funnel_snapshot": "accepted_closed_day_snapshot",
    "web_source_snapshot": "accepted_closed_day_snapshot",
    "promo_by_price": "accepted_closed_day_snapshot",
    "spp_proxy": "accepted_current_snapshot",
}
BOT_CURRENT_ROLE = "accepted_current_snapshot"


def evaluate_web_vitrina_health(
    *,
    runtime: Any,
    now: datetime | None = None,
    history_days: int = 30,
) -> dict[str, Any]:
    observed_now = now or datetime.now(timezone.utc)
    today = current_business_date_iso(observed_now)
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    refresh_status = runtime.load_sheet_vitrina_refresh_status()
    outcome_by_source = {
        str(item.get("source_key") or ""): dict(item)
        for item in refresh_status.source_outcomes
        if str(item.get("source_key") or "")
    }
    active_sources = (
        set(refresh_status.source_temporal_policies) | set(outcome_by_source)
    ) - set(ARCHIVED_ONLY_SOURCE_KEYS)
    matrix: list[dict[str, Any]] = []
    for expectation in active_source_expectations(active_sources):
        source_key = expectation["source_key"]
        policy = effective_source_temporal_policy(
            source_key,
            refresh_status.source_temporal_policies.get(source_key),
        )
        outcome = outcome_by_source.get(source_key, {})
        slots = {
            str(item.get("temporal_slot") or "snapshot"): dict(item)
            for item in outcome.get("slots") or []
            if isinstance(item, Mapping)
        }
        for role, target_date in (
            (TEMPORAL_SLOT_YESTERDAY_CLOSED, yesterday),
            (TEMPORAL_SLOT_TODAY_CURRENT, today),
        ):
            cell = _expectation_cell(
                source_key=source_key,
                source_group_id=expectation["source_group_id"],
                temporal_policy=policy,
                role=role,
                target_date=target_date,
                slot=slots.get(role),
                yesterday_slot=slots.get(TEMPORAL_SLOT_YESTERDAY_CLOSED),
            )
            matrix.append(cell)

    yesterday_cells = [item for item in matrix if item["date_role"] == TEMPORAL_SLOT_YESTERDAY_CLOSED]
    today_cells = [item for item in matrix if item["date_role"] == TEMPORAL_SLOT_TODAY_CURRENT]
    seller_health = runtime.load_source_health_status("seller_portal_auth") or {}
    signals = {
        "yesterday_closed": _matrix_signal(yesterday_cells),
        "today_current": _matrix_signal(today_cells),
        "bot_health": _bot_signal(matrix=matrix, seller_health=seller_health),
    }
    temporal_rows = runtime.list_temporal_source_slot_observations(
        source_keys=sorted(BOT_HISTORICAL_ROLES),
        date_from=(
            date.fromisoformat(today) - timedelta(days=max(1, history_days) - 1)
        ).isoformat(),
        date_to=today,
    )
    bot_observations = detect_bot_backed_date_gaps(
        observations=temporal_rows,
        today=today,
        history_days=history_days,
        active_sku_count=_active_sku_count(runtime),
    )
    recovery_plan = build_recovery_preview(
        matrix=matrix,
        target_date=yesterday,
    )
    payload = {
        "contract": HEALTH_CONTRACT,
        "business_date": today,
        "yesterday_date": yesterday,
        "ready_snapshot": {
            "bundle_version": refresh_status.bundle_version,
            "as_of_date": refresh_status.as_of_date,
            "snapshot_id": refresh_status.snapshot_id,
            "refreshed_at": refresh_status.refreshed_at,
        },
        "expectation_matrix": matrix,
        "signals": signals,
        "bot_date_observations": bot_observations,
        "recovery_preview": recovery_plan,
    }
    payload["fingerprint"] = _fingerprint(payload)
    return payload


def persist_web_vitrina_health_evaluation(
    *,
    runtime: Any,
    evaluation: Mapping[str, Any],
    phase: str,
    observed_at: str,
) -> dict[str, Any]:
    fingerprint = str(evaluation.get("fingerprint") or "")
    if not fingerprint.startswith("sha256:"):
        raise ValueError("Web Vitrina health evaluation fingerprint is required")
    business_date = str(evaluation.get("business_date") or "")
    identity = _fingerprint(
        {
            "business_date": business_date,
            "phase": str(phase or "shadow"),
            "fingerprint": fingerprint,
        }
    ).removeprefix("sha256:")
    return runtime.save_sheet_vitrina_health_observation(
        observation_id=f"health-{identity}",
        business_date=business_date,
        phase=str(phase or "shadow"),
        observed_at=observed_at,
        ready_snapshot_id=str((evaluation.get("ready_snapshot") or {}).get("snapshot_id") or ""),
        payload_fingerprint=fingerprint,
        payload=dict(evaluation),
    )


def detect_bot_backed_date_gaps(
    *,
    observations: Iterable[Mapping[str, Any]],
    today: str,
    history_days: int,
    active_sku_count: int,
) -> dict[str, Any]:
    end = date.fromisoformat(today)
    start = end - timedelta(days=max(1, history_days) - 1)
    indexed = {
        (
            str(item.get("source_key") or ""),
            str(item.get("snapshot_date") or ""),
            str(item.get("snapshot_role") or ""),
        ): item
        for item in observations
    }
    gaps: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        day = cursor.isoformat()
        for source_key, historical_role in BOT_HISTORICAL_ROLES.items():
            role = BOT_CURRENT_ROLE if day == today else historical_role
            row = indexed.get((source_key, day, role))
            state, reason = _observation_state(
                row,
                active_sku_count=active_sku_count,
            )
            if state not in {"complete", "exact_zero"}:
                gaps.append(
                    {
                        "source_key": source_key,
                        "source_group_id": _group_for_source(source_key),
                        "date": day,
                        "snapshot_role": role,
                        "state": state,
                        "reason": reason,
                    }
                )
        cursor += timedelta(days=1)
    return {
        "date_from": start.isoformat(),
        "date_to": today,
        "history_days": max(1, history_days),
        "gap_count": len(gaps),
        "skipped_count": sum(1 for item in gaps if item["state"] == "skipped"),
        "incomplete_count": sum(1 for item in gaps if item["state"] == "incomplete"),
        "gaps": gaps,
    }


def build_recovery_preview(
    *,
    matrix: Iterable[Mapping[str, Any]],
    target_date: str,
) -> dict[str, Any]:
    gaps = [
        dict(item)
        for item in matrix
        if str(item.get("date_role") or "") == TEMPORAL_SLOT_YESTERDAY_CLOSED
        and str(item.get("target_date") or "") == target_date
        and str(item.get("expectation_state") or "") not in GOOD_EXPECTATION_STATES
    ]
    by_group: dict[str, list[dict[str, Any]]] = {}
    for gap in gaps:
        by_group.setdefault(str(gap.get("source_group_id") or "unclassified"), []).append(gap)
    actions: list[dict[str, Any]] = []
    for group_id, group_gaps in sorted(by_group.items()):
        source_keys = sorted({str(item.get("source_key") or "") for item in group_gaps})
        recoverable = sorted(set(source_keys) & HISTORICAL_RECOVERY_SOURCE_KEYS)
        hook = "group_refresh" if recoverable and group_id in WEB_VITRINA_SOURCE_GROUPS else "none"
        action = {
            "source_group_id": group_id,
            "target_date": target_date,
            "gap_source_keys": source_keys,
            "recoverable_source_keys": recoverable,
            "hook": hook,
            "apply_allowed": hook == "group_refresh",
            "reason": (
                "existing single-flight group refresh writer can retry historical-capable sources"
                if hook == "group_refresh"
                else "no exact historical retry hook; preview only"
            ),
        }
        action["action_fingerprint"] = _fingerprint(action)
        actions.append(action)
    preview = {
        "target_date": target_date,
        "status": "recovery_needed" if gaps else "closed",
        "gap_count": len(gaps),
        "actions": actions,
    }
    preview["plan_fingerprint"] = _fingerprint(preview)
    return preview


def _expectation_cell(
    *,
    source_key: str,
    source_group_id: str,
    temporal_policy: str,
    role: str,
    target_date: str,
    slot: Mapping[str, Any] | None,
    yesterday_slot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not source_policy_supports_slot(temporal_policy, role):
        state, reason = "inapplicable", "temporal policy does not require this date role"
    elif slot is None:
        state, reason = "missing", "persisted STATUS has no source/date-role outcome"
    else:
        kind = str(slot.get("kind") or "").strip().lower()
        note = str(slot.get("note") or "").strip()
        requested = _coerce_int(slot.get("requested_count"))
        covered = _coerce_int(slot.get("covered_count"))
        exact_identity = target_date in {
            str(slot.get(key) or "").strip()
            for key in ("freshness", "snapshot_date", "date", "date_from", "date_to")
        }
        if (
            source_key == "sku_action_events"
            and kind == "success"
            and (
                "empty_semantics=no_confirmed_event" in note
                or "missing rows mean no confirmed change" in note.lower()
            )
        ):
            state, reason = "no_events", "scope evaluated; no confirmed operator changes"
        elif temporal_policy == TEMPORAL_POLICY_DUAL_DAY_INTRADAY_TOLERANT and _accepted_fallback(
            temporal_policy=temporal_policy,
            role=role,
            slot=slot,
            yesterday_slot=yesterday_slot,
        ):
            state, reason = "accepted_fallback", str(slot.get("reason") or note or "accepted fallback")
        elif kind in {"error", "blocked", "closure_exhausted"}:
            state, reason = "failure", str(slot.get("reason") or note or kind)
        elif kind in {"missing", "not_found", "not_available"}:
            state, reason = "missing", str(slot.get("reason") or note or kind)
        elif kind in {"incomplete", "closure_pending", "closure_retrying", "closure_rate_limited"}:
            state, reason = "partial", str(slot.get("reason") or note or kind)
        elif requested > 0 and covered < requested:
            state, reason = "partial", f"covered {covered} of {requested}"
        elif kind == "success" and requested == 0 and covered == 0:
            if _note_confirms_exact_zero(note):
                state, reason = "exact_zero", "source confirmed an exact zero"
            else:
                state, reason = "missing", "zero-count STATUS row has no explicit exact-zero evidence"
        elif _accepted_fallback(
            temporal_policy=temporal_policy,
            role=role,
            slot=slot,
            yesterday_slot=yesterday_slot,
        ):
            state, reason = "accepted_fallback", str(slot.get("reason") or note or "accepted fallback")
        elif kind == "success" and exact_identity:
            state, reason = "complete", "exact date identity and required coverage confirmed"
        elif kind == "success":
            state, reason = "missing", "success row has no exact target-date identity or accepted fallback"
        else:
            state, reason = "failure", str(slot.get("reason") or note or kind or "unknown outcome")
    return {
        "source_group_id": source_group_id,
        "source_key": source_key,
        "date_role": role,
        "target_date": target_date,
        "temporal_policy": temporal_policy,
        "expectation_state": state,
        "reason": reason,
        "kind": str((slot or {}).get("kind") or ""),
        "requested_count": _coerce_int((slot or {}).get("requested_count")),
        "covered_count": _coerce_int((slot or {}).get("covered_count")),
    }


def _accepted_fallback(
    *,
    temporal_policy: str,
    role: str,
    slot: Mapping[str, Any],
    yesterday_slot: Mapping[str, Any] | None,
) -> bool:
    if temporal_policy == TEMPORAL_POLICY_DUAL_DAY_INTRADAY_TOLERANT and role == TEMPORAL_SLOT_TODAY_CURRENT:
        yesterday_ok = str((yesterday_slot or {}).get("status") or "") == "success"
        return yesterday_ok and is_tolerated_intraday_current_outcome(slot)
    if temporal_policy != TEMPORAL_POLICY_ACCEPTED_CURRENT_ROLLOVER:
        return False
    if role != TEMPORAL_SLOT_YESTERDAY_CLOSED:
        return False
    text = " ".join(
        str(slot.get(key) or "").lower()
        for key in ("note", "reason")
    )
    return any(marker in text for marker in ("accepted", "preserved", "rollover", "runtime cache"))


def _matrix_signal(cells: list[Mapping[str, Any]]) -> dict[str, Any]:
    problems = [dict(item) for item in cells if str(item.get("expectation_state") or "") not in GOOD_EXPECTATION_STATES]
    state = "ok" if not problems else (
        "error" if any(item.get("expectation_state") == "failure" for item in problems) else "incomplete"
    )
    return {
        "state": state,
        "problem_count": len(problems),
        "problem_sources": sorted({str(item.get("source_key") or "") for item in problems}),
    }


def _bot_signal(*, matrix: list[Mapping[str, Any]], seller_health: Mapping[str, Any]) -> dict[str, Any]:
    current_bot_cells = [
        item for item in matrix
        if item.get("source_group_id") in BOT_GROUP_IDS
        and item.get("date_role") == TEMPORAL_SLOT_TODAY_CURRENT
    ]
    confirmed: list[str] = []
    for cell in current_bot_cells:
        state = str(cell.get("expectation_state") or "")
        if state in {"failure", "partial", "missing"}:
            confirmed.append(f"{cell.get('source_key')}:{state}")
    session_status = str(seller_health.get("session_status") or "").strip().lower()
    if any(
        marker in session_status
        for marker in ("expired", "invalid", "wrong_supplier", "route_unavailable", "collector_unavailable")
    ):
        confirmed.append(f"seller_portal_session:{session_status}")
    return {
        "state": "error" if confirmed else "ok",
        "confirmed_problem_count": len(confirmed),
        "confirmed_problems": confirmed,
        "seller_session_status": session_status or "unknown",
        "generic_auth_is_nonblocking": True,
        "lawful_empty_is_nonblocking": True,
    }


def _observation_state(
    row: Mapping[str, Any] | None,
    *,
    active_sku_count: int,
) -> tuple[str, str]:
    if row is None:
        return "skipped", "durable source/date/role observation is absent"
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return "skipped", "durable observation payload is not an object"
    kind = str(payload.get("kind") or "").lower()
    requested = _coerce_int(payload.get("requested_count")) or active_sku_count
    covered = _coerce_int(payload.get("covered_count"))
    if not covered:
        items = payload.get("items")
        if isinstance(items, (list, dict)):
            covered = len(items)
        elif payload.get("count") is not None:
            covered = _coerce_int(payload.get("count"))
    if kind in {"error", "missing", "not_found", "blocked"}:
        return "skipped", f"payload kind={kind}"
    if kind == "incomplete" or (requested > 0 and covered < requested):
        return "incomplete", f"covered {covered} of {requested}"
    if kind == "success" and requested == 0 and covered == 0:
        return "exact_zero", "source explicitly evaluated an empty active scope"
    if kind == "success" and (requested == 0 or covered >= requested):
        return "complete", f"covered {covered} of {requested}"
    return "skipped", f"unaccepted payload kind={kind or 'unknown'}"


def _active_sku_count(runtime: Any) -> int:
    try:
        return sum(1 for item in runtime.load_current_state().config_v2 if item.enabled)
    except (AttributeError, ValueError):
        return 0


def _group_for_source(source_key: str) -> str:
    for group_id, group in WEB_VITRINA_SOURCE_GROUPS.items():
        if source_key in group["source_keys"]:
            return group_id
    return "unclassified"


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _note_confirms_exact_zero(note: str) -> bool:
    normalized = str(note or "").lower()
    return any(
        marker in normalized
        for marker in (
            "exact_zero",
            "exact zero",
            "confirmed zero",
            "no sales rows returned",
            "empty result confirmed",
            "row_count=0",
        )
    )


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
