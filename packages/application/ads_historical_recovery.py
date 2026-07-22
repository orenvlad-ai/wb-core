"""Production-safe historical recovery for persisted ``ads_compact`` slots.

The recovery deliberately owns only absent accepted closed-day slots.  It
never turns an incomplete upstream answer into zero and never overwrites an
existing temporal snapshot.  The public CLI keeps apply behind an exact
dry-run fingerprint, a fresh approval reference and a verified SQLite backup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from packages.application.ads_snapshot_payload import resolve_ads_snapshot_payload


SCHEMA_VERSION = "ads_historical_recovery_v3"
SOURCE_KEY = "ads_compact"
SNAPSHOT_ROLE = "accepted_closed_day_snapshot"
CLOSURE_SLOT = "yesterday_closed"
ALLOWED_CAMPAIGN_STATUSES = frozenset({7, 9, 11})
MAX_WINDOW_DAYS = 31
MAX_IDS_PER_REQUEST = 50
REQUESTS_PER_MINUTE = 3
MIN_REQUEST_INTERVAL_SECONDS = 60.0 / REQUESTS_PER_MINUTE
BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
DEFAULT_NM_IDS = (245720334,)
DEFAULT_TARGET_DATES = tuple(
    day
    for day in (
        date(2025, 12, 29) + timedelta(days=offset)
        for offset in range((date(2026, 2, 28) - date(2025, 12, 29)).days + 1)
    )
    if day != date(2026, 1, 1)
)


class AdsHistoricalRecoveryError(RuntimeError):
    """Fail-closed recovery error."""


class AdsHistoricalNoStatisticsError(AdsHistoricalRecoveryError):
    """Official singleton ``fullstats`` confirmed no statistics for the window."""


class AdsHistoricalSource(Protocol):
    """Read-only official-history source; tests provide a deterministic fake."""

    min_request_interval_seconds: float

    def list_campaigns(self) -> Any:
        """Return the official campaign-count payload."""

    def fetch_fullstats(
        self, *, campaign_ids: Sequence[int], date_from: date, date_to: date
    ) -> Any:
        """Return one official fullstats response for the exact request."""


@dataclass(frozen=True)
class AdsHistoricalRecoveryScope:
    nm_ids: tuple[int, ...]
    target_dates: tuple[date, ...]

    @classmethod
    def build(
        cls, *, nm_ids: Sequence[int], target_dates: Sequence[date]
    ) -> "AdsHistoricalRecoveryScope":
        normalized_nm_ids = tuple(sorted({int(value) for value in nm_ids}))
        normalized_dates = tuple(sorted(set(target_dates)))
        if not normalized_nm_ids or any(value <= 0 for value in normalized_nm_ids):
            raise ValueError("recovery requires one or more positive exact nmID values")
        if len(normalized_nm_ids) > MAX_IDS_PER_REQUEST:
            raise ValueError(f"recovery nmID scope exceeds {MAX_IDS_PER_REQUEST}")
        if not normalized_dates:
            raise ValueError("recovery requires one or more exact target dates")
        if len(normalized_dates) > 366:
            raise ValueError("recovery date scope is not bounded (maximum 366 exact dates)")
        return cls(nm_ids=normalized_nm_ids, target_dates=normalized_dates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "nm_ids": list(self.nm_ids),
            "target_dates": [value.isoformat() for value in self.target_dates],
        }


def canonical_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def create_coherent_sqlite_backup(
    db_path: Path, backup_dir: Path, *, fingerprint: str
) -> dict[str, Any]:
    """Create and verify a coherent mode-0600 backup before any apply write."""

    if not db_path.is_file():
        raise AdsHistoricalRecoveryError(f"runtime SQLite database is absent: {db_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    source_size = db_path.stat().st_size
    free_bytes = shutil.disk_usage(backup_dir).free
    required_free_bytes = max(source_size * 2, 16 * 1024 * 1024)
    if free_bytes < required_free_bytes:
        raise AdsHistoricalRecoveryError(
            "not enough free space for coherent SQLite backup: "
            f"free={free_bytes}, required={required_free_bytes}"
        )
    suffix = fingerprint.removeprefix("sha256:")[:16]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"ads-historical-{stamp}-{suffix}.sqlite3"
    if backup_path.exists():
        raise AdsHistoricalRecoveryError(f"backup already exists: {backup_path}")
    source_uri = f"file:{db_path.resolve()}?mode=ro"
    try:
        descriptor = os.open(
            backup_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(descriptor)
        os.chmod(backup_path, 0o600)
        with sqlite3.connect(source_uri, uri=True, timeout=60) as source:
            with sqlite3.connect(backup_path, timeout=60) as target:
                source.backup(target)
        with sqlite3.connect(
            f"file:{backup_path.resolve()}?mode=ro", uri=True, timeout=60
        ) as verify:
            integrity = str(verify.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise AdsHistoricalRecoveryError(
                f"backup integrity_check failed: {integrity}"
            )
        sha256 = hashlib.sha256()
        with backup_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                sha256.update(chunk)
        return {
            "created": True,
            "path": str(backup_path),
            "size_bytes": backup_path.stat().st_size,
            "source_size_bytes": source_size,
            "free_space_before_bytes": free_bytes,
            "required_free_bytes": required_free_bytes,
            "permissions": format(backup_path.stat().st_mode & 0o777, "04o"),
            "integrity_check": integrity,
            "sha256": f"sha256:{sha256.hexdigest()}",
        }
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise


class AdsHistoricalRecovery:
    """Plan, apply and reconcile an exact set of missing historical slots."""

    def __init__(
        self,
        *,
        db_path: Path,
        source: AdsHistoricalSource,
        now_factory: Callable[[], datetime] | None = None,
        failure_injector: Callable[[int], None] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.source = source
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.failure_injector = failure_injector

    def plan(self, scope: AdsHistoricalRecoveryScope) -> dict[str, Any]:
        """Build a deterministic read-only plan; upstream errors become blockers."""

        with self._connect(read_only=True) as conn:
            self._require_schema(conn)
            target_before = self._target_state_manifest(conn, scope)
            now = self.now_factory()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            business_today = now.astimezone(BUSINESS_TIMEZONE).date()
            unclosed_dates = {
                value for value in scope.target_dates if value >= business_today
            }
            missing_dates = tuple(
                value
                for value in scope.target_dates
                if value not in unclosed_dates
                and value.isoformat() not in target_before["snapshot_dates"]
            )
            non_target_manifest = self._non_target_manifest(conn, scope)
            blockers: list[dict[str, Any]] = [
                {
                    "code": "ads_target_date_not_closed",
                    "date": value.isoformat(),
                    "business_today": business_today.isoformat(),
                    "timezone": str(BUSINESS_TIMEZONE),
                }
                for value in sorted(unclosed_dates)
            ]
            closure_by_date = {
                str(item["target_date"]): str(item["state"])
                for item in target_before["closures"]
            }
            for existing in target_before["snapshots"]:
                day_text = str(existing["snapshot_date"])
                kind = str(existing["payload_kind"])
                if kind not in {"success", "empty"}:
                    blockers.append(
                        {
                            "code": "ads_existing_payload_not_accepted",
                            "date": day_text,
                            "kind": kind,
                        }
                    )
                if not existing["items_valid"]:
                    blockers.append(
                        {
                            "code": "ads_existing_payload_items_invalid",
                            "date": day_text,
                        }
                    )
                if not existing["count_matches"]:
                    blockers.append(
                        {
                            "code": "ads_existing_payload_count_mismatch",
                            "date": day_text,
                            "payload_count": existing["payload_count"],
                            "item_count": existing["item_count"],
                        }
                    )
                if kind == "empty" and existing["item_count"]:
                    blockers.append(
                        {
                            "code": "ads_existing_empty_payload_has_items",
                            "date": day_text,
                        }
                    )
                if kind == "success" and existing["missing_nm_ids"]:
                    blockers.append(
                        {
                            "code": "ads_existing_sku_coverage_missing",
                            "date": day_text,
                            "missing_nm_ids": existing["missing_nm_ids"],
                        }
                    )
                if closure_by_date.get(day_text) != "success":
                    blockers.append(
                        {
                            "code": "ads_existing_closure_not_success",
                            "date": day_text,
                            "closure_state": closure_by_date.get(day_text),
                        }
                    )
            source_manifest: dict[str, Any] = {
                "status": "not_needed",
                "campaigns": [],
                "campaign_digest": canonical_digest([]),
                "requests": [],
                "response_digest": canonical_digest([]),
                "source_digest": canonical_digest(
                    {
                        "campaign_digest": canonical_digest([]),
                        "response_digest": canonical_digest([]),
                    }
                ),
            }
            candidates: list[dict[str, Any]] = []
            source_min_interval = float(
                getattr(self.source, "min_request_interval_seconds", 0.0)
            )
            if (
                not math.isfinite(source_min_interval)
                or source_min_interval < MIN_REQUEST_INTERVAL_SECONDS
            ):
                blockers.append(
                    {
                        "code": "ads_rate_limit_contract_unsafe",
                        "required_minimum_seconds": MIN_REQUEST_INTERVAL_SECONDS,
                        "actual_seconds": source_min_interval,
                    }
                )
            elif missing_dates:
                try:
                    source_manifest, candidates, source_blockers = self._source_plan(
                        scope=scope, target_dates=missing_dates
                    )
                    blockers.extend(source_blockers)
                except Exception as exc:
                    blockers.append(
                        {
                            "code": "ads_upstream_incomplete",
                            "detail": str(exc),
                        }
                    )
                    source_manifest = {
                        **source_manifest,
                        "status": "incomplete",
                        "error_type": type(exc).__name__,
                    }

            candidate_by_date = {row["snapshot_date"]: row for row in candidates}
            target_rows: list[dict[str, Any]] = []
            for value in scope.target_dates:
                date_text = value.isoformat()
                existing = target_before["snapshots_by_date"].get(date_text)
                if existing is not None:
                    target_rows.append(
                        {
                            "snapshot_date": date_text,
                            "action": "skip_existing",
                            "existing_payload_digest": existing["payload_digest"],
                        }
                    )
                elif date_text in candidate_by_date:
                    target_rows.append(candidate_by_date[date_text])
                else:
                    target_rows.append(
                        {
                            "snapshot_date": date_text,
                            "action": "blocked",
                            "reason": "no_confirmed_success_or_global_empty_payload",
                        }
                    )
            for row in target_rows:
                if row["action"] == "blocked" and not any(
                    item.get("date") == row["snapshot_date"] for item in blockers
                ):
                    blockers.append(
                        {
                            "code": "ads_date_unconfirmed",
                            "date": row["snapshot_date"],
                        }
                    )

            write_rows = [row for row in target_rows if row["action"] == "insert"]
            scope_digest = canonical_digest(scope.as_dict())
            integration_contract = {
                "allowed_campaign_statuses": sorted(ALLOWED_CAMPAIGN_STATUSES),
                "maximum_window_days_inclusive": MAX_WINDOW_DAYS,
                "maximum_campaign_ids_per_request": MAX_IDS_PER_REQUEST,
                "maximum_requests_per_minute": REQUESTS_PER_MINUTE,
                "minimum_request_interval_seconds": MIN_REQUEST_INTERVAL_SECONDS,
                "source_min_request_interval_seconds": source_min_interval,
            }
            plan_core = {
                "schema_version": SCHEMA_VERSION,
                "scope": scope.as_dict(),
                "scope_digest": scope_digest,
                "integration_contract": integration_contract,
                "source_manifest": source_manifest,
                "target_before_digest": canonical_digest(target_before),
                "target_manifest": target_rows,
                "non_target_manifest": non_target_manifest,
                "non_target_digest": canonical_digest(non_target_manifest),
                "write_set": {
                    "tables": [
                        "temporal_source_slot_snapshots",
                        "temporal_source_closure_state",
                        "ads_historical_recovery_audit",
                    ],
                    "source_key": SOURCE_KEY,
                    "snapshot_role": SNAPSHOT_ROLE,
                    "slot_kind": CLOSURE_SLOT,
                    "insert_snapshot_count": len(write_rows),
                    "target_dates": [row["snapshot_date"] for row in write_rows],
                    "existing_slots_never_overwritten": True,
                },
                "blockers": blockers,
            }
            fingerprint = canonical_digest(plan_core)
            return {
                "status": "blocked" if blockers else "ready",
                "dry_run": True,
                **plan_core,
                "fingerprint": fingerprint,
                "apply_allowed": not blockers,
                "human_approval_required": bool(write_rows),
                "backup_recovery_plan": {
                    "required_before_apply": bool(write_rows),
                    "coherent_sqlite_backup": True,
                    "permissions": "0600",
                    "integrity_check": "ok required",
                    "sha256": "required",
                    "transaction": "single BEGIN IMMEDIATE with rollback on drift/error",
                },
            }

    def apply(
        self,
        scope: AdsHistoricalRecoveryScope,
        *,
        expected_fingerprint: str,
        approval_reference: str,
        backup_dir: Path,
    ) -> dict[str, Any]:
        """Apply one reviewed plan with backup, transaction and exact readback."""

        expected_fingerprint = str(expected_fingerprint or "").strip()
        approval_reference = str(approval_reference or "").strip()
        if not expected_fingerprint:
            raise AdsHistoricalRecoveryError("apply requires an exact plan fingerprint")
        if not approval_reference:
            raise AdsHistoricalRecoveryError("apply requires a fresh approval reference")

        prior = self._read_prior_audit(expected_fingerprint)
        if prior is not None:
            if prior.get("scope") != scope.as_dict():
                raise AdsHistoricalRecoveryError(
                    "previously applied fingerprint belongs to a different exact scope"
                )
            readback = self.readback(scope)
            if readback["snapshot_digest"] != prior.get("snapshot_digest"):
                raise AdsHistoricalRecoveryError(
                    "previously applied ads recovery has drifted; new dry-run is required"
                )
            return {
                **prior,
                "status": "no_op_already_applied",
                "idempotent": True,
                "readback": readback,
                "backup": prior.get("backup"),
                "backup_created_this_attempt": False,
            }

        plan = self.plan(scope)
        if plan["fingerprint"] != expected_fingerprint:
            raise AdsHistoricalRecoveryError(
                "ads historical recovery fingerprint drifted before apply"
            )
        if not plan["apply_allowed"]:
            raise AdsHistoricalRecoveryError(
                "ads historical recovery dry-run contains blockers"
            )
        if plan["write_set"]["insert_snapshot_count"] == 0:
            return {
                "status": "no_op_no_missing_slots",
                "fingerprint": expected_fingerprint,
                "scope": scope.as_dict(),
                "idempotent": True,
                "backup": None,
                "readback": self.readback(scope),
            }

        backup = create_coherent_sqlite_backup(
            self.db_path, Path(backup_dir), fingerprint=expected_fingerprint
        )
        with self._connect(read_only=False) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                locked_target_before = self._target_state_manifest(conn, scope)
                if canonical_digest(locked_target_before) != plan["target_before_digest"]:
                    raise AdsHistoricalRecoveryError(
                        "target ads slots drifted after dry-run and backup"
                    )
                locked_non_target = self._non_target_manifest(conn, scope)
                if canonical_digest(locked_non_target) != plan["non_target_digest"]:
                    raise AdsHistoricalRecoveryError(
                        "non-target temporal source state drifted after dry-run and backup"
                    )
                now = self._now_text()
                inserted = 0
                for row in plan["target_manifest"]:
                    if row["action"] != "insert":
                        continue
                    cursor = conn.execute(
                        """INSERT INTO temporal_source_slot_snapshots(
                           source_key,snapshot_date,snapshot_role,captured_at,payload_json
                           ) VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING""",
                        (
                            SOURCE_KEY,
                            row["snapshot_date"],
                            SNAPSHOT_ROLE,
                            now,
                            row["payload_json"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise AdsHistoricalRecoveryError(
                            "existing temporal slot refused overwrite during apply: "
                            + row["snapshot_date"]
                        )
                    conn.execute(
                        """INSERT INTO temporal_source_closure_state(
                           source_key,target_date,slot_kind,state,attempt_count,next_retry_at,
                           last_reason,last_attempt_at,last_success_at,accepted_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(source_key,target_date,slot_kind) DO UPDATE SET
                           state=excluded.state,
                           attempt_count=temporal_source_closure_state.attempt_count+1,
                           next_retry_at=NULL,
                           last_reason=excluded.last_reason,
                           last_attempt_at=excluded.last_attempt_at,
                           last_success_at=excluded.last_success_at,
                           accepted_at=excluded.accepted_at""",
                        (
                            SOURCE_KEY,
                            row["snapshot_date"],
                            CLOSURE_SLOT,
                            "success",
                            1,
                            None,
                            f"{SCHEMA_VERSION}:{row['payload_kind']}",
                            now,
                            now,
                            now,
                        ),
                    )
                    inserted += 1
                    if self.failure_injector is not None:
                        self.failure_injector(inserted)

                transactional_readback = self._readback_conn(conn, scope)
                if transactional_readback["blockers"]:
                    raise AdsHistoricalRecoveryError(
                        "transactional ads readback contains blockers: "
                        + canonical_digest(transactional_readback["blockers"])
                    )
                expected_payloads = {
                    row["snapshot_date"]: row["payload_digest"]
                    for row in plan["target_manifest"]
                    if row["action"] == "insert"
                }
                actual_payloads = {
                    row["snapshot_date"]: row["payload_digest"]
                    for row in transactional_readback["snapshots"]
                }
                for target_date, expected_digest in expected_payloads.items():
                    if actual_payloads.get(target_date) != expected_digest:
                        raise AdsHistoricalRecoveryError(
                            f"target ads readback differs from plan for {target_date}"
                        )
                if any(
                    row["snapshot_date"] in expected_payloads
                    and row.get("closure_state") != "success"
                    for row in transactional_readback["snapshots"]
                ):
                    raise AdsHistoricalRecoveryError(
                        "target ads closure readback is not successful"
                    )
                non_target_after = self._non_target_manifest(conn, scope)
                if canonical_digest(non_target_after) != plan["non_target_digest"]:
                    raise AdsHistoricalRecoveryError(
                        "non-target temporal source state changed during apply"
                    )

                conn.execute(
                    """CREATE TABLE IF NOT EXISTS ads_historical_recovery_audit(
                       fingerprint TEXT PRIMARY KEY,
                       schema_version TEXT NOT NULL,
                       scope_json TEXT NOT NULL,
                       approval_reference TEXT NOT NULL,
                       source_digest TEXT NOT NULL,
                       non_target_digest TEXT NOT NULL,
                       snapshot_digest TEXT NOT NULL,
                       result_json TEXT NOT NULL,
                       created_at TEXT NOT NULL
                       )"""
                )
                result = {
                    "status": "applied",
                    "fingerprint": expected_fingerprint,
                    "schema_version": SCHEMA_VERSION,
                    "scope": scope.as_dict(),
                    "approval_reference": approval_reference,
                    "inserted_snapshot_count": inserted,
                    "source_digest": plan["source_manifest"]["source_digest"],
                    "non_target_digest_before": plan["non_target_digest"],
                    "non_target_digest_after": canonical_digest(non_target_after),
                    "snapshot_digest": transactional_readback["snapshot_digest"],
                    "backup": backup,
                    "backup_created_this_attempt": True,
                    "applied_at": now,
                    "idempotent": True,
                }
                conn.execute(
                    """INSERT INTO ads_historical_recovery_audit(
                       fingerprint,schema_version,scope_json,approval_reference,
                       source_digest,non_target_digest,snapshot_digest,result_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        expected_fingerprint,
                        SCHEMA_VERSION,
                        _canonical_json(scope.as_dict()),
                        approval_reference,
                        result["source_digest"],
                        plan["non_target_digest"],
                        result["snapshot_digest"],
                        _canonical_json(result),
                        now,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        final_readback = self.readback(scope)
        if final_readback["snapshot_digest"] != result["snapshot_digest"]:
            raise AdsHistoricalRecoveryError("post-commit ads readback drifted")
        return {**result, "readback": final_readback}

    def readback(self, scope: AdsHistoricalRecoveryScope) -> dict[str, Any]:
        with self._connect(read_only=True) as conn:
            self._require_schema(conn)
            return self._readback_conn(conn, scope)

    def _source_plan(
        self,
        *,
        scope: AdsHistoricalRecoveryScope,
        target_dates: Sequence[date],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        raw_campaigns = self.source.list_campaigns()
        campaigns = _extract_campaigns(raw_campaigns)
        eligible_all = [
            item for item in campaigns if item["status"] in ALLOWED_CAMPAIGN_STATUSES
        ]
        scope_start = min(target_dates)
        eligible: list[dict[str, Any]] = []
        excluded_completed: list[dict[str, Any]] = []
        for item in eligible_all:
            change_date = _optional_iso_date(item.get("change_time"))
            if item["status"] == 7 and change_date is not None and change_date < scope_start:
                excluded_completed.append(
                    {**item, "source_action": "excluded_completed_before_scope"}
                )
            else:
                eligible.append({**item, "source_action": "requested"})
        unsupported_overlaps: list[dict[str, Any]] = []
        for item in campaigns:
            if item["status"] in ALLOWED_CAMPAIGN_STATUSES or item["status"] == 4:
                continue
            change_date = _optional_iso_date(item.get("change_time"))
            if change_date is None or change_date >= scope_start:
                unsupported_overlaps.append(item)
        campaign_ids = sorted({int(item["campaign_id"]) for item in eligible})
        if not eligible_all and not unsupported_overlaps:
            raise AdsHistoricalRecoveryError(
                "official campaign manifest contains no campaigns in statuses 7/9/11; "
                "historical days cannot be confirmed empty"
            )
        source_blockers: list[dict[str, Any]] = []
        if unsupported_overlaps:
            source_blockers.append(
                {
                    "code": "ads_unsupported_campaign_overlaps_scope",
                    "scope_start": scope_start.isoformat(),
                    "campaigns": unsupported_overlaps,
                    "reason": "fullstats supports only campaign statuses 7/9/11",
                }
            )
        windows = _date_windows(target_dates)
        batches = [
            campaign_ids[index : index + MAX_IDS_PER_REQUEST]
            for index in range(0, len(campaign_ids), MAX_IDS_PER_REQUEST)
        ]
        requests: list[dict[str, Any]] = []
        rows_by_date: dict[str, list[Mapping[str, Any]]] = {
            value.isoformat(): [] for value in target_dates
        }
        if batches:
            for start, end in windows:
                for batch in batches:
                    if (end - start).days + 1 > MAX_WINDOW_DAYS:
                        raise AdsHistoricalRecoveryError("fullstats window exceeds 31 days")
                    if len(batch) > MAX_IDS_PER_REQUEST:
                        raise AdsHistoricalRecoveryError("fullstats batch exceeds 50 IDs")
                    try:
                        payload = self.source.fetch_fullstats(
                            campaign_ids=batch,
                            date_from=start,
                            date_to=end,
                        )
                    except AdsHistoricalNoStatisticsError:
                        payload = []
                        batch_outcome = "confirmed_no_statistics_requires_singletons"
                    else:
                        batch_outcome = "success"
                    if not isinstance(payload, list):
                        raise AdsHistoricalRecoveryError(
                            "official fullstats response is not a complete JSON list"
                        )
                    normalized, seen_campaign_ids = _fullstats_rows(
                        payload,
                        start=start,
                        end=end,
                        allowed_campaign_ids=set(batch),
                        require_all_campaigns=False,
                    )
                    for day_text, day_rows in normalized.items():
                        if day_text in rows_by_date:
                            rows_by_date[day_text].extend(day_rows)
                    requests.append(
                        {
                            "mode": "batch",
                            "outcome": batch_outcome,
                            "date_from": start.isoformat(),
                            "date_to": end.isoformat(),
                            "window_days": (end - start).days + 1,
                            "campaign_ids": list(batch),
                            "campaign_id_count": len(batch),
                            "response_digest": canonical_digest(payload),
                            "normalized_row_count": sum(
                                len(value) for value in normalized.values()
                            ),
                        }
                    )
                    for campaign_id in sorted(set(batch) - seen_campaign_ids):
                        try:
                            singleton_payload = self.source.fetch_fullstats(
                                campaign_ids=[campaign_id],
                                date_from=start,
                                date_to=end,
                            )
                        except AdsHistoricalNoStatisticsError:
                            requests.append(
                                {
                                    "mode": "singleton_confirmation",
                                    "outcome": "confirmed_no_statistics",
                                    "date_from": start.isoformat(),
                                    "date_to": end.isoformat(),
                                    "window_days": (end - start).days + 1,
                                    "campaign_ids": [campaign_id],
                                    "campaign_id_count": 1,
                                    "response_digest": canonical_digest(
                                        {
                                            "campaign_id": campaign_id,
                                            "date_from": start.isoformat(),
                                            "date_to": end.isoformat(),
                                            "result": "official_no_statistics",
                                        }
                                    ),
                                    "normalized_row_count": 0,
                                }
                            )
                            continue
                        if not isinstance(singleton_payload, list):
                            raise AdsHistoricalRecoveryError(
                                "official singleton fullstats response is not a complete JSON list"
                            )
                        singleton_rows, _ = _fullstats_rows(
                            singleton_payload,
                            start=start,
                            end=end,
                            allowed_campaign_ids={campaign_id},
                            require_all_campaigns=True,
                        )
                        for day_text, day_rows in singleton_rows.items():
                            if day_text in rows_by_date:
                                rows_by_date[day_text].extend(day_rows)
                        requests.append(
                            {
                                "mode": "singleton_confirmation",
                                "outcome": "success",
                                "date_from": start.isoformat(),
                                "date_to": end.isoformat(),
                                "window_days": (end - start).days + 1,
                                "campaign_ids": [campaign_id],
                                "campaign_id_count": 1,
                                "response_digest": canonical_digest(singleton_payload),
                                "normalized_row_count": sum(
                                    len(value) for value in singleton_rows.values()
                                ),
                            }
                        )

        campaign_manifest = sorted(
            [*eligible, *excluded_completed], key=lambda item: int(item["campaign_id"])
        )
        response_digest = canonical_digest(requests)
        campaign_digest = canonical_digest(campaign_manifest)
        all_campaign_digest = canonical_digest(campaigns)
        source_digest = canonical_digest(
            {
                "all_campaign_digest": all_campaign_digest,
                "campaign_digest": campaign_digest,
                "response_digest": response_digest,
            }
        )
        source_manifest = {
            "status": "complete",
            "campaign_statuses": sorted(ALLOWED_CAMPAIGN_STATUSES),
            "campaign_count": len(campaign_ids),
            "all_campaign_count": len(campaigns),
            "all_campaign_digest": all_campaign_digest,
            "excluded_completed_before_scope_count": len(excluded_completed),
            "unsupported_overlap_count": len(unsupported_overlaps),
            "unsupported_overlaps": unsupported_overlaps,
            "campaigns": campaign_manifest,
            "campaign_digest": campaign_digest,
            "request_count": len(requests),
            "requests": requests,
            "response_digest": response_digest,
            "source_digest": source_digest,
        }
        candidates: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = list(source_blockers)
        required_nm_ids = {str(value) for value in scope.nm_ids}
        for target_date in target_dates:
            day_text = target_date.isoformat()
            aggregated = _aggregate_day_rows(rows_by_date.get(day_text, []), day_text)
            present_nm_ids = {str(row["nm_id"]) for row in aggregated}
            if aggregated and not required_nm_ids.issubset(present_nm_ids):
                blockers.append(
                    {
                        "code": "ads_target_nm_absent_in_nonempty_response",
                        "date": day_text,
                        "required_nm_ids": sorted(required_nm_ids),
                        "present_nm_id_count": len(present_nm_ids),
                        "reason": "a scoped zero cannot be persisted as a global empty slot",
                    }
                )
                continue
            kind = "success" if aggregated else "empty"
            payload: dict[str, Any] = {
                "kind": kind,
                "snapshot_date": day_text,
                "count": len(aggregated),
                "items": aggregated,
                "recovery": {
                    "schema_version": SCHEMA_VERSION,
                    "source": "official_adv_v3_fullstats",
                    "campaign_statuses": sorted(ALLOWED_CAMPAIGN_STATUSES),
                    "campaign_digest": source_manifest["campaign_digest"],
                    "response_digest": response_digest,
                    "source_digest": source_digest,
                    "required_nm_ids": list(scope.nm_ids),
                    "global_day_response_empty": not aggregated,
                    "synthetic_zero": False,
                },
            }
            if kind == "empty":
                payload["detail"] = (
                    "complete official fullstats responses contained no rows for any nmID "
                    "on this exact date"
                )
            payload_json = _canonical_json(payload)
            candidates.append(
                {
                    "snapshot_date": day_text,
                    "action": "insert",
                    "payload_kind": kind,
                    "payload_count": len(aggregated),
                    "payload_digest": _text_digest(payload_json),
                    "payload_json": payload_json,
                }
            )
        return source_manifest, candidates, blockers

    def _target_state_manifest(
        self, conn: sqlite3.Connection, scope: AdsHistoricalRecoveryScope
    ) -> dict[str, Any]:
        snapshots: list[dict[str, Any]] = []
        closures: list[dict[str, Any]] = []
        for value in scope.target_dates:
            day_text = value.isoformat()
            row = conn.execute(
                """SELECT snapshot_date,captured_at,payload_json
                   FROM temporal_source_slot_snapshots
                   WHERE source_key=? AND snapshot_date=? AND snapshot_role=?""",
                (SOURCE_KEY, day_text, SNAPSHOT_ROLE),
            ).fetchone()
            if row is not None:
                payload_text = str(row["payload_json"])
                try:
                    payload = json.loads(payload_text)
                except json.JSONDecodeError:
                    payload = {}
                result, envelope_origin = resolve_ads_snapshot_payload(payload)
                kind = str((result or {}).get("kind") or "invalid")
                raw_items = (result or {}).get("items")
                items_valid = isinstance(raw_items, list) and all(
                    isinstance(item, Mapping) for item in raw_items
                )
                items = raw_items if items_valid else []
                try:
                    payload_count = int((result or {}).get("count") or 0)
                except (TypeError, ValueError):
                    payload_count = -1
                present_nm_ids = {
                    str(item.get("nm_id", item.get("nmId", "")) or "")
                    for item in items
                    if isinstance(item, Mapping)
                }
                snapshots.append(
                    {
                        "snapshot_date": day_text,
                        "captured_at": str(row["captured_at"]),
                        "payload_digest": _text_digest(payload_text),
                        "payload_kind": kind,
                        "envelope_origin": envelope_origin,
                        "items_valid": items_valid,
                        "payload_count": payload_count,
                        "item_count": len(items),
                        "count_matches": payload_count >= 0
                        and payload_count == len(items),
                        "missing_nm_ids": sorted(
                            str(nm_id)
                            for nm_id in scope.nm_ids
                            if kind == "success" and str(nm_id) not in present_nm_ids
                        ),
                    }
                )
            closure = conn.execute(
                """SELECT target_date,state,attempt_count,next_retry_at,last_reason,
                          last_attempt_at,last_success_at,accepted_at
                   FROM temporal_source_closure_state
                   WHERE source_key=? AND target_date=? AND slot_kind=?""",
                (SOURCE_KEY, day_text, CLOSURE_SLOT),
            ).fetchone()
            if closure is not None:
                closures.append(dict(closure))
        return {
            "snapshot_dates": [row["snapshot_date"] for row in snapshots],
            "snapshots": snapshots,
            "snapshots_by_date": {
                row["snapshot_date"]: row for row in snapshots
            },
            "closures": closures,
        }

    def _non_target_manifest(
        self, conn: sqlite3.Connection, scope: AdsHistoricalRecoveryScope
    ) -> dict[str, Any]:
        excluded = {value.isoformat() for value in scope.target_dates}
        snapshot_hash = hashlib.sha256(b"[")
        snapshot_count = 0
        first = True
        for row in conn.execute(
            """SELECT source_key,snapshot_date,snapshot_role,captured_at,payload_json
               FROM temporal_source_slot_snapshots
               ORDER BY source_key,snapshot_date,snapshot_role"""
        ):
            if (
                row["source_key"] == SOURCE_KEY
                and row["snapshot_role"] == SNAPSHOT_ROLE
                and row["snapshot_date"] in excluded
            ):
                continue
            encoded = _canonical_json(dict(row)).encode("utf-8")
            snapshot_hash.update(b"" if first else b",")
            snapshot_hash.update(encoded)
            first = False
            snapshot_count += 1
        snapshot_hash.update(b"]")

        closure_hash = hashlib.sha256(b"[")
        closure_count = 0
        first = True
        for row in conn.execute(
            """SELECT source_key,target_date,slot_kind,state,attempt_count,next_retry_at,
                      last_reason,last_attempt_at,last_success_at,accepted_at
               FROM temporal_source_closure_state
               ORDER BY source_key,target_date,slot_kind"""
        ):
            if (
                row["source_key"] == SOURCE_KEY
                and row["slot_kind"] == CLOSURE_SLOT
                and row["target_date"] in excluded
            ):
                continue
            encoded = _canonical_json(dict(row)).encode("utf-8")
            closure_hash.update(b"" if first else b",")
            closure_hash.update(encoded)
            first = False
            closure_count += 1
        closure_hash.update(b"]")
        return {
            "temporal_source_slot_snapshots": {
                "row_count": snapshot_count,
                "digest": "sha256:" + snapshot_hash.hexdigest(),
            },
            "temporal_source_closure_state": {
                "row_count": closure_count,
                "digest": "sha256:" + closure_hash.hexdigest(),
            },
        }

    def _readback_conn(
        self, conn: sqlite3.Connection, scope: AdsHistoricalRecoveryScope
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        now = self.now_factory()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        business_today = now.astimezone(BUSINESS_TIMEZONE).date()
        for target_date in scope.target_dates:
            day_text = target_date.isoformat()
            if target_date >= business_today:
                blockers.append(
                    {
                        "code": "ads_target_date_not_closed",
                        "date": day_text,
                        "business_today": business_today.isoformat(),
                        "timezone": str(BUSINESS_TIMEZONE),
                    }
                )
            row = conn.execute(
                """SELECT captured_at,payload_json
                   FROM temporal_source_slot_snapshots
                   WHERE source_key=? AND snapshot_date=? AND snapshot_role=?""",
                (SOURCE_KEY, day_text, SNAPSHOT_ROLE),
            ).fetchone()
            if row is None:
                blockers.append({"code": "ads_date_missing", "date": day_text})
                continue
            payload_text = str(row["payload_json"])
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                payload = {}
            result, envelope_origin = resolve_ads_snapshot_payload(payload)
            closure = conn.execute(
                """SELECT state FROM temporal_source_closure_state
                   WHERE source_key=? AND target_date=? AND slot_kind=?""",
                (SOURCE_KEY, day_text, CLOSURE_SLOT),
            ).fetchone()
            kind = str((result or {}).get("kind") or "invalid")
            closure_state = str(closure["state"]) if closure is not None else None
            if kind not in {"success", "empty"}:
                blockers.append(
                    {"code": "ads_payload_not_accepted", "date": day_text, "kind": kind}
                )
            items = (result or {}).get("items")
            if not isinstance(items, list) or any(
                not isinstance(item, Mapping) for item in (items or [])
            ):
                blockers.append({"code": "ads_payload_items_invalid", "date": day_text})
                items = []
            try:
                payload_count = int((result or {}).get("count") or 0)
            except (TypeError, ValueError):
                payload_count = -1
            if payload_count < 0:
                blockers.append({"code": "ads_payload_count_invalid", "date": day_text})
            elif payload_count != len(items):
                blockers.append({"code": "ads_payload_count_mismatch", "date": day_text})
            if kind == "empty" and items:
                blockers.append({"code": "ads_empty_payload_has_items", "date": day_text})
            if kind == "success":
                present = {
                    str(item.get("nm_id", item.get("nmId", "")) or "")
                    for item in items
                }
                missing_nm_ids = sorted(
                    str(value) for value in scope.nm_ids if str(value) not in present
                )
                if missing_nm_ids:
                    blockers.append(
                        {
                            "code": "ads_sku_coverage_missing",
                            "date": day_text,
                            "missing_nm_ids": missing_nm_ids,
                        }
                    )
            if closure_state != "success":
                blockers.append(
                    {
                        "code": "ads_closure_not_success",
                        "date": day_text,
                        "closure_state": closure_state,
                    }
                )
            rows.append(
                {
                    "snapshot_date": day_text,
                    "captured_at": str(row["captured_at"]),
                    "payload_kind": kind,
                    "envelope_origin": envelope_origin,
                    "payload_count": payload_count,
                    "payload_digest": _text_digest(payload_text),
                    "closure_state": closure_state,
                }
            )
        digest_rows = [
            {
                "snapshot_date": row["snapshot_date"],
                "payload_digest": row["payload_digest"],
                "closure_state": row["closure_state"],
            }
            for row in rows
        ]
        return {
            "status": "blocked" if blockers else "ready",
            "scope": scope.as_dict(),
            "snapshot_count": len(rows),
            "snapshots": rows,
            "snapshot_digest": canonical_digest(digest_rows),
            "blockers": blockers,
        }

    def _read_prior_audit(self, fingerprint: str) -> dict[str, Any] | None:
        with self._connect(read_only=True) as conn:
            table = conn.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='ads_historical_recovery_audit'"""
            ).fetchone()
            if table is None:
                return None
            row = conn.execute(
                """SELECT result_json FROM ads_historical_recovery_audit
                   WHERE fingerprint=?""",
                (fingerprint,),
            ).fetchone()
            if row is None:
                return None
            result = json.loads(str(row["result_json"] or "{}"))
            return result if isinstance(result, dict) else None

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        if read_only:
            uri = f"file:{self.db_path.resolve()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=60, isolation_level=None)
        else:
            conn = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=60000")
        return conn

    @staticmethod
    def _require_schema(conn: sqlite3.Connection) -> None:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "temporal_source_slot_snapshots",
            "temporal_source_closure_state",
        }
        missing = sorted(required - tables)
        if missing:
            raise AdsHistoricalRecoveryError(
                "runtime schema is incomplete; run canonical schema setup first: "
                + ", ".join(missing)
            )

    def _now_text(self) -> str:
        value = self.now_factory()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_campaigns(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("adverts"), list):
        raise AdsHistoricalRecoveryError(
            "official campaign manifest is not a complete adverts mapping"
        )
    rows: dict[int, dict[str, Any]] = {}
    for group in payload["adverts"]:
        if not isinstance(group, Mapping):
            raise AdsHistoricalRecoveryError("campaign manifest contains an invalid group")
        status = _strict_int(group.get("status"), field="campaign status")
        advert_list = group.get("advert_list")
        if not isinstance(advert_list, list):
            raise AdsHistoricalRecoveryError(
                "campaign manifest group has no complete advert_list"
            )
        for advert in advert_list:
            if not isinstance(advert, Mapping):
                raise AdsHistoricalRecoveryError("campaign manifest contains an invalid advert")
            campaign_id = _strict_int(
                advert.get("advertId", advert.get("id")), field="campaign id"
            )
            if campaign_id <= 0:
                raise AdsHistoricalRecoveryError("campaign id must be positive")
            change_time = str(
                advert.get("changeTime", advert.get("change_time", "")) or ""
            ).strip()
            if change_time and _optional_iso_date(change_time) is None:
                raise AdsHistoricalRecoveryError(
                    f"campaign {campaign_id} has invalid changeTime"
                )
            existing = rows.get(campaign_id)
            if existing is not None and (
                existing["status"] != status
                or existing.get("change_time", "") != change_time
            ):
                raise AdsHistoricalRecoveryError(
                    f"campaign {campaign_id} has conflicting manifest values"
                )
            rows[campaign_id] = {
                "campaign_id": campaign_id,
                "status": status,
                "change_time": change_time,
            }
    return [rows[key] for key in sorted(rows)]


def _optional_iso_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _date_windows(values: Sequence[date]) -> list[tuple[date, date]]:
    dates = sorted(set(values))
    windows: list[tuple[date, date]] = []
    index = 0
    while index < len(dates):
        start = dates[index]
        last = start
        index += 1
        while index < len(dates) and (dates[index] - start).days < MAX_WINDOW_DAYS:
            last = dates[index]
            index += 1
        windows.append((start, last))
    return windows


def _fullstats_rows(
    payload: Sequence[Any],
    *,
    start: date,
    end: date,
    allowed_campaign_ids: set[int],
    require_all_campaigns: bool,
) -> tuple[dict[str, list[Mapping[str, Any]]], set[int]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    seen_advert_ids: set[int] = set()
    for advert in payload:
        if not isinstance(advert, Mapping):
            raise AdsHistoricalRecoveryError("fullstats contains a non-object advert")
        advert_id = _strict_int(
            advert.get("advertId", advert.get("advert_id", advert.get("id"))),
            field="fullstats advert id",
        )
        if advert_id not in allowed_campaign_ids:
            raise AdsHistoricalRecoveryError(
                f"fullstats returned unrequested campaign {advert_id}"
            )
        if advert_id in seen_advert_ids:
            raise AdsHistoricalRecoveryError(
                f"fullstats returned duplicate campaign {advert_id}"
            )
        seen_advert_ids.add(advert_id)
        days = advert.get("days")
        if not isinstance(days, list):
            raise AdsHistoricalRecoveryError(
                f"fullstats advert {advert_id} has no complete days list"
            )
        seen_dates: set[str] = set()
        for day in days:
            if not isinstance(day, Mapping):
                raise AdsHistoricalRecoveryError("fullstats contains a non-object day")
            day_text = _normalize_date(day.get("date"))
            try:
                day_value = date.fromisoformat(day_text)
            except ValueError as exc:
                raise AdsHistoricalRecoveryError(
                    f"fullstats contains invalid date: {day.get('date')}"
                ) from exc
            if day_value < start or day_value > end:
                raise AdsHistoricalRecoveryError(
                    f"fullstats returned out-of-window date {day_text}"
                )
            if day_text in seen_dates:
                raise AdsHistoricalRecoveryError(
                    f"fullstats campaign {advert_id} returned duplicate date {day_text}"
                )
            seen_dates.add(day_text)
            apps = day.get("apps")
            if not isinstance(apps, list):
                raise AdsHistoricalRecoveryError(
                    f"fullstats date {day_text} has no complete apps list"
                )
            for app in apps:
                if not isinstance(app, Mapping) or not isinstance(app.get("nms"), list):
                    raise AdsHistoricalRecoveryError(
                        f"fullstats date {day_text} contains an incomplete app"
                    )
                for item in app["nms"]:
                    if not isinstance(item, Mapping):
                        raise AdsHistoricalRecoveryError(
                            f"fullstats date {day_text} contains an invalid nm row"
                        )
                    nm_id = _strict_int(
                        item.get("nmId", item.get("nm_id")), field="fullstats nmID"
                    )
                    if nm_id <= 0:
                        raise AdsHistoricalRecoveryError("fullstats nmID must be positive")
                    result.setdefault(day_text, []).append(
                        {**dict(item), "nm_id": nm_id, "advert_id": advert_id}
                    )
    missing_campaign_ids = sorted(allowed_campaign_ids - seen_advert_ids)
    if require_all_campaigns and missing_campaign_ids:
        raise AdsHistoricalRecoveryError(
            "fullstats response omitted requested campaigns: "
            + ",".join(str(value) for value in missing_campaign_ids[:20])
            + ("..." if len(missing_campaign_ids) > 20 else "")
        )
    return result, seen_advert_ids


def _aggregate_day_rows(
    rows: Sequence[Mapping[str, Any]], snapshot_date: str
) -> list[dict[str, Any]]:
    metrics = (
        "ads_views",
        "ads_clicks",
        "ads_atbs",
        "ads_orders",
        "ads_sum",
        "ads_sum_price",
    )
    source_keys = {
        "ads_views": "views",
        "ads_clicks": "clicks",
        "ads_atbs": "atbs",
        "ads_orders": "orders",
        "ads_sum": "sum",
        "ads_sum_price": "sum_price",
    }
    aggregated: dict[int, dict[str, Decimal]] = {}
    advert_ids: dict[int, set[int]] = {}
    for row in rows:
        nm_id = int(row["nm_id"])
        target = aggregated.setdefault(
            nm_id, {metric: Decimal("0") for metric in metrics}
        )
        advert_ids.setdefault(nm_id, set()).add(int(row["advert_id"]))
        for metric, source_key in source_keys.items():
            if source_key not in row or row.get(source_key) in (None, ""):
                raise AdsHistoricalRecoveryError(
                    f"fullstats nm row has no confirmed {source_key} value"
                )
            target[metric] += _strict_decimal(row.get(source_key), field=source_key)
    result: list[dict[str, Any]] = []
    for nm_id in sorted(aggregated):
        row = aggregated[nm_id]
        clicks = row["ads_clicks"]
        views = row["ads_views"]
        orders = row["ads_orders"]
        ads_sum = row["ads_sum"]
        item = {
            "snapshot_date": snapshot_date,
            "nm_id": nm_id,
            "nmId": nm_id,
            "advert_ids": sorted(advert_ids[nm_id]),
            **{key: _decimal_number(value) for key, value in row.items()},
            "ads_cpc": _decimal_number(ads_sum / clicks if clicks else Decimal("0")),
            "ads_ctr": _decimal_number(
                clicks / views if views else Decimal("0")
            ),
            "ads_cr": _decimal_number(
                orders / clicks if clicks else Decimal("0")
            ),
        }
        result.append(item)
    return result


def _strict_decimal(value: Any, *, field: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, bool):
        raise AdsHistoricalRecoveryError(f"{field} is not numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AdsHistoricalRecoveryError(f"{field} is not numeric") from exc
    if not result.is_finite():
        raise AdsHistoricalRecoveryError(f"{field} is not finite")
    return result


def _strict_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise AdsHistoricalRecoveryError(f"{field} is not an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise AdsHistoricalRecoveryError(f"{field} is not an integer") from exc
    if str(value).strip() not in {str(result), f"{result}.0"} and not isinstance(value, int):
        raise AdsHistoricalRecoveryError(f"{field} is not an exact integer")
    return result


def _normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _decimal_number(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.0001")))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "ALLOWED_CAMPAIGN_STATUSES",
    "AdsHistoricalRecovery",
    "AdsHistoricalRecoveryError",
    "AdsHistoricalNoStatisticsError",
    "AdsHistoricalRecoveryScope",
    "DEFAULT_NM_IDS",
    "DEFAULT_TARGET_DATES",
    "MAX_IDS_PER_REQUEST",
    "MAX_WINDOW_DAYS",
    "MIN_REQUEST_INTERVAL_SECONDS",
    "REQUESTS_PER_MINUTE",
    "canonical_digest",
    "create_coherent_sqlite_backup",
]
