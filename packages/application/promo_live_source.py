"""Server-owned live source seam for promo-backed daily metrics."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
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
PROMO_DIAGNOSTIC_PHASES = (
    "promo_total",
    "browser_session_check",
    "collector_total",
    "promo_discovery",
    "promo_list_traversal",
    "archive_lookup",
    "archive_sync",
    "workbook_download",
    "workbook_reuse",
    "workbook_inspection",
    "metadata_validation",
    "price_truth_lookup",
    "price_truth_join",
    "source_payload_build",
    "acceptance_decision",
    "fallback_preserve",
)
PROMO_DIAGNOSTIC_COUNTERS = (
    "campaign_count",
    "current_promo_count",
    "future_promo_count",
    "past_promo_count",
    "ambiguous_promo_count",
    "archive_hit_count",
    "archive_miss_count",
    "workbook_download_count",
    "workbook_reuse_count",
    "collector_reuse_count",
    "validated_workbook_usable_count",
    "materializer_usable_count",
    "workbook_missing_count",
    "validation_failed_count",
    "metadata_only_count",
    "workbook_without_metadata_count",
    "corrupted_count",
    "ambiguous_date_count",
    "ended_without_download_count",
    "metadata_only_true_artifact_loss_count",
    "non_materializable_expected_count",
    "opened_drawer_count",
    "shallow_status_checked_count",
    "deep_workbook_flow_count",
    "early_ended_no_download_count",
    "early_non_materializable_count",
    "unknown_status_full_flow_count",
    "active_downloadable_full_flow_count",
    "download_attempt_count",
    "generate_screen_attempt_count",
    "heavy_flow_avoided_count",
    "estimated_heavy_flow_avoided_count",
    "early_preflight_duration_ms",
    "deep_flow_duration_ms",
    "timeline_card_seen_count",
    "timeline_status_classified_count",
    "drawer_open_avoided_count",
    "drawer_open_required_count",
    "timeline_unknown_full_flow_count",
    "timeline_non_materializable_count",
    "timeline_shallow_duration_ms",
    "drawer_open_duration_ms",
    "manifest_campaign_seen_count",
    "manifest_timeline_match_count",
    "manifest_match_low_confidence_count",
    "manifest_missing_for_card_count",
    "manifest_status_classified_count",
    "manifest_downloadability_classified_count",
    "manifest_drawer_avoid_count",
    "manifest_drawer_required_count",
    "manifest_unknown_full_flow_count",
    "manifest_low_confidence_full_flow_count",
    "manifest_load_duration_ms",
    "manifest_parse_duration_ms",
    "manifest_match_duration_ms",
    "complete_artifact_count",
    "incomplete_artifact_count",
    "metadata_valid_count",
    "metadata_invalid_count",
    "candidate_row_count",
    "eligible_row_count",
    "accepted_row_count",
    "skipped_row_count",
    "price_truth_missing_count",
    "price_truth_available_count",
)


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
        diagnostics = _new_promo_diagnostics(request)
        promo_total_phase = _start_promo_phase(diagnostics, "promo_total")
        run_dir = (
            self.runtime_dir
            / PROMO_RUNTIME_DIRNAME
            / f"{request.snapshot_date}__{self.now_factory().strftime('%Y%m%d_%H%M%S')}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        archive_root = promo_campaign_archive_root(self.runtime_dir)
        collector_summary = None
        browser_session_phase = _start_promo_phase(diagnostics, "browser_session_check")
        storage_state_path = request.storage_state_path or self.storage_state_path
        _set_promo_context(
            diagnostics,
            "browser_session",
            {
                "storage_state_configured": bool(str(storage_state_path or "").strip()),
                "storage_state_exists": bool(str(storage_state_path or "").strip())
                and Path(str(storage_state_path)).exists(),
            },
        )
        _finish_promo_phase(
            diagnostics,
            browser_session_phase,
            status="success" if str(storage_state_path or "").strip() else "missing",
            note_kind="storage_state_presence_only",
        )
        if request.snapshot_date == self.now_factory().date().isoformat():
            collector_request = PromoXlsxCollectorRequest(
                output_root=str(run_dir),
                storage_state_path=storage_state_path,
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
            collector_phase = _start_promo_phase(diagnostics, "collector_total")
            collector_summary = collector_block.execute(collector_request)
            _finish_promo_phase(
                diagnostics,
                collector_phase,
                status=str(getattr(collector_summary, "status", "") or "unknown"),
                note_kind="seller_portal_browser_collector",
            )
            _apply_collector_summary_diagnostics(diagnostics, collector_summary)
        else:
            collector_phase = _start_promo_phase(diagnostics, "collector_total")
            _finish_promo_phase(diagnostics, collector_phase, status="skipped", note_kind="archive_only")
            _append_promo_gap(diagnostics, "collector not executed for archive-only replay date")
        archive_sync_phase = _start_promo_phase(diagnostics, "archive_sync")
        sync_summary = sync_promo_campaign_archive(self.runtime_dir)
        _finish_promo_phase(diagnostics, archive_sync_phase, status="success")
        _set_promo_context(
            diagnostics,
            "archive_sync",
            {
                "scanned_promo_dirs": sync_summary.scanned_promo_dirs,
                "created_records": sync_summary.created_records,
                "updated_records": sync_summary.updated_records,
                "unchanged_records": sync_summary.unchanged_records,
            },
        )
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
            diagnostics=diagnostics,
        )
        _finish_promo_phase(diagnostics, promo_total_phase, status=str(getattr(result, "kind", "") or "unknown"))
        _finalize_promo_diagnostics(diagnostics)
        result = replace(result, diagnostics=dict(diagnostics))
        (run_dir / "derived_promo_live_source.json").write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return PromoLiveSourceEnvelope(result=result)


def _new_promo_diagnostics(request: PromoLiveSourceRequest) -> dict[str, Any]:
    started_at = _diag_now_iso()
    return {
        "schema_version": "promo_by_price_diagnostics_v1",
        "source_key": "promo_by_price",
        "snapshot_date": request.snapshot_date,
        "started_at": started_at,
        "finished_at": "",
        "duration_ms": None,
        "phase_summary": [],
        "counters": {key: None for key in PROMO_DIAGNOSTIC_COUNTERS},
        "fingerprints": {},
        "fallback": {
            "attempted_current_fetch": False,
            "candidate_accepted": None,
            "candidate_rejected": None,
            "invalid_reason": None,
            "fallback_reason": None,
            "preserved_snapshot_date": None,
            "preserved_snapshot_role": None,
            "preserved_snapshot_age_ms": None,
            "preserved_origin": None,
            "current_attempt_status": None,
            "current_attempt_semantic_status": None,
        },
        "dry_run_skip": {
            "would_skip_if_fingerprint_unchanged": False,
            "would_skip_reason": None,
            "would_not_skip_reason": "missing_comparison_fingerprint",
            "estimated_avoidable_ms": None,
        },
        "context": {
            "requested_count": len(request.nm_ids),
            "source_tab": request.source_tab,
            "source_filter_code": request.source_filter_code,
            "headless": bool(request.headless),
            "max_candidates": request.max_candidates,
            "max_downloads": request.max_downloads,
        },
        "phase_mapping": {
            "collector_total": "PromoXlsxCollectorBlock.execute overall browser collector runtime",
            "promo_discovery": "collector summary only; per-candidate timing is not emitted by the collector",
            "promo_list_traversal": "collector summary only; per-candidate timing is not emitted by the collector",
            "archive_lookup": "archive records load/filter for campaigns covering snapshot_date",
            "archive_sync": "sync promo collector run metadata/workbooks into promo_campaign_archive",
            "workbook_download": "collector summary only; download count is available, per-download timing is not emitted",
            "workbook_reuse": "collector summary only; collector reuse count is not materializer usability",
            "collector_preflight": "collector summary and per-campaign status preflight decisions before workbook/generate/download path",
            "campaign_manifest": "read-only Seller Portal promotions/timeline manifest used only for collector path control before drawer open",
            "workbook_inspection": "validated archive workbook row inspection/materialization",
            "metadata_validation": "archive metadata date-confidence/materialization check",
            "price_truth_lookup": "accepted prices_snapshot lookup for exact promo snapshot_date",
            "price_truth_join": "candidate rows joined with accepted price truth",
            "acceptance_decision": "deferred to sheet_vitrina_v1 temporal acceptance policy",
            "fallback_preserve": "deferred to sheet_vitrina_v1 temporal preservation policy",
        },
        "gaps": [
            "collector emits aggregate preflight/deep-flow timing; adapter-level selector timing still requires a separate adapter refactor",
            "collector workbook reuse means a reused archive candidate was reported; materializer usability is reported separately by artifact validation counters",
        ],
    }


def _start_promo_phase(diagnostics: dict[str, Any], phase_key: str) -> dict[str, Any]:
    return {
        "phase_key": phase_key,
        "started_at": _diag_now_iso(),
        "started_perf": time.perf_counter(),
    }


def _finish_promo_phase(
    diagnostics: dict[str, Any],
    phase: dict[str, Any],
    *,
    status: str,
    note_kind: str | None = None,
    error_kind: str | None = None,
) -> None:
    item = {
        "phase_key": str(phase.get("phase_key") or ""),
        "started_at": str(phase.get("started_at") or ""),
        "finished_at": _diag_now_iso(),
        "duration_ms": max(0, int(round((time.perf_counter() - float(phase.get("started_perf") or time.perf_counter())) * 1000))),
        "status": status,
    }
    if note_kind:
        item["note_kind"] = note_kind
    if error_kind:
        item["error_kind"] = error_kind
    diagnostics.setdefault("phase_summary", []).append(item)


def _append_instant_promo_phase(
    diagnostics: dict[str, Any],
    phase_key: str,
    *,
    status: str,
    note_kind: str,
) -> None:
    ts = _diag_now_iso()
    diagnostics.setdefault("phase_summary", []).append(
        {
            "phase_key": phase_key,
            "started_at": ts,
            "finished_at": ts,
            "duration_ms": 0,
            "status": status,
            "note_kind": note_kind,
        }
    )


def _set_promo_counter(
    diagnostics: dict[str, Any],
    key: str,
    value: Any,
    *,
    overwrite: bool = True,
) -> None:
    counters = diagnostics.setdefault("counters", {})
    if not isinstance(counters, dict):
        return
    if overwrite or counters.get(key) is None:
        counters[key] = value


def _set_promo_fingerprint(diagnostics: dict[str, Any], key: str, value: Any) -> None:
    fingerprints = diagnostics.setdefault("fingerprints", {})
    if isinstance(fingerprints, dict):
        fingerprints[key] = value


def _set_promo_context(diagnostics: dict[str, Any], key: str, value: Any) -> None:
    context = diagnostics.setdefault("context", {})
    if isinstance(context, dict):
        context[key] = value


def _append_promo_gap(diagnostics: dict[str, Any], value: str) -> None:
    gaps = diagnostics.setdefault("gaps", [])
    if isinstance(gaps, list) and value not in gaps:
        gaps.append(value)


def _apply_collector_summary_diagnostics(diagnostics: dict[str, Any], collector_summary: Any) -> None:
    promos = list(getattr(collector_summary, "promos", []) or [])
    _set_promo_counter(diagnostics, "campaign_count", int(getattr(collector_summary, "timeline_candidates_found", 0) or 0))
    _set_promo_counter(
        diagnostics,
        "current_promo_count",
        sum(1 for promo in promos if _promo_temporal_classification(promo) == "current"),
        overwrite=False,
    )
    _set_promo_counter(
        diagnostics,
        "future_promo_count",
        sum(1 for promo in promos if _promo_temporal_classification(promo) == "future"),
    )
    _set_promo_counter(
        diagnostics,
        "past_promo_count",
        int(getattr(collector_summary, "skipped_past_count", 0) or 0),
    )
    _set_promo_counter(
        diagnostics,
        "ambiguous_promo_count",
        int(getattr(collector_summary, "ambiguous_count", 0) or 0),
    )
    _set_promo_counter(
        diagnostics,
        "archive_hit_count",
        int(getattr(collector_summary, "reused_archive_count", 0) or 0),
        overwrite=False,
    )
    _set_promo_counter(
        diagnostics,
        "archive_miss_count",
        int(getattr(collector_summary, "downloaded_count", 0) or 0)
        + int(getattr(collector_summary, "blocked_after_card_count", 0) or 0)
        + int(getattr(collector_summary, "blocked_before_download_count", 0) or 0),
        overwrite=False,
    )
    _set_promo_counter(diagnostics, "workbook_download_count", int(getattr(collector_summary, "downloaded_count", 0) or 0))
    _set_promo_counter(diagnostics, "workbook_reuse_count", int(getattr(collector_summary, "reused_archive_count", 0) or 0))
    _set_promo_counter(diagnostics, "collector_reuse_count", int(getattr(collector_summary, "reused_archive_count", 0) or 0))
    for key in (
        "opened_drawer_count",
        "shallow_status_checked_count",
        "deep_workbook_flow_count",
        "early_ended_no_download_count",
        "early_non_materializable_count",
        "non_materializable_expected_count",
        "unknown_status_full_flow_count",
        "active_downloadable_full_flow_count",
        "download_attempt_count",
        "generate_screen_attempt_count",
        "heavy_flow_avoided_count",
        "estimated_heavy_flow_avoided_count",
        "early_preflight_duration_ms",
        "deep_flow_duration_ms",
        "timeline_card_seen_count",
        "timeline_status_classified_count",
        "drawer_open_avoided_count",
        "drawer_open_required_count",
        "timeline_unknown_full_flow_count",
        "timeline_non_materializable_count",
        "timeline_shallow_duration_ms",
        "drawer_open_duration_ms",
        "manifest_campaign_seen_count",
        "manifest_timeline_match_count",
        "manifest_match_low_confidence_count",
        "manifest_missing_for_card_count",
        "manifest_status_classified_count",
        "manifest_downloadability_classified_count",
        "manifest_drawer_avoid_count",
        "manifest_drawer_required_count",
        "manifest_unknown_full_flow_count",
        "manifest_low_confidence_full_flow_count",
        "manifest_load_duration_ms",
        "manifest_parse_duration_ms",
        "manifest_match_duration_ms",
    ):
        _set_promo_counter(diagnostics, key, int(getattr(collector_summary, key, 0) or 0), overwrite=False)
    _set_promo_counter(
        diagnostics,
        "workbook_missing_count",
        int(getattr(collector_summary, "blocked_before_card_count", 0) or 0)
        + int(getattr(collector_summary, "blocked_after_card_count", 0) or 0)
        + int(getattr(collector_summary, "blocked_before_download_count", 0) or 0),
        overwrite=False,
    )
    _set_promo_context(
        diagnostics,
        "collector_summary",
        {
            "status": str(getattr(collector_summary, "status", "") or ""),
            "hydration_attempts": len(getattr(collector_summary, "hydration_attempts", []) or []),
            "hydration_recoveries_used": int(getattr(collector_summary, "hydration_recoveries_used", 0) or 0),
            "timeline_candidates_found": int(getattr(collector_summary, "timeline_candidates_found", 0) or 0),
            "card_confirmed_count": int(getattr(collector_summary, "card_confirmed_count", 0) or 0),
            "downloaded_count": int(getattr(collector_summary, "downloaded_count", 0) or 0),
            "reused_archive_count": int(getattr(collector_summary, "reused_archive_count", 0) or 0),
            "blocked_before_card_count": int(getattr(collector_summary, "blocked_before_card_count", 0) or 0),
            "blocked_after_card_count": int(getattr(collector_summary, "blocked_after_card_count", 0) or 0),
            "blocked_before_download_count": int(getattr(collector_summary, "blocked_before_download_count", 0) or 0),
            "export_kinds": sorted(str(item) for item in (getattr(collector_summary, "export_kinds", []) or [])),
            "opened_drawer_count": int(getattr(collector_summary, "opened_drawer_count", 0) or 0),
            "shallow_status_checked_count": int(getattr(collector_summary, "shallow_status_checked_count", 0) or 0),
            "deep_workbook_flow_count": int(getattr(collector_summary, "deep_workbook_flow_count", 0) or 0),
            "early_non_materializable_count": int(getattr(collector_summary, "early_non_materializable_count", 0) or 0),
            "heavy_flow_avoided_count": int(getattr(collector_summary, "heavy_flow_avoided_count", 0) or 0),
            "early_preflight_duration_ms": int(getattr(collector_summary, "early_preflight_duration_ms", 0) or 0),
            "deep_flow_duration_ms": int(getattr(collector_summary, "deep_flow_duration_ms", 0) or 0),
            "timeline_card_seen_count": int(getattr(collector_summary, "timeline_card_seen_count", 0) or 0),
            "timeline_status_classified_count": int(getattr(collector_summary, "timeline_status_classified_count", 0) or 0),
            "drawer_open_avoided_count": int(getattr(collector_summary, "drawer_open_avoided_count", 0) or 0),
            "drawer_open_required_count": int(getattr(collector_summary, "drawer_open_required_count", 0) or 0),
            "timeline_unknown_full_flow_count": int(getattr(collector_summary, "timeline_unknown_full_flow_count", 0) or 0),
            "timeline_non_materializable_count": int(getattr(collector_summary, "timeline_non_materializable_count", 0) or 0),
            "timeline_evidence_confidence_counts": dict(getattr(collector_summary, "timeline_evidence_confidence_counts", {}) or {}),
            "timeline_skip_reason_counts": dict(getattr(collector_summary, "timeline_skip_reason_counts", {}) or {}),
            "drawer_fallback_reason_counts": dict(getattr(collector_summary, "drawer_fallback_reason_counts", {}) or {}),
            "timeline_shallow_duration_ms": int(getattr(collector_summary, "timeline_shallow_duration_ms", 0) or 0),
            "drawer_open_duration_ms": int(getattr(collector_summary, "drawer_open_duration_ms", 0) or 0),
            "manifest_loaded_success": bool(getattr(collector_summary, "manifest_loaded_success", False)),
            "manifest_source": str(getattr(collector_summary, "manifest_source", "") or "none"),
            "manifest_campaign_seen_count": int(getattr(collector_summary, "manifest_campaign_seen_count", 0) or 0),
            "manifest_timeline_match_count": int(getattr(collector_summary, "manifest_timeline_match_count", 0) or 0),
            "manifest_status_classified_count": int(getattr(collector_summary, "manifest_status_classified_count", 0) or 0),
            "manifest_downloadability_classified_count": int(getattr(collector_summary, "manifest_downloadability_classified_count", 0) or 0),
            "manifest_drawer_avoid_count": int(getattr(collector_summary, "manifest_drawer_avoid_count", 0) or 0),
            "manifest_drawer_required_count": int(getattr(collector_summary, "manifest_drawer_required_count", 0) or 0),
            "manifest_unknown_full_flow_count": int(getattr(collector_summary, "manifest_unknown_full_flow_count", 0) or 0),
            "manifest_low_confidence_full_flow_count": int(getattr(collector_summary, "manifest_low_confidence_full_flow_count", 0) or 0),
            "manifest_load_duration_ms": int(getattr(collector_summary, "manifest_load_duration_ms", 0) or 0),
            "manifest_parse_duration_ms": int(getattr(collector_summary, "manifest_parse_duration_ms", 0) or 0),
            "manifest_match_duration_ms": int(getattr(collector_summary, "manifest_match_duration_ms", 0) or 0),
        },
    )
    diagnostics["collector_preflight_campaigns"] = [
        _collector_preflight_campaign_diagnostic(promo)
        for promo in promos[:50]
    ]
    _set_promo_fingerprint(
        diagnostics,
        "promo_discovery_fingerprint",
        _json_fingerprint(
            [
                {
                    "status": str(getattr(promo, "status", "") or ""),
                    "promo_id": getattr(promo, "promo_id", None),
                    "period_id": getattr(promo, "period_id", None),
                    "temporal_classification": _promo_temporal_classification(promo),
                    "export_kind": str(getattr(promo, "export_kind", "") or ""),
                }
                for promo in promos
            ]
        ) if promos else None,
    )
    _append_instant_promo_phase(diagnostics, "promo_discovery", status="summary_only", note_kind="collector_summary_only")
    _append_instant_promo_phase(diagnostics, "promo_list_traversal", status="summary_only", note_kind="collector_summary_only")
    _append_instant_promo_phase(diagnostics, "campaign_manifest", status="summary_only", note_kind="collector_manifest_summary")
    _append_instant_promo_phase(diagnostics, "workbook_download", status="summary_only", note_kind="collector_summary_only")
    _append_instant_promo_phase(diagnostics, "workbook_reuse", status="summary_only", note_kind="collector_summary_only")


def _collector_preflight_campaign_diagnostic(promo: Any) -> dict[str, Any]:
    metadata = getattr(promo, "metadata", None)
    return {
        "campaign_id": str(getattr(promo, "promo_id", "") or "") or None,
        "promo_id": getattr(promo, "promo_id", None),
        "period_id": getattr(promo, "period_id", None),
        "status": str(getattr(promo, "status", "") or ""),
        "ui_status": str(getattr(metadata, "ui_status", "") or "unknown"),
        "ui_status_confidence": str(getattr(metadata, "ui_status_confidence", "") or "low"),
        "download_action_state": str(getattr(metadata, "download_action_state", "") or "unknown"),
        "timeline_status": str(getattr(metadata, "timeline_status", "") or "unknown"),
        "timeline_status_confidence": str(getattr(metadata, "timeline_status_confidence", "") or "low"),
        "timeline_classification_decision": getattr(metadata, "timeline_classification_decision", None),
        "timeline_period_text": getattr(metadata, "timeline_period_text", None),
        "timeline_evidence_sources": list(getattr(metadata, "timeline_evidence_sources", []) or []),
        "drawer_opened": getattr(metadata, "drawer_opened", None),
        "drawer_open_reason": getattr(metadata, "drawer_open_reason", None),
        "drawer_skip_reason": getattr(metadata, "drawer_skip_reason", None),
        "manifest_source": getattr(metadata, "manifest_source", None),
        "manifest_campaign_id": getattr(metadata, "manifest_campaign_id", None),
        "manifest_promo_id": getattr(metadata, "manifest_promo_id", None),
        "manifest_status": str(getattr(metadata, "manifest_status", "") or "unknown"),
        "manifest_status_confidence": str(getattr(metadata, "manifest_status_confidence", "") or "low"),
        "manifest_downloadability": str(getattr(metadata, "manifest_downloadability", "") or "unknown"),
        "manifest_match_confidence": str(getattr(metadata, "manifest_match_confidence", "") or "none"),
        "manifest_decision": getattr(metadata, "manifest_decision", None),
        "manifest_evidence_sources": list(getattr(metadata, "manifest_evidence_sources", []) or []),
        "manifest_drawer_skip_reason": getattr(metadata, "manifest_drawer_skip_reason", None),
        "manifest_drawer_required_reason": getattr(metadata, "manifest_drawer_required_reason", None),
        "manifest_match_duration_ms": int(getattr(metadata, "manifest_match_duration_ms", 0) or 0),
        "timeline_shallow_duration_ms": int(getattr(promo, "timeline_shallow_duration_ms", 0) or 0),
        "drawer_open_duration_ms": int(getattr(promo, "drawer_open_duration_ms", 0) or 0),
        "early_preflight_decision": getattr(metadata, "early_preflight_decision", None),
        "heavy_flow_required": getattr(metadata, "heavy_flow_required", None),
        "heavy_flow_reason": getattr(metadata, "heavy_flow_reason", None),
        "non_materializable_reason": getattr(metadata, "non_materializable_reason", None),
        "fallback_to_full_flow_reason": getattr(metadata, "fallback_to_full_flow_reason", None),
        "generate_screen_attempted": bool(getattr(promo, "generate_screen_attempted", False)),
        "download_attempted": bool(getattr(promo, "download_attempted", False)),
    }


def _promo_temporal_classification(promo: Any) -> str:
    metadata = getattr(promo, "metadata", None)
    return str(getattr(metadata, "temporal_classification", "") or "")


def _finalize_promo_diagnostics(diagnostics: dict[str, Any]) -> None:
    finished_at = _diag_now_iso()
    diagnostics["finished_at"] = finished_at
    promo_total = next(
        (
            item
            for item in diagnostics.get("phase_summary", [])
            if isinstance(item, dict) and item.get("phase_key") == "promo_total"
        ),
        None,
    )
    diagnostics["duration_ms"] = int(promo_total.get("duration_ms") or 0) if isinstance(promo_total, dict) else None
    counters = diagnostics.setdefault("counters", {})
    if isinstance(counters, dict):
        for key in PROMO_DIAGNOSTIC_COUNTERS:
            counters.setdefault(key, None)
    observed_phases = {
        str(item.get("phase_key") or "")
        for item in diagnostics.get("phase_summary", [])
        if isinstance(item, dict)
    }
    for phase_key in PROMO_DIAGNOSTIC_PHASES:
        if phase_key in observed_phases:
            continue
        if phase_key in {"acceptance_decision", "fallback_preserve"}:
            _append_instant_promo_phase(diagnostics, phase_key, status="deferred", note_kind="temporal_policy_layer")
        else:
            _append_instant_promo_phase(diagnostics, phase_key, status="not_instrumented", note_kind="not_available_without_refactor")


def _json_fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _diag_now_iso() -> str:
    return datetime.now(BUSINESS_TIMEZONE).replace(microsecond=0).isoformat()


def _default_now_factory() -> datetime:
    return datetime.now(BUSINESS_TIMEZONE)
