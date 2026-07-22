"""Versioned operator calculation parameters and Decimal Proxy 3 semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.warehouse_sync_lock import warehouse_sync_lock
from packages.business_time import current_business_date_iso


PROXY_BLOCK_KEY = "proxy_profit_margin"
INITIAL_EFFECTIVE_DATE = "2026-07-01"
INITIAL_VERSION_ID = "calculation_parameters_proxy_v1_20260701"
FUNCTIONAL_ECONOMICS_ARCHIVE_RETENTION_COUNT = 3
FUNCTIONAL_ECONOMICS_ARCHIVE_PRUNE_LIMIT = 2

RATE_FIELDS: tuple[str, ...] = (
    "tax_rate",
    "wb_agent_and_other_rate",
    "acquiring_rate",
    "wb_logistics_rate",
    "wb_storage_rate",
    "penalties_adjustments_rate",
    "other_expense_rate",
)

RATE_LABELS_RU = {
    "tax_rate": "Налог",
    "wb_agent_and_other_rate": "Агентское вознаграждение WB и прочие расходы",
    "acquiring_rate": "Эквайринг",
    "wb_logistics_rate": "Логистика WB до покупателя",
    "wb_storage_rate": "Хранение WB",
    "penalties_adjustments_rate": "Штрафы/корректировки",
    "other_expense_rate": "Другие расходы",
}


@dataclass(frozen=True)
class ProxyParameters:
    effective_date: str
    buyout_rate: Decimal
    tax_rate: Decimal
    wb_agent_and_other_rate: Decimal
    acquiring_rate: Decimal
    wb_logistics_rate: Decimal
    wb_storage_rate: Decimal
    penalties_adjustments_rate: Decimal
    other_expense_rate: Decimal
    version_id: str = ""
    fingerprint: str = ""

    @property
    def included_expense_rate(self) -> Decimal:
        return sum((getattr(self, field) for field in RATE_FIELDS), Decimal("0"))

    @property
    def retained_share(self) -> Decimal:
        return Decimal("1") - self.included_expense_rate

    def public(self) -> dict[str, Any]:
        rates = {field: _text(getattr(self, field)) for field in RATE_FIELDS}
        return {
            "version_id": self.version_id,
            "effective_date": self.effective_date,
            "buyout_rate": _text(self.buyout_rate),
            **rates,
            "included_expense_rate": _text(self.included_expense_rate),
            "retained_share": _text(self.retained_share),
            "buyout_rate_pct": _text(self.buyout_rate * Decimal("100")),
            "included_expense_rate_pct": _text(self.included_expense_rate * Decimal("100")),
            "retained_share_pct": _text(self.retained_share * Decimal("100")),
            "fingerprint": self.fingerprint,
        }


DEFAULT_PROXY_PARAMETERS = ProxyParameters(
    effective_date=INITIAL_EFFECTIVE_DATE,
    buyout_rate=Decimal("0.91"),
    tax_rate=Decimal("0.06"),
    wb_agent_and_other_rate=Decimal("0.38"),
    acquiring_rate=Decimal("0"),
    wb_logistics_rate=Decimal("0"),
    wb_storage_rate=Decimal("0"),
    penalties_adjustments_rate=Decimal("0"),
    other_expense_rate=Decimal("0"),
    version_id=INITIAL_VERSION_ID,
)


def calculate_proxy_3(
    *,
    order_sum: Any,
    order_count: Any,
    canonical_wb_wac: Any,
    ads_sum: Any,
    parameters: ProxyParameters,
) -> dict[str, Decimal | None]:
    """Calculate one SKU without converting a missing operand into zero."""

    operands = {
        "order_sum": _optional_decimal(order_sum),
        "order_count": _optional_decimal(order_count),
        "canonical_wb_wac": _optional_decimal(canonical_wb_wac),
        "ads_sum": _optional_decimal(ads_sum),
    }
    if any(value is None for value in operands.values()):
        return {
            "expected_buyout_revenue": None,
            "expected_buyout_qty": None,
            "included_expense_rate": parameters.included_expense_rate,
            "proxy_profit_3": None,
            "proxy_margin_3": None,
        }
    expected_revenue = operands["order_sum"] * parameters.buyout_rate  # type: ignore[operator]
    expected_qty = operands["order_count"] * parameters.buyout_rate  # type: ignore[operator]
    profit = (
        expected_revenue * parameters.retained_share
        - expected_qty * operands["canonical_wb_wac"]  # type: ignore[operator]
        - operands["ads_sum"]  # type: ignore[operator]
    )
    return {
        "expected_buyout_revenue": expected_revenue,
        "expected_buyout_qty": expected_qty,
        "included_expense_rate": parameters.included_expense_rate,
        "proxy_profit_3": profit,
        "proxy_margin_3": None if expected_revenue == 0 else profit / expected_revenue,
    }


def aggregate_proxy_3(rows: list[Mapping[str, Any]]) -> dict[str, Decimal | None]:
    """TOTAL is a sum of SKU profits divided by summed expected revenue."""

    profits = [_optional_decimal(row.get("proxy_profit_3")) for row in rows]
    revenues = [_optional_decimal(row.get("expected_buyout_revenue")) for row in rows]
    if not rows or any(value is None for value in profits + revenues):
        return {"proxy_profit_3": None, "expected_buyout_revenue": None, "proxy_margin_3": None}
    profit = sum((value for value in profits if value is not None), Decimal("0"))
    revenue = sum((value for value in revenues if value is not None), Decimal("0"))
    return {
        "proxy_profit_3": profit,
        "expected_buyout_revenue": revenue,
        "proxy_margin_3": None if revenue == 0 else profit / revenue,
    }


class CalculationParametersBlock:
    def __init__(self, *, runtime: RegistryUploadDbBackedRuntime) -> None:
        self.runtime = runtime
        self.runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.runtime.db_path) as conn:
            ensure_calculation_parameters_schema(conn)
            conn.commit()

    def ensure_initial_version(
        self,
        *,
        connection: sqlite3.Connection | None = None,
        created_by: str = "warehouse_functional_cutover",
        created_at: str | None = None,
    ) -> dict[str, Any]:
        own = connection is None
        conn = connection or _connect(self.runtime.db_path)
        try:
            # A caller-supplied connection may already be inside the guarded
            # functional cutover transaction.  ``executescript`` implicitly
            # commits in sqlite3, so schema DDL is permitted only on the
            # independently owned connection.  Constructors establish the
            # schema before any apply transaction begins.
            if own:
                ensure_calculation_parameters_schema(conn)
            existing = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions WHERE version_id=?",
                (INITIAL_VERSION_ID,),
            ).fetchone()
            if existing is not None:
                return {**_version_row(existing), "idempotent": True}
            now = created_at or _now()
            payload = DEFAULT_PROXY_PARAMETERS.public()
            fingerprint = _settings_fingerprint(payload)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_calculation_parameter_versions(
                    version_id,block_key,revision,effective_date,rates_json,fingerprint,
                    source,created_by,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    INITIAL_VERSION_ID,
                    PROXY_BLOCK_KEY,
                    1,
                    INITIAL_EFFECTIVE_DATE,
                    _json(payload),
                    fingerprint,
                    "functional_cutover_initial_version",
                    created_by,
                    now,
                ),
            )
            if own:
                conn.commit()
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions WHERE version_id=?",
                (INITIAL_VERSION_ID,),
            ).fetchone()
            return {**_version_row(row), "idempotent": False}
        finally:
            if own:
                conn.close()

    def parameters_for_date(self, effective_date: str) -> ProxyParameters:
        target = date.fromisoformat(str(effective_date)[:10]).isoformat()
        with _connect(self.runtime.db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions
                WHERE block_key=? AND effective_date<=?
                ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1
                """,
                (PROXY_BLOCK_KEY, target),
            ).fetchone()
        if row is None:
            return DEFAULT_PROXY_PARAMETERS
        return _parameters_from_row(row)

    def preview_version(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        parameters = _parameters_from_payload(payload)
        current = self.parameters_for_date(parameters.effective_date)
        normalized = parameters.public()
        fingerprint = _settings_fingerprint(normalized)
        diff = []
        for field in ("buyout_rate", *RATE_FIELDS):
            before = getattr(current, field)
            after = getattr(parameters, field)
            if before != after:
                diff.append(
                    {
                        "field": field,
                        "label": "Коэффициент выкупа" if field == "buyout_rate" else RATE_LABELS_RU[field],
                        "before_pct": _text(before * Decimal("100")),
                        "after_pct": _text(after * Decimal("100")),
                    }
                )
        return {
            "status": "preview_ready",
            "parameters": normalized,
            "diff": diff,
            "preview_fingerprint": fingerprint,
            "formula_preview": (
                "orderSum × buyout_rate × retained_share − "
                "orderCount × buyout_rate × canonical_WB_WAC − ads_sum"
            ),
        }

    def create_version(
        self,
        payload: Mapping[str, Any],
        *,
        preview_fingerprint: str,
        created_by: str,
    ) -> dict[str, Any]:
        with warehouse_sync_lock(self.runtime.runtime_dir, blocking=False):
            return self._create_version_locked(
                payload,
                preview_fingerprint=preview_fingerprint,
                created_by=created_by,
            )

    def _create_version_locked(
        self,
        payload: Mapping[str, Any],
        *,
        preview_fingerprint: str,
        created_by: str,
    ) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as preflight_conn:
            initial = preflight_conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_calculation_parameter_versions WHERE version_id=?",
                (INITIAL_VERSION_ID,),
            ).fetchone()
        if initial is None:
            raise ValueError(
                "calculation parameters cannot be saved before the functional cutover initial version"
            )
        preview = self.preview_version(payload)
        if preview["preview_fingerprint"] != str(preview_fingerprint or ""):
            raise ValueError("calculation parameters changed after preview")
        parameters = _parameters_from_payload(payload)
        economics_backup = self.prepare_operator_settings_backup(
            preview_fingerprint=str(preview["preview_fingerprint"]),
        )
        now = _now()
        try:
            with _connect(self.runtime.db_path) as conn:
                ensure_calculation_parameters_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                revision = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(revision),0)+1 FROM sheet_vitrina_v1_calculation_parameter_versions WHERE block_key=?",
                        (PROXY_BLOCK_KEY,),
                    ).fetchone()[0]
                )
                version_id = f"calculation_parameters_proxy_v{revision}_{parameters.effective_date.replace('-', '')}"
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_calculation_parameter_versions(
                        version_id,block_key,revision,effective_date,rates_json,fingerprint,
                        source,created_by,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        version_id,
                        PROXY_BLOCK_KEY,
                        revision,
                        parameters.effective_date,
                        _json(parameters.public()),
                        preview["preview_fingerprint"],
                        "operator_version",
                        created_by,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_proxy_targeted_recalc_queue(
                        request_id,effective_date,settings_version_id,status,created_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (f"proxy_recalc:{version_id}", parameters.effective_date, version_id, "pending", now),
                )
                conn.commit()
        except Exception:
            self._archive_functional_economics_backup(economics_backup)
            raise
        recalculation = self.process_pending_targeted_recalculations(
            verified_backup=economics_backup,
        )
        return {
            **self.get_payload(),
            "created_version_id": version_id,
            "diff": preview["diff"],
            "targeted_recalculation": recalculation,
        }

    def process_pending_targeted_recalculations(
        self,
        *,
        verified_backup: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as conn:
            ensure_calculation_parameters_schema(conn)
            pending = [dict(row) for row in conn.execute(
                """SELECT * FROM sheet_vitrina_v1_proxy_targeted_recalc_queue
                   WHERE status IN ('pending','failed') ORDER BY effective_date,created_at,request_id"""
            ).fetchall()]
        if not pending:
            return {"status": "idle", "request_count": 0}
        request_ids = [str(item["request_id"]) for item in pending]
        try:
            effective_backup = verified_backup or self.prepare_functional_economics_backup()
            result = self.publish_current_functional_economics(
                verified_backup=effective_backup,
            )
        except Exception as exc:
            backup_archive: dict[str, Any] | None = None
            archive_error = ""
            if "effective_backup" in locals():
                try:
                    backup_archive = self._archive_functional_economics_backup(
                        effective_backup,
                    )
                except Exception as archive_exc:  # preserve the raw restore point fail-closed
                    archive_error = str(archive_exc)
            with _connect(self.runtime.db_path) as conn:
                placeholders = ",".join("?" for _ in request_ids)
                conn.execute(
                    f"""UPDATE sheet_vitrina_v1_proxy_targeted_recalc_queue
                        SET status='failed',error=? WHERE request_id IN ({placeholders})""",
                    (str(exc), *request_ids),
                )
                conn.commit()
            return {
                "status": "failed",
                "request_count": len(request_ids),
                "error": str(exc),
                "backup_archive": backup_archive,
                "backup_archive_error": archive_error,
            }
        completed_at = _now()
        with _connect(self.runtime.db_path) as conn:
            placeholders = ",".join("?" for _ in request_ids)
            conn.execute(
                f"""UPDATE sheet_vitrina_v1_proxy_targeted_recalc_queue
                    SET status='complete',completed_at=?,error=NULL
                    WHERE request_id IN ({placeholders})""",
                (completed_at, *request_ids),
            )
            conn.commit()
        return {
            "status": "complete",
            "request_count": len(request_ids),
            "plan_fingerprint": result["plan_fingerprint"],
            "changed_snapshot_count": int(result.get("changed_snapshot_count") or 0),
            "database_written": bool(result.get("database_written")),
            "backup_archive": result.get("backup_archive"),
        }

    def prepare_functional_economics_backup(self) -> dict[str, Any]:
        """Prepare or reuse one coherent restore point per open business day.

        Hourly publications change only reconstructable derived cells.  A single
        pre-first-mutation restore point is therefore retained for that business
        day instead of writing a new full-database copy on every hourly refresh.
        """

        business_date = current_business_date_iso()
        backup_root = (self.runtime.runtime_dir / "backups" / "calculation-parameters").resolve()
        backup_root.mkdir(parents=True, exist_ok=True)
        retention = self._prune_verified_functional_economics_archives(backup_root)
        source = backup_root / f"functional-economics-daily-{business_date.replace('-', '')}.sqlite3"
        archive = Path(str(source) + ".zst")
        manifest_path = archive.with_name(archive.name + ".manifest.json")
        raw_manifest_path = source.with_name(source.name + ".manifest.json")
        if archive.is_file() and manifest_path.is_file():
            from apps.sqlite_backup_archive import verify_archive_manifest

            manifest = verify_archive_manifest(manifest_path)
            if (
                str(manifest.get("source_path") or "") != str(source)
                or str(manifest.get("archive_path") or "") != str(archive)
            ):
                raise ValueError("daily functional economics backup archive failed provenance validation")
            self._require_economics_backup_capacity(
                backup_root,
                source_size=int(manifest["source_size_bytes"]),
                raw_backup_exists=False,
                archive_exists=True,
            )
            return {
                **manifest,
                "integrity_check": "ok",
                "backup_scope": "business_day",
                "business_date": business_date,
                "reused": True,
                "retention": retention,
            }
        if archive.exists() or manifest_path.exists():
            raise ValueError("daily functional economics backup archive is incomplete")
        if source.exists():
            plan = _verify_daily_raw_backup_manifest(
                source=source,
                manifest_path=raw_manifest_path,
                business_date=business_date,
            )
            self._require_economics_backup_capacity(
                backup_root,
                source_size=int(plan["source_size_bytes"]),
                raw_backup_exists=True,
            )
            return {
                "path": str(source),
                "size_bytes": int(plan["source_size_bytes"]),
                "sha256": str(plan["source_sha256"]).removeprefix("sha256:"),
                "integrity_check": str(plan["source_integrity_check"]),
                "backup_scope": "business_day",
                "business_date": business_date,
                "raw_manifest_path": str(raw_manifest_path),
                "reused": True,
                "retention": retention,
            }
        if raw_manifest_path.exists():
            raise ValueError("daily functional economics raw backup manifest is incomplete")
        self._require_economics_backup_capacity(
            backup_root,
            source_size=self.runtime.coherent_backup_size_bytes(),
            raw_backup_exists=False,
        )
        backup = self.runtime.backup_database(source)
        source.chmod(0o600)
        try:
            _write_daily_raw_backup_manifest(
                source=source,
                manifest_path=raw_manifest_path,
                business_date=business_date,
                backup=backup,
            )
        except Exception:
            source.unlink(missing_ok=True)
            raw_manifest_path.unlink(missing_ok=True)
            raise
        return {
            **backup,
            "backup_scope": "business_day",
            "business_date": business_date,
            "raw_manifest_path": str(raw_manifest_path),
            "reused": False,
            "retention": retention,
        }

    def prepare_operator_settings_backup(
        self,
        *,
        preview_fingerprint: str,
    ) -> dict[str, Any]:
        """Create a fresh recovery point before an operator-authored settings write."""

        backup_root = (self.runtime.runtime_dir / "backups" / "calculation-parameters").resolve()
        backup_root.mkdir(parents=True, exist_ok=True)
        self._prune_verified_functional_economics_archives(backup_root)
        coherent_size = self.runtime.coherent_backup_size_bytes()
        self._require_economics_backup_capacity(
            backup_root,
            source_size=coherent_size,
            raw_backup_exists=False,
        )
        timestamp = _now().replace(":", "").replace("-", "")
        source = backup_root / (
            f"operator-settings-{timestamp}-{uuid4().hex}.sqlite3"
        )
        backup = self.runtime.backup_database(source)
        source.chmod(0o600)
        raw_manifest_path = source.with_name(source.name + ".manifest.json")
        try:
            _write_operator_settings_raw_backup_manifest(
                source=source,
                manifest_path=raw_manifest_path,
                preview_fingerprint=str(preview_fingerprint),
                backup=backup,
            )
        except Exception:
            source.unlink(missing_ok=True)
            raw_manifest_path.unlink(missing_ok=True)
            _fsync_directory(backup_root)
            raise
        return {
            **backup,
            "backup_scope": "fresh_operator_settings",
            "settings_preview_fingerprint": str(preview_fingerprint),
            "raw_manifest_path": str(raw_manifest_path),
            "reused": False,
        }

    def _require_economics_backup_capacity(
        self,
        backup_root: Path,
        *,
        source_size: int,
        raw_backup_exists: bool,
        archive_exists: bool = False,
    ) -> dict[str, Any]:
        margin = max(256 * 1024 * 1024, source_size // 20)
        current_runtime_size = self.runtime.coherent_backup_size_bytes()
        pipeline_write_margin = max(
            4 * 1024 * 1024 * 1024,
            current_runtime_size // 3,
        )
        raw_bytes = 0 if raw_backup_exists or archive_exists else source_size
        archive_worst_case_bytes = 0 if archive_exists else source_size + margin
        backup_required = raw_bytes + archive_worst_case_bytes
        runtime_root = self.runtime.db_path.parent.resolve()
        backup_available = shutil.disk_usage(backup_root).free
        runtime_available = shutil.disk_usage(runtime_root).free
        same_filesystem = _same_filesystem(backup_root, runtime_root)
        if same_filesystem:
            required = backup_required + pipeline_write_margin
            if backup_available < required:
                raise ValueError(
                    "insufficient filesystem capacity for coherent daily backup and lossless archive: "
                    f"required_free_bytes={required}, available_free_bytes={backup_available}"
                )
        elif backup_available < backup_required:
            raise ValueError(
                "insufficient backup-filesystem capacity for coherent daily backup and lossless archive: "
                f"required_free_bytes={backup_required}, available_free_bytes={backup_available}"
            )
        elif runtime_available < pipeline_write_margin:
            raise ValueError(
                "insufficient runtime-filesystem capacity for bounded warehouse publication: "
                f"required_free_bytes={pipeline_write_margin}, available_free_bytes={runtime_available}"
            )
        return {
            "required_free_bytes": (
                backup_required + pipeline_write_margin
                if same_filesystem
                else backup_required
            ),
            "available_free_bytes": backup_available,
            "backup_required_free_bytes": backup_required,
            "backup_available_free_bytes": backup_available,
            "runtime_required_free_bytes": pipeline_write_margin,
            "runtime_available_free_bytes": runtime_available,
            "pipeline_write_margin_bytes": pipeline_write_margin,
            "current_runtime_coherent_size_bytes": current_runtime_size,
            "same_filesystem": same_filesystem,
        }

    def preflight_fresh_economics_backup_capacity(self, backup_root: Path) -> dict[str, Any]:
        backup_root = backup_root.resolve()
        backup_root.mkdir(parents=True, exist_ok=True)
        retention = self._prune_verified_functional_economics_archives(backup_root)
        return {
            **self._require_economics_backup_capacity(
                backup_root,
                source_size=self.runtime.coherent_backup_size_bytes(),
                raw_backup_exists=False,
            ),
            "retention": retention,
        }

    def publish_current_functional_economics(
        self,
        *,
        verified_backup: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish only WB cost/Proxy target cells from the active functional state."""

        from packages.application.warehouse_functional_economics_backfill import (
            apply_functional_economics_backfill_plan,
            build_functional_economics_backfill_plan,
        )

        effective_backup = verified_backup or self.prepare_functional_economics_backup()
        plan = build_functional_economics_backfill_plan(self.runtime)
        result = apply_functional_economics_backfill_plan(
            self.runtime,
            plan,
            confirm_fingerprint=str(plan["plan_fingerprint"]),
            backup_dir=(self.runtime.runtime_dir / "backups" / "calculation-parameters").resolve(),
            verified_backup=effective_backup,
        )
        backup = dict(result.get("backup") or effective_backup or {})
        archive_evidence = self._archive_functional_economics_backup(backup)
        result["backup_archive"] = archive_evidence
        result["backup"] = archive_evidence
        return result

    def _archive_functional_economics_backup(
        self,
        backup: Mapping[str, Any],
    ) -> dict[str, Any]:
        from apps.sqlite_backup_archive import (
            apply_archive,
            build_plan,
            verify_archive_manifest,
        )

        raw_path = Path(str(backup.get("path") or "")) if backup.get("path") else None
        if raw_path is not None and raw_path.is_file():
            archive_plan = build_plan(source=raw_path)
            expected_sha = str(backup.get("sha256") or "")
            if (
                expected_sha
                and str(archive_plan["source_sha256"]).removeprefix("sha256:")
                != expected_sha.removeprefix("sha256:")
            ):
                raise ValueError(
                    "functional economics backup changed before lossless archive"
                )
            archived = apply_archive(
                source=raw_path,
                archive=None,
                fingerprint=str(archive_plan["fingerprint"]),
            )
            manifest_path = Path(str(archived.get("manifest_path") or ""))
            _persist_functional_archive_lineage(
                manifest_path,
                backup=backup,
                raw_source_path=raw_path,
            )
            archive_evidence = verify_archive_manifest(manifest_path)
            _verify_functional_archive_lineage(archive_evidence)
            raw_manifest = raw_path.with_name(raw_path.name + ".manifest.json")
            if raw_manifest.is_file() and not raw_manifest.is_symlink():
                raw_manifest.unlink()
                _fsync_directory(raw_manifest.parent)
            return {
                **archive_evidence,
                "manifest_path": str(manifest_path),
                "source_removed": bool(archived.get("source_removed")),
                "backup_scope": str(
                    archive_evidence.get("backup_scope")
                    or backup.get("backup_scope")
                    or ""
                ),
                "settings_preview_fingerprint": str(
                    archive_evidence.get("settings_preview_fingerprint")
                    or backup.get("settings_preview_fingerprint")
                    or ""
                ),
                "raw_source_path": str(raw_path),
                "reused": False,
                "retention": self._prune_verified_functional_economics_archives(
                    raw_path.parent,
                ),
            }

        archive_path_text = str(backup.get("archive_path") or "")
        if not archive_path_text and raw_path is not None:
            inferred_archive = Path(str(raw_path) + ".zst")
            inferred_manifest = inferred_archive.with_name(
                inferred_archive.name + ".manifest.json"
            )
            if inferred_archive.is_file() and inferred_manifest.is_file():
                archive_path_text = str(inferred_archive)
        if archive_path_text:
            archive_path = Path(archive_path_text).resolve()
            manifest_path = Path(
                str(backup.get("manifest_path") or "")
                or str(archive_path.with_name(archive_path.name + ".manifest.json"))
            )
            if backup.get("backup_scope") or backup.get("settings_preview_fingerprint"):
                _persist_functional_archive_lineage(
                    manifest_path,
                    backup=backup,
                    raw_source_path=raw_path or Path(
                        str(json.loads(manifest_path.read_text(encoding="utf-8")).get("source_path") or "")
                    ),
                )
            verified = verify_archive_manifest(manifest_path)
            _verify_functional_archive_lineage(verified)
            if str(verified.get("archive_path") or "") != str(archive_path):
                raise ValueError(
                    "functional economics archive failed provenance validation"
                )
            return {
                **verified,
                "manifest_path": str(manifest_path),
                "backup_scope": str(backup.get("backup_scope") or "business_day"),
                "settings_preview_fingerprint": str(
                    verified.get("settings_preview_fingerprint")
                    or backup.get("settings_preview_fingerprint")
                    or ""
                ),
                "reused": True,
                "retention": self._prune_verified_functional_economics_archives(
                    archive_path.parent,
                ),
            }
        raise ValueError("functional economics backup evidence is unavailable")

    def _prune_verified_functional_economics_archives(
        self,
        backup_root: Path,
    ) -> dict[str, Any]:
        """Bound retained archives; remove only reverified older exact pairs."""

        from apps.sqlite_backup_archive import verify_archive_manifest

        backup_root = backup_root.resolve()
        recovered = _recover_retention_audit(backup_root)
        patterns = (
            "functional-economics-daily-*.sqlite3.zst.manifest.json",
            "operator-settings-*.sqlite3.zst.manifest.json",
            "warehouse-functional-pre-sync-*.sqlite3.zst.manifest.json",
        )
        removed: list[dict[str, Any]] = []
        kept: list[str] = []
        removals: list[tuple[Path, Path, dict[str, Any]]] = []
        for pattern in patterns:
            candidates = sorted(backup_root.glob(pattern), key=lambda item: item.name, reverse=True)
            kept.extend(str(item) for item in candidates[:FUNCTIONAL_ECONOMICS_ARCHIVE_RETENTION_COUNT])
            if len(candidates) > FUNCTIONAL_ECONOMICS_ARCHIVE_RETENTION_COUNT:
                for retained_manifest in candidates[:FUNCTIONAL_ECONOMICS_ARCHIVE_RETENTION_COUNT]:
                    if retained_manifest.is_symlink() or retained_manifest.stat().st_mode & 0o777 != 0o600:
                        raise ValueError("functional economics archive retention found unsafe retained manifest")
                    retained = verify_archive_manifest(retained_manifest)
                    _verify_functional_archive_lineage(retained)
            for manifest_path in candidates[FUNCTIONAL_ECONOMICS_ARCHIVE_RETENTION_COUNT:]:
                if len(removals) >= FUNCTIONAL_ECONOMICS_ARCHIVE_PRUNE_LIMIT:
                    break
                if manifest_path.is_symlink() or manifest_path.stat().st_mode & 0o777 != 0o600:
                    raise ValueError("functional economics archive retention found unsafe manifest")
                manifest = verify_archive_manifest(manifest_path)
                _verify_functional_archive_lineage(manifest)
                archive_path = Path(str(manifest.get("archive_path") or "")).resolve()
                source_path = Path(str(manifest.get("source_path") or "")).resolve()
                if (
                    archive_path.parent != backup_root
                    or source_path.parent != backup_root
                    or archive_path.with_name(archive_path.name + ".manifest.json")
                    != manifest_path.resolve()
                    or source_path.exists()
                ):
                    raise ValueError(
                        "functional economics archive retention failed exact provenance"
                    )
                audit_item = {
                    "action_id": uuid4().hex,
                    "status": "intent",
                    "archive_path": str(archive_path),
                    "manifest_path": str(manifest_path.resolve()),
                    "archive_sha256": str(manifest.get("archive_sha256") or ""),
                    "source_sha256": str(manifest.get("source_sha256") or ""),
                    "source_size_bytes": int(manifest.get("source_size_bytes") or 0),
                    "backup_scope": str(manifest.get("backup_scope") or ""),
                    "settings_preview_fingerprint": str(
                        manifest.get("settings_preview_fingerprint") or ""
                    ),
                    "intent_at": _now(),
                }
                removals.append((manifest_path.resolve(), archive_path, audit_item))
        audit_path = backup_root / "functional-economics-archive-retention.jsonl"
        if removals:
            _append_retention_audit(
                audit_path,
                [item for _, _, item in removals],
            )
            for manifest_path, archive_path, audit_item in removals:
                archive_path.unlink()
                _fsync_directory(backup_root)
                manifest_path.unlink()
                _fsync_directory(backup_root)
                completed = {
                    **audit_item,
                    "status": "completed",
                    "completed_at": _now(),
                }
                _append_retention_audit(audit_path, [completed])
                removed.append(completed)
        return {
            "policy": "keep_latest_verified_per_scope",
            "keep_count": FUNCTIONAL_ECONOMICS_ARCHIVE_RETENTION_COUNT,
            "prune_limit": FUNCTIONAL_ECONOMICS_ARCHIVE_PRUNE_LIMIT,
            "removed": removed,
            "kept": kept,
            "recovered": recovered,
        }

    def get_payload(self) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions
                   WHERE block_key=? ORDER BY effective_date DESC,revision DESC""",
                (PROXY_BLOCK_KEY,),
            ).fetchall()
            recalc_rows = conn.execute(
                """SELECT * FROM sheet_vitrina_v1_proxy_targeted_recalc_queue
                   ORDER BY created_at DESC,request_id DESC LIMIT 20"""
            ).fetchall()
        history = [_version_row(row) for row in rows]
        today = current_business_date_iso()
        current = next((item for item in history if str(item.get("effective_date") or "") <= today), None)
        current = current or ({
            "version_id": "planned_default",
            "effective_date": INITIAL_EFFECTIVE_DATE,
            "parameters": DEFAULT_PROXY_PARAMETERS.public(),
            "status": "awaiting_functional_cutover",
        })
        return {
            "contract_name": "sheet_vitrina_v1_calculation_parameters",
            "contract_version": "v1",
            "status": "ready" if history else "awaiting_functional_cutover",
            "current": current,
            "history": history,
            "targeted_recalculation_history": [dict(row) for row in recalc_rows],
            "reference": self._three_closed_week_reference(),
        }

    def _three_closed_week_reference(self) -> dict[str, Any]:
        today = date.fromisoformat(current_business_date_iso())
        last_closed_sunday = today - timedelta(days=today.weekday() + 1)
        with _connect(self.runtime.db_path) as conn:
            if not _table_exists(conn, "wb_finance_weekly_aggregates"):
                return {"status": "unavailable", "weeks": [], "rows": []}
            closed_weeks = conn.execute(
                """
                SELECT DISTINCT week_start,week_end FROM wb_finance_weekly_aggregates
                WHERE week_end<=? ORDER BY week_end DESC LIMIT 3
                """,
                (last_closed_sunday.isoformat(),),
            ).fetchall()
            week_keys = [(str(row["week_start"]), str(row["week_end"])) for row in reversed(closed_weeks)]
            source_rows = []
            for week_start, week_end in week_keys:
                source_rows.extend(
                    conn.execute(
                        """SELECT week_start,week_end,metrics_json FROM wb_finance_weekly_aggregates
                           WHERE week_start=? AND week_end=? ORDER BY seller_id""",
                        (week_start, week_end),
                    ).fetchall()
                )
        metric_keys = (
            "net_revenue",
            "commission",
            "acquiring",
            "logistics",
            "storage",
            "acceptance",
            "penalties",
        )
        metrics_by_week: dict[tuple[str, str], dict[str, Decimal]] = {
            key: {metric: Decimal("0") for metric in metric_keys} for key in week_keys
        }
        for row in source_rows:
            target = metrics_by_week[(str(row["week_start"]), str(row["week_end"]))]
            source = _json_loads(row["metrics_json"])
            for metric in metric_keys:
                target[metric] += _decimal(source.get(metric))
        weeks = [{"week_start": start, "week_end": end} for start, end in week_keys]
        metrics = [metrics_by_week[key] for key in week_keys]
        base_key = "net_revenue"
        specs = (
            ("wb_agent_and_other", "Агентское вознаграждение", lambda item: _decimal(item.get("commission")) - _decimal(item.get("acquiring"))),
            ("acquiring", "Эквайринг", lambda item: _decimal(item.get("acquiring"))),
            ("logistics", "Логистика WB", lambda item: _decimal(item.get("logistics"))),
            ("storage", "Хранение WB", lambda item: _decimal(item.get("storage"))),
            ("acceptance", "Платная приёмка", lambda item: _decimal(item.get("acceptance"))),
            ("penalties", "Штрафы/корректировки", lambda item: _decimal(item.get("penalties"))),
        )
        result_rows = []
        total_base = sum((_decimal(item.get(base_key)) for item in metrics), Decimal("0"))
        for key, label, expense_fn in specs:
            expenses = [expense_fn(item) for item in metrics]
            weekly = [
                None if _decimal(item.get(base_key)) == 0 else expense / _decimal(item.get(base_key))
                for item, expense in zip(metrics, expenses)
            ]
            result_rows.append(
                {
                    "key": key,
                    "label": label,
                    "weekly_rate_pct": [None if value is None else _text(value * Decimal("100")) for value in weekly],
                    "weighted_average_pct": None if total_base == 0 else _text(sum(expenses, Decimal("0")) / total_base * Decimal("100")),
                    "included_in_proxy_by_default": key == "wb_agent_and_other",
                    "note": "excluded_from_proxy_cost_already_capitalized" if key == "acceptance" else ("shown_separately_from_commission" if key == "acquiring" else ""),
                }
            )
        return {
            "status": "ready" if len(weeks) == 3 else "partial",
            "gross_buyout_revenue_field": base_key,
            "weeks": weeks,
            "rows": result_rows,
        }


def ensure_calculation_parameters_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_calculation_parameter_versions(
            version_id TEXT PRIMARY KEY,
            block_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            effective_date TEXT NOT NULL,
            rates_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            source TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(block_key,revision)
        );
        CREATE INDEX IF NOT EXISTS calculation_parameters_by_effective_date
        ON sheet_vitrina_v1_calculation_parameter_versions(block_key,effective_date DESC,revision DESC);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_proxy_targeted_recalc_queue(
            request_id TEXT PRIMARY KEY,
            effective_date TEXT NOT NULL,
            settings_version_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT
        );
        """
    )


def _parameters_from_payload(payload: Mapping[str, Any]) -> ProxyParameters:
    effective_date = date.fromisoformat(str(payload.get("effective_date") or "")[:10]).isoformat()
    if effective_date < INITIAL_EFFECTIVE_DATE:
        raise ValueError(f"effective_date must be on or after {INITIAL_EFFECTIVE_DATE}")
    values = {"buyout_rate": _rate(payload.get("buyout_rate"), "buyout_rate")}
    values.update({field: _rate(payload.get(field, "0"), field) for field in RATE_FIELDS})
    result = ProxyParameters(effective_date=effective_date, **values)
    if result.included_expense_rate >= Decimal("1"):
        raise ValueError("total included expenses must be below 100%")
    return result


def _parameters_from_row(row: sqlite3.Row) -> ProxyParameters:
    raw = _json_loads(row["rates_json"])
    return ProxyParameters(
        effective_date=str(row["effective_date"]),
        buyout_rate=_rate(raw.get("buyout_rate"), "buyout_rate"),
        **{field: _rate(raw.get(field, "0"), field) for field in RATE_FIELDS},
        version_id=str(row["version_id"]),
        fingerprint=str(row["fingerprint"]),
    )


def _version_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        raise ValueError("calculation parameter version disappeared")
    parameters = _parameters_from_row(row)
    return {
        "version_id": str(row["version_id"]),
        "revision": int(row["revision"]),
        "effective_date": str(row["effective_date"]),
        "parameters": parameters.public(),
        "fingerprint": str(row["fingerprint"]),
        "source": str(row["source"]),
        "created_by": str(row["created_by"]),
        "created_at": str(row["created_at"]),
    }


def _rate(value: Any, field: str) -> Decimal:
    result = _decimal(value)
    if result < 0 or result > 1:
        raise ValueError(f"{field} must be between 0 and 1")
    return result


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value if value not in (None, "") else "0"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("decimal must be finite")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None or value == "" else _decimal(value)


def _text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        loaded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


def _settings_fingerprint(value: Mapping[str, Any]) -> str:
    semantic = {key: item for key, item in value.items() if key not in {"version_id", "fingerprint"}}
    return "sha256:" + hashlib.sha256(_json(semantic).encode("utf-8")).hexdigest()


def _same_filesystem(left: Path, right: Path) -> bool:
    return left.stat().st_dev == right.stat().st_dev


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temp_path = path.with_name(path.name + f".tmp-{uuid4().hex}")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _persist_functional_archive_lineage(
    manifest_path: Path,
    *,
    backup: Mapping[str, Any],
    raw_source_path: Path,
) -> None:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("functional economics archive manifest is unavailable")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup_scope = str(
        backup.get("backup_scope") or payload.get("backup_scope") or ""
    )
    settings_preview_fingerprint = str(
        backup.get("settings_preview_fingerprint")
        or payload.get("settings_preview_fingerprint")
        or ""
    )
    lineage = {
        "backup_scope": backup_scope,
        "settings_preview_fingerprint": settings_preview_fingerprint,
        "raw_source_path": str(raw_source_path.resolve()),
        "source_sha256": str(payload.get("source_sha256") or ""),
    }
    payload.update(lineage)
    payload["lineage_fingerprint"] = (
        "sha256:" + hashlib.sha256(_json(lineage).encode("utf-8")).hexdigest()
    )
    _write_private_json_atomic(manifest_path, payload)


def _verify_functional_archive_lineage(manifest: Mapping[str, Any]) -> None:
    backup_scope = str(manifest.get("backup_scope") or "")
    if not backup_scope:
        return
    lineage = {
        "backup_scope": backup_scope,
        "settings_preview_fingerprint": str(
            manifest.get("settings_preview_fingerprint") or ""
        ),
        "raw_source_path": str(manifest.get("raw_source_path") or ""),
        "source_sha256": str(manifest.get("source_sha256") or ""),
    }
    expected = "sha256:" + hashlib.sha256(
        _json(lineage).encode("utf-8")
    ).hexdigest()
    if (
        str(manifest.get("lineage_fingerprint") or "") != expected
        or not lineage["raw_source_path"]
        or not lineage["source_sha256"]
    ):
        raise ValueError("functional economics archive lineage is invalid")
    if (
        backup_scope == "fresh_operator_settings"
        and not lineage["settings_preview_fingerprint"].startswith("sha256:")
    ):
        raise ValueError("operator settings archive lacks preview fingerprint lineage")


def _recover_retention_audit(backup_root: Path) -> list[dict[str, Any]]:
    from apps.sqlite_backup_archive import verify_archive_manifest

    audit_path = backup_root / "functional-economics-archive-retention.jsonl"
    if not audit_path.exists():
        return []
    if audit_path.is_symlink() or audit_path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("functional economics retention audit is unsafe")
    latest: dict[str, dict[str, Any]] = {}
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        action_id = str(item.get("action_id") or "")
        if (
            str(item.get("contract_name") or "")
            == "functional_economics_archive_retention_v1"
            and not action_id
            and str(item.get("removed_at") or "")
        ):
            # Before the intent/completion journal was introduced, successful
            # removals were recorded only after deletion. They are immutable
            # completed history and have no recovery action to resume.
            continue
        if (
            str(item.get("contract_name") or "")
            != "functional_economics_archive_retention_v1"
            or not action_id
        ):
            raise ValueError("functional economics retention audit is invalid")
        latest[action_id] = dict(item)
    recovered: list[dict[str, Any]] = []
    for action_id, item in latest.items():
        if str(item.get("status") or "") == "completed":
            continue
        archive_input = Path(str(item.get("archive_path") or ""))
        manifest_input = Path(str(item.get("manifest_path") or ""))
        archive_is_symlink = archive_input.is_symlink()
        manifest_is_symlink = manifest_input.is_symlink()
        archive_path = archive_input.resolve()
        manifest_path = manifest_input.resolve()
        if (
            archive_path.parent != backup_root
            or manifest_path.parent != backup_root
            or manifest_path
            != archive_path.with_name(archive_path.name + ".manifest.json")
            or archive_is_symlink
            or manifest_is_symlink
        ):
            raise ValueError("functional economics retention recovery path is invalid")
        if archive_path.exists() and manifest_path.exists():
            manifest = verify_archive_manifest(manifest_path)
            _verify_functional_archive_lineage(manifest)
            if (
                str(manifest.get("archive_sha256") or "")
                != str(item.get("archive_sha256") or "")
                or str(manifest.get("source_sha256") or "")
                != str(item.get("source_sha256") or "")
            ):
                raise ValueError("functional economics retention recovery fingerprint changed")
            archive_path.unlink()
            _fsync_directory(backup_root)
            manifest_path.unlink()
            _fsync_directory(backup_root)
        elif not archive_path.exists() and manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                str(manifest.get("archive_path") or "") != str(archive_path)
                or str(manifest.get("archive_sha256") or "")
                != str(item.get("archive_sha256") or "")
                or str(manifest.get("source_sha256") or "")
                != str(item.get("source_sha256") or "")
            ):
                raise ValueError("functional economics retention orphan manifest changed")
            manifest_path.unlink()
            _fsync_directory(backup_root)
        elif archive_path.exists() and not manifest_path.exists():
            raise ValueError("functional economics retention lost archive manifest")
        completed = {
            **item,
            "status": "completed",
            "completed_at": _now(),
            "recovered": True,
        }
        _append_retention_audit(audit_path, [completed])
        recovered.append(completed)
    return recovered


def _append_retention_audit(path: Path, removed: list[dict[str, Any]]) -> None:
    if path.is_symlink():
        raise ValueError("functional economics retention audit is unsafe")
    existing = path.read_bytes() if path.exists() else b""
    if path.exists() and path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("functional economics retention audit is unsafe")
    appended = b"".join(
        (
            json.dumps(
                {
                    "contract_name": "functional_economics_archive_retention_v1",
                    **item,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        for item in removed
    )
    temp_path = path.with_name(path.name + f".tmp-{uuid4().hex}")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(existing)
            handle.write(appended)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _write_daily_raw_backup_manifest(
    *,
    source: Path,
    manifest_path: Path,
    business_date: str,
    backup: Mapping[str, Any],
) -> None:
    source_sha256 = str(backup.get("sha256") or "")
    source_sha256 = (
        source_sha256 if source_sha256.startswith("sha256:") else f"sha256:{source_sha256}"
    )
    evidence = {
        "contract_name": "functional_economics_daily_raw_backup_v1",
        "source_path": str(source.resolve()),
        "source_size_bytes": int(backup.get("size_bytes") or -1),
        "source_sha256": source_sha256,
        "source_integrity_check": str(backup.get("integrity_check") or ""),
        "business_date": str(business_date)[:10],
    }
    payload = {
        **evidence,
        "fingerprint": "sha256:" + hashlib.sha256(_json(evidence).encode("utf-8")).hexdigest(),
        "created_at": _now(),
    }
    temp_path = manifest_path.with_name(manifest_path.name + f".tmp-{uuid4().hex}")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, manifest_path)
        directory_descriptor = os.open(manifest_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _write_operator_settings_raw_backup_manifest(
    *,
    source: Path,
    manifest_path: Path,
    preview_fingerprint: str,
    backup: Mapping[str, Any],
) -> None:
    source_sha256 = str(backup.get("sha256") or "")
    source_sha256 = (
        source_sha256
        if source_sha256.startswith("sha256:")
        else f"sha256:{source_sha256}"
    )
    evidence = {
        "contract_name": "operator_settings_raw_backup_v1",
        "backup_scope": "fresh_operator_settings",
        "settings_preview_fingerprint": str(preview_fingerprint),
        "source_path": str(source.resolve()),
        "source_size_bytes": int(backup.get("size_bytes") or -1),
        "source_sha256": source_sha256,
        "source_integrity_check": str(backup.get("integrity_check") or ""),
    }
    _write_private_json_atomic(
        manifest_path,
        {
            **evidence,
            "fingerprint": "sha256:"
            + hashlib.sha256(_json(evidence).encode("utf-8")).hexdigest(),
            "created_at": _now(),
        },
    )


def _verify_daily_raw_backup_manifest(
    *,
    source: Path,
    manifest_path: Path,
    business_date: str,
) -> dict[str, Any]:
    from apps.sqlite_backup_archive import build_plan

    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("daily functional economics raw backup manifest is unavailable")
    if manifest_path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("daily functional economics raw backup manifest must use mode 0600")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    semantic = {
        key: value
        for key, value in manifest.items()
        if key not in {"fingerprint", "created_at"}
    }
    fingerprint = "sha256:" + hashlib.sha256(_json(semantic).encode("utf-8")).hexdigest()
    actual = build_plan(source=source)
    if (
        str(manifest.get("contract_name") or "")
        != "functional_economics_daily_raw_backup_v1"
        or str(manifest.get("fingerprint") or "") != fingerprint
        or str(manifest.get("source_path") or "") != str(source.resolve())
        or str(manifest.get("business_date") or "")[:10] != str(business_date)[:10]
        or str(manifest.get("source_integrity_check") or "") != "ok"
        or int(manifest.get("source_size_bytes") or -1)
        != int(actual.get("source_size_bytes") or -2)
        or str(manifest.get("source_sha256") or "")
        != str(actual.get("source_sha256") or "")
    ):
        raise ValueError("daily functional economics raw backup manifest failed provenance validation")
    return actual


def _connect(db_path: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
