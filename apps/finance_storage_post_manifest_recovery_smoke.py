#!/usr/bin/env python3
"""Regression checks for bounded post-manifest Finance recovery."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import packages.application.finance_storage_post_manifest_recovery as recovery
from packages.application.storage_registry import (
    atomic_write_manifest,
    build_manifest,
)


SPLIT_GENERATION = "1" * 20


def _create_fixture(runtime: Path) -> dict[str, object]:
    rollback_root = runtime / "generations" / "rollback-smoke"
    split_root = runtime / "generations" / SPLIT_GENERATION
    rollback_root.mkdir(parents=True)
    split_root.mkdir(parents=True)
    monolith_path = rollback_root / "monolith.sqlite3"
    raw_path = split_root / "finance_raw.sqlite3"
    operational_path = split_root / "operational.sqlite3"
    raw_row = (
        "canonical",
        "2026-07-01",
        "2026-07-07",
        "report-1",
        "1",
        "row-hash-1",
    )
    with sqlite3.connect(monolith_path) as connection:
        connection.execute(
            """CREATE TABLE wb_finance_weekly_raw_rows(
                   seller_id TEXT,week_start TEXT,week_end TEXT,
                   report_id TEXT,rrd_id TEXT,row_hash TEXT)"""
        )
        connection.execute(
            "INSERT INTO wb_finance_weekly_raw_rows VALUES(?,?,?,?,?,?)",
            raw_row,
        )
        connection.execute(
            "CREATE TABLE business_rows(id INTEGER PRIMARY KEY,value TEXT)"
        )
        connection.execute(
            "INSERT INTO business_rows VALUES(1,'same')"
        )
        connection.execute(
            """CREATE TABLE sheet_vitrina_v1_wb_incident_projection_cache(
                   seller_id TEXT,snapshot_digest TEXT,
                   policy_revision INTEGER,snapshot_date TEXT,
                   projection_json TEXT,created_at TEXT,
                   PRIMARY KEY(
                       seller_id,snapshot_digest,policy_revision,snapshot_date
                   ))"""
        )
    with sqlite3.connect(raw_path) as connection:
        connection.execute(
            """CREATE TABLE finance_raw_current_rows(
                   seller_id TEXT,week_start TEXT,week_end TEXT,
                   report_id TEXT,rrd_id TEXT,row_hash TEXT)"""
        )
        connection.execute(
            "INSERT INTO finance_raw_current_rows VALUES(?,?,?,?,?,?)",
            raw_row,
        )
    retained_projection = {
        "snapshot_digest": "cache-digest",
        "projection_cache_policy_revision": 2,
        "value": 1,
        "cache": {"status": "miss"},
    }
    with sqlite3.connect(operational_path) as connection:
        connection.execute(
            "CREATE TABLE business_rows(id INTEGER PRIMARY KEY,value TEXT)"
        )
        connection.execute(
            "INSERT INTO business_rows VALUES(1,'same')"
        )
        connection.execute(
            """CREATE TABLE sheet_vitrina_v1_wb_incident_projection_cache(
                   seller_id TEXT,snapshot_digest TEXT,
                   policy_revision INTEGER,snapshot_date TEXT,
                   projection_json TEXT,created_at TEXT,
                   PRIMARY KEY(
                       seller_id,snapshot_digest,policy_revision,snapshot_date
                   ))"""
        )
        connection.execute(
            """INSERT INTO sheet_vitrina_v1_wb_incident_projection_cache
               VALUES(?,?,?,?,?,?)""",
            (
                "canonical",
                "cache-digest",
                2,
                "2026-07-31",
                json.dumps(retained_projection),
                "2026-07-31T00:00:00Z",
            ),
        )
    manifest = build_manifest(
        state="monolith",
        canonical_source="monolith",
        generation_epoch="rollback-smoke",
        raw_generation_id="rollback-smoke",
        raw_relative_path=(
            "generations/rollback-smoke/monolith.sqlite3"
        ),
        raw_watermark="1",
        operational_generation_id="rollback-smoke",
        operational_relative_path=(
            "generations/rollback-smoke/monolith.sqlite3"
        ),
        operational_watermark="same",
        rollback_generation_id=SPLIT_GENERATION,
        source_fingerprint="sha256:" + "2" * 64,
        created_at="2026-07-31T00:00:00Z",
    )
    atomic_write_manifest(
        runtime / "storage_generation_manifest.json",
        manifest,
    )
    return {
        "monolith": monolith_path,
        "retained_operational": operational_path,
        "retained_projection": retained_projection,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(
        prefix="finance-post-manifest-recovery-smoke-"
    ) as raw:
        runtime = Path(raw)
        fixture = _create_fixture(runtime)
        rebuilt_projection = {
            **dict(fixture["retained_projection"]),
            "cache": {"status": "bypassed"},
        }
        with (
            mock.patch.object(
                recovery,
                "barrier_status",
                return_value={
                    "active": True,
                    "phase": "restoring",
                    "hold_confirmed": True,
                },
            ),
            mock.patch.object(
                recovery,
                "_rebuild_projection",
                return_value=rebuilt_projection,
            ),
        ):
            result = recovery.readback(
                runtime,
                expected_retained_generation=SPLIT_GENERATION,
            )
        assert result["status"] == "ready_for_repo_owned_refresh"
        assert result["raw"]["row_count"] == 1
        assert result["operational"]["non_cache_match"] is True
        assert len(
            result["cache"][
                "recoverable_missing_canonical_rows"
            ]
        ) == 1
        assert result["cache"]["direct_row_copy_allowed"] is False

        with (
            mock.patch.object(
                recovery,
                "barrier_status",
                return_value={
                    "active": True,
                    "phase": "restoring",
                    "hold_confirmed": True,
                },
            ),
            mock.patch.object(
                recovery,
                "_rebuild_projection",
                side_effect=(
                    recovery.FinanceStorageProjectionSourceUnavailable(
                        "accepted snapshot unavailable"
                    )
                ),
            ),
        ):
            source_unavailable = recovery.readback(
                runtime,
                expected_retained_generation=SPLIT_GENERATION,
            )
        orphan = source_unavailable["cache"][
            "recoverable_missing_canonical_rows"
        ]
        assert len(orphan) == 1
        assert orphan[0]["accepted_snapshot_available"] is False
        assert (
            orphan[0]["recovery_mode"]
            == "canonical_cache_miss_fresh_refresh"
        )
        assert source_unavailable["cache"]["direct_row_copy_allowed"] is False

        with sqlite3.connect(
            Path(str(fixture["retained_operational"]))
        ) as retained:
            retained.execute(
                """INSERT INTO sheet_vitrina_v1_wb_incident_projection_cache
                   VALUES(?,?,?,?,?,?)""",
                (
                    "canonical",
                    "cache-digest-2",
                    2,
                    "2026-07-30",
                    json.dumps(fixture["retained_projection"]),
                    "2026-07-31T00:00:00Z",
                ),
            )
        with (
            mock.patch.object(
                recovery,
                "barrier_status",
                return_value={
                    "active": True,
                    "phase": "restoring",
                    "hold_confirmed": True,
                },
            ),
            mock.patch.object(
                recovery,
                "_rebuild_projection",
                side_effect=(
                    recovery.FinanceStorageProjectionSourceUnavailable(
                        "accepted snapshot unavailable"
                    )
                ),
            ),
        ):
            try:
                recovery.readback(
                    runtime,
                    expected_retained_generation=SPLIT_GENERATION,
                )
            except recovery.FinanceStoragePostManifestRecoveryError as exc:
                if "source exceed" not in str(exc):
                    raise
            else:
                raise AssertionError(
                    "multiple source-unavailable cache rows were not blocked"
                )
        with sqlite3.connect(
            Path(str(fixture["retained_operational"]))
        ) as retained:
            retained.execute(
                """DELETE FROM sheet_vitrina_v1_wb_incident_projection_cache
                   WHERE snapshot_digest='cache-digest-2'"""
            )

        with (
            sqlite3.connect(
                Path(str(fixture["monolith"]))
            ) as canonical,
            sqlite3.connect(
                Path(str(fixture["retained_operational"]))
            ) as retained,
        ):
            canonical.execute(
                """INSERT INTO sheet_vitrina_v1_wb_incident_projection_cache
                   VALUES(?,?,?,?,?,?)""",
                (
                    "canonical",
                    "cache-digest",
                    2,
                    "2026-07-31",
                    json.dumps(fixture["retained_projection"]),
                    "2026-07-31T00:00:00Z",
                ),
            )
            retained.execute(
                "DELETE FROM sheet_vitrina_v1_wb_incident_projection_cache"
            )
        with (
            mock.patch.object(
                recovery,
                "barrier_status",
                return_value={
                    "active": True,
                    "phase": "restoring",
                    "hold_confirmed": True,
                },
            ),
            mock.patch.object(
                recovery,
                "_rebuild_projection",
                return_value=rebuilt_projection,
            ),
        ):
            canonical_only = recovery.readback(
                runtime,
                expected_retained_generation=SPLIT_GENERATION,
            )
        assert canonical_only["status"] == "reconciled"
        assert canonical_only["cache"]["bounded_drift_row_count"] == 1
        assert canonical_only["cache"]["regeneration_required"] is False
        assert canonical_only["cache"]["accepted_canonical_rows"] == [
            {
                "classification": "canonical_only",
                "seller_id": "canonical",
                "snapshot_digest": "cache-digest",
                "policy_revision": 2,
                "snapshot_date": "2026-07-31",
                "semantic_digest": recovery._digest(
                    recovery._normalized_projection(
                        rebuilt_projection
                    )
                ),
                "canonical_rebuild_match": True,
                "retained_generation_mutation_required": False,
            }
        ]

        retained_stale_projection = {
            **dict(fixture["retained_projection"]),
            "value": 999,
        }
        with sqlite3.connect(
            Path(str(fixture["retained_operational"]))
        ) as retained:
            retained.execute(
                """INSERT INTO sheet_vitrina_v1_wb_incident_projection_cache
                   VALUES(?,?,?,?,?,?)""",
                (
                    "canonical",
                    "cache-digest",
                    2,
                    "2026-07-31",
                    json.dumps(retained_stale_projection),
                    "2026-07-31T00:00:00Z",
                ),
            )
        with (
            mock.patch.object(
                recovery,
                "barrier_status",
                return_value={
                    "active": True,
                    "phase": "restoring",
                    "hold_confirmed": True,
                },
            ),
            mock.patch.object(
                recovery,
                "_rebuild_projection",
                return_value=rebuilt_projection,
            ),
        ):
            semantic_drift = recovery.readback(
                runtime,
                expected_retained_generation=SPLIT_GENERATION,
            )
        assert semantic_drift["status"] == "reconciled"
        assert semantic_drift["cache"]["semantic_mismatch_count"] == 1
        assert (
            semantic_drift["cache"]["accepted_canonical_rows"][0][
                "classification"
            ]
            == "canonical_authoritative_semantic_mismatch"
        )

        with sqlite3.connect(
            Path(str(fixture["monolith"]))
        ) as connection:
            connection.execute(
                "UPDATE business_rows SET value='drift'"
            )
        with (
            mock.patch.object(
                recovery,
                "barrier_status",
                return_value={
                    "active": True,
                    "phase": "restoring",
                    "hold_confirmed": True,
                },
            ),
            mock.patch.object(
                recovery,
                "_rebuild_projection",
                return_value=rebuilt_projection,
            ),
        ):
            try:
                recovery.readback(
                    runtime,
                    expected_retained_generation=SPLIT_GENERATION,
                )
            except recovery.FinanceStoragePostManifestRecoveryError as exc:
                if "non-cache operational table differs" not in str(exc):
                    raise
            else:
                raise AssertionError(
                    "non-cache operational drift was not blocked"
                )
    print(
        "finance_storage_post_manifest_recovery_smoke: ok -> "
        "raw/non-cache equality, deterministic cache regeneration, "
        "no row copy, fail-closed core drift"
    )


if __name__ == "__main__":
    main()
