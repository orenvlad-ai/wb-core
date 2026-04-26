"""Контракты server-owned live source для promo-backed metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


PromoLiveSourceKind = Literal["success", "incomplete"]


@dataclass(frozen=True)
class PromoLiveSourceRequest:
    snapshot_date: str
    nm_ids: list[int]
    source_tab: str = "Доступные"
    source_filter_code: str = "AVAILABLE"
    storage_state_path: str = ""
    headless: bool = True
    hydration_attempt_budget: int = 2
    hydration_wait_sec: int = 45
    max_candidates: int | None = None
    max_downloads: int | None = None


@dataclass(frozen=True)
class PromoLiveSourceItem:
    snapshot_date: str
    nm_id: int
    promo_count_by_price: float
    promo_entry_price_best: float
    promo_participation: float


@dataclass(frozen=True)
class PromoLiveSourceSuccess:
    kind: Literal["success"]
    snapshot_date: str
    date_from: str
    date_to: str
    requested_count: int
    covered_count: int
    items: list[PromoLiveSourceItem]
    detail: str
    trace_run_dir: str
    current_promos: int
    current_promos_downloaded: int
    current_promos_blocked: int
    future_promos: int
    skipped_past_promos: int
    ambiguous_promos: int
    current_download_export_kinds: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromoLiveSourceIncomplete:
    kind: Literal["incomplete"]
    snapshot_date: str
    date_from: str
    date_to: str
    requested_count: int
    covered_count: int
    items: list[PromoLiveSourceItem]
    detail: str
    trace_run_dir: str
    current_promos: int
    current_promos_downloaded: int
    current_promos_blocked: int
    future_promos: int
    skipped_past_promos: int
    ambiguous_promos: int
    missing_nm_ids: list[int]
    current_download_export_kinds: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


PromoLiveSourceResult = PromoLiveSourceSuccess | PromoLiveSourceIncomplete


@dataclass(frozen=True)
class PromoLiveSourceEnvelope:
    result: PromoLiveSourceResult
