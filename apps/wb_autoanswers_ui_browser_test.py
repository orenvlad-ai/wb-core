#!/usr/bin/env python3
"""Read-only browser acceptance for compact autoanswers UI behavior."""

from __future__ import annotations

import base64
from copy import deepcopy
import json
from pathlib import Path
import unittest

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from apps.wb_autoanswers_production_ui_flow import _json_get
from apps.wb_autoanswers_runtime_test import feedback, successful_result


class _FakeJsonResponse:
    def __init__(self, *, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _FakeRequest:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def get(self, *_args: object, **_kwargs: object) -> object:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeContext:
    def __init__(self, outcomes: list[object]) -> None:
        self.request = _FakeRequest(outcomes)


class AutoanswersUiBrowserTest(unittest.TestCase):
    def test_production_json_get_retries_only_transient_connection_reset(self) -> None:
        context = _FakeContext(
            [
                PlaywrightError("read ECONNRESET"),
                _FakeJsonResponse(status=200, payload={"ok": True}),
            ]
        )
        self.assertEqual(
            _json_get(context, "https://example.invalid/read-only", label="fixture", retry_delay_seconds=0),
            {"ok": True},
        )
        self.assertEqual(context.request.calls, 2)

        http_failure = _FakeContext([_FakeJsonResponse(status=500, payload={"error": "failed"})])
        with self.assertRaisesRegex(AssertionError, "expected HTTP 200"):
            _json_get(
                http_failure,
                "https://example.invalid/read-only",
                label="fixture",
                retry_delay_seconds=0,
            )
        self.assertEqual(http_failure.request.calls, 1)

        non_transient = _FakeContext([PlaywrightError("certificate rejected")])
        with self.assertRaisesRegex(PlaywrightError, "certificate rejected"):
            _json_get(
                non_transient,
                "https://example.invalid/read-only",
                label="fixture",
                retry_delay_seconds=0,
            )
        self.assertEqual(non_transient.request.calls, 1)

        exhausted = _FakeContext([PlaywrightError("read ECONNRESET")] * 3)
        with self.assertRaisesRegex(PlaywrightError, "ECONNRESET"):
            _json_get(
                exhausted,
                "https://example.invalid/read-only",
                label="fixture",
                retry_delay_seconds=0,
            )
        self.assertEqual(exhausted.request.calls, 3)

    def test_open_error_badge_refreshes_after_external_lifecycle_recovery(self) -> None:
        fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
        with fixture as base_url:
            repository = fixture.entrypoint.autoanswers_repository
            repository.update_settings(
                master_enabled=True,
                mode="auto_all",
                actor_id="local_operator",
            )
            base_payload = (
                fixture.entrypoint.handle_sheet_feedbacks_autoanswers_settings_request()
            )
            error_payload = deepcopy(base_payload)
            error_payload["lifecycle"] = {
                "desired": True,
                "actual": False,
                "lifecycle_state": "error",
                "drift_status": "blocked",
                "stop_reason": "worker_error",
                "last_error": "publication_already_exists",
                "components": {
                    "worker": {
                        "desired": True,
                        "actual": False,
                        "drift_status": "blocked",
                    },
                    "readonly_sync": {
                        "desired": True,
                        "actual": True,
                        "drift_status": "matched",
                    },
                },
            }
            running_payload = deepcopy(base_payload)
            running_payload["lifecycle"] = {
                "desired": True,
                "actual": True,
                "lifecycle_state": "running",
                "drift_status": "matched",
                "stop_reason": "",
                "last_error": "",
                "components": {
                    "worker": {
                        "desired": True,
                        "actual": True,
                        "drift_status": "matched",
                    },
                    "readonly_sync": {
                        "desired": True,
                        "actual": True,
                        "drift_status": "matched",
                    },
                },
                "runtime_schedule": {
                    "last_scheduler_tick_at": "2026-07-25T10:00:00Z",
                },
            }
            settings_requests = 0

            def route_settings(route: object) -> None:
                nonlocal settings_requests
                settings_requests += 1
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=(
                        json.dumps(
                            error_payload if settings_requests == 1 else running_payload,
                            ensure_ascii=False,
                        )
                    ),
                )

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                context.route(
                    "**/v1/sheet-vitrina-v1/feedbacks/autoanswers/settings",
                    route_settings,
                )
                page = context.new_page()
                page.goto(
                    base_url + "/sheet-vitrina-v1/vitrina?tab=feedbacks",
                    wait_until="domcontentloaded",
                )
                status = page.locator("[data-autoanswers-master-status]")
                status.wait_for()
                self.assertEqual(status.inner_text(), "Остановлено из-за ошибки")
                page.wait_for_function(
                    """() => document.querySelector('[data-autoanswers-master-status]').textContent === 'Работает'""",
                    timeout=7000,
                )
                self.assertGreaterEqual(settings_requests, 2)
                browser.close()

    def test_technical_spoiler_names_not_run_checks_before_generation(self) -> None:
        fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
        with fixture as base_url:
            repository = fixture.entrypoint.autoanswers_repository
            repository.update_settings(master_enabled=True, mode="manual", actor_id="local_operator")
            repository.upsert_feedback(
                feedback("browser-not-generated", text="Отзыв ещё не отправлялся в AI"),
                source_stream="steady",
                run_kind="steady",
            )
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.goto(
                    base_url + "/sheet-vitrina-v1/vitrina?tab=feedbacks",
                    wait_until="domcontentloaded",
                )
                page.locator('[data-feedbacks-subpanel="server-reviews"]:not([hidden])').wait_for()
                page.wait_for_function("document.querySelectorAll('[data-autoanswers-open]').length === 1")
                page.locator("[data-autoanswers-open]").click()
                dialog = page.locator("[data-autoanswers-detail-dialog][open]")
                dialog.wait_for()
                dialog.locator(".autoanswers-technical summary").click()
                expanded = dialog.locator("[data-autoanswers-detail-body]").inner_text()
                for marker in (
                    "AI ещё не запускался",
                    "JSON contract: не запускался",
                    "Hard gates: не запускались",
                    "Fallback: не применялся",
                    "Media uncertainty: не проверялась",
                ):
                    self.assertIn(marker, expanded)
                self.assertEqual(dialog.locator("[data-autoanswers-generate]").count(), 1)
                self.assertEqual(dialog.locator("[data-autoanswers-publish]").count(), 0)
                browser.close()

    def test_current_queued_job_suppresses_duplicate_manual_generation(self) -> None:
        fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
        with fixture as base_url:
            repository = fixture.entrypoint.autoanswers_repository
            repository.update_settings(master_enabled=True, mode="manual", actor_id="local_operator")
            outcome = repository.upsert_feedback(
                feedback("browser-current-queued", text="Отзыв уже поставлен в очередь"),
                source_stream="steady",
                run_kind="steady",
            )
            repository.enqueue_manual_processing(
                "browser-current-queued",
                content_version=outcome["content_version"],
                actor_id="local_operator",
            )
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.goto(
                    base_url + "/sheet-vitrina-v1/vitrina?tab=feedbacks",
                    wait_until="domcontentloaded",
                )
                page.locator('[data-feedbacks-subpanel="server-reviews"]:not([hidden])').wait_for()
                page.wait_for_function("document.querySelectorAll('[data-autoanswers-open]').length === 1")
                page.locator("[data-autoanswers-open]").click()
                dialog = page.locator("[data-autoanswers-detail-dialog][open]")
                dialog.wait_for()
                self.assertIn("В очереди", dialog.locator("[data-autoanswers-detail-body]").inner_text())
                self.assertEqual(dialog.locator("[data-autoanswers-generate]").count(), 0)
                self.assertEqual(dialog.locator("[data-autoanswers-publish]").count(), 0)
                browser.close()

    def test_limits_modal_context_focus_readback_and_immutable_run_cap(self) -> None:
        fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
        with fixture as base_url:
            repository = fixture.entrypoint.autoanswers_repository
            preview = repository.preview_mode_transition(
                "draft_only",
                actor_id="local_operator",
                run_max_usd="0.50",
            )
            applied = repository.apply_mode_transition(
                "draft_only",
                actor_id="local_operator",
                preview_id=preview["preview_id"],
            )
            run_id = applied["sweep"]["transition_run_id"]
            with repository.transaction() as conn:
                repository._set_stop_reason(  # noqa: SLF001 - UI pause fixture
                    conn,
                    "daily_budget_reached",
                    at=fixture.now,
                )

            posted: list[dict[str, object]] = []

            def settings_payload() -> dict[str, object]:
                payload = (
                    fixture.entrypoint.handle_sheet_feedbacks_autoanswers_settings_request()
                )
                payload["lifecycle"] = {
                    "desired": True,
                    "actual": True,
                    "lifecycle_state": "running",
                    "drift_status": "matched",
                    "components": {
                        "worker": {
                            "desired": True,
                            "actual": True,
                            "drift_status": "matched",
                        },
                        "readonly_sync": {
                            "desired": True,
                            "actual": True,
                            "drift_status": "matched",
                        },
                    },
                }
                payload["runtime"]["progress"]["stop_reason"] = (
                    "daily_budget_reached"
                )
                payload["budget"]["daily_used_and_reserved_usd"] = payload[
                    "settings"
                ]["daily_cap_usd"]
                return payload

            def route_settings(route: object) -> None:
                if route.request.method == "POST":
                    body = route.request.post_data_json
                    posted.append(dict(body))
                    repository.update_settings(
                        hourly_cap_usd=body["hourly_cap_usd"],
                        daily_cap_usd=body["daily_cap_usd"],
                        monthly_cap_usd=body["monthly_cap_usd"],
                        max_paid_reviews_per_hour=body[
                            "max_paid_reviews_per_hour"
                        ],
                        global_paid_review_concurrency=body[
                            "global_paid_review_concurrency"
                        ],
                        max_inflight_role_calls=body["max_inflight_role_calls"],
                        max_materialized_processing_jobs=body[
                            "max_materialized_processing_jobs"
                        ],
                        actor_id="local_operator",
                    )
                    payload = settings_payload()
                    payload["confirmed_limits"] = {
                        key: payload["settings"][key]
                        for key in (
                            "hourly_cap_usd",
                            "daily_cap_usd",
                            "monthly_cap_usd",
                            "max_paid_reviews_per_hour",
                            "global_paid_review_concurrency",
                            "max_inflight_role_calls",
                            "max_materialized_processing_jobs",
                        )
                    }
                    payload["mutation_status"] = "confirmed"
                else:
                    payload = settings_payload()
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps(payload, ensure_ascii=False),
                )

            page_errors: list[str] = []
            console_errors: list[str] = []
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                context.route(
                    "**/v1/sheet-vitrina-v1/feedbacks/autoanswers/settings",
                    route_settings,
                )
                page = context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: console_errors.append(message.text)
                    if message.type == "error"
                    else None,
                )
                page.goto(
                    base_url + "/sheet-vitrina-v1/vitrina?tab=feedbacks",
                    wait_until="domcontentloaded",
                )
                page.locator("[data-autoanswers-increase-limit]").wait_for()
                page.locator("[data-autoanswers-increase-limit]").click()
                modal = page.locator("[data-autoanswers-limits-modal]:not([hidden])")
                modal.wait_for()
                daily_input = modal.locator(
                    '[data-autoanswers-setting="daily_cap_usd"]'
                )
                self.assertTrue(daily_input.evaluate("node => node === document.activeElement"))
                daily_context = modal.locator(
                    '[data-autoanswers-limit-current="daily_cap_usd"]'
                ).inner_text()
                self.assertIn("Израсходовано и зарезервировано", daily_context)
                self.assertIn("текущий лимит", daily_context)
                self.assertIn("новый лимит", daily_context)
                run_cap_text = modal.locator(
                    "[data-autoanswers-active-run-cap]"
                ).inner_text()
                self.assertIn("$0.50", run_cap_text)
                self.assertIn(run_id, run_cap_text)
                modal_style = modal.locator(".autoanswers-limits-modal").evaluate(
                    "node => ({background: getComputedStyle(node).backgroundColor, opacity: getComputedStyle(node).opacity})"
                )
                self.assertEqual(modal_style["background"], "rgb(23, 25, 31)")
                self.assertEqual(modal_style["opacity"], "1")

                daily_input.fill("6")
                modal.locator("[data-autoanswers-save-limits]").click()
                page.wait_for_function(
                    """() => {
                      const node = document.querySelector('[data-autoanswers-limits-result]');
                      return node && node.textContent.includes('Сохранено и подтверждено сервером');
                    }"""
                )
                self.assertEqual(repository.settings().daily_cap_usd, 6.0)
                self.assertEqual(
                    repository.reconciliation_status()["run_max_usd"],
                    "0.50000000",
                )
                self.assertEqual(len(posted), 1)
                self.assertTrue(
                    str(posted[0]["expected_settings_revision"]).startswith(
                        "sha256:"
                    )
                )
                self.assertEqual(
                    posted[0]["expected_policy_epoch"],
                    repository.settings().policy_epoch,
                )
                self.assertEqual(page_errors, [])
                self.assertEqual(console_errors, [])
                browser.close()

    def test_compact_detail_autogrow_fixed_answer_copy_media_and_narrow_layout(self) -> None:
        fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
        with fixture as base_url:
            repository = fixture.entrypoint.autoanswers_repository
            repository.update_settings(master_enabled=True, mode="manual", actor_id="local_operator")
            row = feedback("browser-autoanswers", text="Отзыв с подробным описанием установки")
            row["pros"] = ""
            row["cons"] = ""
            row["bables"] = []
            row["photoLinks"].append(
                {
                    "fullSize": "https://cdn.example/photo-2.jpg",
                    "miniSize": "https://cdn.example/photo-2-mini.jpg",
                }
            )
            row["video"] = {"link": "https://videofeedback01.wbbasket.ru/master.m3u8"}
            outcome = repository.upsert_feedback(row, source_stream="steady", run_kind="steady")
            media_root = Path(fixture.runtime_dir) / "wb_autoanswers_media" / "browser" / "1"
            media_root.mkdir(parents=True)
            photo_path = media_root / "photo.png"
            preview_path = media_root / "preview.png"
            frame_path = media_root / "frame.jpg"
            pixel = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
            photo_path.write_bytes(pixel)
            preview_path.write_bytes(pixel)
            frame_path.write_bytes(b"\xff\xd8\xffframe")
            repository.update_media_result(
                feedback_id=row["id"],
                content_version=outcome["content_version"],
                kind="photo",
                ordinal=0,
                fetch_status="downloaded",
                local_path=str(photo_path),
                sha256="a" * 64,
                mime_type="image/png",
                byte_size=photo_path.stat().st_size,
            )
            repository.update_media_result(
                feedback_id=row["id"],
                content_version=outcome["content_version"],
                kind="photo",
                ordinal=1,
                fetch_status="downloaded",
                local_path=str(photo_path),
                sha256="e" * 64,
                mime_type="image/png",
                byte_size=photo_path.stat().st_size,
            )
            repository.update_media_result(
                feedback_id=row["id"],
                content_version=outcome["content_version"],
                kind="video",
                ordinal=0,
                fetch_status="frames_extracted",
                local_path=str(media_root / "master.m3u8"),
                sha256="b" * 64,
                mime_type="application/vnd.apple.mpegurl",
                byte_size=100,
                preview={
                    "local_path": str(preview_path),
                    "sha256": "c" * 64,
                    "mime_type": "image/png",
                    "byte_size": preview_path.stat().st_size,
                },
            )
            repository.replace_video_frames(
                feedback_id=row["id"],
                content_version=outcome["content_version"],
                frames=[
                    {
                        "local_path": str(frame_path),
                        "sha256": "d" * 64,
                        "mime_type": "image/jpeg",
                        "byte_size": frame_path.stat().st_size,
                    }
                ],
            )
            job = repository.enqueue_manual_processing(
                row["id"], content_version=outcome["content_version"], actor_id="local_operator"
            )
            repository.claim_processing_job(worker_id="browser-fixture")
            repository.settle_budget(job["processing_key"], actual_cost_usd="0.0123")
            repository.complete_generation(
                job["processing_key"],
                result=successful_result("public_only"),
                worker_id="browser-fixture",
            )

            page_errors: list[str] = []
            console_errors: list[str] = []
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
                page = context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: console_errors.append(message.text) if message.type == "error" else None,
                )
                page.goto(base_url + "/sheet-vitrina-v1/vitrina?tab=feedbacks", wait_until="domcontentloaded")
                page.locator('[data-feedbacks-subpanel="server-reviews"]:not([hidden])').wait_for()
                page.wait_for_function(
                    "document.querySelectorAll('[data-autoanswers-open]').length === 1"
                )
                normal_pause = page.evaluate(
                    """() => processOperationalPresentation({
                      desired: true,
                      actual: false,
                      lifecycle_state: "error",
                      drift_status: "blocked",
                      stop_reason: "hourly_budget_reached",
                      last_error: "worker oneshot exited"
                    })"""
                )
                self.assertEqual(
                    normal_pause,
                    {"label": "Работает · штатная пауза", "tone": "warning"},
                )
                backlog = page.locator("[data-autoanswers-backlog]")
                self.assertEqual(backlog.count(), 1)
                self.assertTrue(backlog.is_disabled())

                answer_box = page.locator(".autoanswers-answer-box")
                before_text = answer_box.inner_text()
                box_metrics = answer_box.evaluate(
                    "node => ({height: node.getBoundingClientRect().height, overflowY: getComputedStyle(node).overflowY, background: getComputedStyle(node).backgroundColor, color: getComputedStyle(node).color})"
                )
                self.assertLessEqual(box_metrics["height"], 90)
                self.assertIn(box_metrics["overflowY"], {"auto", "scroll"})
                self.assertNotEqual(box_metrics["background"], "rgb(248, 250, 252)")
                self.assertNotEqual(box_metrics["color"], "rgb(0, 0, 0)")
                queue_metrics = page.locator("[data-autoanswers-queue-metrics]")
                self.assertEqual(queue_metrics.locator(".autoanswers-queue-metric").count(), 27)
                self.assertIn("Начальный состав", queue_metrics.inner_text())
                self.assertIn("Добавлено после старта", queue_metrics.inner_text())
                self.assertEqual(page.locator("[data-autoanswers-progress-bars] .autoanswers-progress-row").count(), 2)
                self.assertEqual(page.locator("[data-autoanswers-content-progress-bars] .autoanswers-progress-row").count(), 2)
                self.assertEqual(page.locator("[data-autoanswers-progress-card]").count(), 2)
                all_card = page.locator('[data-autoanswers-progress-card="all"]')
                content_card = page.locator('[data-autoanswers-progress-card="content-bearing"]')
                self.assertIn("Все отзывы", all_card.inner_text())
                self.assertIn("Отзывы с содержанием", content_card.inner_text())
                self.assertIn("1 из 1", content_card.inner_text())
                card_styles = page.evaluate(
                    """() => {
                      const all = document.querySelector('[data-autoanswers-progress-card="all"]');
                      const content = document.querySelector('[data-autoanswers-progress-card="content-bearing"]');
                      return {
                        gap: content.getBoundingClientRect().top - all.getBoundingClientRect().bottom,
                        allBackground: getComputedStyle(all).backgroundColor,
                        contentBackground: getComputedStyle(content).backgroundColor,
                        allBorder: getComputedStyle(all).borderColor,
                        contentBorder: getComputedStyle(content).borderColor
                      };
                    }"""
                )
                self.assertGreaterEqual(card_styles["gap"], 14)
                self.assertTrue(
                    card_styles["allBackground"] != card_styles["contentBackground"]
                    or card_styles["allBorder"] != card_styles["contentBorder"]
                )
                page.locator("[data-autoanswers-copy]").click()
                page.get_by_role("button", name="Скопировано").wait_for()
                self.assertEqual(answer_box.inner_text().replace("Скопировано", "Копировать"), before_text)

                page.locator("[data-autoanswers-open]").click()
                dialog = page.locator("[data-autoanswers-detail-dialog][open]")
                dialog.wait_for()
                self.assertEqual(page.locator(".autoanswers-technical").count(), 2)
                self.assertEqual(page.locator("[data-autoanswers-limits]").count(), 1)
                technical = dialog.locator("[data-autoanswers-detail-technical]")
                self.assertEqual(technical.count(), 1)
                self.assertFalse(technical.evaluate("node => node.open"))
                visible = dialog.locator("[data-autoanswers-detail-body]").inner_text()
                for marker in ("Товар", "Фото и видео покупателя", "Ответ Wildberries", "Готовый ответ", "Статус"):
                    self.assertIn(marker, visible)
                for omitted in ("Плюсы:", "Минусы:", "Теги:", "Route:", "JSON contract", "Audit trail"):
                    self.assertNotIn(omitted, visible)
                self.assertEqual(dialog.locator('.autoanswers-media-item[alt="Фото покупателя"]').count(), 2)
                self.assertEqual(dialog.locator(".autoanswers-media-item").count(), 3)
                media_items = dialog.locator(".autoanswers-media-item")
                media_items.first.wait_for(state="visible")
                page.wait_for_function(
                    "node => node.complete && node.naturalWidth > 0",
                    arg=media_items.first.element_handle(),
                )
                self.assertGreater(media_items.first.evaluate("node => node.naturalWidth"), 0)
                self.assertEqual(media_items.first.get_attribute("loading"), None)

                editor = dialog.locator("[data-autoanswers-manual-reply]")
                initial = editor.evaluate("node => ({client: node.clientHeight, scroll: node.scrollHeight})")
                self.assertGreaterEqual(initial["client"] + 2, initial["scroll"])
                editor.fill(("Длинный проверочный текст ответа. " * 30).strip())
                grown = editor.evaluate("node => ({client: node.clientHeight, scroll: node.scrollHeight})")
                self.assertGreaterEqual(grown["client"] + 2, grown["scroll"])

                technical.locator("summary").click()
                expanded = dialog.locator("[data-autoanswers-detail-body]").inner_text()
                for marker in ("Route:", "JSON contract", "Audit trail", "Hard gates"):
                    self.assertIn(marker, expanded)

                page.set_viewport_size({"width": 390, "height": 844})
                width = dialog.evaluate("node => node.getBoundingClientRect().width")
                self.assertLessEqual(width, 390)
                overflow = page.evaluate(
                    """() => ({
                      documentWidth: document.documentElement.scrollWidth,
                      viewportWidth: document.documentElement.clientWidth,
                      widest: Array.from(document.querySelectorAll('body *'))
                        .map(node => { const rect = node.getBoundingClientRect(); return {tag: node.tagName, cls: node.className || '', left: rect.left, right: rect.right, width: rect.width, scroll: node.scrollWidth}; })
                        .filter(item => item.right > document.documentElement.clientWidth + 1 || item.width > document.documentElement.clientWidth + 1 || item.scroll > document.documentElement.clientWidth + 1)
                        .sort((a, b) => Math.max(b.right, b.width, b.scroll) - Math.max(a.right, a.width, a.scroll))
                        .slice(0, 8)
                    })"""
                )
                self.assertLessEqual(
                    overflow["documentWidth"],
                    overflow["viewportWidth"] + 1,
                    overflow["widest"],
                )
                self.assertEqual(page_errors, [])
                self.assertEqual(console_errors, [])
                browser.close()

if __name__ == "__main__":
    unittest.main(verbosity=2)
