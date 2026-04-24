"""Server-owned live source seam for promo-backed daily metrics."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from packages.adapters.promo_xlsx_collector_block import (
    PlaywrightPromoCollectorDriver,
)
from packages.application.promo_campaign_archive import (
    materialize_promo_result_from_archive,
    promo_campaign_archive_root,
    sync_promo_campaign_archive,
)
from packages.application.promo_xlsx_collector_block import PromoXlsxCollectorBlock
from packages.contracts.promo_live_source import (
    PromoLiveSourceEnvelope,
    PromoLiveSourceRequest,
)
from packages.contracts.promo_xlsx_collector_block import PromoXlsxCollectorRequest


BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
PROMO_RUNTIME_DIRNAME = "promo_xlsx_collector_runs"


class PromoLiveSourceBlock:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        collector_block: PromoXlsxCollectorBlock | None = None,
        now_factory=None,
        storage_state_path: str | None = None,
        headless: bool = True,
        max_candidates: int | None = None,
        max_downloads: int | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.collector_block = collector_block
        self.now_factory = now_factory or _default_now_factory
        self.storage_state_path = (
            storage_state_path
            or str(os.environ.get("PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH", "")).strip()
        )
        self.headless = headless
        self.max_candidates = max_candidates
        self.max_downloads = max_downloads

    def execute(self, request: PromoLiveSourceRequest) -> PromoLiveSourceEnvelope:
        run_dir = (
            self.runtime_dir
            / PROMO_RUNTIME_DIRNAME
            / f"{request.snapshot_date}__{self.now_factory().strftime('%Y%m%d_%H%M%S')}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        archive_root = promo_campaign_archive_root(self.runtime_dir)
        collector_summary = None
        if request.snapshot_date == self.now_factory().date().isoformat():
            collector_request = PromoXlsxCollectorRequest(
                output_root=str(run_dir),
                storage_state_path=request.storage_state_path or self.storage_state_path,
                archive_root=str(archive_root),
                source_tab=request.source_tab,
                source_filter_code=request.source_filter_code,
                headless=request.headless if request.headless is not None else self.headless,
                hydration_attempt_budget=request.hydration_attempt_budget,
                hydration_wait_sec=request.hydration_wait_sec,
                max_candidates=request.max_candidates if request.max_candidates is not None else self.max_candidates,
                max_downloads=request.max_downloads if request.max_downloads is not None else self.max_downloads,
            )
            collector_block = self.collector_block or PromoXlsxCollectorBlock(PlaywrightPromoCollectorDriver(run_dir))
            collector_summary = collector_block.execute(collector_request)
        sync_summary = sync_promo_campaign_archive(self.runtime_dir)
        if collector_summary is None:
            detail_prefix = "collector_mode=archive_only"
            trace_run_dir = str(archive_root)
        else:
            detail_prefix = (
                f"collector_mode=live_refresh; "
                f"trace_run_dir={collector_summary.run_dir}; "
                f"collector_status={collector_summary.status}; "
                f"hydration_attempts={len(collector_summary.hydration_attempts)}; "
                f"archive_reuse_enabled=true"
            )
            trace_run_dir = collector_summary.run_dir
        result = materialize_promo_result_from_archive(
            runtime_dir=self.runtime_dir,
            snapshot_date=request.snapshot_date,
            requested_nm_ids=request.nm_ids,
            sync_summary=sync_summary,
            trace_run_dir=trace_run_dir,
            detail_prefix=detail_prefix,
        )
        (run_dir / "derived_promo_live_source.json").write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return PromoLiveSourceEnvelope(result=result)

def _default_now_factory() -> datetime:
    return datetime.now(BUSINESS_TIMEZONE)
