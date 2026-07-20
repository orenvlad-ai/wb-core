#!/usr/bin/env python3
"""Local contract/UI checks for SellerOS autoanswers integration."""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import Message
import io
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from packages.adapters.registry_upload_http_entrypoint import (
    _ensure_autoanswers_csrf,
    _ensure_feedback_capability,
    _render_sheet_vitrina_web_vitrina_ui,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.wb_autoanswers_node_bridge import NodeBoundaryError
from packages.application.wb_autoanswers_runtime import AutoanswersRuntimeError
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


class FakeHandler:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value
        self.server = type("Server", (), {"server_address": ("127.0.0.1", 8765)})()
        self.wfile = io.BytesIO()
        self.status: int | None = None

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, _key: str, _value: str) -> None:
        return None

    def end_headers(self) -> None:
        return None


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

    def test_five_state_selector_maps_atomically_to_master_and_mode(self) -> None:
        manual = self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
            {"selector_state": "manual"}, actor_id="admin"
        )
        self.assertEqual(manual["selector_state"], "manual")
        self.assertTrue(manual["settings"]["master_enabled"])
        off = self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
            {"selector_state": "off"}, actor_id="admin"
        )
        self.assertEqual(off["selector_state"], "off")
        self.assertFalse(off["settings"]["master_enabled"])
        preview = self.app.handle_sheet_feedbacks_autoanswers_transition_preview_request(
            {"selector_state": "draft_only"}, actor_id="admin"
        )
        draft = self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
            {"selector_state": "draft_only", "preview_id": preview["preview_id"]}, actor_id="admin"
        )
        self.assertEqual(draft["selector_state"], "draft_only")
        self.assertEqual(draft["settings"]["mode"], "draft_only")

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
        self.assertIn("Автоответы выключены", html)
        self.assertIn("WB_AUTOANSWERS_FORCE_OFF=true", html)
        self.assertIn("feedbacks_autoanswers_settings_path", html)
        for label in ("Выключено", "Ручной", "Черновики", "Безопасный", "Полный"):
            self.assertIn(label, html)
        self.assertIn("Сгенерировать ответ", html)
        self.assertIn("Перегенерировать с учётом медиа", html)
        self.assertIn("Опубликовать", html)
        self.assertIn("Техническая информация", html)
        self.assertIn("autoGrowReplyEditors", html)
        self.assertIn('addEventListener("input", handleFeedbacksInputChange)', html)
        self.assertIn("autoanswers-answer-box", html)
        self.assertIn("data-autoanswers-copy", html)
        self.assertIn("Скопировано", html)
        self.assertIn("overflow: auto", html)
        self.assertIn("https://platform.openai.com/settings/organization/billing/overview", html)
        self.assertIn('rel="noopener noreferrer"', html)
        self.assertIn("row.pros ?", html)
        self.assertIn("row.cons ?", html)
        self.assertIn("X-WB-Autoanswers-CSRF", html)
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

    def test_public_detail_hides_signed_urls_and_exposes_private_media_state(self) -> None:
        row = feedback("media-public", photo_query="secret-signature=do-not-expose")
        self.app.autoanswers_repository.upsert_feedback(
            row, source_stream="backfill", run_kind="backfill"
        )
        detail = self.app.handle_sheet_feedbacks_detail_request("media-public")["feedback"]
        serialized = __import__("json").dumps(detail, ensure_ascii=False)
        self.assertNotIn("secret-signature", serialized)
        self.assertNotIn("source_full_url", serialized)
        self.assertIn("primary_available", detail["media"][0])

        with self.app.autoanswers_repository.transaction() as conn:
            self.app.autoanswers_repository._audit(
                conn,
                aggregate_type="feedback",
                aggregate_id="media-public",
                event_type="redaction_probe",
                actor_type="test",
                actor_id="test",
                details={
                    "source_url": "https://cdn.geobasket.ru/photo.webp?secret-signature=hidden",
                    "message": "fetched https://cdn.geobasket.ru/photo.webp?secret-signature=hidden",
                    "local_path": "/private/runtime/photo.webp",
                },
                at=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
            )
        redacted = __import__("json").dumps(
            self.app.handle_sheet_feedbacks_detail_request("media-public")["feedback"],
            ensure_ascii=False,
        )
        self.assertNotIn("secret-signature", redacted)
        self.assertNotIn("/private/runtime", redacted)

    def test_automated_mode_requires_bound_transition_preview(self) -> None:
        self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
            {"selector_state": "manual"}, actor_id="admin"
        )
        with self.assertRaisesRegex(AutoanswersRuntimeError, "preview"):
            self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
                {"selector_state": "auto_safe"}, actor_id="admin"
            )

    def test_autoanswers_mutations_require_csrf_marker_and_same_origin(self) -> None:
        valid = FakeHandler(
            {
                "Host": "selleros.test",
                "X-Forwarded-Proto": "https",
                "Content-Type": "application/json",
                "X-WB-Autoanswers-CSRF": "1",
                "Origin": "https://selleros.test",
                "Sec-Fetch-Site": "same-origin",
            }
        )
        self.assertTrue(_ensure_autoanswers_csrf(valid, "/manual/generate"))
        for headers in (
            {"Host": "selleros.test", "Content-Type": "application/json"},
            {
                "Host": "selleros.test",
                "Content-Type": "application/json",
                "X-WB-Autoanswers-CSRF": "1",
                "Origin": "https://evil.test",
                "Sec-Fetch-Site": "cross-site",
            },
        ):
            handler = FakeHandler(headers)
            self.assertFalse(_ensure_autoanswers_csrf(handler, "/manual/generate"))
            self.assertEqual(handler.status, 403)

    def test_manual_actions_require_ai_review_and_mode_changes_require_admin(self) -> None:
        base_config = {"enabled": True}
        ai_reviewer = {
            "allowed_sections": ["feedbacks", "feedbacks.ai_review"],
            "role": "operator",
        }
        viewer = {"allowed_sections": ["feedbacks"], "role": "operator"}
        handler = FakeHandler({"Host": "selleros.test"})
        with patch(
            "packages.adapters.registry_upload_http_entrypoint._web_auth_config",
            return_value=base_config,
        ), patch(
            "packages.adapters.registry_upload_http_entrypoint._authenticated_web_user",
            return_value=ai_reviewer,
        ):
            self.assertTrue(_ensure_feedback_capability(handler, "/manual/generate", "feedbacks.ai_review"))
            self.assertFalse(
                _ensure_feedback_capability(handler, "/settings", "feedbacks.autoanswers_admin")
            )
        viewer_handler = FakeHandler({"Host": "selleros.test"})
        with patch(
            "packages.adapters.registry_upload_http_entrypoint._web_auth_config",
            return_value=base_config,
        ), patch(
            "packages.adapters.registry_upload_http_entrypoint._authenticated_web_user",
            return_value=viewer,
        ):
            self.assertFalse(
                _ensure_feedback_capability(viewer_handler, "/manual/generate", "feedbacks.ai_review")
            )

    def test_manual_edit_fails_closed_when_frozen_guard_is_unavailable(self) -> None:
        repository = Mock()
        repository.manual_guard_context.return_value = {
            "feedback_id": "feedback-1",
            "content_version": 1,
            "route": "public_only",
            "case_code": None,
            "primary_issue": None,
        }
        bridge = Mock()
        bridge.guard_final.side_effect = NodeBoundaryError(
            "boundary unavailable", code="node_unavailable"
        )
        self.app.autoanswers_repository = repository
        self.app.autoanswers_node_bridge = bridge
        with self.assertRaisesRegex(AutoanswersRuntimeError, "could not validate") as raised:
            self.app.handle_sheet_feedbacks_autoanswers_manual_edit_request(
                {"processing_key": "processing-1", "reply": "Безопасный ответ."},
                actor_id="reviewer",
            )
        self.assertEqual(raised.exception.code, "manual_final_guard_node_unavailable")
        repository.save_manual_reply_review.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
