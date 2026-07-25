"""One bounded background tick for sync, AI processing and publication."""

from __future__ import annotations

from typing import Any

from packages.application.wb_autoanswers_publication import AutoanswersPublicationWorker
from packages.application.wb_autoanswers_runtime import AutoanswersRepository, AutoanswersRuntimeError
from packages.application.wb_autoanswers_sync import FeedbackSyncError, WbFeedbackSyncService
from packages.application.wb_autoanswers_worker import AutoanswersProcessingWorker


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
        coordinator = self.repository.sync_cursor("wb_autoanswers_coordinator")
        tick = int((coordinator or {}).get("cursor", {}).get("tick") or 0) + 1
        report: dict[str, Any] = {
            "tick": tick,
            "sync": [],
            "rolling_admission": None,
            "reconciliation": None,
            "processing": None,
            "publication": None,
            "stale_reservations_released": 0,
            "errors": [],
        }
        command = self.repository.claim_sync_command(worker_id=self.worker_id)
        try:
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
                self.repository.finish_sync_command(
                    str(command["command_id"]),
                    error_code=exc.code,
                    retry_after_seconds=60 if exc.retryable else None,
                )
        self.repository.save_sync_cursor(
            "wb_autoanswers_coordinator", cursor={"tick": tick}, successful=not report["errors"]
        )
        try:
            report["rolling_admission"] = self.repository.refresh_rolling_admissions(
                actor_id=self.worker_id,
                batch_size=250,
            )
        except AutoanswersRuntimeError as exc:
            if exc.code not in {"master_switch_off", "emergency_force_off"}:
                report["errors"].append({"stage": "rolling_admission", "code": exc.code})
        # Reservation cleanup is local and safe in every mode, including OFF.
        # It never claims work and preserves the reservation row as released
        # evidence, so a crashed worker cannot block future budgets forever.
        report["stale_reservations_released"] = self.repository.reconcile_stale_reservations()
        try:
            report["reconciliation"] = self.repository.reconcile_policy_sweep_once(
                worker_id=self.worker_id,
                batch_size=25,
            )
        except AutoanswersRuntimeError as exc:
            if exc.code not in {"master_switch_off", "emergency_force_off"}:
                report["errors"].append({"stage": "reconciliation", "code": exc.code})
        try:
            report["processing"] = self.processing_worker.run_once()
        except AutoanswersRuntimeError as exc:
            if exc.code not in {"master_switch_off", "emergency_force_off"}:
                report["errors"].append({"stage": "processing", "code": exc.code})
        except Exception as exc:
            report["errors"].append({"stage": "processing", "code": getattr(exc, "code", "processing_error")})
        try:
            report["publication"] = self.publication_worker.run_once()
        except AutoanswersRuntimeError as exc:
            report["errors"].append({"stage": "publication", "code": exc.code})
        except Exception as exc:
            report["errors"].append({"stage": "publication", "code": getattr(exc, "code", "publication_error")})
        self.repository.record_scheduler_tick(errors=report["errors"])
        return report
