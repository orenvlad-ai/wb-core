#!/usr/bin/env python3
"""Free media and end-to-end draft worker checks with fake transports."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from packages.application.wb_autoanswers_media import AutoanswersMediaProcessor
from packages.application.wb_autoanswers_node_bridge import NodeAutoanswersBridge
from packages.application.wb_autoanswers_runtime import AutoanswersRepository, AutoanswersRuntimeError
from packages.application.wb_autoanswers_worker import AutoanswersProcessingWorker
from apps.wb_autoanswers_runtime_test import MutableClock, feedback


class FakeFetcher:
    def fetch(self, url: str, destination: Path, *, max_bytes: int) -> dict:
        body = ("video" if destination.suffix == ".mp4" else "photo").encode()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        return {
            "local_path": str(destination),
            "sha256": hashlib.sha256(body).hexdigest(),
            "mime_type": "video/mp4" if destination.suffix == ".mp4" else "image/jpeg",
            "byte_size": len(body),
        }


class FakeFrames:
    def extract(self, video_path: Path, output_dir: Path, *, max_frames: int) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for index in range(min(2, max_frames)):
            frame = output_dir / f"frame-{index:02d}.jpg"
            frame.write_bytes(f"frame-{index}".encode())
            frames.append(frame)
        return frames


class NoopMedia:
    def process(self, **kwargs: object) -> dict:
        return {"media_uncertain": False, "photos_downloaded": 0, "video_frames": 0}


class MediaWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.clock = MutableClock()
        self.env: dict[str, str] = {}
        self.repo = AutoanswersRepository(runtime_dir=Path(self.temp.name), now_factory=self.clock, env=self.env)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_photo_and_bounded_video_frames_are_persisted(self) -> None:
        row = feedback("media")
        row["video"] = {"link": "https://video.wbbasket.ru/video.mp4?sig=1"}
        outcome = self.repo.upsert_feedback(row, source_stream="backfill", run_kind="backfill")
        processor = AutoanswersMediaProcessor(
            repository=self.repo,
            runtime_dir=Path(self.temp.name),
            fetcher=FakeFetcher(),
            frame_extractor=FakeFrames(),
        )
        result = processor.process(feedback_id="media", content_version=outcome["content_version"])
        self.assertEqual(result["photos_downloaded"], 1)
        self.assertEqual(result["video_frames"], 2)
        rows = self.repo.media_rows("media", 1)
        self.assertEqual(sum(item["kind"] == "video_frame" for item in rows), 2)
        self.assertTrue(all(item["fetch_status"] in {"downloaded", "frames_extracted"} for item in rows))

    def worker(self) -> AutoanswersProcessingWorker:
        bridge = NodeAutoanswersBridge(env={**os.environ, "WB_AUTOANSWERS_TEST_MODE": "1"})
        return AutoanswersProcessingWorker(
            repository=self.repo,
            bridge=bridge,
            media_processor=NoopMedia(),
            worker_id="fixture-worker",
        )

    def enqueue(self, feedback_id: str, *, rating: int = 3, text: str = "Пузыри после установки") -> None:
        row = feedback(feedback_id, text=text)
        row["productValuation"] = rating
        if not text:
            row["pros"] = ""
            row["cons"] = ""
        outcome = self.repo.upsert_feedback(row, source_stream="unanswered", run_kind="steady")
        self.repo.enqueue_processing(
            feedback_id,
            content_version=outcome["content_version"],
            trigger_source="steady_sync",
            actor_id="sync",
        )

    def test_draft_only_worker_runs_frozen_pipeline_without_publication(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="draft_only", actor_id="admin")
        self.enqueue("draft")
        result = self.worker().run_once(execution_mode="fixture", fixture_scenario="public_only")
        self.assertEqual(result["state"], "generated")
        detail = self.repo.get_feedback("draft")
        self.assertEqual(detail["publications"], [])
        self.assertEqual(detail["route"], "public_only")

    def test_empty_five_star_stops_before_role_calls(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="draft_only", actor_id="admin")
        self.enqueue("empty", rating=5, text="")
        result = self.worker().run_once(execution_mode="fixture", fixture_scenario="public_only")
        self.assertEqual(result["state"], "skipped")
        self.assertEqual(result["model_calls"], 0)

    def test_off_and_force_off_block_worker_before_node(self) -> None:
        with self.assertRaisesRegex(AutoanswersRuntimeError, "OFF"):
            self.worker().run_once(execution_mode="fixture", fixture_scenario="public_only")
        self.repo.update_settings(master_enabled=True, actor_id="admin")
        self.enqueue("forced")
        self.env["WB_AUTOANSWERS_FORCE_OFF"] = "true"
        with self.assertRaisesRegex(AutoanswersRuntimeError, "OFF"):
            self.worker().run_once(execution_mode="fixture", fixture_scenario="public_only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
