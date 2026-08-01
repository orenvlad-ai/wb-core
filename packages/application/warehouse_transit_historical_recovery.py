"""Query-only submanifest for lost Seller Portal transit facts.

The current production evidence contains a newer successful fact revision than
the previously audited total.  Consequently this submanifest is deliberately
non-applicable: it proves the delta and keeps recovery unavailable until a live
Seller Portal success or separately approved exact source revision exists.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import hashlib
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Iterator, Mapping, Sequence

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_historical_recovery import (
    _connect,
    _fingerprint,
)
from packages.application.warehouse_functional import _supply_business_date
from packages.application.warehouse_stocks import _normalized_wb_record


CONTRACT_NAME = "warehouse_transit_historical_recovery_2026_07_v1"
TARGET_SUPPLY_IDS = (
    "40421940",
    "40422317",
    "40433285",
    "40433397",
    "40557711",
    "40559839",
    "40561872",
    "40562177",
)
EXPECTED_DIAGNOSTIC_TOTAL = Decimal("212369.16")
EXPECTED_DIAGNOSTIC_ACCEPTED_CAPITAL = Decimal("212146.83")
EXPECTED_DIAGNOSTIC_AMOUNTS = {
    "40421940": Decimal("45846.09"),
    "40422317": Decimal("10931.30"),
    "40433285": Decimal("6890.81"),
    "40433397": Decimal("43654.13"),
    "40557711": Decimal("6274.96"),
    "40559839": Decimal("27019.81"),
    "40561872": Decimal("48510.00"),
    "40562177": Decimal("23242.06"),
}
EXPECTED_LAYER_ROW_COUNT = 126
EXPECTED_UNION_NM_ID_COUNT = 28
ZERO = Decimal("0")


class WarehouseTransitHistoricalRecoveryError(RuntimeError):
    """Fail-closed historical transit evidence violation."""


def build_transit_historical_recovery_plan(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    backup_path: Path,
    expected_total: Decimal = EXPECTED_DIAGNOSTIC_TOTAL,
) -> dict[str, Any]:
    source_path = _validated_backup_path(runtime, backup_path)
    source_file_digest = _sha256_file(source_path)
    with _readable_sqlite(source_path) as sqlite_path:
        backup_rows = _backup_rows(sqlite_path)
    _validate_backup_rows(backup_rows)
    with _connect(runtime.db_path, read_only=True) as conn:
        current_rows = _current_enrichment_rows(conn)
        layer_rows = _target_layer_rows(conn)
        supplies = _target_supply_rows(conn)
        queue_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue
                WHERE stable_source_id IN (?,?,?,?,?,?,?,?)
                ORDER BY stable_source_id,queue_id
                """,
                tuple(f"wb_transit_cost:{value}" for value in TARGET_SUPPLY_IDS),
            ).fetchall()
        ]
        non_target_digest = _non_target_digest(conn)
    current_by_id = {str(row["supply_id"]): row for row in current_rows}
    if set(current_by_id) != set(TARGET_SUPPLY_IDS):
        raise WarehouseTransitHistoricalRecoveryError(
            "current target enrichment identity closure drifted"
        )
    if any(
        str(row.get("status") or "") != "session_expired"
        or row.get("amount") is not None
        for row in current_rows
    ):
        raise WarehouseTransitHistoricalRecoveryError(
            "one or more target transit facts are no longer missing/session_expired"
        )
    if len(layer_rows) != EXPECTED_LAYER_ROW_COUNT:
        raise WarehouseTransitHistoricalRecoveryError(
            f"target cost-layer closure is {len(layer_rows)}, expected 126"
        )
    union_nm_ids = sorted({int(row["nm_id"]) for row in layer_rows})
    if len(union_nm_ids) != EXPECTED_UNION_NM_ID_COUNT:
        raise WarehouseTransitHistoricalRecoveryError(
            "target cost-layer SKU union is no longer exactly 28"
        )
    if any(
        str(row.get("source_status") or "") != "pending"
        or row.get("our_wb_unit_cost_rub") is not None
        or _decimal(row.get("transit_amount_total")) != ZERO
        for row in layer_rows
    ):
        raise WarehouseTransitHistoricalRecoveryError(
            "target current cost layers are no longer uniformly pending/null-transit"
        )
    backup_by_id = {str(row["supply_id"]): row for row in backup_rows}
    layer_by_supply = {
        supply_id: [
            row
            for row in layer_rows
            if str(row["wb_supply_id"]) == supply_id
        ]
        for supply_id in TARGET_SUPPLY_IDS
    }
    per_supply: list[dict[str, Any]] = []
    total = ZERO
    accepted_capital = ZERO
    for supply_id in TARGET_SUPPLY_IDS:
        source = backup_by_id[supply_id]
        layers = layer_by_supply[supply_id]
        denominators = {_decimal(row["qty_denominator"]) for row in layers}
        if len(denominators) != 1 or next(iter(denominators)) <= ZERO:
            raise WarehouseTransitHistoricalRecoveryError(
                f"transit denominator is not one positive packed total: {supply_id}"
            )
        denominator = next(iter(denominators))
        accepted_quantity = sum(
            (_decimal(row["accepted_qty"]) for row in layers), ZERO
        )
        amount = _decimal(source["amount"])
        diagnostic_amount = EXPECTED_DIAGNOSTIC_AMOUNTS[supply_id]
        accepted = amount * accepted_quantity / denominator
        total += amount
        accepted_capital += accepted
        supply = supplies.get(supply_id)
        if supply is None:
            raise WarehouseTransitHistoricalRecoveryError(
                f"current WB supply record is missing: {supply_id}"
            )
        per_supply.append(
            {
                "supply_id": supply_id,
                "amount_rub": str(amount),
                "diagnostic_control_amount_rub": str(diagnostic_amount),
                "amount_drift_rub": str(amount - diagnostic_amount),
                "fetched_at": str(source["fetched_at"]),
                "source": str(source["source"]),
                "evidence_type": str(source["evidence_type"]),
                "source_endpoint_path": str(source["source_endpoint_path"]),
                "packed_denominator": str(denominator),
                "accepted_quantity": str(accepted_quantity),
                "accepted_capital_rub": str(accepted),
                "cost_layer_row_count": len(layers),
                "nm_ids": sorted({int(row["nm_id"]) for row in layers}),
                "originating_business_date": _originating_business_date(supply),
                "source_revision": _fact_revision(source),
            }
        )
    drift = total - expected_total
    accepted_capital_drift = (
        accepted_capital - EXPECTED_DIAGNOSTIC_ACCEPTED_CAPITAL
    )
    matches_diagnostic_control = bool(
        drift == ZERO
        and accepted_capital.quantize(Decimal("0.01"))
        == EXPECTED_DIAGNOSTIC_ACCEPTED_CAPITAL
        and all(
            _decimal(item["amount_drift_rub"]) == ZERO
            for item in per_supply
        )
    )
    # Migration 127 deliberately ships no backup-to-production mutation.  A
    # matching backup is review evidence only; canonical ingestion owns facts.
    apply_eligible = False
    manifest = {
        "contract_name": CONTRACT_NAME,
        "scope": {
            "supply_ids": list(TARGET_SUPPLY_IDS),
            "nm_ids": union_nm_ids,
            "cost_layer_row_count": len(layer_rows),
        },
        "backup": {
            "path": str(source_path),
            "file_sha256": source_file_digest,
            "row_count": len(backup_rows),
            "latest_fetched_at": max(str(row["fetched_at"]) for row in backup_rows),
        },
        "facts": per_supply,
        "backup_total_rub": str(total),
        "diagnostic_control_total_rub": str(expected_total),
        "total_drift_rub": str(drift),
        "accepted_capital_rub": str(accepted_capital),
        "diagnostic_control_accepted_capital_rub": str(
            EXPECTED_DIAGNOSTIC_ACCEPTED_CAPITAL
        ),
        "accepted_capital_drift_rub": str(accepted_capital_drift),
        "matches_diagnostic_control": matches_diagnostic_control,
        "current_target_digest": _fingerprint(
            {
                "enrichment": current_rows,
                "layers": layer_rows,
                "queue": queue_rows,
            }
        ),
        "non_target_digest": non_target_digest,
        "source_digest": _fingerprint(
            {
                "backup_sha256": source_file_digest,
                "facts": per_supply,
                "current_supplies": supplies,
            }
        ),
        "apply_eligible": apply_eligible,
        "unavailable_reason": (
            ""
            if matches_diagnostic_control
            else (
                "exact backup fact revision differs from the approved "
                "diagnostic control; production restore remains fail-closed"
            )
        ),
        "mutation_unavailable_reason": (
            "backup-to-production mutation is not implemented; use canonical "
            "Seller Portal ingestion after a successful login"
        ),
        "recovery": {
            "tier_if_applicable": "T1",
            "primary_physical_rows_changed": 0,
            "global_rebuild": False,
            "finance_raw_rows_read": 0,
        },
        "second_run_criterion": {
            "tier": "T0",
            "fact_revisions": 0,
            "cost_layer_revisions": 0,
            "queue_mutations": 0,
            "physical_movements": 0,
        },
    }
    fingerprint = _fingerprint(manifest)
    return {
        **manifest,
        "fingerprint": fingerprint,
        "mode": "dry_run",
        "would_change": False,
        "expected": {
            "supply_count": len(TARGET_SUPPLY_IDS),
            "union_nm_id_count": len(union_nm_ids),
            "cost_layer_row_count": len(layer_rows),
            "targeted_replay_count": len(TARGET_SUPPLY_IDS),
            "physical_movements": 0,
        },
    }


def apply_transit_historical_recovery_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    confirm_fingerprint: str,
    approval_reference: str,
) -> dict[str, Any]:
    del runtime, approval_reference
    if str(plan.get("fingerprint") or "") != str(confirm_fingerprint or ""):
        raise WarehouseTransitHistoricalRecoveryError(
            "apply requires the exact current transit submanifest fingerprint"
        )
    if not bool(plan.get("apply_eligible")):
        raise WarehouseTransitHistoricalRecoveryError(
            "transit backup restore is unavailable: "
            + str(plan.get("unavailable_reason") or "source evidence drift")
        )
    raise WarehouseTransitHistoricalRecoveryError(
        "backup apply is intentionally disabled; use a fresh Seller Portal "
        "success so canonical ingestion versions the fact and enqueues replay"
    )


def public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in plan.items()
        if not str(key).startswith("_")
    }


def _validated_backup_path(
    runtime: RegistryUploadDbBackedRuntime,
    backup_path: Path,
) -> Path:
    path = Path(backup_path).resolve()
    roots = (
        (runtime.runtime_dir / "backups").resolve(),
        (runtime.runtime_dir.parent / "backups").resolve(),
    )
    if not path.is_file() or path.suffix not in {".sqlite3", ".zst"}:
        raise WarehouseTransitHistoricalRecoveryError(
            "transit evidence backup is missing or has an unsupported suffix"
        )
    if not any(path == root or root in path.parents for root in roots):
        raise WarehouseTransitHistoricalRecoveryError(
            "transit evidence backup is outside canonical backup roots"
        )
    return path


@contextmanager
def _readable_sqlite(path: Path) -> Iterator[Path]:
    if path.suffix != ".zst":
        yield path
        return
    with TemporaryDirectory(prefix="wb-transit-evidence-") as temporary:
        target = Path(temporary) / "backup.sqlite3"
        with target.open("wb") as output:
            completed = subprocess.run(
                ["zstd", "-q", "-d", "-c", str(path)],
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
            )
        if completed.returncode != 0:
            raise WarehouseTransitHistoricalRecoveryError(
                "transit evidence backup decompression failed"
            )
        yield target


def _backup_rows(path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=300)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        placeholders = ",".join("?" for _ in TARGET_SUPPLY_IDS)
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM "
                "sheet_vitrina_v1_wb_supply_transit_cost_enrichment "
                f"WHERE supply_id IN ({placeholders}) ORDER BY supply_id",
                TARGET_SUPPLY_IDS,
            ).fetchall()
        ]
    finally:
        conn.close()


def _validate_backup_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    if {str(row.get("supply_id") or "") for row in rows} != set(
        TARGET_SUPPLY_IDS
    ):
        raise WarehouseTransitHistoricalRecoveryError(
            "backup target supply identity closure drifted"
        )
    for row in rows:
        if (
            str(row.get("status") or "") != "success"
            or _decimal(row.get("amount")) <= ZERO
            or str(row.get("currency") or "") != "RUB"
            or int(row.get("is_transit") or 0) != 1
            or str(row.get("source") or "") != "seller_portal_browser"
            or str(row.get("evidence_type") or "") != "network_json"
            or not str(row.get("fetched_at") or "")
        ):
            raise WarehouseTransitHistoricalRecoveryError(
                "backup transit fact is not a confirmed positive RUB success"
            )


def _current_enrichment_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in TARGET_SUPPLY_IDS)
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_wb_supply_transit_cost_enrichment "
            f"WHERE supply_id IN ({placeholders}) ORDER BY supply_id",
            TARGET_SUPPLY_IDS,
        ).fetchall()
    ]


def _target_layer_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in TARGET_SUPPLY_IDS)
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers "
            f"WHERE is_current=1 AND wb_supply_id IN ({placeholders}) "
            "ORDER BY wb_supply_id,nm_id",
            TARGET_SUPPLY_IDS,
        ).fetchall()
    ]


def _target_supply_rows(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    placeholders = ",".join("?" for _ in TARGET_SUPPLY_IDS)
    return {
        str(row["supply_id"]): dict(row)
        for row in conn.execute(
            "SELECT supply_id,raw_goods_hash,raw_detail_hash,raw_list_hash,"
            "fact_date,supply_date,updated_date,source_created_at "
            "FROM sheet_vitrina_v1_wb_supplies "
            f"WHERE supply_id IN ({placeholders}) ORDER BY supply_id",
            TARGET_SUPPLY_IDS,
        ).fetchall()
    }


def _originating_business_date(row: Mapping[str, Any]) -> str:
    normalized = _normalized_wb_record(row)
    business_date = _supply_business_date(normalized, row)
    if business_date:
        return business_date
    for key in ("supply_date", "fact_date", "updated_date", "source_created_at"):
        value = str(normalized.get(key) or row.get(key) or "")[:10]
        if value:
            return value
    raise WarehouseTransitHistoricalRecoveryError(
        "target supply has no originating business date"
    )


def _fact_revision(row: Mapping[str, Any]) -> str:
    material = {
        key: row.get(key)
        for key in (
            "supply_id",
            "amount",
            "currency",
            "is_transit",
            "source",
            "evidence_type",
            "confidence",
            "source_endpoint_path",
        )
    }
    return _fingerprint(material)


def _non_target_digest(conn: sqlite3.Connection) -> str:
    placeholders = ",".join("?" for _ in TARGET_SUPPLY_IDS)
    return _fingerprint(
        {
            "non_target_enrichment": [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM "
                    "sheet_vitrina_v1_wb_supply_transit_cost_enrichment "
                    f"WHERE supply_id NOT IN ({placeholders}) ORDER BY supply_id",
                    TARGET_SUPPLY_IDS,
                ).fetchall()
            ],
            "daily_wac": [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost "
                    "ORDER BY as_of_date,nm_id"
                ).fetchall()
            ],
            "physical_events": [
                dict(row)
                for row in conn.execute(
                    "SELECT event_id,event_type,source_id,source_fingerprint,"
                    "business_date,nm_id,quantity,capital_rub "
                    "FROM sheet_vitrina_v1_warehouse_functional_events "
                    "ORDER BY event_id"
                ).fetchall()
            ],
        }
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value).replace(",", "."))
