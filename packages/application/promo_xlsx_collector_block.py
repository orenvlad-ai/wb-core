"""Application-слой bounded promo XLSX collector блока."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import time
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from packages.adapters.promo_xlsx_collector_block import PromoCollectorDriver
from packages.application.promo_campaign_archive import resolve_reusable_campaign
from packages.contracts.promo_xlsx_collector_block import (
    CollectorRunSummary,
    CollectorStateSnapshot,
    DownloadArtifact,
    ExportKind,
    HydrationAttemptSummary,
    PromoCardData,
    PromoMetadata,
    PromoOutcome,
    PromoXlsxCollectorRequest,
    TemporalClassification,
    TimelineBlockSnapshot,
    TimelineCandidate,
    WorkbookInspection,
)


BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_VISIBLE_TABS = ("Доступные", "Участвую", "Не участвую", "Акции WB", "Мои акции", "Скидка лояльности")
_GATING_HEADERS = {
    "Минимальная цена для применения скидки по автоакции",
    "Минимальная цена: осталось дней",
    "Блокировка применения скидки по автоакции",
    "Блокировка изменения скидки для участия в автоакции: осталось дней",
}
_COMMON_EXPORT_HEADERS = {
    "Товар уже участвует в акции",
    "Бренд",
    "Предмет",
    "Наименование",
    "Артикул поставщика",
    "Артикул WB",
    "Статус",
}
COLLECTOR_UI_SCHEMA_VERSION = "promo_collector_ui_status_v1"
COLLECTOR_PREFLIGHT_SCHEMA_VERSION = "promo_collector_preflight_v1"
_ENDED_LABEL_KEYWORDS = ("акция завершилась", "завершилась", "акция завершена", "завершена")
_ACTIVE_LABEL_KEYWORDS = ("акция идёт", "акция идет")
_FUTURE_LABEL_KEYWORDS = ("запланирована", "запланировано")
_PENDING_LABEL_KEYWORDS = ("ожидает", "на модерации")


class PromoXlsxCollectorBlock:
    def __init__(self, driver: PromoCollectorDriver) -> None:
        self._driver = driver

    def execute(self, request: PromoXlsxCollectorRequest) -> CollectorRunSummary:
        run_dir = Path(request.output_root)
        run_dir.mkdir(parents=True, exist_ok=True)
        summary = CollectorRunSummary(
            run_dir=str(run_dir),
            status="running",
            started_at=_now_iso(),
        )
        self._write_json(run_dir / "run_summary.json", asdict(summary))

        self._driver.start(request)
        try:
            hydrated = False
            for attempt_num in range(1, request.hydration_attempt_budget + 1):
                try:
                    attempt = self._driver.attempt_hydration(attempt_num)
                except Exception as exc:
                    attempt = HydrationAttemptSummary(
                        attempt_num=attempt_num,
                        entry_strategy="direct_open",
                        cookie_clicked=False,
                        hydrated_success=False,
                        title="",
                        url="",
                        timeline_count=0,
                        overlay_count=0,
                        time_to_hydrated_sec=None,
                        blocker=f"hydration_exception={exc}",
                    )
                summary.hydration_attempts.append(attempt)
                self._write_json(run_dir / "run_summary.json", asdict(summary))
                if attempt.hydrated_success:
                    hydrated = True
                    break
            if not hydrated:
                summary.status = "blocked"
                self._write_json(run_dir / "run_summary.json", asdict(summary))
                return summary

            blocks = self._driver.enumerate_timeline_blocks(request.max_candidates)
            candidates = [
                build_timeline_candidate(block)
                for block in blocks
            ]
            candidates = [candidate for candidate in candidates if candidate is not None]
            summary.timeline_candidates_found = len(candidates)
            self._write_json(run_dir / "run_summary.json", asdict(summary))

            downloads_seen = 0
            recovery_available = True
            for candidate in candidates:
                if request.max_downloads is not None and downloads_seen >= request.max_downloads:
                    break

                outcome = self._process_candidate(request=request, candidate=candidate)
                if outcome.status == "blocked_before_card" and recovery_available:
                    current_timeline_count = self._driver.current_timeline_count()
                    if current_timeline_count <= 0:
                        recovery = self._driver.attempt_hydration(len(summary.hydration_attempts) + 1, label_prefix="recovery")
                        summary.hydration_attempts.append(recovery)
                        summary.hydration_recoveries_used += 1
                        recovery_available = False
                        self._write_json(run_dir / "run_summary.json", asdict(summary))
                        if recovery.hydrated_success:
                            outcome = self._process_candidate(request=request, candidate=candidate)

                summary.promos.append(outcome)
                self._apply_outcome_preflight_summary(summary, outcome)
                summary.card_confirmed_count += 1 if outcome.card_path else 0
                if outcome.status == "downloaded":
                    summary.downloaded_count += 1
                    downloads_seen += 1
                elif outcome.status == "reused_archive":
                    summary.reused_archive_count += 1
                    downloads_seen += 1
                elif outcome.status == "skipped_past":
                    summary.skipped_past_count += 1
                elif outcome.status == "blocked_before_card":
                    summary.blocked_before_card_count += 1
                elif outcome.status == "blocked_after_card":
                    summary.blocked_after_card_count += 1
                elif outcome.status == "blocked_before_download":
                    summary.blocked_before_download_count += 1
                elif outcome.status == "ambiguous":
                    summary.ambiguous_count += 1
                if outcome.export_kind and outcome.export_kind not in summary.export_kinds:
                    summary.export_kinds.append(outcome.export_kind)
                self._write_json(run_dir / "run_summary.json", asdict(summary))

            if any(
                count > 0
                for count in [
                    summary.blocked_before_card_count,
                    summary.blocked_after_card_count,
                    summary.blocked_before_download_count,
                ]
            ):
                summary.status = "partial"
            else:
                summary.status = "success"
            self._write_json(run_dir / "run_summary.json", asdict(summary))
            return summary
        finally:
            self._driver.stop()

    def _process_candidate(
        self,
        *,
        request: PromoXlsxCollectorRequest,
        candidate: TimelineCandidate,
    ) -> PromoOutcome:
        run_dir = Path(request.output_root)
        preflight_started = time.perf_counter()
        try:
            card_state = self._driver.open_timeline_candidate(candidate)
        except Exception as exc:
            promo_folder = run_dir / "promos" / build_promo_folder_name(None, None, candidate.title)
            promo_folder.mkdir(parents=True, exist_ok=True)
            failure_path = promo_folder / "failure.json"
            failure_png = promo_folder / "failure.png"
            last_state = self._driver.last_state_snapshot()
            if last_state:
                shutil.copy2(last_state.screenshot, failure_png)
            self._write_json(
                failure_path,
                {
                    "status": "blocked_before_card",
                    "candidate_title": candidate.title,
                    "candidate_index": candidate.index,
                    "blocker": str(exc),
                    "last_state": asdict(last_state) if last_state else None,
                },
            )
            metadata = PromoMetadata(
                collected_at=_now_iso(),
                trace_run_dir=request.output_root,
                source_tab=request.source_tab,
                source_filter_code=request.source_filter_code,
                calendar_url=self._driver.current_url(),
                promo_id=None,
                period_id=None,
                promo_title=candidate.title,
                promo_period_text=candidate.short_period_text or "",
                promo_start_at=None,
                promo_end_at=None,
                period_parse_confidence="low",
                temporal_classification="ambiguous",
                promo_status=None,
                promo_status_text=None,
                eligible_count=None,
                participating_count=None,
                excluded_count=None,
                export_kind=None,
                original_suggested_filename=None,
                saved_filename=None,
                saved_path=None,
            )
            return PromoOutcome(
                promo_title=candidate.title,
                timeline_block_index=candidate.index,
                timeline_short_period_text=candidate.short_period_text,
                timeline_preliminary_classification=candidate.preliminary_classification,
                status="blocked_before_card",
                promo_id=None,
                period_id=None,
                promo_folder=str(promo_folder),
                blocker=str(exc),
                metadata=metadata,
            )

        card = extract_card_data(
            snapshot=card_state,
            fallback_title=candidate.title,
            source_tab=request.source_tab,
            source_filter_code=request.source_filter_code,
            reference_year=datetime.now(BUSINESS_TIMEZONE).year,
        )
        preflight = classify_collector_preflight(card)
        early_preflight_duration_ms = _elapsed_ms(preflight_started)

        if preflight["early_preflight_decision"] == "early_non_materializable":
            promo_folder = run_dir / "promos" / build_promo_folder_name(card.promo_id, None, card.promo_title)
            promo_folder.mkdir(parents=True, exist_ok=True)
            card_path = promo_folder / "card.json"
            card_png = promo_folder / "card.png"
            shutil.copy2(card.state_snapshot.screenshot, card_png)
            self._write_json(card_path, asdict(card))
            metadata = build_metadata(
                card=card,
                trace_run_dir=request.output_root,
                source_tab=request.source_tab,
                source_filter_code=request.source_filter_code,
                export_kind=None,
                download=None,
                workbook=None,
                preflight=preflight,
            )
            metadata_path = promo_folder / "metadata.json"
            self._write_json(metadata_path, asdict(metadata))
            drawer_reset = self._driver.reset_drawer(f"early_non_materializable__{slugify(card.promo_title)}")
            return PromoOutcome(
                promo_title=card.promo_title,
                timeline_block_index=candidate.index,
                timeline_short_period_text=candidate.short_period_text,
                timeline_preliminary_classification=candidate.preliminary_classification,
                status="skipped_past",
                promo_id=card.promo_id,
                period_id=None,
                promo_folder=str(promo_folder),
                blocker=None,
                metadata=metadata,
                card_path=str(card_path),
                metadata_path=str(metadata_path),
                drawer_reset=drawer_reset,
                early_preflight_duration_ms=early_preflight_duration_ms,
                **_preflight_outcome_fields(preflight),
            )

        if card.temporal_classification == "past":
            promo_folder = run_dir / "promos" / build_promo_folder_name(card.promo_id, None, card.promo_title)
            promo_folder.mkdir(parents=True, exist_ok=True)
            card_path = promo_folder / "card.json"
            card_png = promo_folder / "card.png"
            shutil.copy2(card.state_snapshot.screenshot, card_png)
            self._write_json(card_path, asdict(card))
            legacy_preflight = {
                **preflight,
                "early_preflight_decision": "legacy_past_skip",
                "heavy_flow_required": False,
                "heavy_flow_reason": "past_temporal_classification",
                "non_materializable_reason": "past_campaign",
                "fallback_to_full_flow_reason": None,
            }
            metadata = build_metadata(
                card=card,
                trace_run_dir=request.output_root,
                source_tab=request.source_tab,
                source_filter_code=request.source_filter_code,
                export_kind=None,
                download=None,
                workbook=None,
                preflight=legacy_preflight,
            )
            metadata_path = promo_folder / "metadata.json"
            self._write_json(metadata_path, asdict(metadata))
            drawer_reset = self._driver.reset_drawer(f"skipped_past__{slugify(card.promo_title)}")
            return PromoOutcome(
                promo_title=card.promo_title,
                timeline_block_index=candidate.index,
                timeline_short_period_text=candidate.short_period_text,
                timeline_preliminary_classification=candidate.preliminary_classification,
                status="skipped_past",
                promo_id=card.promo_id,
                period_id=None,
                promo_folder=str(promo_folder),
                blocker=None,
                metadata=metadata,
                card_path=str(card_path),
                metadata_path=str(metadata_path),
                drawer_reset=drawer_reset,
                early_preflight_duration_ms=early_preflight_duration_ms,
                **_preflight_outcome_fields(legacy_preflight),
            )

        if card.temporal_classification == "ambiguous":
            promo_folder = run_dir / "promos" / build_promo_folder_name(card.promo_id, None, card.promo_title)
            promo_folder.mkdir(parents=True, exist_ok=True)
            card_path = promo_folder / "card.json"
            card_png = promo_folder / "card.png"
            shutil.copy2(card.state_snapshot.screenshot, card_png)
            self._write_json(card_path, asdict(card))
            metadata = build_metadata(
                card=card,
                trace_run_dir=request.output_root,
                source_tab=request.source_tab,
                source_filter_code=request.source_filter_code,
                export_kind=None,
                download=None,
                workbook=None,
                preflight={
                    **preflight,
                    "early_preflight_decision": "legacy_ambiguous_skip",
                    "heavy_flow_required": False,
                    "heavy_flow_reason": "ambiguous_temporal_classification",
                    "non_materializable_reason": "ambiguous_temporal_classification",
                },
            )
            metadata_path = promo_folder / "metadata.json"
            self._write_json(metadata_path, asdict(metadata))
            drawer_reset = self._driver.reset_drawer(f"ambiguous__{slugify(card.promo_title)}")
            return PromoOutcome(
                promo_title=card.promo_title,
                timeline_block_index=candidate.index,
                timeline_short_period_text=candidate.short_period_text,
                timeline_preliminary_classification=candidate.preliminary_classification,
                status="ambiguous",
                promo_id=card.promo_id,
                period_id=None,
                promo_folder=str(promo_folder),
                blocker=None,
                metadata=metadata,
                card_path=str(card_path),
                metadata_path=str(metadata_path),
                drawer_reset=drawer_reset,
                early_preflight_duration_ms=early_preflight_duration_ms,
                **_preflight_outcome_fields(metadata.__dict__),
            )

        deep_flow_started = time.perf_counter()
        reusable = self._resolve_reusable_archive_record(request=request, card=card)
        if reusable is not None:
            return self._reused_from_archive(
                run_dir=run_dir,
                candidate=candidate,
                card=card,
                request=request,
                record=reusable,
                preflight=preflight,
                early_preflight_duration_ms=early_preflight_duration_ms,
                deep_flow_duration_ms=_elapsed_ms(deep_flow_started),
            )

        try:
            generate_state = self._driver.open_generate_screen(slugify(card.promo_title))
        except Exception as exc:
            return self._blocked_after_card(
                run_dir=run_dir,
                candidate=candidate,
                card=card,
                blocker=str(exc),
                request=request,
                preflight=preflight,
                early_preflight_duration_ms=early_preflight_duration_ms,
                deep_flow_duration_ms=_elapsed_ms(deep_flow_started),
                generate_screen_attempted=True,
            )
        try:
            ready_state = self._driver.generate_file_and_wait_ready(slugify(card.promo_title))
        except Exception as exc:
            return self._blocked_before_download(
                run_dir=run_dir,
                candidate=candidate,
                card=card,
                request=request,
                generate_state=generate_state,
                blocker=str(exc),
                preflight=preflight,
                early_preflight_duration_ms=early_preflight_duration_ms,
                deep_flow_duration_ms=_elapsed_ms(deep_flow_started),
                generate_screen_attempted=True,
            )
        try:
            download = self._driver.download_current_workbook()
        except Exception as exc:
            return self._blocked_before_download(
                run_dir=run_dir,
                candidate=candidate,
                card=card,
                request=request,
                generate_state=ready_state,
                blocker=str(exc),
                preflight=preflight,
                early_preflight_duration_ms=early_preflight_duration_ms,
                deep_flow_duration_ms=_elapsed_ms(deep_flow_started),
                generate_screen_attempted=True,
                download_attempted=True,
            )

        promo_folder = run_dir / "promos" / build_promo_folder_name(card.promo_id, download.period_id, card.promo_title)
        promo_folder.mkdir(parents=True, exist_ok=True)
        card_path = promo_folder / "card.json"
        card_png = promo_folder / "card.png"
        generate_png = promo_folder / "generate_screen.png"
        ready_png = promo_folder / "ready_signal.png"
        workbook_path = promo_folder / "workbook.xlsx"
        shutil.copy2(card.state_snapshot.screenshot, card_png)
        shutil.copy2(generate_state.screenshot, generate_png)
        shutil.copy2(ready_state.screenshot, ready_png)
        shutil.move(download.saved_path, workbook_path)
        self._write_json(card_path, asdict(card))

        inspection = inspect_workbook(workbook_path)
        inspection_path = promo_folder / "workbook_inspection.json"
        self._write_json(inspection_path, asdict(inspection))
        export_kind = classify_export_kind(download.original_suggested_filename, inspection.workbook_header_summary)
        metadata = build_metadata(
            card=card,
            trace_run_dir=request.output_root,
            source_tab=request.source_tab,
            source_filter_code=request.source_filter_code,
            export_kind=export_kind,
            download=DownloadArtifact(
                original_suggested_filename=download.original_suggested_filename,
                saved_filename=workbook_path.name,
                saved_path=str(workbook_path),
                period_id=download.period_id,
            ),
            workbook=inspection,
            preflight=preflight,
        )
        metadata_path = promo_folder / "metadata.json"
        self._write_json(metadata_path, asdict(metadata))
        drawer_reset = self._driver.reset_drawer(f"after_download__{slugify(card.promo_title)}")
        return PromoOutcome(
            promo_title=card.promo_title,
            timeline_block_index=candidate.index,
            timeline_short_period_text=candidate.short_period_text,
            timeline_preliminary_classification=candidate.preliminary_classification,
            status="downloaded",
            promo_id=card.promo_id,
            period_id=download.period_id,
            promo_folder=str(promo_folder),
            blocker=None,
            metadata=metadata,
            card_path=str(card_path),
            metadata_path=str(metadata_path),
            workbook_inspection_path=str(inspection_path),
            saved_path=str(workbook_path),
            original_suggested_filename=download.original_suggested_filename,
            export_kind=export_kind,
            drawer_reset=drawer_reset,
            early_preflight_duration_ms=early_preflight_duration_ms,
            deep_flow_duration_ms=_elapsed_ms(deep_flow_started),
            generate_screen_attempted=True,
            download_attempted=True,
            **_preflight_outcome_fields(preflight),
        )

    @staticmethod
    def _apply_outcome_preflight_summary(summary: CollectorRunSummary, outcome: PromoOutcome) -> None:
        metadata = outcome.metadata
        if bool(getattr(metadata, "ui_loaded_success", False)):
            summary.opened_drawer_count += 1
        if getattr(metadata, "early_preflight_decision", None):
            summary.shallow_status_checked_count += 1
        if bool(getattr(outcome, "heavy_flow_required", False)):
            summary.deep_workbook_flow_count += 1
        if getattr(outcome, "non_materializable_reason", None):
            summary.non_materializable_expected_count += 1
        if getattr(outcome, "non_materializable_reason", None) == "ended_without_download":
            summary.early_ended_no_download_count += 1
        if getattr(outcome, "early_preflight_decision", None) == "early_non_materializable":
            summary.early_non_materializable_count += 1
            summary.heavy_flow_avoided_count += 1
            summary.estimated_heavy_flow_avoided_count += 1
        if bool(getattr(outcome, "heavy_flow_required", False)):
            if str(getattr(metadata, "ui_status", "") or "") == "unknown":
                summary.unknown_status_full_flow_count += 1
            if str(getattr(metadata, "download_action_state", "") or "") == "available":
                summary.active_downloadable_full_flow_count += 1
        if outcome.generate_screen_attempted:
            summary.generate_screen_attempt_count += 1
        if outcome.download_attempted:
            summary.download_attempt_count += 1
        summary.early_preflight_duration_ms += int(outcome.early_preflight_duration_ms or 0)
        summary.deep_flow_duration_ms += int(outcome.deep_flow_duration_ms or 0)

    def _resolve_reusable_archive_record(
        self,
        *,
        request: PromoXlsxCollectorRequest,
        card: PromoCardData,
    ):
        archive_root_text = str(request.archive_root or "").strip()
        if not archive_root_text:
            return None
        archive_root = Path(archive_root_text)
        if not archive_root.exists():
            return None
        live_metadata = build_metadata(
            card=card,
            trace_run_dir=request.output_root,
            source_tab=request.source_tab,
            source_filter_code=request.source_filter_code,
            export_kind=None,
            download=None,
            workbook=None,
        )
        return resolve_reusable_campaign(
            archive_root=archive_root,
            metadata=live_metadata,
        )

    def _reused_from_archive(
        self,
        *,
        run_dir: Path,
        candidate: TimelineCandidate,
        card: PromoCardData,
        request: PromoXlsxCollectorRequest,
        record,
        preflight: dict[str, Any] | None = None,
        early_preflight_duration_ms: int = 0,
        deep_flow_duration_ms: int = 0,
    ) -> PromoOutcome:
        promo_folder = run_dir / "promos" / build_promo_folder_name(
            card.promo_id,
            record.metadata.period_id,
            card.promo_title,
        )
        promo_folder.mkdir(parents=True, exist_ok=True)
        card_path = promo_folder / "card.json"
        card_png = promo_folder / "card.png"
        self._write_json(card_path, asdict(card))
        shutil.copy2(card.state_snapshot.screenshot, card_png)

        inspection = None
        inspection_path: Path | None = None
        if record.workbook_inspection_path and Path(record.workbook_inspection_path).exists():
            inspection = WorkbookInspection(
                **json.loads(Path(record.workbook_inspection_path).read_text(encoding="utf-8"))
            )
            inspection_path = promo_folder / "workbook_inspection.json"
            inspection_path.write_text(
                json.dumps(asdict(inspection), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        workbook_path = Path(str(record.workbook_path))
        metadata = build_metadata(
            card=card,
            trace_run_dir=request.output_root,
            source_tab=request.source_tab,
            source_filter_code=request.source_filter_code,
            export_kind=record.metadata.export_kind,
            download=DownloadArtifact(
                original_suggested_filename=(
                    record.metadata.original_suggested_filename
                    or record.metadata.saved_filename
                    or workbook_path.name
                ),
                saved_filename=workbook_path.name,
                saved_path=str(workbook_path),
                period_id=record.metadata.period_id,
            ),
            workbook=inspection,
            preflight=preflight,
        )
        metadata_path = promo_folder / "metadata.json"
        self._write_json(metadata_path, asdict(metadata))
        (promo_folder / "archive_reuse.json").write_text(
            json.dumps(
                {
                    "archive_key": record.archive_key,
                    "archive_dir": record.archive_dir,
                    "reused_workbook_path": str(workbook_path),
                    "downloaded_at": record.downloaded_at,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        drawer_reset = self._driver.reset_drawer(f"after_archive_reuse__{slugify(card.promo_title)}")
        return PromoOutcome(
            promo_title=card.promo_title,
            timeline_block_index=candidate.index,
            timeline_short_period_text=candidate.short_period_text,
            timeline_preliminary_classification=candidate.preliminary_classification,
            status="reused_archive",
            promo_id=card.promo_id,
            period_id=record.metadata.period_id,
            promo_folder=str(promo_folder),
            blocker=None,
            metadata=metadata,
            card_path=str(card_path),
            metadata_path=str(metadata_path),
            workbook_inspection_path=str(inspection_path) if inspection_path is not None else record.workbook_inspection_path,
            saved_path=str(workbook_path),
            original_suggested_filename=metadata.original_suggested_filename,
            export_kind=metadata.export_kind,
            drawer_reset=drawer_reset,
            early_preflight_duration_ms=early_preflight_duration_ms,
            deep_flow_duration_ms=deep_flow_duration_ms,
            **_preflight_outcome_fields(preflight),
        )

    def _blocked_after_card(
        self,
        *,
        run_dir: Path,
        candidate: TimelineCandidate,
        card: PromoCardData,
        blocker: str,
        request: PromoXlsxCollectorRequest,
        preflight: dict[str, Any] | None = None,
        early_preflight_duration_ms: int = 0,
        deep_flow_duration_ms: int = 0,
        generate_screen_attempted: bool = False,
    ) -> PromoOutcome:
        promo_folder = run_dir / "promos" / build_promo_folder_name(card.promo_id, None, card.promo_title)
        promo_folder.mkdir(parents=True, exist_ok=True)
        card_path = promo_folder / "card.json"
        card_png = promo_folder / "card.png"
        failure_path = promo_folder / "failure.json"
        failure_png = promo_folder / "failure.png"
        shutil.copy2(card.state_snapshot.screenshot, card_png)
        shutil.copy2(card.state_snapshot.screenshot, failure_png)
        self._write_json(card_path, asdict(card))
        self._write_json(failure_path, {"status": "blocked_after_card", "blocker": blocker, "card": asdict(card)})
        metadata = build_metadata(
            card=card,
            trace_run_dir=request.output_root,
            source_tab=request.source_tab,
            source_filter_code=request.source_filter_code,
            export_kind=None,
            download=None,
            workbook=None,
            preflight=preflight,
        )
        metadata_path = promo_folder / "metadata.json"
        self._write_json(metadata_path, asdict(metadata))
        drawer_reset = self._driver.reset_drawer(f"blocked_after_card__{slugify(card.promo_title)}")
        return PromoOutcome(
            promo_title=card.promo_title,
            timeline_block_index=candidate.index,
            timeline_short_period_text=candidate.short_period_text,
            timeline_preliminary_classification=candidate.preliminary_classification,
            status="blocked_after_card",
            promo_id=card.promo_id,
            period_id=None,
            promo_folder=str(promo_folder),
            blocker=blocker,
            metadata=metadata,
            card_path=str(card_path),
            metadata_path=str(metadata_path),
            drawer_reset=drawer_reset,
            early_preflight_duration_ms=early_preflight_duration_ms,
            deep_flow_duration_ms=deep_flow_duration_ms,
            generate_screen_attempted=generate_screen_attempted,
            **_preflight_outcome_fields(preflight),
        )

    def _blocked_before_download(
        self,
        *,
        run_dir: Path,
        candidate: TimelineCandidate,
        card: PromoCardData,
        request: PromoXlsxCollectorRequest,
        generate_state: CollectorStateSnapshot,
        blocker: str,
        preflight: dict[str, Any] | None = None,
        early_preflight_duration_ms: int = 0,
        deep_flow_duration_ms: int = 0,
        generate_screen_attempted: bool = False,
        download_attempted: bool = False,
    ) -> PromoOutcome:
        promo_folder = run_dir / "promos" / build_promo_folder_name(card.promo_id, None, card.promo_title)
        promo_folder.mkdir(parents=True, exist_ok=True)
        card_path = promo_folder / "card.json"
        card_png = promo_folder / "card.png"
        failure_path = promo_folder / "failure.json"
        failure_png = promo_folder / "failure.png"
        shutil.copy2(card.state_snapshot.screenshot, card_png)
        shutil.copy2(generate_state.screenshot, failure_png)
        self._write_json(card_path, asdict(card))
        self._write_json(
            failure_path,
            {
                "status": "blocked_before_download",
                "blocker": blocker,
                "card": asdict(card),
                "generate_state": asdict(generate_state),
            },
        )
        metadata = build_metadata(
            card=card,
            trace_run_dir=request.output_root,
            source_tab=request.source_tab,
            source_filter_code=request.source_filter_code,
            export_kind=None,
            download=None,
            workbook=None,
            preflight=preflight,
        )
        metadata_path = promo_folder / "metadata.json"
        self._write_json(metadata_path, asdict(metadata))
        drawer_reset = self._driver.reset_drawer(f"blocked_before_download__{slugify(card.promo_title)}")
        return PromoOutcome(
            promo_title=card.promo_title,
            timeline_block_index=candidate.index,
            timeline_short_period_text=candidate.short_period_text,
            timeline_preliminary_classification=candidate.preliminary_classification,
            status="blocked_before_download",
            promo_id=card.promo_id,
            period_id=None,
            promo_folder=str(promo_folder),
            blocker=blocker,
            metadata=metadata,
            card_path=str(card_path),
            metadata_path=str(metadata_path),
            drawer_reset=drawer_reset,
            early_preflight_duration_ms=early_preflight_duration_ms,
            deep_flow_duration_ms=deep_flow_duration_ms,
            generate_screen_attempted=generate_screen_attempted,
            download_attempted=download_attempted,
            **_preflight_outcome_fields(preflight),
        )

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_timeline_candidate(block: TimelineBlockSnapshot) -> TimelineCandidate | None:
    lines = _clean_lines(block.raw_text)
    if not lines:
        return None
    filtered = [line for line in lines if line not in {"Автоакция.", "Акция."}]
    if not filtered:
        return None
    title = filtered[0]
    short_period_text = next((line for line in filtered[1:] if _looks_like_short_period(line)), None)
    preliminary = classify_timeline_period(short_period_text)
    return TimelineCandidate(
        index=block.index,
        title=title,
        short_period_text=short_period_text,
        preliminary_classification=preliminary,
        raw_text=block.raw_text,
    )


def classify_timeline_period(period_text: str | None) -> TemporalClassification:
    if not period_text:
        return "ambiguous"
    if any(keyword in period_text for keyword in ("мая", "апреля", "марта", "февраля", "января", "декабря")):
        return "ambiguous"
    return "ambiguous"


def extract_card_data(
    *,
    snapshot: CollectorStateSnapshot,
    fallback_title: str,
    source_tab: str,
    source_filter_code: str,
    reference_year: int,
) -> PromoCardData:
    del source_tab
    del source_filter_code
    lines = _clean_lines(snapshot.body_excerpt)
    promo_title = fallback_title
    matching_indexes = [idx for idx, line in enumerate(lines) if line == fallback_title]
    title_matched = bool(matching_indexes) or _title_matches_card(fallback_title, lines)
    if matching_indexes:
        title_index = matching_indexes[-1]
    else:
        title_index = next((idx for idx, line in enumerate(lines) if line == fallback_title), -1)
    if title_index < 0:
        title_index = 0
    card_lines = lines[title_index : title_index + 80]
    promo_period_text = next((line for line in card_lines[1:] if "→" in line), "")
    ui_status_evidence = classify_card_ui_status(
        card_lines=card_lines,
        snapshot=snapshot,
        title_matched=title_matched,
    )
    promo_status = (
        ui_status_evidence["ui_status_raw_labels"][0]
        if ui_status_evidence["ui_status_raw_labels"]
        else next((line for line in card_lines[1:] if line in {"Запланирована", "Акция идёт", "Завершилась"}), None)
    )
    promo_status_text = next((line for line in card_lines[1:] if line.startswith("Автоакция:")), None)
    promo_start_at, promo_end_at, confidence = parse_period_text(promo_period_text, reference_year=reference_year)
    temporal_classification = classify_temporal_status(promo_status)
    eligible_count, participating_count, excluded_count = extract_counts(card_lines)
    raw_card_excerpt = "\n".join(card_lines)
    promo_id = parse_promo_id(snapshot.url)
    return PromoCardData(
        calendar_url=snapshot.url,
        promo_id=promo_id,
        promo_title=promo_title,
        promo_period_text=promo_period_text,
        promo_start_at=promo_start_at,
        promo_end_at=promo_end_at,
        period_parse_confidence=confidence,
        temporal_classification=temporal_classification,
        temporal_confidence="high" if temporal_classification != "ambiguous" else "low",
        promo_status=promo_status,
        promo_status_text=promo_status_text,
        eligible_count=eligible_count,
        participating_count=participating_count,
        excluded_count=excluded_count,
        raw_card_excerpt=raw_card_excerpt,
        state_snapshot=snapshot,
        ui_status=ui_status_evidence["ui_status"],
        ui_status_confidence=ui_status_evidence["ui_status_confidence"],
        ui_status_raw_labels=ui_status_evidence["ui_status_raw_labels"],
        download_action_state=ui_status_evidence["download_action_state"],
        download_action_evidence=ui_status_evidence["download_action_evidence"],
        status_evidence_sources=ui_status_evidence["status_evidence_sources"],
        ui_loaded_success=ui_status_evidence["ui_loaded_success"],
        campaign_identity_match=ui_status_evidence["campaign_identity_match"],
        collector_ui_schema_version=COLLECTOR_UI_SCHEMA_VERSION,
    )


def classify_temporal_status(status: str | None) -> TemporalClassification:
    if not status:
        return "ambiguous"
    normalized = status.strip().lower()
    if "заверш" in normalized:
        return "past"
    if "заплан" in normalized:
        return "future"
    if "ид" in normalized:
        return "current"
    return "ambiguous"


def classify_card_ui_status(
    *,
    card_lines: list[str],
    snapshot: CollectorStateSnapshot,
    title_matched: bool,
) -> dict[str, Any]:
    ui_loaded_success = bool(title_matched and card_lines)
    normalized_lines = [_normalize_label(line) for line in card_lines]
    status_labels = _status_raw_labels(card_lines)
    status = "unknown"
    confidence = "low"
    sources: list[str] = []
    if any(_label_contains(normalized, _ENDED_LABEL_KEYWORDS) for normalized in normalized_lines):
        status = "ended"
        confidence = "high"
        sources.append("footer_label")
    elif any(_label_contains(normalized, _ACTIVE_LABEL_KEYWORDS) for normalized in normalized_lines):
        status = "active"
        confidence = "high"
        sources.append("footer_label")
    elif any(_label_contains(normalized, _FUTURE_LABEL_KEYWORDS) for normalized in normalized_lines):
        status = "future"
        confidence = "high"
        sources.append("footer_label")
    elif any(_label_contains(normalized, _PENDING_LABEL_KEYWORDS) for normalized in normalized_lines):
        status = "pending"
        confidence = "medium"
        sources.append("footer_label")

    if ui_loaded_success:
        sources.append("drawer_loaded")
    if title_matched:
        sources.append("title_match")

    if not ui_loaded_success:
        download_state = "ui_not_loaded"
        download_evidence = "drawer_not_loaded"
    elif snapshot.has_download:
        download_state = "available"
        download_evidence = "download_button_visible"
    elif snapshot.has_generate:
        download_state = "available"
        download_evidence = "generate_button_visible"
    elif snapshot.has_configure:
        download_state = "available"
        download_evidence = "configure_button_visible"
    else:
        download_state = "absent"
        download_evidence = "configure_generate_download_buttons_absent"
        sources.append("download_button_absent")

    return {
        "ui_status": status,
        "ui_status_confidence": confidence,
        "ui_status_raw_labels": status_labels,
        "download_action_state": download_state,
        "download_action_evidence": download_evidence,
        "status_evidence_sources": sorted(set(sources)),
        "ui_loaded_success": ui_loaded_success,
        "campaign_identity_match": bool(title_matched),
    }


def classify_collector_preflight(card: PromoCardData) -> dict[str, Any]:
    ui_status = str(card.ui_status or "unknown")
    confidence = str(card.ui_status_confidence or "low")
    download_action_state = str(card.download_action_state or "unknown")
    loaded = bool(card.ui_loaded_success)
    identity_matched = bool(card.campaign_identity_match)
    status_sources = set(card.status_evidence_sources or [])
    has_status_evidence = bool(status_sources & {"footer_label", "badge"})

    high_confidence_non_materializable = (
        ui_status == "ended"
        and confidence == "high"
        and download_action_state in {"absent", "disabled"}
        and loaded
        and identity_matched
        and has_status_evidence
    )
    if high_confidence_non_materializable:
        return {
            "early_preflight_decision": "early_non_materializable",
            "heavy_flow_required": False,
            "heavy_flow_reason": "high_confidence_non_materializable",
            "non_materializable_reason": "ended_without_download",
            "fallback_to_full_flow_reason": None,
            "collector_preflight_schema_version": COLLECTOR_PREFLIGHT_SCHEMA_VERSION,
        }

    fallback_reason = None
    if not loaded:
        fallback_reason = "ui_not_loaded"
    elif not identity_matched:
        fallback_reason = "campaign_identity_mismatch"
    elif confidence != "high":
        fallback_reason = "low_confidence_status"
    elif download_action_state in {"unknown", "ui_not_loaded"}:
        fallback_reason = "unclear_download_action"
    elif not has_status_evidence:
        fallback_reason = "missing_status_evidence"
    elif download_action_state == "available":
        fallback_reason = "download_action_available"
    else:
        fallback_reason = "conservative_full_flow"

    return {
        "early_preflight_decision": "full_flow",
        "heavy_flow_required": True,
        "heavy_flow_reason": "materializable_or_unclear",
        "non_materializable_reason": None,
        "fallback_to_full_flow_reason": fallback_reason,
        "collector_preflight_schema_version": COLLECTOR_PREFLIGHT_SCHEMA_VERSION,
    }


def parse_period_text(period_text: str, *, reference_year: int) -> tuple[str | None, str | None, str]:
    match = re.search(
        r"(?P<start_day>\d{1,2})\s+(?P<start_month>[А-Яа-яё]+)\s+(?P<start_time>\d{2}:\d{2})\s*[→-]\s*(?P<end_day>\d{1,2})\s+(?P<end_month>[А-Яа-яё]+)\s+(?P<end_time>\d{2}:\d{2})",
        period_text,
    )
    if not match:
        return None, None, "low"
    start_month = _MONTHS.get(match.group("start_month").lower())
    end_month = _MONTHS.get(match.group("end_month").lower())
    if start_month is None or end_month is None:
        return None, None, "low"
    if end_month < start_month:
        return None, None, "low"
    start_at = datetime(
        reference_year,
        start_month,
        int(match.group("start_day")),
        int(match.group("start_time").split(":")[0]),
        int(match.group("start_time").split(":")[1]),
    )
    end_at = datetime(
        reference_year,
        end_month,
        int(match.group("end_day")),
        int(match.group("end_time").split(":")[0]),
        int(match.group("end_time").split(":")[1]),
    )
    return start_at.strftime("%Y-%m-%dT%H:%M"), end_at.strftime("%Y-%m-%dT%H:%M"), "high"


def extract_counts(lines: list[str]) -> tuple[int | None, int | None, int | None]:
    text = "\n".join(lines)
    eligible_count = _extract_int(text, r"(\d+)\s+подходящих товара")
    participating_count = _extract_int(text, r"(\d+)\s+будут участвовать")
    excluded_count = _extract_int(text, r"(\d+)\s+исключено")
    participating_pair = re.search(r"Участвует\s+(\d+)\s+из\s+(\d+)\s+товаров", text)
    if participating_pair:
        participating_count = int(participating_pair.group(1))
        eligible_count = int(participating_pair.group(2))
    added_pair = re.search(r"Добавлено\s+(\d+)\s+из\s+(\d+)\s+товаров", text)
    if added_pair:
        participating_count = int(added_pair.group(1))
        eligible_count = int(added_pair.group(2))
    return eligible_count, participating_count, excluded_count


def _status_raw_labels(lines: list[str], *, limit: int = 5) -> list[str]:
    labels: list[str] = []
    for line in lines:
        normalized = _normalize_label(line)
        if not normalized:
            continue
        if (
            _label_contains(normalized, _ENDED_LABEL_KEYWORDS)
            or _label_contains(normalized, _ACTIVE_LABEL_KEYWORDS)
            or _label_contains(normalized, _FUTURE_LABEL_KEYWORDS)
            or _label_contains(normalized, _PENDING_LABEL_KEYWORDS)
        ):
            labels.append(_sanitize_label(line))
        if len(labels) >= limit:
            break
    return labels


def _label_contains(normalized: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in normalized for keyword in keywords)


def _normalize_label(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip().lower()


def _sanitize_label(value: str, *, limit: int = 80) -> str:
    sanitized = re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()
    return sanitized[:limit]


def _title_matches_card(fallback_title: str, lines: list[str]) -> bool:
    title = _normalize_label(fallback_title)
    if not title:
        return False
    joined = _normalize_label(" ".join(lines[:20]))
    if title in joined:
        return True
    tokens = [
        token
        for token in re.split(r"\W+", title, flags=re.UNICODE)
        if len(token) >= 4 and token not in {"акция", "автоакция"}
    ]
    if not tokens:
        return False
    matched = sum(1 for token in tokens if token in joined)
    return matched >= max(1, min(len(tokens), 2))


def classify_export_kind(original_filename: str | None, headers: list[str]) -> ExportKind:
    normalized_name = (original_filename or "").strip().lower()
    header_set = {header.strip() for header in headers if header and header.strip()}
    has_gating = bool(_GATING_HEADERS & header_set)
    if normalized_name.startswith("товары для исключения из акции") or has_gating:
        return "exclude_list_template"
    if normalized_name.startswith("все товары подходящие для акции") and header_set:
        return "eligible_items_report"
    if _COMMON_EXPORT_HEADERS.issubset(header_set) and not has_gating:
        return "eligible_items_report"
    return "unknown"


def inspect_workbook(path: Path) -> WorkbookInspection:
    workbook = load_workbook(filename=str(path), read_only=False, data_only=False)
    try:
        sheet_names = workbook.sheetnames
        hidden_sheets = any(workbook[sheet_name].sheet_state != "visible" for sheet_name in sheet_names)
        target_sheet, header_row_index, header_summary = _find_workbook_data_sheet(workbook)
        row_count = target_sheet.max_row
        col_count = target_sheet.max_column
        formulas_present = False
        distinct_statuses: set[str] = set()
        status_column = None
        for idx, header in enumerate(header_summary, start=1):
            if header == "Статус":
                status_column = idx
                break
        non_empty_rows = 0
        for row_index, row in enumerate(target_sheet.iter_rows(), start=1):
            values = [cell.value for cell in row]
            if any(value not in (None, "") for value in values):
                non_empty_rows += 1
            if any(getattr(cell, "data_type", None) == "f" for cell in row):
                formulas_present = True
            if row_index <= header_row_index:
                continue
            if status_column is not None and len(row) >= status_column:
                value = row[status_column - 1].value
                if isinstance(value, str) and value.strip():
                    distinct_statuses.add(value.strip())
        merged_cells_present = bool(target_sheet.merged_cells.ranges)
        workbook_has_date_fields = any(
            any(token in header.lower() for token in ("дата", "date", "period", "период"))
            for header in header_summary
        )
        return WorkbookInspection(
            workbook_sheet_names=sheet_names,
            workbook_row_count=row_count,
            workbook_col_count=col_count,
            workbook_header_summary=header_summary,
            workbook_has_date_fields=workbook_has_date_fields,
            workbook_item_status_distinct_values=sorted(distinct_statuses),
            hidden_sheets=hidden_sheets,
            formulas_present=formulas_present,
            merged_cells_present=merged_cells_present,
            rough_data_completeness_summary=(
                f"non_empty_rows={non_empty_rows}; data_rows={max(row_count - 1, 0)}; "
                f"header_columns={len(header_summary)}"
            ),
        )
    finally:
        workbook.close()


def _find_workbook_data_sheet(workbook: Any) -> tuple[Any, int, list[str]]:
    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        row_index, header_summary = _find_workbook_header_row(sheet)
        if header_summary and _COMMON_EXPORT_HEADERS.issubset(set(header_summary)):
            return sheet, row_index, header_summary
    first_sheet = workbook[workbook.sheetnames[0]]
    row_index, header_summary = _find_workbook_header_row(first_sheet)
    return first_sheet, row_index, header_summary


def _find_workbook_header_row(sheet: Any) -> tuple[int, list[str]]:
    for row_index, row in enumerate(sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 10), values_only=True), start=1):
        header_summary = [str(value).strip() for value in row if value not in (None, "")]
        header_set = set(header_summary)
        if _COMMON_EXPORT_HEADERS.issubset(header_set):
            return row_index, header_summary
    fallback = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))
    return 1, [str(value).strip() for value in fallback if value not in (None, "")]


def build_metadata(
    *,
    card: PromoCardData,
    trace_run_dir: str,
    source_tab: str,
    source_filter_code: str,
    export_kind: ExportKind | None,
    download: DownloadArtifact | None,
    workbook: WorkbookInspection | None,
    preflight: dict[str, Any] | None = None,
) -> PromoMetadata:
    preflight = preflight or {}
    return PromoMetadata(
        collected_at=_now_iso(),
        trace_run_dir=trace_run_dir,
        source_tab=source_tab,
        source_filter_code=source_filter_code,
        calendar_url=card.calendar_url,
        promo_id=card.promo_id,
        period_id=download.period_id if download else None,
        promo_title=card.promo_title,
        promo_period_text=card.promo_period_text,
        promo_start_at=card.promo_start_at,
        promo_end_at=card.promo_end_at,
        period_parse_confidence=card.period_parse_confidence,
        temporal_classification=card.temporal_classification,
        promo_status=card.promo_status,
        promo_status_text=card.promo_status_text,
        eligible_count=card.eligible_count,
        participating_count=card.participating_count,
        excluded_count=card.excluded_count,
        export_kind=export_kind,
        original_suggested_filename=download.original_suggested_filename if download else None,
        saved_filename=download.saved_filename if download else None,
        saved_path=download.saved_path if download else None,
        workbook_sheet_names=workbook.workbook_sheet_names if workbook else [],
        workbook_row_count=workbook.workbook_row_count if workbook else 0,
        workbook_col_count=workbook.workbook_col_count if workbook else 0,
        workbook_header_summary=workbook.workbook_header_summary if workbook else [],
        workbook_has_date_fields=workbook.workbook_has_date_fields if workbook else False,
        workbook_item_status_distinct_values=workbook.workbook_item_status_distinct_values if workbook else [],
        ui_status=card.ui_status,
        ui_status_confidence=card.ui_status_confidence,
        ui_status_raw_labels=list(card.ui_status_raw_labels),
        download_action_state=card.download_action_state,
        download_action_evidence=card.download_action_evidence,
        status_evidence_sources=list(card.status_evidence_sources),
        ui_loaded_success=card.ui_loaded_success,
        campaign_identity_match=card.campaign_identity_match,
        collector_ui_schema_version=card.collector_ui_schema_version,
        early_preflight_decision=preflight.get("early_preflight_decision"),
        heavy_flow_required=preflight.get("heavy_flow_required"),
        heavy_flow_reason=preflight.get("heavy_flow_reason"),
        non_materializable_reason=preflight.get("non_materializable_reason"),
        fallback_to_full_flow_reason=preflight.get("fallback_to_full_flow_reason"),
        collector_preflight_schema_version=str(
            preflight.get("collector_preflight_schema_version")
            or COLLECTOR_PREFLIGHT_SCHEMA_VERSION
        ),
    )


def build_promo_folder_name(promo_id: int | None, period_id: int | None, title: str) -> str:
    promo_part = str(promo_id) if promo_id is not None else "pending"
    period_part = str(period_id) if period_id is not None else "pending"
    return f"{promo_part}__{period_part}__{slugify(title)}"


def slugify(value: str) -> str:
    normalized = re.sub(r"[^\w]+", "-", value.lower(), flags=re.UNICODE)
    normalized = normalized.strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized or "pending"


def parse_promo_id(url: str) -> int | None:
    match = re.search(r"[?&]action=(\d+)", url)
    return int(match.group(1)) if match else None


def _looks_like_short_period(line: str) -> bool:
    return " - " in line and bool(re.search(r"\d", line))


def _extract_int(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


def _clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _now_iso() -> str:
    return datetime.now(BUSINESS_TIMEZONE).isoformat(timespec="seconds")


def _preflight_outcome_fields(preflight: dict[str, Any] | None) -> dict[str, Any]:
    preflight = preflight or {}
    return {
        "early_preflight_decision": preflight.get("early_preflight_decision"),
        "heavy_flow_required": preflight.get("heavy_flow_required"),
        "heavy_flow_reason": preflight.get("heavy_flow_reason"),
        "non_materializable_reason": preflight.get("non_materializable_reason"),
        "fallback_to_full_flow_reason": preflight.get("fallback_to_full_flow_reason"),
    }


def _elapsed_ms(started_perf: float) -> int:
    return max(0, int(round((time.perf_counter() - started_perf) * 1000)))
