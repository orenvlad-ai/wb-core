#!/usr/bin/env python3
"""Deterministic smoke checks for the bounded one-night refresh wrapper."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sheet_vitrina_v1_night_refresh_experiment import (  # noqa: E402
    EXPERIMENT_ID,
    SLOTS,
    NightRefreshExperimentRunner,
)


class FakeContour:
    def __init__(self) -> None:
        self.start_calls: list[str] = []
        self.poll_calls: list[str] = []
        self.busy_once = False
        self.payload_revision = 1

    def start(self, slot: object, wrapper_run_id: str) -> Mapping[str, Any]:
        slot_id = str(getattr(slot, "slot_id", ""))
        self.start_calls.append(slot_id)
        if self.busy_once:
            self.busy_once = False
            return {
                "status": "skipped",
                "already_running_job_id": "ordinary-active-job",
                "retryable": True,
                "due_preserved": True,
                "reason": "ordinary canonical refresh is active",
            }
        return {"status": "running", "operation": "auto_update", "job_id": f"job-{slot_id}-{wrapper_run_id[:6]}"}

    def poll(self, job_id: str) -> Mapping[str, Any]:
        self.poll_calls.append(job_id)
        return {
            "job_id": job_id,
            "operation": "auto_update",
            "status": "success",
            "started_at": "2026-08-22T20:30:01Z",
            "finished_at": "2026-08-22T20:36:01Z",
            "log_lines": ["must not be archived"],
            "result": {
                "status": "success",
                "semantic_status": "success",
                "as_of_date": "2026-08-22",
                "snapshot_id": f"snapshot-{self.payload_revision}",
                "source_outcome_counts": {"success": 3},
                "source_outcomes": [
                    {
                        "source_key": "finance",
                        "status": "success",
                        "captured_at": "2026-08-22T20:35:00Z",
                        "coverage": 33,
                        "fallback": False,
                    }
                ],
                "sheet_row_counts": {"DATA_VITRINA": 99, "STATUS": 3},
                "updated_cell_count": 7,
                "latest_confirmed_cell_count": 0,
            },
        }

    def contract(self) -> Mapping[str, Any]:
        return {
            "contract_name": "web_vitrina_contract",
            "meta": {"as_of_date": "2026-08-22", "revision": self.payload_revision},
            "rows": [{"metric": "orders", "value": self.payload_revision}],
        }

    def source_status(self) -> Mapping[str, Any]:
        return {
            "status_summary": {
                "captured_at": "2026-08-22T20:35:00Z",
                "fallback": False,
                "latest_confirmed": False,
            }
        }


def _runner(runtime_dir: Path, contour: FakeContour, now: datetime) -> NightRefreshExperimentRunner:
    return NightRefreshExperimentRunner(
        runtime_dir=runtime_dir,
        start_refresh=contour.start,
        poll_job=contour.poll,
        fetch_contract=contour.contract,
        fetch_source_status=contour.source_status,
        now_factory=lambda: now,
    )


def main() -> None:
    with TemporaryDirectory(prefix="night-refresh-experiment-") as tmp:
        runtime_dir = Path(tmp)
        contour = FakeContour()
        first_due = SLOTS[0].due_datetime
        runner = _runner(runtime_dir, contour, first_due)

        before = runner.tick(now=first_due - timedelta(seconds=1))
        if before["state"] != "armed" or contour.start_calls:
            raise AssertionError(f"pre-due tick must arm without fetching: {before}")

        first = runner.tick(now=first_due)
        if first["tick_result"]["status"] != "valid" or contour.start_calls != [SLOTS[0].slot_id]:
            raise AssertionError(f"exact due slot must call canonical refresh once: {first}")
        first_path = runtime_dir / "experiments" / EXPERIMENT_ID / f"{SLOTS[0].slot_id}.json"
        first_bytes = first_path.read_bytes()
        first_artifact = json.loads(first_bytes)
        if first_artifact["canonical_contract"]["meta"]["as_of_date"] != "2026-08-22":
            raise AssertionError(f"target-date contract missing: {first_artifact}")
        if "log_lines" in first_artifact["job"]:
            raise AssertionError("operator log lines must not be copied into immutable artifact")
        if not first_artifact["fingerprints"]["canonical_payload_sha256"]:
            raise AssertionError(f"canonical payload fingerprint missing: {first_artifact}")

        runner.tick(now=first_due + timedelta(minutes=10))
        restarted = _runner(runtime_dir, contour, first_due + timedelta(minutes=20))
        restarted.tick(now=first_due + timedelta(minutes=20))
        if contour.start_calls != [SLOTS[0].slot_id] or first_path.read_bytes() != first_bytes:
            raise AssertionError("duplicate/restart tick must neither relaunch nor overwrite an artifact")

    with TemporaryDirectory(prefix="night-refresh-busy-") as tmp:
        runtime_dir = Path(tmp)
        contour = FakeContour()
        contour.busy_once = True
        runner = _runner(runtime_dir, contour, SLOTS[0].due_datetime)
        busy = runner.tick(now=SLOTS[0].due_datetime)
        if busy["tick_result"]["status"] != "busy_retry":
            raise AssertionError(f"active job must retain slot for bounded retry: {busy}")
        success = runner.tick(now=SLOTS[0].due_datetime + timedelta(minutes=10))
        if success["tick_result"]["status"] != "valid" or len(contour.start_calls) != 2:
            raise AssertionError(f"free retry window must launch once after busy evidence: {success}")
        attempts = list((runtime_dir / "experiments" / EXPERIMENT_ID / "attempts" / SLOTS[0].slot_id).glob("*.json"))
        if len(attempts) < 4:
            raise AssertionError(f"trigger/busy/retry/accepted evidence must remain append-only: {attempts}")

    with TemporaryDirectory(prefix="night-refresh-expiry-") as tmp:
        runtime_dir = Path(tmp)
        contour = FakeContour()
        runner = _runner(runtime_dir, contour, SLOTS[0].due_datetime)
        for index, slot in enumerate(SLOTS, start=1):
            contour.payload_revision = index if index < 3 else 3
            result = runner.tick(now=slot.due_datetime)
            if result["tick_result"]["status"] != "valid":
                raise AssertionError(f"slot {slot.slot_id} must terminalize: {result}")
        comparison_path = runtime_dir / "experiments" / EXPERIMENT_ID / "comparison.json"
        comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
        if len(comparison["slots"]) != 4 or len(comparison["comparisons"]) != 3:
            raise AssertionError(f"comparison must cover exact four immutable slots: {comparison}")
        if comparison["comparisons"][-1]["payload_fingerprints_equal"] is not True:
            raise AssertionError(f"equal adjacent payloads must be reported without finality claim: {comparison}")
        calls_before_next_day = list(contour.start_calls)
        expired = runner.tick(now=SLOTS[-1].deadline + timedelta(days=1))
        if expired["state"] != "terminal" or contour.start_calls != calls_before_next_day:
            raise AssertionError(f"next day must never replay terminal slots: {expired}")

    with TemporaryDirectory(prefix="night-refresh-missed-") as tmp:
        runtime_dir = Path(tmp)
        contour = FakeContour()
        runner = _runner(runtime_dir, contour, SLOTS[-1].deadline + timedelta(minutes=1))
        missed = runner.tick(now=SLOTS[-1].deadline + timedelta(minutes=1))
        if contour.start_calls or missed["state"] != "terminal":
            raise AssertionError(f"expired manifest must fail closed without late replay: {missed}")
        for slot in SLOTS:
            artifact = json.loads(
                (runtime_dir / "experiments" / EXPERIMENT_ID / f"{slot.slot_id}.json").read_text(encoding="utf-8")
            )
            if artifact.get("reason_code") != "slot_deadline_elapsed":
                raise AssertionError(f"missed slot must have explicit terminal reason: {artifact}")

    print("sheet_vitrina_v1_night_refresh_experiment: ok")


if __name__ == "__main__":
    main()
