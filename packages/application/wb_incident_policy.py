"""Seller-level WB warehouse incident policy and immutable stock projections."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
import os
from typing import Any, Mapping, Sequence

from packages.application.stocks_block import (
    build_wb_warehouse_exclusion,
    parse_excluded_wb_warehouse_ids,
)
from packages.business_time import current_business_date_iso
from packages.contracts.stocks_block import StocksItem, StocksWarehouseRow


POLICY_CONTRACT_NAME = "wb_warehouse_incident_policy"
POLICY_CONTRACT_VERSION = 1
LEGACY_CONFIG_KEY = "wb_warehouse_exclusions"
POLICY_STATUS_VALUES = {"active", "monitoring", "resolved", "disabled"}


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
    result = dict(record)
    effective_to = str(result.get("effective_to") or "")
    active = bool(result.get("active")) and str(result.get("policy_status") or "") in {
        "active",
        "monitoring",
    }
    if effective_to and target_date > effective_to:
        active = False
    result.update(
        {
            "contract_name": POLICY_CONTRACT_NAME,
            "contract_version": POLICY_CONTRACT_VERSION,
            "active": active,
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
    configured_active = bool(latest.get("active"))
    currently_effective = bool(current.get("active"))
    result.update(
        {
            "contract_name": POLICY_CONTRACT_NAME,
            "contract_version": POLICY_CONTRACT_VERSION,
            "configured_active": configured_active,
            "active": currently_effective,
            "currently_effective": currently_effective,
            "effective_revision": int(current.get("revision") or 0),
            "effective_warehouse_ids": list(current.get("warehouse_ids") or []),
            "effective_warehouse_identities": list(current.get("warehouse_identities") or []),
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
    try:
        warehouse_ids = parse_excluded_wb_warehouse_ids(
            {"excluded_wb_warehouse_ids": payload.get("excluded_wb_warehouse_ids", [])},
            allow_legacy_elektrostal=False,
        )
    except ValueError as exc:
        raise WbIncidentPolicyError(str(exc)) from exc
    reason = str(payload.get("reason") or "").strip()
    effective_from = _iso_date(payload.get("effective_from"), field_name="effective_from")
    effective_to = _iso_date(payload.get("effective_to"), field_name="effective_to", required=False)
    if effective_to and effective_to < effective_from:
        raise WbIncidentPolicyError("effective_to cannot be earlier than effective_from")
    policy_status = str(payload.get("status") or ("active" if active else "disabled")).strip().lower()
    if policy_status not in POLICY_STATUS_VALUES:
        raise WbIncidentPolicyError("status must be active, monitoring, resolved, or disabled")
    if active and not warehouse_ids:
        raise WbIncidentPolicyError("active policy must select at least one warehouse")
    if active and not reason:
        raise WbIncidentPolicyError("active policy requires a reason")

    option_by_id = {
        int(option["warehouse_id"]): dict(option)
        for option in warehouse_options
        if option.get("warehouse_id") is not None
    }
    identities: list[dict[str, Any]] = []
    normalized_names: dict[str, int] = {}
    for warehouse_id in warehouse_ids:
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
        identities.append({"warehouse_id": warehouse_id, "warehouse_name": warehouse_name})

    latest = _latest_policy(runtime, seller_id=owner)
    base_revision = payload.get("base_revision")
    if base_revision is not None and int(base_revision) != int((latest or {}).get("revision") or 0):
        raise WbIncidentPolicyError("WB incident policy revision conflict")
    legacy_payloads = (
        _legacy_policy(runtime, seller_id=owner, snapshot_date=effective_from) or {}
    ).get("legacy_payloads") or []
    created_at = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    saved = runtime.append_wb_incident_policy_revision(
        seller_id=owner,
        active=active,
        warehouse_ids=warehouse_ids,
        warehouse_identities=identities,
        reason=reason,
        effective_from=effective_from,
        effective_to=effective_to,
        policy_status=policy_status,
        actor=str(actor or "").strip(),
        created_at=created_at,
        source="incident_policy",
        legacy_payloads=legacy_payloads,
        expected_revision=int(base_revision) if base_revision is not None else None,
    )
    if saved.get("status") == "conflict":
        raise WbIncidentPolicyError("WB incident policy revision conflict")
    return get_latest_policy_state(
        runtime,
        snapshot_date=current_business_date_iso(),
        seller_id=owner,
    )


def _historical_rows_with_exact_identity(
    rows: Sequence[StocksWarehouseRow],
    *,
    policy: Mapping[str, Any],
) -> list[StocksWarehouseRow]:
    identities = list(policy.get("warehouse_identities") or [])
    by_name: dict[str, int] = {}
    for identity in identities:
        name = _normalize_name(identity.get("warehouse_name"))
        warehouse_id = int(identity.get("warehouse_id") or 0)
        if not name:
            raise WbIncidentPolicyError("active policy contains an empty historical warehouse identity")
        if name in by_name and by_name[name] != warehouse_id:
            raise WbIncidentPolicyError("active policy contains an ambiguous historical warehouse identity")
        by_name[name] = warehouse_id
    if policy.get("active") and policy.get("warehouse_ids") and len(by_name) != len(policy.get("warehouse_ids") or []):
        raise WbIncidentPolicyError("historical incident projection has no exact identity for every selected warehouse")
    return [
        replace(row, warehouse_id=by_name.get(_normalize_name(row.warehouse_name)))
        if row.warehouse_id is None and _normalize_name(row.warehouse_name) in by_name
        else row
        for row in rows
    ]


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
    cache_policy_revision = policy_revision
    if policy.get("migration_pending"):
        legacy_identity = json.dumps(
            {
                "warehouse_ids": list(policy.get("warehouse_ids") or []),
                "legacy_payloads": list(policy.get("legacy_payloads") or []),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        cache_policy_revision = int(
            hashlib.sha256(legacy_identity.encode("utf-8")).hexdigest()[:12],
            16,
        )
    cache_key = hashlib.sha256(
        f"{owner}|{target_date}|{digest}|{cache_policy_revision}|{','.join(map(str, selected))}".encode("utf-8")
    ).hexdigest()
    if digest:
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
    if digest:
        runtime.save_wb_incident_projection_cache(
            seller_id=owner,
            snapshot_digest=digest,
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
