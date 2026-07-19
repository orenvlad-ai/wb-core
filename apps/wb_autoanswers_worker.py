#!/usr/bin/env python3
"""Explicitly gated one-tick worker entrypoint for hosted SellerOS runtime."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_autoanswers import HttpBackedWbAnswerWriter, HttpBackedWbAutoanswersReadAdapter
from packages.application.wb_autoanswers_coordinator import AutoanswersCoordinator
from packages.application.wb_autoanswers_media import AutoanswersMediaProcessor
from packages.application.wb_autoanswers_node_bridge import NodeAutoanswersBridge
from packages.application.wb_autoanswers_publication import AutoanswersPublicationWorker
from packages.application.wb_autoanswers_runtime import AutoanswersRepository
from packages.application.wb_autoanswers_sync import WbFeedbackSyncService
from packages.application.wb_autoanswers_worker import AutoanswersProcessingWorker


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def build_coordinator(runtime_dir: Path) -> AutoanswersCoordinator:
    now = lambda: datetime.now(timezone.utc)
    repository = AutoanswersRepository(runtime_dir=runtime_dir, now_factory=now)
    reader = HttpBackedWbAutoanswersReadAdapter()
    writer = HttpBackedWbAnswerWriter()
    worker_id = f"wb-autoanswers-{socket.gethostname()}-{os.getpid()}"
    sync_service = WbFeedbackSyncService(repository=repository, source=reader, now_factory=now)
    processing_worker = AutoanswersProcessingWorker(
        repository=repository,
        bridge=NodeAutoanswersBridge(),
        media_processor=AutoanswersMediaProcessor(repository=repository, runtime_dir=runtime_dir),
        worker_id=worker_id,
    )
    publication_worker = AutoanswersPublicationWorker(
        repository=repository, transport=writer, worker_id=worker_id
    )
    return AutoanswersCoordinator(
        repository=repository,
        sync_service=sync_service,
        processing_worker=processing_worker,
        publication_worker=publication_worker,
        worker_id=worker_id,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--runtime-dir", type=Path)
    args = parser.parse_args()
    runtime_dir = args.runtime_dir or Path(
        os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", ".runtime/registry_upload")
    ).expanduser()
    if not args.run_once:
        print(json.dumps({"status": "ready", "external_io": False, "hint": "use --run-once behind the external gate"}))
        return 0
    if not _truthy(os.environ.get("WB_AUTOANSWERS_EXTERNAL_IO_ENABLED")):
        print(json.dumps({"status": "blocked", "code": "external_io_gate_off"}))
        return 2
    report = build_coordinator(runtime_dir).run_once()
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
