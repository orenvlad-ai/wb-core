#!/usr/bin/env python3
"""Focused smoke for Web Vitrina health, receipts and morning schedule."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import (
    RegistryUploadHttpEntrypoint,
    SHEET_VITRINA_HEALTH_CANDIDATE_TRIGGER,
)
from packages.application.sheet_vitrina_v1_health import (
    evaluate_web_vitrina_health,
    persist_web_vitrina_health_evaluation,
)
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


def main() -> None:
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
        repeated = persist_web_vitrina_health_evaluation(
            runtime=runtime,
            evaluation=evaluation,
            phase="shadow",
            observed_at="2026-08-29T03:02:00Z",
        )
        assert repeated["status"] == "already_recorded"
        latest = runtime.load_latest_sheet_vitrina_health_observation(business_date=TODAY)
        assert latest and latest["payload_fingerprint"] == evaluation["fingerprint"]
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
    assert "06:30:00 Asia/Yekaterinburg" in candidate_timer
    assert "07:30:00 Asia/Yekaterinburg" in confirmation_timer
    assert "Persistent=" not in candidate_timer + confirmation_timer
    target = json.loads(
        (ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "hosted_runtime_target__europe_api.json").read_text()
    )
    managed = {item["name"]: item for item in target["managed_systemd_units"]}
    assert managed["wb-core-sheet-vitrina-health-candidate.timer"]["enable"] is True
    assert managed["wb-core-sheet-vitrina-health-confirmation.timer"]["enable"] is True
    print("sheet_vitrina_v1 health smoke: ok")


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
