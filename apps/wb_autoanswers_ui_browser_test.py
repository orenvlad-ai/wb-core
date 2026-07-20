#!/usr/bin/env python3
"""Read-only browser acceptance for compact autoanswers UI behavior."""

from __future__ import annotations

import base64
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from apps.wb_autoanswers_runtime_test import feedback, successful_result


class AutoanswersUiBrowserTest(unittest.TestCase):
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

    def test_compact_detail_autogrow_fixed_answer_copy_media_and_narrow_layout(self) -> None:
        fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
        with fixture as base_url:
            repository = fixture.entrypoint.autoanswers_repository
            repository.update_settings(master_enabled=True, mode="manual", actor_id="local_operator")
            row = feedback("browser-autoanswers", text="Отзыв с подробным описанием установки")
            row["pros"] = ""
            row["cons"] = ""
            row["bables"] = []
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
                self.assertEqual(page.locator("[data-autoanswers-queue-metrics] .autoanswers-queue-metric").count(), 18)
                self.assertEqual(page.locator("[data-autoanswers-progress-bars] .autoanswers-progress-row").count(), 2)
                page.locator("[data-autoanswers-copy]").click()
                page.get_by_role("button", name="Скопировано").wait_for()
                self.assertEqual(answer_box.inner_text().replace("Скопировано", "Копировать"), before_text)

                page.locator("[data-autoanswers-open]").click()
                dialog = page.locator("[data-autoanswers-detail-dialog][open]")
                dialog.wait_for()
                technical = dialog.locator(".autoanswers-technical")
                self.assertFalse(technical.evaluate("node => node.open"))
                visible = dialog.locator("[data-autoanswers-detail-body]").inner_text()
                for marker in ("Товар", "Фото и видео покупателя", "Ответ Wildberries", "Готовый ответ", "Статус"):
                    self.assertIn(marker, visible)
                for omitted in ("Плюсы:", "Минусы:", "Теги:", "Route:", "JSON contract", "Audit trail"):
                    self.assertNotIn(omitted, visible)
                self.assertEqual(dialog.locator(".autoanswers-media-item").count(), 2)
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
