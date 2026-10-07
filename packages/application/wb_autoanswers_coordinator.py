"""One bounded background tick for sync, AI processing and publication."""

from __future__ import annotations

from typing import Any
from packages.application.wb_autoanswers_control_lock import autoanswers_control_lock

from packages.application.wb_autoanswers_publication import AutoanswersPublicationWorker
from packages.application.wb_autoanswers_runtime import (
    DEFAULT_POLICY_VERSION,
    AutoanswersRepository,
    AutoanswersRuntimeError,
)
from packages.application.wb_autoanswers_sync import FeedbackSyncError, WbFeedbackSyncService
from packages.application.wb_autoanswers_worker import AutoanswersProcessingWorker


def _error_evidence(stage: str, exc: Exception) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "stage": stage,
        "code": str(getattr(exc, "code", "") or f"{stage}_error"),
        "retryable": bool(getattr(exc, "retryable", False)),
    }
    for source, target in (
        ("wait_ms", "wait_ms"),
        ("retries", "retry_count"),
        ("phase", "contention_phase"),
    ):
        value = getattr(exc, source, None)
        if value is not None:
            evidence[target] = value
    return evidence


class AutoanswersCoordinator:
    """Keeps each invocation bounded to a small, resumable amount of work."""

    def __init__(
        self,
        *,
        repository: AutoanswersRepository,
        sync_service: WbFeedbackSyncService,
        processing_worker: AutoanswersProcessingWorker,
        publication_worker: AutoanswersPublicationWorker,
        worker_id: str,
    ) -> None:
        self.repository = repository
        self.sync_service = sync_service
        self.processing_worker = processing_worker
        self.publication_worker = publication_worker
        self.worker_id = worker_id

    def run_once(self) -> dict[str, Any]:
        try:
            with autoanswers_control_lock(self.repository.runtime_dir):
                return self._run_once_locked()
        except BlockingIOError:
            return {"errors": [], "control_busy": True}

    def _run_once_locked(self) -> dict[str, Any]:
        startup_errors: list[dict[str, Any]] = []
        try:
            coordinator = self.repository.sync_cursor(
                "wb_autoanswers_coordinator"
            )
        except Exception as exc:
            coordinator = None
            startup_errors.append(_error_evidence("coordinator_cursor_read", exc))
        tick = int((coordinator or {}).get("cursor", {}).get("tick") or 0) + 1
        report: dict[str, Any] = {
            "tick": tick,
            "sync": [],
            "full_unanswered_inventory": None,
            "rolling_admission": None,
            "inventory_recovery": None,
            "reconciliation": None,
            "processing": None,
            "publication": None,
            "stale_reservations_released": 0,
            "errors": startup_errors,
        }
        command = None
        try:
            command = self.repository.claim_sync_command(
                worker_id=self.worker_id
            )
            # Two steady pages plus one rotating reconciliation/backfill page
            # keep the official GET request rate bounded per scheduler tick.
            for answered in (False, True):
                report["sync"].append(self.sync_service.steady_sync_tick(is_answered=answered))
            if tick % 12 == 0:
                report["sync"].append(self.sync_service.reconcile_archive_tick())
            else:
                report["sync"].append(self.sync_service.initial_backfill_tick(is_answered=bool(tick % 2)))
            if command:
                self.repository.finish_sync_command(str(command["command_id"]), result={"sync": report["sync"]})
        except FeedbackSyncError as exc:
            report["errors"].append({"stage": "sync", "code": exc.code, "retryable": exc.retryable})
            if command:
                try:
                    self.repository.finish_sync_command(
                        str(command["command_id"]),
                        error_code=exc.code,
                        retry_after_seconds=60 if exc.retryable else None,
                    )
                except Exception as finish_exc:
                    report["errors"].append(
                        _error_evidence("sync_command_finish", finish_exc)
                    )
        except Exception as exc:
            report["errors"].append(_error_evidence("sync", exc))
        try:
            inventory_cursor = self.repository.sync_cursor(
                "wb_feedback_full_unanswered_inventory"
            )
            inventory_active = bool(
                (inventory_cursor or {}).get("cursor", {}).get("active")
            )
            settings = self.repository.settings()
            previous_cursor = dict((coordinator or {}).get("cursor") or {})
            # Discovery runs immediately after startup, a mode epoch change or
            # a long scheduler pause, then remains bounded to one official page.
            from packages.application.wb_autoanswers_runtime import parse_timestamp
            last_run = parse_timestamp((coordinator or {}).get("updated_at"))
            startup_or_resume = (
                tick == 1 or previous_cursor.get("recovery_contract") != 1 or previous_cursor.get("policy_epoch") != settings.policy_epoch
                or last_run is None or self.repository._now().timestamp() - last_run.timestamp() > 180
            )
            if (
                (inventory_active or startup_or_resume or command or tick % 12 == 0)
                and settings.policy_version == DEFAULT_POLICY_VERSION
            ):
                report["full_unanswered_inventory"] = (
                    self.sync_service.full_unanswered_inventory_tick()
                )
        except FeedbackSyncError as exc:
            report["errors"].append(
                {
                    "stage": "full_unanswered_inventory",
                    "code": exc.code,
                    "retryable": exc.retryable,
                }
            )
        except Exception as exc:
            report["errors"].append(
                _error_evidence("full_unanswered_inventory", exc)
            )
        try:
            self.repository.save_sync_cursor(
                "wb_autoanswers_coordinator",
                cursor={"tick": tick, "policy_epoch": self.repository.settings().policy_epoch, "recovery_contract": 1},
                successful=not report["errors"],
            )
        except Exception as exc:
            report["errors"].append(
                _error_evidence("coordinator_cursor_write", exc)
            )
        try:
            report["absent_unanswered_reconciliation"] = self.sync_service.reconcile_absent_unanswered_tick()
        except Exception as exc:
            report["errors"].append(_error_evidence("absent_unanswered_reconciliation", exc))
        try:
            report["inventory_recovery"] = self.repository.recover_unanswered_inventory(actor_id=self.worker_id)
        except Exception as exc:
            report["errors"].append(_error_evidence("inventory_recovery", exc))
        try:
            report["rolling_admission"] = self.repository.refresh_rolling_admissions(
                actor_id=self.worker_id,
                batch_size=250,
            )
        except AutoanswersRuntimeError as exc:
            if exc.code not in {"master_switch_off", "emergency_force_off"}:
                report["errors"].append({"stage": "rolling_admission", "code": exc.code})
        except Exception as exc:
            report["errors"].append(_error_evidence("rolling_admission", exc))
        # Reservation cleanup is local and safe in every mode, including OFF.
        # It never claims work and preserves the reservation row as released
        # evidence, so a crashed worker cannot block future budgets forever.
        try:
            report["stale_reservations_released"] = (
                self.repository.reconcile_stale_reservations()
            )
        except Exception as exc:
            report["errors"].append(_error_evidence("reservation_reconciliation", exc))
        try:
            report["reconciliation"] = self.repository.reconcile_policy_sweep_once(
                worker_id=self.worker_id,
                batch_size=25,
            )
        except AutoanswersRuntimeError as exc:
            if exc.code not in {"master_switch_off", "emergency_force_off"}:
                report["errors"].append({"stage": "reconciliation", "code": exc.code})
        except Exception as exc:
            report["errors"].append(_error_evidence("reconciliation", exc))
        try:
            report["processing"] = self.processing_worker.run_once()
        except AutoanswersRuntimeError as exc:
            if exc.code not in {"master_switch_off", "emergency_force_off"}:
                report["errors"].append({"stage": "processing", "code": exc.code})
        except Exception as exc:
            report["errors"].append(_error_evidence("processing", exc))
        try:
            report["publication"] = self.publication_worker.run_once()
        except AutoanswersRuntimeError as exc:
            report["errors"].append({"stage": "publication", "code": exc.code})
        except Exception as exc:
            report["errors"].append(_error_evidence("publication", exc))
        try:
            self.repository.record_scheduler_tick(errors=report["errors"])
        except Exception as exc:
            report["errors"].append(_error_evidence("scheduler_tick_write", exc))
        return report
