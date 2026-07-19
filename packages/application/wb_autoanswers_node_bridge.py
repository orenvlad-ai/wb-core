"""Narrow versioned Python -> frozen Node pipeline boundary."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping

from packages.contracts.wb_autoanswers import (
    EVALUATION_SIGNATURE,
    NODE_BOUNDARY_VERSION,
    PROMPT_BUNDLE_VERSION,
)


DEFAULT_TIMEOUT_SECONDS = 180
MAX_EMBEDDED_IMAGE_BYTES = 20 * 1024 * 1024


class NodeBoundaryError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _data_url(path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_file():
        return None
    size = path.stat().st_size
    if size <= 0 or size > MAX_EMBEDDED_IMAGE_BYTES:
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def build_frozen_raw_input(feedback: Mapping[str, Any], *, processing_key: str) -> dict[str, Any]:
    product = feedback.get("productDetails") if isinstance(feedback.get("productDetails"), Mapping) else {}
    media_rows = feedback.get("media") if isinstance(feedback.get("media"), list) else []
    photos: list[dict[str, Any]] = []
    videos: list[Mapping[str, Any]] = []
    frames: list[str] = []
    media_uncertain = False
    for row in media_rows:
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("kind") or "")
        status = str(row.get("fetch_status") or "pending")
        local_ref = _data_url(str(row.get("local_path") or "") or None)
        if kind == "photo":
            fetch_status = "downloaded" if local_ref else "fetch_failed" if status == "fetch_failed" else "not_requested"
            photos.append(
                {
                    "full_size_url": row.get("source_full_url") or None,
                    "mini_size_url": row.get("source_preview_url") or None,
                    "fetch_status": fetch_status,
                    "local_ref": local_ref,
                }
            )
            media_uncertain = media_uncertain or fetch_status == "fetch_failed"
        elif kind == "video":
            videos.append(row)
            media_uncertain = media_uncertain or status != "frames_extracted"
        elif kind == "video_frame" and local_ref:
            frames.append(local_ref)
    video = videos[0] if videos else None
    video_status = "none"
    if video:
        if frames:
            video_status = "frames_extracted"
        elif str(video.get("fetch_status")) == "fetch_failed":
            video_status = "fetch_failed"
        else:
            video_status = "not_processed"
    answer = feedback.get("answer") if isinstance(feedback.get("answer"), Mapping) else {}
    raw = {
        "ingestion_id": processing_key,
        "review_id": str(feedback.get("id") or ""),
        "review_version": str(feedback.get("content_version") or ""),
        "created_at": feedback.get("createdDate") or "",
        "received_at": feedback.get("first_seen_at") or feedback.get("createdDate") or "",
        "rating": feedback.get("productValuation"),
        "text": feedback.get("text") or "",
        "pros": feedback.get("pros") or "",
        "cons": feedback.get("cons") or "",
        "wb_tags": feedback.get("tags") or [],
        "nm_id": product.get("nm_id") if product.get("nm_id") is not None else product.get("nmId"),
        "seller_article": product.get("supplier_article") or product.get("supplierArticle"),
        "product_name": product.get("product_name") or product.get("productName"),
        "media": {
            "photos": photos,
            "video": {
                "present": bool(video),
                "source_url": video.get("source_full_url") if video else None,
                "processing_status": video_status,
                "frame_refs": frames[:20],
            },
        },
        "history": {"previous_public_reply": answer.get("text") or None},
    }
    raw["_server_media_uncertain"] = media_uncertain
    return raw


class NodeAutoanswersBridge:
    def __init__(
        self,
        *,
        runner_path: Path | None = None,
        node_binary: str = "node",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.runner_path = runner_path or (
            Path(__file__).resolve().parents[1]
            / "node"
            / "wb_autoanswers_boundary_v1"
            / "runner.mjs"
        )
        self.node_binary = node_binary
        self.timeout_seconds = int(timeout_seconds)
        self.env = dict(env) if env is not None else None

    def verify(self) -> dict[str, Any]:
        return self._invoke({"boundary_version": NODE_BOUNDARY_VERSION, "operation": "verify"})

    def run(
        self,
        *,
        processing_key: str,
        raw_input: Mapping[str, Any],
        execution_mode: str = "live",
        fixture_scenario: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "boundary_version": NODE_BOUNDARY_VERSION,
            "operation": "run",
            "processing_key": processing_key,
            "execution_mode": execution_mode,
            "raw_input": {key: value for key, value in raw_input.items() if not str(key).startswith("_server_")},
        }
        if fixture_scenario:
            payload["fixture_scenario"] = fixture_scenario
        return self._invoke(payload)

    def _invoke(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        environment = os.environ.copy() if self.env is None else dict(self.env)
        try:
            completed = subprocess.run(
                [self.node_binary, str(self.runner_path)],
                input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise NodeBoundaryError("frozen Node boundary timed out", code="node_timeout", retryable=True) from exc
        except OSError as exc:
            raise NodeBoundaryError("frozen Node boundary unavailable", code="node_unavailable") from exc
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise NodeBoundaryError("frozen Node boundary returned invalid JSON", code="node_invalid_json") from exc
        if (
            response.get("boundary_version") != NODE_BOUNDARY_VERSION
            or response.get("bundle_version") != PROMPT_BUNDLE_VERSION
            or response.get("evaluation_signature") != EVALUATION_SIGNATURE
        ):
            raise NodeBoundaryError("frozen Node identity mismatch", code="node_identity_mismatch")
        if completed.returncode != 0 or not bool(response.get("ok")):
            error = response.get("error") if isinstance(response.get("error"), Mapping) else {}
            code = str(error.get("code") or "node_boundary_error")
            retryable = code.startswith("OPENAI_HTTP_429") or code.startswith("OPENAI_HTTP_5") or code == "node_timeout"
            raise NodeBoundaryError(str(error.get("message") or "Node boundary failed"), code=code, retryable=retryable)
        return dict(response["data"])
