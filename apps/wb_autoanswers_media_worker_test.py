#!/usr/bin/env python3
"""Free media and end-to-end draft worker checks with fake transports."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from packages.application.wb_autoanswers_media import (
    AutoanswersMediaProcessor,
    FfmpegVideoFrameExtractor,
    HttpMediaFetcher,
    MediaProcessingError,
    _allowed_url,
)
from packages.application.wb_autoanswers_node_bridge import (
    NodeAutoanswersBridge,
    NodeBoundaryError,
    build_frozen_raw_input,
)
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
            frame.write_bytes(b"\xff\xd8\xff" + f"frame-{index}".encode())
            frames.append(frame)
        return frames


class HlsFetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str, destination: Path, *, max_bytes: int) -> dict:
        self.calls.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if "preview" in url:
            body = b"RIFF" + (8).to_bytes(4, "little") + b"WEBPVP8 "
            mime = "image/webp"
        elif "master.m3u8" in url:
            body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nvariant.m3u8\n"
            mime = "application/vnd.apple.mpegurl"
        elif "variant.m3u8" in url:
            body = b"#EXTM3U\n#EXTINF:1,\nsegment-1.ts\n#EXTINF:1,\nsegment-2.ts\n#EXTINF:1,\nsegment-3.ts\n#EXTINF:1,\nsegment-4.ts\n#EXTINF:1,\nsegment-5.ts\n"
            mime = "application/vnd.apple.mpegurl"
        elif "segment-" in url:
            body = b"\x47" + url.rsplit("-", 1)[-1].encode()
            mime = "video/mp2t"
        else:
            body = b"RIFF" + (8).to_bytes(4, "little") + b"WEBPVP8 "
            mime = "image/webp"
        if len(body) > max_bytes:
            raise MediaProcessingError("too large", code="media_too_large")
        destination.write_bytes(body)
        return {
            "local_path": str(destination),
            "sha256": hashlib.sha256(body).hexdigest(),
            "mime_type": mime,
            "byte_size": len(body),
        }


class ExpiringFetcher(FakeFetcher):
    def __init__(self) -> None:
        self.expired = True

    def fetch(self, url: str, destination: Path, *, max_bytes: int) -> dict:
        if self.expired and "old-signature" in url:
            self.expired = False
            raise MediaProcessingError("expired", code="media_url_expired", retryable=True)
        return super().fetch(url, destination, max_bytes=max_bytes)


class FakeHeaders(dict):
    def get(self, key: str, default: object = None) -> object:
        return super().get(key, default)


class FakeResponse:
    def __init__(self, body: bytes, *, mime: str, url: str, length: int | None = None) -> None:
        self.body = body
        self.offset = 0
        self.url = url
        self.headers = FakeHeaders({"Content-Type": mime})
        if length is not None:
            self.headers["Content-Length"] = str(length)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def read(self, size: int) -> bytes:
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def open(self, request: object, timeout: int) -> FakeResponse:
        return self.response


class NoopMedia:
    def process(self, **kwargs: object) -> dict:
        return {"media_uncertain": False, "photos_downloaded": 0, "video_frames": 0}


class UncertainMedia:
    def process(self, **kwargs: object) -> dict:
        return {
            "media_uncertain": True,
            "photos_downloaded": 0,
            "video_frames": 0,
            "uncertainty": ["photo:media_timeout"],
        }


class ForbiddenBridge:
    def run(self, **kwargs: object) -> dict:
        raise AssertionError("frozen AI must not run while media is uncertain")


class OpaqueExitBridge:
    def run(self, **kwargs: object) -> dict:
        raise NodeBoundaryError(
            "fixture opaque child exit",
            code="node_process_exit_1",
            retryable=True,
            diagnostics={
                "returncode": 1,
                "stderr_bytes": 12,
                "stderr_sha256": "a" * 64,
                "raw_output_persisted": False,
            },
        )


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
        self.assertEqual(result["video_previews"], 1)
        self.assertEqual(result["video_frames"], 2)
        rows = self.repo.media_rows("media", 1)
        self.assertEqual(sum(item["kind"] == "video_frame" for item in rows), 2)
        self.assertTrue(all(item["fetch_status"] in {"downloaded", "frames_extracted"} for item in rows))

    def test_realistic_wb_hls_preview_and_four_deterministic_frames(self) -> None:
        row = feedback("hls")
        row["photoLinks"] = [
            {"fullSize": "https://mow-feedback-uuid-02-cdn-02.geobasket.ru/photo.webp?signature=photo"}
        ]
        row["video"] = {
            "link": "https://videofeedback10.wbbasket.ru/master.m3u8?signature=video",
            "previewImage": "https://videofeedback10.wbbasket.ru/preview.webp",
        }
        self.repo.upsert_feedback(row, source_stream="backfill", run_kind="backfill")
        fetcher = HlsFetcher()
        processor = AutoanswersMediaProcessor(
            repository=self.repo,
            runtime_dir=Path(self.temp.name),
            fetcher=fetcher,
            frame_extractor=FakeFrames(),
        )
        result = processor.process(feedback_id="hls", content_version=1)
        self.assertEqual(result["photos_downloaded"], 1)
        self.assertEqual(result["video_previews"], 1)
        self.assertEqual(result["video_frames"], 4)
        detail = self.repo.get_feedback("hls")
        video = next(item for item in detail["media"] if item["kind"] == "video")
        self.assertTrue(video["preview_local_path"])
        self.assertEqual(len([item for item in detail["media"] if item["kind"] == "video_frame"]), 4)
        raw = build_frozen_raw_input(detail, processing_key="hls|1|1.4.2")
        self.assertEqual(len(raw["media"]["video"]["frame_refs"]), 5)
        self.assertTrue(raw["media"]["photos"][0]["local_ref"].startswith("data:image/webp;base64,"))
        self.assertNotIn("?", str(raw["media"]["photos"][0]["full_size_url"] or ""))
        self.assertNotIn("?", str(raw["media"]["video"]["source_url"] or ""))

        root = Path(__file__).resolve().parents[1]
        script = """
import {readFileSync} from 'node:fs';
import {normalizeTelegramInput} from './packages/node/wb_autoanswers_v1_4_2/make_mvp/scripts/normalizer.mjs';
import {buildClassifierRequest, readJson} from './packages/node/wb_autoanswers_v1_4_2/make_mvp/frozen_bundle/tools/build_context.mjs';
import {buildResponsesPayload, containsCacheBreakpoint} from './packages/node/wb_autoanswers_v1_4_2/make_mvp/scripts/payload_builder.mjs';
const raw = JSON.parse(readFileSync(0, 'utf8'));
const product = await readJson('contracts/product_context.json');
const review = normalizeTelegramInput(raw, product);
const request = await buildClassifierRequest(review);
const payload = await buildResponsesPayload('classifier', request, raw.review_id);
const content = payload.input[0].content;
const firstImage = content.findIndex((item) => item.type === 'input_image');
const breakpoint = content.findIndex((item) => item.prompt_cache_breakpoint);
process.stdout.write(JSON.stringify({images: content.filter((item) => item.type === 'input_image').length, after: firstImage > breakpoint, cached: containsCacheBreakpoint(payload)}));
"""
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=root,
            input=__import__("json").dumps(raw),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        evidence = __import__("json").loads(completed.stdout)
        self.assertEqual(evidence["images"], 6)
        self.assertTrue(evidence["after"])
        self.assertTrue(evidence["cached"])

    def test_single_hls_segment_selects_first_decodable_frame(self) -> None:
        extractor = FfmpegVideoFrameExtractor()
        video = Path(self.temp.name) / "segment.ts"
        video.write_bytes(b"fixture")
        output = Path(self.temp.name) / "frames"
        with patch(
            "packages.application.wb_autoanswers_media.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stderr=b""),
        ) as run:
            frames = extractor.extract(video, output, max_frames=1)
        command = run.call_args.args[0]
        self.assertIn("select='eq(n,0)',scale='min(1280,iw)':-2", command)
        self.assertNotIn("fps=1/15,scale='min(1280,iw)':-2", command)
        self.assertEqual(frames, [])

    def test_multi_frame_video_keeps_bounded_cadence(self) -> None:
        extractor = FfmpegVideoFrameExtractor()
        video = Path(self.temp.name) / "video.mp4"
        video.write_bytes(b"fixture")
        output = Path(self.temp.name) / "frames"
        with patch(
            "packages.application.wb_autoanswers_media.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stderr=b""),
        ) as run:
            extractor.extract(video, output, max_frames=4)
        command = run.call_args.args[0]
        self.assertIn("fps=1/15,scale='min(1280,iw)':-2", command)
        self.assertEqual(command[command.index("-frames:v") + 1], "4")

    def test_expired_signed_url_is_refreshed_by_detail_read(self) -> None:
        row = feedback("refresh")
        row["photoLinks"] = [{"fullSize": "https://cdn.example/photo.jpg?old-signature"}]
        self.repo.upsert_feedback(row, source_stream="steady", run_kind="steady")

        def refresh(feedback_id: str) -> bool:
            updated = feedback(feedback_id)
            updated["photoLinks"] = [{"fullSize": "https://cdn.example/photo.jpg?new-signature"}]
            self.repo.upsert_feedback(updated, source_stream="detail", run_kind="detail_readback")
            return True

        processor = AutoanswersMediaProcessor(
            repository=self.repo,
            runtime_dir=Path(self.temp.name),
            fetcher=ExpiringFetcher(),
            frame_extractor=FakeFrames(),
            refresh_urls=refresh,
        )
        result = processor.process(feedback_id="refresh", content_version=1)
        self.assertEqual(result["photos_downloaded"], 1)
        self.assertFalse(result["media_uncertain"])
        self.assertIn("new-signature", self.repo.media_rows("refresh", 1)[0]["source_full_url"])

    def test_allowlist_dns_and_ttl_fail_closed(self) -> None:
        self.assertTrue(_allowed_url("https://mow-feedback-uuid-01-cdn-01.geobasket.ru/x.webp", ("geobasket.ru",)))
        self.assertFalse(_allowed_url("https://geobasket.ru.evil.test/x.webp", ("geobasket.ru",)))
        public = lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))]
        private = lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))]
        HttpMediaFetcher(allowed_host_suffixes=("geobasket.ru",), resolver=public)._validate_url(
            "https://cdn.geobasket.ru/x.webp"
        )
        with self.assertRaisesRegex(MediaProcessingError, "public address"):
            HttpMediaFetcher(allowed_host_suffixes=("geobasket.ru",), resolver=private)._validate_url(
                "https://cdn.geobasket.ru/x.webp"
            )
        processor = AutoanswersMediaProcessor(
            repository=self.repo,
            runtime_dir=Path(self.temp.name),
            fetcher=FakeFetcher(),
            frame_extractor=FakeFrames(),
            ttl_seconds=60,
        )
        expired = processor.root / "feedback" / "1"
        expired.mkdir(parents=True)
        expired.joinpath("asset").write_bytes(b"x")
        old = time.time() - 120
        os.utime(expired, (old, old))
        self.assertEqual(processor.cleanup_expired(), 1)
        self.assertFalse(expired.exists())

        self.repo.upsert_feedback(feedback("ttl-reset"), source_stream="steady", run_kind="steady")
        processor.process(feedback_id="ttl-reset", content_version=1)
        version_dir = next(path for path in processor.root.glob("*/1") if path.is_dir())
        os.utime(version_dir, (old, old))
        self.assertEqual(processor.cleanup_expired(), 1)
        media = self.repo.media_rows("ttl-reset", 1)
        self.assertEqual(media[0]["fetch_status"], "pending")
        self.assertIsNone(media[0]["local_path"])

    def test_http_fetcher_validates_photo_mime_size_timeout_and_redirect(self) -> None:
        resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))]
        fetcher = HttpMediaFetcher(allowed_host_suffixes=("geobasket.ru",), resolver=resolver, timeout=30)
        destination = Path(self.temp.name) / "photo.webp"
        webp = b"RIFF" + (8).to_bytes(4, "little") + b"WEBPVP8 "
        with patch(
            "packages.application.wb_autoanswers_media.urllib_request.build_opener",
            return_value=FakeOpener(FakeResponse(webp, mime="image/webp", url="https://cdn.geobasket.ru/p.webp")),
        ):
            metadata = fetcher.fetch("https://cdn.geobasket.ru/p.webp", destination, max_bytes=100)
        self.assertEqual(metadata["mime_type"], "image/webp")
        self.assertTrue(destination.is_file())

        with patch(
            "packages.application.wb_autoanswers_media.urllib_request.build_opener",
            return_value=FakeOpener(FakeResponse(b"<html>", mime="text/html", url="https://cdn.geobasket.ru/p.webp")),
        ), self.assertRaisesRegex(MediaProcessingError, "signature"):
            fetcher.fetch("https://cdn.geobasket.ru/p.webp", destination, max_bytes=100)

        with patch(
            "packages.application.wb_autoanswers_media.urllib_request.build_opener",
            return_value=FakeOpener(FakeResponse(webp, mime="image/webp", url="https://cdn.geobasket.ru/p.webp", length=101)),
        ), self.assertRaisesRegex(MediaProcessingError, "byte limit"):
            fetcher.fetch("https://cdn.geobasket.ru/p.webp", destination, max_bytes=100)

        with patch(
            "packages.application.wb_autoanswers_media.urllib_request.build_opener",
            return_value=FakeOpener(FakeResponse(webp, mime="image/webp", url="https://cdn.geobasket.ru/p.webp")),
        ), patch(
            "packages.application.wb_autoanswers_media.time.monotonic", side_effect=[0.0, 31.0]
        ), self.assertRaisesRegex(MediaProcessingError, "timed out"):
            fetcher.fetch("https://cdn.geobasket.ru/p.webp", destination, max_bytes=100)

        class RedirectingOpener:
            def __init__(self, handler: object) -> None:
                self.handler = handler

            def open(self, request: object, timeout: int) -> FakeResponse:
                self.handler.validator("https://evil.test/private")
                raise AssertionError("redirect validation must block before connection")

        with patch(
            "packages.application.wb_autoanswers_media.urllib_request.build_opener",
            side_effect=lambda handler: RedirectingOpener(handler),
        ), self.assertRaisesRegex(MediaProcessingError, "allowlist"):
            fetcher.fetch("https://cdn.geobasket.ru/p.webp", destination, max_bytes=100)

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
            row["photoLinks"] = []
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

    def test_empty_five_star_uses_zero_cost_template_without_role_calls(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="draft_only", actor_id="admin")
        self.enqueue("empty", rating=5, text="")
        result = self.worker().run_once(execution_mode="fixture", fixture_scenario="public_only")
        self.assertEqual(result["state"], "generated")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["processing_kind"], "rating_only_template")
        detail = self.repo.get_feedback("empty")
        self.assertEqual(detail["route"], "rating_only_template")
        self.assertEqual(detail["ai_jobs"][0]["actual_cost_usd"], "0")

    def test_media_uncertainty_blocks_paid_pipeline_and_requires_regeneration(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="draft_only", actor_id="admin")
        self.enqueue("uncertain")
        worker = AutoanswersProcessingWorker(
            repository=self.repo,
            bridge=ForbiddenBridge(),
            media_processor=UncertainMedia(),
            worker_id="fixture-worker",
        )
        result = worker.run_once(execution_mode="live")
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(result["model_calls"], 0)
        self.assertTrue(result["regeneration_required"])
        job = self.repo.get_feedback("uncertain")["ai_jobs"][0]
        self.assertIsNone(job["result_json"])
        self.assertTrue(job["media_uncertain"])
        self.assertTrue(job["regeneration_required"])

    def test_opaque_child_exit_is_contained_and_worker_returns_retry_state(self) -> None:
        self.repo.update_settings(master_enabled=True, mode="draft_only", actor_id="admin")
        self.enqueue("opaque-worker")
        worker = AutoanswersProcessingWorker(
            repository=self.repo,
            bridge=OpaqueExitBridge(),
            media_processor=NoopMedia(),
            worker_id="fixture-worker",
        )
        result = worker.run_once(execution_mode="live")
        self.assertEqual(result["state"], "retryable_error")
        self.assertTrue(result["bounded_retry"])
        self.assertEqual(result["error_code"], "node_process_exit_1")
        self.assertEqual(
            result["uncertainty_accounting"],
            "conservative_upper_bound",
        )
        self.assertEqual(self.repo.budget_status()["uncertainty_hold_count"], 1)

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
