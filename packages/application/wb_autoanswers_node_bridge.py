"""Narrow versioned Python -> frozen Node pipeline boundary."""

from __future__ import annotations

import base64
import hashlib
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
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        partial_cost_usd: float = 0.0,
        partial_usage: Mapping[str, Any] | None = None,
        partial_role_calls: int = 0,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.partial_cost_usd = max(0.0, float(partial_cost_usd or 0))
        self.partial_usage = dict(partial_usage or {})
        self.partial_role_calls = max(0, int(partial_role_calls or 0))
        self.diagnostics = dict(diagnostics or {})


def _data_url(path_value: str | None, mime_type: str | None = None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_file():
        return None
    size = path.stat().st_size
    if size <= 0 or size > MAX_EMBEDDED_IMAGE_BYTES:
        return None
    mime = str(mime_type or "").strip().lower() or mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        return None
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def build_frozen_raw_input(feedback: Mapping[str, Any], *, processing_key: str) -> dict[str, Any]:
    product = feedback.get("productDetails") if isinstance(feedback.get("productDetails"), Mapping) else {}
    media_rows = feedback.get("media") if isinstance(feedback.get("media"), list) else []
    photos: list[dict[str, Any]] = []
    videos: list[Mapping[str, Any]] = []
    frames: list[tuple[str, str]] = []
    media_uncertain = False
    for row in media_rows:
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("kind") or "")
        status = str(row.get("fetch_status") or "pending")
        local_ref = _data_url(
            str(row.get("local_path") or "") or None,
            str(row.get("mime_type") or "") or None,
        )
        if kind == "photo":
            fetch_status = "downloaded" if local_ref else "fetch_failed" if status == "fetch_failed" else "not_requested"
            photos.append(
                {
                    "full_size_url": row.get("stable_full_url") or None,
                    "mini_size_url": row.get("stable_preview_url") or None,
                    "fetch_status": fetch_status,
                    "local_ref": local_ref,
                }
            )
            media_uncertain = media_uncertain or fetch_status == "fetch_failed"
        elif kind == "video":
            videos.append(row)
            media_uncertain = media_uncertain or status != "frames_extracted"
        elif kind == "video_frame" and local_ref:
            frames.append((local_ref, str(row.get("sha256") or "")))
    video = videos[0] if videos else None
    video_status = "none"
    if video:
        if frames:
            video_status = "frames_extracted"
        elif str(video.get("fetch_status")) == "fetch_failed":
            video_status = "fetch_failed"
        else:
            video_status = "not_processed"
    preview_ref = (
        _data_url(
            str(video.get("preview_local_path") or "") or None,
            str(video.get("preview_mime_type") or "") or None,
        )
        if video else None
    )
    answer = feedback.get("answer") if isinstance(feedback.get("answer"), Mapping) else {}
    video_frame_refs: list[str] = [preview_ref] if preview_ref else []
    preview_sha = str(video.get("preview_sha256") or "") if video else ""
    skipped_derived_preview = False
    for ref, frame_sha in frames[:4]:
        if preview_ref and preview_sha and frame_sha == preview_sha and not skipped_derived_preview:
            skipped_derived_preview = True
            continue
        video_frame_refs.append(ref)
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
                "source_url": video.get("stable_full_url") if video else None,
                "processing_status": video_status,
                # The frozen media contract has one image list for sampled
                # video.  Keep the WB preview first, followed by at most four
                # deterministic frames; the frozen payload builder appends
                # them after the classifier cache breakpoint.
                "frame_refs": video_frame_refs[:5],
            },
        },
        "history": {"previous_public_reply": answer.get("text") or None},
    }
    raw["_server_media_uncertain"] = media_uncertain
    raw["_server_content_bearing_prefilter"] = bool(
        any(str(value or "").strip() for value in (raw["text"], raw["pros"], raw["cons"]))
        or raw["wb_tags"]
        or photos
        or video
    )
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
        payload_raw = {
            key: value
            for key, value in raw_input.items()
            if not str(key).startswith("_server_")
        }
        adapter: dict[str, Any] | None = None
        if (
            bool(raw_input.get("_server_content_bearing_prefilter"))
            and int(payload_raw.get("rating") or 0) == 5
            and not any(
                str(payload_raw.get(field) or "").strip()
                for field in ("text", "pros", "cons")
            )
        ):
            tags = [
                str(item).strip()
                for item in payload_raw.get("wb_tags") or []
                if str(item).strip()
            ]
            media = (
                payload_raw.get("media")
                if isinstance(payload_raw.get("media"), Mapping)
                else {}
            )
            photos = (
                media.get("photos")
                if isinstance(media.get("photos"), list)
                else []
            )
            video = (
                media.get("video")
                if isinstance(media.get("video"), Mapping)
                else {}
            )
            evidence_parts: list[str] = []
            if tags:
                evidence_parts.append("Теги отзыва Wildberries: " + "; ".join(tags[:50]))
            if photos:
                evidence_parts.append(f"К отзыву приложено фото: {len(photos)}")
            if bool(video.get("present")):
                evidence_parts.append("К отзыву приложено видео")
            if evidence_parts:
                payload_raw["text"] = ". ".join(evidence_parts)
                adapter = {
                    "contract": "content_bearing_prefilter_adapter_v1",
                    "source_fields": [
                        *([] if not tags else ["wb_tags"]),
                        *([] if not photos else ["media.photos"]),
                        *([] if not bool(video.get("present")) else ["media.video"]),
                    ],
                    "frozen_bundle_changed": False,
                    "original_text_fields_empty": True,
                }
        payload: dict[str, Any] = {
            "boundary_version": NODE_BOUNDARY_VERSION,
            "operation": "run",
            "processing_key": processing_key,
            "execution_mode": execution_mode,
            "raw_input": payload_raw,
        }
        if fixture_scenario:
            payload["fixture_scenario"] = fixture_scenario
        result = self._invoke(payload)
        if adapter is not None:
            result["boundary_adapter"] = adapter
        return result

    def guard_final(
        self,
        *,
        review_id: str,
        review_version: int | str,
        route: str,
        case_code: str | None,
        reply: str,
        primary_issue: str | None,
    ) -> dict[str, Any]:
        """Run the untouched frozen deterministic final guard for an exact manual edit."""

        data = self._invoke(
            {
                "boundary_version": NODE_BOUNDARY_VERSION,
                "operation": "guard_final",
                "guard_input": {
                    "review_id": str(review_id),
                    "review_version": str(review_version),
                    "route": str(route),
                    "case_code": case_code,
                    "reply": str(reply),
                    "primary_issue": primary_issue,
                },
            }
        )
        guard = data.get("guard") if isinstance(data.get("guard"), Mapping) else {}
        return {
            "passed": bool(guard.get("passed")),
            "errors": [str(item) for item in guard.get("errors") or []],
            "reply": str(guard.get("reply") or ""),
        }

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
            code = "node_invalid_json" if completed.returncode == 0 else f"node_process_exit_{completed.returncode}"
            stderr_bytes = completed.stderr.encode("utf-8", errors="replace")
            stdout_bytes = completed.stdout.encode("utf-8", errors="replace")
            raise NodeBoundaryError(
                "frozen Node boundary returned no valid JSON",
                code=code,
                retryable=completed.returncode in {-9, -15, 1},
                diagnostics={
                    "returncode": completed.returncode,
                    "stderr_bytes": len(stderr_bytes),
                    "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
                    "stdout_bytes": len(stdout_bytes),
                    "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
                    "raw_output_persisted": False,
                },
            ) from exc
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
            partial_usage = error.get("partial_usage") if isinstance(error.get("partial_usage"), Mapping) else {}
            raise NodeBoundaryError(
                str(error.get("message") or "Node boundary failed"),
                code=code,
                retryable=retryable,
                partial_cost_usd=float(error.get("partial_cost_usd") or 0),
                partial_usage=partial_usage,
                partial_role_calls=int(error.get("partial_role_calls") or 0),
            )
        return dict(response["data"])
