"""Application-слой bounded promo XLSX collector блока."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
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
                attempt = self._driver.attempt_hydration(attempt_num)
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

        if card.temporal_classification == "past":
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
            )

        reusable = self._resolve_reusable_archive_record(request=request, card=card)
        if reusable is not None:
            return self._reused_from_archive(
                run_dir=run_dir,
                candidate=candidate,
                card=card,
                request=request,
                record=reusable,
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
        )

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
        )

    def _blocked_after_card(
        self,
        *,
        run_dir: Path,
        candidate: TimelineCandidate,
        card: PromoCardData,
        blocker: str,
        request: PromoXlsxCollectorRequest,
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
    if matching_indexes:
        title_index = matching_indexes[-1]
    else:
        title_index = next((idx for idx, line in enumerate(lines) if line == fallback_title), -1)
    if title_index < 0:
        title_index = 0
    card_lines = lines[title_index : title_index + 80]
    promo_period_text = next((line for line in card_lines[1:] if "→" in line), "")
    promo_status = next((line for line in card_lines[1:] if line in {"Запланирована", "Акция идёт", "Завершилась"}), None)
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
) -> PromoMetadata:
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
