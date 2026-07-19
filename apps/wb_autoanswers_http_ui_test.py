#!/usr/bin/env python3
"""Local contract/UI checks for SellerOS autoanswers integration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import unittest

from packages.adapters.registry_upload_http_entrypoint import _render_sheet_vitrina_web_vitrina_ui
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from apps.wb_autoanswers_runtime_test import feedback


class LegacyFeedbacksBlock:
    def build(self, **kwargs: object) -> dict:
        return {
            "contract_name": "sheet_vitrina_v1_feedbacks",
            "contract_version": "v1",
            "meta": {"legacy_compatible": True},
            "summary": {},
            "rows": [],
        }

    def build_export(self, payload: dict) -> tuple[bytes, str]:
        return b"", "feedbacks.xlsx"


class HttpUiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.app = RegistryUploadHttpEntrypoint(
            Path(self.temp.name),
            feedbacks_block=LegacyFeedbacksBlock(),
            now_factory=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_existing_feedbacks_get_contract_remains_compatible(self) -> None:
        payload = self.app.handle_sheet_feedbacks_request(
            date_from="2026-07-01", date_to="2026-07-20", stars=[1, 5], is_answered="all"
        )
        self.assertEqual(payload["contract_name"], "sheet_vitrina_v1_feedbacks")
        self.assertTrue(payload["meta"]["legacy_compatible"])

    def test_local_list_detail_settings_and_sync_command_contracts(self) -> None:
        self.app.autoanswers_repository.upsert_feedback(
            feedback("local"), source_stream="backfill", run_kind="backfill"
        )
        listing = self.app.handle_sheet_feedbacks_local_request()
        detail = self.app.handle_sheet_feedbacks_detail_request("local")
        settings = self.app.handle_sheet_feedbacks_autoanswers_settings_request()
        first = self.app.handle_sheet_feedbacks_autoanswers_sync_request(
            {"request_key": "same-request"}, actor_id="tester"
        )
        second = self.app.handle_sheet_feedbacks_autoanswers_sync_request(
            {"request_key": "same-request"}, actor_id="tester"
        )
        self.assertEqual(listing["page_size"], 50)
        self.assertEqual(detail["feedback"]["id"], "local")
        self.assertFalse(settings["settings"]["effective_enabled"])
        self.assertEqual(first["command"]["command_id"], second["command"]["command_id"])

    def test_rendered_ui_contains_local_routes_protected_controls_and_valid_javascript(self) -> None:
        html = _render_sheet_vitrina_web_vitrina_ui(
            read_path="/read",
            operator_path="/operator",
            refresh_path="/refresh",
            job_path="/job",
            role="admin",
            allowed_sections=["feedbacks", "feedbacks.ai_review", "feedbacks.autoanswers_admin"],
            active_tab="feedbacks",
        )
        self.assertIn("data-feedbacks-subtab=\"server-reviews\"", html)
        self.assertIn("data-autoanswers-master-status", html)
        self.assertIn("feedbacks_autoanswers_settings_path", html)
        self.assertIn("AUTO_ALL", html)
        scripts = re.findall(r"<script(?: [^>]*)?>(.*?)</script>", html, flags=re.DOTALL)
        self.assertTrue(scripts)
        with TemporaryDirectory() as directory:
            script_path = Path(directory) / "rendered-ui.js"
            script_path.write_text("\n".join(scripts), encoding="utf-8")
            checked = subprocess.run(
                ["node", "--check", str(script_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
