#!/usr/bin/env python3
"""Local contract/UI checks for SellerOS autoanswers integration."""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import Message
import io
import json
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


class FakeAutoanswersLifecycle:
    def __init__(self, repository: object) -> None:
        self.repository = repository

    def status(self, *, suspended_by_master: bool) -> dict:
        settings = self.repository.settings()
        mode = settings.mode if settings.master_enabled else "off"
        active = not suspended_by_master and mode != "off"
        return {
            "process_key": "autoanswers",
            "business_mode": mode,
            "actual": False,
            "lifecycle_state": (
                "suspended_by_master"
                if suspended_by_master
                else "off"
                if mode == "off"
                else "starting"
            ),
            "drift_status": "matched",
            "suspended_by_master": suspended_by_master,
            "components": {
                "readonly_sync": {
                    "desired": not suspended_by_master,
                    "actual": not suspended_by_master,
                    "drift_status": "matched",
                },
                "worker": {
                    "desired": active,
                    "actual": active,
                    "drift_status": "matched",
                },
            },
            "budget_state": "confirmed",
            "transition_run_id": (
                self.repository.reconciliation_status() or {}
            ).get("transition_run_id"),
        }

    def reconcile(self, **kwargs: object) -> dict:
        return self.status(
            suspended_by_master=bool(kwargs.get("suspended_by_master"))
        )


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
        (Path(self.temp.name) / ".auto-updates-policy.json").write_text(
            json.dumps({"master_desired": True, "revision": 1}),
            encoding="utf-8",
        )
        self.app.autoanswers_lifecycle = FakeAutoanswersLifecycle(
            self.app.autoanswers_repository
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
        initial = self.app.handle_sheet_feedbacks_autoanswers_settings_request()
        manual = self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
            {
                "selector_state": "manual",
                "expected_policy_epoch": 0,
                "expected_settings_revision": initial["settings_revision"],
                "daily_cap_usd": "7.00",
            },
            actor_id="admin",
        )
        self.assertEqual(manual["selector_state"], "manual")
        self.assertTrue(manual["settings"]["master_enabled"])
        self.assertEqual(manual["settings"]["daily_cap_usd"], 7.0)
        off = self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
            {
                "selector_state": "off",
                "expected_policy_epoch": manual["settings"]["policy_epoch"],
            },
            actor_id="admin",
        )
        self.assertEqual(off["selector_state"], "off")
        self.assertFalse(off["settings"]["master_enabled"])
        preview = self.app.handle_sheet_feedbacks_autoanswers_transition_preview_request(
            {"selector_state": "draft_only", "run_max_usd": "0.50"}, actor_id="admin"
        )
        draft = self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
            {
                "selector_state": "draft_only",
                "preview_id": preview["preview_id"],
                "expected_policy_epoch": off["settings"]["policy_epoch"],
            },
            actor_id="admin",
        )
        self.assertEqual(draft["selector_state"], "draft_only")
        self.assertEqual(draft["settings"]["mode"], "draft_only")

    def test_limit_update_requires_fresh_settings_revision_and_returns_exact_readback(self) -> None:
        initial = self.app.handle_sheet_feedbacks_autoanswers_settings_request()
        saved = self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
            {
                "expected_policy_epoch": initial["settings"]["policy_epoch"],
                "expected_settings_revision": initial["settings_revision"],
                "hourly_cap_usd": "0.75",
                "daily_cap_usd": "6.00",
                "monthly_cap_usd": "60.00",
                "max_paid_reviews_per_hour": 30,
                "global_paid_review_concurrency": 2,
                "max_inflight_role_calls": 2,
                "max_materialized_processing_jobs": 10,
            },
            actor_id="admin",
        )
        self.assertEqual(
            saved["confirmed_limits"],
            {
                "daily_cap_usd": 6.0,
                "global_paid_review_concurrency": 2,
                "hourly_cap_usd": 0.75,
                "max_inflight_role_calls": 2,
                "max_materialized_processing_jobs": 10,
                "max_paid_reviews_per_hour": 30,
                "monthly_cap_usd": 60.0,
            },
        )
        self.assertNotEqual(saved["settings_revision"], initial["settings_revision"])
        self.assertEqual(
            saved["limits_contract"]["fields"]["hourly_cap_usd"]["maximum"],
            10.0,
        )
        with self.assertRaisesRegex(
            AutoanswersRuntimeError,
            "уже изменились",
        ) as stale:
            self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
                {
                    "expected_policy_epoch": saved["settings"]["policy_epoch"],
                    "expected_settings_revision": initial["settings_revision"],
                    "daily_cap_usd": "7.00",
                },
                actor_id="admin",
            )
        self.assertEqual(stale.exception.code, "settings_revision_stale")

        manual = self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
            {
                "selector_state": "manual",
                "expected_policy_epoch": saved["settings"]["policy_epoch"],
            },
            actor_id="admin",
        )
        with self.assertRaises(AutoanswersRuntimeError) as policy_stale:
            self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
                {
                    "expected_policy_epoch": saved["settings"]["policy_epoch"],
                    "expected_settings_revision": manual["settings_revision"],
                    "daily_cap_usd": "7.00",
                },
                actor_id="admin",
            )
        self.assertEqual(policy_stale.exception.code, "policy_epoch_stale")

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
        self.assertIn("data-autoanswers-backlog", html)
        self.assertIn("Legacy backlog отключён", html)
        self.assertIn("Автоответы выключены", html)
        self.assertIn("WB_AUTOANSWERS_FORCE_OFF=true", html)
        self.assertIn("feedbacks_autoanswers_settings_path", html)
        for label in ("Выключено", "Ручной", "Черновики", "Безопасный", "Полный"):
            self.assertIn(label, html)
        self.assertIn("Сгенерировать ответ", html)
        self.assertIn("Перегенерировать с учётом медиа", html)
        self.assertIn("Опубликовать", html)
        self.assertIn("Техническая информация", html)
        self.assertIn("Hard gates: не запускались", html)
        self.assertIn("autoGrowReplyEditors", html)
        self.assertIn('addEventListener("input", handleFeedbacksInputChange)', html)
        self.assertIn("autoanswers-answer-box", html)
        self.assertIn("Настроить лимиты", html)
        self.assertIn("data-autoanswers-limits-modal", html)
        self.assertIn("Лимит текущего запуска — только чтение", html)
        self.assertIn("Увеличить лимит", html)
        self.assertIn("data-autoanswers-copy", html)
        self.assertIn("Скопировано", html)
        self.assertIn("overflow: auto", html)
        self.assertIn("Обработка отзывов", html)
        self.assertIn("data-autoanswers-queue-metrics", html)
        self.assertIn("data-autoanswers-budget-available", html)
        self.assertIn("data-autoanswers-budget-unverified", html)
        self.assertIn("Ответа системы нет", html)
        self.assertIn("run_max_paid_reviews", html)
        self.assertIn("data-autoanswers-progress-bars", html)
        self.assertIn('data-autoanswers-progress-card="all"', html)
        self.assertIn('data-autoanswers-progress-card="content-bearing"', html)
        self.assertIn("data-autoanswers-content-progress-bars", html)
        self.assertIn("Отзывы с содержанием", html)
        self.assertIn(
            "Живая очередь соблюдает буквальный порядок: отзывы с содержанием 1★ → 2★ → 3★ → 4★ → 5★ → пустые.",
            html,
        )
        self.assertIn("Начальный состав", html)
        self.assertIn("Добавлено после старта", html)
        self.assertIn('" · приоритет " +', html)
        self.assertIn("data-autoanswers-stop-reason", html)
        self.assertIn("Без ответа Wildberries", html)
        self.assertIn('data-autoanswers-filter="system_answer"', html)
        self.assertIn("run_max_usd", html)
        self.assertIn("background: var(--control-bg)", html)
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
        manual = self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
            {"selector_state": "manual", "expected_policy_epoch": 0},
            actor_id="admin",
        )
        with self.assertRaisesRegex(AutoanswersRuntimeError, "preview"):
            self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(
                {
                    "selector_state": "auto_safe",
                    "expected_policy_epoch": manual["settings"]["policy_epoch"],
                },
                actor_id="admin",
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
