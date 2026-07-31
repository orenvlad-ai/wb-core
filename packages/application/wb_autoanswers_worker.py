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
from packages.contracts.wb_autoanswers import (
    PROCESSING_KIND_RATING_ONLY_TEMPLATE,
    PROCESSING_KIND_SAFE_PUBLIC_TEMPLATE,
)


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
            # Recheck the exact enable/policy epoch for every processing kind,
            # including the zero-cost deterministic path.  A claim made before
            # an OFF/downgrade transition must not complete under stale policy.
            self.repository.assert_processing_execution_allowed(key)
            if str(claimed.get("processing_kind") or "") == PROCESSING_KIND_RATING_ONLY_TEMPLATE:
                stored = self.repository.complete_rating_only_template(key, worker_id=self.worker_id)
                return {
                    "processing_key": key,
                    "state": stored["state"],
                    "route": stored["final_route"],
                    "cost_usd": 0.0,
                    "model_calls": 0,
                    "processing_kind": PROCESSING_KIND_RATING_ONLY_TEMPLATE,
                }
            if str(claimed.get("processing_kind") or "") == PROCESSING_KIND_SAFE_PUBLIC_TEMPLATE:
                stored = self.repository.complete_safe_public_template(
                    key,
                    worker_id=self.worker_id,
                )
                return {
                    "processing_key": key,
                    "state": stored["state"],
                    "route": stored["final_route"],
                    "cost_usd": 0.0,
                    "model_calls": 0,
                    "processing_kind": PROCESSING_KIND_SAFE_PUBLIC_TEMPLATE,
                }
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
            self.repository.assert_processing_execution_allowed(key)
            detail = self.repository.get_feedback(str(claimed["feedback_id"]))
            if detail is None:
                raise AutoanswersRuntimeError("feedback not found", code="feedback_not_found")
            raw_input = build_frozen_raw_input(detail, processing_key=key)
            self.repository.mark_provider_call_started(key, worker_id=self.worker_id)
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
            pipeline_result = dict(result)
            if node.get("boundary_adapter"):
                pipeline_result["server_boundary_adapter"] = dict(
                    node["boundary_adapter"]
                )
            stored = self.repository.complete_generation(
                key,
                result={
                    "final_route": result.get("route"),
                    "final_reply": result.get("final_reply"),
                    "case_code": result.get("case_code"),
                    "pipeline_result": pipeline_result,
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
            if exc.partial_cost_usd > 0:
                self.repository.record_failed_processing_usage(
                    key,
                    actual_cost_usd=exc.partial_cost_usd,
                    usage=exc.partial_usage,
                    role_calls=exc.partial_role_calls,
                    error_code=exc.code,
                    worker_id=self.worker_id,
                )
            if (
                exc.partial_cost_usd <= 0
                and exc.code in {
                    "node_timeout",
                    "node_invalid_json",
                    "node_process_exit_1",
                }
            ):
                stored = self.repository.record_processing_boundary_failure(
                    key,
                    error_code=exc.code,
                    worker_id=self.worker_id,
                    diagnostics=exc.diagnostics,
                    max_attempts=2,
                )
                return {
                    "processing_key": key,
                    "state": stored["state"],
                    "error_code": stored["last_error_code"],
                    "bounded_retry": stored["state"] == "retryable_error",
                    "uncertainty_accounting": "conservative_upper_bound",
                    "model_calls": 0,
                }
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
            if exc.code in {
                "master_switch_off",
                "emergency_force_off",
                "enable_epoch_stale",
                "policy_epoch_stale",
                "manual_pause",
            }:
                self.repository.record_processing_retry(
                    key,
                    error_code=exc.code,
                    retry_after_seconds=60,
                    worker_id=self.worker_id,
                )
            else:
                if exc.code in {"reservation_missing", "stale_content_version"}:
                    recovered = self.repository.recover_completed_node_result(
                        key,
                        actor_id=self.worker_id,
                    )
                    if recovered is not None:
                        return {
                            "processing_key": key,
                            "state": recovered["state"],
                            "route": recovered["final_route"],
                            "cost_usd": float(recovered.get("actual_cost_usd") or 0),
                            "model_calls": 0,
                            "recovered_from_audit": True,
                        }
                    try:
                        queued = self.repository.schedule_safe_public_recovery(
                            key,
                            error_code=exc.code,
                            actor_id=self.worker_id,
                        )
                    except AutoanswersRuntimeError:
                        # A content version can become stale between claim and
                        # recovery.  Never convert that obsolete version into a
                        # current public answer, and do not mask the original
                        # processing boundary with a second recovery exception.
                        self.repository.record_processing_terminal(
                            key,
                            error_code=exc.code,
                            worker_id=self.worker_id,
                        )
                        raise exc
                    return {
                        "processing_key": key,
                        "state": queued["state"],
                        "error_code": exc.code,
                        "safe_public_recovery": True,
                        "model_calls": 0,
                    }
                self.repository.record_processing_terminal(key, error_code=exc.code, worker_id=self.worker_id)
            raise
