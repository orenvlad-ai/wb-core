"""Query-only proof for bounded Finance post-manifest recovery.

The recovery never copies derived cache rows between generations.  It proves
that core raw and operational state are equal, classifies only the allowlisted
Vitrina projection cache drift, and verifies every missing canonical cache row
can be deterministically rebuilt from the selected monolith's accepted
temporal snapshot and policy.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from packages.application.business_data_write_barrier import barrier_status
from packages.application.finance_storage_migration import (
    FinanceStorageMigrationError,
    FinanceStorageShadowVerifier,
    LEGACY_RAW_TABLE,
    RAW_LEGACY_OBJECTS,
    logical_table_digest,
)
from packages.application.finance_raw_storage import RAW_SCHEMA_TABLES
from packages.application.registry_upload_db_backed_runtime import (
    _deserialize_temporal_source_payload,
    _sheet_vitrina_user_config_row_to_dict,
    _wb_incident_policy_row_to_dict,
)
from packages.application.storage_registry import StoreRegistry
from packages.application.wb_incident_policy import (
    build_vitrina_incident_stock_projection,
)


CONTRACT_NAME = "wb_core_finance_post_manifest_recovery_readback_v1"
CACHE_TABLE = "sheet_vitrina_v1_wb_incident_projection_cache"
MAX_RECOVERABLE_CACHE_ROWS = 8


class FinanceStoragePostManifestRecoveryError(ValueError):
    """The selected monolith cannot be safely recovered in place."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro",
        uri=True,
        timeout=60,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
        connection.close()
        raise FinanceStoragePostManifestRecoveryError(
            "SQLite query_only could not be enabled"
        )
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
        )
    }


def _normalized_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = json.loads(_canonical_json(dict(payload)))
    normalized.pop("cache", None)
    return normalized


def _cache_rows(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str, int, str], dict[str, Any]]:
    if CACHE_TABLE not in _tables(connection):
        return {}
    rows: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for row in connection.execute(
        f"""SELECT seller_id,snapshot_digest,policy_revision,snapshot_date,
                   projection_json,created_at
            FROM {CACHE_TABLE}
            ORDER BY seller_id,snapshot_digest,policy_revision,snapshot_date"""
    ):
        key = (
            str(row["seller_id"]),
            str(row["snapshot_digest"]),
            int(row["policy_revision"]),
            str(row["snapshot_date"]),
        )
        projection = json.loads(str(row["projection_json"]))
        if not isinstance(projection, dict):
            raise FinanceStoragePostManifestRecoveryError(
                "Vitrina projection cache contains a non-object payload"
            )
        rows[key] = {
            "projection": projection,
            "semantic_digest": _digest(
                _normalized_projection(projection)
            ),
            "created_at": str(row["created_at"] or ""),
        }
    return rows


class _ReadOnlyProjectionRuntime:
    """Minimum policy/snapshot facade required by the projection builder."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _policy(
        self,
        *,
        seller_id: str,
        where: str,
        params: tuple[Any, ...],
    ) -> dict[str, Any]:
        row = self.connection.execute(
            f"""SELECT * FROM sheet_vitrina_v1_wb_incident_policy_revisions
                WHERE seller_id=? AND {where}
                ORDER BY revision DESC LIMIT 1""",
            (seller_id, *params),
        ).fetchone()
        return _wb_incident_policy_row_to_dict(
            row,
            seller_id=seller_id,
        )

    def load_latest_wb_incident_policy(
        self,
        *,
        seller_id: str,
    ) -> dict[str, Any]:
        return self._policy(
            seller_id=seller_id,
            where="1=1",
            params=(),
        )

    def load_wb_incident_policy_for_date(
        self,
        *,
        seller_id: str,
        snapshot_date: str,
    ) -> dict[str, Any]:
        normalized = date.fromisoformat(snapshot_date).isoformat()
        return self._policy(
            seller_id=seller_id,
            where="effective_from<=?",
            params=(normalized,),
        )

    def load_wb_incident_policy_started_by_date(
        self,
        *,
        seller_id: str,
        snapshot_date: str,
    ) -> dict[str, Any]:
        return self.load_wb_incident_policy_for_date(
            seller_id=seller_id,
            snapshot_date=snapshot_date,
        )

    def list_sheet_vitrina_user_configs(
        self,
        *,
        config_key: str,
    ) -> list[dict[str, Any]]:
        if (
            "sheet_vitrina_v1_user_configs"
            not in _tables(self.connection)
        ):
            return []
        rows = self.connection.execute(
            """SELECT user_key,config_key,schema_version,payload_json,
                      updated_at,revision
               FROM sheet_vitrina_v1_user_configs
               WHERE config_key=? ORDER BY user_key""",
            (config_key,),
        ).fetchall()
        return [
            _sheet_vitrina_user_config_row_to_dict(row)
            for row in rows
        ]

    def load_temporal_source_snapshot(
        self,
        *,
        source_key: str,
        snapshot_date: str,
    ) -> tuple[Any | None, str | None]:
        row = self.connection.execute(
            """SELECT captured_at,payload_json
               FROM temporal_source_snapshots
               WHERE source_key=? AND snapshot_date=?""",
            (source_key, snapshot_date),
        ).fetchone()
        if row is None:
            return None, None
        return (
            _deserialize_temporal_source_payload(
                str(row["payload_json"])
            ),
            str(row["captured_at"] or ""),
        )


def _rebuild_projection(
    connection: sqlite3.Connection,
    *,
    key: tuple[str, str, int, str],
) -> dict[str, Any]:
    seller_id, snapshot_digest, policy_revision, snapshot_date = key
    runtime = _ReadOnlyProjectionRuntime(connection)
    payload, captured_at = runtime.load_temporal_source_snapshot(
        source_key="stocks",
        snapshot_date=snapshot_date,
    )
    if (
        payload is None
        or str(getattr(payload, "kind", "") or "") != "success"
    ):
        raise FinanceStoragePostManifestRecoveryError(
            "accepted stocks success snapshot is unavailable for bounded "
            f"cache recovery: {snapshot_date}"
        )
    projection = build_vitrina_incident_stock_projection(
        runtime,
        items=list(getattr(payload, "items", []) or []),
        warehouse_rows=list(
            getattr(payload, "warehouse_rows", []) or []
        ),
        snapshot_date=str(
            getattr(payload, "snapshot_date", "") or snapshot_date
        ),
        fetched_at=str(
            getattr(payload, "fetched_at", "")
            or captured_at
            or ""
        ),
        pagination_complete=bool(
            getattr(payload, "pagination_complete", False)
        ),
        raw_rows_digest=str(
            getattr(payload, "raw_rows_digest", "") or ""
        ),
        seller_id=seller_id,
        cache_enabled=False,
    )
    actual_digest = str(
        projection.get("cache_identity_digest")
        or projection.get("snapshot_digest")
        or ""
    )
    if (
        actual_digest != snapshot_digest
        or int(
            projection.get("projection_cache_policy_revision")
            or 0
        )
        != policy_revision
    ):
        raise FinanceStoragePostManifestRecoveryError(
            "rebuilt projection identity does not match the bounded cache row"
        )
    return projection


def readback(
    runtime_dir: Path,
    *,
    expected_retained_generation: str,
) -> dict[str, Any]:
    """Prove post-manifest recovery without mutating either generation."""

    root = Path(runtime_dir).expanduser().resolve()
    registry = StoreRegistry(root)
    manifest = registry.load(require_files=True)
    retained_generation = str(expected_retained_generation or "")
    if (
        manifest.state != "monolith"
        or manifest.canonical_source != "monolith"
        or manifest.raw.relative_path
        != manifest.operational.relative_path
        or manifest.rollback_generation_id != retained_generation
        or not retained_generation
    ):
        raise FinanceStoragePostManifestRecoveryError(
            "exact selected rollback monolith/retained generation binding "
            "is unavailable"
        )
    barrier = barrier_status(root)
    if (
        barrier.get("active") is not True
        or str(barrier.get("phase") or "") != "restoring"
        or barrier.get("hold_confirmed") is not True
    ):
        raise FinanceStoragePostManifestRecoveryError(
            "post-manifest readback requires the exact restoring barrier"
        )
    canonical_path = registry.resolve(
        "operational",
        manifest=manifest,
    )
    retained_root = root / "generations" / retained_generation
    retained_raw_path = retained_root / "finance_raw.sqlite3"
    retained_operational_path = retained_root / "operational.sqlite3"
    for path in (
        canonical_path,
        retained_raw_path,
        retained_operational_path,
    ):
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise FinanceStoragePostManifestRecoveryError(
                "post-manifest recovery path escapes runtime"
            ) from exc
        if not path.is_file():
            raise FinanceStoragePostManifestRecoveryError(
                f"post-manifest recovery file is missing: {path}"
            )

    canonical = _connect_readonly(canonical_path)
    retained_raw = _connect_readonly(retained_raw_path)
    retained_operational = _connect_readonly(
        retained_operational_path
    )
    try:
        canonical_raw = FinanceStorageShadowVerifier._rows_digest(
            canonical,
            table=LEGACY_RAW_TABLE,
        )
        split_raw = FinanceStorageShadowVerifier._rows_digest(
            retained_raw,
            table="finance_raw_current_rows",
        )
        if canonical_raw != split_raw:
            raise FinanceStoragePostManifestRecoveryError(
                "core Finance raw rows differ after manifest recovery"
            )

        excluded = (
            set(RAW_LEGACY_OBJECTS)
            | set(RAW_SCHEMA_TABLES)
            | {CACHE_TABLE}
        )
        canonical_tables = _tables(canonical) - excluded
        retained_tables = _tables(retained_operational) - excluded
        if canonical_tables != retained_tables:
            raise FinanceStoragePostManifestRecoveryError(
                "operational table inventory differs after manifest recovery"
            )
        operational: list[dict[str, Any]] = []
        for table in sorted(canonical_tables):
            source_digest = logical_table_digest(canonical, table)
            retained_digest = logical_table_digest(
                retained_operational,
                table,
            )
            if source_digest != retained_digest:
                raise FinanceStoragePostManifestRecoveryError(
                    "non-cache operational table differs after manifest "
                    f"recovery: {table}"
                )
            operational.append(
                {
                    "table": table,
                    "row_count": source_digest.row_count,
                    "logical_digest": source_digest.digest,
                }
            )

        canonical_cache = _cache_rows(canonical)
        retained_cache = _cache_rows(retained_operational)
        canonical_keys = set(canonical_cache)
        retained_keys = set(retained_cache)
        split_only = sorted(retained_keys - canonical_keys)
        canonical_only = sorted(canonical_keys - retained_keys)
        common = sorted(canonical_keys & retained_keys)
        semantic_mismatches = [
            key
            for key in common
            if canonical_cache[key]["semantic_digest"]
            != retained_cache[key]["semantic_digest"]
        ]
        cache_drift_count = (
            len(split_only)
            + len(canonical_only)
            + len(semantic_mismatches)
        )
        if cache_drift_count > MAX_RECOVERABLE_CACHE_ROWS:
            raise FinanceStoragePostManifestRecoveryError(
                "projection cache drift exceeds the bounded derived-row "
                "recovery contract: "
                + _canonical_json(
                    {
                        "canonical_only_count": len(canonical_only),
                        "retained_only_count": len(split_only),
                        "semantic_mismatch_count": len(
                            semantic_mismatches
                        ),
                        "maximum": MAX_RECOVERABLE_CACHE_ROWS,
                    }
                )
            )
        recoverable_rows: list[dict[str, Any]] = []
        for key in split_only:
            rebuilt = _rebuild_projection(canonical, key=key)
            rebuilt_digest = _digest(
                _normalized_projection(rebuilt)
            )
            if (
                rebuilt_digest
                != retained_cache[key]["semantic_digest"]
            ):
                raise FinanceStoragePostManifestRecoveryError(
                    "deterministic projection rebuild differs from the "
                    "retained cache row"
                )
            recoverable_rows.append(
                {
                    "seller_id": key[0],
                    "snapshot_digest": key[1],
                    "policy_revision": key[2],
                    "snapshot_date": key[3],
                    "semantic_digest": rebuilt_digest,
                    "recovery_owner": (
                        "wb-core-sheet-vitrina-refresh.service"
                    ),
                }
            )
        accepted_canonical_rows: list[dict[str, Any]] = []
        for classification, keys in (
            ("canonical_only", canonical_only),
            (
                "canonical_authoritative_semantic_mismatch",
                semantic_mismatches,
            ),
        ):
            for key in keys:
                rebuilt = _rebuild_projection(canonical, key=key)
                rebuilt_digest = _digest(
                    _normalized_projection(rebuilt)
                )
                if (
                    rebuilt_digest
                    != canonical_cache[key]["semantic_digest"]
                ):
                    raise FinanceStoragePostManifestRecoveryError(
                        "canonical projection cache row differs from the "
                        "deterministic rebuild: "
                        + _canonical_json(
                            {
                                "classification": classification,
                                "seller_id": key[0],
                                "snapshot_digest": key[1],
                                "policy_revision": key[2],
                                "snapshot_date": key[3],
                            }
                        )
                    )
                accepted_canonical_rows.append(
                    {
                        "classification": classification,
                        "seller_id": key[0],
                        "snapshot_digest": key[1],
                        "policy_revision": key[2],
                        "snapshot_date": key[3],
                        "semantic_digest": rebuilt_digest,
                        "canonical_rebuild_match": True,
                        "retained_generation_mutation_required": False,
                    }
                )
    except FinanceStorageMigrationError as exc:
        raise FinanceStoragePostManifestRecoveryError(str(exc)) from exc
    finally:
        canonical.close()
        retained_raw.close()
        retained_operational.close()

    payload: dict[str, Any] = {
        "contract_name": CONTRACT_NAME,
        "status": (
            "ready_for_repo_owned_refresh"
            if recoverable_rows
            else "reconciled"
        ),
        "query_only": True,
        "canonical_source": "monolith",
        "manifest_sha256": manifest.manifest_sha256,
        "canonical_generation": manifest.generation_epoch,
        "retained_split_generation": retained_generation,
        "raw": {
            "row_count": canonical_raw.row_count,
            "logical_digest": canonical_raw.digest,
            "match": True,
        },
        "operational": {
            "table_count": len(operational),
            "row_count": sum(
                int(item["row_count"]) for item in operational
            ),
            "logical_digest": _digest(operational),
            "non_cache_match": True,
        },
        "cache": {
            "canonical_rows": len(canonical_cache),
            "retained_rows": len(retained_cache),
            "bounded_drift_row_count": cache_drift_count,
            "recoverable_missing_canonical_rows": recoverable_rows,
            "accepted_canonical_rows": accepted_canonical_rows,
            "semantic_mismatch_count": len(semantic_mismatches),
            "regeneration_required": bool(recoverable_rows),
            "regeneration_command": (
                "business-data-maintenance durable restore of "
                "wb-core-sheet-vitrina-refresh.service"
            ),
            "direct_row_copy_allowed": False,
        },
        "retained_generation_mutation_count": 0,
        "canonical_mutation_count": 0,
        "retirement_authorized": False,
    }
    payload["fingerprint"] = _digest(payload)
    return payload
