"""Lossless union of independently identified per-target WB observations."""
from __future__ import annotations
from collections import defaultdict
from collections.abc import Mapping
from packages.contracts.search_cluster_cleaner import CleanerError, Snapshot, Target, query_hash


def union_snapshot(target: Target, *, list_entry: Mapping | None, stats_queries: list[str] | None,
                   minus_queries: list[str] | None, observed_at: str,
                   source_times: Mapping[str, str], null_means_empty: frozenset[str] = frozenset()) -> Snapshot:
    """Input is the adapter's exact matching pair, never a fabricated missing pair.

    None is missing/unverified, [] is an explicit complete response. Unknown
    shapes and overlapping list states block this target without erasing data.
    Statistics alone does not assert current activity or universal coverage.
    """
    reasons: list[str] = []
    sources: dict[str, set[str]] = defaultdict(set)
    states: dict[str, str] = {}

    def rows(value, source):
        if value is None and source in null_means_empty:
            return []
        if not isinstance(value, list):
            reasons.append(f"{source}_missing_or_malformed")
            return []
        try:
            for q in value: query_hash(q)
        except CleanerError:
            reasons.append(f"{source}_invalid_query")
            return []
        if len(value) != len(set(value)):
            reasons.append(f"{source}_duplicate_query")
        return value

    if not isinstance(list_entry, Mapping):
        reasons.append("list_pair_missing")
    else:
        for state in ("active", "excluded", "archived"):
            for q in rows(list_entry.get(state), f"list_{state}"):
                if q in states and states[q] != state:
                    reasons.append("list_conflicting_states")
                states[q] = state
                sources[q].add("list")
    stats = rows(stats_queries, "statistics")
    for q in stats:
        states.setdefault(q, "statistics")
        sources[q].add("statistics")
    minus = rows(minus_queries, "minus")
    for q in minus:
        if states.get(q) == "active": reasons.append("list_minus_conflict")
        states[q] = "excluded"
        sources[q].add("minus")
    if isinstance(list_entry, Mapping) and isinstance(list_entry.get("excluded"), list):
        if set(list_entry["excluded"]) - set(minus): reasons.append("list_minus_drift")
    if not observed_at or any(not source_times.get(s) for s in ("list", "statistics", "minus")):
        reasons.append("source_timestamp_missing")
    return Snapshot(target, observed_at, dict(sorted(states.items())), tuple(sorted(set(minus))),
                    {q: tuple(sorted(s)) for q, s in sorted(sources.items())},
                    not reasons, tuple(sorted(set(reasons))), dict(source_times))
