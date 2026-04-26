"""Адаптерная граница bounded promo XLSX collector блока."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Protocol
from urllib import parse as urllib_parse
from zoneinfo import ZoneInfo

from playwright.sync_api import Browser, BrowserContext, Download, Page, Playwright, sync_playwright

from packages.contracts.promo_xlsx_collector_block import (
    CampaignManifestItem,
    CampaignManifestSnapshot,
    CollectorStateSnapshot,
    DownloadArtifact,
    DrawerResetSummary,
    HydrationAttemptSummary,
    ModalHandlingSummary,
    PromoXlsxCollectorRequest,
    TimelineBlockSnapshot,
)


DEFAULT_SESSION_STATE_PATH = "/Users/ovlmacbook/Projects/wb-web-bot/storage_state.json"
SERVER_DEFAULT_SESSION_STATE_PATH = "/opt/wb-web-bot/storage_state.json"
TIMELINE_ACTION_SELECTOR = '[data-testid="timeline-action"]'
COOKIE_ACCEPT_TEXT = "Принимаю"
AUTO_PROMO_MODAL_OVERLAY_SELECTOR = '[data-testid="components/auto-promo-modal-overlay"]'
AUTO_PROMO_MODAL_CLOSE_SELECTOR = (
    '[data-testid="components/auto-promo-modal/close-button-button-interface"]'
)
DRAWER_ROOT_SELECTOR = "#Portal-drawer"
DRAWER_OVERLAY_SELECTOR = (
    '#Portal-drawer [data-testid="pages/main-page/promo-action-wizard/drawer-drawer-overlay"]'
)
DRAWER_CLOSE_SELECTOR = (
    '#Portal-drawer [data-testid="pages/main-page/promo-action-wizard/drawer-close-button-button-ghost"]'
)
CONFIGURE_BUTTON_TEXT = "Настроить список товаров"
GENERATE_BUTTON_TEXT = "Сформировать файл"
DOWNLOAD_BUTTON_TEXT = "Скачать файл"
READY_TEXT = "Файл сформирован"
VISIBLE_TABS = ("Доступные", "Участвую", "Не участвую", "Акции WB", "Мои акции", "Скидка лояльности")
BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
PROMOTIONS_TIMELINE_PATH = "/ns/calendar-api/dp-calendar/web/api/v3/promotions/timeline"
_ENDED_PARTICIPATION_STATUSES = {"PARTICIPATED", "SKIPPED"}
_ACTIVE_PARTICIPATION_STATUSES = {"PARTICIPATING", "SKIPPING"}
_FUTURE_PARTICIPATION_STATUSES = {"WILL_PARTICIPATE"}
_MONTH_GENITIVE = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


class PromoCollectorDriver(Protocol):
    def start(self, request: PromoXlsxCollectorRequest) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def attempt_hydration(self, attempt_num: int, label_prefix: str = "initial") -> HydrationAttemptSummary:
        raise NotImplementedError

    def enumerate_timeline_blocks(self, limit: int | None = None) -> list[TimelineBlockSnapshot]:
        raise NotImplementedError

    def open_timeline_candidate(self, candidate: Any) -> CollectorStateSnapshot:
        raise NotImplementedError

    def open_generate_screen(self, slug: str) -> CollectorStateSnapshot:
        raise NotImplementedError

    def generate_file_and_wait_ready(self, slug: str) -> CollectorStateSnapshot:
        raise NotImplementedError

    def download_current_workbook(self) -> DownloadArtifact:
        raise NotImplementedError

    def reset_drawer(self, label: str) -> DrawerResetSummary:
        raise NotImplementedError

    def current_timeline_count(self) -> int:
        raise NotImplementedError

    def current_url(self) -> str:
        raise NotImplementedError

    def last_state_snapshot(self) -> CollectorStateSnapshot | None:
        raise NotImplementedError

    def campaign_manifest_snapshot(self) -> CampaignManifestSnapshot:
        raise NotImplementedError


class PlaywrightPromoCollectorDriver:
    def __init__(self, output_root: Path) -> None:
        self._output_root = output_root
        self._artifacts_dir = output_root / "artifacts"
        self._logs_dir = output_root / "logs"
        self._downloads_dir = output_root / "downloads"
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._request: PromoXlsxCollectorRequest | None = None
        self._last_state: CollectorStateSnapshot | None = None
        self._latest_period_id: int | None = None
        self._campaign_manifest_snapshot = CampaignManifestSnapshot()

    def start(self, request: PromoXlsxCollectorRequest) -> None:
        self._request = request
        self._campaign_manifest_snapshot = CampaignManifestSnapshot()
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._downloads_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=request.headless)
        self._context = self._browser.new_context(
            storage_state=resolve_storage_state_path(request.storage_state_path),
            locale="ru-RU",
            timezone_id="Asia/Yekaterinburg",
            viewport={"width": 1600, "height": 1200},
            accept_downloads=True,
            record_har_path=str(self._logs_dir / "session.har"),
        )
        self._page = self._context.new_page()
        self._page.on("console", self._on_console)
        self._page.on("request", self._on_request)
        self._page.on("response", self._on_response)

    def stop(self) -> None:
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def attempt_hydration(self, attempt_num: int, label_prefix: str = "initial") -> HydrationAttemptSummary:
        page = self._require_page()
        request = self._require_request()
        started = time.monotonic()
        page.goto(request.start_url, wait_until="domcontentloaded")
        initial = self.capture_state(f"{label_prefix}_attempt{attempt_num}_direct_open")
        cookie_clicked = False
        cookie_before = None
        cookie_after = None
        hydrated = False
        hydrated_state = initial
        while time.monotonic() - started <= request.hydration_wait_sec:
            if not cookie_clicked and self._cookie_button_visible():
                cookie_before = self.capture_state(f"{label_prefix}_attempt{attempt_num}_during_wait_cookie_before")
                self._record_action(
                    "click_cookie_accept",
                    {
                        "text": COOKIE_ACCEPT_TEXT,
                        "url": page.url,
                    },
                )
                page.get_by_text(COOKIE_ACCEPT_TEXT, exact=True).first.click(timeout=5000)
                cookie_clicked = True
                time.sleep(1.0)
                cookie_after = self.capture_state(f"{label_prefix}_attempt{attempt_num}_during_wait_cookie_after")
            time.sleep(1.0)
            probe = self.capture_state(f"{label_prefix}_attempt{attempt_num}_probe", persist=False)
            if is_hydrated_state(probe):
                hydrated = True
                hydrated_state = self.capture_state(f"{label_prefix}_attempt{attempt_num}_hydrated")
                break
        if not hydrated:
            failed = self.capture_state(f"{label_prefix}_attempt{attempt_num}_failed")
            return HydrationAttemptSummary(
                attempt_num=attempt_num,
                entry_strategy="direct_open",
                cookie_clicked=cookie_clicked,
                hydrated_success=False,
                title=failed.title,
                url=failed.url,
                timeline_count=failed.timeline_count,
                overlay_count=failed.overlay_count,
                time_to_hydrated_sec=None,
                blocker="calendar did not hydrate",
            )
        modal_info = self._handle_modal_if_present()
        return HydrationAttemptSummary(
            attempt_num=attempt_num,
            entry_strategy="direct_open",
            cookie_clicked=cookie_clicked,
            hydrated_success=True,
            title=hydrated_state.title,
            url=hydrated_state.url,
            timeline_count=hydrated_state.timeline_count,
            overlay_count=hydrated_state.overlay_count,
            time_to_hydrated_sec=round(time.monotonic() - started, 2),
            modal_info=modal_info,
        )

    def enumerate_timeline_blocks(self, limit: int | None = None) -> list[TimelineBlockSnapshot]:
        page = self._require_page()
        timeline = page.locator(TIMELINE_ACTION_SELECTOR)
        count = timeline.count()
        state = self.capture_state("timeline_enumerated")
        blocks: list[TimelineBlockSnapshot] = []
        for index in range(count):
            if limit is not None and len(blocks) >= limit:
                break
            raw_text = timeline.nth(index).inner_text().strip()
            blocks.append(TimelineBlockSnapshot(index=index, raw_text=raw_text))
        self._record_action(
            "enumerate_timeline",
            {"count": count, "usable_count": len(blocks), "url": state.url},
        )
        return blocks

    def open_timeline_candidate(self, candidate: Any) -> CollectorStateSnapshot:
        page = self._require_page()
        timeline = page.locator(TIMELINE_ACTION_SELECTOR)
        block = timeline.nth(candidate.index)
        block.scroll_into_view_if_needed(timeout=5000)
        self._record_action(
            "click_timeline_candidate",
            {
                "index": candidate.index,
                "title": getattr(candidate, "title", None),
                "url": page.url,
            },
        )
        block.click(timeout=8000)
        self._wait_for(lambda: DRAWER_CLOSE_SELECTOR in page.content() or self._count(DRAWER_CLOSE_SELECTOR) > 0, timeout_sec=10)
        time.sleep(0.5)
        return self.capture_state(f"card__{_slug(getattr(candidate, 'title', 'candidate'))}")

    def open_generate_screen(self, slug: str) -> CollectorStateSnapshot:
        page = self._require_page()
        self._record_action("click_configure", {"slug": slug, "url": page.url})
        page.get_by_text(CONFIGURE_BUTTON_TEXT, exact=True).first.click(timeout=8000)
        self._wait_for(lambda: self._has_generate() or self._has_download(), timeout_sec=12)
        return self.capture_state(f"generate_screen__{slug}")

    def generate_file_and_wait_ready(self, slug: str) -> CollectorStateSnapshot:
        page = self._require_page()
        self._record_action("click_generate", {"slug": slug, "url": page.url})
        page.get_by_text(GENERATE_BUTTON_TEXT, exact=True).first.click(timeout=8000)
        self._wait_for(lambda: self._has_ready() or self._has_download(), timeout_sec=20)
        return self.capture_state(f"ready_signal__{slug}")

    def download_current_workbook(self) -> DownloadArtifact:
        page = self._require_page()
        self._record_action("click_download", {"url": page.url})
        with page.expect_download(timeout=15000) as download_info:
            page.get_by_text(DOWNLOAD_BUTTON_TEXT, exact=True).first.click(timeout=8000)
        download = download_info.value
        timestamp = _ts_slug()
        saved_path = self._downloads_dir / f"{timestamp}__{download.suggested_filename}"
        download.save_as(str(saved_path))
        self._record_action(
            "download_completed",
            {
                "suggested_filename": download.suggested_filename,
                "saved_path": str(saved_path),
                "period_id": self._latest_period_id,
            },
        )
        return DownloadArtifact(
            original_suggested_filename=download.suggested_filename,
            saved_path=str(saved_path),
            saved_filename=saved_path.name,
            period_id=self._latest_period_id,
        )

    def reset_drawer(self, label: str) -> DrawerResetSummary:
        page = self._require_page()
        before_state = self.capture_state(f"{label}__before")
        overlay_before = self._count(DRAWER_OVERLAY_SELECTOR)
        clicked = False
        blocker = None
        if self._count(DRAWER_CLOSE_SELECTOR) > 0:
            clicked = True
            self._record_action(
                "click_drawer_close",
                {"selector": DRAWER_CLOSE_SELECTOR, "url": page.url},
            )
            page.locator(DRAWER_CLOSE_SELECTOR).first.click(timeout=8000)
        try:
            self._wait_for(lambda: self._count(DRAWER_OVERLAY_SELECTOR) == 0 and self.current_timeline_count() > 0, timeout_sec=10)
            after_state = self.capture_state(f"{label}__after")
            return DrawerResetSummary(
                clicked=clicked,
                selector=DRAWER_CLOSE_SELECTOR,
                overlay_before=overlay_before,
                success=True,
                after_state_path=after_state.screenshot,
            )
        except Exception as exc:
            blocker = str(exc)
            after_state = self.capture_state(f"{label}__after_failed")
            return DrawerResetSummary(
                clicked=clicked,
                selector=DRAWER_CLOSE_SELECTOR,
                overlay_before=overlay_before,
                success=False,
                after_state_path=after_state.screenshot,
                blocker=blocker,
            )

    def current_timeline_count(self) -> int:
        return self._count(TIMELINE_ACTION_SELECTOR)

    def current_url(self) -> str:
        return self._require_page().url

    def last_state_snapshot(self) -> CollectorStateSnapshot | None:
        return self._last_state

    def campaign_manifest_snapshot(self) -> CampaignManifestSnapshot:
        return self._campaign_manifest_snapshot

    def capture_state(self, label: str, *, persist: bool = True) -> CollectorStateSnapshot:
        page = self._require_page()
        body_text = page.locator("body").inner_text()
        ts = _now_iso()
        screenshot_path = self._artifacts_dir / f"{_ts_slug()}__{label}.png"
        json_path = screenshot_path.with_suffix(".json")
        page.screenshot(path=str(screenshot_path), full_page=True)
        state = CollectorStateSnapshot(
            ts=ts,
            label=label,
            url=page.url,
            title=page.title(),
            timeline_count=self._count(TIMELINE_ACTION_SELECTOR),
            overlay_count=self._count(AUTO_PROMO_MODAL_OVERLAY_SELECTOR) + self._count(DRAWER_OVERLAY_SELECTOR),
            has_modal_close=self._count(AUTO_PROMO_MODAL_CLOSE_SELECTOR) > 0,
            modal_entry_count=self._modal_entry_count(),
            has_configure=self._has_text(CONFIGURE_BUTTON_TEXT),
            has_generate=self._has_generate(),
            has_download=self._has_download(),
            has_ready=self._has_ready(),
            has_cookie_accept=self._cookie_button_visible(),
            body_excerpt=body_text[:16000],
            visible_tabs=[tab for tab in VISIBLE_TABS if tab in body_text],
            screenshot=str(screenshot_path),
        )
        self._last_state = state
        if persist:
            json_path.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
            self._append_jsonl(self._logs_dir / "states.jsonl", asdict(state))
        return state

    def _handle_modal_if_present(self) -> ModalHandlingSummary:
        if self._count(AUTO_PROMO_MODAL_OVERLAY_SELECTOR) <= 0:
            return ModalHandlingSummary(
                modal_present=False,
                modal_closed=False,
                modal_entry_count=0,
            )
        modal_present_state = self.capture_state("modal_present")
        modal_entry_count = modal_present_state.modal_entry_count
        self._record_action(
            "click_modal_close",
            {"selector": AUTO_PROMO_MODAL_CLOSE_SELECTOR, "url": self._require_page().url},
        )
        self._require_page().locator(AUTO_PROMO_MODAL_CLOSE_SELECTOR).first.click(timeout=8000)
        self._wait_for(lambda: self._count(AUTO_PROMO_MODAL_OVERLAY_SELECTOR) == 0 and self.current_timeline_count() > 0, timeout_sec=10)
        closed_state = self.capture_state("modal_closed")
        return ModalHandlingSummary(
            modal_present=True,
            modal_closed=True,
            modal_entry_count=modal_entry_count,
            timeline_after_close=closed_state.timeline_count,
            overlay_after_close=closed_state.overlay_count,
        )

    def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("promo collector page is not initialized")
        return self._page

    def _require_request(self) -> PromoXlsxCollectorRequest:
        if self._request is None:
            raise RuntimeError("promo collector request is not initialized")
        return self._request

    def _count(self, selector: str) -> int:
        try:
            return self._require_page().locator(selector).count()
        except Exception:
            return 0

    def _modal_entry_count(self) -> int:
        try:
            return self._require_page().get_by_text("Перейти в акцию", exact=False).count()
        except Exception:
            return 0

    def _has_text(self, text: str) -> bool:
        try:
            return self._require_page().get_by_text(text, exact=True).count() > 0
        except Exception:
            return False

    def _has_generate(self) -> bool:
        return self._has_text(GENERATE_BUTTON_TEXT)

    def _has_download(self) -> bool:
        return self._has_text(DOWNLOAD_BUTTON_TEXT)

    def _has_ready(self) -> bool:
        return READY_TEXT in self._require_page().locator("body").inner_text()

    def _cookie_button_visible(self) -> bool:
        try:
            locator = self._require_page().get_by_text(COOKIE_ACCEPT_TEXT, exact=True).first
            return locator.count() > 0 and locator.is_visible()
        except Exception:
            return False

    def _wait_for(self, predicate: Any, *, timeout_sec: float) -> None:
        deadline = time.monotonic() + timeout_sec
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(0.25)
        if last_error is not None:
            raise RuntimeError(str(last_error))
        raise RuntimeError("timeout waiting for expected browser state")

    def _record_action(self, kind: str, payload: dict[str, Any]) -> None:
        entry = {
            "ts": _now_iso(),
            "kind": kind,
            **payload,
        }
        self._append_jsonl(self._logs_dir / "actions.jsonl", entry)

    def _on_console(self, message: Any) -> None:
        self._append_jsonl(
            self._logs_dir / "console.jsonl",
            {
                "ts": _now_iso(),
                "type": getattr(message, "type", "<unknown>"),
                "text": message.text,
            },
        )

    def _on_request(self, request: Any) -> None:
        period_id = _parse_period_id(request.url)
        if period_id is not None:
            self._latest_period_id = period_id
        self._append_jsonl(
            self._logs_dir / "requests.jsonl",
            {
                "ts": _now_iso(),
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
                "period_id": period_id,
            },
        )

    def _on_response(self, response: Any) -> None:
        period_id = _parse_period_id(response.url)
        if period_id is not None:
            self._latest_period_id = period_id
        self._capture_campaign_manifest_response(response)
        self._append_jsonl(
            self._logs_dir / "responses.jsonl",
            {
                "ts": _now_iso(),
                "url": response.url,
                "status": response.status,
                "content_type": response.headers.get("content-type"),
                "period_id": period_id,
            },
        )

    def _capture_campaign_manifest_response(self, response: Any) -> None:
        parsed_url = urllib_parse.urlparse(str(getattr(response, "url", "") or ""))
        if parsed_url.path != PROMOTIONS_TIMELINE_PATH:
            return
        if int(getattr(response, "status", 0) or 0) != 200:
            return
        content_type = str(getattr(response, "headers", {}).get("content-type", "") or "")
        if "json" not in content_type.lower():
            return
        started = time.perf_counter()
        try:
            payload = response.json()
        except Exception as exc:
            self._campaign_manifest_snapshot = CampaignManifestSnapshot(
                manifest_source="network_response",
                manifest_loaded_success=False,
                manifest_error_kind=f"manifest_json_error={type(exc).__name__}",
                manifest_source_path=parsed_url.path,
                manifest_load_duration_ms=_elapsed_ms(started),
                manifest_parse_duration_ms=_elapsed_ms(started),
            )
            return
        snapshot = _build_campaign_manifest_snapshot(
            payload=payload,
            source_path=parsed_url.path,
            started_perf=started,
        )
        self._campaign_manifest_snapshot = snapshot
        self._write_campaign_manifest_snapshot(snapshot)

    def _write_campaign_manifest_snapshot(self, snapshot: CampaignManifestSnapshot) -> None:
        payload = asdict(snapshot)
        path = self._logs_dir / "campaign_manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _parse_period_id(url: str) -> int | None:
    query = urllib_parse.urlparse(url).query
    values = urllib_parse.parse_qs(query).get("periodID") or urllib_parse.parse_qs(query).get("periodId")
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None


def _build_campaign_manifest_snapshot(
    *,
    payload: Any,
    source_path: str,
    started_perf: float,
) -> CampaignManifestSnapshot:
    loaded_at = _now_iso()
    promotions = []
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            raw_promotions = data.get("promotions")
            if isinstance(raw_promotions, list):
                promotions = raw_promotions
    campaigns = [
        item
        for item in (_campaign_manifest_item(raw, loaded_at=loaded_at) for raw in promotions)
        if item is not None
    ]
    elapsed = _elapsed_ms(started_perf)
    return CampaignManifestSnapshot(
        manifest_source="network_response",
        manifest_loaded_success=bool(campaigns),
        manifest_campaign_count=len(campaigns),
        manifest_loaded_at=loaded_at,
        manifest_error_kind=None if campaigns else "manifest_promotions_empty_or_unrecognized",
        manifest_source_path=source_path,
        manifest_load_duration_ms=elapsed,
        manifest_parse_duration_ms=elapsed,
        campaigns=campaigns,
    )


def _campaign_manifest_item(raw: Any, *, loaded_at: str) -> CampaignManifestItem | None:
    if not isinstance(raw, dict):
        return None
    promo_id = _safe_int(raw.get("promoID"))
    title = _sanitize_manifest_label(raw.get("name"), limit=160)
    if not title:
        return None
    start_dt = _parse_wb_utc_datetime(raw.get("startDate"))
    end_dt = _parse_wb_utc_datetime(raw.get("endDate"))
    start_at = _local_minute_iso(start_dt)
    end_at = _local_minute_iso(end_dt)
    participation = raw.get("participation") if isinstance(raw.get("participation"), dict) else {}
    participation_status = _sanitize_manifest_label(participation.get("status"), limit=80)
    counts = participation.get("counts") if isinstance(participation.get("counts"), dict) else {}
    lifecycle_status, lifecycle_confidence, downloadability, downloadability_confidence, sources = (
        _classify_manifest_campaign(
            participation_status=participation_status,
            start_dt=start_dt,
            end_dt=end_dt,
        )
    )
    promo_type = _sanitize_manifest_label(raw.get("type"), limit=80)
    autoaction_marker = None
    if promo_type == "AUTO_PROMO":
        autoaction_marker = "auto_promo"
    elif "автоматические скидки" in title.lower():
        autoaction_marker = "auto_discount"
    if promo_id is not None:
        sources.append("manifest_promo_id")
    if start_dt and end_dt:
        sources.append("manifest_period")
    if participation_status:
        sources.append("manifest_participation_status")
    return CampaignManifestItem(
        campaign_id=str(promo_id) if promo_id is not None else None,
        promo_id=promo_id,
        title=title,
        period_text=_manifest_period_text(start_dt, end_dt),
        start_at=start_at,
        end_at=end_at,
        lifecycle_status=lifecycle_status,
        lifecycle_status_confidence=lifecycle_confidence,
        participation_status=participation_status or None,
        downloadability=downloadability,
        downloadability_confidence=downloadability_confidence,
        goods_count=_safe_int(counts.get("eligible")),
        autoaction_marker=autoaction_marker,
        raw_status_code=participation_status or None,
        confidence=lifecycle_confidence,
        evidence_sources=sorted(set(sources)),
        loaded_at=loaded_at,
    )


def _classify_manifest_campaign(
    *,
    participation_status: str,
    start_dt: datetime | None,
    end_dt: datetime | None,
) -> tuple[str, str, str, str, list[str]]:
    now = datetime.now(BUSINESS_TIMEZONE)
    status_code = str(participation_status or "").strip().upper()
    sources: list[str] = []
    if end_dt is not None and end_dt <= now:
        sources.append("manifest_end_date_elapsed")
        if status_code in _ENDED_PARTICIPATION_STATUSES:
            return "ended", "high", "not_available", "high", sources
    if start_dt is not None and start_dt > now:
        sources.append("manifest_start_date_future")
        if status_code in _FUTURE_PARTICIPATION_STATUSES:
            return "future", "high", "unknown", "low", sources
    if start_dt is not None and end_dt is not None and start_dt <= now <= end_dt:
        sources.append("manifest_current_date_window")
        if status_code in _ACTIVE_PARTICIPATION_STATUSES:
            return "active", "high", "available", "medium", sources
        if status_code in _FUTURE_PARTICIPATION_STATUSES:
            return "pending", "medium", "unknown", "low", sources
    if status_code in _ACTIVE_PARTICIPATION_STATUSES:
        return "active", "medium", "available", "low", sources
    if status_code in _FUTURE_PARTICIPATION_STATUSES:
        return "future", "medium", "unknown", "low", sources
    return "unknown", "low", "unknown", "low", sources


def _parse_wb_utc_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BUSINESS_TIMEZONE)


def _local_minute_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%Y-%m-%dT%H:%M")


def _manifest_period_text(start_dt: datetime | None, end_dt: datetime | None) -> str | None:
    if start_dt is None or end_dt is None:
        return None
    start_month = _MONTH_GENITIVE.get(start_dt.month, "")
    end_month = _MONTH_GENITIVE.get(end_dt.month, "")
    if not start_month or not end_month:
        return None
    if start_dt.month == end_dt.month:
        return f"{start_dt.day:02d} - {end_dt.day:02d} {end_month}"
    return f"{start_dt.day:02d} {start_month} - {end_dt.day:02d} {end_month}"


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _sanitize_manifest_label(value: Any, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()
    return text[:limit]


def _elapsed_ms(started_perf: float) -> int:
    return max(0, int(round((time.perf_counter() - started_perf) * 1000)))


def is_hydrated_state(state: CollectorStateSnapshot) -> bool:
    return state.title == "Акции WB" and (state.timeline_count > 0 or state.overlay_count > 0)


def resolve_storage_state_path(explicit_path: str | None) -> str:
    if explicit_path and explicit_path.strip():
        return explicit_path.strip()
    env_path = str(os.environ.get("PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH", "")).strip()
    if env_path:
        return env_path
    for candidate in (DEFAULT_SESSION_STATE_PATH, SERVER_DEFAULT_SESSION_STATE_PATH):
        if Path(candidate).exists():
            return candidate
    return DEFAULT_SESSION_STATE_PATH


def _slug(value: str) -> str:
    normalized = re.sub(r"[^\w]+", "-", value.lower(), flags=re.UNICODE)
    normalized = normalized.strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized or "pending"


def _now_iso() -> str:
    return datetime.now(BUSINESS_TIMEZONE).isoformat(timespec="seconds")


def _ts_slug() -> str:
    return datetime.now(BUSINESS_TIMEZONE).strftime("%Y%m%d_%H%M%S")
