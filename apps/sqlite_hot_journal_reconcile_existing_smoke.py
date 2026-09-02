#!/usr/bin/env python3
"""Production-shaped smoke for marker-only implicit rollback reconciliation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.business_data_maintenance as maintenance
import apps.sqlite_hot_journal_reconcile_existing as reconcile
import apps.sqlite_hot_journal_recovery as hot
import apps.storage_recovery_sanitation_job as jobs


OLD_SHA = hot.EXPECTED_SOURCE_EPOCH_SHA
INTERMEDIATE_SHA = "a" * 40
NEW_SHA = "b" * 40
RELEASE_SHAS = (
    reconcile.EXPECTED_FIRST_ACTIVATION_SHA,
    INTERMEDIATE_SHA,
    NEW_SHA,
)
WINDOW = hot.EXPECTED_WINDOW_ID
PLAN_FP = "sha256:" + "c" * 64
STATE_FP = "sha256:" + "d" * 64
PARTIAL_JOB_ID = "crjob_2d37204c6d2d1f9aafdac2741db4f4af"
FACT_JOB_ID = "crjob_8698f4d3246c01376b028d0b12ae3907"


def _database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE change_registry_observer_jobs(
            job_id TEXT PRIMARY KEY, seller_id TEXT, account_scope TEXT,
            trigger_kind TEXT, scheduled_slot TEXT, requested_by TEXT,
            requested_at TEXT, request_digest TEXT
        );
        CREATE TABLE change_registry_observer_job_events(
            job_event_id TEXT PRIMARY KEY, job_id TEXT, sequence_no INTEGER,
            state TEXT, occurred_at TEXT, checkpoint_id TEXT, fact_count INTEGER,
            source_status TEXT
        );
        CREATE TABLE change_registry_checkpoints(
            checkpoint_id TEXT PRIMARY KEY, seller_id TEXT, account_scope TEXT,
            source_surface TEXT, scan_kind TEXT, started_at TEXT,
            completed_at TEXT, completeness_status TEXT,
            expected_target_count INTEGER, observed_target_count INTEGER,
            completeness_digest TEXT, evidence_digest TEXT,
            previous_complete_checkpoint_id TEXT, mapping_version TEXT
        );
        CREATE TABLE change_registry_checkpoint_source_manifests(
            source_manifest_id TEXT PRIMARY KEY, checkpoint_id TEXT,
            source_name TEXT, completeness_status TEXT, expected_count INTEGER,
            observed_count INTEGER, summary_json TEXT, evidence_digest TEXT,
            created_at TEXT
        );
        CREATE TABLE change_registry_observation_values(
            observation_value_id TEXT PRIMARY KEY, checkpoint_id TEXT,
            observed_at TEXT
        );
        CREATE TABLE change_registry_identity_incidents(
            incident_id TEXT PRIMARY KEY, seller_id TEXT, account_scope TEXT,
            source_surface TEXT, observed_at TEXT
        );
        CREATE TABLE change_registry_facts(
            fact_id TEXT PRIMARY KEY, seller_id TEXT, account_scope TEXT,
            target_kind TEXT, nm_id INTEGER, advert_id INTEGER,
            placement TEXT, parameter_field TEXT, before_value_kind TEXT,
            before_value_integer INTEGER, before_value_text TEXT,
            after_value_kind TEXT, after_value_integer INTEGER,
            after_value_text TEXT, observed_from TEXT, observed_to TEXT,
            proven_at TEXT, proof_kind TEXT, evidence_digest TEXT,
            mapping_version TEXT
        );
        CREATE TABLE change_registry_fact_links(
            fact_link_id TEXT PRIMARY KEY, fact_id TEXT, link_kind TEXT,
            change_item_id TEXT, checkpoint_id TEXT,
            native_audit_reference TEXT, recommendation_item_id TEXT,
            linked_at TEXT, evidence_digest TEXT
        );
        CREATE TABLE change_registry_observer_health_events(
            health_event_id TEXT PRIMARY KEY, job_id TEXT, checkpoint_id TEXT,
            occurred_at TEXT
        );
        CREATE TABLE change_registry_observer_leases(
            seller_id TEXT, account_scope TEXT, owner_job_id TEXT,
            acquired_at TEXT, expires_at TEXT, revision INTEGER,
            updated_at TEXT,
            PRIMARY KEY(seller_id,account_scope)
        );
        CREATE TABLE sheet_vitrina_v1_source_health_status(
            source_key TEXT PRIMARY KEY, payload_json TEXT, checked_at TEXT
        );
        CREATE TABLE business_projection(
            row_id TEXT PRIMARY KEY, payload BLOB, nullable TEXT
        );
        """
    )
    jobs: list[dict[str, str]] = []
    for index, sha in enumerate((NEW_SHA, RELEASE_SHAS[0], INTERMEDIATE_SHA), 1):
        jobs.append(
            {
                "job_id": reconcile.ACTIVATION_JOB_PREFIX + sha,
                "trigger_kind": "activation",
                "scheduled_slot": "",
                "requested_by": "trusted-release-runner",
                "requested_at": f"2026-09-02T04:0{index + 4}:00Z",
                "deployed_sha": sha,
            }
        )
    scheduled_slot = "2026-09-02T04:00:00Z"
    scheduled_identity = {
        "seller_id": reconcile.EXPECTED_SELLER_ID,
        "account_scope": reconcile.EXPECTED_ACCOUNT_SCOPE,
        "trigger_kind": "scheduled",
        "scheduled_slot": scheduled_slot,
        "requested_by": "systemd",
        "requested_at": scheduled_slot,
    }
    jobs.insert(
        1,
        {
            "job_id": "crjob_" + hashlib.sha256(
                reconcile._canonical_json(scheduled_identity).encode("utf-8")
            ).hexdigest()[:32],
            "trigger_kind": "scheduled",
            "scheduled_slot": scheduled_slot,
            "requested_by": "systemd",
            "requested_at": "2026-09-02T04:06:00Z",
            "deployed_sha": "",
        },
    )
    for index, job in enumerate(jobs, 1):
        job_id = job["job_id"]
        checkpoint_id = f"checkpoint-{index}"
        request_basis = {
            "seller_id": reconcile.EXPECTED_SELLER_ID,
            "account_scope": reconcile.EXPECTED_ACCOUNT_SCOPE,
            "trigger_kind": job["trigger_kind"],
            "scheduled_slot": job["scheduled_slot"],
            "requested_by": job["requested_by"],
            "client_job_id": job_id,
            "deployed_sha": job["deployed_sha"],
        }
        connection.execute(
            "INSERT INTO change_registry_observer_jobs VALUES(?,?,?,?,?,?,?,?)",
            (
                job_id,
                reconcile.EXPECTED_SELLER_ID,
                reconcile.EXPECTED_ACCOUNT_SCOPE,
                job["trigger_kind"],
                job["scheduled_slot"],
                job["requested_by"],
                job["requested_at"],
                reconcile._fingerprint(request_basis),
            ),
        )
        states = {1: "accepted", 2: "running", 3: "complete"}
        event_ids = (
            {1: "z", 2: "a", 3: "m"}
            if index == 1
            else {1: "m", 2: "z", 3: "a"}
        )
        for sequence in (3, 1, 2):
            state = states[sequence]
            connection.execute(
                "INSERT INTO change_registry_observer_job_events VALUES(?,?,?,?,?,?,?,?)",
                (
                    f"event-{index}-{event_ids[sequence]}", job_id, sequence, state,
                    f"2026-09-02T04:0{index + 4}:0{sequence}Z",
                    checkpoint_id if state == "complete" else None,
                    0, "complete" if state == "complete" else "not_observed",
                ),
            )
        connection.execute(
            "INSERT INTO change_registry_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                checkpoint_id,
                reconcile.EXPECTED_SELLER_ID,
                reconcile.EXPECTED_ACCOUNT_SCOPE,
                reconcile.EXPECTED_SOURCE_SURFACE,
                "observer",
                f"2026-09-02T04:0{index + 4}:02Z",
                f"2026-09-02T04:0{index + 4}:03Z", "complete", 818, 818,
                "sha256:" + "b" * 64, "sha256:" + "a" * 64,
                None, reconcile.EXPECTED_MAPPING_VERSION,
            ),
        )
        for source in ("prices", "ads"):
            expected_count = 92 if source == "prices" else 189
            observed_count = 92 if source == "prices" else 179
            summary = {
                "source": source,
                "persistence": {
                    "checkpoints_written": 0,
                    "facts_written": 0,
                    "identity_incidents_written": 0,
                    "registry_rows_written": 0,
                },
                "wb_mutation_calls": {"patch": 0, "post": 0},
            }
            connection.execute(
                "INSERT INTO change_registry_checkpoint_source_manifests "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"manifest-{index}-{source}", checkpoint_id, source,
                    "complete", expected_count, observed_count,
                    reconcile._canonical_json(summary), "sha256:" + "e" * 64,
                    f"2026-09-02T04:0{index + 4}:03Z",
                ),
            )
    connection.execute(
        "INSERT INTO sheet_vitrina_v1_source_health_status VALUES(?,?,?)",
        (
            "seller_portal_auth",
            json.dumps({"session_status": "valid"}, sort_keys=True),
            "2026-09-02T04:06:48Z",
        ),
    )
    connection.execute(
        "INSERT INTO business_projection VALUES(?,?,?)",
        ("stable", sqlite3.Binary(b"\x00\xff"), None),
    )
    connection.commit()
    connection.close()
    return path


def _insert_incident_outcomes(database: Path, *, normalized_through: str = "") -> None:
    """Insert the two immutable production-shaped historical observer outcomes."""

    connection = sqlite3.connect(database)
    rows = (
        (PARTIAL_JOB_ID, "2026-09-02T10:00:00Z", "incident-partial-checkpoint"),
        (FACT_JOB_ID, "2026-09-02T12:00:00Z", "incident-fact-checkpoint"),
    )
    for job_id, slot, checkpoint_id in rows:
        request_basis = {
            "seller_id": reconcile.EXPECTED_SELLER_ID,
            "account_scope": reconcile.EXPECTED_ACCOUNT_SCOPE,
            "trigger_kind": "scheduled",
            "scheduled_slot": slot,
            "requested_by": "systemd",
            "client_job_id": job_id,
            "deployed_sha": "",
        }
        connection.execute(
            "INSERT INTO change_registry_observer_jobs VALUES(?,?,?,?,?,?,?,?)",
            (
                job_id, reconcile.EXPECTED_SELLER_ID,
                reconcile.EXPECTED_ACCOUNT_SCOPE, "scheduled", slot,
                "systemd", slot, reconcile._fingerprint(request_basis),
            ),
        )
        terminal_state = "partial" if job_id == PARTIAL_JOB_ID else "complete"
        if normalized_through in {"states", "facts", "checkpoint"}:
            terminal_state = "complete"
        terminal_fact_count = 2 if job_id == FACT_JOB_ID else 0
        if normalized_through in {"facts", "checkpoint"}:
            terminal_fact_count = 0
        for sequence, state in enumerate(("accepted", "running", terminal_state), 1):
            connection.execute(
                "INSERT INTO change_registry_observer_job_events VALUES(?,?,?,?,?,?,?,?)",
                (
                    f"incident-event-{job_id}-{sequence}", job_id, sequence, state,
                    slot[:-1] + f".{sequence}Z",
                    checkpoint_id if sequence == 3 else None,
                    terminal_fact_count if sequence == 3 else 0,
                    terminal_state if sequence == 3 else "not_observed",
                ),
            )
        completeness = "partial" if job_id == PARTIAL_JOB_ID else "complete"
        observed = 817 if completeness == "partial" else 818
        if normalized_through == "checkpoint":
            completeness = "complete"
            observed = 818
        connection.execute(
            "INSERT INTO change_registry_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                checkpoint_id, reconcile.EXPECTED_SELLER_ID,
                reconcile.EXPECTED_ACCOUNT_SCOPE, reconcile.EXPECTED_SOURCE_SURFACE,
                "observer", slot[:-1] + ".1Z", slot[:-1] + ".3Z",
                completeness, 818, observed, "sha256:" + "7" * 64,
                "sha256:" + "8" * 64, None, reconcile.EXPECTED_MAPPING_VERSION,
            ),
        )
        for source in ("prices", "ads"):
            source_status = (
                "partial"
                if job_id == PARTIAL_JOB_ID and source == "ads"
                else "complete"
            )
            summary = {
                "source": source,
                "persistence": {
                    "checkpoints_written": 0,
                    "facts_written": 0,
                    "identity_incidents_written": 0,
                    "registry_rows_written": 0,
                },
                "wb_mutation_calls": {"patch": 0, "post": 0},
            }
            connection.execute(
                "INSERT INTO change_registry_checkpoint_source_manifests "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"incident-manifest-{job_id}-{source}", checkpoint_id,
                    source, source_status, 92, 92,
                    reconcile._canonical_json(summary), "sha256:" + "9" * 64,
                    slot[:-1] + ".3Z",
                ),
            )
    facts = (
        (
            "incident-fact-bid", "bid", 428855758, 24681012,
            "recommendations", "bid_minor", "integer", 3100, None,
            "integer", 3200, None,
        ),
        (
            "incident-fact-state", "campaign", 428855758, 24681012,
            "", "campaign_state", "text", None, "active",
            "text", None, "paused",
        ),
    )
    for (
        fact_id, target_kind, nm_id, advert_id, placement, parameter_field,
        before_kind, before_integer, before_text, after_kind, after_integer,
        after_text,
    ) in facts:
        connection.execute(
            "INSERT INTO change_registry_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fact_id, reconcile.EXPECTED_SELLER_ID,
                reconcile.EXPECTED_ACCOUNT_SCOPE, target_kind, nm_id, advert_id,
                placement, parameter_field, before_kind, before_integer,
                before_text, after_kind, after_integer, after_text,
                "2026-09-02T10:00:00Z", "2026-09-02T12:00:00Z",
                "2026-09-02T12:00:00Z", "checkpoint_diff",
                "sha256:" + "a" * 64, reconcile.EXPECTED_MAPPING_VERSION,
            ),
        )
        connection.execute(
            "INSERT INTO change_registry_fact_links VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"incident-link-{fact_id}", fact_id, "checkpoint", None,
                "incident-fact-checkpoint", "", "",
                "2026-09-02T12:00:00Z", "sha256:" + "b" * 64,
            ),
        )
    connection.commit()
    connection.close()


def _incident_manifest(database: Path) -> dict[str, object]:
    connection = sqlite3.connect(database)
    jobs = {
        str(row["job_id"]): row
        for row in reconcile._rows(connection, "change_registry_observer_jobs")
        if row["job_id"] in {PARTIAL_JOB_ID, FACT_JOB_ID}
    }
    target_jobs = [jobs[job_id] for job_id in (PARTIAL_JOB_ID, FACT_JOB_ID)]
    target_events: list[dict[str, object]] = []
    target_checkpoints: list[dict[str, object]] = []
    target_manifests: list[dict[str, object]] = []
    target_facts: list[dict[str, object]] = []
    target_links: list[dict[str, object]] = []
    for job_id in (PARTIAL_JOB_ID, FACT_JOB_ID):
        events = sorted(
            reconcile._rows(
                connection, "change_registry_observer_job_events",
                where="job_id=?", params=(job_id,),
            ),
            key=lambda row: int(row["sequence_no"]),
        )
        checkpoint_id = str(events[-1]["checkpoint_id"])
        checkpoints = reconcile._rows(
            connection, "change_registry_checkpoints",
            where="checkpoint_id=?", params=(checkpoint_id,),
        )
        manifests = reconcile._rows(
            connection, "change_registry_checkpoint_source_manifests",
            where="checkpoint_id=?", params=(checkpoint_id,),
        )
        links = reconcile._rows(
            connection, "change_registry_fact_links",
            where="checkpoint_id=?", params=(checkpoint_id,),
        )
        fact_ids = tuple(sorted(str(row["fact_id"]) for row in links))
        facts = (
            reconcile._rows(
                connection, "change_registry_facts",
                where=f"fact_id IN ({','.join('?' for _ in fact_ids)})",
                params=fact_ids,
            )
            if fact_ids else []
        )
        target_events.extend(events)
        target_checkpoints.extend(checkpoints)
        target_manifests.extend(manifests)
        target_facts.extend(facts)
        target_links.extend(links)
    target_jobs = reconcile._rows(
        connection, "change_registry_observer_jobs",
        where="job_id IN (?,?)", params=(PARTIAL_JOB_ID, FACT_JOB_ID),
    )
    target_events = reconcile._rows(
        connection, "change_registry_observer_job_events",
        where="job_id IN (?,?)", params=(PARTIAL_JOB_ID, FACT_JOB_ID),
    )
    target_checkpoints = reconcile._rows(
        connection, "change_registry_checkpoints",
        where="checkpoint_id IN (?,?)",
        params=("incident-partial-checkpoint", "incident-fact-checkpoint"),
    )
    target_manifests = reconcile._rows(
        connection, "change_registry_checkpoint_source_manifests",
        where="checkpoint_id IN (?,?)",
        params=("incident-partial-checkpoint", "incident-fact-checkpoint"),
    )
    target_links = reconcile._rows(
        connection, "change_registry_fact_links",
        where="checkpoint_id IN (?,?)",
        params=("incident-partial-checkpoint", "incident-fact-checkpoint"),
    )
    target_facts = reconcile._rows(
        connection, "change_registry_facts",
        where="fact_id IN (?,?)",
        params=("incident-fact-bid", "incident-fact-state"),
    )
    connection.close()
    row_sets = {
        "jobs": target_jobs,
        "events": target_events,
        "checkpoints": target_checkpoints,
        "source_manifests": target_manifests,
        "facts": target_facts,
        "fact_links": target_links,
    }
    return {
        "contract_name": "wbc0027_observer_historical_outcome_exceptions/v1",
        "validator_contract_name": reconcile.CONTRACT_NAME,
        "historical_deployed_sha": INTERMEDIATE_SHA,
        "observer_contract_name": "wb_change_registry_observer",
        "observer_contract_version": 1,
        "digests": {
            name: {"row_count": len(value), "digest": reconcile._fingerprint(value)}
            for name, value in row_sets.items()
        },
        "exceptions": (
            {
                "job_id": PARTIAL_JOB_ID,
                "scheduled_slot": "2026-09-02T10:00:00Z",
                "checkpoint_id": "incident-partial-checkpoint",
                "event_states": ("accepted", "running", "partial"),
                "terminal_fact_count": 0,
                "checkpoint_completeness": "partial",
                "source_completeness": {"ads": "partial", "prices": "complete"},
                "expected_facts": (),
            },
            {
                "job_id": FACT_JOB_ID,
                "scheduled_slot": "2026-09-02T12:00:00Z",
                "checkpoint_id": "incident-fact-checkpoint",
                "event_states": ("accepted", "running", "complete"),
                "terminal_fact_count": 2,
                "checkpoint_completeness": "complete",
                "source_completeness": {"ads": "complete", "prices": "complete"},
                "expected_facts": (
                    {
                        "target_kind": "bid", "nm_id": 428855758,
                        "advert_id": 24681012, "placement": "recommendations",
                        "parameter_field": "bid_minor",
                        "before_value_kind": "integer", "before_value_integer": 3100,
                        "before_value_text": None, "after_value_kind": "integer",
                        "after_value_integer": 3200, "after_value_text": None,
                    },
                    {
                        "target_kind": "campaign", "nm_id": 428855758,
                        "advert_id": 24681012, "placement": "",
                        "parameter_field": "campaign_state",
                        "before_value_kind": "text", "before_value_integer": None,
                        "before_value_text": "active", "after_value_kind": "text",
                        "after_value_integer": None, "after_value_text": "paused",
                    },
                ),
            },
        ),
    }


def _incident_predicate_order_red_smoke(root: Path) -> None:
    database = _database(root / "incident-exact-accepted.sqlite3")
    _insert_incident_outcomes(database)
    manifest = _incident_manifest(database)
    with mock.patch.object(
        reconcile, "INCIDENT_OBSERVER_EXCEPTION_MANIFEST", manifest,
        create=True,
    ):
        evidence = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    assert evidence["operational_rows"]["incident_exception_job_ids"] == [
        PARTIAL_JOB_ID, FACT_JOB_ID,
    ]


def _incident_exception_rejection_smoke(root: Path) -> None:
    cases = (
        (
            "job-drift",
            "UPDATE change_registry_observer_jobs SET requested_at="
            "'2026-09-02T11:17:24Z' WHERE job_id=?",
            (PARTIAL_JOB_ID,),
            "jobs digest drifted",
        ),
        (
            "missing-event",
            "DELETE FROM change_registry_observer_job_events "
            "WHERE job_id=? AND sequence_no=2",
            (PARTIAL_JOB_ID,),
            "event sequence",
        ),
        (
            "reordered-event",
            "UPDATE change_registry_observer_job_events SET sequence_no=4 "
            "WHERE job_id=? AND sequence_no=2",
            (PARTIAL_JOB_ID,),
            "event sequence",
        ),
        (
            "unknown-state",
            "UPDATE change_registry_observer_job_events SET state='unknown' "
            "WHERE job_id=? AND sequence_no=3",
            (PARTIAL_JOB_ID,),
            "events digest drifted",
        ),
        (
            "unknown-source",
            "UPDATE change_registry_observer_job_events SET source_status='unknown' "
            "WHERE job_id=? AND sequence_no=3",
            (PARTIAL_JOB_ID,),
            "events digest drifted",
        ),
        (
            "checkpoint-drift",
            "UPDATE change_registry_checkpoints SET observed_target_count=816 "
            "WHERE checkpoint_id='incident-partial-checkpoint'",
            (),
            "checkpoints digest drifted",
        ),
        (
            "manifest-drift",
            "UPDATE change_registry_checkpoint_source_manifests "
            "SET completeness_status='complete' "
            "WHERE checkpoint_id='incident-partial-checkpoint' AND source_name='ads'",
            (),
            "source manifests digest drifted",
        ),
        (
            "fact-drift",
            "UPDATE change_registry_facts SET nm_id=1 "
            "WHERE fact_id='incident-fact-bid'",
            (),
            "facts digest drifted",
        ),
        (
            "missing-link",
            "DELETE FROM change_registry_fact_links "
            "WHERE fact_link_id='incident-link-incident-fact-bid'",
            (),
            "facts digest drifted",
        ),
        (
            "unknown-actor",
            "UPDATE change_registry_observer_jobs SET requested_by='operator' "
            "WHERE job_id=?",
            (PARTIAL_JOB_ID,),
            "scheduled job identity",
        ),
    )
    for name, statement, params, expected in cases:
        database = _database(root / f"incident-reject-{name}.sqlite3")
        _insert_incident_outcomes(database)
        manifest = _incident_manifest(database)
        connection = sqlite3.connect(database)
        connection.execute(statement, params)
        connection.commit()
        connection.close()
        with mock.patch.object(
            reconcile, "INCIDENT_OBSERVER_EXCEPTION_MANIFEST", manifest,
        ):
            try:
                reconcile.database_evidence(database, deployed_sha=NEW_SHA)
            except reconcile.ReconcileExistingError as exc:
                assert expected in str(exc), (name, str(exc))
            else:
                raise AssertionError(f"incident drift {name} must fail closed")

    database = _database(root / "incident-reject-extra-event.sqlite3")
    _insert_incident_outcomes(database)
    manifest = _incident_manifest(database)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO change_registry_observer_job_events VALUES(?,?,?,?,?,?,?,?)",
        (
            "incident-event-extra", PARTIAL_JOB_ID, 4, "complete",
            "2026-09-02T10:00:00.4Z", "incident-partial-checkpoint", 0,
            "complete",
        ),
    )
    connection.commit()
    connection.close()
    with mock.patch.object(
        reconcile, "INCIDENT_OBSERVER_EXCEPTION_MANIFEST", manifest,
    ):
        try:
            reconcile.database_evidence(database, deployed_sha=NEW_SHA)
        except reconcile.ReconcileExistingError as exc:
            assert "event sequence" in str(exc)
        else:
            raise AssertionError("extra incident event must fail closed")

    database = _database(root / "incident-reject-third-exception.sqlite3")
    _insert_incident_outcomes(database)
    manifest = _incident_manifest(database)
    manifest["exceptions"] = (*manifest["exceptions"], dict(manifest["exceptions"][0]))
    with mock.patch.object(
        reconcile, "INCIDENT_OBSERVER_EXCEPTION_MANIFEST", manifest,
    ):
        try:
            reconcile.database_evidence(database, deployed_sha=NEW_SHA)
        except reconcile.ReconcileExistingError as exc:
            assert "exception count" in str(exc)
        else:
            raise AssertionError("third incident exception must fail closed")


def _append_generic_observer_outcome(database: Path) -> str:
    slot = "2026-09-02T14:00:00Z"
    identity = {
        "seller_id": reconcile.EXPECTED_SELLER_ID,
        "account_scope": reconcile.EXPECTED_ACCOUNT_SCOPE,
        "trigger_kind": "scheduled",
        "scheduled_slot": slot,
        "requested_by": "systemd",
        "requested_at": slot,
    }
    job_id = "crjob_" + hashlib.sha256(
        reconcile._canonical_json(identity).encode("utf-8")
    ).hexdigest()[:32]
    request = {
        "seller_id": reconcile.EXPECTED_SELLER_ID,
        "account_scope": reconcile.EXPECTED_ACCOUNT_SCOPE,
        "trigger_kind": "scheduled",
        "scheduled_slot": slot,
        "requested_by": "systemd",
        "client_job_id": job_id,
        "deployed_sha": "",
    }
    checkpoint_id = "generic-tail-checkpoint"
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO change_registry_observer_jobs VALUES(?,?,?,?,?,?,?,?)",
        (
            job_id, reconcile.EXPECTED_SELLER_ID,
            reconcile.EXPECTED_ACCOUNT_SCOPE, "scheduled", slot, "systemd",
            "2026-09-02T15:17:00Z", reconcile._fingerprint(request),
        ),
    )
    for sequence, state in enumerate(("accepted", "running", "complete"), 1):
        connection.execute(
            "INSERT INTO change_registry_observer_job_events VALUES(?,?,?,?,?,?,?,?)",
            (
                f"generic-tail-event-{sequence}", job_id, sequence, state,
                slot[:-1] + f".{sequence}Z",
                checkpoint_id if sequence == 3 else None, 0,
                "complete" if sequence == 3 else "not_observed",
            ),
        )
    connection.execute(
        "INSERT INTO change_registry_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            checkpoint_id, reconcile.EXPECTED_SELLER_ID,
            reconcile.EXPECTED_ACCOUNT_SCOPE, reconcile.EXPECTED_SOURCE_SURFACE,
            "observer", slot[:-1] + ".1Z", slot[:-1] + ".3Z", "complete",
            818, 818, "sha256:" + "c" * 64, "sha256:" + "d" * 64,
            None, reconcile.EXPECTED_MAPPING_VERSION,
        ),
    )
    for source in ("prices", "ads"):
        summary = {
            "source": source,
            "persistence": {
                "checkpoints_written": 0, "facts_written": 0,
                "identity_incidents_written": 0, "registry_rows_written": 0,
            },
            "wb_mutation_calls": {"patch": 0, "post": 0},
        }
        connection.execute(
            "INSERT INTO change_registry_checkpoint_source_manifests "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"generic-tail-manifest-{source}", checkpoint_id, source,
                "complete", 92, 92, reconcile._canonical_json(summary),
                "sha256:" + "e" * 64, slot[:-1] + ".3Z",
            ),
        )
    connection.commit()
    connection.close()
    return job_id


def _observer_cutoff_tail_cas_smoke(root: Path) -> None:
    database = _database(root / "observer-tail-cas.sqlite3")
    _insert_incident_outcomes(database)
    manifest = _incident_manifest(database)
    with mock.patch.object(reconcile, "INCIDENT_OBSERVER_EXCEPTION_MANIFEST", manifest):
        reviewed = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
        reviewed_file = hot._file_identity(database)
        cutoff = dict(reviewed["observer_cutoff_tail_cas"]["cutoff"])
        appended_job_id = _append_generic_observer_outcome(database)
        fresh = reconcile.database_evidence(
            database, deployed_sha=NEW_SHA, observer_cutoff=cutoff,
        )
    assert fresh["observer_cutoff_tail_cas"]["tail_job_ids"] == [appended_job_id]
    assert reconcile._fresh_matches_plan(
        {"database": reviewed_file, "database_reconciliation": reviewed},
        {"database": hot._file_identity(database), "database_reconciliation": fresh},
    )

    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO change_registry_fact_links VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "generic-tail-extra-link", "incident-fact-bid", "checkpoint", None,
            "generic-tail-checkpoint", "", "", "2026-09-02T14:00:00Z",
            "sha256:" + "f" * 64,
        ),
    )
    connection.commit()
    connection.close()
    with mock.patch.object(reconcile, "INCIDENT_OBSERVER_EXCEPTION_MANIFEST", manifest):
        try:
            reconcile.database_evidence(
                database, deployed_sha=NEW_SHA, observer_cutoff=cutoff,
            )
        except reconcile.ReconcileExistingError as exc:
            assert "generic observer job produced business facts" in str(exc)
        else:
            raise AssertionError("generic observer tail facts must fail closed")

    connection = sqlite3.connect(database)
    connection.execute(
        "DELETE FROM change_registry_fact_links "
        "WHERE fact_link_id='generic-tail-extra-link'"
    )
    connection.execute(
        "UPDATE change_registry_checkpoints SET evidence_digest=? "
        "WHERE checkpoint_id='checkpoint-1'",
        ("sha256:" + "0" * 64,),
    )
    connection.commit()
    connection.close()
    with mock.patch.object(reconcile, "INCIDENT_OBSERVER_EXCEPTION_MANIFEST", manifest):
        drifted = reconcile.database_evidence(
            database, deployed_sha=NEW_SHA, observer_cutoff=cutoff,
        )
    assert not reconcile._fresh_matches_plan(
        {"database": reviewed_file, "database_reconciliation": reviewed},
        {"database": hot._file_identity(database), "database_reconciliation": drifted},
    )


def _observer_cutoff_terminal_boundary_red_smoke(root: Path) -> None:
    database = _database(root / "observer-terminal-boundary.sqlite3")
    _insert_incident_outcomes(database)
    manifest = _incident_manifest(database)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO change_registry_identity_incidents VALUES(?,?,?,?,?)",
        (
            "same-job-terminal-incident",
            reconcile.EXPECTED_SELLER_ID,
            reconcile.EXPECTED_ACCOUNT_SCOPE,
            reconcile.EXPECTED_SOURCE_SURFACE,
            "2026-09-02T12:00:00.25Z",
        ),
    )
    connection.commit()
    connection.close()
    with mock.patch.object(reconcile, "INCIDENT_OBSERVER_EXCEPTION_MANIFEST", manifest):
        reviewed = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    cutoff = dict(reviewed["observer_cutoff_tail_cas"]["cutoff"])
    assert cutoff["terminal_occurred_at"] == "2026-09-02T12:00:00.3Z"
    assert reviewed["observer_cutoff_tail_cas"]["prefix_table_row_counts"][
        "identity_incidents"
    ] == 1

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE change_registry_identity_incidents SET source_surface=? "
        "WHERE incident_id='same-job-terminal-incident'",
        ("drifted",),
    )
    connection.commit()
    connection.close()
    with mock.patch.object(reconcile, "INCIDENT_OBSERVER_EXCEPTION_MANIFEST", manifest):
        drifted = reconcile.database_evidence(
            database,
            deployed_sha=NEW_SHA,
            observer_cutoff=cutoff,
        )
    assert not reconcile._fresh_matches_plan(
        {
            "database": hot._file_identity(database),
            "database_reconciliation": reviewed,
        },
        {
            "database": hot._file_identity(database),
            "database_reconciliation": drifted,
        },
    )


def _sqlite_qualification_red_smoke(root: Path) -> None:
    qualification_root = root / "qualification-root"
    qualification_root.mkdir()
    database = (root / "qualification.sqlite3").resolve()
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE parent(id INTEGER PRIMARY KEY);
        CREATE TABLE child(
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES parent(id)
        );
        INSERT INTO parent VALUES(1);
        INSERT INTO child VALUES(1,1);
        """
    )
    connection.commit()
    connection.close()
    expected = hot._file_identity(database)
    sqlite_connect = sqlite3.connect
    staged_paths: list[Path] = []

    def portable_connect(database_arg: str, *args: object, **kwargs: object):
        if "/proc/self/fd/" in database_arg or "/dev/fd/" in database_arg:
            raise sqlite3.OperationalError("synthetic Linux fd-path failure")
        if database_arg.startswith("file:") and "mode=ro&immutable=1" in database_arg:
            staged = Path(database_arg.removeprefix("file:").split("?", 1)[0])
            assert staged.exists()
            assert staged.stat().st_mode & 0o777 == 0o600
            staged_paths.append(staged)
        return sqlite_connect(database_arg, *args, **kwargs)

    with mock.patch.object(reconcile.sqlite3, "connect", side_effect=portable_connect):
        qualification = reconcile._qualify_current_sqlite(
            database,
            backup_root=qualification_root,
            expected_source=expected,
            max_bytes=1024 * 1024,
            max_seconds=10.0,
            max_copy_bytes_per_second=1024 * 1024 * 1024,
        )
    reconcile._validate_sqlite_qualification(
        qualification,
        expected_source=expected,
    )
    assert qualification["sqlite"]["integrity_check"] == "ok"
    assert qualification["sqlite"]["foreign_key_violation_count"] == 0
    assert qualification["copy"]["anonymous"] is True
    assert qualification["copy"]["exclusive_create"] is True
    assert qualification["copy"]["staged_mode"] == 0o600
    assert qualification["copy"]["unlinked_before_checks"] is True
    assert qualification["copy"]["zero_leftover"] is True
    assert len(staged_paths) == 1
    assert not staged_paths[0].exists()
    assert list(qualification_root.iterdir()) == []

    with mock.patch.object(
        reconcile.sqlite3,
        "connect",
        side_effect=sqlite3.OperationalError("synthetic open failure"),
    ):
        try:
            reconcile._qualify_current_sqlite(
                database,
                backup_root=qualification_root,
                expected_source=expected,
                max_bytes=1024 * 1024,
                max_seconds=10.0,
                max_copy_bytes_per_second=1024 * 1024 * 1024,
            )
        except reconcile.ReconcileExistingError as exc:
            assert "integrity/FK qualification" in str(exc)
        else:
            raise AssertionError("SQLite open failure must fail closed")
    assert list(qualification_root.iterdir()) == []

    operational_database = _database(
        (root / "qualification-operational.sqlite3").resolve()
    )
    operational_identity = hot._file_identity(operational_database)
    operational_qualification = reconcile._qualify_current_sqlite(
        operational_database,
        backup_root=qualification_root,
        expected_source=operational_identity,
        max_bytes=1024 * 1024,
        max_seconds=10.0,
        max_copy_bytes_per_second=1024 * 1024 * 1024,
    )
    qualified_evidence = reconcile.database_evidence(
        operational_database,
        deployed_sha=NEW_SHA,
        sqlite_qualification=operational_qualification,
        expected_database_identity=operational_identity,
    )
    assert qualified_evidence["sqlite_qualification"] == operational_qualification
    assert qualified_evidence["sqlite_readback"]["integrity_check"] == "ok"

    unknown = json.loads(json.dumps(qualification))
    unknown["contract_name"] = "unknown/v2"
    try:
        reconcile._validate_sqlite_qualification(unknown, expected_source=expected)
    except reconcile.ReconcileExistingError as exc:
        assert "qualification schema" in str(exc)
    else:
        raise AssertionError("unknown qualification schema must fail closed")

    drifted_identity = dict(expected)
    drifted_identity["sha256"] = "0" * 64
    try:
        reconcile._qualify_current_sqlite(
            database,
            backup_root=qualification_root,
            expected_source=drifted_identity,
            max_bytes=1024 * 1024,
            max_seconds=10.0,
            max_copy_bytes_per_second=1024 * 1024 * 1024,
        )
    except reconcile.ReconcileExistingError as exc:
        assert "identity drifted" in str(exc)
    else:
        raise AssertionError("qualification source identity drift must fail closed")

    foreign_key_database = (root / "qualification-fk.sqlite3").resolve()
    connection = sqlite3.connect(foreign_key_database)
    connection.executescript(
        """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE parent(id INTEGER PRIMARY KEY);
        CREATE TABLE child(
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES parent(id)
        );
        INSERT INTO child VALUES(1,999);
        """
    )
    connection.commit()
    connection.close()
    try:
        reconcile._qualify_current_sqlite(
            foreign_key_database,
            backup_root=qualification_root,
            expected_source=hot._file_identity(foreign_key_database),
            max_bytes=1024 * 1024,
            max_seconds=10.0,
            max_copy_bytes_per_second=1024 * 1024 * 1024,
        )
    except reconcile.ReconcileExistingError as exc:
        assert "foreign-key" in str(exc)
    else:
        raise AssertionError("foreign-key drift must fail closed")

    corrupt_database = (root / "qualification-corrupt.sqlite3").resolve()
    corrupt_database.write_bytes(database.read_bytes())
    with corrupt_database.open("r+b") as handle:
        handle.seek(4096)
        handle.write(b"\xff" * 512)
    try:
        reconcile._qualify_current_sqlite(
            corrupt_database,
            backup_root=qualification_root,
            expected_source=hot._file_identity(corrupt_database),
            max_bytes=1024 * 1024,
            max_seconds=10.0,
            max_copy_bytes_per_second=1024 * 1024 * 1024,
        )
    except reconcile.ReconcileExistingError as exc:
        assert "integrity" in str(exc)
    else:
        raise AssertionError("SQLite corruption must fail closed")


def _digest_and_allowed_writer_smoke(root: Path) -> dict[str, object]:
    database = _database(root / "operational.sqlite3")
    before_sha = hot.file_sha256(database)
    connection = sqlite3.connect(database)
    canonical_rows = reconcile._rows(
        connection, "change_registry_observer_job_events"
    )
    connection.close()
    connection = sqlite3.connect(database)
    job_ids = {
        str(row["job_id"])
        for row in reconcile._rows(connection, "change_registry_observer_jobs")
    }
    connection.close()
    for job_id in job_ids:
        assert [
            int(row["sequence_no"])
            for row in canonical_rows
            if row["job_id"] == job_id
        ] != [1, 2, 3]
    first = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    assert first["sqlite_readback"]["integrity_check"] == "ok"
    assert first["sqlite_readback"]["foreign_key_violation_count"] == 0
    assert first["non_operational"]["table_row_counts"]["business_projection"] == 1
    assert first["operational_rows"]["activation_deployed_shas"] == sorted(
        RELEASE_SHAS
    )
    assert len(first["operational_rows"]["scheduled_observer_job_ids"]) == 1
    assert len(first["operational_rows"]["activation_jobs"]) == 4

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE sheet_vitrina_v1_source_health_status "
        "SET payload_json=?,checked_at=? WHERE source_key=?",
        (
            json.dumps({"session_status": "valid", "probe": 2}, sort_keys=True),
            "2026-09-02T04:07:48Z",
            "seller_portal_auth",
        ),
    )
    connection.commit()
    connection.close()
    allowed = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    assert allowed["non_operational"] == first["non_operational"]
    assert allowed["operational"] != first["operational"]
    assert hot.file_sha256(database) != before_sha

    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO change_registry_observer_job_events VALUES(?,?,?,?,?,?,?,?)",
        (
            "unexpected-event", "historical-job", 1, "complete",
            "2026-09-02T04:07:59Z", None, 0, "complete",
        ),
    )
    connection.commit()
    connection.close()
    try:
        reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    except reconcile.ReconcileExistingError as exc:
        assert "event set" in str(exc)
    else:
        raise AssertionError("unexpected operational row writer must fail closed")
    connection = sqlite3.connect(database)
    connection.execute(
        "DELETE FROM change_registry_observer_job_events "
        "WHERE job_event_id='unexpected-event'"
    )
    connection.execute(
        "INSERT INTO business_projection VALUES(?,?,?)",
        ("unexpected", sqlite3.Binary(b"third-writer"), "material"),
    )
    connection.commit()
    connection.close()
    unexpected = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    assert unexpected["non_operational"] != allowed["non_operational"]
    return {"database": database, "evidence": allowed}


def _semantic_rejection_smoke(root: Path) -> None:
    cases = (
        (
            "unknown-type",
            "UPDATE change_registry_observer_jobs SET trigger_kind='manual' "
            "WHERE trigger_kind='scheduled'",
            "type is not allowed",
        ),
        (
            "unknown-actor",
            "UPDATE change_registry_observer_jobs SET requested_by='operator' "
            "WHERE trigger_kind='activation'",
            "activation job identity",
        ),
        (
            "business-facts",
            "UPDATE change_registry_observer_job_events SET fact_count=1 "
            "WHERE sequence_no=3",
            "produced business facts",
        ),
        (
            "non-lifecycle",
            "UPDATE change_registry_observer_job_events SET state='partial' "
            "WHERE sequence_no=2",
            "event states",
        ),
        (
            "source-drift",
            "UPDATE change_registry_checkpoints SET source_surface='unknown' "
            "WHERE checkpoint_id='checkpoint-1'",
            "checkpoint source semantics",
        ),
    )
    for name, statement, expected in cases:
        database = _database(root / f"reject-{name}.sqlite3")
        connection = sqlite3.connect(database)
        connection.execute(statement)
        connection.commit()
        connection.close()
        try:
            reconcile.database_evidence(database, deployed_sha=NEW_SHA)
        except reconcile.ReconcileExistingError as exc:
            assert expected in str(exc), (name, str(exc))
        else:
            raise AssertionError(f"{name} must fail closed")
    database = _database(root / "reject-deploy-metadata.sqlite3")
    try:
        reconcile.database_evidence(database, deployed_sha="c" * 40)
    except reconcile.ReconcileExistingError as exc:
        assert "deploy activation metadata" in str(exc)
    else:
        raise AssertionError("unobserved current deploy metadata must fail closed")


def _marker_only_apply_smoke(root: Path, fixture: dict[str, object]) -> None:
    database = Path(fixture["database"])
    # Restore the one unexpected row before sealing the reviewed plan.
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM business_projection WHERE row_id='unexpected'")
    connection.commit()
    connection.close()
    evidence = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    runtime = root / "runtime"
    backup = root / "backup"
    runtime.mkdir()
    backup.mkdir()
    operation_id = "1" * 64
    result_dir = backup / "operation"
    plan = {
        "contract_name": reconcile.CONTRACT_NAME,
        "mode": reconcile.MODE,
        "read_only": True,
        "operation_id": operation_id,
        "deployed_sha": NEW_SHA,
        "source_epoch_deployed_sha": OLD_SHA,
        "runtime_dir": str(runtime),
        "barrier": {
            "active": True, "phase": "acquiring", "hold_confirmed": False,
            "window_id": WINDOW, "plan_fingerprint": PLAN_FP,
            "state_fingerprint": STATE_FP,
        },
        "maintenance": {
            "partial_epoch_fingerprint": "sha256:" + "f" * 64,
            "business_operation_counters": {"operations": 0, "submits": 0},
        },
        "business_writer_timeline": {
            "event_count": 0, "digest": "sha256:" + hashlib.sha256(b"").hexdigest(),
        },
        "database": hot._file_identity(database),
        "database_reconciliation": evidence,
        "backup": {
            "directory": str(result_dir),
            "reserve_bytes": 0,
            "evidence_envelope_bytes": 1,
        },
        "created_at": "2026-09-02T05:00:00Z",
    }
    plan["fingerprint"] = reconcile._plan_fingerprint(plan)
    plan_path = root / "reviewed.json"
    plan_path.write_text(reconcile._canonical_json(plan) + "\n", encoding="utf-8")
    plan_sha = "sha256:" + hot.file_sha256(plan_path)

    # An unlisted writer/table after review must fail before evidence or marker writes.
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE unexpected_writer(row_id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO unexpected_writer VALUES('third-writer')")
    connection.commit()
    connection.close()
    unexpected = json.loads(json.dumps(plan))
    unexpected["database"] = hot._file_identity(database)
    unexpected["database_reconciliation"] = reconcile.database_evidence(
        database, deployed_sha=NEW_SHA
    )
    unexpected["fingerprint"] = reconcile._plan_fingerprint(unexpected)
    with (
        mock.patch.object(reconcile, "DEFAULT_BACKUP_ROOT", backup),
        mock.patch.object(reconcile, "build_plan", return_value=unexpected),
    ):
        try:
            reconcile.apply_plan(
                plan_path=plan_path,
                plan_sha256=plan_sha,
                fingerprint=plan["fingerprint"],
                deployed_sha_file=root / "unused",
            )
        except reconcile.ReconcileExistingError as exc:
            assert "stale" in str(exc)
        else:
            raise AssertionError("unexpected third-table writer must fail closed")
    assert not result_dir.exists()
    assert not (runtime / hot.MARKER_FILENAME).exists()

    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE unexpected_writer")
    connection.commit()
    connection.close()
    evidence = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    plan["database"] = hot._file_identity(database)
    plan["database_reconciliation"] = evidence
    plan["fingerprint"] = reconcile._plan_fingerprint(plan)
    plan_path.write_text(reconcile._canonical_json(plan) + "\n", encoding="utf-8")
    plan_sha = "sha256:" + hot.file_sha256(plan_path)
    database_before = database.read_bytes()
    with (
        mock.patch.object(reconcile, "DEFAULT_BACKUP_ROOT", backup),
        mock.patch.object(reconcile, "build_plan", return_value=dict(plan)),
    ):
        result = reconcile.apply_plan(
            plan_path=plan_path,
            plan_sha256=plan_sha,
            fingerprint=plan["fingerprint"],
            deployed_sha_file=root / "unused",
        )
    assert result["mode"] == "reconciled_existing"
    assert result["sqlite_write"] is False
    assert result["logical_business_delta"] == 0
    assert database.read_bytes() == database_before
    marker = json.loads((runtime / hot.MARKER_FILENAME).read_text())
    assert marker["non_operational_digest"] == evidence["non_operational"]
    assert marker["operational_digest"] == evidence["operational"]

    stale = json.loads(json.dumps(plan))
    stale["database_reconciliation"]["non_operational"]["digest"] = (
        "sha256:" + "0" * 64
    )
    assert not reconcile._fresh_matches_plan(plan, stale)


def _one_submit_smoke(root: Path) -> None:
    runtime = root / "jobs"
    backups = root / "root-backups"
    runtime.mkdir()
    backups.mkdir()
    sha_file = root / "sha"
    sha_file.write_text(NEW_SHA + "\n", encoding="utf-8")
    plan_path = (
        "/opt/wb-core-runtime/state/private-evidence/production-goals/"
        f"wbc0027-s047-reconcile-existing-plan-{NEW_SHA}.json"
    )
    kwargs = {
        "runtime_dir": runtime,
        "root_backups": backups,
        "deployed_sha_file": sha_file,
        "job_id": "2" * 64,
        "deployed_sha": NEW_SHA,
        "operation": "sqlite-hot-journal-reconcile-existing-apply",
        "root_name": "",
        "family": "",
        "reviewed_plan": plan_path,
        "reviewed_plan_sha256": "sha256:" + "3" * 64,
        "confirm_fingerprint": "sha256:" + "4" * 64,
        "approval_reference": "ROOT exact reconcile callback",
        "starter": lambda job_id: {"name": job_id},
    }
    original = jobs._read_json

    def read(path: Path, *, label: str):
        if label == "hot journal reviewed plan":
            return {
                "contract_name": reconcile.CONTRACT_NAME,
                "operation_id": kwargs["job_id"],
                "deployed_sha": NEW_SHA,
                "fingerprint": kwargs["confirm_fingerprint"],
            }
        return original(path, label=label)

    with (
        mock.patch.object(jobs, "_read_json", side_effect=read),
        mock.patch.object(jobs, "file_sha256", return_value="3" * 64),
    ):
        first = jobs.submit_job(**kwargs)
        second = jobs.submit_job(**kwargs)
        with mock.patch.object(
            reconcile,
            "apply_plan",
            return_value={
                "status": "reconciled_existing",
                "logical_business_delta": 0,
                "sqlite_write": False,
            },
        ) as apply:
            terminal = jobs.run_worker(
                runtime_dir=runtime,
                root_backups=backups,
                deployed_sha_file=sha_file,
                job_id=kwargs["job_id"],
            )
    assert first["unit_start_requested"] is True
    assert second["submit_idempotent"] is True
    assert terminal["status"] == "succeeded"
    apply.assert_called_once_with(
        plan_path=Path(plan_path),
        plan_sha256=kwargs["reviewed_plan_sha256"],
        fingerprint=kwargs["confirm_fingerprint"],
        deployed_sha_file=sha_file,
    )


def _marker_validator_smoke(fixture: dict[str, object]) -> None:
    evidence = reconcile.database_evidence(
        Path(fixture["database"]), deployed_sha=NEW_SHA
    )
    partial = {"deployed_sha": OLD_SHA, "epoch": 2}
    result = {
        "contract_name": maintenance.HOT_JOURNAL_RECOVERY_RESULT_CONTRACT,
        "mode": reconcile.MODE,
        "operation_id": "5" * 64,
        "source_epoch_deployed_sha": OLD_SHA,
        "deployed_sha": NEW_SHA,
        "barrier": {
            "window_id": WINDOW, "plan_fingerprint": PLAN_FP,
            "state_fingerprint": STATE_FP,
        },
        "maintenance_partial_epoch_fingerprint": maintenance._stable_fingerprint(partial),
        "database_after": hot._file_identity(Path(fixture["database"])),
        "journal_absent": True,
        "sqlite_readback": evidence["sqlite_readback"],
        "non_operational_digest": evidence["non_operational"],
        "operational_digest": evidence["operational"],
        "operational_rows": evidence["operational_rows"],
        "observer_cutoff_tail_cas": evidence["observer_cutoff_tail_cas"],
        "business_operation_counters": {"operations": 0},
        "logical_business_delta": 0,
        "sqlite_write": False,
    }
    result["result_fingerprint"] = maintenance._stable_fingerprint(result)
    marker = {
        key: result[key]
        for key in (
            "contract_name", "mode", "operation_id", "source_epoch_deployed_sha",
            "deployed_sha", "barrier", "maintenance_partial_epoch_fingerprint",
            "database_after", "journal_absent", "sqlite_readback",
            "non_operational_digest", "operational_digest",
            "observer_cutoff_tail_cas",
            "business_operation_counters", "result_fingerprint",
        )
    }
    marker["result_path"] = (
        "/opt/wb-core-runtime/state/backups/private-evidence/production-goals/"
        "wbc0027-s047-reconcile-existing-bbbbbbbb/" + result["operation_id"]
        + "/result.json"
    )
    marker["marker_fingerprint"] = maintenance._stable_fingerprint(marker)

    def load(path: Path):
        return marker if path.name == hot.MARKER_FILENAME else result

    with (
        mock.patch.object(maintenance, "_load_json_object", side_effect=load),
        mock.patch.object(reconcile.hot, "_file_identity", return_value=result["database_after"]),
        mock.patch.object(reconcile, "_qualify_current_sqlite", return_value={}),
        mock.patch.object(reconcile, "database_evidence", return_value=evidence),
        mock.patch.object(
            maintenance, "_prepared_abort_breakglass_counters",
            return_value={"operations": 0},
        ),
        mock.patch.object(Path, "exists", return_value=False),
    ):
        maintenance._validate_hot_journal_recovery_marker(
            Path("/runtime"), partial_epoch=partial, deployed_sha=NEW_SHA,
            barrier=result["barrier"],
        )
    drifted = json.loads(json.dumps(evidence))
    drifted["observer_cutoff_tail_cas"]["prefix_digest"] = (
        "sha256:" + "0" * 64
    )
    with (
        mock.patch.object(maintenance, "_load_json_object", side_effect=load),
        mock.patch.object(
            reconcile.hot, "_file_identity", return_value=result["database_after"]
        ),
        mock.patch.object(reconcile, "_qualify_current_sqlite", return_value={}),
        mock.patch.object(reconcile, "database_evidence", return_value=drifted),
        mock.patch.object(
            maintenance, "_prepared_abort_breakglass_counters",
            return_value={"operations": 0},
        ),
        mock.patch.object(Path, "exists", return_value=False),
    ):
        try:
            maintenance._validate_hot_journal_recovery_marker(
                Path("/runtime"), partial_epoch=partial, deployed_sha=NEW_SHA,
                barrier=result["barrier"],
            )
        except RuntimeError as exc:
            assert "logical CAS" in str(exc)
        else:
            raise AssertionError("post-marker operational append must fail closed")


def _query_only_rehearsal_smoke(root: Path) -> None:
    runtime = root / "query-only-rehearsal"
    runtime.mkdir()
    operation_directory = root / "must-not-be-created"
    barrier = {
        "active": True,
        "phase": "acquiring",
        "hold_confirmed": False,
        "window_id": WINDOW,
        "plan_fingerprint": PLAN_FP,
        "state_fingerprint": STATE_FP,
    }
    timer_states = {
        unit: {"is_enabled": "disabled", "is_active": "inactive"}
        for unit in maintenance.ALL_BUSINESS_TIMER_UNITS
    }
    qualification = {
        "contract_name": reconcile.SQLITE_QUALIFICATION_CONTRACT_NAME,
        "method": reconcile.SQLITE_QUALIFICATION_METHOD,
        "source": {"size_bytes": 4, "sha256": "1" * 64},
        "copy": {"anonymous": True, "size_bytes": 4, "sha256": "1" * 64},
        "sqlite": {
            "query_only": 1,
            "journal_mode": "delete",
            "integrity_check": "ok",
            "foreign_key_violation_count": 0,
        },
    }
    preflight = {
        "contract_name": reconcile.CONTRACT_NAME,
        "mode": reconcile.MODE,
        "read_only": True,
        "deployed_sha": NEW_SHA,
        "source_epoch_deployed_sha": OLD_SHA,
        "barrier": barrier,
        "maintenance": {
            "phase": "abort_quiescing",
            "timer_states": timer_states,
            "business_operation_counters": {"submit": 0},
        },
        "systemd_jobs": [],
        "business_writer_timeline": {"event_count": 0},
        "database": {"size_bytes": 4, "sha256": "1" * 64},
        "database_reconciliation": {
            "sqlite_qualification": qualification,
            "non_operational": {"digest": "sha256:" + "2" * 64},
            "operational": {"digest": "sha256:" + "3" * 64},
            "observer_cutoff_tail_cas": {"prefix_digest": "sha256:" + "4" * 64},
            "sqlite_readback": qualification["sqlite"],
        },
        "backup": {
            "directory": str(operation_directory),
            "capacity_before": {"available_bytes": 100},
            "qualification_peak_bytes": 4,
            "qualification_peak_available_bytes": 96,
            "projected_reserve_headroom_bytes": 90,
        },
    }
    systemd = mock.Mock()
    systemd.unit_state.side_effect = lambda unit: timer_states[unit]
    before_paths = set(root.rglob("*"))
    with (
        mock.patch.object(reconcile, "barrier_status", return_value=barrier),
        mock.patch.object(reconcile, "_preflight", return_value=preflight),
        mock.patch.object(maintenance, "SystemdClient", return_value=systemd),
    ):
        result = reconcile.build_rehearsal(
            runtime_dir=runtime,
            backup_root=root,
            deployed_sha=NEW_SHA,
            deployed_sha_file=root / "unused-sha",
            operation_id="5" * 64,
            reserve_bytes=reconcile.DEFAULT_RESERVE_BYTES,
            evidence_envelope_bytes=reconcile.DEFAULT_EVIDENCE_ENVELOPE_BYTES,
            stable_interval_seconds=0,
        )
    assert result["contract_name"] == reconcile.REHEARSAL_CONTRACT_NAME
    assert result["status"] == "READY_FOR_RECOVERY"
    assert result["phase_count"] == 8
    assert [phase["phase"] for phase in result["phases"]] == [
        "preflight",
        "readiness",
        "JIT",
        "worker namespace",
        "storage admission/private plan persistence",
        "submit boundary",
        "query-only readback",
        "release interruption",
    ]
    assert result["private_plan_created"] is False
    assert result["recovery_job_created"] is False
    assert result["submit_count"] == 0
    assert result["marker_created"] is False
    assert set(root.rglob("*")) == before_paths


def main() -> None:
    assert reconcile.EXPECTED_SEALED_ROLLBACK["nonce_hex"] == "5296552f"
    assert reconcile.EXPECTED_SEALED_ROLLBACK["record_count"] == 169
    assert reconcile.EXPECTED_SEALED_ROLLBACK["checksum_mismatch_count"] == 0
    assert reconcile.EXPECTED_SEALED_ROLLBACK["page_number_list_sha256"] == (
        "8ef27ddebf2d2b12dbf0050bd7719d64a18266f38917aa38a4cf170ec7c8a12e"
    )
    with tempfile.TemporaryDirectory(prefix="wbc0027-reconcile-existing-") as raw:
        root = Path(raw)
        _incident_predicate_order_red_smoke(root)
        _incident_exception_rejection_smoke(root)
        _observer_cutoff_tail_cas_smoke(root)
        _observer_cutoff_terminal_boundary_red_smoke(root)
        _sqlite_qualification_red_smoke(root)
        fixture = _digest_and_allowed_writer_smoke(root)
        _semantic_rejection_smoke(root)
        _marker_only_apply_smoke(root, fixture)
        _one_submit_smoke(root)
        _marker_validator_smoke(fixture)
        _query_only_rehearsal_smoke(root)
    print("sqlite_hot_journal_reconcile_existing_smoke: ok")


if __name__ == "__main__":
    main()
