#!/usr/bin/env python3
"""Focused smoke for Web Vitrina health, receipts and morning schedule."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import (
    RegistryUploadHttpEntrypoint,
    SHEET_VITRINA_HEALTH_CANDIDATE_TRIGGER,
    SheetVitrinaHealthRecoveryConflict,
)
from packages.application.sheet_vitrina_v1_health import (
    build_web_vitrina_health_operator_surface,
    evaluate_web_vitrina_health,
    mark_web_vitrina_health_cycle_incomplete,
    persist_web_vitrina_health_evaluation,
)
from packages.application.warehouse_functional_economics_backfill import (
    HISTORICAL_REPAIR_CONTRACT,
    HISTORICAL_REPAIR_METADATA_KEY,
)
from apps.sheet_vitrina_v1_auto_refresh_tick import JobPollDeadlineError
from apps.sheet_vitrina_v1_health_tick import _execute_health_tick
from packages.application.sheet_vitrina_v1_live_plan import SOURCE_TEMPORAL_POLICIES
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot

TODAY = "2026-08-29"
YESTERDAY = "2026-08-28"
NOW = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)


def _slot(
    source_key: str,
    role: str,
    target_date: str,
    *,
    kind: str = "success",
    requested: int = 2,
    covered: int = 2,
    note: str = "",
) -> dict[str, object]:
    status = "success" if kind == "success" and covered >= requested else (
        "error" if kind == "error" else "warning"
    )
    return {
        "source_key": source_key,
        "temporal_slot": role,
        "status": status,
        "kind": kind,
        "freshness": target_date,
        "snapshot_date": target_date,
        "date": target_date,
        "date_from": target_date,
        "date_to": target_date,
        "requested_count": requested,
        "covered_count": covered,
        "note": note,
        "reason": note or kind,
    }


class _FakeRuntime:
    def __init__(self) -> None:
        self.repair_registry: dict[str, object] | None = None
        outcomes: list[dict[str, object]] = []
        for source_key, policy in SOURCE_TEMPORAL_POLICIES.items():
            slots = [_slot(source_key, "yesterday_closed", YESTERDAY)]
            if policy != "yesterday_closed_only":
                slots.append(_slot(source_key, "today_current", TODAY))
            outcomes.append(
                {
                    "source_key": source_key,
                    "status": "success",
                    "slots": slots,
                }
            )
        by_source = {str(item["source_key"]): item for item in outcomes}
        by_source["spp"]["slots"] = [
            _slot("spp", "yesterday_closed", YESTERDAY, kind="missing", requested=2, covered=0),
            _slot("spp", "today_current", TODAY, requested=2, covered=0, note="empty payload"),
        ]
        by_source["spp_proxy"]["slots"] = [
            _slot("spp_proxy", "yesterday_closed", YESTERDAY, kind="incomplete", requested=2, covered=1),
            _slot("spp_proxy", "today_current", TODAY),
        ]
        by_source["fin_report_daily"]["slots"] = [
            _slot("fin_report_daily", "yesterday_closed", YESTERDAY),
            _slot(
                "fin_report_daily",
                "today_current",
                TODAY,
                kind="error",
                requested=2,
                covered=0,
                note="429 retry-after exact current not captured",
            ),
        ]
        by_source["sku_action_events"]["slots"] = [
            _slot(
                "sku_action_events",
                role,
                target,
                note="confirmed_event_rows=0; empty_semantics=no_confirmed_event",
            )
            for role, target in (
                ("yesterday_closed", YESTERDAY),
                ("today_current", TODAY),
            )
        ]
        by_source["own_product_capital"]["slots"] = [
            _slot(
                "own_product_capital",
                role,
                target,
                requested=0,
                covered=0,
                note="exact_zero; row_count=0",
            )
            for role, target in (
                ("yesterday_closed", YESTERDAY),
                ("today_current", TODAY),
            )
        ]
        self.refresh_status = SimpleNamespace(
            bundle_version="bundle",
            as_of_date=YESTERDAY,
            snapshot_id="snapshot",
            refreshed_at="2026-08-29T02:59:00Z",
            source_temporal_policies=dict(SOURCE_TEMPORAL_POLICIES),
            source_outcomes=outcomes,
        )

    def load_sheet_vitrina_refresh_status(self):
        return self.refresh_status

    def load_source_health_status(self, source_key: str):
        assert source_key == "seller_portal_auth"
        return {"session_status": "session_valid_canonical"}

    def list_temporal_source_slot_observations(self, **kwargs):
        del kwargs
        return [
            {
                "source_key": "seller_funnel_snapshot",
                "snapshot_date": YESTERDAY,
                "snapshot_role": "accepted_closed_day_snapshot",
                "payload": {"kind": "success", "requested_count": 2, "covered_count": 2},
            },
            {
                "source_key": "spp_proxy",
                "snapshot_date": YESTERDAY,
                "snapshot_role": "accepted_current_snapshot",
                "payload": {"kind": "incomplete", "requested_count": 2, "covered_count": 1},
            },
            {
                "source_key": "seller_funnel_snapshot",
                "snapshot_date": TODAY,
                "snapshot_role": "accepted_current_snapshot",
                "payload": {"kind": "success", "requested_count": 2, "covered_count": 2},
            },
        ]

    def load_current_state(self):
        return SimpleNamespace(config_v2=[SimpleNamespace(enabled=True), SimpleNamespace(enabled=True)])

    def load_sheet_vitrina_ready_snapshot_any_bundle(self, *, as_of_date: str):
        assert as_of_date == self.refresh_status.as_of_date
        return SimpleNamespace(
            metadata=(
                {HISTORICAL_REPAIR_METADATA_KEY: deepcopy(self.repair_registry)}
                if self.repair_registry is not None
                else {}
            )
        )


class _TickRuntime(_FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        seller_funnel = next(
            item
            for item in self.refresh_status.source_outcomes
            if item["source_key"] == "seller_funnel_snapshot"
        )
        seller_yesterday = next(
            item
            for item in seller_funnel["slots"]
            if item["temporal_slot"] == "yesterday_closed"
        )
        seller_yesterday.update(
            _slot(
                "seller_funnel_snapshot",
                "yesterday_closed",
                YESTERDAY,
                kind="missing",
                requested=2,
                covered=0,
            )
        )
        self.observations: list[dict[str, object]] = []

    def save_sheet_vitrina_health_observation(self, **payload):
        record = {
            "observation_id": payload["observation_id"],
            "business_date": payload["business_date"],
            "phase": payload["phase"],
            "observed_at": payload["observed_at"],
            "ready_snapshot_id": payload["ready_snapshot_id"],
            "payload_fingerprint": payload["payload_fingerprint"],
            "payload": deepcopy(payload["payload"]),
        }
        if not any(item["observation_id"] == record["observation_id"] for item in self.observations):
            self.observations.append(record)
        return {
            "status": "appended",
            "observation_id": record["observation_id"],
            "business_date": record["business_date"],
            "phase": record["phase"],
            "payload_fingerprint": record["payload_fingerprint"],
            "transition_count": 1,
        }

    def list_sheet_vitrina_health_observations(self, *, business_date: str, limit: int):
        return [
            deepcopy(item)
            for item in reversed(self.observations)
            if item["business_date"] == business_date
        ][:limit]

    def list_sheet_vitrina_health_transitions(self, *, business_date: str, limit: int):
        del business_date, limit
        return []


class _HealthSurfaceRuntime:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        phases: tuple[str, ...],
        repair_registry: dict[str, object] | None = None,
    ) -> None:
        self.observations = [
            {
                "observation_id": f"health-{phase}",
                "business_date": TODAY,
                "phase": phase,
                "observed_at": f"2026-08-29T0{index + 1}:30:00Z",
                "ready_snapshot_id": "snapshot",
                "payload_fingerprint": str(payload.get("fingerprint") or "sha256:payload"),
                "payload": deepcopy(payload),
            }
            for index, phase in enumerate(phases)
        ]
        self.receipts: dict[str, dict[str, object]] = {}
        self.repair_registry = deepcopy(repair_registry)

    def load_sheet_vitrina_ready_snapshot_any_bundle(self, *, as_of_date: str):
        del as_of_date
        return SimpleNamespace(
            metadata=(
                {HISTORICAL_REPAIR_METADATA_KEY: deepcopy(self.repair_registry)}
                if self.repair_registry is not None
                else {}
            )
        )

    def list_sheet_vitrina_health_observations(self, *, business_date: str, limit: int):
        assert business_date == TODAY
        return list(reversed(self.observations))[:limit]

    def list_sheet_vitrina_health_transitions(self, *, business_date: str, limit: int):
        assert business_date == TODAY
        return [
            {
                "signal_key": "yesterday_closed",
                "previous_state": "unobserved",
                "current_state": "incomplete",
                "observed_at": "2026-08-29T02:30:00Z",
            }
        ][:limit]

    def load_sheet_vitrina_health_recovery_receipt(self, action_fingerprint: str):
        return deepcopy(self.receipts.get(action_fingerprint))

    def save_sheet_vitrina_health_recovery_receipt(self, **payload):
        fingerprint = str(payload["action_fingerprint"])
        self.receipts.setdefault(fingerprint, dict(payload))
        return deepcopy(self.receipts[fingerprint])


class _HealthJobs:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, object]] = {}
        self.active: dict[str, object] | None = None

    def active_job(self, *, operations):
        del operations
        return deepcopy(self.active)

    def get(self, job_id: str):
        if job_id not in self.jobs:
            raise ValueError("job not found")
        return deepcopy(self.jobs[job_id])


def _operator_payload(
    *,
    yesterday_state: str = "ok",
    today_state: str = "ok",
    bot_state: str = "ok",
    recoverable: bool = False,
    unsupported: bool = False,
) -> dict[str, object]:
    matrix = [
        {
            "source_group_id": "wb_api",
            "source_key": "fin_report_daily",
            "date_role": "yesterday_closed",
            "target_date": YESTERDAY,
            "expectation_state": "complete" if yesterday_state == "ok" else "partial",
            "requested_count": 2,
            "covered_count": 2 if yesterday_state == "ok" else 1,
        },
        {
            "source_group_id": "other_sources",
            "source_key": "sku_action_events",
            "date_role": "today_current",
            "target_date": TODAY,
            "expectation_state": "no_events",
            "requested_count": 0,
            "covered_count": 0,
        },
        {
            "source_group_id": "wb_api",
            "source_key": "stocks",
            "date_role": "today_current",
            "target_date": TODAY,
            "expectation_state": "inapplicable",
            "requested_count": 0,
            "covered_count": 0,
        },
    ]
    actions: list[dict[str, object]] = []
    if recoverable:
        matrix.append(
            {
                "source_group_id": "wb_api",
                "source_key": "sales_funnel_history",
                "date_role": "yesterday_closed",
                "target_date": YESTERDAY,
                "expectation_state": "missing",
                "requested_count": 2,
                "covered_count": 0,
            }
        )
        actions.append(
            {
                "source_group_id": "wb_api",
                "target_date": YESTERDAY,
                "gap_source_keys": ["sales_funnel_history"],
                "recoverable_source_keys": ["sales_funnel_history"],
                "hook": "group_refresh",
                "apply_allowed": True,
                "action_fingerprint": "sha256:recoverable-action",
            }
        )
    if unsupported:
        matrix.append(
            {
                "source_group_id": "wb_public_card_bot",
                "source_key": "spp_proxy",
                "date_role": "yesterday_closed",
                "target_date": YESTERDAY,
                "expectation_state": "partial",
                "requested_count": 2,
                "covered_count": 1,
            }
        )
        actions.append(
            {
                "source_group_id": "wb_public_card_bot",
                "target_date": YESTERDAY,
                "gap_source_keys": ["spp_proxy"],
                "recoverable_source_keys": [],
                "hook": "none",
                "apply_allowed": False,
                "action_fingerprint": "sha256:unsupported-action",
            }
        )
    return {
        "contract": "sheet_vitrina_v1_web_health/v1",
        "business_date": TODAY,
        "yesterday_date": YESTERDAY,
        "ready_snapshot": {"as_of_date": YESTERDAY},
        "fingerprint": "sha256:operator-payload",
        "expectation_matrix": matrix,
        "signals": {
            "yesterday_closed": {
                "state": yesterday_state,
                "problem_count": 0 if yesterday_state == "ok" else 1,
                "problem_sources": [] if yesterday_state == "ok" else ["fin_report_daily"],
            },
            "today_current": {
                "state": today_state,
                "problem_count": 0 if today_state == "ok" else 1,
                "problem_sources": [] if today_state == "ok" else ["current_source"],
            },
            "bot_health": {
                "state": bot_state,
                "confirmed_problem_count": 0 if bot_state == "ok" else 1,
                "confirmed_problems": [] if bot_state == "ok" else ["seller_portal_session:invalid"],
            },
        },
        "recovery_preview": {
            "status": "recovery_needed" if actions else "closed",
            "target_date": YESTERDAY,
            "gap_count": len(actions),
            "plan_fingerprint": "sha256:operator-plan",
            "actions": actions,
        },
    }


def _health_entrypoint_shell(runtime: _HealthSurfaceRuntime, jobs: _HealthJobs):
    shell = object.__new__(RegistryUploadHttpEntrypoint)
    shell.runtime = runtime
    shell.operator_jobs = jobs
    shell._health_recovery_lock = threading.RLock()
    shell.now_factory = lambda: datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
    shell.activated_at_factory = lambda: "2026-08-29T06:00:00Z"
    launches: list[dict[str, object]] = []

    def _start(**payload):
        launches.append(dict(payload))
        job = {
            "job_id": "health-job-1",
            "operation": "refresh_group",
            "status": "running",
            "started_at": "2026-08-29T06:00:00Z",
        }
        jobs.jobs[str(job["job_id"])] = job
        return deepcopy(job)

    shell.start_sheet_source_group_refresh_job = _start
    return shell, launches


def main() -> None:
    _assert_timeout_cycle_is_durable_and_single_flight_confirmation_is_bounded()
    _assert_sku_no_event_status_is_covered()
    evaluation = evaluate_web_vitrina_health(runtime=_FakeRuntime(), now=NOW, history_days=2)
    cells = {
        (item["source_key"], item["date_role"]): item
        for item in evaluation["expectation_matrix"]
    }
    assert len(evaluation["expectation_matrix"]) == 28
    assert "cost_price" not in {item["source_key"] for item in evaluation["expectation_matrix"]}
    assert "onec_stocks" not in {item["source_key"] for item in evaluation["expectation_matrix"]}
    assert cells[("sku_action_events", "yesterday_closed")]["expectation_state"] == "no_events"
    assert cells[("own_product_capital", "today_current")]["expectation_state"] == "exact_zero"
    assert cells[("stocks", "today_current")]["expectation_state"] == "inapplicable"
    assert cells[("fin_report_daily", "today_current")]["expectation_state"] == "accepted_fallback"
    assert cells[("spp", "yesterday_closed")]["expectation_state"] == "missing"
    assert cells[("spp_proxy", "yesterday_closed")]["expectation_state"] == "partial"
    assert evaluation["signals"]["yesterday_closed"]["state"] == "incomplete"
    assert evaluation["signals"]["today_current"]["state"] == "incomplete"
    assert evaluation["signals"]["bot_health"]["state"] == "ok"
    assert evaluation["bot_date_observations"]["incomplete_count"] >= 1
    assert not any(
        item["source_key"] == "seller_funnel_snapshot" and item["date"] == TODAY
        for item in evaluation["bot_date_observations"]["gaps"]
    )
    assert all(not item["apply_allowed"] for item in evaluation["recovery_preview"]["actions"])

    legacy_runtime = _FakeRuntime()
    legacy_sku = next(
        item for item in legacy_runtime.refresh_status.source_outcomes
        if item["source_key"] == "sku_action_events"
    )
    for item in legacy_sku["slots"]:
        item["covered_count"] = 0
        item["status"] = "warning"
        item["note"] = "missing rows mean no confirmed change, not zero"
    legacy_evaluation = evaluate_web_vitrina_health(runtime=legacy_runtime, now=NOW, history_days=2)
    assert all(
        item["expectation_state"] == "no_events"
        for item in legacy_evaluation["expectation_matrix"]
        if item["source_key"] == "sku_action_events"
    )

    repair_registry = {
        "contract_name": HISTORICAL_REPAIR_CONTRACT,
        "status": "historical_repair_required",
        "dates": {
            YESTERDAY: {
                "status": "historical_repair_required",
                "functional_version_id": "whfv_closed",
                "ordinary_publication_applied": False,
                "repair_contract": "version_bound_historical_reconciliation",
                "issues": [
                    {
                        "scope": "SKU:801",
                        "nm_id": 801,
                        "family": "our_wb_cost_proxy_3_4",
                        "component": "inventory_cost_evidence",
                        "reason_codes": ["ff_stage_evidence_mismatch"],
                        "metric_keys": ["our_wb_unit_cost_rub"],
                        "last_good_preserved": True,
                    }
                ],
            }
        },
    }
    repair_runtime = _FakeRuntime()
    repair_runtime.repair_registry = repair_registry
    repair_evaluation = evaluate_web_vitrina_health(
        runtime=repair_runtime,
        now=NOW,
        history_days=2,
    )
    repair_cell = next(
        item
        for item in repair_evaluation["expectation_matrix"]
        if item["source_key"] == "warehouse_functional_history"
    )
    assert repair_cell["expectation_state"] == "failure"
    assert repair_cell["affected_dates"] == [YESTERDAY]
    assert repair_cell["last_good_preserved_count"] == 1
    assert repair_evaluation["signals"]["yesterday_closed"]["state"] == "error"

    seller_gap = deepcopy(evaluation["expectation_matrix"])
    seller_cell = next(
        item for item in seller_gap
        if item["source_key"] == "seller_funnel_snapshot"
        and item["date_role"] == "yesterday_closed"
    )
    seller_cell["expectation_state"] = "missing"
    from packages.application.sheet_vitrina_v1_health import build_recovery_preview
    seller_preview = build_recovery_preview(matrix=seller_gap, target_date=YESTERDAY)
    assert any(
        item["source_group_id"] == "seller_portal_bot"
        and item["hook"] == "group_refresh"
        for item in seller_preview["actions"]
    )

    observing_runtime = _HealthSurfaceRuntime(
        _operator_payload(yesterday_state="incomplete", bot_state="error"),
        phases=("shadow",),
    )
    observing_surface = build_web_vitrina_health_operator_surface(
        runtime=observing_runtime,
        now=NOW,
    )
    assert observing_surface["morning_cycle"]["state"] == "observing"
    assert {item["state"] for item in observing_surface["indicators"]} == {"observing"}

    confirmed_runtime = _HealthSurfaceRuntime(
        _operator_payload(),
        phases=("candidate", "confirmation"),
    )
    confirmed_surface = build_web_vitrina_health_operator_surface(
        runtime=confirmed_runtime,
        now=NOW,
    )
    confirmed_by_id = {item["indicator_id"]: item for item in confirmed_surface["indicators"]}
    assert confirmed_by_id["yesterday_closed"]["state"] == "ok"
    assert confirmed_by_id["today_current"]["state"] == "observing"
    assert confirmed_by_id["bot_health"]["state"] == "ok"
    repair_surface = build_web_vitrina_health_operator_surface(
        runtime=_HealthSurfaceRuntime(
            _operator_payload(),
            phases=("candidate", "confirmation"),
            repair_registry=repair_registry,
        ),
        now=datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc),
    )
    repair_indicators = {
        item["indicator_id"]: item for item in repair_surface["indicators"]
    }
    assert repair_indicators["yesterday_closed"]["state"] == "error"
    repair_group = next(
        item
        for item in repair_surface["source_groups"]
        if item["source_group_id"] == "warehouse_history"
    )
    assert repair_group["source_group_label"] == "История складских метрик"
    repair_action = next(
        item
        for item in repair_surface["recovery_preview"]["actions"]
        if item["source_group_id"] == "warehouse_history"
    )
    assert repair_action["operator_apply_allowed"] is False
    assert repair_action["hook"] == "none"
    after_boundary = build_web_vitrina_health_operator_surface(
        runtime=confirmed_runtime,
        now=datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc),
    )
    assert {item["indicator_id"]: item for item in after_boundary["indicators"]}["today_current"]["state"] == "ok"

    bot_failure_runtime = _HealthSurfaceRuntime(
        _operator_payload(bot_state="error"),
        phases=("candidate", "confirmation"),
    )
    bot_failure_surface = build_web_vitrina_health_operator_surface(
        runtime=bot_failure_runtime,
        now=datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc),
    )
    bot_failure_by_id = {item["indicator_id"]: item for item in bot_failure_surface["indicators"]}
    assert bot_failure_by_id["yesterday_closed"]["state"] == "ok"
    assert bot_failure_by_id["bot_health"]["state"] == "error"

    recovery_runtime = _HealthSurfaceRuntime(
        _operator_payload(yesterday_state="incomplete", recoverable=True),
        phases=("candidate", "confirmation"),
    )
    recovery_jobs = _HealthJobs()
    recovery_shell, recovery_launches = _health_entrypoint_shell(recovery_runtime, recovery_jobs)
    recovery_surface = recovery_shell.handle_sheet_web_vitrina_health_request()
    recovery_action = next(
        item for item in recovery_surface["recovery_preview"]["actions"]
        if item["hook"] == "group_refresh"
    )
    request = {
        "observation_id": recovery_surface["latest_observation"]["observation_id"],
        "plan_fingerprint": recovery_surface["recovery_preview"]["plan_fingerprint"],
        "action_fingerprint": recovery_action["action_fingerprint"],
        "source_group_id": recovery_action["source_group_id"],
        "target_date": recovery_action["target_date"],
    }
    started = recovery_shell.handle_sheet_web_vitrina_health_recovery_start_request(request)
    assert started["status"] == "running" and started["idempotent_replay"] is False
    replay = recovery_shell.handle_sheet_web_vitrina_health_recovery_start_request(request)
    assert replay["job_id"] == started["job_id"] and replay["idempotent_replay"] is True
    assert len(recovery_launches) == 1
    stale_request = {**request, "action_fingerprint": "sha256:new-action"}
    try:
        recovery_shell.handle_sheet_web_vitrina_health_recovery_start_request(stale_request)
    except SheetVitrinaHealthRecoveryConflict as exc:
        assert exc.reason == "stale_health_action"
    else:
        raise AssertionError("stale recovery action was not rejected")

    active_runtime = _HealthSurfaceRuntime(
        _operator_payload(yesterday_state="incomplete", recoverable=True),
        phases=("candidate", "confirmation"),
    )
    active_jobs = _HealthJobs()
    active_jobs.active = {"job_id": "already-running", "status": "running", "operation": "refresh"}
    active_shell, active_launches = _health_entrypoint_shell(active_runtime, active_jobs)
    active_surface = active_shell.handle_sheet_web_vitrina_health_request()
    active_action = active_surface["recovery_preview"]["actions"][0]
    try:
        active_shell.handle_sheet_web_vitrina_health_recovery_start_request(
            {
                "observation_id": active_surface["latest_observation"]["observation_id"],
                "plan_fingerprint": active_surface["recovery_preview"]["plan_fingerprint"],
                "action_fingerprint": active_action["action_fingerprint"],
                "source_group_id": active_action["source_group_id"],
                "target_date": active_action["target_date"],
            }
        )
    except SheetVitrinaHealthRecoveryConflict as exc:
        assert exc.reason == "active_vitrina_writer"
    else:
        raise AssertionError("active writer was not rejected")
    assert active_launches == []

    unsupported_runtime = _HealthSurfaceRuntime(
        _operator_payload(yesterday_state="incomplete", unsupported=True),
        phases=("candidate", "confirmation"),
    )
    unsupported_jobs = _HealthJobs()
    unsupported_shell, unsupported_launches = _health_entrypoint_shell(unsupported_runtime, unsupported_jobs)
    unsupported_surface = unsupported_shell.handle_sheet_web_vitrina_health_request()
    unsupported_action = unsupported_surface["recovery_preview"]["actions"][0]
    assert unsupported_action["operator_apply_allowed"] is False
    assert "историческое восстановление недоступно" in unsupported_action["reason"]
    try:
        unsupported_shell.handle_sheet_web_vitrina_health_recovery_start_request(
            {
                "observation_id": unsupported_surface["latest_observation"]["observation_id"],
                "plan_fingerprint": unsupported_surface["recovery_preview"]["plan_fingerprint"],
                "action_fingerprint": unsupported_action["action_fingerprint"],
                "source_group_id": unsupported_action["source_group_id"],
                "target_date": unsupported_action["target_date"],
            }
        )
    except SheetVitrinaHealthRecoveryConflict as exc:
        assert exc.reason == "unsupported_health_recovery"
    else:
        raise AssertionError("unsupported recovery was not rejected")
    assert unsupported_launches == []

    with tempfile.TemporaryDirectory(prefix="web-vitrina-health-") as temp_dir:
        db_path = Path(temp_dir) / "operational.sqlite"
        runtime = RegistryUploadDbBackedRuntime(Path(temp_dir), operational_db_path=db_path)
        receipt = persist_web_vitrina_health_evaluation(
            runtime=runtime,
            evaluation=evaluation,
            phase="shadow",
            observed_at="2026-08-29T03:01:00Z",
        )
        assert receipt["status"] == "appended"
        assert receipt["transition_count"] == 3
        incomplete = mark_web_vitrina_health_cycle_incomplete(
            evaluation,
            phase="candidate",
            failure_code="poll_deadline_exceeded",
            reason="bounded fixture timeout",
            observed_at="2026-08-29T03:02:00Z",
            job={"job_id": "job-timeout", "status": "running", "operation": "auto_update"},
        )
        incomplete_receipt = persist_web_vitrina_health_evaluation(
            runtime=runtime,
            evaluation=incomplete,
            phase="candidate",
            observed_at="2026-08-29T03:02:00Z",
        )
        assert incomplete_receipt["status"] == "appended"
        assert incomplete_receipt["transition_count"] >= 1
        assert incomplete["signals"]["yesterday_closed"]["state"] == "incomplete"
        assert incomplete["signals"]["bot_health"]["state"] == "error"
        assert incomplete["fingerprint"] != evaluation["fingerprint"]
        repeated = persist_web_vitrina_health_evaluation(
            runtime=runtime,
            evaluation=evaluation,
            phase="shadow",
            observed_at="2026-08-29T03:02:00Z",
        )
        assert repeated["status"] == "already_recorded"
        latest = runtime.load_latest_sheet_vitrina_health_observation(business_date=TODAY)
        assert latest and latest["payload_fingerprint"] == incomplete["fingerprint"]
        durable_receipt = runtime.save_sheet_vitrina_health_recovery_receipt(
            action_fingerprint="sha256:durable-action",
            plan_fingerprint="sha256:durable-plan",
            observation_id=str(latest["observation_id"]),
            target_date=YESTERDAY,
            source_group_id="wb_api",
            job_id="job-1",
            created_at="2026-08-29T03:03:00Z",
        )
        repeated_receipt = runtime.save_sheet_vitrina_health_recovery_receipt(
            action_fingerprint="sha256:durable-action",
            plan_fingerprint="sha256:different-plan",
            observation_id=str(latest["observation_id"]),
            target_date=YESTERDAY,
            source_group_id="wb_api",
            job_id="job-2",
            created_at="2026-08-29T03:04:00Z",
        )
        assert durable_receipt == repeated_receipt
        assert runtime.load_sheet_vitrina_health_recovery_receipt("sha256:durable-action") == durable_receipt
        with sqlite3.connect(runtime.store_registry.resolve("operational")) as conn:
            try:
                conn.execute(
                    "UPDATE sheet_vitrina_v1_health_observations SET phase='changed'"
                )
            except sqlite3.DatabaseError as exc:
                assert "append-only" in str(exc)
            else:
                raise AssertionError("health observation update was not rejected")

    shell = object.__new__(RegistryUploadHttpEntrypoint)
    shell.start_sheet_refresh_job = lambda *, as_of_date, auto_load: {
        "as_of_date": as_of_date,
        "auto_load": auto_load,
    }
    candidate = RegistryUploadHttpEntrypoint.start_sheet_auto_refresh_job(
        shell,
        trigger_source=SHEET_VITRINA_HEALTH_CANDIDATE_TRIGGER,
    )
    assert candidate == {"as_of_date": None, "auto_load": True}

    systemd_dir = ROOT / "artifacts" / "registry_upload_http_entrypoint" / "systemd"
    candidate_timer = (systemd_dir / "wb-core-sheet-vitrina-health-candidate.timer").read_text()
    confirmation_timer = (systemd_dir / "wb-core-sheet-vitrina-health-confirmation.timer").read_text()
    candidate_service = (systemd_dir / "wb-core-sheet-vitrina-health-candidate.service").read_text()
    confirmation_service = (systemd_dir / "wb-core-sheet-vitrina-health-confirmation.service").read_text()
    assert "06:30:00 Asia/Yekaterinburg" in candidate_timer
    assert "07:30:00 Asia/Yekaterinburg" in confirmation_timer
    assert "Persistent=" not in candidate_timer + confirmation_timer
    assert "--timeout-seconds 1500" in candidate_service + confirmation_service
    assert candidate_service.count("TimeoutStartSec=1800") == 1
    assert confirmation_service.count("TimeoutStartSec=1800") == 1
    target = json.loads(
        (ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "hosted_runtime_target__europe_api.json").read_text()
    )
    managed = {item["name"]: item for item in target["managed_systemd_units"]}
    assert managed["wb-core-sheet-vitrina-health-candidate.timer"]["enable"] is True
    assert managed["wb-core-sheet-vitrina-health-confirmation.timer"]["enable"] is True
    print("sheet_vitrina_v1 health smoke: ok")


def _assert_timeout_cycle_is_durable_and_single_flight_confirmation_is_bounded() -> None:
    runtime = _TickRuntime()
    args = SimpleNamespace(
        phase="candidate",
        dry_run=False,
        timeout_seconds=1500,
        poll_seconds=0.1,
    )
    launches: list[dict[str, object]] = []
    polls: list[str] = []

    def post_candidate(url, payload, *, cookie, timeout):
        del url, payload, cookie, timeout
        launch = {
            "job_id": "long-running-job",
            "status": "running",
            "operation": "auto_update",
            "single_flight": False,
        }
        launches.append(launch)
        return launch

    def poll_timeout(**payload):
        polls.append(str(payload["job_id"]))
        raise JobPollDeadlineError("fixture deadline")

    exit_code = _execute_health_tick(
        runtime=runtime,
        base_url="http://fixture",
        cookie="fixture",
        args=args,
        post_json=post_candidate,
        poll_job=poll_timeout,
    )
    assert exit_code == 1
    assert len(launches) == 1 and polls == ["long-running-job"]
    assert [item["phase"] for item in runtime.observations] == ["candidate"]
    candidate_payload = runtime.observations[0]["payload"]
    assert candidate_payload["morning_cycle_execution"]["failure_code"] == "poll_deadline_exceeded"

    args.phase = "confirmation"
    confirmation_posts: list[dict[str, object]] = []

    def post_single_flight(url, payload, *, cookie, timeout):
        del url, payload, cookie, timeout
        response = {
            "job_id": "long-running-job",
            "status": "running",
            "operation": "auto_update",
            "single_flight": True,
        }
        confirmation_posts.append(response)
        return response

    exit_code = _execute_health_tick(
        runtime=runtime,
        base_url="http://fixture",
        cookie="fixture",
        args=args,
        post_json=post_single_flight,
        poll_job=lambda **payload: (_ for _ in ()).throw(
            AssertionError(f"confirmation must not poll active single-flight: {payload}")
        ),
    )
    assert exit_code == 1
    assert len(confirmation_posts) == 1
    assert [item["phase"] for item in runtime.observations] == ["candidate", "confirmation"]
    surface = build_web_vitrina_health_operator_surface(
        runtime=runtime,
        now=datetime.now(timezone.utc),
    )
    assert surface["morning_cycle"]["pair_complete"] is True
    indicators = {item["indicator_id"]: item for item in surface["indicators"]}
    assert indicators["yesterday_closed"]["state"] == "incomplete"
    assert indicators["bot_health"]["state"] == "error"
    assert surface["latest_observation"]["cycle_execution"]["failure_code"] == "active_single_flight"
    morning_group = next(
        item for item in surface["source_groups"]
        if item["source_group_id"] == "health_runtime"
    )
    assert morning_group["source_group_label"] == "Утренний контур"


def _assert_sku_no_event_status_is_covered() -> None:
    temp_dir = tempfile.TemporaryDirectory(prefix="web-vitrina-sku-empty-")
    runtime = RegistryUploadDbBackedRuntime(
        Path(temp_dir.name),
        operational_db_path=Path(temp_dir.name) / "operational.sqlite",
    )
    block = object.__new__(SheetVitrinaV1LivePlanBlock)
    block.runtime = runtime
    block.now_factory = lambda: NOW
    config = [
        ConfigV2Item(
            nm_id=101,
            enabled=True,
            display_name="SKU",
            group="Test",
            display_order=1,
        )
    ]
    sources = SheetVitrinaV1LivePlanBlock._load_live_sources(
        block,
        enabled_config=config,
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="today_current",
                slot_label="Today",
                column_date=TODAY,
            )
        ],
        cost_price_state=None,
        source_keys={"sku_action_events"},
        diagnostics={},
    )
    status = next(item for item in sources.statuses if item.source_key == "sku_action_events")
    assert status.kind == "success"
    assert status.requested_count == status.covered_count == 1
    assert "confirmed_event_rows=0" in status.note
    assert "empty_semantics=no_confirmed_event" in status.note
    temp_dir.cleanup()


if __name__ == "__main__":
    main()
