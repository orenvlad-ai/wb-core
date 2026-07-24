#!/usr/bin/env python3
"""Force-off, GET-only production sync/backfill runner for WB feedbacks.

This entrypoint deliberately imports the WB read adapter only.  It has no
OpenAI bridge and no WB answer-writer capability.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV
from packages.adapters.wb_autoanswers import HttpBackedWbAutoanswersReadAdapter, WbFeedbackReadPort
from packages.application.wb_autoanswers_runtime import AutoanswersRepository
from packages.application.wb_autoanswers_media import AutoanswersMediaProcessor
from packages.application.wb_autoanswers_sync import FeedbackSyncError, WbFeedbackSyncService
from packages.contracts.wb_autoanswers import MODE_MANUAL


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
EXTERNAL_GATE_ENV = "WB_AUTOANSWERS_EXTERNAL_IO_ENABLED"
FORCE_OFF_ENV = "WB_AUTOANSWERS_FORCE_OFF"
SAFE_ENV_KEYS = frozenset(
    {
        DEFAULT_WB_API_TOKEN_ENV,
        "WB_FEEDBACKS_API_BASE_URL",
        "OFFICIAL_API_TIMEOUT_SECONDS",
    }
)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _load_safe_env_file(path: Path) -> None:
    """Load only the WB GET adapter's allowlisted settings, without shell evaluation."""

    if not path.is_file():
        raise ValueError(f"environment file does not exist: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in SAFE_ENV_KEYS:
            continue
        lexer = shlex.shlex(raw_value, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            os.environ[key] = " ".join(lexer)
        except ValueError as exc:
            raise ValueError(f"invalid value for environment key {key}") from exc


def _sum_counts(values: dict[str, int]) -> int:
    return sum(int(value) for value in values.values())


def _assert_force_off(repository: AutoanswersRepository) -> None:
    settings = repository.settings()
    if not settings.force_off or settings.effective_enabled:
        raise RuntimeError("read-only production operation requires effective emergency force-off")


def _assert_manual_on(repository: AutoanswersRepository) -> None:
    settings = repository.settings()
    if (
        not settings.master_enabled
        or settings.force_off
        or not settings.effective_enabled
        or settings.mode != MODE_MANUAL
    ):
        raise RuntimeError("manual GET-only canary requires effective manual mode")


def _safe_status(repository: AutoanswersRepository) -> dict[str, Any]:
    status = repository.operational_status()
    status["capabilities"] = {
        "wb_get": True,
        "wb_post_patch": False,
        "openai": False,
        "ai_claim": False,
        "publication_claim": False,
    }
    return status


def run_operation(
    *,
    operation: str,
    repository: AutoanswersRepository,
    source: WbFeedbackReadPort | None,
    now_factory: Callable[[], datetime],
    page_size: int,
    max_pages: int,
    min_request_interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    media_processor: AutoanswersMediaProcessor | None = None,
) -> dict[str, Any]:
    if operation == "status":
        runtime = _safe_status(repository)
        runtime["schema_backup"] = repository.verified_schema_backup_status()
        return {"status": "ready", "operation": operation, "runtime": runtime}
    if source is None:
        raise ValueError("external read source is required")
    if not _truthy(os.environ.get(EXTERNAL_GATE_ENV)):
        raise RuntimeError("external IO gate is OFF")
    if operation in {"manual-canary", "manual-media-canary"}:
        _assert_manual_on(repository)
    else:
        _assert_force_off(repository)
    before = repository.operational_status()
    before_ai = _sum_counts(before["ai_jobs"])
    before_publication = _sum_counts(before["publication_jobs"])

    if operation == "manual-media-canary":
        processor = media_processor
        if processor is None:
            def refresh_urls(feedback_id: str) -> bool:
                detail = source.fetch_detail(feedback_id)
                if not detail:
                    return False
                repository.upsert_feedback(
                    detail,
                    source_stream="detail",
                    run_kind="reconciliation",
                )
                return True

            processor = AutoanswersMediaProcessor(
                repository=repository,
                runtime_dir=repository.runtime_dir,
                refresh_urls=refresh_urls,
            )
        evidence: dict[str, Any] = {}
        for index, kind in enumerate(("photo", "video")):
            candidate = repository.media_canary_candidate(kind)
            if candidate is None:
                raise RuntimeError(f"no unpublished {kind} candidate exists for the bounded media canary")
            if index and min_request_interval_seconds > 0:
                sleep(min_request_interval_seconds)
            feedback_id = str(candidate["feedback_id"])
            detail = source.fetch_detail(feedback_id)
            if not detail:
                raise RuntimeError(f"WB detail GET returned no {kind} feedback")
            outcome = repository.upsert_feedback(
                detail,
                source_stream="detail",
                run_kind="reconciliation",
            )
            result = processor.process(
                feedback_id=feedback_id,
                content_version=int(outcome["content_version"]),
                asset_kinds=frozenset({kind}),
            )
            if result.get("media_uncertain"):
                raise RuntimeError(f"bounded {kind} canary remained media-uncertain")
            if kind == "photo" and int(result.get("photos_downloaded") or 0) < 1:
                raise RuntimeError("bounded photo canary downloaded no validated image")
            if kind == "video" and (
                int(result.get("video_previews") or 0) < 1
                or not 1 <= int(result.get("video_frames") or 0) <= 4
            ):
                raise RuntimeError("bounded video canary produced no preview or bounded frames")
            evidence[kind] = {
                "feedback_id_sha256": hashlib.sha256(feedback_id.encode("utf-8")).hexdigest(),
                "content_version": int(outcome["content_version"]),
                "photos_downloaded": int(result.get("photos_downloaded") or 0),
                "video_previews": int(result.get("video_previews") or 0),
                "video_frames": int(result.get("video_frames") or 0),
            }
        after = repository.operational_status()
        if _sum_counts(after["ai_jobs"]) != before_ai or _sum_counts(after["publication_jobs"]) != before_publication:
            raise RuntimeError("GET-only media canary created AI or publication jobs")
        return {
            "status": "passed",
            "operation": operation,
            "media": evidence,
            "ttl_seconds": processor.ttl_seconds,
            "runtime": _safe_status(repository),
        }

    service = WbFeedbackSyncService(
        repository=repository,
        source=source,
        now_factory=now_factory,
        page_size=page_size,
    )

    if operation in {"canary", "manual-canary"}:
        page = service.steady_sync_tick(is_answered=False)
        if int(page.get("enqueued") or 0) != 0:
            raise RuntimeError("GET-only canary unexpectedly enqueued AI work")
        feedback_id = repository.latest_feedback_id(sync_run_id=str(page["run_id"]))
        if not feedback_id:
            raise RuntimeError("bounded unanswered page was empty; detail GET was not attempted")
        if min_request_interval_seconds > 0:
            sleep(min_request_interval_seconds)
        detail = source.fetch_detail(feedback_id)
        if not detail:
            raise RuntimeError("WB detail GET returned no feedback")
        outcome = repository.upsert_feedback(
            detail,
            source_stream="detail",
            run_kind="reconciliation",
            sync_run_id=str(page["run_id"]),
        )
        after = repository.operational_status()
        if _sum_counts(after["ai_jobs"]) != before_ai or _sum_counts(after["publication_jobs"]) != before_publication:
            raise RuntimeError("read-only canary created AI or publication jobs")
        return {
            "status": "passed",
            "operation": operation,
            "page": page,
            "detail": {
                "feedback_id_sha256": hashlib.sha256(feedback_id.encode("utf-8")).hexdigest(),
                "content_version": outcome["content_version"],
                "has_external_answer": outcome["has_external_answer"],
                "auto_enqueue": outcome["auto_enqueue"],
            },
            "runtime": _safe_status(repository),
        }

    if operation == "steady":
        command = repository.claim_sync_command(worker_id="wb-autoanswers-readonly-steady")
        results: list[dict[str, Any]] = []
        try:
            for index, is_answered in enumerate((False, True)):
                if index and min_request_interval_seconds > 0:
                    sleep(min_request_interval_seconds)
                result = service.steady_sync_tick(is_answered=is_answered)
                if int(result.get("enqueued") or 0) != 0:
                    raise RuntimeError("force-off steady sync unexpectedly enqueued AI work")
                results.append(result)
            coordinator = repository.sync_cursor("wb_autoanswers_readonly_steady")
            tick = int((coordinator or {}).get("cursor", {}).get("tick") or 0) + 1
            if tick % 12 == 0:
                if min_request_interval_seconds > 0:
                    sleep(min_request_interval_seconds)
                results.append(service.reconcile_archive_tick())
            repository.save_sync_cursor(
                "wb_autoanswers_readonly_steady",
                cursor={"tick": tick},
                successful=True,
            )
            if command:
                repository.finish_sync_command(str(command["command_id"]), result={"sync": results})
        except FeedbackSyncError as exc:
            if command:
                repository.finish_sync_command(
                    str(command["command_id"]),
                    error_code=exc.code,
                    retry_after_seconds=exc.retry_after_seconds or (60 if exc.retryable else None),
                )
            raise
        except Exception as exc:
            if command:
                repository.finish_sync_command(
                    str(command["command_id"]),
                    error_code=f"readonly_steady_{type(exc).__name__}",
                )
            raise
        # Each sync tick reports its own causal enqueue count above.  Do not
        # compare store-wide queue totals here: the feature worker shares this
        # database and can legitimately advance AI/publication jobs while the
        # GET-only timer is fetching WB pages.  The runner remains force-off,
        # has no provider/writer imports, and fails immediately if either of
        # its own ticks reports an enqueue.
        return {
            "status": "passed",
            "operation": operation,
            "sync": results,
            "command_processed": bool(command),
            "runtime": _safe_status(repository),
        }

    if operation != "backfill":
        raise ValueError(f"unsupported operation: {operation}")

    calls = 0
    rows = 0
    upserted = 0
    consecutive_retryable_errors = 0
    last_call_at: float | None = None
    streams_complete = {"unanswered": False, "answered": False, "archive": False}
    while calls < max(1, int(max_pages)) and not all(streams_complete.values()):
        made_progress = False
        for stream, is_answered in (("unanswered", False), ("answered", True), ("archive", None)):
            if streams_complete[stream] or calls >= max(1, int(max_pages)):
                continue
            if last_call_at is not None:
                remaining = min_request_interval_seconds - (time.monotonic() - last_call_at)
                if remaining > 0:
                    sleep(remaining)
            try:
                tick = (
                    service.reconcile_archive_tick(resume_cursor=True)
                    if is_answered is None
                    else service.initial_backfill_tick(is_answered=is_answered)
                )
                last_call_at = time.monotonic()
                calls += 1
                rows += int(tick.get("rows") or 0)
                upserted += int(tick.get("upserted") or 0)
                streams_complete[stream] = bool(
                    tick.get("complete")
                    if tick.get("complete") is not None
                    else (tick.get("cursor") or {}).get("complete")
                )
                consecutive_retryable_errors = 0
                made_progress = True
                if int(tick.get("enqueued") or 0) != 0:
                    raise RuntimeError("historical backfill unexpectedly enqueued AI work")
            except FeedbackSyncError as exc:
                last_call_at = time.monotonic()
                if not exc.retryable:
                    raise
                consecutive_retryable_errors += 1
                if consecutive_retryable_errors > 6:
                    raise RuntimeError("read-only backfill exhausted bounded retry budget") from exc
                sleep(
                    max(
                        float(exc.retry_after_seconds or 0),
                        min(30.0, float(2 ** (consecutive_retryable_errors - 1))),
                    )
                )
        if not made_progress and consecutive_retryable_errors == 0:
            break

    after = repository.operational_status()
    if _sum_counts(after["ai_jobs"]) != before_ai or _sum_counts(after["publication_jobs"]) != before_publication:
        raise RuntimeError("historical backfill created AI or publication jobs")
    return {
        "status": "complete" if all(streams_complete.values()) else "checkpointed",
        "operation": operation,
        "calls": calls,
        "rows_observed": rows,
        "upserted": upserted,
        "streams_complete": streams_complete,
        "runtime": _safe_status(repository),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Force-off, GET-only WB feedback sync/backfill.")
    parser.add_argument(
        "--operation",
        choices=("status", "canary", "manual-canary", "manual-media-canary", "steady", "backfill"),
        required=True,
    )
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--min-request-interval-seconds", type=float, default=1.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.operation != "status":
            if args.env_file is None:
                raise ValueError("external read operations require --env-file")
            _load_safe_env_file(args.env_file.resolve())
            # Reassert after the dotenv load so its contents cannot choose the
            # canary safety posture.
            os.environ[FORCE_OFF_ENV] = (
                "false" if args.operation in {"manual-canary", "manual-media-canary"} else "true"
            )
        repository = AutoanswersRepository(runtime_dir=args.runtime_dir.resolve())
        source = HttpBackedWbAutoanswersReadAdapter() if args.operation != "status" else None
        result = run_operation(
            operation=args.operation,
            repository=repository,
            source=source,
            now_factory=lambda: datetime.now(timezone.utc),
            page_size=args.page_size,
            max_pages=args.max_pages,
            min_request_interval_seconds=max(0.333, float(args.min_request_interval_seconds)),
        )
    except Exception as exc:
        print(
            json.dumps(
                {"status": "error", "operation": args.operation, "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
