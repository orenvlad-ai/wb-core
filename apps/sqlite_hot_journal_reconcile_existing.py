#!/usr/bin/env python3
"""Reconcile the one WBC0027 SQLite rollback already owned by SQLite.

This incident-only contour never opens the operational database read-write.  A
dry-run binds the exact implicit rollback and the two audited operational
writers.  The detached one-submit worker may then publish evidence and the
maintenance marker only when every query-only CAS still matches.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any, Final, Mapping, Sequence, TypedDict


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.business_data_maintenance as maintenance  # noqa: E402
import apps.hosted_runtime_deploy_barrier as deploy_barrier  # noqa: E402
import apps.sqlite_hot_journal_recovery as hot  # noqa: E402
from apps.recovery_file_utils import file_sha256  # noqa: E402
from apps.wbc0027_breakglass_last_good import (  # noqa: E402
    _canonicalize_sqlite_scalar,
    _update_digest_with_canonical_json,
)
from packages.application.business_data_write_barrier import (  # noqa: E402
    barrier_status,
)
from packages.application.storage_registry import (  # noqa: E402
    MANIFEST_FILENAME,
    StoreRegistry,
    manifest_payload,
)


CONTRACT_NAME = "wbc0027_s047_reconcile_existing_rollback_v1"
RESULT_CONTRACT_NAME = hot.RESULT_CONTRACT_NAME
MODE = "reconciled_existing"
DEFAULT_BACKUP_ROOT = hot.DEFAULT_BACKUP_ROOT
DEFAULT_RESERVE_BYTES = hot.DEFAULT_RESERVE_BYTES
DEFAULT_EVIDENCE_ENVELOPE_BYTES = hot.DEFAULT_EVIDENCE_ENVELOPE_BYTES
EXPECTED_WINDOW_ID = hot.EXPECTED_WINDOW_ID
EXPECTED_GENERATION_ID = hot.EXPECTED_GENERATION_ID
EXPECTED_DATABASE_DEVICE = 2081
EXPECTED_DATABASE_INODE = 1_835_015
EXPECTED_DATABASE_SIZE = hot.EXPECTED_DATABASE_SIZE
EXPECTED_FIRST_ACTIVATION_SHA = (
    "9d4aa7b5f8a605bed6e3a1dd3287a038eb0256d7"
)
EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE = "2026-09-02T04:04:00Z"
EXPECTED_SEALED_ROLLBACK = {
    "database_sha256": hot.EXPECTED_DATABASE_SHA256,
    "database_size_bytes": hot.EXPECTED_DATABASE_SIZE,
    "journal_sha256": hot.EXPECTED_JOURNAL_SHA256,
    "journal_size_bytes": hot.EXPECTED_JOURNAL_SIZE,
    "magic_hex": hot.EXPECTED_JOURNAL_MAGIC.hex(),
    "record_count": 169,
    "nonce_hex": "5296552f",
    "initial_database_pages": 1_814_968,
    "sector_size": 512,
    "page_size": 4096,
    "record_region_bytes": 694_088,
    "trailing_bytes": 7_255_464,
    "unique_page_count": 169,
    "page_number_min": 1,
    "page_number_max": 1_809_317,
    "page_number_list_sha256": (
        "8ef27ddebf2d2b12dbf0050bd7719d64a18266f38917aa38a4cf170ec7c8a12e"
    ),
    "checksum_mismatch_count": 0,
    "pages_different_from_main": 168,
    "pages_equal_to_main": 1,
    "expected_recovered_database_sha256": hot.EXPECTED_RECOVERED_DATABASE_SHA256,
}
OPERATIONAL_TABLES = (
    "change_registry_observer_jobs",
    "change_registry_observer_job_events",
    "change_registry_checkpoints",
    "change_registry_checkpoint_source_manifests",
    "change_registry_observation_values",
    "change_registry_identity_incidents",
    "change_registry_facts",
    "change_registry_fact_links",
    "change_registry_observer_health_events",
    "change_registry_observer_leases",
    "sheet_vitrina_v1_source_health_status",
)
INCIDENT_FACTS_TABLE = "change_registry_facts"
INCIDENT_FACT_LINKS_TABLE = "change_registry_fact_links"
ACTIVATION_JOB_PREFIX = "crjob_activation_"
ACTIVATION_JOB_PATTERN = re.compile(r"crjob_activation_([0-9a-f]{40})")
SCHEDULED_JOB_PATTERN = re.compile(r"crjob_[0-9a-f]{32}")
EXPECTED_SELLER_ID = "c0ed0bf8-c443-41f2-b9db-1c14f0099815"
EXPECTED_ACCOUNT_SCOPE = "seller-portal-primary"
EXPECTED_SOURCE_SURFACE = "wb_prices_ads_joint"
EXPECTED_MAPPING_VERSION = "wb_change_registry_mapping_v1"
OBSERVER_EVENT_FAILURE_FIELDS = (
    "error_code",
    "error_message",
    "failure_origin",
    "persistence_stage",
    "persistence_table",
    "persistence_operation",
    "fallback_error_code",
    "fallback_error_message",
)
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
OPERATION_PATTERN = re.compile(r"[0-9a-f]{64}")


class IncidentDigestBinding(TypedDict):
    row_count: int
    digest: str


class IncidentExpectedFact(TypedDict):
    target_kind: str
    nm_id: int
    advert_id: int
    placement: str
    parameter_field: str
    before_value_kind: str
    before_value_integer: int | None
    before_value_text: str | None
    after_value_kind: str
    after_value_integer: int | None
    after_value_text: str | None


class IncidentObserverException(TypedDict):
    job_id: str
    scheduled_slot: str
    checkpoint_id: str
    event_states: tuple[str, str, str]
    terminal_fact_count: int
    checkpoint_completeness: str
    source_completeness: Mapping[str, str]
    expected_facts: tuple[IncidentExpectedFact, ...]


class IncidentObserverExceptionManifest(TypedDict):
    contract_name: str
    validator_contract_name: str
    historical_deployed_sha: str
    observer_contract_name: str
    observer_contract_version: int
    digests: Mapping[str, IncidentDigestBinding]
    exceptions: tuple[IncidentObserverException, IncidentObserverException]


INCIDENT_OBSERVER_EXCEPTION_MANIFEST: Final[IncidentObserverExceptionManifest] = {
    "contract_name": "wbc0027_observer_historical_outcome_exceptions/v1",
    "validator_contract_name": CONTRACT_NAME,
    "historical_deployed_sha": "e58d9a6b71bd4940ab43f4d6cd240b5e0043b9a1",
    "observer_contract_name": "wb_change_registry_observer",
    "observer_contract_version": 1,
    "digests": {
        "jobs": {
            "row_count": 2,
            "digest": "sha256:e09acb5967e59daa9c55cd4291e490b2875c6dfac4f667fc825f73dd03bc4f18",
        },
        "events": {
            "row_count": 6,
            "digest": "sha256:c528c100cafaffdd4e7b20bddd14410120a9beeb8e94db98fee6d438dedd2923",
        },
        "checkpoints": {
            "row_count": 2,
            "digest": "sha256:c36ed5050b3db1e25106d82ea23d0f7bf247ee901b73e07b8f49c9cebfa1c707",
        },
        "source_manifests": {
            "row_count": 4,
            "digest": "sha256:d5164078f5616b581fd7401871e841f9fb29f1a1d8ac98c26461dce04ac3ca34",
        },
        "facts": {
            "row_count": 2,
            "digest": "sha256:8bcd709b4051b90f3298c9f6842355924021889392eb3dd9a5e5fa708a829918",
        },
        "fact_links": {
            "row_count": 2,
            "digest": "sha256:714b3814125a312cffbc37f4880db5da260258a1b188dd9b51659ad212b59fe4",
        },
    },
    "exceptions": (
        {
            "job_id": "crjob_2d37204c6d2d1f9aafdac2741db4f4af",
            "scheduled_slot": "2026-09-02T10:00:00Z",
            "checkpoint_id": "crcp_1f97684daf992e9301e9b7be049a1e6b",
            "event_states": ("accepted", "running", "partial"),
            "terminal_fact_count": 0,
            "checkpoint_completeness": "partial",
            "source_completeness": {"ads": "partial", "prices": "complete"},
            "expected_facts": (),
        },
        {
            "job_id": "crjob_8698f4d3246c01376b028d0b12ae3907",
            "scheduled_slot": "2026-09-02T12:00:00Z",
            "checkpoint_id": "crcp_8d8e46543bb97084d2023ad8f1ea109d",
            "event_states": ("accepted", "running", "complete"),
            "terminal_fact_count": 2,
            "checkpoint_completeness": "complete",
            "source_completeness": {"ads": "complete", "prices": "complete"},
            "expected_facts": (
                {
                    "target_kind": "bid",
                    "nm_id": 259460529,
                    "advert_id": 39293049,
                    "placement": "recommendations",
                    "parameter_field": "bid_minor",
                    "before_value_kind": "integer",
                    "before_value_integer": 500,
                    "before_value_text": None,
                    "after_value_kind": "integer",
                    "after_value_integer": 400,
                    "after_value_text": None,
                },
                {
                    "target_kind": "campaign",
                    "nm_id": 259460529,
                    "advert_id": 39293049,
                    "placement": "",
                    "parameter_field": "campaign_state",
                    "before_value_kind": "text",
                    "before_value_integer": None,
                    "before_value_text": "active",
                    "after_value_kind": "text",
                    "after_value_integer": None,
                    "after_value_text": "paused",
                },
            ),
        },
    ),
}


class ReconcileExistingError(RuntimeError):
    """The exact implicit-rollback reconciliation is not admissible."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    digest = hashlib.sha256()
    _update_digest_with_canonical_json(digest, value)
    return "sha256:" + digest.hexdigest()


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    material = json.loads(_canonical_json(plan))
    material.pop("fingerprint", None)
    material.pop("created_at", None)
    return _fingerprint(material)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    return hot._read_json(path, label=label)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    hot._atomic_json(path, payload)


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({json.dumps(table)})"
        )
    ]


def _stream_tables(
    connection: sqlite3.Connection,
    tables: Sequence[str],
) -> dict[str, Any]:
    """Stream the canonical {table:[rows]} byte sequence into SHA-256."""

    digest = hashlib.sha256()
    digest.update(b"{")
    table_counts: dict[str, int] = {}
    total_rows = 0
    try:
        for table_index, table in enumerate(sorted(tables)):
            if table_index:
                digest.update(b",")
            _update_digest_with_canonical_json(digest, table)
            digest.update(b":[")
            columns = _table_columns(connection, table)
            if not columns:
                raise ReconcileExistingError(f"table has no columns: {table}")
            select = ",".join(f'"{item}"' for item in columns)
            count = 0
            for row in connection.execute(
                f'SELECT {select} FROM "{table}" ORDER BY {select}'
            ):
                if count:
                    digest.update(b",")
                _update_digest_with_canonical_json(
                    digest,
                    [_canonicalize_sqlite_scalar(value) for value in row],
                )
                count += 1
            digest.update(b"]")
            table_counts[table] = count
            total_rows += count
    except Exception as exc:
        if isinstance(exc, ReconcileExistingError):
            raise
        raise ReconcileExistingError("canonical SQLite table streaming failed") from exc
    digest.update(b"}")
    return {
        "contract": "sqlite_canonical_table_stream_v1",
        "digest": "sha256:" + digest.hexdigest(),
        "table_count": len(table_counts),
        "row_count": total_rows,
        "table_row_counts": table_counts,
    }


def _rows(
    connection: sqlite3.Connection,
    table: str,
    *,
    where: str = "",
    params: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    columns = _table_columns(connection, table)
    select = ",".join(f'"{item}"' for item in columns)
    suffix = f" WHERE {where}" if where else ""
    order = ",".join(f'"{item}"' for item in columns)
    return [
        {
            column: _canonicalize_sqlite_scalar(value)
            for column, value in zip(columns, row)
        }
        for row in connection.execute(
            f'SELECT {select} FROM "{table}"{suffix} ORDER BY {order}',
            tuple(params),
        )
    ]


def _validate_job_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    job_id = str(row.get("job_id") or "")
    trigger = str(row.get("trigger_kind") or "")
    requested_by = str(row.get("requested_by") or "")
    slot = str(row.get("scheduled_slot") or "")
    if (
        row.get("seller_id") != EXPECTED_SELLER_ID
        or row.get("account_scope") != EXPECTED_ACCOUNT_SCOPE
    ):
        raise ReconcileExistingError("post-recovery observer job source drifted")
    deployed_sha = ""
    if trigger == "activation":
        match = ACTIVATION_JOB_PATTERN.fullmatch(job_id)
        if match is None or requested_by != "trusted-release-runner" or slot:
            raise ReconcileExistingError("post-recovery activation job identity drifted")
        deployed_sha = match.group(1)
    elif trigger == "scheduled":
        if (
            SCHEDULED_JOB_PATTERN.fullmatch(job_id) is None
            or requested_by != "systemd"
            or not slot.endswith("Z")
        ):
            raise ReconcileExistingError("post-recovery scheduled job identity drifted")
        try:
            moment = datetime.fromisoformat(slot[:-1] + "+00:00")
        except ValueError as exc:
            raise ReconcileExistingError(
                "post-recovery scheduled slot is invalid"
            ) from exc
        canonical_slot = moment.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        if (
            slot != canonical_slot
            or moment.minute
            or moment.second
            or moment.microsecond
            or moment.hour % 2
        ):
            raise ReconcileExistingError("post-recovery scheduled slot is invalid")
        identity_basis = {
            "seller_id": row["seller_id"],
            "account_scope": row["account_scope"],
            "trigger_kind": trigger,
            "scheduled_slot": slot,
            "requested_by": requested_by,
            "requested_at": slot,
        }
        expected_job_id = "crjob_" + hashlib.sha256(
            _canonical_json(identity_basis).encode("utf-8")
        ).hexdigest()[:32]
        if job_id != expected_job_id:
            raise ReconcileExistingError("post-recovery scheduled job identity drifted")
    else:
        raise ReconcileExistingError("post-recovery observer job type is not allowed")
    request_basis = {
        "seller_id": row["seller_id"],
        "account_scope": row["account_scope"],
        "trigger_kind": trigger,
        "scheduled_slot": slot,
        "requested_by": requested_by,
        "client_job_id": job_id,
        "deployed_sha": deployed_sha,
    }
    if row.get("request_digest") != _fingerprint(request_basis):
        raise ReconcileExistingError("post-recovery observer job digest drifted")
    return trigger, deployed_sha


def _incident_exception_bindings(
    connection: sqlite3.Connection,
    *,
    jobs: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    checkpoints: Sequence[Mapping[str, Any]],
    manifests: Sequence[Mapping[str, Any]],
    activation_shas: set[str],
) -> tuple[
    dict[str, IncidentObserverException],
    dict[str, IncidentObserverException],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    manifest = INCIDENT_OBSERVER_EXCEPTION_MANIFEST
    if set(manifest) != {
        "contract_name", "validator_contract_name", "historical_deployed_sha",
        "observer_contract_name", "observer_contract_version", "digests",
        "exceptions",
    } or any(
        (
            manifest.get("contract_name")
            != "wbc0027_observer_historical_outcome_exceptions/v1",
            manifest.get("validator_contract_name") != CONTRACT_NAME,
            manifest.get("observer_contract_name") != "wb_change_registry_observer",
            manifest.get("observer_contract_version") != 1,
            re.fullmatch(
                r"[0-9a-f]{40}", str(manifest.get("historical_deployed_sha") or "")
            ) is None,
        )
    ):
        raise ReconcileExistingError("incident observer exception manifest is invalid")
    raw_exceptions = tuple(manifest.get("exceptions") or ())
    if len(raw_exceptions) != 2:
        raise ReconcileExistingError("incident observer exception count is not exact")
    required_exception_keys = {
        "job_id", "scheduled_slot", "checkpoint_id", "event_states",
        "terminal_fact_count", "checkpoint_completeness",
        "source_completeness", "expected_facts",
    }
    by_job: dict[str, IncidentObserverException] = {}
    by_checkpoint: dict[str, IncidentObserverException] = {}
    for raw in raw_exceptions:
        if set(raw) != required_exception_keys:
            raise ReconcileExistingError("incident observer exception shape is invalid")
        item = raw
        job_id = str(item.get("job_id") or "")
        checkpoint_id = str(item.get("checkpoint_id") or "")
        if (
            SCHEDULED_JOB_PATTERN.fullmatch(job_id) is None
            or not checkpoint_id
            or job_id in by_job
            or checkpoint_id in by_checkpoint
            or tuple(item.get("event_states") or ())
            not in {
                ("accepted", "running", "partial"),
                ("accepted", "running", "complete"),
            }
            or set(item.get("source_completeness") or {}) != {"ads", "prices"}
        ):
            raise ReconcileExistingError("incident observer exception identity is invalid")
        by_job[job_id] = item
        by_checkpoint[checkpoint_id] = item
    if set(by_job) != {
        "crjob_2d37204c6d2d1f9aafdac2741db4f4af",
        "crjob_8698f4d3246c01376b028d0b12ae3907",
    }:
        raise ReconcileExistingError("incident observer exception identities are not exact")
    present_exception_jobs = {
        str(row.get("job_id") or "") for row in jobs
    } & set(by_job)
    if not present_exception_jobs:
        return {}, {}, [], []
    if present_exception_jobs != set(by_job):
        raise ReconcileExistingError("incident observer exception job set is incomplete")
    if str(manifest["historical_deployed_sha"]) not in activation_shas:
        raise ReconcileExistingError("incident observer deployed binding is absent")

    target_jobs = [row for row in jobs if str(row.get("job_id") or "") in by_job]
    target_events = [
        row for row in events if str(row.get("job_id") or "") in by_job
    ]
    target_checkpoints = [
        row for row in checkpoints
        if str(row.get("checkpoint_id") or "") in by_checkpoint
    ]
    target_manifests = [
        row for row in manifests
        if str(row.get("checkpoint_id") or "") in by_checkpoint
    ]
    checkpoint_ids = tuple(sorted(by_checkpoint))
    placeholders = ",".join("?" for _ in checkpoint_ids)
    target_links = _rows(
        connection,
        INCIDENT_FACT_LINKS_TABLE,
        where=f"checkpoint_id IN ({placeholders})",
        params=checkpoint_ids,
    )
    fact_ids = tuple(sorted(str(row.get("fact_id") or "") for row in target_links))
    target_facts = (
        _rows(
            connection,
            INCIDENT_FACTS_TABLE,
            where=f"fact_id IN ({','.join('?' for _ in fact_ids)})",
            params=fact_ids,
        )
        if fact_ids else []
    )
    row_sets: Mapping[str, Sequence[Mapping[str, Any]]] = {
        "jobs": target_jobs,
        "events": target_events,
        "checkpoints": target_checkpoints,
        "source_manifests": target_manifests,
        "facts": target_facts,
        "fact_links": target_links,
    }
    digest_bindings = manifest.get("digests") or {}
    if set(digest_bindings) != set(row_sets):
        raise ReconcileExistingError("incident observer digest binding set is invalid")
    for name, rows in row_sets.items():
        binding = digest_bindings[name]
        if (
            set(binding) != {"row_count", "digest"}
            or int(binding.get("row_count") or -1) != len(rows)
            or DIGEST_PATTERN.fullmatch(str(binding.get("digest") or "")) is None
            or binding.get("digest") != _fingerprint(rows)
        ):
            raise ReconcileExistingError(
                f"incident observer {name.replace('_', ' ')} digest drifted"
            )

    expected_fact_fields = tuple(IncidentExpectedFact.__required_keys__)
    expected_fact_rows = tuple(
        sorted(
            (
                {field: row.get(field) for field in expected_fact_fields}
                for row in target_facts
            ),
            key=lambda row: _canonical_json(row),
        )
    )
    manifest_fact_rows = tuple(
        sorted(
            (
                dict(row)
                for item in raw_exceptions
                for row in tuple(item.get("expected_facts") or ())
            ),
            key=lambda row: _canonical_json(row),
        )
    )
    if expected_fact_rows != manifest_fact_rows:
        raise ReconcileExistingError("incident observer fact semantics drifted")
    fact_checkpoint_ids = {
        str(item["checkpoint_id"])
        for item in raw_exceptions
        if int(item["terminal_fact_count"]) > 0
    }
    if len(fact_checkpoint_ids) != 1 or any(
        row.get("link_kind") != "checkpoint"
        or str(row.get("checkpoint_id") or "")
        not in fact_checkpoint_ids
        for row in target_links
    ):
        raise ReconcileExistingError("incident observer fact link semantics drifted")
    return by_job, by_checkpoint, target_facts, target_links


def _validate_operational_rows(
    connection: sqlite3.Connection,
    *,
    deployed_sha: str,
) -> dict[str, Any]:
    jobs = _rows(
        connection,
        "change_registry_observer_jobs",
        where="requested_at>=?",
        params=(EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,),
    )
    job_ids = {str(row.get("job_id") or "") for row in jobs}
    if not jobs or len(job_ids) != len(jobs):
        raise ReconcileExistingError("post-recovery observer job identity is ambiguous")
    activation_shas: set[str] = set()
    scheduled_job_ids: set[str] = set()
    for row in jobs:
        trigger, job_deployed_sha = _validate_job_identity(row)
        if trigger == "activation":
            activation_shas.add(job_deployed_sha)
        else:
            scheduled_job_ids.add(str(row["job_id"]))
    if not {EXPECTED_FIRST_ACTIVATION_SHA, deployed_sha} <= activation_shas:
        raise ReconcileExistingError("observed deploy activation metadata is incomplete")

    events_since_recovery = _rows(
        connection,
        "change_registry_observer_job_events",
        where="occurred_at>=?",
        params=(EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,),
    )
    events = events_since_recovery
    if {str(row.get("job_id") or "") for row in events} != job_ids:
        raise ReconcileExistingError("post-recovery observer event set is not exact")
    checkpoints: set[str] = set()
    events_by_job: dict[str, list[dict[str, Any]]] = {}
    for job_id in sorted(job_ids):
        own = sorted(
            (row for row in events if row.get("job_id") == job_id),
            key=lambda row: int(row.get("sequence_no") or 0),
        )
        if [int(row.get("sequence_no") or 0) for row in own] != [1, 2, 3]:
            raise ReconcileExistingError("observer event sequence is not exact")
        events_by_job[job_id] = own
        checkpoint_id = str(own[-1].get("checkpoint_id") or "")
        if not checkpoint_id:
            raise ReconcileExistingError("observer checkpoint is absent")
        checkpoints.add(checkpoint_id)
    if len(checkpoints) != len(job_ids):
        raise ReconcileExistingError("observer checkpoint identity is ambiguous")

    checkpoint_rows = _rows(
        connection,
        "change_registry_checkpoints",
        where=f"checkpoint_id IN ({','.join('?' for _ in checkpoints)})",
        params=tuple(sorted(checkpoints)),
    )
    if {str(row.get("checkpoint_id") or "") for row in checkpoint_rows} != checkpoints:
        raise ReconcileExistingError("observer checkpoint rows are incomplete")
    checkpoints_since_recovery = _rows(
        connection,
        "change_registry_checkpoints",
        where="completed_at>=?",
        params=(EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,),
    )
    if checkpoints_since_recovery != checkpoint_rows:
        raise ReconcileExistingError("post-recovery checkpoint set is not exact")
    manifests = _rows(
        connection,
        "change_registry_checkpoint_source_manifests",
        where=f"checkpoint_id IN ({','.join('?' for _ in checkpoints)})",
        params=tuple(sorted(checkpoints)),
    )
    if len(manifests) != 2 * len(checkpoints):
        raise ReconcileExistingError("observer source manifest count is not exact")
    manifests_since_recovery = _rows(
        connection,
        "change_registry_checkpoint_source_manifests",
        where="created_at>=?",
        params=(EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,),
    )
    if manifests_since_recovery != manifests:
        raise ReconcileExistingError("post-recovery source manifest set is not exact")

    exception_by_job, exception_by_checkpoint, target_facts, target_links = (
        _incident_exception_bindings(
            connection,
            jobs=jobs,
            events=events,
            checkpoints=checkpoint_rows,
            manifests=manifests,
            activation_shas=activation_shas,
        )
    )
    observer_links = _rows(
        connection,
        INCIDENT_FACT_LINKS_TABLE,
        where=f"checkpoint_id IN ({','.join('?' for _ in checkpoints)})",
        params=tuple(sorted(checkpoints)),
    )
    observer_fact_ids = tuple(
        sorted(str(row.get("fact_id") or "") for row in observer_links)
    )
    observer_facts = (
        _rows(
            connection,
            INCIDENT_FACTS_TABLE,
            where=f"fact_id IN ({','.join('?' for _ in observer_fact_ids)})",
            params=observer_fact_ids,
        )
        if observer_fact_ids else []
    )
    if observer_links != target_links or observer_facts != target_facts:
        raise ReconcileExistingError("generic observer job produced business facts")
    for job_id, own in events_by_job.items():
        exception = exception_by_job.get(job_id)
        expected_states = (
            list(exception["event_states"])
            if exception is not None
            else ["accepted", "running", "complete"]
        )
        if [row.get("state") for row in own] != expected_states:
            raise ReconcileExistingError("observer event states are not exact")
        expected_fact_counts = [
            0,
            0,
            int(exception["terminal_fact_count"]) if exception is not None else 0,
        ]
        if [int(row.get("fact_count") or 0) for row in own] != expected_fact_counts:
            raise ReconcileExistingError("observer job produced business facts")
        expected_source_statuses = [
            "not_observed",
            "not_observed",
            expected_states[-1],
        ]
        if "source_status" in own[0] and [
            str(row.get("source_status") or "") for row in own
        ] != expected_source_statuses:
            raise ReconcileExistingError("observer event source semantics drifted")
        if any(
            str(row.get(field) or "")
            for row in own
            for field in OBSERVER_EVENT_FAILURE_FIELDS
        ):
            raise ReconcileExistingError("observer event failure metadata is present")
        if any(row.get("checkpoint_id") not in (None, "") for row in own[:2]):
            raise ReconcileExistingError("observer event checkpoint lifecycle drifted")
        checkpoint_id = str(own[-1].get("checkpoint_id") or "")
        if exception is not None and checkpoint_id != exception["checkpoint_id"]:
            raise ReconcileExistingError("incident observer checkpoint identity drifted")

    for row in checkpoint_rows:
        checkpoint_id = str(row.get("checkpoint_id") or "")
        exception = exception_by_checkpoint.get(checkpoint_id)
        expected_status = (
            exception["checkpoint_completeness"]
            if exception is not None else "complete"
        )
        counts_are_valid = (
            int(row.get("observed_target_count") or 0)
            < int(row.get("expected_target_count") or 0)
            if expected_status == "partial"
            else int(row.get("expected_target_count") or 0)
            == int(row.get("observed_target_count") or -1)
        )
        if (
            row.get("seller_id") != EXPECTED_SELLER_ID
            or row.get("account_scope") != EXPECTED_ACCOUNT_SCOPE
            or row.get("source_surface") != EXPECTED_SOURCE_SURFACE
            or row.get("scan_kind") != "observer"
            or row.get("completeness_status") != expected_status
            or not counts_are_valid
            or row.get("mapping_version") != EXPECTED_MAPPING_VERSION
        ):
            raise ReconcileExistingError("observer checkpoint source semantics drifted")
    for checkpoint_id in checkpoints:
        sources = {
            str(row.get("source_name") or "")
            for row in manifests
            if row.get("checkpoint_id") == checkpoint_id
        }
        if sources != {"prices", "ads"}:
            raise ReconcileExistingError("observer source manifest set drifted")
    for row in manifests:
        exception = exception_by_checkpoint.get(str(row.get("checkpoint_id") or ""))
        expected_source_status = (
            str(exception["source_completeness"].get(row.get("source_name")) or "")
            if exception is not None else "complete"
        )
        if row.get("completeness_status") != expected_source_status:
            raise ReconcileExistingError("observer source manifest semantics drifted")
        try:
            summary = json.loads(str(row.get("summary_json") or ""))
        except json.JSONDecodeError as exc:
            raise ReconcileExistingError(
                "observer source manifest summary is invalid"
            ) from exc
        persistence = summary.get("persistence") if isinstance(summary, dict) else None
        wb_mutations = (
            summary.get("wb_mutation_calls") if isinstance(summary, dict) else None
        )
        if (
            not isinstance(summary, dict)
            or summary.get("source") != row.get("source_name")
            or not isinstance(persistence, dict)
            or set(persistence) != {
                "checkpoints_written", "facts_written",
                "identity_incidents_written", "registry_rows_written",
            }
            or any(int(value) != 0 for value in persistence.values())
            or not isinstance(wb_mutations, dict)
            or set(wb_mutations) != {"patch", "post"}
            or any(int(value) != 0 for value in wb_mutations.values())
        ):
            raise ReconcileExistingError("observer source manifest semantics drifted")

    source_health = _rows(
        connection,
        "sheet_vitrina_v1_source_health_status",
        where="checked_at>=?",
        params=(EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,),
    )
    if len(source_health) != 1 or source_health[0].get("source_key") != "seller_portal_auth":
        raise ReconcileExistingError("post-recovery source-health writer set drifted")
    try:
        payload = json.loads(str(source_health[0].get("payload_json") or ""))
    except json.JSONDecodeError as exc:
        raise ReconcileExistingError("seller source-health payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ReconcileExistingError("seller source-health payload is not an object")

    return {
        "not_before": EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,
        "activation_deployed_shas": sorted(activation_shas),
        "scheduled_observer_job_ids": sorted(scheduled_job_ids),
        "activation_jobs": jobs,
        "activation_job_events": events,
        "activation_checkpoints": checkpoint_rows,
        "activation_source_manifests": manifests,
        "incident_exception_job_ids": sorted(exception_by_job),
        "incident_exception_contract": {
            key: INCIDENT_OBSERVER_EXCEPTION_MANIFEST[key]
            for key in (
                "contract_name", "validator_contract_name",
                "historical_deployed_sha", "observer_contract_name",
                "observer_contract_version", "digests",
            )
        },
        "incident_exception_facts": target_facts,
        "incident_exception_fact_links": target_links,
        "source_health_status": source_health,
        "code_paths": {
            "activation": (
                "apps/change_registry_observer.py:run -> "
                "packages.application.change_registry_observer.ChangeRegistryObserver.run"
            ),
            "source_health": (
                "RegistryUploadHttpApplication.handle_seller_portal_session_check_request "
                "-> RegistryUploadDbBackedRuntime.save_source_health_status"
            ),
        },
    }


def _observer_cutoff_tail_cas(
    connection: sqlite3.Connection,
    *,
    operational_rows: Mapping[str, Any],
    requested_cutoff: Mapping[str, Any] | None,
) -> dict[str, Any]:
    jobs = list(operational_rows.get("activation_jobs") or [])
    if not jobs:
        raise ReconcileExistingError("observer cutoff job set is empty")
    jobs_by_key = {
        (str(row.get("requested_at") or ""), str(row.get("job_id") or "")): row
        for row in jobs
    }
    if len(jobs_by_key) != len(jobs):
        raise ReconcileExistingError("observer cutoff job identity is ambiguous")
    if requested_cutoff is None:
        cutoff_key = max(jobs_by_key)
    else:
        if set(requested_cutoff) != {
            "requested_at", "job_id", "terminal_occurred_at",
        }:
            raise ReconcileExistingError("reviewed observer cutoff shape drifted")
        cutoff_key = (
            str(requested_cutoff.get("requested_at") or ""),
            str(requested_cutoff.get("job_id") or ""),
        )
        if cutoff_key not in jobs_by_key:
            raise ReconcileExistingError("reviewed observer cutoff job is absent")
    prefix_jobs = [row for key, row in jobs_by_key.items() if key <= cutoff_key]
    tail_jobs = [row for key, row in jobs_by_key.items() if key > cutoff_key]
    exception_job_ids = set(operational_rows.get("incident_exception_job_ids") or [])
    prefix_job_ids = {str(row["job_id"]) for row in prefix_jobs}
    if exception_job_ids - prefix_job_ids:
        raise ReconcileExistingError("incident observer exception is after cutoff")
    if any(
        row.get("trigger_kind") != "scheduled"
        or str(row.get("job_id") or "") in exception_job_ids
        for row in tail_jobs
    ):
        raise ReconcileExistingError("observer tail is not generic scheduled-only")

    all_events = list(operational_rows.get("activation_job_events") or [])
    cutoff_terminal_events = [
        row for row in all_events
        if str(row.get("job_id") or "") == cutoff_key[1]
        and int(row.get("sequence_no") or 0) == 3
    ]
    if len(cutoff_terminal_events) != 1:
        raise ReconcileExistingError("observer cutoff terminal event is ambiguous")
    cutoff_terminal_at = str(cutoff_terminal_events[0].get("occurred_at") or "")
    if (
        not cutoff_terminal_at
        or requested_cutoff is not None
        and requested_cutoff.get("terminal_occurred_at") != cutoff_terminal_at
    ):
        raise ReconcileExistingError("reviewed observer cutoff terminal event drifted")
    prefix_events = [
        row for row in all_events if str(row.get("job_id") or "") in prefix_job_ids
    ]
    prefix_checkpoint_ids = {
        str(row.get("checkpoint_id") or "")
        for row in prefix_events
        if int(row.get("sequence_no") or 0) == 3
    }
    if "" in prefix_checkpoint_ids or len(prefix_checkpoint_ids) != len(prefix_jobs):
        raise ReconcileExistingError("observer cutoff checkpoint set is ambiguous")
    placeholders = ",".join("?" for _ in prefix_checkpoint_ids)
    checkpoint_params = tuple(sorted(prefix_checkpoint_ids))
    prefix_checkpoints = _rows(
        connection,
        "change_registry_checkpoints",
        where=f"checkpoint_id IN ({placeholders})",
        params=checkpoint_params,
    )
    prefix_manifests = _rows(
        connection,
        "change_registry_checkpoint_source_manifests",
        where=f"checkpoint_id IN ({placeholders})",
        params=checkpoint_params,
    )
    prefix_observations = _rows(
        connection,
        "change_registry_observation_values",
        where=f"checkpoint_id IN ({placeholders})",
        params=checkpoint_params,
    )
    prefix_fact_links = _rows(
        connection,
        INCIDENT_FACT_LINKS_TABLE,
        where=f"checkpoint_id IN ({placeholders})",
        params=checkpoint_params,
    )
    prefix_fact_ids = tuple(
        sorted(str(row.get("fact_id") or "") for row in prefix_fact_links)
    )
    prefix_facts = (
        _rows(
            connection,
            INCIDENT_FACTS_TABLE,
            where=f"fact_id IN ({','.join('?' for _ in prefix_fact_ids)})",
            params=prefix_fact_ids,
        )
        if prefix_fact_ids else []
    )
    prefix_health = _rows(
        connection,
        "change_registry_observer_health_events",
        where=f"job_id IN ({','.join('?' for _ in prefix_job_ids)})",
        params=tuple(sorted(prefix_job_ids)),
    )
    prefix_identity_incidents = _rows(
        connection,
        "change_registry_identity_incidents",
        where="observed_at>=? AND observed_at<=?",
        params=(EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE, cutoff_terminal_at),
    )
    prefix_rows = {
        "jobs": sorted(prefix_jobs, key=_canonical_json),
        "events": sorted(prefix_events, key=_canonical_json),
        "checkpoints": prefix_checkpoints,
        "source_manifests": prefix_manifests,
        "observation_values": prefix_observations,
        "identity_incidents": prefix_identity_incidents,
        "facts": prefix_facts,
        "fact_links": prefix_fact_links,
        "health_events": prefix_health,
    }
    return {
        "contract_name": "wbc0027_observer_cutoff_tail_cas/v1",
        "cutoff": {
            "requested_at": cutoff_key[0],
            "job_id": cutoff_key[1],
            "terminal_occurred_at": cutoff_terminal_at,
        },
        "cutoff_request_digest": jobs_by_key[cutoff_key].get("request_digest"),
        "prefix_digest": _fingerprint(prefix_rows),
        "prefix_job_count": len(prefix_jobs),
        "prefix_checkpoint_count": len(prefix_checkpoint_ids),
        "prefix_table_row_counts": {
            name: len(rows) for name, rows in prefix_rows.items()
        },
        "tail_policy": "generic_scheduled_complete_fact0_only",
        "tail_job_ids": sorted(str(row["job_id"]) for row in tail_jobs),
        "tail_job_count": len(tail_jobs),
    }


def database_evidence(
    database: Path,
    *,
    deployed_sha: str,
    observer_cutoff: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    with closing(
        sqlite3.connect(
            f"file:{database.resolve().as_posix()}?mode=ro&immutable=1",
            uri=True,
            timeout=120,
        )
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not set(OPERATIONAL_TABLES) <= tables:
            raise ReconcileExistingError("operational reconciliation table set is incomplete")
        operational_rows = _validate_operational_rows(
            connection,
            deployed_sha=deployed_sha,
        )
        observer_cutoff_tail_cas = _observer_cutoff_tail_cas(
            connection,
            operational_rows=operational_rows,
            requested_cutoff=observer_cutoff,
        )
        non_operational = _stream_tables(
            connection,
            sorted(tables - set(OPERATIONAL_TABLES)),
        )
        operational = _stream_tables(connection, OPERATIONAL_TABLES)
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        if integrity != ["ok"] or foreign_keys:
            raise ReconcileExistingError("query-only SQLite integrity readback failed")
        sqlite_readback = {
            "query_only": int(connection.execute("PRAGMA query_only").fetchone()[0]),
            "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
            "integrity_check": "ok",
            "foreign_key_violation_count": 0,
            "schema_version": int(connection.execute("PRAGMA schema_version").fetchone()[0]),
            "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        }
    return {
        "non_operational": non_operational,
        "operational": operational,
        "operational_rows": operational_rows,
        "observer_cutoff_tail_cas": observer_cutoff_tail_cas,
        "sqlite_readback": sqlite_readback,
    }


def _business_writer_timeline() -> dict[str, Any]:
    command = [
        "journalctl",
        "--no-pager",
        "--output=json",
        "--since",
        EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,
    ]
    for unit in maintenance.ALL_BUSINESS_SERVICE_UNITS:
        command.extend(["--unit", unit])
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise ReconcileExistingError("business writer journal timeline is unavailable")
    rows = [line for line in completed.stdout.splitlines() if line.strip()]
    if rows:
        raise ReconcileExistingError("business writer timeline is not empty")
    return {
        "since": EXPECTED_IMPLICIT_RECOVERY_NOT_BEFORE,
        "units": list(maintenance.ALL_BUSINESS_SERVICE_UNITS),
        "event_count": 0,
        "digest": "sha256:" + hashlib.sha256(b"").hexdigest(),
    }


def _incident_control_preflight(
    *,
    runtime_dir: Path,
    backup_root: Path,
    deployed_sha: str,
    deployed_sha_file: Path,
    operation_id: str,
    window_id: str,
    plan_fingerprint: str,
    allow_reconcile_job: bool,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.resolve()
    backup_root = backup_root.resolve()
    if runtime_dir != Path("/opt/wb-core-runtime/state"):
        raise ReconcileExistingError("reconciliation requires canonical runtime dir")
    if backup_root != DEFAULT_BACKUP_ROOT or backup_root.is_symlink():
        raise ReconcileExistingError("reconciliation requires canonical backup mount")
    if OPERATION_PATTERN.fullmatch(operation_id) is None:
        raise ReconcileExistingError("operation id must be exact 64-hex")
    if window_id != EXPECTED_WINDOW_ID or DIGEST_PATTERN.fullmatch(plan_fingerprint) is None:
        raise ReconcileExistingError("exact barrier identity is invalid")
    current_sha = hot._deployed_sha(deployed_sha_file, deployed_sha)
    barrier = barrier_status(runtime_dir)
    if (
        barrier.get("active") is not True
        or barrier.get("phase") != "acquiring"
        or barrier.get("hold_confirmed") is not False
        or barrier.get("window_id") != window_id
        or barrier.get("plan_fingerprint") != plan_fingerprint
    ):
        raise ReconcileExistingError("exact acquiring barrier drifted")
    state = _read_json(runtime_dir / maintenance.STATE_FILENAME, label="maintenance state")
    epoch = dict(state.get("prepared_abort_partial_restore_recovery_epoch") or {})
    if (
        state.get("phase") != "abort_quiescing"
        or state.get("exact_prior_state_restored") is True
        or epoch.get("schema_version") != hot.EXPECTED_PARTIAL_EPOCH_SCHEMA
        or int(epoch.get("epoch") or 0) != 2
        or epoch.get("deployed_sha") != hot.EXPECTED_SOURCE_EPOCH_SHA
        or epoch.get("window_id") != window_id
        or epoch.get("plan_fingerprint") != plan_fingerprint
        or epoch.get("barrier_state_fingerprint") != barrier.get("state_fingerprint")
        or sorted(epoch.get("timer_units_to_disable") or [])
        != sorted(hot.RECOVERY_PAUSE_OWNED_TIMERS)
        or sorted(epoch.get("disabled_timer_units") or [])
        != sorted(hot.RECOVERY_PAUSE_OWNED_TIMERS)
        or str(epoch.get("pending_disable_unit") or "")
    ):
        raise ReconcileExistingError("partial abort recovery epoch drifted")
    try:
        preserved = deploy_barrier._preserved_units(runtime_dir)
    except Exception as exc:
        raise ReconcileExistingError("pause-owned deploy barrier validation failed") from exc
    if preserved != set(hot.RECOVERY_PAUSE_OWNED_TIMERS):
        raise ReconcileExistingError("pause-owned unit preservation set drifted")
    systemd = maintenance.SystemdClient()
    timer_states = {
        unit: systemd.unit_state(unit) for unit in maintenance.ALL_BUSINESS_TIMER_UNITS
    }
    service_states = {
        unit: systemd.unit_state(unit) for unit in maintenance.ALL_BUSINESS_SERVICE_UNITS
    }
    if any(hot._timer_pair(value) != ("disabled", "inactive") for value in timer_states.values()):
        raise ReconcileExistingError("a business timer is not paused")
    if any(
        str(value.get("is_active") or "") not in maintenance.QUIESCENT_SERVICE_STATES
        or int((value.get("properties") or {}).get("MainPID") or 0) != 0
        for value in service_states.values()
    ):
        raise ReconcileExistingError("a business writer service is not terminal")
    processes = maintenance._writer_processes()
    if processes:
        raise ReconcileExistingError("a business writer process is present")
    jobs = hot._systemd_jobs()
    allowed_marker = f"wb-core-storage-recovery-sanitation@{operation_id}.service"
    unexpected_jobs = [
        row for row in jobs
        if not (allow_reconcile_job and allowed_marker in row)
    ]
    if unexpected_jobs:
        raise ReconcileExistingError("a systemd job is active")
    locks = maintenance._lock_summary(runtime_dir)
    if any(
        bool(value.get("held"))
        for key, value in locks.items()
        if key != "seller_portal"
    ) or bool((locks.get("seller_portal") or {}).get("busy")):
        raise ReconcileExistingError("a business writer lock is held")
    counters = maintenance._prepared_abort_breakglass_counters(runtime_dir)
    if any(int(value) != 0 for value in counters.values()):
        raise ReconcileExistingError("WBC0027 business operation counters are nonzero")
    return {
        "deployed_sha": current_sha,
        "source_epoch_deployed_sha": str(epoch["deployed_sha"]),
        "barrier": {
            key: barrier.get(key)
            for key in (
                "active", "phase", "hold_confirmed", "window_id",
                "plan_fingerprint", "state_fingerprint",
            )
        },
        "maintenance": {
            "phase": state.get("phase"),
            "partial_epoch_fingerprint": _fingerprint(epoch),
            "timer_states": timer_states,
            "service_states": service_states,
            "writer_processes": processes,
            "writer_locks": locks,
            "business_operation_counters": counters,
        },
        "systemd_jobs": jobs,
        "business_writer_timeline": _business_writer_timeline(),
    }


def _preflight(
    *,
    runtime_dir: Path,
    backup_root: Path,
    deployed_sha: str,
    deployed_sha_file: Path,
    operation_id: str,
    window_id: str,
    plan_fingerprint: str,
    reserve_bytes: int,
    evidence_envelope_bytes: int,
    allow_existing_operation: bool = False,
    allow_reconcile_job: bool = False,
    observer_cutoff: Mapping[str, Any] | None = None,
    stable_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    if reserve_bytes != DEFAULT_RESERVE_BYTES:
        raise ReconcileExistingError("Finance reserve must remain exact")
    if evidence_envelope_bytes != DEFAULT_EVIDENCE_ENVELOPE_BYTES:
        raise ReconcileExistingError("evidence envelope must remain exact")
    control = _incident_control_preflight(
        runtime_dir=runtime_dir,
        backup_root=backup_root,
        deployed_sha=deployed_sha,
        deployed_sha_file=deployed_sha_file,
        operation_id=operation_id,
        window_id=window_id,
        plan_fingerprint=plan_fingerprint,
        allow_reconcile_job=allow_reconcile_job,
    )
    runtime_dir = runtime_dir.resolve()
    registry = StoreRegistry(runtime_dir)
    manifest = registry.load(require_files=True)
    if manifest.state != "cutover" or manifest.canonical_source != "split":
        raise ReconcileExistingError("canonical storage is not exact split cutover")
    database = registry.resolve("operational", manifest=manifest)
    raw = registry.resolve("finance_raw", manifest=manifest)
    expected_database = runtime_dir / "generations" / EXPECTED_GENERATION_ID / "operational.sqlite3"
    if database != expected_database:
        raise ReconcileExistingError("operational generation identity drifted")
    journal = Path(str(database) + "-journal")
    if journal.exists():
        raise ReconcileExistingError("rollback journal reappeared")
    first = hot._file_identity(database)
    if stable_interval_seconds > 0:
        time.sleep(stable_interval_seconds)
    if journal.exists():
        raise ReconcileExistingError("rollback journal reappeared during stable read")
    second = hot._file_identity(database)
    if first != second:
        raise ReconcileExistingError("implicit-recovery database is not physically stable")
    if (
        first["device"] != EXPECTED_DATABASE_DEVICE
        or first["inode"] != EXPECTED_DATABASE_INODE
        or first["size_bytes"] != EXPECTED_DATABASE_SIZE
        or first["sha256"] == hot.EXPECTED_DATABASE_SHA256
        or first["sha256"] == hot.EXPECTED_RECOVERED_DATABASE_SHA256
    ):
        raise ReconcileExistingError("implicit-recovery database identity is outside incident")
    openers = hot._openers({database})
    kernel_locks = hot._kernel_locks({database})
    if kernel_locks:
        raise ReconcileExistingError("operational database has a kernel lock")
    for opener in openers:
        if opener["access"] != "read_only":
            raise ReconcileExistingError("operational database has a writer opener")
        if (
            "apps/registry_upload_http_entrypoint_live.py" not in opener["command"]
            or "wb-core-registry-http.service" not in opener["cgroup"]
        ):
            raise ReconcileExistingError("operational database opener is unknown")
    database_snapshot = database_evidence(
        database,
        deployed_sha=control["deployed_sha"],
        observer_cutoff=observer_cutoff,
    )
    if journal.exists() or hot._file_identity(database) != second:
        raise ReconcileExistingError("database drifted across logical reconciliation")
    manifest_file = hot._file_identity(runtime_dir / MANIFEST_FILENAME)
    raw_file = hot._file_identity(raw)
    if (
        manifest_file["sha256"] != hot.EXPECTED_MANIFEST_FILE_SHA256
        or raw_file["sha256"] != hot.EXPECTED_RAW_SHA256
    ):
        raise ReconcileExistingError("Finance raw/generation manifest identity drifted")
    capacity = hot._filesystem(backup_root)
    if int(capacity["available_bytes"]) < reserve_bytes + evidence_envelope_bytes:
        raise ReconcileExistingError("reconciliation evidence would breach Finance reserve")
    backup_directory = (
        backup_root / "private-evidence" / "production-goals"
        / f"wbc0027-s047-reconcile-existing-{control['deployed_sha'][:8]}"
        / operation_id
    )
    if backup_directory.exists() and not allow_existing_operation:
        raise ReconcileExistingError("reconciliation operation directory already exists")
    return {
        "contract_name": CONTRACT_NAME,
        "mode": MODE,
        "read_only": True,
        "operation_id": operation_id,
        "deployed_sha": control["deployed_sha"],
        "source_epoch_deployed_sha": control["source_epoch_deployed_sha"],
        "runtime_dir": str(runtime_dir),
        "barrier": control["barrier"],
        "maintenance": control["maintenance"],
        "systemd_jobs": control["systemd_jobs"],
        "business_writer_timeline": control["business_writer_timeline"],
        "storage_generation_manifest": manifest_payload(manifest),
        "storage_generation_manifest_file": manifest_file,
        "raw_database": raw_file,
        "database": first,
        "journal": {
            "path": str(journal),
            "absent": True,
            "sealed_before": EXPECTED_SEALED_ROLLBACK,
        },
        "database_reconciliation": database_snapshot,
        "openers": openers,
        "kernel_locks": kernel_locks,
        "backup": {
            "directory": str(backup_directory),
            "reserve_bytes": reserve_bytes,
            "evidence_envelope_bytes": evidence_envelope_bytes,
            "capacity_before": capacity,
            "projected_available_bytes": int(capacity["available_bytes"]) - evidence_envelope_bytes,
            "projected_reserve_headroom_bytes": int(capacity["available_bytes"]) - evidence_envelope_bytes - reserve_bytes,
        },
        "expected_effect": {
            "logical_business_delta": 0,
            "sqlite_write": False,
            "physical_database_change": False,
            "journal_removed_by_this_operation": False,
            "evidence_marker_only": True,
        },
    }


def build_plan(**kwargs: Any) -> dict[str, Any]:
    plan = {**_preflight(**kwargs), "created_at": _now()}
    plan["fingerprint"] = _plan_fingerprint(plan)
    return plan


def _fresh_matches_plan(plan: Mapping[str, Any], fresh: Mapping[str, Any]) -> bool:
    fresh_material = json.loads(_canonical_json(fresh))
    plan_material = json.loads(_canonical_json(plan))
    plan_database = dict(plan_material.get("database") or {})
    fresh_database = dict(fresh_material.get("database") or {})
    if not hot._same_file_identity(
        plan_database,
        fresh_database,
        allow_mtime_change=True,
        allow_content_change=True,
    ):
        return False
    plan_reconciliation = dict(plan_material.get("database_reconciliation") or {})
    fresh_reconciliation = dict(fresh_material.get("database_reconciliation") or {})
    plan_cutoff_cas = dict(plan_reconciliation.get("observer_cutoff_tail_cas") or {})
    fresh_cutoff_cas = dict(fresh_reconciliation.get("observer_cutoff_tail_cas") or {})
    stable_cutoff_keys = {
        "contract_name", "cutoff", "cutoff_request_digest", "prefix_digest",
        "prefix_job_count", "prefix_checkpoint_count", "prefix_table_row_counts",
        "tail_policy",
    }
    if (
        set(plan_cutoff_cas) != stable_cutoff_keys | {"tail_job_ids", "tail_job_count"}
        or set(fresh_cutoff_cas)
        != stable_cutoff_keys | {"tail_job_ids", "tail_job_count"}
        or any(plan_cutoff_cas.get(key) != fresh_cutoff_cas.get(key)
               for key in stable_cutoff_keys)
        or int(plan_cutoff_cas.get("tail_job_count") or 0) != 0
        or list(plan_cutoff_cas.get("tail_job_ids") or [])
        or int(fresh_cutoff_cas.get("tail_job_count") or 0)
        != len(list(fresh_cutoff_cas.get("tail_job_ids") or []))
    ):
        return False
    for database in (plan_database, fresh_database):
        database.pop("mtime_ns", None)
        database.pop("sha256", None)
        database.pop("allocated_bytes", None)
    plan_material["database"] = plan_database
    fresh_material["database"] = fresh_database
    for reconciliation in (plan_reconciliation, fresh_reconciliation):
        reconciliation.pop("operational", None)
        reconciliation.pop("operational_rows", None)
        cutoff_cas = dict(reconciliation.get("observer_cutoff_tail_cas") or {})
        cutoff_cas.pop("tail_job_ids", None)
        cutoff_cas.pop("tail_job_count", None)
        reconciliation["observer_cutoff_tail_cas"] = cutoff_cas
    plan_material["database_reconciliation"] = plan_reconciliation
    fresh_material["database_reconciliation"] = fresh_reconciliation
    for material in (fresh_material, plan_material):
        material.pop("created_at", None)
        material.pop("fingerprint", None)
    return fresh_material == plan_material


def apply_plan(
    *,
    plan_path: Path,
    plan_sha256: str,
    fingerprint: str,
    deployed_sha_file: Path,
) -> dict[str, Any]:
    if DIGEST_PATTERN.fullmatch(plan_sha256) is None or DIGEST_PATTERN.fullmatch(fingerprint) is None:
        raise ReconcileExistingError("reviewed reconciliation identity is invalid")
    plan_path = plan_path.resolve()
    plan = _read_json(plan_path, label="reviewed reconcile-existing plan")
    if "sha256:" + file_sha256(plan_path) != plan_sha256:
        raise ReconcileExistingError("reviewed reconciliation plan bytes drifted")
    if (
        plan.get("contract_name") != CONTRACT_NAME
        or plan.get("mode") != MODE
        or plan.get("read_only") is not True
        or plan.get("fingerprint") != fingerprint
        or _plan_fingerprint(plan) != fingerprint
    ):
        raise ReconcileExistingError("reviewed reconciliation plan identity drifted")
    fresh = build_plan(
        runtime_dir=Path(str(plan["runtime_dir"])),
        backup_root=DEFAULT_BACKUP_ROOT,
        deployed_sha=str(plan["deployed_sha"]),
        deployed_sha_file=deployed_sha_file,
        operation_id=str(plan["operation_id"]),
        window_id=str(plan["barrier"]["window_id"]),
        plan_fingerprint=str(plan["barrier"]["plan_fingerprint"]),
        reserve_bytes=int(plan["backup"]["reserve_bytes"]),
        evidence_envelope_bytes=int(plan["backup"]["evidence_envelope_bytes"]),
        allow_existing_operation=True,
        allow_reconcile_job=True,
        observer_cutoff=dict(
            plan["database_reconciliation"]["observer_cutoff_tail_cas"]["cutoff"]
        ),
    )
    if not _fresh_matches_plan(plan, fresh):
        raise ReconcileExistingError("reviewed reconciliation plan is stale")
    runtime_dir = Path(str(plan["runtime_dir"]))
    backup_dir = Path(str(plan["backup"]["directory"]))
    state_path = backup_dir / "reconciliation-state.json"
    result_path = backup_dir / "result.json"
    if backup_dir.exists():
        state = _read_json(state_path, label="reconciliation operation state")
        if any(
            state.get(key) != value
            for key, value in {
                "contract_name": RESULT_CONTRACT_NAME,
                "mode": MODE,
                "operation_id": plan["operation_id"],
                "deployed_sha": plan["deployed_sha"],
                "plan_sha256": plan_sha256,
                "plan_fingerprint": fingerprint,
            }.items()
        ):
            raise ReconcileExistingError("reconciliation operation state drifted")
    else:
        backup_dir.mkdir(parents=True, mode=0o700)
        os.chmod(backup_dir, 0o700)
        hot._fsync_directory(backup_dir.parent)
        state = {
            "contract_name": RESULT_CONTRACT_NAME,
            "mode": MODE,
            "operation_id": plan["operation_id"],
            "deployed_sha": plan["deployed_sha"],
            "plan_sha256": plan_sha256,
            "plan_fingerprint": fingerprint,
            "phase": "evidence_intent",
            "started_at": _now(),
        }
        _atomic_json(state_path, state)
    if result_path.exists():
        result = _read_json(result_path, label="reconciliation result")
        material = dict(result)
        observed = str(material.pop("result_fingerprint", ""))
        if observed != _fingerprint(material):
            raise ReconcileExistingError("reconciliation result drifted")
    else:
        result = {
            "contract_name": RESULT_CONTRACT_NAME,
            "mode": MODE,
            "status": "reconciled_existing",
            "operation_id": plan["operation_id"],
            "deployed_sha": plan["deployed_sha"],
            "source_epoch_deployed_sha": plan["source_epoch_deployed_sha"],
            "plan_sha256": plan_sha256,
            "plan_fingerprint": fingerprint,
            "barrier": plan["barrier"],
            "maintenance_partial_epoch_fingerprint": plan["maintenance"]["partial_epoch_fingerprint"],
            "database_after": plan["database"],
            "journal_absent": True,
            "sqlite_readback": plan["database_reconciliation"]["sqlite_readback"],
            "non_operational_digest": plan["database_reconciliation"]["non_operational"],
            "operational_digest": plan["database_reconciliation"]["operational"],
            "operational_rows": plan["database_reconciliation"]["operational_rows"],
            "observer_cutoff_tail_cas": plan["database_reconciliation"][
                "observer_cutoff_tail_cas"
            ],
            "business_writer_timeline": plan["business_writer_timeline"],
            "business_operation_counters": plan["maintenance"]["business_operation_counters"],
            "logical_business_delta": 0,
            "sqlite_write": False,
            "completed_at": _now(),
        }
        result["result_fingerprint"] = _fingerprint(result)
        _atomic_json(result_path, result)
    marker = {
        "contract_name": RESULT_CONTRACT_NAME,
        "mode": MODE,
        "operation_id": result["operation_id"],
        "source_epoch_deployed_sha": result["source_epoch_deployed_sha"],
        "deployed_sha": result["deployed_sha"],
        "barrier": result["barrier"],
        "maintenance_partial_epoch_fingerprint": result["maintenance_partial_epoch_fingerprint"],
        "database_after": result["database_after"],
        "journal_absent": True,
        "sqlite_readback": result["sqlite_readback"],
        "non_operational_digest": result["non_operational_digest"],
        "operational_digest": result["operational_digest"],
        "observer_cutoff_tail_cas": result["observer_cutoff_tail_cas"],
        "business_operation_counters": result["business_operation_counters"],
        "result_path": str(result_path),
        "result_fingerprint": result["result_fingerprint"],
        "completed_at": result["completed_at"],
    }
    marker["marker_fingerprint"] = _fingerprint(marker)
    marker_path = runtime_dir / hot.MARKER_FILENAME
    if marker_path.exists():
        if _read_json(marker_path, label="reconciliation marker") != marker:
            raise ReconcileExistingError("reconciliation marker drifted")
    else:
        _atomic_json(marker_path, marker)
    state.update(
        {
            "phase": "completed",
            "completed_at": result["completed_at"],
            "result_fingerprint": result["result_fingerprint"],
        }
    )
    _atomic_json(state_path, state)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--deployed-sha-file", type=Path, default=ROOT / ".wb-core-runtime-sha")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--window-id", required=True)
    parser.add_argument("--plan-fingerprint", required=True)
    parser.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
    parser.add_argument("--evidence-envelope-bytes", type=int, default=DEFAULT_EVIDENCE_ENVELOPE_BYTES)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    plan = build_plan(
        runtime_dir=args.runtime_dir,
        backup_root=args.backup_root,
        deployed_sha=args.deployed_sha,
        deployed_sha_file=args.deployed_sha_file,
        operation_id=args.operation_id,
        window_id=args.window_id,
        plan_fingerprint=args.plan_fingerprint,
        reserve_bytes=args.reserve_bytes,
        evidence_envelope_bytes=args.evidence_envelope_bytes,
    )
    if args.output:
        if args.output.resolve().exists():
            raise ReconcileExistingError("dry-run output already exists")
        _atomic_json(args.output.resolve(), plan)
    else:
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
