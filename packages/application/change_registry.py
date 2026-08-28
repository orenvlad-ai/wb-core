"""Append-only storage foundation for the seller change registry.

The module owns only the operational SQLite contract and internal repository
primitives.  Writer-facing atomic lifecycle methods remain storage-only: this
module does not call WB, expose an HTTP route, project outcomes, or import
legacy Prices/Ads/SKU audit evidence.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Mapping, Sequence

from packages.application.storage_registry import StoreRegistry


CONTRACT_NAME = "wb_change_registry_foundation"
CONTRACT_VERSION = 1
MAPPING_VERSION = "wb_change_registry_mapping_v1"

OPERATIONS_TABLE = "change_registry_operations"
ITEMS_TABLE = "change_registry_items"
ATTEMPT_EVENTS_TABLE = "change_registry_attempt_events"
FACTS_TABLE = "change_registry_facts"
FACT_LINKS_TABLE = "change_registry_fact_links"
CHECKPOINTS_TABLE = "change_registry_checkpoints"
OBSERVATION_VALUES_TABLE = "change_registry_observation_values"
IDENTITY_INCIDENTS_TABLE = "change_registry_identity_incidents"
ANNOTATION_REVISIONS_TABLE = "change_registry_annotation_revisions"
MANUAL_PENDING_EVENTS_TABLE = "change_registry_manual_pending_events"
MANUAL_PENDING_CURRENT_TABLE = "change_registry_manual_pending_current"
CHECKPOINT_SOURCE_MANIFESTS_TABLE = "change_registry_checkpoint_source_manifests"
OBSERVER_JOBS_TABLE = "change_registry_observer_jobs"
OBSERVER_JOB_EVENTS_TABLE = "change_registry_observer_job_events"
OBSERVER_HEALTH_EVENTS_TABLE = "change_registry_observer_health_events"
OBSERVER_LEASES_TABLE = "change_registry_observer_leases"

IMMUTABLE_TABLES = (
    OPERATIONS_TABLE,
    ITEMS_TABLE,
    ATTEMPT_EVENTS_TABLE,
    FACTS_TABLE,
    FACT_LINKS_TABLE,
    CHECKPOINTS_TABLE,
    OBSERVATION_VALUES_TABLE,
    IDENTITY_INCIDENTS_TABLE,
    ANNOTATION_REVISIONS_TABLE,
    MANUAL_PENDING_EVENTS_TABLE,
    CHECKPOINT_SOURCE_MANIFESTS_TABLE,
    OBSERVER_JOBS_TABLE,
    OBSERVER_JOB_EVENTS_TABLE,
    OBSERVER_HEALTH_EVENTS_TABLE,
)

TARGET_KINDS = frozenset({"price", "bid", "campaign"})
PRICE_FIELDS = frozenset(
    {"original_price_minor", "discount_bps", "seller_price_minor"}
)
BID_FIELDS = frozenset({"bid_minor"})
CAMPAIGN_FIELDS = frozenset(
    {"campaign_state", "payment_model", "payment_unit"}
)
NUMERIC_FIELDS = PRICE_FIELDS | BID_FIELDS
TEXT_FIELDS = CAMPAIGN_FIELDS
PLACEMENTS = frozenset({"combined", "search", "recommendations"})
VALUE_KINDS = frozenset({"missing", "null", "integer", "text", "boolean"})
ATTEMPT_STATES = frozenset(
    {
        "created",
        "submitted",
        "confirmed",
        "failed",
        "rejected",
        "cancelled",
        "ambiguous",
        "resolved",
    }
)
MANUAL_PENDING_STATES = frozenset(
    {"pending", "superseded", "matched", "deviated", "expired"}
)
OBSERVATION_STATUSES = frozenset(
    {"exact", "exact_zero", "missing", "inapplicable", "error"}
)
PROOF_KINDS = frozenset(
    {"wb_readback", "native_audit", "checkpoint_diff", "reconciliation"}
)


class ChangeRegistryError(ValueError):
    """Base error for fail-closed registry validation."""


class ChangeRegistryConflict(ChangeRegistryError):
    """An immutable identity or idempotency key already owns other bytes."""


class ChangeRegistryNotFound(ChangeRegistryError):
    """A referenced canonical entity does not exist."""


class _MissingValue:
    __slots__ = ()


MISSING = _MissingValue()


@dataclass(frozen=True)
class CanonicalValue:
    kind: str
    integer_value: int | None = None
    text_value: str | None = None

    def columns(self, prefix: str) -> dict[str, Any]:
        return {
            f"{prefix}_kind": self.kind,
            f"{prefix}_integer": self.integer_value,
            f"{prefix}_text": self.text_value,
        }


@dataclass(frozen=True)
class TargetIdentity:
    target_kind: str
    nm_id: int
    advert_id: int = 0
    placement: str = ""


def canonical_json(value: Any) -> str:
    """Return deterministic JSON without accepting non-finite numbers."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ChangeRegistryError("value is not canonical JSON") from exc


def canonical_digest(value: Any) -> str:
    payload = value if isinstance(value, str) else canonical_json(value)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonicalize_value(value: Any) -> CanonicalValue:
    """Canonicalize one scalar while keeping missing, null and zero distinct."""

    if value is MISSING:
        return CanonicalValue("missing")
    if value is None:
        return CanonicalValue("null")
    if isinstance(value, bool):
        return CanonicalValue("boolean", integer_value=int(value))
    if isinstance(value, int):
        if not -(2**63) <= value <= 2**63 - 1:
            raise ChangeRegistryError("integer value is outside SQLite INTEGER range")
        return CanonicalValue("integer", integer_value=value)
    if isinstance(value, str):
        if "\x00" in value or len(value) > 512:
            raise ChangeRegistryError("text value is invalid or too long")
        return CanonicalValue("text", text_value=value)
    raise ChangeRegistryError(
        "canonical values accept only missing, null, boolean, integer or text"
    )


def target_identity(
    target_kind: str,
    *,
    nm_id: int,
    advert_id: int = 0,
    placement: str = "",
) -> TargetIdentity:
    kind = _required_token(target_kind, "target_kind", TARGET_KINDS)
    exact_nm_id = _positive_integer(nm_id, "nm_id")
    exact_advert_id = _non_negative_integer(advert_id, "advert_id")
    exact_placement = str(placement or "").strip().lower()
    if kind == "price":
        if exact_advert_id != 0 or exact_placement:
            raise ChangeRegistryError(
                "price identity is seller_id + nm_id + field only"
            )
    elif kind == "bid":
        if exact_advert_id <= 0 or exact_placement not in PLACEMENTS:
            raise ChangeRegistryError(
                "bid identity requires advert_id and exact normalized placement"
            )
    elif exact_advert_id <= 0 or exact_placement:
        raise ChangeRegistryError(
            "campaign identity requires advert_id and exactly one nm_id"
        )
    return TargetIdentity(
        target_kind=kind,
        nm_id=exact_nm_id,
        advert_id=exact_advert_id,
        placement=exact_placement,
    )


def ensure_change_registry_schema(conn: sqlite3.Connection) -> None:
    """Install the empty additive foundation in the selected operational DB."""

    value_check_before = _value_storage_check("before_value")
    value_check_requested = _value_storage_check("requested_value")
    value_check_after = _value_storage_check("after_value")
    value_check_observed = _value_storage_check("value")
    target_check = _target_storage_check()
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {OPERATIONS_TABLE}(
            operation_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            source_surface TEXT NOT NULL,
            actor_principal TEXT NOT NULL,
            actor_kind TEXT NOT NULL
                CHECK(actor_kind IN ('human','service','system','import')),
            requested_at TEXT NOT NULL
                CHECK(substr(requested_at,-1,1)='Z' AND julianday(requested_at) IS NOT NULL),
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            native_idempotency_key TEXT NOT NULL DEFAULT '',
            correlation_id TEXT NOT NULL DEFAULT '',
            calculation_id TEXT NOT NULL DEFAULT '',
            apply_operation_id TEXT NOT NULL DEFAULT '',
            provenance_digest TEXT NOT NULL CHECK({_digest_check('provenance_digest')}),
            mapping_version TEXT NOT NULL CHECK(mapping_version='{MAPPING_VERSION}'),
            CHECK({_identity_text_check('operation_id', 120)}),
            CHECK({_identity_text_check('seller_id', 120)}),
            CHECK({_identity_text_check('account_scope', 120)}),
            CHECK({_identity_text_check('source_surface', 120)}),
            CHECK({_identity_text_check('actor_principal', 160)}),
            CHECK(julianday(requested_at)<=julianday(created_at)),
            UNIQUE(operation_id,seller_id,account_scope)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS change_registry_operations_native_idempotency
        ON {OPERATIONS_TABLE}(seller_id,account_scope,source_surface,native_idempotency_key)
        WHERE native_idempotency_key<>'';
        CREATE INDEX IF NOT EXISTS change_registry_operations_by_scope_time
        ON {OPERATIONS_TABLE}(seller_id,account_scope,created_at,operation_id);

        CREATE TABLE IF NOT EXISTS {ITEMS_TABLE}(
            change_item_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            target_kind TEXT NOT NULL CHECK(target_kind IN ('price','bid','campaign')),
            nm_id INTEGER NOT NULL,
            advert_id INTEGER NOT NULL DEFAULT 0,
            placement TEXT NOT NULL DEFAULT '',
            parameter_field TEXT NOT NULL CHECK(parameter_field IN (
                'original_price_minor','discount_bps','seller_price_minor',
                'bid_minor','campaign_state','payment_model','payment_unit'
            )),
            before_value_kind TEXT NOT NULL,
            before_value_integer INTEGER,
            before_value_text TEXT,
            requested_value_kind TEXT NOT NULL,
            requested_value_integer INTEGER,
            requested_value_text TEXT,
            recommendation_item_id TEXT NOT NULL DEFAULT '',
            mapping_version TEXT NOT NULL CHECK(mapping_version='{MAPPING_VERSION}'),
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            FOREIGN KEY(operation_id,seller_id,account_scope)
                REFERENCES {OPERATIONS_TABLE}(operation_id,seller_id,account_scope),
            CHECK({_identity_text_check('change_item_id', 120)}),
            CHECK({target_check}),
            CHECK({value_check_before}),
            CHECK({value_check_requested}),
            CHECK({_field_value_check('before_value', requested=False)}),
            CHECK({_field_value_check('requested_value', requested=True)}),
            UNIQUE(operation_id,target_kind,nm_id,advert_id,placement,parameter_field)
        );
        CREATE INDEX IF NOT EXISTS change_registry_items_by_target
        ON {ITEMS_TABLE}(
            seller_id,account_scope,target_kind,nm_id,advert_id,placement,
            parameter_field,created_at,change_item_id
        );

        CREATE TABLE IF NOT EXISTS {ATTEMPT_EVENTS_TABLE}(
            attempt_event_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL,
            change_item_id TEXT NOT NULL REFERENCES {ITEMS_TABLE}(change_item_id),
            sequence_no INTEGER NOT NULL
                CHECK(typeof(sequence_no)='integer' AND sequence_no>0),
            state TEXT NOT NULL CHECK(state IN (
                'created','submitted','confirmed','failed','rejected','cancelled',
                'ambiguous','resolved'
            )),
            resolution_state TEXT NOT NULL DEFAULT '' CHECK(
                (state='resolved' AND resolution_state IN
                    ('confirmed','failed','rejected','cancelled'))
                OR (state<>'resolved' AND resolution_state='')
            ),
            occurred_at TEXT NOT NULL
                CHECK(substr(occurred_at,-1,1)='Z' AND julianday(occurred_at) IS NOT NULL),
            receipt_reference TEXT NOT NULL DEFAULT '',
            receipt_digest TEXT NOT NULL DEFAULT '' CHECK(
                receipt_digest='' OR {_digest_check('receipt_digest')}
            ),
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            readback_proof_kind TEXT NOT NULL DEFAULT '',
            readback_digest TEXT NOT NULL DEFAULT '' CHECK(
                readback_digest='' OR {_digest_check('readback_digest')}
            ),
            native_event_key TEXT NOT NULL DEFAULT '',
            CHECK({_identity_text_check('attempt_event_id', 120)}),
            CHECK({_identity_text_check('attempt_id', 120)}),
            CHECK(length(error_message)<=800),
            UNIQUE(attempt_id,sequence_no)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS change_registry_attempt_events_native_key
        ON {ATTEMPT_EVENTS_TABLE}(change_item_id,native_event_key)
        WHERE native_event_key<>'';
        CREATE INDEX IF NOT EXISTS change_registry_attempt_events_by_item
        ON {ATTEMPT_EVENTS_TABLE}(change_item_id,occurred_at,attempt_id,sequence_no);

        CREATE TABLE IF NOT EXISTS {CHECKPOINTS_TABLE}(
            checkpoint_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            source_surface TEXT NOT NULL,
            scan_kind TEXT NOT NULL
                CHECK(scan_kind IN ('observer','readback','reconciliation','manual')),
            started_at TEXT NOT NULL
                CHECK(substr(started_at,-1,1)='Z' AND julianday(started_at) IS NOT NULL),
            completed_at TEXT NOT NULL
                CHECK(substr(completed_at,-1,1)='Z' AND julianday(completed_at) IS NOT NULL),
            completeness_status TEXT NOT NULL
                CHECK(completeness_status IN ('complete','partial','failed')),
            expected_target_count INTEGER NOT NULL CHECK(
                typeof(expected_target_count)='integer' AND expected_target_count>=0
            ),
            observed_target_count INTEGER NOT NULL CHECK(
                typeof(observed_target_count)='integer' AND observed_target_count>=0
                AND observed_target_count<=expected_target_count
            ),
            completeness_digest TEXT NOT NULL CHECK({_digest_check('completeness_digest')}),
            evidence_digest TEXT NOT NULL CHECK({_digest_check('evidence_digest')}),
            previous_complete_checkpoint_id TEXT
                REFERENCES {CHECKPOINTS_TABLE}(checkpoint_id),
            mapping_version TEXT NOT NULL CHECK(mapping_version='{MAPPING_VERSION}'),
            CHECK({_identity_text_check('checkpoint_id', 120)}),
            CHECK({_identity_text_check('seller_id', 120)}),
            CHECK({_identity_text_check('account_scope', 120)}),
            CHECK(completeness_status<>'complete'
                OR observed_target_count=expected_target_count),
            CHECK(julianday(started_at)<=julianday(completed_at)),
            UNIQUE(seller_id,account_scope,source_surface,completed_at,evidence_digest)
        );
        CREATE INDEX IF NOT EXISTS change_registry_checkpoints_by_scope_time
        ON {CHECKPOINTS_TABLE}(
            seller_id,account_scope,source_surface,completed_at,checkpoint_id
        );

        CREATE TABLE IF NOT EXISTS {FACTS_TABLE}(
            fact_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            target_kind TEXT NOT NULL CHECK(target_kind IN ('price','bid','campaign')),
            nm_id INTEGER NOT NULL,
            advert_id INTEGER NOT NULL DEFAULT 0,
            placement TEXT NOT NULL DEFAULT '',
            parameter_field TEXT NOT NULL CHECK(parameter_field IN (
                'original_price_minor','discount_bps','seller_price_minor',
                'bid_minor','campaign_state','payment_model','payment_unit'
            )),
            before_value_kind TEXT NOT NULL,
            before_value_integer INTEGER,
            before_value_text TEXT,
            after_value_kind TEXT NOT NULL,
            after_value_integer INTEGER,
            after_value_text TEXT,
            observed_from TEXT NOT NULL
                CHECK(substr(observed_from,-1,1)='Z' AND julianday(observed_from) IS NOT NULL),
            observed_to TEXT NOT NULL
                CHECK(substr(observed_to,-1,1)='Z' AND julianday(observed_to) IS NOT NULL),
            proven_at TEXT NOT NULL
                CHECK(substr(proven_at,-1,1)='Z' AND julianday(proven_at) IS NOT NULL),
            proof_kind TEXT NOT NULL CHECK(
                proof_kind IN ('wb_readback','native_audit','checkpoint_diff','reconciliation')
            ),
            evidence_digest TEXT NOT NULL CHECK({_digest_check('evidence_digest')}),
            mapping_version TEXT NOT NULL CHECK(mapping_version='{MAPPING_VERSION}'),
            CHECK({_identity_text_check('fact_id', 120)}),
            CHECK({_identity_text_check('seller_id', 120)}),
            CHECK({_identity_text_check('account_scope', 120)}),
            CHECK({target_check}),
            CHECK({value_check_before}),
            CHECK({value_check_after}),
            CHECK({_field_value_check('before_value', requested=False)}),
            CHECK({_field_value_check('after_value', requested=True)}),
            CHECK(before_value_kind<>after_value_kind
                OR before_value_integer IS NOT after_value_integer
                OR before_value_text IS NOT after_value_text),
            CHECK(julianday(observed_from)<=julianday(observed_to)),
            CHECK(julianday(observed_to)<=julianday(proven_at)),
            UNIQUE(
                seller_id,account_scope,target_kind,nm_id,advert_id,placement,
                parameter_field,observed_from,observed_to,proof_kind,evidence_digest
            )
        );
        CREATE INDEX IF NOT EXISTS change_registry_facts_by_target_interval
        ON {FACTS_TABLE}(
            seller_id,account_scope,target_kind,nm_id,advert_id,placement,
            parameter_field,observed_from,observed_to,fact_id
        );
        CREATE INDEX IF NOT EXISTS change_registry_facts_by_proven_time
        ON {FACTS_TABLE}(seller_id,account_scope,proven_at,fact_id);

        CREATE TABLE IF NOT EXISTS {OBSERVATION_VALUES_TABLE}(
            observation_value_id TEXT PRIMARY KEY,
            checkpoint_id TEXT NOT NULL REFERENCES {CHECKPOINTS_TABLE}(checkpoint_id),
            target_kind TEXT NOT NULL CHECK(target_kind IN ('price','bid','campaign')),
            nm_id INTEGER NOT NULL,
            advert_id INTEGER NOT NULL DEFAULT 0,
            placement TEXT NOT NULL DEFAULT '',
            parameter_field TEXT NOT NULL CHECK(parameter_field IN (
                'original_price_minor','discount_bps','seller_price_minor',
                'bid_minor','campaign_state','payment_model','payment_unit'
            )),
            observation_status TEXT NOT NULL CHECK(observation_status IN (
                'exact','exact_zero','missing','inapplicable','error'
            )),
            value_kind TEXT NOT NULL,
            value_integer INTEGER,
            value_text TEXT,
            health_code TEXT NOT NULL DEFAULT '',
            health_detail TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL
                CHECK(substr(observed_at,-1,1)='Z' AND julianday(observed_at) IS NOT NULL),
            evidence_digest TEXT NOT NULL CHECK({_digest_check('evidence_digest')}),
            mapping_version TEXT NOT NULL CHECK(mapping_version='{MAPPING_VERSION}'),
            CHECK({_identity_text_check('observation_value_id', 120)}),
            CHECK({target_check}),
            CHECK({value_check_observed}),
            CHECK({_field_value_check('value', requested=False)}),
            CHECK(
                (observation_status='exact' AND value_kind<>'missing')
                OR (observation_status='exact_zero' AND value_kind='integer'
                    AND value_integer=0)
                OR (observation_status IN ('missing','inapplicable','error')
                    AND value_kind='missing')
            ),
            CHECK(length(health_detail)<=800),
            UNIQUE(
                checkpoint_id,target_kind,nm_id,advert_id,placement,parameter_field
            )
        );
        CREATE INDEX IF NOT EXISTS change_registry_observations_by_target
        ON {OBSERVATION_VALUES_TABLE}(
            target_kind,nm_id,advert_id,placement,parameter_field,
            observed_at,observation_value_id
        );
        CREATE TRIGGER IF NOT EXISTS change_registry_observation_within_checkpoint
        BEFORE INSERT ON {OBSERVATION_VALUES_TABLE}
        WHEN NOT EXISTS(
            SELECT 1 FROM {CHECKPOINTS_TABLE} checkpoint
            WHERE checkpoint.checkpoint_id=NEW.checkpoint_id
              AND julianday(checkpoint.started_at)<=julianday(NEW.observed_at)
              AND julianday(NEW.observed_at)<=julianday(checkpoint.completed_at)
        )
        BEGIN
            SELECT RAISE(ABORT,'observation timestamp is outside checkpoint interval');
        END;

        CREATE TABLE IF NOT EXISTS {IDENTITY_INCIDENTS_TABLE}(
            incident_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            incident_kind TEXT NOT NULL CHECK(incident_kind IN (
                'campaign_nm_mapping_cardinality','invalid_target_identity','identity_drift'
            )),
            target_kind TEXT NOT NULL CHECK(target_kind IN ('price','bid','campaign')),
            advert_id INTEGER NOT NULL DEFAULT 0 CHECK(
                typeof(advert_id)='integer' AND advert_id>=0
            ),
            candidate_nm_ids_json TEXT NOT NULL CHECK(
                json_valid(candidate_nm_ids_json)
                AND json_type(candidate_nm_ids_json)='array'
            ),
            candidate_count INTEGER NOT NULL CHECK(
                typeof(candidate_count)='integer' AND candidate_count>=0
                AND candidate_count=json_array_length(candidate_nm_ids_json)
            ),
            source_surface TEXT NOT NULL,
            observed_at TEXT NOT NULL
                CHECK(substr(observed_at,-1,1)='Z' AND julianday(observed_at) IS NOT NULL),
            evidence_digest TEXT NOT NULL CHECK({_digest_check('evidence_digest')}),
            mapping_version TEXT NOT NULL CHECK(mapping_version='{MAPPING_VERSION}'),
            CHECK({_identity_text_check('incident_id', 120)}),
            CHECK({_identity_text_check('seller_id', 120)}),
            CHECK({_identity_text_check('account_scope', 120)}),
            CHECK(incident_kind<>'campaign_nm_mapping_cardinality'
                OR (target_kind='campaign' AND advert_id>0 AND candidate_count<>1)),
            UNIQUE(
                seller_id,account_scope,incident_kind,target_kind,advert_id,
                observed_at,evidence_digest
            )
        );
        CREATE INDEX IF NOT EXISTS change_registry_identity_incidents_by_scope_time
        ON {IDENTITY_INCIDENTS_TABLE}(
            seller_id,account_scope,observed_at,incident_id
        );

        CREATE TABLE IF NOT EXISTS {FACT_LINKS_TABLE}(
            fact_link_id TEXT PRIMARY KEY,
            fact_id TEXT NOT NULL REFERENCES {FACTS_TABLE}(fact_id),
            link_kind TEXT NOT NULL CHECK(link_kind IN (
                'change_item','checkpoint','native_audit','recommendation_item'
            )),
            change_item_id TEXT REFERENCES {ITEMS_TABLE}(change_item_id),
            checkpoint_id TEXT REFERENCES {CHECKPOINTS_TABLE}(checkpoint_id),
            native_audit_reference TEXT NOT NULL DEFAULT '',
            recommendation_item_id TEXT NOT NULL DEFAULT '',
            linked_at TEXT NOT NULL
                CHECK(substr(linked_at,-1,1)='Z' AND julianday(linked_at) IS NOT NULL),
            evidence_digest TEXT NOT NULL CHECK({_digest_check('evidence_digest')}),
            CHECK({_identity_text_check('fact_link_id', 120)}),
            CHECK(
                (link_kind='change_item' AND change_item_id IS NOT NULL
                    AND checkpoint_id IS NULL AND native_audit_reference=''
                    AND recommendation_item_id='')
                OR (link_kind='checkpoint' AND change_item_id IS NULL
                    AND checkpoint_id IS NOT NULL AND native_audit_reference=''
                    AND recommendation_item_id='')
                OR (link_kind='native_audit' AND change_item_id IS NULL
                    AND checkpoint_id IS NULL AND native_audit_reference<>''
                    AND recommendation_item_id='')
                OR (link_kind='recommendation_item' AND change_item_id IS NULL
                    AND checkpoint_id IS NULL AND native_audit_reference=''
                    AND recommendation_item_id<>'')
            )
        );
        CREATE UNIQUE INDEX IF NOT EXISTS change_registry_fact_links_change_item
        ON {FACT_LINKS_TABLE}(fact_id,change_item_id)
        WHERE link_kind='change_item';
        CREATE UNIQUE INDEX IF NOT EXISTS change_registry_fact_links_checkpoint
        ON {FACT_LINKS_TABLE}(fact_id,checkpoint_id)
        WHERE link_kind='checkpoint';
        CREATE UNIQUE INDEX IF NOT EXISTS change_registry_fact_links_native_audit
        ON {FACT_LINKS_TABLE}(fact_id,native_audit_reference)
        WHERE link_kind='native_audit';
        CREATE UNIQUE INDEX IF NOT EXISTS change_registry_fact_links_recommendation
        ON {FACT_LINKS_TABLE}(fact_id,recommendation_item_id)
        WHERE link_kind='recommendation_item';
        CREATE INDEX IF NOT EXISTS change_registry_fact_links_by_fact_time
        ON {FACT_LINKS_TABLE}(fact_id,linked_at,fact_link_id);

        CREATE TRIGGER IF NOT EXISTS change_registry_fact_link_exact_scope
        BEFORE INSERT ON {FACT_LINKS_TABLE}
        BEGIN
            SELECT CASE WHEN NEW.link_kind='change_item' AND NOT EXISTS(
                SELECT 1
                FROM {FACTS_TABLE} fact
                JOIN {ITEMS_TABLE} item ON item.change_item_id=NEW.change_item_id
                WHERE fact.fact_id=NEW.fact_id
                  AND fact.seller_id=item.seller_id
                  AND fact.account_scope=item.account_scope
                  AND fact.target_kind=item.target_kind
                  AND fact.nm_id=item.nm_id
                  AND fact.advert_id=item.advert_id
                  AND fact.placement=item.placement
                  AND fact.parameter_field=item.parameter_field
            ) THEN RAISE(ABORT,'fact link target identity mismatch') END;
            SELECT CASE WHEN NEW.link_kind='checkpoint' AND NOT EXISTS(
                SELECT 1
                FROM {FACTS_TABLE} fact
                JOIN {CHECKPOINTS_TABLE} checkpoint
                  ON checkpoint.checkpoint_id=NEW.checkpoint_id
                WHERE fact.fact_id=NEW.fact_id
                  AND fact.seller_id=checkpoint.seller_id
                  AND fact.account_scope=checkpoint.account_scope
            ) THEN RAISE(ABORT,'fact link checkpoint scope mismatch') END;
        END;

        CREATE TABLE IF NOT EXISTS {ANNOTATION_REVISIONS_TABLE}(
            annotation_revision_id TEXT PRIMARY KEY,
            subject_kind TEXT NOT NULL CHECK(subject_kind IN (
                'operation','change_item','fact','checkpoint','identity_incident',
                'manual_pending'
            )),
            subject_id TEXT NOT NULL,
            revision_no INTEGER NOT NULL
                CHECK(typeof(revision_no)='integer' AND revision_no>0),
            parent_revision_id TEXT REFERENCES {ANNOTATION_REVISIONS_TABLE}(
                annotation_revision_id
            ),
            actor_principal TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            CHECK({_identity_text_check('annotation_revision_id', 120)}),
            CHECK({_identity_text_check('subject_id', 160)}),
            CHECK({_identity_text_check('actor_principal', 160)}),
            CHECK(length(reason)<=1000 AND length(comment)<=4000),
            UNIQUE(subject_kind,subject_id,revision_no)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS change_registry_annotation_parent_child
        ON {ANNOTATION_REVISIONS_TABLE}(parent_revision_id)
        WHERE parent_revision_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS change_registry_annotations_by_subject
        ON {ANNOTATION_REVISIONS_TABLE}(
            subject_kind,subject_id,revision_no,annotation_revision_id
        );

        CREATE TABLE IF NOT EXISTS {CHECKPOINT_SOURCE_MANIFESTS_TABLE}(
            source_manifest_id TEXT PRIMARY KEY,
            checkpoint_id TEXT NOT NULL REFERENCES {CHECKPOINTS_TABLE}(checkpoint_id),
            source_name TEXT NOT NULL CHECK(source_name IN ('prices','ads')),
            completeness_status TEXT NOT NULL
                CHECK(completeness_status IN ('complete','partial','failed')),
            expected_count INTEGER NOT NULL CHECK(
                typeof(expected_count)='integer' AND expected_count>=0
            ),
            observed_count INTEGER NOT NULL CHECK(
                typeof(observed_count)='integer' AND observed_count>=0
                AND observed_count<=expected_count
            ),
            summary_json TEXT NOT NULL CHECK(
                json_valid(summary_json) AND json_type(summary_json)='object'
                AND length(summary_json)<=4000
            ),
            evidence_digest TEXT NOT NULL CHECK({_digest_check('evidence_digest')}),
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            CHECK({_identity_text_check('source_manifest_id', 120)}),
            UNIQUE(checkpoint_id,source_name)
        );
        CREATE INDEX IF NOT EXISTS change_registry_source_manifests_by_checkpoint
        ON {CHECKPOINT_SOURCE_MANIFESTS_TABLE}(checkpoint_id,source_name);

        CREATE TABLE IF NOT EXISTS {OBSERVER_JOBS_TABLE}(
            job_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('scheduled','manual','activation')),
            scheduled_slot TEXT NOT NULL DEFAULT '',
            requested_by TEXT NOT NULL,
            requested_at TEXT NOT NULL
                CHECK(substr(requested_at,-1,1)='Z' AND julianday(requested_at) IS NOT NULL),
            request_digest TEXT NOT NULL CHECK({_digest_check('request_digest')}),
            CHECK({_identity_text_check('job_id', 120)}),
            CHECK({_identity_text_check('seller_id', 120)}),
            CHECK({_identity_text_check('account_scope', 120)}),
            CHECK({_identity_text_check('requested_by', 160)}),
            CHECK((trigger_kind='scheduled' AND scheduled_slot<>'')
                OR (trigger_kind<>'scheduled' AND scheduled_slot=''))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS change_registry_observer_scheduled_slot
        ON {OBSERVER_JOBS_TABLE}(seller_id,account_scope,scheduled_slot)
        WHERE trigger_kind='scheduled';
        CREATE INDEX IF NOT EXISTS change_registry_observer_jobs_by_scope_time
        ON {OBSERVER_JOBS_TABLE}(seller_id,account_scope,requested_at,job_id);

        CREATE TABLE IF NOT EXISTS {OBSERVER_JOB_EVENTS_TABLE}(
            job_event_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES {OBSERVER_JOBS_TABLE}(job_id),
            sequence_no INTEGER NOT NULL CHECK(
                typeof(sequence_no)='integer' AND sequence_no>0
            ),
            state TEXT NOT NULL CHECK(state IN (
                'accepted','running','complete','partial','failed','busy'
            )),
            occurred_at TEXT NOT NULL
                CHECK(substr(occurred_at,-1,1)='Z' AND julianday(occurred_at) IS NOT NULL),
            checkpoint_id TEXT REFERENCES {CHECKPOINTS_TABLE}(checkpoint_id),
            fact_count INTEGER NOT NULL DEFAULT 0 CHECK(
                typeof(fact_count)='integer' AND fact_count>=0
            ),
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '' CHECK(length(error_message)<=800),
            evidence_digest TEXT NOT NULL CHECK({_digest_check('evidence_digest')}),
            CHECK({_identity_text_check('job_event_id', 120)}),
            UNIQUE(job_id,sequence_no)
        );
        CREATE INDEX IF NOT EXISTS change_registry_observer_job_events_by_job
        ON {OBSERVER_JOB_EVENTS_TABLE}(job_id,sequence_no,job_event_id);

        CREATE TABLE IF NOT EXISTS {OBSERVER_HEALTH_EVENTS_TABLE}(
            health_event_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            scheduled_slot TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('complete','partial','failed')),
            consecutive_noncomplete INTEGER NOT NULL CHECK(
                typeof(consecutive_noncomplete)='integer' AND consecutive_noncomplete>=0
            ),
            health_state TEXT NOT NULL CHECK(health_state IN ('normal','degraded')),
            job_id TEXT NOT NULL REFERENCES {OBSERVER_JOBS_TABLE}(job_id),
            checkpoint_id TEXT REFERENCES {CHECKPOINTS_TABLE}(checkpoint_id),
            occurred_at TEXT NOT NULL
                CHECK(substr(occurred_at,-1,1)='Z' AND julianday(occurred_at) IS NOT NULL),
            evidence_digest TEXT NOT NULL CHECK({_digest_check('evidence_digest')}),
            CHECK({_identity_text_check('health_event_id', 120)}),
            UNIQUE(seller_id,account_scope,scheduled_slot)
        );
        CREATE INDEX IF NOT EXISTS change_registry_observer_health_by_scope_time
        ON {OBSERVER_HEALTH_EVENTS_TABLE}(
            seller_id,account_scope,occurred_at,health_event_id
        );

        CREATE TABLE IF NOT EXISTS {OBSERVER_LEASES_TABLE}(
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            owner_job_id TEXT NOT NULL DEFAULT '',
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>0),
            updated_at TEXT NOT NULL
                CHECK(substr(updated_at,-1,1)='Z' AND julianday(updated_at) IS NOT NULL),
            CHECK({_identity_text_check('seller_id', 120)}),
            CHECK({_identity_text_check('account_scope', 120)}),
            CHECK((owner_job_id='' AND acquired_at='' AND expires_at='') OR (
                owner_job_id<>''
                AND substr(acquired_at,-1,1)='Z' AND julianday(acquired_at) IS NOT NULL
                AND substr(expires_at,-1,1)='Z' AND julianday(expires_at) IS NOT NULL
                AND julianday(acquired_at)<julianday(expires_at)
            )),
            PRIMARY KEY(seller_id,account_scope)
        );

        CREATE TABLE IF NOT EXISTS {MANUAL_PENDING_EVENTS_TABLE}(
            pending_event_id TEXT PRIMARY KEY,
            pending_id TEXT NOT NULL,
            change_item_id TEXT NOT NULL REFERENCES {ITEMS_TABLE}(change_item_id),
            sequence_no INTEGER NOT NULL
                CHECK(typeof(sequence_no)='integer' AND sequence_no>0),
            state TEXT NOT NULL CHECK(state IN (
                'pending','superseded','matched','deviated','expired'
            )),
            related_fact_id TEXT REFERENCES {FACTS_TABLE}(fact_id),
            supersedes_pending_id TEXT NOT NULL DEFAULT '',
            occurred_at TEXT NOT NULL
                CHECK(substr(occurred_at,-1,1)='Z' AND julianday(occurred_at) IS NOT NULL),
            evidence_digest TEXT NOT NULL CHECK({_digest_check('evidence_digest')}),
            native_event_key TEXT NOT NULL DEFAULT '',
            CHECK({_identity_text_check('pending_event_id', 120)}),
            CHECK({_identity_text_check('pending_id', 120)}),
            CHECK((state IN ('matched','deviated') AND related_fact_id IS NOT NULL)
                OR (state NOT IN ('matched','deviated') AND related_fact_id IS NULL)),
            UNIQUE(pending_id,sequence_no)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS change_registry_manual_pending_native_key
        ON {MANUAL_PENDING_EVENTS_TABLE}(change_item_id,native_event_key)
        WHERE native_event_key<>'';
        CREATE INDEX IF NOT EXISTS change_registry_manual_pending_events_by_item
        ON {MANUAL_PENDING_EVENTS_TABLE}(
            change_item_id,occurred_at,pending_id,sequence_no
        );

        CREATE TABLE IF NOT EXISTS {MANUAL_PENDING_CURRENT_TABLE}(
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            target_kind TEXT NOT NULL CHECK(target_kind IN ('price','bid','campaign')),
            nm_id INTEGER NOT NULL,
            advert_id INTEGER NOT NULL DEFAULT 0,
            placement TEXT NOT NULL DEFAULT '',
            parameter_field TEXT NOT NULL CHECK(parameter_field IN (
                'original_price_minor','discount_bps','seller_price_minor',
                'bid_minor','campaign_state','payment_model','payment_unit'
            )),
            current_pending_id TEXT NOT NULL,
            current_event_id TEXT NOT NULL
                REFERENCES {MANUAL_PENDING_EVENTS_TABLE}(pending_event_id),
            active INTEGER NOT NULL CHECK(active IN (0,1)),
            revision INTEGER NOT NULL
                CHECK(typeof(revision)='integer' AND revision>0),
            updated_at TEXT NOT NULL
                CHECK(substr(updated_at,-1,1)='Z' AND julianday(updated_at) IS NOT NULL),
            CHECK({_identity_text_check('seller_id', 120)}),
            CHECK({_identity_text_check('account_scope', 120)}),
            CHECK({_identity_text_check('current_pending_id', 120)}),
            CHECK({target_check}),
            PRIMARY KEY(
                seller_id,account_scope,target_kind,nm_id,advert_id,placement,
                parameter_field
            )
        );

        CREATE TRIGGER IF NOT EXISTS change_registry_attempt_identity_consistent
        BEFORE INSERT ON {ATTEMPT_EVENTS_TABLE}
        WHEN EXISTS(
            SELECT 1 FROM {ATTEMPT_EVENTS_TABLE}
            WHERE attempt_id=NEW.attempt_id AND change_item_id<>NEW.change_item_id
        )
        BEGIN
            SELECT RAISE(ABORT,'change registry attempt identity conflict');
        END;
        CREATE TRIGGER IF NOT EXISTS change_registry_attempt_lifecycle
        BEFORE INSERT ON {ATTEMPT_EVENTS_TABLE}
        BEGIN
            SELECT CASE WHEN NEW.sequence_no=1 AND NEW.state<>'created'
                THEN RAISE(ABORT,'attempt lifecycle must begin with created') END;
            SELECT CASE WHEN NEW.sequence_no>1 AND NOT EXISTS(
                SELECT 1 FROM {ATTEMPT_EVENTS_TABLE} previous
                WHERE previous.attempt_id=NEW.attempt_id
                  AND previous.sequence_no=NEW.sequence_no-1
                  AND (
                    (previous.state='created' AND NEW.state IN
                        ('submitted','failed','rejected','cancelled'))
                    OR (previous.state='submitted' AND NEW.state IN
                        ('confirmed','failed','rejected','cancelled','ambiguous'))
                    OR (previous.state='ambiguous' AND NEW.state='resolved')
                  )
                  AND julianday(previous.occurred_at)<=julianday(NEW.occurred_at)
            ) THEN RAISE(ABORT,'attempt lifecycle transition mismatch') END;
        END;

        CREATE TRIGGER IF NOT EXISTS change_registry_checkpoint_previous_complete
        BEFORE INSERT ON {CHECKPOINTS_TABLE}
        WHEN NEW.previous_complete_checkpoint_id IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM {CHECKPOINTS_TABLE} previous
            WHERE previous.checkpoint_id=NEW.previous_complete_checkpoint_id
              AND previous.completeness_status='complete'
              AND previous.seller_id=NEW.seller_id
              AND previous.account_scope=NEW.account_scope
              AND julianday(previous.completed_at)<=julianday(NEW.completed_at)
        )
        BEGIN
            SELECT RAISE(ABORT,'previous checkpoint is not a complete same-scope baseline');
        END;

        CREATE TRIGGER IF NOT EXISTS change_registry_identity_incident_candidates
        BEFORE INSERT ON {IDENTITY_INCIDENTS_TABLE}
        WHEN EXISTS(
            SELECT 1 FROM json_each(NEW.candidate_nm_ids_json)
            WHERE type<>'integer' OR value<=0
        ) OR (
            SELECT COUNT(*) FROM json_each(NEW.candidate_nm_ids_json)
        )<>(
            SELECT COUNT(DISTINCT value) FROM json_each(NEW.candidate_nm_ids_json)
        )
        BEGIN
            SELECT RAISE(ABORT,'identity incident candidates must be unique positive integers');
        END;

        CREATE TRIGGER IF NOT EXISTS change_registry_annotation_parent_chain
        BEFORE INSERT ON {ANNOTATION_REVISIONS_TABLE}
        BEGIN
            SELECT CASE WHEN NEW.parent_revision_id IS NULL AND NEW.revision_no<>1
                THEN RAISE(ABORT,'annotation root revision must be one') END;
            SELECT CASE WHEN NEW.parent_revision_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM {ANNOTATION_REVISIONS_TABLE} parent
                WHERE parent.annotation_revision_id=NEW.parent_revision_id
                  AND parent.subject_kind=NEW.subject_kind
                  AND parent.subject_id=NEW.subject_id
                  AND parent.revision_no+1=NEW.revision_no
                  AND julianday(parent.created_at)<=julianday(NEW.created_at)
            ) THEN RAISE(ABORT,'annotation parent chain mismatch') END;
        END;

        CREATE TRIGGER IF NOT EXISTS change_registry_manual_pending_lifecycle
        BEFORE INSERT ON {MANUAL_PENDING_EVENTS_TABLE}
        BEGIN
            SELECT CASE WHEN NEW.sequence_no=1 AND NEW.state<>'pending'
                THEN RAISE(ABORT,'manual pending lifecycle must begin with pending') END;
            SELECT CASE WHEN NEW.sequence_no>1 AND NOT EXISTS(
                SELECT 1 FROM {MANUAL_PENDING_EVENTS_TABLE} previous
                WHERE previous.pending_id=NEW.pending_id
                  AND previous.change_item_id=NEW.change_item_id
                  AND previous.sequence_no=NEW.sequence_no-1
                  AND previous.state='pending'
                  AND NEW.state IN ('superseded','matched','deviated','expired')
                  AND julianday(previous.occurred_at)<=julianday(NEW.occurred_at)
            ) THEN RAISE(ABORT,'manual pending lifecycle transition mismatch') END;
            SELECT CASE WHEN NEW.related_fact_id IS NOT NULL AND NOT EXISTS(
                SELECT 1
                FROM {FACTS_TABLE} fact
                JOIN {ITEMS_TABLE} item ON item.change_item_id=NEW.change_item_id
                WHERE fact.fact_id=NEW.related_fact_id
                  AND fact.seller_id=item.seller_id
                  AND fact.account_scope=item.account_scope
                  AND fact.target_kind=item.target_kind
                  AND fact.nm_id=item.nm_id
                  AND fact.advert_id=item.advert_id
                  AND fact.placement=item.placement
                  AND fact.parameter_field=item.parameter_field
            ) THEN RAISE(ABORT,'manual pending fact target identity mismatch') END;
        END;

        CREATE TRIGGER IF NOT EXISTS change_registry_manual_current_exact_insert
        BEFORE INSERT ON {MANUAL_PENDING_CURRENT_TABLE}
        WHEN NOT EXISTS(
            SELECT 1
            FROM {MANUAL_PENDING_EVENTS_TABLE} event
            JOIN {ITEMS_TABLE} item ON item.change_item_id=event.change_item_id
            WHERE event.pending_event_id=NEW.current_event_id
              AND event.pending_id=NEW.current_pending_id
              AND item.seller_id=NEW.seller_id
              AND item.account_scope=NEW.account_scope
              AND item.target_kind=NEW.target_kind
              AND item.nm_id=NEW.nm_id
              AND item.advert_id=NEW.advert_id
              AND item.placement=NEW.placement
              AND item.parameter_field=NEW.parameter_field
              AND event.occurred_at=NEW.updated_at
              AND NOT EXISTS(
                SELECT 1 FROM {MANUAL_PENDING_EVENTS_TABLE} later
                WHERE later.pending_id=event.pending_id
                  AND later.sequence_no>event.sequence_no
              )
              AND ((NEW.active=1 AND event.state='pending')
                OR (NEW.active=0 AND event.state IN
                    ('superseded','matched','deviated','expired')))
        )
        BEGIN
            SELECT RAISE(ABORT,'manual pending coordination event mismatch');
        END;
        CREATE TRIGGER IF NOT EXISTS change_registry_manual_current_exact_update
        BEFORE UPDATE ON {MANUAL_PENDING_CURRENT_TABLE}
        WHEN NOT EXISTS(
            SELECT 1
            FROM {MANUAL_PENDING_EVENTS_TABLE} event
            JOIN {ITEMS_TABLE} item ON item.change_item_id=event.change_item_id
            WHERE event.pending_event_id=NEW.current_event_id
              AND event.pending_id=NEW.current_pending_id
              AND item.seller_id=NEW.seller_id
              AND item.account_scope=NEW.account_scope
              AND item.target_kind=NEW.target_kind
              AND item.nm_id=NEW.nm_id
              AND item.advert_id=NEW.advert_id
              AND item.placement=NEW.placement
              AND item.parameter_field=NEW.parameter_field
              AND event.occurred_at=NEW.updated_at
              AND NOT EXISTS(
                SELECT 1 FROM {MANUAL_PENDING_EVENTS_TABLE} later
                WHERE later.pending_id=event.pending_id
                  AND later.sequence_no>event.sequence_no
              )
              AND ((NEW.active=1 AND event.state='pending')
                OR (NEW.active=0 AND event.state IN
                    ('superseded','matched','deviated','expired')))
        )
        BEGIN
            SELECT RAISE(ABORT,'manual pending coordination event mismatch');
        END;

        CREATE TRIGGER IF NOT EXISTS change_registry_manual_current_stable_identity
        BEFORE UPDATE ON {MANUAL_PENDING_CURRENT_TABLE}
        WHEN NEW.seller_id<>OLD.seller_id
          OR NEW.account_scope<>OLD.account_scope
          OR NEW.target_kind<>OLD.target_kind
          OR NEW.nm_id<>OLD.nm_id
          OR NEW.advert_id<>OLD.advert_id
          OR NEW.placement<>OLD.placement
          OR NEW.parameter_field<>OLD.parameter_field
          OR NEW.revision<>OLD.revision+1
          OR julianday(NEW.updated_at)<julianday(OLD.updated_at)
        BEGIN
            SELECT RAISE(ABORT,'manual pending coordination CAS mismatch');
        END;
        CREATE TRIGGER IF NOT EXISTS change_registry_manual_current_no_delete
        BEFORE DELETE ON {MANUAL_PENDING_CURRENT_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'manual pending coordination rows are retained');
        END;
        CREATE TRIGGER IF NOT EXISTS change_registry_observer_lease_cas
        BEFORE UPDATE ON {OBSERVER_LEASES_TABLE}
        WHEN NEW.seller_id<>OLD.seller_id
          OR NEW.account_scope<>OLD.account_scope
          OR NEW.revision<>OLD.revision+1
          OR julianday(NEW.updated_at)<julianday(OLD.updated_at)
        BEGIN
            SELECT RAISE(ABORT,'change registry observer lease CAS mismatch');
        END;
        CREATE TRIGGER IF NOT EXISTS change_registry_observer_lease_no_delete
        BEFORE DELETE ON {OBSERVER_LEASES_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'change registry observer lease rows are retained');
        END;
        """
    )
    for table in IMMUTABLE_TABLES:
        trigger_stem = table.removeprefix("change_registry_")
        conn.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS change_registry_{trigger_stem}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS change_registry_{trigger_stem}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
            """
        )


class ChangeRegistryRepository:
    """Internal StoreRegistry-backed create/read/append foundation API."""

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.store_registry = StoreRegistry(self.runtime_dir)

    def initialize_schema(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with self.store_registry.session(
            "operational",
            mode="rw",
            operation="change_registry_schema_init",
        ) as conn:
            ensure_change_registry_schema(conn)
            conn.commit()

    def prepare_writer_operation(
        self,
        *,
        operation_id: str,
        seller_id: str,
        account_scope: str,
        source_surface: str,
        actor_principal: str,
        actor_kind: str,
        requested_at: str,
        created_at: str,
        provenance_digest: str,
        items: Sequence[Mapping[str, Any]],
        native_idempotency_key: str = "",
        correlation_id: str = "",
        calculation_id: str = "",
        apply_operation_id: str = "",
        provenance_annotation_id: str = "",
        provenance_comment: str = "",
    ) -> dict[str, Any]:
        """Atomically create one writer header, exact items and created attempts."""

        operation_row = {
            "operation_id": _identifier(operation_id, "operation_id"),
            "seller_id": _identifier(seller_id, "seller_id"),
            "account_scope": _identifier(account_scope, "account_scope"),
            "source_surface": _identifier(source_surface, "source_surface"),
            "actor_principal": _identifier(
                actor_principal, "actor_principal", maximum=160
            ),
            "actor_kind": _required_token(
                actor_kind,
                "actor_kind",
                {"human", "service", "system", "import"},
            ),
            "requested_at": _timestamp(requested_at, "requested_at"),
            "created_at": _timestamp(created_at, "created_at"),
            "native_idempotency_key": _optional_text(
                native_idempotency_key, "native_idempotency_key", 240
            ),
            "correlation_id": _optional_text(
                correlation_id, "correlation_id", 240
            ),
            "calculation_id": _optional_text(
                calculation_id, "calculation_id", 240
            ),
            "apply_operation_id": _optional_text(
                apply_operation_id, "apply_operation_id", 240
            ),
            "provenance_digest": _digest(
                provenance_digest, "provenance_digest"
            ),
            "mapping_version": MAPPING_VERSION,
        }
        if _timestamp_moment(operation_row["requested_at"]) > _timestamp_moment(
            operation_row["created_at"]
        ):
            raise ChangeRegistryError(
                "operation created_at precedes requested_at"
            )
        if not items:
            raise ChangeRegistryError(
                "writer operation requires at least one atomic item"
            )

        item_rows: list[dict[str, Any]] = []
        attempt_rows: list[dict[str, Any]] = []
        seen_targets: set[tuple[str, int, int, str, str]] = set()
        for raw_item in items:
            target = raw_item.get("target")
            if not isinstance(target, TargetIdentity):
                raise ChangeRegistryError(
                    "writer item target must be a TargetIdentity"
                )
            exact_target = target_identity(
                target.target_kind,
                nm_id=target.nm_id,
                advert_id=target.advert_id,
                placement=target.placement,
            )
            field = _validate_parameter_field(
                exact_target.target_kind,
                str(raw_item.get("parameter_field") or ""),
            )
            target_key = (
                exact_target.target_kind,
                exact_target.nm_id,
                exact_target.advert_id,
                exact_target.placement,
                field,
            )
            if target_key in seen_targets:
                raise ChangeRegistryError(
                    "writer operation contains a duplicate atomic target"
                )
            seen_targets.add(target_key)
            before = _canonicalize_field_value(
                field, raw_item.get("before_value", MISSING)
            )
            requested = _canonicalize_field_value(
                field, raw_item.get("requested_value", MISSING)
            )
            _validate_field_value(field, before, requested=False)
            _validate_field_value(field, requested, requested=True)
            change_item_id = _identifier(
                raw_item.get("change_item_id"), "change_item_id"
            )
            attempt_id = _identifier(
                raw_item.get("attempt_id"), "attempt_id"
            )
            item_rows.append(
                {
                    "change_item_id": change_item_id,
                    "operation_id": operation_row["operation_id"],
                    "seller_id": operation_row["seller_id"],
                    "account_scope": operation_row["account_scope"],
                    "target_kind": exact_target.target_kind,
                    "nm_id": exact_target.nm_id,
                    "advert_id": exact_target.advert_id,
                    "placement": exact_target.placement,
                    "parameter_field": field,
                    **before.columns("before_value"),
                    **requested.columns("requested_value"),
                    "recommendation_item_id": _optional_text(
                        raw_item.get("recommendation_item_id"),
                        "recommendation_item_id",
                        160,
                    ),
                    "mapping_version": MAPPING_VERSION,
                    "created_at": operation_row["created_at"],
                }
            )
            attempt_rows.append(
                {
                    "attempt_event_id": _identifier(
                        raw_item.get("attempt_event_id"),
                        "attempt_event_id",
                    ),
                    "attempt_id": attempt_id,
                    "change_item_id": change_item_id,
                    "sequence_no": 1,
                    "state": "created",
                    "resolution_state": "",
                    "occurred_at": operation_row["created_at"],
                    "receipt_reference": "",
                    "receipt_digest": "",
                    "error_code": "",
                    "error_message": "",
                    "readback_proof_kind": "",
                    "readback_digest": "",
                    "native_event_key": "created",
                }
            )

        annotation_row: dict[str, Any] | None = None
        if provenance_comment:
            annotation_row = {
                "annotation_revision_id": _identifier(
                    provenance_annotation_id,
                    "provenance_annotation_id",
                ),
                "subject_kind": "operation",
                "subject_id": operation_row["operation_id"],
                "revision_no": 1,
                "parent_revision_id": None,
                "actor_principal": operation_row["actor_principal"],
                "reason": "writer_provenance",
                "comment": _optional_text(
                    provenance_comment, "provenance_comment", 4000
                ),
                "created_at": operation_row["created_at"],
            }

        with self._transaction("prepare_writer_operation") as conn:
            existed_before = conn.execute(
                f"SELECT 1 FROM {OPERATIONS_TABLE} WHERE operation_id=?",
                (operation_row["operation_id"],),
            ).fetchone() is not None
            operation = self._insert_idempotent_conn(
                conn, OPERATIONS_TABLE, "operation_id", operation_row
            )
            stored_items = [
                self._insert_idempotent_conn(
                    conn, ITEMS_TABLE, "change_item_id", row
                )
                for row in item_rows
            ]
            stored_attempts = [
                self._insert_idempotent_conn(
                    conn,
                    ATTEMPT_EVENTS_TABLE,
                    "attempt_event_id",
                    row,
                )
                for row in attempt_rows
            ]
            annotation = (
                self._insert_idempotent_conn(
                    conn,
                    ANNOTATION_REVISIONS_TABLE,
                    "annotation_revision_id",
                    annotation_row,
                )
                if annotation_row is not None
                else None
            )
            return {
                "operation": operation,
                "items": stored_items,
                "attempt_events": stored_attempts,
                "annotation": annotation,
                "created_new": not existed_before,
            }

    def create_operation(
        self,
        *,
        operation_id: str,
        seller_id: str,
        account_scope: str,
        source_surface: str,
        actor_principal: str,
        actor_kind: str,
        requested_at: str,
        created_at: str,
        provenance_digest: str,
        native_idempotency_key: str = "",
        correlation_id: str = "",
        calculation_id: str = "",
        apply_operation_id: str = "",
    ) -> dict[str, Any]:
        row = {
            "operation_id": _identifier(operation_id, "operation_id"),
            "seller_id": _identifier(seller_id, "seller_id"),
            "account_scope": _identifier(account_scope, "account_scope"),
            "source_surface": _identifier(source_surface, "source_surface"),
            "actor_principal": _identifier(
                actor_principal, "actor_principal", maximum=160
            ),
            "actor_kind": _required_token(
                actor_kind,
                "actor_kind",
                {"human", "service", "system", "import"},
            ),
            "requested_at": _timestamp(requested_at, "requested_at"),
            "created_at": _timestamp(created_at, "created_at"),
            "native_idempotency_key": _optional_text(
                native_idempotency_key, "native_idempotency_key", 240
            ),
            "correlation_id": _optional_text(correlation_id, "correlation_id", 240),
            "calculation_id": _optional_text(calculation_id, "calculation_id", 240),
            "apply_operation_id": _optional_text(
                apply_operation_id, "apply_operation_id", 240
            ),
            "provenance_digest": _digest(provenance_digest, "provenance_digest"),
            "mapping_version": MAPPING_VERSION,
        }
        if _timestamp_moment(row["requested_at"]) > _timestamp_moment(
            row["created_at"]
        ):
            raise ChangeRegistryError("operation created_at precedes requested_at")
        return self._insert_idempotent(
            OPERATIONS_TABLE, "operation_id", row, operation="create_operation"
        )

    def append_change_item(
        self,
        *,
        change_item_id: str,
        operation_id: str,
        target: TargetIdentity,
        parameter_field: str,
        before_value: Any,
        requested_value: Any,
        created_at: str,
        recommendation_item_id: str = "",
    ) -> dict[str, Any]:
        exact_target = target_identity(
            target.target_kind,
            nm_id=target.nm_id,
            advert_id=target.advert_id,
            placement=target.placement,
        )
        field = _validate_parameter_field(exact_target.target_kind, parameter_field)
        before = _canonicalize_field_value(field, before_value)
        requested = _canonicalize_field_value(field, requested_value)
        _validate_field_value(field, before, requested=False)
        _validate_field_value(field, requested, requested=True)
        with self._transaction("append_change_item") as conn:
            operation = self._required_row(
                conn, OPERATIONS_TABLE, "operation_id", operation_id
            )
            row = {
                "change_item_id": _identifier(change_item_id, "change_item_id"),
                "operation_id": str(operation["operation_id"]),
                "seller_id": str(operation["seller_id"]),
                "account_scope": str(operation["account_scope"]),
                "target_kind": exact_target.target_kind,
                "nm_id": exact_target.nm_id,
                "advert_id": exact_target.advert_id,
                "placement": exact_target.placement,
                "parameter_field": field,
                **before.columns("before_value"),
                **requested.columns("requested_value"),
                "recommendation_item_id": _optional_text(
                    recommendation_item_id, "recommendation_item_id", 160
                ),
                "mapping_version": MAPPING_VERSION,
                "created_at": _timestamp(created_at, "created_at"),
            }
            return self._insert_idempotent_conn(
                conn, ITEMS_TABLE, "change_item_id", row
            )

    def append_attempt_event(
        self,
        *,
        attempt_event_id: str,
        attempt_id: str,
        change_item_id: str,
        sequence_no: int,
        state: str,
        occurred_at: str,
        resolution_state: str = "",
        receipt_reference: str = "",
        receipt_digest: str = "",
        error_code: str = "",
        error_message: str = "",
        readback_proof_kind: str = "",
        readback_digest: str = "",
        native_event_key: str = "",
    ) -> dict[str, Any]:
        exact_attempt_id = _identifier(attempt_id, "attempt_id")
        exact_sequence = _positive_integer(sequence_no, "sequence_no")
        exact_state = _required_token(state, "state", ATTEMPT_STATES)
        exact_resolution = str(resolution_state or "").strip().lower()
        if (exact_state == "resolved") != bool(exact_resolution):
            raise ChangeRegistryError(
                "resolved attempt events require one terminal resolution_state"
            )
        if exact_resolution and exact_resolution not in {
            "confirmed",
            "failed",
            "rejected",
            "cancelled",
        }:
            raise ChangeRegistryError("invalid attempt resolution_state")
        row = {
            "attempt_event_id": _identifier(
                attempt_event_id, "attempt_event_id"
            ),
            "attempt_id": exact_attempt_id,
            "change_item_id": _identifier(change_item_id, "change_item_id"),
            "sequence_no": exact_sequence,
            "state": exact_state,
            "resolution_state": exact_resolution,
            "occurred_at": _timestamp(occurred_at, "occurred_at"),
            "receipt_reference": _sanitized_text(
                receipt_reference, "receipt_reference", 320
            ),
            "receipt_digest": _optional_digest(
                receipt_digest, "receipt_digest"
            ),
            "error_code": _optional_text(error_code, "error_code", 120),
            "error_message": _sanitized_text(
                error_message, "error_message", 800
            ),
            "readback_proof_kind": _optional_text(
                readback_proof_kind, "readback_proof_kind", 120
            ),
            "readback_digest": _optional_digest(
                readback_digest, "readback_digest"
            ),
            "native_event_key": _optional_text(
                native_event_key, "native_event_key", 240
            ),
        }
        with self._transaction("append_attempt_event") as conn:
            self._required_row(conn, ITEMS_TABLE, "change_item_id", change_item_id)
            existing = conn.execute(
                f"SELECT * FROM {ATTEMPT_EVENTS_TABLE} WHERE attempt_event_id=?",
                (row["attempt_event_id"],),
            ).fetchone()
            if existing is not None:
                if _row_matches(existing, row):
                    return dict(existing)
                raise ChangeRegistryConflict(
                    "attempt event identity owns different immutable bytes"
                )
            previous = conn.execute(
                f"""SELECT * FROM {ATTEMPT_EVENTS_TABLE}
                    WHERE attempt_id=? ORDER BY sequence_no DESC LIMIT 1""",
                (exact_attempt_id,),
            ).fetchone()
            if previous is None:
                if exact_sequence != 1 or exact_state != "created":
                    raise ChangeRegistryError(
                        "attempt lifecycle must begin with created sequence 1"
                    )
            else:
                if str(previous["change_item_id"]) != str(change_item_id):
                    raise ChangeRegistryConflict("attempt_id belongs to another item")
                if exact_sequence != int(previous["sequence_no"]) + 1:
                    raise ChangeRegistryError("attempt sequence must be contiguous")
                _validate_attempt_transition(str(previous["state"]), exact_state)
                if _timestamp_moment(row["occurred_at"]) < _timestamp_moment(
                    str(previous["occurred_at"])
                ):
                    raise ChangeRegistryError(
                        "attempt event timestamp precedes previous sequence"
                    )
            return self._insert_idempotent_conn(
                conn, ATTEMPT_EVENTS_TABLE, "attempt_event_id", row
            )

    def append_fact(
        self,
        *,
        fact_id: str,
        seller_id: str,
        account_scope: str,
        target: TargetIdentity,
        parameter_field: str,
        before_value: Any,
        after_value: Any,
        observed_from: str,
        observed_to: str,
        proven_at: str,
        proof_kind: str,
        evidence_digest: str,
    ) -> dict[str, Any]:
        exact_target = target_identity(
            target.target_kind,
            nm_id=target.nm_id,
            advert_id=target.advert_id,
            placement=target.placement,
        )
        field = _validate_parameter_field(exact_target.target_kind, parameter_field)
        before = _canonicalize_field_value(field, before_value)
        after = _canonicalize_field_value(field, after_value)
        _validate_field_value(field, before, requested=False)
        _validate_field_value(field, after, requested=True)
        if before == after:
            raise ChangeRegistryError("facts must prove a transition, not a repeated value")
        start = _timestamp(observed_from, "observed_from")
        end = _timestamp(observed_to, "observed_to")
        proven = _timestamp(proven_at, "proven_at")
        if _timestamp_moment(start) > _timestamp_moment(end):
            raise ChangeRegistryError("fact observed interval is reversed")
        if _timestamp_moment(end) > _timestamp_moment(proven):
            raise ChangeRegistryError("fact proven_at precedes observed interval")
        row = {
            "fact_id": _identifier(fact_id, "fact_id"),
            "seller_id": _identifier(seller_id, "seller_id"),
            "account_scope": _identifier(account_scope, "account_scope"),
            "target_kind": exact_target.target_kind,
            "nm_id": exact_target.nm_id,
            "advert_id": exact_target.advert_id,
            "placement": exact_target.placement,
            "parameter_field": field,
            **before.columns("before_value"),
            **after.columns("after_value"),
            "observed_from": start,
            "observed_to": end,
            "proven_at": proven,
            "proof_kind": _required_token(
                proof_kind, "proof_kind", PROOF_KINDS
            ),
            "evidence_digest": _digest(evidence_digest, "evidence_digest"),
            "mapping_version": MAPPING_VERSION,
        }
        return self._insert_idempotent(
            FACTS_TABLE, "fact_id", row, operation="append_fact"
        )

    def append_fact_link(
        self,
        *,
        fact_link_id: str,
        fact_id: str,
        link_kind: str,
        linked_id: str,
        linked_at: str,
        evidence_digest: str,
    ) -> dict[str, Any]:
        kind = _required_token(
            link_kind,
            "link_kind",
            {"change_item", "checkpoint", "native_audit", "recommendation_item"},
        )
        exact_linked_id = _identifier(linked_id, "linked_id", maximum=320)
        if kind in {"native_audit", "recommendation_item"}:
            exact_linked_id = _sanitized_text(
                exact_linked_id, "linked_id", 320
            )
        row = {
            "fact_link_id": _identifier(fact_link_id, "fact_link_id"),
            "fact_id": _identifier(fact_id, "fact_id"),
            "link_kind": kind,
            "change_item_id": exact_linked_id if kind == "change_item" else None,
            "checkpoint_id": exact_linked_id if kind == "checkpoint" else None,
            "native_audit_reference": (
                exact_linked_id if kind == "native_audit" else ""
            ),
            "recommendation_item_id": (
                exact_linked_id if kind == "recommendation_item" else ""
            ),
            "linked_at": _timestamp(linked_at, "linked_at"),
            "evidence_digest": _digest(evidence_digest, "evidence_digest"),
        }
        with self._transaction("append_fact_link") as conn:
            fact = self._required_row(conn, FACTS_TABLE, "fact_id", fact_id)
            if kind == "change_item":
                linked = self._required_row(
                    conn, ITEMS_TABLE, "change_item_id", exact_linked_id
                )
                identity_columns = (
                    "seller_id",
                    "account_scope",
                    "target_kind",
                    "nm_id",
                    "advert_id",
                    "placement",
                    "parameter_field",
                )
                if any(fact[column] != linked[column] for column in identity_columns):
                    raise ChangeRegistryConflict(
                        "fact and change item exact target identities differ"
                    )
            elif kind == "checkpoint":
                linked = self._required_row(
                    conn, CHECKPOINTS_TABLE, "checkpoint_id", exact_linked_id
                )
                if (
                    fact["seller_id"] != linked["seller_id"]
                    or fact["account_scope"] != linked["account_scope"]
                ):
                    raise ChangeRegistryConflict(
                        "fact and checkpoint seller scopes differ"
                    )
            return self._insert_idempotent_conn(
                conn, FACT_LINKS_TABLE, "fact_link_id", row
            )

    def append_checkpoint(
        self,
        *,
        checkpoint_id: str,
        seller_id: str,
        account_scope: str,
        source_surface: str,
        scan_kind: str,
        started_at: str,
        completed_at: str,
        completeness_status: str,
        expected_target_count: int,
        observed_target_count: int,
        completeness_digest: str,
        evidence_digest: str,
        previous_complete_checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        status = _required_token(
            completeness_status,
            "completeness_status",
            {"complete", "partial", "failed"},
        )
        expected = _non_negative_integer(
            expected_target_count, "expected_target_count"
        )
        observed = _non_negative_integer(
            observed_target_count, "observed_target_count"
        )
        if observed > expected or (status == "complete" and observed != expected):
            raise ChangeRegistryError("checkpoint completeness counts are inconsistent")
        row = {
            "checkpoint_id": _identifier(checkpoint_id, "checkpoint_id"),
            "seller_id": _identifier(seller_id, "seller_id"),
            "account_scope": _identifier(account_scope, "account_scope"),
            "source_surface": _identifier(source_surface, "source_surface"),
            "scan_kind": _required_token(
                scan_kind,
                "scan_kind",
                {"observer", "readback", "reconciliation", "manual"},
            ),
            "started_at": _timestamp(started_at, "started_at"),
            "completed_at": _timestamp(completed_at, "completed_at"),
            "completeness_status": status,
            "expected_target_count": expected,
            "observed_target_count": observed,
            "completeness_digest": _digest(
                completeness_digest, "completeness_digest"
            ),
            "evidence_digest": _digest(evidence_digest, "evidence_digest"),
            "previous_complete_checkpoint_id": (
                _identifier(
                    previous_complete_checkpoint_id,
                    "previous_complete_checkpoint_id",
                )
                if previous_complete_checkpoint_id
                else None
            ),
            "mapping_version": MAPPING_VERSION,
        }
        if _timestamp_moment(row["started_at"]) > _timestamp_moment(
            row["completed_at"]
        ):
            raise ChangeRegistryError("checkpoint completed_at precedes started_at")
        with self._transaction("append_checkpoint") as conn:
            previous_id = row["previous_complete_checkpoint_id"]
            if previous_id:
                previous = self._required_row(
                    conn, CHECKPOINTS_TABLE, "checkpoint_id", previous_id
                )
                if (
                    str(previous["completeness_status"]) != "complete"
                    or str(previous["seller_id"]) != row["seller_id"]
                    or str(previous["account_scope"]) != row["account_scope"]
                ):
                    raise ChangeRegistryError(
                        "previous checkpoint must be complete in the same seller scope"
                    )
                if _timestamp_moment(str(previous["completed_at"])) > _timestamp_moment(
                    row["completed_at"]
                ):
                    raise ChangeRegistryError(
                        "previous checkpoint completes after the new checkpoint"
                    )
            return self._insert_idempotent_conn(
                conn, CHECKPOINTS_TABLE, "checkpoint_id", row
            )

    def append_observation_value(
        self,
        *,
        observation_value_id: str,
        checkpoint_id: str,
        target: TargetIdentity,
        parameter_field: str,
        observation_status: str,
        value: Any,
        observed_at: str,
        evidence_digest: str,
        health_code: str = "",
        health_detail: str = "",
    ) -> dict[str, Any]:
        exact_target = target_identity(
            target.target_kind,
            nm_id=target.nm_id,
            advert_id=target.advert_id,
            placement=target.placement,
        )
        field = _validate_parameter_field(exact_target.target_kind, parameter_field)
        status = _required_token(
            observation_status, "observation_status", OBSERVATION_STATUSES
        )
        canonical = _canonicalize_field_value(field, value)
        _validate_field_value(field, canonical, requested=False)
        if status == "exact" and canonical.kind == "missing":
            raise ChangeRegistryError("exact observation cannot be missing")
        if status == "exact_zero" and canonical != CanonicalValue(
            "integer", integer_value=0
        ):
            raise ChangeRegistryError("exact_zero requires canonical integer zero")
        if status in {"missing", "inapplicable", "error"} and canonical.kind != "missing":
            raise ChangeRegistryError(f"{status} observation requires missing value")
        row = {
            "observation_value_id": _identifier(
                observation_value_id, "observation_value_id"
            ),
            "checkpoint_id": _identifier(checkpoint_id, "checkpoint_id"),
            "target_kind": exact_target.target_kind,
            "nm_id": exact_target.nm_id,
            "advert_id": exact_target.advert_id,
            "placement": exact_target.placement,
            "parameter_field": field,
            "observation_status": status,
            **canonical.columns("value"),
            "health_code": _optional_text(health_code, "health_code", 120),
            "health_detail": _sanitized_text(
                health_detail, "health_detail", 800
            ),
            "observed_at": _timestamp(observed_at, "observed_at"),
            "evidence_digest": _digest(evidence_digest, "evidence_digest"),
            "mapping_version": MAPPING_VERSION,
        }
        return self._insert_idempotent(
            OBSERVATION_VALUES_TABLE,
            "observation_value_id",
            row,
            operation="append_observation_value",
        )

    def append_identity_incident(
        self,
        *,
        incident_id: str,
        seller_id: str,
        account_scope: str,
        incident_kind: str,
        target_kind: str,
        advert_id: int,
        candidate_nm_ids: Sequence[int],
        source_surface: str,
        observed_at: str,
        evidence_digest: str,
    ) -> dict[str, Any]:
        kind = _required_token(
            incident_kind,
            "incident_kind",
            {
                "campaign_nm_mapping_cardinality",
                "invalid_target_identity",
                "identity_drift",
            },
        )
        exact_target_kind = _required_token(
            target_kind, "target_kind", TARGET_KINDS
        )
        candidates = sorted(
            {_positive_integer(item, "candidate_nm_id") for item in candidate_nm_ids}
        )
        exact_advert_id = _non_negative_integer(advert_id, "advert_id")
        if kind == "campaign_nm_mapping_cardinality" and (
            exact_target_kind != "campaign"
            or exact_advert_id <= 0
            or len(candidates) == 1
        ):
            raise ChangeRegistryError(
                "campaign mapping incident requires advert_id and cardinality zero or many"
            )
        row = {
            "incident_id": _identifier(incident_id, "incident_id"),
            "seller_id": _identifier(seller_id, "seller_id"),
            "account_scope": _identifier(account_scope, "account_scope"),
            "incident_kind": kind,
            "target_kind": exact_target_kind,
            "advert_id": exact_advert_id,
            "candidate_nm_ids_json": canonical_json(candidates),
            "candidate_count": len(candidates),
            "source_surface": _identifier(source_surface, "source_surface"),
            "observed_at": _timestamp(observed_at, "observed_at"),
            "evidence_digest": _digest(evidence_digest, "evidence_digest"),
            "mapping_version": MAPPING_VERSION,
        }
        return self._insert_idempotent(
            IDENTITY_INCIDENTS_TABLE,
            "incident_id",
            row,
            operation="append_identity_incident",
        )

    def resolve_campaign_identity(
        self,
        *,
        incident_id: str,
        seller_id: str,
        account_scope: str,
        advert_id: int,
        candidate_nm_ids: Sequence[int],
        source_surface: str,
        observed_at: str,
        evidence_digest: str,
    ) -> TargetIdentity | None:
        candidates = sorted(
            {_positive_integer(item, "candidate_nm_id") for item in candidate_nm_ids}
        )
        if len(candidates) == 1:
            return target_identity(
                "campaign", nm_id=candidates[0], advert_id=advert_id
            )
        self.append_identity_incident(
            incident_id=incident_id,
            seller_id=seller_id,
            account_scope=account_scope,
            incident_kind="campaign_nm_mapping_cardinality",
            target_kind="campaign",
            advert_id=advert_id,
            candidate_nm_ids=candidates,
            source_surface=source_surface,
            observed_at=observed_at,
            evidence_digest=evidence_digest,
        )
        return None

    def append_annotation_revision(
        self,
        *,
        annotation_revision_id: str,
        subject_kind: str,
        subject_id: str,
        actor_principal: str,
        created_at: str,
        reason: str = "",
        comment: str = "",
        parent_revision_id: str | None = None,
    ) -> dict[str, Any]:
        kind = _required_token(
            subject_kind,
            "subject_kind",
            {
                "operation",
                "change_item",
                "fact",
                "checkpoint",
                "identity_incident",
                "manual_pending",
            },
        )
        exact_subject_id = _identifier(subject_id, "subject_id", maximum=160)
        with self._transaction("append_annotation_revision") as conn:
            _require_annotation_subject(conn, kind, exact_subject_id)
            existing = conn.execute(
                f"""SELECT * FROM {ANNOTATION_REVISIONS_TABLE}
                    WHERE annotation_revision_id=?""",
                (annotation_revision_id,),
            ).fetchone()
            if existing is not None:
                expected = {
                    "annotation_revision_id": _identifier(
                        annotation_revision_id, "annotation_revision_id"
                    ),
                    "subject_kind": kind,
                    "subject_id": exact_subject_id,
                    "parent_revision_id": parent_revision_id,
                    "actor_principal": _identifier(
                        actor_principal, "actor_principal", maximum=160
                    ),
                    "reason": _optional_text(reason, "reason", 1000),
                    "comment": _optional_text(comment, "comment", 4000),
                    "created_at": _timestamp(created_at, "created_at"),
                }
                if all(existing[key] == value for key, value in expected.items()):
                    return dict(existing)
                raise ChangeRegistryConflict(
                    "annotation revision identity owns different immutable bytes"
                )
            latest = conn.execute(
                f"""SELECT * FROM {ANNOTATION_REVISIONS_TABLE}
                    WHERE subject_kind=? AND subject_id=?
                    ORDER BY revision_no DESC LIMIT 1""",
                (kind, exact_subject_id),
            ).fetchone()
            expected_parent = (
                str(latest["annotation_revision_id"]) if latest is not None else None
            )
            if parent_revision_id != expected_parent:
                raise ChangeRegistryConflict(
                    "annotation parent must be the current subject revision"
                )
            exact_created_at = _timestamp(created_at, "created_at")
            if latest is not None and _timestamp_moment(
                str(latest["created_at"])
            ) > _timestamp_moment(exact_created_at):
                raise ChangeRegistryError(
                    "annotation timestamp precedes parent revision"
                )
            row = {
                "annotation_revision_id": _identifier(
                    annotation_revision_id, "annotation_revision_id"
                ),
                "subject_kind": kind,
                "subject_id": exact_subject_id,
                "revision_no": int(latest["revision_no"]) + 1 if latest else 1,
                "parent_revision_id": expected_parent,
                "actor_principal": _identifier(
                    actor_principal, "actor_principal", maximum=160
                ),
                "reason": _optional_text(reason, "reason", 1000),
                "comment": _optional_text(comment, "comment", 4000),
                "created_at": exact_created_at,
            }
            return self._insert_idempotent_conn(
                conn,
                ANNOTATION_REVISIONS_TABLE,
                "annotation_revision_id",
                row,
            )

    def append_manual_pending_event(
        self,
        *,
        pending_event_id: str,
        pending_id: str,
        change_item_id: str,
        sequence_no: int,
        state: str,
        occurred_at: str,
        evidence_digest: str,
        related_fact_id: str | None = None,
        supersedes_pending_id: str = "",
        native_event_key: str = "",
        expected_pointer_revision: int | None = None,
    ) -> dict[str, Any]:
        exact_pending_id = _identifier(pending_id, "pending_id")
        exact_sequence = _positive_integer(sequence_no, "sequence_no")
        exact_state = _required_token(
            state, "state", MANUAL_PENDING_STATES
        )
        exact_fact_id = (
            _identifier(related_fact_id, "related_fact_id")
            if related_fact_id
            else None
        )
        if (exact_state in {"matched", "deviated"}) != bool(exact_fact_id):
            raise ChangeRegistryError(
                "matched/deviated pending events require a proven related fact"
            )
        row = {
            "pending_event_id": _identifier(
                pending_event_id, "pending_event_id"
            ),
            "pending_id": exact_pending_id,
            "change_item_id": _identifier(change_item_id, "change_item_id"),
            "sequence_no": exact_sequence,
            "state": exact_state,
            "related_fact_id": exact_fact_id,
            "supersedes_pending_id": _optional_text(
                supersedes_pending_id, "supersedes_pending_id", 120
            ),
            "occurred_at": _timestamp(occurred_at, "occurred_at"),
            "evidence_digest": _digest(evidence_digest, "evidence_digest"),
            "native_event_key": _optional_text(
                native_event_key, "native_event_key", 240
            ),
        }
        with self._transaction("append_manual_pending_event") as conn:
            item = self._required_row(
                conn, ITEMS_TABLE, "change_item_id", change_item_id
            )
            if exact_fact_id:
                self._required_row(conn, FACTS_TABLE, "fact_id", exact_fact_id)
            existing = conn.execute(
                f"""SELECT * FROM {MANUAL_PENDING_EVENTS_TABLE}
                    WHERE pending_event_id=?""",
                (row["pending_event_id"],),
            ).fetchone()
            if existing is not None:
                if _row_matches(existing, row):
                    return dict(existing)
                raise ChangeRegistryConflict(
                    "manual pending event identity owns different immutable bytes"
                )
            previous = conn.execute(
                f"""SELECT * FROM {MANUAL_PENDING_EVENTS_TABLE}
                    WHERE pending_id=? ORDER BY sequence_no DESC LIMIT 1""",
                (exact_pending_id,),
            ).fetchone()
            if previous is None:
                if exact_sequence != 1 or exact_state != "pending":
                    raise ChangeRegistryError(
                        "manual pending lifecycle must begin with pending sequence 1"
                    )
            else:
                if str(previous["change_item_id"]) != str(change_item_id):
                    raise ChangeRegistryConflict("pending_id belongs to another item")
                if str(previous["state"]) != "pending":
                    raise ChangeRegistryConflict("manual pending lifecycle is terminal")
                if exact_sequence != int(previous["sequence_no"]) + 1:
                    raise ChangeRegistryError("pending sequence must be contiguous")
                if exact_state == "pending":
                    raise ChangeRegistryError("pending state cannot repeat")
                if _timestamp_moment(row["occurred_at"]) < _timestamp_moment(
                    str(previous["occurred_at"])
                ):
                    raise ChangeRegistryError(
                        "pending event timestamp precedes previous sequence"
                    )
            event = self._insert_idempotent_conn(
                conn, MANUAL_PENDING_EVENTS_TABLE, "pending_event_id", row
            )
            target_columns = (
                "seller_id",
                "account_scope",
                "target_kind",
                "nm_id",
                "advert_id",
                "placement",
                "parameter_field",
            )
            target_values = tuple(item[column] for column in target_columns)
            pointer = conn.execute(
                f"""SELECT * FROM {MANUAL_PENDING_CURRENT_TABLE}
                    WHERE seller_id=? AND account_scope=? AND target_kind=?
                      AND nm_id=? AND advert_id=? AND placement=?
                      AND parameter_field=?""",
                target_values,
            ).fetchone()
            if expected_pointer_revision is not None:
                actual_revision = int(pointer["revision"]) if pointer else 0
                if actual_revision != expected_pointer_revision:
                    raise ChangeRegistryConflict(
                        "manual pending coordination revision changed"
                    )
            if previous is None:
                if pointer is not None and int(pointer["active"]) == 1:
                    raise ChangeRegistryConflict(
                        "another manual pending item owns this exact target"
                    )
                if pointer is None:
                    values = {
                        **{column: item[column] for column in target_columns},
                        "current_pending_id": exact_pending_id,
                        "current_event_id": event["pending_event_id"],
                        "active": 1,
                        "revision": 1,
                        "updated_at": row["occurred_at"],
                    }
                    _plain_insert(conn, MANUAL_PENDING_CURRENT_TABLE, values)
                else:
                    conn.execute(
                        f"""UPDATE {MANUAL_PENDING_CURRENT_TABLE}
                            SET current_pending_id=?,current_event_id=?,active=1,
                                revision=revision+1,updated_at=?
                            WHERE seller_id=? AND account_scope=? AND target_kind=?
                              AND nm_id=? AND advert_id=? AND placement=?
                              AND parameter_field=?""",
                        (
                            exact_pending_id,
                            event["pending_event_id"],
                            row["occurred_at"],
                            *target_values,
                        ),
                    )
            else:
                if (
                    pointer is None
                    or int(pointer["active"]) != 1
                    or str(pointer["current_pending_id"]) != exact_pending_id
                ):
                    raise ChangeRegistryConflict(
                        "manual pending coordination pointer does not own lifecycle"
                    )
                conn.execute(
                    f"""UPDATE {MANUAL_PENDING_CURRENT_TABLE}
                        SET current_event_id=?,active=0,revision=revision+1,updated_at=?
                        WHERE seller_id=? AND account_scope=? AND target_kind=?
                          AND nm_id=? AND advert_id=? AND placement=?
                          AND parameter_field=?""",
                    (event["pending_event_id"], row["occurred_at"], *target_values),
                )
            return event

    def read_operation(self, operation_id: str) -> dict[str, Any]:
        exact_id = _identifier(operation_id, "operation_id")
        with self._read_session("read_operation") as conn:
            operation = self._required_row(
                conn, OPERATIONS_TABLE, "operation_id", exact_id
            )
            items = conn.execute(
                f"""SELECT * FROM {ITEMS_TABLE} WHERE operation_id=?
                    ORDER BY created_at,change_item_id""",
                (exact_id,),
            ).fetchall()
            annotations = conn.execute(
                f"""SELECT * FROM {ANNOTATION_REVISIONS_TABLE}
                    WHERE subject_kind='operation' AND subject_id=?
                    ORDER BY revision_no,annotation_revision_id""",
                (exact_id,),
            ).fetchall()
            return {
                "operation": dict(operation),
                "items": [dict(row) for row in items],
                "annotations": [dict(row) for row in annotations],
            }

    def find_operation_by_receipt_reference(
        self, receipt_reference: str
    ) -> dict[str, Any] | None:
        exact_reference = _sanitized_text(
            receipt_reference, "receipt_reference", 320
        )
        if not exact_reference:
            raise ChangeRegistryError("receipt_reference is required")
        with self._read_session("find_operation_by_receipt_reference") as conn:
            rows = conn.execute(
                f"""SELECT DISTINCT item.operation_id
                    FROM {ATTEMPT_EVENTS_TABLE} event
                    JOIN {ITEMS_TABLE} item
                      ON item.change_item_id=event.change_item_id
                    WHERE event.receipt_reference=?
                    ORDER BY item.operation_id""",
                (exact_reference,),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ChangeRegistryConflict(
                "receipt reference resolves to multiple operations"
            )
        return self.read_operation(str(rows[0]["operation_id"]))

    def append_writer_operation_state(
        self,
        *,
        operation_id: str,
        state: str,
        occurred_at: str,
        receipt_reference: str = "",
        receipt_digest: str = "",
        error_code: str = "",
        error_message: str = "",
        readback_proof_kind: str = "",
        readback_digest: str = "",
        resolution_state: str = "",
    ) -> dict[str, Any]:
        """Append the same lifecycle transition to all atomic writer attempts."""

        exact_operation_id = _identifier(operation_id, "operation_id")
        exact_state = _required_token(state, "state", ATTEMPT_STATES)
        exact_occurred_at = _timestamp(occurred_at, "occurred_at")
        exact_resolution = str(resolution_state or "").strip().lower()
        if (exact_state == "resolved") != bool(exact_resolution):
            raise ChangeRegistryError(
                "resolved attempt events require one terminal resolution_state"
            )
        if exact_resolution and exact_resolution not in {
            "confirmed",
            "failed",
            "rejected",
            "cancelled",
        }:
            raise ChangeRegistryError("invalid attempt resolution_state")
        common = {
            "receipt_reference": _sanitized_text(
                receipt_reference, "receipt_reference", 320
            ),
            "receipt_digest": _optional_digest(
                receipt_digest, "receipt_digest"
            ),
            "error_code": _optional_text(error_code, "error_code", 120),
            "error_message": _sanitized_text(
                error_message, "error_message", 800
            ),
            "readback_proof_kind": _optional_text(
                readback_proof_kind, "readback_proof_kind", 120
            ),
            "readback_digest": _optional_digest(
                readback_digest, "readback_digest"
            ),
        }
        with self._transaction("append_writer_operation_state") as conn:
            self._required_row(
                conn, OPERATIONS_TABLE, "operation_id", exact_operation_id
            )
            items = conn.execute(
                f"""SELECT * FROM {ITEMS_TABLE} WHERE operation_id=?
                    ORDER BY change_item_id""",
                (exact_operation_id,),
            ).fetchall()
            if not items:
                raise ChangeRegistryConflict(
                    "writer operation has no atomic items"
                )
            stored: list[dict[str, Any]] = []
            for item in items:
                previous = conn.execute(
                    f"""SELECT * FROM {ATTEMPT_EVENTS_TABLE}
                        WHERE change_item_id=?
                        ORDER BY sequence_no DESC LIMIT 1""",
                    (item["change_item_id"],),
                ).fetchone()
                if previous is None:
                    raise ChangeRegistryConflict(
                        "writer item has no created attempt"
                    )
                previous_state = str(previous["state"])
                previous_resolution = str(previous["resolution_state"])
                if previous_state == exact_state and previous_resolution == exact_resolution:
                    stored.append(dict(previous))
                    continue
                if previous_state in {
                    "confirmed",
                    "failed",
                    "rejected",
                    "cancelled",
                    "resolved",
                }:
                    if (
                        exact_state == "resolved"
                        and previous_state == "resolved"
                        and previous_resolution == exact_resolution
                    ):
                        stored.append(dict(previous))
                        continue
                    raise ChangeRegistryConflict(
                        "writer attempt is already terminal"
                    )
                _validate_attempt_transition(previous_state, exact_state)
                if _timestamp_moment(exact_occurred_at) < _timestamp_moment(
                    str(previous["occurred_at"])
                ):
                    raise ChangeRegistryError(
                        "attempt event timestamp precedes previous sequence"
                    )
                sequence_no = int(previous["sequence_no"]) + 1
                event_basis = {
                    "attempt_id": str(previous["attempt_id"]),
                    "sequence_no": sequence_no,
                    "state": exact_state,
                    "resolution_state": exact_resolution,
                    "occurred_at": exact_occurred_at,
                    **common,
                }
                row = {
                    "attempt_event_id": _stable_registry_id(
                        "crae", event_basis
                    ),
                    "attempt_id": str(previous["attempt_id"]),
                    "change_item_id": str(item["change_item_id"]),
                    "sequence_no": sequence_no,
                    "state": exact_state,
                    "resolution_state": exact_resolution,
                    "occurred_at": exact_occurred_at,
                    **common,
                    "native_event_key": _optional_text(
                        f"{exact_state}:{exact_resolution}:{common['receipt_reference']}"
                        [:240],
                        "native_event_key",
                        240,
                    ),
                }
                stored.append(
                    self._insert_idempotent_conn(
                        conn,
                        ATTEMPT_EVENTS_TABLE,
                        "attempt_event_id",
                        row,
                    )
                )
            return {"operation_id": exact_operation_id, "events": stored}

    def confirm_writer_operation(
        self,
        *,
        operation_id: str,
        confirmed_values: Mapping[str, Any],
        confirmed_at: str,
        readback_digest: str,
        receipt_reference: str = "",
        native_audit_references: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Atomically confirm exact items, create/reuse facts and late-link proof."""

        exact_operation_id = _identifier(operation_id, "operation_id")
        exact_confirmed_at = _timestamp(confirmed_at, "confirmed_at")
        exact_readback_digest = _digest(
            readback_digest, "readback_digest"
        )
        exact_receipt_reference = _sanitized_text(
            receipt_reference, "receipt_reference", 320
        )
        exact_native_references = tuple(
            _sanitized_text(item, "native_audit_reference", 320)
            for item in native_audit_references
            if str(item or "").strip()
        )
        with self._transaction("confirm_writer_operation") as conn:
            self._required_row(
                conn, OPERATIONS_TABLE, "operation_id", exact_operation_id
            )
            items = conn.execute(
                f"""SELECT * FROM {ITEMS_TABLE} WHERE operation_id=?
                    ORDER BY change_item_id""",
                (exact_operation_id,),
            ).fetchall()
            if not items:
                raise ChangeRegistryConflict(
                    "writer operation has no atomic items"
                )
            facts: list[dict[str, Any]] = []
            events: list[dict[str, Any]] = []
            for item in items:
                item_id = str(item["change_item_id"])
                if item_id not in confirmed_values:
                    raise ChangeRegistryError(
                        "confirmed readback is missing an atomic item"
                    )
                field = str(item["parameter_field"])
                after = _canonicalize_field_value(
                    field, confirmed_values[item_id]
                )
                _validate_field_value(field, after, requested=True)
                before = CanonicalValue(
                    str(item["before_value_kind"]),
                    item["before_value_integer"],
                    item["before_value_text"],
                )
                target = TargetIdentity(
                    str(item["target_kind"]),
                    int(item["nm_id"]),
                    int(item["advert_id"]),
                    str(item["placement"]),
                )
                fact: dict[str, Any] | None = None
                if before != after:
                    fact = _find_reconcilable_transition_fact(
                        conn,
                        seller_id=str(item["seller_id"]),
                        account_scope=str(item["account_scope"]),
                        target=target,
                        parameter_field=field,
                        before=before,
                        after=after,
                        observed_from=str(item["created_at"]),
                        observed_to=exact_confirmed_at,
                        incoming_proof_kind="wb_readback",
                    )
                    if fact is None:
                        fact_basis = {
                            "operation_id": exact_operation_id,
                            "change_item_id": item_id,
                            "target": {
                                "target_kind": target.target_kind,
                                "nm_id": target.nm_id,
                                "advert_id": target.advert_id,
                                "placement": target.placement,
                            },
                            "parameter_field": field,
                            "before": _canonical_value_payload(before),
                            "after": _canonical_value_payload(after),
                            "observed_from": str(item["created_at"]),
                            "observed_to": exact_confirmed_at,
                            "readback_digest": exact_readback_digest,
                        }
                        fact_row = {
                            "fact_id": _stable_registry_id("crf", fact_basis),
                            "seller_id": str(item["seller_id"]),
                            "account_scope": str(item["account_scope"]),
                            "target_kind": target.target_kind,
                            "nm_id": target.nm_id,
                            "advert_id": target.advert_id,
                            "placement": target.placement,
                            "parameter_field": field,
                            **before.columns("before_value"),
                            **after.columns("after_value"),
                            "observed_from": str(item["created_at"]),
                            "observed_to": exact_confirmed_at,
                            "proven_at": exact_confirmed_at,
                            "proof_kind": "wb_readback",
                            "evidence_digest": canonical_digest(fact_basis),
                            "mapping_version": MAPPING_VERSION,
                        }
                        fact = self._insert_idempotent_conn(
                            conn, FACTS_TABLE, "fact_id", fact_row
                        )
                    fact_id = str(fact["fact_id"])
                    _append_fact_link_conn(
                        self,
                        conn,
                        fact_id=fact_id,
                        link_kind="change_item",
                        linked_id=item_id,
                        linked_at=exact_confirmed_at,
                        evidence_basis={
                            "fact_id": fact_id,
                            "change_item_id": item_id,
                            "readback_digest": exact_readback_digest,
                        },
                    )
                    for native_reference in exact_native_references:
                        _append_fact_link_conn(
                            self,
                            conn,
                            fact_id=fact_id,
                            link_kind="native_audit",
                            linked_id=native_reference,
                            linked_at=exact_confirmed_at,
                            evidence_basis={
                                "fact_id": fact_id,
                                "native_audit_reference": native_reference,
                                "readback_digest": exact_readback_digest,
                            },
                        )
                    recommendation_id = str(
                        item["recommendation_item_id"] or ""
                    )
                    if recommendation_id:
                        _append_fact_link_conn(
                            self,
                            conn,
                            fact_id=fact_id,
                            link_kind="recommendation_item",
                            linked_id=recommendation_id,
                            linked_at=exact_confirmed_at,
                            evidence_basis={
                                "fact_id": fact_id,
                                "recommendation_item_id": recommendation_id,
                                "readback_digest": exact_readback_digest,
                            },
                        )
                    facts.append(dict(fact))

                previous = conn.execute(
                    f"""SELECT * FROM {ATTEMPT_EVENTS_TABLE}
                        WHERE change_item_id=?
                        ORDER BY sequence_no DESC LIMIT 1""",
                    (item_id,),
                ).fetchone()
                if previous is None:
                    raise ChangeRegistryConflict(
                        "writer item has no attempt lifecycle"
                    )
                previous_state = str(previous["state"])
                previous_resolution = str(previous["resolution_state"])
                if previous_state == "confirmed" or (
                    previous_state == "resolved"
                    and previous_resolution == "confirmed"
                ):
                    events.append(dict(previous))
                    continue
                if previous_state == "submitted":
                    next_state = "confirmed"
                    resolution = ""
                elif previous_state == "ambiguous":
                    next_state = "resolved"
                    resolution = "confirmed"
                else:
                    raise ChangeRegistryConflict(
                        "writer confirmation requires submitted or ambiguous state"
                    )
                sequence_no = int(previous["sequence_no"]) + 1
                event_basis = {
                    "attempt_id": str(previous["attempt_id"]),
                    "sequence_no": sequence_no,
                    "state": next_state,
                    "resolution_state": resolution,
                    "readback_digest": exact_readback_digest,
                }
                event_row = {
                    "attempt_event_id": _stable_registry_id(
                        "crae", event_basis
                    ),
                    "attempt_id": str(previous["attempt_id"]),
                    "change_item_id": item_id,
                    "sequence_no": sequence_no,
                    "state": next_state,
                    "resolution_state": resolution,
                    "occurred_at": exact_confirmed_at,
                    "receipt_reference": exact_receipt_reference,
                    "receipt_digest": "",
                    "error_code": "",
                    "error_message": "",
                    "readback_proof_kind": "wb_readback",
                    "readback_digest": exact_readback_digest,
                    "native_event_key": f"{next_state}:wb_readback",
                }
                events.append(
                    self._insert_idempotent_conn(
                        conn,
                        ATTEMPT_EVENTS_TABLE,
                        "attempt_event_id",
                        event_row,
                    )
                )
            return {
                "operation_id": exact_operation_id,
                "facts": facts,
                "events": events,
            }

    def read_fact(self, fact_id: str) -> dict[str, Any]:
        exact_id = _identifier(fact_id, "fact_id")
        with self._read_session("read_fact") as conn:
            fact = self._required_row(conn, FACTS_TABLE, "fact_id", exact_id)
            links = conn.execute(
                f"""SELECT * FROM {FACT_LINKS_TABLE} WHERE fact_id=?
                    ORDER BY linked_at,fact_link_id""",
                (exact_id,),
            ).fetchall()
            return {"fact": dict(fact), "links": [dict(row) for row in links]}

    def list_operations(
        self,
        *,
        seller_id: str,
        account_scope: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._list_scope_rows(
            table=OPERATIONS_TABLE,
            entity="operation",
            id_column="operation_id",
            time_column="created_at",
            seller_id=seller_id,
            account_scope=account_scope,
            limit=limit,
            cursor=cursor,
        )

    def list_facts(
        self,
        *,
        seller_id: str,
        account_scope: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._list_scope_rows(
            table=FACTS_TABLE,
            entity="fact",
            id_column="fact_id",
            time_column="proven_at",
            seller_id=seller_id,
            account_scope=account_scope,
            limit=limit,
            cursor=cursor,
        )

    def _list_scope_rows(
        self,
        *,
        table: str,
        entity: str,
        id_column: str,
        time_column: str,
        seller_id: str,
        account_scope: str,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        exact_seller = _identifier(seller_id, "seller_id")
        exact_account = _identifier(account_scope, "account_scope")
        exact_limit = _bounded_limit(limit)
        cursor_values = _decode_cursor(cursor, entity) if cursor else None
        conditions = ["seller_id=?", "account_scope=?"]
        params: list[Any] = [exact_seller, exact_account]
        if cursor_values:
            conditions.append(f"({time_column}>? OR ({time_column}=? AND {id_column}>?))")
            params.extend(
                [
                    cursor_values[0],
                    cursor_values[0],
                    cursor_values[1],
                ]
            )
        params.append(exact_limit + 1)
        with self._read_session(f"list_{entity}s") as conn:
            rows = conn.execute(
                f"""SELECT * FROM {table}
                    WHERE {' AND '.join(conditions)}
                    ORDER BY {time_column},{id_column} LIMIT ?""",
                tuple(params),
            ).fetchall()
        page = rows[:exact_limit]
        next_cursor = ""
        if len(rows) > exact_limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(
                entity, str(last[time_column]), str(last[id_column])
            )
        return {"items": [dict(row) for row in page], "next_cursor": next_cursor}

    def _insert_idempotent(
        self,
        table: str,
        identity_column: str,
        row: Mapping[str, Any],
        *,
        operation: str,
    ) -> dict[str, Any]:
        with self._transaction(operation) as conn:
            return self._insert_idempotent_conn(
                conn, table, identity_column, row
            )

    def _insert_idempotent_conn(
        self,
        conn: sqlite3.Connection,
        table: str,
        identity_column: str,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            _plain_insert(conn, table, row)
        except sqlite3.IntegrityError as exc:
            existing = conn.execute(
                f"SELECT * FROM {table} WHERE {identity_column}=?",
                (row[identity_column],),
            ).fetchone()
            if existing is not None and _row_matches(existing, row):
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

    @contextmanager
    def _transaction(self, operation: str) -> Iterator[sqlite3.Connection]:
        with self.store_registry.session(
            "operational",
            mode="rw",
            operation=f"change_registry_{operation}",
        ) as conn:
            _require_initialized_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def _read_session(self, operation: str) -> Iterator[sqlite3.Connection]:
        with self.store_registry.session(
            "operational",
            mode="ro",
            operation=f"change_registry_{operation}",
        ) as conn:
            _require_initialized_schema(conn)
            yield conn

    @staticmethod
    def _required_row(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        identity: Any,
    ) -> sqlite3.Row:
        row = conn.execute(
            f"SELECT * FROM {table} WHERE {column}=?", (identity,)
        ).fetchone()
        if row is None:
            raise ChangeRegistryNotFound(f"{table} identity is missing")
        return row


def _plain_insert(
    conn: sqlite3.Connection, table: str, row: Mapping[str, Any]
) -> None:
    columns = tuple(row)
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
        tuple(row[column] for column in columns),
    )


def _stable_registry_id(prefix: str, basis: Mapping[str, Any]) -> str:
    return f"{prefix}_{hashlib.sha256(canonical_json(basis).encode('utf-8')).hexdigest()}"


def _canonical_value_payload(value: CanonicalValue) -> dict[str, Any]:
    return {
        "kind": value.kind,
        "integer": value.integer_value,
        "text": value.text_value,
    }


def find_reconcilable_transition_fact(
    conn: sqlite3.Connection,
    *,
    seller_id: str,
    account_scope: str,
    target: TargetIdentity,
    parameter_field: str,
    before: CanonicalValue,
    after: CanonicalValue,
    observed_from: str,
    observed_to: str,
    incoming_proof_kind: str,
) -> dict[str, Any] | None:
    """Find one exact cross-proof transition whose interval contains the other."""

    exact_kind = _required_token(
        incoming_proof_kind,
        "incoming_proof_kind",
        {"wb_readback", "checkpoint_diff"},
    )
    candidate_kinds = (
        ("checkpoint_diff",)
        if exact_kind == "wb_readback"
        else ("wb_readback", "native_audit", "reconciliation")
    )
    placeholders = ",".join("?" for _ in candidate_kinds)
    rows = conn.execute(
        f"""SELECT * FROM {FACTS_TABLE}
            WHERE seller_id=? AND account_scope=? AND target_kind=? AND nm_id=?
              AND advert_id=? AND placement=? AND parameter_field=?
              AND before_value_kind=? AND before_value_integer IS ?
              AND before_value_text IS ? AND after_value_kind=?
              AND after_value_integer IS ? AND after_value_text IS ?
              AND proof_kind IN ({placeholders})
            ORDER BY observed_from,observed_to,fact_id""",
        (
            seller_id,
            account_scope,
            target.target_kind,
            target.nm_id,
            target.advert_id,
            target.placement,
            parameter_field,
            before.kind,
            before.integer_value,
            before.text_value,
            after.kind,
            after.integer_value,
            after.text_value,
            *candidate_kinds,
        ),
    ).fetchall()
    incoming_start = _timestamp_moment(observed_from)
    incoming_end = _timestamp_moment(observed_to)
    candidates: list[sqlite3.Row] = []
    for row in rows:
        candidate_start = _timestamp_moment(str(row["observed_from"]))
        candidate_end = _timestamp_moment(str(row["observed_to"]))
        contained = (
            candidate_start <= incoming_start <= candidate_end
            if exact_kind == "wb_readback"
            else incoming_start <= candidate_start <= incoming_end
        )
        if contained:
            candidates.append(row)
    if len(candidates) > 1:
        raise ChangeRegistryConflict(
            "exact transition/provenance reconciliation is ambiguous"
        )
    return dict(candidates[0]) if candidates else None


_find_reconcilable_transition_fact = find_reconcilable_transition_fact


def append_fact_link_in_transaction(
    repository: "ChangeRegistryRepository",
    conn: sqlite3.Connection,
    *,
    fact_id: str,
    link_kind: str,
    linked_id: str,
    linked_at: str,
    evidence_basis: Mapping[str, Any],
) -> dict[str, Any]:
    kind = _required_token(
        link_kind,
        "link_kind",
        {"change_item", "checkpoint", "native_audit", "recommendation_item"},
    )
    exact_linked_id = _identifier(linked_id, "linked_id", maximum=320)
    if kind in {"native_audit", "recommendation_item"}:
        exact_linked_id = _sanitized_text(exact_linked_id, "linked_id", 320)
    row = {
        "fact_link_id": _stable_registry_id(
            "crfl",
            {"fact_id": fact_id, "link_kind": kind, "linked_id": exact_linked_id},
        ),
        "fact_id": _identifier(fact_id, "fact_id"),
        "link_kind": kind,
        "change_item_id": exact_linked_id if kind == "change_item" else None,
        "checkpoint_id": exact_linked_id if kind == "checkpoint" else None,
        "native_audit_reference": exact_linked_id if kind == "native_audit" else "",
        "recommendation_item_id": (
            exact_linked_id if kind == "recommendation_item" else ""
        ),
        "linked_at": _timestamp(linked_at, "linked_at"),
        "evidence_digest": canonical_digest(evidence_basis),
    }
    return repository._insert_idempotent_conn(
        conn, FACT_LINKS_TABLE, "fact_link_id", row
    )


_append_fact_link_conn = append_fact_link_in_transaction


def _require_initialized_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (OPERATIONS_TABLE,),
    ).fetchone()
    if row is None:
        raise ChangeRegistryError(
            "change registry schema is not initialized in the selected operational store"
        )


def _row_matches(row: sqlite3.Row, expected: Mapping[str, Any]) -> bool:
    return all(row[key] == value for key, value in expected.items())


def _require_annotation_subject(
    conn: sqlite3.Connection, subject_kind: str, subject_id: str
) -> None:
    mapping = {
        "operation": (OPERATIONS_TABLE, "operation_id"),
        "change_item": (ITEMS_TABLE, "change_item_id"),
        "fact": (FACTS_TABLE, "fact_id"),
        "checkpoint": (CHECKPOINTS_TABLE, "checkpoint_id"),
        "identity_incident": (IDENTITY_INCIDENTS_TABLE, "incident_id"),
        "manual_pending": (MANUAL_PENDING_EVENTS_TABLE, "pending_id"),
    }
    table, column = mapping[subject_kind]
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (subject_id,)
    ).fetchone()
    if row is None:
        raise ChangeRegistryNotFound("annotation subject is missing")


def _validate_attempt_transition(previous: str, current: str) -> None:
    allowed = {
        "created": {"submitted", "failed", "rejected", "cancelled", "ambiguous"},
        "submitted": {
            "confirmed",
            "failed",
            "rejected",
            "cancelled",
            "ambiguous",
        },
        "ambiguous": {"resolved"},
    }
    if current not in allowed.get(previous, set()):
        raise ChangeRegistryError(
            f"invalid attempt lifecycle transition: {previous} -> {current}"
        )


def _validate_parameter_field(target_kind: str, parameter_field: str) -> str:
    field = str(parameter_field or "").strip().lower()
    allowed = {
        "price": PRICE_FIELDS,
        "bid": BID_FIELDS,
        "campaign": CAMPAIGN_FIELDS,
    }[target_kind]
    if field not in allowed:
        raise ChangeRegistryError(
            f"{field or '<missing>'} is not valid for {target_kind} target"
        )
    return field


def _validate_field_value(
    parameter_field: str,
    value: CanonicalValue,
    *,
    requested: bool,
) -> None:
    if value.kind not in VALUE_KINDS:
        raise ChangeRegistryError("unsupported canonical value kind")
    if parameter_field in NUMERIC_FIELDS:
        permitted = {"integer"} if requested else {"missing", "null", "integer"}
        if value.kind not in permitted:
            raise ChangeRegistryError(f"{parameter_field} must use integer canonical value")
        if value.kind == "integer":
            exact = int(value.integer_value or 0)
            if exact < 0:
                raise ChangeRegistryError(f"{parameter_field} cannot be negative")
            if parameter_field == "discount_bps" and exact > 10_000:
                raise ChangeRegistryError("discount_bps must be between 0 and 10000")
    else:
        permitted = {"text"} if requested else {"missing", "null", "text"}
        if value.kind not in permitted:
            raise ChangeRegistryError(f"{parameter_field} must use text canonical value")
        if value.kind == "text" and not str(value.text_value or "").strip():
            raise ChangeRegistryError(f"{parameter_field} text cannot be empty")


def _canonicalize_field_value(parameter_field: str, value: Any) -> CanonicalValue:
    canonical = canonicalize_value(value)
    if parameter_field in TEXT_FIELDS and canonical.kind == "text":
        token = str(canonical.text_value or "").strip().lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9_:-]{0,119}", token) is None:
            raise ChangeRegistryError(
                f"{parameter_field} must be a canonical mapping token"
            )
        return CanonicalValue("text", text_value=token)
    return canonical


def _value_storage_check(prefix: str) -> str:
    return f"""{prefix}_kind IN ('missing','null','integer','text','boolean')
        AND (
            ({prefix}_kind IN ('missing','null')
                AND {prefix}_integer IS NULL AND {prefix}_text IS NULL)
            OR ({prefix}_kind='integer' AND typeof({prefix}_integer)='integer'
                AND {prefix}_text IS NULL)
            OR ({prefix}_kind='boolean' AND {prefix}_integer IN (0,1)
                AND {prefix}_text IS NULL)
            OR ({prefix}_kind='text' AND {prefix}_integer IS NULL
                AND typeof({prefix}_text)='text' AND length({prefix}_text)<=512)
        )"""


def _target_storage_check() -> str:
    return """typeof(nm_id)='integer' AND nm_id>0
        AND typeof(advert_id)='integer' AND advert_id>=0
        AND (
            (target_kind='price' AND advert_id=0 AND placement=''
                AND parameter_field IN
                    ('original_price_minor','discount_bps','seller_price_minor'))
            OR (target_kind='bid' AND advert_id>0
                AND placement IN ('combined','search','recommendations')
                AND parameter_field='bid_minor')
            OR (target_kind='campaign' AND advert_id>0 AND placement=''
                AND parameter_field IN
                    ('campaign_state','payment_model','payment_unit'))
        )"""


def _field_value_check(prefix: str, *, requested: bool) -> str:
    numeric_kinds = "('integer')" if requested else "('missing','null','integer')"
    text_kinds = "('text')" if requested else "('missing','null','text')"
    return f"""(
            parameter_field IN
                ('original_price_minor','discount_bps','seller_price_minor','bid_minor')
            AND {prefix}_kind IN {numeric_kinds}
            AND ({prefix}_kind<>'integer' OR {prefix}_integer>=0)
            AND (parameter_field<>'discount_bps'
                OR {prefix}_kind<>'integer' OR {prefix}_integer<=10000)
        ) OR (
            parameter_field IN ('campaign_state','payment_model','payment_unit')
            AND {prefix}_kind IN {text_kinds}
            AND ({prefix}_kind<>'text' OR (
                length({prefix}_text) BETWEEN 1 AND 120
                AND trim({prefix}_text)={prefix}_text
                AND lower({prefix}_text)={prefix}_text
                AND {prefix}_text NOT GLOB '*[^a-z0-9_:-]*'
            ))
        )"""


def _identity_text_check(column: str, maximum: int) -> str:
    return f"length(trim({column})) BETWEEN 1 AND {maximum}"


def _digest_check(column: str) -> str:
    return f"""length({column})=71 AND substr({column},1,7)='sha256:'
        AND substr({column},8) NOT GLOB '*[^0-9a-f]*'"""


def _identifier(value: Any, name: str, *, maximum: int = 120) -> str:
    text = str(value or "").strip()
    if not 1 <= len(text) <= maximum or "\x00" in text:
        raise ChangeRegistryError(f"{name} is required and must be <= {maximum} chars")
    return text


def _optional_text(value: Any, name: str, maximum: int) -> str:
    text = str(value or "").strip()
    if len(text) > maximum or "\x00" in text:
        raise ChangeRegistryError(f"{name} must be <= {maximum} chars")
    return text


def _sanitized_text(value: Any, name: str, maximum: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if any(
        marker in text.casefold()
        for marker in (
            "authorization:",
            "bearer ",
            "cookie:",
            "password=",
            "token=",
            "secret=",
        )
    ):
        raise ChangeRegistryError(f"{name} may not contain credential material")
    return _optional_text(text, name, maximum)


def _required_token(value: Any, name: str, allowed: set[str] | frozenset[str]) -> str:
    token = str(value or "").strip().lower()
    if token not in allowed:
        raise ChangeRegistryError(f"unsupported {name}: {token or '<missing>'}")
    return token


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ChangeRegistryError(f"{name} must be a positive integer")
    return value


def _non_negative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChangeRegistryError(f"{name} must be a non-negative integer")
    return value


def _timestamp(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if (
        not text.endswith("Z")
        or "T" not in text
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", text)
        is None
    ):
        raise ChangeRegistryError(f"{name} must be an ISO-8601 UTC timestamp")
    try:
        datetime.fromisoformat(text.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ChangeRegistryError(f"{name} is not a real UTC timestamp") from exc
    return text


def _timestamp_moment(value: str) -> datetime:
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def _digest(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", text) is None:
        raise ChangeRegistryError(f"{name} must be a lowercase sha256 digest")
    return text


def _optional_digest(value: Any, name: str) -> str:
    text = str(value or "").strip()
    return _digest(text, name) if text else ""


def _bounded_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
        raise ChangeRegistryError("limit must be an integer between 1 and 200")
    return value


def _encode_cursor(entity: str, timestamp: str, identity: str) -> str:
    payload = canonical_json(
        {"entity": entity, "id": identity, "timestamp": timestamp, "version": 1}
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, entity: str) -> tuple[str, str]:
    text = str(cursor or "").strip()
    if not text or len(text) > 1024:
        raise ChangeRegistryError("cursor is invalid")
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChangeRegistryError("cursor is invalid") from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("version") != 1
        or payload.get("entity") != entity
    ):
        raise ChangeRegistryError("cursor does not match the requested entity")
    return (
        _timestamp(payload.get("timestamp"), "cursor timestamp"),
        _identifier(payload.get("id"), "cursor id"),
    )


__all__ = [
    "ANNOTATION_REVISIONS_TABLE",
    "ATTEMPT_EVENTS_TABLE",
    "CHECKPOINTS_TABLE",
    "CHECKPOINT_SOURCE_MANIFESTS_TABLE",
    "CanonicalValue",
    "ChangeRegistryConflict",
    "ChangeRegistryError",
    "ChangeRegistryNotFound",
    "ChangeRegistryRepository",
    "FACT_LINKS_TABLE",
    "FACTS_TABLE",
    "IDENTITY_INCIDENTS_TABLE",
    "IMMUTABLE_TABLES",
    "ITEMS_TABLE",
    "MANUAL_PENDING_CURRENT_TABLE",
    "MANUAL_PENDING_EVENTS_TABLE",
    "MAPPING_VERSION",
    "MISSING",
    "OBSERVATION_VALUES_TABLE",
    "OBSERVER_HEALTH_EVENTS_TABLE",
    "OBSERVER_JOB_EVENTS_TABLE",
    "OBSERVER_JOBS_TABLE",
    "OBSERVER_LEASES_TABLE",
    "OPERATIONS_TABLE",
    "TargetIdentity",
    "canonical_digest",
    "canonical_json",
    "canonicalize_value",
    "append_fact_link_in_transaction",
    "ensure_change_registry_schema",
    "find_reconcilable_transition_fact",
    "target_identity",
]
