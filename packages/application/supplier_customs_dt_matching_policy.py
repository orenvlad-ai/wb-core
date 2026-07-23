"""Versioned, deterministic matching policy for customs-declaration annex rows.

This policy is intentionally scoped to the tabular box-31 annex used by the
customs breakdown export.  It must not be reused by supplier-invoice identity,
which remains barcode-only.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from packages.application.supplier_invoice_parser import extract_iphone_model_keys


DT_ANNEX_MATCHING_POLICY_VERSION = "supplier_customs_dt_annex_strict_reconciliation_v2"

_ANTI_SPY_PATTERNS = (
    r"\banti[\s-]*spy\b",
    r"\bantispy\b",
    r"\bprivacy\b",
    r"анти[\s-]*шпион",
    r"防窥",
)
_MATTE_PATTERNS = (
    r"\bmatte\b",
    r"\bmatt\b",
    r"\bмат(?:овый|овая|овое|овые|ов|)\b",
    r"磨砂",
)
_CLEAN_PATTERNS = (
    r"\bclean\b",
    r"\bclear\b",
    r"\btransparent\b",
    r"прозрач",
    r"\bhd\b",
    r"高清",
)


def resolve_dt_annex_series_model(item: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve one annex row without fuzzy or cross-document inference."""

    article = str(item.get("article") or "").strip()
    model = str(item.get("source_model") or item.get("model") or "").strip()
    source_name = str(item.get("source_name") or "").strip()
    primary_evidence = [
        _field_evidence("article", article),
        _field_evidence("model", model),
    ]
    populated = [evidence for evidence in primary_evidence if evidence["text_present"]]

    if len(populated) == 2:
        left, right = populated
        if (
            not left["model_keys"]
            or not right["model_keys"]
            or left["series"] != right["series"]
            or left["model_keys"] != right["model_keys"]
            or left["conflicting_series"]
            or right["conflicting_series"]
        ):
            return _resolution(
                status="ambiguous",
                reason="article_model_conflict",
                series="",
                model_keys=(),
                evidence=primary_evidence,
            )
        return _resolution(
            status="confirmed",
            reason="article_model_semantically_equal",
            series=left["series"],
            model_keys=left["model_keys"],
            evidence=primary_evidence,
        )

    if len(populated) == 1:
        only = populated[0]
        if only["model_keys"] and only["series"] and not only["conflicting_series"]:
            return _resolution(
                status="confirmed",
                reason=f"{only['field']}_only",
                series=only["series"],
                model_keys=only["model_keys"],
                evidence=primary_evidence,
            )
        return _resolution(
            status="ambiguous" if only["conflicting_series"] else "unrecognized",
            reason="single_primary_field_incomplete",
            series="",
            model_keys=(),
            evidence=primary_evidence,
        )

    name_evidence = _field_evidence("source_name", source_name)
    evidence = [*primary_evidence, name_evidence]
    if name_evidence["model_keys"] and name_evidence["series"] and not name_evidence["conflicting_series"]:
        return _resolution(
            status="confirmed",
            reason="source_name_fallback",
            series=name_evidence["series"],
            model_keys=name_evidence["model_keys"],
            evidence=evidence,
        )
    return _resolution(
        status="ambiguous" if name_evidence["conflicting_series"] else "unrecognized",
        reason="series_or_model_missing",
        series="",
        model_keys=(),
        evidence=evidence,
    )


def canonical_dt_series(value: Any) -> str:
    """Map authoritative SKU group keys to the three DT series families."""

    key = str(value or "").strip().casefold()
    return {
        "clean": "clean",
        "clear": "clean",
        "no_frame_clean": "clean",
        "anti_spy": "anti_spy",
        "no_frame_anti_spy": "anti_spy",
        "matte": "matte",
        "no_frame_matte": "matte",
    }.get(key, "")


def normalized_model_key_set(value: Any) -> tuple[str, ...]:
    return tuple(sorted(set(extract_iphone_model_keys(value))))


def _field_evidence(field: str, value: str) -> dict[str, Any]:
    text = _normalize_marker_text(value)
    markers = _series_markers(text)
    model_keys = normalized_model_key_set(value)
    conflicting = len(markers) > 1
    series = next(iter(markers)) if len(markers) == 1 else ""
    if not markers and model_keys:
        series = "clean"
    return {
        "field": field,
        "text_present": bool(value),
        "series": series,
        "model_keys": model_keys,
        "conflicting_series": conflicting,
    }


def _normalize_marker_text(value: Any) -> str:
    text = str(value or "").casefold()
    text = text.replace("‑", "-").replace("–", "-").replace("—", "-")
    text = re.sub(r"[_/\\]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _series_markers(text: str) -> set[str]:
    markers: set[str] = set()
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in _ANTI_SPY_PATTERNS):
        markers.add("anti_spy")
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in _MATTE_PATTERNS):
        markers.add("matte")
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in _CLEAN_PATTERNS):
        markers.add("clean")
    return markers


def _resolution(
    *,
    status: str,
    reason: str,
    series: str,
    model_keys: tuple[str, ...],
    evidence: list[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "policy_version": DT_ANNEX_MATCHING_POLICY_VERSION,
        "status": status,
        "reason": reason,
        "series": series,
        "model_keys": list(model_keys),
        "evidence": [dict(item) for item in evidence],
    }
