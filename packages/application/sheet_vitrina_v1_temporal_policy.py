"""Canonical temporal policy helpers for source-aware sheet_vitrina_v1 status reduction."""

from __future__ import annotations

from typing import Any, Mapping

TEMPORAL_SLOT_YESTERDAY_CLOSED = "yesterday_closed"
TEMPORAL_SLOT_TODAY_CURRENT = "today_current"
TEMPORAL_POLICY_DUAL_DAY_CAPABLE = "dual_day_capable"
TEMPORAL_POLICY_ACCEPTED_CURRENT_ROLLOVER = "accepted_current_rollover"
TEMPORAL_POLICY_YESTERDAY_CLOSED_ONLY = "yesterday_closed_only"
TEMPORAL_POLICY_DUAL_DAY_INTRADAY_TOLERANT = "dual_day_intraday_tolerant"
LEGACY_TEMPORAL_POLICY_NOT_AVAILABLE_FOR_TODAY = "not_available_for_today"

CANONICAL_SOURCE_TEMPORAL_POLICIES = {
    "seller_funnel_snapshot": TEMPORAL_POLICY_DUAL_DAY_CAPABLE,
    "sales_funnel_history": TEMPORAL_POLICY_DUAL_DAY_CAPABLE,
    "web_source_snapshot": TEMPORAL_POLICY_DUAL_DAY_CAPABLE,
    "prices_snapshot": TEMPORAL_POLICY_ACCEPTED_CURRENT_ROLLOVER,
    "sf_period": TEMPORAL_POLICY_DUAL_DAY_CAPABLE,
    "spp": TEMPORAL_POLICY_DUAL_DAY_INTRADAY_TOLERANT,
    "ads_bids": TEMPORAL_POLICY_ACCEPTED_CURRENT_ROLLOVER,
    "stocks": TEMPORAL_POLICY_YESTERDAY_CLOSED_ONLY,
    "ads_compact": TEMPORAL_POLICY_DUAL_DAY_CAPABLE,
    "fin_report_daily": TEMPORAL_POLICY_DUAL_DAY_INTRADAY_TOLERANT,
    "cost_price": TEMPORAL_POLICY_DUAL_DAY_CAPABLE,
    "promo_by_price": TEMPORAL_POLICY_DUAL_DAY_CAPABLE,
}

_INTRADAY_TOLERATED_MARKERS = (
    "429",
    "too many requests",
    "rate limit",
    "retry-after",
    "timeout",
    "timed out",
    "response not captured",
    "not captured",
    "transport failed",
    "no payload returned",
    "payload не materialized",
    "payload not materialized",
    "invalid_exact_snapshot",
    "runtime cache",
    "resolution_rule=accepted_current_preserved_after_invalid_attempt",
    "resolution_rule=exact_date_runtime_cache",
    "resolution_rule=accepted_prior_current_runtime_cache",
    "no sales rows returned",
    "empty result",
    "empty payload",
)


def normalize_temporal_policy(policy: str | None) -> str:
    normalized = str(policy or "").strip()
    if normalized == LEGACY_TEMPORAL_POLICY_NOT_AVAILABLE_FOR_TODAY:
        return TEMPORAL_POLICY_YESTERDAY_CLOSED_ONLY
    return normalized


def effective_source_temporal_policy(source_key: str, persisted_policy: str | None = None) -> str:
    normalized_source_key = str(source_key or "").strip()
    if normalized_source_key in CANONICAL_SOURCE_TEMPORAL_POLICIES:
        return CANONICAL_SOURCE_TEMPORAL_POLICIES[normalized_source_key]
    normalized_policy = normalize_temporal_policy(persisted_policy)
    return normalized_policy or str(persisted_policy or "").strip()


def effective_source_temporal_policies(
    persisted_policies: Mapping[str, str] | None,
) -> dict[str, str]:
    return {
        str(source_key): effective_source_temporal_policy(source_key, policy)
        for source_key, policy in (persisted_policies or {}).items()
    }


def source_policy_supports_slot(temporal_policy: str, temporal_slot: str) -> bool:
    normalized_policy = normalize_temporal_policy(temporal_policy)
    if normalized_policy in {
        TEMPORAL_POLICY_DUAL_DAY_CAPABLE,
        TEMPORAL_POLICY_ACCEPTED_CURRENT_ROLLOVER,
        TEMPORAL_POLICY_DUAL_DAY_INTRADAY_TOLERANT,
    }:
        return True
    if normalized_policy == TEMPORAL_POLICY_YESTERDAY_CLOSED_ONLY:
        return temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED
    if normalized_policy == TEMPORAL_SLOT_YESTERDAY_CLOSED:
        return temporal_slot == TEMPORAL_SLOT_YESTERDAY_CLOSED
    if normalized_policy == TEMPORAL_SLOT_TODAY_CURRENT:
        return temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT
    return False


def reduce_source_temporal_semantics(
    *,
    source_key: str,
    temporal_policy: str | None,
    slot_outcomes: list[Mapping[str, Any]],
) -> dict[str, Any]:
    effective_policy = effective_source_temporal_policy(source_key, temporal_policy)
    normalized_slots = [
        {
            "temporal_slot": str(
                item.get("temporal_slot")
                or item.get("slot")
                or "snapshot"
            ).strip()
            or "snapshot",
            "status": str(item.get("status") or "warning").strip() or "warning",
            "kind": str(item.get("kind") or "").strip().lower(),
            "note": str(item.get("note") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
            "requested_count": _coerce_int(item.get("requested_count")),
            "covered_count": _coerce_int(item.get("covered_count")),
        }
        for item in slot_outcomes
    ]
    slot_by_name = {item["temporal_slot"]: item for item in normalized_slots}
    yesterday_slot = slot_by_name.get(TEMPORAL_SLOT_YESTERDAY_CLOSED)
    has_confirmed_yesterday_success = bool(yesterday_slot) and yesterday_slot["status"] == "success"

    status_inputs: list[str] = []
    reason_lines: list[str] = []
    seen_slots: set[str] = set()
    for slot_name in _ordered_source_slots(effective_policy, normalized_slots):
        seen_slots.add(slot_name)
        slot = slot_by_name.get(slot_name)
        if slot is None:
            if source_policy_supports_slot(effective_policy, slot_name):
                nonblocking_reason = source_nonblocking_slot_reason(
                    source_key=source_key,
                    temporal_policy=effective_policy,
                    temporal_slot=slot_name,
                    slot_outcome={},
                    has_confirmed_yesterday_success=has_confirmed_yesterday_success,
                )
                if nonblocking_reason:
                    reason_lines.append(f"{_slot_label(slot_name)}: {nonblocking_reason}")
                else:
                    status_inputs.append("warning")
                    reason_lines.append(f"{_slot_label(slot_name)}: слот не materialized")
            continue
        if slot_counts_toward_source_status(
            source_key=source_key,
            temporal_policy=effective_policy,
            temporal_slot=slot_name,
            slot_outcome=slot,
            has_confirmed_yesterday_success=has_confirmed_yesterday_success,
        ):
            status_inputs.append(slot["status"])
            reason_lines.append(
                f"{_slot_label(slot_name)}: {slot['reason'] or _semantic_label(slot['status'])}"
            )
            continue
        nonblocking_reason = source_nonblocking_slot_reason(
            source_key=source_key,
            temporal_policy=effective_policy,
            temporal_slot=slot_name,
            slot_outcome=slot,
            has_confirmed_yesterday_success=has_confirmed_yesterday_success,
        )
        if nonblocking_reason:
            reason_lines.append(f"{_slot_label(slot_name)}: {nonblocking_reason}")
        elif slot["reason"] and slot["status"] != "success":
            reason_lines.append(f"{_slot_label(slot_name)}: {slot['reason']}")

    for slot in normalized_slots:
        slot_name = slot["temporal_slot"]
        if slot_name in seen_slots:
            continue
        status_inputs.append(slot["status"])
        reason_lines.append(
            f"{_slot_label(slot_name)}: {slot['reason'] or _semantic_label(slot['status'])}"
        )

    status = _reduce_semantic_status(status_inputs)
    if not reason_lines:
        if status == "error":
            reason = "источник завершился ошибкой"
        elif status == "warning":
            reason = "обновление не подтверждено"
        else:
            reason = "источник соответствует ожидаемой temporal model"
    else:
        reason = " · ".join(reason_lines)
    return {
        "status": status,
        "reason": reason,
        "temporal_policy": effective_policy,
    }


def slot_counts_toward_source_status(
    *,
    source_key: str,
    temporal_policy: str,
    temporal_slot: str,
    slot_outcome: Mapping[str, Any] | None,
    has_confirmed_yesterday_success: bool,
) -> bool:
    if _is_non_required_slot(temporal_policy=temporal_policy, temporal_slot=temporal_slot):
        return False
    if not _is_intraday_tolerant_slot(temporal_policy=temporal_policy, temporal_slot=temporal_slot):
        return True
    if not has_confirmed_yesterday_success:
        return True
    return not is_tolerated_intraday_current_outcome(slot_outcome)


def source_nonblocking_slot_reason(
    *,
    source_key: str,
    temporal_policy: str,
    temporal_slot: str,
    slot_outcome: Mapping[str, Any] | None,
    has_confirmed_yesterday_success: bool,
) -> str:
    if _is_non_required_slot(temporal_policy=temporal_policy, temporal_slot=temporal_slot):
        return "текущий день для этого источника не требуется"
    if (
        _is_intraday_tolerant_slot(temporal_policy=temporal_policy, temporal_slot=temporal_slot)
        and has_confirmed_yesterday_success
        and is_tolerated_intraday_current_outcome(slot_outcome)
    ):
        return "текущий день для этого источника ещё не дал финальные данные; используется подтверждённый закрытый день"
    return ""


def is_tolerated_intraday_current_outcome(slot_outcome: Mapping[str, Any] | None) -> bool:
    if not slot_outcome:
        return True
    kind = str(slot_outcome.get("kind") or "").strip().lower()
    note = str(slot_outcome.get("note") or "").strip().lower()
    requested_count = _coerce_int(slot_outcome.get("requested_count"))
    covered_count = _coerce_int(slot_outcome.get("covered_count"))
    if kind in {"missing", "not_available", "not_found", "empty"}:
        return True
    if kind == "success" and requested_count > 0 and covered_count <= 0:
        return True
    if "fin_storage_fee_total=0" in note:
        return True
    if any(marker in note for marker in _INTRADAY_TOLERATED_MARKERS):
        return True
    return False


def _ordered_source_slots(
    temporal_policy: str,
    slot_outcomes: list[Mapping[str, Any]],
) -> list[str]:
    ordered: list[str] = []
    for slot_name in (TEMPORAL_SLOT_YESTERDAY_CLOSED, TEMPORAL_SLOT_TODAY_CURRENT):
        if source_policy_supports_slot(temporal_policy, slot_name):
            ordered.append(slot_name)
    extras = sorted(
        {
            str(item.get("temporal_slot") or item.get("slot") or "snapshot").strip() or "snapshot"
            for item in slot_outcomes
        }
        - set(ordered),
        key=_slot_sort_key,
    )
    return ordered + extras


def _is_non_required_slot(*, temporal_policy: str, temporal_slot: str) -> bool:
    return (
        normalize_temporal_policy(temporal_policy) == TEMPORAL_POLICY_YESTERDAY_CLOSED_ONLY
        and temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT
    )


def _is_intraday_tolerant_slot(*, temporal_policy: str, temporal_slot: str) -> bool:
    return (
        normalize_temporal_policy(temporal_policy) == TEMPORAL_POLICY_DUAL_DAY_INTRADAY_TOLERANT
        and temporal_slot == TEMPORAL_SLOT_TODAY_CURRENT
    )


def _slot_sort_key(slot: str) -> tuple[int, str]:
    if slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
        return (0, slot)
    if slot == TEMPORAL_SLOT_TODAY_CURRENT:
        return (1, slot)
    if slot == "snapshot":
        return (2, slot)
    return (3, slot)


def _slot_label(slot: str) -> str:
    if slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
        return "вчера"
    if slot == TEMPORAL_SLOT_TODAY_CURRENT:
        return "сегодня"
    if slot == "snapshot":
        return "срез"
    return slot or "срез"


def _semantic_label(status: str) -> str:
    if status == "success":
        return "Успешно"
    if status == "error":
        return "Ошибка"
    return "Внимание"


def _reduce_semantic_status(statuses: list[str]) -> str:
    if any(status == "error" for status in statuses):
        return "error"
    if any(status == "warning" for status in statuses):
        return "warning"
    return "success"


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0
