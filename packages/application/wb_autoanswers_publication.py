"""Durable WB publication worker with mandatory readback reconciliation."""

from __future__ import annotations

from typing import Any, Mapping

from packages.adapters.wb_autoanswers import (
    WbAnswerWritePort,
    WbAutoanswersHttpError,
    WbAutoanswersTransportError,
)
from packages.application.wb_autoanswers_runtime import AutoanswersRepository, AutoanswersRuntimeError


def _readback_answer(detail: Mapping[str, Any] | None) -> str | None:
    if not detail:
        return None
    answer = detail.get("answer")
    if isinstance(answer, Mapping):
        return str(answer.get("text") or "") or None
    return str(answer or "") or None


class AutoanswersPublicationWorker:
    def __init__(
        self,
        *,
        repository: AutoanswersRepository,
        transport: WbAnswerWritePort,
        worker_id: str,
    ) -> None:
        self.repository = repository
        self.transport = transport
        self.worker_id = worker_id

    def run_once(self) -> dict[str, Any] | None:
        claimed = self.repository.claim_publication_job(worker_id=self.worker_id)
        if claimed is None:
            return None
        key = str(claimed["publication_key"])
        if claimed["action"] == "readback":
            return self._readback(claimed)

        # begin_publication_write is the last durable OFF/invariant gate.  From
        # this point any transport exception is ambiguous and can only read back.
        attempt = self.repository.begin_publication_write(key, worker_id=self.worker_id)
        try:
            status = self.transport.create_answer(
                feedback_id=str(attempt["feedback_id"]), text=str(attempt["exact_reply"])
            )
            outcome = "http_response"
        except WbAutoanswersHttpError as exc:
            status = exc.status_code
            outcome = "http_error_ambiguous"
        except WbAutoanswersTransportError:
            status = None
            outcome = "transport_ambiguous"
        self.repository.record_publication_transport(
            key,
            attempt_id=str(attempt["attempt_id"]),
            outcome=outcome,
            http_status=status,
            worker_id=self.worker_id,
        )
        return {"publication_key": key, "state": "publish_pending_readback", "write_attempted": True}

    def _readback(self, claimed: Mapping[str, Any]) -> dict[str, Any]:
        key = str(claimed["publication_key"])
        try:
            detail = self.transport.fetch_detail(str(claimed["feedback_id"]))
        except WbAutoanswersHttpError as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                self.repository.record_publication_readback_retry(
                    key,
                    error_code=f"wb_readback_http_{exc.status_code}",
                    retry_after_seconds=exc.retry_after_seconds or 60,
                    worker_id=self.worker_id,
                )
                return {"publication_key": key, "state": "retryable_error", "write_attempted": False}
            raise
        except WbAutoanswersTransportError:
            self.repository.record_publication_readback_retry(
                key,
                error_code="wb_readback_transport",
                retry_after_seconds=60,
                worker_id=self.worker_id,
            )
            return {"publication_key": key, "state": "retryable_error", "write_attempted": False}
        stored = self.repository.record_publication_readback(
            key,
            answer_text=_readback_answer(detail),
            worker_id=self.worker_id,
        )
        return {"publication_key": key, "state": stored["state"], "write_attempted": False}
