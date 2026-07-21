"""Compatibility resolver for persisted temporal ads snapshots."""

from __future__ import annotations

from typing import Any, Mapping


VALID_ADS_KINDS = frozenset({"success", "empty", "missing", "error"})


def resolve_ads_snapshot_payload(
    payload: Any,
) -> tuple[Mapping[str, Any] | None, str]:
    """Return a valid ads result from either the nested or root envelope.

    Historical accepted snapshots are stored in both shapes.  A consumer must
    not treat a valid root payload as absent merely because ``result`` is not
    present.  Invalid envelopes remain invalid and are never converted to an
    empty/zero result.
    """

    if not isinstance(payload, Mapping):
        return None, "invalid"
    nested = payload.get("result")
    for candidate, origin in ((nested, "nested_result"), (payload, "root")):
        if not isinstance(candidate, Mapping):
            continue
        kind = str(candidate.get("kind") or "").strip().casefold()
        items = candidate.get("items")
        if kind not in VALID_ADS_KINDS or not isinstance(items, list):
            continue
        if any(not isinstance(item, Mapping) for item in items):
            continue
        return candidate, origin
    return None, "invalid"
