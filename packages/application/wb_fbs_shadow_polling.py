"""Dedicated single-flight polling and query-only Stage 7B readiness."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator, Mapping
from uuid import uuid4

from packages.adapters.official_api_rate_budget import FileBackedOfficialApiRateBudget
from packages.adapters.wb_fbs_orders import HttpBackedWbFbsOrdersSource
from packages.application.wb_fbs_orders import (
    COLLECTOR_ENABLED_ENV,
    IDENTITY_EVIDENCE_TABLE,
    IDENTITY_MAPPINGS_TABLE,
    OBSERVATIONS_TABLE,
    POLL_RUNS_TABLE,
    STATE_TABLE,
    STATUS_CURRENT_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    STATUS_TRANSITIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
    WbFbsOrdersCollector,
    ensure_wb_fbs_orders_schema,
)
from packages.application.ff_pool_fbs_lifecycle import (
    ensure_ff_pool_fbs_lifecycle_schema,
    process_post_t_fbs_lifecycle,
)
from packages.application.warehouse_domain_write_guard import warehouse_domain_write_status
from packages.application.warehouse_functional_lock import (
    WarehouseFunctionalBusyError,
    warehouse_functional_write_lock,
)


CONTRACT_NAME = "wb_fbs_shadow_polling_v1"
READINESS_CONTRACT = "wb_fbs_shadow_handoff_readiness_v1"
CADENCE_SECONDS = 5 * 60
FRESHNESS_SLO_SECONDS = 10 * 60
LOOKBACK_SECONDS = 7 * 24 * 60 * 60
PAGE_LIMIT = 1000
MAX_PAGES_PER_CYCLE = 10
RATE_INTERVAL_SECONDS = 0.22
LOCK_FILENAME = ".wb-fbs-shadow-collector.lock"
REPEATABLE_HANDOFF_ORDER_THRESHOLD = 3
_PROCESS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.Lock] = {}


class WbFbsShadowPollingError(RuntimeError):
    pass


class WbFbsShadowPollingBusy(WbFbsShadowPollingError):
    pass


def _process_lock(path: Path) -> threading.Lock:
    with _PROCESS_GUARD:
        return _PROCESS_LOCKS.setdefault(path, threading.Lock())


@contextmanager
def fbs_shadow_poll_lock(runtime_dir: Path) -> Iterator[None]:
    lock_path = (Path(runtime_dir).resolve() / LOCK_FILENAME).resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _process_lock(lock_path)
    if not process_lock.acquire(blocking=False):
        raise WbFbsShadowPollingBusy("FBS shadow poll is already running in this process")
    handle = lock_path.open("a+b")
    os.chmod(lock_path, 0o600)
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WbFbsShadowPollingBusy("FBS shadow poll is already running") from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            process_lock.release()


class WbFbsShadowPollingService:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        db_path: Path,
        source: Any | None = None,
        timestamp_factory: Any | None = None,
        unix_time_factory: Any | None = None,
        monotonic_factory: Any | None = None,
        enabled: bool | None = None,
        max_pages_per_cycle: int = MAX_PAGES_PER_CYCLE,
        lookback_seconds: int = LOOKBACK_SECONDS,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()
        self.db_path = Path(db_path).resolve()
        self.timestamp_factory = timestamp_factory or _utc_now
        self.unix_time_factory = unix_time_factory or time.time
        self.monotonic_factory = monotonic_factory or time.monotonic
        self.enabled = _env_enabled(COLLECTOR_ENABLED_ENV) if enabled is None else bool(enabled)
        self.max_pages_per_cycle = max(1, min(int(max_pages_per_cycle), 50))
        self.lookback_seconds = max(CADENCE_SECONDS, min(int(lookback_seconds), 30 * 24 * 60 * 60))
        self.rate_budget = None
        if source is None:
            self.rate_budget = FileBackedOfficialApiRateBudget(
                runtime_dir=self.runtime_dir,
                family="wb_fbs_orders",
                min_interval_seconds=RATE_INTERVAL_SECONDS,
            )
            source = HttpBackedWbFbsOrdersSource(rate_budget=self.rate_budget)
        self.source = source
        self.collector = WbFbsOrdersCollector(
            db_path=self.db_path,
            timestamp_factory=self.timestamp_factory,
            source=self.source,
            enabled=self.enabled,
            unix_time_factory=self.unix_time_factory,
        )

    def poll_once(self) -> dict[str, Any]:
        started_at = str(self.timestamp_factory())
        started_monotonic = float(self.monotonic_factory())
        run_id = "fbs_poll_" + uuid4().hex
        if not self.enabled:
            return {
                "contract_name": CONTRACT_NAME,
                "status": "disabled",
                "reason": "collector_default_off",
                "run_id": run_id,
                "mutates_wb": False,
            }
        try:
            lock = fbs_shadow_poll_lock(self.runtime_dir)
            lock.__enter__()
        except WbFbsShadowPollingBusy:
            completed_at = str(self.timestamp_factory())
            result = self._base_run(
                run_id=run_id,
                status="single_flight_skipped",
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=_duration_ms(started_monotonic, self.monotonic_factory),
            )
            self._persist_poll_run(result)
            return self._public_result(result)

        aggregate = self._base_run(
            run_id=run_id,
            status="failed",
            started_at=started_at,
            completed_at=started_at,
            duration_ms=0,
        )
        result: dict[str, Any]
        try:
            reset = getattr(self.source, "reset_telemetry", None)
            if callable(reset):
                reset()
            window_from, window_to, cursor = self._resume_window()
            start_cursor = cursor
            aggregate["status"] = "bounded_partial"
            aggregate.update(
                {
                    "window_date_from": window_from,
                    "window_date_to": window_to,
                    "start_cursor": start_cursor,
                    "next_cursor": cursor,
                }
            )
            complete = False
            for _ in range(self.max_pages_per_cycle):
                page = self.collector.collect(
                    date_from=window_from,
                    date_to=window_to,
                    next_cursor=cursor,
                    page_limit=PAGE_LIMIT,
                    max_pages=1,
                )
                aggregate["page_count"] += int(page.get("page_count") or 0)
                aggregate["accepted_order_count"] += int(page.get("accepted_order_count") or 0)
                aggregate["new_order_observation_count"] += int(page.get("new_observation_count") or 0)
                aggregate["status_response_count"] += int(page.get("status_observation_count") or 0)
                aggregate["new_status_observation_count"] += int(page.get("new_status_observation_count") or 0)
                aggregate["transition_count"] += int(page.get("transition_count") or 0)
                aggregate["missing_status_count"] += int(page.get("missing_status_count") or 0)
                aggregate["duplicate_status_count"] += int(page.get("duplicate_status_count") or 0)
                aggregate["reappeared_pair_count"] += int(page.get("reappeared_pair_count") or 0)
                aggregate["schema_drift_count"] += int(page.get("schema_drift_count") or 0)
                cursor = int(page.get("next_cursor") or 0)
                aggregate["next_cursor"] = cursor
                if bool(page.get("complete")):
                    complete = True
                    break
                if cursor <= 0:
                    raise WbFbsShadowPollingError(
                        "bounded FBS page returned without a crash-safe resume cursor"
                    )
            aggregate["status"] = "success" if complete else "bounded_partial"
            aggregate["completed_at"] = str(self.timestamp_factory())
            aggregate["duration_ms"] = _duration_ms(started_monotonic, self.monotonic_factory)
            aggregate.update(self._telemetry())
            result = aggregate
            self._persist_poll_run(result)
            if aggregate["status"] == "success":
                aggregate["lifecycle_processor"] = self._process_lifecycle_after_poll()
        except Exception as exc:
            aggregate["status"] = "failed"
            aggregate["completed_at"] = str(self.timestamp_factory())
            aggregate["duration_ms"] = _duration_ms(
                started_monotonic, self.monotonic_factory
            )
            aggregate.update(self._telemetry())
            aggregate["error"] = _safe_error(exc)
            result = aggregate
            self._persist_poll_run(result)
            raise
        finally:
            lock.__exit__(None, None, None)
        return self._public_result(result)

    def _process_lifecycle_after_poll(self) -> dict[str, Any]:
        """Keep collection independent while folding an activated epoch exactly."""

        try:
            with warehouse_functional_write_lock(
                self.runtime_dir,
                blocking=False,
            ):
                conn = sqlite3.connect(self.db_path, timeout=30.0)
                conn.row_factory = sqlite3.Row
                try:
                    ensure_ff_pool_fbs_lifecycle_schema(conn)
                    conn.commit()
                    barrier = warehouse_domain_write_status(conn)
                    if barrier.get("active") is True:
                        return {
                            "status": "held",
                            "reason": "warehouse_domain_write_barrier_active",
                            "mutates_wb": False,
                        }
                    conn.execute("BEGIN IMMEDIATE")
                    result = process_post_t_fbs_lifecycle(
                        conn,
                        occurred_at=str(self.timestamp_factory()),
                        schema_ready=True,
                    )
                    conn.commit()
                    return result
                except Exception as exc:
                    conn.rollback()
                    return {
                        "status": "failed",
                        "error": _safe_error(exc),
                        "mutates_wb": False,
                    }
                finally:
                    conn.close()
        except WarehouseFunctionalBusyError:
            return {
                "status": "held",
                "reason": "warehouse_functional_writer_active",
                "mutates_wb": False,
            }

    def _resume_window(self) -> tuple[int, int, int]:
        now = int(self.unix_time_factory())
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            ensure_wb_fbs_orders_schema(conn)
            row = conn.execute(
                f"SELECT last_status,window_date_from,window_date_to,next_cursor,complete FROM {STATE_TABLE} WHERE state_id=1"
            ).fetchone()
            conn.commit()
        if row and str(row[0]) in {"bounded_partial", "failed"} and not bool(row[4]):
            window_from = int(row[1])
            window_to = int(row[2])
            cursor = int(row[3])
            if 0 < window_from <= window_to and window_to - window_from <= 30 * 24 * 60 * 60:
                return window_from, window_to, cursor
        return now - self.lookback_seconds, now, 0

    @staticmethod
    def _base_run(
        *, run_id: str, status: str, started_at: str, completed_at: str, duration_ms: int
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_ms": duration_ms,
            "window_date_from": 0,
            "window_date_to": 0,
            "start_cursor": 0,
            "next_cursor": 0,
            "page_count": 0,
            "accepted_order_count": 0,
            "new_order_observation_count": 0,
            "status_response_count": 0,
            "new_status_observation_count": 0,
            "transition_count": 0,
            "missing_status_count": 0,
            "duplicate_status_count": 0,
            "reappeared_pair_count": 0,
            "schema_drift_count": 0,
            "request_count": 0,
            "retry_count": 0,
            "rate_limited_count": 0,
            "server_error_count": 0,
            "transport_error_count": 0,
            "rate_budget_wait_ms": 0,
            "retry_wait_ms": 0,
            "error": "",
        }

    def _telemetry(self) -> dict[str, int]:
        snapshot = getattr(self.source, "telemetry_snapshot", None)
        raw = snapshot() if callable(snapshot) else {}
        return {
            "request_count": int(raw.get("request_count") or 0),
            "retry_count": int(raw.get("retry_count") or 0),
            "rate_limited_count": int(raw.get("rate_limited_count") or 0),
            "server_error_count": int(raw.get("server_error_count") or 0),
            "transport_error_count": int(raw.get("transport_error_count") or 0),
            "rate_budget_wait_ms": max(0, round(float(raw.get("rate_budget_wait_ms") or 0.0))),
            "retry_wait_ms": max(0, round(float(raw.get("retry_wait_ms") or 0.0))),
        }

    def _persist_poll_run(self, result: Mapping[str, Any]) -> None:
        columns = (
            "run_id", "status", "started_at", "completed_at", "duration_ms",
            "window_date_from", "window_date_to", "start_cursor", "next_cursor",
            "page_count", "accepted_order_count", "new_order_observation_count",
            "status_response_count", "new_status_observation_count", "transition_count",
            "missing_status_count", "duplicate_status_count", "reappeared_pair_count",
            "schema_drift_count", "request_count", "retry_count", "rate_limited_count",
            "server_error_count", "transport_error_count", "rate_budget_wait_ms",
            "retry_wait_ms", "error",
        )
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            ensure_wb_fbs_orders_schema(conn)
            conn.execute(
                f"INSERT INTO {POLL_RUNS_TABLE}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(result.get(column, "") for column in columns),
            )
            conn.commit()

    def _public_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "contract_name": CONTRACT_NAME,
            **dict(result),
            "cadence_seconds": CADENCE_SECONDS,
            "freshness_slo_seconds": FRESHNESS_SLO_SECONDS,
            "lookback_seconds": self.lookback_seconds,
            "page_limit": PAGE_LIMIT,
            "max_pages_per_cycle": self.max_pages_per_cycle,
            "single_flight": True,
            "shared_rate_interval_seconds": RATE_INTERVAL_SECONDS,
            "official_read_methods": ["GET /api/v3/orders", "POST /api/v3/orders/status"],
            "status_post_semantic": "official_read_only",
            "mutates_wb": False,
            "creates_inventory_movement": False,
        }


def build_readiness_report(
    *, db_path: Path, runtime_dir: Path, now_unix: int | None = None
) -> dict[str, Any]:
    """Build a default-off, query-only go/no-go report without schema writes."""

    now = int(time.time()) if now_unix is None else int(now_unix)
    conn = _connect_query_only(db_path)
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {
            OBSERVATIONS_TABLE,
            STATE_TABLE,
            STATUS_OBSERVATIONS_TABLE,
            STATUS_CURRENT_TABLE,
            STATUS_TRANSITIONS_TABLE,
            POLL_RUNS_TABLE,
            WAREHOUSE_MAPPINGS_TABLE,
            IDENTITY_MAPPINGS_TABLE,
            IDENTITY_EVIDENCE_TABLE,
        }
        missing_tables = sorted(required - tables)
        if missing_tables:
            return _readiness_schema_absent(missing_tables)

        state_row = conn.execute(
            f"SELECT * FROM {STATE_TABLE} WHERE state_id=1"
        ).fetchone()
        recent_runs = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM {POLL_RUNS_TABLE} ORDER BY run_sequence DESC LIMIT 12"
            )
        ]
        successful = [row for row in recent_runs if row["status"] == "success"]
        success_epochs = sorted(
            _iso_epoch(str(row["completed_at"])) for row in successful
        )
        intervals = [
            success_epochs[index] - success_epochs[index - 1]
            for index in range(1, len(success_epochs))
        ]
        latest_window = int(state_row["window_date_to"] or 0) if state_row else 0
        lag_seconds = max(0, now - latest_window) if latest_window else None
        error_totals = {
            key: sum(int(row[key] or 0) for row in recent_runs)
            for key in (
                "retry_count",
                "rate_limited_count",
                "server_error_count",
                "transport_error_count",
                "missing_status_count",
                "duplicate_status_count",
                "reappeared_pair_count",
                "schema_drift_count",
            )
        }
        backpressure_count = sum(
            1
            for row in recent_runs
            if row["status"] in {"bounded_partial", "failed", "single_flight_skipped"}
        )

        transition_pairs = [
            dict(row)
            for row in conn.execute(
                f"""SELECT previous_supplier_status,previous_wb_status,
                           current_supplier_status,current_wb_status,
                           COUNT(*) AS transition_count,COUNT(DISTINCT order_id) AS order_count
                    FROM {STATUS_TRANSITIONS_TABLE}
                    GROUP BY previous_supplier_status,previous_wb_status,
                             current_supplier_status,current_wb_status
                    ORDER BY transition_count DESC,
                             previous_supplier_status,previous_wb_status,
                             current_supplier_status,current_wb_status"""
            )
        ]
        handoff_row = conn.execute(
            f"""SELECT COUNT(*) AS transition_count,COUNT(DISTINCT order_id) AS order_count
                FROM {STATUS_TRANSITIONS_TABLE}
                WHERE previous_supplier_status='complete' AND previous_wb_status='waiting'
                  AND current_supplier_status='complete' AND current_wb_status='sorted'"""
        ).fetchone()
        handoff_transition_count = int(handoff_row[0])
        handoff_order_count = int(handoff_row[1])
        sorted_current = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {STATUS_CURRENT_TABLE} WHERE supplier_status='complete' AND wb_status='sorted'"
            ).fetchone()[0]
        )
        status_current_count = int(conn.execute(f"SELECT COUNT(*) FROM {STATUS_CURRENT_TABLE}").fetchone()[0])
        status_observation_count = int(conn.execute(f"SELECT COUNT(*) FROM {STATUS_OBSERVATIONS_TABLE}").fetchone()[0])
        outcomes = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                f"SELECT outcome,COUNT(*) FROM {IDENTITY_EVIDENCE_TABLE} GROUP BY outcome"
            )
        }
        mappings = {
            "warehouse_mapping_count": int(conn.execute(f"SELECT COUNT(*) FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE active=1").fetchone()[0]),
            "identity_mapping_count": int(conn.execute(f"SELECT COUNT(*) FROM {IDENTITY_MAPPINGS_TABLE} WHERE active=1").fetchone()[0]),
            "matched_count": int(outcomes.get("matched", 0)),
            "unmatched_count": int(outcomes.get("unmatched_warehouse", 0)) + int(outcomes.get("unmatched_identity", 0)),
            "deferred_count": int(outcomes.get("deferred", 0)),
            "ambiguous_count": _active_mapping_ambiguity_count(conn),
            "outcomes": outcomes,
            "active_warehouse": [
                dict(row)
                for row in conn.execute(
                    f"""SELECT mapping_id,seller_warehouse_id,facility_id,mapping_digest
                        FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE active=1
                        ORDER BY seller_warehouse_id,mapping_id"""
                )
            ],
            "active_identity": [
                dict(row)
                for row in conn.execute(
                    f"""SELECT mapping_id,source_nm_id,source_chrt_id,source_barcode,
                               source_sku,target_nm_id,mapping_digest
                        FROM {IDENTITY_MAPPINGS_TABLE} WHERE active=1
                        ORDER BY source_nm_id,source_chrt_id,source_barcode,source_sku,mapping_id"""
                )
            ],
        }
        facilities = _facility_state(conn, tables)
        aggregate_pool = _aggregate_pool_state(conn, tables)
        pending_acceptance = _pending_acceptance(conn, tables)
        blockers: list[str] = []
        if not recent_runs or len(successful) < 3:
            blockers.append("fewer than three successful dedicated polling cycles are available")
        if lag_seconds is None or lag_seconds > FRESHNESS_SLO_SECONDS:
            blockers.append("collector freshness exceeds the 10-minute normal-state SLO")
        if intervals and max(intervals) > FRESHNESS_SLO_SECONDS:
            blockers.append("recent successful polling cadence contains a gap over 10 minutes")
        if backpressure_count:
            blockers.append("recent polling contains failed or bounded-partial backpressure")
        if any(error_totals[key] for key in ("rate_limited_count", "server_error_count", "transport_error_count")):
            blockers.append("recent polling contains official API rate/server/transport errors")
        if error_totals["missing_status_count"]:
            blockers.append("recent official status responses omitted one or more requested orders")
        if error_totals["duplicate_status_count"] or error_totals["schema_drift_count"]:
            blockers.append("recent polling contains duplicate or schema-drift evidence")
        if mappings["unmatched_count"] or mappings["deferred_count"] or mappings["ambiguous_count"]:
            blockers.append("official order identity evidence is unmatched, deferred or ambiguous")
        if not facilities or not any(bool(item["active"]) for item in facilities):
            blockers.append("no active FF facility is available for an exact future design")
        if not mappings["warehouse_mapping_count"] or not mappings["identity_mapping_count"]:
            blockers.append("exact active warehouse/SKU mappings are incomplete")
        if handoff_order_count < REPEATABLE_HANDOFF_ORDER_THRESHOLD:
            blockers.append(
                "complete/waiting -> complete/sorted is not repeatably observed on at least three distinct orders"
            )
        if not aggregate_pool["pool_zero"]:
            blockers.append("facility-pool ledger is not in the expected unopened zero state")
        if pending_acceptance["ambiguous_or_partially_posted_count"]:
            blockers.append(
                "one or more pending China shipments have ambiguous or partial FF postings"
            )

        candidate_ready = handoff_order_count >= REPEATABLE_HANDOFF_ORDER_THRESHOLD
        report = {
            "contract_name": READINESS_CONTRACT,
            "mode": "query_only_dry_run",
            "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "collector": {
                "configured_cadence_seconds": CADENCE_SECONDS,
                "freshness_slo_seconds": FRESHNESS_SLO_SECONDS,
                "latest_window_date_to": latest_window,
                "lag_seconds": lag_seconds,
                "recent_run_count": len(recent_runs),
                "successful_run_count": len(successful),
                "recent_success_intervals_seconds": intervals,
                "backpressure_count": backpressure_count,
                "error_totals": error_totals,
                "state": dict(state_row) if state_row else {},
                "recent_runs": recent_runs,
                "lock_path_digest": "sha256:" + hashlib.sha256(
                    str((Path(runtime_dir).resolve() / LOCK_FILENAME)).encode("utf-8")
                ).hexdigest(),
            },
            "transition_evidence": {
                "status_observation_count": status_observation_count,
                "current_episode_count": status_current_count,
                "transition_pairs": transition_pairs,
                "complete_waiting_to_complete_sorted": {
                    "transition_count": handoff_transition_count,
                    "distinct_order_count": handoff_order_count,
                    "repeatability_threshold_distinct_orders": REPEATABLE_HANDOFF_ORDER_THRESHOLD,
                    "candidate_evidence_sufficient": candidate_ready,
                },
                "current_complete_sorted_count": sorted_current,
                "source_status_timestamp_available": False,
                "local_first_last_seen_are_observation_times": True,
            },
            "portal_lane_diagnostics": _portal_lane_diagnostics(conn),
            "facilities": facilities,
            "mappings": mappings,
            "aggregate_and_pool": aggregate_pool,
            "pending_acceptance": pending_acceptance,
            "physical_handoff": {
                "supplier_status_complete_debit_trigger": False,
                "wb_status_sorted_candidate_only": True,
                "automatic_trigger_selected": False,
                "official_semantics_review_still_required": True,
                "clean_pending_receipt_may_be_manifest_excluded": True,
            },
            "go_no_go": "GO_FOR_OWNER_GATED_DESIGN_REVIEW" if not blockers else "NO_GO",
            "blockers": blockers,
            "next_action": (
                "Owner-gated next stage may review an exact trigger and opening/cutover plan; no automatic trigger is selected."
                if not blockers
                else _next_action(blockers)
            ),
            "query_only": True,
            "mutates_wb": False,
            "mutates_business_data": False,
        }
        report["evidence_digest"] = _fingerprint(report)
        return report
    finally:
        conn.close()


def _aggregate_pool_state(conn: sqlite3.Connection, tables: set[str]) -> dict[str, Any]:
    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone() if "sheet_vitrina_v1_warehouse_functional_active" in tables else None
    version_id = str(active[0]) if active else ""
    aggregate = conn.execute(
        """SELECT COUNT(*),COALESCE(SUM(CAST(quantity AS NUMERIC)),0),
                  COALESCE(SUM(CAST(capital_rub AS NUMERIC)),0)
           FROM sheet_vitrina_v1_warehouse_functional_balances
           WHERE version_id=? AND warehouse_key='ff'""",
        (version_id,),
    ).fetchone() if "sheet_vitrina_v1_warehouse_functional_balances" in tables else (0, 0, 0)
    zero_tables = (
        "sheet_vitrina_v1_ff_pool_feature_epochs",
        "sheet_vitrina_v1_ff_pool_balances",
        "sheet_vitrina_v1_warehouse_business_operations",
        "sheet_vitrina_v1_ff_pool_movement_lines",
        "sheet_vitrina_v1_ff_pool_cutover_manifests",
        "sheet_vitrina_v1_ff_pool_cutover_checkpoints",
        "sheet_vitrina_v1_ff_pool_cutover_opening_reservations",
    )
    counts = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if table in tables else 0
        for table in zero_tables
    }
    return {
        "aggregate_ff": {
            "version_id": version_id,
            "row_count": int(aggregate[0]),
            "quantity": str(aggregate[1]),
            "capital_rub": str(aggregate[2]),
        },
        "pool_counts": counts,
        "pool_zero": not any(counts.values()),
        "opening_or_cutover_applied": any(counts.values()),
    }


def _active_mapping_ambiguity_count(conn: sqlite3.Connection) -> int:
    warehouse = int(
        conn.execute(
            f"""SELECT COUNT(*) FROM (
                    SELECT seller_warehouse_id FROM {WAREHOUSE_MAPPINGS_TABLE}
                    WHERE active=1 GROUP BY seller_warehouse_id HAVING COUNT(*)>1
                )"""
        ).fetchone()[0]
    )
    identity = int(
        conn.execute(
            f"""SELECT COUNT(*) FROM (
                    SELECT source_nm_id,source_chrt_id,source_barcode,source_sku
                    FROM {IDENTITY_MAPPINGS_TABLE} WHERE active=1
                    GROUP BY source_nm_id,source_chrt_id,source_barcode,source_sku
                    HAVING COUNT(*)>1
                )"""
        ).fetchone()[0]
    )
    return warehouse + identity


def _facility_state(conn: sqlite3.Connection, tables: set[str]) -> list[dict[str, Any]]:
    facilities = "sheet_vitrina_v1_ff_facilities"
    profiles = "sheet_vitrina_v1_ff_facility_profiles"
    if facilities not in tables:
        return []
    if profiles in tables:
        query = f"""SELECT facility.facility_id,facility.code,facility.name,facility.active,
                           facility.display_timezone,COALESCE(profile.city,'') AS city
                    FROM {facilities} AS facility
                    LEFT JOIN {profiles} AS profile USING(facility_id)
                    ORDER BY facility.name,facility.facility_id"""
    else:
        query = f"""SELECT facility_id,code,name,active,display_timezone,'' AS city
                    FROM {facilities} ORDER BY name,facility_id"""
    return [
        dict(row) | {"active": bool(row["active"])}
        for row in conn.execute(query)
    ]


def _pending_acceptance(conn: sqlite3.Connection, tables: set[str]) -> dict[str, Any]:
    table = "sheet_vitrina_v1_supplier_shipments"
    if table not in tables:
        return {
            "count": 0,
            "excluded_pending_receipt_eligible_count": 0,
            "ambiguous_or_partially_posted_count": 0,
            "rows": [],
            "source_table_available": False,
        }
    source_rows = [
        dict(row)
        for row in conn.execute(
            f"""SELECT shipment_id,COALESCE(invoice_no,'') AS invoice_no,
                       COALESCE(actual_shipment_date,'') AS actual_shipment_date,
                       COALESCE(actual_ff_acceptance_date,'') AS actual_ff_acceptance_date,
                       order_status,product_qty_total
                FROM {table}
                WHERE COALESCE(actual_shipment_date,'')<>''
                  AND COALESCE(actual_ff_acceptance_date,'')=''
                  AND COALESCE(archived_at,'')=''
                ORDER BY actual_shipment_date,shipment_id"""
        )
    ]
    rows: list[dict[str, Any]] = []
    clean_count = 0
    ambiguous_count = 0
    for source in source_rows:
        shipment_id = str(source["shipment_id"])
        receipt_table_available = "sheet_vitrina_v1_ff_stock_operations" in tables
        receipt_count = (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations "
                    "WHERE source_key=?",
                    (f"supplier_shipment_acceptance:{shipment_id}",),
                ).fetchone()[0]
            )
            if receipt_table_available
            else None
        )
        cost_table_available = "sheet_vitrina_v1_supplier_ff_cost_layers" in tables
        cost_layer_count = (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_supplier_ff_cost_layers "
                    "WHERE supplier_shipment_id=?",
                    (shipment_id,),
                ).fetchone()[0]
            )
            if cost_table_available
            else None
        )
        lines_table_available = "sheet_vitrina_v1_supplier_shipment_lines" in tables
        line_quantities = (
            [
                _exact_positive_int_or_none(row[0])
                for row in conn.execute(
                    "SELECT qty FROM sheet_vitrina_v1_supplier_shipment_lines "
                    "WHERE shipment_id=? AND line_type='product' ORDER BY sort_order,line_id",
                    (shipment_id,),
                ).fetchall()
            ]
            if lines_table_available
            else []
        )
        shipment_quantity = _exact_positive_int_or_none(source.get("product_qty_total"))
        line_quantity = (
            sum(int(value) for value in line_quantities)
            if line_quantities and all(value is not None for value in line_quantities)
            else None
        )
        exclusion_eligible = (
            not str(source.get("actual_ff_acceptance_date") or "")
            and receipt_count == 0
            and cost_layer_count == 0
            and shipment_quantity is not None
            and line_quantity == shipment_quantity
        )
        if exclusion_eligible:
            clean_count += 1
        else:
            ambiguous_count += 1
        rows.append(
            {
                **source,
                "receipt_operation_count": receipt_count,
                "cost_layer_count": cost_layer_count,
                "shipment_quantity_exact": shipment_quantity,
                "product_line_count": len(line_quantities),
                "product_line_quantity_exact": line_quantity,
                "classification": (
                    "excluded_pending_receipt_eligible"
                    if exclusion_eligible
                    else "ambiguous_or_partially_posted"
                ),
                "requires_exact_cutover_manifest_pin": exclusion_eligible,
            }
        )
    return {
        "count": len(rows),
        "excluded_pending_receipt_eligible_count": clean_count,
        "ambiguous_or_partially_posted_count": ambiguous_count,
        "rows": rows,
        "source_table_available": True,
    }


def _exact_positive_int_or_none(value: Any) -> int | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (
        not number.is_finite()
        or number <= 0
        or number != number.to_integral_value()
    ):
        return None
    return int(number)


def _portal_lane_diagnostics(conn: sqlite3.Connection) -> dict[str, Any]:
    current_counts = {
        f"{str(row[0])}/{str(row[1])}": int(row[2])
        for row in conn.execute(
            f"""SELECT supplier_status,wb_status,COUNT(*)
                FROM {STATUS_CURRENT_TABLE}
                GROUP BY supplier_status,wb_status ORDER BY supplier_status,wb_status"""
        )
    }
    return {
        "official_current_status_pairs": current_counts,
        "lane_inferences": [
            {"portal_lane": "Новые", "candidate_official_filter": "supplierStatus=new", "proof": "inference_only"},
            {"portal_lane": "На сборке", "candidate_official_filter": "supplierStatus=confirm", "proof": "inference_only"},
            {"portal_lane": "В доставке", "candidate_official_filter": "supplierStatus=complete", "proof": "inference_only"},
            {"portal_lane": "Завершённые", "candidate_official_filter": "not established", "proof": "unproven"},
            {"portal_lane": "Отменённые", "candidate_official_filter": "cancellation-like official statuses", "proof": "inference_only"},
        ],
        "seller_portal_scraped": False,
        "static_ui_counts_are_api_trigger_evidence": False,
    }


def _readiness_schema_absent(missing: list[str]) -> dict[str, Any]:
    return {
        "contract_name": READINESS_CONTRACT,
        "mode": "query_only_dry_run",
        "go_no_go": "NO_GO",
        "blockers": ["Stage 7B query schema is absent"],
        "missing_tables": missing,
        "next_action": "Deploy the repository-owned Stage 7B schema and collector before reassessing readiness.",
        "query_only": True,
        "mutates_wb": False,
        "mutates_business_data": False,
    }


def _next_action(blockers: list[str]) -> str:
    if any("complete/waiting" in item for item in blockers):
        return "Keep the read-only collector running until the exact WB-controlled handoff transition is repeatably observed; do not open/cut over or select a debit trigger."
    return "Resolve the first listed query-only readiness blocker before any owner-gated opening/cutover stage."


def _connect_query_only(path: Path) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise WbFbsShadowPollingError("operational store is missing")
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _duration_ms(started: float, monotonic_factory: Any) -> int:
    return max(0, round((float(monotonic_factory()) - started) * 1000.0))


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _env_enabled(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().casefold() in {"1", "true", "yes", "on"}


def _safe_error(exc: Exception) -> str:
    return str(exc or "").replace("\x00", "")[:1000]


def _fingerprint(value: Any) -> str:
    material = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
