"""Transactional baseline/diff engine for the seller change registry.

The engine consumes only the sanitized deterministic result produced by
``ChangeRegistrySourceAcquirer``.  It owns no scheduler, HTTP/UI route, WB
client, writer instrumentation or automatic invocation. The active observer
invokes ``ingest`` explicitly after its read-only source acquisition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping

from packages.application.change_registry import (
    CHECKPOINTS_TABLE,
    FACT_LINKS_TABLE,
    FACTS_TABLE,
    IDENTITY_INCIDENTS_TABLE,
    MAPPING_VERSION,
    MISSING,
    OBSERVATION_VALUES_TABLE,
    CanonicalValue,
    ChangeRegistryConflict,
    ChangeRegistryError,
    TargetIdentity,
    canonical_digest,
    canonical_json,
    canonicalize_value,
    target_identity,
)
from packages.application.change_registry_source_acquisition import (
    CONTRACT_NAME as ACQUISITION_CONTRACT_NAME,
    CONTRACT_VERSION as ACQUISITION_CONTRACT_VERSION,
)
from packages.application.storage_registry import StoreRegistry


CONTRACT_NAME = "wb_change_registry_baseline_engine"
CONTRACT_VERSION = 1
SOURCE_SURFACE = "wb_prices_ads_joint"
CREATION_ABSENT_STATE = "absent"
COMPARABLE_KINDS = frozenset({"integer", "text", "boolean"})
FIELD_BY_TARGET = {
    "price": frozenset(
        {"original_price_minor", "discount_bps", "seller_price_minor"}
    ),
    "bid": frozenset({"bid_minor"}),
    "campaign": frozenset(
        {"campaign_state", "payment_model", "payment_unit"}
    ),
}
NUMERIC_FIELDS = FIELD_BY_TARGET["price"] | FIELD_BY_TARGET["bid"]
TEXT_FIELDS = FIELD_BY_TARGET["campaign"]
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,119}")


@dataclass(frozen=True)
class _Observation:
    target: TargetIdentity
    parameter_field: str
    status: str
    value: CanonicalValue
    health_code: str
    evidence_digest: str

    @property
    def key(self) -> tuple[str, int, int, str, str]:
        return (
            self.target.target_kind,
            self.target.nm_id,
            self.target.advert_id,
            self.target.placement,
            self.parameter_field,
        )


@dataclass(frozen=True)
class _Incident:
    incident_kind: str
    advert_id: int
    candidate_nm_ids: tuple[int, ...]
    evidence_digest: str


@dataclass(frozen=True)
class _NormalizedAcquisition:
    seller_id: str
    account_scope: str
    started_at: str
    completed_at: str
    status: str
    manifest_digest: str
    observations: tuple[_Observation, ...]
    incidents: tuple[_Incident, ...]
    expected_target_count: int
    observed_target_count: int
    completeness_digest: str


class ChangeRegistryBaselineEngine:
    """Persist explicit source acquisitions and project proven intervals."""

    def __init__(
        self,
        *,
        runtime_dir: Path,
        seller_id: str,
        account_scope: str,
        source_surface: str = SOURCE_SURFACE,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.seller_id = _identity(seller_id, "seller_id")
        self.account_scope = _identity(account_scope, "account_scope")
        self.source_surface = _identity(source_surface, "source_surface")
        self.store_registry = StoreRegistry(self.runtime_dir)

    def ingest(
        self,
        acquisition: Mapping[str, Any],
        *,
        transaction_hook: Callable[[sqlite3.Connection, Mapping[str, Any]], None]
        | None = None,
    ) -> dict[str, Any]:
        """Persist one explicit invocation as one all-or-nothing transaction."""

        normalized = _normalize_acquisition(
            acquisition,
            expected_seller=self.seller_id,
            expected_account=self.account_scope,
        )
        checkpoint_id = _stable_id(
            "crcp",
            {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "seller_id": self.seller_id,
                "account_scope": self.account_scope,
                "source_surface": self.source_surface,
                "manifest_digest": normalized.manifest_digest,
            },
        )
        incident_surface = _incident_surface(checkpoint_id)

        with self.store_registry.session(
            "operational",
            mode="rw",
            operation="change_registry_baseline_engine_ingest",
        ) as conn:
            _require_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    f"SELECT * FROM {CHECKPOINTS_TABLE} WHERE checkpoint_id=?",
                    (checkpoint_id,),
                ).fetchone()
                if existing is not None:
                    _validate_existing_checkpoint(existing, normalized, self.source_surface)
                    receipt = self._receipt(
                        conn,
                        checkpoint_id=checkpoint_id,
                        incident_surface=incident_surface,
                    )
                    if transaction_hook is not None:
                        transaction_hook(conn, receipt)
                    conn.commit()
                    return receipt

                same_time = conn.execute(
                    f"""SELECT checkpoint_id FROM {CHECKPOINTS_TABLE}
                        WHERE seller_id=? AND account_scope=? AND source_surface=?
                          AND completed_at=? LIMIT 1""",
                    (
                        self.seller_id,
                        self.account_scope,
                        self.source_surface,
                        normalized.completed_at,
                    ),
                ).fetchone()
                if same_time is not None:
                    raise ChangeRegistryConflict(
                        "checkpoint chronology already owns this completion time"
                    )

                later_complete = conn.execute(
                    f"""SELECT checkpoint_id FROM {CHECKPOINTS_TABLE}
                        WHERE seller_id=? AND account_scope=? AND source_surface=?
                          AND completeness_status='complete' AND completed_at>=?
                        LIMIT 1""",
                    (
                        self.seller_id,
                        self.account_scope,
                        self.source_surface,
                        normalized.completed_at,
                    ),
                ).fetchone()
                if normalized.status == "complete" and later_complete is not None:
                    raise ChangeRegistryConflict(
                        "complete checkpoints must advance in strict chronological order"
                    )

                previous = conn.execute(
                    f"""SELECT * FROM {CHECKPOINTS_TABLE}
                        WHERE seller_id=? AND account_scope=? AND source_surface=?
                          AND completeness_status='complete' AND completed_at<?
                        ORDER BY completed_at DESC,checkpoint_id DESC LIMIT 1""",
                    (
                        self.seller_id,
                        self.account_scope,
                        self.source_surface,
                        normalized.completed_at,
                    ),
                ).fetchone()
                previous_id = str(previous["checkpoint_id"]) if previous else None
                checkpoint_row = {
                    "checkpoint_id": checkpoint_id,
                    "seller_id": self.seller_id,
                    "account_scope": self.account_scope,
                    "source_surface": self.source_surface,
                    "scan_kind": "observer",
                    "started_at": normalized.started_at,
                    "completed_at": normalized.completed_at,
                    "completeness_status": normalized.status,
                    "expected_target_count": normalized.expected_target_count,
                    "observed_target_count": normalized.observed_target_count,
                    "completeness_digest": normalized.completeness_digest,
                    "evidence_digest": normalized.manifest_digest,
                    "previous_complete_checkpoint_id": previous_id,
                    "mapping_version": MAPPING_VERSION,
                }
                _insert_idempotent(conn, CHECKPOINTS_TABLE, "checkpoint_id", checkpoint_row)

                current_by_key: dict[tuple[str, int, int, str, str], dict[str, Any]] = {}
                for observation in normalized.observations:
                    row = _observation_row(checkpoint_id, normalized.completed_at, observation)
                    stored = _insert_idempotent(
                        conn,
                        OBSERVATION_VALUES_TABLE,
                        "observation_value_id",
                        row,
                    )
                    current_by_key[observation.key] = stored

                for incident in normalized.incidents:
                    row = _incident_row(
                        checkpoint_id=checkpoint_id,
                        seller_id=self.seller_id,
                        account_scope=self.account_scope,
                        source_surface=incident_surface,
                        observed_at=normalized.completed_at,
                        incident=incident,
                    )
                    _insert_idempotent(
                        conn, IDENTITY_INCIDENTS_TABLE, "incident_id", row
                    )

                if normalized.status == "complete" and previous is not None:
                    self._append_disappearance_observations(
                        conn,
                        previous_checkpoint_id=str(previous["checkpoint_id"]),
                        current_checkpoint_id=checkpoint_id,
                        observed_at=normalized.completed_at,
                        current_by_key=current_by_key,
                        excluded_advert_ids={
                            incident.advert_id for incident in normalized.incidents
                        },
                    )

                if normalized.status == "complete" and previous is not None:
                    self._append_diffs(
                        conn,
                        previous=previous,
                        current_checkpoint=checkpoint_row,
                        current_by_key=current_by_key,
                    )

                receipt = self._receipt(
                    conn,
                    checkpoint_id=checkpoint_id,
                    incident_surface=incident_surface,
                )
                if transaction_hook is not None:
                    transaction_hook(conn, receipt)
                conn.commit()
                return receipt
            except Exception:
                conn.rollback()
                raise

    def _append_disappearance_observations(
        self,
        conn: sqlite3.Connection,
        *,
        previous_checkpoint_id: str,
        current_checkpoint_id: str,
        observed_at: str,
        current_by_key: dict[
            tuple[str, int, int, str, str], dict[str, Any]
        ],
        excluded_advert_ids: set[int],
    ) -> None:
        previous_rows = conn.execute(
            f"""SELECT * FROM {OBSERVATION_VALUES_TABLE}
                WHERE checkpoint_id=?
                ORDER BY target_kind,nm_id,advert_id,placement,parameter_field""",
            (previous_checkpoint_id,),
        ).fetchall()
        for previous_row in previous_rows:
            key = _row_key(previous_row)
            if key in current_by_key or (
                key[2] > 0 and key[2] in excluded_advert_ids
            ):
                continue
            target = target_identity(
                key[0],
                nm_id=key[1],
                advert_id=key[2],
                placement=key[3],
            )
            observation = _Observation(
                target=target,
                parameter_field=key[4],
                status="missing",
                value=CanonicalValue("missing"),
                health_code="target_disappeared",
                evidence_digest=canonical_digest(
                    {
                        "previous_checkpoint_id": previous_checkpoint_id,
                        "current_checkpoint_id": current_checkpoint_id,
                        "target": _target_payload(target),
                        "parameter_field": key[4],
                        "status": "missing",
                        "health_code": "target_disappeared",
                    }
                ),
            )
            row = _observation_row(
                current_checkpoint_id,
                observed_at,
                observation,
            )
            current_by_key[key] = _insert_idempotent(
                conn,
                OBSERVATION_VALUES_TABLE,
                "observation_value_id",
                row,
            )

    def _append_diffs(
        self,
        conn: sqlite3.Connection,
        *,
        previous: sqlite3.Row,
        current_checkpoint: Mapping[str, Any],
        current_by_key: Mapping[tuple[str, int, int, str, str], Mapping[str, Any]],
    ) -> None:
        previous_id = str(previous["checkpoint_id"])
        previous_rows = conn.execute(
            f"""SELECT * FROM {OBSERVATION_VALUES_TABLE}
                WHERE checkpoint_id=?
                ORDER BY target_kind,nm_id,advert_id,placement,parameter_field""",
            (previous_id,),
        ).fetchall()
        previous_by_key = {_row_key(row): row for row in previous_rows}
        previous_advert_ids = _checkpoint_advert_ids(conn, previous_id)

        for key in sorted(current_by_key):
            current = current_by_key[key]
            current_value = _comparable_row_value(current)
            if current_value is None:
                continue
            previous_row = previous_by_key.get(key)
            previous_value = (
                _comparable_row_value(previous_row) if previous_row is not None else None
            )
            target_kind, nm_id, advert_id, placement, parameter_field = key
            proof_checkpoint_id = previous_id
            proof_completed_at = str(previous["completed_at"])
            if previous_value is None:
                prior_exact = conn.execute(
                    f"""SELECT observation.*, checkpoint.checkpoint_id AS proof_checkpoint_id,
                               checkpoint.completed_at AS proof_completed_at
                        FROM {OBSERVATION_VALUES_TABLE} observation
                        JOIN {CHECKPOINTS_TABLE} checkpoint
                          ON checkpoint.checkpoint_id=observation.checkpoint_id
                        WHERE checkpoint.seller_id=? AND checkpoint.account_scope=?
                          AND checkpoint.source_surface=?
                          AND checkpoint.completeness_status='complete'
                          AND checkpoint.completed_at<?
                          AND observation.target_kind=? AND observation.nm_id=?
                          AND observation.advert_id=? AND observation.placement=?
                          AND observation.parameter_field=?
                          AND observation.observation_status IN ('exact','exact_zero')
                          AND observation.value_kind IN ('integer','text','boolean')
                        ORDER BY checkpoint.completed_at DESC,checkpoint.checkpoint_id DESC
                        LIMIT 1""",
                    (
                        self.seller_id,
                        self.account_scope,
                        self.source_surface,
                        current_checkpoint["completed_at"],
                        target_kind,
                        nm_id,
                        advert_id,
                        placement,
                        parameter_field,
                    ),
                ).fetchone()
                if prior_exact is not None:
                    previous_value = _comparable_row_value(prior_exact)
                    proof_checkpoint_id = str(prior_exact["proof_checkpoint_id"])
                    proof_completed_at = str(prior_exact["proof_completed_at"])
            if previous_value is not None:
                if previous_value == current_value:
                    continue
                before_value = previous_value
            elif (
                target_kind == "campaign"
                and parameter_field == "campaign_state"
                and advert_id not in previous_advert_ids
            ):
                before_value = CanonicalValue("text", text_value=CREATION_ABSENT_STATE)
            else:
                continue

            target = target_identity(
                target_kind,
                nm_id=nm_id,
                advert_id=advert_id,
                placement=placement,
            )
            fact_basis = {
                "proof_kind": "checkpoint_diff",
                "seller_id": self.seller_id,
                "account_scope": self.account_scope,
                "previous_checkpoint_id": proof_checkpoint_id,
                "current_checkpoint_id": current_checkpoint["checkpoint_id"],
                "target": _target_payload(target),
                "parameter_field": parameter_field,
                "before": _value_payload(before_value),
                "after": _value_payload(current_value),
                "observed_from": proof_completed_at,
                "observed_to": str(current_checkpoint["completed_at"]),
            }
            evidence_digest = canonical_digest(fact_basis)
            fact_id = _stable_id("crf", fact_basis)
            fact_row = {
                "fact_id": fact_id,
                "seller_id": self.seller_id,
                "account_scope": self.account_scope,
                "target_kind": target.target_kind,
                "nm_id": target.nm_id,
                "advert_id": target.advert_id,
                "placement": target.placement,
                "parameter_field": parameter_field,
                **_value_columns("before_value", before_value),
                **_value_columns("after_value", current_value),
                "observed_from": proof_completed_at,
                "observed_to": str(current_checkpoint["completed_at"]),
                "proven_at": str(current_checkpoint["completed_at"]),
                "proof_kind": "checkpoint_diff",
                "evidence_digest": evidence_digest,
                "mapping_version": MAPPING_VERSION,
            }
            _insert_idempotent(conn, FACTS_TABLE, "fact_id", fact_row)

            exact_current = conn.execute(
                f"""SELECT 1 FROM {OBSERVATION_VALUES_TABLE}
                    WHERE checkpoint_id=? AND target_kind=? AND nm_id=?
                      AND advert_id=? AND placement=? AND parameter_field=?
                      AND observation_status IN ('exact','exact_zero')
                      AND value_kind IN ('integer','text','boolean')""",
                (
                    current_checkpoint["checkpoint_id"],
                    target.target_kind,
                    target.nm_id,
                    target.advert_id,
                    target.placement,
                    parameter_field,
                ),
            ).fetchone()
            if exact_current is None:
                raise ChangeRegistryConflict(
                    "checkpoint fact has no exact target/field observation"
                )
            link_basis = {
                "fact_id": fact_id,
                "checkpoint_id": current_checkpoint["checkpoint_id"],
                "target": _target_payload(target),
                "parameter_field": parameter_field,
            }
            link_row = {
                "fact_link_id": _stable_id("crfl", link_basis),
                "fact_id": fact_id,
                "link_kind": "checkpoint",
                "change_item_id": None,
                "checkpoint_id": current_checkpoint["checkpoint_id"],
                "native_audit_reference": "",
                "recommendation_item_id": "",
                "linked_at": str(current_checkpoint["completed_at"]),
                "evidence_digest": canonical_digest(link_basis),
            }
            _insert_idempotent(conn, FACT_LINKS_TABLE, "fact_link_id", link_row)

    def _receipt(
        self,
        conn: sqlite3.Connection,
        *,
        checkpoint_id: str,
        incident_surface: str,
    ) -> dict[str, Any]:
        checkpoint = conn.execute(
            f"SELECT * FROM {CHECKPOINTS_TABLE} WHERE checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()
        if checkpoint is None:
            raise ChangeRegistryConflict("checkpoint transaction readback is missing")
        observation_ids = [
            str(row[0])
            for row in conn.execute(
                f"""SELECT observation_value_id FROM {OBSERVATION_VALUES_TABLE}
                    WHERE checkpoint_id=? ORDER BY observation_value_id""",
                (checkpoint_id,),
            ).fetchall()
        ]
        fact_ids = [
            str(row[0])
            for row in conn.execute(
                f"""SELECT fact.fact_id FROM {FACTS_TABLE} fact
                    JOIN {FACT_LINKS_TABLE} link ON link.fact_id=fact.fact_id
                    WHERE link.link_kind='checkpoint' AND link.checkpoint_id=?
                    ORDER BY fact.fact_id""",
                (checkpoint_id,),
            ).fetchall()
        ]
        incident_ids = [
            str(row[0])
            for row in conn.execute(
                f"""SELECT incident_id FROM {IDENTITY_INCIDENTS_TABLE}
                    WHERE seller_id=? AND account_scope=? AND source_surface=?
                    ORDER BY incident_id""",
                (self.seller_id, self.account_scope, incident_surface),
            ).fetchall()
        ]
        payload = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "seller_id": self.seller_id,
            "account_scope": self.account_scope,
            "checkpoint_id": checkpoint_id,
            "completeness_status": str(checkpoint["completeness_status"]),
            "previous_complete_checkpoint_id": (
                str(checkpoint["previous_complete_checkpoint_id"])
                if checkpoint["previous_complete_checkpoint_id"] is not None
                else None
            ),
            "baseline_only": bool(
                checkpoint["completeness_status"] == "complete"
                and checkpoint["previous_complete_checkpoint_id"] is None
            ),
            "observation_value_ids": observation_ids,
            "identity_incident_ids": incident_ids,
            "fact_ids": fact_ids,
            "row_counts": {
                "checkpoints": 1,
                "observations": len(observation_ids),
                "identity_incidents": len(incident_ids),
                "facts": len(fact_ids),
            },
        }
        payload["receipt_digest"] = canonical_digest(payload)
        return payload

    def project_intervals(
        self,
        *,
        target: TargetIdentity,
        parameter_field: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return a deterministic exact-state interval page for one atomic field."""

        exact_target = target_identity(
            target.target_kind,
            nm_id=target.nm_id,
            advert_id=target.advert_id,
            placement=target.placement,
        )
        field = _field(exact_target.target_kind, parameter_field)
        exact_limit = _limit(limit)
        target_binding = canonical_digest(
            {
                "seller_id": self.seller_id,
                "account_scope": self.account_scope,
                "target": _target_payload(exact_target),
                "parameter_field": field,
            }
        )
        after = _decode_projection_cursor(cursor, target_binding) if cursor else None

        with self.store_registry.session(
            "operational",
            mode="ro",
            operation="change_registry_interval_projection",
        ) as conn:
            _require_schema(conn)
            intervals = self._build_projection(conn, exact_target, field)

        if after is not None:
            intervals = [
                item
                for item in intervals
                if (str(item["start_at"]), str(item["interval_id"])) > after
            ]
        page = intervals[:exact_limit]
        next_cursor = ""
        if len(intervals) > exact_limit and page:
            last = page[-1]
            next_cursor = _encode_projection_cursor(
                target_binding,
                str(last["start_at"]),
                str(last["interval_id"]),
            )
        payload = {
            "contract_name": "wb_change_registry_interval_projection",
            "contract_version": 1,
            "seller_id": self.seller_id,
            "account_scope": self.account_scope,
            "target": _target_payload(exact_target),
            "parameter_field": field,
            "items": page,
            "next_cursor": next_cursor,
        }
        payload["projection_digest"] = canonical_digest(payload)
        return payload

    def _build_projection(
        self,
        conn: sqlite3.Connection,
        target: TargetIdentity,
        field: str,
    ) -> list[dict[str, Any]]:
        checkpoints = conn.execute(
            f"""SELECT * FROM {CHECKPOINTS_TABLE}
                WHERE seller_id=? AND account_scope=? AND source_surface=?
                  AND completeness_status='complete'
                ORDER BY completed_at,checkpoint_id""",
            (self.seller_id, self.account_scope, self.source_surface),
        ).fetchall()
        for index, checkpoint in enumerate(checkpoints):
            expected_previous = (
                str(checkpoints[index - 1]["checkpoint_id"]) if index else None
            )
            actual_previous = checkpoint["previous_complete_checkpoint_id"]
            if actual_previous != expected_previous:
                raise ChangeRegistryConflict(
                    "complete checkpoint chronology is ambiguous or non-contiguous"
                )

        fact_rows = conn.execute(
            f"""SELECT * FROM {FACTS_TABLE}
                WHERE seller_id=? AND account_scope=? AND target_kind=? AND nm_id=?
                  AND advert_id=? AND placement=? AND parameter_field=?
                ORDER BY observed_to,fact_id""",
            (
                self.seller_id,
                self.account_scope,
                target.target_kind,
                target.nm_id,
                target.advert_id,
                target.placement,
                field,
            ),
        ).fetchall()
        if any(str(row["proof_kind"]) != "checkpoint_diff" for row in fact_rows):
            raise ChangeRegistryConflict(
                "interval projection rejects non-checkpoint or ambiguous proof"
            )
        facts_by_checkpoint: dict[str, sqlite3.Row] = {}
        checkpoints_by_id = {str(row["checkpoint_id"]): row for row in checkpoints}
        for fact in fact_rows:
            links = conn.execute(
                f"""SELECT * FROM {FACT_LINKS_TABLE}
                    WHERE fact_id=? AND link_kind='checkpoint'
                    ORDER BY fact_link_id""",
                (fact["fact_id"],),
            ).fetchall()
            if len(links) != 1:
                raise ChangeRegistryConflict(
                    "projected fact requires exactly one checkpoint link"
                )
            checkpoint_id = str(links[0]["checkpoint_id"])
            checkpoint = checkpoints_by_id.get(checkpoint_id)
            if checkpoint is None or checkpoint_id in facts_by_checkpoint:
                raise ChangeRegistryConflict(
                    "projected fact checkpoint identity is ambiguous"
                )
            proof_candidates = [
                row
                for row in checkpoints
                if str(row["completed_at"]) == str(fact["observed_from"])
                and str(row["completed_at"]) < str(checkpoint["completed_at"])
            ]
            previous = proof_candidates[0] if len(proof_candidates) == 1 else None
            if (
                previous is None
                or str(fact["observed_to"]) != str(checkpoint["completed_at"])
                or str(fact["proven_at"]) != str(checkpoint["completed_at"])
            ):
                raise ChangeRegistryConflict(
                    "projected fact interval does not match checkpoint chronology"
                )
            current_observation = _load_observation(conn, checkpoint_id, target, field)
            current_value = _comparable_row_value(current_observation)
            after_value = _row_value(fact, "after_value")
            before_value = _row_value(fact, "before_value")
            if current_value is None or current_value != after_value:
                raise ChangeRegistryConflict(
                    "projected fact does not match the linked exact observation"
                )
            previous_observation = _load_observation(
                conn, str(previous["checkpoint_id"]), target, field
            )
            previous_value = _comparable_row_value(previous_observation)
            if before_value == CanonicalValue("text", text_value=CREATION_ABSENT_STATE):
                immediate_previous_id = checkpoint["previous_complete_checkpoint_id"]
                if not (
                    target.target_kind == "campaign"
                    and field == "campaign_state"
                    and immediate_previous_id is not None
                    and str(previous["checkpoint_id"]) == str(immediate_previous_id)
                    and target.advert_id
                    not in _checkpoint_advert_ids(conn, str(previous["checkpoint_id"]))
                ):
                    raise ChangeRegistryConflict(
                        "campaign creation absence proof is not exact"
                    )
            elif previous_value is None or previous_value != before_value:
                raise ChangeRegistryConflict(
                    "projected fact before-value is not proven by previous checkpoint"
                )
            facts_by_checkpoint[checkpoint_id] = fact

        intervals: list[dict[str, Any]] = []
        active_value: CanonicalValue | None = None
        active_start = ""
        active_checkpoint_id = ""
        active_fact_id = ""

        def close_active(end_at: str, end_checkpoint_id: str) -> None:
            nonlocal active_value, active_start, active_checkpoint_id, active_fact_id
            if active_value is None:
                return
            intervals.append(
                _interval_payload(
                    target=target,
                    field=field,
                    value=active_value,
                    start_at=active_start,
                    end_at=end_at,
                    start_checkpoint_id=active_checkpoint_id,
                    end_checkpoint_id=end_checkpoint_id,
                    fact_id=active_fact_id,
                )
            )
            active_value = None
            active_start = ""
            active_checkpoint_id = ""
            active_fact_id = ""

        for index, checkpoint in enumerate(checkpoints):
            checkpoint_id = str(checkpoint["checkpoint_id"])
            completed_at = str(checkpoint["completed_at"])
            observation = _load_observation(conn, checkpoint_id, target, field)
            current_value = _comparable_row_value(observation)
            fact = facts_by_checkpoint.get(checkpoint_id)
            if current_value is None:
                if fact is not None:
                    raise ChangeRegistryConflict(
                        "non-proven checkpoint evidence cannot carry a transition fact"
                    )
                close_active(completed_at, checkpoint_id)
                continue

            if active_value is None:
                if fact is not None:
                    before = _row_value(fact, "before_value")
                    if before == CanonicalValue(
                        "text", text_value=CREATION_ABSENT_STATE
                    ):
                        previous_id = str(checkpoint["previous_complete_checkpoint_id"])
                        previous = checkpoints_by_id[previous_id]
                        absent = CanonicalValue("text", text_value=CREATION_ABSENT_STATE)
                        intervals.append(
                            _interval_payload(
                                target=target,
                                field=field,
                                value=absent,
                                start_at=str(previous["completed_at"]),
                                end_at=completed_at,
                                start_checkpoint_id=previous_id,
                                end_checkpoint_id=checkpoint_id,
                                fact_id=str(fact["fact_id"]),
                            )
                        )
                    active_fact_id = str(fact["fact_id"])
                elif (
                    index
                    and target.target_kind == "campaign"
                    and field == "campaign_state"
                    and target.advert_id
                    not in _checkpoint_advert_ids(
                        conn, str(checkpoints[index - 1]["checkpoint_id"])
                    )
                ):
                    raise ChangeRegistryConflict(
                        "campaign appearance is missing exact creation proof"
                    )
                active_value = current_value
                active_start = completed_at
                active_checkpoint_id = checkpoint_id
                continue

            if current_value == active_value:
                if fact is not None:
                    raise ChangeRegistryConflict(
                        "unchanged exact state cannot carry a transition fact"
                    )
                continue
            if fact is None:
                raise ChangeRegistryConflict(
                    "exact value transition is missing admitted checkpoint proof"
                )
            if (
                _row_value(fact, "before_value") != active_value
                or _row_value(fact, "after_value") != current_value
            ):
                raise ChangeRegistryConflict(
                    "fact chain does not match the projected exact state"
                )
            close_active(completed_at, checkpoint_id)
            active_value = current_value
            active_start = completed_at
            active_checkpoint_id = checkpoint_id
            active_fact_id = str(fact["fact_id"])

        if active_value is not None:
            intervals.append(
                _interval_payload(
                    target=target,
                    field=field,
                    value=active_value,
                    start_at=active_start,
                    end_at=None,
                    start_checkpoint_id=active_checkpoint_id,
                    end_checkpoint_id=None,
                    fact_id=active_fact_id,
                )
            )
        intervals.sort(key=lambda item: (str(item["start_at"]), str(item["interval_id"])))
        return intervals


def _normalize_acquisition(
    acquisition: Mapping[str, Any],
    *,
    expected_seller: str,
    expected_account: str,
) -> _NormalizedAcquisition:
    if not isinstance(acquisition, Mapping):
        raise ChangeRegistryError("acquisition must be a canonical object")
    _assert_sanitized(acquisition)
    manifest_digest = _digest(acquisition.get("manifest_digest"), "manifest_digest")
    unsigned = dict(acquisition)
    unsigned.pop("manifest_digest", None)
    if canonical_digest(unsigned) != manifest_digest:
        raise ChangeRegistryConflict("acquisition manifest digest does not match bytes")
    if (
        acquisition.get("contract_name") != ACQUISITION_CONTRACT_NAME
        or acquisition.get("contract_version") != ACQUISITION_CONTRACT_VERSION
        or acquisition.get("mapping_version") != MAPPING_VERSION
    ):
        raise ChangeRegistryError("unsupported acquisition contract or mapping version")
    seller = acquisition.get("seller")
    if not isinstance(seller, Mapping):
        raise ChangeRegistryError("acquisition seller scope is missing")
    seller_id = _identity(seller.get("seller_id"), "seller_id")
    account_scope = _identity(seller.get("account_scope"), "account_scope")
    if seller_id != expected_seller or account_scope != expected_account:
        raise ChangeRegistryConflict("acquisition seller/account scope differs")
    interval = acquisition.get("interval")
    if not isinstance(interval, Mapping):
        raise ChangeRegistryError("acquisition interval is missing")
    started_at = _utc_timestamp(interval.get("started_at"), "started_at")
    completed_at = _utc_timestamp(interval.get("completed_at"), "completed_at")
    if _moment(started_at) > _moment(completed_at):
        raise ChangeRegistryError("acquisition interval is reversed")
    status = str(acquisition.get("completeness_status") or "").strip().lower()
    if status not in {"complete", "partial", "failed"}:
        raise ChangeRegistryError("unsupported acquisition completeness status")
    joint_complete = acquisition.get("joint_complete")
    if not isinstance(joint_complete, bool) or joint_complete != (status == "complete"):
        raise ChangeRegistryError("joint completeness does not match checkpoint status")
    sources = acquisition.get("sources")
    if not isinstance(sources, Mapping):
        raise ChangeRegistryError("acquisition sources are missing")
    prices = sources.get("prices")
    ads = sources.get("ads")
    if not isinstance(prices, Mapping) or not isinstance(ads, Mapping):
        raise ChangeRegistryError("both Prices and Ads evidence are required")
    _verify_embedded_manifest(prices, "Prices")
    _verify_embedded_manifest(ads, "Ads")
    if (
        prices.get("seller_id") != seller_id
        or prices.get("account_scope") != account_scope
        or ads.get("seller_id") != seller_id
        or ads.get("account_scope") != account_scope
    ):
        raise ChangeRegistryConflict(
            "source manifest seller/account scope differs from joint acquisition"
        )
    price_status = str(prices.get("completeness_status") or "").strip().lower()
    ads_status = str(ads.get("completeness_status") or "").strip().lower()
    if price_status not in {"complete", "partial", "failed"} or ads_status not in {
        "complete",
        "partial",
        "failed",
    }:
        raise ChangeRegistryError("source completeness status is invalid")
    if (price_status == "complete" and ads_status == "complete") != (
        status == "complete"
    ):
        raise ChangeRegistryError(
            "joint checkpoint completeness differs from Prices and Ads"
        )
    persistence = acquisition.get("persistence")
    if not isinstance(persistence, Mapping) or any(
        persistence.get(key) != 0
        for key in (
            "registry_rows_written",
            "checkpoints_written",
            "facts_written",
            "identity_incidents_written",
        )
    ):
        raise ChangeRegistryConflict(
            "baseline input must come from the zero-persistence acquisition seam"
        )

    observation_map: dict[tuple[str, int, int, str, str], _Observation] = {}
    goods = prices.get("goods")
    if not isinstance(goods, list):
        raise ChangeRegistryError("Prices goods evidence must be an array")
    seen_nm_ids: set[int] = set()
    for good in goods:
        if not isinstance(good, Mapping):
            raise ChangeRegistryError("Prices good evidence must be an object")
        nm_id = _positive_int(good.get("nm_id"), "nm_id")
        if nm_id in seen_nm_ids:
            raise ChangeRegistryConflict("duplicate Prices nm_id is ambiguous")
        seen_nm_ids.add(nm_id)
        values = good.get("sku_values")
        if not isinstance(values, Mapping):
            raise ChangeRegistryError("Prices SKU values are missing")
        for field in sorted(FIELD_BY_TARGET["price"]):
            shape = values.get(field)
            observation = _shape_observation(
                target=target_identity("price", nm_id=nm_id),
                parameter_field=field,
                shape=shape,
                fallback_evidence=good.get("record_digest"),
            )
            _merge_observation(observation_map, observation)

    campaigns = ads.get("campaigns")
    if not isinstance(campaigns, list):
        raise ChangeRegistryError("Ads campaigns evidence must be an array")
    incidents: list[_Incident] = []
    seen_advert_ids: set[int] = set()
    for campaign in campaigns:
        if not isinstance(campaign, Mapping):
            raise ChangeRegistryError("Ads campaign evidence must be an object")
        advert_id = _positive_int(campaign.get("advert_id"), "advert_id")
        if advert_id in seen_advert_ids:
            raise ChangeRegistryConflict("duplicate Ads advert_id is ambiguous")
        seen_advert_ids.add(advert_id)
        mapping = campaign.get("mapping")
        if not isinstance(mapping, Mapping):
            raise ChangeRegistryError("campaign mapping evidence is missing")
        candidates_raw = mapping.get("candidate_nm_ids")
        if not isinstance(candidates_raw, list):
            raise ChangeRegistryError("campaign candidate nmIDs must be an array")
        candidates = tuple(sorted({_positive_int(value, "candidate_nm_id") for value in candidates_raw}))
        if mapping.get("candidate_count") != len(candidates):
            raise ChangeRegistryConflict("campaign candidate cardinality is inconsistent")
        exact_nm = mapping.get("exact_nm_id")
        mapping_status = str(mapping.get("status") or "").strip().lower()
        campaign_digest = _digest(
            campaign.get("record_digest"), "campaign record_digest"
        )
        if mapping_status == "exact" and len(candidates) == 1:
            nm_id = _positive_int(exact_nm, "exact_nm_id")
            if nm_id != candidates[0]:
                raise ChangeRegistryConflict("campaign exact nmID differs from candidates")
            campaign_target = target_identity(
                "campaign", nm_id=nm_id, advert_id=advert_id
            )
            for field in sorted(FIELD_BY_TARGET["campaign"]):
                observation = _shape_observation(
                    target=campaign_target,
                    parameter_field=field,
                    shape=campaign.get(field),
                    fallback_evidence=campaign_digest,
                )
                if (
                    field == "campaign_state"
                    and observation.value
                    == CanonicalValue("text", text_value=CREATION_ABSENT_STATE)
                ):
                    raise ChangeRegistryConflict(
                        "source campaign state cannot use reserved absent token"
                    )
                _merge_observation(observation_map, observation)
            bids = campaign.get("bids")
            if not isinstance(bids, list):
                raise ChangeRegistryError("campaign bids evidence must be an array")
            for bid in bids:
                if not isinstance(bid, Mapping):
                    raise ChangeRegistryError("bid evidence must be an object")
                if (
                    _positive_int(bid.get("nm_id"), "bid nm_id") != nm_id
                    or _positive_int(bid.get("advert_id"), "bid advert_id") != advert_id
                ):
                    raise ChangeRegistryConflict("bid exact identity differs from campaign")
                placement = str(bid.get("placement") or "").strip().lower()
                observation = _shape_observation(
                    target=target_identity(
                        "bid",
                        nm_id=nm_id,
                        advert_id=advert_id,
                        placement=placement,
                    ),
                    parameter_field="bid_minor",
                    shape=bid.get("bid_minor"),
                    fallback_evidence=bid.get("target_digest"),
                )
                _merge_observation(observation_map, observation)
        elif mapping_status == "error" and len(candidates) != 1:
            incidents.append(
                _Incident(
                    incident_kind="campaign_nm_mapping_cardinality",
                    advert_id=advert_id,
                    candidate_nm_ids=candidates,
                    evidence_digest=campaign_digest,
                )
            )
        elif mapping_status == "inapplicable" and not candidates:
            incidents.append(
                _Incident(
                    incident_kind="invalid_target_identity",
                    advert_id=advert_id,
                    candidate_nm_ids=(),
                    evidence_digest=campaign_digest,
                )
            )
        else:
            raise ChangeRegistryConflict("campaign mapping evidence is ambiguous")

    observations = tuple(sorted(observation_map.values(), key=lambda item: item.key))
    incidents_tuple = tuple(
        sorted(
            incidents,
            key=lambda item: (
                item.advert_id,
                item.incident_kind,
                item.candidate_nm_ids,
                item.evidence_digest,
            ),
        )
    )
    ads_counts = ads.get("counts") if isinstance(ads.get("counts"), Mapping) else {}
    declared_price = _non_negative_count(
        (prices.get("counts") or {}).get("goods")
        if isinstance(prices.get("counts"), Mapping)
        else len(goods),
        "price goods count",
        fallback=len(goods),
    )
    declared_ads = _non_negative_count(
        ads_counts.get("manifest_campaigns"),
        "ads manifest count",
        fallback=len(campaigns),
    )
    declared_bids = _non_negative_count(
        ads_counts.get("bids"), "ads bid count", fallback=0
    )
    observed_units = len(goods) + len(campaigns) + sum(
        len(campaign.get("bids") or [])
        for campaign in campaigns
        if isinstance(campaign, Mapping)
    )
    expected_units = max(observed_units, declared_price + declared_ads + declared_bids)
    if status == "complete":
        if (
            declared_price != len(goods)
            or declared_ads != len(campaigns)
            or declared_bids
            != sum(
                len(campaign.get("bids") or [])
                for campaign in campaigns
                if isinstance(campaign, Mapping)
            )
        ):
            raise ChangeRegistryConflict(
                "complete source counts differ from normalized target evidence"
            )
        expected_units = observed_units
    completeness_basis = {
        "status": status,
        "prices_manifest_digest": _digest(
            prices.get("manifest_digest"), "prices manifest_digest"
        ),
        "ads_manifest_digest": _digest(
            ads.get("manifest_digest"), "ads manifest_digest"
        ),
        "expected_target_count": expected_units,
        "observed_target_count": observed_units,
        "observation_count": len(observations),
        "identity_incident_count": len(incidents_tuple),
    }
    return _NormalizedAcquisition(
        seller_id=seller_id,
        account_scope=account_scope,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        manifest_digest=manifest_digest,
        observations=observations,
        incidents=incidents_tuple,
        expected_target_count=expected_units,
        observed_target_count=min(expected_units, observed_units),
        completeness_digest=canonical_digest(completeness_basis),
    )


def _shape_observation(
    *,
    target: TargetIdentity,
    parameter_field: str,
    shape: Any,
    fallback_evidence: Any,
) -> _Observation:
    field = _field(target.target_kind, parameter_field)
    if not isinstance(shape, Mapping):
        raise ChangeRegistryError(f"{field} observation shape is missing")
    status = str(shape.get("status") or "").strip().lower()
    if status not in {"exact", "exact_zero", "missing", "inapplicable", "error"}:
        raise ChangeRegistryError(f"{field} observation status is invalid")
    value_shape = shape.get("value")
    if not isinstance(value_shape, Mapping):
        raise ChangeRegistryError(f"{field} observation value is missing")
    kind = str(value_shape.get("kind") or "").strip().lower()
    integer_value = value_shape.get("integer_value")
    text_value = value_shape.get("text_value")
    if kind == "missing":
        value = canonicalize_value(MISSING)
    elif kind == "null":
        if integer_value is not None or text_value is not None:
            raise ChangeRegistryError("null observation carries a scalar")
        value = canonicalize_value(None)
    elif kind == "integer":
        if isinstance(integer_value, bool) or not isinstance(integer_value, int):
            raise ChangeRegistryError("integer observation is invalid")
        value = canonicalize_value(integer_value)
    elif kind == "boolean":
        if integer_value not in (0, 1) or isinstance(integer_value, bool):
            raise ChangeRegistryError("boolean observation is invalid")
        value = canonicalize_value(bool(integer_value))
    elif kind == "text":
        if not isinstance(text_value, str):
            raise ChangeRegistryError("text observation is invalid")
        value = canonicalize_value(text_value.strip().lower() if field in TEXT_FIELDS else text_value)
    else:
        raise ChangeRegistryError("observation value kind is invalid")
    _validate_value(field, value)
    if status == "exact_zero" and value != CanonicalValue("integer", integer_value=0):
        raise ChangeRegistryError("exact_zero requires integer zero")
    if status == "exact" and value.kind == "missing":
        raise ChangeRegistryError("exact observation cannot be missing")
    if status in {"missing", "inapplicable", "error"} and value.kind != "missing":
        raise ChangeRegistryError(f"{status} observation must keep missing value")
    reason = str(shape.get("reason") or "").strip().lower()
    health_code = _health_code(reason or ("ok" if status in {"exact", "exact_zero"} else status))
    fallback = _digest(fallback_evidence, f"{field} evidence digest")
    evidence_digest = canonical_digest(
        {
            "target": _target_payload(target),
            "parameter_field": field,
            "status": status,
            "value": _value_payload(value),
            "health_code": health_code,
            "source_evidence_digest": fallback,
        }
    )
    return _Observation(
        target=target,
        parameter_field=field,
        status=status,
        value=value,
        health_code=health_code,
        evidence_digest=evidence_digest,
    )


def _merge_observation(
    target: dict[tuple[str, int, int, str, str], _Observation],
    observation: _Observation,
) -> None:
    existing = target.get(observation.key)
    if existing is None:
        target[observation.key] = observation
        return
    if existing == observation:
        return
    conflict_digest = canonical_digest(
        {
            "existing": existing.evidence_digest,
            "incoming": observation.evidence_digest,
            "target": observation.key,
        }
    )
    target[observation.key] = _Observation(
        target=observation.target,
        parameter_field=observation.parameter_field,
        status="error",
        value=CanonicalValue("missing"),
        health_code="ambiguous_duplicate_observation",
        evidence_digest=conflict_digest,
    )


def _observation_row(
    checkpoint_id: str, observed_at: str, observation: _Observation
) -> dict[str, Any]:
    basis = {
        "checkpoint_id": checkpoint_id,
        "target": _target_payload(observation.target),
        "parameter_field": observation.parameter_field,
    }
    return {
        "observation_value_id": _stable_id("crobs", basis),
        "checkpoint_id": checkpoint_id,
        "target_kind": observation.target.target_kind,
        "nm_id": observation.target.nm_id,
        "advert_id": observation.target.advert_id,
        "placement": observation.target.placement,
        "parameter_field": observation.parameter_field,
        "observation_status": observation.status,
        **_value_columns("value", observation.value),
        "health_code": observation.health_code,
        "health_detail": "",
        "observed_at": observed_at,
        "evidence_digest": observation.evidence_digest,
        "mapping_version": MAPPING_VERSION,
    }


def _incident_row(
    *,
    checkpoint_id: str,
    seller_id: str,
    account_scope: str,
    source_surface: str,
    observed_at: str,
    incident: _Incident,
) -> dict[str, Any]:
    basis = {
        "checkpoint_id": checkpoint_id,
        "incident_kind": incident.incident_kind,
        "advert_id": incident.advert_id,
        "candidate_nm_ids": list(incident.candidate_nm_ids),
        "source_evidence_digest": incident.evidence_digest,
    }
    return {
        "incident_id": _stable_id("crii", basis),
        "seller_id": seller_id,
        "account_scope": account_scope,
        "incident_kind": incident.incident_kind,
        "target_kind": "campaign",
        "advert_id": incident.advert_id,
        "candidate_nm_ids_json": canonical_json(list(incident.candidate_nm_ids)),
        "candidate_count": len(incident.candidate_nm_ids),
        "source_surface": source_surface,
        "observed_at": observed_at,
        "evidence_digest": canonical_digest(basis),
        "mapping_version": MAPPING_VERSION,
    }


def _validate_existing_checkpoint(
    checkpoint: sqlite3.Row,
    normalized: _NormalizedAcquisition,
    source_surface: str,
) -> None:
    expected = {
        "seller_id": normalized.seller_id,
        "account_scope": normalized.account_scope,
        "source_surface": source_surface,
        "scan_kind": "observer",
        "started_at": normalized.started_at,
        "completed_at": normalized.completed_at,
        "completeness_status": normalized.status,
        "expected_target_count": normalized.expected_target_count,
        "observed_target_count": normalized.observed_target_count,
        "completeness_digest": normalized.completeness_digest,
        "evidence_digest": normalized.manifest_digest,
        "mapping_version": MAPPING_VERSION,
    }
    if any(checkpoint[key] != value for key, value in expected.items()):
        raise ChangeRegistryConflict(
            "checkpoint identity owns different immutable acquisition bytes"
        )


def _checkpoint_advert_ids(conn: sqlite3.Connection, checkpoint_id: str) -> set[int]:
    ids = {
        int(row[0])
        for row in conn.execute(
            f"""SELECT DISTINCT advert_id FROM {OBSERVATION_VALUES_TABLE}
                WHERE checkpoint_id=? AND target_kind IN ('campaign','bid')
                  AND advert_id>0""",
            (checkpoint_id,),
        ).fetchall()
    }
    ids.update(
        int(row[0])
        for row in conn.execute(
            f"""SELECT advert_id FROM {IDENTITY_INCIDENTS_TABLE}
                WHERE seller_id=(SELECT seller_id FROM {CHECKPOINTS_TABLE} WHERE checkpoint_id=?)
                  AND account_scope=(SELECT account_scope FROM {CHECKPOINTS_TABLE} WHERE checkpoint_id=?)
                  AND source_surface=? AND advert_id>0""",
            (checkpoint_id, checkpoint_id, _incident_surface(checkpoint_id)),
        ).fetchall()
    )
    return ids


def _load_observation(
    conn: sqlite3.Connection,
    checkpoint_id: str,
    target: TargetIdentity,
    field: str,
) -> sqlite3.Row | None:
    return conn.execute(
        f"""SELECT * FROM {OBSERVATION_VALUES_TABLE}
            WHERE checkpoint_id=? AND target_kind=? AND nm_id=? AND advert_id=?
              AND placement=? AND parameter_field=?""",
        (
            checkpoint_id,
            target.target_kind,
            target.nm_id,
            target.advert_id,
            target.placement,
            field,
        ),
    ).fetchone()


def _comparable_row_value(row: Mapping[str, Any] | None) -> CanonicalValue | None:
    if row is None or str(row["observation_status"]) not in {"exact", "exact_zero"}:
        return None
    value = _row_value(row, "value")
    return value if value.kind in COMPARABLE_KINDS else None


def _row_value(row: Mapping[str, Any], prefix: str) -> CanonicalValue:
    return CanonicalValue(
        str(row[f"{prefix}_kind"]),
        integer_value=(
            int(row[f"{prefix}_integer"])
            if row[f"{prefix}_integer"] is not None
            else None
        ),
        text_value=(
            str(row[f"{prefix}_text"])
            if row[f"{prefix}_text"] is not None
            else None
        ),
    )


def _row_key(row: Mapping[str, Any]) -> tuple[str, int, int, str, str]:
    return (
        str(row["target_kind"]),
        int(row["nm_id"]),
        int(row["advert_id"]),
        str(row["placement"]),
        str(row["parameter_field"]),
    )


def _interval_payload(
    *,
    target: TargetIdentity,
    field: str,
    value: CanonicalValue,
    start_at: str,
    end_at: str | None,
    start_checkpoint_id: str,
    end_checkpoint_id: str | None,
    fact_id: str,
) -> dict[str, Any]:
    basis = {
        "target": _target_payload(target),
        "parameter_field": field,
        "value": _value_payload(value),
        "start_at": start_at,
        "end_at": end_at,
        "start_checkpoint_id": start_checkpoint_id,
        "end_checkpoint_id": end_checkpoint_id,
        "fact_id": fact_id,
    }
    return {"interval_id": _stable_id("crip", basis), **basis}


def _insert_idempotent(
    conn: sqlite3.Connection,
    table: str,
    identity_column: str,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    columns = tuple(row)
    try:
        conn.execute(
            f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(row[column] for column in columns),
        )
    except sqlite3.IntegrityError as exc:
        existing = conn.execute(
            f"SELECT * FROM {table} WHERE {identity_column}=?",
            (row[identity_column],),
        ).fetchone()
        if existing is not None and all(existing[key] == value for key, value in row.items()):
            return dict(existing)
        raise ChangeRegistryConflict(
            f"{table} immutable identity or idempotency conflict"
        ) from exc
    stored = conn.execute(
        f"SELECT * FROM {table} WHERE {identity_column}=?",
        (row[identity_column],),
    ).fetchone()
    if stored is None:
        raise ChangeRegistryConflict(f"{table} insert readback is missing")
    return dict(stored)


def _require_schema(conn: sqlite3.Connection) -> None:
    required = {
        CHECKPOINTS_TABLE,
        OBSERVATION_VALUES_TABLE,
        FACTS_TABLE,
        FACT_LINKS_TABLE,
        IDENTITY_INCIDENTS_TABLE,
    }
    actual = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = sorted(required - actual)
    if missing:
        raise ChangeRegistryError(
            "change registry foundation is not initialized: " + ",".join(missing)
        )


def _field(target_kind: str, parameter_field: Any) -> str:
    field = str(parameter_field or "").strip().lower()
    if field not in FIELD_BY_TARGET[target_kind]:
        raise ChangeRegistryError("parameter field does not match target kind")
    return field


def _validate_value(field: str, value: CanonicalValue) -> None:
    if field in NUMERIC_FIELDS:
        if value.kind not in {"missing", "null", "integer"}:
            raise ChangeRegistryError(f"{field} must use an integer value")
        if value.kind == "integer":
            exact = int(value.integer_value or 0)
            if exact < 0 or (field == "discount_bps" and exact > 10_000):
                raise ChangeRegistryError(f"{field} is outside its exact range")
    elif value.kind not in {"missing", "null", "text"}:
        raise ChangeRegistryError(f"{field} must use a canonical text value")
    elif value.kind == "text":
        text = str(value.text_value or "")
        if re.fullmatch(r"[a-z0-9][a-z0-9_:-]{0,119}", text) is None:
            raise ChangeRegistryError(f"{field} text is not canonical")


def _value_columns(prefix: str, value: CanonicalValue) -> dict[str, Any]:
    return {
        f"{prefix}_kind": value.kind,
        f"{prefix}_integer": value.integer_value,
        f"{prefix}_text": value.text_value,
    }


def _value_payload(value: CanonicalValue) -> dict[str, Any]:
    return {
        "kind": value.kind,
        "integer_value": value.integer_value,
        "text_value": value.text_value,
    }


def _target_payload(target: TargetIdentity) -> dict[str, Any]:
    return {
        "target_kind": target.target_kind,
        "nm_id": target.nm_id,
        "advert_id": target.advert_id,
        "placement": target.placement,
    }


def _stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _incident_surface(checkpoint_id: str) -> str:
    return f"checkpoint:{checkpoint_id}"


def _identity(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if _IDENTITY.fullmatch(text) is None:
        raise ChangeRegistryError(f"{name} is invalid")
    return text


def _digest(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if _DIGEST.fullmatch(text) is None:
        raise ChangeRegistryError(f"{name} must be a lowercase sha256 digest")
    return text


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ChangeRegistryError(f"{name} must be a positive integer")
    return value


def _non_negative_count(value: Any, name: str, *, fallback: int) -> int:
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChangeRegistryError(f"{name} must be a non-negative integer")
    return value


def _health_code(value: str) -> str:
    token = value.strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_:-]{0,119}", token) is None:
        raise ChangeRegistryError("health code is invalid")
    return token


def _assert_sanitized(value: Any, path: tuple[str, ...] = ()) -> None:
    forbidden = {
        "authorization",
        "cookie",
        "cookies",
        "password",
        "raw_payload",
        "request_body",
        "response_body",
        "secret",
        "token",
    }
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key).strip().casefold()
            if name in forbidden:
                raise ChangeRegistryConflict(
                    "acquisition contains forbidden raw or credential material"
                )
            _assert_sanitized(child, (*path, name))
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_sanitized(child, path)


def _verify_embedded_manifest(source: Mapping[str, Any], name: str) -> None:
    digest = _digest(source.get("manifest_digest"), f"{name} manifest_digest")
    unsigned = dict(source)
    unsigned.pop("manifest_digest", None)
    if canonical_digest(unsigned) != digest:
        raise ChangeRegistryConflict(f"{name} manifest digest does not match bytes")


def _utc_timestamp(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text or "T" not in text:
        raise ChangeRegistryError(f"{name} must be an ISO-8601 timestamp")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ChangeRegistryError(f"{name} is invalid") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ChangeRegistryError(f"{name} requires an explicit timezone")
    utc = moment.astimezone(timezone.utc)
    rendered = utc.isoformat().replace("+00:00", "Z")
    return rendered


def _moment(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
        raise ChangeRegistryError("limit must be an integer between 1 and 200")
    return value


def _encode_projection_cursor(binding: str, start_at: str, interval_id: str) -> str:
    payload = canonical_json(
        {
            "version": 1,
            "entity": "change_registry_interval",
            "binding": binding,
            "start_at": start_at,
            "interval_id": interval_id,
        }
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_projection_cursor(cursor: str, binding: str) -> tuple[str, str]:
    text = str(cursor or "").strip()
    if not text or len(text) > 2048:
        raise ChangeRegistryError("projection cursor is invalid")
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChangeRegistryError("projection cursor is invalid") from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("version") != 1
        or payload.get("entity") != "change_registry_interval"
        or payload.get("binding") != binding
    ):
        raise ChangeRegistryError("projection cursor does not match target/field")
    return (
        _utc_timestamp(payload.get("start_at"), "cursor start_at"),
        _identity(payload.get("interval_id"), "cursor interval_id"),
    )


__all__ = [
    "CONTRACT_NAME",
    "CONTRACT_VERSION",
    "CREATION_ABSENT_STATE",
    "ChangeRegistryBaselineEngine",
]
