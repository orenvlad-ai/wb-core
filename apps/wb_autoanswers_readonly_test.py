#!/usr/bin/env python3
"""Free tests for the force-off, GET-only production runner."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from apps.wb_autoanswers_readonly import run_operation
from apps.wb_autoanswers_runtime_test import feedback
from packages.adapters.wb_autoanswers import FeedbackPage
from packages.application.wb_autoanswers_runtime import AutoanswersRepository


class FakeReadSource:
    def __init__(self) -> None:
        self.page_calls = 0
        self.detail_calls = 0

    def fetch_feedbacks_page(self, **kwargs: object) -> FeedbackPage:
        self.page_calls += 1
        row = feedback(f"read-{self.page_calls}")
        row["createdDate"] = "2026-01-01T10:00:00Z"
        return FeedbackPage(rows=[row], take=int(kwargs["take"]), skip=int(kwargs["skip"]), has_more=False)

    def fetch_detail(self, feedback_id: str) -> dict:
        self.detail_calls += 1
        row = feedback(feedback_id)
        row["createdDate"] = "2026-01-01T10:00:00Z"
        row["wasViewed"] = True
        return row

    def fetch_archive_page(self, *, take: int, skip: int) -> FeedbackPage:
        return FeedbackPage(rows=[], take=take, skip=skip, has_more=False)

    def count_unanswered(self) -> int:
        return 1


class ReadonlyRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.env = {"WB_AUTOANSWERS_FORCE_OFF": "true"}
        self.now = lambda: datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.repo = AutoanswersRepository(runtime_dir=Path(self.temp.name), now_factory=self.now, env=self.env)
        self.source = FakeReadSource()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_case(self, operation: str, **overrides: object) -> dict:
        values = {
            "operation": operation,
            "repository": self.repo,
            "source": self.source,
            "now_factory": self.now,
            "page_size": 50,
            "max_pages": 10,
            "min_request_interval_seconds": 0,
            "sleep": lambda _seconds: None,
        }
        values.update(overrides)
        with patch.dict("os.environ", {"WB_AUTOANSWERS_EXTERNAL_IO_ENABLED": "true"}, clear=False):
            return run_operation(**values)

    def test_canary_performs_one_page_and_one_detail_without_jobs(self) -> None:
        result = self.run_case("canary")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(self.source.page_calls, 1)
        self.assertEqual(self.source.detail_calls, 1)
        self.assertEqual(result["runtime"]["ai_jobs"], {})
        self.assertEqual(result["runtime"]["publication_jobs"], {})
        self.assertFalse(result["runtime"]["settings"]["effective_enabled"])
        self.assertFalse(result["detail"]["auto_enqueue"])

    def test_backfill_completes_both_streams_without_jobs(self) -> None:
        result = self.run_case("backfill")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["calls"], 3)
        self.assertEqual(
            result["streams_complete"],
            {"unanswered": True, "answered": True, "archive": True},
        )
        self.assertEqual(result["runtime"]["feedbacks"]["min_created_date"], "2026-01-01")
        self.assertEqual(result["runtime"]["ai_jobs"], {})
        self.assertEqual(result["runtime"]["publication_jobs"], {})

    def test_steady_sync_processes_local_command_without_ai_or_publication_jobs(self) -> None:
        self.repo.enqueue_sync_command(request_key="ui-request", actor_id="viewer")
        result = self.run_case("steady")
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["command_processed"])
        self.assertEqual(len(result["sync"]), 2)
        self.assertEqual(result["runtime"]["ai_jobs"], {})
        self.assertEqual(result["runtime"]["publication_jobs"], {})

    def test_persisted_on_blocks_external_read_operation_even_with_force_off(self) -> None:
        self.env.pop("WB_AUTOANSWERS_FORCE_OFF")
        self.repo.update_settings(master_enabled=True, actor_id="admin")
        self.env["WB_AUTOANSWERS_FORCE_OFF"] = "true"
        with self.assertRaisesRegex(RuntimeError, "master-switch OFF"):
            self.run_case("canary")
        self.assertEqual(self.source.page_calls, 0)

    def test_status_has_no_external_dependency_and_reports_get_only_capabilities(self) -> None:
        result = run_operation(
            operation="status",
            repository=self.repo,
            source=None,
            now_factory=self.now,
            page_size=50,
            max_pages=1,
            min_request_interval_seconds=0,
        )
        self.assertTrue(result["runtime"]["capabilities"]["wb_get"])
        self.assertFalse(result["runtime"]["capabilities"]["wb_post_patch"])
        self.assertFalse(result["runtime"]["capabilities"]["openai"])

    def test_entrypoint_has_no_writer_or_openai_bridge_import(self) -> None:
        source = (Path(__file__).resolve().parent / "wb_autoanswers_readonly.py").read_text(encoding="utf-8")
        self.assertNotIn("HttpBackedWbAnswerWriter", source)
        self.assertNotIn("NodeAutoanswersBridge", source)
        self.assertNotIn("AutoanswersPublicationWorker", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
