"""Контракты bounded promo XLSX collector блока."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


EntryStrategy = Literal["direct_open"]
PeriodParseConfidence = Literal["high", "medium", "low"]
TemporalClassification = Literal["current", "future", "past", "ambiguous"]
PromoOutcomeStatus = Literal[
    "downloaded",
    "reused_archive",
    "skipped_past",
    "blocked_before_card",
    "blocked_after_card",
    "blocked_before_download",
    "ambiguous",
]
ExportKind = Literal["exclude_list_template", "eligible_items_report", "unknown"]


@dataclass(frozen=True)
class PromoXlsxCollectorRequest:
    output_root: str
    storage_state_path: str
    archive_root: str = ""
    start_url: str = "https://seller.wildberries.ru/dp-promo-calendar"
    source_tab: str = "Доступные"
    source_filter_code: str = "AVAILABLE"
    headless: bool = True
    hydration_attempt_budget: int = 2
    hydration_wait_sec: int = 45
    max_candidates: int | None = None
    max_downloads: int | None = None


@dataclass(frozen=True)
class CollectorStateSnapshot:
    ts: str
    label: str
    url: str
    title: str
    timeline_count: int
    overlay_count: int
    has_modal_close: bool
    modal_entry_count: int
    has_configure: bool
    has_generate: bool
    has_download: bool
    has_ready: bool
    has_cookie_accept: bool
    body_excerpt: str
    visible_tabs: list[str]
    screenshot: str


@dataclass(frozen=True)
class TimelineBlockSnapshot:
    index: int
    raw_text: str


@dataclass(frozen=True)
class TimelineCandidate:
    index: int
    title: str
    short_period_text: str | None
    preliminary_classification: TemporalClassification
    raw_text: str


@dataclass(frozen=True)
class ModalHandlingSummary:
    modal_present: bool
    modal_closed: bool
    modal_entry_count: int
    timeline_after_close: int | None = None
    overlay_after_close: int | None = None


@dataclass(frozen=True)
class HydrationAttemptSummary:
    attempt_num: int
    entry_strategy: EntryStrategy
    cookie_clicked: bool
    hydrated_success: bool
    title: str
    url: str
    timeline_count: int
    overlay_count: int
    time_to_hydrated_sec: float | None
    blocker: str | None = None
    modal_info: ModalHandlingSummary | None = None


@dataclass(frozen=True)
class PromoCardData:
    calendar_url: str
    promo_id: int | None
    promo_title: str
    promo_period_text: str
    promo_start_at: str | None
    promo_end_at: str | None
    period_parse_confidence: PeriodParseConfidence
    temporal_classification: TemporalClassification
    temporal_confidence: Literal["high", "medium", "low"]
    promo_status: str | None
    promo_status_text: str | None
    eligible_count: int | None
    participating_count: int | None
    excluded_count: int | None
    raw_card_excerpt: str
    state_snapshot: CollectorStateSnapshot


@dataclass(frozen=True)
class WorkbookInspection:
    workbook_sheet_names: list[str]
    workbook_row_count: int
    workbook_col_count: int
    workbook_header_summary: list[str]
    workbook_has_date_fields: bool
    workbook_item_status_distinct_values: list[str]
    hidden_sheets: bool
    formulas_present: bool
    merged_cells_present: bool
    rough_data_completeness_summary: str


@dataclass(frozen=True)
class DownloadArtifact:
    original_suggested_filename: str
    saved_path: str
    saved_filename: str
    period_id: int | None


@dataclass(frozen=True)
class DrawerResetSummary:
    clicked: bool
    selector: str
    overlay_before: int
    success: bool
    after_state_path: str | None
    blocker: str | None = None


@dataclass(frozen=True)
class PromoMetadata:
    collected_at: str
    trace_run_dir: str
    source_tab: str
    source_filter_code: str
    calendar_url: str
    promo_id: int | None
    period_id: int | None
    promo_title: str
    promo_period_text: str
    promo_start_at: str | None
    promo_end_at: str | None
    period_parse_confidence: PeriodParseConfidence
    temporal_classification: TemporalClassification
    promo_status: str | None
    promo_status_text: str | None
    eligible_count: int | None
    participating_count: int | None
    excluded_count: int | None
    export_kind: ExportKind | None
    original_suggested_filename: str | None
    saved_filename: str | None
    saved_path: str | None
    workbook_sheet_names: list[str] = field(default_factory=list)
    workbook_row_count: int = 0
    workbook_col_count: int = 0
    workbook_header_summary: list[str] = field(default_factory=list)
    workbook_has_date_fields: bool = False
    workbook_item_status_distinct_values: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PromoOutcome:
    promo_title: str
    timeline_block_index: int
    timeline_short_period_text: str | None
    timeline_preliminary_classification: TemporalClassification
    status: PromoOutcomeStatus
    promo_id: int | None
    period_id: int | None
    promo_folder: str
    blocker: str | None
    metadata: PromoMetadata
    card_path: str | None = None
    metadata_path: str | None = None
    workbook_inspection_path: str | None = None
    saved_path: str | None = None
    original_suggested_filename: str | None = None
    export_kind: ExportKind | None = None
    drawer_reset: DrawerResetSummary | None = None


@dataclass
class CollectorRunSummary:
    run_dir: str
    status: str
    started_at: str
    hydration_attempts: list[HydrationAttemptSummary] = field(default_factory=list)
    hydration_recoveries_used: int = 0
    timeline_candidates_found: int = 0
    card_confirmed_count: int = 0
    downloaded_count: int = 0
    reused_archive_count: int = 0
    skipped_past_count: int = 0
    blocked_before_card_count: int = 0
    blocked_after_card_count: int = 0
    blocked_before_download_count: int = 0
    ambiguous_count: int = 0
    export_kinds: list[ExportKind] = field(default_factory=list)
    promos: list[PromoOutcome] = field(default_factory=list)
