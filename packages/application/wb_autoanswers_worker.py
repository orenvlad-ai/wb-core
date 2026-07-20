"""Durable processing worker that invokes the frozen Node pipeline."""

from __future__ import annotations

from typing import Any

from packages.application.wb_autoanswers_media import AutoanswersMediaProcessor
from packages.application.wb_autoanswers_node_bridge import (
    NodeAutoanswersBridge,
    NodeBoundaryError,
    build_frozen_raw_input,
)
from packages.application.wb_autoanswers_runtime import AutoanswersRepository, AutoanswersRuntimeError


class AutoanswersProcessingWorker:
    def __init__(
        self,
        *,
        repository: AutoanswersRepository,
        bridge: NodeAutoanswersBridge,
        media_processor: AutoanswersMediaProcessor,
        worker_id: str,
    ) -> None:
        self.repository = repository
        self.bridge = bridge
        self.media_processor = media_processor
        self.worker_id = worker_id

    def run_once(
        self,
        *,
        execution_mode: str = "live",
        fixture_scenario: str | None = None,
    ) -> dict[str, Any] | None:
        claimed = self.repository.claim_processing_job(worker_id=self.worker_id)
        if claimed is None:
            return None
        key = str(claimed["processing_key"])
        try:
            self.repository.assert_effective_on(operation="media processing")
            media = self.media_processor.process(
                feedback_id=str(claimed["feedback_id"]),
                content_version=int(claimed["content_version"]),
            )
            if bool(media.get("media_uncertain")):
                stored = self.repository.complete_media_uncertainty(
                    key,
                    uncertainty=media.get("uncertainty") or [],
                    worker_id=self.worker_id,
                )
                return {
                    "processing_key": key,
                    "state": stored["state"],
                    "regeneration_required": True,
                    "model_calls": 0,
                }
            self.repository.assert_effective_on(operation="frozen AI invocation")
            detail = self.repository.get_feedback(str(claimed["feedback_id"]))
            if detail is None:
                raise AutoanswersRuntimeError("feedback not found", code="feedback_not_found")
            raw_input = build_frozen_raw_input(detail, processing_key=key)
            node = self.bridge.run(
                processing_key=key,
                raw_input=raw_input,
                execution_mode=execution_mode,
                fixture_scenario=fixture_scenario,
            )
            self.repository.append_node_audit(key, node.get("audit") or [])
            pipeline = node["pipeline"]
            result = pipeline["result"]
            if result.get("publication_action") == "skip":
                stored = self.repository.complete_skip(
                    key,
                    reason=str(result.get("skip_reason") or "prefilter_skip"),
                    worker_id=self.worker_id,
                )
                return {"processing_key": key, "state": stored["state"], "model_calls": 0}
            actual_cost = pipeline.get("estimated_cost_usd") or 0
            self.repository.settle_budget(key, actual_cost_usd=actual_cost)
            stored = self.repository.complete_generation(
                key,
                result={
                    "final_route": result.get("route"),
                    "final_reply": result.get("final_reply"),
                    "case_code": result.get("case_code"),
                    "pipeline_result": result,
                    "usage": pipeline.get("usage") or {},
                    "hard_gates_passed": True,
                    "fallback_used": result.get("outcome") == "fallback",
                    "media_uncertain": bool(media.get("media_uncertain")),
                    "node_contract_valid": True,
                },
                worker_id=self.worker_id,
            )
            return {
                "processing_key": key,
                "state": stored["state"],
                "route": stored["final_route"],
                "cost_usd": float(actual_cost),
                "model_calls": int(pipeline.get("model_calls_this_run") or 0),
            }
        except NodeBoundaryError as exc:
            if exc.retryable:
                self.repository.record_processing_retry(
                    key,
                    error_code=exc.code,
                    retry_after_seconds=60,
                    worker_id=self.worker_id,
                )
            else:
                self.repository.record_processing_terminal(key, error_code=exc.code, worker_id=self.worker_id)
            raise
        except AutoanswersRuntimeError as exc:
            if exc.code in {"master_switch_off", "emergency_force_off"}:
                self.repository.record_processing_retry(
                    key,
                    error_code=exc.code,
                    retry_after_seconds=60,
                    worker_id=self.worker_id,
                )
            else:
                self.repository.record_processing_terminal(key, error_code=exc.code, worker_id=self.worker_id)
            raise
