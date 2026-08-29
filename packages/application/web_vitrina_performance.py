"""Bounded, content-free browser performance telemetry for Web Vitrina."""

from __future__ import annotations

import json
import math
import sys
from typing import Any, Mapping


WEB_VITRINA_PERFORMANCE_CONTRACT_NAME = "web_vitrina_performance_v1"
WEB_VITRINA_PERFORMANCE_EVENT_NAME = "web_vitrina_browser_performance_v1"
WEB_VITRINA_PERFORMANCE_MAX_REQUEST_BYTES = 4 * 1024
WEB_VITRINA_PERFORMANCE_VIEWPORT_BUCKETS = frozenset(
    {"compact_560", "standard", "wide_1440"}
)
WEB_VITRINA_PERFORMANCE_TIMING_METRICS = frozenset(
    {
        "shell_ttfb_ms",
        "shell_download_ms",
        "shell_json_parse_ms",
        "shell_merge_render_ms",
        "shell_double_raf_paint_ms",
        "table_ttfb_ms",
        "table_download_ms",
        "table_json_parse_ms",
        "table_merge_render_ms",
        "table_double_raf_paint_ms",
    }
)
WEB_VITRINA_PERFORMANCE_BYTE_METRICS = frozenset(
    {
        "shell_transfer_bytes",
        "shell_encoded_body_bytes",
        "shell_decoded_body_bytes",
        "table_transfer_bytes",
        "table_encoded_body_bytes",
        "table_decoded_body_bytes",
    }
)
WEB_VITRINA_PERFORMANCE_METRICS = frozenset(
    WEB_VITRINA_PERFORMANCE_TIMING_METRICS
    | WEB_VITRINA_PERFORMANCE_BYTE_METRICS
)
_ROOT_FIELDS = frozenset(
    {"contract_name", "envelope_kind", "viewport_bucket", "metrics", "unavailable_metrics"}
)
_MAX_TIMING_MS = 600_000.0
_MAX_BYTE_COUNT = 1_000_000_000


def normalize_web_vitrina_performance_envelope(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate an exact allowlist-only RUM envelope and return journal-safe fields."""

    if set(payload) != _ROOT_FIELDS:
        raise ValueError("performance envelope fields must match the v1 contract")
    if payload.get("contract_name") != WEB_VITRINA_PERFORMANCE_CONTRACT_NAME:
        raise ValueError("performance contract_name is invalid")
    if payload.get("envelope_kind") != "page_load":
        raise ValueError("performance envelope_kind is invalid")

    viewport_bucket = payload.get("viewport_bucket")
    if viewport_bucket not in WEB_VITRINA_PERFORMANCE_VIEWPORT_BUCKETS:
        raise ValueError("performance viewport_bucket is invalid")

    metrics = payload.get("metrics")
    if not isinstance(metrics, Mapping) or set(metrics) != WEB_VITRINA_PERFORMANCE_METRICS:
        raise ValueError("performance metrics must match the v1 allowlist")
    unavailable = payload.get("unavailable_metrics")
    if not isinstance(unavailable, list):
        raise ValueError("performance unavailable_metrics must be an array")
    if len(unavailable) != len(set(unavailable)):
        raise ValueError("performance unavailable_metrics must be unique")
    unavailable_set = set(unavailable)
    if not unavailable_set.issubset(WEB_VITRINA_PERFORMANCE_METRICS):
        raise ValueError("performance unavailable_metrics contains an unknown metric")

    normalized_metrics: dict[str, int | float | None] = {}
    for name in sorted(WEB_VITRINA_PERFORMANCE_METRICS):
        value = metrics[name]
        if value is None:
            if name not in unavailable_set:
                raise ValueError(f"performance metric {name} must be marked unavailable")
            normalized_metrics[name] = None
            continue
        if name in unavailable_set:
            raise ValueError(f"performance metric {name} cannot be both available and unavailable")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"performance metric {name} must be numeric or null")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError(f"performance metric {name} must be finite and non-negative")
        if name in WEB_VITRINA_PERFORMANCE_TIMING_METRICS:
            if numeric > _MAX_TIMING_MS:
                raise ValueError(f"performance metric {name} exceeds the timing bound")
            normalized_metrics[name] = round(numeric, 3)
        else:
            if not numeric.is_integer() or numeric > _MAX_BYTE_COUNT:
                raise ValueError(f"performance metric {name} must be a bounded integer")
            normalized_metrics[name] = int(numeric)

    return {
        "event": WEB_VITRINA_PERFORMANCE_EVENT_NAME,
        "contract_name": WEB_VITRINA_PERFORMANCE_CONTRACT_NAME,
        "envelope_kind": "page_load",
        "viewport_bucket": viewport_bucket,
        "metrics": normalized_metrics,
        "unavailable_metrics": sorted(unavailable_set),
    }


def emit_web_vitrina_performance_event(payload: Mapping[str, Any]) -> None:
    """Emit one compact sanitized operational event without durable state."""

    print(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        file=sys.stderr,
        flush=True,
    )
