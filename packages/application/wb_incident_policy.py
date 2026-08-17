"""Seller-level WB warehouse incident policy and immutable stock projections."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime, timezone
import hashlib
import json
import os
from typing import Any, Mapping, Sequence

from packages.application.stocks_block import (
    REGION_TO_FIELD,
    build_wb_warehouse_exclusion,
    parse_excluded_wb_warehouse_ids,
)
from packages.business_time import current_business_date_iso
from packages.contracts.stocks_block import StocksItem, StocksWarehouseRow
from packages.contracts.wb_supply_planning_zones import (
    SUPPLY_PLANNING_ZONE_TO_STOCK_FIELD,
)


POLICY_CONTRACT_NAME = "wb_warehouse_incident_policy"
POLICY_CONTRACT_VERSION = 2
LEGACY_CONFIG_KEY = "wb_warehouse_exclusions"
POLICY_STATUS_VALUES = {"active", "monitoring", "resolved", "disabled"}
VITRINA_PROVISIONAL_QUALITY_MESSAGE_RU = (
    "Рассчитано по полученному снимку, полнота WB не подтверждена"
)


class WbIncidentPolicyError(ValueError):
    """A controlled policy validation or projection error."""


def canonical_seller_id() -> str:
    return str(os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID") or "canonical").strip()


def _iso_date(value: Any, *, field_name: str, required: bool = True) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        if required:
            raise WbIncidentPolicyError(f"{field_name} is required")
        return ""
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise WbIncidentPolicyError(f"{field_name} must be YYYY-MM-DD") from exc


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (1, "1", "true", "True", "yes", "on"):
        return True
    if value in (0, "0", "false", "False", "no", "off", None, ""):
        return False
    raise WbIncidentPolicyError("active must be boolean")


def _normalize_name(value: Any) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def _latest_policy(runtime: Any, *, seller_id: str) -> dict[str, Any] | None:
    record = runtime.load_latest_wb_incident_policy(seller_id=seller_id)
    return dict(record) if isinstance(record, Mapping) and record.get("status") == "ok" else None


def _policy_entries(policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return v2 per-warehouse intervals, projecting v1 rows without mutation."""

    raw_entries = list(policy.get("warehouse_entries") or [])
    if not raw_entries:
        identities = {
            int(item.get("warehouse_id") or 0): str(item.get("warehouse_name") or "").strip()
            for item in policy.get("warehouse_identities") or []
        }
        raw_entries = [
            {
                "warehouse_id": int(item),
                "warehouse_name": identities.get(int(item), ""),
                "effective_from": str(policy.get("effective_from") or ""),
                "effective_to_exclusive": "",
                "source": "v1_projection",
            }
            for item in policy.get("warehouse_ids") or []
        ]
    entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        warehouse_id = int(raw.get("warehouse_id") or 0)
        if warehouse_id <= 0:
            continue
        effective_from = _iso_date(
            raw.get("effective_from") or policy.get("effective_from"),
            field_name=f"warehouse_entries[{warehouse_id}].effective_from",
        )
        effective_to_exclusive = _iso_date(
            raw.get("effective_to_exclusive"),
            field_name=f"warehouse_entries[{warehouse_id}].effective_to_exclusive",
            required=False,
        )
        if effective_to_exclusive and effective_to_exclusive <= effective_from:
            raise WbIncidentPolicyError(
                f"warehouseId {warehouse_id} effective_to_exclusive must be later than effective_from"
            )
        entries.append(
            {
                "warehouse_id": warehouse_id,
                "warehouse_name": str(raw.get("warehouse_name") or "").strip(),
                "effective_from": effective_from,
                "effective_to_exclusive": effective_to_exclusive,
                "source": str(raw.get("source") or "incident_policy_v2"),
            }
        )
    ordered = sorted(
        entries,
        key=lambda item: (
            int(item["warehouse_id"]),
            str(item["effective_from"]),
            str(item["effective_to_exclusive"]),
        ),
    )
    previous_by_id: dict[int, dict[str, Any]] = {}
    for entry in ordered:
        warehouse_id = int(entry["warehouse_id"])
        previous = previous_by_id.get(warehouse_id)
        if previous is not None:
            previous_end = str(previous.get("effective_to_exclusive") or "")
            if not previous_end or str(entry["effective_from"]) < previous_end:
                raise WbIncidentPolicyError(
                    f"warehouseId {warehouse_id} has overlapping incident intervals"
                )
        previous_by_id[warehouse_id] = entry
    return ordered


def _entry_owns_date(entry: Mapping[str, Any], target_date: str) -> bool:
    return (
        str(entry.get("effective_from") or "") <= target_date
        and (
            not str(entry.get("effective_to_exclusive") or "")
            or target_date < str(entry.get("effective_to_exclusive") or "")
        )
    )


def _resolve_policy_record(record: Mapping[str, Any], *, target_date: str) -> dict[str, Any]:
    result = dict(record)
    entries = _policy_entries(result)
    effective_entries = [item for item in entries if _entry_owns_date(item, target_date)]
    overall_end = str(result.get("effective_to") or "")
    configured_active = bool(result.get("active")) and str(result.get("policy_status") or "") in {
        "active",
        "monitoring",
    }
    active = configured_active and (not overall_end or target_date <= overall_end)
    result.update(
        {
            "warehouse_entries": entries,
            "effective_warehouse_entries": effective_entries,
            "warehouse_ids": [int(item["warehouse_id"]) for item in effective_entries],
            "warehouse_identities": [
                {
                    "warehouse_id": int(item["warehouse_id"]),
                    "warehouse_name": str(item.get("warehouse_name") or ""),
                }
                for item in effective_entries
            ],
            "active": bool(active and effective_entries),
        }
    )
    return result


def _legacy_policy(runtime: Any, *, seller_id: str, snapshot_date: str) -> dict[str, Any] | None:
    records = runtime.list_sheet_vitrina_user_configs(config_key=LEGACY_CONFIG_KEY)
    payloads: list[dict[str, Any]] = []
    identities: set[tuple[int, ...]] = set()
    for record in records:
        config = dict(record.get("config") or {})
        try:
            warehouse_ids = parse_excluded_wb_warehouse_ids(
                {"excluded_wb_warehouse_ids": config.get("excluded_wb_warehouse_ids", [])}
            )
        except ValueError:
            continue
        if warehouse_ids:
            identities.add(warehouse_ids)
        payloads.append(
            {
                "user_key": str(record.get("user_key") or ""),
                "revision": int(record.get("revision") or 0),
                "updated_at": str(record.get("updated_at") or ""),
                "excluded_wb_warehouse_ids": list(warehouse_ids),
            }
        )
    if not payloads:
        return None
    conflicting = len(identities) > 1
    selected = next(iter(identities), ())
    return {
        "status": "ok",
        "contract_name": POLICY_CONTRACT_NAME,
        "contract_version": POLICY_CONTRACT_VERSION,
        "seller_id": seller_id,
        "revision": 0,
        "active": bool(selected)
        and not conflicting
        and snapshot_date == current_business_date_iso(),
        "warehouse_ids": list(selected) if not conflicting else [],
        "warehouse_identities": [],
        "warehouse_entries": [
            {
                "warehouse_id": int(warehouse_id),
                "warehouse_name": "",
                "effective_from": snapshot_date,
                "effective_to_exclusive": "",
                "source": "legacy_user_config",
            }
            for warehouse_id in selected
            if int(warehouse_id) > 0
        ] if not conflicting else [],
        "reason": "Совместимая настройка исключения складов до миграции",
        "effective_from": snapshot_date,
        "effective_to": "",
        "policy_status": "active" if selected and not conflicting else "disabled",
        "actor": "",
        "created_at": "",
        "source": "legacy_user_config",
        "migration_pending": True,
        "legacy_conflict": conflicting,
        "legacy_payloads": payloads,
    }


def get_policy_state(
    runtime: Any,
    *,
    snapshot_date: str,
    seller_id: str | None = None,
    include_legacy: bool = True,
) -> dict[str, Any]:
    """Resolve the revision whose effective interval owns an exact snapshot date."""

    target_date = _iso_date(snapshot_date, field_name="snapshot_date")
    owner = seller_id or canonical_seller_id()
    record = runtime.load_wb_incident_policy_for_date(
        seller_id=owner,
        snapshot_date=target_date,
    )
    if not isinstance(record, Mapping) or record.get("status") != "ok":
        legacy = _legacy_policy(runtime, seller_id=owner, snapshot_date=target_date) if include_legacy else None
        if legacy is not None:
            legacy["materialize_incident_metrics"] = (
                bool(legacy.get("active"))
                or target_date == current_business_date_iso()
            )
            return legacy
        started = runtime.load_wb_incident_policy_started_by_date(
            seller_id=owner,
            snapshot_date=target_date,
        )
        latest = dict(started) if isinstance(started, Mapping) and started.get("status") == "ok" else _latest_policy(runtime, seller_id=owner)
        materialize = (
            isinstance(started, Mapping)
            and started.get("status") == "ok"
        ) or target_date == current_business_date_iso()
        return {
            "status": "ok",
            "contract_name": POLICY_CONTRACT_NAME,
            "contract_version": POLICY_CONTRACT_VERSION,
            "seller_id": owner,
            "revision": int((latest or {}).get("revision") or 0),
            "active": False,
            "warehouse_ids": list((latest or {}).get("warehouse_ids") or []),
            "warehouse_identities": list((latest or {}).get("warehouse_identities") or []),
            "reason": str((latest or {}).get("reason") or ""),
            "effective_from": str((latest or {}).get("effective_from") or ""),
            "effective_to": str((latest or {}).get("effective_to") or ""),
            "policy_status": str((latest or {}).get("policy_status") or "disabled"),
            "actor": str((latest or {}).get("actor") or ""),
            "created_at": str((latest or {}).get("created_at") or ""),
            "source": str((latest or {}).get("source") or "incident_policy"),
            "migration_pending": False,
            "legacy_conflict": False,
            "legacy_payloads": list((latest or {}).get("legacy_payloads") or []),
            "materialize_incident_metrics": materialize,
        }
    result = _resolve_policy_record(record, target_date=target_date)
    result.update(
        {
            "contract_name": POLICY_CONTRACT_NAME,
            "contract_version": POLICY_CONTRACT_VERSION,
            "active": bool(result.get("active")),
            "migration_pending": False,
            "legacy_conflict": False,
            "materialize_incident_metrics": True,
        }
    )
    return result


def get_latest_policy_state(
    runtime: Any,
    *,
    snapshot_date: str,
    seller_id: str | None = None,
) -> dict[str, Any]:
    """Return editable latest state while preserving exact-date resolution."""

    owner = seller_id or canonical_seller_id()
    latest = _latest_policy(runtime, seller_id=owner)
    if latest is None:
        return get_policy_state(
            runtime,
            snapshot_date=snapshot_date,
            seller_id=owner,
            include_legacy=True,
        )
    current = get_policy_state(
        runtime,
        snapshot_date=snapshot_date,
        seller_id=owner,
        include_legacy=False,
    )
    result = dict(latest)
    all_entries = _policy_entries(result)
    configured_entries = [
        item for item in all_entries
        if not str(item.get("effective_to_exclusive") or "")
    ]
    configured_active = bool(latest.get("active"))
    currently_effective = bool(current.get("active"))
    effective_warehouse_ids = (
        list(current.get("warehouse_ids") or [])
        if currently_effective
        else []
    )
    effective_warehouse_identities = (
        list(current.get("warehouse_identities") or [])
        if currently_effective
        else []
    )
    effective_warehouse_entries = (
        list(current.get("effective_warehouse_entries") or [])
        if currently_effective
        else []
    )
    result.update(
        {
            "contract_name": POLICY_CONTRACT_NAME,
            "contract_version": POLICY_CONTRACT_VERSION,
            "configured_active": configured_active,
            "configured_warehouse_entries": configured_entries,
            "legacy_warehouse_entries": all_entries,
            "warehouse_entries": configured_entries,
            "warehouse_ids": [int(item["warehouse_id"]) for item in configured_entries],
            "warehouse_identities": [
                {
                    "warehouse_id": int(item["warehouse_id"]),
                    "warehouse_name": str(item.get("warehouse_name") or ""),
                }
                for item in configured_entries
            ],
            "active": currently_effective,
            "currently_effective": currently_effective,
            "effective_revision": int(current.get("revision") or 0),
            "effective_warehouse_ids": effective_warehouse_ids,
            "effective_warehouse_identities": effective_warehouse_identities,
            "effective_warehouse_entries": effective_warehouse_entries,
            "effective_reason": str(current.get("reason") or ""),
            "effective_effective_from": str(current.get("effective_from") or ""),
            "effective_effective_to": str(current.get("effective_to") or ""),
            "materialize_incident_metrics": bool(current.get("materialize_incident_metrics")),
            "migration_pending": False,
            "legacy_conflict": False,
        }
    )
    return result


def save_policy_revision(
    runtime: Any,
    *,
    payload: Mapping[str, Any],
    actor: str,
    warehouse_options: Sequence[Mapping[str, Any]],
    timestamp: str | None = None,
    seller_id: str | None = None,
) -> dict[str, Any]:
    owner = seller_id or canonical_seller_id()
    active = _bool(payload.get("active"))
    reason = str(payload.get("reason") or "").strip()
    raw_requested_entries = payload.get("warehouse_entries")
    legacy_payload_shape = raw_requested_entries is None
    change_date = _iso_date(
        payload.get("change_effective_from")
        or (payload.get("effective_from") if legacy_payload_shape else "")
        or current_business_date_iso(),
        field_name="change_effective_from",
    )
    effective_to = _iso_date(payload.get("effective_to"), field_name="effective_to", required=False)
    policy_status = str(payload.get("status") or ("active" if active else "disabled")).strip().lower()
    if policy_status not in POLICY_STATUS_VALUES:
        raise WbIncidentPolicyError("status must be active, monitoring, resolved, or disabled")

    option_by_id = {
        int(option["warehouse_id"]): dict(option)
        for option in warehouse_options
        if option.get("warehouse_id") is not None
    }
    if raw_requested_entries is None:
        try:
            legacy_ids = parse_excluded_wb_warehouse_ids(
                {"excluded_wb_warehouse_ids": payload.get("excluded_wb_warehouse_ids", [])},
                allow_legacy_elektrostal=False,
            )
        except ValueError as exc:
            raise WbIncidentPolicyError(str(exc)) from exc
        legacy_from = _iso_date(payload.get("effective_from"), field_name="effective_from")
        requested_entries = [
            {"warehouse_id": int(warehouse_id), "effective_from": legacy_from}
            for warehouse_id in legacy_ids
        ]
    elif not isinstance(raw_requested_entries, Sequence) or isinstance(raw_requested_entries, (str, bytes)):
        raise WbIncidentPolicyError("warehouse_entries must be a list")
    else:
        requested_entries = [dict(item) for item in raw_requested_entries if isinstance(item, Mapping)]
        if len(requested_entries) != len(raw_requested_entries):
            raise WbIncidentPolicyError("every warehouse_entries item must be an object")

    requested_by_id: dict[int, dict[str, Any]] = {}
    normalized_names: dict[str, int] = {}
    for requested in requested_entries:
        warehouse_id = int(requested.get("warehouse_id") or 0)
        if warehouse_id <= 0:
            raise WbIncidentPolicyError(
                "warehouseId 0 is a service bucket and cannot be an incident-policy destination"
            )
        if warehouse_id in requested_by_id:
            raise WbIncidentPolicyError(f"warehouseId {warehouse_id} is duplicated")
        warehouse_effective_from = _iso_date(
            requested.get("effective_from"),
            field_name=f"warehouse_entries[{warehouse_id}].effective_from",
        )
        option = option_by_id.get(warehouse_id)
        if option is None or bool(option.get("temporarily_missing")):
            raise WbIncidentPolicyError(
                f"warehouseId {warehouse_id} has no exact identity in the complete current snapshot"
            )
        warehouse_name = str(option.get("warehouse_name") or "").strip()
        normalized_name = _normalize_name(warehouse_name)
        if not normalized_name:
            raise WbIncidentPolicyError(f"warehouseId {warehouse_id} has an empty warehouse name")
        other_id = normalized_names.get(normalized_name)
        if other_id is not None and other_id != warehouse_id:
            raise WbIncidentPolicyError(
                f"warehouse name {warehouse_name!r} is ambiguous between IDs {other_id} and {warehouse_id}"
            )
        normalized_names[normalized_name] = warehouse_id
        requested_by_id[warehouse_id] = {
            "warehouse_id": warehouse_id,
            "warehouse_name": warehouse_name,
            "effective_from": warehouse_effective_from,
            "effective_to_exclusive": "",
            "source": "incident_policy_v2",
        }

    warehouse_ids = sorted(requested_by_id)
    if active and not warehouse_ids:
        raise WbIncidentPolicyError("active policy must select at least one warehouse")
    if active and not reason:
        raise WbIncidentPolicyError("active policy requires a reason")

    latest = _latest_policy(runtime, seller_id=owner)
    base_revision = payload.get("base_revision")
    if base_revision is not None and int(base_revision) != int((latest or {}).get("revision") or 0):
        raise WbIncidentPolicyError("WB incident policy revision conflict")
    existing_entries = _policy_entries(latest or {}) if latest else []
    closed_entries = [
        dict(item) for item in existing_entries
        if str(item.get("effective_to_exclusive") or "")
    ]
    existing_open = {
        int(item["warehouse_id"]): dict(item)
        for item in existing_entries
        if not str(item.get("effective_to_exclusive") or "")
    }
    changed_dates: list[str] = []
    configured_entries: list[dict[str, Any]] = []
    for warehouse_id, requested in sorted(requested_by_id.items()):
        previous = existing_open.get(warehouse_id)
        if previous is not None:
            if legacy_payload_shape:
                requested["effective_from"] = str(previous.get("effective_from") or "")
            if str(previous.get("effective_from") or "") != str(requested["effective_from"]):
                raise WbIncidentPolicyError(
                    f"warehouseId {warehouse_id} already starts at {previous.get('effective_from')}; "
                    "an existing start date cannot be rewritten retroactively"
                )
            requested["source"] = str(previous.get("source") or "incident_policy_v2")
        else:
            prior_closed_dates = [
                str(item.get("effective_to_exclusive") or "")
                for item in closed_entries
                if int(item.get("warehouse_id") or 0) == warehouse_id
                and str(item.get("effective_to_exclusive") or "")
            ]
            if prior_closed_dates and str(requested["effective_from"]) < max(prior_closed_dates):
                raise WbIncidentPolicyError(
                    f"warehouseId {warehouse_id} cannot be re-selected before its prior interval closed"
                )
            changed_dates.append(str(requested["effective_from"]))
        configured_entries.append(requested)
    for warehouse_id, previous in sorted(existing_open.items()):
        if warehouse_id in requested_by_id:
            continue
        if change_date > str(previous.get("effective_from") or ""):
            closed = dict(previous)
            closed["effective_to_exclusive"] = change_date
            closed_entries.append(closed)
        changed_dates.append(change_date)

    identities = [
        {
            "warehouse_id": int(item["warehouse_id"]),
            "warehouse_name": str(item.get("warehouse_name") or ""),
            "effective_from": str(item["effective_from"]),
        }
        for item in configured_entries
    ]
    canonical_entries = sorted(
        [*closed_entries, *configured_entries],
        key=lambda item: (
            int(item["warehouse_id"]),
            str(item["effective_from"]),
            str(item.get("effective_to_exclusive") or ""),
        ),
    )
    latest_signature = {
        "active": bool((latest or {}).get("active")),
        "entries": [
            (int(item["warehouse_id"]), str(item["effective_from"]))
            for item in existing_entries
            if not str(item.get("effective_to_exclusive") or "")
        ],
        "reason": str((latest or {}).get("reason") or ""),
        "effective_to": str((latest or {}).get("effective_to") or ""),
        "status": str((latest or {}).get("policy_status") or ""),
    }
    requested_signature = {
        "active": active,
        "entries": [(int(item["warehouse_id"]), str(item["effective_from"])) for item in configured_entries],
        "reason": reason,
        "effective_to": effective_to,
        "status": policy_status,
    }
    if latest is not None and latest_signature == requested_signature:
        result = get_latest_policy_state(
            runtime,
            snapshot_date=change_date,
            seller_id=owner,
        )
        result["idempotency_status"] = "T0"
        result["changed_from"] = ""
        return result

    if not changed_dates:
        changed_dates.append(change_date)
    revision_effective_from = min(changed_dates)
    if effective_to and effective_to < revision_effective_from:
        raise WbIncidentPolicyError("effective_to cannot be earlier than the changed interval")
    legacy_payloads = (
        _legacy_policy(runtime, seller_id=owner, snapshot_date=revision_effective_from) or {}
    ).get("legacy_payloads") or []
    created_at = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    saved = runtime.append_wb_incident_policy_revision(
        seller_id=owner,
        active=active,
        warehouse_ids=warehouse_ids,
        warehouse_identities=identities,
        warehouse_entries=canonical_entries,
        reason=reason,
        effective_from=revision_effective_from,
        effective_to=effective_to,
        policy_status=policy_status,
        actor=str(actor or "").strip(),
        created_at=created_at,
        source="incident_policy_v2",
        legacy_payloads=legacy_payloads,
        expected_revision=int(base_revision) if base_revision is not None else None,
    )
    if saved.get("status") == "conflict":
        raise WbIncidentPolicyError("WB incident policy revision conflict")
    result = get_latest_policy_state(
        runtime,
        snapshot_date=change_date,
        seller_id=owner,
    )
    result["idempotency_status"] = "applied"
    result["changed_from"] = revision_effective_from
    return result


def _historical_rows_with_exact_identity(
    rows: Sequence[StocksWarehouseRow],
    *,
    policy: Mapping[str, Any],
) -> list[StocksWarehouseRow]:
    identities = list(policy.get("warehouse_identities") or [])
    by_name: dict[str, int] = {}
    identity_ids: set[int] = set()
    for identity in identities:
        name = _normalize_name(identity.get("warehouse_name"))
        warehouse_id = int(identity.get("warehouse_id") or 0)
        if not name:
            raise WbIncidentPolicyError("active policy contains an empty historical warehouse identity")
        if name in by_name and by_name[name] != warehouse_id:
            raise WbIncidentPolicyError("active policy contains an ambiguous historical warehouse identity")
        by_name[name] = warehouse_id
        identity_ids.add(warehouse_id)
        if warehouse_id == 0 and name == _normalize_name(
            "Остальные — служебная группа WB"
        ):
            # Historical CSV uses the exact official OfficeName while current
            # WB warehouse options render the same canonical ID with an
            # explanatory suffix.
            by_name[_normalize_name("Остальные")] = 0
    if (
        policy.get("active")
        and policy.get("warehouse_ids")
        and identity_ids
        != {int(item) for item in policy.get("warehouse_ids") or []}
    ):
        raise WbIncidentPolicyError("historical incident projection has no exact identity for every selected warehouse")
    result: list[StocksWarehouseRow] = []
    for row in rows:
        mapped_id = by_name.get(_normalize_name(row.warehouse_name))
        if row.warehouse_id is not None or mapped_id is None:
            result.append(row)
            continue
        if is_dataclass(row):
            result.append(replace(row, warehouse_id=mapped_id))
            continue
        result.append(
            StocksWarehouseRow(
                nm_id=int(getattr(row, "nm_id")),
                warehouse_id=mapped_id,
                warehouse_name=str(getattr(row, "warehouse_name", "") or ""),
                region_name=str(getattr(row, "region_name", "") or ""),
                quantity=float(getattr(row, "quantity", 0.0) or 0.0),
                planning_zone_key=(
                    str(getattr(row, "planning_zone_key"))
                    if getattr(row, "planning_zone_key", None) is not None
                    else None
                ),
                classification_status=str(
                    getattr(row, "classification_status", "") or ""
                ),
                classification_source=str(
                    getattr(row, "classification_source", "") or ""
                ),
                in_way_to_client=float(
                    getattr(row, "in_way_to_client", 0.0) or 0.0
                ),
                in_way_from_client=float(
                    getattr(row, "in_way_from_client", 0.0) or 0.0
                ),
                exclusion_codes=tuple(
                    getattr(row, "exclusion_codes", ()) or ()
                ),
            )
        )
    return result


def _projection_cache_policy_revision(policy: Mapping[str, Any]) -> int:
    policy_revision = int(policy.get("revision") or 0)
    if not policy.get("migration_pending"):
        return policy_revision
    legacy_identity = json.dumps(
        {
            "warehouse_ids": list(policy.get("warehouse_ids") or []),
            "legacy_payloads": list(policy.get("legacy_payloads") or []),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return int(hashlib.sha256(legacy_identity.encode("utf-8")).hexdigest()[:12], 16)


def _accepted_payload_digest(
    *,
    items: Sequence[StocksItem],
    warehouse_rows: Sequence[StocksWarehouseRow],
) -> str:
    def _jsonable(value: Any) -> Any:
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, Mapping):
            return {str(key): _jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable(item) for item in value]
        if hasattr(value, "__dict__"):
            return {
                str(key): _jsonable(item)
                for key, item in vars(value).items()
            }
        return value

    payload = {
        "items": sorted(
            (_jsonable(item) for item in items),
            key=lambda item: (
                int(item.get("nm_id") or 0),
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        ),
        "warehouse_rows": sorted(
            (_jsonable(row) for row in warehouse_rows),
            key=lambda row: (
                int(row.get("nm_id") or 0),
                -1 if row.get("warehouse_id") is None else int(row.get("warehouse_id")),
                str(row.get("warehouse_name") or ""),
                str(row.get("region_name") or ""),
                str(row.get("planning_zone_key") or ""),
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _projection_cache_key(
    *,
    owner: str,
    target_date: str,
    cache_digest: str,
    cache_policy_revision: int,
    selected: Sequence[int],
    mode: str,
) -> str:
    return hashlib.sha256(
        (
            f"{owner}|{target_date}|{cache_digest}|{cache_policy_revision}|"
            f"{','.join(map(str, selected))}|{mode}"
        ).encode("utf-8")
    ).hexdigest()


def _validate_projection_invariants(
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    field_names = [
        ("total", "stock_total_mp"),
        *[
            (region, "stock_total_mp" if source_field == "stock_total" else source_field)
            for region, source_field in (
                ("central", "stock_ru_central"),
                ("northwest", "stock_ru_northwest"),
                ("volga", "stock_ru_volga"),
                ("south_caucasus", "stock_ru_south_caucasus"),
                ("ural", "stock_ru_ural"),
                ("far_siberia", "stock_ru_far_siberia"),
            )
        ],
    ]
    totals: dict[str, dict[str, float | int]] = {}
    checked_cells = 0
    for region, field_name in field_names:
        fact_key = f"actual_{field_name}"
        incident_key = f"excluded_{field_name}"
        effective_key = f"effective_{field_name}"
        fact_total = 0.0
        incident_total = 0.0
        effective_total = 0.0
        projected_rows = 0
        for row in dict(projection.get("by_nm_id") or {}).values():
            values = (row.get(fact_key), row.get(incident_key), row.get(effective_key))
            if values == (None, None, None):
                continue
            if any(value is None for value in values):
                raise WbIncidentPolicyError(
                    f"incident projection {region} has a partial fact/incident/effective triple"
                )
            fact, incident, effective = (float(value) for value in values)
            if min(fact, incident, effective) < 0:
                raise WbIncidentPolicyError(
                    f"incident projection {region} contains a negative value"
                )
            if incident > fact + 1e-6:
                raise WbIncidentPolicyError(
                    f"incident projection {region} exceeds factual stock"
                )
            if abs(effective - (fact - incident)) > 1e-6:
                raise WbIncidentPolicyError(
                    f"incident projection {region} does not reconcile"
                )
            fact_total += fact
            incident_total += incident
            effective_total += effective
            projected_rows += 1
            checked_cells += 3
        if abs(effective_total - (fact_total - incident_total)) > 1e-6:
            raise WbIncidentPolicyError(
                f"incident projection {region} TOTAL does not reconcile"
            )
        totals[region] = {
            "projected_sku_count": projected_rows,
            "fact": round(fact_total, 6),
            "incident": round(incident_total, 6),
            "effective": round(effective_total, 6),
        }
    return {
        "status": "ok",
        "checked_cells": checked_cells,
        "totals": totals,
    }


def _apply_provisional_evidence_blanks(
    projection: dict[str, Any],
    *,
    items: Sequence[StocksItem],
    warehouse_rows: Sequence[StocksWarehouseRow],
    selected: Sequence[int],
) -> dict[str, Any]:
    selected_ids = {int(item) for item in selected}
    selected_rows_by_nm_and_id: dict[int, dict[int, list[StocksWarehouseRow]]] = {}
    selected_quantity_by_nm_and_field: dict[int, dict[str, float]] = {}
    selected_region_by_id: dict[int, str] = {}
    ambiguous_region_ids: set[int] = set()
    for row in warehouse_rows:
        warehouse_id = row.warehouse_id
        if warehouse_id is None or int(warehouse_id) not in selected_ids:
            continue
        numeric_id = int(warehouse_id)
        selected_rows_by_nm_and_id.setdefault(int(row.nm_id), {}).setdefault(
            numeric_id, []
        ).append(row)
        quantity_by_field = selected_quantity_by_nm_and_field.setdefault(
            int(row.nm_id), {}
        )
        quantity_by_field["stock_total_mp"] = (
            quantity_by_field.get("stock_total_mp", 0.0)
            + max(float(row.quantity), 0.0)
        )
        region_field = REGION_TO_FIELD.get(str(row.region_name or "").strip())
        if region_field:
            quantity_by_field[region_field] = (
                quantity_by_field.get(region_field, 0.0)
                + max(float(row.quantity), 0.0)
            )
            previous = selected_region_by_id.get(numeric_id)
            if previous is not None and previous != region_field:
                ambiguous_region_ids.add(numeric_id)
            else:
                selected_region_by_id[numeric_id] = region_field
    for warehouse_id in ambiguous_region_ids:
        selected_region_by_id.pop(warehouse_id, None)

    canonical_region_fields = sorted(set(REGION_TO_FIELD.values()))
    blank_reasons: dict[str, dict[str, str]] = {}
    for item in items:
        nm_id = int(item.nm_id)
        row_contract = dict(projection.get("by_nm_id", {}).get(str(nm_id)) or {})
        if not row_contract:
            continue
        by_selected_id = selected_rows_by_nm_and_id.get(nm_id, {})
        field_requirements: dict[str, set[int]] = {
            "stock_total_mp": set(selected_ids),
        }
        for region_field in canonical_region_fields:
            field_requirements[region_field] = {
                warehouse_id
                for warehouse_id, mapped_region in selected_region_by_id.items()
                if mapped_region == region_field
            }
        unseen_selected_ids = selected_ids - {
            int(row.warehouse_id)
            for row in warehouse_rows
            if row.warehouse_id is not None and int(row.warehouse_id) in selected_ids
        }
        for field_name, required_ids in field_requirements.items():
            evidence_complete = (
                bool(set(by_selected_id) & selected_ids)
                if field_name == "stock_total_mp"
                else (not required_ids or bool(required_ids & set(by_selected_id)))
            )
            if field_name != "stock_total_mp" and unseen_selected_ids:
                evidence_complete = False
            actual_value = row_contract.get(f"actual_{field_name}")
            received_incident = selected_quantity_by_nm_and_field.get(
                nm_id, {}
            ).get(field_name, 0.0)
            incident_exceeds_fact = (
                actual_value is not None
                and received_incident > float(actual_value) + 1e-6
            )
            if incident_exceeds_fact:
                evidence_complete = False
            if evidence_complete:
                continue
            for prefix in ("actual", "excluded", "effective"):
                row_contract[f"{prefix}_{field_name}"] = None
            reason = (
                "Полученный физический incident quantity превышает factual stock; "
                "конкретная SKU/региональная строка не доказана и оставлена пустой."
                if incident_exceeds_fact
                else (
                    "Недостаточно фактически сохранённых строк выбранных складов для "
                    "этого SKU/региона; нулевое значение не предполагается."
                )
            )
            blank_reasons.setdefault(str(nm_id), {})[field_name] = reason
            row_contract.setdefault("blank_reasons_by_field", {})[field_name] = reason
            if field_name == "stock_total_mp":
                row_contract["over_exclusion"] = None
                row_contract["reconciliation_difference"] = None
        projection["by_nm_id"][str(nm_id)] = row_contract
    projection["blank_reasons_by_nm_id"] = blank_reasons
    projection["accepted_selected_warehouse_ids"] = sorted(
        {
            int(row.warehouse_id)
            for row in warehouse_rows
            if row.warehouse_id is not None and int(row.warehouse_id) in selected_ids
        }
    )
    return projection


def _blank_aggregate_only_incident_regions(
    projection: dict[str, Any],
) -> dict[str, Any]:
    reason = (
        "WB вернул агрегированный остаток без полного складского распределения; "
        "региональное значение не доказано и оставлено пустым."
    )
    regional_fields = sorted(
        {
            *REGION_TO_FIELD.values(),
            *SUPPLY_PLANNING_ZONE_TO_STOCK_FIELD.values(),
        }
    )
    blank_reasons = dict(projection.get("blank_reasons_by_nm_id") or {})
    for nm_id, raw_row in dict(projection.get("by_nm_id") or {}).items():
        row = dict(raw_row or {})
        row_reasons = dict(row.get("blank_reasons_by_field") or {})
        for field_name in regional_fields:
            for prefix in ("actual", "excluded", "effective"):
                row[f"{prefix}_{field_name}"] = None
            row_reasons[field_name] = reason
        row["blank_reasons_by_field"] = row_reasons
        blank_reasons[str(nm_id)] = {
            **dict(blank_reasons.get(str(nm_id)) or {}),
            **{field_name: reason for field_name in regional_fields},
        }
        projection["by_nm_id"][str(nm_id)] = row
    projection["blank_reasons_by_nm_id"] = blank_reasons
    return projection


def build_incident_stock_projection(
    runtime: Any,
    *,
    items: Sequence[StocksItem],
    warehouse_rows: Sequence[StocksWarehouseRow],
    snapshot_date: str,
    fetched_at: str,
    pagination_complete: bool,
    raw_rows_digest: str,
    seller_id: str | None = None,
    cache_enabled: bool = True,
) -> dict[str, Any]:
    """Project fact/incident/effective quantities without mutating canonical rows."""

    target_date = _iso_date(snapshot_date, field_name="snapshot_date")
    owner = seller_id or canonical_seller_id()
    policy = get_policy_state(runtime, snapshot_date=target_date, seller_id=owner)
    selected = tuple(int(item) for item in policy.get("warehouse_ids") or []) if policy.get("active") else ()
    digest = str(raw_rows_digest or "").strip()
    if selected and (not pagination_complete or not digest):
        raise WbIncidentPolicyError(
            "active incident policy requires a complete snapshot with a stable digest"
        )
    policy_revision = int(policy.get("revision") or 0)
    cache_policy_revision = _projection_cache_policy_revision(policy)
    cache_key = hashlib.sha256(
        f"{owner}|{target_date}|{digest}|{cache_policy_revision}|{','.join(map(str, selected))}".encode("utf-8")
    ).hexdigest()
    if digest and cache_enabled:
        cached = runtime.load_wb_incident_projection_cache(
            seller_id=owner,
            snapshot_digest=digest,
            policy_revision=cache_policy_revision,
            snapshot_date=target_date,
        )
        if isinstance(cached, Mapping) and cached.get("status") == "ok":
            projection = dict(cached.get("projection") or {})
            projection["cache"] = {"status": "hit", "key": cache_key}
            return projection

    exact_rows = _historical_rows_with_exact_identity(warehouse_rows, policy=policy) if selected else list(warehouse_rows)
    projection = build_wb_warehouse_exclusion(
        items=list(items),
        warehouse_rows=exact_rows,
        excluded_warehouse_ids=selected,
        snapshot_date=target_date,
        fetched_at=fetched_at,
        pagination_complete=bool(pagination_complete),
        raw_rows_digest=digest,
        require_complete=bool(selected),
    )
    affected_ids = [
        int(nm_id)
        for nm_id, row in projection.get("by_nm_id", {}).items()
        if float(row.get("excluded_stock_total_mp") or 0.0) > 0
    ]
    projection.update(
        {
            "contract_name": "wb_incident_stock_projection",
            "contract_version": 1,
            "seller_id": owner,
            "policy": policy,
            "policy_revision": policy_revision,
            "projection_cache_policy_revision": cache_policy_revision,
            "policy_active": bool(policy.get("active")),
            "affected_nm_ids": affected_ids,
            "snapshot_digest": digest,
            "cache": {"status": "miss", "key": cache_key},
        }
    )
    if digest and cache_enabled:
        runtime.save_wb_incident_projection_cache(
            seller_id=owner,
            snapshot_digest=digest,
            policy_revision=cache_policy_revision,
            snapshot_date=target_date,
            projection=projection,
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
    return projection


def build_vitrina_incident_stock_projection(
    runtime: Any,
    *,
    items: Sequence[StocksItem],
    warehouse_rows: Sequence[StocksWarehouseRow],
    snapshot_date: str,
    fetched_at: str,
    pagination_complete: bool,
    raw_rows_digest: str,
    warehouse_granularity_complete: bool = True,
    seller_id: str | None = None,
    cache_enabled: bool = True,
) -> dict[str, Any]:
    """Build the information-only Vitrina projection from accepted rows.

    This is intentionally a separate adapter from the strict shared API used by
    Supply and SKU Management.  A partial snapshot may publish only triples
    supported by actually persisted item/warehouse rows.  Missing evidence stays
    blank and the result carries an explicit provisional quality contract.
    """

    target_date = _iso_date(snapshot_date, field_name="snapshot_date")
    owner = seller_id or canonical_seller_id()
    policy = get_policy_state(runtime, snapshot_date=target_date, seller_id=owner)
    selected = (
        tuple(int(item) for item in policy.get("warehouse_ids") or [])
        if policy.get("active")
        else ()
    )
    source_digest = str(raw_rows_digest or "").strip()
    completeness_confirmed = bool(
        pagination_complete
        and source_digest
        and warehouse_granularity_complete
    )
    if not selected or completeness_confirmed:
        projection = build_incident_stock_projection(
            runtime,
            items=items,
            warehouse_rows=warehouse_rows,
            snapshot_date=target_date,
            fetched_at=fetched_at,
            pagination_complete=bool(
                pagination_complete and warehouse_granularity_complete
            ),
            raw_rows_digest=source_digest,
            seller_id=owner,
            cache_enabled=cache_enabled,
        )
        projection["projection_mode"] = "vitrina_information"
        if not warehouse_granularity_complete:
            projection = _blank_aggregate_only_incident_regions(projection)
        projection["quality"] = {
            "state": (
                "confirmed"
                if completeness_confirmed
                else (
                    "aggregate_only"
                    if not warehouse_granularity_complete
                    else "received_rows"
                )
            ),
            "label_ru": (
                "Полнота WB подтверждена"
                if completeness_confirmed
                else (
                    "Только агрегированный остаток WB"
                    if not warehouse_granularity_complete
                    else "Полученный снимок"
                )
            ),
            "message_ru": (
                "Полнота WB подтверждена"
                if completeness_confirmed
                else (
                    "WB не привязал остаток к конкретным складам; региональные и incident-распределения не публикуются"
                    if not warehouse_granularity_complete
                    else "Политика не требует исключения складов; показаны фактически полученные строки"
                )
            ),
            "completeness_confirmed": completeness_confirmed,
            "pagination_complete": bool(pagination_complete),
            "raw_rows_digest_present": bool(source_digest),
            "raw_rows_digest": source_digest,
            "warehouse_granularity_complete": bool(
                warehouse_granularity_complete
            ),
            "accepted_payload_digest": _accepted_payload_digest(
                items=items,
                warehouse_rows=warehouse_rows,
            ),
            "accepted_item_count": len(items),
            "accepted_warehouse_row_count": len(warehouse_rows),
            "policy_revision": int(policy.get("revision") or 0),
            "policy_effective_date": str(policy.get("effective_from") or ""),
            "snapshot_date": target_date,
        }
        projection["warehouse_granularity_complete"] = bool(
            warehouse_granularity_complete
        )
        projection["invariants"] = _validate_projection_invariants(projection)
        return projection

    accepted_payload_digest = _accepted_payload_digest(
        items=items,
        warehouse_rows=warehouse_rows,
    )
    cache_digest = (
        "vitrina-accepted-payload:"
        if warehouse_granularity_complete
        else "vitrina-aggregate-only-accepted-payload:"
    ) + accepted_payload_digest
    policy_revision = int(policy.get("revision") or 0)
    cache_policy_revision = _projection_cache_policy_revision(policy)
    cache_key = _projection_cache_key(
        owner=owner,
        target_date=target_date,
        cache_digest=cache_digest,
        cache_policy_revision=cache_policy_revision,
        selected=selected,
        mode=(
            "vitrina_provisional_received_rows_v1"
            if warehouse_granularity_complete
            else "vitrina_aggregate_only_received_rows_v1"
        ),
    )
    if cache_enabled:
        cached = runtime.load_wb_incident_projection_cache(
            seller_id=owner,
            snapshot_digest=cache_digest,
            policy_revision=cache_policy_revision,
            snapshot_date=target_date,
        )
        if isinstance(cached, Mapping) and cached.get("status") == "ok":
            projection = dict(cached.get("projection") or {})
            projection["cache"] = {"status": "hit", "key": cache_key}
            return projection

    exact_rows = _historical_rows_with_exact_identity(warehouse_rows, policy=policy)
    projection = build_wb_warehouse_exclusion(
        items=list(items),
        warehouse_rows=exact_rows,
        excluded_warehouse_ids=selected,
        snapshot_date=target_date,
        fetched_at=fetched_at,
        # The low-level arithmetic requires this flag to avoid the strict gate.
        # The source quality below restores the truthful unconfirmed state and
        # is the only contract exposed to Vitrina.
        pagination_complete=True,
        raw_rows_digest=cache_digest,
        require_complete=False,
    )
    projection = _apply_provisional_evidence_blanks(
        projection,
        items=items,
        warehouse_rows=exact_rows,
        selected=selected,
    )
    if not warehouse_granularity_complete:
        projection = _blank_aggregate_only_incident_regions(projection)
    affected_ids = [
        int(nm_id)
        for nm_id, row in projection.get("by_nm_id", {}).items()
        if row.get("excluded_stock_total_mp") is not None
        and float(row.get("excluded_stock_total_mp") or 0.0) > 0
    ]
    quality = {
        "state": (
            "provisional_received_rows"
            if warehouse_granularity_complete
            else "aggregate_only"
        ),
        "label_ru": (
            "Полнота WB не подтверждена"
            if warehouse_granularity_complete
            else "Только агрегированный остаток WB"
        ),
        "message_ru": (
            VITRINA_PROVISIONAL_QUALITY_MESSAGE_RU
            if warehouse_granularity_complete
            else (
                "WB не привязал остаток к конкретным складам; точный stock_total сохранён, "
                "а региональные и incident-распределения оставлены пустыми"
            )
        ),
        "completeness_confirmed": False,
        "pagination_complete": bool(pagination_complete),
        "raw_rows_digest_present": bool(source_digest),
        "raw_rows_digest": source_digest,
        "warehouse_granularity_complete": bool(
            warehouse_granularity_complete
        ),
        "accepted_payload_digest": accepted_payload_digest,
        "accepted_item_count": len(items),
        "accepted_warehouse_row_count": len(warehouse_rows),
        "projected_item_count": sum(
            1
            for row in projection.get("by_nm_id", {}).values()
            if row.get("actual_stock_total_mp") is not None
        ),
        "blank_item_count": sum(
            1
            for row in projection.get("by_nm_id", {}).values()
            if row.get("actual_stock_total_mp") is None
        ),
        "policy_revision": policy_revision,
        "policy_effective_date": str(policy.get("effective_from") or ""),
        "snapshot_date": target_date,
    }
    projection.update(
        {
            "contract_name": "wb_incident_stock_projection",
            "contract_version": 2,
            "projection_mode": (
                "vitrina_provisional_received_rows"
                if warehouse_granularity_complete
                else "vitrina_aggregate_only_received_rows"
            ),
            "seller_id": owner,
            "policy": policy,
            "policy_revision": policy_revision,
            "projection_cache_policy_revision": cache_policy_revision,
            "policy_active": True,
            "affected_nm_ids": affected_ids,
            # Never expose the accepted-payload digest as source completeness.
            "snapshot_digest": source_digest,
            "cache_identity_digest": cache_digest,
            "quality": quality,
            "warehouse_granularity_complete": bool(
                warehouse_granularity_complete
            ),
            "cache": {
                "status": "miss" if cache_enabled else "bypassed",
                "key": cache_key,
            },
        }
    )
    projection["pagination_complete"] = bool(pagination_complete)
    projection["raw_rows_digest"] = source_digest
    projection["invariants"] = _validate_projection_invariants(projection)
    if cache_enabled:
        runtime.save_wb_incident_projection_cache(
            seller_id=owner,
            snapshot_digest=cache_digest,
            policy_revision=cache_policy_revision,
            snapshot_date=target_date,
            projection=projection,
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
    return projection


def policy_badge(policy: Mapping[str, Any], *, options: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    selected = {
        int(item)
        for item in (
            policy.get("effective_warehouse_ids")
            if policy.get("active") and "effective_warehouse_ids" in policy
            else policy.get("warehouse_ids")
        )
        or []
    }
    names = [
        str(option.get("warehouse_name") or f"warehouseId {option.get('warehouse_id')}")
        for option in options
        if option.get("warehouse_id") is not None and int(option["warehouse_id"]) in selected
    ]
    if not names:
        names = [
            str(identity.get("warehouse_name") or f"warehouseId {identity.get('warehouse_id')}")
            for identity in (
                policy.get("effective_warehouse_identities")
                if policy.get("active") and "effective_warehouse_identities" in policy
                else policy.get("warehouse_identities")
            )
            or []
        ]
    active = bool(policy.get("active"))
    return {
        "active": active,
        "label": "Учитывается политика инцидентов" if active else "Политика инцидентов не активна",
        "detail": f"Не участвуют: {', '.join(names)}" if active and names else "",
        "warehouse_names": names,
        "revision": int(policy.get("effective_revision") or policy.get("revision") or 0),
        "effective_from": str(
            policy.get("effective_effective_from") or policy.get("effective_from") or ""
        ),
    }


def projection_digest(projection: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(projection), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
