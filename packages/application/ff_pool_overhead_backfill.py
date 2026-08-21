"""Exact owner-gated repair for the five 2026-08-21 FBS overhead documents."""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping

from packages.application.ff_document_workflow import (
    mark_ff_replay_economics,
    mark_ff_replay_finance,
)
from packages.application.ff_pool_documents import (
    DOCUMENTS_TABLE,
    DOCUMENT_LINES_TABLE,
    DOCUMENT_RELATIONS_TABLE,
    REQUESTS_TABLE,
    TARGETED_RECALC_QUEUE_TABLE,
    FfPoolDocumentService,
    ensure_ff_pool_document_schema,
)
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
)
from packages.application.ff_pool_fbs_lifecycle import EVENTS_TABLE
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_functional import WarehouseFunctionalBlock
from packages.application.warehouse_functional_economics_backfill import (
    apply_functional_economics_backfill_plan,
    build_functional_economics_backfill_plan,
)
from packages.application.warehouse_functional_lock import (
    warehouse_functional_write_lock,
)
from packages.application.wb_finance_weekly import block_from_env


CONTRACT_NAME = "ff_pool_overhead_backfill_20260821_v2"
CONTRACT_VERSION = 2
BUSINESS_DATE = "2026-08-21"
EXPECTED_DOCUMENT_COUNT = 5
EXPECTED_TOTAL_RUB = Decimal("175206.50")
EXPECTED_CITY_SCOPE = {
    "Москва": {"document_count": 4, "amount_rub": Decimal("115206.50")},
    "Оренбург": {"document_count": 1, "amount_rub": Decimal("60000.00")},
}
SAFE_SHA_RE = re.compile(r"[0-9a-f]{40}")
ZERO = Decimal("0")
CAPITAL_MINOR_UNIT = Decimal("0.01")
CAPITAL_COMPARISON = "decimal_round_half_up_kopeck_v1"


class FfPoolOverheadBackfillError(RuntimeError):
    pass


class FfPoolOverheadBackfill:
    """Resolve, plan, apply and read back one immutable five-document scope."""

    def __init__(
        self,
        *,
        runtime_dir: Path,
        deployed_sha: str,
        timestamp_factory: Any | None = None,
    ) -> None:
        self.runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(runtime_dir).resolve()
        )
        self.deployed_sha = str(deployed_sha or "").strip().lower()
        if not SAFE_SHA_RE.fullmatch(self.deployed_sha):
            raise FfPoolOverheadBackfillError(
                "deployed_sha must be an exact 40-hex SHA"
            )
        self.timestamp_factory = timestamp_factory or _utc_now

    def build_plan(self) -> dict[str, Any]:
        self._assert_runtime_sha()
        snapshot = _read_snapshot(self.runtime.db_path)
        blockers = list(snapshot["blockers"])
        aggregate_updates = [
            item
            for item in snapshot["target_projection"]
            if item["aggregate_write_required"]
        ]
        aggregate_delta = sum(
            (Decimal(str(item["aggregate_write_delta_rub"])) for item in aggregate_updates),
            ZERO,
        )
        publication_required = any(
            item["publication_state"] != "complete"
            for item in snapshot["queues"]
        )
        plan: dict[str, Any] = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "mode": "dry_run",
            "deployed_sha": self.deployed_sha,
            "generated_at": str(self.timestamp_factory()),
            "scope": {
                "business_date": BUSINESS_DATE,
                "document_ids": [
                    item["document_id"] for item in snapshot["documents"]
                ],
                "request_ids": [
                    item["request_id"] for item in snapshot["documents"]
                ],
                "facility_ids": sorted(
                    {item["facility_id"] for item in snapshot["documents"]}
                ),
                "pool": "FBS",
                "affected_nm_ids": snapshot["affected_nm_ids"],
                "queue_ids": [item["queue_id"] for item in snapshot["queues"]],
                "stable_source_ids": [
                    item["stable_source_id"] for item in snapshot["queues"]
                ],
            },
            "resolved_documents": snapshot["documents"],
            "event_revisions": snapshot["queues"],
            "pre_change": {
                "target_projection": snapshot["target_projection"],
                "target_projection_digest": snapshot[
                    "target_projection_digest"
                ],
                "pool_balance_digest": snapshot["pool_balance_digest"],
                "lifecycle_digest": snapshot["lifecycle_digest"],
                "document_digest": snapshot["document_digest"],
                "quantity_digest": snapshot["quantity_digest"],
                "non_target_digest": snapshot["non_target_digest"],
            },
            "expected_effects": {
                "document_insert_count": 0,
                "ledger_insert_count": 0,
                "business_document_replay_count": 0,
                "queue_insert_count": snapshot["queue_insert_count"],
                "aggregate_row_update_count": len(aggregate_updates),
                "quantity_delta": 0,
                "selected_document_amount_rub": _money(EXPECTED_TOTAL_RUB),
                "aggregate_capital_rewrite_rub": _decimal_text(aggregate_delta),
                "capital_delta_rub": _decimal_text(aggregate_delta),
                "canonical_publication_required": publication_required,
                "canonical_publication_queue_count": sum(
                    1
                    for item in snapshot["queues"]
                    if item["publication_state"] != "complete"
                ),
                "already_current_no_op": (
                    not aggregate_updates
                    and snapshot["queue_insert_count"] == 0
                    and not publication_required
                ),
                "fulfilled_order_update_count": 0,
                "current_projection_only": True,
                "future_handoff_uses_current_wac": True,
            },
            "invariants": {
                "selected_document_count": len(snapshot["documents"]),
                "selected_amount_rub": snapshot["selected_amount_rub"],
                "city_scope": snapshot["city_scope"],
                "quantities_unchanged": True,
                "capital_conserved": not blockers,
                "capital_comparison": CAPITAL_COMPARISON,
                "past_lifecycle_events_immutable": True,
                "non_target_digest": snapshot["non_target_digest"],
                "no_missing_to_zero": True,
            },
            "recovery": {
                "kind": "coherent_sqlite_backup_plus_exact_forward_readback",
                "full_database_copy_outside_writer_lock": True,
                "restore_requires_separate_authorization": True,
                "idempotency": "exact manifest and queue identities",
            },
            "pre_state": snapshot["aggregate_pre_state"],
            "apply_allowed": not blockers,
            "blockers": blockers,
        }
        plan["fingerprint"] = _fingerprint(_fingerprint_material(plan))
        return plan

    def apply(
        self,
        reviewed_plan: Mapping[str, Any],
        *,
        fingerprint: str,
        approval_reference: str,
        actor: str,
        backup_dir: Path,
        evidence_dir: Path,
    ) -> dict[str, Any]:
        self._assert_runtime_sha()
        _validate_reviewed_plan(
            reviewed_plan,
            fingerprint=fingerprint,
            deployed_sha=self.deployed_sha,
            approval_reference=approval_reference,
            actor=actor,
        )
        backup_root = _external_directory(backup_dir, name="backup_dir")
        evidence_root = _external_directory(evidence_dir, name="evidence_dir")
        evidence_path = evidence_root / (
            "ff-pool-overhead-backfill-"
            + fingerprint.removeprefix("sha256:")[:16]
            + ".json"
        )
        if evidence_path.is_file():
            prior = _read_json(evidence_path)
            if (
                prior.get("status") != "complete"
                or prior.get("manifest_fingerprint") != fingerprint
                or prior.get("deployed_sha") != self.deployed_sha
                or prior.get("evidence_fingerprint")
                != _fingerprint(
                    {
                        key: value
                        for key, value in prior.items()
                        if key != "evidence_fingerprint"
                    }
                )
            ):
                raise FfPoolOverheadBackfillError(
                    "existing evidence is invalid or belongs to another manifest"
                )
            readback = self.readback(reviewed_plan=reviewed_plan)
            _assert_complete_readback(readback)
            return {
                **prior,
                "idempotent": True,
                "readback": readback,
                "evidence_path": str(evidence_path),
            }

        backup_path = backup_root / (
            "ff-pool-overhead-backfill-"
            + fingerprint.removeprefix("sha256:")[:16]
            + ".sqlite3"
        )
        backup = _ensure_backup(
            self.runtime,
            backup_path,
            manifest_fingerprint=fingerprint,
            deployed_sha=self.deployed_sha,
        )

        expected_before = dict(reviewed_plan["pre_change"])
        current = _read_snapshot(
            self.runtime.db_path,
            exact_document_ids=list(reviewed_plan["scope"]["document_ids"]),
        )
        already_short_applied = _short_apply_already_complete(
            current=current,
            reviewed_plan=reviewed_plan,
        )
        if not already_short_applied:
            if current["target_projection_digest"] != expected_before[
                "target_projection_digest"
            ]:
                raise FfPoolOverheadBackfillError(
                    "current aggregate projection drifted after reviewed dry-run"
                )
            if current["non_target_digest"] != expected_before[
                "non_target_digest"
            ]:
                raise FfPoolOverheadBackfillError(
                    "non-target state drifted after reviewed dry-run"
                )
            self._apply_short_projection_and_queue(reviewed_plan)

        queue_rows = _exact_queue_rows(
            self.runtime.db_path,
            list(reviewed_plan["event_revisions"]),
        )
        affected_nm_ids = [
            int(value) for value in reviewed_plan["scope"]["affected_nm_ids"]
        ]
        stable_source_ids = list(reviewed_plan["scope"]["stable_source_ids"])
        functional_result: dict[str, Any]
        queue_ids = [str(item["queue_id"]) for item in reviewed_plan["event_revisions"]]
        warehouse_required = any(
            str(item.get("status") or "") != "complete" for item in queue_rows
        )
        economics_required = any(
            str(item.get("economics_status") or "") != "complete"
            for item in queue_rows
        )
        finance_required = any(
            str(item.get("finance_status") or "") != "complete"
            or not str(item.get("finance_source_fingerprint") or "").startswith(
                "sha256:"
            )
            for item in queue_rows
        )
        try:
            if not warehouse_required:
                functional_result = {
                    "status": "already_complete",
                    "idempotent": True,
                    "queue_ids": [item["queue_id"] for item in queue_rows],
                }
            else:
                block = WarehouseFunctionalBlock(runtime=self.runtime)
                functional_plan = block.build_targeted_recovery_plan(
                    affected_nm_ids=affected_nm_ids,
                    stable_source_ids=stable_source_ids,
                    targeted_recalc_requests=queue_rows,
                )
                functional_result = block.apply_plan(
                    functional_plan,
                    confirm_fingerprint=str(functional_plan["plan_fingerprint"]),
                )
                functional_result = {
                    "plan_fingerprint": str(functional_plan["plan_fingerprint"]),
                    "active_version_id": str(
                        (functional_result.get("active_version") or {}).get(
                            "version_id"
                        )
                        or ""
                    ),
                    "idempotent": bool(functional_result.get("idempotent")),
                }
        except Exception as exc:
            _mark_publication_error(
                self.runtime,
                queue_ids=queue_ids,
                occurred_at=str(self.timestamp_factory()),
                error=f"Warehouse publication: {exc}",
            )
            raise

        if economics_required:
            try:
                economics_plan = build_functional_economics_backfill_plan(
                    self.runtime,
                    affected_nm_ids=affected_nm_ids,
                    earliest_business_date=BUSINESS_DATE,
                )
                economics_result = apply_functional_economics_backfill_plan(
                    self.runtime,
                    economics_plan,
                    confirm_fingerprint=str(economics_plan["plan_fingerprint"]),
                    backup_dir=(backup_root / "economics").resolve(),
                    target_scoped_undo=True,
                )
                completed_at = str(self.timestamp_factory())
                mark_ff_replay_economics(
                    self.runtime,
                    queue_ids=queue_ids,
                    status="complete",
                    occurred_at=completed_at,
                )
                economics_publication = {
                    "status": "complete",
                    "plan_fingerprint": str(economics_plan["plan_fingerprint"]),
                    "changed_snapshot_count": int(
                        economics_result.get("changed_snapshot_count") or 0
                    ),
                    "database_written": bool(
                        economics_result.get("database_written")
                    ),
                    "rollback_manifest_digest": str(
                        economics_result.get("rollback_manifest_digest") or ""
                    ),
                }
            except Exception as exc:
                _mark_publication_error(
                    self.runtime,
                    queue_ids=queue_ids,
                    occurred_at=str(self.timestamp_factory()),
                    error=f"economics publication: {exc}",
                )
                raise
        else:
            economics_publication = {
                "status": "already_complete",
                "idempotent": True,
                "database_written": False,
            }

        if finance_required:
            try:
                finance_result = block_from_env(
                    self.runtime.runtime_dir
                ).recalculate_stale_cost_weeks(date_from=date(2026, 7, 1))
            except Exception as exc:
                _mark_publication_error(
                    self.runtime,
                    queue_ids=queue_ids,
                    occurred_at=str(self.timestamp_factory()),
                    error=f"Finance publication: {exc}",
                )
                raise
            finance_fingerprint = str(finance_result.get("fingerprint") or "")
            if (
                str(finance_result.get("status") or "")
                not in {"applied", "already_current"}
                or not finance_fingerprint.startswith("sha256:")
                or finance_result.get("non_target_preserved") is not True
            ):
                error = "Finance CAS publication did not produce complete exact readback"
                _mark_publication_error(
                    self.runtime,
                    queue_ids=queue_ids,
                    occurred_at=str(self.timestamp_factory()),
                    error=error,
                )
                raise FfPoolOverheadBackfillError(error)
            mark_ff_replay_finance(
                self.runtime,
                queue_ids=queue_ids,
                status="complete",
                occurred_at=str(self.timestamp_factory()),
                source_fingerprint=finance_fingerprint,
            )
            finance_publication = {
                "fingerprint": finance_fingerprint,
                "status": str(finance_result.get("status") or ""),
                "recalculated_week_count": int(
                    finance_result.get("recalculated_week_count") or 0
                ),
                "non_target_preserved": bool(
                    finance_result.get("non_target_preserved")
                ),
                "phase_timings_ms": dict(
                    finance_result.get("phase_timings_ms") or {}
                ),
            }
        else:
            finance_publication = {
                "status": "already_complete",
                "idempotent": True,
                "database_written": False,
                "fingerprints": sorted(
                    {
                        str(item["finance_source_fingerprint"])
                        for item in queue_rows
                    }
                ),
            }
        if warehouse_required or economics_required or finance_required:
            FfPoolDocumentService(
                db_path=self.runtime.db_path,
                runtime_dir=self.runtime.runtime_dir,
                resume=False,
            ).resume_incomplete()

        readback = self.readback(reviewed_plan=reviewed_plan)
        try:
            _assert_complete_readback(readback)
        except Exception as exc:
            _mark_publication_error(
                self.runtime,
                queue_ids=queue_ids,
                occurred_at=str(self.timestamp_factory()),
                error=f"reconciliation: {exc}",
            )
            raise
        evidence: dict[str, Any] = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "complete",
            "manifest_fingerprint": fingerprint,
            "deployed_sha": self.deployed_sha,
            "approval_reference": str(approval_reference).strip(),
            "actor": str(actor).strip(),
            "completed_at": str(self.timestamp_factory()),
            "backup": backup,
            "functional_publication": functional_result,
            "economics_publication": economics_publication,
            "finance_publication": finance_publication,
            "readback": readback,
            "idempotent": (
                already_short_applied
                and not warehouse_required
                and not economics_required
                and not finance_required
            ),
        }
        evidence["evidence_fingerprint"] = _fingerprint(evidence)
        _write_private_json(evidence_path, evidence)
        return {**evidence, "evidence_path": str(evidence_path)}

    def readback(
        self,
        *,
        reviewed_plan: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._assert_runtime_sha()
        document_ids = (
            list((reviewed_plan or {}).get("scope", {}).get("document_ids") or [])
            or None
        )
        snapshot = _read_snapshot(
            self.runtime.db_path,
            exact_document_ids=document_ids,
        )
        queues = snapshot["queue_rows"]
        complete = bool(queues) and len(queues) == EXPECTED_DOCUMENT_COUNT and all(
            str(item.get("status") or "") == "complete"
            and str(item.get("economics_status") or "") == "complete"
            and str(item.get("finance_status") or "") == "complete"
            and str(item.get("finance_source_fingerprint") or "").startswith(
                "sha256:"
            )
            for item in queues
        )
        projection_current = all(
            _capital_equal(
                Decimal(str(item["aggregate_capital_rub"])),
                Decimal(str(item["detail_capital_rub"])),
            )
            and int(item["aggregate_quantity"])
            == int(item["detail_quantity"])
            for item in snapshot["target_projection"]
        )
        lifecycle_unchanged = (
            reviewed_plan is None
            or snapshot["lifecycle_digest"]
            == reviewed_plan["pre_change"]["lifecycle_digest"]
        )
        document_unchanged = (
            reviewed_plan is None
            or snapshot["document_digest"]
            == reviewed_plan["pre_change"]["document_digest"]
        )
        quantity_unchanged = (
            reviewed_plan is None
            or snapshot["quantity_digest"]
            == reviewed_plan["pre_change"]["quantity_digest"]
        )
        non_target_unchanged = (
            reviewed_plan is None
            or snapshot["non_target_digest"]
            == reviewed_plan["pre_change"]["non_target_digest"]
        )
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": (
                "complete"
                if complete
                and projection_current
                and lifecycle_unchanged
                and document_unchanged
                and quantity_unchanged
                and non_target_unchanged
                else "pending"
            ),
            "deployed_sha": self.deployed_sha,
            "document_ids": [item["document_id"] for item in snapshot["documents"]],
            "selected_amount_rub": snapshot["selected_amount_rub"],
            "affected_nm_ids": snapshot["affected_nm_ids"],
            "queues": queues,
            "target_projection": snapshot["target_projection"],
            "projection_current": projection_current,
            "capital_comparison": CAPITAL_COMPARISON,
            "aggregate_pre_state": snapshot["aggregate_pre_state"],
            "quantity_unchanged": quantity_unchanged,
            "capital_conserved": projection_current,
            "past_fulfilled_lifecycle_unchanged": lifecycle_unchanged,
            "documents_unchanged": document_unchanged,
            "non_target_unchanged": non_target_unchanged,
            "pre_change_invariants_verified": reviewed_plan is not None,
            "no_duplicate_submit": len(snapshot["documents"])
            == EXPECTED_DOCUMENT_COUNT,
            "blockers": snapshot["blockers"],
        }

    def _apply_short_projection_and_queue(
        self, reviewed_plan: Mapping[str, Any]
    ) -> None:
        projection = list(reviewed_plan["pre_change"]["target_projection"])
        revisions = list(reviewed_plan["event_revisions"])
        with warehouse_functional_write_lock(
            self.runtime.runtime_dir, timeout_seconds=300
        ):
            with sqlite3.connect(self.runtime.db_path, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                ensure_ff_pool_document_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    active = conn.execute(
                        "SELECT version_id FROM "
                        "sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
                    ).fetchone()
                    if active is None:
                        raise FfPoolOverheadBackfillError(
                            "active aggregate warehouse version is missing"
                        )
                    version_id = str(active["version_id"])
                    if version_id != str(projection[0]["version_id"]):
                        raise FfPoolOverheadBackfillError(
                            "active aggregate version drifted before short apply"
                        )
                    for item in projection:
                        row = conn.execute(
                            "SELECT quantity,capital_rub,provenance_json FROM "
                            "sheet_vitrina_v1_warehouse_functional_balances "
                            "WHERE version_id=? AND warehouse_key='ff' AND nm_id=?",
                            (version_id, int(item["nm_id"])),
                        ).fetchone()
                        if row is None:
                            raise FfPoolOverheadBackfillError(
                                f"aggregate FF row disappeared: {item['nm_id']}"
                            )
                        if (
                            int(Decimal(str(row["quantity"])))
                            != int(item["aggregate_quantity"])
                            or Decimal(str(row["capital_rub"]))
                            != Decimal(str(item["aggregate_capital_rub"]))
                        ):
                            raise FfPoolOverheadBackfillError(
                                f"target aggregate row drifted: {item['nm_id']}"
                            )
                        if not bool(item["aggregate_write_required"]):
                            if not _capital_equal(
                                Decimal(str(row["capital_rub"])),
                                Decimal(str(item["detail_capital_rub"])),
                            ):
                                raise FfPoolOverheadBackfillError(
                                    "already-current aggregate row lost numeric parity: "
                                    f"{item['nm_id']}"
                                )
                            continue
                        provenance = {
                            "source": CONTRACT_NAME,
                            "manifest_fingerprint": str(
                                reviewed_plan["fingerprint"]
                            ),
                            "selected_document_ids": list(
                                reviewed_plan["scope"]["document_ids"]
                            ),
                            "previous_provenance_sha256": _fingerprint(
                                _loads(row["provenance_json"], {})
                            ),
                        }
                        conn.execute(
                            "UPDATE sheet_vitrina_v1_warehouse_functional_balances "
                            "SET capital_rub=?,wac_rub=?,provenance_json=? "
                            "WHERE version_id=? AND warehouse_key='ff' AND nm_id=?",
                            (
                                str(item["detail_capital_rub"]),
                                str(item["detail_wac_rub"]),
                                _json(provenance),
                                version_id,
                                int(item["nm_id"]),
                            ),
                        )
                    for item in revisions:
                        conn.execute(
                            f"INSERT OR IGNORE INTO {TARGETED_RECALC_QUEUE_TABLE}("
                            "queue_id,stable_source_id,source_revision,effective_date,"
                            "affected_nm_ids_json,status,requested_at,started_at,"
                            "finished_at,error,economics_status,economics_started_at,"
                            "economics_finished_at,economics_error,finance_status,"
                            "finance_source_fingerprint,finance_started_at,"
                            "finance_finished_at,finance_error) "
                            "VALUES(?,?,?,?,?,'queued',?,NULL,NULL,NULL,'',NULL,NULL,NULL,"
                            "'', '',NULL,NULL,NULL)",
                            (
                                str(item["queue_id"]),
                                str(item["stable_source_id"]),
                                str(item["source_revision"]),
                                BUSINESS_DATE,
                                _json(item["affected_nm_ids"]),
                                str(item["requested_at"]),
                            ),
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

    def _assert_runtime_sha(self) -> None:
        markers = (
            self.runtime.runtime_dir / ".wb-core-runtime-sha",
            self.runtime.runtime_dir.parent / "app" / ".wb-core-runtime-sha",
        )
        existing = [marker for marker in markers if marker.is_file()]
        if not existing:
            raise FfPoolOverheadBackfillError(
                "canonical deployed SHA marker is missing"
            )
        actual = {
            marker.read_text(encoding="utf-8").strip().lower()
            for marker in existing
        }
        if actual != {self.deployed_sha}:
            raise FfPoolOverheadBackfillError(
                "canonical runtime SHA differs from requested deployed_sha"
            )


def _read_snapshot(
    db_path: Path,
    *,
    exact_document_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    with closing(_open_query_only(db_path)) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            DOCUMENTS_TABLE,
            DOCUMENT_LINES_TABLE,
            DOCUMENT_RELATIONS_TABLE,
            REQUESTS_TABLE,
            TARGETED_RECALC_QUEUE_TABLE,
            BALANCES_TABLE,
            FACILITIES_TABLE,
            FACILITY_PROFILES_TABLE,
            EVENTS_TABLE,
            "sheet_vitrina_v1_warehouse_functional_active",
            "sheet_vitrina_v1_warehouse_functional_balances",
        }
        missing = sorted(required - tables)
        if missing:
            raise FfPoolOverheadBackfillError(
                "required production tables are missing: " + ",".join(missing)
            )
        ids = sorted({str(item) for item in (exact_document_ids or []) if str(item)})
        params: list[Any] = [BUSINESS_DATE]
        where = "d.document_kind='pool_overhead' AND d.business_date=?"
        if ids:
            where += " AND d.document_id IN (" + ",".join("?" for _ in ids) + ")"
            params.extend(ids)
        raw_documents = [
            dict(row)
            for row in conn.execute(
                f"""SELECT d.*,r.posted_manifest_sha256 request_manifest_sha256,
                           r.source_revision request_source_revision,
                           r.state request_state
                    FROM {DOCUMENTS_TABLE} d
                    JOIN {REQUESTS_TABLE} r ON r.request_id=d.request_id
                    WHERE {where} ORDER BY d.posted_at,d.document_id""",
                params,
            ).fetchall()
        ]
        documents: list[dict[str, Any]] = []
        blockers: list[str] = []
        for row in raw_documents:
            manifest = _loads(row["posted_manifest_json"], {})
            domain = dict(manifest.get("domain") or {})
            if str(domain.get("scope") or "") != "FBS":
                continue
            document_id = str(row["document_id"])
            lines = [
                dict(item)
                for item in conn.execute(
                    f"SELECT * FROM {DOCUMENT_LINES_TABLE} "
                    "WHERE document_id=? ORDER BY line_no",
                    (document_id,),
                ).fetchall()
            ]
            facility_ids = sorted(
                {str(item["facility_id"] or "") for item in lines}
            )
            pools = sorted({str(item["pool"] or "") for item in lines})
            if len(facility_ids) != 1 or pools != ["FBS"] or not lines:
                blockers.append(
                    f"document {document_id} lacks one exact FBS facility manifest"
                )
                continue
            facility_id = facility_ids[0]
            facility = conn.execute(
                f"""SELECT f.facility_id,f.name,p.city
                    FROM {FACILITIES_TABLE} f
                    LEFT JOIN {FACILITY_PROFILES_TABLE} p
                      ON p.facility_id=f.facility_id
                    WHERE f.facility_id=?""",
                (facility_id,),
            ).fetchall()
            if len(facility) != 1:
                blockers.append(
                    f"document {document_id} facility identity is unavailable"
                )
                continue
            city = str(facility[0]["city"] or "")
            amount = sum(
                (Decimal(str(item["capital_rub"])) for item in lines), ZERO
            )
            expense = sum(
                (Decimal(str(item["expense_rub"])) for item in lines), ZERO
            )
            domain_amount = Decimal(str(domain.get("amount_rub") or "0"))
            if amount != expense or amount != domain_amount:
                blockers.append(
                    f"document {document_id} allocation does not conserve its amount"
                )
            storno = conn.execute(
                f"""SELECT child.document_id
                    FROM {DOCUMENT_RELATIONS_TABLE} relation
                    JOIN {DOCUMENTS_TABLE} child
                      ON child.document_id=relation.child_document_id
                    WHERE relation.parent_document_id=?
                      AND child.document_kind='storno'""",
                (document_id,),
            ).fetchall()
            if storno:
                blockers.append(f"document {document_id} already has a storno")
            nm_ids = sorted({int(item["nm_id"]) for item in lines})
            revision = _fingerprint(
                {
                    "contract": "pool_overhead_targeted_publication_v1",
                    "document_id": document_id,
                    "request_source_revision": str(
                        row["request_source_revision"]
                    ),
                    "facility_id": facility_id,
                    "pools": pools,
                    "nm_ids": nm_ids,
                    "basis_digest": str(domain.get("basis_digest") or ""),
                    "amount_rub": _money(domain_amount),
                    "posted_manifest_sha256": str(
                        row["request_manifest_sha256"]
                    ),
                }
            )
            stable = f"pool_overhead:{document_id}"
            queue_id = "whrq_" + hashlib.sha256(
                _json(
                    {"stable_source_id": stable, "source_revision": revision}
                ).encode("utf-8")
            ).hexdigest()[:24]
            documents.append(
                {
                    "document_id": document_id,
                    "request_id": str(row["request_id"]),
                    "operation_id": str(row["operation_id"]),
                    "facility_id": facility_id,
                    "facility_name": str(facility[0]["name"] or ""),
                    "city": city,
                    "pool": "FBS",
                    "amount_rub": _money(amount),
                    "nm_ids": nm_ids,
                    "line_count": len(lines),
                    "posted_at": str(row["posted_at"]),
                    "posted_manifest_sha256": str(
                        row["posted_manifest_sha256"]
                    ),
                    "request_manifest_sha256": str(
                        row["request_manifest_sha256"]
                    ),
                    "request_source_revision": str(
                        row["request_source_revision"]
                    ),
                    "basis_digest": str(domain.get("basis_digest") or ""),
                    "queue_id": queue_id,
                    "stable_source_id": stable,
                    "event_revision": revision,
                }
            )
        documents.sort(key=lambda item: (item["posted_at"], item["document_id"]))
        if ids and sorted(item["document_id"] for item in documents) != ids:
            blockers.append("exact reviewed document set is missing or changed")
        selected_total = sum(
            (Decimal(item["amount_rub"]) for item in documents), ZERO
        )
        city_scope: dict[str, dict[str, Any]] = {}
        for city in sorted({item["city"] for item in documents}):
            selected = [item for item in documents if item["city"] == city]
            city_scope[city] = {
                "document_count": len(selected),
                "amount_rub": _money(
                    sum((Decimal(item["amount_rub"]) for item in selected), ZERO)
                ),
            }
        expected_city_public = {
            city: {
                "document_count": int(expected["document_count"]),
                "amount_rub": _money(expected["amount_rub"]),
            }
            for city, expected in EXPECTED_CITY_SCOPE.items()
        }
        if len(documents) != EXPECTED_DOCUMENT_COUNT:
            blockers.append(
                f"expected exactly {EXPECTED_DOCUMENT_COUNT} FBS overhead documents"
            )
        if selected_total != EXPECTED_TOTAL_RUB:
            blockers.append("selected five-document capital total is not exact")
        if city_scope != expected_city_public:
            blockers.append("Moscow/Orenburg document scope is not exact")
        affected_nm_ids = sorted(
            {nm_id for item in documents for nm_id in item["nm_ids"]}
        )
        active = conn.execute(
            "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active "
            "WHERE slot=1"
        ).fetchone()
        if active is None:
            blockers.append("active aggregate warehouse version is missing")
            version_id = ""
        else:
            version_id = str(active["version_id"])
        selected_delta: dict[int, Decimal] = {nm_id: ZERO for nm_id in affected_nm_ids}
        for document in documents:
            rows = conn.execute(
                f"SELECT nm_id,capital_rub FROM {DOCUMENT_LINES_TABLE} "
                "WHERE document_id=? ORDER BY line_no",
                (document["document_id"],),
            ).fetchall()
            for line in rows:
                nm_id = int(line["nm_id"])
                selected_delta[nm_id] += Decimal(str(line["capital_rub"]))
        target_projection: list[dict[str, Any]] = []
        projection_states: set[str] = set()
        for nm_id in affected_nm_ids:
            detail_rows = conn.execute(
                f"SELECT quantity,capital_rub FROM {BALANCES_TABLE} "
                "WHERE nm_id=? ORDER BY facility_id,pool",
                (nm_id,),
            ).fetchall()
            aggregate = conn.execute(
                "SELECT quantity,capital_rub,wac_rub FROM "
                "sheet_vitrina_v1_warehouse_functional_balances "
                "WHERE version_id=? AND warehouse_key='ff' AND nm_id=?",
                (version_id, nm_id),
            ).fetchone()
            if aggregate is None:
                blockers.append(f"aggregate FF row is missing for nmID {nm_id}")
                continue
            detail_quantity = sum(int(row["quantity"]) for row in detail_rows)
            aggregate_quantity = int(Decimal(str(aggregate["quantity"])))
            detail_capital = sum(
                (Decimal(str(row["capital_rub"])) for row in detail_rows), ZERO
            )
            aggregate_capital = Decimal(str(aggregate["capital_rub"]))
            selected_document_delta = selected_delta[nm_id]
            raw_difference = detail_capital - aggregate_capital
            if detail_quantity <= 0 or aggregate_quantity != detail_quantity:
                blockers.append(
                    f"quantity parity is not exact for nmID {nm_id}"
                )
            if _capital_equal(detail_capital, aggregate_capital):
                projection_state = "already_current"
                aggregate_write_required = False
                aggregate_write_delta = ZERO
            elif _capital_equal(raw_difference, selected_document_delta):
                projection_state = "selected_capital_pending"
                aggregate_write_required = True
                aggregate_write_delta = raw_difference
            else:
                projection_state = "ambiguous_capital"
                aggregate_write_required = False
                aggregate_write_delta = ZERO
                blockers.append(
                    "aggregate capital is neither already current nor behind by "
                    f"the selected documents for nmID {nm_id}"
                )
            projection_states.add(projection_state)
            with localcontext() as context:
                context.prec = 160
                wac = (
                    detail_capital / Decimal(detail_quantity)
                    if detail_quantity > 0
                    else ZERO
                )
            target_projection.append(
                {
                    "version_id": version_id,
                    "nm_id": nm_id,
                    "aggregate_quantity": aggregate_quantity,
                    "detail_quantity": detail_quantity,
                    "aggregate_capital_rub": _decimal_text(aggregate_capital),
                    "detail_capital_rub": _decimal_text(detail_capital),
                    "raw_capital_difference_rub": _decimal_text(raw_difference),
                    "selected_document_delta_rub": _decimal_text(
                        selected_document_delta
                    ),
                    "projection_state": projection_state,
                    "aggregate_write_required": aggregate_write_required,
                    "aggregate_write_delta_rub": _decimal_text(
                        aggregate_write_delta
                    ),
                    "capital_comparison": CAPITAL_COMPARISON,
                    "detail_wac_rub": _decimal_text(wac),
                }
            )
        queues = [
            {
                "queue_id": item["queue_id"],
                "stable_source_id": item["stable_source_id"],
                "source_revision": item["event_revision"],
                "effective_date": BUSINESS_DATE,
                "affected_nm_ids": item["nm_ids"],
                "requested_at": item["posted_at"],
            }
            for item in documents
        ]
        queue_rows = _queue_rows_in_connection(conn, queues)
        queues = [
            {
                **queue,
                "identity_state": selected["state"],
                "publication_state": _queue_publication_state(selected),
            }
            for queue, selected in zip(queues, queue_rows, strict=True)
        ]
        queue_insert_count = sum(1 for item in queue_rows if item["state"] == "missing")
        conflicts = [item for item in queue_rows if item["state"] == "conflict"]
        if conflicts:
            blockers.append("one or more canonical queue identities conflict")
        if any(
            item["aggregate_write_required"] for item in target_projection
        ) and any(item["state"] == "present" for item in queue_rows):
            blockers.append(
                "pending aggregate capital conflicts with an existing publication queue"
            )
        document_ids = [item["document_id"] for item in documents]
        stable_ids = [item["stable_source_id"] for item in documents]
        non_target = {
            "pool_balances": _query_digest(
                conn,
                f"SELECT * FROM {BALANCES_TABLE} ORDER BY facility_id,pool,nm_id",
            ),
            "aggregate_non_target": _rows_digest(
                [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances "
                        "WHERE version_id=? AND warehouse_key='ff' ORDER BY nm_id",
                        (version_id,),
                    ).fetchall()
                    if int(row["nm_id"]) not in set(affected_nm_ids)
                ]
            ),
            "queue_non_target": _rows_digest(
                [
                    dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM {TARGETED_RECALC_QUEUE_TABLE} "
                        "ORDER BY queue_id"
                    ).fetchall()
                    if str(row["stable_source_id"]) not in set(stable_ids)
                ]
            ),
        }
        document_rows = []
        for document_id in document_ids:
            document_rows.extend(
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM {DOCUMENTS_TABLE} WHERE document_id=?",
                    (document_id,),
                ).fetchall()
            )
            document_rows.extend(
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM {DOCUMENT_LINES_TABLE} WHERE document_id=? "
                    "ORDER BY line_no",
                    (document_id,),
                ).fetchall()
            )
        lifecycle_rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM {EVENTS_TABLE} ORDER BY event_id"
            ).fetchall()
            if int(row["nm_id"]) in set(affected_nm_ids)
        ]
        quantities = [
            [row["nm_id"], row["quantity"]]
            for row in conn.execute(
                "SELECT nm_id,quantity FROM "
                "sheet_vitrina_v1_warehouse_functional_balances "
                "WHERE version_id=? AND warehouse_key='ff' ORDER BY nm_id",
                (version_id,),
            ).fetchall()
        ]
        return {
            "documents": documents,
            "affected_nm_ids": affected_nm_ids,
            "queues": queues,
            "queue_rows": [
                dict(item["row"]) for item in queue_rows if item["row"] is not None
            ],
            "queue_insert_count": queue_insert_count,
            "target_projection": target_projection,
            "target_projection_digest": _fingerprint(target_projection),
            "pool_balance_digest": non_target["pool_balances"],
            "lifecycle_digest": _fingerprint(lifecycle_rows),
            "document_digest": _fingerprint(document_rows),
            "quantity_digest": _fingerprint(quantities),
            "non_target_digest": _fingerprint(non_target),
            "selected_amount_rub": _money(selected_total),
            "city_scope": city_scope,
            "aggregate_pre_state": (
                "ambiguous"
                if "ambiguous_capital" in projection_states
                else "mixed"
                if len(projection_states) > 1
                else next(iter(projection_states), "empty")
            ),
            "blockers": sorted(set(blockers)),
        }


def _queue_rows_in_connection(
    conn: sqlite3.Connection, queues: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    result = []
    for item in queues:
        row = conn.execute(
            f"SELECT * FROM {TARGETED_RECALC_QUEUE_TABLE} "
            "WHERE stable_source_id=? OR queue_id=? ORDER BY queue_id",
            (str(item["stable_source_id"]), str(item["queue_id"])),
        ).fetchall()
        if not row:
            result.append({"state": "missing", "row": None, **dict(item)})
            continue
        if len(row) != 1:
            result.append({"state": "conflict", "row": None, **dict(item)})
            continue
        stored = dict(row[0])
        expected_nm = list(item["affected_nm_ids"])
        valid = (
            str(stored["queue_id"]) == str(item["queue_id"])
            and str(stored["stable_source_id"])
            == str(item["stable_source_id"])
            and str(stored["source_revision"]) == str(item["source_revision"])
            and _loads(stored["affected_nm_ids_json"], []) == expected_nm
        )
        result.append(
            {
                "state": "present" if valid else "conflict",
                "row": stored,
                **dict(item),
            }
        )
    return result


def _queue_publication_state(selected: Mapping[str, Any]) -> str:
    if selected.get("state") != "present":
        return str(selected.get("state") or "missing")
    row = dict(selected.get("row") or {})
    if (
        str(row.get("status") or "") == "complete"
        and str(row.get("economics_status") or "") == "complete"
        and str(row.get("finance_status") or "") == "complete"
        and str(row.get("finance_source_fingerprint") or "").startswith(
            "sha256:"
        )
    ):
        return "complete"
    return "publication_pending"


def _exact_queue_rows(
    db_path: Path, queues: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    with closing(_open_query_only(db_path)) as conn:
        selected = _queue_rows_in_connection(conn, queues)
    if any(item["state"] != "present" for item in selected):
        raise FfPoolOverheadBackfillError(
            "exact overhead queue identities are missing or conflicting"
        )
    return [dict(item["row"]) for item in selected]


def _short_apply_already_complete(
    *, current: Mapping[str, Any], reviewed_plan: Mapping[str, Any]
) -> bool:
    expected = list(reviewed_plan["pre_change"]["target_projection"])
    actual = {int(item["nm_id"]): item for item in current["target_projection"]}
    projection_complete = all(
        int(item["nm_id"]) in actual
        and _capital_equal(
            Decimal(
                str(actual[int(item["nm_id"])]["aggregate_capital_rub"])
            ),
            Decimal(str(item["detail_capital_rub"])),
        )
        and int(actual[int(item["nm_id"])]["aggregate_quantity"])
        == int(item["detail_quantity"])
        for item in expected
    )
    return (
        projection_complete
        and len(current["queue_rows"]) == EXPECTED_DOCUMENT_COUNT
        and all(
            item.get("identity_state") == "present"
            for item in current["queues"]
        )
    )


def _validate_reviewed_plan(
    reviewed_plan: Mapping[str, Any],
    *,
    fingerprint: str,
    deployed_sha: str,
    approval_reference: str,
    actor: str,
) -> None:
    if (
        reviewed_plan.get("contract_name") != CONTRACT_NAME
        or int(reviewed_plan.get("contract_version") or 0) != CONTRACT_VERSION
        or reviewed_plan.get("mode") != "dry_run"
        or reviewed_plan.get("apply_allowed") is not True
        or reviewed_plan.get("deployed_sha") != deployed_sha
        or reviewed_plan.get("fingerprint") != fingerprint
        or reviewed_plan.get("blockers")
        or len(reviewed_plan.get("scope", {}).get("document_ids") or [])
        != EXPECTED_DOCUMENT_COUNT
        or reviewed_plan.get("expected_effects", {}).get(
            "selected_document_amount_rub"
        )
        != _money(EXPECTED_TOTAL_RUB)
        or Decimal(
            str(
                reviewed_plan.get("expected_effects", {}).get(
                    "aggregate_capital_rewrite_rub", "-1"
                )
            )
        )
        < ZERO
    ):
        raise FfPoolOverheadBackfillError(
            "reviewed plan does not match the exact five-document scope"
        )
    if _fingerprint(_fingerprint_material(reviewed_plan)) != fingerprint:
        raise FfPoolOverheadBackfillError("reviewed plan fingerprint is invalid")
    if not str(approval_reference or "").strip():
        raise FfPoolOverheadBackfillError("exact apply approval reference is required")
    if not str(actor or "").strip():
        raise FfPoolOverheadBackfillError("apply actor is required")


def _mark_publication_error(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    queue_ids: list[str],
    occurred_at: str,
    error: str,
) -> None:
    bounded_error = str(error or "publication error")[:2000]
    try:
        mark_ff_replay_economics(
            runtime,
            queue_ids=queue_ids,
            status="error",
            occurred_at=occurred_at,
            error=bounded_error,
        )
        mark_ff_replay_finance(
            runtime,
            queue_ids=queue_ids,
            status="error",
            occurred_at=occurred_at,
            error=bounded_error,
        )
        FfPoolDocumentService(
            db_path=runtime.db_path,
            runtime_dir=runtime.runtime_dir,
            resume=False,
        ).resume_incomplete()
    except Exception:
        # Preserve the original publication failure. The queue's prior durable
        # stage is still authoritative if this best-effort error annotation
        # itself loses a concurrent CAS.
        pass


def _assert_complete_readback(readback: Mapping[str, Any]) -> None:
    if (
        readback.get("status") != "complete"
        or readback.get("projection_current") is not True
        or readback.get("quantity_unchanged") is not True
        or readback.get("capital_conserved") is not True
        or readback.get("past_fulfilled_lifecycle_unchanged") is not True
        or readback.get("documents_unchanged") is not True
        or readback.get("non_target_unchanged") is not True
        or readback.get("pre_change_invariants_verified") is not True
        or readback.get("no_duplicate_submit") is not True
    ):
        raise FfPoolOverheadBackfillError(
            "post-apply query-only reconciliation is incomplete"
        )


def _ensure_backup(
    runtime: RegistryUploadDbBackedRuntime,
    destination: Path,
    *,
    manifest_fingerprint: str,
    deployed_sha: str,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = destination.with_name(destination.name + ".receipt.json")
    if not destination.exists():
        if receipt_path.exists():
            raise FfPoolOverheadBackfillError(
                "backup receipt exists without its exact database"
            )
        runtime.backup_database(destination)
        evidence = _backup_file_evidence(destination)
        receipt = {
            "contract_name": CONTRACT_NAME,
            "manifest_fingerprint": manifest_fingerprint,
            "deployed_sha": deployed_sha,
            **evidence,
        }
        receipt["receipt_fingerprint"] = _fingerprint(receipt)
        _write_private_json(receipt_path, receipt)
        return {**evidence, "receipt_path": str(receipt_path), "reused": False}
    if not receipt_path.is_file():
        raise FfPoolOverheadBackfillError(
            "pre-existing backup has no exact manifest-bound receipt"
        )
    receipt = _read_json(receipt_path)
    receipt_fingerprint = str(receipt.get("receipt_fingerprint") or "")
    if (
        receipt.get("contract_name") != CONTRACT_NAME
        or receipt.get("manifest_fingerprint") != manifest_fingerprint
        or receipt.get("deployed_sha") != deployed_sha
        or receipt_fingerprint
        != _fingerprint(
            {key: value for key, value in receipt.items() if key != "receipt_fingerprint"}
        )
    ):
        raise FfPoolOverheadBackfillError(
            "retained backup receipt does not match this exact manifest"
        )
    evidence = _backup_file_evidence(destination)
    if (
        evidence["sha256"] != receipt.get("sha256")
        or evidence["size_bytes"] != receipt.get("size_bytes")
    ):
        raise FfPoolOverheadBackfillError("retained backup bytes drifted")
    return {**evidence, "receipt_path": str(receipt_path), "reused": True}


def _backup_file_evidence(destination: Path) -> dict[str, Any]:
    with sqlite3.connect(
        f"file:{destination.resolve()}?mode=ro", uri=True
    ) as conn:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise FfPoolOverheadBackfillError("retained backup integrity check failed")
    digest = hashlib.sha256()
    size = 0
    with destination.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return {
        "path": str(destination),
        "size_bytes": size,
        "sha256": digest.hexdigest(),
        "integrity_check": "ok",
    }


def _external_directory(path: Path, *, name: str) -> Path:
    raw = Path(path)
    if not raw.is_absolute():
        raise FfPoolOverheadBackfillError(f"{name} must be absolute")
    target = raw.resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    import os

    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FfPoolOverheadBackfillError("evidence JSON must be an object")
    return value


def _open_query_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=30
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        conn.close()
        raise FfPoolOverheadBackfillError("query-only SQLite preflight failed")
    return conn


def _fingerprint_material(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key not in {"fingerprint", "generated_at"}
    }


def _query_digest(conn: sqlite3.Connection, query: str) -> str:
    return _rows_digest([dict(row) for row in conn.execute(query).fetchall()])


def _rows_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    return _fingerprint([dict(row) for row in rows])


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _capital_equal(left: Decimal, right: Decimal) -> bool:
    with localcontext() as context:
        context.prec = 160
        return left.quantize(
            CAPITAL_MINOR_UNIT,
            rounding=ROUND_HALF_UP,
        ) == right.quantize(
            CAPITAL_MINOR_UNIT,
            rounding=ROUND_HALF_UP,
        )


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
