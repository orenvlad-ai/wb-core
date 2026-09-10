"""Additive, payload-free evidence for Ads and daily Finance attempts."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping


class SourceAttemptError(ValueError):
    """Keep the safe rejection code and its evidence across handler boundaries."""

    def __init__(self, code: str, diagnostics: Mapping[str, Any]) -> None:
        self.code = code
        self.diagnostics = deepcopy(dict(diagnostics))
        self.diagnostics.update(attempt_status="rejected", error_code=code)
        super().__init__(code)


def source_digest(payload: Any) -> str | None:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return None  # Malformed diagnostic input must not hide the source error.
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def new_attempt(source_key: str, snapshot_date: str) -> dict[str, Any]:
    return {
        "schema_version": "source_attempt_diagnostics_v1",
        "source_key": source_key,
        "source_date": snapshot_date,
        "attempt_started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        # This is filled only when a source response is actually observed.
        "source_observed_at": None,
        "attempt_status": "started",
        "error_code": None,
        "anomaly_codes": [],
    }


def observed_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def unknown_diagnostics(source_key: str) -> dict[str, Any]:
    """Old artifacts cannot prove zero pages, zero campaigns or fresh observation."""
    result = {
        "schema_version": "source_attempt_diagnostics_v1",
        "source_key": source_key,
        "attempt_status": "unknown",
        "source_observed_at": None,
        "source_digest": None,
        "counter_basis": "legacy_unknown",
    }
    if source_key == "ads_compact":
        result.update({key: None for key in (
            "expected_campaign_ids", "returned_campaign_ids", "missing_campaign_ids",
            "duplicate_campaign_ids", "batch_count", "batches",
        )})
    else:
        result.update({key: None for key in (
            "source_row_count", "exact_date_row_count", "target_row_count",
            "covered_count", "date_fallback_count", "date_discard_count",
        )})
        result["pagination"] = dict.fromkeys((
            "pages", "rrdid_start", "rrdid_end", "terminal_status", "complete",
        ))
    return result
